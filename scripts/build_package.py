"""각 PC에 나눠 줄 설치 꾸러미 만들기.

파이썬이 없는 PC에서도 압축만 풀면 바로 실행되도록, 파이썬 실행기와 꾸러미,
모델 파일까지 한 폴더에 담습니다. 인터넷이 없는 내부망 PC를 염두에 둔 구성입니다.

    python -m scripts.build_package                    # 전부 담기 (약 1.4GB)
    python -m scripts.build_package --no-models        # 모델 빼고 (약 500MB)
    python -m scripts.build_package --with-members     # 명부·얼굴·성문까지 담기 (내부용)
    python -m scripts.build_package --with-roster      # 명부(이름·직함)만 담기 (배포용, 생체정보 없음)
    python -m scripts.build_package --zip              # 끝나고 zip으로 압축

만들어지는 구조

    배포/말결/
        python/            파이썬 실행기 (설치 불필요)
        app/  web/  scripts/
        data/models/       음성인식·얼굴·화자 모델
        data/glossary.json 용어사전
        말결 시작.bat   ← 이것만 두 번 누르면 됩니다
        읽어보세요.txt
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import paths  # noqa: E402

ROOT = paths.ROOT
BUILD = ROOT / "배포"
PY_VERSION = "3.12.10"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

READ_ME = """말결 — 설치 없이 쓰는 꾸러미

1. 이 폴더를 통째로 C:\\말결 같은 곳에 풀어 주세요.
   (바탕화면이나 네트워크 드라이브에서 바로 실행하면 느릴 수 있습니다)

2. '말결 시작.bat' 을 두 번 누르면 프로그램 창이 열립니다.
   처음 한 번은 바탕화면에 '말결' 아이콘도 만들어 줍니다. 다음부터는 그 아이콘을 누르세요.
   (Windows가 '알 수 없는 게시자' 경고를 띄우면 '실행'을 누르면 됩니다)

3. 처음 한 번만 해 주세요
   - 설정 탭 → 성능 모드를 고릅니다. 사무용 PC라면 '절전'을 권합니다.
   - 받아쓰기 탭 → '화면 감시'를 켜고 발언자 얼굴을 캡처해 이름을 붙여 두면
     다음부터 화자 이름이 자동으로 붙습니다.

4. 회의 녹취하기
   - 중계를 브라우저로 틀어 놓고 → 소스 'PC 소리(스피커)' → 받아쓰기 시작
   - 지난 회의는 소스 '저장된 파일' 에 영상 파일이나 주소를 넣으면 됩니다.

주의
 - 음성인식은 전부 이 PC 안에서 돌아갑니다. 회의 음성이 밖으로 나가지 않습니다.
 - 요약과 일부 화자 지명 판별은 로그인된 Claude Code 또는 Codex를 통해 녹취 텍스트를 외부로 보냅니다.
 - 녹음 파일은 data\\sessions 폴더에 쌓입니다. 1시간에 약 115MB입니다.
 - 자세한 설명은 README.md 를 보세요.
