"""로그인된 CLI 선택과 실행 형식을 실제 모델 호출 없이 확인합니다."""

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from app import cli_ai, summarize
from app.speakers.context import ContextSpeaker


class CliAiTests(unittest.TestCase):
    @patch("app.cli_ai.shutil.which", side_effect=lambda name: name)
    @patch("app.cli_ai.logged_in", side_effect=lambda name: name == "codex_cli")
    def test_codex_receives_prompt_and_returns_answer(self, _login, _which):
        def fake_run(args, **kwargs):
            payload = kwargs["input"]
            self.assertTrue(payload.startswith("지침\n\n"))
            self.assertTrue(payload.endswith("\n\n질문"))
            self.assertIn("요청한 결과물만 출력하십시오.", payload)
            self.assertIn("read-only", args)
            answer = Path(args[args.index("--output-last-message") + 1])
            answer.write_text("답변", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch("app.cli_ai.subprocess.run", side_effect=fake_run) as run:
            self.assertEqual(cli_ai.run("codex_cli", "질문", "지침"), "답변")
        self.assertEqual(run.call_count, 1)

    @patch("app.cli_ai.shutil.which", side_effect=lambda name: name)
    @patch("app.cli_ai.logged_in", side_effect=lambda name: name == "claude_cli")
    def test_falls_back_to_claude_when_codex_is_not_logged_in(self, _login, _which):
        result = subprocess.CompletedProcess([], 0, json.dumps({"result": "대체 답변"}), "")
        with patch("app.cli_ai.subprocess.run", return_value=result) as run:
            self.assertEqual(cli_ai.run("codex_cli", "질문", "지침"), "대체 답변")
        self.assertIn("--no-session-persistence", run.call_args.args[0])

    @patch("app.cli_ai.shutil.which", side_effect=lambda name: name)
    @patch("app.cli_ai.logged_in", return_value=True)
    def test_failed_request_is_not_retried(self, _login, _which):
        result = subprocess.CompletedProcess([], 1, "", "오류")
        with patch("app.cli_ai.subprocess.run", return_value=result) as run:
            with self.assertRaises(RuntimeError):
                cli_ai.run("codex_cli", "질문", "지침")
        self.assertEqual(run.call_count, 1)

    @patch("app.cli_ai.run", return_value="분석 결과")
    def test_short_speaker_question_uses_codex_login(self, run):
        answer = summarize.ask("질문", {"provider": "codex_cli"}, "지침")
        self.assertEqual(answer, "분석 결과")
        self.assertEqual(run.call_args.args[:3], ("codex_cli", "질문", "지침"))

    def test_speaker_provider_is_independent_of_summary_provider(self):
        context = ContextSpeaker({"provider": "claude_cli", "model": "summary-model"},
                                 provider="codex_cli")
        try:
            self.assertEqual(context.cfg["provider"], "codex_cli")
            self.assertEqual(context.cfg["model"], "")
        finally:
            context.close()

    @patch("app.summarize.available", return_value=True)
    @patch("app.cli_ai.run", return_value="회의 요약")
    def test_summary_uses_claude_login(self, run, _available):
        pieces = []
        answer = summarize.summarize("[00:00:01] 발언 내용", {"provider": "claude_cli"},
                                     on_text=pieces.append)
        self.assertEqual(answer, "회의 요약")
        self.assertEqual(pieces, ["회의 요약"])
        self.assertEqual(run.call_args.args[0], "claude_cli")


if __name__ == "__main__":
    unittest.main()
