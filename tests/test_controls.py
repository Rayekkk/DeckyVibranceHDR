# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""RPC inputs, applying SDR/HDR, look ownership and worker coalescing."""

import asyncio
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from unittest.mock import patch

from _fixtures import PluginCase, PluginTest, WRITE_LOOK, drain, write_settings  # installs the decky stand-in first
import vibrance_control as M
import vibrance_lut as V


class Clamping(PluginTest):

    RANGES = (("sdr", "SDR_MIN", "SDR_SOFT_WIDE", "SDR_MAX_WIDE"),
              ("hdr", "HDR_MIN", "HDR_SOFT_LIMIT", "HDR_MAX"))

    def _bounds(self):
        for half, lo, soft, hi in self.RANGES:
            yield half, *(getattr(M.vibrance_state, name) for name in (lo, soft, hi))

    def test_each_range_is_enforced(self):
        for half, lo, _soft, hi in self._bounds():
            self.assertEqual(M.vibrance_state.clamp(-1.0, lo, hi), lo, half)
            self.assertEqual(M.vibrance_state.clamp(99.0, lo, hi), hi, half)
            self.assertEqual(M.vibrance_state.clamp(lo, lo, hi), lo, half)
            self.assertEqual(M.vibrance_state.clamp(hi, lo, hi), hi, half)

    def test_neither_half_can_desaturate(self):
        """The floor is neutral on both. Below it the picture is being drained,
        which is the one thing this plugin is not for."""
        for half, lo, _soft, hi in self._bounds():
            self.assertEqual(lo, 1.0, half)
            self.assertEqual(M.vibrance_state.clamp(0.0, lo, hi), 1.0, half)
            self.assertEqual(M.vibrance_state.clamp(0.999, lo, hi), 1.0, half)

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
            ("sdr_soft_limit", M.vibrance_state.SDR_SOFT_WIDE),
            ("hdr_min", M.HDR_MIN),
            ("hdr_max", M.HDR_MAX),
            ("hdr_soft_limit", M.vibrance_state.HDR_SOFT_LIMIT),
        ):
            self.assertIn(key, M.state.public)
            self.assertEqual(M.state.public[key], constant, key)

    def test_the_requested_ranges_are_what_shipped(self):
        """Pinned to literals on purpose. Everything else here is relative, so
        without this a tuning pass could hand back half the range and the suite
        would stay green."""
        self.assertEqual(M.SDR_MIN, 1.0)
        self.assertEqual(M.SDR_MAX_WIDE, 3.0)
        self.assertEqual(M.vibrance_effects.SDR_MAX_STANDARD, 3.0)
        self.assertEqual(M.HDR_MIN, 1.0)
        self.assertEqual(M.HDR_MAX, 1.5)

    def test_hdr_ceiling_stays_below_the_sdr_one(self):
        """Different mechanisms, different useful ranges - if these ever become
        one number again the LUT half is the one that suffers."""
        self.assertLess(M.HDR_MAX, M.SDR_MAX_WIDE)


