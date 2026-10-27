"""프로그램 제거 (Windows 설정 → 앱 → 회의 받아쓰기 → 제거 에서 실행됩니다).

설치 마법사가 등록해 둔 명령:
    "<설치 폴더>\\python\\pythonw.exe" "<설치 폴더>\\scripts\\uninstall_main.py"

설치본의 파이썬에는 tkinter 가 없으므로 질문은 Windows 기본 알림창(MessageBox)으로 하고,
지우는 동안의 진행 창(막대·%·지운 파일 수)은 PowerShell 로 띄웁니다.

하는 일
  1. 정말 지울지, 회의 기록(녹음·녹취록)·설정·등록된 얼굴/목소리도 지울지 묻습니다. (기본: 남겨 둠)
  2. 프로그램이 켜져 있으면 닫을지 묻습니다.
  3. 바로가기(바탕화면·시작 메뉴)와 '설치된 앱' 등록을 지웁니다.
  4. 프로그램 파일을 지웁니다. 지금 쓰고 있는 파이썬 폴더는 이 창이 닫힌 뒤 따로 지웁니다.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "말결"
OLD_NAMES = ("회의 실시간 자막", "회의 받아쓰기")        # 예전 이름
REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\LiveCaption"
# 회의 기록을 남길 때 지우지 않는 것
KEEP = ("data/sessions", "data/settings.json", "data/members.json", "data/faces", "data/glossary.json")

MB_OK, MB_YESNO, MB_ICONQUESTION, MB_ICONINFO, MB_ICONWARN = 0x0, 0x4, 0x20, 0x40, 0x30
MB_DEFBUTTON2 = 0x100
MB_TOPMOST = 0x40000                    # 진행 창(맨 앞) 뒤에 가려지지 않게
IDYES = 6
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def box(text: str, flags: int = MB_OK | MB_ICONINFO) -> int:
    return ctypes.windll.user32.MessageBoxW(None, text, f"{APP_NAME} 제거", flags | 0x10000)  # 맨 앞에


def root_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def running_app(root: Path) -> list[int]:
    """설치 폴더의 파이썬으로 켜져 있는 프로그램(나 말고)."""
    ps = (f"Get-Process | Where-Object {{ $_.Path -like '{str(root).replace(chr(39), chr(39) * 2)}\\*' "
          f"-and $_.Id -ne {os.getpid()} }} | ForEach-Object {{ $_.Id }}")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=20, creationflags=NO_WINDOW).stdout
    except Exception:  # noqa: BLE001
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def remove_shortcuts() -> None:
    places = [Path(os.path.expanduser("~")) / "Desktop"]
    if os.environ.get("APPDATA"):
        places.append(Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    for place in places:
        for name in (APP_NAME, *OLD_NAMES):
            try:
                (place / f"{name}.lnk").unlink(missing_ok=True)
            except OSError:
                pass


def remove_registration() -> None:
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_KEY)
    except OSError:
        pass


# 진행 창: 설치본의 파이썬에는 tkinter 가 없어, 모든 Windows 에 있는 PowerShell 로 작은 창을 띄웁니다.
# 제거 프로그램이 진행 상황을 파일(퍼센트|문구)에 적으면 창이 0.15초마다 읽어 보여 줍니다. 'done' 이면 닫힙니다.
PROGRESS_PS = r"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$file = $env:LC_PROGRESS
$font = New-Object System.Drawing.Font('맑은 고딕', 10)
$form = New-Object System.Windows.Forms.Form
$form.Text = '말결 제거'
$form.ClientSize = New-Object System.Drawing.Size(440, 120)
$form.FormBorderStyle = 'FixedDialog'; $form.ControlBox = $false; $form.TopMost = $true
$form.StartPosition = 'CenterScreen'; $form.BackColor = [System.Drawing.Color]::White
$label = New-Object System.Windows.Forms.Label
$label.SetBounds(22, 20, 396, 24); $label.Font = $font; $label.Text = '제거를 준비하는 중...'
$bar = New-Object System.Windows.Forms.ProgressBar
$bar.SetBounds(22, 52, 396, 22); $bar.Minimum = 0; $bar.Maximum = 100
$pct = New-Object System.Windows.Forms.Label
$pct.SetBounds(22, 82, 396, 22); $pct.Font = $font; $pct.ForeColor = [System.Drawing.Color]::DimGray; $pct.Text = '0%'
$form.Controls.AddRange(@($label, $bar, $pct))
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 150
$timer.Add_Tick({
    try { $s = [System.IO.File]::ReadAllText($file, [System.Text.Encoding]::UTF8) } catch { return }
    $p, $m = $s -split '\|', 2
    if ($m -eq 'done') { $timer.Stop(); $form.Close(); return }
    $v = 0; if ([int]::TryParse($p, [ref]$v)) { $bar.Value = [Math]::Max(0, [Math]::Min(100, $v)); $pct.Text = "$v%" }
    if ($m) { $label.Text = $m }
})
$timer.Start()
[void]$form.ShowDialog()
"""


