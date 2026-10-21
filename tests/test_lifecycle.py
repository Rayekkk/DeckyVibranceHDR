# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Startup, suspend/resume, monitoring, session recovery and unload."""

import asyncio
import itertools
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from _fixtures import PluginCase, PluginTest, READ_STRING_ATOM, SAVE_SETTINGS, drain, edid_with_red  # installs the loader stand-ins first
import main as M


class Recovery(PluginCase):
    def stub_connect(self, identity, **configure):
        """Stand in for everything connecting touches except the SDR atom."""
        self.mock(M.vibrance_x11, "session_identity", return_value=identity)
        self.mock(M, "_pick_display", return_value=":0")
        self.mock(M.vibrance_x11, "count_gamescope_atoms", return_value=44)
        self.mock(M, "GAMESCOPE_SETTLE_S", 0)
        self.mock(M.Plugin, "_configure_for_panel", **(configure or {"return_value": False}))
        self.mock(M.Plugin, "_capture_existing_looks")
        self.mock(self.plugin, "_reassert_repeatedly", return_value=None)

    async def reconnect(self, identity):
        self.stub_connect(identity)
        await self.plugin._start_when_ready(start_monitor=False)
        await self.drain()

    async def test_each_enable_captures_the_current_sdr_baseline(self):
        for baseline in (0.8, 0.6):
            self.live_sdr = baseline
            await self.plugin.set_sdr_enabled(True)
            await self.plugin.set_sdr_enabled(False)
            self.assertEqual(self.live_sdr, baseline)

    async def test_our_own_leftover_is_not_mistaken_for_a_baseline(self):
        """Reloading the plugin with the half already on used to capture what we
        wrote last time, so "off" restored our own saturation."""
        M.Plugin._state.update(sdr_enabled=True, sdr_saturation=1.4)
        M.Plugin._sdr_baseline = self.live_sdr = 1.7   # our write before the reload
        await self.reconnect((":0", (1, 2)))
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, M.SDR_UNSET)

    async def test_a_new_server_after_a_gamescope_restart_gives_the_baseline(self):
        """Its atoms are fresh, so a value there is somebody else's and must be
        what disabling restores - not gamescope's -1."""
        M.Plugin._session_identity = (":0", (1, 1))          # the server before
        M.Plugin._state.update(sdr_enabled=True, sdr_saturation=1.5)
        self.live_sdr = 0.6                                  # Steam or another tool
        await self.reconnect((":0", (2, 2)))
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.6)

    async def test_a_reconnect_to_another_server_holding_our_write_keeps_the_baseline(self):
        """A reconnect can move between gamescope's servers; the one it lands
        on may still hold our earlier write, which is not a baseline."""
        M.Plugin._session_identity = (":1", (1, 1))
        M.Plugin._sdr_baseline = 0.8
        M.Plugin._last_sdr_written = self.live_sdr = 1.4
        M.Plugin._state.update(sdr_enabled=True, sdr_saturation=1.4)
        await self.reconnect((":0", (2, 2)))
        self.assertEqual(M.Plugin._sdr_baseline, 0.8)

    async def test_a_retried_move_to_another_server_still_knows_our_write(self):
        """The failed attempt forgot our values; its retry must not take the
        write still on that server for a baseline."""
        M.Plugin._session_identity = (":1", (1, 1))
        M.Plugin._sdr_baseline = 0.8
        M.Plugin._last_sdr_written = self.live_sdr = 1.4
        M.Plugin._state.update(sdr_enabled=True, sdr_saturation=1.4)
        self.stub_connect((":0", (2, 2)), side_effect=[RuntimeError("probe"), False])
        with self.assertRaises(RuntimeError):
            await self.plugin._connect()
        await self.plugin._connect()                         # the monitor's retry
        self.assertEqual(M.Plugin._sdr_baseline, 0.8)
        self.assertEqual(M.Plugin._connect_ours, ())         # done once connected

    async def test_an_unreadable_session_identity_is_no_reason_to_trust_the_atom(self):
        M.Plugin._session_identity = (":0", (1, 1))
        M.Plugin._sdr_baseline = 0.8
        self.live_sdr = 1.4                                  # possibly ours
        M.Plugin._state.update(sdr_enabled=True, sdr_saturation=1.4)
        await self.reconnect(None)
        self.assertIsNone(M.Plugin._sdr_baseline)            # forgotten, not adopted

    async def test_a_retried_first_connect_still_distrusts_our_leftover(self):
        """A first connect that raised must not leave its identity behind:
        the retry would see a preserved session and adopt our old value."""
        M.Plugin._state.update(sdr_enabled=True, sdr_saturation=1.4)
        self.live_sdr = 1.7                                  # our write before the reload
        self.stub_connect((":0", (1, 1)), side_effect=[RuntimeError("probe"), False])
        with self.assertRaises(RuntimeError):
            await self.plugin._connect()
        await self.plugin._connect()                         # the monitor's retry
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, M.SDR_UNSET)

    async def test_sdr_switched_on_while_disconnected_restores_the_value_found_later(self):
        """Enabling during a brief disconnect skips the baseline read; when the
        same server comes back, the value there now is the one to restore."""
        M.Plugin._session_identity = session = (":0", (1, 2))
        M.Plugin._sdr_baseline = 0.4                     # from earlier in the session
        M.Plugin._state.update(sdr_enabled=True, sdr_applied=False, sdr_saturation=1.4)
        self.live_sdr = 0.7                              # another writer meanwhile
        await self.reconnect(session)
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.7)

    async def test_a_lost_first_write_is_not_taken_as_the_baseline_on_reconnect(self):
        """Our first write timed out but landed; the same server coming back
        must not record that value of ours as the one to restore."""
        M.Plugin._session_identity = session = (":0", (1, 2))
        M.Plugin._sdr_baseline = 0.8
        M.Plugin._last_sdr_attempted = self.live_sdr = 1.4
        M.Plugin._state.update(sdr_enabled=True, sdr_applied=False, sdr_saturation=1.4)
        await self.reconnect(session)
        self.assertEqual(M.Plugin._sdr_baseline, 0.8)
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)

    async def test_uninstall_removes_only_our_lut_files(self):
        """They live outside the plugin folder Decky deletes, so nothing else
        would ever clean them up."""
        with tempfile.TemporaryDirectory() as folder:
            ours = [f"{prefix}-look-{slot}.cube" for prefix in ("hdr", "sdr") for slot in (0, 1)]
            for name in ours + ["settings.json", "hdr-look-2.cube"]:
                Path(folder, name).write_text("x")
            self.mock(M, "_runtime_dir", return_value=folder)
            await self.plugin._uninstall()
            self.assertEqual(sorted(os.listdir(folder)), ["hdr-look-2.cube", "settings.json"])
            await self.plugin._uninstall()          # missing files are not an error

    async def test_uninstall_does_not_unload_a_second_time(self):
        """Decky runs _unload and then _uninstall within one stop budget."""
        unload = self.mock(self.plugin, "_unload")
        M.Plugin._unloading = True                   # Decky's _unload already ran
        await self.plugin._uninstall()
        unload.assert_not_awaited()
        M.Plugin._unloading = False                  # called on its own, it still unloads
        await self.plugin._uninstall()
        unload.assert_awaited_once()

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
        M.Plugin._watch_once(())
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
        M.Plugin._slow_checks = M.SLOW_CHECK_EVERY - 1   # pending looks retry on the slow tick
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
        M.Plugin._watch_once(())
        self.assertEqual(M.Plugin._state["sdr_error"], "")
        self.assertFalse(M.Plugin._state["sdr_applied"])

    async def test_missing_sdr_property_is_recreated_only_on_gamescope(self):
        await self.plugin.set_sdr_enabled(True)
        self.live_sdr = None
        self.float_writer.reset_mock()
        count = self.mock(M.vibrance_x11, "count_gamescope_atoms", return_value=0)
        M.Plugin._watch_once(())
        self.float_writer.assert_not_called()
        count.return_value = 42
        M.Plugin._watch_once(())
        self.float_writer.assert_called_once()
        self.assertEqual(self.live_sdr, M.SDR_UNSET)        # 100%
        self.assertEqual(M.Plugin._state["sdr_error"], "")

    async def test_failed_initial_sdr_write_recovers(self):
        self.float_writer.side_effect = None
        self.float_writer.return_value = False
        await self.plugin.set_sdr_enabled(True)
        self.assertFalse(M.Plugin._state["sdr_applied"])
        self.float_writer.side_effect = self.write_float
        M.Plugin._watch_once(())
        self.assertTrue(M.Plugin._state["sdr_applied"])