class Setters(PluginTest):
    """clamp is exercised above in isolation; this is about its call sites.

    Passing one half's bounds to the other half's setter is a single-token slip
    that nothing else in this file would notice.
    """

    def setUp(self):
        self.plugin = M.Controller()
        M.state.settings_loaded = True  # StateGuard resets it afterwards
        M.state.public["sdr_enabled"] = False
        M.state.public["hdr_enabled"] = False
        M.state.public["sdr_max"] = M.SDR_MAX_WIDE

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
        write_settings({
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
            write_settings(None)
        self.assertEqual(M.state.public["sdr_saturation"], 1.0)
        self.assertEqual(M.state.public["hdr_saturation"], 1.0)


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
            M.state.public["sdr_applied"] = True

        def fake_release():
            calls.append("release")
            M.state.public["sdr_applied"] = False
            return True

        async def scenario():
            plugin = M.Controller()
            enabling = asyncio.create_task(plugin.set_sdr_enabled(True))
            self.assertTrue(await asyncio.to_thread(apply_started.wait, 1))
            disabling = asyncio.create_task(plugin.set_sdr_enabled(False))
            await asyncio.sleep(0)
            self.assertFalse(disabling.done())
            let_apply_finish.set()
            await asyncio.gather(enabling, disabling)
            await asyncio.gather(*list(M.state.tasks))
            result.update(M.state.public)

        M.state.settings_loaded = True
        M.Controller._save = staticmethod(fake_save)
        M.vibrance_effects.apply_sdr = staticmethod(fake_apply)
        M.vibrance_effects.release_sdr = staticmethod(fake_release)
        M.Controller._schedule_look = lambda self, component: None
        M.state.mutation_lock = asyncio.Lock()
        M.state.unloading = False
        M.state.disconnected = False
        M.vibrance_x11.read_float_atom = lambda atom: 0.8
        M.state.public.update({
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
        M.state.sdr_neutral_atom, M.state.sdr_atom_span = 1.0, 2.0

    def _standard(self):
        M.state.sdr_neutral_atom, M.state.sdr_atom_span = 0.0, 1.0

    def test_a_wide_gamut_panel_never_gets_a_look(self):
        """The whole point of the gate: nothing about the Go 2 changes."""
        self._wide()
        for value in (1.5, 2.0, 2.5, 3.0):
            atom, look = M.vibrance_effects.sdr_split(value)
            self.assertEqual(look, 1.0, value)
            self.assertAlmostEqual(atom, value, msg=str(value))

    def test_a_standard_panel_fills_the_atom_first(self):
        self._standard()
        for value, expected in ((1.5, 0.5), (2.0, 1.0)):
            atom, look = M.vibrance_effects.sdr_split(value)
            self.assertAlmostEqual(atom, expected, msg=str(value))
            self.assertEqual(look, 1.0, value)

    def test_100_percent_is_gamescope_s_own_default_on_any_panel(self):
        """-1 is the neutral of both branches, so it is right even before the
        panel is known - where the wide default's 1.0 would be the harshest."""
        for mapping in (self._wide, self._standard):
            mapping()
            self.assertEqual(M.vibrance_effects.sdr_split(1.0), (M.vibrance_effects.SDR_UNSET, 1.0))

    def test_the_look_only_starts_where_cfit_stops(self):
        """cfit() clamps at atom 1.0, so below 200% the look must stay out."""
        self._standard()
        self.assertEqual(M.vibrance_effects.sdr_split(1.99)[1], 1.0)
        self.assertGreater(M.vibrance_effects.sdr_split(2.01)[1], 1.0)

    def test_the_atom_never_goes_past_where_it_responds(self):
        self._standard()
        for value in (2.0, 2.5, 3.0):
            self.assertAlmostEqual(M.vibrance_effects.sdr_split(value)[0], 1.0, msg=str(value))

    def test_the_top_of_the_slider_asks_the_look_for_its_ceiling(self):
        self._standard()
        self.assertAlmostEqual(
            M.vibrance_effects.sdr_split(M.vibrance_effects.SDR_MAX_STANDARD)[1],
            M.vibrance_effects.SDR_LOOK_CEILING)

    def test_raising_the_maximum_did_not_weaken_what_was_below_it(self):
        """The look is a rate, not a fraction of the range, so 300% asks for
        exactly what it asked for when 300% was the top of the slider."""
        self._standard()
        self.assertAlmostEqual(M.vibrance_effects.sdr_split(3.0)[1], 1.5)
        self.assertAlmostEqual(M.vibrance_effects.sdr_split(2.5)[1], 1.25)

    def test_the_slider_is_twice_the_look_where_there_is_one(self):
        """The panel shows the SDR look's Last applied as 2 x look."""
        self._standard()
        for value in (2.01, 2.5, 2.8, M.vibrance_effects.SDR_MAX_STANDARD):
            self.assertAlmostEqual(2 * M.vibrance_effects.sdr_split(value)[1], value, msg=str(value))

    def test_the_look_ramps_rather_than_jumping(self):
        self._standard()
        looks = [M.vibrance_effects.sdr_split(v)[1] for v in (2.0, 2.25, 2.5, 2.75, 3.0)]
        for earlier, later in zip(looks, looks[1:]):
            self.assertGreater(later, earlier)

    def test_the_look_ceiling_matches_the_hdr_one(self):
        """Same code, same clipping, so the same ceiling."""
        self.assertEqual(M.vibrance_effects.SDR_LOOK_CEILING, M.HDR_MAX)

    def test_the_ceiling_is_never_asked_for_more_than_it_allows(self):
        self._standard()
        for value in (4.0, 6.0, 99.0):
            self.assertLessEqual(M.vibrance_effects.sdr_split(value)[1],
                                 M.vibrance_effects.SDR_LOOK_CEILING, str(value))

    def test_the_warning_sits_where_the_hdr_half_warns(self):
        """Identical maths, so the look value that starts clipping is the same;
        only the slider position it corresponds to differs."""
        self._standard()
        at_warning = M.vibrance_effects.sdr_split(M.vibrance_effects.SDR_SOFT_STANDARD)[1]
        self.assertAlmostEqual(at_warning, M.vibrance_state.HDR_SOFT_LIMIT, places=6)


class StaleLookJobs(PluginTest):
    """A worker may finish its file, but no stale worker may commit an atom."""

    def setUp(self):
        loaded = patch.object(M.state, "settings_loaded", True)
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
            return os.path.join(M.vibrance_effects.runtime_dir(), "hdr-look-1.cube")

        async def scenario():
            plugin = M.Controller()
            plugin._schedule_look("hdr")
            worker = M.state.tasks[-1]
            for _ in range(1000):  # bounded: a regression fails instead of hanging
                if build_started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(build_started.is_set())
            await plugin.set_hdr_enabled(False)
            let_build_finish.set()
            await asyncio.gather(worker, return_exceptions=True)
            result.update(M.state.public)

        M.vibrance_effects.write_look = late_build
        M.vibrance_x11.write_string_atom = lambda atom, value: (
            writes.append((atom, value)) or True)
        M.vibrance_x11.read_string_atom = lambda atom: None
        M.DEBOUNCE_S = 0
        M.state.mutation_lock = asyncio.Lock()
        M.state.tasks = []
        M.state.unloading = False
        M.state.hdr_look_path = None
        M.state.hdr_look_baseline = None
        M.state.public.update({
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
        old = os.path.join(M.vibrance_effects.runtime_dir(), "sdr-look-0.cube")
        result = {}

        async def scenario():
            plugin = M.Controller()
            plugin._schedule_look("sdr")
            worker = M.state.tasks[-1]
            await asyncio.gather(worker, return_exceptions=True)
            result["path"] = M.state.sdr_look_path
            result["applied"] = M.state.public["sdr_look_applied"]

        M.vibrance_effects.write_look = lambda *_args: ""
        M.DEBOUNCE_S = 0
        M.state.mutation_lock = asyncio.Lock()
        M.state.tasks = []
        M.state.unloading = False
        M.state.sdr_neutral_atom, M.state.sdr_atom_span = 0.0, 1.0
        M.state.sdr_look_path = old
        M.state.sdr_look_baseline = "/tmp/foreign-sdr.cube"
        M.state.sdr_look_slot = 0
        M.state.public.update({
            "external_display": False, "sdr_enabled": True,
            "sdr_saturation": 2.5, "sdr_look_applied": True,
        })
        asyncio.run(scenario())
        self.assertEqual(result["path"], old)
        self.assertTrue(result["applied"])


class LookOwnership(PluginTest):

    def test_release_restores_the_foreign_hdr_baseline(self):
        own = os.path.join(M.vibrance_effects.runtime_dir(), "hdr-look-1.cube")
        foreign = "/tmp/foreign-hdr.cube"
        writes = []
        M.state.hdr_look_path = own
        M.state.hdr_look_baseline = foreign
        M.state.public["hdr_applied"] = True
        M.vibrance_x11.read_string_atom = lambda atom: own
        M.vibrance_x11.write_string_atom = lambda atom, value: (
            writes.append((atom, value)) or True)
        self.assertTrue(M.vibrance_effects.release_look("hdr"))
        self.assertEqual(writes, [(M.vibrance_x11.ATOM_LOOK_PQ, foreign)])
        self.assertFalse(M.state.public["hdr_applied"])

    def test_failed_clear_keeps_ownership_for_a_retry(self):
        own = os.path.join(M.vibrance_effects.runtime_dir(), "sdr-look-1.cube")
        M.state.sdr_look_path = own
        M.state.sdr_look_baseline = None
        M.state.public["sdr_look_applied"] = True
        M.vibrance_x11.read_string_atom = lambda atom: own
        M.vibrance_x11.write_string_atom = lambda atom, value: False
        self.assertFalse(M.vibrance_effects.release_look("sdr"))
        self.assertEqual(M.state.sdr_look_path, own)
        self.assertTrue(M.state.public["sdr_look_applied"])


class ReleaseIsReallyOff(PluginTest):
    """Turning the half off has to leave the picture where it was before it was
    ever turned on - on both branches, whose neutral values disagree."""

    def setUp(self):
        self.written = []
        M.vibrance_x11.write_float_atom = lambda name, value: (
            self.written.append(value) or True)
        M.vibrance_effects.release_look = staticmethod(lambda component: None)
        M.state.public.update({"sdr_applied": True, "sdr_enabled": True})

    def test_without_a_baseline_it_hands_the_atom_back_to_gamescope(self):
        """A negative wideness is gamescope's own "unset", and both branches of
        buildSDRColorimetry read it as that branch's native gamut. 1.0 would be
        neutral on one and the harshest setting on the other."""
        M.state.sdr_baseline = None
        M.vibrance_effects.release_sdr()
        self.assertEqual(self.written, [M.vibrance_effects.SDR_UNSET])
        self.assertLess(M.vibrance_effects.SDR_UNSET, 0.0)

    def test_a_real_baseline_is_still_restored(self):
        M.state.sdr_baseline = 0.4
        M.vibrance_effects.release_sdr()
        self.assertEqual(self.written, [0.4])

    def test_the_look_goes_first_even_if_the_atom_was_never_applied(self):
        """Both mechanisms are on at once above 200%; releasing one and not the
        other leaves half the saturation behind, which is what shipped."""
        calls = []
        M.vibrance_effects.release_look = staticmethod(lambda component: calls.append(component))
        M.state.public["sdr_applied"] = False
        M.vibrance_effects.release_sdr()
        self.assertEqual(calls, ["sdr"])


class RequestHandling(PluginCase):
    async def test_unload_restores_both_halves_saves_and_bounds_xprop_wait(self):
        calls = []
        self.mock(M.vibrance_effects, "release_sdr", side_effect=lambda: calls.append("sdr"))
        self.mock(M.vibrance_effects, "release_look", side_effect=calls.append)
        self.mock(M.Controller, "_save", side_effect=lambda values=None: calls.append("save"))
        M.vibrance_x11.timeout = 3.0                       # what _main leaves it at
        await self.plugin._unload()
        self.assertEqual(sorted(calls), ["hdr", "save", "sdr"])
        with patch.object(M.vibrance_x11.subprocess, "run", side_effect=subprocess.TimeoutExpired("xprop", 0.25)) as run:
            self.assertIsNone(M.vibrance_x11.read_int_atom(M.vibrance_x11.ATOM_IS_EXTERNAL))
            self.assertLessEqual(run.call_args.kwargs["timeout"], 0.25)

    async def test_unload_saves_while_x_restores_are_still_running(self):
        """A slow X server must not push the settings flush past Decky's
        five-second stop budget, so the disk does not wait for X."""
        saved, seen = threading.Event(), []
        self.mock(M.vibrance_effects, "release_sdr", side_effect=lambda: seen.append(saved.wait(5)))
        self.mock(M.vibrance_effects, "release_look", side_effect=lambda component: None)
        self.mock(M.Controller, "_save", side_effect=lambda values=None: saved.set())
        await self.plugin._unload()
        self.assertEqual(seen, [True])

    async def test_a_failed_read_while_committing_keeps_the_foreign_look(self):
        """A read that times out returns the same None as an absent atom; it
        must not replace the foreign look we found and must restore."""
        self.strings[M.vibrance_x11.ATOM_LOOK_PQ] = "/tmp/foreign.cube"
        def build(*args):
            self.strings.pop(M.vibrance_x11.ATOM_LOOK_PQ)   # the commit-time read now fails
            return "/tmp/hdr-look-1.cube"
        self.builder.side_effect = build
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3)
        self.plugin._schedule_look("hdr")
        await self.drain()
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], "/tmp/hdr-look-1.cube")
        await self.plugin.set_hdr_enabled(False)
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], "/tmp/foreign.cube")

    async def test_failed_reads_while_enabling_keep_the_foreign_look_found_at_connect(self):
        foreign = "/home/deck/other-tool.cube"
        M.state.hdr_look_baseline = self.strings[M.vibrance_x11.ATOM_LOOK_PQ] = foreign
        reads = iter([None, None])                  # both reads time out
        M.vibrance_x11.read_string_atom.side_effect = lambda atom: next(reads, self.strings.get(atom))
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.2)
        self.plugin._schedule_look("hdr")
        await self.drain()
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], "/tmp/hdr-look-1.cube")
        M.vibrance_effects.release_look("hdr")
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], foreign)

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
        self.assertFalse(M.state.public["sdr_applied"])
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
                ("hdr", M.vibrance_x11.ATOM_LOOK_PQ, self.plugin.set_hdr_enabled, 1.3),
                ("sdr", M.vibrance_x11.ATOM_LOOK_G22, self.plugin.set_sdr_enabled, 2.6)):
            with self.subTest(component=component):
                M.state.sdr_neutral_atom, M.state.sdr_atom_span = 0.0, 1.0   # so SDR 260% needs a look
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
        self.strings[M.vibrance_x11.ATOM_LOOK_PQ] = "/tmp/foreign.cube"
        self.string_writer.side_effect = lambda name, path: False
        await self.plugin.set_hdr_enabled(True, 1.3)
        await self.drain()
        self.string_writer.side_effect = self.write_string
        await self.plugin.set_hdr_enabled(False)
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], "/tmp/foreign.cube")

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
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3)
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
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3)
        self.plugin._schedule_look("hdr")
        await self.drain()
        self.assertTrue(M.state.public["hdr_error"])
        await self.plugin._watch_tick()                       # an ordinary 4 s tick
        await self.drain()
        self.assertEqual(M.state.public["hdr_error"], "")
        self.assertEqual(M.state.public["hdr_applied_saturation"], 1.3)

    async def test_releasing_a_look_clears_its_failure_count(self):
        """Old failures must not make a later one-off failure wait 16 s."""
        M.state.look_failures["hdr"] = 3
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.0)
        self.plugin._schedule_look("hdr")                     # 100%: the look is released
        await self.drain()
        self.assertEqual(M.state.look_failures["hdr"], 0)

    async def test_two_hundred_requests_use_bounded_workers_and_latest_value(self):
        M.state.public.update(hdr_enabled=True)
        self.mock(M, "DEBOUNCE_S", 0.005)
        for n in range(200):
            await self.plugin.set_hdr_saturation(1 + n / 400)
        self.assertLessEqual(len(M.state.tasks), 2)
        await self.drain()
        self.builder.assert_called_once()
        self.assertAlmostEqual(M.state.public["hdr_applied_saturation"], 1.4975)

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
        M.state.public.update(hdr_enabled=True)
        await self.plugin.set_hdr_saturation(1.1)
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        worker = M.state.workers["hdr"]
        for n in range(40):
            await self.plugin.set_hdr_saturation(1.2 + n / 200)
        self.assertIs(M.state.workers["hdr"], worker)
        await self.drain()
        self.assertEqual(builds, [1.1, 1.395])
        self.assertEqual(M.state.public["hdr_applied_saturation"], 1.395)
        self.string_writer.assert_called_once()

    async def test_strict_rpc_inputs_and_toggle_carries_pending_value(self):
        for bad in (None, True, "1.3", float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                await self.plugin.set_hdr_saturation(bad)
        with self.assertRaises(ValueError):
            await self.plugin.set_hdr_enabled("false")
        await self.plugin.set_hdr_enabled(False, 1.35)
        self.assertEqual(M.state.public["hdr_saturation"], 1.35)

class Controls(PluginCase):

    async def test_unchanged_healthy_hdr_does_not_rebuild_or_save(self):
        await self.plugin.set_hdr_saturation(1.3)
        await self.plugin.set_hdr_enabled(True)
        await self.drain()
        self.builder.reset_mock()
        M.Controller._save.reset_mock()
        for _ in range(30):
            await self.plugin.set_hdr_saturation(1.3)
        await self.drain()
        self.builder.assert_not_called()
        M.Controller._save.assert_not_called()
        self.assertFalse(M.state.public["generating"])

    async def test_unchanged_healthy_sdr_does_not_write_or_save(self):
        await self.plugin.set_sdr_saturation(1.4)
        await self.plugin.set_sdr_enabled(True)
        await self.drain()
        self.float_writer.reset_mock()
        M.Controller._save.reset_mock()
        for _ in range(30):
            await self.plugin.set_sdr_saturation(1.4)
        await self.drain()
        self.float_writer.assert_not_called()
        M.Controller._save.assert_not_called()

    async def test_unchanged_value_can_retry_failed_apply_and_save(self):
        M.Controller._save.side_effect = OSError(28, "disk full")
        await self.plugin.set_hdr_saturation(1.3)
        self.builder.side_effect = None
        self.builder.return_value = ""
        await self.plugin.set_hdr_enabled(True)
        await self.drain()
        self.assertIn("could not", M.state.public["hdr_error"])
        self.assertIn("disk full", M.state.public["settings_error"])
        self.assertTrue(M.state.settings_dirty)
        self.builder.return_value = "/tmp/hdr-look-1.cube"
        M.Controller._save.side_effect = None
        M.Controller._save.reset_mock()
        await self.plugin.set_hdr_saturation(1.3)
        await self.drain()
        self.assertEqual(M.state.public["hdr_applied_saturation"], 1.3)
        self.assertEqual(M.state.public["hdr_error"], "")
        self.assertEqual(M.state.public["settings_error"], "")
        self.assertFalse(M.state.settings_dirty)
        M.Controller._save.assert_called_once()

    async def test_unchanged_values_rebuild_missing_hdr_and_sdr_files(self):
        M.state.sdr_neutral_atom, M.state.sdr_atom_span = 0, 1
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
        self.assertEqual(M.state.public["hdr_applied_saturation"], 1.3)
        self.assertAlmostEqual(M.state.public["sdr_look_saturation"], 1.2)

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
            generation = M.state.hdr_generation
            for _ in range(30):
                await self.plugin.set_hdr_saturation(1.3)
            self.assertEqual(M.state.hdr_generation, generation)
        finally:
            release.set()
            await self.drain()
        self.builder.assert_called_once()
        self.assertEqual(M.state.public["hdr_applied_saturation"], 1.3)

    async def test_unchanged_sdr_value_retries_a_failed_atom_write(self):
        self.float_writer.side_effect = None
        self.float_writer.return_value = False
        await self.plugin.set_sdr_saturation(1.4)
        await self.plugin.set_sdr_enabled(True)
        await self.drain()
        self.assertFalse(M.state.public["sdr_applied"])
        self.float_writer.side_effect = self.write_float
        await self.plugin.set_sdr_saturation(1.4)
        await self.drain()
        self.assertEqual(self.live_sdr, 1.4)
        self.assertTrue(M.state.public["sdr_applied"])
        self.assertEqual(M.state.public["sdr_error"], "")

    async def test_foreign_look_changed_during_owned_replacement_is_restored(self):
        M.state.sdr_neutral_atom, M.state.sdr_atom_span = 0, 1
        for component, atom, first, second, setter, enable in (
            ("hdr", M.vibrance_x11.ATOM_LOOK_PQ, 1.2, 1.4, self.plugin.set_hdr_saturation, self.plugin.set_hdr_enabled),
            ("sdr", M.vibrance_x11.ATOM_LOOK_G22, 2.2, 2.6, self.plugin.set_sdr_saturation, self.plugin.set_sdr_enabled),
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
        M.state.sdr_neutral_atom, M.state.sdr_atom_span = 0, 1
        for recover in (M.vibrance_effects.reassert_looks_if_dropped, M.vibrance_effects.reassert):
            with self.subTest(recover=recover.__name__):
                for atom in (M.vibrance_x11.ATOM_LOOK_G22, M.vibrance_x11.ATOM_LOOK_PQ):
                    self.strings[atom] = "/tmp/foreign-old.cube"
                await self.plugin.set_sdr_saturation(2.4)
                await self.plugin.set_hdr_saturation(1.3)
                await self.plugin.set_sdr_enabled(True)
                await self.plugin.set_hdr_enabled(True)
                await self.drain()
                for atom in (M.vibrance_x11.ATOM_LOOK_G22, M.vibrance_x11.ATOM_LOOK_PQ):
                    self.strings[atom] = "/tmp/foreign-new.cube"
                recover()
                await self.plugin.reset()
                for atom in (M.vibrance_x11.ATOM_LOOK_G22, M.vibrance_x11.ATOM_LOOK_PQ):
                    self.assertEqual(self.strings[atom], "/tmp/foreign-new.cube", atom)

    async def test_waking_up_docked_writes_nothing_before_the_panel_check_sees_it(self):
        M.state.sdr_neutral_atom, M.state.sdr_atom_span = 0, 1
        await self.plugin.set_sdr_saturation(2.4)
        await self.plugin.set_hdr_saturation(1.3)
        await self.plugin.set_sdr_enabled(True)
        await self.plugin.set_hdr_enabled(True)
        await self.drain()
        self.float_writer.reset_mock()
        self.string_writer.reset_mock()
        self.mock(M.vibrance_x11, "display_is_external", return_value=True)  # still believed internal
        M.vibrance_effects.reassert()
        self.float_writer.assert_not_called()
        self.string_writer.assert_not_called()

    async def test_dropped_atoms_and_own_stale_slots_cannot_replace_foreign_baselines(self):
        self.mock(M.vibrance_effects, "runtime_dir", return_value="/tmp")
        M.state.sdr_neutral_atom, M.state.sdr_atom_span = 0, 1
        self.strings.update({M.vibrance_x11.ATOM_LOOK_G22: "/tmp/foreign.cube", M.vibrance_x11.ATOM_LOOK_PQ: "/tmp/foreign.cube"})
        await self.plugin.set_sdr_saturation(2.4)
        await self.plugin.set_hdr_saturation(1.3)
        await self.plugin.set_sdr_enabled(True)
        await self.plugin.set_hdr_enabled(True)
        await self.drain()
        for atom in (M.vibrance_x11.ATOM_LOOK_G22, M.vibrance_x11.ATOM_LOOK_PQ):
            self.strings.pop(atom)
        M.vibrance_effects.reassert()
        self.strings[M.vibrance_x11.ATOM_LOOK_G22] = "/tmp/sdr-look-0.cube"
        self.strings[M.vibrance_x11.ATOM_LOOK_PQ] = "/tmp/hdr-look-0.cube"
        M.vibrance_effects.reassert_looks_if_dropped()
        await self.plugin.reset()
        for atom in (M.vibrance_x11.ATOM_LOOK_G22, M.vibrance_x11.ATOM_LOOK_PQ):
            self.assertEqual(self.strings[atom], "/tmp/foreign.cube", atom)


class SlotReuse(PluginCase):

    async def check_slot_reuse_through_rpc(self, component, values, atom):
        with tempfile.TemporaryDirectory() as folder:
            self.mock(M.vibrance_effects, "runtime_dir", return_value=folder)
            self.mock(M.vibrance_lutfile, "SIZE", 5)
            self.mock(M.vibrance_lutfile, "_cache", OrderedDict())
            self.mock(M.vibrance_effects, "write_look", side_effect=WRITE_LOOK)
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
        await self.check_slot_reuse_through_rpc("hdr", (1.1, 1.3), M.vibrance_x11.ATOM_LOOK_PQ)
        self.assertEqual(M.state.public["hdr_applied_saturation"], 1.1)
        self.assertFalse(M.state.public["generating"])

    async def test_sdr_rpc_reuses_previous_inactive_slot_and_applies_it(self):
        self.mock(M.state, "sdr_neutral_atom", 0.0)
        self.mock(M.state, "sdr_atom_span", 1.0)
        await self.check_slot_reuse_through_rpc("sdr", (2.2, 2.6), M.vibrance_x11.ATOM_LOOK_G22)
        self.assertAlmostEqual(M.state.public["sdr_look_saturation"], 1.1)
        self.assertFalse(M.state.public["sdr_generating"])


class ReassertFailures(PluginCase):

    async def prepare_looks(self):
        self.mock(M.state, "sdr_neutral_atom", 0.0)
        self.mock(M.state, "sdr_atom_span", 1.0)
        await self.plugin.set_sdr_enabled(True, 2.4)
        await self.plugin.set_hdr_enabled(True, 1.3)
        await self.drain()
        self.string_writer.reset_mock()

    async def test_resume_write_failure_is_reported_and_monitor_recovers(self):
        await self.prepare_looks()
        self.string_writer.side_effect = lambda *_: False
        M.vibrance_effects.reassert()
        self.assertTrue(M.state.public["sdr_look_error"], "failed SDR reassert was ignored")
        self.assertTrue(M.state.public["hdr_error"], "failed HDR reassert was ignored")
        self.string_writer.side_effect = self.write_string
        M.state.slow_checks = M.SLOW_CHECK_EVERY - 1   # pending looks retry on the slow tick
        await self.plugin._watch_tick()
        await self.drain()
        self.assertEqual(M.state.public["sdr_look_error"], "")
        self.assertEqual(M.state.public["hdr_error"], "")

    async def test_dropped_look_write_failure_is_reported_and_resume_recovers(self):
        await self.prepare_looks()
        self.strings = {M.vibrance_x11.ATOM_LOOK_PQ: "/tmp/foreign-pq.cube",
                        M.vibrance_x11.ATOM_LOOK_G22: "/tmp/foreign-g22.cube"}
        self.string_writer.side_effect = lambda *_: False
        M.vibrance_effects.reassert_looks_if_dropped()
        self.assertTrue(M.state.public["sdr_look_error"], "failed monitor write was ignored")
        self.assertTrue(M.state.public["hdr_error"], "failed monitor write was ignored")
        self.string_writer.side_effect = self.write_string
        M.vibrance_effects.reassert()
        self.assertEqual(M.state.public["sdr_look_error"], "")
        self.assertEqual(M.state.public["hdr_error"], "")
        await self.plugin.reset()
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_G22], "/tmp/foreign-g22.cube")
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], "/tmp/foreign-pq.cube")

    async def test_reasserting_old_looks_cannot_hide_failed_newer_replacements(self):
        await self.prepare_looks()
        self.builder.side_effect = lambda *_: ""
        await self.plugin.set_sdr_saturation(2.8)
        await self.plugin.set_hdr_saturation(1.4)
        await self.drain()
        sdr_error = M.state.public["sdr_look_error"]
        hdr_error = M.state.public["hdr_error"]
        M.vibrance_effects.reassert()
        self.assertEqual(M.state.public["sdr_look_error"], sdr_error)
        self.assertEqual(M.state.public["hdr_error"], hdr_error)
        self.assertAlmostEqual(M.state.public["sdr_look_saturation"], 1.2)
        self.assertEqual(M.state.public["hdr_applied_saturation"], 1.3)

    async def test_successful_monitor_reassert_of_old_look_keeps_replacement_error(self):
        await self.prepare_looks()
        self.builder.side_effect = lambda *_: ""
        await self.plugin.set_sdr_saturation(2.8)
        await self.plugin.set_hdr_saturation(1.4)
        await self.drain()
        errors = (M.state.public["sdr_look_error"], M.state.public["hdr_error"])
        self.strings = {}
        M.vibrance_effects.reassert_looks_if_dropped()
        self.assertEqual((M.state.public["sdr_look_error"], M.state.public["hdr_error"]), errors)

    async def test_failure_on_one_atom_does_not_mark_the_other_look_failed(self):
        await self.prepare_looks()
        def write(atom, value):
            return False if atom == M.vibrance_x11.ATOM_LOOK_PQ else self.write_string(atom, value)
        self.string_writer.side_effect = write
        M.vibrance_effects.reassert()
        self.assertEqual(M.state.public["sdr_look_error"], "")
        self.assertTrue(M.state.public["hdr_error"])

    async def check_neutral_recovery(self, reassert):
        await self.prepare_looks()
        self.string_writer.side_effect = lambda *_: False
        await self.plugin.set_sdr_saturation(2.0)
        await self.plugin.set_hdr_saturation(1.0)
        await self.drain()
        self.assertTrue(M.state.public["sdr_look_error"])
        self.assertTrue(M.state.public["hdr_error"])
        self.builder.reset_mock()
        self.string_writer.side_effect = self.write_string
        reassert()
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_G22], "", "old SDR look was reasserted after neutral request")
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], "", "old HDR look was reasserted after neutral request")
        self.assertEqual(M.state.public["sdr_look_error"], "")
        self.assertEqual(M.state.public["hdr_error"], "")
        self.builder.assert_not_called()

    async def test_resume_retries_a_pending_neutral_clear_instead_of_old_look(self):
        await self.check_neutral_recovery(M.vibrance_effects.reassert)

    async def test_monitor_retries_a_pending_neutral_clear_instead_of_old_look(self):
        def dropped():
            self.strings = {}
            M.vibrance_effects.reassert_looks_if_dropped()
        await self.check_neutral_recovery(dropped)

    async def test_monitor_clears_neutral_even_if_the_old_path_is_still_present(self):
        await self.check_neutral_recovery(M.vibrance_effects.reassert_looks_if_dropped)


