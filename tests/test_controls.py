# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""RPC inputs, applying SDR/HDR, look ownership and worker coalescing."""

import asyncio
import json
import os
import subprocess
import tempfile
import threading
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _fixtures import PATH_EXISTS, PluginCase, READ_STRING_ATOM, SAVE_SETTINGS, WRITE_LOOK, drain, edid_with_red  # installs the loader stand-ins
import main  # noqa: E402
import main as M  # noqa: E402
import vibrance_lut as V  # noqa: E402


class Clamping(unittest.TestCase):

    RANGES = (("sdr", "SDR_MIN", "SDR_SOFT_WIDE", "SDR_MAX_WIDE"),
              ("hdr", "HDR_MIN", "HDR_SOFT_LIMIT", "HDR_MAX"))

    def _bounds(self):
        for half, lo, soft, hi in self.RANGES:
            yield half, getattr(main, lo), getattr(main, soft), getattr(main, hi)

    def test_each_range_is_enforced(self):
        for half, lo, _soft, hi in self._bounds():
            self.assertEqual(main._clamp(-1.0, lo, hi), lo, half)
            self.assertEqual(main._clamp(99.0, lo, hi), hi, half)
            self.assertEqual(main._clamp(lo, lo, hi), lo, half)
            self.assertEqual(main._clamp(hi, lo, hi), hi, half)

    def test_neither_half_can_desaturate(self):
        """The floor is neutral on both. Below it the picture is being drained,
        which is the one thing this plugin is not for."""
        for half, lo, _soft, hi in self._bounds():
            self.assertEqual(lo, 1.0, half)
            self.assertEqual(main._clamp(0.0, lo, hi), 1.0, half)
            self.assertEqual(main._clamp(0.999, lo, hi), 1.0, half)

    def test_default_is_the_floor_of_both(self):
        for half, lo, _soft, _hi in self._bounds():
            self.assertEqual(main.SAT_DEFAULT, lo, half)

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
            ("sdr_min", main.SDR_MIN),
            ("sdr_max", main.SDR_MAX_WIDE),
            ("sdr_soft_limit", main.SDR_SOFT_WIDE),
            ("hdr_min", main.HDR_MIN),
            ("hdr_max", main.HDR_MAX),
            ("hdr_soft_limit", main.HDR_SOFT_LIMIT),
        ):
            self.assertIn(key, main.Plugin._state)
            self.assertEqual(main.Plugin._state[key], constant, key)

    def test_the_requested_ranges_are_what_shipped(self):
        """Pinned to literals on purpose. Everything else here is relative, so
        without this a tuning pass could hand back half the range and the suite
        would stay green."""
        self.assertEqual(main.SDR_MIN, 1.0)
        self.assertEqual(main.SDR_MAX_WIDE, 3.0)
        self.assertEqual(main.SDR_MAX_STANDARD, 3.0)
        self.assertEqual(main.HDR_MIN, 1.0)
        self.assertEqual(main.HDR_MAX, 1.5)

    def test_hdr_ceiling_stays_below_the_sdr_one(self):
        """Different mechanisms, different useful ranges - if these ever become
        one number again the LUT half is the one that suffers."""
        self.assertLess(main.HDR_MAX, main.SDR_MAX_WIDE)


