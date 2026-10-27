"""맥락으로 다음 화자 정하기 (AI API 사용).

사회자가 "이영희 의원님 나오셔서 제안설명해 주시기 바랍니다."처럼 발언권을 넘기면
그 다음에 이어지는 목소리를 지명된 사람으로 표시합니다.

외부 API 호출을 줄이려고, 지명하는 말투(이름·직함 + "나오셔서", "해 주시기 바랍니다" 등)가
보이는 문장만 AI에게 보냅니다. 전송되는 것은 그 문장과 직전 몇 문장뿐입니다.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .. import summarize

log = logging.getLogger(__name__)

# 제공자별로 빠르고 저렴한 모델을 씁니다. (요약용 모델은 느립니다)
FAST_MODELS = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4.1-mini",
    "gemini": "gemini-3.1-flash-lite",
    "clova": "HCX-GOV-THINK-V1-32B",       # Clova GOV 에서 가장 작은(빠른) 모델
}

TITLES = ("의원", "위원", "위원장", "의장", "부의장", "국장", "과장", "실장", "팀장", "시장", "부시장",
          "군수", "부군수", "구청장", "청장", "단장", "소장", "관장", "원장", "교육감", "교육장", "대표",
          "전문위원", "사무국장", "본부장", "센터장", "이사장", "사장")
_TITLE_RE = re.compile(r"[가-힣]{2,4}\s*(?:" + "|".join(sorted(TITLES, key=len, reverse=True)) + r")")
_REQUEST_RE = re.compile(
    r"(나오셔서|나와\s*주|나오시기|단상|발언대|순서|차례|말씀해|말씀하여|발언해|발언하여|질의해|질의하여|"
    r"답변해|답변하여|설명해|설명하여|보고해|보고하여|토론해|의견|질문해|해\s*주시기\s*바랍니다|"
    r"해\s*주십시오|해\s*주세요|부탁드립니다)"
)

SYSTEM = """당신은 한국 지방의회 회의 녹취에서 발언자를 판별하는 도우미입니다.
음성인식 결과라 이름에 오탈자가 있을 수 있습니다. 반드시 JSON 한 줄만 출력하십시오."""

PROMPT = """등록된 인물: {members}

직전 발언:
{history}

지금 발언:
"{text}"

