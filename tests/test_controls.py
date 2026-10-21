# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""RPC inputs, applying SDR/HDR, look ownership and worker coalescing."""

import asyncio
import os
import re
import subprocess
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from unittest.mock import patch

from _fixtures import PluginCase, PluginTest, WRITE_LOOK, drain  # installs the loader stand-ins first
import main as M
import vibrance_lut as V


class Clamping(PluginTest):

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


class Setters(PluginTest):
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
            # _main loads and clamps the settings, then leaves the gamescope
            # part to a task that drain() cancels, so no subprocess runs.
            drain(self.plugin._main())
        finally:
            M.settings._store.clear()
        self.assertEqual(M.Plugin._state["sdr_saturation"], 1.0)
        self.assertEqual(M.Plugin._state["hdr_saturation"], 1.0)


class SerializedMutations(PluginTest):
    """An older RPC must never commit after a newer toggle has finished."""

    def test_sdr_enable_cannot_overtake_disable(self):
        calls = []
        apply_started = threading.Event()
        let_apply_finish = threading.Event()
        result = {}

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

        M.Plugin._settings_loaded = True
        M.Plugin._save = staticmethod(fake_save)
        M.Plugin._apply_sdr = staticmethod(fake_apply)
        M.Plugin._release_sdr = staticmethod(fake_release)
        M.Plugin._schedule_look = lambda self, component: None
        M.Plugin._mutation_lock = asyncio.Lock()
        M.Plugin._unloading = False
        M.Plugin._disconnected = False
        M.vibrance_x11.read_float_atom = lambda atom: 0.8
        M.Plugin._state.update({
            "external_display": False, "sdr_enabled": False,
            "sdr_applied": False,
        })
        try:
            asyncio.run(scenario())
        finally:
            let_apply_finish.set()
        self.assertEqual(calls, ["apply", "release"])
        self.assertFalse(result["sdr_enabled"])
        self.assertFalse(result["sdr_applied"])


class SdrLookSplit(PluginTest):
    """EXPERIMENTAL, Legion Go S only. The atom carries what it can and the
    look carries the rest - and on a wide-gamut panel there is never a rest, so
    a Legion Go 2 must come out of this untouched."""

    def _wide(self):
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 1.0, 2.0

    def _standard(self):
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 0.0, 1.0

    def test_a_wide_gamut_panel_never_gets_a_look(self):
        """The whole point of the gate: nothing about the Go 2 changes."""
        self._wide()
        for value in (1.5, 2.0, 2.5, 3.0):
            atom, look = M._sdr_split(value)
            self.assertEqual(look, 1.0, value)
            self.assertAlmostEqual(atom, value, msg=str(value))

    def test_a_standard_panel_fills_the_atom_first(self):
        self._standard()
        for value, expected in ((1.5, 0.5), (2.0, 1.0)):
            atom, look = M._sdr_split(value)
            self.assertAlmostEqual(atom, expected, msg=str(value))
            self.assertEqual(look, 1.0, value)

    def test_100_percent_is_gamescope_s_own_default_on_any_panel(self):
        """-1 is the neutral of both branches, so it is right even before the
        panel is known - where the wide default's 1.0 would be the harshest."""
        for mapping in (self._wide, self._standard):
            mapping()
            self.assertEqual(M._sdr_split(1.0), (M.SDR_UNSET, 1.0))

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


class StaleLookJobs(PluginTest):
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

        def late_build(*_args):
            build_started.set()
            let_build_finish.wait(2)
            return os.path.join(M._runtime_dir(), "hdr-look-1.cube")

        async def scenario():
            plugin = M.Plugin()
            plugin._schedule_look("hdr")
            worker = M.Plugin._tasks[-1]
            for _ in range(1000):  # bounded: a regression fails instead of hanging
                if build_started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(build_started.is_set())
            await plugin.set_hdr_enabled(False)
            let_build_finish.set()
            await asyncio.gather(worker, return_exceptions=True)
            result.update(M.Plugin._state)

        M._write_look = late_build
        M.vibrance_x11.write_string_atom = lambda atom, value: (
            writes.append((atom, value)) or True)
        M.vibrance_x11.read_string_atom = lambda atom: None
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
        self.assertFalse(result["hdr_enabled"])
        self.assertFalse(result["hdr_applied"])
        self.assertFalse(any(value for _atom, value in writes))

    def test_failed_sdr_replacement_keeps_the_previous_look_owned(self):
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
        asyncio.run(scenario())
        self.assertEqual(result["path"], old)
        self.assertTrue(result["applied"])


