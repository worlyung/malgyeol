"""AI 모델 목록: 제공자에게 못 물으면 기본 목록, 물을 수 있으면 받은 목록."""
import unittest
from unittest.mock import patch

from app import ai_models


class AiModelsTests(unittest.TestCase):
    def test_fallback_without_key(self):
        r = ai_models.models("anthropic", {})
        self.assertFalse(r["live"])
        self.assertIn("claude-sonnet-5-5", [m["id"] for m in r["models"]])
        self.assertEqual([m["id"] for m in ai_models.models("claude_cli", {})["models"]], ["opus", "sonnet", "haiku"])

    def test_live_list_wins_and_errors_fall_back(self):
        with patch.object(ai_models, "_live", return_value=[("gpt-x", "GPT X")]):
            self.assertEqual(ai_models.models("codex_cli", {}), {"models": [{"id": "gpt-x", "label": "GPT X"}], "live": True})
        with patch.object(ai_models, "_live", side_effect=RuntimeError("offline")):
            self.assertFalse(ai_models.models("gemini", {"api_key": "k"})["live"])


if __name__ == "__main__":
    unittest.main()
