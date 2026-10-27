"""오디오 입력.

- Windows: WASAPI 루프백으로 "PC에서 나는 소리"를 그대로 캡처합니다 (PyAudioWPatch).
- 마이크 입력도 같은 인터페이스로 지원합니다.
- 저장된 영상/음성 파일도 같은 인터페이스로 흘려보낼 수 있습니다 (PyAV).

모든 소스는 16kHz mono float32(-1..1) 청크를 큐에 넣습니다.
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterator

import numpy as np

log = logging.getLogger(__name__)

TARGET_SR = 16000
IS_WINDOWS = sys.platform == "win32"


def _resample(x: np.ndarray, src_sr: int) -> np.ndarray:
    if src_sr == TARGET_SR:
        return x.astype(np.float32, copy=False)
    import soxr

    return soxr.resample(x, src_sr, TARGET_SR).astype(np.float32, copy=False)


@dataclass
class DeviceInfo:
    index: int
    name: str
    kind: str        # "loopback" | "microphone"
    channels: int
    sample_rate: int
    default: bool = False

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "kind": self.kind,
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "default": self.default,
        }


NO_OUTPUT_DEVICE = "이 PC에는 소리 출력 장치가 없어 PC 소리를 받을 수 없습니다. 스피커나 이어폰을 연결해주세요."


class AudioUnavailable(RuntimeError):
    """사용자에게 그대로 보여 줄 소리 입력 문제 (화면에 문구만 띄웁니다)."""


def has_output_device() -> bool:
    """소리 출력 장치(스피커·이어폰·모니터 소리)가 하나라도 있는지. 확인할 수 없으면 있다고 봅니다."""
    if not IS_WINDOWS:
        return True
    try:
        import pyaudiowpatch as pyaudio

        p = pyaudio.PyAudio()
        try:
            wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            if wasapi.get("defaultOutputDevice", -1) >= 0:
                return True
            return any(p.get_device_info_by_index(i).get("maxOutputChannels", 0) > 0
                       and p.get_device_info_by_index(i).get("hostApi") == wasapi["index"]
                       for i in range(p.get_device_count()))
        finally:
            p.terminate()
    except Exception:  # noqa: BLE001
        log.debug("출력 장치 확인 실패", exc_info=True)
        return True


def list_devices() -> list[DeviceInfo]:
    """녹음 가능한 장치 목록. 루프백(스피커 소리) 우선."""
    if not IS_WINDOWS:
        return _list_devices_sounddevice()
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        log.warning("PyAudioWPatch가 없습니다. 루프백 캡처를 쓸 수 없습니다.")
        return _list_devices_sounddevice()

    devices: list[DeviceInfo] = []
    p = pyaudio.PyAudio()
    try:
        try:
            wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_out_name = p.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
        except (OSError, KeyError):
            default_out_name = ""

        for info in p.get_loopback_device_info_generator():
            devices.append(
                DeviceInfo(
                    index=int(info["index"]),
                    name=str(info["name"]).replace(" [Loopback]", ""),
                    kind="loopback",
                    channels=int(info["maxInputChannels"]),
                    sample_rate=int(info["defaultSampleRate"]),
                    default=str(info["name"]).startswith(default_out_name),
                )
            )

        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) < 1 or info.get("isLoopbackDevice"):
                continue
            if info.get("hostApi") != wasapi["index"]:
                continue
            devices.append(
                DeviceInfo(
                    index=int(info["index"]),
                    name=str(info["name"]),
                    kind="microphone",
                    channels=int(info["maxInputChannels"]),
                    sample_rate=int(info["defaultSampleRate"]),
                    default=int(info["index"]) == wasapi.get("defaultInputDevice"),
                )
            )
    finally:
        p.terminate()
    return devices


def _list_devices_sounddevice() -> list[DeviceInfo]:
    try:
        import sounddevice as sd
    except ImportError:
        return []
    out: list[DeviceInfo] = []
    default_in = sd.default.device[0] if sd.default.device else None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] < 1:
            continue
        out.append(
            DeviceInfo(
                index=i,
                name=d["name"],
                kind="microphone",
                channels=int(d["max_input_channels"]),
                sample_rate=int(d["default_samplerate"]),
                default=i == default_in,
            )
        )
    return out


class AudioSource:
    """16kHz mono float32 청크를 내보내는 공통 인터페이스."""

    realtime = True

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def chunks(self) -> Iterator[np.ndarray]:
        raise NotImplementedError


class Leveler:
    """작게 들어온 소리를 음성인식에 알맞은 크기로 자동으로 키웁니다. (PC 소리 = 루프백 전용)

    사무실에서 주변에 들리지 않게 Windows 볼륨을 1~5% 로 낮춰 두면, 모니터 소리 드라이버 등은
    볼륨을 적용한 뒤의 소리를 넘겨 주어 거의 무음처럼 들어옵니다. 소리는 float32 로 받으므로
    작아져도 세밀함이 남아 있어, 다시 키우면 원래 소리와 거의 같아집니다.
    최근 몇 초 동안의 가장 큰 소리를 기준으로 서서히 맞추므로 말소리 사이사이가 들쭉날쭉하지 않습니다.
    (볼륨 0%·음소거는 소리가 아예 오지 않아 키울 수 없습니다)
    """

    TARGET = 0.25        # 맞출 크기 (최근 최대값 기준, 1.0 이 가장 큼)
    MAX_GAIN = 1000.0    # 볼륨을 아주 낮춰 둔 PC 까지 살립니다
    FLOOR = 1e-6         # 이보다 작으면 무음으로 보고 키우지 않습니다 (디지털 잡음까지 키우지 않게)
    HOLD_SEC = 3.0       # 최근 이 시간 안의 가장 큰 소리를 기준으로

    def __init__(self, sample_rate: int) -> None:
        self.peak = 0.0
        self.gain = 1.0
        self.decay = 0.0
        self.sr = sample_rate

    def apply(self, block: np.ndarray) -> np.ndarray:
        if block.size == 0:
            return block
        loud = float(np.max(np.abs(block)))
        # 최근 최대값: 크면 바로 따라가고, 작아지면 HOLD_SEC 에 걸쳐 천천히 내려옵니다
        if not self.decay:
            self.decay = 0.5 ** (block.size / (self.sr * self.HOLD_SEC))
        self.peak = max(loud, self.peak * self.decay)
        if self.peak < self.FLOOR:
            return block
        want = min(self.MAX_GAIN, max(1.0, self.TARGET / self.peak))
        # 키울 때는 천천히, 줄일 때는 바로 (갑자기 큰 소리가 와도 찢어지지 않게)
        self.gain = want if want < self.gain else self.gain + (want - self.gain) * 0.2
        out = block * self.gain
        return np.clip(out, -1.0, 1.0, out=out)


class DeviceSource(AudioSource):
    """WASAPI 루프백 또는 마이크."""

    def __init__(self, device_index: int | None = None, kind: str = "loopback"):
        self.device_index = device_index
        self.kind = kind
        self._q: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=400)
        self._stream = None
        self._pa = None
        self._running = False
        self._src_sr = TARGET_SR
        self._channels = 1

    def _resolve_device(self) -> DeviceInfo:
        devices = list_devices()
        if not devices:
            raise RuntimeError("녹음 가능한 오디오 장치를 찾지 못했습니다.")
        if self.device_index is not None:
            for d in devices:
                if d.index == self.device_index:
                    return d
            log.warning("장치 %s 를 찾지 못해 기본 장치를 씁니다.", self.device_index)
        pool = [d for d in devices if d.kind == self.kind] or devices
        for d in pool:
            if d.default:
                return d
        return pool[0]

    def start(self) -> None:
        dev = self._resolve_device()
        self._src_sr = dev.sample_rate
        self._channels = max(1, dev.channels)
        log.info("오디오 입력: %s (%s, %dch, %dHz)", dev.name, dev.kind, self._channels, self._src_sr)

        if IS_WINDOWS:
            import pyaudiowpatch as pyaudio

            self._pa = pyaudio.PyAudio()
            frames = max(512, int(self._src_sr * 0.05))

            def callback(in_data, frame_count, time_info, status):  # noqa: ANN001
                if self._running:
                    buf = np.frombuffer(in_data, dtype=np.float32)
                    try:
                        self._q.put_nowait(buf)
                    except queue.Full:
                        log.warning("오디오 큐가 가득 찼습니다 (받아쓰기가 입력 속도를 못 따라감).")
                return (None, pyaudio.paContinue)

            self._running = True
            self._stream = self._pa.open(
                format=pyaudio.paFloat32,
                channels=self._channels,
                rate=self._src_sr,
                frames_per_buffer=frames,
                input=True,
                input_device_index=dev.index,
                stream_callback=callback,
            )
            self._stream.start_stream()
        else:
            import sounddevice as sd

            def callback(indata, frames, time_info, status):  # noqa: ANN001
                if self._running:
                    try:
                        self._q.put_nowait(indata.copy().reshape(-1))
                    except queue.Full:
                        pass

            self._running = True
            self._stream = sd.InputStream(
                device=dev.index,
                channels=self._channels,
                samplerate=self._src_sr,
                dtype="float32",
                blocksize=int(self._src_sr * 0.05),
                callback=callback,
            )
            self._stream.start()

    def stop(self) -> None:
        self._running = False
        try:
            if self._stream is not None:
                self._stream.stop_stream() if IS_WINDOWS else self._stream.stop()
                self._stream.close()
        except Exception:  # noqa: BLE001 - 종료 경로에서는 삼킴
            log.debug("오디오 스트림 종료 중 예외", exc_info=True)
        self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None
        self._q.put(None)

    def chunks(self) -> Iterator[np.ndarray]:
        # PC 소리(루프백)만 작게 들어와도 키웁니다. 마이크는 주변 잡음까지 커지므로 그대로 둡니다.
        leveler = Leveler(TARGET_SR) if self.kind == "loopback" else None
        while True:
            item = self._q.get()
            if item is None:
                return
            block = item
            if self._channels > 1:
                block = block.reshape(-1, self._channels).mean(axis=1)
            block = _resample(block, self._src_sr)
            yield leveler.apply(block) if leveler else block


BROWSER_HEADERS = {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
}


class FileSource(AudioSource):
    """저장된 영상/음성 파일 또는 인터넷 주소.

    의회 영상 페이지의 mp4 주소를 그대로 넣어도 됩니다. 파일을 통째로 내려받지 않고
    필요한 만큼만 읽어옵니다.
    """

    realtime = False

    def __init__(self, path: str, realtime: bool = False, start_sec: float = 0.0, referer: str = ""):
        self.path = path
        self.realtime = realtime
        self.start_sec = max(0.0, float(start_sec or 0.0))
        self.referer = referer
        self._stop = threading.Event()

    @property
    def is_url(self) -> bool:
        return self.path.lower().startswith(("http://", "https://"))

    def start(self) -> None:
        self._stop.clear()

    def stop(self) -> None:
        self._stop.set()

    def chunks(self) -> Iterator[np.ndarray]:
        import av

        options = {}
        if self.is_url:
            options.update(BROWSER_HEADERS)
            if self.referer:
                options["referer"] = self.referer
            options["reconnect"] = "1"
            options["reconnect_streamed"] = "1"
            options["reconnect_delay_max"] = "5"
        container = av.open(self.path, options=options, timeout=30)
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise RuntimeError("파일에서 오디오 트랙을 찾지 못했습니다.")
        if self.start_sec > 0:
            container.seek(int(self.start_sec * av.time_base))
        resampler = av.audio.resampler.AudioResampler(format="flt", layout="mono", rate=TARGET_SR)
        t0 = time.monotonic()
        emitted = 0
        try:
            for frame in container.decode(stream):
                if self._stop.is_set():
                    break
                for out in resampler.resample(frame):
                    samples = out.to_ndarray().reshape(-1).astype(np.float32)
                    emitted += len(samples)
                    if self.realtime:
                        target = emitted / TARGET_SR - (time.monotonic() - t0)
                        if target > 0:
                            time.sleep(min(target, 0.5))
                    yield samples
        finally:
            container.close()


def make_source(
    kind: str,
    device_index: int | None = None,
    file_path: str | None = None,
    start_sec: float = 0.0,
    referer: str = "",
    realtime: bool = False,
) -> AudioSource:
    if kind == "file":
        if not file_path:
            raise ValueError("파일 경로 또는 주소가 필요합니다.")
        # realtime=True 면 실제 재생 속도로 흘려보냅니다(생방송 주소, 부하 측정용).
        return FileSource(file_path, realtime=realtime, start_sec=start_sec, referer=referer)
    if kind in ("loopback", "silent") and IS_WINDOWS:
        # PC 소리: 스피커 볼륨·음소거와 상관없이 받는 방식을 먼저, 안 되면 출력 장치 루프백 (app/audio/pc_sound.py)
        from .pc_sound import PcSoundSource

        return PcSoundSource(device_index)
    return DeviceSource(device_index=device_index, kind="microphone" if kind == "microphone" else "loopback")
