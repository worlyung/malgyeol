"""faster-whisper(CTranslate2) 기반 로컬 엔진. Windows/Linux/Mac 모두 동작."""
from __future__ import annotations

import logging
import os
import re
import threading

import numpy as np

from .base import ASREngine, Word

log = logging.getLogger(__name__)

# pip 로 받은 NVIDIA 꾸러미(cublas·cudnn)의 DLL 은 Windows 가 스스로 찾지 못해 경로를 알려 줍니다.
if os.name == "nt":
    try:
        import nvidia

        for _p in nvidia.__path__:
            for _sub in os.listdir(_p):
                _bin = os.path.join(_p, _sub, "bin")
                if os.path.isdir(_bin):
                    os.add_dll_directory(_bin)
                    os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")
    except ImportError:
        pass

# 짧은 이름 -> HuggingFace CT2 저장소
MODEL_ALIASES = {
    "large-v3-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
}
DEFAULT_MODEL = "large-v3-turbo"
# 실제 회의 녹음으로 비교해 쓸모없다고 본 모델입니다. 예전 설정에 남아 있으면 기본 모델로 바꿉니다.
#   small  : 앞 30초를 통째로 놓치거나 힌트의 이름만 되풀이 (기준 문장과 41~100% 차이)
#   medium : turbo보다 조금 빠를 뿐 틀리는 비율이 두 배
#   tiny·base·large-v2 : small 보다 부정확하거나 turbo 보다 느림
#   large-v3 : turbo 와 같은 구조에 글자를 만드는 부분이 훨씬 커서, CPU 에서는 실시간을 따라가지 못함
RETIRED_MODELS = {"tiny", "base", "small", "medium", "large-v2", "large-v3"}


def resolve_model(name: str) -> str:
    """모델 이름을 실제 경로나 HuggingFace 저장소 이름으로 바꿉니다.

    배포본에는 모델이 data/models/whisper/<이름> 폴더에 들어 있어,
    인터넷이 없는 PC에서도 바로 불러옵니다.
    """
    if name in RETIRED_MODELS:
        log.info("쓰지 않는 모델(%s) 대신 %s 을 씁니다.", name, DEFAULT_MODEL)
        name = DEFAULT_MODEL
    if os.path.isdir(name):
        return name
    from ..paths import MODELS_DIR

    local = MODELS_DIR / "whisper" / name
    if (local / "model.bin").exists():
        return str(local)
    return MODEL_ALIASES.get(name, name)


