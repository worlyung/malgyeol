import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.repack_payload import repack


class RepackTests(unittest.TestCase):
    def test_replaces_one_file_and_preserves_other_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'payload.zip'
            (root / 'app.py').write_bytes(b'new code')
            with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_LZMA) as output:
                output.writestr('app.py', b'old code')
                output.writestr('model.bin', bytes(range(256)) * 100)
                output.writestr('empty/', b'')
                output.comment = b'release'
            repack(archive, root, ['app.py'])
            with zipfile.ZipFile(archive) as result:
                self.assertIsNone(result.testzip())
                self.assertEqual(result.read('app.py'), b'new code')
                self.assertEqual(result.read('model.bin'), bytes(range(256)) * 100)
                self.assertEqual(len(result.infolist()), 3)
                self.assertEqual(result.comment, b'release')