class Reassert(PluginTest):
    """gamescope drops these atoms when it re-initialises, which it does across
    a suspend. Telling that apart from another plugin holding the other end is
    the whole job here - the first heals itself, the second needs saying."""

    def setUp(self):
        self.written = []
        self.live = [2.0]

        def fake_write(name, value):
            self.written.append(value)
            self.live[0] = value
            return True

        M.vibrance_x11.read_float_atom = lambda name: self.live[0]
        M.vibrance_x11.write_float_atom = fake_write
        M.vibrance_x11.foreign_sdr_values = lambda: ()
        M.Plugin._reassert_looks_if_dropped = staticmethod(lambda: None)
        M.Plugin._slow_checks = 0
        M.Plugin._foreign_sdr_seen, M.Plugin._foreign_active_ticks = None, 0
        M.Plugin._state.update({
            "sdr_enabled": True, "sdr_applied": True,
            "sdr_saturation": 2.0, "conflict": "",
        })
        M.Plugin._last_sdr_written = 2.0
        M.Plugin._reassert_strikes = 0

    def test_a_dropped_value_is_put_back_without_crying_conflict(self):
        self.live[0] = 1.0                      # gamescope re-initialised
        M.Plugin._watch_once(())
        self.assertEqual(self.written, [2.0])
        self.assertEqual(M.Plugin._state["conflict"], "")

    def test_something_holding_the_other_end_gets_named(self):
        for _ in range(M.CONFLICT_STRIKES):  # overwritten after every put-back
            self.live[0] = 0.5
            M.Plugin._watch_once(())
        self.assertIn("vibrantDeck", M.Plugin._state["conflict"])

    def test_one_strike_short_stays_quiet(self):
        for _ in range(M.CONFLICT_STRIKES - 1):
            self.live[0] = 0.5
            M.Plugin._watch_once(())
        self.assertEqual(M.Plugin._state["conflict"], "")

    def test_agreement_clears_the_count(self):
        self.live[0] = 1.0
        M.Plugin._watch_once(())
        self.assertEqual(M.Plugin._reassert_strikes, 1)
        M.Plugin._watch_once(())               # agrees now, we just wrote it
        self.assertEqual(M.Plugin._reassert_strikes, 0)
        self.assertEqual(M.Plugin._state["conflict"], "")

    def test_a_disabled_half_is_left_alone(self):
        M.Plugin._state["sdr_enabled"] = False
        self.live[0] = 0.5
        M.Plugin._watch_once(())
        self.assertEqual(self.written, [])

    def _slow_ticks(self, *readings):
        """One slow tick per reading of the other X servers."""
        for reading in readings:
            M.Plugin._slow_checks = M.SLOW_CHECK_EVERY - 1
            M.Plugin._watch_once(reading)

    def test_a_writer_on_another_server_is_named_even_when_we_agree(self):
        """The case our readback cannot see: gamescope applies a write from any
        root, but we only ever read one of them, so the value we read back can
        agree with us while the picture shows somebody else's."""
        self._slow_ticks(((":1", "1056964608"),), ((":1", "1065353216"),))
        self.assertIn("different X server", M.Plugin._state["conflict"])
        self.assertEqual(self.written, [])

    def test_a_lingering_atom_from_a_stopped_writer_is_not_a_conflict(self):
        """The atom outlives its writer until gamescope restarts: vibrantDeck
        switched off, or our own write from before a reconnect moved us."""
        still = ((":1", "1056964608"),)
        self._slow_ticks(still, still, still)
        self.assertEqual(M.Plugin._state["conflict"], "")

    def test_the_cross_server_warning_fades_once_the_writer_goes_quiet(self):
        self._slow_ticks(((":1", "1"),), ((":1", "2"),))
        self.assertIn("different X server", M.Plugin._state["conflict"])
        self._slow_ticks(*[((":1", "2"),)] * M.CONFLICT_STRIKES)
        self.assertEqual(M.Plugin._state["conflict"], "")

    def test_a_dropped_value_is_restored_while_another_server_is_active(self):
        self._slow_ticks(((":1", "1"),), ((":1", "2"),))
        self.live[0] = 1.0                      # gamescope re-initialised
        M.Plugin._watch_once(None)
        self.assertEqual(self.written, [2.0])
        self.assertIn("different X server", M.Plugin._state["conflict"])

    def test_an_unreadable_other_server_changes_nothing(self):
        self._slow_ticks(((":1", "1"),), None, ((":1", "1"),))
        self.assertEqual(M.Plugin._state["conflict"], "")

    def test_writes_made_while_our_sdr_was_off_are_not_a_conflict(self):
        """The other servers are not read while SDR is off, so a reference from
        before must not make vibrantDeck's writes in between look current."""
        self._slow_ticks(((":1", "1"),), ((":1", "2"),))
        self.assertIn("different X server", M.Plugin._state["conflict"])
        M.Plugin._state["sdr_enabled"] = False
        M.Plugin._watch_once(None)
        M.Plugin._state["sdr_enabled"] = True
        self._slow_ticks(((":1", "3"),))
        self.assertEqual(M.Plugin._state["conflict"], "")


