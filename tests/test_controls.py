# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""RPC inputs, applying SDR/HDR, look ownership and worker coalescing."""

import asyncio
import os
import subprocess
import tempfile
import threading
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import patch

from _fixtures import PluginCase, WRITE_LOOK, drain  # installs the loader stand-ins first
import main as M
import vibrance_lut as V


class Clamping(unittest.TestCase):

    RANGES = (("sdr", "SDR_MIN", "SDR_SOFT_WIDE", "SDR_MAX_WIDE"),
              ("hdr", "HDR_MIN", "HDR_SOFT_LIMIT", "HDR_MAX"))

    def _bounds(self):
        for half, lo, soft, hi in self.RANGES:
            yield half, getattr(M, lo), getattr(M, soft), getattr(M, hi)

    def test_each_range_is_enforced(self):
        for half, lo, _soft, hi in self._bounds():
            self.assertEqual(M._clamp(-1.0, lo, hi), lo, half)
            self.assertEqual(M._clamp(99.0, lo, hi), hi, half)
            self.assertEqual(M._clamp(lo, lo, hi), lo, half)
            self.assertEqual(M._clamp(hi, lo, hi), hi, half)

    def test_neither_half_can_desaturate(self):
        """The floor is neutral on both. Below it the picture is being drained,
        which is the one thing this plugin is not for."""
        for half, lo, _soft, hi in self._bounds():
            self.assertEqual(lo, 1.0, half)
            self.assertEqual(M._clamp(0.0, lo, hi), 1.0, half)
            self.assertEqual(M._clamp(0.999, lo, hi), 1.0, half)

    def test_default_is_the_floor_of_both(self):
        for half, lo, _soft, _hi in self._bounds():
            self.assertEqual(M.SAT_DEFAULT, lo, half)

    def test_soft_limits_sit_inside_their_ranges(self):
        for half, lo, soft, hi in self._bounds():
            self.assertGreater(soft, lo, half)
            self.assertLess(soft, hi, half)

    def test_state_advertises_every_bound(self):
        """The frontend reads its slider bounds from here and nothing else
        checks them, so all six are pinned to their constants. Asserting only
        some of them lets a copy-paste slip in a block of near-identical lines
        through: an sdr_min left at the old 0.0 would draw a slider a third of
        which snaps back the moment it is released."""
        for key, constant in (
            ("sdr_min", M.SDR_MIN),
            ("sdr_max", M.SDR_MAX_WIDE),
            ("sdr_soft_limit", M.SDR_SOFT_WIDE),
            ("hdr_min", M.HDR_MIN),
            ("hdr_max", M.HDR_MAX),
            ("hdr_soft_limit", M.HDR_SOFT_LIMIT),
        ):
            self.assertIn(key, M.Plugin._state)
            self.assertEqual(M.Plugin._state[key], constant, key)

    def test_the_requested_ranges_are_what_shipped(self):
        """Pinned to literals on purpose. Everything else here is relative, so
        without this a tuning pass could hand back half the range and the suite
        would stay green."""
        self.assertEqual(M.SDR_MIN, 1.0)
        self.assertEqual(M.SDR_MAX_WIDE, 3.0)
        self.assertEqual(M.SDR_MAX_STANDARD, 3.0)
        self.assertEqual(M.HDR_MIN, 1.0)
        self.assertEqual(M.HDR_MAX, 1.5)

    def test_hdr_ceiling_stays_below_the_sdr_one(self):
        """Different mechanisms, different useful ranges - if these ever become
        one number again the LUT half is the one that suffers."""
        self.assertLess(M.HDR_MAX, M.SDR_MAX_WIDE)


class Setters(unittest.TestCase):
    """_clamp is exercised above in isolation; this is about its call sites.

    Passing one half's bounds to the other half's setter is a single-token slip
    that nothing else in this file would notice.
    """

    def setUp(self):
        self.plugin = M.Plugin()
        for key in ("_disconnected", "_settings_loaded"):
            fixture = patch.object(M.Plugin, key, key == "_settings_loaded")
            fixture.start()
            self.addCleanup(fixture.stop)
        M.Plugin._state["sdr_enabled"] = False
        M.Plugin._state["hdr_enabled"] = False
        M.Plugin._state["sdr_max"] = M.SDR_MAX_WIDE

    def test_sdr_setter_uses_the_sdr_ceiling(self):
        state = drain(self.plugin.set_sdr_saturation(M.SDR_MAX_WIDE))
        self.assertEqual(state["sdr_saturation"], M.SDR_MAX_WIDE)

    def test_hdr_setter_does_not_get_the_sdr_ceiling(self):
        state = drain(self.plugin.set_hdr_saturation(M.SDR_MAX_WIDE))
        self.assertEqual(state["hdr_saturation"], M.HDR_MAX)

    def test_neither_setter_accepts_desaturation(self):
        self.assertEqual(
            drain(self.plugin.set_sdr_saturation(0.0))["sdr_saturation"], 1.0)
        self.assertEqual(
            drain(self.plugin.set_hdr_saturation(0.0))["hdr_saturation"], 1.0)

    def test_saved_values_from_an_older_range_are_lifted_on_load(self):
        """1.0.0 allowed 0.0. Without the migration those users come back to a
        drained picture the new UI cannot even represent."""
        M.settings._store.update({
            "sdr_saturation": 0.4,
            "hdr_saturation": 0.0,
            "sdr_enabled": True,
            "hdr_enabled": True,
        })
        try:
            # _main reaches the clamps and then returns at the unreachable
            # gamescope check, so no subprocess is involved.
            drain(self.plugin._main())
        finally:
            M.settings._store.clear()
        self.assertEqual(M.Plugin._state["sdr_saturation"], 1.0)
        self.assertEqual(M.Plugin._state["hdr_saturation"], 1.0)