class FrontendContract(PluginTest):

    def test_every_key_the_menu_reads_is_in_the_backend_state(self):
        """A key renamed on one side arrives as undefined on the other, and
        the menu silently hides what depends on it (the HDR section, say)."""
        source = (Path(__file__).resolve().parent.parent / "src" / "index.tsx").read_text(encoding="utf-8")
        body = re.search(r"interface State \{(.*?)\}", source, re.S).group(1)
        keys = set(re.findall(r"^\s*(\w+)\s*:", body, re.M))
        self.assertGreater(len(keys), 20)
        self.assertLessEqual(keys, set(M.state.public))


class LookOwnership(PluginCase):
    async def test_disable_restores_when_the_atom_shows_our_other_slot(self):
        # A replacement whose reply was lost still landed: the atom names our
        # newer slot, not the one we track. That is ours, not a foreign baseline.
        with tempfile.TemporaryDirectory() as folder:
            self.mock(M.vibrance_effects, "runtime_dir", return_value=folder)
            M.state.hdr_look_path = os.path.join(folder, "hdr-look-0.cube")
            M.state.hdr_look_baseline = "/foreign.cube"
            self.strings[M.vibrance_x11.ATOM_LOOK_PQ] = os.path.join(folder, "hdr-look-1.cube")
            self.assertTrue(M.vibrance_effects.release_look("hdr"))
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], "/foreign.cube")

    async def test_a_superseded_build_is_not_reported_as_a_failure(self):
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3)
        def cancelled_build(*_args):
            M.state.hdr_generation += 1  # a newer slider move arrived
            return ""
        self.builder.side_effect = cancelled_build
        self.plugin._schedule_look("hdr")
        await self.drain()
        self.assertEqual(M.state.public["hdr_error"], "")
        self.assertEqual(M.state.look_failures["hdr"], 0)

    async def test_returning_to_100_percent_gives_the_atom_back_without_a_build(self):
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3, hdr_applied=True,
                              hdr_applied_saturation=1.3)
        M.state.hdr_look_path = "/tmp/hdr-look-0.cube"
        M.state.hdr_look_baseline = "/foreign.cube"
        self.strings[M.vibrance_x11.ATOM_LOOK_PQ] = "/tmp/hdr-look-0.cube"
        await self.plugin.set_hdr_saturation(1.0)
        await self.drain()
        self.builder.assert_not_called()
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], "/foreign.cube")
        self.assertFalse(M.state.public["hdr_applied"])

    async def test_failed_hdr_release_is_retried_on_the_slow_tick(self):
        M.state.hdr_look_path = "/tmp/hdr-look-0.cube"
        self.strings[M.vibrance_x11.ATOM_LOOK_PQ] = "/tmp/hdr-look-0.cube"
        self.string_writer.side_effect = lambda atom, value: False
        self.assertFalse(M.vibrance_effects.release_look("hdr"))
        self.string_writer.side_effect = self.write_string
        M.state.slow_checks = M.SLOW_CHECK_EVERY - 1
        M.Controller._watch_once(None)
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], "")
        self.assertIsNone(M.state.hdr_look_path)

    async def test_preserved_session_hands_a_foreign_look_its_baseline(self):
        M.state.hdr_look_path = "/tmp/hdr-look-0.cube"
        M.state.public["hdr_applied"] = True
        self.strings[M.vibrance_x11.ATOM_LOOK_PQ] = "/foreign.cube"
        M.vibrance_effects.capture_existing_looks(True)
        self.assertEqual(M.state.hdr_look_baseline, "/foreign.cube")
        self.assertIsNone(M.state.hdr_look_path)
        self.assertFalse(M.state.public["hdr_applied"])


