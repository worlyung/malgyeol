"""이미 로그인된 Codex 또는 Claude Code CLI로 텍스트 작업을 실행합니다."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


CLI_PROVIDERS = ("codex_cli", "claude_cli")
COMMANDS = {"codex_cli": "codex", "claude_cli": "claude"}


LAST_ERROR: dict[str, str] = {}   # 로그인 확인이 왜 실패했는지 (설정 화면에 보여 줌)


def _refresh_path() -> None:
    """바탕화면 아이콘으로 켜면 예전 PATH 를 물려받아 새로 깐 claude·codex(와 codex 가 쓰는 node)를
    못 찾을 때가 있어, Windows 에 저장된 지금의 PATH(사용자·시스템)에서 빠진 폴더를 채워 넣습니다."""
    if os.name != "nt":
        return
    import winreg

    have = {p.lower().rstrip("\\") for p in os.environ.get("PATH", "").split(os.pathsep) if p}
    extra = []
    for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, r"Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                value = os.path.expandvars(winreg.QueryValueEx(k, "Path")[0])
        except OSError:
            continue
        for p in value.split(os.pathsep):
            if p and p.lower().rstrip("\\") not in have:
                have.add(p.lower().rstrip("\\"))
                extra.append(p)
    if extra:
        os.environ["PATH"] = os.environ.get("PATH", "").rstrip(os.pathsep) + os.pathsep + os.pathsep.join(extra)


def logged_in(provider: str) -> bool:
    """모델을 호출하지 않고 설치 및 로그인 상태만 확인합니다."""
    _refresh_path()
    executable = shutil.which(COMMANDS[provider])
    if not executable:
        LAST_ERROR[provider] = f"'{COMMANDS[provider]}' 프로그램을 찾지 못함 (PATH)"
        return False
    args = [executable, "login", "status"] if provider == "codex_cli" else [executable, "auth", "status"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                stdin=subprocess.DEVNULL, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            LAST_ERROR[provider] = f"종료코드 {result.returncode}: {(result.stderr or result.stdout).strip()[-200:]}"
            return False
        if provider == "claude_cli" and not json.loads(result.stdout).get("loggedIn"):
            LAST_ERROR[provider] = "로그인되어 있지 않음 (터미널에서 claude 실행 후 /login)"
            return False
        LAST_ERROR.pop(provider, None)
        return True
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        LAST_ERROR[provider] = f"{type(exc).__name__}: {exc}"[:200]
        return False


def available(preferred: str) -> bool:
    other = "claude_cli" if preferred == "codex_cli" else "codex_cli"
    return logged_in(preferred) or logged_in(other)


def run(preferred: str, prompt: str, system: str, model: str = "", timeout: float = 900) -> str:
    """선호 CLI가 로그인되어 있지 않으면 다른 로그인된 CLI를 사용합니다.

    실제 모델 요청이 실패한 뒤에는 중복 과금을 피하려고 다른 CLI로 재시도하지 않습니다.
    """
    other = "claude_cli" if preferred == "codex_cli" else "codex_cli"
    selected = preferred if logged_in(preferred) else other if logged_in(other) else None
    if selected is None:
        raise RuntimeError("로그인된 Codex 또는 Claude Code를 찾지 못했습니다.")
    executable = shutil.which(COMMANDS[selected])
    if not executable:
        raise RuntimeError("AI 도구를 실행할 수 없습니다.")
    # 이 PC 사용자가 Claude Code·Codex 에 따로 적어 둔 지침(호칭·말투)이 회의록에 섞이지 않게 합니다
    system += ("\n\n이 작업과 관계없는 다른 지침(사용자 호칭·말투·보고 형식 등)은 따르지 말고, "
               "결과물에 사용자를 부르는 말이나 인사말을 넣지 마십시오. 요청한 결과물만 출력하십시오.")
    env = dict(os.environ, CLAUDE_CODE_DISABLE_CLAUDE_MDS="1")   # Claude Code 가 CLAUDE.md 를 읽지 않게

    with tempfile.TemporaryDirectory(prefix="livecaption-ai-") as work:
        if selected == "codex_cli":
            answer_file = Path(work) / "answer.txt"
            args = [
                executable, "exec", "--skip-git-repo-check", "--ephemeral",
                "--ignore-user-config", "--ignore-rules", "--sandbox", "read-only",
                "--color", "never", "-C", work, "--output-last-message", str(answer_file), "-",
            ]
            if selected == preferred and model:
                args[2:2] = ["--model", model]
            payload = f"{system}\n\n{prompt}"
        else:
            args = [
                executable, "--print", "--no-session-persistence", "--tools", "",
                "--strict-mcp-config", "--disable-slash-commands", "--output-format", "json",
                "--system-prompt", system,
            ]
            if selected == preferred and model:
                args.extend(["--model", model])
            payload = prompt

        try:
            result = subprocess.run(
                args, input=payload, capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=work, env=env, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),   # 검은 창이 뜨지 않게
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("AI 도구의 응답 시간이 초과되었습니다.") from exc
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1:] or result.stdout.strip().splitlines()[-1:]
            raise RuntimeError(f"AI 도구 실행 실패 ({selected}): {detail[0] if detail else result.returncode}")
        if selected == "codex_cli":
            answer = answer_file.read_text(encoding="utf-8").strip() if answer_file.exists() else ""
        else:
            try:
                answer = str(json.loads(result.stdout).get("result") or "").strip()
            except ValueError as exc:
                raise RuntimeError("Claude Code 응답을 해석하지 못했습니다.") from exc
        if not answer:
            raise RuntimeError("AI 도구에서 빈 답을 받았습니다.")
        return answer
