"""발언 속 '이름'으로 화자를 알아보기 (AI 없이 규칙으로).

충청남도의회 제371회 임시회 회의록 8건(본회의·상임위·예결위·인사청문, 발언 3,140개)을 분석해 만든 규칙입니다.
의원이 발언하기 직전 사회자 발언의 87%에 그 의원 이름이 나왔고, 거의 모두 아래 꼴이었습니다.

  지명 (사회자)
    "김철수 위원님 질의해 주시기 바랍니다."            ← 가장 많음 (약 절반)
    "김철수 위원님 질의하여 주시기 바랍니다." / "질의해 주십시오." / "질의하시기 바랍니다." / "질의하십시오."
    "최지은 부위원장님 추가 질의해 주시기 바랍니다."   "김철수 위원님 먼저 질의해 주시기 바랍니다."
    "이훈 의원님 나오셔서 발언하여 주시기 바랍니다."   (본회의 5분 발언·도정질문)
    "박민수 위원님(은) 나오셔서 제안설명해 주시기 바랍니다."
    "김철수 위원님."  "김철수 위원님 질의하시겠습니까?"  (이름만 짧게 부르기)
  자기소개 (발언자 첫마디)
    "천안 출신 박민수 의원입니다."  "교육위원회 최지은 의원입니다."  "안녕하십니까? 정다운 위원입니다."
    "서산 김철수입니다."  "김철수입니다."              (직함 없이 이름만 — 등록된 명부와 맞을 때만)
    "행정국장 최병묵입니다."                           (집행부는 직함이 앞)
  지명이 아닌 것
    "김철수 위원님 수고하셨습니다."  "앞서 김철수 위원님께서 질의하신 부분에 답변해 주시기 바랍니다."
    "경제통상국장님께 질의하실지 말씀해 주시기 바랍니다."  (님께 = 그 사람에게 묻는 말)
  명부가 있으면 의원·위원 직함 앞의 이름은 명부와 맞을 때만 씁니다. (모든 의원이 명부에 있으므로)

주의: 행정부지사가 간부를 소개할 때 "이승열 기획조정실장 직무대리입니다."처럼 남을 소개하는 말도
'이름 + 직함 + 입니다' 꼴입니다. 그래서 의원·위원이 아닌 직함은 '직함 + 이름' 순서이거나
"저는" 같은 말이 붙은 경우만 자기소개로 봅니다.

음성인식은 띄어쓰기와 이름을 자주 틀리므로(홍길동 → 홍길둥), 등록된 명부가 있으면 이름 자리의 글자를
명부와 자모로 비교해 가장 가까운 사람을 고릅니다. 명부에 없는 사람은 들린 대로 씁니다.
"""
from __future__ import annotations

import re

from ..glossary import similarity

# 의회 쪽 직함 (자기를 이렇게 소개합니다)
COUNCIL = ("부위원장", "위원장", "부의장", "의장", "의원", "위원")
# 집행부·사무처 직함 (간부 소개 때 남을 이렇게 부르기도 합니다)
OFFICIAL = ("행정부지사", "정무부지사", "부지사", "도지사", "교육감", "부교육감", "수석전문위원", "전문위원",
            "기획조정실장", "실장", "국장", "본부장", "과장", "원장", "소장", "청장", "관장", "단장", "팀장",
            "담당관", "정책기획관", "기획관", "사무처장", "처장", "대변인", "비서실장", "직무대리", "후보자")
_TITLES = sorted(COUNCIL + OFFICIAL, key=len, reverse=True)
_T = "|".join(map(re.escape, _TITLES))
_C = "|".join(map(re.escape, sorted(COUNCIL, key=len, reverse=True)))

# 말을 시키는 끝맺음 ("해 주시기 바랍니다", "하시기 바랍니다", "하십시오" …)
_END = (r"(?:(?:해|하여)\s*주\s*시기\s*바(?:랍니다|라겠습니다)|(?:해|하여)\s*주\s*십시오|해\s*주\s*세요"
        r"|하\s*시기\s*바랍니다|하\s*십시오|하\s*시겠습니까|부탁\s*드립니다)")
# 시키는 일
_ACT = r"(?:질의|질문|답변|설명|검토|보고|발언|말씀|인사|토론|의견|선서)"
# 남의 말을 받는 표현 ("○○ 위원님께서 질의하신 부분에 답변해 주시기 바랍니다" 는 그 위원 지명이 아님)
_NOT_CALL = r"(?!하신|하셨|주신|주셨|에\s*대해|관련)"