class SdrRecovery(PluginCase):
    async def test_watch_retries_a_newer_value_whose_write_never_landed(self):
        M.state.public.update(sdr_enabled=True, sdr_applied=True, sdr_saturation=1.8,
                              sdr_error="could not write SDR to gamescope")
        M.state.last_sdr_written = self.live_sdr = 1.5
        M.state.last_sdr_attempted = 1.8
        M.Controller._watch_once(None)
        self.assertAlmostEqual(self.live_sdr, 1.8)
        self.assertEqual(M.state.public["sdr_error"], "")

    async def test_release_with_nothing_held_clears_a_stale_restore_error(self):
        M.state.public.update(sdr_enabled=False, sdr_applied=False,
                              sdr_error="waiting for gamescope before restoring SDR")
        M.vibrance_effects.release_sdr()
        self.assertEqual(M.state.public["sdr_error"], "")


class SdrRewrite(PluginCase):
    async def test_the_same_slider_value_rewrites_an_atom_holding_an_older_target(self):
        M.state.public.update(sdr_enabled=True, sdr_saturation=1.5, sdr_applied=True, sdr_error="")
        M.state.last_sdr_written = self.live_sdr = 1.2     # e.g. written before the mapping moved
        await self.plugin.set_sdr_saturation(1.5)
        await self.drain()
        self.assertEqual(self.live_sdr, 1.5)


