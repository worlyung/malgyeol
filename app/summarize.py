"""녹취록 요약 (선택 기능).

요약은 로그인된 Claude Code 또는 Codex CLI를 기본으로 사용합니다.
API 키 방식의 제공자도 선택할 수 있습니다.

지원: Claude Code / Codex 로그인, Anthropic / OpenAI / Gemini / Clova Studio GOV API
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from . import cli_ai

log = logging.getLogger(__name__)

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-4.1",
    "gemini": "gemini-3.1-flash-lite",
    "clova": "HCX-GOV-THINK-V1-32B",
}

# 한 번에 넣을 수 있는 대략적인 글자 수 (넘으면 나눠 요약 후 합칩니다)
CHUNK_CHARS = 120_000
# Clova GOV 모델은 대부분 128K 토큰이고 한글은 1자에 1~2토큰이라 더 작게 나눕니다.
PROVIDER_CHUNK_CHARS = {"clova": 40_000}

# Clova Studio GOV — Chat Completions 만 /api/v1 입니다. (도구 API 는 /v1 이라 섞으면 404)
CLOVA_URL = "https://api.clovastudio.go.kr/api/v1"
# 모델마다 추론을 켜고 끄는 방법이 다릅니다. (Clova Studio GOV 매뉴얼 '언어 모델 종류')
CLOVA_THINKING_PARAM = {
    "HCX-GOV-THINK-V1-32B": "thinking",                     # 기본 꺼짐
    "K-Exaone-236B": "enable_thinking",                     # 기본 켜짐
    "LLM42-Gemma4": "enable_thinking",                      # 기본 꺼짐
    "NVIDIA-Nemotron-3-Ultra-550B-A55B": "enable_thinking",  # 기본 켜짐
}

SYSTEM = """당신은 한국 지방의회 회의록을 정리하는 전문 속기사입니다.
주어진 것은 자동 음성인식으로 만든 녹취록이라 오탈자와 화자 오인식이 있을 수 있습니다.
사실을 지어내지 말고, 녹취록에 실제로 있는 내용만 쓰십시오.
불확실한 부분은 '(불분명)'으로 표시하십시오."""

PROMPT = """다음은 회의 녹취록입니다. 아래 형식의 한국어 요약문을 작성하십시오.

## 회의 개요
- 회의명:
- 일시:
- 장소:
- 참석 인원:
- 참석자:
(맨 앞 [회의 정보]에 적힌 값은 그대로 쓰고, 빈 칸은 녹취록에서 확인되는 범위로 채우되 확인 안 되면 비워 둘 것.
 녹취록에서 채운 값 뒤에는 '(녹취록 기준)'을 붙일 것)

## 안건별 요약
안건마다 소제목을 달고, 논의 내용을 3~6줄로 정리. 각 항목 끝에 해당 발언의 시각 [hh:mm:ss] 표기.

## 발언자별 주요 발언
발언자마다 핵심 주장·질의 내용을 정리.

## 질의와 답변
질의한 사람 / 답변한 사람 / 쟁점 / 답변 요지.

## 결정 사항과 후속 조치
의결·합의된 내용, 담당 부서에 요구된 자료나 조치. 없으면 '없음'.

---
녹취록:
{transcript}
"""

MERGE_PROMPT = """아래는 같은 회의의 녹취록을 앞에서부터 나눠 요약한 것입니다.
중복을 없애고 시간 순서대로 하나의 회의 요약문으로 합치십시오.
형식은 각 부분 요약과 동일하게 유지하십시오.