class LookOwnership(PluginTest):

    def test_release_restores_the_foreign_hdr_baseline(self):
        own = os.path.join(M._runtime_dir(), "hdr-look-1.cube")
        foreign = "/tmp/foreign-hdr.cube"
        writes = []
        M.Plugin._hdr_look_path = own
        M.Plugin._hdr_look_baseline = foreign
        M.Plugin._state["hdr_applied"] = True
        M.vibrance_x11.read_string_atom = lambda atom: own
        M.vibrance_x11.write_string_atom = lambda atom, value: (
            writes.append((atom, value)) or True)
        self.assertTrue(M.Plugin._release_look("hdr"))
        self.assertEqual(writes, [(M.ATOM_LOOK_PQ, foreign)])
        self.assertFalse(M.Plugin._state["hdr_applied"])

    def test_failed_clear_keeps_ownership_for_a_retry(self):
        own = os.path.join(M._runtime_dir(), "sdr-look-1.cube")
        M.Plugin._sdr_look_path = own
        M.Plugin._sdr_look_baseline = None
        M.Plugin._state["sdr_look_applied"] = True
        M.vibrance_x11.read_string_atom = lambda atom: own
        M.vibrance_x11.write_string_atom = lambda atom, value: False
        self.assertFalse(M.Plugin._release_look("sdr"))
        self.assertEqual(M.Plugin._sdr_look_path, own)
        self.assertTrue(M.Plugin._state["sdr_look_applied"])


class ReleaseIsReallyOff(PluginTest):
    """Turning the half off has to leave the picture where it was before it was
    ever turned on - on both branches, whose neutral values disagree."""

    def setUp(self):
        self.written = []
        M.vibrance_x11.write_float_atom = lambda name, value: (
            self.written.append(value) or True)
        M.Plugin._release_look = staticmethod(lambda component: None)
        M.Plugin._state.update({"sdr_applied": True, "sdr_enabled": True})

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


