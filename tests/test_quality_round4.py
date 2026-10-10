"""Fourth review: redundant work, concurrent checks and evolving LUT baselines."""
import asyncio
import json
import threading
import unittest
from unittest.mock import patch

import _harness
_harness.install()
import main as M
import lego_updater as U
import test_recovery as R


class Controls(unittest.IsolatedAsyncioTestCase):
    setUp = R.Recovery.setUp
    asyncTearDown = R.Recovery.asyncTearDown
    mock = R.Recovery.mock
    drain = R.Recovery.drain
    write_float = R.Recovery.write_float
    write_string = R.Recovery.write_string

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


class UpdateChecks(unittest.TestCase):
    def overlapping_checks(self, newest):
        updater = U.Updater(releases_url=M.GITHUB_RELEASES_URL, user_agent="test", log_prefix="test",
                            plugin_dir=M.PLUGIN_DIR, logger=M.decky.logger,
                            asset_name=lambda v: f"DeckyVibranceHDR-{v}.zip")
        entered, release = threading.Event(), threading.Event()
        older = {}
        def stream(_url, out, *args, **kwargs):
            if threading.current_thread().name == "older-check":
                entered.set()
                release.wait(2)
                data = self.release("9.0.0")
            else:
                data = newest
            out.write(json.dumps(data).encode())
        thread = threading.Thread(target=lambda: older.update(updater.check()), name="older-check")
        with patch.object(updater, "plugin_version", return_value="8.0.0"), \
                patch.object(updater, "_stream_to", side_effect=stream):
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                info = updater.check()
            finally:
                release.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
        return updater, info, older

    def release(self, version):
        name = f"DeckyVibranceHDR-{version}.zip"
        return {"tag_name": version, "assets": [{"name": name,
            "browser_download_url": f"https://github.com/Rayekkk/DeckyVibranceHDR/releases/download/{version}/{name}"}]}

    def test_older_check_cannot_overwrite_the_newer_visible_asset(self):
        updater, newest, older = self.overlapping_checks(self.release("10.0.0"))
        self.assertEqual(updater._checked_asset, (newest["download_url"], newest["asset_name"]))
        self.assertNotIn("download_url", older)
        self.assertIn("superseded", older["error"])

    def test_older_check_cannot_reauthorize_after_the_newer_check_failed(self):
        updater, newest, older = self.overlapping_checks({"message": "service unavailable"})
        self.assertIn("error", newest)
        self.assertIsNone(updater._checked_asset)
        self.assertNotIn("download_url", older)
