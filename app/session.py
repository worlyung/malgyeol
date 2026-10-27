"""받아쓰기 세션: 오디오 입력 -> 실시간 받아쓰기 -> 화자 배정 -> 저장.

스레드 구성
  reader : 오디오 청크를 받아 녹음 파일에 쓰고 VAD에 먹이고 큐에 넣는다 (가벼움)
  asr    : 큐를 비우며 Whisper를 돌린다 (무거움)
  face   : 화면을 보며 누가 나오는지 기록한다 (선택)
"""
from __future__ import annotations

import json
import os
import re
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable

import numpy as np

from . import config, notify, paths, summarize
from .asr.base import Word
from .asr.online import NativeStreamingASR, OnlineASR
from .asr.vad import StreamingVAD
from .audio import capture, recorder as rec
from .glossary import Glossary
from .speakers.face import FaceWatcher
from .speakers.registry import MemberRegistry
from .speakers import voice as voice_mod
from .speakers.context import ContextSpeaker, Designation, looks_like_designation
from .speakers import resolve as resolve_mod
from .speakers import intro as intro_mod

log = logging.getLogger(__name__)
SR = 16000
SENTENCE_END = tuple(".?!…。？！")
MAX_SEGMENT_SEC = 45.0
# 절전 모드에서 말이 계속 이어질 때 이만큼 쌓이면 한 번 인식합니다.
MAX_NO_PARTIAL_SEC = 20.0
MIN_SENTENCE_SEC = 2.5

# Whisper가 음악·잡음 구간에서 버릇처럼 만들어 내는 문장들.
# (유튜브 자막으로 학습한 흔적입니다. 회의에서는 나올 일이 없어 통째로 버립니다)
HALLUCINATION_PHRASES = {
    "다음 영상에서 만나요", "다음 영상에서 뵙겠습니다", "시청해주셔서 감사합니다",
    "시청해 주셔서 감사합니다", "구독과 좋아요 부탁드립니다", "구독과 좋아요",
    "감사합니다 다음 영상에서 만나요", "영상 편집", "한글자막by",
    "이 영상은 유료광고를 포함하고 있습니다", "지금까지 시청해주셔서 감사합니다",
    "mbc 뉴스", "kbs 뉴스", "sbs 뉴스", "끝까지 시청해주셔서 감사합니다",
}


# 방송 뉴스로 학습한 흔적: 이름 힌트를 주면 "홍길동 기자가 보도합니다" 같은 문장을 지어냅니다.
# 문장 안 어디에 끼어 있어도 떼어 냅니다. (회의에서 이런 말을 할 일은 거의 없습니다)
NEWS_HALLUCINATION = re.compile(
    r"[가-힣]{2,4}\s*기자가\s*보도합니다\.?|(?:MBC|KBS|SBS|YTN|JTBC)\s*뉴스\s*[가-힣]{2,4}입니다\.?", re.I
)


def _strip_hallucination(text: str) -> str:
    return re.sub(r"\s{2,}", " ", NEWS_HALLUCINATION.sub("", text)).strip()


def _is_hallucination(text: str) -> bool:
    normalized = "".join(ch for ch in text.lower() if ch.isalnum() or ch.isspace()).strip()
    compact = normalized.replace(" ", "")
    return any(compact == "".join(p.split()) for p in HALLUCINATION_PHRASES)


@dataclass
class Segment:
    id: int
    start: float
    end: float
    text: str
    raw_text: str = ""
    speaker_id: str | None = None
    speaker: str = "화자 미상"
    speaker_source: str = "none"        # face | voice | context | manual | none
    speaker_score: float = 0.0
    corrections: list[dict] = field(default_factory=list)
    words: list[dict] = field(default_factory=list)
    edited: bool = False
    # 방법별 화자 결과 (켜 둔 방법만으로 speaker 를 다시 정합니다 — speakers/resolve.py)
    cands: dict = field(default_factory=dict)
    anon: str = ""
    follow: bool = False
    checked: list[str] = field(default_factory=list)
    # 직접 지정할 때 함께 바꾼 것 ('지정 취소' 때 되돌림): 다른 문장들의 원래 화자, 새로 배운 성문
    undo: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class TranscriptStore:
    def __init__(self, session_dir: Path):
        self.dir = session_dir
        self.segments: list[Segment] = []
        self._lock = threading.RLock()
        self._dirty = False

    def add(self, seg: Segment) -> None:
        with self._lock:
            self.segments.append(seg)
            self._dirty = True

    def get(self, seg_id: int) -> Segment | None:
        with self._lock:
            for s in self.segments:
                if s.id == seg_id:
                    return s
        return None

    def update(self, seg_id: int, edited: bool = True, **fields) -> Segment | None:
        with self._lock:
            seg = self.get(seg_id)
            if not seg:
                return None
            for k, v in fields.items():
                if hasattr(seg, k):
                    setattr(seg, k, v)
            seg.edited = seg.edited or edited
            self._dirty = True
            return seg

    def delete(self, seg_id: int) -> bool:
        with self._lock:
            before = len(self.segments)
            self.segments = [s for s in self.segments if s.id != seg_id]
            self._dirty = len(self.segments) != before
            return self._dirty

    def rename_speaker(self, old: str, new: str, member_id: str | None = None) -> int:
        with self._lock:
            n = 0
            for s in self.segments:
                if s.speaker == old:
                    s.speaker = new
                    s.speaker_source = "manual"
                    if member_id:
                        s.speaker_id = member_id
                    n += 1
            self._dirty = n > 0
            return n

    def mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True

    def as_list(self) -> list[dict]:
        with self._lock:
            return [s.as_dict() for s in self.segments]

    def save(self, force: bool = False) -> None:
        with self._lock:
            if not (self._dirty or force):
                return
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / "transcript.tmp"
            tmp.write_text(
                json.dumps({"segments": self.as_list()}, ensure_ascii=False, indent=1), "utf-8"
            )
            tmp.replace(self.dir / "transcript.json")
            self._dirty = False

    def load(self) -> None:
        f = self.dir / "transcript.json"
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        with self._lock:
            self.segments = [
                Segment(**{k: v for k, v in s.items() if k in Segment.__annotations__})
                for s in data.get("segments", [])
            ]


