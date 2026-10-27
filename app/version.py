"""프로그램 버전과 업데이트 확인.

GitLab 패키지 저장소에 올려 둔 최신 버전 정보(latest.json)를 확인해서, 새 버전이 있으면 화면 위에 알립니다.
내려받기는 사용자가 누를 때만 합니다. (회의 녹취 중에 멋대로 받지 않습니다)

새 버전을 낼 때
  1. 이 파일의 VERSION 을 올립니다.
  2. `python -m scripts.build_exe --no-models --model-url …` 로 경량 설치 파일을 만듭니다.
  3. 커밋·푸시한 뒤 `배포/릴리스_올리기.ps1` 을 실행합니다.
     (설치 파일 → 패키지 저장소, 릴리스 v1.1.0 만들기, latest.json 갱신)
"""
from __future__ import annotations

import json
import logging
import threading
import time

import httpx

VERSION = "1.00"
PRODUCT = "malgyeol"
GITLAB = "https://gitlab.aigov.go.kr"
PROJECT = "soyoung776/livecaption"
# 릴리스 목록 대신, 로그인 없이 받을 수 있는
# 패키지 저장소에 둔 '최신 버전 정보' 파일을 읽습니다. (릴리스 스크립트가 릴리스와 함께 올림)
#   {"version": "1.1.0", "notes": "...", "page": "...", "download": {"name": "...", "url": "...", "size": 0}}
LATEST_URL = "https://github.com/worlyung/malgyeol/releases/latest/download/latest.json"
RELEASE_PAGE = "https://github.com/worlyung/malgyeol/releases"
CHECK_INTERVAL = 6 * 3600          # 6시간에 한 번만 확인

log = logging.getLogger(__name__)

_cache: dict = {"checked": 0.0, "result": None}
_lock = threading.Lock()


def parse(version: str) -> tuple:
    """'v1.2.3' -> (1, 2, 3). 비교하기 쉽게 숫자 튜플로 바꿉니다."""
    cleaned = version.strip().lstrip("vV").split("-")[0]
    parts = []
    for piece in cleaned.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def fetch_latest(timeout: float = 6.0) -> dict | None:
    """GitLab 패키지 저장소의 최신 버전 정보를 가져옵니다. 인터넷이 없거나 아직 올린 적이 없으면 None."""
    headers = {"User-Agent": f"live-caption/{VERSION}"}
    try:
        resp = httpx.get(LATEST_URL, headers=headers, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        data = json.loads(resp.content)
    except Exception as exc:  # noqa: BLE001 - 인터넷이 막힌 내부망에서도 조용히 넘어가야 합니다
        log.debug("업데이트 확인 실패: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    # 이름과 버전 체계가 바뀐 말결에는 이전 LiveCaption 배포본을 권하지 않습니다.
    if data.get("product") != PRODUCT:
        return None

    download = data.get("download") or {}
    url = str(download.get("url") or "")
    assets = ([{"name": download.get("name") or url.rsplit("/", 1)[-1], "url": url, "size": download.get("size", 0)}]
              if url.startswith("https://") else [])            # 설치 파일 주소는 https 만 씁니다
    return {
        "version": str(data.get("version") or "").strip(),
        "notes": str(data.get("notes") or "").strip()[:2000],
        "published": str(data.get("published") or ""),
        "page": data.get("page") if str(data.get("page") or "").startswith("https://") else RELEASE_PAGE,
        "assets": assets,
    }


def check(force: bool = False) -> dict:
    """현재 버전과 최신 버전을 비교합니다."""
    with _lock:
        fresh = time.time() - _cache["checked"] < CHECK_INTERVAL
        if fresh and not force and _cache["result"] is not None:
            return _cache["result"]

    latest = fetch_latest()
    result = {
        "current": VERSION,
        "latest": latest["version"] if latest else None,
        "update_available": False,
        "notes": latest["notes"] if latest else "",
        "page": latest["page"] if latest else RELEASE_PAGE,
        "download": (latest["assets"][0] if latest and latest["assets"] else None),
        "checked_at": time.time(),
        "offline": latest is None,
    }
    if latest and latest["version"]:
        result["update_available"] = parse(latest["version"]) > parse(VERSION)

    with _lock:
        _cache["checked"] = time.time()
        _cache["result"] = result
    return result
