"""독립 프로그램 창으로 실행하기.

브라우저를 띄우지 않고, 프로그램 자체 창(WebView2) 안에 화면을 그립니다.
서버는 같은 프로그램 안에서 백그라운드로 돌아갑니다.

    python -m app.gui            # 창으로 실행
    python -m app.main           # 예전처럼 브라우저로 실행 (개발용)
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
import time

from . import config, paths, version
from .main import app as fastapi_app, set_low_priority

WINDOW_TITLE = f"말결 · {version.VERSION}"
APP_ID = "chungnam.council.livecaption"       # 작업표시줄에서 따로 묶이도록


def ensure_output_streams() -> None:
    """pythonw.exe에서 없는 출력 스트림을 시작 로그에 연결합니다.

    uvicorn은 시작할 때 출력 스트림을 확인합니다. Windows의 pythonw.exe는
    stdout/stderr가 None이어서, 그대로 두면 서버 스레드가 창을 띄우기 전에
    종료될 수 있습니다.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    paths.ensure_dirs()
    stream = open(paths.DATA_DIR / "startup.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def free_port(preferred: int) -> int:
    """설정된 포트가 이미 쓰이고 있으면 빈 포트를 찾아 씁니다."""
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferred


def serve(port: int) -> None:
    import uvicorn

    uvicorn.run(fastapi_app, host="127.0.0.1", port=port, log_level="warning")


def wait_until_ready(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def main() -> int:
    ensure_output_streams()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")
    cfg = config.load()
    if cfg["asr"].get("low_priority", True):
        set_low_priority()

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:  # noqa: BLE001
            pass

    port = free_port(int(cfg.get("server", {}).get("port", 8760)))
    threading.Thread(target=serve, args=(port,), daemon=True).start()
    if not wait_until_ready(port):
        print("프로그램을 시작하지 못했습니다.")
        return 1

    try:
        import webview
    except ImportError:
        # 창 기능을 못 쓰면 브라우저로라도 띄웁니다.
        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{port}")
        print(f"창을 열지 못해 브라우저로 엽니다: http://127.0.0.1:{port}")
        input("끝내려면 엔터를 누르세요...")
        return 0

    # 기본값은 다운로드 차단입니다. 켜야 TXT·DOCX·SRT 내보내기와 음성 파일 받기가 됩니다.
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.create_window(
        WINDOW_TITLE,
        f"http://127.0.0.1:{port}",
        width=1280, height=880, min_size=(900, 640),
        confirm_close=False,
    )
    icon = paths.ROOT / "assets" / "icon.ico"
    webview.start(
        private_mode=False,
        storage_path=str(paths.DATA_DIR / "webview"),
        icon=str(icon) if icon.exists() else None,
    )
    finish()
    return 0


def finish(timeout: float = 120.0) -> None:
    """창이 닫혔을 때: 받아쓰는 중인 회의를 저장한 뒤 프로세스를 확실히 끝냅니다.

    서버는 백그라운드 스레드라 스스로 정리 단계를 밟지 못하고, 남은 작업 스레드가 하나라도 있으면
    파이썬이 그 스레드를 기다리느라 창이 사라진 뒤에도 프로세스가 남습니다. (작업 관리자에만 보이는 상태)
    저장할 것을 먼저 저장하고(최대 timeout 초), 그다음 남은 스레드와 상관없이 끝냅니다.
    """
    import os

    from .main import shutdown

    worker = threading.Thread(target=shutdown, args=(timeout,), name="shutdown", daemon=True)
    worker.start()
    worker.join(timeout + 5)
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