class SerializedMutations(unittest.TestCase):
    """An older RPC must never commit after a newer toggle has finished."""

    def test_sdr_enable_cannot_overtake_disable(self):
        calls = []
        apply_started = threading.Event()
        let_apply_finish = threading.Event()
        result = {}
        real_save = M.Plugin._save
        real_apply = M.Plugin._apply_sdr
        real_release = M.Plugin._release_sdr
        real_schedule = M.Plugin._schedule_look
        real_lock = M.Plugin._mutation_lock
        real_read = M._read_float_atom
        saved_state = dict(M.Plugin._state)
        saved_unloading = M.Plugin._unloading
        saved_disconnected = M.Plugin._disconnected
        saved_baseline = M.Plugin._sdr_baseline

        def fake_save(values=None):
            pass

        def fake_apply():
            apply_started.set()
            let_apply_finish.wait(2)
            calls.append("apply")
            M.Plugin._state["sdr_applied"] = True

        def fake_release():
            calls.append("release")
            M.Plugin._state["sdr_applied"] = False
            return True

        async def scenario():
            plugin = M.Plugin()
            enabling = asyncio.create_task(plugin.set_sdr_enabled(True))
            self.assertTrue(await asyncio.to_thread(apply_started.wait, 1))
            disabling = asyncio.create_task(plugin.set_sdr_enabled(False))
            await asyncio.sleep(0)
            self.assertFalse(disabling.done())
            let_apply_finish.set()
            await asyncio.gather(enabling, disabling)
            await asyncio.gather(*list(M.Plugin._tasks))
            result.update(M.Plugin._state)

        loaded = patch.object(M.Plugin, "_settings_loaded", True)
        loaded.start()
        self.addCleanup(loaded.stop)
        M.Plugin._save = staticmethod(fake_save)
        M.Plugin._apply_sdr = staticmethod(fake_apply)
        M.Plugin._release_sdr = staticmethod(fake_release)
        M.Plugin._schedule_look = lambda self, component: None
        M.Plugin._mutation_lock = asyncio.Lock()
        M.Plugin._unloading = False
        M.Plugin._disconnected = False
        M._read_float_atom = lambda atom: 0.8
        M.Plugin._state.update({
            "external_display": False, "sdr_enabled": False,
            "sdr_applied": False,
        })
        try:
            asyncio.run(scenario())
        finally:
            let_apply_finish.set()
            M.Plugin._save = real_save
            M.Plugin._apply_sdr = real_apply
            M.Plugin._release_sdr = real_release
            M.Plugin._schedule_look = real_schedule
            M.Plugin._mutation_lock = real_lock
            M.Plugin._unloading = saved_unloading
            M.Plugin._disconnected = saved_disconnected
            M.Plugin._sdr_baseline = saved_baseline
            M._read_float_atom = real_read
            M.Plugin._state.update(saved_state)
        self.assertEqual(calls, ["apply", "release"])
        self.assertFalse(result["sdr_enabled"])
        self.assertFalse(result["sdr_applied"])


