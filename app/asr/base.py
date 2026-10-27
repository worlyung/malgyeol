"""음성인식 엔진 공통 인터페이스."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Word:
    start: float          # 버퍼 시작 기준 초
    end: float
    text: str
    prob: float = 1.0

    def shifted(self, offset: float) -> "Word":
        return Word(self.start + offset, self.end + offset, self.text, self.prob)


class ASREngine:
    """오디오 한 덩어리를 받아 단어 단위 타임스탬프를 돌려주는 엔진."""

    # 단어를 이어붙일 때 쓰는 구분자 (faster-whisper는 단어에 공백이 포함됨)
    sep = ""
    name = "base"

    def transcribe(self, audio, prompt: str | None = None, final: bool = False) -> list[Word]:
        raise NotImplementedError

    def warmup(self) -> None:
        pass

    def close(self) -> None:
        pass
