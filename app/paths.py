"""프로젝트 경로 모음."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("LT_DATA_DIR", ROOT / "data"))
MODELS_DIR = DATA_DIR / "models"
SESSIONS_DIR = DATA_DIR / "sessions"
FACES_DIR = DATA_DIR / "faces"
WEB_DIR = ROOT / "web"

SETTINGS_FILE = DATA_DIR / "settings.json"
SECRETS_FILE = DATA_DIR / ".env"
GLOSSARY_FILE = DATA_DIR / "glossary.json"
MEMBERS_FILE = DATA_DIR / "members.json"

# 다운로드되는 로컬 모델 파일
SILERO_VAD = MODELS_DIR / "silero_vad.onnx"
FACE_DETECT_MODEL = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
FACE_RECOG_MODEL = MODELS_DIR / "face_recognition_sface_2021dec.onnx"
SPEAKER_EMBED_MODEL = MODELS_DIR / "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"


def ensure_dirs() -> None:
    for d in (DATA_DIR, MODELS_DIR, SESSIONS_DIR, FACES_DIR):
        d.mkdir(parents=True, exist_ok=True)


def ascii_safe(path: Path) -> str:
    """OpenCV가 읽을 수 있는 경로를 돌려줍니다.

    Windows의 OpenCV는 경로에 한글 등 ASCII가 아닌 글자가 있으면 파일을 열지 못합니다.
    그런 경우 ASCII 경로로 복사해 두고 그 경로를 씁니다.
    """
    text = str(path)
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        pass

    import shutil
    import tempfile

    candidates = [
        Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "LiveTranscription" / "models",
        Path(tempfile.gettempdir()) / "livetranscription_models",
        Path(r"C:\LiveTranscription\models"),
    ]
    for base in candidates:
        try:
            str(base).encode("ascii")
        except UnicodeEncodeError:
            continue
        try:
            base.mkdir(parents=True, exist_ok=True)
            dest = base / path.name
            if not dest.exists() or dest.stat().st_size != path.stat().st_size:
                shutil.copy2(path, dest)
            return str(dest)
        except OSError:
            continue
    raise RuntimeError(
        f"OpenCV가 읽을 수 있는 경로를 만들지 못했습니다: {path}\n"
        "프로젝트를 영문 경로(예: C:/LiveTranscription)로 옮기면 해결됩니다."
    )
