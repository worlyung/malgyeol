"""화면 속 발언자 얼굴 인식.

의회 중계 화면(브라우저 창)을 주기적으로 캡처해서
가장 크게 잡힌 얼굴 = 지금 발언 중인 의원으로 보고 명부와 대조합니다.

모델: OpenCV Zoo의 YuNet(검출) + SFace(인식). 둘 다 로컬 ONNX입니다.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

from .. import paths
from .registry import MemberRegistry

log = logging.getLogger(__name__)


@dataclass
class FaceObservation:
    t: float                  # 세션 기준 초
    member_id: str | None
    name: str | None
    score: float
    box: tuple[int, int, int, int]


class FaceRecognizer:
    """YuNet + SFace 래퍼."""

    def __init__(self) -> None:
        import cv2

        if not paths.FACE_DETECT_MODEL.exists() or not paths.FACE_RECOG_MODEL.exists():
            raise FileNotFoundError(
                "얼굴 인식 모델이 없습니다. `python -m scripts.download_models` 를 먼저 실행하세요."
            )
        self.cv2 = cv2
        # 경로에 한글이 있으면 OpenCV가 못 읽으므로 ASCII 경로로 바꿔서 넘깁니다.
        self.detector = cv2.FaceDetectorYN.create(
            paths.ascii_safe(paths.FACE_DETECT_MODEL), "", (320, 320), 0.75, 0.3, 5000
        )
        self.recognizer = cv2.FaceRecognizerSF.create(paths.ascii_safe(paths.FACE_RECOG_MODEL), "")

    def detect(self, bgr: np.ndarray) -> np.ndarray:
        h, w = bgr.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(bgr)
        return faces if faces is not None else np.zeros((0, 15), dtype=np.float32)

    def feature(self, bgr: np.ndarray, face_row: np.ndarray) -> np.ndarray:
        aligned = self.recognizer.alignCrop(bgr, face_row)
        feat = self.recognizer.feature(aligned)
        return np.asarray(feat, dtype=np.float32).reshape(-1)

    def crop(self, bgr: np.ndarray, face_row: np.ndarray, margin: float = 0.25) -> np.ndarray:
        x, y, w, h = [int(v) for v in face_row[:4]]
        mx, my = int(w * margin), int(h * margin)
        H, W = bgr.shape[:2]
        x0, y0 = max(0, x - mx), max(0, y - my)
        x1, y1 = min(W, x + w + mx), min(H, y + h + my)
        return bgr[y0:y1, x0:x1].copy()


class ScreenGrabber:
    """mss로 화면 일부를 캡처."""

    def __init__(self, monitor: int = 1, region: list[int] | None = None):
        import mss

        self._mss = mss.mss()
        mons = self._mss.monitors
        idx = monitor if 0 < monitor < len(mons) else (1 if len(mons) > 1 else 0)
        base = mons[idx]
        if region and len(region) == 4 and region[2] > 10 and region[3] > 10:
            self.area = {"left": int(region[0]), "top": int(region[1]),
                         "width": int(region[2]), "height": int(region[3])}
        else:
            self.area = dict(base)
        self.area.pop("monitor", None)

    def grab(self) -> np.ndarray:
        shot = self._mss.grab(self.area)
        frame = np.asarray(shot)[:, :, :3]     # BGRA -> BGR
        return np.ascontiguousarray(frame)

    def close(self) -> None:
        try:
            self._mss.close()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def monitors() -> list[dict]:
        import mss

        with mss.mss() as m:
            out = []
            for i, mon in enumerate(m.monitors):
                out.append({"index": i, "label": "전체 화면" if i == 0 else f"모니터 {i}", **mon})
            return out


class FaceWatcher:
    """백그라운드로 화면을 보며 '지금 누가 보이는지' 기록."""

    def __init__(self, registry: MemberRegistry, cfg: dict, clock=time.monotonic, on_event=None):
        self.registry = registry
        self.cfg = cfg
        self.clock = clock              # 세션 시작 기준 시각을 돌려주는 함수
        self.on_event = on_event
        self.observations: list[FaceObservation] = []
        self.current: FaceObservation | None = None
        self.preview_jpeg: bytes | None = None
        self.last_faces: list[dict] = []     # 등록 UI용 (얼굴 크롭 + 특징)
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._recognizer: FaceRecognizer | None = None

    # --- 수명주기 ---
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="face-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # --- 본체 ---
    def _run(self) -> None:
        import cv2

        grabber = None
        try:
            self._recognizer = FaceRecognizer()
            grabber = ScreenGrabber(self.cfg.get("face_monitor", 1), self.cfg.get("face_region"))
            fps = float(self.cfg.get("face_fps", 2.0)) or 2.0
            interval = 1.0 / max(0.2, fps)
            threshold = float(self.cfg.get("face_threshold", 0.36))
            self.error = None

            while not self._stop.is_set():
                t_start = time.monotonic()
                try:
                    frame = grabber.grab()
                    self._process(frame, threshold, cv2)
                except Exception as exc:  # noqa: BLE001 - 캡처 실패로 세션이 죽지 않게
                    log.debug("얼굴 인식 루프 오류", exc_info=True)
                    self.error = str(exc)
                wait = interval - (time.monotonic() - t_start)
                if wait > 0:
                    self._stop.wait(wait)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            log.warning("얼굴 인식을 시작하지 못했습니다: %s", exc)
        finally:
            if grabber:
                grabber.close()

    def _process(self, frame: np.ndarray, threshold: float, cv2) -> None:  # noqa: ANN001
        h, w = frame.shape[:2]
        scale = 1.0
        if w > 1280:
            scale = 1280 / w
            frame_small = cv2.resize(frame, (1280, int(h * scale)))
        else:
            frame_small = frame

        faces = self._recognizer.detect(frame_small)
        t = self.clock()
        observation: FaceObservation | None = None
        crops: list[dict] = []

        # 가장 큰 얼굴 = 화면이 잡고 있는 발언자
        order = sorted(range(len(faces)), key=lambda i: -(faces[i][2] * faces[i][3]))
        for rank, i in enumerate(order[:5]):
            row = faces[i]
            try:
                feat = self._recognizer.feature(frame_small, row)
            except Exception:  # noqa: BLE001
                continue
            mid, score = self.registry.match_face(feat, threshold)
            box = tuple(int(v) for v in row[:4])
            crop = self._recognizer.crop(frame_small, row)
            ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
            crops.append(
                {
                    "rank": rank,
                    "box": box,
                    "member_id": mid,
                    "name": self.registry.name_of(mid),
                    "score": round(float(score), 3),
                    "feature": feat.tolist(),
                    "jpeg": bytes(buf) if ok else b"",
                }
            )
            if rank == 0:
                observation = FaceObservation(t, mid, self.registry.name_of(mid), float(score), box)

        with self._lock:
            self.last_faces = crops
            if observation is not None:
                self.current = observation
                self.observations.append(observation)
                if len(self.observations) > 20000:
                    del self.observations[:5000]
            else:
                self.current = None
            self.preview_jpeg = self._render_preview(frame_small, faces, crops, cv2)

        if observation is not None and self.on_event:
            try:
                self.on_event(observation)
            except Exception:  # noqa: BLE001
                log.debug("얼굴 이벤트 콜백 오류", exc_info=True)

    def _render_preview(self, frame, faces, crops, cv2) -> bytes | None:  # noqa: ANN001
        try:
            preview = frame.copy()
            for c in crops:
                x, y, w, h = c["box"]
                known = c["member_id"] is not None
                color = (80, 220, 80) if known else (120, 120, 200)
                cv2.rectangle(preview, (x, y), (x + w, y + h), color, 2)
            max_w = 640
            if preview.shape[1] > max_w:
                r = max_w / preview.shape[1]
                preview = cv2.resize(preview, (max_w, int(preview.shape[0] * r)))
            ok, buf = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 70])
            return bytes(buf) if ok else None
        except Exception:  # noqa: BLE001
            return None

    # --- 조회 ---
    def snapshot(self) -> dict:
        with self._lock:
            cur = self.current
            return {
                "running": self.running,
                "error": self.error,
                "current": None
                if cur is None
                else {"name": cur.name, "member_id": cur.member_id, "score": round(cur.score, 3)},
                "faces": [
                    {k: v for k, v in c.items() if k not in {"feature", "jpeg"}} for c in self.last_faces
                ],
            }

    def candidates(self) -> list[dict]:
        """등록 UI에서 쓸 현재 화면의 얼굴들."""
        import base64

        with self._lock:
            return [
                {
                    "rank": c["rank"],
                    "name": c["name"],
                    "member_id": c["member_id"],
                    "score": c["score"],
                    "feature": c["feature"],
                    "image": "data:image/jpeg;base64," + base64.b64encode(c["jpeg"]).decode()
                    if c["jpeg"]
                    else None,
                }
                for c in self.last_faces
            ]

    def take_face(self, rank: int = 0) -> dict | None:
        """등록용으로 현재 화면의 얼굴 하나(특징 + 사진)를 꺼냅니다."""
        with self._lock:
            for c in self.last_faces:
                if c["rank"] == rank:
                    return dict(c)
        return None

    def presence_ratio(self, start: float, end: float, member_id: str) -> float:
        """구간 [start, end] 동안 그 사람이 화면에 보인 비율 (성문 자동 학습 판단용)."""
        with self._lock:
            obs = [o for o in self.observations if start - 0.5 <= o.t <= end + 0.5]
        if not obs:
            return 0.0
        return sum(1 for o in obs if o.member_id == member_id) / len(obs)

    def vote(self, start: float, end: float, min_ratio: float = 0.34) -> tuple[str | None, float]:
        """구간 [start, end] 동안 가장 많이 보인 인물."""
        with self._lock:
            obs = [o for o in self.observations if start - 0.5 <= o.t <= end + 0.5]
        if not obs:
            return None, 0.0
        counts: dict[str, list[float]] = {}
        for o in obs:
            if o.member_id:
                counts.setdefault(o.member_id, []).append(o.score)
        if not counts:
            return None, 0.0
        mid, scores = max(counts.items(), key=lambda kv: (len(kv[1]), sum(kv[1])))
        ratio = len(scores) / len(obs)
        if ratio < min_ratio:
            return None, ratio
        return mid, float(sum(scores) / len(scores))
