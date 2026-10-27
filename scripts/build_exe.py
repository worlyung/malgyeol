"""다른 PC에 건네줄 '단일 exe' 만들기.

    python -m scripts.build_exe                 # 배포용: 모델 + 명부(이름·직함), 얼굴·성문 없음 (약 1GB)
    python -m scripts.build_exe --with-members  # 개인용: 명부·얼굴·성문까지 → 회의받아쓰기_설치_개인용.exe
    python -m scripts.build_exe --no-models     # 경량판: 모델 없이, 설치할 때 인터넷에서 받음 (명부 이름·용어사전은 포함, 얼굴·성문 없음)
                                                #   → 회의받아쓰기_설치_경량.exe
    python -m scripts.build_exe --no-models --model-url https://…/조각을올린곳/   (끝이 / 면 조각들, 아니면 묶음 하나)
                                                # 경량판이 원본(1.6GB) 대신 올려 둔 '가벼운 모델 묶음'(약 0.9GB)을 받게 함
                                                #   (묶음은 python -m scripts.download_models --make-bundle 로 만듦)

만들어지는 것:  배포/회의받아쓰기_설치.exe   ← 이 파일 하나만 주면 됩니다. (인터넷 없이 설치)

구조
    [작은 설치 프로그램 exe]  +  [프로그램·모델 zip]   을 한 파일로 이어 붙입니다.
    받는 사람이 실행하면 폴더에 풀고 바탕화면 바로가기를 만듭니다.
    (매번 압축을 푸는 방식이 아니라 처음 한 번만 풀기 때문에 실행이 빠릅니다)
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import paths, version  # noqa: E402
from scripts.build_package import (  # noqa: E402
    BUILD, _zip_allowed, copy_program, folder_size_mb, install_packages, make_embedded_python,
)

EXE_NAME = f"말결_{version.VERSION}_설치.exe"
LAUNCHER_NAME = "말결.exe"
WORK = BUILD / "_빌드"
ICON = paths.ROOT / "assets" / "icon.ico"
INSTALLER_FONT = paths.ROOT / "assets" / "NotoSansKR-Installer.ttf"


def version_resource() -> Path:
    """Windows 파일 속성에 표시할 제품 이름과 버전을 만듭니다."""
    WORK.mkdir(parents=True, exist_ok=True)
    resource = WORK / "windows-version.txt"
    numeric = (*version.parse(version.VERSION), 0)
    resource.write_text(
        f"VSVersionInfo(ffi=FixedFileInfo(filevers={numeric!r}, prodvers={numeric!r}, "
        "mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0,0)), "
        "kids=[StringFileInfo([StringTable('041204B0', ["
        "StringStruct('ProductName', '말결'), StringStruct('FileDescription', '말결 설치 및 실행'), "
        f"StringStruct('FileVersion', '{version.VERSION}'), StringStruct('ProductVersion', '{version.VERSION}')"
        "])]), VarFileInfo([VarStruct('Translation', [1042, 1200])])])", encoding="utf-8")
    return resource


def build_launcher(target: Path) -> None:
    """설치 폴더에 들어갈 실행기(아이콘 붙은 exe)를 만듭니다."""
    print("  실행기 exe 만드는 중")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--onefile", "--noconsole",
         "--name", LAUNCHER_NAME.replace(".exe", ""),
         "--icon", str(ICON),
         "--version-file", str(version_resource()),
         "--distpath", str(WORK / "launcher"), "--workpath", str(WORK / "work_l"),
         "--specpath", str(WORK), "--noconfirm", "--clean",
         str(paths.ROOT / "scripts" / "launcher_main.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    built = WORK / "launcher" / LAUNCHER_NAME
    if result.returncode != 0 or not built.exists():
        print(result.stderr[-1500:])
        raise RuntimeError("실행기 exe를 만들지 못했습니다.")
    shutil.copy2(built, target / LAUNCHER_NAME)
    print(f"    실행기: {built.stat().st_size / 1048576:.0f}MB")


def build_payload_folder(name: str, with_models: bool, with_members: bool, with_roster: bool,
                         reuse: bool) -> Path:
    """설치될 내용(파이썬 실행기 + 프로그램 + 모델)을 한 폴더에 모읍니다."""
    target = BUILD / name
    ready = (target / "python" / "python.exe").exists()
    if ready and reuse:
        print(f"[1/4] 기존 꾸러미 재사용: {target}")
        copy_program(target, with_models, with_members, with_roster)     # 프로그램만 최신으로
        build_launcher(target)
        return target

    print("[1/4] 파이썬 실행기 준비")
    target.mkdir(parents=True, exist_ok=True)
    py_dir = make_embedded_python(target, BUILD / "_다운로드")
    install_packages(py_dir)
    copy_program(target, with_models, with_members, with_roster)
    build_launcher(target)
    return target


def zip_payload(folder: Path, dest: Path, with_members: bool, with_roster: bool) -> Path:
    """폴더를 zip 하나로 묶습니다. (경로는 폴더 안쪽 기준)

    정해진 파일만 담습니다. 꾸러미 폴더에서 프로그램을 실행해 보면 녹음·설정(API 키)·실행 기록이 쌓이는데,
    그런 것이 설치 파일에 섞여 나가지 않게 합니다.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    # __pycache__ 는 넣지 않습니다. 용량도 크고 경로가 길어져 Windows 260자 제한에 걸립니다.
    files = [f for f in folder.rglob("*")
             if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc"
             and (f.name == LAUNCHER_NAME and f.parent == folder
                  or _zip_allowed(f.relative_to(folder), with_members, with_roster))]
    total = sum(f.stat().st_size for f in files)
    print(f"[2/4] 압축 중: 파일 {len(files)}개 / {total / 1073741824:.1f}GB (몇 분 걸립니다)")
    done = 0
    step = 0
    started = time.time()
    # NVIDIA DLL을 포함한 전체 설치본도 단일 배포 파일 크기를 줄입니다.
    # 설치기는 Python zipfile로 읽으므로 LZMA 형식도 그대로 풀 수 있습니다.
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_LZMA) as z:
        z.writestr("data/sessions/", "")          # 빈 녹음 폴더
        for f in files:
            z.write(f, str(f.relative_to(folder)))
            done += f.stat().st_size
            percent = int(done * 100 / max(1, total))
            if percent >= step + 10:
                step = percent - percent % 10
                print(f"    {step}%  ({time.time() - started:.0f}초)", flush=True)
    print(f"    압축본: {dest.stat().st_size / 1073741824:.2f}GB")
    return dest