class SuspendDetection(PluginTest):

    def test_boottime_is_usable_everywhere(self):
        """CLOCK_BOOTTIME is Linux only; the fallback keeps the detector idle
        rather than raising on a dev box."""
        self.assertIsInstance(M._boottime(), float)

    def test_slack_exceeds_the_poll_interval(self):
        """Otherwise the jitter of an ordinary tick reads as a suspend and the
        values get re-pushed every second forever."""
        self.assertGreater(M.SUSPEND_SLACK_S, M.SUSPEND_POLL_S)

    def test_the_retry_ladder_is_staggered_and_reaches_past_startup(self):
        delays = M.REASSERT_DELAYS_S
        self.assertEqual(list(delays), sorted(delays))
        self.assertGreater(sum(delays), 10.0)


class IsolatedStartup(PluginTest):
    """Startup fixtures must never adopt atoms from the developer's live display."""

    def setUp(self):
        from unittest.mock import patch
        state = dict(M.Plugin._state)
        state.update(sdr_applied=False, sdr_look_applied=False, hdr_applied=False)
        patches = [(M.vibrance_x11, "read_string_atom", lambda _name: None),
                   (M.vibrance_x11, "read_float_atom", lambda _name: 1.0),
                   (M.vibrance_x11, "write_string_atom", lambda *_args: True),
                   (M.vibrance_x11, "write_float_atom", lambda *_args: True),
                   (M.vibrance_x11, "display_is_external", lambda: False),
                   (M.vibrance_x11, "session_identity", lambda: None),
                   (M.Plugin, "_state", state),
                   (M.Plugin, "_session_identity", None),
                   (M.Plugin, "_disconnected", False),
                   (M.Plugin, "_settings_loaded", True),
                   (M.Plugin, "_sdr_look_path", None),
                   (M.Plugin, "_hdr_look_path", None),
                   (M.Plugin, "_sdr_look_baseline", None),
                   (M.Plugin, "_hdr_look_baseline", None)]
        for obj, name, value in patches:
            item = patch.object(obj, name, value)
            item.start()
            self.addCleanup(item.stop)


class StartupRetry(IsolatedStartup):
    """Decky starts plugins as a system service and can win the race against
    the session; giving up permanently meant a reinstall to recover."""

    def test_the_backoff_is_bounded_and_starts_quickly(self):
        self.assertGreater(M.GAMESCOPE_RETRY_FIRST_S, 0.0)
        self.assertLessEqual(M.GAMESCOPE_RETRY_FIRST_S, 2.0)
        self.assertGreater(M.GAMESCOPE_RETRY_MAX_S,
                           M.GAMESCOPE_RETRY_FIRST_S)
        self.assertLessEqual(M.GAMESCOPE_RETRY_MAX_S, 60.0)

    def test_startup_keeps_trying_until_gamescope_turns_up(self):
        attempts = []

        def flaky():
            attempts.append(1)
            return ":0" if len(attempts) >= 3 else None

        async def no_wait(_seconds):
            return None

        M._pick_display = flaky
        M._panel_is_wide_gamut = lambda *_args: True
        M._panel_supports_hdr = lambda *_args: True
        M.vibrance_x11.read_float_atom = lambda name: 1.0
        M.vibrance_x11.count_gamescope_atoms = lambda display: 44
        with patch.object(asyncio, "sleep", no_wait):
            drain(M.Plugin()._start_when_ready())
        self.assertEqual(len(attempts), 3)
        self.assertTrue(M.Plugin._state["ready"])


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

        async def no_wait(_seconds):
            return None

        M.vibrance_x11.count_gamescope_atoms = counter
        M._pick_display = lambda: ":0"
        M._panel_is_wide_gamut = lambda *_args: True
        M._panel_supports_hdr = lambda *_args: True
        M.vibrance_x11.read_float_atom = lambda name: 1.0
        M.vibrance_x11.write_float_atom = lambda name, value: True
        M.vibrance_x11.write_string_atom = lambda name, value: True
        with patch.object(asyncio, "sleep", no_wait):
            drain(M.Plugin()._start_when_ready())
        # Stops on the first repeat rather than reading forever.
        self.assertEqual(seen, [20, 30, 44, 44])

    def test_the_settle_wait_is_bounded(self):
        """A session that never stops growing must not hold startup for ever."""
        self.assertGreater(M.GAMESCOPE_SETTLE_TRIES, 2)
        self.assertLessEqual(
            M.GAMESCOPE_SETTLE_TRIES * M.GAMESCOPE_SETTLE_S, 30.0)

    def test_startup_puts_the_values_back_more_than_once(self):
        """One write into a settling session is not enough; the same ladder the
        resume path uses is what makes it stick."""
        ladders = []
        M._pick_display = lambda: ":0"
        M.vibrance_x11.count_gamescope_atoms = lambda display: 44
        M.GAMESCOPE_SETTLE_S = 0
        M.Plugin._configure_for_panel = staticmethod(lambda *args: False)
        M.Plugin._schedule_reassert = lambda self: ladders.append("_reassert_repeatedly")
        drain(M.Plugin()._start_when_ready(start_monitor=False))
        self.assertEqual(ladders, ["_reassert_repeatedly"])

    def test_a_disabled_half_clears_what_the_last_run_left(self):
        """The look atom outlives a plugin reload, and nothing reloads it into
        our state - so an owned path has to be cleared, not assumed."""
        cleared = []
        path = os.path.join(M._runtime_dir(), "sdr-look-0.cube")
        M.vibrance_x11.write_string_atom = lambda name, value: (
            cleared.append((name, value)) or True)
        M.vibrance_x11.read_string_atom = lambda name: path
        M.Plugin._state["sdr_look_applied"] = True
        M.Plugin._sdr_look_path = path
        M.Plugin._release_look("sdr")
        self.assertEqual(cleared, [(M.ATOM_LOOK_G22, "")])


