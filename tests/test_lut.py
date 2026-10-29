# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""LUT maths, .cube output, LUT file caching and the native ARM worker."""

import hashlib
import io
import math
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock, patch

from _fixtures import PluginTest, emulated_decky  # installs the decky stand-in first
import vibrance_control as M
import vibrance_lut as V


CODE_VALUE_10BIT = 1.0 / 1023.0


class PqTransfer(PluginTest):
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

    def test_crumbs_snap_to_black(self):
        # A rounding crumb would otherwise encode as visibly non-zero black.
        self.assertEqual(V.linear_to_pq(1e-14), 0.0)

    def test_values_past_the_peak_still_round_trip(self):
        # Boosted chroma pushes the PQ inside ICtCp to about 1.12 before clipping.
        self.assertAlmostEqual(V.linear_to_pq(V.pq_to_linear(1.1)), 1.1, places=9)

    def test_monotonic(self):
        previous = -1.0
        for i in range(0, 1024):
            value = V.linear_to_pq(i / 1023.0)
            self.assertGreater(value, previous)
            previous = value


class Saturation(PluginTest):

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
        for saturation in (0.0, 0.5, 1.0, M.vibrance_state.HDR_SOFT_LIMIT, M.HDR_MAX):
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
        for saturation in (0.0, M.HDR_MAX):
            for rgb in self._grid(step=3):
                for channel in V.saturate(rgb, saturation):
                    self.assertGreaterEqual(channel, 0.0)
                    self.assertLessEqual(channel, 1.0)


class CubeFile(PluginTest):

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


class G22Transfer(PluginTest):
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

    def test_an_impossible_white_point_is_refused_before_any_build(self):
        # main.py relies on validate() so the native worker never sees one.
        for saturation in (1.0, 1.5):
            for white in ((0.6, 0.5), (0.0, 0.3), (float("nan"), 0.3)):
                with self.assertRaises(ValueError):
                    V.validate(saturation, 33, "g22", white)

    def test_a_cancelled_build_stops_before_preparing_its_grid(self):
        with self.assertRaises(V.BuildCancelled):
            V._prepared_grid(9, "pq", V.D65, lambda: True)

    def test_an_unknown_encoding_is_refused(self):
        with self.assertRaises(ValueError):
            V.saturate((0.5, 0.5, 0.5), 1.5, "srgb")
        with self.assertRaises(ValueError):
            V.generate(1.5, 5, "srgb")

    def test_the_cube_declares_which_domain_it_is_for(self):
        self.assertIn("g22 domain", V.generate(1.2, 5, "g22"))
        self.assertIn("pq domain", V.generate(1.2, 5))


