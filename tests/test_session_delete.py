"""회의 삭제가 sessions 폴더 밖을 건드리지 않는지 확인합니다."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import paths, session


class DeleteSessionTests(unittest.TestCase):
    def test_deletes_only_selected_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            sessions = data / "sessions"
            selected = sessions / "20261027-120000-abcd"
            other = sessions / "20261027-120001-ef01"
            selected.mkdir(parents=True)
            other.mkdir()
            marker = data / "settings.json"
            marker.write_text("keep", encoding="utf-8")

            with patch.object(paths, "SESSIONS_DIR", sessions):
                session.delete_session(selected.name)

            self.assertFalse(selected.exists())
            self.assertTrue(other.exists())
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_rejects_paths_outside_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            sessions = data / "sessions"
            sessions.mkdir(parents=True)
            marker = data / "settings.json"
            marker.write_text("keep", encoding="utf-8")

            with patch.object(paths, "SESSIONS_DIR", sessions):
                for value in ("..", ".", "../outside", str(data)):
                    with self.subTest(value=value), self.assertRaises(ValueError):
                        session.delete_session(value)

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertTrue(sessions.exists())


if __name__ == "__main__":
    unittest.main()