class StartupAssertsOff(IsolatedStartup):
    """Startup clears stale paths owned by us, but never somebody else's."""

    def _boot_with(self, sdr_on, hdr_on, own_looks=True):
        written = []

        async def no_wait(_seconds):
            return None

        M._pick_display = lambda: ":0"
        M._panel_is_wide_gamut = lambda *_args: True
        M._panel_supports_hdr = lambda *_args: True
        M.vibrance_x11.read_float_atom = lambda name: 1.0
        M.vibrance_x11.write_float_atom = lambda name, value: True
        M.vibrance_x11.count_gamescope_atoms = lambda display: 44
        own = {
            M.ATOM_LOOK_G22: os.path.join(
                M._runtime_dir(), "sdr-look-0.cube"),
            M.ATOM_LOOK_PQ: os.path.join(
                M._runtime_dir(), "hdr-look-0.cube"),
        }
        foreign = {
            M.ATOM_LOOK_G22: "/tmp/foreign-sdr.cube",
            M.ATOM_LOOK_PQ: "/tmp/foreign-hdr.cube",
        }
        M.vibrance_x11.read_string_atom = lambda name: (
            own if own_looks else foreign).get(name)
        M.vibrance_x11.write_string_atom = lambda name, value: (
            written.append((name, value)) or True)
        M.settings._store.update({"sdr_enabled": sdr_on,
                                     "hdr_enabled": hdr_on})
        try:
            with patch.object(asyncio, "sleep", no_wait):
                drain(M.Plugin()._main())
                drain(M.Plugin()._start_when_ready())
        finally:
            M.settings._store.clear()  # the stand-in store is not under StateGuard
        return written

    def test_a_disabled_sdr_half_clears_the_g22_look_on_startup(self):
        written = self._boot_with(sdr_on=False, hdr_on=False)
        self.assertIn((M.ATOM_LOOK_G22, ""), written)

    def test_a_disabled_hdr_half_clears_the_pq_look_on_startup(self):
        written = self._boot_with(sdr_on=False, hdr_on=False)
        self.assertIn((M.ATOM_LOOK_PQ, ""), written)

    def test_an_enabled_half_is_not_cleared_out_from_under_itself(self):
        written = self._boot_with(sdr_on=True, hdr_on=False)
        self.assertNotIn((M.ATOM_LOOK_G22, ""), written)

    def test_foreign_looks_are_not_cleared_on_disabled_startup(self):
        written = self._boot_with(
            sdr_on=False, hdr_on=False, own_looks=False)
        self.assertEqual(written, [])


class MonitorLatency(PluginCase):

    async def test_other_xserver_probe_does_not_block_slider_changes(self):
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            release.wait(10)
            return ()
        self.mock(M.vibrance_x11, "foreign_sdr_values", side_effect=probe)
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
            done, _ = await asyncio.wait([change], timeout=2)
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
            raise M.vibrance_x11.subprocess.TimeoutExpired(command, kwargs["timeout"])
        self.mock(M.vibrance_x11, "read_float_atom", side_effect=read)
        self.mock(M.vibrance_x11.subprocess, "run", side_effect=run)
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
        self.identity = self.mock(M.vibrance_x11, "session_identity", return_value=self.session)
        self.mock(M, "_pick_display", return_value=":0")
        self.mock(M.vibrance_x11, "count_gamescope_atoms", return_value=44)
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
            blocked = {"writer": M.vibrance_lutfile.wait_for_writer,
                       "tls": M.updater.ssl_context, "settings": M.vibrance_settings.read}[phase]
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
            self.assertTrue(M.vibrance_lutfile.disabled, "late startup reenabled LUT writes after unload")

    async def test_unload_while_waiting_for_writer_preserves_unread_settings(self):
        await self.stopped_startup("writer")

    async def test_unload_during_tls_setup_preserves_unread_settings(self):
        await self.stopped_startup("tls")

    async def test_unload_during_settings_read_preserves_unread_settings(self):
        await self.stopped_startup("settings")


