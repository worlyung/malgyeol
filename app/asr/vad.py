"""스트리밍 음성 검출(VAD).

faster-whisper가 함께 배포하는 Silero VAD ONNX 모델을 그대로 씁니다.
(추가 다운로드 없음. 모델을 못 불러오면 에너지 기반으로 자동 대체)
"""
from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

FRAME = 512               # 16kHz에서 32ms
SR = 16000


class StreamingVAD:
    """청크를 넣으면 '지금 말하는 중인지'와 '연속 무음 길이'를 알려줍니다."""

    def __init__(self, threshold: float = 0.5, min_silence_sec: float = 0.7):
        self.threshold = threshold
        self.min_silence_sec = min_silence_sec
        self._buf = np.zeros(0, dtype=np.float32)
        self._silence_sec = 0.0
        self._speech_sec = 0.0
        self.speaking = False
        self.last_prob = 0.0
        self._model = None
        try:
            from faster_whisper.vad import get_vad_model

            self._model = get_vad_model()
        except Exception:  # noqa: BLE001 - 모델이 없어도 동작해야 함
            log.warning("Silero VAD를 불러오지 못했습니다. 에너지 기반 VAD로 대체합니다.", exc_info=True)

    def _probs(self, frames: np.ndarray) -> np.ndarray:
        """frames: (n, 512) -> 프레임별 음성 확률"""
        if self._model is None:
            rms = np.sqrt(np.mean(frames.astype(np.float32) ** 2, axis=1) + 1e-12)
            # 대략 -45dBFS 이상이면 음성으로 간주
            return np.clip((20 * np.log10(rms + 1e-12) + 45) / 20, 0, 1)
        try:
            out = self._model(frames.reshape(-1).astype(np.float32), num_samples=FRAME)
            return np.asarray(out).reshape(-1)
        except Exception:  # noqa: BLE001
            log.debug("VAD 추론 실패", exc_info=True)
            self._model = None
            return self._probs(frames)

    def feed(self, samples: np.ndarray) -> None:
        self._buf = np.concatenate([self._buf, samples.astype(np.float32, copy=False)])
        n = len(self._buf) // FRAME
        if n == 0:
            return
        frames = self._buf[: n * FRAME].reshape(n, FRAME)
        self._buf = self._buf[n * FRAME :]

        probs = self._probs(frames)
        self.last_prob = float(probs[-1]) if len(probs) else 0.0
        dur = FRAME / SR
        for p in probs:
            if p >= self.threshold:
                self._silence_sec = 0.0
                self._speech_sec += dur
                if self._speech_sec >= 0.1:
                    self.speaking = True
            else:
                self._silence_sec += dur
                self._speech_sec = 0.0
                if self._silence_sec >= self.min_silence_sec:
                    self.speaking = False

    @property
    def silence_sec(self) -> float:
        return self._silence_sec

    def utterance_ended(self) -> bool:
        """직전에 말이 있었고 지금은 충분히 조용한 상태."""
        return not self.speaking and self._silence_sec >= self.min_silence_sec

    def reset_silence(self) -> None:
        self._silence_sec = 0.0
