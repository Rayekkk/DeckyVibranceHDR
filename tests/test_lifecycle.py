# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Startup, suspend/resume, monitoring, session recovery and unload."""

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _fixtures import PluginCase, SAVE_SETTINGS, drain  # installs the loader stand-ins
import main  # noqa: E402
import main as M  # noqa: E402


class Recovery(PluginCase):
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
        real_mono = M.time.monotonic
        self.mock(M.time, "monotonic", side_effect=lambda: real_mono() + ticks)
        self.mock(M, "_boottime", side_effect=lambda: real_mono() + ticks)
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


class Reassert(unittest.TestCase):
    """gamescope drops these atoms when it re-initialises, which it does across
    a suspend. Telling that apart from another plugin holding the other end is
    the whole job here - the first heals itself, the second needs saying."""

    def setUp(self):
        self.written = []
        self.live = [2.0]
        self._read, self._write = main._read_float_atom, main._write_float_atom
        self._other = main._other_writer_present

        def fake_write(name, value):
            self.written.append(value)
            self.live[0] = value
            return True

        main._read_float_atom = lambda name: self.live[0]
        main._write_float_atom = fake_write
        main._other_writer_present = lambda: False
        self._looks = main.Plugin._reassert_looks_if_dropped
        main.Plugin._reassert_looks_if_dropped = staticmethod(lambda: None)
        main.Plugin._slow_checks = 0
        main.Plugin._other_writer = False
        main.Plugin._state.update({
            "sdr_enabled": True, "sdr_applied": True,
            "sdr_saturation": 2.0, "conflict": "",
        })
        main.Plugin._last_sdr_written = 2.0
        main.Plugin._reassert_strikes = 0

    def tearDown(self):
        main._read_float_atom, main._write_float_atom = self._read, self._write
        main._other_writer_present = self._other
        main.Plugin._reassert_looks_if_dropped = self._looks
        main.Plugin._slow_checks = 0
        main.Plugin._state.update({
            "sdr_enabled": False, "sdr_applied": False, "conflict": "",
        })
        main.Plugin._last_sdr_written = None
        main.Plugin._reassert_strikes = 0

    def test_a_dropped_value_is_put_back_without_crying_conflict(self):
        self.live[0] = 1.0                      # gamescope re-initialised
        main.Plugin._watch_once()
        self.assertEqual(self.written, [2.0])
        self.assertEqual(main.Plugin._state["conflict"], "")

    def test_something_holding_the_other_end_gets_named(self):
        for _ in range(main.CONFLICT_STRIKES):  # overwritten after every put-back
            self.live[0] = 0.5
            main.Plugin._watch_once()
        self.assertIn("vibrantDeck", main.Plugin._state["conflict"])

    def test_one_strike_short_stays_quiet(self):
        for _ in range(main.CONFLICT_STRIKES - 1):
            self.live[0] = 0.5
            main.Plugin._watch_once()
        self.assertEqual(main.Plugin._state["conflict"], "")

    def test_agreement_clears_the_count(self):
        self.live[0] = 1.0
        main.Plugin._watch_once()
        self.assertEqual(main.Plugin._reassert_strikes, 1)
        main.Plugin._watch_once()               # agrees now, we just wrote it
        self.assertEqual(main.Plugin._reassert_strikes, 0)
        self.assertEqual(main.Plugin._state["conflict"], "")

    def test_a_disabled_half_is_left_alone(self):
        main.Plugin._state["sdr_enabled"] = False
        self.live[0] = 0.5
        main.Plugin._watch_once()
        self.assertEqual(self.written, [])

    def test_a_writer_on_another_server_is_named_even_when_we_agree(self):
        """The case our readback cannot see: gamescope applies a write from any
        root, but we only ever read one of them, so the value we read back can
        agree with us while the picture shows somebody else's."""
        main._other_writer_present = lambda: True
        main.Plugin._reassert_looks_if_dropped = staticmethod(lambda: None)
        main.Plugin._slow_checks = main.SLOW_CHECK_EVERY - 1
        main.Plugin._watch_once()
        self.assertIn("different X server", main.Plugin._state["conflict"])