class LookReleaseGaps(PluginCase):
    """Two lines of look ownership that nothing else would notice going missing."""

    async def test_a_look_that_replaced_ours_is_restored_after_we_go_off_and_on(self):
        # release_look's "somebody else replaced us" branch must keep their look
        # as the baseline; later reads can fail and never learn it again.
        other = "/opt/other/look.cube"
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3, hdr_applied=True,
                              hdr_applied_saturation=1.3)
        M.state.hdr_look_path = "/tmp/hdr-look-0.cube"
        self.strings[M.vibrance_x11.ATOM_LOOK_PQ] = other
        await self.plugin.set_hdr_enabled(False)
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], other)
        M.vibrance_x11.read_string_atom.side_effect = lambda atom: None   # reads now fail
        await self.plugin.set_hdr_enabled(True)
        await self.drain()
        self.assertTrue(self.strings[M.vibrance_x11.ATOM_LOOK_PQ].startswith("/tmp/hdr-look-"))
        M.vibrance_x11.read_string_atom.side_effect = lambda atom: self.strings.get(atom)
        await self.plugin.set_hdr_enabled(False)
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], other)

    async def test_our_own_live_slot_is_claimed_so_a_failed_build_still_clears_it(self):
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3)
        self.strings[M.vibrance_x11.ATOM_LOOK_PQ] = "/tmp/hdr-look-1.cube"    # ours, unowned in memory
        self.builder.side_effect = lambda *_args: ""             # the build fails
        self.plugin._schedule_look("hdr")
        await self.drain()
        self.assertEqual(M.state.hdr_look_slot, 1)
        await self.plugin.set_hdr_enabled(False)
        self.assertFalse(self.strings.get(M.vibrance_x11.ATOM_LOOK_PQ))