# 지명: 이름 + 직함님 + (은/께서는) + (먼저/추가로) + [나오셔서 …] 또는 [… 질의 …] + 끝맺음
_CALL = re.compile(
    rf"({_T})\s*님(?!들)(?!\s*께(?!서))\s*(?:은|는|께서는?)?[,\s]*(?:먼저\s*|추가로?\s*|보충\s*)?"
    r"(?:(?:나오셔서|나와서|단상으로|발언대로)[^.?!]{0,30}?"
    rf"|(?:{_NOT_CALL}[^.?!]){{0,12}}?{_ACT}\s*(?:을|를)?\s*)"
    rf"{_END}"
)
# 이름만 짧게 부르기: "김철수 위원님." (명부에 있는 사람일 때만)
_CALL_SHORT = re.compile(rf"({_C})\s*님\s*[.?!]?\s*$")

# 자기소개
_SELF_TITLE = re.compile(rf"({_C})\s*입니다")                 # "… 박민수 의원입니다"
_SELF_OFFICIAL = re.compile(rf"({_T})\s*([가-힣]{{2,4}})\s*입니다")   # "행정국장 최병묵입니다"
_SELF_I_AM = re.compile(rf"(?:저는|제가)\s*([가-힣]{{2,4}})\s*({_T})\s*입니다")   # "저는 홍길동 국장입니다"
_SELF_BARE = re.compile(r"입니다")                             # "서산 김철수입니다" (명부에 있을 때만)
# 이 말이 앞에 오면 남을 소개하는 말입니다 ("다음 질의자는 ○○ 위원입니다")
_INTRODUCES_OTHER = re.compile(r"(?:다음|순서|차례|질의자|발언자|옆|소개)")

_STOP = {"여러분", "위원님", "의원님", "존경하는", "그러면", "다음은", "이상으로", "오늘은", "먼저", "다시",
         "본위원", "본의원", "이상", "감사", "질의", "답변", "말씀", "부탁", "그렇", "알겠", "맞습", "아닙"}

SELF_HEAD = 60            # 자기소개는 말의 첫 60글자 안에서만 찾습니다
NAME_MATCH = 0.72         # 직함과 함께 들린 이름: 이 정도 비슷하면 명부의 그 사람
BARE_MATCH = 0.86         # 직함 없이 "○○○입니다" 만 들린 이름: 더 엄격하게


def _compact_len(s: str) -> int:
    return len(re.sub(r"\s", "", s))


# 이름 자리에 와도 이름이 아닌 말 ("질의하실 위원님 계시면…", "해당 과장님…")
_NOT_NAME = re.compile(r"(?:하실|하신|계신|계시|여러|모든|다른|해당|담당|관계|각|질의|답변|말씀|존경|될|것|이렇게|그렇게|저렇게|어떻게|이렇|그렇)")


# 사람 이름처럼 생겼는지 (명부에 없는 이름을 들린 대로 쓸 때만 봅니다)
# "김철수 국회의원 공약사항입니다" 의 '공약사항' 처럼 직함 뒤에 온 낱말을 이름으로 잡지 않도록 합니다.
_SURNAMES = set(
    "김이박최정강조윤장임한오서신권황안송류유전홍고문양손배백허남심노하곽성차주우구민나진지엄채원천방공"
    "현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예봉경사부가복태목형피두감음빈동온호범좌"
    "변승하진갈엽")
_COMPOUND_SURNAMES = ("남궁", "황보", "제갈", "선우", "독고", "사공", "서문", "동방")
# 이름 끝에 잘 오지 않는, 흔한 회의 낱말 (사람 이름과 겹치는 '지원·기준·정도' 같은 말은 넣지 않습니다)
_NOUN_TAILS = ("사항", "계획", "사업", "예산", "문제", "내용", "부분", "결과", "안건", "현황", "자료", "정책",
               "방안", "대책", "제도", "조례", "의견", "질문", "기관", "부서", "시설", "관련", "공약", "추진",
               "검토", "확인", "때문", "경우", "생각", "입장", "상황", "기간", "규모", "금액", "비용", "효과",
               "목적", "필요", "대상", "과제", "분야", "업무", "역할", "원칙", "절차", "방식", "방법", "요청",
               "보고", "설명", "발표", "회의", "위원회", "의회", "도청", "지역", "사회", "행정", "대표", "후보")


