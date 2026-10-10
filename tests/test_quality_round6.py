"""Sixth review: report failed LUT reasserts and retain pending replacements."""
import hashlib
import asyncio
import io
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import _harness
_harness.install()
import main as M
import test_recovery as R
import test_audit_regressions as A
import vibrance_lut as V


class ReassertFailures(unittest.IsolatedAsyncioTestCase):
    setUp = R.Recovery.setUp
    asyncTearDown = R.Recovery.asyncTearDown
    mock = R.Recovery.mock
    drain = R.Recovery.drain
    write_float = R.Recovery.write_float
    write_string = R.Recovery.write_string

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


class SessionOwnership(unittest.IsolatedAsyncioTestCase):
    setUp = R.Recovery.setUp
    asyncTearDown = R.Recovery.asyncTearDown
    mock = R.Recovery.mock
    drain = R.Recovery.drain
    write_float = R.Recovery.write_float
    write_string = R.Recovery.write_string

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


class SocketIdentity(unittest.TestCase):
    def test_identity_detects_replacement_at_same_display_and_missing_path(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(M, "X11_SOCKET_DIR", folder), \
                patch.object(M, "DISPLAY", ":12"):
            socket = Path(folder) / "X12"
            socket.write_text("first socket fixture")
            first = M._display_session_identity()
            replacement = Path(folder) / "replacement"
            replacement.write_text("new socket fixture")
            replacement.replace(socket)
            self.assertNotEqual(M._display_session_identity(), first)
            socket.unlink()
            self.assertIsNone(M._display_session_identity())


class ArchiveNames(unittest.TestCase):
    make = A.Updater.make
    archive = A.Updater.archive
    attempt = A.Updater.attempt

    def test_embedded_nul_in_entry_names_cannot_replace_the_previous_archive(self):
        output = io.BytesIO(self.archive())
        name = "DeckyVibranceHDR/fileXtail"
        with zipfile.ZipFile(output, "a") as archive:
            archive.writestr(name, "payload")
        payload = output.getvalue().replace(name.encode(), name.replace("X", "\0").encode())
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            entry = archive.infolist()[-1]
            self.assertNotEqual(entry.orig_filename, entry.filename)
        result, data = self.attempt(payload)
        self.assertFalse(result["success"], "a truncated filename passed archive validation")
        self.assertEqual(data, b"previous")
