"""Malformed settings paths must fail promptly without leaking descriptors."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _harness
_harness.install()
import main as M


class SettingsFileContract(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX named pipes")
    def test_constructor_cannot_read_fifo_before_startup_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            os.mkfifo(Path(directory) / "settings.json")
            code = "\n".join([
                "import sys, os, json",
                "sys.path.insert(0, sys.argv[2])",
                "import _harness; _harness.install(sys.argv[1])",
                "import settings",
                "class EagerManager:",
                " def __init__(self, name, settings_directory):",
                "  self.path = os.path.join(settings_directory, name + '.json')",
                "  self.settings = {}; self.read()",
                " def read(self):",
                "  with open(self.path) as source: self.settings = json.load(source)",
                "settings.SettingsManager = EagerManager",
                "import main as M",
                "assert M.settings.settings == {}",
                "try: M._read_settings()",
                "except ValueError as e: assert 'regular file' in str(e)",
                "else: raise AssertionError('FIFO accepted')",
            ])
            subprocess.run([sys.executable, "-W", "error", "-c", code,
                            directory, str(Path(__file__).parent.resolve())],
                           cwd=Path(M.__file__).parent, check=True,
                           capture_output=True, timeout=3)

    def test_regular_file_retains_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            values = {"sdr_enabled": True, "custom": {"label": "zażółć"}}
            path.write_text(json.dumps(values), encoding="utf-8")
            store = SimpleNamespace(path=str(path), settings={})
            with patch.object(M, "settings", store), patch.object(M, "_settings_new", False):
                M._read_settings()
            self.assertEqual(store.settings, values)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), values)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX named pipes")
    def test_fifo_rejected_promptly_without_fd_leak_or_file_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            os.mkfifo(path)
            code = "\n".join([
                "import os, sys",
                "sys.path.insert(0, sys.argv[2])",
                "import _harness; _harness.install()",
                "import main as M",
                "from types import SimpleNamespace",
                "M.settings = SimpleNamespace(path=sys.argv[1], settings={'custom': 'keep'})",
                "M._settings_new = False",
                "before = len(os.listdir('/proc/self/fd'))",
                "for _ in range(100):",
                " try: M._read_settings()",
                " except ValueError as e: assert 'regular file' in str(e)",
                " else: raise AssertionError('FIFO accepted')",
                "assert len(os.listdir('/proc/self/fd')) == before",
                "assert M.settings.settings == {'custom': 'keep'}",
            ])
            subprocess.run([sys.executable, "-W", "error", "-c", code,
                            str(path), str(Path(__file__).parent.resolve())],
                           cwd=Path(M.__file__).parent, check=True,
                           capture_output=True, timeout=3)
            self.assertTrue(path.exists())

    @unittest.skipUnless(os.name == "posix", "requires POSIX symlinks")
    def test_regular_symlink_remains_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "real.json"
            real.write_text('{"hdr_saturation": 1.4}', encoding="utf-8")
            link = Path(directory) / "settings.json"
            link.symlink_to(real)
            store = SimpleNamespace(path=str(link), settings={})
            with patch.object(M, "settings", store), patch.object(M, "_settings_new", False):
                M._read_settings()
            self.assertEqual(store.settings, {"hdr_saturation": 1.4})
