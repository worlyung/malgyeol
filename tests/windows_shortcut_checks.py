"""제작용 Python(tkinter 포함)에서 실행하는 Windows 바로가기 회귀 검사."""
import base64
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.installer_main import Installer, shell_folder


class ShortcutTests(unittest.TestCase):
    def test_physical_paths_and_unicode_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "말결 ' 설치"
            (root / 'python').mkdir(parents=True)
            (root / 'assets').mkdir()
            (root / 'python/pythonw.exe').touch()
            (root / 'assets/malgyeol.ico').touch()
            with patch('scripts.installer_main.subprocess.run') as run:
                Installer._make_link(None, root, root / '말결.lnk')
            command = run.call_args.args[0]
            self.assertIn('-EncodedCommand', command)
            script = base64.b64decode(command[-1]).decode('utf-16-le')
            self.assertIn(str(root.resolve()).replace("'", "''"), script)
            self.assertIn('malgyeol.ico,0', script)
            self.assertTrue(run.call_args.kwargs['check'])

    def test_missing_icon_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'python').mkdir()
            (root / 'python/pythonw.exe').touch()
            with self.assertRaises(OSError):
                Installer._make_link(None, root, root / '말결.lnk')

    def test_windows_shortcut_folders_exist(self):
        self.assertTrue(shell_folder(0x10).is_dir())
        self.assertTrue(shell_folder(0x02).is_dir())


if __name__ == '__main__':
    unittest.main()