{parts}
"""


class SummaryError(RuntimeError):
    pass


def friendly_error(exc: Exception) -> str:
    """사용자가 알아들을 수 있는 오류 문장."""
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
        return ("보안 인증서 문제로 AI 서버에 연결하지 못했습니다. 기관 보안 장비가 연결을 검사하는 경우입니다. "
                "프로그램을 다시 켜도 같으면 정보화 담당자에게 기관 인증서 설치를 문의하세요.")
    if "401" in text or "403" in text or "API key not valid" in text or "invalid_api_key" in text:
        return "API 키가 올바르지 않습니다. 설정 → AI 요약에서 키를 다시 확인하세요."
    if "429" in text:
        return "AI 사용 한도를 넘었습니다. 잠시 뒤 다시 시도하세요. (무료 키는 분당·하루 한도가 있습니다)"
    return text


# --------------------------------------------------------------------- 제공자별 호출
# on_text 를 주면 답을 조금씩 받아(스트리밍) 받는 대로 on_text(조각)를 부릅니다.
# 화면에 요약이 한 글자씩 써지도록 하려는 것입니다.
def _sse(name: str, url: str, headers: dict, body: dict, timeout: float, pick, on_text) -> str:  # noqa: ANN001
    import httpx

    out: list[str] = []
    with httpx.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
        if resp.status_code >= 400:
            resp.read()
            raise SummaryError(f"{name} 오류 {resp.status_code}: {resp.text[:300]}")
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                piece = pick(json.loads(data))
            except (ValueError, KeyError, IndexError, TypeError):
                continue
            if piece:
                out.append(piece)
                on_text(piece)
    return "".join(out).strip()


def _chat_delta(obj: dict) -> str:
    """OpenAI 방식 스트림의 조각 (Clova 도 같습니다). 추론 과정(reasoning)은 빼고 답만."""
    return ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""


def _call_anthropic(prompt: str, cfg: dict, system: str = SYSTEM, max_tokens: int = 16000,
                    timeout: float = 900, on_text=None) -> str:  # noqa: ANN001
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise SummaryError("anthropic 패키지가 설치되어 있지 않습니다. pip install anthropic") from exc

    kwargs = {"api_key": cfg["api_key"], "timeout": timeout}
    if cfg.get("base_url"):
        kwargs["base_url"] = cfg["base_url"]
    client = anthropic.Anthropic(**kwargs)
    model = cfg.get("model") or DEFAULT_MODELS["anthropic"]
    # 녹취록이 길 수 있으므로 스트리밍으로 받아 타임아웃을 피합니다.
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        if on_text:
            for piece in stream.text_stream:
                on_text(piece)
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise SummaryError("모델이 요약 생성을 거부했습니다.")
    return "".join(b.text for b in message.content if b.type == "text").strip()


def _call_openai(prompt: str, cfg: dict, system: str = SYSTEM, max_tokens: int = 16000,
                 timeout: float = 900, on_text=None) -> str:  # noqa: ANN001
    import httpx

    base = (cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    model = cfg.get("model") or DEFAULT_MODELS["openai"]
    if on_text:
        return _sse("OpenAI", f"{base}/chat/completions", {"Authorization": f"Bearer {cfg['api_key']}"},
                    {"model": model, "stream": True, "temperature": 0.2,
                     "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]},
                    timeout, _chat_delta, on_text)
    resp = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise SummaryError(f"OpenAI 오류 {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"].strip()


def _call_gemini(prompt: str, cfg: dict, system: str = SYSTEM, max_tokens: int = 16000,
                 timeout: float = 900, on_text=None) -> str:  # noqa: ANN001
    import httpx

    base = (cfg.get("base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    model = cfg.get("model") or DEFAULT_MODELS["gemini"]
    if on_text:
        return _sse("Gemini", f"{base}/models/{model}:streamGenerateContent?alt=sse",
                    {"x-goog-api-key": cfg["api_key"]},
                    {"systemInstruction": {"parts": [{"text": system}]},
                     "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                     "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens}},
                    timeout,
                    lambda o: "".join(p.get("text", "") for p in o["candidates"][0]["content"]["parts"]),
                    on_text)
    resp = httpx.post(
        f"{base}/models/{model}:generateContent",
        headers={"x-goog-api-key": cfg["api_key"]},
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise SummaryError(f"Gemini 오류 {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError) as exc:
        raise SummaryError(f"Gemini 응답을 해석할 수 없습니다: {str(data)[:300]}") from exc
    return "".join(p.get("text", "") for p in parts).strip()


def _call_clova(prompt: str, cfg: dict, system: str = SYSTEM, max_tokens: int = 16000,
                timeout: float = 900, on_text=None) -> str:  # noqa: ANN001
    import httpx

    base = (cfg.get("base_url") or CLOVA_URL).rstrip("/")
    model = cfg.get("model") or DEFAULT_MODELS["clova"]
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        # 추론 모델은 생각하는 데 쓴 토큰도 이 한도에 들어가므로 넉넉히 둡니다.
        "max_tokens": min(max_tokens, 8192),
    }
    # 요약·화자 판별은 깊은 추론이 필요 없고, 추론을 켜 두면 느리고 답이 잘리기 쉬워 끕니다.
    param = CLOVA_THINKING_PARAM.get(model)
    if param:
        body["chat_template_kwargs"] = {param: False}
    elif model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = "low"          # gpt-oss 는 끌 수 없어 가장 약하게
    if on_text:
        text = _sse("Clova", f"{base}/chat/completions", {"Authorization": f"Bearer {cfg['api_key']}"},
                    {**body, "stream": True}, timeout, _chat_delta, on_text)
        if not text:
            raise SummaryError("Clova 답이 비어 있습니다. 추론형 모델이면 다른 모델을 골라 보세요.")
        return text
    resp = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        json=body,
        timeout=timeout,
    )
    if resp.status_code == 401:
        raise SummaryError("Clova API 키가 올바르지 않습니다. (401)")
    if resp.status_code == 404:
        raise SummaryError(f"Clova 주소 또는 모델 이름을 찾을 수 없습니다: {model} (404)")
    if resp.status_code == 429:
        raise SummaryError("Clova 요청 한도를 넘었습니다. 잠시 후 다시 해 주세요. (429)")
    if resp.status_code >= 400:
        raise SummaryError(f"Clova 오류 {resp.status_code}: {resp.text[:300]}")
    try:
        choice = resp.json()["choices"][0]
    except (KeyError, IndexError, ValueError) as exc:
        raise SummaryError(f"Clova 응답을 해석할 수 없습니다: {resp.text[:300]}") from exc
    text = ((choice.get("message") or {}).get("content") or "").strip()
    if not text and choice.get("finish_reason") == "length":
        raise SummaryError("Clova 답이 길이 한도에 걸려 비어 있습니다. 추론형 모델이면 다른 모델을 골라 보세요.")
    return text


PROVIDERS: dict[str, Callable[[str, dict], str]] = {
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "gemini": _call_gemini,
    "clova": _call_clova,
}


def available(cfg: dict) -> bool:
    """선택한 AI 제공자를 현재 사용할 수 있는지 확인합니다."""
    provider = (cfg.get("provider") or "claude_cli").lower()
    if provider in cli_ai.CLI_PROVIDERS:
        return cli_ai.available(provider)
    return provider in PROVIDERS and bool(cfg.get("api_key"))


def _call(prompt: str, cfg: dict, *, system: str = SYSTEM, max_tokens: int = 16000,
          timeout: float = 900, on_text=None) -> str:  # noqa: ANN001
    provider = (cfg.get("provider") or "claude_cli").lower()
    if provider in cli_ai.CLI_PROVIDERS:
        try:
            answer = cli_ai.run(provider, prompt, system, cfg.get("model") or "", timeout)
        except RuntimeError as exc:
            raise SummaryError(str(exc)) from exc
        if on_text:
            on_text(answer)
        return answer
    if provider not in PROVIDERS:
        raise SummaryError(f"지원하지 않는 제공자입니다: {provider}")
    if not cfg.get("api_key"):
        raise SummaryError("API 키가 설정되어 있지 않습니다.")
    return PROVIDERS[provider](prompt, cfg, system=system, max_tokens=max_tokens,
                               timeout=timeout, on_text=on_text)


# --------------------------------------------------------------------- 공개 함수
def ask(prompt: str, cfg: dict, system: str, max_tokens: int = 300, timeout: float = 30) -> str:
    """요약이 아닌 짧은 질문 한 번. (화자 지정 등) cfg 는 summary 설정 형식입니다."""
    return _call(prompt, cfg, system=system, max_tokens=max_tokens, timeout=timeout)


def summarize(transcript: str, cfg: dict, progress: Callable[[str], None] | None = None,
              on_text: Callable[[str], None] | None = None) -> str:
    """on_text 를 주면 최종 요약을 받는 대로 조금씩 넘겨 줍니다."""
    provider = (cfg.get("provider") or "claude_cli").lower()
    if not available(cfg):
        raise SummaryError("로그인된 AI 도구 또는 API 키가 없습니다. 설정에서 연결을 확인하세요.")
    if not transcript.strip():
        raise SummaryError("요약할 녹취 내용이 없습니다.")

    call = lambda prompt, cfg, **kwargs: _call(prompt, cfg, **kwargs)
    say = progress or (lambda _msg: None)
    chunk_chars = PROVIDER_CHUNK_CHARS.get(provider, CHUNK_CHARS)

    if len(transcript) <= chunk_chars:
        say("요약하는 중…")
        return call(PROMPT.format(transcript=transcript), cfg, on_text=on_text)

    # 아주 긴 회의: 나눠 요약한 뒤 합칩니다.
    lines = transcript.split("\n")
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) > chunk_chars and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))

    parts = []
    for i, chunk in enumerate(chunks, 1):
        say(f"회의가 길어 나눠서 요약하는 중… ({i}/{len(chunks)})")
        parts.append(f"[부분 {i}]\n" + call(PROMPT.format(transcript=chunk), cfg))
    say("부분 요약을 하나로 합치는 중…")
    return call(MERGE_PROMPT.format(parts="\n\n".join(parts)), cfg, on_text=on_text)
