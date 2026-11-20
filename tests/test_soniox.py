"""Soniox 받아쓰기: 키 보관, 엔진 고르기, 서버 응답 읽기, 미리보기 전달."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app import asr, config, paths
from app.asr.base import Word
from app.asr.online import NativeStreamingASR
from app.asr.soniox_engine import SonioxEngine


class SonioxTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        data = Path(temp.name)
        for name, value in (("DATA_DIR", data), ("SETTINGS_FILE", data / "settings.json"),
                            ("SECRETS_FILE", data / ".env"), ("MODELS_DIR", data / "models"),
                            ("GLOSSARY_FILE", data / "glossary.json")):
            p = patch.object(paths, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(paths, "ensure_dirs", side_effect=lambda: data.mkdir(exist_ok=True))
        p.start()
        self.addCleanup(p.stop)
        config._cache = config._cache_stamp = None
        self.addCleanup(lambda: setattr(config, "_cache", None))

    def test_key_only_in_env_and_masked(self):
        config.save({"asr": {"soniox_key": "abcdefgh1234"}})
        self.assertNotIn("abcdefgh1234", paths.SETTINGS_FILE.read_text("utf-8"))
        self.assertEqual(config.soniox_key(), "abcdefgh1234")
        with patch("app.summarize.available", return_value=True):
            self.assertEqual(config.public()["asr"]["soniox_key"], "********1234")

    def test_engine_choice(self):
        with self.assertRaises(RuntimeError):            # 키도 PC 위스퍼도 없음
            asr.build_engine({"engine": "auto"})
        config.save({"asr": {"soniox_key": "k"}})
        self.assertIsInstance(asr.build_engine({"engine": "auto"}), SonioxEngine)

    def test_old_whisper_setting_becomes_auto(self):
        paths.SETTINGS_FILE.write_text(json.dumps({"asr": {"engine": "faster-whisper"}}), "utf-8")
        self.assertEqual(config.load()["asr"]["engine"], "auto")

    def test_reads_tokens_with_offset(self):
        e = SonioxEngine({"language": "ko"}, key="k")
        msgs = [
            {"tokens": [{"text": "안녕", "start_ms": 100, "end_ms": 400, "is_final": True},
                        {"text": "하세", "start_ms": 400, "end_ms": 600, "is_final": False}]},
            {"tokens": [{"text": "<end>", "is_final": True}, {"text": "<fin>", "is_final": True}]},
            {"tokens": [], "finished": True},
        ]
        e._read([json.dumps(m) for m in msgs], offset=10.0)
        self.assertEqual([(w.text, w.start) for w in e.step()], [("안녕", 10.1)])
        self.assertTrue(e._fin.is_set())

    def test_error_message_reaches_session(self):
        e = SonioxEngine({}, key="k")
        e._read([json.dumps({"tokens": [], "error_code": 401, "error_message": "bad"})], offset=0.0)
        with self.assertRaisesRegex(RuntimeError, "키가 맞지 않습니다"):
            e.step()

    def test_native_processor_passes_partial(self):
        class Fake:
            began = 0

            def begin(self):
                self.began += 1

            def insert_audio(self, chunk):
                pass

            def step(self, final=False):
                return [Word(0, 1, "확정")]

            def partial_words(self):
                return [Word(1, 2, "미리")]

        f = Fake()
        proc = NativeStreamingASR(f)
        proc.insert_audio(np.zeros(1600, np.float32))
        committed, partial = proc.process_iter()
        self.assertEqual((f.began, committed[0].text, partial[0].text), (1, "확정", "미리"))


if __name__ == "__main__":
    unittest.main()