class SdrLookSplit(unittest.TestCase):
    """EXPERIMENTAL, Legion Go S only. The atom carries what it can and the
    look carries the rest - and on a wide-gamut panel there is never a rest, so
    a Legion Go 2 must come out of this untouched."""

    def setUp(self):
        self.saved = (M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN)

    def tearDown(self):
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = self.saved

    def _wide(self):
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 1.0, 2.0

    def _standard(self):
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 0.0, 1.0

    def test_a_wide_gamut_panel_never_gets_a_look(self):
        """The whole point of the gate: nothing about the Go 2 changes."""
        self._wide()
        for value in (1.0, 1.5, 2.0, 2.5, 3.0):
            atom, look = M._sdr_split(value)
            self.assertEqual(look, 1.0, value)
            self.assertAlmostEqual(atom, value, msg=str(value))

    def test_a_standard_panel_fills_the_atom_first(self):
        self._standard()
        for value, expected in ((1.0, 0.0), (1.5, 0.5), (2.0, 1.0)):
            atom, look = M._sdr_split(value)
            self.assertAlmostEqual(atom, expected, msg=str(value))
            self.assertEqual(look, 1.0, value)

    def test_the_look_only_starts_where_cfit_stops(self):
        """cfit() clamps at atom 1.0, so below 200% the look must stay out."""
        self._standard()
        self.assertEqual(M._sdr_split(1.99)[1], 1.0)
        self.assertGreater(M._sdr_split(2.01)[1], 1.0)

    def test_the_atom_never_goes_past_where_it_responds(self):
        self._standard()
        for value in (2.0, 2.5, 3.0):
            self.assertAlmostEqual(M._sdr_split(value)[0], 1.0, msg=str(value))

    def test_the_top_of_the_slider_asks_the_look_for_its_ceiling(self):
        self._standard()
        self.assertAlmostEqual(
            M._sdr_split(M.SDR_MAX_STANDARD)[1],
            M.SDR_LOOK_CEILING)

    def test_raising_the_maximum_did_not_weaken_what_was_below_it(self):
        """The look is a rate, not a fraction of the range, so 300% asks for
        exactly what it asked for when 300% was the top of the slider."""
        self._standard()
        self.assertAlmostEqual(M._sdr_split(3.0)[1], 1.5)
        self.assertAlmostEqual(M._sdr_split(2.5)[1], 1.25)

    def test_the_look_ramps_rather_than_jumping(self):
        self._standard()
        looks = [M._sdr_split(v)[1] for v in (2.0, 2.25, 2.5, 2.75, 3.0)]
        for earlier, later in zip(looks, looks[1:]):
            self.assertGreater(later, earlier)

    def test_the_look_ceiling_matches_the_hdr_one(self):
        """Same code, same clipping, so the same ceiling."""
        self.assertEqual(M.SDR_LOOK_CEILING, M.HDR_MAX)

    def test_the_ceiling_is_never_asked_for_more_than_it_allows(self):
        self._standard()
        for value in (4.0, 6.0, 99.0):
            self.assertLessEqual(M._sdr_split(value)[1],
                                 M.SDR_LOOK_CEILING, str(value))

    def test_the_warning_sits_where_the_hdr_half_warns(self):
        """Identical maths, so the look value that starts clipping is the same;
        only the slider position it corresponds to differs."""
        self._standard()
        at_warning = M._sdr_split(M.SDR_SOFT_STANDARD)[1]
        self.assertAlmostEqual(at_warning, M.HDR_SOFT_LIMIT, places=6)


class StaleLookJobs(unittest.TestCase):
    """A worker may finish its file, but no stale worker may commit an atom."""

    def setUp(self):
        loaded = patch.object(M.Plugin, "_settings_loaded", True)
        loaded.start()
        self.addCleanup(loaded.stop)

    def test_hdr_worker_finishing_after_disable_cannot_restore_the_look(self):
        build_started = threading.Event()
        let_build_finish = threading.Event()
        writes = []
        result = {}
        real_build = M._write_look
        real_write = M._write_string_atom
        real_read = M._read_string_atom
        real_debounce = M.DEBOUNCE_S
        real_lock = M.Plugin._mutation_lock
        saved_state = dict(M.Plugin._state)
        saved_unloading = M.Plugin._unloading
        saved_fields = (M.Plugin._hdr_look_path,
                        M.Plugin._hdr_look_baseline,
                        M.Plugin._look_slot,
                        M.Plugin._hdr_generation)

        def late_build(*_args):
            build_started.set()
            let_build_finish.wait(2)
            return os.path.join(M._runtime_dir(), "hdr-look-1.cube")

        async def scenario():
            plugin = M.Plugin()
            plugin._schedule_look("hdr")
            worker = M.Plugin._tasks[-1]
            while not build_started.is_set():
                await asyncio.sleep(0.005)
            await plugin.set_hdr_enabled(False)
            let_build_finish.set()
            await asyncio.gather(worker, return_exceptions=True)
            result.update(M.Plugin._state)

        M._write_look = late_build
        M._write_string_atom = lambda atom, value: (
            writes.append((atom, value)) or True)
        M._read_string_atom = lambda atom: None
        M.DEBOUNCE_S = 0
        M.Plugin._mutation_lock = asyncio.Lock()
        M.Plugin._tasks = []
        M.Plugin._unloading = False
        M.Plugin._hdr_look_path = None
        M.Plugin._hdr_look_baseline = None
        M.Plugin._state.update({
            "external_display": False, "hdr_enabled": True,
            "hdr_supported": True, "hdr_saturation": 1.2,
            "hdr_applied": False,
        })
        try:
            asyncio.run(scenario())
        finally:
            let_build_finish.set()
            M._write_look = real_build
            M._write_string_atom = real_write
            M._read_string_atom = real_read
            M.DEBOUNCE_S = real_debounce
            M.Plugin._mutation_lock = real_lock
            M.Plugin._unloading = saved_unloading
            M.Plugin._tasks = []
            (M.Plugin._hdr_look_path,
             M.Plugin._hdr_look_baseline,
             M.Plugin._look_slot,
             M.Plugin._hdr_generation) = saved_fields
            M.Plugin._state.update(saved_state)
        self.assertFalse(result["hdr_enabled"])
        self.assertFalse(result["hdr_applied"])
        self.assertFalse(any(value for _atom, value in writes))

    def test_failed_sdr_replacement_keeps_the_previous_look_owned(self):
        real_build = M._write_look
        real_debounce = M.DEBOUNCE_S
        real_lock = M.Plugin._mutation_lock
        saved_mapping = (M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN)
        saved_state = dict(M.Plugin._state)
        saved_unloading = M.Plugin._unloading
        saved_fields = (M.Plugin._sdr_look_path,
                        M.Plugin._sdr_look_baseline,
                        M.Plugin._sdr_look_slot,
                        M.Plugin._sdr_look_generation)
        old = os.path.join(M._runtime_dir(), "sdr-look-0.cube")
        result = {}

        async def scenario():
            plugin = M.Plugin()
            plugin._schedule_look("sdr")
            worker = M.Plugin._tasks[-1]
            await asyncio.gather(worker, return_exceptions=True)
            result["path"] = M.Plugin._sdr_look_path
            result["applied"] = M.Plugin._state["sdr_look_applied"]

        M._write_look = lambda *_args: ""
        M.DEBOUNCE_S = 0
        M.Plugin._mutation_lock = asyncio.Lock()
        M.Plugin._tasks = []
        M.Plugin._unloading = False
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 0.0, 1.0
        M.Plugin._sdr_look_path = old
        M.Plugin._sdr_look_baseline = "/tmp/foreign-sdr.cube"
        M.Plugin._sdr_look_slot = 0
        M.Plugin._state.update({
            "external_display": False, "sdr_enabled": True,
            "sdr_saturation": 2.5, "sdr_look_applied": True,
        })
        try:
            asyncio.run(scenario())
        finally:
            M._write_look = real_build
            M.DEBOUNCE_S = real_debounce
            M.Plugin._mutation_lock = real_lock
            M.Plugin._tasks = []
            M.Plugin._unloading = saved_unloading
            M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = saved_mapping
            (M.Plugin._sdr_look_path,
             M.Plugin._sdr_look_baseline,
             M.Plugin._sdr_look_slot,
             M.Plugin._sdr_look_generation) = saved_fields
            M.Plugin._state.update(saved_state)
        self.assertEqual(result["path"], old)
        self.assertTrue(result["applied"])


