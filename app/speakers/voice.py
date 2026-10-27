"""목소리(성문) 기반 화자 구분.

얼굴이 안 보이는 구간(전경 샷, 자료 화면 등)을 위해 목소리로도 화자를 가립니다.
얼굴로 확인된 구간의 목소리는 자동으로 그 의원의 성문으로 등록되므로,
회의가 진행될수록 얼굴 없이도 화자를 맞히게 됩니다.

모델: sherpa-onnx의 3D-Speaker CAM++ (로컬 ONNX)
"""
from __future__ import annotations

import logging
import threading

import numpy as np

from .. import paths

log = logging.getLogger(__name__)
SR = 16000


class VoiceEmbedder:
    def __init__(self) -> None:
        import sherpa_onnx

        if not paths.SPEAKER_EMBED_MODEL.exists():
            raise FileNotFoundError(
                "화자 임베딩 모델이 없습니다. `python -m scripts.download_models` 를 실행하세요."
            )
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(paths.SPEAKER_EMBED_MODEL), num_threads=1, debug=False, provider="cpu"
        )
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self._lock = threading.Lock()
        self.dim = self._extractor.dim

    def embed(self, samples: np.ndarray) -> np.ndarray | None:
        if samples is None or len(samples) < int(SR * 0.7):
            return None
        with self._lock:
            stream = self._extractor.create_stream()
            stream.accept_waveform(sample_rate=SR, waveform=samples.astype(np.float32, copy=False))
            stream.input_finished()
            if not self._extractor.is_ready(stream):
                return None
            emb = np.asarray(self._extractor.compute(stream), dtype=np.float32)
        n = np.linalg.norm(emb)
        return emb / n if n > 0 else None


class UnknownSpeakerPool:
    """명부에 없는 화자를 '화자 1, 화자 2...'로 묶어 둡니다."""

    def __init__(self, threshold: float = 0.6):
        self.threshold = threshold
        self._centroids: list[np.ndarray] = []
        self._counts: list[int] = []
        self._lock = threading.Lock()

    def assign(self, emb: np.ndarray) -> str:
        with self._lock:
            if self._centroids:
                mat = np.vstack(self._centroids)
                scores = mat @ emb
                idx = int(np.argmax(scores))
                if float(scores[idx]) >= self.threshold:
                    n = self._counts[idx]
                    merged = (self._centroids[idx] * n + emb) / (n + 1)
                    norm = np.linalg.norm(merged)
                    self._centroids[idx] = merged / norm if norm else merged
                    self._counts[idx] = n + 1
                    return f"화자 {idx + 1}"
            self._centroids.append(emb)
            self._counts.append(1)
            return f"화자 {len(self._centroids)}"

    def reset(self) -> None:
        with self._lock:
            self._centroids.clear()
            self._counts.clear()


_embedder: VoiceEmbedder | None = None
_embedder_error: str | None = None


def get_embedder() -> VoiceEmbedder | None:
    """지연 로딩. 실패하면 None (음성 화자구분만 비활성화)."""
    global _embedder, _embedder_error
    if _embedder is None and _embedder_error is None:
        try:
            _embedder = VoiceEmbedder()
        except Exception as exc:  # noqa: BLE001
            _embedder_error = str(exc)
            log.warning("화자 임베딩 모델을 불러오지 못했습니다: %s", exc)
    return _embedder


def embedder_error() -> str | None:
    return _embedder_error