def build_installer_exe() -> Path:
    """설치 프로그램만 작은 exe로 만듭니다."""
    print("[3/4] 설치 프로그램 exe 만드는 중")
    WORK.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--onefile", "--noconsole",
         "--name", "installer_stub",
         "--icon", str(ICON),
         "--version-file", str(version_resource()),
         "--add-data", f"{ICON};.",
         "--add-data", f"{INSTALLER_FONT};.",
         "--add-data", f"{ICON.with_name('icon-44.png')};.",
         "--distpath", str(WORK / "dist"), "--workpath", str(WORK / "work"),
         "--specpath", str(WORK), "--noconfirm", "--clean",
         str(paths.ROOT / "scripts" / "installer_main.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    stub = WORK / "dist" / "installer_stub.exe"
    if result.returncode != 0 or not stub.exists():
        print(result.stdout[-2000:])
        print(result.stderr[-2000:])
        raise RuntimeError("설치 프로그램 exe를 만들지 못했습니다.")
    print(f"    설치 프로그램: {stub.stat().st_size / 1048576:.0f}MB")
    return stub


def bundle_source(bundle: Path, url: str) -> dict | None:
    """올려 둔 모델 묶음의 주소·지문·크기 (경량판 data/models_source.json 에 들어갑니다)."""
    import hashlib

    if not bundle.exists():
        print(f"모델 묶음 파일이 없습니다: {bundle}\n"
              f"  먼저 만드세요:  python -m scripts.download_models --make-bundle \"{bundle}\"")
        return None
    if not url.lower().startswith("https://"):
        print("모델 묶음 주소는 https:// 로 시작해야 합니다.")
        return None
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    with zipfile.ZipFile(bundle) as z:
        models = {n.split("/")[1] for n in z.namelist() if n.startswith("whisper/") and n.count("/") >= 2}
    info = {"sha256": sha256(bundle), "size": bundle.stat().st_size, "model": next(iter(models), "")}
    if not url.endswith("/"):
        return {"url": url, **info}
    # 주소가 / 로 끝나면 조각(묶음이름.001 …)을 올려 둔 곳입니다
    parts = sorted(bundle.parent.glob(bundle.name + ".[0-9][0-9][0-9]"))
    if not parts or sum(p.stat().st_size for p in parts) != info["size"]:
        print(f"조각 파일이 없거나 묶음과 크기가 다릅니다: {bundle}.001 …\n"
              f"  먼저 나누세요:  python -m scripts.download_models --make-bundle \"{bundle}\" --split 45")
        return None
    return {**info, "parts": [{"url": url + p.name, "sha256": sha256(p), "size": p.stat().st_size} for p in parts]}


MEI_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"     # PyInstaller 꾸러미 끝 표시
MEI_COOKIE = "!8sIIII64s"


def concat(stub: Path, payload: Path, dest: Path) -> Path:
    """[설치 프로그램 앞부분] + [zip] + [설치 프로그램 꾸러미] 순서로 합칩니다.

    설치 프로그램(PyInstaller)은 시작할 때 파일 '맨 끝'에서 자기 꾸러미 표시를 찾습니다.
    zip 을 맨 뒤에 붙이면 1GB 가까이를 거꾸로 훑느라 창이 늦게 뜨므로, 꾸러미를 맨 끝에 둡니다.
    설치 프로그램은 꾸러미 앞까지를 zip 으로 읽습니다. (installer_main.open_payload)
    """
    import struct

    print("[4/4] 한 파일로 합치는 중")
    data = stub.read_bytes()
    at = data.rfind(MEI_MAGIC)
    if at < 0 or at + struct.calcsize(MEI_COOKIE) != len(data):
        raise RuntimeError("설치 프로그램 exe 끝에서 꾸러미 표시를 찾지 못했습니다.")
    pkg_len = struct.unpack(MEI_COOKIE, data[at:])[1]
    pkg_start = len(data) - pkg_len
    if dest.exists():
        dest.unlink()
    with open(dest, "wb") as out:
        out.write(data[:pkg_start])
        with open(payload, "rb") as fh:
            shutil.copyfileobj(fh, out, 1024 * 1024)
        out.write(data[pkg_start:])
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="단일 exe 만들기")
    ap.add_argument("--no-models", action="store_true", help="음성인식 모델 빼기")
    ap.add_argument("--with-members", action="store_true", help="등록된 사람(얼굴·목소리 특징)까지 넣기 (내부용)")
    ap.add_argument("--no-roster", action="store_true", help="명부(이름·직함)도 빼기")
    ap.add_argument("--fresh", action="store_true", help="파이썬 꾸러미부터 새로 만들기")
    ap.add_argument("--exe-name", default="", help="설치 파일 이름 (기본: 회의받아쓰기_설치, --with-members 면 _개인용)")
    ap.add_argument("--model-url", default="", help="경량판: 올려 둔 모델 묶음 주소 (원본 대신 이것을 받음)")
    ap.add_argument("--model-bundle", default=str(BUILD / "livecaption-models-large-v3-turbo.zip"),
                    help="그 주소에 올린 것과 같은 묶음 파일 (지문·크기를 읽음)")
    args = ap.parse_args()
    if args.model_url and not args.no_models:
        print("--model-url 은 경량판(--no-models)에서만 씁니다.")
        return 1
    source = None
    if args.model_url:
        source = bundle_source(Path(args.model_bundle), args.model_url)
        if not source:
            return 1

    if sys.platform != "win32":
        print("이 설치 파일은 Windows용입니다.")
        return 1

    BUILD.mkdir(parents=True, exist_ok=True)
    # 모델을 넣는 꾸러미와 빼는 꾸러미를 따로 둡니다 (섞이면 용량이 꼬입니다)
    # 기본은 배포용(명부만, 얼굴·성문 없음). 내부용(--with-members)은 폴더를 따로 씁니다.
    with_roster = not args.with_members and not args.no_roster
    if args.no_models:
        folder_name = "말결-경량"
    else:
        folder_name = "말결" if args.with_members else "말결 배포용"
    folder = build_payload_folder(folder_name, not args.no_models, args.with_members, with_roster,
                                  reuse=not args.fresh)
    # 경량판이 받을 모델 묶음 주소 (없으면 지난번 주소가 남지 않게 지웁니다 → 원본을 받는 방법으로)
    source_file = folder / "data" / "models_source.json"
    if source:
        source_file.write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8")
        where = f"{len(source['parts'])}조각 · {source['parts'][0]['url']} …" if source.get("parts") else source["url"]
        print(f"  모델 묶음 주소 넣음: {where}  ({source['size'] / 1073741824:.2f}GB)")
    else:
        source_file.unlink(missing_ok=True)
    # 배포용·개인용·경량판이 서로 덮어쓰지 않도록 이름을 나눕니다
    suffix = "_개인용" if args.with_members else ("_경량" if args.no_models else "")
    payload_zip = zip_payload(folder, WORK / f"payload{suffix}.zip", args.with_members, with_roster)
    stub = build_installer_exe()
    exe_name = (args.exe_name or (EXE_NAME.removesuffix(".exe") + suffix)).removesuffix(".exe") + ".exe"
    dest = concat(stub, payload_zip, BUILD / exe_name)

    size = dest.stat().st_size
    print()
    print(f"완성: {dest}")
    print(f"  크기: {size / 1073741824:.2f}GB   (설치 후 {folder_size_mb(folder)}MB 차지)")
    print("  이 파일 하나만 다른 PC에 주면 됩니다. 두 번 누르면 설치됩니다.")
    print()
    print("  ※ 백신이 처음 보는 exe를 막을 수 있습니다. 사내 배포 전에 한 대에서 확인해 보세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