class RequestHandling(PluginCase):
    async def test_unload_restores_both_halves_saves_and_bounds_xprop_wait(self):
        calls = []
        self.mock(M.Plugin, "_release_sdr", side_effect=lambda: calls.append("sdr"))
        self.mock(M.Plugin, "_release_look", side_effect=calls.append)
        self.mock(M.Plugin, "_save", side_effect=lambda values=None: calls.append("save"))
        M.vibrance_x11.timeout = 3.0                       # what _main leaves it at
        await self.plugin._unload()
        self.assertEqual(sorted(calls), ["hdr", "save", "sdr"])
        with patch.object(M.vibrance_x11.subprocess, "run", side_effect=subprocess.TimeoutExpired("xprop", 0.25)) as run:
            self.assertIsNone(M.vibrance_x11.read_int_atom(M.ATOM_IS_EXTERNAL))
            self.assertLessEqual(run.call_args.kwargs["timeout"], 0.25)

    async def test_unload_saves_while_x_restores_are_still_running(self):
        """A slow X server must not push the settings flush past Decky's
        five-second stop budget, so the disk does not wait for X."""
        saved, seen = threading.Event(), []
        self.mock(M.Plugin, "_release_sdr", side_effect=lambda: seen.append(saved.wait(5)))
        self.mock(M.Plugin, "_release_look", side_effect=lambda component: None)
        self.mock(M.Plugin, "_save", side_effect=lambda values=None: saved.set())
        await self.plugin._unload()
        self.assertEqual(seen, [True])

    async def test_a_failed_read_while_committing_keeps_the_foreign_look(self):
        """A read that times out returns the same None as an absent atom; it
        must not replace the foreign look we found and must restore."""
        self.strings[M.ATOM_LOOK_PQ] = "/tmp/foreign.cube"
        def build(*args):
            self.strings.pop(M.ATOM_LOOK_PQ)   # the commit-time read now fails
            return "/tmp/hdr-look-1.cube"
        self.builder.side_effect = build
        M.Plugin._state.update(hdr_enabled=True, hdr_saturation=1.3)
        self.plugin._schedule_look("hdr")
        await self.drain()
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/tmp/hdr-look-1.cube")
        await self.plugin.set_hdr_enabled(False)
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/tmp/foreign.cube")

    async def test_failed_reads_while_enabling_keep_the_foreign_look_found_at_connect(self):
        foreign = "/home/deck/other-tool.cube"
        M.Plugin._hdr_look_baseline = self.strings[M.ATOM_LOOK_PQ] = foreign
        reads = iter([None, None])                  # both reads time out
        M.vibrance_x11.read_string_atom.side_effect = lambda atom: next(reads, self.strings.get(atom))
        M.Plugin._state.update(hdr_enabled=True, hdr_saturation=1.2)
        self.plugin._schedule_look("hdr")
        await self.drain()
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/tmp/hdr-look-1.cube")
        M.Plugin._release_look("hdr")
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], foreign)

    async def test_a_write_reported_failed_that_landed_is_still_ours_to_restore(self):
        await self.plugin.set_sdr_enabled(True)          # baseline 0.8, writes 1.0
        def lands_but_times_out(atom, value):
            self.live_sdr = value
            return False
        self.float_writer.side_effect = lands_but_times_out
        await self.plugin.set_sdr_saturation(2.0)
        await self.drain()
        self.assertEqual(self.live_sdr, 2.0)
        self.float_writer.side_effect = self.write_float
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)

    async def test_a_first_write_reported_failed_that_landed_is_restored(self):
        """Not even one write was reported successful, but the atom shows ours."""
        def lands_but_times_out(atom, value):
            self.live_sdr = value
            return False
        self.float_writer.side_effect = lands_but_times_out
        await self.plugin.set_sdr_enabled(True, 1.4)     # baseline 0.8, 1.4 lands
        self.assertFalse(M.Plugin._state["sdr_applied"])
        self.float_writer.side_effect = self.write_float
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)

    async def test_a_first_write_reported_failed_that_never_landed_touches_nothing(self):
        self.float_writer.side_effect = None
        self.float_writer.return_value = False             # nothing lands
        await self.plugin.set_sdr_enabled(True, 1.4)
        self.live_sdr = 0.6                                # another writer meanwhile
        self.float_writer.side_effect = self.write_float
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.6)

    async def test_a_lost_first_look_write_that_landed_is_released(self):
        """The look twin of a lost SDR write: the atom names our LUT although
        the write reported failure, and disabling must take it back off."""
        for component, atom, enable, value in (
                ("hdr", M.ATOM_LOOK_PQ, self.plugin.set_hdr_enabled, 1.3),
                ("sdr", M.ATOM_LOOK_G22, self.plugin.set_sdr_enabled, 2.6)):
            with self.subTest(component=component):
                M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 0.0, 1.0   # so SDR 260% needs a look
                def lands_but_times_out(name, path):
                    self.strings[name] = path
                    return name != atom
                self.string_writer.side_effect = lands_but_times_out
                await enable(True, value)
                await self.drain()
                self.assertIn(f"{component}-look-", self.strings[atom])
                self.string_writer.side_effect = self.write_string
                await enable(False)
                self.assertEqual(self.strings[atom], "")

    async def test_a_lost_first_look_write_that_never_landed_keeps_the_foreign_look(self):
        self.strings[M.ATOM_LOOK_PQ] = "/tmp/foreign.cube"
        self.string_writer.side_effect = lambda name, path: False
        await self.plugin.set_hdr_enabled(True, 1.3)
        await self.drain()
        self.string_writer.side_effect = self.write_string
        await self.plugin.set_hdr_enabled(False)
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/tmp/foreign.cube")

    def lands_but_times_out(self, atom, value):
        self.live_sdr = value
        return False

    async def test_reenabling_after_a_lost_write_does_not_adopt_our_value(self):
        self.float_writer.side_effect = self.lands_but_times_out
        await self.plugin.set_sdr_enabled(True, 1.5)       # baseline 0.8, 1.5 lands
        self.float_writer.side_effect = lambda atom, value: False
        await self.plugin.set_sdr_enabled(False)           # the restore fails outright
        await self.plugin.set_sdr_enabled(True, 1.5)       # back on before any retry
        self.float_writer.side_effect = self.write_float
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)

    async def test_a_failed_read_when_enabling_keeps_the_known_baseline(self):
        reads = iter([None])
        self.mock(M.vibrance_x11, "read_float_atom", side_effect=lambda atom: next(reads, self.live_sdr))
        await self.plugin.set_sdr_enabled(True, 1.4)       # the baseline read times out
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.4)               # the one known before, not -1

    async def test_a_lasting_build_failure_is_retried_on_the_slow_tick_only(self):
        """Each retry is a full LUT build; every 4 s it drains the battery. One
        quick retry covers a passing failure, then only the slow tick retries."""
        self.builder.side_effect = lambda *args: ""          # e.g. an unwritable folder
        M.Plugin._state.update(hdr_enabled=True, hdr_saturation=1.3)
        self.plugin._schedule_look("hdr")
        await self.drain()
        self.builder.reset_mock()
        for _ in range(2 * M.SLOW_CHECK_EVERY):              # two slow ticks' worth
            await self.plugin._watch_tick()
            await self.drain()
        self.assertEqual(self.builder.call_count, 3)          # 1 quick + 2 slow

    async def test_a_single_failure_is_retried_on_the_next_tick(self):
        outcomes = iter([False])                              # fails once, then works
        self.builder.side_effect = lambda value, slot, *args: (
            f"/tmp/hdr-look-{slot}.cube" if next(outcomes, True) else "")
        M.Plugin._state.update(hdr_enabled=True, hdr_saturation=1.3)
        self.plugin._schedule_look("hdr")
        await self.drain()
        self.assertTrue(M.Plugin._state["hdr_error"])
        await self.plugin._watch_tick()                       # an ordinary 4 s tick
        await self.drain()
        self.assertEqual(M.Plugin._state["hdr_error"], "")
        self.assertEqual(M.Plugin._state["hdr_applied_saturation"], 1.3)

    async def test_releasing_a_look_clears_its_failure_count(self):
        """Old failures must not make a later one-off failure wait 16 s."""
        M.Plugin._look_failures["hdr"] = 3
        M.Plugin._state.update(hdr_enabled=True, hdr_saturation=1.0)
        self.plugin._schedule_look("hdr")                     # 100%: the look is released
        await self.drain()
        self.assertEqual(M.Plugin._look_failures["hdr"], 0)

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

    async def test_waking_up_docked_writes_nothing_before_the_panel_check_sees_it(self):
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 0, 1
        await self.plugin.set_sdr_saturation(2.4)
        await self.plugin.set_hdr_saturation(1.3)
        await self.plugin.set_sdr_enabled(True)
        await self.plugin.set_hdr_enabled(True)
        await self.drain()
        self.float_writer.reset_mock()
        self.string_writer.reset_mock()
        self.mock(M.vibrance_x11, "display_is_external", return_value=True)  # still believed internal
        M.Plugin._reassert()
        self.float_writer.assert_not_called()
        self.string_writer.assert_not_called()

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
            self.mock(M.vibrance_lutfile, "SIZE", 5)
            self.mock(M.vibrance_lutfile, "_cache", OrderedDict())
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
        M.Plugin._slow_checks = M.SLOW_CHECK_EVERY - 1   # pending looks retry on the slow tick
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