class LookCommit(PluginCase):
    """Kills CM2, CM6."""

    async def test_look_failures_count_a_failed_write_and_reset_on_success(self):
        self.string_writer.side_effect = lambda *_args: False
        M.vibrance_effects.commit_look("hdr", "/tmp/hdr-look-1.cube", 1, 1.2)
        self.assertEqual(M.state.look_failures["hdr"], 1)
        self.string_writer.side_effect = self.write_string
        M.vibrance_effects.commit_look("hdr", "/tmp/hdr-look-0.cube", 0, 1.2)
        self.assertEqual(M.state.look_failures["hdr"], 0)


class HdrLookStuckAfterPanelLosesPq(PluginCase):
    async def test_a_failed_release_when_the_hdr_half_goes_away_is_retried(self):
        """The panel stops advertising PQ while an HDR look is applied (an EDID
        that only became readable later, say). _apply_both releases the look
        once; if that write fails nothing retries it - the slow tick only
        retries while hdr_enabled is False, _retry_pending_looks and the
        reassert skip a half that is not look_enabled, and the panel cache is
        complete - and the frontend hides the HDR toggle, so the saturated
        look stays on screen until Reset or reload.
        """
        path = "/tmp/hdr-look-0.cube"
        M.state.hdr_look_path = path
        self.strings[M.vibrance_x11.ATOM_LOOK_PQ] = path
        M.state.public.update(hdr_enabled=True, hdr_applied=True,
                              hdr_applied_saturation=1.3, hdr_saturation=1.3)

        def lose_pq(*_):
            M.state.public["hdr_supported"] = False
            return True
        self.mock(M.vibrance_panel, "configure_for_panel", side_effect=lose_pq)
        writes = iter([False])                        # one transient failure
        self.string_writer.side_effect = lambda atom, value: (
            next(writes, True) and self.write_string(atom, value))
        await self.plugin._refresh_panel(probe=(False, ("edid", "p", 1), None))
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], path)   # first try failed

        for _ in range(2 * M.SLOW_CHECK_EVERY):     # half a minute of watch ticks
            await self.plugin._watch_tick()
        await self.drain()
        self.assertIn(self.strings.get(M.vibrance_x11.ATOM_LOOK_PQ), (None, ""),
                      "HDR look never released")


class LookBuildFailureLogging(PluginCase):
    async def test_a_lasting_build_failure_is_logged_once_not_every_retry(self):
        """A runtime folder that cannot be written (full or read-only storage)
        fails every build. The retry runs on each slow tick (~16 s) and every
        attempt logs "could not write look" - about 225 lines an hour - while
        settings saves and the monitor already log a lasting failure once.
        """
        self.builder.side_effect = WRITE_LOOK         # the real one
        self.mock(M.vibrance_lutfile, "write", side_effect=OSError(28, "No space left on device"))
        error = self.mock(M.vibrance_state.logger, "error")
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3)
        for _ in range(3):
            self.plugin._schedule_look("hdr")
            await self.drain()
        logged = [c.args[0] for c in error.call_args_list if "could not write look" in c.args[0]]
        self.assertEqual(len(logged), 1, logged)
        self.assertIn("could not build", M.state.public["hdr_error"])


class ToggleArguments(PluginCase):
    async def test_switches_take_only_booleans_and_clamp_the_carried_value(self):
        for setter in (self.plugin.set_sdr_enabled, self.plugin.set_hdr_enabled):
            with self.assertRaises(ValueError):
                await setter("false")
            with self.assertRaises(ValueError):
                await setter(True, math.nan)
        await self.plugin.set_sdr_enabled(True, 99.0)
        self.assertEqual(M.state.public["sdr_saturation"], M.state.public["sdr_max"])
        await self.plugin.set_hdr_enabled(True, 99.0)
        self.assertEqual(M.state.public["hdr_saturation"], M.HDR_MAX)


class UntouchedAtom(PluginCase):
    async def test_releasing_sdr_that_was_never_applied_writes_nothing(self):
        """The slow tick releases a disabled SDR half every time; an atom we
        never wrote belongs to gamescope or another tool."""
        M.state.public["sdr_enabled"] = False
        self.assertTrue(M.vibrance_effects.release_sdr())
        self.float_writer.assert_not_called()


class Debounce(PluginCase):
    """PluginCase sets DEBOUNCE_S and SDR_COALESCE_S to 0, so nothing else waits."""

    async def test_a_worker_waits_until_its_due_time(self):
        M.state.due["probe"] = time.monotonic() + 0.1
        started = time.monotonic()
        await self.plugin._debounce("probe")
        self.assertGreaterEqual(time.monotonic() - started, 0.09)


