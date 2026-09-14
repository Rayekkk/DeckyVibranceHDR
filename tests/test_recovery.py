# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Regression scenarios from the 1.0.2 review, with no device writes."""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _harness
_harness.install()
import main as M
import vibrance_lut as V


class Recovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = []
        self.plugin = M.Plugin()
        state = dict(M.Plugin._state)
        state.update(M.DEFAULT_SETTINGS)
        state.update(ready=True, external_display=False, hdr_supported=True,
                     sdr_applied=False, sdr_look_applied=False, hdr_applied=False,
                     sdr_error="", sdr_look_error="", hdr_error="", reason="ready",
                     sdr_generating=False, generating=False,
                     hdr_applied_saturation=None, sdr_look_saturation=None)
        for key, value in dict(
            _state=state, _mutation_lock=asyncio.Lock(), _tasks=[],
            _unloading=False, _sdr_baseline=0.4, _last_sdr_written=None,
            _sdr_look_path=None, _hdr_look_path=None,
            _sdr_look_baseline=None, _hdr_look_baseline=None,
            _sdr_generation=0, _sdr_look_generation=0, _hdr_generation=0,
            _slow_checks=0, _reassert_strikes=0, _look_slot=0, _sdr_look_slot=0,
        ).items():
            self.mock(M.Plugin, key, value)
        self.mock(M.Plugin, "_save")
        self.mock(M, "DEBOUNCE_S", 0)
        self.mock(M, "SDR_COALESCE_S", 0)
        self.mock(M, "SDR_NEUTRAL_ATOM", 1.0)
        self.mock(M, "SDR_ATOM_SPAN", 2.0)
        self.mock(M, "_other_writer_present", return_value=False)
        self.live_sdr = 0.8
        self.strings = {}
        self.float_writer = self.mock(M, "_write_float_atom", side_effect=self.write_float)
        self.mock(M, "_read_float_atom", side_effect=lambda _: self.live_sdr)
        self.string_writer = self.mock(M, "_write_string_atom", side_effect=self.write_string)
        self.mock(M, "_read_string_atom", side_effect=lambda atom: self.strings.get(atom))
        self.mock(M.os.path, "isfile", return_value=True)
        self.mock(M.os.path, "exists", return_value=True)
        self.builder = self.mock(M, "_write_look", side_effect=lambda value, slot, *args:
                                 f"/tmp/{'sdr' if args else 'hdr'}-look-{slot}.cube")

    def mock(self, obj, key, *args, **kwargs):
        item = patch.object(obj, key, *args, **kwargs)
        self.patches.append(item)
        return item.start()

    async def asyncTearDown(self):
        tasks = list(M.Plugin._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for item in reversed(self.patches):
            item.stop()

    def write_float(self, atom, value):
        self.live_sdr = value
        return True

    def write_string(self, atom, value):
        self.strings[atom] = value
        return True

    async def drain(self):
        await asyncio.gather(*list(M.Plugin._tasks))

    async def test_each_enable_captures_the_current_sdr_baseline(self):
        for baseline in (0.8, 0.6):
            self.live_sdr = baseline
            await self.plugin.set_sdr_enabled(True)
            await self.plugin.set_sdr_enabled(False)
            self.assertEqual(self.live_sdr, baseline)

    async def test_reenable_after_failed_release_does_not_capture_our_value(self):
        await self.plugin.set_sdr_enabled(True)
        self.float_writer.side_effect = None
        self.float_writer.return_value = False
        await self.plugin.set_sdr_enabled(False)
        self.float_writer.side_effect = self.write_float
        await self.plugin.set_sdr_enabled(True)
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)

    async def test_failed_hdr_replacement_retries_the_requested_value(self):
        await self.plugin.set_hdr_enabled(True)
        await self.plugin.set_hdr_saturation(1.1)
        await self.drain()
        old = M.Plugin._hdr_look_path
        self.string_writer.side_effect = None
        self.string_writer.return_value = False
        await self.plugin.set_hdr_saturation(1.3)
        await self.drain()
        self.assertEqual(M.Plugin._hdr_look_path, old)
        self.assertEqual(M.Plugin._state["hdr_applied_saturation"], 1.1)
        M.Plugin._slow_checks = M.SLOW_CHECK_EVERY - 1
        M.Plugin._watch_once()
        self.assertIn("could not", M.Plugin._state["hdr_error"])
        self.string_writer.side_effect = self.write_string
        self.plugin._retry_pending_looks()
        await self.drain()
        self.assertEqual(M.Plugin._state["hdr_applied_saturation"], 1.3)
        self.assertEqual(M.Plugin._state["hdr_error"], "")
        self.assertNotEqual(M.Plugin._hdr_look_path, old)

    async def test_failed_first_hdr_build_recovers_without_a_slider_move(self):
        self.builder.side_effect = None
        self.builder.return_value = ""
        await self.plugin.set_hdr_saturation(1.2)
        await self.plugin.set_hdr_enabled(True)
        await self.drain()
        self.assertFalse(M.Plugin._state["hdr_applied"])
        self.builder.return_value = "/tmp/hdr-look-1.cube"
        self.plugin._retry_pending_looks()
        await self.drain()
        self.assertEqual(M.Plugin._state["hdr_applied_saturation"], 1.2)

    async def test_failed_sdr_look_replacement_recovers(self):
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 0.0, 1.0
        await self.plugin.set_sdr_enabled(True)
        await self.plugin.set_sdr_saturation(2.2)
        await self.drain()
        self.string_writer.side_effect = None
        self.string_writer.return_value = False
        await self.plugin.set_sdr_saturation(2.6)
        await self.drain()
        self.assertAlmostEqual(M.Plugin._state["sdr_look_saturation"], 1.1)
        self.string_writer.side_effect = self.write_string
        self.plugin._retry_pending_looks()
        await self.drain()
        self.assertAlmostEqual(M.Plugin._state["sdr_look_saturation"], 1.3)

    async def test_retry_does_not_supersede_workers_or_apply_while_docked(self):
        M.Plugin._state.update(hdr_enabled=True, hdr_saturation=1.3, generating=True)
        self.plugin._retry_pending_looks()
        self.assertEqual(M.Plugin._tasks, [])
        M.Plugin._state.update(generating=False, external_display=True)
        self.plugin._retry_pending_looks()
        self.assertEqual(M.Plugin._tasks, [])
        M.Plugin._state.update(external_display=False, hdr_enabled=False)
        self.plugin._retry_pending_looks()
        self.assertEqual(M.Plugin._tasks, [])

    async def test_monitor_runs_recovery_and_a_healthy_look_is_not_rebuilt(self):
        M.Plugin._state.update(hdr_enabled=True, hdr_saturation=1.3)
        self.mock(M, "WATCH_INTERVAL_S", 1)
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 100)
        real_sleep = asyncio.sleep
        ticks = 0

        async def sleep(delay):
            nonlocal ticks
            if delay == M.SUSPEND_POLL_S:
                ticks += 1
                if ticks > 1:
                    raise asyncio.CancelledError()
            await real_sleep(0)

        self.mock(M.asyncio, "sleep", side_effect=sleep)
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._monitor_loop()
        await self.drain()
        self.assertEqual(M.Plugin._state["hdr_applied_saturation"], 1.3)
        self.builder.reset_mock()
        self.plugin._retry_pending_looks()
        await self.drain()
        self.builder.assert_not_called()

    async def test_reset_error_survives_unrelated_release_then_clears_on_recovery(self):
        await self.plugin.set_sdr_enabled(True)
        self.float_writer.side_effect = None
        self.float_writer.return_value = False
        state = await self.plugin.reset()
        self.assertFalse(state["sdr_enabled"])
        self.assertTrue(state["sdr_applied"])
        self.assertIn("could not restore", state["sdr_error"])
        self.assertIn(state["sdr_error"], state["reason"])
        self.float_writer.side_effect = self.write_float
        M.Plugin._slow_checks = M.SLOW_CHECK_EVERY - 1
        M.Plugin._watch_once()
        self.assertEqual(M.Plugin._state["sdr_error"], "")
        self.assertFalse(M.Plugin._state["sdr_applied"])

    async def test_missing_sdr_property_is_recreated_only_on_gamescope(self):
        await self.plugin.set_sdr_enabled(True)
        self.live_sdr = None
        self.float_writer.reset_mock()
        count = self.mock(M, "_count_gamescope_atoms", return_value=0)
        M.Plugin._watch_once()
        self.float_writer.assert_not_called()
        count.return_value = 42
        M.Plugin._watch_once()
        self.float_writer.assert_called_once()
        self.assertEqual(self.live_sdr, 1.0)
        self.assertEqual(M.Plugin._state["sdr_error"], "")

    async def test_failed_initial_sdr_write_recovers(self):
        self.float_writer.side_effect = None
        self.float_writer.return_value = False
        await self.plugin.set_sdr_enabled(True)
        self.assertFalse(M.Plugin._state["sdr_applied"])
        self.float_writer.side_effect = self.write_float
        M.Plugin._watch_once()
        self.assertTrue(M.Plugin._state["sdr_applied"])


