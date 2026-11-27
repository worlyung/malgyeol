"""옵시디언 볼트(또는 아무 마크다운 폴더)에 회의록을 저장합니다.

회의가 끝나거나 요약·메모 반영·화자 다시 나누기가 끝나면 같은 노트를 새로 씁니다.
사용자가 옵시디언에서 노트를 직접 고쳤으면 덮어쓰지 않습니다. (말결이 마지막에 쓴 뒤로 파일이 바뀌었는지로 판단)
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from . import config, export, paths, session as session_mod

log = logging.getLogger(__name__)


def _safe(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]', "_", "".join(c for c in (text or "") if c >= " ")).strip(" .")[:80]


def _date(meta: dict) -> str:
    """회의 정보의 일시(앞의 날짜만) → 없으면 녹음한 날."""
    m = re.search(r"(20\d{2})[-./ ]?(\d{1,2})[-./ ]?(\d{1,2})", (meta.get("info") or {}).get("date", ""))
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return time.strftime("%Y-%m-%d", time.localtime(meta.get("created") or time.time()))


def render(meta: dict, segments: list[dict], summary: str = "") -> str:
    title = meta.get("title") or meta.get("id") or "회의"
    info = meta.get("info") or {}
    people = [p.strip() for p in re.split(r"[,·]", info.get("attendees", "")) if p.strip()]
    fm = ["---", "type: meeting", f"created: {_date(meta)}", "source: 말결", f"session: {meta.get('id', '')}"]
    if meta.get("folder"):
        fm.append(f'folder: "{meta["folder"]}"')
    if people:
        fm += ["participants:"] + [f'  - "{p}"' for p in people]
    fm += ["tags:", "  - 회의", "  - 회의록", "---", ""]
    body = [f"# 회의록 — {title}", ""]
    head = export.info_lines(meta)
    if head:
        body += [f"- **{k}**: {v}" for k, v in head] + [""]
    if summary.strip():
        body += ["## 요약", "", summary.strip(), ""]
    body += ["## 전체 기록", ""]
    for s in segments:                     # 문장마다 빈 줄 (옵시디언 밖의 마크다운 보기에서도 줄이 붙지 않게)
        body += [f"**[{export.hms(s['start'])}] {s.get('speaker') or '화자 미상'}:** {s['text']}", ""]
    return "\n".join(fm + body).rstrip() + "\n"


def save(session_id: str, force: bool = False) -> Path | None:
    """설정한 폴더에 회의록 노트를 씁니다. 저장 안 함 설정이면(force 아님) None."""
    vcfg = config.load().get("vault") or {}
    if not force and not vcfg.get("enabled"):
        return None
    folder = Path(vcfg.get("folder") or "")
    if not str(folder).strip() or not folder.is_dir():
        raise RuntimeError("설정 → 노트 저장에서 저장할 폴더를 먼저 고르세요.")
    meta = session_mod.read_meta(session_id)
    _meta, store = session_mod.load_session(session_id)
    segments = store.as_list()
    if not segments:
        return None
    sfile = paths.SESSIONS_DIR / session_id / "summary.md"
    text = render({**meta, "id": session_id}, segments, sfile.read_text("utf-8") if sfile.exists() else "")

    target = Path(meta["vault_file"]) if meta.get("vault_file") else None
    if target and target.parent != folder:
        target = None                      # 폴더를 바꿨으면 새 폴더에 새로 씁니다
    if target and target.exists():
        if target.stat().st_mtime > float(meta.get("vault_saved_at") or 0) + 2:
            log.info("볼트 노트를 직접 고쳐서 덮어쓰지 않았습니다: %s", target)
            raise RuntimeError(f"'{target.name}'을 직접 고쳐서 덮어쓰지 않았습니다.")
    if not target:
        base = f"회의록_{_safe(meta.get('title') or session_id)}_{_date(meta).replace('-', '')}"
        target, n = folder / f"{base}.md", 2
        while target.exists():             # 같은 이름의 다른 회의록은 건드리지 않습니다
            target, n = folder / f"{base}_{n}.md", n + 1
    target.write_text(text, "utf-8")
    session_mod.save_meta(session_id, {"vault_file": str(target), "vault_saved_at": target.stat().st_mtime})
    return target
