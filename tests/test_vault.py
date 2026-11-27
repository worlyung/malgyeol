"""옵시디언 볼트 저장: 노트 모양, 같은 노트 새로 쓰기, 직접 고친 노트 지키기, 같은 이름 피하기."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, paths, vault


class VaultTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.vault_dir = root / "볼트" / "회의"
        self.vault_dir.mkdir(parents=True)
        for name, value in (("DATA_DIR", root), ("SETTINGS_FILE", root / "settings.json"),
                            ("SECRETS_FILE", root / ".env"), ("SESSIONS_DIR", root / "sessions")):
            p = patch.object(paths, name, value)
            p.start()
            self.addCleanup(p.stop)
        config._cache = config._cache_stamp = None
        self.addCleanup(lambda: setattr(config, "_cache", None))
        config.save({"vault": {"enabled": True, "folder": str(self.vault_dir)}})
        self.sid = self._meeting("20261127-100000-aaaa", "운영회의: 1차")

    def _meeting(self, sid, title):
        d = paths.SESSIONS_DIR / sid
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"id": sid, "title": title, "created": 1795700000,
                                                 "info": {"date": "2026-11-27 14:00", "attendees": "김하늘, 이바다"}},
                                                ensure_ascii=False), "utf-8")
        (d / "transcript.json").write_text(json.dumps({"segments": [
            {"id": 1, "start": 0, "end": 3, "speaker": "김하늘", "text": "안내판은 금요일까지 만듭니다."}]},
            ensure_ascii=False), "utf-8")
        return sid

    def test_note_shape_and_resave(self):
        p = vault.save(self.sid)
        self.assertEqual(p.name, "회의록_운영회의_ 1차_20261127.md")
        text = p.read_text("utf-8")
        for part in ("type: meeting", "created: 2026-11-27", "source: 말결", '  - "이바다"',
                     "# 회의록 — 운영회의: 1차", "## 전체 기록", "**[00:00:00] 김하늘:** 안내판은"):
            self.assertIn(part, text)
        self.assertNotIn("## 요약", text)
        (paths.SESSIONS_DIR / self.sid / "summary.md").write_text("- 안내판 금요일", "utf-8")
        time.sleep(0.05)
        self.assertEqual(vault.save(self.sid), p)                  # 같은 노트를 새로 씀
        self.assertIn("## 요약\n\n- 안내판 금요일", p.read_text("utf-8"))

    def test_keeps_note_edited_in_obsidian(self):
        p = vault.save(self.sid)
        p.write_text("곰곰이 고친 내용", "utf-8")
        later = time.time() + 10
        os.utime(p, (later, later))
        with self.assertRaisesRegex(RuntimeError, "덮어쓰지 않았습니다"):
            vault.save(self.sid)
        self.assertEqual(p.read_text("utf-8"), "곰곰이 고친 내용")

    def test_same_name_gets_number_and_off_means_nothing(self):
        other = self._meeting("20261127-110000-bbbb", "운영회의: 1차")
        a, b = vault.save(self.sid), vault.save(other)
        self.assertNotEqual(a, b)
        self.assertTrue(b.name.endswith("_2.md"))
        config.save({"vault": {"enabled": False}})
        self.assertIsNone(vault.save(self.sid))
        self.assertIsNotNone(vault.save(self.sid, force=True))     # 공유 → 볼트에 저장은 꺼져 있어도 됨


if __name__ == "__main__":
    unittest.main()
