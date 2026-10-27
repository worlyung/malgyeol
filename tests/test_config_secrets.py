"""요약 API 키가 설정 JSON에 남지 않는지 확인합니다."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, paths


class ConfigSecretsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        data = Path(temp.name)
        for name, value in (
            ("DATA_DIR", data),
            ("SETTINGS_FILE", data / "settings.json"),
            ("SECRETS_FILE", data / ".env"),
        ):
            p = patch.object(paths, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(paths, "ensure_dirs", side_effect=lambda: data.mkdir(exist_ok=True))
        p.start()
        self.addCleanup(p.stop)
        config._cache = None
        config._cache_stamp = None
        self.addCleanup(self._clear_cache)

    @staticmethod
    def _clear_cache():
        config._cache = None
        config._cache_stamp = None

    def test_new_key_is_saved_only_in_env(self):
        config.save({"summary": {"api_key": "test-value"}})
        saved = json.loads(paths.SETTINGS_FILE.read_text("utf-8"))
        self.assertNotIn("api_key", saved["summary"])
        self.assertIn("SUMMARY_API_KEY=", paths.SECRETS_FILE.read_text("utf-8"))
        self.assertEqual(config.load()["summary"]["api_key"], "test-value")

    def test_old_settings_key_is_migrated(self):
        paths.SETTINGS_FILE.write_text(
            json.dumps({"summary": {"provider": "gemini", "api_key": "old-test-value"}}), "utf-8")
        loaded = config.load()
        self.assertEqual(loaded["summary"]["api_key"], "old-test-value")
        self.assertNotIn("old-test-value", paths.SETTINGS_FILE.read_text("utf-8"))
        self.assertIn("old-test-value", paths.SECRETS_FILE.read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