class Session:
    def __init__(
        self,
        title: str,
        cfg: dict,
        glossary: Glossary,
        registry: MemberRegistry,
        on_event: Callable[[dict], None] | None = None,
        source_kind: str | None = None,
        device_index: int | None = None,
        file_path: str | None = None,
        start_sec: float = 0.0,
        referer: str = "",
        realtime: bool = False,
        engine=None,
    ):
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.title = title or time.strftime("회의 %Y-%m-%d %H:%M")
        self.cfg = cfg
        self.glossary = glossary
        self.registry = registry
        self.on_event = on_event or (lambda e: None)
        self.dir = paths.SESSIONS_DIR / self.id
        self.dir.mkdir(parents=True, exist_ok=True)

        acfg = cfg["asr"]
        scfg = cfg["speaker"]
        self.source_kind = source_kind or cfg["audio"]["source"]
        self.device_index = device_index if device_index is not None else cfg["audio"].get("device_index")
        self.file_path = file_path
        self.start_sec = start_sec
        self.referer = referer
        self.realtime = realtime

        self.engine = engine
        self.online: OnlineASR | None = None
        self.vad = StreamingVAD(
            threshold=float(acfg.get("vad_threshold", 0.5)),
            min_silence_sec=float(acfg.get("silence_finalize_sec", 0.7)),
        )
        self.store = TranscriptStore(self.dir)
        self.recorder: rec.WavRecorder | None = None
        self.face: FaceWatcher | None = None
        self.unknown_pool = voice_mod.UnknownSpeakerPool(threshold=float(scfg.get("voice_threshold", 0.58)))
        # 얼굴 / 목소리 / 맥락 스위치. 녹취 중에도 바꿀 수 있습니다.
        self.modes = resolve_mod.default_modes(scfg)

        self._pending: list[np.ndarray] = []
        self._pending_lock = threading.Condition()
        self._audio_time = 0.0
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()           # 사용자가 멈춤
        self._input_done = threading.Event()     # 입력이 끝남 (파일·주소 끝, 입력 오류)
        self._seg_counter = 0
        self._cur_words: list[Word] = []
        self._cur_face_member: str | None = None
        self._speech_since_run = False
        self.partial_enabled = True
        self._next_run_at = 0.0           # '보통' 모드: 다음 인식을 시작해도 되는 시각 (CPU 쉬는 시간)
        self._last_speaker: tuple[str, str | None, str] | None = None
        self._last_end = 0.0
        self._last_emb = None
        # 맥락 화자: 사회자가 지명한 사람을 다음 발언에 붙입니다 (AI API 키가 있을 때만)
        self.context: ContextSpeaker | None = None
        context_cfg = {**cfg["summary"], "provider": scfg.get("context_provider", "codex_cli")}
        if summarize.available(context_cfg):
            self.context = ContextSpeaker(cfg["summary"], scfg.get("context_model", ""),
                                          provider=context_cfg["provider"])
        self._designation: Designation | None = None
        self._backfilling = False
        self._anon_map: dict[str, str] = {}
        self._ctx_lock = threading.RLock()
        self._seg_embs: dict[int, np.ndarray | None] = {}
        self.state = "idle"            # idle | running | stopping | finished | error
        self.error: str | None = None
        self.started_at = time.time()
        self.partial_text = ""
        self.stats = {"iterations": 0, "avg_iter_sec": 0.0, "lag_sec": 0.0}

    # ------------------------------------------------------------------ 수명주기
    def start(self) -> None:
        acfg = self.cfg["asr"]
        if self.engine is None:
            from .asr import build_engine

            self.engine = build_engine(acfg)

        # 성능 모드에 따라 '얼마나 자주 다시 인식할지'가 달라집니다.
        # 같은 소리를 여러 번 인식하는 것이 CPU를 가장 많이 쓰는 부분이라,
        # PC 속도에 맞춰 간격을 두거나(보통) 아예 말이 끊길 때만 인식하면(절전) 부하가 크게 줄어듭니다.
        mode = str(acfg.get("performance", "balanced")).lower()
        self.partial_enabled = mode != "low"
        interval = self._interval_for(mode)

        processor = NativeStreamingASR if getattr(self.engine, "native_streaming", False) else OnlineASR
        self.online = processor(
            self.engine,
            prompt_fn=self._make_prompt,
            min_chunk_sec=interval,
            trim_sec=float(acfg.get("buffer_trim_sec", 14.0)),
        )
        self.source = capture.make_source(
            self.source_kind, self.device_index, self.file_path,
            self.start_sec, self.referer, self.realtime
        )
        if self.cfg["audio"].get("record", True):
            self.recorder = rec.WavRecorder(self.dir / "audio.wav")

        if self.modes["face"]:
            self.start_face()

        self.source.start()
        self.state = "running"
        self._save_meta()

        for target, name in ((self._reader_loop, "reader"), (self._asr_loop, "asr"),
                             (self._keep_awake, "awake")):
            t = threading.Thread(target=target, name=f"session-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        self._emit({"type": "status", "status": self.status()})

    # '보통' 모드: 인식이 끝나면 곧바로 다음 인식을 합니다. (빠른 PC는 최소 2초 간격)
    # 인식 한 번은 소리 길이와 관계없이 30초 창을 통째로 처리해 이 PC에서 6초쯤 걸립니다(측정).
    # 사이에 쉬는 시간을 두면(예전 1.6배 규칙) 미리보기가 30초 가까이 멈춰 있고 문장이 한꺼번에 올라와
    # 없앴습니다. 다른 프로그램이 느려지지 않도록 하는 일은 '낮은 우선순위'가 맡습니다.
    # (예전 '빠름'은 이것과 같아 '보통'으로 합쳤습니다)
    MIN_SPACING_SEC = 2.0

    def _keep_awake(self) -> None:
        """받아쓰는 동안 PC 절전과 화면 꺼짐을 막습니다. (꺼지면 소리가 끊기고, 화면 얼굴 인식도 멈춤)

        Windows 는 이 요청을 '부른 스레드'에 묶어 두므로, 끝날 때까지 이 스레드가 붙들고 있다가 풉니다.
        PC 전원 설정 자체는 바꾸지 않습니다.
        """
        if os.name != "nt":
            return
        import ctypes

        kernel = ctypes.windll.kernel32
        es_continuous, es_system, es_display = 0x80000000, 0x00000001, 0x00000002
        if not kernel.SetThreadExecutionState(es_continuous | es_system | es_display):
            log.warning("절전 막기를 켜지 못했습니다")
            return
        self._stop.wait()
        kernel.SetThreadExecutionState(es_continuous)

    def _interval_for(self, mode: str) -> float:
        return 60.0 if mode == "low" else self.MIN_SPACING_SEC     # 'fast'(예전 설정)는 '보통'으로 봅니다

    def _rest_after(self, iter_sec: float) -> float:
        """방금 인식에 iter_sec 가 걸렸을 때, 다음 인식까지 기다릴 시간. 빠른 PC만 최소 간격을 지킵니다."""
        if self.cfg["asr"].get("performance") == "low" or not getattr(self.source, "realtime", True):
            return 0.0                         # 절전은 말이 끊길 때만 인식 · 파일은 최대한 빨리
        return max(0.0, self.MIN_SPACING_SEC - iter_sec)

    def apply_performance(self, mode: str) -> None:
        """받아쓰는 도중에 성능 모드를 바꿉니다. (다음 인식부터 바로 적용)"""
        mode = str(mode or "balanced").lower()
        self.cfg["asr"]["performance"] = mode
        self.partial_enabled = mode != "low"
        if self.online:
            self.online.min_chunk_sec = self._interval_for(mode)
        if mode == "low" and self.partial_text:
            self.partial_text = ""
            self._emit({"type": "partial", "text": "", "t": round(self._audio_time, 2)})
        log.info("성능 모드 변경: %s", mode)

    def start_face(self) -> None:
        """화면 감시를 켭니다. (파일 받아쓰기는 화면과 소리가 맞지 않아 쓰지 않습니다)"""
        if self.face or self.source_kind == "file":
            return
        self.face = FaceWatcher(
            self.registry,
            self.cfg["speaker"],
            clock=lambda: self._audio_time,
            on_event=self._on_face,
        )
        self.face.start()

    # 멈출 때 작업 스레드(입력·인식)가 실제로 끝날 때까지 기다리는 최대 시간.
    # 느린 PC는 마지막 인식 한 번이 30초를 넘기도 하므로 넉넉히 기다리고, 그동안은 'stopping'을 유지합니다.
    # (스레드가 고장 나 영영 끝나지 않으면 이 시간 뒤 오류로 표시하고 마무리합니다)
    STOP_WAIT_SEC = 600

    def stop(self) -> None:
        if self.state in {"finished", "stopping"}:
            return
        was_error = self.state == "error"
        self.state = "stopping"
        self._emit({"type": "status", "status": self.status()})
        self._stop.set()
        try:
            self.source.stop()
        except Exception:  # noqa: BLE001
            log.debug("오디오 소스 종료 오류", exc_info=True)
        with self._pending_lock:
            self._pending_lock.notify_all()
        deadline = time.monotonic() + self.STOP_WAIT_SEC
        for t in self._threads:
            if t is threading.current_thread():
                continue
            while t.is_alive() and time.monotonic() < deadline:
                t.join(timeout=1)
                with self._pending_lock:
                    self._pending_lock.notify_all()
        stuck = [t.name for t in self._threads if t.is_alive() and t is not threading.current_thread()]
        if stuck:
            log.error("받아쓰기 스레드가 %d초 안에 끝나지 않았습니다: %s", self.STOP_WAIT_SEC, stuck)
            self.error = self.error or "마지막 문장을 정리하지 못하고 마쳤습니다 (작업이 끝나지 않음)"
        if self.face:
            self.face.stop()
        if self.context:
            self.context.close(wait=False)   # AI 답을 기다리지 않습니다 (늦게 오면 그때 반영)
        if self.recorder:
            self.recorder.close()
        self.store.save(force=True)
        # 입력 오류 등으로 끝났으면 '오류'를 그대로 보여 줍니다 (완료로 덮지 않음)
        self.state = "error" if (was_error or stuck or (self.error and self.state == "error")) else "finished"
        self._save_meta()
        self._remove_upload()
        self._emit({"type": "status", "status": self.status()})

    def _remove_upload(self) -> None:
        """'저장된 파일'로 올린 원본은 받아쓰기가 끝나면 지웁니다. (녹음은 audio.wav 로 따로 남아 있음)"""
        try:
            path = Path(self.file_path or "").resolve()
            uploads = (paths.DATA_DIR / "uploads").resolve()
            if self.source_kind == "file" and uploads in path.parents and path.is_file():
                path.unlink()
        except Exception:  # noqa: BLE001
            log.debug("업로드 원본 정리 실패", exc_info=True)

    def _finish_by_itself(self) -> None:
        """입력이 끝나(파일·주소 끝, 입력 오류) 인식까지 마치면 사용자가 멈추지 않아도 마무리합니다.

        stop() 은 인식 스레드가 끝나기를 기다리므로, 인식 스레드 안에서 부르지 않고 따로 띄웁니다.
        """
        if not self._stop.is_set():
            threading.Thread(target=self.stop, name="session-finish", daemon=True).start()

    # ------------------------------------------------------------------ 스레드
    def _reader_loop(self) -> None:
        try:
            for chunk in self.source.chunks():
                if self._stop.is_set():
                    break
                if self.recorder:
                    self.recorder.append(chunk)
                self.vad.feed(chunk)
                if self.vad.speaking:
                    self._speech_since_run = True      # 말이 없었으면 인식을 건너뜁니다
                self._audio_time += len(chunk) / SR
                with self._pending_lock:
                    self._pending.append(chunk)
                    self._pending_lock.notify()
                    # 파일 입력은 ASR이 못 따라오면 잠시 기다린다
                    if not self.source.realtime:
                        while (
                            sum(len(c) for c in self._pending) > SR * 8
                            and not self._stop.is_set()
                        ):
                            self._pending_lock.wait(0.2)
        except Exception as exc:  # noqa: BLE001
            self.error = f"오디오 입력 오류: {exc}"
            self.state = "error"
            log.exception("오디오 입력 오류")
            self._emit({"type": "status", "status": self.status()})
        finally:
            self._input_done.set()          # 파일 끝·입력 오류: 인식 스레드가 남은 소리를 처리하고 마무리합니다
            with self._pending_lock:
                self._pending_lock.notify_all()

    def _drain(self) -> bool:
        with self._pending_lock:
            if not self._pending:
                self._pending_lock.wait(0.1)
            chunks, self._pending = self._pending, []
            self._pending_lock.notify_all()
        if not chunks:
            return False
        self.online.insert_audio(np.concatenate(chunks))
        return True

    def _asr_loop(self) -> None:
        reader_alive = True
        try:
            while True:
                self._drain()
                reader_alive = any(t.name == "session-reader" and t.is_alive() for t in self._threads)
                # 사용자가 멈췄거나 입력이 끝났고(파일 끝·입력 오류), 입력 스레드도 끝났으면 마지막 인식 후 마칩니다
                finishing = (self._stop.is_set() or self._input_done.is_set()) and not reader_alive

                if finishing:
                    with self._pending_lock:
                        leftover = bool(self._pending)
                    if leftover:
                        continue
                    self._run_iteration(final=True)
                    break

                if self.online.buffer_sec <= 0.05:
                    continue

                utterance_done = self.vad.utterance_ended() and self.online.buffer_sec > 0.4
                if utterance_done:
                    self.vad.reset_silence()
                    if self._speech_since_run or self.online.buffer_sec > 1.0:
                        self._run_iteration(final=True)
                    else:
                        self.online.reset(offset=self.online.buffer_end_time)   # 무음뿐이면 버림
                        self._speech_since_run = False
                elif self.online.ready():
                    # 말이 없었던 구간은 인식할 필요가 없습니다 (CPU 절약)
                    if not self._speech_since_run:
                        continue
                    if time.monotonic() < self._next_run_at:   # 인식 사이에 CPU를 쉬게 합니다
                        continue
                    if self.partial_enabled or self.online.buffer_sec >= MAX_NO_PARTIAL_SEC:
                        self._run_iteration(final=False)
        except Exception as exc:  # noqa: BLE001
            self.error = f"받아쓰기 오류: {exc}"
            self.state = "error"
            log.exception("받아쓰기 루프 오류")
        finally:
            self._close_segment(force=True)
            self.store.save(force=True)
            self._emit({"type": "status", "status": self.status()})
            self._finish_by_itself()

    def _run_iteration(self, final: bool) -> None:
        self._speech_since_run = False
        committed, partial = self.online.process_iter(final=final)
        self.stats["iterations"] += 1
        n = self.stats["iterations"]
        self.stats["avg_iter_sec"] = (
            self.stats["avg_iter_sec"] * (n - 1) + self.online.last_iteration_sec
        ) / n
        self.stats["lag_sec"] = round(max(0.0, self._audio_time - self.online.buffer_end_time), 2)
        self._next_run_at = time.monotonic() + self._rest_after(self.online.last_iteration_sec)

        if committed:
            self._cur_words.extend(committed)
            self._maybe_close_segment(final)
        elif final:
            self._close_segment(force=True)

        text = "".join(w.text for w in partial).strip()
        if text != self.partial_text:
            self.partial_text = text
            self._emit({"type": "partial", "text": text, "t": round(self._audio_time, 2)})
        if final and not text:
            self.partial_text = ""
            self._emit({"type": "partial", "text": "", "t": round(self._audio_time, 2)})
        self._emit({"type": "stats", "stats": dict(self.stats), "audio_time": round(self._audio_time, 2)})

    # ------------------------------------------------------------------ 문장/화자
    def _maybe_close_segment(self, final: bool) -> None:
        """문장 부호를 만날 때마다 한 문장씩 끊어 녹취록에 넣는다."""
        if not self._cur_words:
            return
        speaker_changed = self._face_changed()

        while True:
            idx = self._sentence_end_index()
            if idx is None:
                break
            self._close_segment(self._cur_words[: idx + 1])
            self._cur_words = self._cur_words[idx + 1 :]

        if not self._cur_words:
            return
        dur = self._cur_words[-1].end - self._cur_words[0].start
        if final or dur >= MAX_SEGMENT_SEC or (speaker_changed and dur >= 2.0):
            words, self._cur_words = self._cur_words, []
            self._close_segment(words)

    def _sentence_end_index(self) -> int | None:
        if not self._cur_words:
            return None
        start = self._cur_words[0].start
        for i, w in enumerate(self._cur_words):
            if w.text.strip().endswith(SENTENCE_END) and w.end - start >= MIN_SENTENCE_SEC:
                return i
        return None

    def _face_changed(self) -> bool:
        if not self.face:
            return False
        cur = self.face.current
        now = cur.member_id if cur else None
        changed = bool(self._cur_face_member and now and now != self._cur_face_member)
        if now:
            self._cur_face_member = now
        return changed

    def _close_segment(self, words: list[Word] | None = None, force: bool = False) -> None:
        if words is None:
            words, self._cur_words = self._cur_words, []
        self._cur_face_member = None
        if not words:
            return
        raw = "".join(w.text for w in words).strip()
        if not raw:
            return
        # 말한 시간에 비해 글자가 지나치게 많으면 환각으로 보고 버린다
        # (사람의 한국어 발화는 초당 10자를 넘기 어렵다)
        span = max(0.4, words[-1].end - words[0].start)
        if len(raw) / span > 14:
            log.info("환각으로 보이는 구간을 건너뜀: %.1fs / %d자", span, len(raw))
            return
        if _is_hallucination(raw):
            log.info("환각 상용구를 건너뜀: %s", raw)
            return
        cleaned = _strip_hallucination(raw)
        if cleaned != raw:
            log.info("지어낸 뉴스 문장을 떼어 냄: %s", raw[:80])
            if not cleaned:
                return
            raw = cleaned

        gcfg = self.cfg["glossary"]
        if gcfg.get("correction_enabled", True):
            text, corrections = self.glossary.correct(raw, float(gcfg.get("correction_threshold", 0.72)))
        else:
            text, corrections = raw, []

        self._seg_counter += 1
        seg = Segment(
            id=self._seg_counter,
            start=round(words[0].start, 2),
            end=round(words[-1].end, 2),
            text=text,
            raw_text=raw,
            corrections=corrections,
            words=[{"s": round(w.start, 2), "e": round(w.end, 2), "w": w.text} for w in words],
        )
        emb = self._assign_speaker(seg)
        with self._ctx_lock:
            self._seg_embs[seg.id] = emb
            back: list[Segment] = []
            if self.modes["context"]:
                names = self.registry.names()
                called = self._rule_call(seg, names)
                if self._designation and (called or looks_like_designation(seg.text, self.registry.names())):
                    self._designation = None       # 사회자가 다시 지명하는 중이면 이전 지명은 끝
                me = intro_mod.find_self_intro(seg.text, names)
                if me:
                    back = self._start_self_intro(seg, emb, *me)
                elif self._designation:
                    self._designation.apply(seg, emb)
                    if not self._designation.active:
                        self._designation = None
            prev = self.store.segments[-1] if self.store.segments else None
            resolve_mod.resolve_one(seg, prev, self.modes, self._anon_map)
            # 새 문장이 들어오면 바로 앞 문장을 앞뒤 흐름으로 한 번 더 다듬습니다
            before_prev = self.store.segments[-2] if len(self.store.segments) >= 2 else None
            smoothed = prev if resolve_mod.smooth(before_prev, prev, seg, self.modes) else None
            self._remember(seg)
            self.store.add(seg)
            if back:                               # 자기소개 앞의 인사말 등도 같은 사람으로
                back = resolve_mod.resolve_all(self.store.segments, self.modes, start_after=back[0].id - 1,
                                               anon_map=self._anon_map)
                back = [s for s in back if s.id != seg.id]
        self.store.save()
        self._emit({"type": "segment", "segment": seg.as_dict()})
        if back:
            self._emit({"type": "segments_update", "segments": [s.as_dict() for s in back]})
        self._check_keywords(seg)
        if smoothed:
            self._emit({"type": "segments_update", "segments": [smoothed.as_dict()]})
        if self.modes["context"]:
            seg.checked.append("context")
            self._maybe_ask_context(seg)

    def _assign_speaker(self, seg: Segment):  # noqa: ANN202
        """방법별 결과를 seg.cands 에 적습니다. (표시 이름은 resolve 가 정합니다) 성문을 돌려줍니다."""
        scfg = self.cfg["speaker"]
        duration = seg.end - seg.start

        face_ratio, face_score = 0.0, 0.0
        if self.face:
            mid, s = self.face.vote(seg.start, seg.end)
            if mid:
                face_ratio, face_score = self.face.presence_ratio(seg.start, seg.end, mid), s
                seg.cands["face"] = {**self._cand(mid, s), "ratio": round(face_ratio, 2)}

        # 성문은 목소리 스위치가 꺼져 있어도 뽑아 둡니다. (나중에 켜면 바로 반영하려고)
        emb = None
        if self.recorder and duration >= float(scfg.get("min_segment_sec", 1.2)):
            try:
                embedder = voice_mod.get_embedder()
                if embedder:
                    samples = rec.read_samples(self.recorder.path, seg.start, min(seg.end, seg.start + 12))
                    emb = embedder.embed(samples)
            except Exception:  # noqa: BLE001
                log.debug("화자 임베딩 실패", exc_info=True)

        # 짧은 발언("네", "감사합니다")은 성문이 불안정해 엉뚱한 사람으로 붙기 쉽습니다.
        if emb is not None:
            if duration >= float(scfg.get("voice_match_min_sec", 2.5)):
                mid, s = self.registry.match_voice(emb, float(scfg.get("voice_threshold", 0.65)))
                if mid:
                    seg.cands["voice"] = self._cand(mid, s)
            seg.anon = self.unknown_pool.assign(emb)
        if voice_mod.get_embedder() or not self.recorder:
            seg.checked.append("voice")
        seg.follow = resolve_mod.follows(
            seg, emb, self._last_end if self.store.segments else None, self._last_emb, bool(self.store.segments)
        )

        # 얼굴로 확인된 목소리를 성문으로 학습합니다.
        # 다만 잘못 배운 성문은 이후 회의를 통째로 망치므로 조건을 엄격하게 둡니다.
        # (화면이 듣고 있는 다른 의원을 비추는 동안 의장이 말하는 경우가 흔합니다)
        if (
            emb is not None
            and "face" in seg.cands
            and self.modes["face"]
            and scfg.get("voice_autoenroll", True)
            and face_score >= 0.45                  # 얼굴이 확실할 때만
            and face_ratio >= 0.7                   # 구간 내내 그 사람이 보였을 때만
            and duration >= 3.0                     # 충분히 긴 발언만
        ):
            self.registry.add_voice(seg.cands["face"]["id"], emb)

        self._last_end = seg.end
        if emb is not None:
            self._last_emb = emb
        return emb

    def _check_keywords(self, seg: Segment) -> None:
        """관심 키워드가 나오면 화면과 Windows 알림으로 알립니다."""
        acfg = config.load().get("alerts") or {}           # 받아쓰는 중에 바꾼 키워드도 바로 씁니다
        hits = notify.find_keywords(seg.text, acfg.get("keywords") or [])
        if not hits:
            return
        self._emit({"type": "keyword", "keywords": hits, "segment_id": seg.id,
                    "speaker": seg.speaker, "text": seg.text, "t": seg.start})
        if acfg.get("os_notify", True):
            who = seg.speaker if seg.speaker and seg.speaker != "화자 미상" else "발언"
            notify.toast(f"관심 키워드: {', '.join(hits)}", f"{who} — {seg.text}", key="|".join(hits))

    def _cand(self, member_id: str, score: float) -> dict:
        return {"id": member_id, "name": resolve_mod.label_of(self.registry, member_id),
                "score": round(float(score), 3)}

    def _remember(self, seg: Segment) -> None:
        self._last_speaker = (seg.speaker, seg.speaker_id, seg.speaker_source)

    # ------------------------------------------------------------------ 스위치
    def set_modes(self, modes: dict) -> tuple[list[Segment], list[str]]:
        """얼굴/목소리/맥락 스위치를 바꾸고 녹취록 전체에 다시 적용합니다."""
        old = dict(self.modes)
        self.modes = resolve_mod.clean_modes(modes, self.modes)
        notes: list[str] = []
        if self.modes["face"] and not old["face"] and self.state == "running":
            if self.source_kind == "file":
                notes.append("파일 받아쓰기에서는 얼굴 인식을 쓰지 않습니다.")
            else:
                self.start_face()
        with self._ctx_lock:
            changed = resolve_mod.resolve_all(self.store.segments, self.modes, anon_map=self._anon_map)
            if self.store.segments:
                self._remember(self.store.segments[-1])
        if changed:
            self.store.mark_dirty()
            self.store.save()
        self._save_meta()
        return changed, notes

    def needs_backfill(self) -> bool:
        if self._backfilling:
            return False
        want = [m for m in ("voice", "context") if self.modes[m]]
        return any(m not in s.checked for s in list(self.store.segments) for m in want)

    def backfill(self, progress=None) -> list[Segment]:  # noqa: ANN001
        """스위치를 켜기 전에 지나간 문장들을 채웁니다. (별도 스레드에서 부릅니다)"""
        self._backfilling = True
        try:
            segments = list(self.store.segments)
            job = resolve_mod.Backfill(
                segments, self.recorder.path if self.recorder else None, self.registry, self.cfg,
                embs=self._seg_embs, context=self.context if self.modes["context"] else None,
                progress=progress,
            )
            if self.modes["voice"]:
                job.voice()
            if self.modes["context"]:
                job.context_scan()
            with self._ctx_lock:
                # 채우는 동안 끝나지 않은 지명은 이어서 들어오는 발언에도 적용합니다
                d = job.last_active
                if d and d.active and not (self._designation and self._designation.after > d.after):
                    d._learn = self._learn_context_voice   # noqa: SLF001
                    self._designation = d
                changed = resolve_mod.resolve_all(self.store.segments, self.modes, anon_map=self._anon_map)
                if self.store.segments:
                    self._remember(self.store.segments[-1])
            self.store.mark_dirty()
            self.store.save()
            return changed
        finally:
            self._backfilling = False
        self._anon_map: dict[str, str] = {}

    # ------------------------------------------------------------------ 맥락 화자
    def _start_self_intro(self, seg: Segment, emb, name: str, title: str) -> list[Segment]:  # noqa: ANN001
        """자기소개("… 홍길동 의원입니다"): 이 문장과, 같은 목소리로 이어지는 앞뒤 문장을 그 사람으로.

        이름은 등록된 사람과 비교해 가장 비슷한 사람으로 맞춥니다. (_ctx_lock 안에서 부릅니다)
        """
        member = intro_mod.resolve_person(name, title, self.registry)
        label = resolve_mod.label_of(self.registry, member.id)
        names = self.registry.names()
        d = Designation(seg.id - 1, member.id, label, None, seg.start, learn=self._learn_context_voice)
        d.started, d.block_emb = True, emb
        d.apply(seg, emb)
        self._designation = d
        log.info("자기소개로 화자 지정: %s (들린 이름: %s)", label, name)
        self._emit({"type": "log", "message": f"자기소개로 화자를 정했습니다: {label}"})
        # 바로 앞 문장("안녕하십니까?" 등)이 같은 목소리로 이어져 있으면 함께
        back: list[Segment] = []
        nxt_start = seg.start
        for s in reversed(self.store.segments[-3:]):
            if s.speaker_source == "manual" or nxt_start - s.end > 3.0:
                break
            e = self._seg_embs.get(s.id)
            if e is not None and emb is not None and float(np.dot(e, emb)) < 0.55:
                break
            if intro_mod.find_call(s.text, names) or intro_mod.find_self_intro(s.text, names):
                break
            s.cands["context"] = {"id": member.id, "name": label, "score": 0.0}
            back.append(s)
            nxt_start = s.start
            if len(back) >= 2:
                break
        return sorted(back, key=lambda s: s.id)

    def _rule_call(self, seg: Segment, names: list[str]) -> tuple[str, str] | None:
        """규칙으로 찾은 지명. 음성인식이 '이름 + 위원님' / '질의해 주시기 바랍니다'를 둘로 나눈 경우도 봅니다."""
        called = intro_mod.find_call(seg.text, names)
        if called:
            return called
        prev = next((s for s in reversed(self.store.segments) if s.id < seg.id), None)
        if prev is not None and seg.start - prev.end <= 2.0:
            return intro_mod.find_call_after(prev.text, seg.text, names)
        return None

    def _maybe_ask_context(self, seg: Segment) -> None:
        called = self._rule_call(seg, self.registry.names())
        if called:                                  # AI 없이 규칙으로 찾았으면 그것으로
            self._on_designation(seg, *called)
            return
        if not self.context or not looks_like_designation(seg.text, self.registry.names()):
            return
        members = [f"{m['name']} {m.get('title') or ''}".strip() for m in self.registry.list()]
        history = [f"{s.speaker}: {s.text}" for s in self.store.segments[-6:] if s.id < seg.id][-4:]
        self.context.submit(
            seg.text, history, members,
            lambda name, title: self._on_designation(seg, name, title),
        )

    def _on_designation(self, caller: Segment, name: str, title: str) -> None:
        """사회자가 다음 화자를 지명했을 때(규칙 또는 AI). 이미 올라온 뒤 문장들도 고쳐 줍니다."""
        member = intro_mod.resolve_person(name, title, self.registry)   # 등록된 이름과 비슷하면 그 이름으로
        label = resolve_mod.label_of(self.registry, member.id)
        log.info("맥락 화자 지정: %s (문장 %d 다음)", label, caller.id)
        with self._ctx_lock:
            if self._designation and self._designation.after > caller.id:   # 더 나중 지명이 이미 있으면 무시
                return
            d = Designation(caller.id, member.id, label, self._seg_embs.get(caller.id), caller.end,
                            learn=self._learn_context_voice)
            names = self.registry.names()
            for s in list(self.store.segments):
                if s.id <= caller.id or s.speaker_source == "manual":
                    continue
                if not d.active or looks_like_designation(s.text, names):
                    d.active = False
                    break
                d.apply(s, self._seg_embs.get(s.id))
            self._designation = d if d.active else None
            changed = resolve_mod.resolve_all(self.store.segments, self.modes, start_after=caller.id,
                                              anon_map=self._anon_map)
            if self.store.segments:
                self._remember(self.store.segments[-1])
        if changed:
            self.store.mark_dirty()
            self.store.save()
        if self.modes["context"]:
            self._emit({"type": "log", "message": f"다음 화자: {label} (사회자 발언에서 지정)"})
        if changed:
            self._emit({"type": "segments_update", "segments": [s.as_dict() for s in changed]})

    def _learn_context_voice(self, member_id: str, emb) -> None:  # noqa: ANN001
        if self.modes["context"] and self.cfg["speaker"].get("voice_autoenroll", True):
            self.registry.add_voice(member_id, emb)

    def _on_face(self, obs) -> None:  # noqa: ANN001
        self._emit(
            {
                "type": "face",
                "name": obs.name,
                "member_id": obs.member_id,
                "score": round(obs.score, 3),
                "t": round(obs.t, 2),
            }
        )

    # ------------------------------------------------------------------ 프롬프트
    def _make_prompt(self, tail: str) -> str | None:
        """인식 힌트: 직전에 확정된 문장만 줍니다 (whisper_streaming 방식).

        예전에는 용어집의 이름·용어 목록도 넣을 수 있었지만, 같은 말을 되풀이하거나 없는 문장을
        지어내 정확도가 떨어져(회의 90초 실측: 기준과 차이 17.6% → 11.7%) 없앴습니다.
        용어집은 확정 뒤 자동 교정에 모두 쓰입니다.
        """
        if not self.cfg["asr"].get("condition_on_previous", True):
            return None
        return tail or None

    # ------------------------------------------------------------------ 기타
    def _emit(self, event: dict) -> None:
        event.setdefault("session_id", self.id)
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001
            log.debug("이벤트 전송 오류", exc_info=True)

    def status(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "error": self.error,
            "audio_time": round(self._audio_time, 2),
            "duration": round(self.recorder.duration if self.recorder else self._audio_time, 2),
            "segments": len(self.store.segments),
            "speaking": self.vad.speaking,
            "source": self.source_kind,
            "engine": getattr(self.engine, "name", "?"),
            "model": getattr(self.engine, "model_name", ""),
            "device": getattr(self.engine, "device", ""),
            "recording": self.recorder is not None,
            "audio_mb": round((self.recorder.path.stat().st_size / 1048576) if self.recorder
                              and self.recorder.path.exists() else 0.0, 1),
            "face": self.face.snapshot() if self.face else {"running": False},
            "speaker_modes": dict(self.modes),
            "has_context_key": self.context is not None,
            "stats": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.stats.items()},
            "partial": self.partial_text,
        }

    def _save_meta(self) -> None:
        meta = {
            "id": self.id,
            "title": self.title,
            "created": self.started_at,
            "duration": round(self.recorder.duration if self.recorder else self._audio_time, 2),
            "source": self.source_kind,
            "file": self.file_path,
            "start_sec": self.start_sec,
            "model": getattr(self.engine, "model_name", ""),
            "state": self.state,
            "segments": len(self.store.segments),
            "glossary_terms": len(self.glossary.terms),
            "speaker_modes": dict(self.modes),
        }
        # 사용자가 따로 적은 폴더·회의 정보는 지우지 않고 그대로 둡니다
        meta = {**read_meta(self.id), **meta}
        (self.dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")

    def rename(self, title: str) -> None:
        self.title = title
        self._save_meta()


# ---------------------------------------------------------------------- 과거 세션
def list_sessions() -> list[dict]:
    out = []
    if not paths.SESSIONS_DIR.exists():
        return out
    for d in sorted(paths.SESSIONS_DIR.iterdir(), reverse=True):
        meta_file = d / "meta.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta["has_audio"] = (d / "audio.wav").exists()
        meta["has_summary"] = (d / "summary.md").exists()
        out.append(meta)
    return out


def load_session(session_id: str) -> tuple[dict, TranscriptStore]:
    d = paths.SESSIONS_DIR / session_id
    if not d.exists():
        raise FileNotFoundError(session_id)
    meta = json.loads((d / "meta.json").read_text("utf-8")) if (d / "meta.json").exists() else {"id": session_id}
    store = TranscriptStore(d)
    store.load()
    return meta, store


FOLDERS_FILE = paths.DATA_DIR / "folders.json"


def list_folders() -> list[str]:
    """만든 폴더 이름. 빈 폴더도 남도록 따로 저장하고, 회의에 적힌 폴더도 함께 보여 줍니다."""
    try:
        names = json.loads(FOLDERS_FILE.read_text("utf-8")) if FOLDERS_FILE.exists() else []
    except (OSError, json.JSONDecodeError):
        names = []
    for m in list_sessions():
        if m.get("folder") and m["folder"] not in names:
            names.append(m["folder"])
    return names


def save_folders(names: list[str]) -> None:
    FOLDERS_FILE.write_text(json.dumps(names, ensure_ascii=False, indent=2), "utf-8")


def move_folder(old: str, new: str) -> None:
    """폴더 이름 바꾸기(new) 또는 지우기(new 가 빈 값: 회의는 '폴더 없음'으로 돌아감)."""
    for m in list_sessions():
        if m.get("folder") == old:
            save_meta(m["id"], {"folder": new})
    names = [n for n in list_folders() if n != old]
    if new and new not in names:
        names.append(new)
    save_folders(names)


def read_meta(session_id: str) -> dict:
    f = paths.SESSIONS_DIR / session_id / "meta.json"
    try:
        return json.loads(f.read_text("utf-8")) if f.exists() else {"id": session_id}
    except (OSError, json.JSONDecodeError):
        return {"id": session_id}


def save_meta(session_id: str, patch: dict) -> dict:
    d = paths.SESSIONS_DIR / session_id
    f = d / "meta.json"
    meta = read_meta(session_id)
    meta.update(patch)
    f.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    return meta


def delete_session(session_id: str) -> None:
    import shutil

    sessions_dir = paths.SESSIONS_DIR.resolve()
    target = (sessions_dir / session_id).resolve()
    if target.parent != sessions_dir:
        raise ValueError("잘못된 회의 번호입니다.")
    shutil.rmtree(target, ignore_errors=True)
