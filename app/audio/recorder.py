"""녹음 파일 기록과 구간 재생.

16kHz mono 16bit WAV로 계속 이어 씁니다. 헤더 크기는 주기적으로 갱신하므로
녹음 중에도 앞부분을 바로 재생할 수 있습니다.
"""
from __future__ import annotations

import struct
import threading
from pathlib import Path

import numpy as np

SR = 16000
BYTES_PER_SAMPLE = 2
HEADER_SIZE = 44


def _header(data_bytes: int, sr: int = SR, channels: int = 1) -> bytes:
    block_align = channels * BYTES_PER_SAMPLE
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + data_bytes),
            b"WAVEfmt ",
            struct.pack("<IHHIIHH", 16, 1, channels, sr, sr * block_align, block_align, 16),
            b"data",
            struct.pack("<I", data_bytes),
        ]
    )


class WavRecorder:
    def __init__(self, path: str | Path, sr: int = SR):
        self.path = Path(path)
        self.sr = sr
        self._lock = threading.Lock()
        self._data_bytes = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "wb+")
        self._fh.write(_header(0, sr))
        self._fh.flush()
        self._unflushed = 0

    def append(self, samples: np.ndarray) -> None:
        pcm = np.clip(samples, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype("<i2").tobytes()
        with self._lock:
            self._fh.seek(0, 2)
            self._fh.write(pcm)
            self._data_bytes += len(pcm)
            self._unflushed += len(pcm)
            if self._unflushed >= self.sr * BYTES_PER_SAMPLE:   # 1초마다 헤더 갱신
                self._update_header()

    def _update_header(self) -> None:
        pos = self._fh.tell()
        self._fh.seek(0)
        self._fh.write(_header(self._data_bytes, self.sr))
        self._fh.seek(pos)
        self._fh.flush()
        self._unflushed = 0

    @property
    def duration(self) -> float:
        return self._data_bytes / (self.sr * BYTES_PER_SAMPLE)

    def close(self) -> None:
        with self._lock:
            if self._fh.closed:
                return
            self._update_header()
            self._fh.close()


def read_slice(path: str | Path, start: float, end: float, sr: int = SR, pad: float = 0.15) -> bytes:
    """[start, end] 구간을 잘라 작은 WAV 바이트로 돌려줍니다."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    start = max(0.0, start - pad)
    end = max(start + 0.05, end + pad)
    with open(p, "rb") as fh:
        fh.seek(0, 2)
        total = fh.tell() - HEADER_SIZE
        begin = min(total, int(start * sr) * BYTES_PER_SAMPLE)
        length = min(total - begin, int((end - start) * sr) * BYTES_PER_SAMPLE)
        if length <= 0:
            return _header(0, sr)
        fh.seek(HEADER_SIZE + begin)
        data = fh.read(length)
    return _header(len(data), sr) + data


def read_samples(path: str | Path, start: float, end: float, sr: int = SR) -> np.ndarray:
    """화자 임베딩 계산용 float32 샘플."""
    raw = read_slice(path, start, end, sr, pad=0.0)[HEADER_SIZE:]
    if not raw:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