class Lut(PluginTest):
    def test_production_grids_match_pre_audit_golden_output(self):
        hashes = {"pq": "351335e1d2a649dab1b2f6076f5c4ed27f16e31a286ae5541aa3e879a7f9f4a2",
                  "g22": "4887c2e523a5d3f17fc350364006aac72efa95144004beb0318117e0f9981893"}
        for encoding, expected in hashes.items():
            self.assertEqual(hashlib.sha256(V.generate(1.3, 33, encoding).encode()).hexdigest(), expected)

    def test_an_unwritable_folder_fails_before_the_build(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "file").write_text("x")
            missing = os.path.join(folder, "file", "runtime")  # cannot be created either
            with patch.object(M.vibrance_effects, "runtime_dir", return_value=missing), \
                    patch.object(M.vibrance_lutfile, "generate") as build, \
                    patch.object(M.vibrance_lutfile, "disabled", False):
                self.assertEqual(M.vibrance_effects.write_look(1.3, 0), "")
            build.assert_not_called()

    def test_a_failed_write_is_logged_without_the_user_s_folders(self):
        folder = "/home/someone/homebrew/data/DeckyVibranceHDR"
        failure = OSError(13, "Permission denied", folder + "/hdr-look-0.cube.123.tmp")
        with patch.object(M.vibrance_lutfile, "write", side_effect=failure), \
                patch.object(M.vibrance_effects, "runtime_dir", return_value=folder), \
                patch.object(M.vibrance_state.logger, "error") as error:
            self.assertEqual(M.vibrance_effects.write_look(1.3, 0, prefix="private-look"), "")
        self.assertEqual(error.call_count, 1)
        self.assertIn("Permission denied: hdr-look-0.cube.123.tmp", error.call_args[0][0])
        self.assertNotIn("someone", error.call_args[0][0])
        self.assertEqual(M.vibrance_host.loggable(ValueError("bad /x")), "bad /x")

    def test_a_lasting_write_failure_is_logged_once_though_each_try_names_a_new_temporary(self):
        failures = [OSError(13, "Permission denied", f".hdr-look-0.cube.1.{n}.tmp") for n in range(3)]
        with patch.object(M.vibrance_lutfile, "write", side_effect=failures), \
                patch.object(M.vibrance_effects, "runtime_dir", return_value="/run"), \
                patch.object(M.vibrance_state.logger, "error") as error:
            for _ in failures:
                self.assertEqual(M.vibrance_effects.write_look(1.3, 0, prefix="lasting-look"), "")
        self.assertEqual(error.call_count, 1)

    def test_an_unexpected_build_error_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(M.vibrance_effects, "runtime_dir", return_value=folder), \
                    patch.object(M.vibrance_lutfile, "generate", side_effect=TypeError("bad request")), \
                    patch.object(M.vibrance_lutfile, "disabled", False):
                self.assertEqual(M.vibrance_effects.write_look(1.3, 0), "")
            self.assertEqual(os.listdir(folder), [])

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
        with tempfile.TemporaryDirectory() as folder, patch.object(M.vibrance_effects, "runtime_dir", return_value=folder), \
                patch.object(M.vibrance_lutfile, "disabled", False):
            path = os.path.join(folder, "hdr-look-1.cube")
            with open(path, "w") as f:
                f.write("old")
            called = 0
            def cancelled():
                nonlocal called
                called += 1
                return called > 3
            self.assertEqual(M.vibrance_effects.write_look(1.3, 1, cancelled=cancelled), "")
            with open(path) as f:
                self.assertEqual(f.read(), "old")
            self.assertEqual(os.listdir(folder), ["hdr-look-1.cube"])

    def test_job_superseded_while_waiting_for_file_lock_never_generates(self):
        cancelled = threading.Event()
        result = []
        with patch.object(M.vibrance_lutfile, "disabled", False), patch.object(V, "generate") as generate:
            with M.vibrance_lutfile.lock:
                worker = threading.Thread(target=lambda: result.append(M.vibrance_effects.write_look(1.3, 1, cancelled=cancelled.is_set)))
                worker.start()
                cancelled.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [""])
            generate.assert_not_called()


class Interpolation(PluginTest):
    """What gamescope makes of a 33-cube between its nodes, against exact maths.

    Error in ΔE_ITP (ITU-R BT.2124) over the same 15 000 seeded samples the
    colour audit of 2026-10-23 used, PQ up to ~1000 nit. The bounds sit just
    above what 33 nodes give today (PQ 1.5: max 13.65, p95 2.12; G22 1.5: max
    5.97, p95 0.86): they stop the error from growing, they do not ask for less.
    The worst cases sit where channels clip at the gamut edge.
    """

    @staticmethod
    def tetrahedral(nodes, size, rgb):
        """gamescope's lookup: the tetrahedron the point's fractions order picks."""
        pos = [c * (size - 1) for c in rgb]
        corner = [min(int(p), size - 2) for p in pos]
        frac = [p - c for p, c in zip(pos, corner)]
        order = sorted(range(3), key=lambda axis: -frac[axis])
        at = lambda c: nodes[c[0] + size * (c[1] + size * c[2])]   # red fastest
        out = [(1 - frac[order[0]]) * v for v in at(corner)]
        weights = (frac[order[0]] - frac[order[1]], frac[order[1]] - frac[order[2]], frac[order[2]])
        for axis, weight in zip(order, weights):
            corner[axis] += 1
            out = [o + weight * v for o, v in zip(out, at(corner))]
        return out

    def errors(self, saturation, encoding, cap):
        nodes = [tuple(map(float, line.split()))
                 for line in V.generate(saturation, 33, encoding).splitlines()[6:]]
        rng = random.Random(5)
        found = []
        for _ in range(15000):
            rgb = [rng.random() * cap for _ in range(3)]
            exact = V._to_ictcp(V.saturate(rgb, saturation, encoding), encoding, V.D65)
            looked_up = V._to_ictcp(self.tetrahedral(nodes, 33, rgb), encoding, V.D65)
            i, t, p = (a - b for a, b in zip(exact, looked_up))
            found.append(720 * math.sqrt(i * i + (0.5 * t) ** 2 + p * p))
        found.sort()
        return found[-1], found[int(0.95 * len(found))]

    def test_pq_error_at_the_strongest_setting_does_not_grow(self):
        worst, p95 = self.errors(1.5, "pq", 0.7518)        # 0.7518 PQ is ~1000 nit
        self.assertLess(worst, 14.0)
        self.assertLess(p95, 2.25)

    def test_g22_error_at_the_strongest_setting_does_not_grow(self):
        worst, p95 = self.errors(1.5, "g22", 1.0)
        self.assertLess(worst, 6.2)
        self.assertLess(p95, 0.9)


