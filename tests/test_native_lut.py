"""Native ARM worker output, platform selection, and cancellation."""
import io
import sys
import subprocess
import unittest
from unittest.mock import patch, MagicMock
import _harness
_harness.install()
import main as M
import vibrance_lut as V


class NativeLut(unittest.TestCase):
    def test_native_output_matches_both_transfers_and_white_points(self):
        with patch.object(M, '_native_lut_python', return_value=sys.executable), patch.object(M, '_lut_writes_disabled', False):
            for encoding, white in (('pq', V.D65), ('g22', V.D65), ('g22', (.30, .32))):
                self.assertEqual(M._generate_lut(1.37, 9, encoding, white, lambda: False),
                                 V.generate(1.37, 9, encoding, white))

    def test_platform_selection_preserves_ordinary_steamos(self):
        def elf(machine):
            return b'\x7fELF\x02\x01' + bytes(12) + machine.to_bytes(2, 'little')
        with patch.object(M.sys, 'frozen', True, create=True):
            for native, loader, expected in ((183, 62, '/proc/self/root/usr/bin/python3'), (62, 62, None), (183, 183, None)):
                with patch('builtins.open', side_effect=[io.BytesIO(elf(native)), io.BytesIO(elf(loader))]):
                    self.assertEqual(M._native_lut_python(), expected)

    def test_native_worker_is_killed_and_reaped_on_cancel(self):
        worker = MagicMock()
        worker.__enter__.return_value = worker
        worker.poll.return_value = None
        worker.communicate.side_effect = [subprocess.TimeoutExpired('python', .05), (b'', b'')]
        with patch.object(M, '_native_lut_python', return_value=sys.executable), patch.object(M, '_lut_writes_disabled', False), patch.object(M.subprocess, 'Popen', return_value=worker), patch.object(M, '_system_env', return_value={'PYTHONHOME': 'bad', 'PYTHONPATH': 'bad'}) as env:
            cancelled = iter((False, False, True))
            with self.assertRaises(V.BuildCancelled):
                M._generate_lut(1.2, 33, 'pq', V.D65, lambda: next(cancelled))
            worker.kill.assert_called_once()
            self.assertEqual(worker.communicate.call_count, 2)
            self.assertEqual(env.return_value, {})

    def test_validation_and_worker_failure_reject_partial_output(self):
        with patch.object(M, '_native_lut_python', return_value=sys.executable), patch.object(M, '_lut_writes_disabled', False):
            for saturation, size in ((True, 33), (1.2, True), (float('nan'), 33)):
                with self.assertRaises(ValueError):
                    M._generate_lut(saturation, size, 'pq', V.D65, lambda: False)
            worker = MagicMock()
            worker.__enter__.return_value = worker
            worker.communicate.return_value = (b'partial', b'error')
            worker.returncode = 1
            worker.poll.return_value = 1
            with patch.object(M.subprocess, 'Popen', return_value=worker):
                with self.assertRaisesRegex(ValueError, 'worker failed'):
                    M._generate_lut(1.2, 33, 'pq', V.D65, lambda: False)

    def test_steamos_keeps_existing_generator_and_xprop_invocation(self):
        with patch.object(M, "_native_lut_python", return_value=None), patch.object(V, "generate", return_value="original") as generate, patch.object(M.subprocess, "run") as run:
            self.assertEqual(M._generate_lut(1.2, 33, "pq", V.D65, lambda: False), "original")
            generate.assert_called_once()
            M._run_xprop(["xprop", "-root"], timeout=3)
            run.assert_called_once_with(["xprop", "-root"], timeout=3)

    def test_arm_xprop_uses_absolute_host_binary_and_posix_spawn(self):
        with patch.object(M, "_native_lut_python", return_value="native"), patch.object(M.subprocess, "run") as run:
            M._run_xprop(["xprop", "-root", "ATOM"], timeout=3)
            run.assert_called_once_with(["/proc/self/root/usr/bin/xprop", "-root", "ATOM"], timeout=3, close_fds=False)

    def test_native_worker_deadline_kills_and_reaps_instead_of_hanging(self):
        worker = MagicMock()
        worker.__enter__.return_value = worker
        worker.poll.return_value = None
        with patch.object(M, "_native_lut_python", return_value=sys.executable), \
                patch.object(M, "_lut_writes_disabled", False), \
                patch.object(M.subprocess, "Popen", return_value=worker), \
                patch.object(M.time, "monotonic", side_effect=[0, 31]):
            with self.assertRaisesRegex(ValueError, "worker timed out"):
                M._generate_lut(1.2, 33, "pq", V.D65, lambda: False)
        worker.kill.assert_called_once()
        worker.communicate.assert_called_once_with()