class LookOwnership(unittest.TestCase):

    def setUp(self):
        self.real_read = M._read_string_atom
        self.real_write = M._write_string_atom
        self.saved = (
            M.Plugin._sdr_look_path, M.Plugin._sdr_look_baseline,
            M.Plugin._hdr_look_path, M.Plugin._hdr_look_baseline,
            M.Plugin._state["sdr_look_applied"],
            M.Plugin._state["hdr_applied"],
        )

    def tearDown(self):
        M._read_string_atom = self.real_read
        M._write_string_atom = self.real_write
        (M.Plugin._sdr_look_path, M.Plugin._sdr_look_baseline,
         M.Plugin._hdr_look_path, M.Plugin._hdr_look_baseline,
         M.Plugin._state["sdr_look_applied"],
         M.Plugin._state["hdr_applied"]) = self.saved

    def test_release_restores_the_foreign_hdr_baseline(self):
        own = os.path.join(M._runtime_dir(), "hdr-look-1.cube")
        foreign = "/tmp/foreign-hdr.cube"
        writes = []
        M.Plugin._hdr_look_path = own
        M.Plugin._hdr_look_baseline = foreign
        M.Plugin._state["hdr_applied"] = True
        M._read_string_atom = lambda atom: own
        M._write_string_atom = lambda atom, value: (
            writes.append((atom, value)) or True)
        self.assertTrue(M.Plugin._release_look("hdr"))
        self.assertEqual(writes, [(M.ATOM_LOOK_PQ, foreign)])
        self.assertFalse(M.Plugin._state["hdr_applied"])

    def test_failed_clear_keeps_ownership_for_a_retry(self):
        own = os.path.join(M._runtime_dir(), "sdr-look-1.cube")
        M.Plugin._sdr_look_path = own
        M.Plugin._sdr_look_baseline = None
        M.Plugin._state["sdr_look_applied"] = True
        M._read_string_atom = lambda atom: own
        M._write_string_atom = lambda atom, value: False
        self.assertFalse(M.Plugin._release_look("sdr"))
        self.assertEqual(M.Plugin._sdr_look_path, own)
        self.assertTrue(M.Plugin._state["sdr_look_applied"])