class SuspendDetection(unittest.TestCase):

    def test_boottime_is_usable_everywhere(self):
        """CLOCK_BOOTTIME is Linux only; the fallback keeps the detector idle
        rather than raising on a dev box."""
        self.assertIsInstance(main._boottime(), float)

    def test_slack_exceeds_the_poll_interval(self):
        """Otherwise the jitter of an ordinary tick reads as a suspend and the
        values get re-pushed every second forever."""
        self.assertGreater(main.SUSPEND_SLACK_S, main.SUSPEND_POLL_S)

    def test_the_retry_ladder_is_staggered_and_reaches_past_startup(self):
        delays = main.REASSERT_DELAYS_S
        self.assertEqual(list(delays), sorted(delays))
        self.assertGreater(sum(delays), 10.0)


class IsolatedStartup(unittest.TestCase):
    """Startup fixtures must never adopt atoms from the developer's live display."""

    def setUp(self):
        from unittest.mock import patch
        state = dict(main.Plugin._state)
        state.update(sdr_applied=False, sdr_look_applied=False, hdr_applied=False)
        patches = [(main, "_read_string_atom", lambda _name: None),
                   (main, "_read_float_atom", lambda _name: 1.0),
                   (main, "_write_string_atom", lambda *_args: True),
                   (main, "_write_float_atom", lambda *_args: True),
                   (main, "_display_is_external", lambda: False),
                   (main, "_display_session_identity", lambda: None),
                   (main.Plugin, "_state", state),
                   (main.Plugin, "_session_identity", None),
                   (main.Plugin, "_disconnected", False),
                   (main.Plugin, "_settings_loaded", True),
                   (main.Plugin, "_sdr_look_path", None),
                   (main.Plugin, "_hdr_look_path", None),
                   (main.Plugin, "_sdr_look_baseline", None),
                   (main.Plugin, "_hdr_look_baseline", None)]
        for obj, name, value in patches:
            item = patch.object(obj, name, value)
            item.start()
            self.addCleanup(item.stop)


class StartupRetry(IsolatedStartup):
    """Decky starts plugins as a system service and can win the race against
    the session; giving up permanently meant a reinstall to recover."""

    def test_the_backoff_is_bounded_and_starts_quickly(self):
        self.assertGreater(main.GAMESCOPE_RETRY_FIRST_S, 0.0)
        self.assertLessEqual(main.GAMESCOPE_RETRY_FIRST_S, 2.0)
        self.assertGreater(main.GAMESCOPE_RETRY_MAX_S,
                           main.GAMESCOPE_RETRY_FIRST_S)
        self.assertLessEqual(main.GAMESCOPE_RETRY_MAX_S, 60.0)

    def test_startup_keeps_trying_until_gamescope_turns_up(self):
        attempts = []
        real_pick, real_sleep = main._pick_display, asyncio.sleep
        real_wide, real_pq = main._panel_is_wide_gamut, main._panel_supports_hdr
        real_read = main._read_float_atom
        saved = (main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN)

        def flaky():
            attempts.append(1)
            return ":0" if len(attempts) >= 3 else None

        async def no_wait(_seconds):
            return None

        main._pick_display = flaky
        main._panel_is_wide_gamut = lambda *_args: True
        main._panel_supports_hdr = lambda *_args: True
        main._read_float_atom = lambda name: 1.0
        from unittest.mock import patch
        count_patch = patch.object(main, "_count_gamescope_atoms", return_value=44)
        count_patch.start()
        asyncio.sleep = no_wait
        try:
            drain(main.Plugin()._start_when_ready())
        finally:
            main._pick_display, asyncio.sleep = real_pick, real_sleep
            main._panel_is_wide_gamut, main._panel_supports_hdr = real_wide, real_pq
            main._read_float_atom = real_read
            count_patch.stop()
            main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = saved
        self.assertEqual(len(attempts), 3)
        self.assertTrue(main.Plugin._state["ready"])