지금 발언이 특정 인물에게 발언권을 주거나 다음 순서로 지명하는 말이라면, 바로 다음에 말할 사람을 답하십시오.
등록된 인물 중 같은 사람이 있으면 등록된 이름 그대로 쓰십시오.
지명하는 말이 아니거나 누구인지 알 수 없으면 next_speaker 를 null 로 두십시오.
{{"next_speaker": "이름" 또는 null, "title": "직함 또는 빈 문자열"}}"""


def looks_like_designation(text: str, names: list[str]) -> bool:
    """AI에게 물어볼 만한 문장인지 빠르게 거릅니다. (대부분의 문장은 여기서 걸러집니다)"""
    compact = text.replace(" ", "")
    has_person = bool(_TITLE_RE.search(text)) or any(n and n in compact for n in names)
    return has_person and bool(_REQUEST_RE.search(text))


class ContextSpeaker:
    """지명 문장을 한 번에 하나씩 AI에게 물어봅니다. (받아쓰기를 멈추지 않도록 별도 스레드)"""

    def __init__(self, summary_cfg: dict, model: str = "", provider: str = "") -> None:
        self.cfg = dict(summary_cfg)
        if provider:
            self.cfg["provider"] = provider
        provider = (self.cfg.get("provider") or "anthropic").lower()
        self.cfg["model"] = (model if provider in {"codex_cli", "claude_cli"}
                             else model or FAST_MODELS.get(provider, self.cfg.get("model", "")))
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="context-speaker")
        self.error: str | None = None

    def submit(self, text: str, history: list[str], members: list[str], callback) -> None:  # noqa: ANN001
        """다음 화자를 찾으면 callback(name, title) 을 부릅니다."""
        self._pool.submit(self._run, text, history, members, callback)

    def _run(self, text: str, history: list[str], members: list[str], callback) -> None:  # noqa: ANN001
        found = self.ask(text, history, members)
        if not found:
            return
        try:
            callback(*found)
        except Exception:  # noqa: BLE001
            log.exception("맥락 화자 적용 실패")

    def ask(self, text: str, history: list[str], members: list[str]) -> tuple[str, str] | None:
        """지금 문장이 지명이면 (이름, 직함). 아니면 None. (기다려서 답을 받습니다)"""
        prompt = PROMPT.format(
            members=", ".join(members) or "(없음)",
            history="\n".join(f"- {h}" for h in history) or "(없음)",
            text=text,
        )
        try:
            timeout = 90 if self.cfg.get("provider") in {"codex_cli", "claude_cli"} else 20
            answer = summarize.ask(prompt, self.cfg, SYSTEM, max_tokens=200, timeout=timeout)
            self.error = None
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            log.warning("맥락 화자 판별 실패: %s", exc)
            return None
        name, title = parse_answer(answer)
        if not name:
            return None
        # AI가 문장에 없는 이름을 지어내면 쓰지 않습니다.
        known = {m.split()[0] for m in members if m}
        if name.replace(" ", "") not in text.replace(" ", "") and name not in known:
            log.info("맥락 화자 무시(문장에 없는 이름): %s", name)
            return None
        return name, title

    def close(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=True)


class Designation:
    """사회자가 지명한 사람을 뒤따르는 발언에 붙입니다.

    지명 직후에는 사회자가 말을 조금 더 이어갈 수 있으므로, 사회자와 목소리가 다른 첫 발언부터
    지명된 사람으로 보고, 그 목소리가 이어지는 동안만 유지합니다. 목소리가 바뀌면 끝납니다.
    결과는 seg.cands["context"] 에만 적고, 실제 표시는 resolve 가 정합니다.
    """

    def __init__(self, after: int, member_id: str, label: str, caller_emb, caller_end: float,  # noqa: ANN001
                 learn=None) -> None:  # noqa: ANN001
        self.after = after
        self.member_id = member_id
        self.label = label
        self.caller_emb = caller_emb
        self.caller_end = caller_end
        self.last_end = caller_end
        self.started = False
        self.block_emb = None
        self.active = True
        self.learned = 0
        self._learn = learn

    def apply(self, seg, emb) -> bool:  # noqa: ANN001
        """seg 에 지명된 사람을 붙였으면 True."""
        if not self.active or seg.id <= self.after:
            return False
        gap = seg.start - self.last_end
        if not self.started:
            if seg.start - self.caller_end > 180:           # 3분 넘게 안 나오면 지명 취소
                self.active = False
                return False
            if emb is not None and self.caller_emb is not None and float(np.dot(emb, self.caller_emb)) >= 0.55:
                self.caller_end = self.last_end = seg.end     # 아직 사회자가 말하는 중
                return False
            if emb is None and self.caller_emb is not None and gap < 1.5:
                self.last_end = seg.end                       # 사회자의 짧은 덧붙임
                return False
            self.started, self.block_emb = True, emb
        elif emb is not None and self.block_emb is not None:
            if float(np.dot(emb, self.block_emb)) < 0.45:   # 다른 사람이 말하기 시작
                self.active = False
                return False
        elif emb is None and gap > 8.0:
            self.active = False
            return False
        elif self.block_emb is None:
            self.block_emb = emb

        self.last_end = seg.end
        seg.cands["context"] = {"id": self.member_id, "name": self.label, "score": 0.0}
        # 지명으로 확인된 목소리를 성문으로 배워 두면, 다음부터는 목소리만으로도 알아봅니다.
        if self._learn and emb is not None and self.learned < 3 and seg.end - seg.start >= 3.0:
            self._learn(self.member_id, emb)
            self.learned += 1
        return True


def parse_answer(answer: str) -> tuple[str | None, str]:
    m = re.search(r"\{.*\}", answer or "", re.S)
    if not m:
        return None, ""
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, ""
    name = (data.get("next_speaker") or "").strip() if isinstance(data.get("next_speaker"), str) else ""
    title = (data.get("title") or "").strip() if isinstance(data.get("title"), str) else ""
    # "이영희 의원"처럼 이름에 직함을 붙여 오면 떼어 냅니다.
    for t in sorted(TITLES, key=len, reverse=True):
        if name.endswith(t) and len(name) > len(t) + 1:
            name, title = name[: -len(t)].strip(), title or t
            break
    return (name or None), title
