"""설정 파일 로드/저장.

설정은 data/settings.json, 요약용 API 키는 data/.env 에 따로 저장됩니다.
"""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from typing import Any

from . import paths

DEFAULTS: dict[str, Any] = {
    "asr": {
        "engine": "faster-whisper",       # faster-whisper | lightning-simulwhisper
        "model": "large-v3-turbo",        # HuggingFace 이름 또는 로컬 CT2 디렉터리
        "device": "auto",                 # auto | cpu | cuda
        "compute_type": "auto",           # auto | int8 | int8_float16 | float16 | float32
        "language": "ko",
        "beam_size": 1,                   # 실시간이라 1(greedy) 권장
        "cpu_threads": 0,                 # 0이면 자동 (물리 코어 - 2)
        # 성능 모드: balanced(보통) | low(절전)   ※ 예전 fast 는 보통으로 처리합니다
        #  balanced - 인식이 끝나는 대로 다음 인식을 해 미리보기를 계속 갱신합니다. (기본, 빠른 PC는 2초마다)
        #  low      - 말이 끊길 때만 인식합니다. CPU를 가장 적게 씁니다(실시간 미리보기 없음).
        "performance": "balanced",
        "low_priority": True,             # 다른 작업에 CPU를 양보 (창 전환이 끊기지 않게)
        "min_chunk_sec": 1.0,             # (예전 fast 모드용, 지금은 쓰지 않습니다)
        "buffer_trim_sec": 14.0,          # 버퍼가 이보다 길면 문장 단위로 잘라냄
        "vad_threshold": 0.5,
        "silence_finalize_sec": 0.7,      # 이만큼 조용하면 발화 종료로 간주
        "condition_on_previous": True,    # 직전 확정 텍스트를 프롬프트로 사용
        "simulwhisper_path": "",          # (Mac 전용) 사용자가 클론한 Lightning-SimulWhisper 경로
        "frame_threshold": 25,            # (Mac 전용) AlignAtt 프레임 임계값
    },
    "audio": {
        "source": "loopback",             # loopback | microphone | file
        "device_index": None,             # None이면 기본 장치
        "record": True,
    },
    "speaker": {
        "face_enabled": False,            # 화면 감시는 켜고 싶을 때만 (기본 끔)
        "face_monitor": 1,                # mss 모니터 번호 (1=주 모니터)
        "face_region": None,              # [x, y, w, h] 화면 좌표. None이면 모니터 전체
        "face_fps": 2.0,
        "face_threshold": 0.36,           # SFace 코사인 유사도 임계값
        "voice_enabled": True,
        "voice_threshold": 0.65,          # 성문 일치 판정 기준 (낮추면 엉뚱한 사람으로 붙습니다)
        "voice_autoenroll": True,         # 얼굴로 확인된 구간의 목소리를 자동 등록
        "min_segment_sec": 1.2,           # 성문을 뽑을 수 있는 최소 길이
        "voice_match_min_sec": 2.5,       # 이보다 짧은 발언은 이름을 붙이지 않습니다
        "context_enabled": True,          # 사회자의 지명("OOO 의원님 나오셔서…")으로 다음 화자 지정
        "context_model": "",              # 비우면 제공자별 빠른 모델
        "context_provider": "codex_cli",  # 규칙으로 못 찾은 화자만 로그인된 Codex로 확인
    },
    "glossary": {
        # 용어집은 확정 뒤 자동 교정에 씁니다. (인식 힌트에 용어 목록을 넣는 기능은 정확도가 떨어져 없앴습니다:
        #  회의 90초 실측 기준과 차이 17.6% → 11.7%. 인식 힌트로는 직전 문장만 줍니다)
        "correction_enabled": True,
        "correction_threshold": 0.72,     # 자모 유사도 임계값
    },
    "summary": {
        "provider": "claude_cli",         # Claude Code 로그인 우선, 없으면 Codex 로그인 사용
        "api_key": "",
        "model": "",                      # 비우면 제공자별 기본 모델
        "base_url": "",                   # 사내 프록시 등 사용 시
    },
    "alerts": {
        "keywords": [],                   # 관심 키워드 (담당 사업명·소속 부서명 등)
        "os_notify": True,                # Windows 알림 (녹음에 섞이지 않도록 소리는 내지 않습니다)
    },
    "export": {
        "timestamp": True,
        "speaker": True,
    },
    "server": {
        # 기본은 이 PC에서만 접속 가능합니다.
        # 사내망에 열려면 "0.0.0.0" 으로 바꾸되, 반드시 관리자 비밀번호를 먼저 정하세요.
        "host": "127.0.0.1",
        "port": 8760,
    },
}


# 없앤 기능(인식 힌트에 용어 목록 넣기)의 옛 설정 이름
LEGACY_GLOSSARY_KEYS = ("prompt_enabled", "prompt_token_budget")