class LutOutput(PluginTest):
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


class NativeLut(PluginTest):
    """Under FEX the maths runs in the host's native Python (dk_process.run_helper); elsewhere in-process.
    dk_process's own tests cover the emulator check, the deadline and the kill on cancel."""

    def test_native_output_matches_both_transfers_and_white_points(self):
        with emulated_decky():
            for encoding, white in (('pq', V.D65), ('g22', V.D65), ('g22', (.30, .32))):
                self.assertEqual(M.vibrance_lutfile.generate(1.37, 9, encoding, white, lambda: False),
                                 V.generate(1.37, 9, encoding, white))

    def test_a_cancel_or_unload_stops_the_native_worker(self):
        with emulated_decky():
            with self.assertRaises(V.BuildCancelled):
                M.vibrance_lutfile.generate(1.2, 33, 'pq', V.D65, lambda: True)
            with patch.object(M.vibrance_lutfile, 'disabled', True), self.assertRaises(V.BuildCancelled):
                M.vibrance_lutfile.generate(1.2, 33, 'pq', V.D65, lambda: False)
            with patch.object(M.dk_process, "run_helper", side_effect=InterruptedError("cancelled")) as helper, \
                    self.assertRaises(V.BuildCancelled):
                M.vibrance_lutfile.generate(1.2, 33, 'pq', V.D65, lambda: False)
            cancel = helper.call_args.args[2]
            with patch.object(M.vibrance_lutfile, 'disabled', True):
                self.assertTrue(cancel(), "an unload stops a running build too")

    def test_validation_and_worker_failure_reject_partial_output(self):
        with emulated_decky():
            for saturation, size in ((True, 33), (1.2, True), (float('nan'), 33)):
                with self.assertRaises(ValueError):
                    M.vibrance_lutfile.generate(saturation, size, 'pq', V.D65, lambda: False)
            with patch.object(M.dk_process, "run_helper", return_value=(1, b'partial', b'Traceback: boom')):
                with self.assertRaisesRegex(ValueError, 'worker failed: Traceback: boom'):
                    M.vibrance_lutfile.generate(1.2, 33, 'pq', V.D65, lambda: False)
            with patch.object(M.dk_process, "run_helper", side_effect=TimeoutError):
                with self.assertRaisesRegex(ValueError, "worker timed out"):
                    M.vibrance_lutfile.generate(1.2, 33, "pq", V.D65, lambda: False)

    def test_the_native_worker_runs_the_lut_script_with_a_deadline(self):
        with emulated_decky(), patch.object(M.dk_process, "run_helper", return_value=(0, b"LUT\n", b"")) as helper:
            self.assertEqual(M.vibrance_lutfile.generate(1.2, 33, "pq", V.D65, lambda: False), "LUT\n")
        args, seconds = helper.call_args.args[:2]
        self.assertEqual(os.path.basename(args[0]), "vibrance_lut.py")
        self.assertEqual(args[1:], ["1.2", "33", "pq", repr(V.D65[0]), repr(V.D65[1])])
        self.assertEqual(seconds, M.vibrance_lutfile.WORKER_S)

    def test_steamos_keeps_existing_generator_and_xprop_invocation(self):
        with patch.object(M.dk_process, "emulated", return_value=False), \
                patch.object(V, "generate", return_value="original") as generate, \
                patch.object(M.vibrance_x11.subprocess, "run") as run:
            self.assertEqual(M.vibrance_lutfile.generate(1.2, 33, "pq", V.D65, lambda: False), "original")
            generate.assert_called_once()
            M.vibrance_x11.run_xprop(["xprop", "-root"], timeout=3)
            self.assertEqual(run.call_args.args[0], ["xprop", "-root"])
            self.assertNotIn("close_fds", run.call_args.kwargs)

    def test_arm_xprop_uses_absolute_host_binary_and_posix_spawn(self):
        with patch.object(M.dk_process, "emulated", return_value=True), \
                patch.object(M.vibrance_x11.subprocess, "run") as run:
            M.vibrance_x11.run_xprop(["xprop", "-root", "ATOM"], timeout=3)
        self.assertEqual(run.call_args.args[0], ["/proc/self/root/usr/bin/xprop", "-root", "ATOM"])
        self.assertIs(run.call_args.kwargs["close_fds"], False)
        self.assertEqual(run.call_args.kwargs["timeout"], 3)