def default_threads() -> int:
    """CPU를 다 먹지 않도록 기본 스레드 수를 정합니다.

    이 PC 실측(Ryzen 7 8845HS, 논리 16): 12스레드 4.08초 / 6스레드 4.60초.
    스레드를 두 배로 늘려도 12%밖에 빨라지지 않으므로, 절반만 쓰고 나머지는 남겨 둡니다.
    """
    logical = os.cpu_count() or 4
    physical = max(1, logical // 2)          # 대부분의 최신 CPU는 코어당 2스레드
    return max(2, physical - 2) if physical >= 6 else max(2, physical - 1)


def pick_device(device: str, compute_type: str) -> tuple[str, str]:
    if device == "auto":
        try:
            import ctranslate2

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:  # noqa: BLE001
            device = "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    return device, compute_type


_SYLLABLE_LOOP = re.compile(r"([^\s\d]{1,2})\1{5,}")   # 숫자(10000000원)는 빼고


def max_tokens_for(audio_sec: float) -> int:
    """이 길이의 소리에서 나올 수 있는 글자 조각(토큰) 수의 넉넉한 상한.

    한국어 발화는 1초에 7~8음절 안팎이라 초당 10토큰이면 충분합니다. 상한이 없으면 환각으로
    같은 말을 끝없이 반복할 때 인식 한 번이 20초 가까이 걸렸습니다.
    """
    return int(min(440, 24 + audio_sec * 10))


def _drop_repetitions(words: list[Word], max_phrase: int = 8, repeats: int = 3) -> list[Word]:
    """같은 말·같은 구절이 계속 반복되면 그 지점에서 잘라냅니다.

    "상대, 상대, 상대..." 또는 "의회 사무처장 손철웅 의회 사무처장 손철웅..." 처럼
    Whisper가 잡음·무음 구간에서 흔히 일으키는 환각을 막습니다.
    """
    # "사회사사사사사사…"처럼 한 낱말 안에서 같은 글자(1~2자)가 5번 넘게 이어지면 거기서 자릅니다.
    for i, w in enumerate(words):
        if _SYLLABLE_LOOP.search(w.text):
            return words[:i]
    keys = [w.text.strip().strip(",.!?… ") for w in words]
    n_words = len(words)
    for i in range(n_words):
        for n in range(1, max_phrase + 1):
            if i + n * repeats > n_words:
                break
            first = keys[i : i + n]
            if not any(first):
                continue
            if all(keys[i + n * k : i + n * (k + 1)] == first for k in range(1, repeats)):
                return words[: i + n]        # 첫 한 번만 남기고 자름
    return words


class FasterWhisperEngine(ASREngine):
    sep = ""
    name = "faster-whisper"

    def __init__(self, cfg: dict):
        from faster_whisper import WhisperModel

        model_name = resolve_model(cfg.get("model", "large-v3-turbo"))
        device, compute_type = pick_device(cfg.get("device", "auto"), cfg.get("compute_type", "auto"))
        threads = int(cfg.get("cpu_threads") or 0) or default_threads()
        log.info("Whisper 모델 로딩: %s (%s/%s, threads=%d)", model_name, device, compute_type, threads)

        from ..paths import MODELS_DIR

        self.model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            cpu_threads=threads,
            download_root=str(MODELS_DIR / "whisper"),
        )
        self.language = cfg.get("language") or None
        if self.language == "auto":
            self.language = None
        self.beam_size = int(cfg.get("beam_size", 1))
        self.device = device
        self.compute_type = compute_type
        self.model_name = model_name
        self._lock = threading.Lock()

    def count_tokens(self, text: str) -> int:
        try:
            return len(self.model.hf_tokenizer.encode(text, add_special_tokens=False).ids)
        except Exception:  # noqa: BLE001
            return max(1, len(text) // 2)

    def transcribe(self, audio: np.ndarray, prompt: str | None = None, final: bool = False) -> list[Word]:
        """용어집 힌트를 주고 인식하되, 힌트 때문에 무너지면 힌트 없이 다시 인식합니다.

        용어집 힌트(이름·용어 목록)는 이름 표기에 도움이 되지만, 소리에 따라 Whisper 가
        "사업자의 사업자의 …"처럼 같은 말을 끝없이 되풀이하거나 없는 문장("기상캐스터 ○○○")을
        지어냅니다. 같은 소리를 힌트 없이 인식하면 멀쩡했습니다(실측, 2026-09-30).
        """
        if audio is None or len(audio) < 1600:   # 0.1초 미만은 건너뜀
            return []
        with self._lock:
            if prompt:
                # 힌트를 준 인식은 온도 재시도 없이 한 번만 (무너지면 어차피 힌트 없이 다시 합니다)
                words, broken = self._run(audio, prompt, [0.0])
                if not broken:
                    return words
                log.info("힌트 때문에 인식이 무너져 힌트 없이 다시 인식합니다.")
            words, _broken = self._run(audio, None, [0.0, 0.2])
            return words

    def _run(self, audio: np.ndarray, prompt: str | None, temperature) -> tuple[list[Word], bool]:  # noqa: ANN001
        """(낱말들, 무너졌는지). 같은 말 되풀이(압축률 2.4 초과)나 반복을 잘라 낸 흔적이 있으면 무너진 것."""
        segments, _info = self.model.transcribe(
            audio.astype(np.float32, copy=False),
            language=self.language,
            task="transcribe",
            beam_size=self.beam_size,
            temperature=temperature,
            word_timestamps=True,
            initial_prompt=prompt or None,
            condition_on_previous_text=False,   # 프롬프트는 우리가 직접 관리
            # 무음/잡음 구간에서 없는 말을 지어내는 것(환각)을 막습니다.
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 300},
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            suppress_blank=True,
            max_new_tokens=self._max_new_tokens(len(audio) / 16000, prompt),
        )
        words: list[Word] = []
        broken = False
        for seg in segments:
            if seg.compression_ratio > 2.4:
                broken = True
            if seg.no_speech_prob > 0.85:
                continue
            for w in seg.words or []:
                text = w.word
                if not text.strip():
                    continue
                words.append(Word(float(w.start), float(w.end), text, float(w.probability)))
        kept = _drop_repetitions(words)
        return kept, broken or len(kept) < len(words)

    def _max_new_tokens(self, audio_sec: float, prompt: str | None) -> int:
        """받아 적을 토큰 상한. 힌트 토큰과 합쳐 Whisper 한도(448)를 넘으면 오류가 나므로 그만큼 뺍니다.

        faster-whisper 는 힌트를 최대 223토큰까지 쓰고, 앞뒤에 시작 표시 토큰 몇 개를 붙입니다.
        """
        used = 0
        if prompt:
            used = 1 + min(self.count_tokens(" " + prompt.strip()), 223)
        room = 448 - used - 8                 # 시작 표시(언어·작업 등)와 여유분
        return max(32, min(max_tokens_for(audio_sec), room))

    def warmup(self) -> None:
        """첫 인식이 느린 것을 막기 위해 미리 한 번 돌려 둡니다."""
        noise = (np.random.rand(16000 * 3).astype(np.float32) - 0.5) * 0.001
        self.transcribe(noise)
