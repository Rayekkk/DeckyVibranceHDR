# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""LUT maths, .cube output, LUT file caching and the native ARM worker."""

import hashlib
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import _fixtures  # noqa: F401  installs the loader stand-ins
import main  # noqa: E402
import main as M  # noqa: E402
import vibrance_lut as V  # noqa: E402


CODE_VALUE_10BIT = 1.0 / 1023.0


class PqTransfer(unittest.TestCase):
    """SMPTE ST 2084, with 1.0 encoded meaning 10000 nits."""

    def test_round_trip_across_the_useful_range(self):
        # The last three are this panel's black, full-field and peak.
        for nits in (0.0, 1.0, 100.0, 203.0, 0.0007, 475.683, 1107.128, 10000.0):
            encoded = V.linear_to_pq(nits / 10000.0)
            back = V.pq_to_linear(encoded) * 10000.0
            self.assertAlmostEqual(back, nits, places=6)

    def test_anchors(self):
        self.assertEqual(V.linear_to_pq(0.0), 0.0)
        self.assertAlmostEqual(V.linear_to_pq(1.0), 1.0, places=9)
        self.assertEqual(V.pq_to_linear(0.0), 0.0)

    def test_monotonic(self):
        previous = -1.0
        for i in range(0, 1024):
            value = V.linear_to_pq(i / 1023.0)
            self.assertGreater(value, previous)
            previous = value


class Saturation(unittest.TestCase):

    def _grid(self, step=4):
        size = 33
        for r in range(0, size, step):
            for g in range(0, size, step):
                for b in range(0, size, step):
                    yield (r / (size - 1), g / (size - 1), b / (size - 1))

    def test_unity_is_exactly_identity(self):
        for rgb in self._grid():
            self.assertEqual(V.saturate(rgb, 1.0), rgb)

    def test_round_trip_stays_under_one_code_value(self):
        """Forced through the full path, unity must still be invisible."""
        worst = 0.0
        for rgb in self._grid(step=2):
            out = V.saturate(rgb, 1.0000001)
            worst = max(worst, max(abs(a - b) for a, b in zip(rgb, out)))
        self.assertLess(worst, CODE_VALUE_10BIT)

    def test_black_stays_black(self):
        """gamescope's bRaisesBlackLevelFloor keys off the first LUT entry."""
        for saturation in (0.0, 0.5, 1.0, main.HDR_SOFT_LIMIT, main.HDR_MAX):
            self.assertEqual(V.saturate((0.0, 0.0, 0.0), saturation),
                             (0.0, 0.0, 0.0))

    def test_greys_stay_neutral(self):
        for level in (0.1, 0.25, 0.5, 0.75, 1.0):
            for saturation in (0.0, 0.5, 1.5):
                out = V.saturate((level, level, level), saturation)
                self.assertLess(max(out) - min(out), 1e-4,
                                f"level {level} at saturation {saturation}")

    def test_zero_saturation_is_greyscale(self):
        for rgb in ((0.6, 0.2, 0.2), (0.2, 0.6, 0.3), (0.1, 0.1, 0.7)):
            out = V.saturate(rgb, 0.0)
            self.assertLess(max(out) - min(out), 1e-6)

    def test_saturation_increases_monotonically(self):
        rgb = (0.55, 0.30, 0.30)
        spreads = [max(V.saturate(rgb, s)) - min(V.saturate(rgb, s))
                   for s in (0.4, 0.6, 0.8, 1.0, 1.2)]
        for earlier, later in zip(spreads, spreads[1:]):
            self.assertGreater(later, earlier)

    def test_output_stays_inside_the_cube(self):
        """Tetrahedral interpolation in gamescope expects 0..1.

        Bounded by the range the UI can actually reach, so raising the ceiling
        later keeps the worst selectable case covered instead of quietly
        dropping out of it.
        """
        for saturation in (0.0, main.HDR_MAX):
            for rgb in self._grid(step=3):
                for channel in V.saturate(rgb, saturation):
                    self.assertGreaterEqual(channel, 0.0)
                    self.assertLessEqual(channel, 1.0)