class FrontendContract(PluginTest):

    def test_every_key_the_menu_reads_is_in_the_backend_state(self):
        """A key renamed on one side arrives as undefined on the other, and
        the menu silently hides what depends on it (the HDR section, say)."""
        source = (Path(__file__).resolve().parent.parent / "src" / "index.tsx").read_text(encoding="utf-8")
        body = re.search(r"interface State \{(.*?)\}", source, re.S).group(1)
        keys = set(re.findall(r"^\s*(\w+)\s*:", body, re.M))
        self.assertGreater(len(keys), 20)
        self.assertLessEqual(keys, set(M.Plugin._state))


class LookOwnership(PluginCase):
    async def test_disable_restores_when_the_atom_shows_our_other_slot(self):
        # A replacement whose reply was lost still landed: the atom names our
        # newer slot, not the one we track. That is ours, not a foreign baseline.
        with tempfile.TemporaryDirectory() as folder:
            self.mock(M, "_runtime_dir", return_value=folder)
            M.Plugin._hdr_look_path = os.path.join(folder, "hdr-look-0.cube")
            M.Plugin._hdr_look_baseline = "/foreign.cube"
            self.strings[M.ATOM_LOOK_PQ] = os.path.join(folder, "hdr-look-1.cube")
            self.assertTrue(M.Plugin._release_look("hdr"))
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/foreign.cube")

    async def test_a_superseded_build_is_not_reported_as_a_failure(self):
        M.Plugin._state.update(hdr_enabled=True, hdr_saturation=1.3)
        def cancelled_build(*_args):
            M.Plugin._hdr_generation += 1  # a newer slider move arrived
            return ""
        self.builder.side_effect = cancelled_build
        self.plugin._schedule_look("hdr")
        await self.drain()
        self.assertEqual(M.Plugin._state["hdr_error"], "")
        self.assertEqual(M.Plugin._look_failures["hdr"], 0)

    async def test_returning_to_100_percent_gives_the_atom_back_without_a_build(self):
        M.Plugin._state.update(hdr_enabled=True, hdr_saturation=1.3, hdr_applied=True,
                               hdr_applied_saturation=1.3)
        M.Plugin._hdr_look_path = "/tmp/hdr-look-0.cube"
        M.Plugin._hdr_look_baseline = "/foreign.cube"
        self.strings[M.ATOM_LOOK_PQ] = "/tmp/hdr-look-0.cube"
        await self.plugin.set_hdr_saturation(1.0)
        await self.drain()
        self.builder.assert_not_called()
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/foreign.cube")
        self.assertFalse(M.Plugin._state["hdr_applied"])

    async def test_failed_hdr_release_is_retried_on_the_slow_tick(self):
        M.Plugin._hdr_look_path = "/tmp/hdr-look-0.cube"
        self.strings[M.ATOM_LOOK_PQ] = "/tmp/hdr-look-0.cube"
        self.string_writer.side_effect = lambda atom, value: False
        self.assertFalse(M.Plugin._release_look("hdr"))
        self.string_writer.side_effect = self.write_string
        M.Plugin._slow_checks = M.SLOW_CHECK_EVERY - 1
        M.Plugin._watch_once(None)
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "")
        self.assertIsNone(M.Plugin._hdr_look_path)

    async def test_preserved_session_hands_a_foreign_look_its_baseline(self):
        M.Plugin._hdr_look_path = "/tmp/hdr-look-0.cube"
        M.Plugin._state["hdr_applied"] = True
        self.strings[M.ATOM_LOOK_PQ] = "/foreign.cube"
        M.Plugin._capture_existing_looks(True)
        self.assertEqual(M.Plugin._hdr_look_baseline, "/foreign.cube")
        self.assertIsNone(M.Plugin._hdr_look_path)
        self.assertFalse(M.Plugin._state["hdr_applied"])


