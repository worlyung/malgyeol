"""AI 제공자별로 고를 수 있는 모델 목록과 연결 시험.

모델 이름은 자주 바뀌므로 되도록 그때그때 제공자에게 물어 목록을 만듭니다.
물어볼 수 없으면(키 없음·인터넷 없음) 아래 기본 목록을 씁니다.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time

from . import cli_ai, summarize

log = logging.getLogger(__name__)

# (모델 이름, 화면에 보일 설명). 첫 줄 "" 는 '기본값(제공자가 정한 모델)'
FALLBACK = {
    "claude_cli": [("opus", "Opus — 가장 똑똑함 (느리고 사용량 많음)"), ("sonnet", "Sonnet — 균형 (권장)"),
                   ("haiku", "Haiku — 빠르고 가벼움")],
    "codex_cli": [],
    "anthropic": [("claude-opus-5-5", "Claude Opus 5.5"), ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
                  ("claude-haiku-5-5", "Claude Haiku 5.5")],
    "openai": [],
    "gemini": [("gemini-pro-latest", "Gemini Pro (최신)"), ("gemini-flash-latest", "Gemini Flash (최신)"),
               ("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite")],
    "clova": [("HCX-GOV-THINK-V1-32B", "HCX-GOV-THINK 32B (기본 · 가장 빠름)"), ("openai/gpt-oss-120b", "GPT-OSS 120B"),
              ("K-Exaone-236B", "K-Exaone 236B"), ("LLM42-Gemma4", "LLM42-Gemma4 31B"),
              ("NVIDIA-Nemotron-3-Ultra-550B-A55B", "Nemotron-3-Ultra 550B")],
}


def _codex() -> list[tuple[str, str]]:
    cli_ai._refresh_path()        # noqa: SLF001 - 바탕화면 아이콘으로 켠 창은 PATH 가 옛것이라 node 를 못 찾습니다
    exe = shutil.which(cli_ai.COMMANDS["codex_cli"])
    if not exe:
        return []
    out = subprocess.run([exe, "debug", "models"], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if out.returncode != 0:
        raise RuntimeError(f"codex debug models 실패({out.returncode}): {(out.stderr or '').strip()[-200:]}")
    models = [m for m in json.loads(out.stdout).get("models", []) if m.get("visibility") == "list"]
    models.sort(key=lambda m: m.get("priority", 99))
    return [(m["slug"], f"{m.get('display_name') or m['slug']} — {m.get('description', '')}".strip(" —"))
            for m in models]


def _live(provider: str, cfg: dict) -> list[tuple[str, str]]:
    import httpx

    key = cfg.get("api_key") or ""
    if provider == "codex_cli":
        return _codex()
    if not key:
        return []
    if provider == "anthropic":
        r = httpx.get("https://api.anthropic.com/v1/models", timeout=15,
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        r.raise_for_status()
        return [(m["id"], m.get("display_name") or m["id"]) for m in r.json().get("data", [])]
    if provider == "openai":
        base = (cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        r = httpx.get(f"{base}/models", timeout=15, headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        ids = sorted((m["id"] for m in r.json().get("data", []) if m["id"].startswith(("gpt-", "o"))), reverse=True)
        return [(i, i) for i in ids]
    if provider == "gemini":
        base = (cfg.get("base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        r = httpx.get(f"{base}/models", timeout=15, params={"key": key, "pageSize": 200})
        r.raise_for_status()
        return [(m["name"].removeprefix("models/"), m.get("displayName") or m["name"])
                for m in r.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
    return []


def models(provider: str, cfg: dict) -> dict:
    """{"models": [{"id","label"}], "live": 제공자에게서 받은 목록인지}"""
    live: list[tuple[str, str]] = []
    try:
        live = _live(provider, cfg)
    except Exception as exc:  # noqa: BLE001 - 목록을 못 받아도 기본 목록·직접 입력으로 고를 수 있습니다
        log.info("모델 목록을 받지 못했습니다(%s): %s", provider, exc)
    items = live or FALLBACK.get(provider, [])
    return {"models": [{"id": i, "label": label} for i, label in items], "live": bool(live)}


def test(cfg: dict) -> dict:
    """아주 짧은 질문 하나로 실제 연결을 시험합니다. 요금은 거의 들지 않습니다."""
    t0 = time.monotonic()
    answer = summarize._call("연결 확인입니다. '확인'이라고만 답하세요.", cfg, system="짧게 답합니다.",  # noqa: SLF001
                             max_tokens=20, timeout=120)
    return {"ok": True, "seconds": round(time.monotonic() - t0, 1), "answer": answer.strip()[:40],
            "used": cli_ai.LAST_USED.get("provider", "") if hasattr(cli_ai, "LAST_USED") else ""}