class CubeFile(unittest.TestCase):

    def test_shape_matches_what_gamescope_parses(self):
        size = 9
        text = V.generate(1.2, size)
        lines = text.splitlines()
        self.assertIn(f"LUT_3D_SIZE {size}", lines)
        data = [l for l in lines
                if l and not l.startswith(("#", "LUT_", "DOMAIN_"))]
        self.assertEqual(len(data), size ** 3)
        for line in (data[0], data[-1]):
            self.assertEqual(len(line.split()), 3)

    def test_first_entry_is_black_and_last_is_white(self):
        data = [l for l in V.generate(1.4, 5).splitlines()
                if l and not l.startswith(("#", "LUT_", "DOMAIN_"))]
        self.assertEqual(data[0], "0.000000 0.000000 0.000000")
        self.assertEqual(data[-1], "1.000000 1.000000 1.000000")

    def test_red_changes_fastest(self):
        """gamescope's loader assumes that ordering; getting it wrong scrambles
        the image rather than failing loudly."""
        size = 5
        data = [tuple(map(float, l.split()))
                for l in V.generate(1.0, size).splitlines()
                if l and not l.startswith(("#", "LUT_", "DOMAIN_"))]
        # Unity is identity, so entry n must equal its own grid coordinate.
        step = 1.0 / (size - 1)
        for index, entry in enumerate(data):
            r = index % size
            g = (index // size) % size
            b = index // (size * size)
            self.assertAlmostEqual(entry[0], r * step, places=5)
            self.assertAlmostEqual(entry[1], g * step, places=5)
            self.assertAlmostEqual(entry[2], b * step, places=5)

    def test_size_bounds_are_enforced(self):
        for bad in (1, 0, 129, 200):
            with self.assertRaises(ValueError):
                V.generate(1.0, bad)


class G22Transfer(unittest.TestCase):
    """The SDR look runs on gamma-encoded values, not PQ ones."""

    def test_round_trip(self):
        for i in range(0, 256):
            e = i / 255.0
            self.assertAlmostEqual(V.linear_to_g22(V.g22_to_linear(e)), e,
                                   places=9)

    def test_encoded_white_lands_on_sdr_reference_white(self):
        self.assertAlmostEqual(V.g22_to_linear(1.0) * 10000.0,
                               V.SDR_REFERENCE_NITS, places=6)

    def test_black_and_white_survive_a_boost(self):
        self.assertEqual(V.saturate((0.0, 0.0, 0.0), 1.5, "g22"),
                         (0.0, 0.0, 0.0))
        for channel in V.saturate((1.0, 1.0, 1.0), 1.5, "g22"):
            self.assertAlmostEqual(channel, 1.0, places=6)

    def test_greys_stay_neutral(self):
        for level in (0.1, 0.25, 0.5, 0.75):
            out = V.saturate((level, level, level), 1.5, "g22")
            self.assertLess(max(out) - min(out), 1e-6, str(level))
            self.assertAlmostEqual(out[0], level, places=6)

    def test_unity_is_identity(self):
        self.assertEqual(V.saturate((0.3, 0.6, 0.2), 1.0, "g22"),
                         (0.3, 0.6, 0.2))

    def test_saturation_rises_monotonically(self):
        rgb = (0.60, 0.25, 0.25)
        spreads = [max(V.saturate(rgb, s, "g22")) - min(V.saturate(rgb, s, "g22"))
                   for s in (1.0, 1.2, 1.4, 1.5)]
        for earlier, later in zip(spreads, spreads[1:]):
            self.assertGreater(later, earlier)

    def test_output_stays_inside_the_cube(self):
        size = 9
        step = 1.0 / (size - 1)
        for r in range(size):
            for g in range(size):
                for b in range(size):
                    out = V.saturate((r * step, g * step, b * step), 1.5, "g22")
                    for channel in out:
                        self.assertGreaterEqual(channel, 0.0)
                        self.assertLessEqual(channel, 1.0)

    def test_the_pq_path_is_untouched(self):
        """A Legion Go 2 regression here would be invisible until someone
        looked at an HDR game, so it is pinned explicitly."""
        for channel in V.saturate((1.0, 1.0, 1.0), 1.5):
            self.assertAlmostEqual(channel, 1.0, places=6)
        self.assertEqual(V.saturate((0.42, 0.42, 0.42), 1.5)[0],
                         V.saturate((0.42, 0.42, 0.42), 1.5, "pq")[0])

    def test_an_unknown_encoding_is_refused(self):
        with self.assertRaises(ValueError):
            V.saturate((0.5, 0.5, 0.5), 1.5, "srgb")
        with self.assertRaises(ValueError):
            V.generate(1.5, 5, "srgb")

    def test_the_cube_declares_which_domain_it_is_for(self):
        self.assertIn("g22 domain", V.generate(1.2, 5, "g22"))
        self.assertIn("pq domain", V.generate(1.2, 5))


class Lut(unittest.TestCase):
    def test_production_grids_match_pre_audit_golden_output(self):
        hashes = {"pq": "351335e1d2a649dab1b2f6076f5c4ed27f16e31a286ae5541aa3e879a7f9f4a2",
                  "g22": "4887c2e523a5d3f17fc350364006aac72efa95144004beb0318117e0f9981893"}
        for encoding, expected in hashes.items():
            self.assertEqual(hashlib.sha256(V.generate(1.3, 33, encoding).encode()).hexdigest(), expected)

    def test_grid_cache_is_bounded_and_separates_source_whites(self):
        V._grids.clear()
        for white in (V.D65, (0.296, 0.307), (0.307, 0.322)):
            V.generate(1.2, 33, "g22", white)
            self.assertLessEqual(len(V._grids), 2)
        self.assertEqual({key[2] for key in V._grids}, {(0.296, 0.307), (0.307, 0.322)})
    def test_prepared_grid_matches_direct_math_for_every_supported_step(self):
        for encoding in ("pq", "g22"):
            for white in (V.D65, (0.296, 0.307)):
                for n in range(51):
                    saturation = 1 + n / 100
                    rows = V.generate(saturation, 3, encoding, white).splitlines()[6:]
                    expected = ["%.6f %.6f %.6f" % V.saturate((r/2, g/2, b/2), saturation, encoding, white)
                                for b in range(3) for g in range(3) for r in range(3)]
                    self.assertEqual(rows, expected)

    def test_cancellation_interrupts_build_and_cannot_replace_a_slot(self):
        with self.assertRaises(V.BuildCancelled):
            V.generate(1.3, cancelled=lambda: True)
        with tempfile.TemporaryDirectory() as folder, patch.object(M, "_runtime_dir", return_value=folder), \
                patch.object(M, "_lut_writes_disabled", False):
            path = os.path.join(folder, "hdr-look-1.cube")
            with open(path, "w") as f:
                f.write("old")
            called = 0
            def cancelled():
                nonlocal called
                called += 1
                return called > 3
            self.assertEqual(M._write_look(1.3, 1, cancelled=cancelled), "")
            with open(path) as f:
                self.assertEqual(f.read(), "old")
            self.assertEqual(os.listdir(folder), ["hdr-look-1.cube"])

    def test_job_superseded_while_waiting_for_file_lock_never_generates(self):
        cancelled = threading.Event()
        result = []
        with patch.object(M, "_lut_writes_disabled", False), patch.object(V, "generate") as generate:
            with M._lut_file_lock:
                worker = threading.Thread(target=lambda: result.append(M._write_look(1.3, 1, cancelled=cancelled.is_set)))
                worker.start()
                cancelled.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [""])
            generate.assert_not_called()