class StartupSettles(IsolatedStartup):
    """gamescope publishes its atoms as it comes up. Writing into a half-built
    session gets thrown away when it finishes, which showed up as a Legion Go S
    coming back from a restart with the atom applied and the look missing -
    exactly the part of the slider the atom cannot reach."""

    def test_it_waits_for_the_count_to_stop_growing(self):
        counts = iter([20, 30, 44, 44, 44])
        seen = []

        def counter(display):
            value = next(counts)
            seen.append(value)
            return value

        real_count, real_sleep = main._count_gamescope_atoms, asyncio.sleep
        real_pick = main._pick_display
        real_wide, real_pq = main._panel_is_wide_gamut, main._panel_supports_hdr
        real_read, real_write = main._read_float_atom, main._write_float_atom
        real_string = main._write_string_atom
        saved = (main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN)

        async def no_wait(_seconds):
            return None

        main._count_gamescope_atoms = counter
        main._pick_display = lambda: ":0"
        main._panel_is_wide_gamut = lambda *_args: True
        main._panel_supports_hdr = lambda *_args: True
        main._read_float_atom = lambda name: 1.0
        main._write_float_atom = lambda name, value: True
        main._write_string_atom = lambda name, value: True
        asyncio.sleep = no_wait
        try:
            drain(main.Plugin()._start_when_ready())
        finally:
            main._count_gamescope_atoms, asyncio.sleep = real_count, real_sleep
            main._pick_display = real_pick
            main._panel_is_wide_gamut, main._panel_supports_hdr = real_wide, real_pq
            main._read_float_atom, main._write_float_atom = real_read, real_write
            main._write_string_atom = real_string
            main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = saved
        # Stops on the first repeat rather than reading forever.
        self.assertEqual(seen, [20, 30, 44, 44])

    def test_the_settle_wait_is_bounded(self):
        """A session that never stops growing must not hold startup for ever."""
        self.assertGreater(main.GAMESCOPE_SETTLE_TRIES, 2)
        self.assertLessEqual(
            main.GAMESCOPE_SETTLE_TRIES * main.GAMESCOPE_SETTLE_S, 30.0)

    def test_startup_puts_the_values_back_more_than_once(self):
        """One write into a settling session is not enough; the same ladder the
        resume path uses is what makes it stick."""
        with open(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "main.py"), encoding="utf-8") as source_file:
            source = source_file.read()
        start = source.index("async def _start_when_ready")
        end = source.index("_monitor_loop()", start)
        self.assertIn("_reassert_repeatedly", source[start:end])

    def test_a_disabled_half_clears_what_the_last_run_left(self):
        """The look atom outlives a plugin reload, and nothing reloads it into
        our state - so an owned path has to be cleared, not assumed."""
        cleared = []
        real_write, real_read = main._write_string_atom, main._read_string_atom
        path = os.path.join(main._runtime_dir(), "sdr-look-0.cube")
        main._write_string_atom = lambda name, value: (
            cleared.append((name, value)) or True)
        main._read_string_atom = lambda name: path
        main.Plugin._state["sdr_look_applied"] = True
        main.Plugin._sdr_look_path = path
        try:
            main.Plugin._release_look("sdr")
        finally:
            main._write_string_atom, main._read_string_atom = real_write, real_read
            main.Plugin._sdr_look_path = None
        self.assertEqual(cleared, [(main.ATOM_LOOK_G22, "")])


class StartupAssertsOff(IsolatedStartup):
    """Startup clears stale paths owned by us, but never somebody else's."""

    def _boot_with(self, sdr_on, hdr_on, own_looks=True):
        written = []
        real = {name: getattr(main, name) for name in
                ("_pick_display", "_panel_is_wide_gamut", "_panel_supports_hdr",
                 "_read_float_atom", "_write_float_atom", "_write_string_atom",
                 "_read_string_atom", "_count_gamescope_atoms")}
        real_sleep = asyncio.sleep
        saved_mapping = (main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN)
        saved_state = {k: main.Plugin._state[k] for k in
                       ("sdr_enabled", "hdr_enabled", "sdr_look_applied",
                        "hdr_applied", "sdr_applied")}
        saved_looks = (
            main.Plugin._sdr_look_path, main.Plugin._sdr_look_baseline,
            main.Plugin._hdr_look_path, main.Plugin._hdr_look_baseline,
        )

        async def no_wait(_seconds):
            return None

        main._pick_display = lambda: ":0"
        main._panel_is_wide_gamut = lambda *_args: True
        main._panel_supports_hdr = lambda *_args: True
        main._read_float_atom = lambda name: 1.0
        main._write_float_atom = lambda name, value: True
        main._count_gamescope_atoms = lambda display: 44
        own = {
            main.ATOM_LOOK_G22: os.path.join(
                main._runtime_dir(), "sdr-look-0.cube"),
            main.ATOM_LOOK_PQ: os.path.join(
                main._runtime_dir(), "hdr-look-0.cube"),
        }
        foreign = {
            main.ATOM_LOOK_G22: "/tmp/foreign-sdr.cube",
            main.ATOM_LOOK_PQ: "/tmp/foreign-hdr.cube",
        }
        main._read_string_atom = lambda name: (
            own if own_looks else foreign).get(name)
        main._write_string_atom = lambda name, value: (
            written.append((name, value)) or True)
        asyncio.sleep = no_wait
        main.settings._store.update({"sdr_enabled": sdr_on,
                                     "hdr_enabled": hdr_on})
        try:
            drain(main.Plugin()._main())
            drain(main.Plugin()._start_when_ready())
        finally:
            for name, fn in real.items():
                setattr(main, name, fn)
            asyncio.sleep = real_sleep
            main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = saved_mapping
            main.Plugin._state.update(saved_state)
            (main.Plugin._sdr_look_path, main.Plugin._sdr_look_baseline,
             main.Plugin._hdr_look_path,
             main.Plugin._hdr_look_baseline) = saved_looks
            main.settings._store.clear()
        return written

    def test_a_disabled_sdr_half_clears_the_g22_look_on_startup(self):
        written = self._boot_with(sdr_on=False, hdr_on=False)
        self.assertIn((main.ATOM_LOOK_G22, ""), written)

    def test_a_disabled_hdr_half_clears_the_pq_look_on_startup(self):
        written = self._boot_with(sdr_on=False, hdr_on=False)
        self.assertIn((main.ATOM_LOOK_PQ, ""), written)

    def test_an_enabled_half_is_not_cleared_out_from_under_itself(self):
        written = self._boot_with(sdr_on=True, hdr_on=False)
        self.assertNotIn((main.ATOM_LOOK_G22, ""), written)

    def test_foreign_looks_are_not_cleared_on_disabled_startup(self):
        written = self._boot_with(
            sdr_on=False, hdr_on=False, own_looks=False)
        self.assertEqual(written, [])