class MonitorAndResume(PluginCase):
    """The monitor loop, session rediscovery and the resume ladder."""

    async def test_same_server_conflict_is_not_relabelled_as_another_server(self):
        M.Plugin._state.update(sdr_enabled=True, sdr_applied=True,
                              conflict="another plugin keeps overwriting", sdr_saturation=1.4)
        M.Plugin._last_sdr_written = self.live_sdr = 1.4
        M.Plugin._watch_once(())
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
        self.mock(M.vibrance_x11, "count_gamescope_atoms", return_value=44)
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
        timeout = subprocess.TimeoutExpired("xprop", M.vibrance_x11.timeout)
        self.mock(M.vibrance_x11.subprocess, "run", side_effect=[
            timeout, timeout, SimpleNamespace(stdout="GAMESCOPE_FOCUSED_APP(CARDINAL) = 0")])
        recovery = self.mock(self.plugin, "_start_when_ready")
        refresh = self.mock(self.plugin, "_refresh_panel", side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._monitor_loop()
        recovery.assert_not_awaited()
        refresh.assert_awaited_once()
        self.assertEqual(M.vibrance_x11.subprocess.run.call_count, 3)
        self.assertTrue(M.Plugin._state["ready"])
        self.assertFalse(M.Plugin._disconnected)

    async def test_a_reconnect_that_failed_is_retried_on_healthy_probes(self):
        """A reconnect that raised leaves the plugin disconnected while the
        probes look healthy; the monitor must keep trying, not just refresh."""
        M.Plugin._disconnected = True
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 0)
        self.mock(M, "SUSPEND_POLL_S", 0)
        self.mock(M.Plugin, "_monitor_probe", return_value=(44, None))
        # Either path ends the loop, so a regression fails instead of spinning.
        refresh = self.mock(self.plugin, "_refresh_panel", side_effect=asyncio.CancelledError())
        recovery = self.mock(self.plugin, "_start_when_ready", side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._monitor_loop()
        recovery.assert_awaited_once_with(start_monitor=False)
        refresh.assert_not_awaited()

    async def test_the_monitor_starts_even_when_connecting_raises(self):
        self.mock(self.plugin, "_connect", side_effect=RuntimeError("panel probe failed"))
        monitor = self.mock(self.plugin, "_monitor_loop", return_value=None)
        with self.assertRaises(RuntimeError):
            await self.plugin._start_when_ready()
        monitor.assert_called_once()
        await self.drain()

    async def test_a_monitor_failure_that_repeats_is_logged_once(self):
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 0)
        self.mock(M.Plugin, "_monitor_probe", side_effect=RuntimeError("probe broke"))
        sleeps = 0
        async def sleep(delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 10:
                raise asyncio.CancelledError()
        self.mock(M.asyncio, "sleep", side_effect=sleep)
        error = self.mock(M.decky.logger, "error")
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._monitor_loop()
        self.assertEqual(error.call_count, 1)

    async def test_a_failure_that_returns_after_a_recovery_is_logged_again(self):
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 0)
        self.mock(M.Plugin, "_monitor_probe", side_effect=[
            RuntimeError("probe broke"), (44, None), RuntimeError("probe broke")])
        self.mock(self.plugin, "_refresh_panel")
        sleeps = 0
        async def sleep(delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 5:
                raise asyncio.CancelledError()
        self.mock(M.asyncio, "sleep", side_effect=sleep)
        error = self.mock(M.decky.logger, "error")
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._monitor_loop()
        self.assertEqual(error.call_count, 2)

    async def test_boot_time_running_ahead_of_the_monotonic_clock_is_a_resume(self):
        """Only CLOCK_BOOTTIME counts suspended time; the gap is what reapplies."""
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 100)
        self.mock(M, "WATCH_INTERVAL_S", 100)
        ladders = []
        self.mock(self.plugin, "_schedule_reassert", side_effect=lambda: ladders.append("_reassert_repeatedly"))
        asleep = iter([0, 10, 0, 0])      # the second poll comes back after 10 s suspended
        offset = {"mono": 0.0, "boot": 0.0}
        async def sleep(delay):
            gap = next(asleep, None)
            if gap is None:
                raise asyncio.CancelledError()
            offset["mono"] += 1
            offset["boot"] += 1 + gap
        real_mono = M.time.monotonic
        self.mock(M.asyncio, "sleep", side_effect=sleep)
        self.mock(M.time, "monotonic", side_effect=lambda: real_mono() + offset["mono"])
        self.mock(M, "_boottime", side_effect=lambda: real_mono() + offset["boot"])
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._monitor_loop()
        self.assertEqual(ladders, ["_reassert_repeatedly"])

    async def test_after_a_resume_the_display_is_checked_before_the_watch(self):
        """Docked while asleep: a watch first would write the built-in screen's
        values to the external display before the plugin noticed."""
        events = []
        self.mock(self.plugin, "_schedule_reassert", side_effect=lambda: events.append("resume"))
        self.mock(M.Plugin, "_monitor_probe", side_effect=lambda: events.append("panel") or (44, None))
        self.mock(M.vibrance_x11, "session_identity", return_value=None)
        self.mock(self.plugin, "_refresh_panel")
        async def watch():
            events.append("watch")
        self.mock(self.plugin, "_watch_tick", side_effect=watch)
        gaps = iter([0, 0, 0, 0, 0, 10, 0, 0, 0, 0, 0])
        offset = {"mono": 0.0, "boot": 0.0}
        async def sleep(delay):
            gap = next(gaps, None)
            if gap is None:
                raise asyncio.CancelledError()
            offset["mono"] += 1
            offset["boot"] += 1 + gap
        real_mono = M.time.monotonic
        self.mock(M.asyncio, "sleep", side_effect=sleep)
        self.mock(M.time, "monotonic", side_effect=lambda: real_mono() + offset["mono"])
        self.mock(M, "_boottime", side_effect=lambda: real_mono() + offset["boot"])
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._monitor_loop()
        after = events[events.index("resume") + 1:]
        self.assertEqual(after[0], "panel", after)

    async def watch_failures_logged(self, watch_results):
        """Run the monitor with a healthy panel check and these watch outcomes."""
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 0)
        self.mock(M, "WATCH_INTERVAL_S", 0)
        self.mock(M.Plugin, "_monitor_probe", return_value=(44, None))
        self.mock(M.vibrance_x11, "session_identity", return_value=None)
        self.mock(self.plugin, "_refresh_panel")
        self.mock(self.plugin, "_watch_tick", side_effect=[*watch_results, asyncio.CancelledError()])
        sleeps = 0
        async def sleep(delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 50:  # a bound in case the loop stops reaching the watch
                raise asyncio.CancelledError()
        self.mock(M.asyncio, "sleep", side_effect=sleep)
        error = self.mock(M.decky.logger, "error")
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._monitor_loop()
        return error.call_count

    async def test_a_healthy_panel_check_does_not_relog_a_lasting_watch_failure(self):
        broken = RuntimeError("watch broke")
        self.assertEqual(await self.watch_failures_logged([broken] * 4), 1)

    async def test_a_watch_failure_that_returns_after_a_good_tick_is_logged_again(self):
        broken = RuntimeError("watch broke")
        self.assertEqual(await self.watch_failures_logged([broken, None, broken]), 2)

    async def test_repeated_resume_events_share_one_worker_and_restart_the_ladder(self):
        self.mock(M, "REASSERT_DELAYS_S", (0.02, 0))
        applied = self.mock(M.Plugin, "_reassert")
        self.plugin._schedule_reassert()
        await asyncio.sleep(0)
        worker = M.Plugin._workers["reassert"]
        for _ in range(100):
            self.plugin._schedule_reassert()
        self.assertEqual(M.Plugin._tasks, [worker])
        await self.drain()
        self.assertEqual(applied.call_count, 2)

    async def test_resume_while_a_step_waits_for_the_lock_still_runs_the_new_ladder(self):
        """The superseded ladder quits on its token and never consumes the
        resume event; the new ladder must not mistake it for a further resume."""
        self.mock(M, "REASSERT_DELAYS_S", (0.01, 0.01))
        applied = self.mock(M.Plugin, "_reassert")
        async with M.Plugin._mutation_lock:
            self.plugin._schedule_reassert()
            await asyncio.sleep(0.05)  # its first step now waits for this lock
            self.plugin._schedule_reassert()
        await self.drain()
        self.assertEqual(applied.call_count, 2)

    async def test_zero_atoms_during_settle_never_become_ready(self):
        self.mock(M, "_pick_display", return_value=":0")
        self.mock(M.vibrance_x11, "count_gamescope_atoms", side_effect=[0, 44, 44])
        self.mock(M, "GAMESCOPE_SETTLE_S", 0)
        self.mock(M, "GAMESCOPE_RETRY_FIRST_S", 0)
        self.mock(M.Plugin, "_capture_existing_looks")
        self.mock(M.Plugin, "_configure_for_panel", return_value=False)
        self.mock(self.plugin, "_reassert_repeatedly", return_value=None)
        await self.plugin._start_when_ready(start_monitor=False)
        self.assertEqual(M._pick_display.call_count, 2)
        self.assertTrue(M.Plugin._state["ready"])
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
        self.mock(M.vibrance_x11.subprocess, "run", side_effect=run)
        self.mock(M.vibrance_x11, "read_string_atom", side_effect=READ_STRING_ATOM)
        self.mock(M.vibrance_host, "read_edid", return_value=edid_with_red(0.640, 0.330))
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

    async def test_monitor_external_or_missing_metadata_never_adds_atom_queries(self):
        read = self.mock(M.vibrance_host, "read_edid")
        run = self.mock(M.vibrance_x11.subprocess, "run")
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
        run = self.mock(M.vibrance_x11.subprocess, "run", return_value=SimpleNamespace(stdout=
            f'{M.ATOM_IS_EXTERNAL}(CARDINAL) = 0\n'
            f'{M.ATOM_EDID_PATH}(UTF8_STRING) = "/tmp/a\\\"b\\\\c.bin"'))
        self.mock(M.vibrance_host, "file_identity", return_value=(1, 2, 128, 3, 4))
        read = self.mock(M.vibrance_host, "read_edid", return_value=edid_with_red(0.640, 0.330))
        self.mock(M.Plugin, "_panel_cache_key", None)
        self.mock(M.Plugin, "_panel_cache_complete", False)
        _, probe = M.Plugin._monitor_probe()
        M.Plugin._configure_for_panel(probe)
        _, cached = M.Plugin._monitor_probe()
        self.assertEqual(run.call_count, 2)
        read.assert_called_once_with('/tmp/a"b\\c.bin')
        self.assertIsNone(cached[2])

    async def test_failed_monitor_root_read_does_not_read_an_edid(self):
        self.mock(M.vibrance_x11.subprocess, "run", side_effect=OSError("X unavailable"))
        read = self.mock(M.vibrance_host, "read_edid")
        self.assertIsNone(M.Plugin._monitor_probe())
        read.assert_not_called()


class ReassertGuards(PluginCase):
    def arm(self):
        M.Plugin._state.update(sdr_enabled=True, sdr_saturation=1.5, sdr_applied=True,
                               hdr_enabled=True, hdr_saturation=1.3, hdr_applied=True,
                               hdr_applied_saturation=1.3)
        M.Plugin._hdr_look_path = "/tmp/hdr-look-0.cube"
        M.Plugin._last_sdr_written = 1.5
        self.float_writer.reset_mock()
        self.string_writer.reset_mock()

    def test_a_look_whose_file_is_gone_is_not_pointed_at_again(self):
        """gamescope would fail to open it and drop the look; the pending-look
        retry rebuilds it instead."""
        self.arm()
        self.mock(M.vibrance_x11, "display_is_external", return_value=False)
        self.mock(M.os.path, "exists", side_effect=lambda p: p != M.Plugin._hdr_look_path)
        M.Plugin._reassert()
        self.string_writer.assert_not_called()
        self.float_writer.assert_called()               # the SDR half still goes back

    def test_nothing_is_reasserted_while_known_docked_or_disconnected(self):
        for flags in ({"external_display": True}, {"disconnected": True}):
            with self.subTest(**flags):
                self.arm()
                M.Plugin._state["external_display"] = flags.get("external_display", False)
                M.Plugin._disconnected = flags.get("disconnected", False)
                # An older gamescope (or a failed read) cannot say it is docked.
                with patch.object(M.vibrance_x11, "display_is_external", return_value=None):
                    M.Plugin._reassert()
                self.string_writer.assert_not_called()
                self.float_writer.assert_not_called()


class GamescopeBackoff(IsolatedStartup):
    def test_waiting_for_gamescope_backs_off_to_the_ceiling(self):
        delays = []
        attempts = []

        def absent_then_up():
            attempts.append(1)
            return ":0" if len(attempts) > 7 else None

        async def record(seconds):
            delays.append(seconds)

        M._pick_display = absent_then_up
        M._panel_is_wide_gamut = lambda *_args: True
        M._panel_supports_hdr = lambda *_args: True
        M.vibrance_x11.count_gamescope_atoms = lambda display: 44
        with patch.object(asyncio, "sleep", record), patch.object(M.shutil, "which", return_value="/usr/bin/xprop"):
            drain(M.Plugin()._start_when_ready(start_monitor=False))
        first, ceiling = M.GAMESCOPE_RETRY_FIRST_S, M.GAMESCOPE_RETRY_MAX_S
        expected = [min(first * 2 ** n, ceiling) for n in range(7)]
        self.assertEqual(delays[:7], expected)


class UnreadableIdentity(PluginCase):
    def stub_connect(self, identity):
        self.mock(M.vibrance_x11, "session_identity", return_value=identity)
        self.mock(M, "_pick_display", return_value=":0")
        self.mock(M.vibrance_x11, "count_gamescope_atoms", return_value=44)
        self.mock(M, "GAMESCOPE_SETTLE_S", 0)
        self.mock(M.Plugin, "_configure_for_panel", return_value=False)
        self.mock(M.Plugin, "_capture_existing_looks")
        self.mock(self.plugin, "_reassert_repeatedly", return_value=None)

    async def test_an_unreadable_identity_twice_is_still_not_the_same_session(self):
        M.Plugin._session_identity = None             # the last connect could not read it either
        M.Plugin._disconnected = True
        M.Plugin._hdr_look_baseline = "/somebody/else.cube"
        M.Plugin._sdr_baseline = 0.4
        self.stub_connect(None)
        await self.plugin._start_when_ready(start_monitor=False)
        await self.drain()
        self.assertIsNone(M.Plugin._hdr_look_baseline)
        self.assertNotEqual(M.Plugin._sdr_baseline, 0.4)


class MonitorCadence(PluginCase):
    def run_ticks(self, ticks):
        clock = itertools.count()
        # Not time.monotonic itself: the event loop reads that clock too.
        self.mock(M, "time", SimpleNamespace(monotonic=lambda: float(next(clock))))
        self.mock(M, "_boottime", side_effect=lambda: 0.0)   # never ahead: no resume
        sleeps = 0

        async def sleep(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > ticks:
                raise asyncio.CancelledError()
        self.mock(M.asyncio, "sleep", side_effect=sleep)

    async def test_an_unreadable_socket_is_not_a_new_session(self):
        M.Plugin._session_identity = (":0", (1, 1))
        self.mock(M.vibrance_x11, "session_identity", return_value=None)
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 0)
        self.mock(M, "SUSPEND_POLL_S", 0)
        self.mock(M.Plugin, "_monitor_probe", return_value=(44, None))
        # Either path ends the loop, so a regression fails instead of spinning.
        recovery = self.mock(self.plugin, "_start_when_ready", side_effect=asyncio.CancelledError())
        refresh = self.mock(self.plugin, "_refresh_panel", side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._monitor_loop()
        recovery.assert_not_awaited()
        refresh.assert_awaited_once()
        self.assertEqual(M.Plugin._sdr_baseline, 0.4)

    async def test_the_panel_is_probed_every_interval_and_a_failed_save_retried(self):
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 5.0)
        self.mock(M, "WATCH_INTERVAL_S", 1000.0)
        self.mock(M.vibrance_x11, "session_identity", return_value=None)
        probe = self.mock(M.Plugin, "_monitor_probe", return_value=(44, None))
        self.mock(self.plugin, "_refresh_panel")
        retry = self.mock(self.plugin, "_retry_settings_save")
        self.run_ticks(20)                                 # 20 one-second ticks
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._monitor_loop()
        self.assertEqual(probe.call_count, 4)
        self.assertEqual(retry.call_count, 4)


class NewSessionBaselines(PluginCase):
    """test_lifecycle.py, next to UnreadableIdentity. Kills A8."""

    async def test_an_unreadable_atom_on_a_replaced_server_restores_gamescope_s_default(self):
        """The old server's baseline means nothing to a restarted gamescope."""
        M.Plugin._session_identity = (":0", (1, 1))
        M.Plugin._disconnected = True
        M.Plugin._sdr_baseline = 0.4
        M.Plugin._state["sdr_enabled"] = True
        self.mock(M.vibrance_x11, "session_identity", return_value=(":0", (2, 2)))
        self.mock(M, "_pick_display", return_value=":0")
        self.mock(M.vibrance_x11, "count_gamescope_atoms", return_value=44)
        self.mock(M, "GAMESCOPE_SETTLE_S", 0)
        self.mock(M.Plugin, "_configure_for_panel", return_value=False)
        self.mock(M.Plugin, "_capture_existing_looks")
        self.mock(self.plugin, "_reassert_repeatedly", return_value=None)
        self.live_sdr = None                                  # every read of the atom fails
        await self.plugin._start_when_ready(start_monitor=False)
        await self.drain()
        self.assertIsNone(M.Plugin._sdr_baseline)


class MonitorIdentity(PluginCase):
    """Kills PT2."""

    async def test_an_identity_readable_only_now_is_not_a_new_session(self):
        M.Plugin._session_identity = None                     # connect could not read the socket
        self.mock(M.vibrance_x11, "session_identity", return_value=(":0", (1, 1)))
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 0)
        self.mock(M, "SUSPEND_POLL_S", 0)
        self.mock(M.Plugin, "_monitor_probe", return_value=(44, None))
        recovery = self.mock(self.plugin, "_start_when_ready", side_effect=asyncio.CancelledError())
        refresh = self.mock(self.plugin, "_refresh_panel", side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin._monitor_loop()
        recovery.assert_not_awaited()
        refresh.assert_awaited_once()


class WaitingForGamescope(PluginTest):
    """test_lifecycle.py, next to the retry back-off test. Kills W1."""

    def test_a_missing_xprop_is_named_while_waiting(self):
        reasons = []

        async def record(_seconds):
            reasons.append(M.Plugin._state["reason"])

        answers = iter((None, ":0"))
        M._pick_display = lambda: next(answers)
        M.vibrance_x11.count_gamescope_atoms = lambda display: 44
        M.Plugin._unloading = False
        with patch.object(asyncio, "sleep", record), patch.object(M.shutil, "which", return_value=None):
            self.assertTrue(asyncio.run(M.Plugin()._wait_for_gamescope()))
        self.assertEqual(reasons[0], "xprop is missing; install xorg-xprop")


class ConflictWarning(PluginTest):
    """Kills F2, F4."""

    def setUp(self):
        M.Plugin._foreign_sdr_seen, M.Plugin._foreign_active_ticks = None, 0

    def test_the_warning_fades_only_after_quiet_slow_ticks(self):
        M.Plugin._foreign_conflict(((":1", "1"),), True)                 # reference only
        self.assertTrue(M.Plugin._foreign_conflict(((":1", "2"),), True))  # it moved
        for _ in range(M.CONFLICT_STRIKES - 1):
            self.assertTrue(M.Plugin._foreign_conflict(((":1", "2"),), True))
        self.assertEqual(M.Plugin._foreign_conflict(((":1", "2"),), True), "")

    def test_a_foreign_atom_that_disappears_is_not_a_new_writer(self):
        M.Plugin._foreign_conflict(((":1", "1"),), True)
        self.assertEqual(M.Plugin._foreign_conflict((), True), "")


class WriteIntoUnadoptedSession(PluginCase):
    async def test_a_watch_tick_does_not_write_into_a_server_not_yet_adopted(self):
        """gamescope restarted between two panel ticks: the watch tick finds
        the SDR atom gone on the new server and recreates it before the panel
        tick has reconnected. _adopt_session then reads our own write back,
        takes the old server's baseline, and "off" later writes that leftover
        into a session that never had it, instead of gamescope's unset.
        """
        old, new = (":0", (1, 1)), (":0", (2, 2))
        M.Plugin._session_identity = old
        M.Plugin._sdr_baseline = 0.4                  # found on the old server
        M.Plugin._state.update(sdr_enabled=True, sdr_applied=True, sdr_saturation=2.0)
        M.Plugin._last_sdr_written = M.Plugin._last_sdr_attempted = 2.0
        self.mock(M.vibrance_x11, "session_identity", return_value=new)
        self.mock(M.vibrance_x11, "count_gamescope_atoms", return_value=44)
        self.live_sdr = None                          # fresh server: atom absent

        M.Plugin._watch_once(None)
        self.assertEqual(self.float_writer.call_count, 0,
                         "wrote into a session whose baseline was never read")

        # What it costs: the reconnect that follows keeps the old baseline.
        async with M.Plugin._mutation_lock:
            await self.plugin._adopt_session(new)
        self.assertIsNone(M.Plugin._sdr_baseline)


class SessionEdges(PluginCase):
    def stub_connect(self, identity):
        self.mock(M.vibrance_x11, "session_identity", return_value=identity)
        self.mock(M, "_pick_display", return_value=":0")
        self.mock(M.vibrance_x11, "count_gamescope_atoms", return_value=44)
        self.mock(M, "GAMESCOPE_SETTLE_S", 0)
        self.mock(M.Plugin, "_configure_for_panel", return_value=False)
        self.mock(M.Plugin, "_capture_existing_looks")
        self.mock(self.plugin, "_reassert_repeatedly", return_value=None)

    async def test_after_a_restart_the_new_session_s_value_is_restored_even_if_we_had_written(self):
        """The usual case: our value was on the old server when gamescope restarted."""
        M.Plugin._session_identity = (":0", (1, 1))
        M.Plugin._state.update(sdr_enabled=True, sdr_saturation=1.5, sdr_applied=True)
        M.Plugin._last_sdr_written = M.Plugin._last_sdr_attempted = 1.5
        self.live_sdr = 0.6                                   # set on the new server by another tool
        self.stub_connect((":0", (2, 2)))
        await self.plugin._start_when_ready(start_monitor=False)
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.6)

    async def test_a_healthy_tick_on_the_known_session_does_not_reconnect(self):
        M.Plugin._session_identity = (":0", (1, 1))
        self.mock(M.vibrance_x11, "session_identity", return_value=(":0", (1, 1)))
        self.mock(M.Plugin, "_monitor_probe", return_value=(44, (False, ("k",), None)))
        start = self.mock(M.Plugin, "_start_when_ready", new_callable=AsyncMock)
        self.mock(M.Plugin, "_refresh_panel", new_callable=AsyncMock)
        self.assertTrue(await self.plugin._panel_tick())
        start.assert_not_called()
        self.assertFalse(M.Plugin._disconnected)

    async def test_unload_returns_only_once_the_restore_has_run(self):
        done = []
        self.mock(M.Plugin, "_release_sdr", side_effect=lambda: (time.sleep(0.2), done.append("sdr")))
        await self.plugin._unload()
        self.assertEqual(done, ["sdr"])

    async def test_a_settings_file_that_keeps_failing_is_retried_at_its_pace(self):
        reads = []
        M.Plugin._settings_loaded = False
        self.mock(M, "SETTINGS_RETRY_S", 0.05)
        self.mock(M.vibrance_settings, "read", side_effect=lambda _m: reads.append(1) or (_ for _ in ()).throw(
            PermissionError("denied")))
        task = asyncio.create_task(self.plugin._load_settings())
        await asyncio.sleep(0.2)
        M.Plugin._unloading = True
        self.assertFalse(await task)
        self.assertGreaterEqual(len(reads), 2)       # it does retry...
        self.assertLessEqual(len(reads), 8)          # ...at its pace


