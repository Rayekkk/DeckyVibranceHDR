"""Odin audit: display metadata must not block startup on a FIFO."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import _harness
_harness.install()
import main as M


class MetadataFiles(unittest.TestCase):
    def test_regular_edid_read_remains_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "edid.bin"
            data = bytes(range(256)) * 200
            path.write_bytes(data)
            self.assertEqual(M._read_edid(str(path)), data[:128 * 256 + 1])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO fixture")
    def test_fifo_metadata_cannot_hang_or_leak_descriptors(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "edid-fifo"
            os.mkfifo(path)
            code = """import sys,os
sys.path.insert(0, 'tests')
import _harness
_harness.install()
import main
before=len(os.listdir('/proc/self/fd'))
for _ in range(20):
 assert main._read_edid(sys.argv[1]) is None
assert len(os.listdir('/proc/self/fd'))==before
"""
            try:
                result = subprocess.run([sys.executable, "-W", "error", "-c", code, str(path)],
                                        cwd=Path(M.__file__).parent, capture_output=True,
                                        text=True, timeout=2)
            except subprocess.TimeoutExpired:
                self.fail("EDID FIFO blocked startup instead of returning unknown metadata")
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture")
    def test_regular_edid_target_is_read_through_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "edid.bin"
            target.write_bytes(b"display metadata")
            alias = Path(folder) / "edid-link"
            alias.symlink_to(target)
            self.assertEqual(M._read_edid(str(alias)), target.read_bytes())