"""


def download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 1000:
        print(f"  이미 있음: {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  내려받는 중: {dest.name}")
    headers = {"User-Agent": "LiveTranscription-build"}
    with (httpx.stream("GET", url, headers=headers, timeout=180, follow_redirects=True) as resp,
          open(dest, "wb") as fh):
        resp.raise_for_status()
        for chunk in resp.iter_bytes(1 << 16):
            fh.write(chunk)
    return dest


def make_embedded_python(target: Path, cache: Path) -> Path:
    """설치가 필요 없는 파이썬 실행기를 target/python 에 만듭니다."""
    py_dir = target / "python"
    if py_dir.exists():
        shutil.rmtree(py_dir)
    py_dir.mkdir(parents=True)

    zip_path = download(PY_URL, cache / f"python-{PY_VERSION}-embed-amd64.zip")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(py_dir)

    # 임베디드 파이썬은 기본적으로 site-packages를 보지 않습니다. 열어 줍니다.
    for pth in py_dir.glob("python*._pth"):
        lines = pth.read_text().splitlines()
        out = []
        for line in lines:
            out.append("import site" if line.strip() == "#import site" else line)
        if "import site" not in out:
            out.append("import site")
        out.append("..")            # 프로그램 폴더(app 패키지)를 경로에 추가
        out.append("Lib\\site-packages")
        pth.write_text("\n".join(out) + "\n")

    get_pip = download(GET_PIP_URL, cache / "get-pip.py")
    print("  pip 설치 중...")
    subprocess.run([str(py_dir / "python.exe"), str(get_pip), "--no-warn-script-location", "-q"],
                   check=True)
    return py_dir


def install_packages(py_dir: Path) -> None:
    print("  꾸러미 설치 중... (몇 분 걸립니다)")
    # 설치 불필요판 파이썬은 pip 의 임시 조립 공간에서 조립 도구를 못 찾으므로,
    # 원본만 있는 꾸러미(프로그램 창용 proxy_tools)는 조립 도구를 먼저 넣고 바로 조립합니다.
    py = str(py_dir / "python.exe")
    subprocess.run([py, "-m", "pip", "install", "-q", "--no-warn-script-location", "setuptools", "wheel"], check=True)
    subprocess.run([py, "-m", "pip", "install", "-q", "--no-warn-script-location", "--no-build-isolation",
                    "proxy_tools"], check=True)
    subprocess.run(
        [str(py_dir / "python.exe"), "-m", "pip", "install", "--no-warn-script-location",
         "-q", "-r", str(ROOT / "requirements.txt")],
        check=True,
    )
    slim_packages(py_dir)


# 실행에 쓰지 않는 꾸러미 (조립 도구·exe 만들기 도구·고속 내려받기 도구)
UNUSED_PACKAGES = ("pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile", "pywin32-ctypes",
                   "setuptools", "wheel", "hf-xet")


def slim_packages(py_dir: Path) -> None:
    """배포 크기를 줄이려고 실행에 필요 없는 꾸러미와 파일을 지웁니다."""
    py = str(py_dir / "python.exe")
    subprocess.run([py, "-m", "pip", "uninstall", "-y", "-q", *UNUSED_PACKAGES], check=False)
    site = py_dir / "Lib" / "site-packages"
    freed = 0
    # OpenCV 의 영상 파일 읽기용 ffmpeg (영상은 av 로 읽으므로 안 씀, 약 30MB)
    junk = list((site / "cv2").glob("opencv_videoio_ffmpeg*.dll"))
    # 꾸러미마다 들어 있는 시험 코드
    junk += [d for d in site.rglob("tests") if d.is_dir()]
    for p in junk:
        if not p.exists():
            continue
        if p.is_dir():
            freed += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            shutil.rmtree(p, ignore_errors=True)
        else:
            freed += p.stat().st_size
            p.unlink()
    print(f"  쓰지 않는 꾸러미 {len(UNUSED_PACKAGES)}개·파일 {freed >> 20}MB 정리")


def copy_program(target: Path, with_models: bool, with_members: bool, with_roster: bool = False) -> None:
    for name in ("app", "web", "scripts", "assets"):
        dest = target / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(ROOT / name, dest,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("requirements.txt", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, target / name)

    data = target / "data"
    (data / "models").mkdir(parents=True, exist_ok=True)
    (data / "sessions").mkdir(exist_ok=True)

    if with_models:
        for onnx in paths.MODELS_DIR.glob("*.onnx"):      # 얼굴·화자 모델
            shutil.copy2(onnx, data / "models" / onnx.name)
        copy_whisper_model(data / "models" / "whisper")
    if paths.GLOSSARY_FILE.exists():
        shutil.copy2(paths.GLOSSARY_FILE, data / "glossary.json")
        print("  용어사전 포함")
    if with_members and paths.MEMBERS_FILE.exists():
        shutil.copy2(paths.MEMBERS_FILE, data / "members.json")
        # 성문 정리 때 남긴 백업(_voice_backup_*)은 담지 않습니다. (누구 것인지 모르는 목소리라 퍼뜨리면 안 됨)
        # 예전에 담은 얼굴·성문이 섞이지 않게 새로 담습니다. (명부에서 빠진 사람의 성문이 남아 나가지 않도록)
        shutil.rmtree(data / "faces", ignore_errors=True)
        shutil.copytree(paths.FACES_DIR, data / "faces",
                        ignore=shutil.ignore_patterns("_voice_backup*"))
        print("  명부·얼굴·목소리 특징 포함 (민감한 생체정보가 들어갑니다. 배포 범위를 확인하세요)")
    elif with_roster:
        write_roster(data / "members.json")

    shutil.copy2(ROOT / "말결 시작.bat", target / "말결 시작.bat")
    (target / "읽어보세요.txt").write_text(READ_ME, encoding="utf-8")
    # 모델까지 담았으면 설치가 끝난 상태입니다. (시작 파일이 곧바로 프로그램을 엽니다)
    # 모델을 뺐으면 처음 실행 때 모델만 받습니다.
    if with_models:
        (target / "python" / ".ready").write_text("bundle", encoding="ascii")


def write_roster(dest: Path) -> None:
    """명부 전체의 이름·직함을 담습니다. (명부는 개인정보가 아닙니다)
    얼굴 사진·얼굴 특징·성문은 이 PC에서 배운 생체정보라 빼고, 개수도 0으로 둡니다."""
    import json

    src = json.loads(paths.MEMBERS_FILE.read_text(encoding="utf-8")) if paths.MEMBERS_FILE.exists() else {}
    roster = [{"id": m["id"], "name": m["name"], "title": m.get("title", ""), "note": "",
               "photos": [], "face_count": 0, "voice_count": 0}
              for m in src.get("members", [])]
    dest.write_text(json.dumps({"members": roster}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  명부 {len(roster)}명 포함 (이름·직함만, 얼굴·성문 없음)")


WHISPER_MODEL_ALIASES = {
    "large-v3-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
}


def copy_whisper_model(dest_root: Path) -> None:
    """설정에 잡힌 음성인식 모델 하나를 가볍게(int8) 저장해 담습니다. (1.5GB → 약 0.85GB, 인식 결과 같음)

    HuggingFace 캐시는 blobs + snapshots(링크) 구조라, 실제 파일을 읽어 평평한 폴더로 만듭니다.
    """
    from app import config
    from scripts.slim_whisper import slim

    name = config.load()["asr"]["model"]
    repo = WHISPER_MODEL_ALIASES.get(name, name)
    cache = paths.MODELS_DIR / "whisper"
    source = None
    local = cache / name                       # 이미 가볍게 만들어 둔 모델이 있으면 그것을
    if (local / "model.bin").exists():
        source = local
    snapshots = cache / ("models--" + repo.replace("/", "--")) / "snapshots"
    if source is None and snapshots.exists():
        for candidate in snapshots.iterdir():
            if (candidate / "model.bin").exists():
                source = candidate
                break
    if source is None:
        print(f"  ! 음성인식 모델({name})을 찾지 못했습니다. 먼저 download_models 를 실행하세요.")
        return

    dest = dest_root / name
    slim(source, dest)                         # 이미 가벼운 모델이면 그대로 복사됩니다
    total = sum(f.stat().st_size for f in dest.iterdir() if f.is_file())
    print(f"  음성인식 모델(가볍게 저장): {name} ({total >> 20}MB)")


def folder_size_mb(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) // (1024 * 1024)


# zip 에 담는 것 (이 목록 밖은 담지 않습니다)
# 배포 폴더에서 프로그램을 실행해 보면 녹음·설정(API 키)·실행 기록이 쌓이는데, 그런 것이 섞여 나가지 않게 합니다.
ZIP_TOP = {"python", "app", "web", "scripts", "assets", "requirements.txt", "README.md", "LICENSE",
           "말결 시작.bat", "읽어보세요.txt"}
ZIP_DATA = {"models", "glossary.json", "models_source.json"}
ZIP_MEMBERS = {"members.json", "faces"}


def _zip_allowed(rel: Path, with_members: bool, with_roster: bool = False) -> bool:
    top = rel.parts[0]
    # pip 이 만든 명령 실행 파일(python/Scripts/pip.exe, uvicorn.exe 등). 프로그램은 python.exe -m … 으로만 실행해
    # 쓰지 않고, 안에 빌드한 PC의 폴더 경로(사용자 이름 등)가 박혀 있어 담지 않습니다.
    if rel.parts[:2] == ("python", "Scripts"):
        return False
    if top != "data":
        return top in ZIP_TOP
    if len(rel.parts) < 2:
        return False
    if rel.parts[1] == "members.json" and with_roster:
        return True
    return rel.parts[1] in ZIP_DATA or (with_members and rel.parts[1] in ZIP_MEMBERS)


def make_zip(target: Path, with_members: bool = False, with_roster: bool = False) -> Path:
    zip_path = target.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    print(f"  압축 중: {zip_path.name} (시간이 꽤 걸립니다)")
    skipped = set()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr(target.name + "/data/sessions/", "")          # 빈 녹음 폴더
        for f in target.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(target)
            if not _zip_allowed(rel, with_members, with_roster):
                skipped.add("/".join(rel.parts[:2]))
                continue
            z.write(f, target.name + "/" + rel.as_posix())
    if skipped:
        print("  담지 않음(실행하며 생긴 파일 등): " + ", ".join(sorted(skipped)))
    return zip_path


def main() -> int:
    ap = argparse.ArgumentParser(description="배포 꾸러미 만들기")
    ap.add_argument("--no-models", action="store_true", help="모델 파일 빼기(각 PC에서 따로 받아야 함)")
    ap.add_argument("--with-members", action="store_true", help="명부와 얼굴·목소리 특징까지 담기 (내부용)")
    ap.add_argument("--with-roster", action="store_true", help="명부(이름·직함)만 담기 (배포용, 생체정보 없음)")
    ap.add_argument("--zip", action="store_true", help="끝나고 zip으로 압축")
    ap.add_argument("--name", default="말결", help="폴더 이름")
    args = ap.parse_args()

    if sys.platform != "win32":
        print("이 꾸러미는 Windows용입니다.")
        return 1

    target = BUILD / args.name
    cache = BUILD / "_다운로드"
    target.mkdir(parents=True, exist_ok=True)

    print(f"[1/3] 파이썬 실행기 준비 ({PY_VERSION})")
    py_dir = make_embedded_python(target, cache)
    print("[2/3] 꾸러미 설치")
    install_packages(py_dir)
    print("[3/3] 프로그램·모델 복사")
    copy_program(target, not args.no_models, args.with_members, args.with_roster)

    print(f"\n완성: {target}  ({folder_size_mb(target)}MB)")
    if args.zip:
        zip_path = make_zip(target, args.with_members, args.with_roster)
        print(f"압축본: {zip_path}  ({zip_path.stat().st_size // 1024 // 1024}MB)")
    print("이 폴더(또는 zip)를 다른 PC에 복사하고 '말결 시작.bat' 을 누르면 됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
