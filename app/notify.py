"""관심 키워드 알림.

발언에 관심 키워드(담당 사업명·소속 부서명 등)가 나오면 Windows 알림을 띄웁니다.
프로그램 창이 최소화되어 있어도 보이도록 서버 쪽에서 띄웁니다.
별도 패키지 없이 Windows PowerShell 의 알림 기능을 씁니다.
알림 소리는 끕니다. ('PC 소리'를 녹음할 때 알림음이 녹음에 섞이지 않도록)
"""
from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import sys
import threading
import time

log = logging.getLogger(__name__)

# 알림 기능이 등록된 앱 이름이 있어야 알림이 뜹니다. Windows PowerShell 의 것을 빌려 씁니다.
_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $xml.GetElementsByTagName('text')
$t.Item(0).AppendChild($xml.CreateTextNode($env:LT_TITLE)) > $null
$t.Item(1).AppendChild($xml.CreateTextNode($env:LT_BODY)) > $null
$audio = $xml.CreateElement('audio'); $audio.SetAttribute('silent', 'true')
$xml.SelectSingleNode('/toast').AppendChild($audio) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:LT_APPID).Show($toast)
"""
_ENCODED = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")

_last: dict[str, float] = {}
_lock = threading.Lock()
QUIET_SEC = 30.0          # 같은 키워드는 이 시간 안에 다시 알리지 않습니다


def find_keywords(text: str, keywords: list[str]) -> list[str]:
    """띄어쓰기와 대소문자를 무시하고 찾습니다. (음성인식은 띄어쓰기가 들쭉날쭉합니다)"""
    compact = re.sub(r"\s+", "", text or "").lower()
    hits = []
    for kw in keywords:
        k = re.sub(r"\s+", "", kw or "").lower()
        if k and k in compact and kw not in hits:
            hits.append(kw)
    return hits


def toast(title: str, body: str, key: str = "") -> bool:
    """Windows 알림을 띄웁니다. 같은 key 는 QUIET_SEC 동안 한 번만. 띄웠으면 True."""
    if sys.platform != "win32":
        return False
    now = time.monotonic()
    with _lock:
        if key and now - _last.get(key, -1e9) < QUIET_SEC:
            return False
        _last[key] = now
    # 발언 내용은 명령에 끼워 넣지 않고 환경 변수로 넘깁니다.
    env = dict(os.environ, LT_TITLE=title[:120], LT_BODY=body[:240], LT_APPID=_APP_ID)
    threading.Thread(target=_run, args=(env,), daemon=True).start()
    return True


def _run(env: dict) -> None:
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", _ENCODED],
            env=env, timeout=20, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa: BLE001
        log.debug("Windows 알림 실패", exc_info=True)
