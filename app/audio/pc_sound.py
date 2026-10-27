"""'PC 소리'를 스피커 볼륨과 상관없이 받아 적기.

Windows 10 2004(빌드 19041) 이후의 '프로세스 루프백' 기능으로 **이 프로그램만 빼고 PC의 모든 소리**를 받습니다.
스피커 볼륨·음소거를 적용하기 전의 소리라, 사무실에서 볼륨을 0으로 하거나 음소거해도 받아 적습니다.
듣고 싶으면 볼륨을 올리면 되고, 받아쓰기는 그대로입니다. (이어폰이 없어도 조용히 쓸 수 있습니다)

- 그 기능을 부르는 작은 도구(scripts/proc_capture.cs)를 처음 쓸 때 Windows 에 기본으로 있는
  C# 컴파일러로 만들어 data/bin 에 둡니다. 가상 오디오 드라이버·외부 소프트웨어·관리자 권한이 필요 없습니다.
- 이 기능을 쓸 수 없는 PC(옛 Windows 등)는 예전 방식(기본 출력 장치 루프백)으로 받아 적습니다.
"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Iterator

import numpy as np

from .. import paths
from .capture import NO_OUTPUT_DEVICE, TARGET_SR, AudioSource, AudioUnavailable, DeviceSource, Leveler, _resample, has_output_device

log = logging.getLogger(__name__)

SOURCE = paths.ROOT / "scripts" / "proc_capture.cs"
TOOL = paths.DATA_DIR / "bin" / "proc_capture.exe"
CSC = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
RATE, CHANNELS = 48000, 2


class AppLoopbackUnavailable(RuntimeError):
    pass


def ensure_tool() -> Path:
    """도구가 없거나 소스보다 오래됐으면 만듭니다."""
    if TOOL.exists() and (not SOURCE.exists() or TOOL.stat().st_mtime >= SOURCE.stat().st_mtime):
        return TOOL
    if not SOURCE.exists():
        raise AppLoopbackUnavailable("scripts/proc_capture.cs 가 없습니다.")
    if not CSC.exists():
        raise AppLoopbackUnavailable(".NET Framework 4 C# 컴파일러가 없습니다.")
    TOOL.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOOL.with_suffix(".tmp.exe")
    r = subprocess.run([str(CSC), "-nologo", "-optimize", f"-out:{tmp}", str(SOURCE)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if r.returncode != 0 or not tmp.exists():
        log.error("proc_capture 만들기 실패: %s", r.stdout[-800:])
        raise AppLoopbackUnavailable("proc_capture 도구를 만들지 못했습니다.")
    tmp.replace(TOOL)
    return TOOL


class AppLoopbackSource(AudioSource):
    """이 프로그램을 뺀 PC의 모든 소리 (스피커 볼륨·음소거 적용 전)."""

    def __init__(self) -> None:
        self._q: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=400)
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        tool = ensure_tool()
        self._proc = subprocess.Popen([str(tool), "except", str(os.getpid())], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        first = self._proc.stderr.readline().decode("utf-8", "replace").strip()
        if first != "READY":
            self._proc.kill()
            raise AppLoopbackUnavailable(first or "프로세스 루프백을 시작하지 못했습니다.")
        threading.Thread(target=self._read, name="app-loopback", daemon=True).start()

    def _read(self) -> None:
        leveler = Leveler(TARGET_SR)          # 중계 프로그램(브라우저) 볼륨이 작아도 알맞게
        block = RATE // 20 * CHANNELS * 4     # 50ms
        try:
            while self._proc and self._proc.stdout:
                data = self._proc.stdout.read(block)
                if not data:
                    break
                x = np.frombuffer(data[: len(data) // 8 * 8], dtype=np.float32)
                if x.size == 0:
                    continue
                mono = x.reshape(-1, CHANNELS).mean(axis=1)
                try:
                    self._q.put_nowait(leveler.apply(_resample(mono, RATE)))
                except queue.Full:
                    log.warning("오디오 큐가 가득 찼습니다.")
        finally:
            self._q.put(None)

    def stop(self) -> None:
        if self._proc:
            try:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None
        self._q.put(None)

    def chunks(self) -> Iterator[np.ndarray]:
        while True:
            item = self._q.get()
            if item is None:
                return
            yield item


class PcSoundSource(AudioSource):
    """'PC 소리': 볼륨과 상관없는 프로세스 루프백을 먼저 쓰고, 안 되면 예전 방식(출력 장치 루프백)으로."""

    def __init__(self, device_index: int | None = None) -> None:
        self.device_index = device_index
        self.inner: AudioSource | None = None

    def start(self) -> None:
        # 출력 장치가 하나도 없으면 브라우저가 소리를 낼 곳이 없어 받을 소리도 생기지 않습니다
        if not has_output_device():
            raise AudioUnavailable(NO_OUTPUT_DEVICE)
        try:
            src = AppLoopbackSource()
            src.start()
            self.inner = src
            log.info("PC 소리: 프로세스 루프백 (스피커 볼륨과 상관없이 받음)")
        except Exception as exc:  # noqa: BLE001
            log.warning("프로세스 루프백을 쓸 수 없어 출력 장치 루프백으로 받습니다: %s", exc)
            self.inner = DeviceSource(device_index=self.device_index, kind="loopback")
            self.inner.start()

    def stop(self) -> None:
        if self.inner:
            self.inner.stop()

    def chunks(self) -> Iterator[np.ndarray]:
        return self.inner.chunks() if self.inner else iter(())