class Setters(unittest.TestCase):
    """_clamp is exercised above in isolation; this is about its call sites.

    Passing one half's bounds to the other half's setter is a single-token slip
    that nothing else in this file would notice.
    """

    def setUp(self):
        self.plugin = main.Plugin()
        for key in ("_disconnected", "_settings_loaded"):
            fixture = patch.object(main.Plugin, key, key == "_settings_loaded")
            fixture.start()
            self.addCleanup(fixture.stop)
        main.Plugin._state["sdr_enabled"] = False
        main.Plugin._state["hdr_enabled"] = False
        main.Plugin._state["sdr_max"] = main.SDR_MAX_WIDE

    def test_sdr_setter_uses_the_sdr_ceiling(self):
        state = drain(self.plugin.set_sdr_saturation(main.SDR_MAX_WIDE))
        self.assertEqual(state["sdr_saturation"], main.SDR_MAX_WIDE)

    def test_hdr_setter_does_not_get_the_sdr_ceiling(self):
        state = drain(self.plugin.set_hdr_saturation(main.SDR_MAX_WIDE))
        self.assertEqual(state["hdr_saturation"], main.HDR_MAX)

    def test_neither_setter_accepts_desaturation(self):
        self.assertEqual(
            drain(self.plugin.set_sdr_saturation(0.0))["sdr_saturation"], 1.0)
        self.assertEqual(
            drain(self.plugin.set_hdr_saturation(0.0))["hdr_saturation"], 1.0)

    def test_saved_values_from_an_older_range_are_lifted_on_load(self):
        """1.0.0 allowed 0.0. Without the migration those users come back to a
        drained picture the new UI cannot even represent."""
        main.settings._store.update({
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
            main.settings._store.clear()
        self.assertEqual(main.Plugin._state["sdr_saturation"], 1.0)
        self.assertEqual(main.Plugin._state["hdr_saturation"], 1.0)


class SerializedMutations(unittest.TestCase):
    """An older RPC must never commit after a newer toggle has finished."""

    def test_sdr_enable_cannot_overtake_disable(self):
        calls = []
        apply_started = threading.Event()
        let_apply_finish = threading.Event()
        result = {}
        real_save = main.Plugin._save
        real_apply = main.Plugin._apply_sdr
        real_release = main.Plugin._release_sdr
        real_schedule = main.Plugin._schedule_look
        real_lock = main.Plugin._mutation_lock
        real_read = main._read_float_atom
        saved_state = dict(main.Plugin._state)
        saved_unloading = main.Plugin._unloading
        saved_disconnected = main.Plugin._disconnected
        saved_baseline = main.Plugin._sdr_baseline

        def fake_save(values=None):
            pass

        def fake_apply():
            apply_started.set()
            let_apply_finish.wait(2)
            calls.append("apply")
            main.Plugin._state["sdr_applied"] = True

        def fake_release():
            calls.append("release")
            main.Plugin._state["sdr_applied"] = False
            return True

        async def scenario():
            plugin = main.Plugin()
            enabling = asyncio.create_task(plugin.set_sdr_enabled(True))
            self.assertTrue(await asyncio.to_thread(apply_started.wait, 1))
            disabling = asyncio.create_task(plugin.set_sdr_enabled(False))
            await asyncio.sleep(0)
            self.assertFalse(disabling.done())
            let_apply_finish.set()
            await asyncio.gather(enabling, disabling)
            await asyncio.gather(*list(main.Plugin._tasks))
            result.update(main.Plugin._state)

        loaded = patch.object(main.Plugin, "_settings_loaded", True)
        loaded.start()
        self.addCleanup(loaded.stop)
        main.Plugin._save = staticmethod(fake_save)
        main.Plugin._apply_sdr = staticmethod(fake_apply)
        main.Plugin._release_sdr = staticmethod(fake_release)
        main.Plugin._schedule_look = lambda self, component: None
        main.Plugin._mutation_lock = asyncio.Lock()
        main.Plugin._unloading = False
        main.Plugin._disconnected = False
        main._read_float_atom = lambda atom: 0.8
        main.Plugin._state.update({
            "external_display": False, "sdr_enabled": False,
            "sdr_applied": False,
        })
        try:
            asyncio.run(scenario())
        finally:
            let_apply_finish.set()
            main.Plugin._save = real_save
            main.Plugin._apply_sdr = real_apply
            main.Plugin._release_sdr = real_release
            main.Plugin._schedule_look = real_schedule
            main.Plugin._mutation_lock = real_lock
            main.Plugin._unloading = saved_unloading
            main.Plugin._disconnected = saved_disconnected
            main.Plugin._sdr_baseline = saved_baseline
            main._read_float_atom = real_read
            main.Plugin._state.update(saved_state)
        self.assertEqual(calls, ["apply", "release"])
        self.assertFalse(result["sdr_enabled"])
        self.assertFalse(result["sdr_applied"])


class SdrLookSplit(unittest.TestCase):
    """EXPERIMENTAL, Legion Go S only. The atom carries what it can and the
    look carries the rest - and on a wide-gamut panel there is never a rest, so
    a Legion Go 2 must come out of this untouched."""

    def setUp(self):
        self.saved = (main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN)

    def tearDown(self):
        main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = self.saved

    def _wide(self):
        main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = 1.0, 2.0

    def _standard(self):
        main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = 0.0, 1.0

    def test_a_wide_gamut_panel_never_gets_a_look(self):
        """The whole point of the gate: nothing about the Go 2 changes."""
        self._wide()
        for value in (1.0, 1.5, 2.0, 2.5, 3.0):
            atom, look = main._sdr_split(value)
            self.assertEqual(look, 1.0, value)
            self.assertAlmostEqual(atom, value, msg=str(value))

    def test_a_standard_panel_fills_the_atom_first(self):
        self._standard()
        for value, expected in ((1.0, 0.0), (1.5, 0.5), (2.0, 1.0)):
            atom, look = main._sdr_split(value)
            self.assertAlmostEqual(atom, expected, msg=str(value))
            self.assertEqual(look, 1.0, value)

    def test_the_look_only_starts_where_cfit_stops(self):
        """cfit() clamps at atom 1.0, so below 200% the look must stay out."""
        self._standard()
        self.assertEqual(main._sdr_split(1.99)[1], 1.0)
        self.assertGreater(main._sdr_split(2.01)[1], 1.0)

    def test_the_atom_never_goes_past_where_it_responds(self):
        self._standard()
        for value in (2.0, 2.5, 3.0):
            self.assertAlmostEqual(main._sdr_split(value)[0], 1.0, msg=str(value))

    def test_the_top_of_the_slider_asks_the_look_for_its_ceiling(self):
        self._standard()
        self.assertAlmostEqual(
            main._sdr_split(main.SDR_MAX_STANDARD)[1],
            main.SDR_LOOK_CEILING)

    def test_raising_the_maximum_did_not_weaken_what_was_below_it(self):
        """The look is a rate, not a fraction of the range, so 300% asks for
        exactly what it asked for when 300% was the top of the slider."""
        self._standard()
        self.assertAlmostEqual(main._sdr_split(3.0)[1], 1.5)
        self.assertAlmostEqual(main._sdr_split(2.5)[1], 1.25)

    def test_the_look_ramps_rather_than_jumping(self):
        self._standard()
        looks = [main._sdr_split(v)[1] for v in (2.0, 2.25, 2.5, 2.75, 3.0)]
        for earlier, later in zip(looks, looks[1:]):
            self.assertGreater(later, earlier)

    def test_the_look_ceiling_matches_the_hdr_one(self):
        """Same code, same clipping, so the same ceiling."""
        self.assertEqual(main.SDR_LOOK_CEILING, main.HDR_MAX)

    def test_the_ceiling_is_never_asked_for_more_than_it_allows(self):
        self._standard()
        for value in (4.0, 6.0, 99.0):
            self.assertLessEqual(main._sdr_split(value)[1],
                                 main.SDR_LOOK_CEILING, str(value))

    def test_the_warning_sits_where_the_hdr_half_warns(self):
        """Identical maths, so the look value that starts clipping is the same;
        only the slider position it corresponds to differs."""
        self._standard()
        at_warning = main._sdr_split(main.SDR_SOFT_STANDARD)[1]
        self.assertAlmostEqual(at_warning, main.HDR_SOFT_LIMIT, places=6)


class StaleLookJobs(unittest.TestCase):
    """A worker may finish its file, but no stale worker may commit an atom."""

    def setUp(self):
        loaded = patch.object(main.Plugin, "_settings_loaded", True)
        loaded.start()
        self.addCleanup(loaded.stop)

    def test_hdr_worker_finishing_after_disable_cannot_restore_the_look(self):
        build_started = threading.Event()
        let_build_finish = threading.Event()
        writes = []
        result = {}
        real_build = main._write_look
        real_write = main._write_string_atom
        real_read = main._read_string_atom
        real_debounce = main.DEBOUNCE_S
        real_lock = main.Plugin._mutation_lock
        saved_state = dict(main.Plugin._state)
        saved_unloading = main.Plugin._unloading
        saved_fields = (main.Plugin._hdr_look_path,
                        main.Plugin._hdr_look_baseline,
                        main.Plugin._look_slot,
                        main.Plugin._hdr_generation)

        def late_build(*_args):
            build_started.set()
            let_build_finish.wait(2)
            return os.path.join(main._runtime_dir(), "hdr-look-1.cube")

        async def scenario():
            plugin = main.Plugin()
            plugin._schedule_look("hdr")
            worker = main.Plugin._tasks[-1]
            while not build_started.is_set():
                await asyncio.sleep(0.005)
            await plugin.set_hdr_enabled(False)
            let_build_finish.set()
            await asyncio.gather(worker, return_exceptions=True)
            result.update(main.Plugin._state)

        main._write_look = late_build
        main._write_string_atom = lambda atom, value: (
            writes.append((atom, value)) or True)
        main._read_string_atom = lambda atom: None
        main.DEBOUNCE_S = 0
        main.Plugin._mutation_lock = asyncio.Lock()
        main.Plugin._tasks = []
        main.Plugin._unloading = False
        main.Plugin._hdr_look_path = None
        main.Plugin._hdr_look_baseline = None
        main.Plugin._state.update({
            "external_display": False, "hdr_enabled": True,
            "hdr_supported": True, "hdr_saturation": 1.2,
            "hdr_applied": False,
        })
        try:
            asyncio.run(scenario())
        finally:
            let_build_finish.set()
            main._write_look = real_build
            main._write_string_atom = real_write
            main._read_string_atom = real_read
            main.DEBOUNCE_S = real_debounce
            main.Plugin._mutation_lock = real_lock
            main.Plugin._unloading = saved_unloading
            main.Plugin._tasks = []
            (main.Plugin._hdr_look_path,
             main.Plugin._hdr_look_baseline,
             main.Plugin._look_slot,
             main.Plugin._hdr_generation) = saved_fields
            main.Plugin._state.update(saved_state)
        self.assertFalse(result["hdr_enabled"])
        self.assertFalse(result["hdr_applied"])
        self.assertFalse(any(value for _atom, value in writes))

    def test_failed_sdr_replacement_keeps_the_previous_look_owned(self):
        real_build = main._write_look
        real_debounce = main.DEBOUNCE_S
        real_lock = main.Plugin._mutation_lock
        saved_mapping = (main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN)
        saved_state = dict(main.Plugin._state)
        saved_unloading = main.Plugin._unloading
        saved_fields = (main.Plugin._sdr_look_path,
                        main.Plugin._sdr_look_baseline,
                        main.Plugin._sdr_look_slot,
                        main.Plugin._sdr_look_generation)
        old = os.path.join(main._runtime_dir(), "sdr-look-0.cube")
        result = {}

        async def scenario():
            plugin = main.Plugin()
            plugin._schedule_look("sdr")
            worker = main.Plugin._tasks[-1]
            await asyncio.gather(worker, return_exceptions=True)
            result["path"] = main.Plugin._sdr_look_path
            result["applied"] = main.Plugin._state["sdr_look_applied"]

        main._write_look = lambda *_args: ""
        main.DEBOUNCE_S = 0
        main.Plugin._mutation_lock = asyncio.Lock()
        main.Plugin._tasks = []
        main.Plugin._unloading = False
        main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = 0.0, 1.0
        main.Plugin._sdr_look_path = old
        main.Plugin._sdr_look_baseline = "/tmp/foreign-sdr.cube"
        main.Plugin._sdr_look_slot = 0
        main.Plugin._state.update({
            "external_display": False, "sdr_enabled": True,
            "sdr_saturation": 2.5, "sdr_look_applied": True,
        })
        try:
            asyncio.run(scenario())
        finally:
            main._write_look = real_build
            main.DEBOUNCE_S = real_debounce
            main.Plugin._mutation_lock = real_lock
            main.Plugin._tasks = []
            main.Plugin._unloading = saved_unloading
            main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = saved_mapping
            (main.Plugin._sdr_look_path,
             main.Plugin._sdr_look_baseline,
             main.Plugin._sdr_look_slot,
             main.Plugin._sdr_look_generation) = saved_fields
            main.Plugin._state.update(saved_state)
        self.assertEqual(result["path"], old)
        self.assertTrue(result["applied"])


class LookOwnership(unittest.TestCase):

    def setUp(self):
        self.real_read = main._read_string_atom
        self.real_write = main._write_string_atom
        self.saved = (
            main.Plugin._sdr_look_path, main.Plugin._sdr_look_baseline,
            main.Plugin._hdr_look_path, main.Plugin._hdr_look_baseline,
            main.Plugin._state["sdr_look_applied"],
            main.Plugin._state["hdr_applied"],
        )

    def tearDown(self):
        main._read_string_atom = self.real_read
        main._write_string_atom = self.real_write
        (main.Plugin._sdr_look_path, main.Plugin._sdr_look_baseline,
         main.Plugin._hdr_look_path, main.Plugin._hdr_look_baseline,
         main.Plugin._state["sdr_look_applied"],
         main.Plugin._state["hdr_applied"]) = self.saved

    def test_release_restores_the_foreign_hdr_baseline(self):
        own = os.path.join(main._runtime_dir(), "hdr-look-1.cube")
        foreign = "/tmp/foreign-hdr.cube"
        writes = []
        main.Plugin._hdr_look_path = own
        main.Plugin._hdr_look_baseline = foreign
        main.Plugin._state["hdr_applied"] = True
        main._read_string_atom = lambda atom: own
        main._write_string_atom = lambda atom, value: (
            writes.append((atom, value)) or True)
        self.assertTrue(main.Plugin._release_look("hdr"))
        self.assertEqual(writes, [(main.ATOM_LOOK_PQ, foreign)])
        self.assertFalse(main.Plugin._state["hdr_applied"])

    def test_failed_clear_keeps_ownership_for_a_retry(self):
        own = os.path.join(main._runtime_dir(), "sdr-look-1.cube")
        main.Plugin._sdr_look_path = own
        main.Plugin._sdr_look_baseline = None
        main.Plugin._state["sdr_look_applied"] = True
        main._read_string_atom = lambda atom: own
        main._write_string_atom = lambda atom, value: False
        self.assertFalse(main.Plugin._release_look("sdr"))
        self.assertEqual(main.Plugin._sdr_look_path, own)
        self.assertTrue(main.Plugin._state["sdr_look_applied"])


class ReleaseIsReallyOff(unittest.TestCase):
    """Turning the half off has to leave the picture where it was before it was
    ever turned on - on both branches, whose neutral values disagree."""

    def setUp(self):
        self.written = []
        self._write = main._write_float_atom
        self._release_look = main.Plugin.__dict__["_release_look"]
        main._write_float_atom = lambda name, value: (
            self.written.append(value) or True)
        main.Plugin._release_look = staticmethod(lambda component: None)
        main.Plugin._state.update({"sdr_applied": True, "sdr_enabled": True})

    def tearDown(self):
        main._write_float_atom = self._write
        main.Plugin._release_look = self._release_look
        main.Plugin._state.update({"sdr_applied": False, "sdr_enabled": False})
        main.Plugin._sdr_baseline = None

    def test_without_a_baseline_it_hands_the_atom_back_to_gamescope(self):
        """A negative wideness is gamescope's own "unset", and both branches of
        buildSDRColorimetry read it as that branch's native gamut. 1.0 would be
        neutral on one and the harshest setting on the other."""
        main.Plugin._sdr_baseline = None
        main.Plugin._release_sdr()
        self.assertEqual(self.written, [main.SDR_UNSET])
        self.assertLess(main.SDR_UNSET, 0.0)

    def test_a_real_baseline_is_still_restored(self):
        main.Plugin._sdr_baseline = 0.4
        main.Plugin._release_sdr()
        self.assertEqual(self.written, [0.4])

    def test_the_look_goes_first_even_if_the_atom_was_never_applied(self):
        """Both mechanisms are on at once above 200%; releasing one and not the
        other leaves half the saturation behind, which is what shipped."""
        calls = []
        main.Plugin._release_look = staticmethod(lambda component: calls.append(component))
        main.Plugin._state["sdr_applied"] = False
        main.Plugin._release_sdr()
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


class Persistence(PluginCase):
    async def test_disk_full_does_not_prevent_disable_or_restore(self):
        await self.plugin.set_sdr_enabled(True)
        self.mock(M.Plugin, "_save", side_effect=OSError("disk full"))
        await self.plugin.set_sdr_enabled(False)
        await self.drain()
        self.assertFalse(M.Plugin._state["sdr_applied"])
        self.assertEqual(self.live_sdr, 0.8)
        self.assertIn("disk full", M.Plugin._state["settings_error"])
        self.assertTrue(M.Plugin._settings_dirty)

    async def test_disabled_hdr_slider_is_persisted(self):
        await self.plugin.set_hdr_saturation(1.3)
        await self.drain()
        M.Plugin._save.assert_called_once()
        self.builder.assert_not_called()

    async def test_unload_flushes_a_pending_slider_value(self):
        self.mock(M, "SDR_COALESCE_S", 5)
        saved = []
        self.mock(M.Plugin, "_save", side_effect=lambda: saved.append(dict(M.Plugin._state)))
        await self.plugin.set_hdr_saturation(1.4)
        await self.plugin._unload()
        self.assertEqual(saved[-1]["hdr_saturation"], 1.4)
        self.assertEqual(len(saved), 1)

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

    async def test_late_rpc_after_unload_cannot_change_flushed_settings(self):
        M.Plugin._unloading = True
        before = dict(M.Plugin._state)
        await self.plugin.set_sdr_saturation(1.4)
        await self.plugin.set_hdr_saturation(1.4)
        await self.plugin.set_sdr_enabled(True, 1.4)
        await self.plugin.set_hdr_enabled(False, 1.4)
        await self.plugin.reset()
        self.assertEqual(M.Plugin._state, before)
        self.assertEqual(M.Plugin._tasks, [])

    async def test_both_sliders_share_one_settings_write(self):
        await self.plugin.set_sdr_saturation(1.4)
        await self.plugin.set_hdr_saturation(1.3)
        await self.drain()
        M.Plugin._save.assert_called_once()

    async def test_same_server_conflict_is_not_relabelled_as_another_server(self):
        M.Plugin._state.update(sdr_enabled=True, sdr_applied=True,
                              conflict="another plugin keeps overwriting", sdr_saturation=1.4)
        M.Plugin._last_sdr_written = self.live_sdr = 1.4
        M.Plugin._watch_once()
        self.assertEqual(M.Plugin._state["conflict"], "")

    async def test_unsupported_panel_releases_hdr_and_cancels_work(self):
        M.Plugin._state.update(hdr_enabled=True)
        M.Plugin._hdr_look_path = "/tmp/hdr-look-0.cube"
        M.Plugin._state["hdr_applied"] = True
        self.mock(M.Plugin, "_configure_for_panel", side_effect=lambda *args:
                  M.Plugin._state.update(hdr_supported=False) or True)
        await self.plugin._refresh_panel()
        self.assertIsNone(M.Plugin._hdr_look_path)
        self.builder.assert_not_called()

    async def test_disconnected_session_is_rediscovered_without_another_monitor(self):
        M.Plugin._disconnect()
        M.Plugin._state.update(sdr_enabled=True, sdr_saturation=1.4)
        self.mock(M, "_pick_display", return_value=":2")
        self.mock(M, "_count_gamescope_atoms", return_value=44)
        self.mock(M, "GAMESCOPE_SETTLE_S", 0)
        self.mock(M.Plugin, "_configure_for_panel", return_value=False)
        self.mock(M.Plugin, "_capture_existing_looks")
        self.mock(self.plugin, "_reassert_repeatedly", return_value=None)
        await self.plugin._start_when_ready(start_monitor=False)
        self.assertTrue(M.Plugin._state["ready"])
        self.assertFalse(M.Plugin._disconnected)
        self.assertAlmostEqual(self.live_sdr, 1.4)
        await self.drain()

    async def test_monitor_detects_hdr_only_disconnect_and_invokes_recovery(self):
        M.Plugin._state.update(sdr_enabled=False, hdr_enabled=True, hdr_applied=True)
        M.Plugin._hdr_look_path = "/tmp/old.cube"
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 0)
        self.mock(M.Plugin, "_monitor_probe", return_value=(0, None))
        recovery = self.mock(self.plugin, "_start_when_ready", side_effect=asyncio.CancelledError())
        self.mock(M, "SUSPEND_POLL_S", 0)
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._monitor_loop()
        recovery.assert_awaited_once_with(start_monitor=False)
        self.assertFalse(M.Plugin._state["ready"])
        # Retain ownership until recovery identifies the old or a new server.
        self.assertEqual(M.Plugin._hdr_look_path, "/tmp/old.cube")

    async def test_timed_out_monitor_probe_is_not_a_disconnect(self):
        M.Plugin._disconnected = False
        M.Plugin._state["ready"] = True
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 0)
        self.mock(M, "SUSPEND_POLL_S", 0)
        timeout = subprocess.TimeoutExpired("xprop", M._xprop_timeout)
        self.mock(M.subprocess, "run", side_effect=[
            timeout, timeout, SimpleNamespace(stdout="GAMESCOPE_FOCUSED_APP(CARDINAL) = 0")])
        recovery = self.mock(self.plugin, "_start_when_ready")
        refresh = self.mock(self.plugin, "_refresh_panel", side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._monitor_loop()
        recovery.assert_not_awaited()
        refresh.assert_awaited_once()
        self.assertEqual(M.subprocess.run.call_count, 3)
        self.assertTrue(M.Plugin._state["ready"])
        self.assertFalse(M.Plugin._disconnected)

    async def test_repeated_resume_events_share_one_worker_and_restart_the_ladder(self):
        self.mock(M, "REASSERT_DELAYS_S", (0.02, 0))
        applied = self.mock(M.Plugin, "_reassert")
        self.plugin._schedule_reassert(self.plugin._reassert_repeatedly)
        await asyncio.sleep(0)
        worker = M.Plugin._workers["reassert"]
        for _ in range(100):
            self.plugin._schedule_reassert(self.plugin._reassert_repeatedly)
        self.assertEqual(M.Plugin._tasks, [worker])
        await self.drain()
        self.assertEqual(applied.call_count, 2)

    async def test_zero_atoms_during_settle_never_become_ready(self):
        self.mock(M, "_pick_display", return_value=":0")
        self.mock(M, "_count_gamescope_atoms", side_effect=[0, 44, 44])
        self.mock(M, "GAMESCOPE_SETTLE_S", 0)
        self.mock(M, "GAMESCOPE_RETRY_FIRST_S", 0)
        self.mock(M.Plugin, "_capture_existing_looks")
        self.mock(M.Plugin, "_configure_for_panel", return_value=False)
        self.mock(self.plugin, "_reassert_repeatedly", return_value=None)
        await self.plugin._start_when_ready(start_monitor=False)
        self.assertEqual(M._pick_display.call_count, 2)
        self.assertTrue(M.Plugin._state["ready"])
        await self.drain()


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


class Latency(PluginCase):

    async def test_slow_save_does_not_block_disable_reset_or_other_slider(self):
        await self.plugin.set_sdr_enabled(True)
        await self.drain()
        entered, release = threading.Event(), threading.Event()
        def save(*args):
            entered.set()
            release.wait(2)
        self.mock(M.Plugin, "_save", side_effect=save)
        await self.plugin.set_hdr_saturation(1.2)
        pending = []
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            pending = [asyncio.create_task(call) for call in (
                self.plugin.set_sdr_enabled(False), self.plugin.reset(), self.plugin.set_hdr_saturation(1.4))]
            done, _ = await asyncio.wait(pending, timeout=0.2)
            self.assertEqual(len(done), len(pending), "disk IO held the control mutation lock")
            self.assertEqual(self.live_sdr, 0.8)
        finally:
            release.set()
            await asyncio.gather(*pending)
            await self.drain()

    async def test_disable_restores_before_slow_persistence_finishes(self):
        await self.plugin.set_sdr_enabled(True)
        await self.drain()
        entered, release = threading.Event(), threading.Event()
        def save(*args):
            entered.set()
            release.wait(2)
        self.mock(M.Plugin, "_save", side_effect=save)
        disable = asyncio.create_task(self.plugin.set_sdr_enabled(False))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertTrue(disable.done(), "disable RPC waits for filesystem persistence")
            self.assertFalse(M.Plugin._state["sdr_applied"])
            self.assertEqual(self.live_sdr, 0.8)
        finally:
            release.set()
            await disable
            await self.drain()

    async def test_monitor_reads_health_and_panel_metadata_with_one_process(self):
        lines = [f'{M.ATOM_IS_EXTERNAL}(CARDINAL) = 0',
                 f'{M.ATOM_EDID_PATH}(UTF8_STRING) = "/tmp/panel.bin"',
                 'GAMESCOPE_FOCUSED_APP(CARDINAL) = 0']
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            selected = lines if len(command) == 2 else [line for line in lines
                if any(line.startswith(atom + "(") for atom in command[2:])]
            return SimpleNamespace(stdout="\n".join(selected), returncode=0)
        self.mock(M.subprocess, "run", side_effect=run)
        self.mock(M, "_read_string_atom", side_effect=READ_STRING_ATOM)
        self.mock(M, "_read_edid", return_value=edid_with_red(0.640, 0.330))
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 0)
        self.mock(M, "WATCH_INTERVAL_S", 10000)
        self.mock(M.Plugin, "_panel_cache_key", None)
        self.mock(M.Plugin, "_panel_cache_complete", False)
        real_sleep, ticks = asyncio.sleep, 0
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
        self.assertEqual(commands, [["xprop", "-root"]])
        self.assertFalse(M.Plugin._state["external_display"])
        self.assertFalse(M.Plugin._state["hdr_supported"])

    async def test_new_edits_survive_completion_of_an_older_disk_snapshot(self):
        first, second = threading.Event(), threading.Event()
        release_first, release_second = threading.Event(), threading.Event()
        snapshots = []
        def save(values):
            snapshots.append(dict(values))
            if len(snapshots) == 1:
                first.set()
                release_first.wait(2)
            elif len(snapshots) == 2:
                second.set()
                release_second.wait(2)
        self.mock(M.Plugin, "_save", side_effect=save)
        await self.plugin.set_hdr_saturation(1.2)
        try:
            self.assertTrue(await asyncio.to_thread(first.wait, 1))
            worker = M.Plugin._workers["settings"]
            await self.plugin.set_hdr_saturation(1.4)
            await self.plugin.set_sdr_saturation(1.5)
            generation = M.Plugin._save_generation
            for _ in range(30):
                await self.plugin.set_hdr_saturation(1.4)
                self.plugin._retry_settings_save()
            self.assertEqual(M.Plugin._save_generation, generation)
            self.assertIs(M.Plugin._workers["settings"], worker)
            release_first.set()
            self.assertTrue(await asyncio.to_thread(second.wait, 1))
            self.assertTrue(M.Plugin._settings_dirty, "older completion acknowledged unsaved edits")
            self.assertEqual(snapshots[0]["hdr_saturation"], 1.2)
            self.assertEqual(snapshots[1]["hdr_saturation"], 1.4)
            self.assertEqual(snapshots[1]["sdr_saturation"], 1.5)
        finally:
            release_first.set()
            release_second.set()
            await self.drain()
        self.assertEqual(len(snapshots), 2)
        self.assertFalse(M.Plugin._settings_dirty)

    async def test_latest_disk_failure_remains_visible_then_same_value_retries(self):
        first, release = threading.Event(), threading.Event()
        snapshots = []
        def save(values):
            snapshots.append(dict(values))
            if len(snapshots) == 1:
                first.set()
                release.wait(2)
            elif len(snapshots) == 2:
                raise OSError("latest snapshot failed")
        self.mock(M.Plugin, "_save", side_effect=save)
        await self.plugin.set_hdr_saturation(1.2)
        try:
            self.assertTrue(await asyncio.to_thread(first.wait, 1))
            await self.plugin.set_hdr_saturation(1.4)
        finally:
            release.set()
            await self.drain()
        self.assertTrue(M.Plugin._settings_dirty)
        self.assertIn("latest snapshot failed", M.Plugin._state["settings_error"])
        await self.plugin.set_hdr_saturation(1.4)
        await self.drain()
        self.assertEqual([value["hdr_saturation"] for value in snapshots], [1.2, 1.4, 1.4])
        self.assertFalse(M.Plugin._settings_dirty)
        self.assertEqual(M.Plugin._state["settings_error"], "")

    async def test_unload_serializes_behind_an_older_write_and_flushes_latest_values(self):
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text('{"unrelated": "keep"}', encoding="utf-8")
            self.mock(M, "settings", SimpleNamespace(path=str(path), settings={"unrelated": "keep"}))
            self.mock(M.os.path, "exists", side_effect=PATH_EXISTS)
            def save(values=None):
                with M._settings_lock:
                    if values is not None:
                        entered.set()
                        release.wait(2)
                    SAVE_SETTINGS(values)
            self.mock(M.Plugin, "_save", side_effect=save)
            await self.plugin.set_hdr_saturation(1.2)
            unload = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                await self.plugin.set_hdr_saturation(1.4)
                unload = asyncio.create_task(self.plugin._unload())
                await asyncio.sleep(0)
                self.assertTrue(M.Plugin._unloading)
                late = await self.plugin.set_hdr_saturation(1.5)
                self.assertEqual(late["hdr_saturation"], 1.4)
            finally:
                release.set()
                if unload is not None:
                    await unload
                else:
                    await self.drain()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["hdr_saturation"], 1.4)
            self.assertEqual(saved["unrelated"], "keep")
            self.assertFalse(M.Plugin._settings_dirty)

    async def test_monitor_external_or_missing_metadata_never_adds_atom_queries(self):
        read = self.mock(M, "_read_edid")
        run = self.mock(M.subprocess, "run")
        for out, expected in ((f'{M.ATOM_IS_EXTERNAL}(CARDINAL) = 1\n'
                               f'{M.ATOM_EDID_PATH}(UTF8_STRING) = "/tmp/external.bin"', True),
                              ('GAMESCOPE_FOCUSED_APP(CARDINAL) = 0', None)):
            with self.subTest(expected=expected):
                run.return_value = SimpleNamespace(stdout=out)
                run.reset_mock()
                count, probe = M.Plugin._monitor_probe()
                self.assertGreater(count, 0)
                self.assertIs(probe[0], expected)
                self.assertIsNone(probe[2])
                run.assert_called_once()
        read.assert_not_called()

    async def test_monitor_reuses_string_decoding_and_cached_edid(self):
        run = self.mock(M.subprocess, "run", return_value=SimpleNamespace(stdout=
            f'{M.ATOM_IS_EXTERNAL}(CARDINAL) = 0\n'
            f'{M.ATOM_EDID_PATH}(UTF8_STRING) = "/tmp/a\\\"b\\\\c.bin"'))
        self.mock(M, "_file_identity", return_value=(1, 2, 128, 3, 4))
        read = self.mock(M, "_read_edid", return_value=edid_with_red(0.640, 0.330))
        self.mock(M.Plugin, "_panel_cache_key", None)
        self.mock(M.Plugin, "_panel_cache_complete", False)
        _, probe = M.Plugin._monitor_probe()
        M.Plugin._configure_for_panel(probe)
        _, cached = M.Plugin._monitor_probe()
        self.assertEqual(run.call_count, 2)
        read.assert_called_once_with('/tmp/a"b\\c.bin')
        self.assertIsNone(cached[2])

    async def test_failed_monitor_root_read_does_not_read_an_edid(self):
        self.mock(M.subprocess, "run", side_effect=OSError("X unavailable"))
        read = self.mock(M, "_read_edid")
        self.assertIsNone(M.Plugin._monitor_probe())
        read.assert_not_called()

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