class CompletedSlots(PluginTest):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, name, value in ((M.vibrance_effects, "runtime_dir", lambda: folder.name),
                                    (M.vibrance_lutfile, "SIZE", 5),
                                    (M.vibrance_lutfile, "disabled", False),
                                    (M.vibrance_lutfile, "_cache", OrderedDict())):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_returning_to_a_previous_saturation_reuses_its_unchanged_file(self):
        with patch.object(V, "generate", wraps=V.generate) as generate:
            for encoding in ("pq", "g22"):
                with self.subTest(encoding=encoding):
                    generate.reset_mock()
                    prefix = "hdr-look" if encoding == "pq" else "sdr-look"
                    first = M.vibrance_effects.write_look(1.1, 0, encoding, prefix)
                    original = Path(first).read_bytes()
                    M.vibrance_effects.write_look(1.3, 1, encoding, prefix)
                    returned = M.vibrance_effects.write_look(1.1, 0, encoding, prefix)
                    self.assertEqual(Path(returned).read_bytes(), original)
                    self.assertEqual(generate.call_count, 2, "existing requested LUT was rebuilt")

    def test_encoding_white_point_and_size_invalidate_a_completed_slot(self):
        with patch.object(V, "generate", wraps=V.generate) as generate:
            M.vibrance_effects.write_look(1.2, 0, "g22")
            M.vibrance_effects.write_look(1.2, 0, "g22", source_white=(0.30, 0.32))
            M.vibrance_effects.write_look(1.2, 0, "pq", source_white=(0.30, 0.32))
            with patch.object(M.vibrance_lutfile, "SIZE", 6):
                path = M.vibrance_effects.write_look(1.2, 0, "pq", source_white=(0.30, 0.32))
            self.assertIn("LUT_3D_SIZE 6", Path(path).read_text())
            self.assertEqual(generate.call_count, 4)

    def test_replaced_or_deleted_files_are_regenerated(self):
        with patch.object(V, "generate", wraps=V.generate) as generate:
            path = Path(M.vibrance_effects.write_look(1.2, 0))
            original = path.read_bytes()
            replacement = path.with_suffix(".replacement")
            replacement.write_text("damaged")
            replacement.replace(path)
            self.assertEqual(M.vibrance_effects.write_look(1.2, 0), str(path))
            self.assertEqual(path.read_bytes(), original)
            path.unlink()
            self.assertEqual(M.vibrance_effects.write_look(1.2, 0), str(path))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(generate.call_count, 3)

    def test_cached_hit_honors_cancellation_and_unload_without_writing(self):
        path = Path(M.vibrance_effects.write_look(1.2, 0))
        original = M.vibrance_host.file_identity(path)
        with patch.object(V, "generate", wraps=V.generate) as generate, \
                patch.object(M.os, "replace") as replace:
            self.assertEqual(M.vibrance_effects.write_look(1.2, 0, cancelled=lambda: True), "")
            with patch.object(M.vibrance_lutfile, "disabled", True):
                self.assertEqual(M.vibrance_effects.write_look(1.2, 0), "")
            checks = iter((False, True))
            self.assertEqual(M.vibrance_effects.write_look(1.2, 0, cancelled=lambda: next(checks)), "")
            self.assertEqual(M.vibrance_effects.write_look(1.2, 0), str(path))
            self.assertEqual(M.vibrance_host.file_identity(path), original)
            generate.assert_not_called()
            replace.assert_not_called()

    def test_metadata_cache_is_bounded_and_eviction_rebuilds(self):
        with patch.object(V, "generate", wraps=V.generate) as generate:
            for i in range(6):
                M.vibrance_effects.write_look(1.2, 0, prefix=f"slot-{i}")
            self.assertEqual(len(M.vibrance_lutfile._cache), 4)
            M.vibrance_effects.write_look(1.2, 0, prefix="slot-5")
            self.assertEqual(generate.call_count, 6)
            M.vibrance_effects.write_look(1.2, 0, prefix="slot-0")
            self.assertEqual(generate.call_count, 7)
            self.assertEqual(len(M.vibrance_lutfile._cache), 4)

    def test_cache_does_not_bypass_saturation_or_size_type_validation(self):
        path = Path(M.vibrance_effects.write_look(1.0, 0))
        original = path.read_bytes()
        self.assertEqual(M.vibrance_effects.write_look(True, 0), "")
        with patch.object(M.vibrance_lutfile, "SIZE", 5.0):
            self.assertEqual(M.vibrance_effects.write_look(1.0, 0), "")
        self.assertEqual(path.read_bytes(), original)