class TaskBookkeeping(PluginCase):
    async def test_finished_workers_leave_the_task_list(self):
        await self.plugin.set_hdr_saturation(1.3)
        await self.drain()
        await asyncio.sleep(0)
        self.assertEqual(M.state.tasks, [])


class BuildingIndicator(PluginCase):
    async def test_a_new_hdr_value_shows_building_until_the_lut_lands(self):
        M.state.public["hdr_enabled"] = True
        await self.plugin.set_hdr_saturation(1.3)
        self.assertTrue(M.state.public["generating"])
        await self.drain()
        self.assertFalse(M.state.public["generating"])
        await self.plugin.set_hdr_saturation(1.0)          # identity: nothing to build
        self.assertFalse(M.state.public["generating"])


class RuntimeFolder(PluginTest):
    def test_luts_go_to_decky_s_runtime_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = os.path.join(folder, "data", "DeckyVibranceHDR")
            with patch.dict(M.os.environ, {"DECKY_PLUGIN_RUNTIME_DIR": runtime}):
                self.assertEqual(M.vibrance_effects.runtime_dir(), runtime)
            self.assertTrue(os.path.isdir(runtime))


class RpcReplies(PluginCase):
    """Every setter's reply is what the frontend renders next (stateSync stores it)."""

    async def test_every_setter_answers_with_the_new_state(self):
        for call in (lambda: self.plugin.set_sdr_enabled(True), lambda: self.plugin.set_hdr_enabled(True),
                     lambda: self.plugin.set_sdr_saturation(1.5), lambda: self.plugin.set_hdr_saturation(1.2),
                     lambda: self.plugin.set_sdr_saturation(1.5), lambda: self.plugin.set_hdr_saturation(1.2),
                     self.plugin.reset):
            reply = await call()
            self.assertEqual(reply, M.state.public)
        await self.drain()


class ResetAndPersistence(PluginCase):
    async def test_reset_returns_every_control_to_its_default_and_saves_it(self):
        M.state.public.update(sdr_enabled=True, sdr_saturation=2.0, hdr_enabled=True, hdr_saturation=1.3)
        await self.plugin.reset()
        await self.drain()
        self.assertEqual({k: M.state.public[k] for k in M.DEFAULT_SETTINGS}, M.DEFAULT_SETTINGS)
        self.assertEqual(M.Controller._save.call_args.args[0], M.DEFAULT_SETTINGS)

    async def test_switching_hdr_and_moving_sdr_are_saved(self):
        await self.plugin.set_hdr_enabled(True, 1.2)
        await self.drain()
        self.assertTrue(M.Controller._save.call_args.args[0]["hdr_enabled"])
        await self.plugin.set_sdr_saturation(1.7)
        await self.drain()
        self.assertEqual(M.Controller._save.call_args.args[0]["sdr_saturation"], 1.7)


class SaveWrites(PluginTest):
    def test_a_debounced_snapshot_reaches_the_disk_while_running(self):
        M.state.settings_loaded = True
        M.state.unloading = False
        with patch.object(M.vibrance_settings, "write") as write:
            M.Controller._save({"sdr_enabled": True})
        write.assert_called_once_with(M.settings_path, {"sdr_enabled": True})


class WorkersDebounce(PluginCase):
    async def test_a_quick_second_value_replaces_the_first_before_any_build(self):
        self.mock(M, "DEBOUNCE_S", 0.4)
        M.state.public["hdr_enabled"] = True
        await self.plugin.set_hdr_saturation(1.2)
        await asyncio.sleep(0.02)                      # long enough for an undebounced build
        await self.plugin.set_hdr_saturation(1.4)
        await self.drain()
        self.assertEqual([c.args[0] for c in self.builder.call_args_list], [1.4])

    async def test_a_quick_sdr_drag_writes_the_atom_and_the_file_once(self):
        self.mock(M, "SDR_COALESCE_S", 0.4)
        M.state.public["sdr_enabled"] = True
        await self.plugin.set_sdr_saturation(1.5)
        await asyncio.sleep(0.02)
        await self.plugin.set_sdr_saturation(1.7)
        await self.drain()
        self.assertEqual([c.args[1] for c in self.float_writer.call_args_list], [M.vibrance_effects.sdr_split(1.7)[0]])
        self.assertEqual(M.Controller._save.call_count, 1)

    async def test_a_disabled_hdr_half_never_shows_building(self):
        M.state.public["hdr_enabled"] = False
        await self.plugin.set_hdr_saturation(1.4)
        self.assertFalse(M.state.public["generating"])
        await self.drain()


class LookFailureLogEdges(PluginCase):
    async def test_a_failure_that_comes_back_after_a_good_build_is_logged_again(self):
        self.builder.side_effect = WRITE_LOOK
        full = OSError(28, "No space left on device")
        outcomes = iter([full, True, full])
        def write(*_):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        self.mock(M.vibrance_lutfile, "write", side_effect=write)
        error = self.mock(M.vibrance_state.logger, "error")
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3)
        for _ in range(3):
            self.plugin._schedule_look("hdr")
            await self.drain()
        logged = [c.args[0] for c in error.call_args_list if "could not write look" in c.args[0]]
        self.assertEqual(len(logged), 2, logged)

    async def test_each_half_logs_its_own_lasting_failure_once(self):
        self.builder.side_effect = WRITE_LOOK
        self.mock(M.vibrance_lutfile, "write",
                  side_effect=lambda path, *_: (_ for _ in ()).throw(OSError(28, "No space left on device", path)))
        error = self.mock(M.vibrance_state.logger, "error")
        # 4.0 is past the atom's span (2.0 here), so the SDR half needs a look too.
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3, sdr_enabled=True, sdr_saturation=4.0)
        for _ in range(3):
            self.plugin._schedule_look("hdr")
            self.plugin._schedule_look("sdr")
            await self.drain()
        logged = [c.args[0] for c in error.call_args_list if "could not write look" in c.args[0]]
        self.assertEqual(len(logged), 2, logged)
        self.assertEqual(sum("hdr-look" in line for line in logged), 1, logged)


class InvalidatedBuildClearsBuilding(PluginCase):
    """A build superseded by a disconnect never clears its own flag (its token
    is stale), so the invalidation must; otherwise the menu says "building"
    forever and _retry_pending_looks skips the half after a preserved reconnect."""

    async def check(self, component, flag):
        self.mock(M, "DEBOUNCE_S", 0.05)
        self.plugin._schedule_look(component)
        self.assertTrue(M.state.public[flag])
        M.Controller._disconnect()
        await self.drain()
        self.assertFalse(M.state.public[flag])
        M.state.disconnected = False                 # same session comes back
        self.plugin._retry_pending_looks(component)
        await self.drain()
        self.assertTrue(self.builder.called)

    async def test_hdr(self):
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3)
        await self.check("hdr", "generating")

    async def test_sdr(self):
        M.vibrance_panel.use_mapping(False)          # standard panel: the look carries 250%
        M.state.public.update(sdr_enabled=True, sdr_saturation=2.5)
        await self.check("sdr", "sdr_generating")


class SwitchingOffWhileDocked(PluginCase):
    """A switch-off queued behind a slow request can land after docking; it
    must still switch the half off, or undocking brings the effect back."""

    async def test_both_halves_switch_off_while_docked(self):
        M.state.public.update(sdr_enabled=True, hdr_enabled=True, external_display=True)
        await self.plugin.set_sdr_enabled(False)
        await self.plugin.set_hdr_enabled(False)
        self.assertFalse(M.state.public["sdr_enabled"])
        self.assertFalse(M.state.public["hdr_enabled"])


