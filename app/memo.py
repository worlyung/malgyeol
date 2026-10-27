"""회의 중에 적어 둔 메모('지리 → 질의', '화자 2는 강신원 대표이사')를 끝난 녹취록에 한 번에 반영합니다.

메모를 그대로 찾아 바꾸지 않고 AI에게 녹취록과 함께 보여 줘, 메모가 가리키는 자리만 고치게 합니다.
('지리 → 질의'라도 실제로 '지리'라고 말한 문장은 그대로 둡니다.)
"""
from __future__ import annotations

import io
import json
import re
import shutil

from . import summarize

SYSTEM = (
    "너는 회의 녹취록 교정 담당이다. 사용자가 회의 중에 적은 메모와 회의 자료의 이름·용어만 근거로 "
    "녹취록을 고친다. 그 밖의 내용은 절대 고치지 않는다. 답은 JSON 하나만 출력한다."
)

PROMPT = """회의 중 적은 메모:
<memo>
{memo}
</memo>

회의 관련 자료에서 뽑은 이름·용어 (정확한 표기):
<terms>
{terms}
</terms>

지금 녹취록에 있는 화자 이름: {speakers}

녹취록 (형식: [문장번호] 화자: 내용):
<transcript>
{transcript}
</transcript>

메모를 녹취록에 반영할 변경만 아래 JSON으로 답하라.
{{"renames": [{{"old": "지금 화자 이름 그대로", "new": "바꿀 이름"}}],
 "edits": [{{"id": 문장번호, "text": "고친 문장 전체"}}]}}

규칙:
- renames: 메모가 화자 이름을 바로잡거나 정해 주면 쓴다. old 는 위 '화자 이름' 목록에 있는 것 그대로.
  메모가 이름만 말하고 직함은 안 바꾸라 하면 기존 직함은 살린다 (예: '최정 위원'인데 이름이 최정의면 '최정의 위원').
- edits: 메모가 말한 잘못 받아 적힌 말을, 문맥상 실제로 잘못 적힌 문장에서만 고친다.
  그 말을 실제로 그 뜻으로 쓴 문장은 고치지 않는다. 메모와 관계없는 글자는 한 글자도 바꾸지 않는다.
- 문장 내용 속 이름도 메모가 바로잡으면 같이 고친다.
- 위 이름·용어와 소리가 비슷하게 잘못 받아 적힌 고유명사(사람 이름·직함·기관명·사업명)는
  메모에 없어도 바른 표기로 고친다. 확실할 때만 고치고, 그 밖의 말은 건드리지 않는다.
- 바꿀 게 없으면 빈 목록."""

TERMS_SYSTEM = "너는 회의 자료에서 받아쓰기에 필요한 고유명사를 뽑는 담당이다. 답은 JSON 하나만 출력한다."
TERMS_PROMPT = """아래는 곧 열릴 회의의 관련 자료다. 회의에서 말로 나올 법한 고유명사를 뽑아라.
- 사람 이름(직함이 있으면 '이름 직함' 꼴로, 예: '강신원 대표이사'), 기관·부서명, 사업·행사·사업명, 지명, 법령·조례명, 자주 나오는 전문 용어
- 흔한 일반 낱말, 숫자만 있는 것, 문장은 빼라. 자료에 적힌 표기 그대로.
{{"terms": ["...", "..."]}}

<material>
{material}
</material>"""

MATERIAL_CHARS = 60000   # shortcut: 자료가 이보다 길면 앞부분만 봄, 두꺼운 업무보고서가 잘리면 나눠 뽑기로 바꿀 것


def read_material(name: str, raw: bytes) -> str:
    """올린 자료 파일에서 글자만 꺼냅니다. PDF·MD만 받습니다 (다른 형식은 변환해 넣어야 표가 덜 깨집니다)."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext == "md":
        for enc in ("utf-8-sig", "cp949"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")
    if ext == "pdf":
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(raw)).pages)
    raise ValueError("PDF나 MD 파일만 넣을 수 있습니다. HWP·HWPX·DOCX·PPT·XLS 같은 파일은 "
                     "PDF(2쪽 모아 보기 말고 1쪽씩)나 MD로 저장해 넣어 주세요. 그래야 인식률이 더 좋아집니다.")


def extract_terms(material: str, cfg: dict) -> list[str]:
    answer = summarize.ask(TERMS_PROMPT.format(material=material[:MATERIAL_CHARS]), cfg,
                           TERMS_SYSTEM, max_tokens=4000, timeout=600)
    terms = _parse(answer).get("terms") or []
    out: list[str] = []
    for t in terms:
        t = str(t).strip()
        if 2 <= len(t) <= 40 and t not in out:
            out.append(t)
    return out


def _parse(answer: str) -> dict:
    m = re.search(r"\{.*\}", answer, re.S)
    if not m:
        raise ValueError("AI 답에서 JSON을 찾지 못했습니다.")
    return json.loads(m.group(0))


def apply(store, memo: str, cfg: dict, terms: list[str] | None = None) -> dict:
    """store(TranscriptStore)에 메모를 반영하고 {'edits': n, 'renames': n} 을 돌려줍니다.

    고치기 전 녹취록은 transcript.before-memo.json 으로 남겨 되돌릴 수 있게 합니다.
    """
    memo = memo.strip()
    segs = store.as_list()
    if not (memo or terms) or not segs:
        return {"edits": 0, "renames": 0}
    speakers = sorted({s["speaker"] for s in segs})
    # shortcut: 녹취록 전체를 한 번에 보냄, 몇 시간짜리 회의에서 AI가 잘리면 나눠 보내기로 바꿀 것
    transcript = "\n".join(f"[{s['id']}] {s['speaker']}: {s['text']}" for s in segs)
    answer = summarize.ask(
        PROMPT.format(memo=memo or "(없음)", terms=", ".join(terms or []) or "(없음)", speakers=", ".join(speakers), transcript=transcript),
        cfg, SYSTEM, max_tokens=8000, timeout=600,
    )
    plan = _parse(answer)

    store.save(force=True)
    src = store.dir / "transcript.json"
    if src.exists():
        shutil.copyfile(src, store.dir / "transcript.before-memo.json")

    renames = 0
    for r in plan.get("renames") or []:
        old, new = str(r.get("old", "")).strip(), str(r.get("new", "")).strip()
        if old in speakers and new and new != old:
            renames += store.rename_speaker(old, new) > 0
    edits = 0
    for e in plan.get("edits") or []:
        try:
            seg = store.get(int(e.get("id")))
        except (TypeError, ValueError):
            continue
        text = str(e.get("text", "")).strip()
        if seg and text and text != seg.text:
            store.update(seg.id, text=text)
            edits += 1
    store.save(force=True)
    if memo:
        (store.dir / "memo.txt").write_text(memo, "utf-8")
    return {"edits": edits, "renames": renames}
