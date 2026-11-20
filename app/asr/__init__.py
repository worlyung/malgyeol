"""엔진 선택."""
from __future__ import annotations

import logging

from .base import ASREngine, Word

log = logging.getLogger(__name__)


def whisper_installed() -> bool:
    """PC 위스퍼(선택 설치)가 깔려 있는지."""
    import importlib.util

    from ..paths import MODELS_DIR

    return (importlib.util.find_spec("faster_whisper") is not None
            and any((MODELS_DIR / "whisper").glob("**/model.bin")))


def build_engine(cfg: dict) -> ASREngine:
    name = (cfg.get("engine") or "auto").lower()
    if name in {"auto", "soniox"}:
        from .. import config

        if config.soniox_key():
            from .soniox_engine import SonioxEngine

            return SonioxEngine(cfg)
        if name == "soniox" or not whisper_installed():
            raise RuntimeError("Soniox API 키가 없습니다. 설정 → 음성인식에서 키를 넣거나, "
                               "인터넷 없이 쓰려면 'PC 위스퍼 설치'를 눌러 주세요.")
        log.info("Soniox 키가 없어 PC 위스퍼로 받아씁니다.")
    if name in {"lightning-simulwhisper", "simulwhisper", "mlx"}:
        from .simulwhisper_engine import LightningSimulWhisperEngine

        try:
            return LightningSimulWhisperEngine(cfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("Lightning-SimulWhisper를 쓸 수 없어 faster-whisper로 대체합니다: %s", exc)
    from .faster_whisper_engine import FasterWhisperEngine

    return FasterWhisperEngine(cfg)


__all__ = ["ASREngine", "Word", "build_engine"]
