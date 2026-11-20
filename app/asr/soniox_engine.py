"""Soniox 실시간 받아쓰기 엔진 (인터넷).

회의 소리를 Soniox 서버로 흘려보내고, 확정된 글자를 받아 옵니다.
PC에 무거운 음성인식 모델(약 3GB)을 둘 필요가 없어 설치 파일이 가벼워집니다.
실제 내부회의 45분 녹음으로 비교했을 때 PC 위스퍼보다 고유명사·사투리를 훨씬 잘 받아 적었습니다(2026-11-20).

세션 쪽에서 보면 '스스로 스트리밍하는 엔진'(native_streaming)입니다.
    begin()            회의마다 한 번 — 새 연결을 준비
    insert_audio(소리) 소리를 바로 서버로 보냄 (처음 보낼 때 연결)
    step(final)        그사이 확정된 낱말을 돌려줌. final 이면 지금까지 보낸 소리를 모두 확정시킴
    partial_words()    아직 바뀔 수 있는 뒷부분 (화면의 회색 미리보기)
"""
from __future__ import annotations

import json
import logging
import threading

import numpy as np

from .base import ASREngine, Word

log = logging.getLogger(__name__)

URL = "wss://stt-rt.soniox.com/transcribe-websocket"
MODEL = "stt-rt-v6"
SR = 16000
FINALIZE_WAIT_SEC = 6.0       # '지금까지 소리 확정' 요청 뒤 기다리는 최대 시간
MAX_TERMS = 500               # 용어집에서 힌트로 줄 낱말 수 (서버 한도는 8000토큰)

_ERRORS = {
    401: "Soniox API 키가 맞지 않습니다. 설정에서 키를 확인해 주세요.",
    402: "Soniox 잔액이 부족합니다. Soniox 사이트에서 충전해 주세요.",
    429: "Soniox 동시 사용 한도를 넘었습니다. 잠시 뒤 다시 시작해 주세요.",
}


def glossary_terms() -> list[str]:
    """용어집의 이름·용어를 인식 힌트로 씁니다."""
    try:
        from ..glossary import Glossary

        seen: dict[str, None] = {}
        for t in Glossary().terms:
            if t.text.strip():
                seen.setdefault(t.text.strip(), None)
        return list(seen)[:MAX_TERMS]
    except Exception:  # noqa: BLE001 - 힌트가 없어도 받아쓰기는 됩니다
        log.debug("용어집을 읽지 못했습니다", exc_info=True)
        return []