class MonitorLatency(PluginCase):

    async def test_other_xserver_probe_does_not_block_slider_changes(self):
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            release.wait(2)
            return False
        self.mock(M, "_other_writer_present", side_effect=probe)
        self.mock(M, "SUSPEND_POLL_S", 0.03)
        self.mock(M, "WATCH_INTERVAL_S", 0)
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 10000)
        M.Plugin._slow_checks = M.SLOW_CHECK_EVERY - 1
        M.Plugin._state.update(sdr_enabled=True, sdr_applied=True)
        real_sleep, ticks = asyncio.sleep, 0
        async def sleep(delay):
            nonlocal ticks
            if delay == 0.03:
                ticks += 1
                if ticks > 1:
                    raise asyncio.CancelledError()
            await real_sleep(0)
        self.mock(M.asyncio, "sleep", side_effect=sleep)
        monitor = asyncio.create_task(self.plugin._monitor_loop())
        change = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            change = asyncio.create_task(self.plugin.set_hdr_saturation(1.2))
            done, _ = await asyncio.wait([change], timeout=0.2)
            self.assertIn(change, done, "another X server held the settings lock")
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await monitor
            if change is not None:
                await change
            await self.drain()

    async def test_accepted_saturation_is_saved_when_enable_is_blocked(self):
        M.Plugin._state.update(external_display=True)
        result = await self.plugin.set_sdr_enabled(True, 1.4)
        self.assertFalse(result["sdr_enabled"])
        self.assertEqual(result["sdr_saturation"], 1.4)
        await self.drain()
        M.Plugin._save.assert_called_once()
        M.Plugin._save.reset_mock()
        M.Plugin._state.update(external_display=False, hdr_supported=False)
        result = await self.plugin.set_hdr_enabled(True, 1.3)
        self.assertFalse(result["hdr_enabled"])
        self.assertEqual(result["hdr_saturation"], 1.3)
        await self.drain()
        M.Plugin._save.assert_called_once()

    async def test_unload_bounds_reachability_probe_already_queued_by_monitor(self):
        entered, release = threading.Event(), threading.Event()
        timeouts = []
        def read(_atom):
            entered.set()
            release.wait(2)
            return None
        def run(command, **kwargs):
            self.assertEqual(command, ["xprop", "-root"])
            timeouts.append(kwargs["timeout"])
            raise M.subprocess.TimeoutExpired(command, kwargs["timeout"])
        self.mock(M, "_read_float_atom", side_effect=read)
        self.mock(M.subprocess, "run", side_effect=run)
        M.Plugin._state.update(sdr_enabled=True, sdr_applied=True)
        M.Plugin._last_sdr_written = 1.0
        watch = asyncio.create_task(self.plugin._watch_tick())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            unload = asyncio.create_task(self.plugin._unload())
            await asyncio.sleep(0)
        finally:
            release.set()
        await watch
        await unload
        self.assertEqual(len(timeouts), 1)
        self.assertLessEqual(timeouts[0], M.UNLOAD_XPROP_TIMEOUT_S,
                             "reachability can consume Decky's entire five-second stop budget")