def looks_like_name(s: str) -> bool:
    """한국 사람 이름처럼 생겼으면 True. (2~4글자 한글, 성씨로 시작, 흔한 낱말로 끝나지 않음)"""
    if not re.fullmatch(r"[가-힣]{2,4}", s or ""):
        return False
    if s.startswith(_COMPOUND_SURNAMES):
        return len(s) >= 3
    if len(s) == 4 or s[0] not in _SURNAMES:          # 네 글자는 두 글자 성씨(남궁·황보 …)일 때만
        return False
    return not (len(s) >= 3 and s.endswith(_NOUN_TAILS)) and s[1:] not in _NOUN_TAILS


def _roster_best(chunk: str, names, strict: bool, exact: bool = False) -> str | None:
    """chunk 끝부분(2~4글자)과 가장 가까운 명부 이름."""
    best, best_s = None, 0.0
    for k in (2, 3, 4):
        if len(chunk) < k or chunk[-k:] in _STOP:
            continue
        c = chunk[-k:]
        for n in names:
            s = similarity(c, n)
            # 같은 점수면 길이가 같은 쪽 ("은이훈" 보다 "이훈")
            if s > best_s or (s == best_s and len(c) == len(n)):
                best, best_s = n, s
    need = 1.0 if exact else (BARE_MATCH if strict else NAME_MATCH)
    return best if best and best_s >= need else None


def _guess(text: str, end: int, names, strict: bool, title: str = "", skip: bool = True) -> str | None:
    """text[:end] 끝에 붙은 이름. 명부(names)가 있으면 그중 가장 가까운 사람의 이름.

    집행부는 "최병묵 행정국장님"처럼 이름과 직함 사이에 부서가 붙으므로, 직함에 붙은 낱말이
    이름이 아니면 그 앞 낱말을 이름으로 봅니다. (skip=False 면 건너뛰지 않음)
    명부는 의원 명부이므로, 집행부 직함(국장·과장 …)과 함께 들린 이름은 명부와 똑같을 때만 명부 이름으로 봅니다.
    ("김재한 수석전문위원"을 비슷한 의원 '김재훈'로 바꾸지 않도록)
    """
    words = re.findall(r"[가-힣]+", text[:end])
    if not words:
        return None
    glued = not text[:end].endswith((" ", ",")) and text[:end][-1:].isalpha()  # 직함과 붙어 있는 낱말
    if names:
        # 직함 바로 앞 낱말, 그 앞 낱말까지 붙여서 명부와 비교 ("다음은김철수", "김 영수")
        exact = bool(title) and title not in COUNCIL
        tail = "".join(words[-2:] if skip else words[-1:])[-6:]
        got = _roster_best(tail, names, strict, exact)
        if not got and skip and glued and len(words) >= 2:  # "홍길동 행정국장님" 의 '행정' 건너뛰기
            got = _roster_best(words[-2][-6:], names, strict, exact)
        if got:
            return got
    if strict or (names and title in COUNCIL):
        # 의원·위원은 명부에 다 있으므로, 명부에 없는 이름이면 잘못 들은 것으로 봅니다
        return None
    # 명부에 없는 사람: 띄어 쓴 낱말 하나가 이름 (붙여 쓴 긴 덩어리는 어디까지가 이름인지 몰라 건너뜁니다)
    cand = words[-1]
    if glued and not (2 <= len(cand) <= 4 and cand not in _STOP):
        cand = ""
    if (glued and len(words) >= 2 and 2 <= len(words[-2]) <= 4
            and any(cand.endswith(d) for d in ("행정", "기획", "교육", "건설", "복지", "경제", "안전", "문화"))):
        cand = words[-2]                                    # "최병묵 행정국장님"
    if not (2 <= len(cand) <= 4) or cand in _STOP or _NOT_NAME.search(cand):
        return None
    if any(cand.endswith(t) for t in _TITLES) or not looks_like_name(cand):
        return None
    return cand