class SonioxEngine(ASREngine):
    sep = ""
    name = "soniox"
    native_streaming = True

    def __init__(self, cfg: dict, key: str | None = None):
        from .. import config

        self.key = key or config.soniox_key()
        if not self.key:
            raise RuntimeError("Soniox API 키가 없습니다. 설정 → 음성인식에서 키를 넣어 주세요.")
        lang = cfg.get("language") or "ko"
        self.language = None if lang == "auto" else lang
        self.model_name = f"Soniox {MODEL}"
        self._lock = threading.Lock()
        self._ws = None
        self._reader: threading.Thread | None = None
        self._finals: list[Word] = []
        self._partial: list[Word] = []
        self._fin = threading.Event()
        self._error: str | None = None
        self._offset = 0.0         # 연결이 끊겨 다시 이었을 때, 앞 연결에서 보낸 소리 길이
        self._sent = 0.0           # 지금 연결로 보낸 소리 길이(초)

    # ------------------------------------------------------------ 연결
    def _connect(self) -> None:
        from websockets.sync.client import connect

        ws = connect(URL, additional_headers={"Authorization": f"Bearer {self.key}"},
                     open_timeout=15, max_size=None)
        start = {
            "model": MODEL,
            "audio_format": "pcm_s16le",
            "sample_rate": SR,
            "num_channels": 1,
            "enable_endpoint_detection": True,
        }
        if self.language:
            start["language_hints"] = [self.language]
        terms = glossary_terms()
        if terms:
            start["context"] = {"terms": terms}
        ws.send(json.dumps(start, ensure_ascii=False))
        self._ws = ws
        self._sent = 0.0
        self._reader = threading.Thread(target=self._read, args=(ws, self._offset),
                                         name="soniox-reader", daemon=True)
        self._reader.start()
        log.info("Soniox 연결 (용어 힌트 %d개)", len(terms))

    def _read(self, ws, offset: float) -> None:  # noqa: ANN001
        try:
            for raw in ws:
                msg = json.loads(raw)
                if msg.get("error_code"):
                    code = int(msg.get("error_code") or 0)
                    self._error = _ERRORS.get(code) or f"Soniox 오류 ({code}): {msg.get('error_message', '')}"
                    log.error("Soniox 오류: %s %s", msg.get("error_type"), msg.get("error_message"))
                    break
                partial = []
                with self._lock:
                    for tok in msg.get("tokens", []):
                        text = tok.get("text", "")
                        if text == "<fin>":
                            self._fin.set()
                            continue
                        if text == "<end>" or "start_ms" not in tok:
                            continue
                        w = Word(offset + tok["start_ms"] / 1000, offset + tok["end_ms"] / 1000,
                                 text, float(tok.get("confidence", 1.0)))
                        (self._finals if tok.get("is_final") else partial).append(w)
                    self._partial = partial
                if msg.get("finished"):
                    break
        except Exception as exc:  # noqa: BLE001
            if self._ws is ws:          # 우리가 닫은 게 아니면 끊긴 것
                log.warning("Soniox 연결이 끊겼습니다: %s", exc)
        finally:
            self._fin.set()
            if self._ws is ws:
                self._ws = None         # 다음 소리를 보낼 때 다시 잇습니다

    def _close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is None:
            return
        try:
            ws.send("")                 # 소리 끝
            ws.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 세션 인터페이스
    def begin(self) -> None:
        """새 회의 시작: 앞 회의의 연결과 남은 글자를 정리합니다."""
        self._close()
        with self._lock:
            self._finals, self._partial = [], []
        self._error = None
        self._offset = 0.0

    def reset(self) -> None:
        """조용한 구간을 버릴 때도 불리지만, 서버 연결은 그대로 이어 갑니다."""

    def insert_audio(self, chunk: np.ndarray) -> None:
        if self._error:
            raise RuntimeError(self._error)
        if self._ws is None:
            self._offset += self._sent          # 끊겼다 다시 이으면 시간이 이어지도록
            self._connect()
        pcm = (np.clip(chunk, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        try:
            self._ws.send(pcm)
        except Exception as exc:  # noqa: BLE001
            log.warning("Soniox로 소리를 보내지 못했습니다(다음 소리에서 다시 연결): %s", exc)
            self._ws = None
            return
        self._sent += len(chunk) / SR

    def step(self, final: bool = False) -> list[Word]:
        if final and self._ws is not None:
            self._fin.clear()
            try:
                self._ws.send(json.dumps({"type": "finalize"}))
                self._fin.wait(FINALIZE_WAIT_SEC)
            except Exception:  # noqa: BLE001
                log.debug("확정 요청 실패", exc_info=True)
        if self._error:
            raise RuntimeError(self._error)
        with self._lock:
            out, self._finals = self._finals, []
            if final:
                self._partial = []
        return out

    def partial_words(self) -> list[Word]:
        with self._lock:
            return list(self._partial)

    def close(self) -> None:
        self._close()

    def transcribe(self, audio, prompt: str | None = None, final: bool = False) -> list[Word]:  # noqa: ANN001
        raise NotImplementedError("Soniox 엔진은 실시간 스트리밍 전용입니다.")

    # ------------------------------------------------------------ 녹음 파일 (한 번에)
    def transcribe_file(self, audio: np.ndarray) -> list[Word]:
        """녹음 파일은 실시간 연결(실제 속도로만 처리) 대신 파일 전사로 한 번에 받습니다.

        실측: 45분 회의 → 41초. 서버에 올린 소리와 결과는 받자마자 지웁니다.
        """
        import io
        import time
        import wave

        import httpx

        api, h = "https://api.soniox.com/v1", {"Authorization": f"Bearer {self.key}"}
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes())

        def ok(r):  # noqa: ANN001, ANN202
            if r.status_code >= 400:
                raise RuntimeError(_ERRORS.get(r.status_code) or f"Soniox 오류 ({r.status_code}): {r.text[:200]}")
            return r.json() if r.content else {}

        with httpx.Client(timeout=httpx.Timeout(60, write=1800)) as c:
            fid = ok(c.post(f"{api}/files", headers=h,
                            files={"file": ("audio.wav", buf.getvalue(), "audio/wav")}))["id"]
            tid = None
            try:
                body = {"model": MODEL.replace("-rt-", "-async-"), "file_id": fid}
                if self.language:
                    body["language_hints"] = [self.language]
                terms = glossary_terms()
                if terms:
                    body["context"] = {"terms": terms}
                tid = ok(c.post(f"{api}/transcriptions", headers=h, json=body))["id"]
                while True:
                    st = ok(c.get(f"{api}/transcriptions/{tid}", headers=h))
                    if st["status"] == "completed":
                        break
                    if st["status"] == "error":
                        raise RuntimeError(f"Soniox 받아쓰기 실패: {st.get('error_message', '')}")
                    time.sleep(2)
                tokens = ok(c.get(f"{api}/transcriptions/{tid}/transcript", headers=h)).get("tokens", [])
            finally:
                for url in ([f"{api}/transcriptions/{tid}"] if tid else []) + [f"{api}/files/{fid}"]:
                    try:
                        c.delete(url, headers=h)
                    except Exception:  # noqa: BLE001
                        log.warning("Soniox 서버의 사본을 지우지 못했습니다: %s", url)
        return [Word(t["start_ms"] / 1000, t["end_ms"] / 1000, t["text"], float(t.get("confidence", 1.0)))
                for t in tokens if "start_ms" in t and t.get("text") not in ("<end>", "<fin>")]