class ReleaseIsReallyOff(unittest.TestCase):
    """Turning the half off has to leave the picture where it was before it was
    ever turned on - on both branches, whose neutral values disagree."""

    def setUp(self):
        self.written = []
        self._write = M._write_float_atom
        self._release_look = M.Plugin.__dict__["_release_look"]
        M._write_float_atom = lambda name, value: (
            self.written.append(value) or True)
        M.Plugin._release_look = staticmethod(lambda component: None)
        M.Plugin._state.update({"sdr_applied": True, "sdr_enabled": True})

    def tearDown(self):
        M._write_float_atom = self._write
        M.Plugin._release_look = self._release_look
        M.Plugin._state.update({"sdr_applied": False, "sdr_enabled": False})
        M.Plugin._sdr_baseline = None

    def test_without_a_baseline_it_hands_the_atom_back_to_gamescope(self):
        """A negative wideness is gamescope's own "unset", and both branches of
        buildSDRColorimetry read it as that branch's native gamut. 1.0 would be
        neutral on one and the harshest setting on the other."""
        M.Plugin._sdr_baseline = None
        M.Plugin._release_sdr()
        self.assertEqual(self.written, [M.SDR_UNSET])
        self.assertLess(M.SDR_UNSET, 0.0)

    def test_a_real_baseline_is_still_restored(self):
        M.Plugin._sdr_baseline = 0.4
        M.Plugin._release_sdr()
        self.assertEqual(self.written, [0.4])

    def test_the_look_goes_first_even_if_the_atom_was_never_applied(self):
        """Both mechanisms are on at once above 200%; releasing one and not the
        other leaves half the saturation behind, which is what shipped."""
        calls = []
        M.Plugin._release_look = staticmethod(lambda component: calls.append(component))
        M.Plugin._state["sdr_applied"] = False
        M.Plugin._release_sdr()
        self.assertEqual(calls, ["sdr"])


class BaselineCapture(unittest.TestCase):

    def test_our_own_leftover_is_not_mistaken_for_a_baseline(self):
        """Reloading the plugin with the half already on used to capture what we
        wrote last time, so "off" restored our own saturation."""
        with open(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "main.py"), encoding="utf-8") as source_file:
            source = source_file.read()
        start = source.index("Remember what was there before")
        window = source[start:start + 1200]
        self.assertIn('if Plugin._state["sdr_enabled"]:', window)
        self.assertIn("Plugin._sdr_baseline = None", window)