class Progress:
    """제거 진행 창 (PowerShell). 띄우지 못해도 제거는 그대로 진행합니다."""

    def __init__(self) -> None:
        import tempfile

        self.dir = Path(tempfile.mkdtemp(prefix="livecaption_uninstall_"))
        self.file = self.dir / "progress.txt"
        self.proc = None
        self.set(0, "제거를 준비하는 중...")
        script = self.dir / "progress.ps1"
        script.write_text(PROGRESS_PS, encoding="utf-8-sig")        # BOM: Windows PowerShell 이 한글을 읽도록
        try:
            self.proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script)],
                env=dict(os.environ, LC_PROGRESS=str(self.file)), creationflags=NO_WINDOW)
        except OSError:
            self.proc = None

    def set(self, percent: int, message: str) -> None:
        tmp = self.file.with_suffix(".tmp")
        try:
            tmp.write_text(f"{int(percent)}|{message}", encoding="utf-8")
            tmp.replace(self.file)                                   # 반쯤 쓴 내용을 읽지 않게 통째로 바꿉니다
        except OSError:
            pass

    def close(self) -> None:
        self.set(100, "done")
        if self.proc:
            try:
                self.proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


def targets(root: Path, keep_data: bool) -> list[Path]:
    """지울 것 (지금 쓰고 있는 python 폴더와, 남겨 둘 데이터는 뺍니다)."""
    out = []
    for item in root.iterdir():
        if item.name == "python":
            continue
        if item.name == "data" and keep_data:
            out += [sub for sub in item.iterdir() if f"data/{sub.name}" not in KEEP]
            continue
        out.append(item)
    return out


def remove_files(root: Path, keep_data: bool, progress: Progress | None = None, start: int = 10, end: int = 95) -> None:
    """지울 파일을 먼저 세고, 하나씩 지우며 진행률(start~end%)을 알립니다."""
    items = targets(root, keep_data)
    files, dirs = [], []
    for item in items:
        if item.is_dir() and not item.is_symlink():
            dirs.append(item)
            for base, _sub, names in os.walk(item):
                files += [Path(base) / n for n in names]
        else:
            files.append(item)
    total = len(files) or 1
    last = -1
    for done, f in enumerate(files, 1):
        try:
            f.unlink()
        except OSError:
            try:
                os.chmod(f, 0o666)                                   # 읽기 전용 파일
                f.unlink()
            except OSError:
                pass
        pct = start + (end - start) * done // total
        if progress and (pct != last or done == total):
            progress.set(pct, f"프로그램 파일 지우는 중... {done:,} / {total:,}개")
            last = pct
    for d in dirs:                                                   # 비어 있는 폴더 정리
        shutil.rmtree(d, ignore_errors=True)


def remove_later(root: Path, keep_data: bool) -> None:
    """이 창이 닫히고 몇 초 뒤 python 폴더(와 빈 설치 폴더)를 지웁니다."""
    target = root / "python" if keep_data else root
    # 목록이 아닌 문자열로 넘깁니다. (목록이면 경로의 따옴표가 \" 로 바뀌어 cmd 가 못 알아듣습니다)
    cmd = f'cmd /c ping -n 4 127.0.0.1 >nul & rmdir /s /q "{target}"'
    detached, new_group, breakaway = 0x00000008, 0x00000200, 0x01000000
    for flags in (detached | new_group | breakaway, detached | new_group):
        try:
            subprocess.Popen(cmd, cwd=os.environ.get("TEMP", "C:\\"), creationflags=flags, close_fds=True)
            return
        except OSError:
            continue


def main() -> int:
    root = root_dir()
    if not (root / "app").exists() and not (root / "python").exists():
        box("설치 폴더를 찾지 못했습니다.", MB_OK | MB_ICONWARN)
        return 1
    if box(f"{APP_NAME}를 이 PC에서 제거할까요?\n\n설치 위치: {root}", MB_YESNO | MB_ICONQUESTION) != IDYES:
        return 0
    wipe = box("회의 기록(녹음·녹취록), 설정(API 키), 등록된 얼굴·목소리도 함께 지울까요?\n\n"
               f"[아니요]를 누르면 아래 폴더에 남겨 둡니다. 나중에 다시 설치하면 그대로 이어서 쓸 수 있습니다.\n{root / 'data'}",
               MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2) == IDYES
    # 켜져 있는 프로그램을 찾는 데 몇 초 걸리므로, 진행 창부터 띄워 멈춘 것처럼 보이지 않게 합니다
    progress = Progress()
    progress.set(1, "켜져 있는 프로그램이 있는지 확인하는 중...")
    pids = running_app(root)
    if pids and box("프로그램이 켜져 있습니다. 닫고 계속할까요?", MB_YESNO | MB_ICONWARN | MB_TOPMOST) != IDYES:
        progress.close()
        return 0
    try:
        if pids:
            progress.set(3, "켜져 있는 프로그램을 닫는 중...")
            for pid in pids:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=NO_WINDOW)
        progress.set(6, "바로가기와 '설치된 앱' 등록을 지우는 중...")
        remove_shortcuts()
        remove_registration()
        progress.set(10, "지울 파일을 세는 중...")
        remove_files(root, keep_data=not wipe, progress=progress, start=10, end=97)
        progress.set(99, "마무리하는 중...")
        remove_later(root, keep_data=not wipe)
    finally:
        progress.close()
    box(f"{APP_NAME}를 제거했습니다."
        + ("" if wipe else f"\n\n회의 기록과 설정은 여기에 남겨 두었습니다:\n{root / 'data'}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