def find_self_intro(text: str, names=None) -> tuple[str, str] | None:  # noqa: ANN001
    """자기소개면 (이름, 직함). names 는 등록된 사람 이름 목록(있으면 더 잘 맞힙니다)."""
    text = text or ""
    # 1) "… 박민수 의원입니다" / "안녕하십니까? 정다운 위원입니다"
    for m in _SELF_TITLE.finditer(text):
        if _compact_len(text[:m.start()]) > SELF_HEAD:      # 자기소개는 말의 첫머리에 옵니다
            break
        before = text[max(0, m.start() - 16):m.start()]
        if _INTRODUCES_OTHER.search(before):
            continue
        name = _guess(text, m.start(), names, strict=False, title=m.group(1))
        if name:
            return name, m.group(1)
    # 2) "저는 홍길동 국장입니다"
    m = _SELF_I_AM.search(text)
    if m:
        name = _guess(text, m.start(2), names, strict=False, title=m.group(2))
        if name:
            return name, m.group(2)
    # 3) "행정국장 최병묵입니다" (직함이 앞)
    m = _SELF_OFFICIAL.search(text)
    if (m and _compact_len(text[:m.start()]) <= SELF_HEAD and m.group(2) not in _STOP
            and not _NOT_NAME.search(m.group(2)) and not any(m.group(2).endswith(t) for t in _TITLES)
            and looks_like_name(m.group(2))):
        return m.group(2), m.group(1)
    # 4) "서산 김철수입니다" — 직함 없이 이름만: 첫마디 근처이고 명부와 꼭 맞을 때만
    if names:
        for m in _SELF_BARE.finditer(text):
            if _compact_len(text[:m.start()]) > 24:
                break
            if _INTRODUCES_OTHER.search(text[max(0, m.start() - 16):m.start()]):
                continue
            name = _guess(text, m.start(), names, strict=True, skip=False)
            if name:
                return name, ""
    return None


def find_call(text: str, names=None) -> tuple[str, str] | None:  # noqa: ANN001
    """사회자가 다음 사람을 지명하면 (이름, 직함). 여러 명이면 마지막 사람."""
    text = text or ""
    found = None
    for m in _CALL.finditer(text):
        name = _guess(text, m.start(), names, strict=False, title=m.group(1))
        if name:
            found = (name, m.group(1))
    if found:
        return found
    # "김철수 위원님." 처럼 이름만 부르고 끝나는 짧은 말
    if names and len(re.sub(r"[\s.?!,]", "", text)) <= 12:
        m = _CALL_SHORT.search(text)
        if m:
            name = _guess(text, m.start(), names, strict=True, title=m.group(1))
            if name:
                return name, m.group(1)
    return None


_END_RE = re.compile(_END)
_NAME_TAIL = re.compile(rf"({_T})\s*님(?!들)\s*(?:은|는|께서는?)?[,\s]*$")


def find_call_after(prev_text: str, text: str, names=None) -> tuple[str, str] | None:  # noqa: ANN001
    """음성인식이 "김철수 위원님" / "질의해 주시기 바랍니다" 를 두 문장으로 나눴을 때의 지명.

    지금 문장이 시키는 말로 끝나고, 바로 앞 문장(같은 사람)이 '이름 + 직함님'으로 끝날 때만 봅니다.
    """
    if not prev_text or not _NAME_TAIL.search(prev_text) or not _END_RE.search(text or ""):
        return None
    if find_call(prev_text, names):           # 앞 문장만으로 이미 지명이었으면 따로 볼 필요 없음
        return None
    return find_call(prev_text.strip()[-30:] + " " + text, names)


def match_member(name: str, registry, threshold: float = NAME_MATCH):  # noqa: ANN001, ANN201
    """등록된 사람 중 이름이 같거나 가장 비슷한 사람. 없으면 None.

    음성인식이 한두 소리를 틀려도(홍길동 → 홍길둥, 이영희 → 이영히) 같은 사람으로 봅니다.
    """
    best, best_s = None, 0.0
    for m in registry.list():
        s = similarity(name, m["name"])
        if s > best_s:
            best, best_s = m, s
    if best and (best_s >= threshold or best["name"] == name):
        return registry.get(best["id"])
    return None


def resolve_person(name: str, title: str, registry):  # noqa: ANN001, ANN201
    """이름을 등록된 사람으로 맞추고, 없으면 새로 등록합니다. (명부의 이름이 우선)"""
    m = match_member(name, registry)
    if m:
        return m
    return registry.add(name, title)
