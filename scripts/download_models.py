"""로컬 모델 내려받기.

    python -m scripts.download_models            # 전부
    python -m scripts.download_models --whisper  # 음성인식 모델만
    python -m scripts.download_models --make-bundle 모델묶음.zip   # 받아 둔 모델을 묶음 하나로 (올려 두기용)

내려받는 것:
  - YuNet 얼굴검출 / SFace 얼굴인식 (OpenCV Zoo, 합쳐서 약 40MB)
  - 3D-Speaker CAM++ 화자 임베딩 (sherpa-onnx, 약 28MB)
  - Whisper large-v3-turbo CT2 (HuggingFace, 약 1.6GB → 받은 뒤 가볍게(int8) 저장해 약 0.85GB)
VAD(Silero)는 faster-whisper에 들어 있어 따로 받지 않습니다.

모델 묶음 (경량 설치판)
  data/models_source.json 이 있으면 먼저 그 주소에서 '가볍게 저장해 둔 모델 묶음(약 0.9GB)'을 받습니다.
  원본(1.6GB)을 받아 PC에서 줄이는 것보다 받는 양이 절반 가까이 적습니다.
  받은 파일은 SHA-256 지문을 맞춰 보고, 주소에 연결되지 않거나 지문이 다르면 원래 방법으로 받습니다.
      {"url": "https://…/모델묶음.zip", "sha256": "…", "size": 912345678}
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import paths  # noqa: E402

# 기관망 보안 장비의 인증서도 믿도록 Windows 인증서 저장소를 씁니다. (없으면 그냥 넘어감)
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001
    pass

ZOO = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
# (업스트림 릴리스 태그 철자가 'recongition' 입니다. 오타 아님)
SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models"

FILES = [
    (paths.FACE_DETECT_MODEL, f"{ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx"),
    (paths.FACE_RECOG_MODEL, f"{ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx"),
    (paths.SPEAKER_EMBED_MODEL, f"{SHERPA}/{paths.SPEAKER_EMBED_MODEL.name}"),
]


def download(dest: Path, url: str) -> None:
    if dest.exists() and dest.stat().st_size > 100_000:
        print(f"  이미 있음: {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  내려받는 중: {dest.name} <- {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    headers = {"User-Agent": "LiveTranscription/1.0"}
    with (httpx.stream("GET", url, headers=headers, timeout=120, follow_redirects=True) as resp,
          open(tmp, "wb") as fh):
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        step = 0
        for chunk in resp.iter_bytes(1 << 16):
            fh.write(chunk)
            done += len(chunk)
            if total and done * 20 // total > step:
                step = done * 20 // total
                print(f"    {done * 100 // total}% ({done // 1024}KB)")
    print(f"    받음: {done // 1024}KB")
    if tmp.stat().st_size < 100_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{dest.name} 다운로드 실패 (파일이 너무 작습니다)")
    tmp.replace(dest)


def download_whisper(model: str) -> None:
    from app.asr.faster_whisper_engine import DEFAULT_MODEL, RETIRED_MODELS, resolve_model
    from huggingface_hub import snapshot_download

    repo = resolve_model(model)
    print(f"  Whisper 모델: {repo}")
    if os.path.isdir(repo):
        print("    이미 받아 둔 모델이 있습니다.")
        return
    cache = paths.MODELS_DIR / "whisper"
    tmp_cache = cache / "_download"            # 원본은 여기에 받았다가 가볍게 저장한 뒤 통째로 지웁니다
    got = snapshot_download(
        repo_id=repo,
        cache_dir=str(tmp_cache),
        allow_patterns=["*.bin", "*.json", "*.txt", "*.model"],
    )
    # 가볍게(int8) 다시 저장하고 원본은 지웁니다. (디스크 1.5GB → 약 0.85GB, 인식 결과 같음)
    from scripts.slim_whisper import slim

    print("    가볍게 저장하는 중...")
    slim(Path(got), cache / (DEFAULT_MODEL if model in RETIRED_MODELS else model))
    shutil.rmtree(tmp_cache, ignore_errors=True)


SOURCE_FILE = paths.DATA_DIR / "models_source.json"
BUNDLE_WHISPER = "whisper"            # 묶음 안: whisper/<모델 이름>/… 와 얼굴·화자 onnx


def make_bundle(dest: Path, model: str) -> dict:
    """받아 두고 가볍게 저장한 모델을 zip 하나로 묶습니다. models_source.json 에 넣을 정보를 돌려줍니다."""
    import hashlib
    import zipfile

    wdir = paths.MODELS_DIR / "whisper" / model
    if not (wdir / "model.bin").exists():
        raise RuntimeError(f"가볍게 저장한 음성인식 모델이 없습니다: {wdir}  (먼저 download_models 를 실행하세요)")
    files = [(f, f"{BUNDLE_WHISPER}/{model}/{f.name}") for f in sorted(wdir.iterdir()) if f.is_file()]
    for dest_model, _url in FILES:
        if not dest_model.exists():
            raise RuntimeError(f"얼굴·화자 모델이 없습니다: {dest_model.name}")
        files.append((dest_model, dest_model.name))
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 모델 파일은 거의 줄지 않으므로 압축하지 않고 담습니다 (빠르고, 받는 PC에서 푸는 것도 빠름)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as z:
        for src, name in files:
            z.write(src, name)
    digest = hashlib.sha256()
    with open(dest, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return {"model": model, "sha256": digest.hexdigest(), "size": dest.stat().st_size}


def split_bundle(bundle: Path, part_mb: int) -> list[dict]:
    """묶음을 part_mb 크기 조각(묶음이름.001, .002 …)으로 나눕니다. 조각마다 이름·지문·크기를 돌려줍니다.

    올려 둘 곳(GitLab 패키지 저장소 등)에 파일 하나의 크기 제한이 있을 때 씁니다.
    """
    import hashlib

    for old in bundle.parent.glob(bundle.name + ".[0-9][0-9][0-9]"):
        old.unlink()                          # 지난번 조각이 섞이지 않게
    size = part_mb << 20
    out = []
    with open(bundle, "rb") as src:
        for no in range(1, 1000):
            data = src.read(size)
            if not data:
                break
            part = bundle.with_name(f"{bundle.name}.{no:03d}")
            part.write_bytes(data)
            out.append({"name": part.name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    return out


def download_bundle(source: dict, model: str) -> bool:
    """올려 둔 모델 묶음을 받아 data/models 에 풉니다. 성공하면 True (실패하면 원래 방법으로 받습니다)."""
    import hashlib
    import zipfile

    want = (source.get("sha256") or "").lower()
    # 서버의 파일 크기 제한 때문에 여러 조각(parts)으로 올려 둘 수 있습니다. 조각을 차례로 받아 이어 붙입니다.
    parts = source.get("parts") or ([{"url": source["url"]}] if source.get("url") else [])
    if not parts or not want:
        return False
    if source.get("model") and source["model"] != model:
        print(f"  묶음의 모델({source['model']})이 설정({model})과 달라 원래 방법으로 받습니다.")
        return False
    paths.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = paths.MODELS_DIR / "_bundle.zip.part"
    base = parts[0]["url"].rsplit("/", 1)[0]
    print(f"  모델 묶음 받는 중 <- {base}/ ({len(parts)}조각)" if len(parts) > 1 else f"  모델 묶음 받는 중 <- {base}")
    try:
        digest = hashlib.sha256()
        headers = {"User-Agent": "LiveTranscription/1.0"}
        total = int(source.get("size") or 0)
        done = step = 0
        with open(tmp, "wb") as fh:
            for no, part in enumerate(parts, 1):
                piece = hashlib.sha256()
                with httpx.stream("GET", part["url"], headers=headers, timeout=120, follow_redirects=True) as resp:
                    resp.raise_for_status()
                    if not total:
                        total = int(resp.headers.get("Content-Length") or 0)
                    for chunk in resp.iter_bytes(1 << 20):
                        fh.write(chunk)
                        digest.update(chunk)
                        piece.update(chunk)
                        done += len(chunk)
                        if total and done * 20 // total > step:
                            step = done * 20 // total
                            print(f"    {min(100, done * 100 // total)}% ({done >> 20}MB / {total >> 20}MB)")
                if part.get("sha256") and piece.hexdigest() != part["sha256"].lower():
                    raise RuntimeError(f"{no}번째 조각의 지문(SHA-256)이 맞지 않습니다. 손상되었거나 다른 파일입니다.")
        if digest.hexdigest() != want:
            raise RuntimeError("받은 파일의 지문(SHA-256)이 맞지 않습니다. 손상되었거나 다른 파일입니다.")
        print("  푸는 중...")
        root = paths.MODELS_DIR.resolve()
        with zipfile.ZipFile(tmp) as z:
            for info in z.infolist():
                out = (root / info.filename).resolve()
                if root not in out.parents:          # 묶음 밖으로 나가는 경로는 풀지 않습니다
                    raise RuntimeError(f"묶음 안의 경로가 잘못되었습니다: {info.filename}")
            z.extractall(root)
        print("  모델 묶음을 받았습니다.")
        return True
    except Exception as exc:  # noqa: BLE001 - 어떤 이유든 원래 방법으로 이어서 받습니다
        print(f"  ! 모델 묶음을 받지 못했습니다: {exc}")
        print("  원래 방법(원본을 받아 PC에서 가볍게 저장)으로 받습니다.")
        return False
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--whisper", action="store_true", help="음성인식 모델만")
    ap.add_argument("--aux", action="store_true", help="얼굴/화자 모델만")
    ap.add_argument("--model", default=None, help="Whisper 모델 이름 (기본: 설정값)")
    ap.add_argument("--make-bundle", metavar="ZIP", help="받아 둔 모델을 올려 두기용 묶음 하나로 만들기")
    ap.add_argument("--split", type=int, default=0, metavar="MB",
                    help="--make-bundle 과 함께: 묶음을 이 크기(MB) 조각으로도 나누기 (올릴 곳의 크기 제한용)")
    args = ap.parse_args()

    paths.ensure_dirs()
    do_all = not (args.whisper or args.aux)

    if args.make_bundle or (do_all and SOURCE_FILE.exists()):
        from app import config
        from app.asr.faster_whisper_engine import DEFAULT_MODEL, RETIRED_MODELS

        model = args.model or config.load()["asr"]["model"]
        model = DEFAULT_MODEL if model in RETIRED_MODELS else model
        if args.make_bundle:
            bundle = Path(args.make_bundle)
            info = make_bundle(bundle, model) if not bundle.exists() or not args.split else \
                {"model": model, "size": bundle.stat().st_size}
            print(f"묶음: {args.make_bundle}  ({info['size'] >> 20}MB)")
            if args.split:
                parts = split_bundle(bundle, args.split)
                print(f"조각 {len(parts)}개: {parts[0]['name']} … {parts[-1]['name']} (각 {args.split}MB 이하)")
            print(json.dumps(info, ensure_ascii=False))
            return 0
        need = not (paths.MODELS_DIR / "whisper" / model / "model.bin").exists() or \
            not all(dest.exists() for dest, _url in FILES)
        if need:
            print("[모델 묶음] 줄여 둔 모델을 한 번에 받습니다")
            try:
                source = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                source = {}
            if download_bundle(source, model):
                print("완료.")
                return 0

    if do_all or args.aux:
        print("[1/2] 얼굴/화자 모델")
        for dest, url in FILES:
            try:
                download(dest, url)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! 실패: {dest.name}: {exc}")

    if do_all or args.whisper:
        print("[2/2] 음성인식 모델")
        from app import config

        model = args.model or config.load()["asr"]["model"]
        try:
            download_whisper(model)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 실패: {exc}")
            return 1

    print("완료.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
