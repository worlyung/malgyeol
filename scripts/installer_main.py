"""단일 exe 설치 프로그램 (GUI).

exe 뒤에 붙어 있는 압축 꾸러미를 풀어 설치합니다.
받는 사람은 exe 하나만 있으면 되고, 파이썬을 따로 깔 필요가 없습니다.

    회의받아쓰기_설치.exe  =  [이 설치 프로그램]  +  [프로그램·모델 zip]

검은 터미널 창 없이 설치 창이 뜹니다.
"""
from __future__ import annotations

import json
import base64
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "말결"
FOLDER_NAME = "LiveCaption"
EXE_NAME = "말결.exe"
# 예전 이름. 새 판으로 덮어쓸 때 옛 이름의 바로가기·실행 파일이 남아 아이콘이 둘이 되지 않게 지웁니다.
OLD_NAMES = ("회의 실시간 자막", "회의 받아쓰기")
VERSION_FILE = "버전.txt"
# Windows '설치된 앱' 등록 이름 (scripts/uninstall_main.py 의 REG_KEY 와 같아야 함)
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\LiveCaption"

# 본 프로그램 라이트 모드와 같은 색상 체계
BG = "#f4f7f4"
SURFACE = "#ffffff"
SURFACE_2 = "#eef4ef"
LINE = "#d8e2da"
TEXT = "#203e30"
TEXT_2 = "#3d584a"
DIM = "#627168"
PRIMARY = "#24664b"
PRIMARY_HOVER = "#194d38"
PRIMARY_SOFT = "#dcebe1"
FONT_FAMILY = "Noto Sans KR"
FONT_FILE = "NotoSansKR-Installer.ttf"


def default_target() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / FOLDER_NAME


def shell_folder(folder_id: int) -> Path:
    """Windows가 지정한 바탕화면/시작 메뉴 위치를 사용합니다."""
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, folder_id, None, 0, buffer)
    if result != 0:
        raise OSError(f"Windows 바로가기 폴더를 찾지 못했습니다: {result}")
    return Path(buffer.value).resolve()


def long_path(path: Path) -> str:
    r"""Windows의 260자 경로 제한을 피합니다."""
    text = str(path.resolve())
    if sys.platform == "win32" and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + text
    return text


def human(n: float) -> str:
    return f"{n / 1048576:.0f}MB" if n < 1073741824 else f"{n / 1073741824:.1f}GB"


def app_command(target: Path) -> list[str]:
    """설치 폴더에서 앱을 실행할 명령을 돌려줍니다.

    별도 실행기 exe가 일부 PC에서 시작 직후 종료되는 경우가 있어, 설치된
    파이썬으로 앱을 직접 실행하는 경로를 우선합니다.
    """
    for name in ("pythonw.exe", "python.exe"):
        runner = target / "python" / name
        if runner.exists():
            return [str(runner), "-m", "app.gui"]
    launcher = target / EXE_NAME
    if launcher.exists():
        return [str(launcher)]
    batch = target / "실행.bat"
    return [str(batch)] if batch.exists() else []


# '프로그램만 덮어쓰기' 때 이 PC에서 쌓인 것을 지키는 파일
KEEP_ON_UPDATE = {"data/glossary.json", "data/settings.json"}
ROSTER = "data/members.json"