class LutFileWrites(PluginTest):
    """test_lut.py, next to the native worker tests. Kills L11, L14, L5."""

    def test_a_cancel_during_the_write_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(M.vibrance_lutfile, "disabled", False), \
                patch.object(M.vibrance_lutfile, "generate", return_value="LUT\n"):
            calls = iter((False, False, True))              # cancelled after the text is written
            path = os.path.join(folder, "hdr-look-0.cube")
            self.assertFalse(M.vibrance_lutfile.write(path, 1.2, "pq", V.D65, lambda: next(calls)))
            self.assertEqual(os.listdir(folder), [])

    def test_a_build_cancelled_inside_generate_is_quietly_false(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(M.vibrance_lutfile, "disabled", False), \
                patch.object(M.vibrance_lutfile, "generate", side_effect=V.BuildCancelled()):
            path = os.path.join(folder, "hdr-look-0.cube")
            self.assertFalse(M.vibrance_lutfile.write(path, 1.2, "pq", V.D65, lambda: False))
            self.assertEqual(os.listdir(folder), [])

    @staticmethod
    def _write(path):
        with patch.object(M.vibrance_lutfile, "disabled", False), \
                patch.object(M.vibrance_lutfile, "_cache", OrderedDict()), \
                patch.object(M.vibrance_lutfile, "generate", return_value="LUT\n"):
            return M.vibrance_lutfile.write(path, 1.2, "pq", V.D65, lambda: False)

    def _write_beside(self, plant):
        """Write slot 0 after *plant* aliased a victim at each temporary name."""
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "hdr-look-0.cube")
            victim = os.path.join(folder, "victim")
            Path(victim).write_text("victim")
            for name in (path + ".tmp", f"{path}.{os.getpid()}.tmp"):
                plant(victim, name)
            self.assertTrue(self._write(path))
            self.assertEqual(Path(victim).read_text(), "victim")
            self.assertFalse(os.path.islink(path))
            self.assertFalse(os.path.samefile(path, victim))
            self.assertEqual(Path(path).read_text(), "LUT\n")

    @unittest.skipUnless(os.name == "posix", "symlinks need POSIX")
    def test_a_symlink_at_the_temporary_name_is_not_followed(self):
        self._write_beside(os.symlink)

    def test_a_hard_link_at_the_temporary_name_is_not_written_through(self):
        self._write_beside(os.link)

    def test_only_old_temporaries_of_the_same_slot_are_swept(self):
        with tempfile.TemporaryDirectory() as folder:
            aged = ["hdr-look-0.cube.tmp", "hdr-look-0.cube.999.tmp",
                    "hdr-look-1.cube.999.tmp", "settings-abc123", "settings.json"]
            fresh = "hdr-look-0.cube.888.tmp"
            old = time.time() - 24 * 3600
            for name in aged + [fresh]:
                Path(folder, name).write_text("x")
            for name in aged:
                os.utime(os.path.join(folder, name), (old, old))
            self.assertTrue(self._write(os.path.join(folder, "hdr-look-0.cube")))
            self.assertEqual(sorted(os.listdir(folder)),
                             sorted(aged[2:] + [fresh, "hdr-look-0.cube"]))