class SdrColorimetry(unittest.TestCase):
    # Independent reference: exact rational Gaussian elimination of the RGB
    # primary matrices from gamescope's generic gamut and BT.2020, both D65.
    TO_2020 = ((0.8421220396036041, 0.1405712459507854, 0.017306714445610463),
               (0.045033388222038326, 0.9528639745224308, 0.0021026372555309094),
               (-0.00044189761592675655, 0.010632628934500689, 0.989809268681426))

    def ictcp(self, rgb):
        linear = V._mul3(self.TO_2020, tuple(V.g22_to_linear(c) for c in rgb))
        lms = V._mul3(V._RGB_TO_LMS, linear)
        return V._mul3(V._LMS_TO_ICTCP, tuple(V.linear_to_pq(c) for c in lms))

    def test_sdr_chroma_scaling_preserves_intensity_in_the_real_source_gamut(self):
        for rgb in ((0.6, 0.4, 0.2), (0.4, 0.2, 0.6), (0.4, 0.5, 0.3)):
            original = self.ictcp(rgb)
            out = V.saturate(rgb, 1.25, "g22")
            self.assertTrue(all(0 < c < 1 for c in out), "reference must not clip")
            adjusted = self.ictcp(out)
            for actual, expected in zip(adjusted,
                                        (original[0], original[1]*1.25, original[2]*1.25)):
                self.assertAlmostEqual(actual, expected, places=9)

    def test_non_d65_white_keeps_greys_neutral(self):
        for white in ((0.296, 0.307), (0.307, 0.322), V.D65):
            for grey in (0.0, 0.1, 0.5, 1.0):
                out = V.saturate((grey,)*3, 1.5, "g22", white)
                for channel in out:
                    self.assertAlmostEqual(channel, grey, places=6)

    def test_edid_white_bits_and_invalid_fallback(self):
        blob = bytearray(128)
        x, y = 303, 314
        blob[0x21], blob[0x22] = x >> 2, y >> 2
        blob[0x1A] = ((x & 3) << 2) | (y & 3)
        self.assertEqual(M._edid_white_point(blob), (x/1024, y/1024))
        for invalid in (None, b"", bytes(128)):
            self.assertEqual(M._edid_white_point(invalid), V.D65)