class DisconnectFlags(PluginTest):
    def test_a_lost_session_blocks_writes_and_forgets_the_panel(self):
        M.Plugin._state.update(ready=True, reason="ready")
        M.Plugin._disconnected = False
        M.Plugin._panel_cache_key, M.Plugin._panel_cache_complete = ("k",), True
        M.Plugin._foreign_sdr_seen, M.Plugin._foreign_active_ticks = ((":1", "1"),), 3
        hdr, sdr = M.Plugin._hdr_generation, M.Plugin._sdr_generation
        M.Plugin._disconnect()
        self.assertTrue(M.Plugin._disconnected)
        self.assertFalse(M.Plugin._state["ready"])
        self.assertIn("disconnected", M.Plugin._state["reason"])
        self.assertEqual((M.Plugin._panel_cache_key, M.Plugin._panel_cache_complete), (None, False))
        self.assertEqual((M.Plugin._foreign_sdr_seen, M.Plugin._foreign_active_ticks), (None, 0))
        self.assertGreater(M.Plugin._hdr_generation, hdr)
        self.assertGreater(M.Plugin._sdr_generation, sdr)


class WatchEdges(PluginCase):
    async def test_a_slow_tick_puts_a_dropped_look_back(self):
        M.Plugin._state.update(hdr_enabled=True, hdr_saturation=1.3)
        self.plugin._schedule_look("hdr")
        await self.drain()
        ours = self.strings[M.ATOM_LOOK_PQ]
        self.strings[M.ATOM_LOOK_PQ] = ""                     # gamescope dropped it
        M.Plugin._slow_checks = M.SLOW_CHECK_EVERY - 1
        M.Plugin._watch_once(None)
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], ours)

    async def test_switching_sdr_off_clears_its_conflict_warning(self):
        M.Plugin._state.update(sdr_enabled=False, conflict="another plugin keeps overwriting")
        M.Plugin._reassert_strikes = 3
        M.Plugin._watch_once(None)
        self.assertEqual((M.Plugin._state["conflict"], M.Plugin._reassert_strikes), ("", 0))

    async def test_a_matching_atom_clears_an_old_sdr_error(self):
        M.Plugin._state.update(sdr_enabled=True, sdr_applied=True, sdr_error="could not write SDR to gamescope")
        target, _ = M._sdr_split(M.Plugin._state["sdr_saturation"])
        M.Plugin._last_sdr_written = self.live_sdr = target
        M.Plugin._watch_once(None)
        self.assertEqual(M.Plugin._state["sdr_error"], "")