class LutOutput(unittest.TestCase):
    def test_production_clipping_and_source_white_match_independent_prechange_hashes(self):
        # Computed using the captured pre-round-six generator, not the helpers
        # under test. Cover both range ends and non-D65 adaptation at 33^3.
        goldens = (
            ("pq", 1.01, V.D65, "667123e016ad9bdcf6527e19a7582b210371d57dbe51890d95ced3d26caa5aff"),
            ("pq", 1.5, V.D65, "fa6a2eab8b63fdb107af2349308bccbe0eb526572054ffb63993e2270f62b3ad"),
            ("g22", 1.01, V.D65, "44851e4e662bb6c41b449775952068f15d460247b1e5661944ebd5274d59716b"),
            ("g22", 1.5, V.D65, "c8d8af6f41f34cf6b9f40a7f50ac0d44f40a388be0b51d590432c8b847a2c25f"),
            ("g22", 1.01, (0.296, 0.307), "7a283d8ef79b8d9d63490f4dd1eefa984892cd13f194386ab0209c536bdc51e7"),
            ("g22", 1.5, (0.296, 0.307), "52a4919e5703b1726bdb4e6600e4977d467e532ae48dcaccf13674d51412a953"),
        )
        for encoding, saturation, white, expected in goldens:
            with self.subTest(encoding=encoding, saturation=saturation, white=white):
                output = V.generate(saturation, 33, encoding, white)
                self.assertEqual(hashlib.sha256(output.encode()).hexdigest(), expected)


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


