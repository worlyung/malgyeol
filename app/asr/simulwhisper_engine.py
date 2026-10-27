"""(선택) Apple Silicon 전용 Lightning-SimulWhisper 어댑터.

이 저장소의 코드는 여기에 포함하지 않습니다. 사용자가 직접 클론한 경로를
설정(asr.simulwhisper_path)에 넣으면 그때 불러옵니다.

  git clone https://github.com/altalt-org/Lightning-SimulWhisper.git
  pip install mlx coremltools ane_transformers

주의: 해당 저장소는 PolyForm Noncommercial 라이선스(비상업용)입니다.
Windows/Linux에서는 동작하지 않으며, 그 경우 자동으로 faster-whisper로 되돌아갑니다.
"""
from __future__ import annotations

import logging
import platform
import sys
from pathlib import Path

import numpy as np

from .base import ASREngine, Word

log = logging.getLogger(__name__)


class LightningSimulWhisperEngine(ASREngine):
    """AlignAtt 정책을 쓰는 네이티브 스트리밍 엔진.

    버퍼 단위 재디코딩이 아니라 자체적으로 상태를 들고 있으므로
    `native_streaming = True` 로 표시하고, 세션이 전용 처리기를 씁니다.
    """

    sep = " "
    name = "lightning-simulwhisper"
    native_streaming = True

    def __init__(self, cfg: dict):
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("Lightning-SimulWhisper는 Apple Silicon(macOS arm64)에서만 동작합니다.")
        repo = Path(cfg.get("simulwhisper_path") or "").expanduser()
        if not (repo / "simulstreaming_whisper.py").exists():
            raise RuntimeError(f"Lightning-SimulWhisper 경로가 올바르지 않습니다: {repo}")
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))

        from simulstreaming_whisper import SimulWhisperASR, SimulWhisperOnline  # type: ignore

        model_name = cfg.get("model", "large-v3-turbo")
        self.model_name = model_name
        self.device = "mlx"
        self._asr = SimulWhisperASR(
            language=cfg.get("language", "ko"),
            model_path=cfg.get("simulwhisper_model_path") or model_name,
            model_name=model_name,
            cif_ckpt_path=cfg.get("cif_ckpt_path") or None,
            frame_threshold=int(cfg.get("frame_threshold", 25)),
            audio_max_len=float(cfg.get("audio_max_len", 30.0)),
            audio_min_len=float(cfg.get("audio_min_len", 0.0)),
            segment_length=float(cfg.get("min_chunk_sec", 1.0)),
            beams=int(cfg.get("beam_size", 1)),
            task="transcribe",
            decoder_type="greedy" if int(cfg.get("beam_size", 1)) == 1 else "beam",
            never_fire=False,
            init_prompt=None,
            static_init_prompt=None,
            max_context_tokens=int(cfg.get("max_context_tokens", 100)),
            logdir=None,
            vad_silence_ms=int(float(cfg.get("silence_finalize_sec", 0.7)) * 1000),
            use_coreml=bool(cfg.get("use_coreml", True)),
            coreml_encoder_path=cfg.get("coreml_encoder_path") or None,
            coreml_compute_units=cfg.get("coreml_compute_units", "ALL"),
        )
        self._online = SimulWhisperOnline(self._asr)

    # --- 네이티브 스트리밍 인터페이스 ---
    def set_static_prompt(self, prompt: str | None) -> None:
        """용어집을 고정 프롬프트로 넣습니다 (--static_init_prompt 와 동일)."""
        try:
            self._asr.model.cfg.static_init_prompt = prompt
        except Exception:  # noqa: BLE001
            log.debug("static_init_prompt 설정 실패", exc_info=True)

    def insert_audio(self, chunk: np.ndarray) -> None:
        self._online.insert_audio_chunk(chunk.astype(np.float32, copy=False))

    def step(self, final: bool = False) -> list[Word]:
        beg, end, text = self._online.finish() if final else self._online.process_iter()
        if not text:
            return []
        return [Word(float(beg or 0.0), float(end or 0.0), text.strip() + " ", 1.0)]

    def reset(self) -> None:
        self._online.init()

    # --- 배치 인터페이스는 쓰지 않음 ---
    def transcribe(self, audio, prompt: str | None = None, final: bool = False) -> list[Word]:
        raise NotImplementedError("이 엔진은 native streaming 전용입니다.")
