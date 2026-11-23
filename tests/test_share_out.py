"""공유: 메신저용 글(요약·녹취록)과 끌어다 놓을 Word 파일."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import export, main, paths
from app.main import app

SUMMARY = "## 결정 사항\n\n- **안내판**은 금요일까지 제작\n\n---\n\n## 할 일\n1. 견적 받기\n"


class ShareOutTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.sessions = root / "sessions"
        m = self.sessions / "20261123-100000-abcd"
        m.mkdir(parents=True)
        (m / "meta.json").write_text(json.dumps({
            "title": "운영회의: 1차", "info": {"date": "2026-11-23", "place": "회의실", "attendees": "김하늘"},
        }, ensure_ascii=False), encoding="utf-8")
        (m / "transcript.json").write_text(json.dumps({"segments": [{
            "id": 1, "start": 0, "end": 5, "speaker": "김하늘", "text": "안내판은 금요일까지 제작합니다.",
        }]}, ensure_ascii=False), encoding="utf-8")
        self.meeting = m
        self.share_dir = root / "share"
        for p in (patch.object(paths, "SESSIONS_DIR", self.sessions), patch.object(main, "SHARE_DIR", self.share_dir),
                  patch("os.startfile", create=True)):
            p.start()
            self.addCleanup(p.stop)

    def test_plain_summary_strips_markdown(self):
        text = export.plain_summary(SUMMARY)
        self.assertNotIn("**", text)
        self.assertNotIn("---", text)
        self.assertIn("■ 결정 사항", text)
        self.assertIn("• 안내판은 금요일까지 제작", text)

    def test_share_text(self):
        with TestClient(app) as c:
            sid = self.meeting.name
            self.assertEqual(c.get(f"/api/sessions/{sid}/share-text?kind=summary").status_code, 404)
            r = c.get(f"/api/sessions/{sid}/share-text?kind=transcript").json()
            self.assertTrue(r["text"].startswith("[회의록] 운영회의: 1차\n2026-11-23 · 회의실\n참석: 김하늘"))
            self.assertIn("안내판은 금요일까지", r["text"])
            (self.meeting / "summary.md").write_text(SUMMARY, encoding="utf-8")
            r = c.get(f"/api/sessions/{sid}/share-text?kind=summary").json()
            self.assertIn("• 안내판은 금요일까지 제작", r["text"])
            self.assertEqual(c.get(f"/api/sessions/{sid}/share-text?kind=x").status_code, 400)

    def test_share_files(self):
        (self.meeting / "summary.md").write_text(SUMMARY, encoding="utf-8")
        with TestClient(app) as c:
            r = c.post(f"/api/sessions/{self.meeting.name}/share-files").json()
        self.assertEqual(r["files"], ["운영회의_ 1차 녹취록.docx", "운영회의_ 1차 요약.docx"])
        for name in r["files"]:
            self.assertGreater((Path(r["folder"]) / name).stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