class CompletedSlots(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, name, value in ((M, "_runtime_dir", lambda: folder.name),
                                    (M, "LUT_SIZE", 5),
                                    (M, "_lut_writes_disabled", False),
                                    (M, "_look_file_cache", OrderedDict())):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_returning_to_a_previous_saturation_reuses_its_unchanged_file(self):
        with patch.object(V, "generate", wraps=V.generate) as generate:
            for encoding in ("pq", "g22"):
                with self.subTest(encoding=encoding):
                    generate.reset_mock()
                    prefix = "hdr-look" if encoding == "pq" else "sdr-look"
                    first = M._write_look(1.1, 0, encoding, prefix)
                    original = Path(first).read_bytes()
                    M._write_look(1.3, 1, encoding, prefix)
                    returned = M._write_look(1.1, 0, encoding, prefix)
                    self.assertEqual(Path(returned).read_bytes(), original)
                    self.assertEqual(generate.call_count, 2, "existing requested LUT was rebuilt")

    def test_encoding_white_point_and_size_invalidate_a_completed_slot(self):
        with patch.object(V, "generate", wraps=V.generate) as generate:
            M._write_look(1.2, 0, "g22")
            M._write_look(1.2, 0, "g22", source_white=(0.30, 0.32))
            M._write_look(1.2, 0, "pq", source_white=(0.30, 0.32))
            with patch.object(M, "LUT_SIZE", 6):
                path = M._write_look(1.2, 0, "pq", source_white=(0.30, 0.32))
            self.assertIn("LUT_3D_SIZE 6", Path(path).read_text())
            self.assertEqual(generate.call_count, 4)

    def test_replaced_or_deleted_files_are_regenerated(self):
        with patch.object(V, "generate", wraps=V.generate) as generate:
            path = Path(M._write_look(1.2, 0))
            original = path.read_bytes()
            replacement = path.with_suffix(".replacement")
            replacement.write_text("damaged")
            replacement.replace(path)
            self.assertEqual(M._write_look(1.2, 0), str(path))
            self.assertEqual(path.read_bytes(), original)
            path.unlink()
            self.assertEqual(M._write_look(1.2, 0), str(path))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(generate.call_count, 3)

    def test_cached_hit_honors_cancellation_and_unload_without_writing(self):
        path = Path(M._write_look(1.2, 0))
        original = M._file_identity(path)
        with patch.object(V, "generate", wraps=V.generate) as generate, \
                patch.object(M.os, "replace") as replace:
            self.assertEqual(M._write_look(1.2, 0, cancelled=lambda: True), "")
            with patch.object(M, "_lut_writes_disabled", True):
                self.assertEqual(M._write_look(1.2, 0), "")
            checks = iter((False, True))
            self.assertEqual(M._write_look(1.2, 0, cancelled=lambda: next(checks)), "")
            self.assertEqual(M._write_look(1.2, 0), str(path))
            self.assertEqual(M._file_identity(path), original)
            generate.assert_not_called()
            replace.assert_not_called()

    def test_metadata_cache_is_bounded_and_eviction_rebuilds(self):
        with patch.object(V, "generate", wraps=V.generate) as generate:
            for i in range(6):
                M._write_look(1.2, 0, prefix=f"slot-{i}")
            self.assertEqual(len(M._look_file_cache), 4)
            M._write_look(1.2, 0, prefix="slot-5")
            self.assertEqual(generate.call_count, 6)
            M._write_look(1.2, 0, prefix="slot-0")
            self.assertEqual(generate.call_count, 7)
            self.assertEqual(len(M._look_file_cache), 4)

    def test_cache_does_not_bypass_saturation_or_size_type_validation(self):
        path = Path(M._write_look(1.0, 0))
        original = path.read_bytes()
        self.assertEqual(M._write_look(True, 0), "")
        with patch.object(M, "LUT_SIZE", 5.0):
            self.assertEqual(M._write_look(1.0, 0), "")
        self.assertEqual(path.read_bytes(), original)