class SdrRecovery(PluginCase):
    async def test_watch_retries_a_newer_value_whose_write_never_landed(self):
        M.Plugin._state.update(sdr_enabled=True, sdr_applied=True, sdr_saturation=1.8,
                               sdr_error="could not write SDR to gamescope")
        M.Plugin._last_sdr_written = self.live_sdr = 1.5
        M.Plugin._last_sdr_attempted = 1.8
        M.Plugin._watch_once(None)
        self.assertAlmostEqual(self.live_sdr, 1.8)
        self.assertEqual(M.Plugin._state["sdr_error"], "")

    async def test_release_with_nothing_held_clears_a_stale_restore_error(self):
        M.Plugin._state.update(sdr_enabled=False, sdr_applied=False,
                               sdr_error="waiting for gamescope before restoring SDR")
        M.Plugin._release_sdr()
        self.assertEqual(M.Plugin._state["sdr_error"], "")


class SdrRewrite(PluginCase):
    async def test_the_same_slider_value_rewrites_an_atom_holding_an_older_target(self):
        M.Plugin._state.update(sdr_enabled=True, sdr_saturation=1.5, sdr_applied=True, sdr_error="")
        M.Plugin._last_sdr_written = self.live_sdr = 1.2     # e.g. written before the mapping moved
        await self.plugin.set_sdr_saturation(1.5)
        await self.drain()
        self.assertEqual(self.live_sdr, 1.5)
