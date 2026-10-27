"""엔진 선택."""
from __future__ import annotations

import logging

from .base import ASREngine, Word

log = logging.getLogger(__name__)


def build_engine(cfg: dict) -> ASREngine:
    name = (cfg.get("engine") or "faster-whisper").lower()
    if name in {"lightning-simulwhisper", "simulwhisper", "mlx"}:
        from .simulwhisper_engine import LightningSimulWhisperEngine

        try:
            return LightningSimulWhisperEngine(cfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("Lightning-SimulWhisper를 쓸 수 없어 faster-whisper로 대체합니다: %s", exc)
    from .faster_whisper_engine import FasterWhisperEngine

    return FasterWhisperEngine(cfg)


__all__ = ["ASREngine", "Word", "build_engine"]
