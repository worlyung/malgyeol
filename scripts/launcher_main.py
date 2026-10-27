"""프로그램 실행기 (아이콘이 붙은 exe).

설치 폴더 안의 파이썬으로 프로그램 창을 띄웁니다. 검은 터미널 창은 뜨지 않습니다.

    회의 받아쓰기.exe  ->  python/pythonw.exe -m app.gui
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def base_dir() -> Path:
    """exe 가 놓인 폴더 (= 설치 폴더)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def error(message: str) -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("말결", message)
        root.destroy()
    except Exception:  # noqa: BLE001
        print(message)
    return 1


def main() -> int:
    root = base_dir()
    pythonw = root / "python" / "pythonw.exe"
    python = root / "python" / "python.exe"
    runner = pythonw if pythonw.exists() else python
    if not runner.exists():
        return error(f"프로그램 파일을 찾지 못했습니다.\n{runner}\n\n설치 파일로 다시 설치해 주세요.")

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        subprocess.Popen(
            [str(runner), "-m", "app.gui"],
            cwd=str(root), env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        return error(f"프로그램을 시작하지 못했습니다.\n{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