def merge_roster(existing: Path, incoming: bytes) -> int:
    """이 PC의 명부(배운 얼굴·목소리 포함)는 그대로 두고, 새 명부에만 있는 사람을 더합니다. 더한 수."""
    try:
        mine = json.loads(existing.read_text(encoding="utf-8"))
        new = json.loads(incoming.decode("utf-8"))
    except (OSError, ValueError):
        return 0
    have = {m.get("name") for m in mine.get("members", [])}
    have_ids = {m.get("id") for m in mine.get("members", [])}
    added = [m for m in new.get("members", []) if m.get("name") not in have and m.get("id") not in have_ids]
    if added:
        mine.setdefault("members", []).extend(added)
        existing.write_text(json.dumps(mine, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(added)


MEI_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
MEI_COOKIE = "!8sIIII64s"

# 경량판(모델을 빼고 만든 설치 파일)은 설치 끝에 인터넷에서 이 모델들을 받습니다. (scripts/download_models.py)
AUX_MODELS = ("face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx",
              "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx")
MODEL_DOWNLOAD_SIZE = 1.7 * 1073741824      # 받는 양 (음성인식 원본 약 1.6GB + 얼굴·화자 약 70MB)
MODEL_DISK_EXTRA = 2.6 * 1073741824         # 받는 동안 잠깐 더 필요한 공간 (원본 + 가볍게 저장한 것)


def models_missing(target: Path) -> bool:
    """설치 폴더에 음성인식·얼굴·화자 모델이 다 있는지."""
    models = target / "data" / "models"
    has_whisper = any((models / "whisper").glob("*/model.bin"))
    return not has_whisper or not all((models / name).exists() for name in AUX_MODELS)


def same_model_file(target: Path, member: zipfile.ZipInfo, check_crc: bool = True) -> bool:
    """설치 폴더에 설치 파일 속 모델 파일과 같은 파일이 이미 있는지.

    크기가 같고, check_crc 면 내용의 CRC32(zip 에 기록된 값)까지 같아야 같다고 봅니다.
    (0.9GB 모델의 CRC 계산은 몇 초 걸립니다)
    """
    if not member.filename.startswith("data/models/") or member.is_dir():
        return False
    have = target / member.filename
    try:
        if not have.is_file() or have.stat().st_size != member.file_size:
            return False
        if not check_crc:
            return True
        import zlib

        crc = 0
        with open(have, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                crc = zlib.crc32(chunk, crc)
        return (crc & 0xFFFFFFFF) == member.CRC
    except OSError:
        return False


def payload_has_models(zf: zipfile.ZipFile) -> bool:
    return any(n.startswith("data/models/whisper/") and n.endswith("model.bin") for n in zf.namelist())


class _Slice:
    """파일의 앞부분 [0, end) 만 보이는 읽기 전용 파일. (zip 은 이 끝에서 목차를 찾습니다)"""

    def __init__(self, path: str, end: int) -> None:
        self._f = open(path, "rb")  # noqa: SIM115
        self._end = end
        self._pos = 0

    def seek(self, offset: int, whence: int = 0) -> int:
        base = {0: 0, 1: self._pos, 2: self._end}[whence]
        self._pos = max(0, min(self._end, base + offset))
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, n: int = -1) -> bytes:
        left = self._end - self._pos
        n = left if n is None or n < 0 else min(n, left)
        self._f.seek(self._pos)
        data = self._f.read(n)
        self._pos += len(data)
        return data

    def seekable(self) -> bool:
        return True

    def close(self) -> None:
        self._f.close()


def open_payload() -> zipfile.ZipFile:
    """이 exe 안의 설치 내용(zip).

    설치 파일 = [설치 프로그램 앞부분] + [zip] + [설치 프로그램 꾸러미(맨 끝)] 입니다.
    맨 끝 꾸러미 표시에서 꾸러미 길이를 읽어, 그 앞까지를 zip 으로 엽니다.
    (예전처럼 zip 이 맨 끝에 붙은 파일도 그대로 엽니다)
    """
    import struct

    path = sys.executable
    size = os.path.getsize(path)
    cookie = struct.calcsize(MEI_COOKIE)
    with open(path, "rb") as fh:
        fh.seek(max(0, size - cookie))
        tail = fh.read()
    if tail.startswith(MEI_MAGIC):
        pkg_len = struct.unpack(MEI_COOKIE, tail)[1]
        return zipfile.ZipFile(_Slice(path, size - pkg_len))
    return zipfile.ZipFile(path)


def ps_quote(value: str | Path) -> str:
    """PowerShell 작은따옴표 문자열 안에 안전하게 넣습니다."""
    return str(value).replace("'", "''")


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def running_in(target: Path) -> list[int]:
    """설치 폴더 안의 실행 파일로 켜져 있는 프로그램(이 설치 프로그램 말고)의 프로세스 번호."""
    if sys.platform != "win32" or not target.exists():
        return []
    folder = ps_quote(str(target.resolve()).rstrip("\\") + "\\*")
    ps = (f"Get-Process | Where-Object {{ $_.Path -like '{folder}' -and $_.Id -ne {os.getpid()} }} "
          "| ForEach-Object { $_.Id }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=20, creationflags=NO_WINDOW).stdout
    except Exception:  # noqa: BLE001
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def close_processes(pids: list[int]) -> bool:
    """프로세스를 닫고, 파일을 놓을 때까지 잠깐 기다립니다. 다 닫혔으면 True."""
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    for _ in range(20):
        time.sleep(0.5)
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout
        alive = {int(line.split('","')[1]) for line in out.splitlines() if line.count('","') >= 2}
        if not alive & set(pids):
            time.sleep(1)                               # 파일 잠금이 풀릴 때까지 조금 더
            return True
    return False


class RoundedButton(tk.Canvas):
    """본 프로그램의 10px 둥근 단추를 Tk Canvas로 그립니다."""

    def __init__(self, parent, text: str, command, *, primary: bool = False,
                 width: int = 96, height: int = 40) -> None:  # noqa: ANN001
        self._width = width
        self._height = height
        self._text = text
        self._command = command
        self._primary = primary
        self._enabled = True
        self._hovered = False
        self._pressed = False
        super().__init__(
            parent, width=width, height=height, bg=parent.cget("bg"),
            highlightthickness=0, bd=0, takefocus=1, cursor="hand2",
        )
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", lambda _e: self._draw())
        self.bind("<FocusOut>", lambda _e: self._draw())
        self.bind("<Return>", self._keyboard_click)
        self.bind("<space>", self._keyboard_click)
        self._draw()

    def _round_rect(self, x1: int, y1: int, x2: int, y2: int, radius: int,
                    *, fill: str, outline: str = "", line_width: int = 1) -> None:
        points = (
            x1 + radius, y1, x2 - radius, y1,
            x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2,
            x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius,
            x1, y1 + radius, x1, y1,
        )
        self.create_polygon(
            points, smooth=True, splinesteps=24, fill=fill,
            outline=outline, width=line_width,
        )

    def _draw(self) -> None:
        self.delete("all")
        disabled = not self._enabled
        if self._primary:
            fill = "#aebff2" if disabled else (PRIMARY_HOVER if self._hovered else PRIMARY)
            border = fill
            text_color = "#eef2ff" if disabled else "#ffffff"
            if not disabled:
                self._round_rect(2, 4, self._width - 2, self._height - 1, 10,
                                 fill="#c8d6fb")
        else:
            fill = "#f4f6fb" if disabled else ("#f2f6ff" if self._hovered else SURFACE)
            border = "#dfe5f2" if disabled else ("#b8c8ee" if self._hovered else "#aebde0")
            text_color = "#9ba5bf" if disabled else TEXT_2

        offset = 1 if self._pressed and self._enabled else 0
        focus = self.focus_get() == self
        if focus:
            self._round_rect(0, 0, self._width, self._height - 2, 12,
                             fill=parent_color(self), outline="#91abf7", line_width=2)
        self._round_rect(2, 1 + offset, self._width - 2, self._height - 4 + offset, 10,
                         fill=fill, outline=border)
        self.create_text(
            self._width // 2, (self._height - 3) // 2 + offset,
            text=self._text, fill=text_color, font=(FONT_FAMILY, 10, "bold"),
        )

    def _enter(self, _event) -> None:  # noqa: ANN001
        if self._enabled:
            self._hovered = True
            self._draw()

    def _leave(self, _event) -> None:  # noqa: ANN001
        self._hovered = False
        self._pressed = False
        self._draw()

    def _press(self, _event) -> None:  # noqa: ANN001
        if self._enabled:
            self.focus_set()
            self._pressed = True
            self._draw()

    def _release(self, event) -> None:  # noqa: ANN001
        should_run = (self._enabled and self._pressed and
                      0 <= event.x <= self._width and 0 <= event.y <= self._height)
        self._pressed = False
        self._draw()
        if should_run:
            self._command()

    def _keyboard_click(self, _event):  # noqa: ANN001
        if self._enabled:
            self._command()
        return "break"

    def set(self, *, text: str | None = None, state: str | None = None) -> None:
        if text is not None:
            self._text = text
        if state is not None:
            self._enabled = state != "disabled"
            self.config(cursor="hand2" if self._enabled else "arrow")
        self._draw()


def parent_color(widget: tk.Widget) -> str:
    """투명 캔버스를 대신할 부모 배경색을 돌려줍니다."""
    return str(widget.master.cget("bg"))


class Installer(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        # 경량판(모델 없이 만든 것)이면 받을 양과 받는 동안 더 필요한 공간을 정합니다
        self.download_size, self.disk_extra = MODEL_DOWNLOAD_SIZE, MODEL_DISK_EXTRA
        self.release_version = ""
        try:
            with open_payload() as zf:
                import re
                match = re.search(r'VERSION\s*=\s*"([^"]+)"', zf.read("app/version.py").decode("utf-8"))
                self.release_version = match.group(1) if match else ""
                self.lite = not payload_has_models(zf)
                if self.lite and "data/models_source.json" in zf.namelist():
                    size = int(json.loads(zf.read("data/models_source.json")).get("size") or 0)
                    if size:   # 가볍게 저장해 둔 묶음: 받은 묶음 + 푼 모델이 잠깐 같이 있습니다
                        self.download_size, self.disk_extra = size, size * 2.1
        except (zipfile.BadZipFile, OSError, ValueError):
            self.lite = False
        self.height = 516 if self.lite else 480             # 경량판 안내 줄만큼 높입니다
        self.title(f"{APP_NAME} {self.release_version} 설치")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.geometry(f"640x{self.height}")
        self._center()
        self._set_icon()
        self._load_font()

        self.target = tk.StringVar(value=str(default_target()))
        self.busy = False
        self.installed_at: Path | None = None
        self._build()

    # ── 화면 ────────────────────────────────────────────────
    def _center(self) -> None:
        self.update_idletasks()
        x = (self.winfo_screenwidth() - 640) // 2
        y = (self.winfo_screenheight() - self.height) // 3
        self.geometry(f"+{x}+{y}")

    def _set_icon(self) -> None:
        icon = Path(getattr(sys, "_MEIPASS", ".")) / "icon.ico"
        if icon.exists():
            try:
                self.iconbitmap(str(icon))
            except tk.TclError:
                pass

    def _load_font(self) -> None:
        """설치 파일에 포함된 Noto Sans KR을 현재 프로세스에만 불러옵니다."""
        font = Path(getattr(sys, "_MEIPASS", ".")) / FONT_FILE
        if sys.platform != "win32" or not font.exists():
            return
        try:
            import ctypes

            # FR_PRIVATE: Windows 글꼴 목록에 설치하지 않고 이 프로세스에서만 사용
            ctypes.windll.gdi32.AddFontResourceExW(str(font), 0x10, 0)
        except Exception:  # noqa: BLE001 - 글꼴 로드 실패 시 Windows 대체 글꼴 사용
            pass

    @staticmethod
    def _button(parent, text: str, command, *, primary: bool = False,
                width: int = 96, height: int = 40):  # noqa: ANN001
        return RoundedButton(parent, text, command, primary=primary,
                             width=width, height=height)

    def _brand_orb(self, parent) -> tk.Widget:  # noqa: ANN001
        """머리글 로고. 프로그램 아이콘과 같은 그림(icon-44.png)을 씁니다."""
        logo = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent / "assets")) / "icon-44.png"
        if logo.exists():
            try:
                self._logo = tk.PhotoImage(file=str(logo))      # 참조를 잡아 둬야 그림이 사라지지 않습니다
                return tk.Label(parent, image=self._logo, bg=SURFACE, bd=0)
            except tk.TclError:
                pass
        # 그림이 없을 때만 간단히 그립니다
        orb = tk.Canvas(parent, width=44, height=44, bg=SURFACE,
                        highlightthickness=0, bd=0)
        orb.create_oval(2, 2, 42, 42, fill="#eef3ff", outline="#d3defd", width=1)
        orb.create_oval(6, 6, 29, 29, fill="#8fd0ff", outline="")
        orb.create_oval(19, 7, 39, 28, fill="#f4b3e2", outline="")
        orb.create_oval(9, 19, 36, 40, fill="#9d9cf9", outline="")
        orb.create_oval(7, 7, 39, 39, outline="#ffffff", width=2)
        orb.create_rectangle(19, 12, 25, 25, outline="#2445c8", width=2)
        orb.create_arc(14, 17, 30, 31, start=180, extent=180,
                       style="arc", outline="#2445c8", width=2)
        orb.create_line(22, 30, 22, 35, fill="#2445c8", width=2)
        return orb

    def _build(self) -> None:
        # 상단 앱바
        header = tk.Frame(self, bg=SURFACE, height=68)
        header.pack(fill="x")
        header.pack_propagate(False)
        self._brand_orb(header).pack(side="left", padx=(24, 12), pady=11)
        brand = tk.Frame(header, bg=SURFACE)
        brand.pack(side="left", pady=12)
        tk.Label(brand, text=APP_NAME, bg=SURFACE, fg=TEXT,
                 font=(FONT_FAMILY, 12, "bold")).pack(anchor="w")
        tk.Label(brand, text=f"{self.release_version} · 대화를 기록으로 잇다", bg=SURFACE, fg=DIM,
                 font=(FONT_FAMILY, 8)).pack(anchor="w")
        tk.Label(header, text="  안전한 로컬 설치  ", bg=PRIMARY_SOFT, fg="#23408f",
                 font=(FONT_FAMILY, 8, "bold"), padx=5, pady=4).pack(side="right", padx=24)
        tk.Frame(self, bg=LINE, height=1).pack(fill="x")

        # 본문
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=(23, 20))
        tk.Label(body, text="말결을 설치합니다", bg=BG, fg=TEXT,
                 font=(FONT_FAMILY, 16, "bold")).pack(anchor="w")
        tk.Label(body, text="회의 소리를 실시간으로 받아 적고 녹취록을 만드는 프로그램입니다.",
                 bg=BG, fg=DIM, font=(FONT_FAMILY, 9)).pack(anchor="w", pady=(4, 6 if self.lite else 16))
        if self.lite:
            tk.Label(body, text=f"경량판 · 음성인식 모델이 없으면 설치 중에 인터넷에서 받습니다(약 {human(self.download_size)}). "
                                "이미 있으면 다시 받지 않습니다.",
                     bg=PRIMARY_SOFT, fg="#23408f", font=(FONT_FAMILY, 8, "bold"),
                     padx=8, pady=4).pack(anchor="w", pady=(0, 12))

        card_edge = tk.Frame(body, bg=LINE, bd=0)
        card_edge.pack(fill="x")
        card = tk.Frame(card_edge, bg=SURFACE, bd=0)
        card.pack(fill="both", padx=1, pady=1)
        tk.Label(card, text="설치 위치", bg=SURFACE, fg=TEXT,
                 font=(FONT_FAMILY, 10, "bold")).pack(anchor="w", padx=18, pady=(15, 3))
        tk.Label(card, text="프로그램과 음성인식 모델을 저장할 폴더입니다.", bg=SURFACE, fg=DIM,
                 font=(FONT_FAMILY, 8)).pack(anchor="w", padx=18)
        row = tk.Frame(card, bg=SURFACE)
        row.pack(fill="x", padx=18, pady=(10, 15))
        self.entry = tk.Entry(
            row, textvariable=self.target, bg=SURFACE_2, fg=TEXT,
            insertbackground=PRIMARY, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=LINE, highlightcolor=PRIMARY,
            font=(FONT_FAMILY, 10),
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=8, ipadx=10)
        self._button(row, "찾아보기", self.browse, width=96, height=40).pack(
            side="left", padx=(9, 0))

        self.status = tk.Label(body, text="준비됨 · 설치 단추를 누르면 시작합니다.", bg=BG, fg=DIM,
                               font=(FONT_FAMILY, 9), anchor="w")
        self.status.pack(fill="x", pady=(16, 7))

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("bar.Horizontal.TProgressbar", troughcolor="#e5ecfa", background=PRIMARY,
                        bordercolor="#e5ecfa", lightcolor=PRIMARY, darkcolor=PRIMARY, thickness=8)
        self.bar = ttk.Progressbar(body, style="bar.Horizontal.TProgressbar", maximum=1000)
        self.bar.pack(fill="x")

        # 하단 단추
        footer = tk.Frame(self, bg=SURFACE, height=80)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        tk.Frame(footer, bg=LINE, height=1).pack(fill="x")
        self.run_btn = self._button(footer, "설치하기", self.start,
                                    primary=True, width=126, height=48)
        self.run_btn.pack(side="right", padx=(8, 24), pady=15)
        self.close_btn = self._button(footer, "취소", self.close, width=92, height=48)
        self.close_btn.pack(side="right", pady=15)

    # ── 동작 ────────────────────────────────────────────────
    def browse(self) -> None:
        chosen = filedialog.askdirectory(title="설치할 폴더를 고르세요")
        if chosen:
            self.target.set(str(Path(chosen) / FOLDER_NAME))

    def say(self, text: str) -> None:
        self.status.config(text=text)
        self.update_idletasks()

    def start(self) -> None:
        if self.busy:
            return
        if self.installed_at:                      # 설치가 끝난 뒤에는 '실행' 단추가 됩니다
            self.launch()
            return

        target = Path(self.target.get()).expanduser()
        try:
            str(target).encode("ascii")
        except UnicodeEncodeError:
            if not messagebox.askyesno(
                "경로에 한글이 있습니다",
                "설치 경로에 한글이 들어 있으면 얼굴 인식이 동작하지 않을 수 있습니다.\n"
                "영문 경로(예: C:\\LiveCaption)를 권합니다.\n\n그래도 계속할까요?",
            ):
                return

        mode = "clean"
        if target.exists() and any(target.iterdir()):
            answer = messagebox.askyesnocancel(
                "이미 설치되어 있습니다",
                f"{target}\n\n"
                "[예] 프로그램만 새로 덮어쓰기 — 회의 기록과 설정은 그대로 둡니다\n"
                "[아니오] 전부 지우고 새로 설치\n"
                "[취소] 그만두기",
            )
            if answer is None:
                return
            mode = "update" if answer else "fresh"

            # 그 폴더의 프로그램이 켜져 있으면 쓰고 있는 파일을 지우거나 덮어쓸 수 없어 설치가 멈춥니다.
            # (새 버전 알림에서 받은 설치 파일을 프로그램을 켠 채 실행하는 경우가 많습니다)
            pids = running_in(target)
            if pids:
                if not messagebox.askyesno(
                    "프로그램이 켜져 있습니다",
                    f"{APP_NAME}가 켜져 있습니다. 닫고 계속할까요?\n\n"
                    "받아쓰는 중이었다면 [아니오]를 누르고, 받아쓰기를 멈춘 뒤 다시 설치해 주세요.",
                ):
                    return
                if not close_processes(pids):
                    messagebox.showerror("닫지 못했습니다",
                                         f"{APP_NAME}를 닫지 못했습니다. 직접 닫은 뒤 다시 설치해 주세요.")
                    return

        try:
            with open_payload() as zf:
                # 이미 있는 같은 모델 파일은 다시 풀지 않으므로 필요한 공간에서 뺍니다 (크기로 어림, 풀 때 CRC 로 확인)
                need = sum(m.file_size for m in zf.infolist() if not same_model_file(target, m, check_crc=False))
        except (zipfile.BadZipFile, OSError):
            need = 0
        # 경량판은 모델이 없을 때만 받습니다 ('전부 새로 설치'도 모델은 남기므로 같은 기준)
        if need and self.lite and models_missing(target):
            need += self.disk_extra
        probe = target
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            free = None
        if need and free is not None and free < need * 1.1:
            messagebox.showerror(
                "공간이 부족합니다",
                f"설치하려면 약 {human(need * 1.1)}가 필요한데, 남은 공간은 {human(free)}입니다.\n"
                "다른 드라이브를 고르거나 공간을 비운 뒤 다시 해 주세요.")
            return

        self.busy = True
        self.run_btn.set(state="disabled", text="설치 중...")
        threading.Thread(target=self._install, args=(target, mode), daemon=True).start()

    def _install(self, target: Path, mode: str) -> None:
        started = time.time()
        try:
            zf = open_payload()
        except (zipfile.BadZipFile, OSError):
            self.after(0, lambda: self._fail("이 파일에는 설치할 내용이 들어 있지 않습니다."))
            return

        try:
            if mode == "fresh":
                # 음성인식 모델(약 0.9GB)은 사용자 데이터가 아니므로 남겨 두어 다시 받지 않게 합니다
                self.after(0, lambda: self.say("기존 폴더를 지우는 중... (음성인식 모델은 남겨 둡니다)"))
                for item in target.iterdir():
                    if item.name == "data":
                        for sub in item.iterdir():
                            if sub.name != "models":
                                shutil.rmtree(sub, ignore_errors=True) if sub.is_dir() else sub.unlink(missing_ok=True)
                    elif item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
            elif mode == "update":
                for name in ("app", "web", "scripts", "python", "assets"):
                    shutil.rmtree(target / name, ignore_errors=True)
                for old in OLD_NAMES:                      # 옛 이름의 실행 파일
                    (target / f"{old}.exe").unlink(missing_ok=True)

            target.mkdir(parents=True, exist_ok=True)
            members = zf.infolist()
            total = sum(m.file_size for m in members) or 1
            done = 0
            safe = long_path(target)
            added = 0
            for m in members:
                name = m.filename.rstrip("/")
                # 이미 같은 모델 파일이 있으면 다시 풀지 않습니다 (정식판 덮어쓰기 때 0.9GB 를 아낍니다)
                # 크기뿐 아니라 내용(CRC)까지 같아야 건너뜁니다. 손상됐거나 바뀐 모델은 새로 풉니다.
                if same_model_file(target, m):
                    done += m.file_size
                    self.after(0, lambda p=done / total, d=done: self._progress(p, d, total))
                    continue
                # 프로그램만 덮어쓸 때는 이 PC에서 쌓인 용어사전·설정·명부를 지킵니다
                if mode == "update" and (target / name).is_file():
                    if name in KEEP_ON_UPDATE:
                        done += m.file_size
                        continue
                    if name == ROSTER:
                        added = merge_roster(target / name, zf.read(m))
                        done += m.file_size
                        continue
                zf.extract(m, safe)
                done += m.file_size
                percent = done / total
                self.after(0, lambda p=percent, d=done: self._progress(p, d, total))
            zf.close()
            target = target.resolve(strict=True)

            (target / VERSION_FILE).write_text(
                f"{APP_NAME} {self.release_version}\n설치 시각: {time.strftime('%Y-%m-%d %H:%M')}\n", encoding="utf-8")
            self._make_shortcut(target)
            self._register_app(target, total)
            models_ok = self._download_models(target) if models_missing(target) else True
            self.after(0, lambda: self._done(target, time.time() - started, added, models_ok))
        except OSError as exc:
            self.after(0, lambda: self._fail(f"설치에 실패했습니다.\n{exc}"))

    def _download_models(self, target: Path) -> bool:
        """경량판: 설치된 파이썬으로 모델을 인터넷에서 받습니다. 받는 동안 진행 상황을 창에 보여 줍니다."""
        python = target / "python" / "python.exe"
        if not python.exists():
            return False
        self.after(0, self._stop_bar)
        self.after(0, lambda: self.bar.configure(value=0))
        self.after(0, lambda: self.say("음성인식 모델을 인터넷에서 받는 중... (몇 분~수십 분 걸립니다)"))
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        try:
            proc = subprocess.Popen(
                [str(python), "-u", "-m", "scripts.download_models"], cwd=str(target), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            self.after(0, self._stop_bar)
            return False
        # 받은 양 / 전체 양(MB)과 진행률(%)을 1초마다 보여 주고, 진행 막대도 그만큼 채웁니다.
        #  - 모델 묶음: 전체 크기를 알고 있으므로(설치 파일의 models_source.json) 받는 중인 파일 크기로 정확히 셉니다.
        #  - 원래 방법(원본을 받아 줄이기)으로 넘어가면 전체 크기를 미리 알 수 없어 약 MODEL_DOWNLOAD_SIZE 로 어림합니다.
        models = target / "data" / "models"
        part = models / "_bundle.zip.part"
        phase = ["준비 중"]
        started = time.time()

        def received() -> int:
            total = 0
            for f in models.rglob("*"):
                try:
                    total += f.stat().st_size if f.is_file() else 0
                except OSError:
                    pass
            return total

        def mb(n: float) -> str:
            return f"{n / 1048576:,.0f}MB"

        def tick() -> None:
            base, base_phase = received(), phase[0]     # 이미 있던 모델은 빼고 이번에 받은 양만 셉니다
            while proc.poll() is None:
                spent = int(time.time() - started)
                clock = f"{spent // 60}분 {spent % 60}초"
                now = phase[0]
                if now == "준비 중":
                    self.after(0, lambda c=clock: self.say(f"모델 받을 준비 중... · {c}"))
                    time.sleep(1)
                    continue
                if now == "모델 묶음":
                    try:
                        got = part.stat().st_size
                    except OSError:
                        got = 0
                    total, about = self.download_size, ""
                elif "정리" in now or "푸는" in now:
                    # 원본과 줄인 모델(또는 묶음과 푼 파일)이 잠깐 같이 있어 크기로는 셀 수 없습니다
                    self.after(0, lambda c=clock, p=now: (self.bar.configure(value=1000),
                                                          self.say(f"모델을 다 받았습니다 · {p}... · {c}")))
                    time.sleep(1)
                    continue
                else:
                    if now != base_phase and base_phase == "모델 묶음":   # 묶음을 못 받아 원래 방법으로 넘어감
                        base, base_phase = received(), now
                    got, total, about = max(0, received() - base), MODEL_DOWNLOAD_SIZE, "약 "
                pct = min(100 if not about else 99, int(got * 100 / total)) if total else 0
                text = f"모델 받는 중 · {mb(got)} / {about}{mb(total)} ({pct}%) · {clock}"
                self.after(0, lambda s=text, v=pct: (self.bar.configure(value=v * 10), self.say(s)))
                time.sleep(1)

        threading.Thread(target=tick, daemon=True).start()
        # 진행 막대(tqdm)는 줄바꿈 없이 \r 로 덮어쓰므로 \r, \n 모두에서 끊고, 단계가 바뀌는 줄만 골라 둡니다.
        buf = b""
        while True:
            chunk = proc.stdout.read1(4096)       # 쌓일 때까지 기다리지 않고 온 만큼 바로 읽습니다
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.replace(b"\r", b"\n").split(b"\n")
            for raw in lines:
                line = raw.decode("utf-8", "replace").strip()
                if "모델 묶음" in line and "받는" in line:
                    phase[0] = "모델 묶음"
                elif "푸는 중" in line:
                    phase[0] = "모델 묶음 푸는 중"
                elif "얼굴" in line:
                    phase[0] = "얼굴·화자 모델"
                elif "음성인식" in line:
                    phase[0] = "음성인식 모델"
                elif "가볍게" in line:
                    phase[0] = "음성인식 모델 정리 중"
        proc.wait()
        self.after(0, self._stop_bar)
        return proc.returncode == 0 and not models_missing(target)

    def _stop_bar(self) -> None:
        self.bar.stop()
        self.bar.config(mode="determinate")

    def _progress(self, ratio: float, done: int, total: int) -> None:
        self.bar["value"] = ratio * 1000
        self.say(f"푸는 중... {int(ratio * 100)}%   ({human(done)} / {human(total)})")

    def _done(self, target: Path, seconds: float, added: int = 0, models_ok: bool = True) -> None:
        self.busy = False
        self.installed_at = target
        self.bar["value"] = 1000
        extra = f"  명부에 {added}명을 더했습니다." if added else ""
        self.say(f"설치가 끝났습니다. ({seconds:.0f}초)  바탕화면과 시작 메뉴에 바로가기를 만들었습니다.{extra}")
        self.run_btn.set(state="normal", text="실행")
        # 설치가 오래 걸려 다른 창을 보고 있을 수 있으므로, 창을 앞으로 가져오고 알림음을 냅니다
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(500, lambda: self.attributes("-topmost", False))
        self.focus_force()
        self.bell()
        if not models_ok:
            self.say("프로그램은 설치했지만 음성인식 모델을 받지 못했습니다.")
            messagebox.showwarning(
                "모델을 받지 못했습니다",
                "인터넷에 연결되어 있지 않거나 보안 장비가 내려받기를 막았습니다.\n"
                "이대로는 받아쓰기를 할 수 없습니다.\n\n"
                "인터넷에 연결한 뒤 이 설치 파일을 다시 실행하고\n"
                "[예] 프로그램만 새로 덮어쓰기를 고르면 모델만 이어서 받습니다.", parent=self)
            return
        if messagebox.askyesno("설치 완료", "설치가 완료되었습니다.\n바로 프로그램을 실행할까요?", parent=self):
            self.launch()

    def _fail(self, message: str) -> None:
        self.busy = False
        self.run_btn.set(state="normal", text="설치하기")
        self.say("설치하지 못했습니다.")
        self.lift()                                      # 오류 창이 다른 창 뒤에 가려지지 않게
        self.attributes("-topmost", True)
        self.after(500, lambda: self.attributes("-topmost", False))
        messagebox.showerror("설치 실패", message + "\n\n프로그램이 켜져 있지 않은지, 공간이 충분한지, "
                             "백신이 막고 있지 않은지 확인해 주세요.", parent=self)

    def _make_shortcut(self, target: Path) -> None:
        """바탕화면과 시작 메뉴에 바로가기를 만듭니다."""
        places = [shell_folder(0x10), shell_folder(0x02)]
        for place in places:
            if place.exists():
                for old in OLD_NAMES:
                    try:
                        (place / f"{old}.lnk").unlink(missing_ok=True)
                    except OSError:
                        pass
                self._make_link(target, place / f"{APP_NAME}.lnk")

    def _register_app(self, target: Path, size: int) -> None:
        """Windows 설정 → 앱(제어판 → 프로그램 제거) 목록에 올립니다. 거기서 '제거'를 누르면 제거 프로그램이 뜹니다.

        관리자 권한 없이 설치하므로 현재 사용자(HKCU) 아래에 등록합니다.
        """
        if sys.platform != "win32":
            return
        import re
        import winreg

        runner = target / "python" / "pythonw.exe"
        if not runner.exists():
            runner = target / "python" / "python.exe"
        uninstall = f'"{runner}" "{target / "scripts" / "uninstall_main.py"}"'
        version = ""
        try:
            m = re.search(r'VERSION\s*=\s*"([^"]+)"', (target / "app" / "version.py").read_text(encoding="utf-8"))
            version = m.group(1) if m else ""
        except OSError:
            pass
        values = {
            "DisplayName": APP_NAME,
            "DisplayIcon": str(target / "assets" / "icon.ico"),
            "DisplayVersion": version,
            "InstallLocation": str(target),
            "InstallDate": time.strftime("%Y%m%d"),
            "UninstallString": uninstall,
        }
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
                for name, value in values.items():
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
                winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, int(size // 1024))
                winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        except OSError:
            pass

    def _make_link(self, target: Path, link: Path) -> None:
        target = target.resolve(strict=True)
        command = app_command(target)
        if not command:
            return
        icon = (target / "assets" / "malgyeol.ico").resolve(strict=True)
        launcher = command[0]
        arguments = subprocess.list2cmdline(command[1:])
        ps = (
            "$ErrorActionPreference='Stop';"
            f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{ps_quote(link)}');"
            f"$s.TargetPath='{ps_quote(launcher)}';$s.Arguments='{ps_quote(arguments)}';"
            f"$s.WorkingDirectory='{ps_quote(target)}';"
            f"$s.IconLocation='{ps_quote(icon)},0';$s.Save();"
            f"if(-not(Test-Path -LiteralPath '{ps_quote(link)}')){{throw 'Shortcut was not created'}}"
        )
        try:
            encoded = base64.b64encode(ps.encode('utf-16-le')).decode('ascii')
            subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
                           check=True, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (subprocess.SubprocessError, OSError) as exc:
            raise OSError("바로가기를 만들지 못했습니다.") from exc

    def launch(self) -> None:
        target = self.installed_at
        if not target:
            return
        command = app_command(target)
        if not command:
            messagebox.showerror("실행 실패", "설치된 실행 파일을 찾지 못했습니다.")
            return
        try:
            subprocess.Popen(
                command,
                cwd=str(target),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            messagebox.showerror("실행 실패", str(exc))
        self.close()

    def close(self) -> None:
        if self.busy and not messagebox.askyesno("설치 중", "설치를 멈추고 닫을까요?"):
            return
        self.destroy()


def main() -> int:
    Installer().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
