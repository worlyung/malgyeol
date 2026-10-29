"""로컬 웹 서버 (FastAPI).

브라우저에서 http://127.0.0.1:8760 으로 접속해 씁니다. 외부에 열지 않습니다.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
import logging
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np
from fastapi import (
    Body, FastAPI, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, export, glossary_import, memo as memo_mod, paths, session as session_mod, summarize, version
from .audio import capture, recorder as rec
from .glossary import Glossary
from .speakers.face import FaceWatcher, ScreenGrabber
from .speakers.registry import registry
from .speakers import voice as voice_mod
from .speakers import resolve as resolve_mod
from .speakers.context import ContextSpeaker

# 기관망의 보안 장비가 암호화 연결을 검사하면 그 장비의 인증서로 바뀌어 옵니다.
# 브라우저는 Windows 인증서 저장소를 믿어서 문제가 없지만, 파이썬은 자기 목록만 믿어
# 'CERTIFICATE_VERIFY_FAILED' 가 납니다. Windows 인증서 저장소를 쓰도록 바꿉니다.
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001
    pass

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger("livetranscription")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.loop = asyncio.get_running_loop()
    pump = asyncio.create_task(_pump_events())
    state.loop.run_in_executor(None, _clean_uploads)
    yield
    pump.cancel()


app = FastAPI(title="말결", version=version.VERSION, lifespan=lifespan)


def _clean_uploads() -> None:
    """프로그램을 켤 때 지난번에 올린 파일 원본을 지웁니다. (받아쓰기가 끝나면 원본은 쓰지 않습니다)

    받아쓰는 도중 프로그램이 꺼져 지우지 못한 원본이 쌓이지 않게 합니다. 녹음은 회의 폴더의 audio.wav 로 남아 있습니다.
    """
    folder = paths.DATA_DIR / "uploads"
    if not folder.exists():
        return
    removed = 0
    for f in folder.iterdir():
        try:
            if f.is_file():
                f.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        log.info("지난번에 올린 파일 원본 %d개를 정리했습니다.", removed)


def shutdown(timeout: float = 120.0) -> None:
    """프로그램 창을 닫을 때: 받아쓰는 중인 회의를 멈춰 저장하고 화면 감시를 끕니다.

    마지막 문장 정리(남은 소리 인식)에 시간이 걸릴 수 있어, 저장이 끝날 때까지 timeout 초까지 기다립니다.
    """
    if state.starting:
        state.cancel_start.set()            # 모델을 불러오는 중이면 끝나는 대로 멈춥니다
    sess = state.session
    if sess and sess.state != "finished":
        log.info("창을 닫아 받아쓰기를 멈추고 저장합니다.")
        sess.stop()                          # 이미 멈추는 중이면 바로 돌아오므로 아래에서 끝날 때까지 기다립니다
        deadline = time.time() + timeout
        while sess.state == "stopping" and time.time() < deadline:   # 완료 또는 오류면 끝
            time.sleep(0.2)
    try:
        state.stop_idle_face()
    except Exception:  # noqa: BLE001
        log.debug("화면 감시 종료 오류", exc_info=True)


def _code_stamp() -> float:
    """프로그램 코드(app 폴더)의 가장 최근 수정 시각."""
    base = Path(__file__).resolve().parent
    return max((p.stat().st_mtime for p in base.rglob("*.py")), default=0.0)


# 화면(web)은 새로고침하면 바로 새것이 되지만, 서버 코드는 프로그램을 다시 켜야 바뀝니다.
# 켠 뒤에 코드가 바뀌었으면 화면에 '다시 시작하세요'를 띄워 둘이 어긋나지 않게 합니다.
_STARTED_STAMP = _code_stamp()
paths.ensure_dirs()
UPLOADS = paths.DATA_DIR / "uploads"


class AppState:
    def __init__(self) -> None:
        self.glossary = Glossary()
        self.registry = registry
        self.session: session_mod.Session | None = None
        self.idle_face: FaceWatcher | None = None
        self.engine = None
        self._engine_key: str | None = None
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.events: queue.Queue[dict] = queue.Queue()
        self.summary_status: dict = {"running": False, "message": "", "error": None}
        self.speaker_jobs: set[str] = set()      # 화자 다시 채우기가 도는 회의
        self.memo = ""                            # 받아쓰기 중 적는 메모 (끝나면 녹취록에 반영)
        self.material_terms: list[str] = []       # 회의 자료에서 뽑은 이름·용어 (끝날 때 교정에도 씀)
        self.memo_done: set[str] = set()
        self.lock = threading.Lock()
        # 시작 준비(모델 로딩) 중에도 중지를 누를 수 있도록 따로 표시합니다.
        self.starting = False
        self.cancel_start = threading.Event()

    # --- 엔진 캐시 (모델 재로딩 방지) ---
    def get_engine(self, acfg: dict):
        key = json.dumps(
            {k: acfg.get(k) for k in ("engine", "model", "device", "compute_type", "language",
                                      "beam_size", "cpu_threads", "simulwhisper_path")},
            sort_keys=True,
        )
        if self.engine is None or key != self._engine_key:
            from .asr import build_engine

            self.publish({"type": "log", "message": "음성인식 모델 로딩 중..."})
            try:
                engine = build_engine(acfg)
                engine.warmup()
            except Exception as exc:
                if acfg.get("model", "large-v3-turbo") == "large-v3-turbo":
                    raise
                log.warning("선택한 Whisper 모델을 열지 못해 기본 모델을 사용합니다: %s", exc)
                self.publish({"type": "log", "message": "선택한 Whisper 모델을 열지 못해 기본 모델을 사용합니다."})
                engine = build_engine({**acfg, "model": "large-v3-turbo"})
                engine.warmup()
            self.engine = engine
            self._engine_key = key
            self.publish({"type": "log", "message": f"모델 준비 완료: {getattr(self.engine, 'model_name', '')}"})
        return self.engine

    # --- 이벤트 전달 (작업 스레드 -> 브라우저) ---
    def publish(self, event: dict) -> None:
        self.events.put(event)
        st = event.get("status") if event.get("type") == "status" else None
        if st and st.get("state") == "finished" and st.get("id") and st["id"] not in self.memo_done:
            self.memo_done.add(st["id"])
            if self.memo.strip() or self.material_terms:
                memo, self.memo = self.memo, ""
                threading.Thread(target=_apply_memo, args=(st["id"], memo, list(self.material_terms)),
                                 daemon=True).start()

    # --- 얼굴 감시 (세션이 없을 때 등록용) ---
    def face_watcher(self) -> FaceWatcher | None:
        if self.session and self.session.face:
            return self.session.face
        return self.idle_face

    def start_idle_face(self) -> FaceWatcher:
        cfg = config.load()["speaker"]
        if self.idle_face and self.idle_face.running:
            return self.idle_face
        self.idle_face = FaceWatcher(self.registry, cfg, clock=time.monotonic)
        self.idle_face.start()
        return self.idle_face

    def stop_idle_face(self) -> None:
        if self.idle_face:
            self.idle_face.stop()
            self.idle_face = None


state = AppState()

# 여러 사람에게 똑같이 들어간(누구 것인지 알 수 없는) 성문은 켤 때마다 정리합니다. (지운 것은 백업)
try:
    _purged = registry.purge_shared_voices(backup_dir=paths.FACES_DIR / "_voice_backup_auto")
    if _purged:
        log.info("여러 사람에게 겹친 성문을 정리했습니다: %s", _purged)
except Exception:  # noqa: BLE001
    log.debug("성문 정리 실패", exc_info=True)


# ---------------------------------------------------------------- 웹소켓
def _next_event() -> dict | None:
    # 끝없이 기다리면 이 작업 스레드가 끝나지 않아, 창을 닫아도 프로그램이 꺼지지 않습니다.
    # 잠깐씩만 기다리고 돌아옵니다.
    try:
        return state.events.get(timeout=0.5)
    except queue.Empty:
        return None


async def _pump_events() -> None:
    while True:
        try:
            event = await asyncio.get_running_loop().run_in_executor(None, _next_event)
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.1)
            continue
        if event is None:
            continue
        dead = []
        payload = json.dumps(event, ensure_ascii=False)
        for ws in list(state.clients):
            try:
                await ws.send_text(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            state.clients.discard(ws)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    state.clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "status", "status": _status()}, ensure_ascii=False))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        state.clients.discard(ws)


def _status() -> dict:
    if state.starting:
        return {"state": "starting", "face": {"running": False}}
    if state.session:
        return state.session.status()
    face = state.face_watcher()
    return {
        "state": "idle",
        "face": face.snapshot() if face else {"running": False},
        "engine": getattr(state.engine, "name", None),
        "model": getattr(state.engine, "model_name", ""),
    }


# ---------------------------------------------------------------- 정적 파일
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """화면 파일에 버전(수정시각)을 붙여 내보냅니다.

    이렇게 하지 않으면 프로그램을 갱신해도 브라우저가 옛 화면을 계속 씁니다.
    """
    html = (paths.WEB_DIR / "index.html").read_text("utf-8")
    for name in ("app.js", "style.css"):
        f = paths.WEB_DIR / name
        version = int(f.stat().st_mtime) if f.exists() else 0
        html = html.replace(f"/static/{name}", f"/static/{name}?v={version}")
    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})


class NoCacheStatic(StaticFiles):
    """프로그램을 갱신했을 때 브라우저가 옛 화면을 쓰지 않도록 합니다."""

    def file_response(self, *args, **kwargs):  # noqa: ANN002, ANN003
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


app.mount("/static", NoCacheStatic(directory=str(paths.WEB_DIR)), name="static")


# ---------------------------------------------------------------- 설정 / 장치
@app.get("/api/state")
async def api_state() -> dict:
    return {
        "version": version.VERSION,
        "status": _status(),
        "settings": config.public(),
        "glossary": state.glossary.as_dict(),
        "members": state.registry.list(),
        "sessions": session_mod.list_sessions(),
        "voice_error": voice_mod.embedder_error(),
        "restart_needed": _code_stamp() > _STARTED_STAMP + 1,
    }


@app.get("/api/restart-needed")
async def api_restart_needed() -> dict:
    return {"restart_needed": _code_stamp() > _STARTED_STAMP + 1}


@app.get("/api/update")
async def api_update(force: bool = False) -> dict:
    """GitLab 에 새 버전이 올라왔는지 확인합니다. (인터넷이 없으면 조용히 넘어갑니다)"""
    return await asyncio.get_running_loop().run_in_executor(None, version.check, force)


@app.get("/api/devices")
async def api_devices() -> dict:
    try:
        return {"devices": [d.as_dict() for d in capture.list_devices()]}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"오디오 장치를 읽지 못했습니다: {exc}") from exc


@app.get("/api/monitors")
async def api_monitors() -> dict:
    try:
        return {"monitors": ScreenGrabber.monitors()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"모니터 목록을 읽지 못했습니다: {exc}") from exc


@app.get("/api/settings")
async def api_get_settings() -> dict:
    return config.public()


@app.post("/api/settings")
async def api_set_settings(patch: dict = Body(...)) -> dict:
    # 가려진 키(****1234)가 되돌아오면 무시합니다.
    key = (patch.get("summary") or {}).get("api_key")
    if key and set(key[:-4]) == {"*"}:
        patch["summary"].pop("api_key")
    el = (patch.get("speaker") or {}).get("elevenlabs_key")
    if el and set(el[:-4]) == {"*"}:
        patch["speaker"].pop("elevenlabs_key")
    before = config.load()
    config.save(patch)
    _apply_live_settings(before, config.load())
    return config.public()


def _apply_live_settings(before: dict, after: dict) -> None:
    """실제로 바뀐 설정 중 진행 중인 받아쓰기·화면 감시에 바로 적용할 수 있는 것을 적용합니다."""
    sess = state.session if (state.session and state.session.state == "running") else None
    perf = after["asr"].get("performance")
    if sess and perf != before["asr"].get("performance"):
        sess.apply_performance(perf)
    low = after["asr"].get("low_priority", True)
    if low != before["asr"].get("low_priority", True):
        set_low_priority(bool(low))            # 받아쓰는 중이어도 프로그램 전체에 바로 적용됩니다
    if any(before["speaker"].get(k) != after["speaker"].get(k)
           for k in ("face_region", "face_monitor", "face_threshold")):
        _restart_face()


def _restart_face() -> None:
    """화면 감시 영역·모니터가 바뀌면 감시를 새 설정으로 다시 시작합니다."""
    scfg = config.load()["speaker"]
    sess = state.session if (state.session and state.session.state == "running") else None
    if sess and sess.face:
        sess.cfg["speaker"].update(scfg)
        sess.face.stop()
        sess.face = None
        sess.start_face()
    elif state.idle_face and state.idle_face.running:
        state.stop_idle_face()
        state.start_idle_face()


@app.get("/api/face/screen")
async def api_face_screen(monitor: int = 1) -> dict:
    """화면 영역을 드래그로 고를 수 있도록 모니터 전체를 한 장 찍어 보냅니다."""
    def grab() -> dict:
        import base64

        import cv2

        grabber = ScreenGrabber(monitor, None)
        try:
            frame = grabber.grab()
            area = dict(grabber.area)
        finally:
            grabber.close()
        h, w = frame.shape[:2]
        scale = min(1.0, 1600 / w)                 # 전송량을 줄이려고 줄여 보냅니다 (좌표는 원래 크기 기준)
        if scale < 1.0:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            raise RuntimeError("화면을 이미지로 바꾸지 못했습니다.")
        return {"image": "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode("ascii"),
                "left": area["left"], "top": area["top"], "width": area["width"], "height": area["height"]}
    try:
        return await asyncio.get_running_loop().run_in_executor(None, grab)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"화면을 찍지 못했습니다: {exc}") from exc


# ---------------------------------------------------------------- 세션
@app.post("/api/session/start")
async def api_start(payload: dict = Body(default={})) -> dict:
    # 모델 로딩은 수십 초 걸릴 수 있어 별도 스레드에서 합니다.
    # (그러지 않으면 그동안 서버 전체가 멈춰 '중지'도 받지 못합니다)
    return await asyncio.get_running_loop().run_in_executor(None, _start_session, payload)


def _start_session(payload: dict) -> dict:
    if not state.lock.acquire(blocking=False):
        raise HTTPException(400, "이미 시작하는 중입니다.")
    try:
        if state.session and state.session.state in {"running", "stopping"}:
            raise HTTPException(400, "이미 받아쓰는 중입니다.")
        cfg = config.load()
        source = payload.get("source") or cfg["audio"]["source"]
        device_index = payload.get("device_index", cfg["audio"].get("device_index"))
        file_path = payload.get("file_path") or payload.get("url")
        if source == "file":
            is_url = str(file_path or "").lower().startswith(("http://", "https://"))
            if not file_path or (not is_url and not Path(file_path).exists()):
                raise HTTPException(400, "파일 또는 주소를 찾을 수 없습니다.")

        state.cancel_start.clear()
        state.starting = True
        state.publish({"type": "status", "status": _status()})
        state.stop_idle_face()
        sess = None
        try:
            engine = state.get_engine(cfg["asr"])
            if state.cancel_start.is_set():
                return _start_cancelled()
            sess = session_mod.Session(
                title=payload.get("title", ""),
                cfg=cfg,
                glossary=state.glossary,
                registry=state.registry,
                on_event=state.publish,
                source_kind=source,
                device_index=device_index,
                file_path=file_path,
                start_sec=float(payload.get("start_sec") or 0.0),
                referer=payload.get("referer", ""),
                realtime=bool(payload.get("realtime", False)),
                engine=engine,
            )
            sess.start()
        except capture.AudioUnavailable as exc:   # 출력 장치 없음 등: 안내 문구만 그대로 보여 줍니다
            log.warning("세션 시작 불가: %s", exc)
            _discard_failed(sess)
            state.starting = False
            state.publish({"type": "status", "status": _status()})
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("세션 시작 실패")
            _discard_failed(sess)
            state.starting = False
            state.publish({"type": "status", "status": _status()})
            raise HTTPException(500, f"시작하지 못했습니다: {exc}") from exc
        state.session = sess
        state.starting = False
        config.save({"audio": {"source": source, "device_index": device_index}})
        if state.cancel_start.is_set():       # 준비하는 동안 중지를 눌렀다면 바로 끝냅니다
            sess.stop()
            if not sess.store.segments:       # 아무것도 받아 적지 못했으면 빈 기록을 남기지 않습니다
                session_mod.delete_session(sess.id)
                state.session = None
                return _start_cancelled()
        return {"status": sess.status()}
    finally:
        state.lock.release()


def _discard_failed(sess) -> None:  # noqa: ANN001
    """시작하지 못한 회의는 빈 폴더(녹음 파일 머리만 있는 것)를 남기지 않고 지웁니다."""
    if sess is None:
        return
    try:
        if sess.recorder:
            sess.recorder.close()
    except Exception:  # noqa: BLE001
        log.debug("녹음 파일 닫기 실패", exc_info=True)
    session_mod.delete_session(sess.id)


def _start_cancelled() -> dict:
    state.starting = False
    status = _status()
    state.publish({"type": "status", "status": status})
    state.publish({"type": "log", "message": "시작을 취소했습니다."})
    return {"status": status, "cancelled": True}


@app.post("/api/session/stop")
async def api_stop() -> dict:
    if state.starting:
        # 아직 모델을 불러오는 중: 준비가 끝나는 즉시 멈추도록 표시만 해 둡니다.
        state.cancel_start.set()
        return {"status": {"state": "starting"}, "cancelling": True}
    sess = state.session
    if not sess:
        raise HTTPException(400, "진행 중인 세션이 없습니다.")
    # 마지막 문장 정리(남은 소리 인식)는 몇 초~수십 초 걸릴 수 있어 기다리지 않고 바로 답합니다.
    # 끝나면 'finished' 상태가 웹소켓으로 전달되어 화면이 최신 녹취록을 다시 불러옵니다.
    if sess.state not in {"stopping", "finished"}:
        threading.Thread(target=sess.stop, name="session-stop", daemon=True).start()
    status = sess.status()
    if status["state"] == "running":
        status["state"] = "stopping"
    return {"status": status}


@app.get("/api/session/current")
async def api_current() -> dict:
    if not state.session:
        return {"status": _status(), "segments": []}
    return {"status": state.session.status(), "segments": state.session.store.as_list()}


# ---------------------------------------------------------------- 기록 조회/편집
def _store_for(session_id: str):
    if state.session and state.session.id == session_id:
        return state.session.store, {"id": session_id, "title": state.session.title}
    meta, store = session_mod.load_session(session_id)
    return store, meta


@app.get("/api/sessions")
async def api_sessions() -> dict:
    return {"sessions": session_mod.list_sessions(), "folders": session_mod.list_folders()}


@app.post("/api/folders")
async def api_add_folder(payload: dict = Body(...)) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "폴더 이름이 비어 있습니다.")
    names = session_mod.list_folders()
    if name not in names:
        session_mod.save_folders(names + [name])
    return {"folders": session_mod.list_folders()}


@app.patch("/api/folders")
async def api_move_folder(payload: dict = Body(...)) -> dict:
    """{'old': 이름, 'new': 새 이름} — new 가 비면 폴더만 지우고 회의는 남깁니다."""
    old = str(payload.get("old") or "").strip()
    if not old:
        raise HTTPException(400, "폴더를 고르세요.")
    session_mod.move_folder(old, str(payload.get("new") or "").strip())
    return {"folders": session_mod.list_folders()}


@app.get("/api/sessions/{session_id}")
async def api_session(session_id: str) -> dict:
    try:
        store, meta = _store_for(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "세션을 찾을 수 없습니다.") from exc
    wav = paths.SESSIONS_DIR / session_id / "audio.wav"
    meta = {**session_mod.read_meta(session_id), **meta}    # 진행 중인 회의도 폴더·회의 정보를 함께
    meta["has_audio"] = wav.exists()
    if wav.exists():           # 헤더 44바이트를 뺀 실제 소리 길이
        meta["audio_duration"] = round(max(0, wav.stat().st_size - 44) / (16000 * 2), 2)
    cfg = config.load()
    meta["speaker_modes"] = resolve_mod.clean_modes(
        meta.get("speaker_modes"), resolve_mod.default_modes(cfg["speaker"])
    )
    meta["has_context_key"] = summarize.available(
        {**cfg["summary"], "provider": cfg["speaker"].get("context_provider", "codex_cli")})
    summary_file = paths.SESSIONS_DIR / session_id / "summary.md"
    return {
        "meta": meta,
        "segments": store.as_list(),
        "summary": summary_file.read_text("utf-8") if summary_file.exists() else "",
    }


@app.patch("/api/sessions/{session_id}")
async def api_rename_session(session_id: str, payload: dict = Body(...)) -> dict:
    # 폴더·회의 정보(일시·장소·인원·참석자)는 제목과 따로 고칠 수 있습니다. 비워 두어도 됩니다.
    patch = {}
    if "folder" in payload:
        patch["folder"] = str(payload["folder"] or "").strip()
        if patch["folder"] and patch["folder"] not in session_mod.list_folders():
            session_mod.save_folders(session_mod.list_folders() + [patch["folder"]])
    if isinstance(payload.get("info"), dict):
        patch["info"] = {k: str(payload["info"].get(k) or "").strip()
                         for k in ("date", "place", "count", "attendees")}
    if patch:
        session_mod.save_meta(session_id, patch)
    if "title" not in payload:
        return {"ok": True}
    title = payload.get("title", "").strip()
    if not title:
        raise HTTPException(400, "제목이 비어 있습니다.")
    if state.session and state.session.id == session_id:
        state.session.rename(title)
        return {"ok": True}
    return {"ok": True, "meta": session_mod.save_meta(session_id, {"title": title})}


def _still_writing(session_id: str) -> bool:
    """받아쓰는 중이거나 마지막 문장을 정리하는 중(stopping)인 회의인지. 이때는 녹음·녹취 파일이 아직 바뀝니다."""
    s = state.session
    return bool(s and s.id == session_id and s.state in {"starting", "running", "stopping"})


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str) -> dict:
    if _still_writing(session_id):
        raise HTTPException(400, "받아쓰는 중이거나 마지막 문장을 정리하는 중인 회의는 지울 수 없습니다. 끝난 뒤 지워 주세요.")
    try:
        session_mod.delete_session(session_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


_eleven_lock = threading.Lock()


@app.post("/api/sessions/{session_id}/elevenlabs")
async def api_elevenlabs(session_id: str) -> dict:
    """일레븐랩스로 화자 다시 나누기. 녹음 전체를 보내므로 끝난 회의에서만 합니다."""
    return await asyncio.get_running_loop().run_in_executor(None, _elevenlabs, session_id)


def _elevenlabs(session_id: str) -> dict:
    from .speakers import elevenlabs

    key = config.elevenlabs_key()
    if not key:
        raise HTTPException(400, "설정 → 화자 구분에서 일레븐랩스 API 키를 먼저 넣어 주세요.")
    if state.session and state.session.id == session_id and state.session.state in {"running", "stopping"}:
        raise HTTPException(400, "받아쓰는 중인 회의입니다. 끝난 뒤 다시 눌러 주세요.")
    d = paths.SESSIONS_DIR / session_id
    wav = d / "audio.wav"
    if not wav.exists():
        raise HTTPException(400, "이 회의에는 녹음이 없어 화자를 다시 나눌 수 없습니다.")
    if not _eleven_lock.acquire(blocking=False):
        raise HTTPException(400, "다른 회의의 화자를 나누는 중입니다. 끝난 뒤 다시 눌러 주세요.")
    try:
        state.publish({"type": "log", "message": "일레븐랩스로 녹음을 보내는 중입니다… (1시간 녹음 기준 1~3분)"})
        try:
            words = elevenlabs.fetch_words(wav, key, config.load()["asr"].get("language", "ko"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, str(exc)) from exc
        store, _meta = _store_for(session_id)
        with store._lock:
            backup = d / "transcript.before-elevenlabs.json"
            if not backup.exists():                # 처음 상태만 남겨 둡니다 (되돌릴 때 씀)
                store.save(force=True)
                backup.write_bytes((d / "transcript.json").read_bytes())
            new = elevenlabs.resplit(store.segments, words, lambda **f: session_mod.Segment(
                id=0, speaker_source="manual", **f))
            for i, seg in enumerate(new, 1):
                seg.id = i
            store.segments = new
            store.save(force=True)
        session_mod.save_meta(session_id, {"segments": len(new)})
        speakers = sorted({s.speaker for s in new}, key=lambda x: int(x.split()[-1]))
        return {"ok": True, "segments": len(new), "speakers": speakers}
    finally:
        _eleven_lock.release()


@app.post("/api/sessions/{session_id}/speaker-modes")
async def api_speaker_modes(session_id: str, payload: dict = Body(...)) -> dict:
    """얼굴/목소리/맥락 스위치. 녹취 중이든 끝난 뒤든 녹취록에 바로 다시 적용합니다."""
    return await asyncio.get_running_loop().run_in_executor(None, _set_speaker_modes, session_id, payload)


def _set_speaker_modes(session_id: str, payload: dict) -> dict:
    cfg = config.load()
    live = state.session if (state.session and state.session.id == session_id) else None

    def progress(msg: str) -> None:
        state.publish({"type": "log", "message": msg})

    def finish(changed: list) -> None:
        state.speaker_jobs.discard(session_id)
        state.publish({"type": "segments_update", "session_id": session_id,
                       "segments": [s.as_dict() for s in changed]})
        state.publish({"type": "log", "message": "화자 다시 채우기를 마쳤습니다."})

    if live:
        changed, notes = live.set_modes(payload)
        job = live.needs_backfill() and session_id not in state.speaker_jobs
        if job:
            state.speaker_jobs.add(session_id)

            def run_live() -> None:
                try:
                    finish(live.backfill(progress))
                except Exception as exc:  # noqa: BLE001
                    log.exception("화자 다시 채우기 실패")
                    state.speaker_jobs.discard(session_id)
                    progress(f"화자 다시 채우기 실패: {exc}")

            threading.Thread(target=run_live, daemon=True).start()
        return {"modes": live.modes, "segments": [s.as_dict() for s in changed], "notes": notes, "job": job}

    try:
        meta, store = session_mod.load_session(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, "세션을 찾을 수 없습니다.") from exc
    if session_id in state.speaker_jobs:
        raise HTTPException(400, "이 회의의 화자를 채우는 중입니다. 잠시 후 다시 해 주세요.")
    old = resolve_mod.clean_modes(meta.get("speaker_modes"), resolve_mod.default_modes(cfg["speaker"]))
    modes = resolve_mod.clean_modes(payload, old)
    session_mod.save_meta(session_id, {"speaker_modes": modes})
    changed = resolve_mod.resolve_all(store.segments, modes)
    store.mark_dirty()
    store.save()

    notes: list[str] = []
    if modes["face"] and not old["face"] and not any(s.cands.get("face") for s in store.segments):
        notes.append("이 회의에는 얼굴 인식 기록이 없습니다. (얼굴은 녹취하는 동안에만 알아볼 수 있습니다)")
    context_cfg = {**cfg["summary"], "provider": cfg["speaker"].get("context_provider", "codex_cli")}
    has_key = summarize.available(context_cfg)
    # 맥락(자기소개·사회자 지명)은 규칙으로 찾으므로 AI 키 없이도 됩니다. (키가 있으면 규칙으로 못 찾은 지명만 AI에게)
    want = [m for m in ("voice", "context") if modes[m]]
    job = any(m not in s.checked for s in store.segments for m in want if s.speaker_source != "manual")
    if job:
        state.speaker_jobs.add(session_id)

        def run_archive() -> None:
            try:
                ctx = ContextSpeaker(cfg["summary"], cfg["speaker"].get("context_model", ""),
                                     provider=context_cfg["provider"]) \
                    if "context" in want and has_key else None
                fill = resolve_mod.Backfill(store.segments, paths.SESSIONS_DIR / session_id / "audio.wav",
                                            state.registry, cfg, context=ctx, progress=progress)
                if modes["voice"]:
                    fill.voice()
                if "context" in want:
                    fill.context_scan()
                if ctx:
                    ctx.close()
                changed2 = resolve_mod.resolve_all(store.segments, modes)
                store.mark_dirty()
                store.save()
                finish(changed2)
            except Exception as exc:  # noqa: BLE001
                log.exception("화자 다시 채우기 실패")
                state.speaker_jobs.discard(session_id)
                progress(f"화자 다시 채우기 실패: {exc}")

        threading.Thread(target=run_archive, daemon=True).start()
    return {"modes": modes, "segments": [s.as_dict() for s in changed], "notes": notes, "job": job}


@app.patch("/api/sessions/{session_id}/segments/{seg_id}")
async def api_edit_segment(session_id: str, seg_id: int, payload: dict = Body(...)) -> dict:
    store, _ = _store_for(session_id)
    fields = {k: v for k, v in payload.items() if k in {"text", "speaker", "speaker_id"}}
    if "speaker" in fields:
        fields["speaker_source"] = "manual"
    seg = store.update(seg_id, **fields)
    if not seg:
        raise HTTPException(404, "문장을 찾을 수 없습니다.")
    store.save(force=True)
    return {"segment": seg.as_dict()}


@app.delete("/api/sessions/{session_id}/segments/{seg_id}")
async def api_delete_segment(session_id: str, seg_id: int) -> dict:
    store, _ = _store_for(session_id)
    ok = store.delete(seg_id)
    store.save(force=True)
    return {"ok": ok}


@app.post("/api/sessions/{session_id}/segments/{seg_id}/speaker/clear")
async def api_clear_speaker_name(session_id: str, seg_id: int, payload: dict = Body(default={})) -> dict:
    """잘못 붙은 이름을 지웁니다. '화자 N'(이름 모를 목소리 번호)으로 돌아갑니다.

    all=True 면 이 회의에서 같은 이름이 붙은 문장을 모두. 목소리로 붙은 이름이면 그 사람의 성문 중
    이 목소리와 닮은 것도 지워, 다음 회의에서 같은 실수를 하지 않게 합니다.
    """
    return await asyncio.get_running_loop().run_in_executor(None, _clear_speaker_name, session_id, seg_id, payload)


def _clear_speaker_name(session_id: str, seg_id: int, payload: dict) -> dict:
    live = state.session if (state.session and state.session.id == session_id) else None
    store, meta = _store_for(session_id)
    seg = store.get(seg_id)
    if not seg:
        raise HTTPException(404, "문장을 찾을 수 없습니다.")
    mid, name = seg.speaker_id, seg.speaker
    if bool(payload.get("all")) and mid:
        targets = [s for s in store.segments if s.speaker_id == mid]
    else:
        targets = [seg]
    by_voice = [s for s in targets if s.speaker_source == "voice" and s.speaker_id]

    forgot = 0
    for s in targets:
        resolve_mod.migrate(s)
        # 직접 지정하면서 배운 목소리가 있으면 그것도 잊습니다
        if s.speaker_id and (s.undo or {}).get("voice") and state.registry.remove_voice(s.speaker_id, s.undo["voice"]):
            forgot += 1
        s.cands = {}
        s.undo = {}
        s.follow = False
        s.speaker_source = "none"
        if not s.anon:                        # 목소리 번호가 없던 문장은 지운 사람 단위로 새 번호를 줍니다
            s.anon = f"지움-{mid or s.id}"
        if resolve_mod.CLEARED not in s.checked:
            s.checked.append(resolve_mod.CLEARED)

    # 목소리로 잘못 붙었다면 그 사람의 성문에서 이 목소리를 잊게 합니다
    if mid and by_voice:
        th = float(config.load()["speaker"].get("voice_threshold", 0.65)) - 0.05
        wav = paths.SESSIONS_DIR / session_id / "audio.wav"
        embedder = voice_mod.get_embedder()
        for s in by_voice[:5]:
            emb = live._seg_embs.get(s.id) if live else None          # noqa: SLF001
            if emb is None and embedder and wav.exists():
                try:
                    emb = embedder.embed(rec.read_samples(wav, s.start, min(s.end, s.start + 12)))
                except Exception:  # noqa: BLE001
                    emb = None
            if emb is not None:
                forgot += state.registry.forget_similar_voice(mid, emb, th)

    if live:
        modes, anon_map = live.modes, live._anon_map                    # noqa: SLF001
    else:
        modes = resolve_mod.clean_modes((meta or {}).get("speaker_modes"),
                                        resolve_mod.default_modes(config.load()["speaker"]))
        anon_map = None
    changed = resolve_mod.resolve_all(store.segments, modes, anon_map=anon_map)
    for s in targets:                         # 이미 '화자 N'이던 문장도 화면에 다시 칠하도록
        if s not in changed:
            changed.append(s)
    store.mark_dirty()
    store.save(force=True)
    return {"name": name, "cleared": len(targets), "forgot_voices": forgot,
            "segments": [s.as_dict() for s in changed]}


@app.delete("/api/sessions/{session_id}/segments/{seg_id}/speaker")
async def api_clear_speaker(session_id: str, seg_id: int) -> dict:
    """직접 지정한 화자를 취소합니다. 얼굴·목소리·맥락으로 알아본 원래 화자로 되돌립니다."""
    live = state.session if (state.session and state.session.id == session_id) else None
    store, meta = _store_for(session_id)
    seg = store.get(seg_id)
    if not seg:
        raise HTTPException(404, "문장을 찾을 수 없습니다.")
    if seg.speaker_source != "manual":
        raise HTTPException(400, "직접 지정한 화자가 아닙니다.")
    if live:
        modes = live.modes
    else:
        modes = resolve_mod.clean_modes((meta or {}).get("speaker_modes"),
                                        resolve_mod.default_modes(config.load()["speaker"]))
    undo = seg.undo or {}
    changed = []
    # 1) 함께 바뀌었던 다른 문장을 원래 화자로 (그 뒤 사람이 다시 고친 문장은 건드리지 않습니다)
    mid = undo.get("member_id")
    for snap in undo.get("others", []):
        other = store.get(snap["id"])
        if not other or other.speaker_id != mid:
            continue
        other.speaker, other.speaker_id = snap["speaker"], snap["speaker_id"]
        other.speaker_source, other.speaker_score = snap["speaker_source"], snap["speaker_score"]
        if snap.get("voice_cand"):
            other.cands["voice"] = snap["voice_cand"]
        else:
            other.cands.pop("voice", None)
        changed.append(other)
    # 2) 그때 배운 성문을 뺍니다
    unlearned = bool(undo.get("voice") and mid and state.registry.remove_voice(mid, undo["voice"]))
    # 3) 이번 지정으로 새로 생긴 이름은, 아무 데도 쓰이지 않으면 명부·용어집에서도 뺍니다
    removed_member = False
    m = state.registry.get(mid) if mid else None
    if m and undo.get("new_member"):
        used = any(o.speaker_id == mid for o in store.segments if o.id != seg.id)
        if not used and not m.face_count and not m.voice_count:
            state.registry.delete(mid)
            removed_member = True
            if undo.get("new_term"):
                terms = [t.as_dict() for t in state.glossary.terms if t.text != m.name]
                state.glossary.replace_all(terms)
    # 4) 이 문장은 얼굴·목소리·맥락으로 알아본 화자로
    seg.undo = {}
    seg.speaker_source = "none"
    idx = store.segments.index(seg)
    prev = store.segments[idx - 1] if idx > 0 else None
    resolve_mod.resolve_one(seg, prev, modes, live._anon_map if live else None)
    store.mark_dirty()
    store.save(force=True)
    return {"segment": seg.as_dict(), "others": [o.as_dict() for o in changed], "unlearned": unlearned,
            "removed_member": removed_member}


@app.post("/api/sessions/{session_id}/segments/{seg_id}/speaker")
async def api_set_speaker(session_id: str, seg_id: int, payload: dict = Body(...)) -> dict:
    """이 발언의 화자를 사람이 직접 정합니다.

    정해 준 이름의 '목소리'를 그 자리에서 학습해서
      - 같은 회의 안에서 아직 이름이 없는(화자 1, 화자 미상) 발언 중 목소리가 같은 것들도 함께 바꾸고
      - 이후 들어오는 발언도 목소리만으로 같은 이름이 붙게 합니다.
    """
    name = (payload.get("name") or "").strip()
    # 이름 뒤에 '모두'를 붙이면 (예: '김민준 의원 모두') 같은 화자로 잡힌 문장을 한꺼번에 바꿉니다
    apply_same = False
    if name.endswith(" 모두"):
        name, apply_same = name[:-3].strip(), True
    if not name:
        raise HTTPException(400, "이름이 비어 있습니다.")

    live = state.session if (state.session and state.session.id == session_id) else None
    store, _meta = _store_for(session_id)
    seg = store.get(seg_id)
    if not seg:
        raise HTTPException(404, "문장을 찾을 수 없습니다.")
    # '화자 1'처럼 번호만 붙은 화자는 한 사람이므로, 이름을 정하면 그 번호 문장 전체를 바꿉니다
    if re.fullmatch(r"화자 \d+", seg.speaker or ""):
        apply_same = True

    # 직함을 입력하지 않은 직접 지정 화자는 이름만 표시합니다.
    existed = name.split()[0] in state.registry.names()
    had_term = any(t.text == name.split()[0] for t in state.glossary.terms)
    member = state.registry.add(name.split()[0], " ".join(name.split()[1:]))
    state.glossary.add(member.name, "의원")

    applied = [{"id": seg.id, "speaker": name, "speaker_source": "manual"}]
    old = seg.speaker
    resolve_mod.migrate(seg)       # 예전 녹취록: 원래 알아본 화자를 남겨 둬야 '지정 취소' 때 되돌릴 수 있습니다
    # '지정 취소' 때 되돌릴 수 있도록 함께 바꾸는 것을 모두 적어 둡니다
    undo = {"member_id": member.id, "others": [], "voice": None,
            "new_member": not existed, "new_term": not had_term}
    store.update(seg.id, speaker=name, speaker_id=member.id, speaker_source="manual")
    # 같은 화자로 잡힌 문장도 한꺼번에 바꿉니다. ('화자 미상'은 한 사람이 아니므로 제외)
    if apply_same and old and old != "화자 미상" and old != name:
        for other in list(store.segments):
            if other.id != seg.id and other.speaker == old:
                undo["others"].append(_snapshot(other))
                resolve_mod.migrate(other)
                store.update(other.id, speaker=name, speaker_id=member.id, speaker_source="manual")
                applied.append({"id": other.id, "speaker": name, "speaker_source": "manual"})

    seg.undo = undo
    store.save(force=True)
    if live:
        live._last_speaker = (name, member.id, "manual")   # noqa: SLF001
        # 받아쓰는 중이면 앞으로 들어올 같은 번호('화자 1') 문장에도 이 이름이 붙게 합니다
        if apply_same:
            for k, v in list(live._anon_map.items()):      # noqa: SLF001
                if v == old:
                    live._anon_map[k] = name               # noqa: SLF001

    # 목소리 학습은 회의 전체 문장의 성문을 하나씩 비교하므로 오래 걸립니다.
    # 이름은 지금 바로 돌려주고, 같은 목소리 찾기는 뒤에서 한 뒤 끝나면 화면에 알려 줍니다.
    learning = bool(payload.get("learn", True))
    if learning:
        threading.Thread(target=_learn_voice_later, name="learn-voice", daemon=True,
                         args=(session_id, store, seg, member, name, undo)).start()
    return {"applied": applied, "learned": 0, "learning": learning, "member": member.as_dict()}


def _learn_voice_later(session_id: str, store, seg, member, name: str, undo: dict) -> None:  # noqa: ANN001
    """화자 직접 지정 뒤 목소리 학습 (뒤에서 실행). 바뀐 문장은 segments_update 로 화면에 보냅니다."""
    found: list = []
    try:
        learned = _learn_voice(session_id, store, seg, member, name, found, undo)
    except Exception:  # noqa: BLE001
        log.exception("목소리 학습 실패")
        return
    if not learned:
        return
    store.save(force=True)
    ids = {a["id"] for a in found}
    state.publish({"type": "segments_update", "session_id": session_id,
                   "segments": [s.as_dict() for s in store.segments if s.id in ids]})
    state.publish({"type": "log", "message": f"같은 목소리 {learned}문장도 {name}(으)로 바꿨습니다."})


def _snapshot(seg) -> dict:  # noqa: ANN001
    """되돌리기용: 문장의 화자 관련 값."""
    return {"id": seg.id, "speaker": seg.speaker, "speaker_id": seg.speaker_id,
            "speaker_source": seg.speaker_source, "speaker_score": seg.speaker_score,
            "voice_cand": seg.cands.get("voice")}


def _learn_voice(session_id: str, store, seg, member, name: str, applied: list, undo: dict | None = None) -> int:
    """지정한 발언의 성문을 등록하고, 같은 목소리의 다른 발언에도 이름을 붙입니다."""
    from .speakers import voice as voice_mod

    wav = paths.SESSIONS_DIR / session_id / "audio.wav"
    if not wav.exists():
        return 0
    embedder = voice_mod.get_embedder()
    if not embedder:
        return 0

    try:
        samples = rec.read_samples(wav, seg.start, min(seg.end, seg.start + 12))
        emb = embedder.embed(samples)
    except Exception:  # noqa: BLE001
        log.debug("성문 추출 실패", exc_info=True)
        return 0
    if emb is None:
        return 0

    if state.registry.add_voice(member.id, emb) and undo is not None:
        undo["voice"] = [round(float(x), 6) for x in emb]      # 새로 배운 성문 (취소 때 뺍니다)

    # 같은 회의에서 아직 사람 이름이 없는 발언들을 같은 목소리인지 비교합니다.
    cfg = config.load()["speaker"]
    threshold = float(cfg.get("voice_threshold", 0.65))
    min_sec = float(cfg.get("voice_match_min_sec", 2.5))
    learned = 0
    for other in list(store.segments):
        if other.id == seg.id or other.speaker_source == "manual":
            continue
        if other.speaker_id:                      # 이미 사람 이름이 붙은 것은 건드리지 않습니다
            continue
        if other.end - other.start < min_sec:
            continue
        try:
            other_emb = embedder.embed(rec.read_samples(wav, other.start, min(other.end, other.start + 12)))
        except Exception:  # noqa: BLE001
            continue
        if other_emb is None:
            continue
        if float(np.dot(emb, other_emb)) >= threshold:
            if undo is not None:
                undo["others"].append(_snapshot(other))
            store.update(other.id, speaker=name, speaker_id=member.id, speaker_source="voice")
            other.cands["voice"] = {"id": member.id, "name": name, "score": round(float(np.dot(emb, other_emb)), 3)}
            applied.append({"id": other.id, "speaker": name, "speaker_source": "voice"})
            learned += 1
    return learned


@app.post("/api/sessions/{session_id}/rename-speaker")
async def api_rename_speaker(session_id: str, payload: dict = Body(...)) -> dict:
    store, _ = _store_for(session_id)
    n = store.rename_speaker(payload["old"], payload["new"], payload.get("member_id"))
    store.save(force=True)
    return {"changed": n}


# ---------------------------------------------------------------- 음성 재생
@app.get("/api/sessions/{session_id}/audio")
async def api_audio_slice(session_id: str, start: float = 0.0, end: float = 0.0) -> Response:
    wav_path = paths.SESSIONS_DIR / session_id / "audio.wav"
    if not wav_path.exists():
        raise HTTPException(404, "녹음 파일이 없습니다.")
    if end <= start:
        end = start + 5.0
    data = rec.read_slice(wav_path, start, end)
    return Response(content=data, media_type="audio/wav")


@app.get("/api/sessions/{session_id}/audio/full")
async def api_audio_full(session_id: str) -> FileResponse:
    wav_path = paths.SESSIONS_DIR / session_id / "audio.wav"
    if not wav_path.exists():
        raise HTTPException(404, "녹음 파일이 없습니다.")
    return FileResponse(wav_path, media_type="audio/wav", filename=f"{session_id}.wav")


# ---------------------------------------------------------------- 내보내기
@app.get("/api/sessions/{session_id}/export")
async def api_export(session_id: str, format: str = "txt") -> Response:
    store, meta = _store_for(session_id)
    meta = {**session_mod.read_meta(session_id), **meta}     # 머리말에 넣을 회의 정보
    segments = store.as_list()
    cfg = config.load()["export"]
    title = meta.get("title", session_id)
    ts, spk = bool(cfg.get("timestamp", True)), bool(cfg.get("speaker", True))

    def disposition(ext: str, suffix: str = "") -> dict:
        # 받는 파일 이름을 회의 제목으로 합니다. (한글은 filename*, 옛 프로그램용으로 회의 번호도 함께)
        from urllib.parse import quote
        name = re.sub(r'[\\/:*?"<>|]', "_", "".join(c for c in (title or "") if c >= " ")).strip(" .") or session_id
        return {"Content-Disposition":
                f"attachment; filename=\"{session_id}.{ext}\"; filename*=UTF-8''{quote(name[:120] + suffix)}.{ext}"}

    if format == "txt":
        body = export.to_text(segments, title, ts, spk).encode("utf-8-sig")
        return Response(body, media_type="text/plain; charset=utf-8", headers=disposition("txt"))
    if format == "md":
        body = export.to_md(segments, title, ts, spk, meta).encode("utf-8")
        return Response(body, media_type="text/markdown; charset=utf-8", headers=disposition("md"))
    if format == "srt":
        body = export.to_srt(segments, spk).encode("utf-8-sig")
        return Response(body, media_type="text/plain; charset=utf-8", headers=disposition("srt"))
    if format == "json":
        return Response(export.to_json(segments, meta).encode("utf-8"), media_type="application/json",
                        headers=disposition("json"))
    if format == "docx":
        body = export.to_docx(segments, title, ts, spk, meta)
        return Response(
            body,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers=disposition("docx"),
        )
    if format in {"summary_md", "summary_docx"}:
        f = paths.SESSIONS_DIR / session_id / "summary.md"
        if not f.exists():
            raise HTTPException(404, "아직 AI 요약이 없습니다.")
        summary = f.read_text("utf-8")
        if format == "summary_md":
            return Response(export.summary_md(summary, title).encode("utf-8"),
                            media_type="text/markdown; charset=utf-8", headers=disposition("md", " 요약"))
        return Response(
            export.summary_docx(summary, title),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers=disposition("docx", " 요약"),
        )
    raise HTTPException(400, "지원하지 않는 형식입니다.")


# ---------------------------------------------------------------- 요약
def _run_summary(session_id: str, segments: list[dict], cfg: dict) -> None:
    state.summary_status = {"running": True, "message": "준비 중...", "error": None, "session_id": session_id}
    got: list[str] = []
    last_sent = [0.0]

    def progress(msg: str) -> None:
        state.summary_status["message"] = msg
        state.publish({"type": "summary_progress", "session_id": session_id, "message": msg})

    def on_text(piece: str) -> None:
        # 받은 만큼 화면에 보냅니다. (너무 자주 보내지 않도록 0.15초마다 모아서)
        got.append(piece)
        now = time.monotonic()
        if now - last_sent[0] >= 0.15:
            last_sent[0] = now
            state.publish({"type": "summary_delta", "session_id": session_id, "text": "".join(got)})

    try:
        meta = session_mod.read_meta(session_id)
        info = meta.get("info") or {}
        head = "\n".join([
            "[회의 정보 — 사용자가 적은 값. 빈 칸은 모름]",
            f"회의명: {meta.get('title') or ''}",
            f"일시: {info.get('date', '')}",
            f"장소: {info.get('place', '')}",
            f"참석 인원: {info.get('count', '')}",
            f"참석자: {info.get('attendees', '')}",
            "", "[녹취록]",
        ])
        text = summarize.summarize(head + "\n" + export.plain_transcript(segments), cfg, progress, on_text)
        (paths.SESSIONS_DIR / session_id / "summary.md").write_text(text, "utf-8")
        state.summary_status = {"running": False, "message": "완료", "error": None, "session_id": session_id}
        state.publish({"type": "summary_done", "session_id": session_id, "summary": text})
    except Exception as exc:  # noqa: BLE001
        log.warning("요약 실패: %s", exc)
        msg = summarize.friendly_error(exc)
        state.summary_status = {"running": False, "message": "", "error": msg, "session_id": session_id}
        state.publish({"type": "summary_error", "session_id": session_id, "error": msg})


@app.post("/api/sessions/{session_id}/summary")
async def api_summary(session_id: str) -> dict:
    if state.summary_status["running"]:
        raise HTTPException(400, "이미 요약을 만드는 중입니다.")
    if _still_writing(session_id):            # 마지막 문장이 빠진 요약이 저장되지 않게
        raise HTTPException(400, "받아쓰기가 끝난 뒤에 요약할 수 있습니다. 마지막 문장을 정리하는 중이면 잠시 기다려 주세요.")
    store, _ = _store_for(session_id)
    segments = store.as_list()
    if not segments:
        raise HTTPException(400, "요약할 내용이 없습니다.")
    cfg = config.load()["summary"]
    if not summarize.available(cfg):
        raise HTTPException(400, "로그인된 AI 도구 또는 API 키가 없습니다. 설정에서 연결을 확인하세요.")
    threading.Thread(target=_run_summary, args=(session_id, segments, cfg), daemon=True).start()
    return {"started": True}


def _apply_memo(session_id: str, memo: str, terms: list[str]) -> None:
    state.publish({"type": "memo_progress", "session_id": session_id,
                   "message": "메모·회의 자료로 녹취록을 다듬는 중…"})
    try:
        store, _ = _store_for(session_id)
        r = memo_mod.apply(store, memo, config.load()["summary"], terms)
        state.publish({"type": "memo_done", "session_id": session_id, **r})
    except Exception as exc:  # noqa: BLE001
        log.warning("메모 반영 실패: %s", exc)
        state.memo = memo                       # 메모를 잃지 않도록 칸에 되돌려 둡니다
        state.publish({"type": "memo_error", "session_id": session_id, "memo": memo,
                       "error": summarize.friendly_error(exc)})


@app.get("/api/memo")
async def api_get_memo() -> dict:
    return {"memo": state.memo}


@app.put("/api/memo")
async def api_put_memo(payload: dict = Body(...)) -> dict:
    state.memo = str(payload.get("memo", ""))
    return {"ok": True}


@app.get("/api/material")
async def api_get_material() -> dict:
    return {"terms": state.material_terms}


@app.post("/api/material")
async def api_add_material(files: list[UploadFile] = File(...)) -> dict:
    """회의 자료에서 이름·용어를 뽑아 용어집('회의자료')에 넣습니다. 받아쓰는 동안 비슷한 말 교정에 쓰입니다."""
    texts = []
    for f in files:
        try:
            texts.append(memo_mod.read_material(f.filename or "", await f.read()))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"{f.filename}: {exc}") from exc
    material = "\n\n".join(t for t in texts if t.strip())
    if not material.strip():
        raise HTTPException(400, "자료에서 글자를 찾지 못했습니다. (스캔한 PDF는 글자가 없어 읽지 못합니다)")
    cfg = config.load()["summary"]
    if not summarize.available(cfg):
        raise HTTPException(400, "로그인된 AI 도구 또는 API 키가 없습니다. 설정에서 연결을 확인하세요.")
    try:
        terms = await asyncio.get_running_loop().run_in_executor(None, memo_mod.extract_terms, material, cfg)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "용어를 뽑지 못했습니다: " + summarize.friendly_error(exc)) from exc
    for t in terms:
        state.glossary.add(t, "회의자료")
        if t not in state.material_terms:
            state.material_terms.append(t)
    return {"added": terms, "terms": state.material_terms}


@app.delete("/api/material")
async def api_clear_material() -> dict:
    """끝날 때 교정에 쓰는 목록만 비웁니다. (용어집에 들어간 낱말은 용어집 탭에서 지움)"""
    state.material_terms = []
    return {"terms": []}


@app.get("/api/summary/status")
async def api_summary_status() -> dict:
    return state.summary_status


# ---------------------------------------------------------------- 용어집
@app.get("/api/glossary")
async def api_glossary() -> dict:
    return state.glossary.as_dict()


@app.put("/api/glossary")
async def api_put_glossary(payload: dict = Body(...)) -> dict:
    state.glossary.replace_all(
        payload.get("terms", []),
        payload.get("replacements"),
        payload.get("meeting_note"),
    )
    return state.glossary.as_dict()


@app.post("/api/glossary/bulk")
async def api_glossary_bulk(payload: dict = Body(...)) -> dict:
    """줄바꿈/쉼표로 구분된 목록을 한 번에 추가."""
    text = payload.get("text", "")
    category = payload.get("category", "용어")
    added = 0
    for raw in text.replace(",", "\n").split("\n"):
        term = raw.strip()
        if term:
            state.glossary.add(term, category)
            added += 1
    return {"added": added, "glossary": state.glossary.as_dict()}


@app.post("/api/glossary/import-csv")
async def api_glossary_import(
    file: UploadFile = File(...),
    replace: bool = False,
) -> dict:
    """오인식 사전 CSV를 통째로 들여옵니다."""
    raw = await file.read()
    try:
        result = glossary_import.import_csv(state.glossary, raw, replace=replace)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, f"CSV를 읽지 못했습니다: {exc}") from exc
    return {"result": result, "glossary": state.glossary.as_dict()}


@app.get("/api/glossary/terms.txt")
async def api_glossary_terms_txt() -> Response:
    """등록된 용어를 텍스트 파일로 봅니다. (화면 목록은 길어서 잘리기 때문)"""
    data = state.glossary.as_dict()
    lines = [
        "말결 — 등록된 용어",
        f"용어 {len(data['terms'])}개 · 바꾸기 규칙 {len(data['replacements'])}개",
        "=" * 50,
        "",
    ]
    by_category: dict[str, list[str]] = {}
    for term in data["terms"]:
        label = term["category"]
        by_category.setdefault(label, []).append(term["text"])
    for category in sorted(by_category):
        items = by_category[category]
        lines.append(f"[{category}] {len(items)}개")
        lines += [f"  {x}" for x in sorted(items)]
        lines.append("")

    lines += ["=" * 50, f"[바꾸기 규칙] {len(data['replacements'])}개", ""]
    for rule in data["replacements"]:
        lines.append(f"  {rule.get('from', '')}  →  {rule.get('to', '')}")

    # HTTP 헤더에는 한글을 그대로 넣을 수 없어 RFC 5987 방식으로 적습니다.
    from urllib.parse import quote

    korean = quote("용어목록.txt")
    return Response(
        "\n".join(lines).encode("utf-8-sig"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"inline; filename=\"terms.txt\"; filename*=UTF-8''{korean}"},
    )


@app.get("/api/glossary/export-csv")
async def api_glossary_export() -> Response:
    """지금 용어집을 같은 형식의 CSV로 내보냅니다."""
    import csv
    import io as _io

    buf = _io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["오인식표현", "표준용어", "분류"])
    data = state.glossary.as_dict()
    for rule in data["replacements"]:
        writer.writerow([rule.get("from", ""), rule.get("to", ""), rule.get("note", "")])
    for term in data["terms"]:
        writer.writerow(["", term["text"], term["category"]])
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="glossary.csv"'},
    )


# ---------------------------------------------------------------- 의원 명부 / 얼굴
@app.get("/api/members")
async def api_members() -> dict:
    return {"members": state.registry.list()}


@app.post("/api/members")
async def api_add_member(payload: dict = Body(...)) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "이름이 비어 있습니다.")
    member = state.registry.add(name, (payload.get("title") or "").strip(), payload.get("note", ""))
    if payload.get("add_to_glossary", True):
        state.glossary.add(name, "의원")
    return {"member": member.as_dict()}


@app.patch("/api/members/{member_id}")
async def api_update_member(member_id: str, payload: dict = Body(...)) -> dict:
    member = state.registry.update(member_id, **payload)
    if not member:
        raise HTTPException(404, "의원을 찾을 수 없습니다.")
    return {"member": member.as_dict()}


@app.delete("/api/members/{member_id}")
async def api_delete_member(member_id: str) -> dict:
    state.registry.delete(member_id)
    return {"ok": True}


@app.post("/api/members/{member_id}/voice/clear")
async def api_clear_voice(member_id: str) -> dict:
    state.registry.clear_voice(member_id)
    return {"ok": True}


@app.post("/api/members/voices/clear-all")
async def api_clear_all_voices() -> dict:
    """화자가 엉뚱하게 붙을 때 학습된 성문을 모두 지웁니다."""
    n = state.registry.clear_all_voices()
    return {"cleared": n}


@app.get("/api/face/candidates")
async def api_face_candidates() -> dict:
    watcher = state.face_watcher()
    if not watcher:
        raise HTTPException(400, "얼굴 인식이 꺼져 있습니다. 먼저 화면 감시를 켜세요.")
    return {"faces": watcher.candidates(), "error": watcher.error}


@app.get("/api/face/preview.jpg")
async def api_face_preview() -> Response:
    watcher = state.face_watcher()
    if not watcher or not watcher.preview_jpeg:
        raise HTTPException(404, "미리보기가 없습니다.")
    return Response(watcher.preview_jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/face/watch")
async def api_face_watch(payload: dict = Body(default={})) -> dict:
    if payload.get("on", True):
        sess = state.session if (state.session and state.session.state == "running") else None
        if sess and sess.source_kind != "file":
            sess.start_face()                  # 받아쓰는 중이면 그 세션의 감시를 켭니다 (감시가 두 개 돌지 않게)
            watcher = sess.face
        else:
            watcher = state.start_idle_face()
        await asyncio.sleep(1.2)     # 첫 프레임을 기다림
        return {"running": watcher.running, "error": watcher.error}
    state.stop_idle_face()
    if state.session and state.session.face:      # 진행 중인 세션의 감시도 끕니다
        state.session.face.stop()
        state.session.face = None
    return {"running": False}


@app.post("/api/members/{member_id}/face/from-screen")
async def api_enroll_face_screen(member_id: str, payload: dict = Body(default={})) -> dict:
    watcher = state.face_watcher()
    if not watcher:
        raise HTTPException(400, "화면 감시가 꺼져 있습니다.")
    match = watcher.take_face(int(payload.get("rank", 0)))
    if not match:
        raise HTTPException(404, "선택한 얼굴을 찾지 못했습니다. 화면을 확인하세요.")
    import cv2

    photo = cv2.imdecode(np.frombuffer(match["jpeg"], np.uint8), cv2.IMREAD_COLOR) if match["jpeg"] else None
    state.registry.add_face(member_id, np.asarray(match["feature"], dtype=np.float32), photo)
    return {"member": state.registry.get(member_id).as_dict()}


@app.post("/api/members/{member_id}/face/upload")
async def api_enroll_face_upload(member_id: str, file: UploadFile = File(...)) -> dict:
    import cv2

    from .speakers.face import FaceRecognizer

    data = await file.read()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "이미지를 읽을 수 없습니다.")
    try:
        recognizer = FaceRecognizer()
    except FileNotFoundError as exc:
        raise HTTPException(500, str(exc)) from exc
    faces = recognizer.detect(img)
    if not len(faces):
        raise HTTPException(400, "사진에서 얼굴을 찾지 못했습니다.")
    row = max(faces, key=lambda r: r[2] * r[3])
    feat = recognizer.feature(img, row)
    state.registry.add_face(member_id, feat, recognizer.crop(img, row))
    return {"member": state.registry.get(member_id).as_dict()}


@app.get("/api/members/{member_id}/photo/{name}")
async def api_member_photo(member_id: str, name: str) -> FileResponse:
    f = paths.FACES_DIR / member_id / name
    if not f.exists() or ".." in name:
        raise HTTPException(404, "사진이 없습니다.")
    return FileResponse(f, media_type="image/jpeg")


# ---------------------------------------------------------------- 파일 업로드(받아쓰기용)
@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)) -> dict:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    # 같은 이름을 같은 순간에 올려도 덮어쓰지 않게 고유한 이름을 붙입니다. (받아쓰기가 끝나면 지웁니다)
    import uuid

    dest = UPLOADS / f"{uuid.uuid4().hex[:12]}_{Path(file.filename or 'audio').name}"
    with open(dest, "wb") as fh:
        while chunk := await file.read(1 << 20):
            fh.write(chunk)
    return {"path": str(dest), "name": dest.name}


def set_low_priority(low: bool = True) -> None:
    """다른 프로그램이 느려지지 않도록 우선순위를 낮춥니다(low=False 면 보통으로 되돌림). CPU 총량은 그대로."""
    try:
        if sys.platform == "win32":
            import ctypes

            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            NORMAL_PRIORITY_CLASS = 0x00000020
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.kernel32.SetPriorityClass(
                handle, BELOW_NORMAL_PRIORITY_CLASS if low else NORMAL_PRIORITY_CLASS)
        elif low:
            os.nice(5)                 # 리눅스·맥은 권한 없이 다시 올릴 수 없어 낮추기만 합니다
    except Exception:  # noqa: BLE001
        log.debug("우선순위 조정 실패", exc_info=True)


def main() -> None:
    import uvicorn

    if config.load()["asr"].get("low_priority", True):
        set_low_priority()

    server = config.load().get("server", {})
    host = server.get("host", "127.0.0.1")
    port = int(server.get("port", 8760))
    log.info("주소: http://%s:%d", "127.0.0.1" if host == "0.0.0.0" else host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