_lock = threading.RLock()
_cache: dict[str, Any] | None = None
_cache_stamp: tuple[int, int] | None = None   # 캐시를 읽을 때 파일의 (수정 시각, 크기)


def _stamp() -> tuple[int, int] | None:
    try:
        st = paths.SETTINGS_FILE.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_api_key() -> str | None:
    """data/.env 의 요약 키를 읽습니다. 항목이 없으면 None을 돌려줍니다."""
    try:
        lines = paths.SECRETS_FILE.read_text("utf-8").splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        if line.startswith("SUMMARY_API_KEY="):
            raw = line.split("=", 1)[1]
            try:
                return str(json.loads(raw))
            except json.JSONDecodeError:
                return raw
    return None


def _write_api_key(key: str) -> None:
    """다른 .env 항목을 보존하며 요약 키만 바꿉니다."""
    try:
        lines = paths.SECRETS_FILE.read_text("utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    lines = [line for line in lines if not line.startswith("SUMMARY_API_KEY=")]
    lines.append("SUMMARY_API_KEY=" + json.dumps(key, ensure_ascii=False))
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    paths.SECRETS_FILE.write_text("\n".join(lines) + "\n", "utf-8")


def load() -> dict[str, Any]:
    """설정을 돌려줍니다. 파일이 바뀌었으면 다시 읽습니다.

    프로그램이 둘 떠 있거나(두 번 실행 등) 파일을 직접 고친 경우에도 옛 내용을 들고 있다가
    저장하면서 다른 쪽이 넣은 값(관심 키워드 등)을 지워 버리지 않도록 합니다.
    """
    global _cache, _cache_stamp
    with _lock:
        stamp = _stamp()
        if _cache is None or stamp != _cache_stamp:
            saved: dict[str, Any] | None = {}
            if stamp is not None:
                try:
                    saved = json.loads(paths.SETTINGS_FILE.read_text("utf-8"))
                except (OSError, json.JSONDecodeError):
                    saved = None
            if saved is not None:
                summary = saved.get("summary", {})
                if isinstance(summary, dict) and "api_key" in summary:
                    old_key = summary.pop("api_key")
                    if _read_api_key() is None and old_key:
                        _write_api_key(str(old_key))
                    clean = paths.SETTINGS_FILE.with_suffix(".tmp")
                    clean.write_text(json.dumps(saved, ensure_ascii=False, indent=2), "utf-8")
                    clean.replace(paths.SETTINGS_FILE)
                    stamp = _stamp()
                _cache = _merge(DEFAULTS, saved)
                for key in LEGACY_GLOSSARY_KEYS:     # 없앤 기능의 옛 설정은 버립니다 (다음 저장 때 파일에서도 빠짐)
                    _cache["glossary"].pop(key, None)
                _cache_stamp = stamp
            elif _cache is None:          # 처음부터 못 읽으면 기본값으로 (파일은 건드리지 않음)
                _cache = deepcopy(DEFAULTS)
        _cache["summary"]["api_key"] = _read_api_key() or ""
        return deepcopy(_cache)


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """부분 업데이트 후 저장. 저장된 전체 설정을 돌려줍니다."""
    global _cache, _cache_stamp
    with _lock:
        current = load()                   # 파일의 최신 내용을 기준으로 합칩니다
        patch = deepcopy(patch)
        summary_patch = patch.get("summary")
        if isinstance(summary_patch, dict) and "api_key" in summary_patch:
            _write_api_key(str(summary_patch.pop("api_key")))
        merged = _merge(current, patch)
        merged["summary"]["api_key"] = _read_api_key() or ""
        paths.ensure_dirs()
        tmp = paths.SETTINGS_FILE.with_suffix(".tmp")
        saved = deepcopy(merged)
        saved["summary"].pop("api_key", None)
        tmp.write_text(json.dumps(saved, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(paths.SETTINGS_FILE)
        _cache = merged
        _cache_stamp = _stamp()
        return deepcopy(merged)


def public() -> dict[str, Any]:
    """UI로 내보낼 설정 (API 키는 가립니다)."""
    cfg = load()
    key = cfg["summary"].get("api_key") or ""
    cfg["summary"]["api_key"] = ("*" * max(0, len(key) - 4) + key[-4:]) if key else ""
    cfg["summary"]["has_key"] = bool(key)
    from . import summarize

    cfg["summary"]["ai_ready"] = summarize.available(cfg["summary"])
    if not cfg["summary"]["ai_ready"]:
        from . import cli_ai

        cfg["summary"]["ai_reason"] = " / ".join(f"{k}: {v}" for k, v in cli_ai.LAST_ERROR.items())
    return cfg