class RequestHandling(PluginCase):
    async def test_unload_restores_before_persistence_and_bounds_xprop_wait(self):
        calls = []
        self.mock(M.Plugin, "_release_sdr", side_effect=lambda: calls.append("sdr"))
        self.mock(M.Plugin, "_release_look", side_effect=calls.append)
        self.mock(M.Plugin, "_save", side_effect=lambda: calls.append("save"))
        await self.plugin._unload()
        self.assertEqual(set(calls[:2]), {"sdr", "hdr"})
        self.assertEqual(calls[-1], "save")
        with patch.object(M.subprocess, "run", side_effect=subprocess.TimeoutExpired("xprop", 0.25)) as run:
            self.assertIsNone(M._read_int_atom(M.ATOM_IS_EXTERNAL))
            self.assertLessEqual(run.call_args.kwargs["timeout"], 0.25)

    async def test_two_hundred_requests_use_bounded_workers_and_latest_value(self):
        M.Plugin._state.update(hdr_enabled=True)
        self.mock(M, "DEBOUNCE_S", 0.005)
        for n in range(200):
            await self.plugin.set_hdr_saturation(1 + n / 400)
        self.assertLessEqual(len(M.Plugin._tasks), 2)
        await self.drain()
        self.builder.assert_called_once()
        self.assertAlmostEqual(M.Plugin._state["hdr_applied_saturation"], 1.4975)

    async def test_new_value_cancels_inflight_build_then_same_worker_applies_latest(self):
        started = threading.Event()
        builds = []
        def build(value, slot, *args):
            builds.append(value)
            if len(builds) == 1:
                started.set()
                self.assertTrue(threading.Event().wait(0.001) is False)
                for _ in range(1000):
                    if args[-1]():
                        return ""
                    threading.Event().wait(0.001)
                raise AssertionError("obsolete build was never cancelled")
            return f"/tmp/hdr-look-{slot}.cube"
        self.builder.side_effect = build
        M.Plugin._state.update(hdr_enabled=True)
        await self.plugin.set_hdr_saturation(1.1)
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        worker = M.Plugin._workers["hdr"]
        for n in range(40):
            await self.plugin.set_hdr_saturation(1.2 + n / 200)
        self.assertIs(M.Plugin._workers["hdr"], worker)
        await self.drain()
        self.assertEqual(builds, [1.1, 1.395])
        self.assertEqual(M.Plugin._state["hdr_applied_saturation"], 1.395)
        self.string_writer.assert_called_once()

    async def test_strict_rpc_inputs_and_toggle_carries_pending_value(self):
        for bad in (None, True, "1.3", float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                await self.plugin.set_hdr_saturation(bad)
        with self.assertRaises(ValueError):
            await self.plugin.set_hdr_enabled("false")
        await self.plugin.set_hdr_enabled(False, 1.35)
        self.assertEqual(M.Plugin._state["hdr_saturation"], 1.35)

class Controls(PluginCase):

    async def test_unchanged_healthy_hdr_does_not_rebuild_or_save(self):
        await self.plugin.set_hdr_saturation(1.3)
        await self.plugin.set_hdr_enabled(True)
        await self.drain()
        self.builder.reset_mock()
        M.Plugin._save.reset_mock()
        for _ in range(30):
            await self.plugin.set_hdr_saturation(1.3)
        await self.drain()
        self.builder.assert_not_called()
        M.Plugin._save.assert_not_called()
        self.assertFalse(M.Plugin._state["generating"])

    async def test_unchanged_healthy_sdr_does_not_write_or_save(self):
        await self.plugin.set_sdr_saturation(1.4)
        await self.plugin.set_sdr_enabled(True)
        await self.drain()
        self.float_writer.reset_mock()
        M.Plugin._save.reset_mock()
        for _ in range(30):
            await self.plugin.set_sdr_saturation(1.4)
        await self.drain()
        self.float_writer.assert_not_called()
        M.Plugin._save.assert_not_called()

    async def test_unchanged_value_can_retry_failed_apply_and_save(self):
        M.Plugin._save.side_effect = OSError("disk full")
        await self.plugin.set_hdr_saturation(1.3)
        self.builder.side_effect = None
        self.builder.return_value = ""
        await self.plugin.set_hdr_enabled(True)
        await self.drain()
        self.assertIn("could not", M.Plugin._state["hdr_error"])
        self.assertIn("disk full", M.Plugin._state["settings_error"])
        self.assertTrue(M.Plugin._settings_dirty)
        self.builder.return_value = "/tmp/hdr-look-1.cube"
        M.Plugin._save.side_effect = None
        M.Plugin._save.reset_mock()
        await self.plugin.set_hdr_saturation(1.3)
        await self.drain()
        self.assertEqual(M.Plugin._state["hdr_applied_saturation"], 1.3)
        self.assertEqual(M.Plugin._state["hdr_error"], "")
        self.assertEqual(M.Plugin._state["settings_error"], "")
        self.assertFalse(M.Plugin._settings_dirty)
        M.Plugin._save.assert_called_once()

    async def test_unchanged_values_rebuild_missing_hdr_and_sdr_files(self):
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 0, 1
        await self.plugin.set_sdr_saturation(2.4)
        await self.plugin.set_hdr_saturation(1.3)
        await self.plugin.set_sdr_enabled(True)
        await self.plugin.set_hdr_enabled(True)
        await self.drain()
        self.builder.reset_mock()
        self.mock(M.os.path, "isfile", return_value=False)
        await self.plugin.set_hdr_saturation(1.3)
        await self.plugin.set_sdr_saturation(2.4)
        await self.drain()
        self.assertEqual(self.builder.call_count, 2)
        self.assertEqual({call.args[2] for call in self.builder.call_args_list}, {"pq", "g22"})
        self.assertEqual(M.Plugin._state["hdr_applied_saturation"], 1.3)
        self.assertAlmostEqual(M.Plugin._state["sdr_look_saturation"], 1.2)

    async def test_unchanged_value_does_not_cancel_the_build_already_in_flight(self):
        entered, release = threading.Event(), threading.Event()
        def build(*args):
            entered.set()
            release.wait(2)
            return "/tmp/hdr-look-1.cube"
        self.builder.side_effect = build
        await self.plugin.set_hdr_saturation(1.3)
        await self.plugin.set_hdr_enabled(True)
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            generation = M.Plugin._hdr_generation
            for _ in range(30):
                await self.plugin.set_hdr_saturation(1.3)
            self.assertEqual(M.Plugin._hdr_generation, generation)
        finally:
            release.set()
            await self.drain()
        self.builder.assert_called_once()
        self.assertEqual(M.Plugin._state["hdr_applied_saturation"], 1.3)

    async def test_unchanged_sdr_value_retries_a_failed_atom_write(self):
        self.float_writer.side_effect = None
        self.float_writer.return_value = False
        await self.plugin.set_sdr_saturation(1.4)
        await self.plugin.set_sdr_enabled(True)
        await self.drain()
        self.assertFalse(M.Plugin._state["sdr_applied"])
        self.float_writer.side_effect = self.write_float
        await self.plugin.set_sdr_saturation(1.4)
        await self.drain()
        self.assertEqual(self.live_sdr, 1.4)
        self.assertTrue(M.Plugin._state["sdr_applied"])
        self.assertEqual(M.Plugin._state["sdr_error"], "")

    async def test_foreign_look_changed_during_owned_replacement_is_restored(self):
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 0, 1
        for component, atom, first, second, setter, enable in (
            ("hdr", M.ATOM_LOOK_PQ, 1.2, 1.4, self.plugin.set_hdr_saturation, self.plugin.set_hdr_enabled),
            ("sdr", M.ATOM_LOOK_G22, 2.2, 2.6, self.plugin.set_sdr_saturation, self.plugin.set_sdr_enabled),
        ):
            with self.subTest(component=component):
                self.strings[atom] = "/tmp/foreign-old.cube"
                self.builder.side_effect = lambda value, slot, *args: f"/tmp/{component}-look-{slot}.cube"
                await setter(first)
                await enable(True)
                await self.drain()
                def build(value, slot, *args):
                    self.strings[atom] = "/tmp/foreign-new.cube"
                    return f"/tmp/{component}-look-{slot}.cube"
                self.builder.side_effect = build
                await setter(second)
                await self.drain()
                await enable(False)
                self.assertEqual(self.strings[atom], "/tmp/foreign-new.cube")

    async def test_foreign_looks_changed_before_monitor_or_resume_are_restored(self):
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 0, 1
        for recover in (M.Plugin._reassert_looks_if_dropped, M.Plugin._reassert):
            with self.subTest(recover=recover.__name__):
                for atom in (M.ATOM_LOOK_G22, M.ATOM_LOOK_PQ):
                    self.strings[atom] = "/tmp/foreign-old.cube"
                await self.plugin.set_sdr_saturation(2.4)
                await self.plugin.set_hdr_saturation(1.3)
                await self.plugin.set_sdr_enabled(True)
                await self.plugin.set_hdr_enabled(True)
                await self.drain()
                for atom in (M.ATOM_LOOK_G22, M.ATOM_LOOK_PQ):
                    self.strings[atom] = "/tmp/foreign-new.cube"
                recover()
                await self.plugin.reset()
                for atom in (M.ATOM_LOOK_G22, M.ATOM_LOOK_PQ):
                    self.assertEqual(self.strings[atom], "/tmp/foreign-new.cube", atom)

    async def test_dropped_atoms_and_own_stale_slots_cannot_replace_foreign_baselines(self):
        self.mock(M, "_runtime_dir", return_value="/tmp")
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 0, 1
        self.strings.update({M.ATOM_LOOK_G22: "/tmp/foreign.cube", M.ATOM_LOOK_PQ: "/tmp/foreign.cube"})
        await self.plugin.set_sdr_saturation(2.4)
        await self.plugin.set_hdr_saturation(1.3)
        await self.plugin.set_sdr_enabled(True)
        await self.plugin.set_hdr_enabled(True)
        await self.drain()
        for atom in (M.ATOM_LOOK_G22, M.ATOM_LOOK_PQ):
            self.strings.pop(atom)
        M.Plugin._reassert()
        self.strings[M.ATOM_LOOK_G22] = "/tmp/sdr-look-0.cube"
        self.strings[M.ATOM_LOOK_PQ] = "/tmp/hdr-look-0.cube"
        M.Plugin._reassert_looks_if_dropped()
        await self.plugin.reset()
        for atom in (M.ATOM_LOOK_G22, M.ATOM_LOOK_PQ):
            self.assertEqual(self.strings[atom], "/tmp/foreign.cube", atom)


class SlotReuse(PluginCase):

    async def check_slot_reuse_through_rpc(self, component, values, atom):
        with tempfile.TemporaryDirectory() as folder:
            self.mock(M, "_runtime_dir", return_value=folder)
            self.mock(M, "LUT_SIZE", 5)
            self.mock(M, "_look_file_cache", OrderedDict())
            self.mock(M, "_write_look", side_effect=WRITE_LOOK)
            generate = self.mock(V, "generate", wraps=V.generate)
            await getattr(self.plugin, f"set_{component}_enabled")(True, values[0])
            await self.drain()
            first = self.strings[atom]
            original = Path(first).read_bytes()
            await getattr(self.plugin, f"set_{component}_saturation")(values[1])
            await self.drain()
            self.assertNotEqual(self.strings[atom], first)
            await getattr(self.plugin, f"set_{component}_saturation")(values[0])
            await self.drain()
            self.assertEqual(self.strings[atom], first)
            self.assertEqual(Path(first).read_bytes(), original)
            self.assertEqual(generate.call_count, 2)
            self.assertEqual(self.string_writer.call_count, 3)

    async def test_hdr_rpc_reuses_previous_inactive_slot_and_applies_it(self):
        await self.check_slot_reuse_through_rpc("hdr", (1.1, 1.3), M.ATOM_LOOK_PQ)
        self.assertEqual(M.Plugin._state["hdr_applied_saturation"], 1.1)
        self.assertFalse(M.Plugin._state["generating"])

    async def test_sdr_rpc_reuses_previous_inactive_slot_and_applies_it(self):
        self.mock(M, "SDR_NEUTRAL_ATOM", 0.0)
        self.mock(M, "SDR_ATOM_SPAN", 1.0)
        await self.check_slot_reuse_through_rpc("sdr", (2.2, 2.6), M.ATOM_LOOK_G22)
        self.assertAlmostEqual(M.Plugin._state["sdr_look_saturation"], 1.1)
        self.assertFalse(M.Plugin._state["sdr_generating"])


class ReassertFailures(PluginCase):

    async def prepare_looks(self):
        self.mock(M, "SDR_NEUTRAL_ATOM", 0.0)
        self.mock(M, "SDR_ATOM_SPAN", 1.0)
        await self.plugin.set_sdr_enabled(True, 2.4)
        await self.plugin.set_hdr_enabled(True, 1.3)
        await self.drain()
        self.string_writer.reset_mock()

    async def test_resume_write_failure_is_reported_and_monitor_recovers(self):
        await self.prepare_looks()
        self.string_writer.side_effect = lambda *_: False
        M.Plugin._reassert()
        self.assertTrue(M.Plugin._state["sdr_look_error"], "failed SDR reassert was ignored")
        self.assertTrue(M.Plugin._state["hdr_error"], "failed HDR reassert was ignored")
        self.string_writer.side_effect = self.write_string
        await self.plugin._watch_tick()
        await self.drain()
        self.assertEqual(M.Plugin._state["sdr_look_error"], "")
        self.assertEqual(M.Plugin._state["hdr_error"], "")

    async def test_dropped_look_write_failure_is_reported_and_resume_recovers(self):
        await self.prepare_looks()
        self.strings = {M.ATOM_LOOK_PQ: "/tmp/foreign-pq.cube",
                        M.ATOM_LOOK_G22: "/tmp/foreign-g22.cube"}
        self.string_writer.side_effect = lambda *_: False
        M.Plugin._reassert_looks_if_dropped()
        self.assertTrue(M.Plugin._state["sdr_look_error"], "failed monitor write was ignored")
        self.assertTrue(M.Plugin._state["hdr_error"], "failed monitor write was ignored")
        self.string_writer.side_effect = self.write_string
        M.Plugin._reassert()
        self.assertEqual(M.Plugin._state["sdr_look_error"], "")
        self.assertEqual(M.Plugin._state["hdr_error"], "")
        await self.plugin.reset()
        self.assertEqual(self.strings[M.ATOM_LOOK_G22], "/tmp/foreign-g22.cube")
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/tmp/foreign-pq.cube")

    async def test_reasserting_old_looks_cannot_hide_failed_newer_replacements(self):
        await self.prepare_looks()
        self.builder.side_effect = lambda *_: ""
        await self.plugin.set_sdr_saturation(2.8)
        await self.plugin.set_hdr_saturation(1.4)
        await self.drain()
        sdr_error = M.Plugin._state["sdr_look_error"]
        hdr_error = M.Plugin._state["hdr_error"]
        M.Plugin._reassert()
        self.assertEqual(M.Plugin._state["sdr_look_error"], sdr_error)
        self.assertEqual(M.Plugin._state["hdr_error"], hdr_error)
        self.assertAlmostEqual(M.Plugin._state["sdr_look_saturation"], 1.2)
        self.assertEqual(M.Plugin._state["hdr_applied_saturation"], 1.3)

    async def test_successful_monitor_reassert_of_old_look_keeps_replacement_error(self):
        await self.prepare_looks()
        self.builder.side_effect = lambda *_: ""
        await self.plugin.set_sdr_saturation(2.8)
        await self.plugin.set_hdr_saturation(1.4)
        await self.drain()
        errors = (M.Plugin._state["sdr_look_error"], M.Plugin._state["hdr_error"])
        self.strings = {}
        M.Plugin._reassert_looks_if_dropped()
        self.assertEqual((M.Plugin._state["sdr_look_error"], M.Plugin._state["hdr_error"]), errors)

    async def test_failure_on_one_atom_does_not_mark_the_other_look_failed(self):
        await self.prepare_looks()
        def write(atom, value):
            return False if atom == M.ATOM_LOOK_PQ else self.write_string(atom, value)
        self.string_writer.side_effect = write
        M.Plugin._reassert()
        self.assertEqual(M.Plugin._state["sdr_look_error"], "")
        self.assertTrue(M.Plugin._state["hdr_error"])

    async def check_neutral_recovery(self, reassert):
        await self.prepare_looks()
        self.string_writer.side_effect = lambda *_: False
        await self.plugin.set_sdr_saturation(2.0)
        await self.plugin.set_hdr_saturation(1.0)
        await self.drain()
        self.assertTrue(M.Plugin._state["sdr_look_error"])
        self.assertTrue(M.Plugin._state["hdr_error"])
        self.builder.reset_mock()
        self.string_writer.side_effect = self.write_string
        reassert()
        self.assertEqual(self.strings[M.ATOM_LOOK_G22], "", "old SDR look was reasserted after neutral request")
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "", "old HDR look was reasserted after neutral request")
        self.assertEqual(M.Plugin._state["sdr_look_error"], "")
        self.assertEqual(M.Plugin._state["hdr_error"], "")
        self.builder.assert_not_called()

    async def test_resume_retries_a_pending_neutral_clear_instead_of_old_look(self):
        await self.check_neutral_recovery(M.Plugin._reassert)

    async def test_monitor_retries_a_pending_neutral_clear_instead_of_old_look(self):
        def dropped():
            self.strings = {}
            M.Plugin._reassert_looks_if_dropped()
        await self.check_neutral_recovery(dropped)

    async def test_monitor_clears_neutral_even_if_the_old_path_is_still_present(self):
        await self.check_neutral_recovery(M.Plugin._reassert_looks_if_dropped)
