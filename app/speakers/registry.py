"""의원(화자) 명부 + 얼굴/목소리 특징 저장소.

data/
  members.json            메타데이터
  faces/<member_id>/
      face.npy            얼굴 특징 (N, 128)
      voice.npy           목소리 특징 (N, D)
      photo_*.jpg         등록에 쓴 사진 (UI 표시용)
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict

import numpy as np

from .. import paths

log = logging.getLogger(__name__)

def _normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


@dataclass
class Member:
    id: str
    name: str
    # 화면에서 직접 등록한 사람은 의원이 아닐 수 있습니다.
    # 의원 명부 가져오기는 직함을 명시해서 넣으므로 기존 명부에는 영향이 없습니다.
    title: str = ""
    note: str = ""
    photos: list[str] = field(default_factory=list)
    face_count: int = 0
    voice_count: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


class MemberRegistry:
    MAX_FACE = 12
    MAX_VOICE = 24

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.members: dict[str, Member] = {}
        self._face: dict[str, np.ndarray] = {}
        self._voice: dict[str, np.ndarray] = {}
        self._mtime = 0.0
        self.load()

    def refresh_if_changed(self) -> None:
        """다른 프로그램(가져오기 스크립트 등)이 명부를 바꿨으면 다시 읽습니다."""
        try:
            mtime = paths.MEMBERS_FILE.stat().st_mtime
        except OSError:
            return
        if mtime > self._mtime + 0.001:
            self.load()

    # --- 저장/로드 ---
    def load(self) -> None:
        paths.ensure_dirs()
        with self._lock:
            try:
                self._mtime = paths.MEMBERS_FILE.stat().st_mtime
            except OSError:
                self._mtime = 0.0
            self.members.clear()
            # 파일에서 지워진 얼굴·목소리가 메모리에 남아 계속 쓰이던 문제를 막습니다.
            self._face.clear()
            self._voice.clear()
            if paths.MEMBERS_FILE.exists():
                try:
                    data = json.loads(paths.MEMBERS_FILE.read_text("utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = {}
                for m in data.get("members", []):
                    member = Member(**{k: v for k, v in m.items() if k in Member.__annotations__})
                    self.members[member.id] = member
            for mid in self.members:
                d = paths.FACES_DIR / mid
                for kind, store in (("face", self._face), ("voice", self._voice)):
                    f = d / f"{kind}.npy"
                    if f.exists():
                        try:
                            store[mid] = np.load(f)
                        except (OSError, ValueError):
                            pass
                self.members[mid].face_count = len(self._face.get(mid, []))
                self.members[mid].voice_count = len(self._voice.get(mid, []))

    def save(self) -> None:
        with self._lock:
            paths.ensure_dirs()
            payload = {"members": [m.as_dict() for m in self.members.values()]}
            tmp = paths.MEMBERS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(paths.MEMBERS_FILE)
            try:
                self._mtime = paths.MEMBERS_FILE.stat().st_mtime
            except OSError:
                pass

    def _save_vectors(self, mid: str, kind: str) -> None:
        store = self._face if kind == "face" else self._voice
        d = paths.FACES_DIR / mid
        d.mkdir(parents=True, exist_ok=True)
        if mid in store:
            np.save(d / f"{kind}.npy", store[mid])

    # --- 명부 ---
    def list(self) -> list[dict]:
        self.refresh_if_changed()
        with self._lock:
            return [m.as_dict() for m in self.members.values()]

    def names(self) -> list[str]:
        with self._lock:
            return [m.name for m in self.members.values()]

    def get(self, mid: str) -> Member | None:
        return self.members.get(mid)

    def name_of(self, mid: str | None) -> str | None:
        m = self.members.get(mid or "")
        return m.name if m else None

    def add(self, name: str, title: str = "", note: str = "") -> Member:
        with self._lock:
            for m in self.members.values():
                if m.name == name:
                    return m
            mid = uuid.uuid4().hex[:10]
            member = Member(id=mid, name=name, title=title, note=note)
            self.members[mid] = member
            (paths.FACES_DIR / mid).mkdir(parents=True, exist_ok=True)
            self.save()
            return member

    def update(self, mid: str, **fields) -> Member | None:
        with self._lock:
            m = self.members.get(mid)
            if not m:
                return None
            for k, v in fields.items():
                if k in {"name", "title", "note"}:
                    setattr(m, k, v)
            self.save()
            return m

    def delete(self, mid: str) -> None:
        import shutil

        with self._lock:
            self.members.pop(mid, None)
            self._face.pop(mid, None)
            self._voice.pop(mid, None)
            shutil.rmtree(paths.FACES_DIR / mid, ignore_errors=True)
            self.save()

    # --- 특징 등록 ---
    def add_face(self, mid: str, feature: np.ndarray, photo_bgr=None) -> None:
        with self._lock:
            if mid not in self.members:
                return
            f = _normalize(feature)
            cur = self._face.get(mid)
            self._face[mid] = f[None, :] if cur is None else np.vstack([cur, f[None, :]])[-self.MAX_FACE :]
            self._save_vectors(mid, "face")
            self.members[mid].face_count = len(self._face[mid])
            if photo_bgr is not None:
                try:
                    import cv2

                    d = paths.FACES_DIR / mid
                    d.mkdir(parents=True, exist_ok=True)
                    fname = f"photo_{int(time.time())}_{len(self.members[mid].photos)}.jpg"
                    # cv2.imwrite는 한글 경로를 못 쓰므로 직접 저장합니다.
                    ok, buf = cv2.imencode(".jpg", photo_bgr, [cv2.IMWRITE_JPEG_QUALITY, 88])
                    if not ok:
                        raise OSError("jpeg 인코딩 실패")
                    (d / fname).write_bytes(buf.tobytes())
                    self.members[mid].photos.append(fname)
                except Exception:  # noqa: BLE001
                    pass
            self.save()

    # 다른 사람의 성문과 이만큼 비슷하면 '같은 목소리'로 봅니다.
    # (화면에 다른 의원이 잡힌 동안 말한 목소리가 그 의원 이름으로 학습되어
    #  한 목소리가 세 사람에게 똑같이 들어간 일이 있었습니다: 유사도 0.98~1.00)
    SAME_VOICE = 0.80

    def add_voice(self, mid: str, embedding: np.ndarray) -> bool:
        """성문을 더합니다. 실제로 더했으면 True.

        거의 같은 성문이 이미 있거나, 이 목소리가 이미 **다른 사람**의 성문과 같으면 더하지 않습니다.
        """
        with self._lock:
            if mid not in self.members:
                return False
            e = _normalize(embedding)
            cur = self._voice.get(mid)
            if cur is not None and len(cur) and float(np.max(cur @ e)) > 0.97:
                return False    # 거의 같은 벡터는 더 넣지 않음
            for other, mat in self._voice.items():
                if other != mid and mat is not None and len(mat) and float(np.max(mat @ e)) >= self.SAME_VOICE:
                    log.info("다른 사람(%s)의 목소리와 같아 %s 의 성문으로 배우지 않습니다.",
                             self.name_of(other), self.name_of(mid))
                    return False
            self._voice[mid] = e[None, :] if cur is None else np.vstack([cur, e[None, :]])[-self.MAX_VOICE :]
            self._save_vectors(mid, "voice")
            self.members[mid].voice_count = len(self._voice[mid])
            self.save()
            return True

    def forget_similar_voice(self, mid: str, embedding, threshold: float) -> int:  # noqa: ANN001
        """이 목소리와 닮은 mid 의 성문을 모두 뺍니다. ('이름 지우기' — 잘못 배운 목소리를 잊게) 뺀 수."""
        with self._lock:
            cur = self._voice.get(mid)
            if cur is None or not len(cur):
                return 0
            e = _normalize(np.asarray(embedding, dtype=np.float32))
            keep = (cur @ e) < threshold
            n = int((~keep).sum())
            if not n:
                return 0
            if keep.any():
                self._voice[mid] = cur[keep]
                self._save_vectors(mid, "voice")
                self.members[mid].voice_count = int(keep.sum())
                self.save()
            else:
                self.clear_voice(mid)
            return n

    def remove_voice(self, mid: str, embedding) -> bool:  # noqa: ANN001
        """잘못 배운 성문 하나를 뺍니다. ('지정 취소' 때 그때 배운 것만 되돌리려고)"""
        with self._lock:
            cur = self._voice.get(mid)
            if cur is None or not len(cur):
                return False
            e = _normalize(np.asarray(embedding, dtype=np.float32))
            keep = (cur @ e) < 0.999
            if keep.all():
                return False
            if keep.any():
                self._voice[mid] = cur[keep]
                self._save_vectors(mid, "voice")
                self.members[mid].voice_count = int(keep.sum())
                self.save()
            else:
                self.clear_voice(mid)
            return True

    def clear_all_voices(self) -> int:
        """모든 의원의 성문을 지웁니다. (화자가 엉뚱하게 붙을 때 복구용)"""
        with self._lock:
            targets = [mid for mid, v in self._voice.items() if len(v)]
            for mid in targets:
                self.clear_voice(mid)
            return len(targets)

    def clear_voice(self, mid: str) -> None:
        with self._lock:
            self._voice.pop(mid, None)
            f = paths.FACES_DIR / mid / "voice.npy"
            if f.exists():
                f.unlink()
            if mid in self.members:
                self.members[mid].voice_count = 0
            self.save()

    # --- 대조 ---
    def _match(self, store: dict[str, np.ndarray], vec: np.ndarray, threshold: float) -> tuple[str | None, float]:
        v = _normalize(vec)
        best_id, best = None, -1.0
        with self._lock:
            for mid, mat in store.items():
                if mat is None or not len(mat):
                    continue
                score = float(np.max(mat @ v))
                if score > best:
                    best_id, best = mid, score
        if best_id is not None and best >= threshold:
            return best_id, best
        return None, best if best > -1 else 0.0

    def match_face(self, feature: np.ndarray, threshold: float) -> tuple[str | None, float]:
        return self._match(self._face, feature, threshold)

    VOICE_MARGIN = 0.06     # 1등과 2등(다른 사람)의 차이가 이보다 작으면 누구인지 모르는 것으로 봅니다

    def match_voice(self, embedding: np.ndarray, threshold: float) -> tuple[str | None, float]:
        """가장 비슷한 사람. 두 사람이 비슷하게 가까우면(애매하면) 이름을 붙이지 않습니다."""
        v = _normalize(embedding)
        scores: list[tuple[float, str]] = []
        with self._lock:
            for mid, mat in self._voice.items():
                if mat is not None and len(mat):
                    scores.append((float(np.max(mat @ v)), mid))
        if not scores:
            return None, 0.0
        scores.sort(reverse=True)
        best, best_id = scores[0]
        if best < threshold:
            return None, best
        if len(scores) > 1 and best - scores[1][0] < self.VOICE_MARGIN:
            return None, best                  # 애매: 엉뚱한 사람으로 붙이느니 이름 없이 둡니다
        return best_id, best

    def purge_shared_voices(self, backup_dir=None) -> dict[str, int]:  # noqa: ANN001
        """여러 사람에게 똑같이 들어간(누구 것인지 알 수 없는) 성문을 모두에게서 뺍니다. {이름: 뺀 수}"""
        import shutil

        removed: dict[str, int] = {}
        with self._lock:
            mids = [m for m, v in self._voice.items() if v is not None and len(v)]
            drop: dict[str, set[int]] = {}
            for i, a in enumerate(mids):
                for b in mids[i + 1:]:
                    sim = self._voice[a] @ self._voice[b].T
                    ia, ib = np.nonzero(sim >= self.SAME_VOICE)
                    if len(ia):
                        drop.setdefault(a, set()).update(int(x) for x in ia)
                        drop.setdefault(b, set()).update(int(x) for x in ib)
            if not drop:
                return removed
            if backup_dir:
                for mid in drop:
                    f = paths.FACES_DIR / mid / "voice.npy"
                    if f.exists():
                        dest = backup_dir / mid
                        dest.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, dest / "voice.npy")
            for mid, idx in drop.items():
                mat = self._voice[mid]
                keep = np.array([i not in idx for i in range(len(mat))])
                removed[self.name_of(mid) or mid] = int((~keep).sum())
                if keep.any():
                    self._voice[mid] = mat[keep]
                    self._save_vectors(mid, "voice")
                    self.members[mid].voice_count = int(keep.sum())
                else:
                    self._voice.pop(mid, None)
                    f = paths.FACES_DIR / mid / "voice.npy"
                    if f.exists():
                        f.unlink()
                    self.members[mid].voice_count = 0
            self.save()
        return removed

    def has_faces(self) -> bool:
        return any(len(v) for v in self._face.values())

    def has_voices(self) -> bool:
        return any(len(v) for v in self._voice.values())


registry = MemberRegistry()