class TrackedTaskLogging(PluginCase):
    """Only a task that really failed is logged: not every finished slider job,
    and a cancelled one must not raise inside the done callback."""

    async def test_only_a_failed_task_is_logged(self):
        error = self.mock(M.vibrance_state.logger, "error")
        raised = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: raised.append(context))
        async def fine(): return 1
        async def broken(): raise OSError("boom")
        async def stuck(): await asyncio.sleep(10)
        tasks = [asyncio.create_task(c()) for c in (fine, broken, stuck)]
        for task in tasks:
            M.Controller._track(task)
        tasks[2].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)                                   # done callbacks
        logged = [c.args[0] for c in error.call_args_list]
        self.assertEqual(len(logged), 1, logged)
        self.assertIn("boom", logged[0])
        self.assertEqual(M.state.tasks, [])
        self.assertEqual(raised, [])                             # no error inside the callback


# What the panel and the log show of a failure: never the folders of a path (they hold the user name).
VANISHED = FileNotFoundError(2, "No such file or directory", "/home/someone/homebrew/x.cube")


class SdrApplyCrashIsShown(PluginCase):
    async def test_an_sdr_write_that_raises_is_reported_in_the_menu(self):
        M.state.public.update(sdr_enabled=True, sdr_applied=True)
        self.mock(M.vibrance_effects, "apply_sdr", side_effect=VANISHED)
        with self.assertLogs(M.vibrance_state.logger, "ERROR") as logged:
            await self.plugin.set_sdr_saturation(1.6)
            await self.drain()
        self.assertEqual("could not apply SDR: No such file or directory", M.state.public["sdr_error"])
        self.assertIn("No such file or directory: x.cube", logged.output[0])
        self.assertIn("Traceback", logged.output[0])
        self.assertNotIn("someone", "".join(logged.output).split("Traceback")[0])


class LookCrashIsCountedAndShown(PluginCase):
    async def test_a_look_commit_that_raises_is_reported_and_counted(self):
        M.state.public.update(hdr_enabled=True)
        self.mock(M.vibrance_effects, "commit_look", side_effect=VANISHED)
        with self.assertLogs(M.vibrance_state.logger, "ERROR") as logged:
            await self.plugin.set_hdr_saturation(1.3)
            await self.drain()
        self.assertEqual("could not apply the HDR LUT: No such file or directory", M.state.public["hdr_error"])
        self.assertIn("HDR look failed: No such file or directory: x.cube", logged.output[0])
        self.assertEqual(M.state.look_failures["hdr"], 1)
        self.assertFalse(M.state.public["generating"])


class FreshState(PluginTest):
    """StateGuard calls state.reset() on the one long-lived object, so a field
    reset() forgot keeps the previous test's value and every suite still passes;
    a fresh process (Decky starts one per load) would then raise AttributeError."""

    def test_a_fresh_state_has_every_field_the_plugin_reads(self):
        import ast, inspect
        fresh = M.vibrance_state.PluginState()
        used = {name for look in M.vibrance_effects.LOOKS.values()
                for key, name in look.items() if key in ("path", "baseline", "slot", "generation")}
        for module in (M, M.vibrance_effects, M.vibrance_panel):
            for node in ast.walk(ast.parse(inspect.getsource(module))):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id == "state" and not callable(getattr(type(fresh), node.attr, None))):
                    used.add(node.attr)
        self.assertEqual(sorted(name for name in used if not hasattr(fresh, name)), [])

    def test_a_fresh_state_is_a_reset_one(self):
        fresh, reset = M.vibrance_state.PluginState(), M.vibrance_state.PluginState()
        reset.public["ready"] = True
        reset.reset()
        self.assertEqual(fresh.public, reset.public)
        self.assertFalse(fresh.public["ready"])

    def test_restore_needs_a_matching_session_once_one_is_known(self):
        with patch.object(M.vibrance_x11, "session_identity", return_value=(":0", (1, 1))):
            fresh = M.vibrance_state.PluginState()
            self.assertTrue(fresh.can_restore_session())        # nothing known, connected
            fresh.disconnected = True
            self.assertFalse(fresh.can_restore_session())
            fresh.session_identity = (":0", (1, 1))
            self.assertTrue(fresh.can_restore_session())
            fresh.session_identity = (":0", (2, 2))
            self.assertFalse(fresh.can_restore_session())


class EffectsReporting(PluginCase):
    async def test_a_failed_sdr_write_is_shown(self):
        M.state.public.update(sdr_enabled=True)
        self.float_writer.side_effect = lambda atom, value: False
        M.vibrance_effects.apply_sdr()
        self.assertEqual(M.state.public["sdr_error"], "could not write SDR to gamescope")
        self.assertFalse(M.state.public["sdr_applied"])

    async def test_switching_off_after_another_writer_took_over_forgets_our_values(self):
        M.state.public.update(sdr_enabled=True, sdr_applied=True, sdr_error="old trouble")
        M.state.last_sdr_written = M.state.last_sdr_attempted = 1.5
        self.live_sdr = 0.7                                    # somebody else's now
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.7)
        self.assertFalse(M.state.public["sdr_applied"])
        self.assertIsNone(M.state.last_sdr_written)
        self.assertIsNone(M.state.last_sdr_attempted)
        self.assertEqual(M.state.public["sdr_error"], "")
        await self.plugin.set_sdr_enabled(True)                # their value is the new baseline
        self.assertEqual(M.state.sdr_baseline, 0.7)

    async def test_releasing_nothing_on_a_replaced_server_does_not_wait(self):
        M.state.session_identity = (":0", (1, 1))
        self.mock(M.vibrance_x11, "session_identity", return_value=(":0", (2, 2)))
        M.state.public.update(sdr_enabled=False, sdr_applied=False)
        M.vibrance_effects.release_sdr()
        self.assertEqual(M.state.public["sdr_error"], "")

    async def test_a_look_waiting_for_a_replaced_server_says_so(self):
        M.state.session_identity = (":0", (1, 1))
        self.mock(M.vibrance_x11, "session_identity", return_value=(":0", (2, 2)))
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3)
        M.vibrance_effects.commit_look("hdr", "/tmp/hdr-look-1.cube", 1, 1.3)
        self.assertIn("waiting for gamescope", M.state.public["hdr_error"])
        M.state.public["hdr_error"] = ""
        M.vibrance_effects.reapply_look("hdr", "/tmp/hdr-look-1.cube", None)
        self.assertIn("waiting for gamescope", M.state.public["hdr_error"])
        self.string_writer.assert_not_called()

    async def test_a_look_still_in_place_is_not_rewritten_on_the_slow_tick(self):
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3, hdr_applied=True)
        M.state.hdr_look_path = self.strings[M.vibrance_x11.ATOM_LOOK_PQ] = "/tmp/hdr-look-1.cube"
        M.vibrance_effects.reassert_looks_if_dropped()
        self.string_writer.assert_not_called()                # a rewrite makes gamescope reload it

    async def test_going_neutral_forgets_the_last_lut_sent(self):
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3)
        await self.plugin.set_hdr_saturation(1.3)
        await self.drain()
        self.assertEqual(M.state.public["hdr_applied_saturation"], 1.3)
        await self.plugin.set_hdr_saturation(1.0)
        await self.drain()
        self.assertIsNone(M.state.public["hdr_applied_saturation"])   # not "Last LUT sent: 130%"