class LoadedLikeDecky(PluginTest):
    def test_main_finds_its_own_modules_when_loaded_from_its_file(self):
        """Decky execs main.py by path (spec_from_file_location) and only adds
        py_modules to sys.path, so main.py has to add its own folder."""
        tests = os.path.dirname(os.path.abspath(__file__))
        script = (f"import sys, importlib.util\nsys.path.insert(0, {tests!r})\n"
                  "import _harness\n_harness.install()\n"
                  f"sys.path.remove({tests!r})\n"
                  f"spec = importlib.util.spec_from_file_location('_', {os.path.abspath(M.__file__)!r})\n"
                  "module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
                  "print(module.Plugin.__name__)\n")
        with tempfile.TemporaryDirectory() as folder:
            run = subprocess.run([sys.executable, "-I", "-c", script], cwd=folder,
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(run.stdout.strip().splitlines()[-1:], ["Plugin"], run.stderr[-500:])


class LookIntoUnadoptedSession(PluginCase):
    """The SDR half no longer writes into a gamescope that restarted between two
    panel ticks; the looks still do. A slider move commits our LUT over a look
    the new session already had, adoption then reads our own path back as
    "ours, baseline none", and switching HDR off erases the other look instead
    of restoring it.
    """

    def setUp(self):
        super().setUp()
        self.old, self.new = (":0", (1, 1)), (":0", (2, 2))
        M.Plugin._session_identity = self.old
        self.mock(M.vibrance_x11, "session_identity", return_value=self.new)
        M.Plugin._hdr_look_path = "/tmp/hdr-look-0.cube"   # ours, on the old server
        M.Plugin._look_slot = 0
        M.Plugin._state.update(hdr_enabled=True, hdr_applied=True,
                               hdr_saturation=1.2, hdr_applied_saturation=1.2)
        self.strings[M.ATOM_LOOK_PQ] = "/other/look.cube"   # the new session's own look

    async def test_a_slider_move_does_not_commit_a_look_into_a_session_not_yet_adopted(self):
        await self.plugin.set_hdr_saturation(1.4)
        await self.drain()
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/other/look.cube",
                         "committed into a session whose look was never read")

        # What it costs: adopt the new session, then switch HDR off.
        async with M.Plugin._mutation_lock:
            await self.plugin._adopt_session(self.new)
        M.Plugin._session_identity = self.new
        await self.plugin.set_hdr_enabled(False)
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/other/look.cube")

    def test_the_slow_tick_does_not_reapply_a_look_into_a_session_not_yet_adopted(self):
        self.strings.pop(M.ATOM_LOOK_PQ)               # fresh server: no look at all
        M.Plugin._reassert_looks_if_dropped()
        self.string_writer.assert_not_called()


class UnadoptedSessionError(PluginCase):
    async def test_a_held_back_write_says_why_and_the_next_matching_one_clears_it(self):
        old, new = (":0", (1, 1)), (":0", (2, 2))
        M.Plugin._session_identity = old
        M.Plugin._state.update(sdr_enabled=True, sdr_saturation=2.0)
        identity = self.mock(M.vibrance_x11, "session_identity", return_value=new)
        M.Plugin._apply_sdr()
        self.float_writer.assert_not_called()
        self.assertEqual(M.Plugin._state["sdr_error"], "waiting for gamescope before applying SDR")
        self.assertIn("waiting for gamescope", M.Plugin._state["reason"])
        identity.return_value = old
        M.Plugin._apply_sdr()
        self.assertEqual(M.Plugin._state["sdr_error"], "")
        self.assertTrue(M.Plugin._state["sdr_applied"])
