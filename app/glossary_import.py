"""CSV 용어사전 가져오기.

"오인식표현, 표준용어, 분류, 세부분류" 형식을 기본으로 읽고,
영문 머리글(from/to, wrong/correct 등)도 알아서 맞춰 봅니다.

한 줄이 하나의 '치환 규칙'이 되고, 표준용어는 '용어 목록'에도 들어갑니다.
  - 치환 규칙: 오인식표현이 나오면 표준용어로 바꿉니다.
  - 용어 목록: 인식 프롬프트 주입과 자모 단위 교정에 쓰입니다.
"""
from __future__ import annotations

import csv
import io
import logging

from .glossary import Glossary, Term

log = logging.getLogger(__name__)

WRONG_KEYS = ("오인식표현", "오인식", "잘못된표기", "wrong", "from", "source", "before")
RIGHT_KEYS = ("표준용어", "정답", "올바른표기", "correct", "to", "target", "after")
CATEGORY_KEYS = ("분류", "category", "type")
SUBCATEGORY_KEYS = ("세부분류", "subcategory", "detail")

# CSV의 분류 -> 이 프로그램의 용어 분류
CATEGORY_MAP = {
    ("인명", "도의원/발언자"): "의원",
    ("인명", "집행부/교육청/의회직원"): "공무원",
    ("인명", None): "공무원",
    ("의회용어", "직책/호칭"): "직책",
    ("의회용어", None): "용어",
    ("기관·부서명", None): "기관",
    ("기관/부서명", None): "기관",
    ("사업·정책명", None): "사업",
    ("사업/정책명", None): "사업",
}


def _pick(row: dict, keys) -> str:
    for k in keys:
        for actual in row:
            if actual and actual.strip().lower().replace(" ", "") == k.lower():
                return (row[actual] or "").strip()
    return ""


def map_category(category: str, subcategory: str) -> str:
    return (
        CATEGORY_MAP.get((category, subcategory))
        or CATEGORY_MAP.get((category, None))
        or "용어"
    )


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp949", "utf-8", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def import_csv(glossary: Glossary, raw: bytes, replace: bool = False) -> dict:
    """CSV를 용어집에 병합합니다. replace=True면 기존 사전을 지우고 새로 넣습니다."""
    text = decode(raw)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV에 머리글이 없습니다.")

    rules: dict[str, dict] = {}
    terms: dict[str, Term] = {}
    skipped = 0

    for row in reader:
        wrong = _pick(row, WRONG_KEYS)
        right = _pick(row, RIGHT_KEYS)
        if not right:
            skipped += 1
            continue
        category = map_category(_pick(row, CATEGORY_KEYS), _pick(row, SUBCATEGORY_KEYS) or None)

        if wrong and wrong != right:
            rules[wrong] = {"from": wrong, "to": right, "note": category}
        if right not in terms:
            terms[right] = Term(text=right, category=category, pinned=False, correct=True)

    with glossary._lock:  # noqa: SLF001 - 같은 패키지
        if replace:
            glossary.terms = []
            glossary.replacements = []

        existing_terms = {t.text for t in glossary.terms}
        added_terms = 0
        for text_value, term in terms.items():
            if text_value not in existing_terms:
                glossary.terms.append(term)
                added_terms += 1

        existing_rules = {r.get("from") for r in glossary.replacements}
        added_rules = 0
        for wrong, rule in rules.items():
            if wrong not in existing_rules:
                glossary.replacements.append(rule)
                added_rules += 1

        glossary.save()

    result = {
        "rows": len(rules) + skipped,
        "added_terms": added_terms,
        "added_rules": added_rules,
        "total_terms": len(glossary.terms),
        "total_rules": len(glossary.replacements),
        "skipped": skipped,
    }
    log.info("용어사전 가져오기: %s", result)
    return result
