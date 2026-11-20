"""외부 AI용 프롬프트가 가상 회의만 읽고 전체 녹취를 보존하는지 확인합니다."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import paths
from app.main import app, state


class SharePromptTests(unittest.TestCase):
    def test_prompt_without_ai_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            meeting = sessions / "example"
            meeting.mkdir(parents=True)
            (meeting / "meta.json").write_text(json.dumps({
                "title": "문화예술 행사 운영회의",
                "info": {"date": "2026-10-30", "place": "가상 회의실", "attendees": "김하늘"},
            }, ensure_ascii=False), encoding="utf-8")
            (meeting / "transcript.json").write_text(json.dumps({"segments": [{
                "id": 1, "start": 0, "end": 5, "speaker": "김하늘", "text": "안내판은 금요일까지 제작합니다.",
            }]}, ensure_ascii=False), encoding="utf-8")
            with patch.object(paths, "SESSIONS_DIR", sessions), TestClient(app) as client:
                response = client.get("/api/sessions/example/summary-prompt")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
                for phrase in ("회의명: 문화예술 행사 운영회의", "일시: 2026-10-30",
                               "장소: 가상 회의실", "[00:00:00] 김하늘:", "안내판은 금요일까지"):
                    self.assertIn(phrase, response.text)
                self.assertEqual(client.get("/api/sessions/missing/summary-prompt").status_code, 404)

                state.session = SimpleNamespace(id="example", state="running")
                try:
                    self.assertEqual(client.get("/api/sessions/example/summary-prompt").status_code, 400)
                finally:
                    state.session = None

    def test_empty_transcript_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            (sessions / "empty").mkdir(parents=True)
            with patch.object(paths, "SESSIONS_DIR", sessions), TestClient(app) as client:
                self.assertEqual(client.get("/api/sessions/empty/summary-prompt").status_code, 400)


if __name__ == "__main__":
    unittest.main()
