"""고유명사 사전(용어집).

확정된 텍스트를 사전으로 고쳐 인식률을 올립니다. (바꾸기 규칙은 모두, 비슷한 말 교정은 MAX_TERM_CHARS 이하 용어만)

1. 바꾸기 규칙: 자주 틀리는 표기를 그대로 바꿉니다. 예) "예산결산특위원회" -> "예산결산특별위원회"
2. 사후 교정: 사전 항목과 '자모 단위로' 비슷한 낱말을 찾아 고칩니다.
   예) "홍길둥" -> "홍길동"처럼 한 글자만 틀린 경우를 잡아냅니다.

예전에는 이름·용어 목록을 Whisper 인식 힌트(프롬프트)로도 넣었지만, 같은 말을 되풀이하거나
없는 문장을 지어내 정확도가 떨어져(회의 90초 실측: 기준과 차이 17.6% → 11.7%) 없앴습니다.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field, asdict
from typing import Callable, Iterable

from . import paths

CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
JONG = " ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"

WORD_RE = re.compile(r"[가-힣A-Za-z0-9]+")


def to_jamo(text: str) -> str:
    """한글을 자모열로 분해. (비교용이라 종성 공백은 버림)"""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            idx = code - 0xAC00
            out.append(CHO[idx // 588])
            out.append(JUNG[(idx % 588) // 28])
            j = JONG[idx % 28]
            if j != " ":
                out.append(j)
        else:
            out.append(ch.lower())
    return "".join(out)


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    """자모 기준 0~1 유사도."""
    ja, jb = to_jamo(a), to_jamo(b)
    if not ja or not jb:
        return 0.0
    return 1.0 - _levenshtein(ja, jb) / max(len(ja), len(jb))


@dataclass
class Term:
    text: str
    category: str = "용어"        # 의원 | 공무원 | 직책 | 용어 | 기관 | 사업 | 지명 | 기타
    pinned: bool = False          # 예전 판의 '항상 프롬프트에 포함' 표시 (지금은 쓰지 않음, 파일 호환용)
    correct: bool = True          # 사후 교정 대상
    note: str = ""
    aliases: list[str] = field(default_factory=list)   # 자주 잘못 인식되는 표기

    def as_dict(self) -> dict:
        return asdict(self)


# 비슷한 말 교정 후보로 쓰는 용어 길이. 이보다 긴 정식명칭은 후보에서 빠지고(바르게 적힌 문장을 지키는 데만 쓰임),
# 틀리게 인식되면 바꾸기 규칙으로만 고쳐집니다.
MAX_TERM_CHARS = 20
# 이보다 짧은 치환 규칙은 앞 글자가 한글이면 건너뜁니다.
# ("심 사"→"심사" 규칙이 "핵심 사업"을 "핵심사업"으로 망가뜨리는 것을 막습니다)
SHORT_RULE_CHARS = 5
HANGUL = re.compile(r"[가-힣]")

# 낱말 뒤에 붙는 조사·호칭. 비교하기 전에 떼어 내고, 바꿀 때 다시 붙입니다.
# ("예산결산특위원회를" -> "예산결산특별위원회를")
JOSA = tuple(sorted({
    "께서는", "께서도", "에서는", "에서도", "에게는", "에게서", "으로는", "으로도", "이라는",
    "까지는", "부터는", "이라도", "에는", "에도", "에만", "에서", "에게", "으로", "이라",
    "께서", "라는", "까지", "부터", "보다", "처럼", "마다", "조차",
    "님은", "님이", "님을", "님과", "님께", "님도",
    "은", "는", "이", "가", "을", "를", "과", "와", "로", "도", "만", "의", "에", "님",
}, key=len, reverse=True))


def split_josa(word: str) -> tuple[str, str]:
    """낱말을 (본체, 조사)로 나눕니다. 본체가 두 글자 미만이면 그대로 둡니다."""
    for josa in JOSA:
        if len(word) - len(josa) >= 2 and word.endswith(josa):
            return word[: -len(josa)], josa
    return word, ""


def required_similarity(jamo_len: int, base: float) -> float:
    """짧은 낱말일수록 더 비슷해야 고칩니다.

    "예산을"과 "예산안"은 자모 하나 차이라 유사도가 0.75나 되지만 서로 다른 말입니다.
    반대로 긴 정식명칭은 몇 글자 틀려도 같은 말일 가능성이 높습니다.
    """
    if jamo_len <= 9:
        return max(base, 0.87)
    if jamo_len <= 14:
        return max(base, 0.80)
    return base


class Glossary:
    def __init__(self, path=None):
        self.path = path or paths.GLOSSARY_FILE
        self._lock = threading.RLock()
        self.terms: list[Term] = []
        self.replacements: list[dict] = []     # [{"from": "...", "to": "..."}]
        self.meeting_note: str = ""            # 예전 판의 프롬프트 머리말 (지금은 쓰지 않음, 파일 호환용)
        self._by_jamo_len: dict[int, list[tuple[str, str]]] | None = None
        self._sorted_replacements: list[dict] = []
        self._exact: set[str] = set()
        self.load()

    # --- 저장/로드 ---
    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._seed()
                return
            try:
                data = json.loads(self.path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            self.terms = [Term(**t) for t in data.get("terms", [])]
            self.replacements = data.get("replacements", [])
            self.meeting_note = data.get("meeting_note", "")
            self._by_jamo_len = None       # 교정 색인 다시 만들기

    def _seed(self) -> None:
        """처음 실행할 때 지방의회에서 흔한 용어를 미리 넣어 둡니다. (지우거나 고쳐도 됩니다)"""
        common = [
            "의사일정", "재석의원", "원안가결", "수정가결", "부결", "의결",
            "도정질문", "시정질문", "5분 자유발언", "신상발언", "제안설명",
            "예산결산특별위원회", "행정사무감사", "인사청문특별위원회", "본회의", "임시회", "정례회",
            "조례안", "동의안", "건의안", "결의안", "추가경정예산",
            "운영위원회", "행정문화복지위원회", "농업경제환경위원회", "건설소방위원회", "교육위원회",
            "의장", "부의장", "위원장", "간사", "도지사", "교육감", "실국원장", "사무처장",
        ]
        self.terms = [Term(text=t, category="용어", pinned=False) for t in common]
        # 자주 잘못 들리는 말 (실제 도의회 영상에서 확인된 예)
        self.replacements = [
            {"from": "재석위원", "to": "재석의원"},
            {"from": "오분 발언", "to": "5분 발언"},
        ]
        self.save()

    def save(self) -> None:
        with self._lock:
            paths.ensure_dirs()
            payload = {
                "terms": [t.as_dict() for t in self.terms],
                "replacements": self.replacements,
                "meeting_note": self.meeting_note,
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(self.path)
            self._by_jamo_len = None       # 사전이 바뀌었으니 색인 다시 만들기

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "terms": [t.as_dict() for t in self.terms],
                "replacements": list(self.replacements),
                "meeting_note": self.meeting_note,
            }

    def replace_all(self, terms: Iterable[dict], replacements: list[dict] | None = None,
                    meeting_note: str | None = None) -> None:
        with self._lock:
            self.terms = [Term(**{k: v for k, v in t.items() if k in Term.__annotations__}) for t in terms]
            if replacements is not None:
                self.replacements = replacements
            if meeting_note is not None:
                self.meeting_note = meeting_note
            self.save()

    def add(self, text: str, category: str = "용어", pinned: bool = False) -> Term:
        with self._lock:
            text = text.strip()
            for t in self.terms:
                if t.text == text:
                    return t
            term = Term(text=text, category=category, pinned=pinned)
            self.terms.append(term)
            self.save()
            return term

    # --- 사후 교정 ---
    def _rebuild_index(self) -> None:
        """교정용 색인. 용어 수가 많아도 빠르게 비교하려고 자모 길이별로 묶어 둡니다."""
        self._by_jamo_len: dict[int, list[tuple[str, str]]] = {}
        for t in self.terms:
            if not t.correct or len(t.text) < 2 or len(t.text) > MAX_TERM_CHARS:
                continue        # 아주 긴 정식명칭은 낱말 단위 비교 대상이 아님
            key = len(to_jamo(t.text))
            self._by_jamo_len.setdefault(key, []).append((t.text, to_jamo(t.text)))
        # 긴 규칙부터 적용해야 짧은 규칙이 긴 이름을 깨뜨리지 않습니다.
        self._sorted_replacements = sorted(
            [r for r in self.replacements if r.get("from")],
            key=lambda r: -len(r["from"]),
        )
        self._exact = {t.text for t in self.terms}

    def _apply_replacements(self, text: str) -> tuple[str, list[dict]]:
        changes: list[dict] = []
        for rep in self._sorted_replacements:
            src, dst = rep["from"], rep.get("to", "")
            if not src or src == dst or src not in text:
                continue
            if len(src.replace(" ", "")) >= SHORT_RULE_CHARS:
                text = text.replace(src, dst)
                changes.append({"from": src, "to": dst, "kind": "replace"})
                continue
            # 짧은 규칙: 앞 글자가 한글이면 다른 낱말의 일부일 수 있으므로 건너뜁니다.
            out, pos, used = [], 0, False
            while True:
                idx = text.find(src, pos)
                if idx < 0:
                    out.append(text[pos:])
                    break
                before = text[idx - 1] if idx > 0 else ""
                out.append(text[pos:idx])
                if before and HANGUL.match(before):
                    out.append(src)
                else:
                    out.append(dst)
                    used = True
                pos = idx + len(src)
            text = "".join(out)
            if used:
                changes.append({"from": src, "to": dst, "kind": "replace"})
        return text, changes

    def correct(self, text: str, threshold: float = 0.72) -> tuple[str, list[dict]]:
        """비슷한 낱말을 사전 표기로 교정. (교정문, 교정내역)"""
        if not text.strip():
            return text, []
        with self._lock:
            if self._by_jamo_len is None:
                self._rebuild_index()
            index = self._by_jamo_len
            exact = self._exact
            terms = [t for t in self.terms if t.correct and 2 <= len(t.text) <= MAX_TERM_CHARS]
            text, changes = self._apply_replacements(text)

        if not terms:
            return text, changes

        # 별칭(자주 틀리는 표기)은 그대로 치환
        for t in terms:
            for alias in t.aliases:
                if alias and alias in text:
                    text = text.replace(alias, t.text)
                    changes.append({"from": alias, "to": t.text, "kind": "alias"})

        tokens = [(m.group(), m.start(), m.end()) for m in WORD_RE.finditer(text)]
        if not tokens:
            return text, changes

        # 낱말 1~2개를 묶어 후보로 삼는다
        candidates: list[tuple[str, int, int]] = []
        for i, (w, s, e) in enumerate(tokens):
            candidates.append((w, s, e))
            if i + 1 < len(tokens):
                s2, e2 = tokens[i][1], tokens[i + 1][2]
                candidates.append((text[s2:e2], s2, e2))

        edits: list[tuple[int, int, str, str]] = []
        used: list[tuple[int, int]] = []
        for cand, start, end in candidates:
            if cand in exact or len(cand) < 2:
                continue
            stem, josa = split_josa(cand)
            if stem in exact or len(stem) < 2:
                continue
            best, best_score = None, 0.0
            cand_jamo = to_jamo(stem)
            cand_len = len(cand_jamo)
            # 자모 길이가 비슷한 용어만 꺼내 비교합니다 (사전이 1천 개가 넘어도 빠릅니다)
            for delta in range(-3, 4):
                for term_text, term_jamo in index.get(cand_len + delta, ()):
                    score = 1.0 - _levenshtein(cand_jamo, term_jamo) / max(cand_len, len(term_jamo))
                    if score > best_score:
                        best, best_score = term_text, score
            # 이미 올바른 말에 못 떼어 낸 꼬리가 붙은 경우(예: "방청석에" -> "방청석")는 손대지 않습니다.
            if best and stem.startswith(best):
                continue
            if best and best != stem and best_score >= required_similarity(cand_len, threshold):
                if any(not (end <= s or start >= e) for s, e in used):
                    continue
                used.append((start, end))
                edits.append((start, end, cand, best + josa))

        for start, end, cand, best in sorted(edits, key=lambda x: -x[0]):
            text = text[:start] + best + text[end:]
            changes.append({"from": cand, "to": best, "kind": "fuzzy"})

        return text, changes