class SessionOwnership(PluginCase):

    def configure_session(self):
        self.mock(M, "_runtime_dir", return_value="/tmp")
        self.session = (":0", (1, 2, 3))
        self.mock(M.Plugin, "_session_identity", self.session)
        self.identity = self.mock(M, "_display_session_identity", return_value=self.session)
        self.mock(M, "_pick_display", return_value=":0")
        self.mock(M, "_count_gamescope_atoms", return_value=44)
        self.mock(M.Plugin, "_configure_for_panel", return_value=False)
        self.mock(M.Plugin, "_schedule_reassert")
        self.mock(M, "GAMESCOPE_SETTLE_S", 0)

    async def test_transient_disconnect_preserves_sdr_restore_value_in_the_same_session(self):
        self.configure_session()
        await self.plugin.set_sdr_enabled(True, 1.5)
        await self.drain()
        M.Plugin._disconnect()
        await self.plugin._start_when_ready(start_monitor=False)
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8, "transient probe failure erased the SDR baseline")

    async def test_transient_disconnect_preserves_foreign_hdr_look_in_the_same_session(self):
        self.configure_session()
        self.strings[M.ATOM_LOOK_PQ] = "/tmp/foreign-original.cube"
        await self.plugin.set_hdr_enabled(True, 1.3)
        await self.drain()
        M.Plugin._disconnect()
        await self.plugin._start_when_ready(start_monitor=False)
        await self.drain()
        await self.plugin.set_hdr_enabled(False)
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/tmp/foreign-original.cube",
                         "same-session reconnect discarded the foreign baseline")

    async def test_disable_during_transient_disconnect_cannot_leave_sdr_enabled(self):
        self.configure_session()
        await self.plugin.set_sdr_enabled(True, 1.5)
        await self.drain()
        M.Plugin._disconnect()
        await self.plugin.set_sdr_enabled(False)
        await self.plugin._start_when_ready(start_monitor=False)
        await self.drain()
        self.assertEqual(self.live_sdr, 0.8, "off RPC lost ownership before restoring SDR")

    async def test_new_socket_session_rejects_old_restore_and_captures_new_baselines(self):
        self.configure_session()
        await self.plugin.set_sdr_enabled(True, 1.5)
        self.strings[M.ATOM_LOOK_PQ] = "/tmp/old-foreign.cube"
        await self.plugin.set_hdr_enabled(True, 1.3)
        await self.drain()
        M.Plugin._disconnect()
        self.identity.return_value = (":0", (1, 3, 4))
        self.live_sdr = 0.6
        self.strings[M.ATOM_LOOK_PQ] = "/tmp/new-foreign.cube"
        self.float_writer.reset_mock()
        self.string_writer.reset_mock()
        await self.plugin.set_sdr_enabled(False)
        await self.plugin.set_hdr_enabled(False)
        self.float_writer.assert_not_called()
        self.string_writer.assert_not_called()
        await self.plugin._start_when_ready(start_monitor=False)
        await self.drain()
        self.assertEqual(self.live_sdr, 0.6)
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/tmp/new-foreign.cube")
        await self.plugin.set_sdr_enabled(True, 1.6)
        await self.plugin.set_hdr_enabled(True, 1.4)
        await self.drain()
        await self.plugin.reset()
        self.assertEqual(self.live_sdr, 0.6)
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/tmp/new-foreign.cube")

    async def test_unidentified_disconnected_server_cannot_receive_an_old_restore(self):
        self.configure_session()
        await self.plugin.set_sdr_enabled(True, 1.5)
        await self.drain()
        M.Plugin._disconnect()
        self.identity.return_value = None
        self.float_writer.reset_mock()
        await self.plugin.set_sdr_enabled(False)
        self.float_writer.assert_not_called()
        self.assertTrue(M.Plugin._state["sdr_error"])
        self.assertTrue(M.Plugin._state["sdr_applied"])

    async def test_replaced_server_cannot_receive_old_restore_before_the_monitor_tick(self):
        self.configure_session()
        self.strings[M.ATOM_LOOK_PQ] = "/tmp/old-foreign.cube"
        await self.plugin.set_hdr_enabled(True, 1.3)
        await self.drain()
        self.identity.return_value = (":0", (1, 6, 7))
        self.strings = {}
        self.string_writer.reset_mock()
        self.assertFalse(M.Plugin._disconnected)
        await self.plugin.set_hdr_enabled(False)
        self.string_writer.assert_not_called()
        self.assertTrue(M.Plugin._state["hdr_error"])

    async def test_same_session_dropped_atoms_keep_foreign_baselines_without_rebuilding(self):
        self.configure_session()
        self.mock(M, "SDR_NEUTRAL_ATOM", 0.0)
        self.mock(M, "SDR_ATOM_SPAN", 1.0)
        self.strings = {M.ATOM_LOOK_G22: "/tmp/foreign-g22.cube", M.ATOM_LOOK_PQ: "/tmp/foreign-pq.cube"}
        await self.plugin.set_sdr_enabled(True, 2.4)
        await self.plugin.set_hdr_enabled(True, 1.3)
        await self.drain()
        self.strings = {}
        self.builder.reset_mock()
        M.Plugin._disconnect()
        await self.plugin._start_when_ready(start_monitor=False)
        await self.drain()
        self.builder.assert_not_called()
        M.Plugin._reassert()
        await self.plugin.reset()
        self.assertEqual(self.strings[M.ATOM_LOOK_G22], "/tmp/foreign-g22.cube")
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/tmp/foreign-pq.cube")

    async def test_changed_socket_is_detected_even_when_root_atoms_remain_healthy(self):
        self.configure_session()
        self.identity.return_value = (":0", (1, 4, 5))
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 0)
        self.mock(M, "SUSPEND_POLL_S", 0)
        self.mock(M.Plugin, "_monitor_probe", return_value=(44, (False, None, None)))
        recovery = self.mock(self.plugin, "_start_when_ready", side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._monitor_loop()
        recovery.assert_awaited_once_with(start_monitor=False)
        self.assertFalse(M.Plugin._state["ready"])

    async def test_same_session_panel_change_rebuilds_sdr_for_the_new_white(self):
        self.configure_session()
        self.mock(M, "SDR_NEUTRAL_ATOM", 0.0)
        self.mock(M, "SDR_ATOM_SPAN", 1.0)
        self.mock(M.Plugin, "_sdr_white_point", (0.3127, 0.3290))
        await self.plugin.set_sdr_enabled(True, 2.4)
        await self.drain()
        self.builder.reset_mock()
        M.Plugin._disconnect()
        def change_panel():
            M.Plugin._sdr_white_point = (0.296, 0.307)
            return True
        self.mock(M.Plugin, "_configure_for_panel", side_effect=change_panel)
        await self.plugin._start_when_ready(start_monitor=False)
        await self.drain()
        self.builder.assert_called_once()
        self.assertEqual(self.builder.call_args.args[4], (0.296, 0.307))


class StartupShutdown(PluginCase):

    async def stopped_startup(self, phase):
        self.mock(M.Plugin, "_settings_loaded", False, create=True)
        self.mock(M.Plugin, "_save", side_effect=SAVE_SETTINGS)
        self.mock(M.updater, "ssl_context", return_value=None)
        recovery = self.mock(self.plugin, "_start_when_ready")
        entered, release = asyncio.Event(), asyncio.Event()
        saved = {"sdr_enabled": True, "hdr_enabled": True,
                 "sdr_saturation": 2.6, "hdr_saturation": 1.4,
                 "unrelated": "preserve"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps(saved), encoding="utf-8")
            store = SimpleNamespace(path=str(path), settings={})
            def read():
                store.settings = json.loads(path.read_text(encoding="utf-8"))
            store.read = read
            self.mock(M, "settings", store)
            blocked = {"writer": M._wait_for_lut_writer,
                       "tls": M.updater.ssl_context, "settings": M._read_settings}[phase]
            real_offload = M._offload
            async def offload(fn, *args):
                if fn is blocked:
                    entered.set()
                    await release.wait()
                return await real_offload(fn, *args)
            self.mock(M, "_offload", side_effect=offload)
            startup = asyncio.create_task(self.plugin._main())
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                await self.plugin._unload()
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), saved,
                                 "unload replaced unread settings with startup defaults")
            finally:
                release.set()
                await startup
            recovery.assert_not_awaited()
            self.assertEqual(M.Plugin._tasks, [], "startup created tasks after unload")
            self.assertTrue(M._lut_writes_disabled, "late startup reenabled LUT writes after unload")

    async def test_unload_while_waiting_for_writer_preserves_unread_settings(self):
        await self.stopped_startup("writer")

    async def test_unload_during_tls_setup_preserves_unread_settings(self):
        await self.stopped_startup("tls")

    async def test_unload_during_settings_read_preserves_unread_settings(self):
        await self.stopped_startup("settings")
