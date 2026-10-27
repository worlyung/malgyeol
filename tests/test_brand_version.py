import json
import unittest
from unittest.mock import Mock, patch

from app import version


class BrandVersionTests(unittest.TestCase):
    def test_brand_version(self):
        self.assertEqual(version.VERSION, "1.00")
        self.assertEqual(version.parse("1.00"), (1, 0, 0))
        self.assertGreater(version.parse("1.01"), version.parse("1.00"))

    def test_legacy_release_is_not_offered(self):
        response = Mock(content=json.dumps({"version": "1.0.7"}).encode())
        with patch.object(version.httpx, "get", return_value=response):
            self.assertIsNone(version.fetch_latest())

    def test_malgyeol_release_is_recognized(self):
        response = Mock(content=json.dumps({"product": "malgyeol", "version": "1.01"}).encode())
        with patch.object(version.httpx, "get", return_value=response):
            self.assertEqual(version.fetch_latest()["version"], "1.01")


if __name__ == "__main__":
    unittest.main()
