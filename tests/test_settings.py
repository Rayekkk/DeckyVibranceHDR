# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Reading and writing the settings file.

The file-level guarantees (atomic fsynced writes, modes, temp files, FIFOs,
symlinks) belong to dk_fs and are tested in test_dk_fs; this covers what the
plugin builds on them: retries, notices, merging, refusals and save timing.
"""

import asyncio
import json
import os
import secrets
import threading
from pathlib import Path
from unittest.mock import patch

from _fixtures import (PATH_EXISTS, PluginCase, PluginTest, SAVE_SETTINGS,  # installs the decky stand-in first
                       write_settings)
import vibrance_control as M

dk_fs = M.vibrance_settings.dk_fs
REAL_READ = dk_fs.read_bytes
REAL_WRITE = dk_fs.write_atomic
LOST = "The saved settings could not be read, so defaults are in use."


def settings_file() -> Path:
    return Path(M.settings_path)


def corrupt_copies():
    return sorted(settings_file().parent.glob("settings.json.corrupt-*"))


class FileCase(PluginCase):
    """A PluginCase with a real, empty settings folder and ways to make it fail."""

    def setUp(self):
        super().setUp()
        write_settings(None)
        self.addCleanup(write_settings, None)

    def fail_reads(self, *errors, forever=None):
        """dk_fs.read_bytes of settings.json raises *errors* in turn, then forever() if given."""
        loop = asyncio.get_running_loop()
        self.read_failed = asyncio.Event()
        pending = list(errors)

        def read(path, *args, **kwargs):
            if os.path.basename(path) == "settings.json" and (pending or forever):
                loop.call_soon_threadsafe(self.read_failed.set)
                raise pending.pop(0) if pending else forever()
            return REAL_READ(path, *args, **kwargs)
        return self.mock(dk_fs, "read_bytes", side_effect=read)

    def fail_aside(self, *errors, forever=None):
        """dk_fs.write_atomic of a settings.json.corrupt-* copy raises like fail_reads."""
        loop = asyncio.get_running_loop()
        self.aside_failed = asyncio.Event()
        pending = list(errors)

        def write(path, *args, **kwargs):
            if ".corrupt-" in os.path.basename(path) and (pending or forever):
                loop.call_soon_threadsafe(self.aside_failed.set)
                raise pending.pop(0) if pending else forever()
            return REAL_WRITE(path, *args, **kwargs)
        return self.mock(dk_fs, "write_atomic", side_effect=write)

    async def until(self, condition, timeout=2.0):
        for _ in range(int(timeout / 0.01)):
            if condition():
                return
            await asyncio.sleep(0.01)
        self.fail("condition not met in time")


class SettingsValues(PluginTest):
    def setUp(self):
        M.state.settings_loaded = True
        write_settings(None)
        self.addCleanup(write_settings, None)

    def test_nonfinite_xproperty_cannot_become_a_saved_baseline(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            self.assertIsNone(M.vibrance_x11.cardinal_to_float(M.vibrance_x11.float_to_cardinal(value)))

    def test_one_snapshot_preserves_other_keys_and_keeps_old_file_on_failure(self):
        old = {"unrelated": 17, **M.DEFAULT_SETTINGS}
        write_settings(old)
        self.assertEqual(M.vibrance_settings.load(M.settings_path), "")
        M.state.public.update({**M.DEFAULT_SETTINGS, "hdr_saturation": 1.4})
        with patch.object(dk_fs, "write_atomic", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                M.Controller._save()
        self.assertEqual(json.loads(settings_file().read_text()), old)
        self.assertEqual(M.vibrance_settings.saved, old)
        M.Controller._save()
        self.assertEqual(json.loads(settings_file().read_text()), {**old, "hdr_saturation": 1.4})
        self.assertEqual(M.vibrance_settings.saved, {**old, "hdr_saturation": 1.4})

    def test_corrupt_settings_fields_use_defaults_independently(self):
        for bad in (None, "wrong", [], {}, float("nan"), float("inf"), True):
            with patch.dict(M.vibrance_settings.saved, {"hdr_saturation": bad}):
                self.assertEqual(M._setting("hdr_saturation", 1.0), 1.0)
        for bad in ("false", 0, 1, None, []):
            with patch.dict(M.vibrance_settings.saved, {"hdr_enabled": bad}):
                self.assertIs(M._setting("hdr_enabled", False), False)

    def test_regular_file_retains_unknown_fields(self):
        values = {"sdr_enabled": True, "custom": {"label": "zażółć"}}
        settings_file().write_text(json.dumps(values), encoding="utf-8")
        self.assertEqual(M.vibrance_settings.load(M.settings_path), "")
        self.assertEqual(M.vibrance_settings.saved, values)
        self.assertEqual(json.loads(settings_file().read_text(encoding="utf-8")), values)

    def test_a_root_that_is_not_an_object_is_damage(self):
        for invalid in ("null", "[]", "42"):
            with self.subTest(content=invalid):
                write_settings(None)
                settings_file().write_text(invalid)
                M.vibrance_settings.saved["stale"] = 1
                self.assertTrue(M.vibrance_settings.load(M.settings_path).startswith(LOST))
                self.assertEqual(M.vibrance_settings.saved, {})
                self.assertEqual(settings_file().read_text(), invalid)  # replaced only by the next save

    def test_no_file_is_a_first_install_on_defaults(self):
        M.vibrance_settings.saved["stale"] = 1
        self.assertEqual(M.vibrance_settings.load(M.settings_path), "")
        self.assertEqual(M.vibrance_settings.saved, {})
        self.assertFalse(settings_file().exists())

    def test_an_empty_file_loads_as_defaults_and_is_left_as_it_is(self):
        # Decky's first-install file, or 1.0.0's truncating write cut short:
        # nothing to protect, so it must not block startup.
        for empty in ("", "  \n"):
            with self.subTest(content=empty):
                settings_file().write_text(empty)
                M.vibrance_settings.saved["stale"] = 1
                self.assertEqual(M.vibrance_settings.load(M.settings_path), "")
                self.assertEqual(M.vibrance_settings.saved, {})
                self.assertEqual(settings_file().read_text(), empty)
                self.assertEqual(corrupt_copies(), [])

    def test_a_file_that_loaded_once_and_goes_missing_comes_back_silently(self):
        values = {"hdr_saturation": 1.4, "custom": "keep"}
        write_settings(values)
        self.assertEqual(M.vibrance_settings.load(M.settings_path), "")
        os.remove(M.settings_path)  # card unmounted, say
        M.vibrance_settings.saved.clear()
        self.assertEqual(M.vibrance_settings.load(M.settings_path), "")
        self.assertEqual(M.vibrance_settings.saved, values)
        self.assertEqual(json.loads(settings_file().read_text()), values)


class ReadFailure(FileCase):

    async def test_failed_settings_read_then_unload_preserves_existing_file(self):
        self.mock(M.Controller, "_save", side_effect=SAVE_SETTINGS)
        self.mock(self.plugin, "_start_when_ready")
        write_settings({"sdr_enabled": True, "hdr_enabled": True,
                        "sdr_saturation": 2.6, "hdr_saturation": 1.4, "custom": "keep"})
        before = settings_file().read_bytes()
        self.fail_reads(forever=lambda: PermissionError(13, "Permission denied", M.settings_path))
        startup = asyncio.create_task(self.plugin._main())
        try:
            await asyncio.wait_for(self.read_failed.wait(), 5)
            await self.until(lambda: M.state.public["settings_error"])
            self.assertFalse(M.state.settings_loaded)
            self.assertIn("settings", M.state.public["reason"])
            await self.plugin._unload()
            self.assertEqual(settings_file().read_bytes(), before)
        finally:
            M.state.unloading = True
            await asyncio.wait_for(startup, 2)

    async def test_transient_settings_read_failure_recovers_saved_values(self):
        recovery = self.mock(self.plugin, "_start_when_ready")
        self.mock(M, "SETTINGS_RETRY_S", 0.013)
        real_sleep = asyncio.sleep
        sleep = self.mock(M.asyncio, "sleep", side_effect=real_sleep)
        saved = {"sdr_enabled": True, "hdr_enabled": True,
                 "sdr_saturation": 2.7, "hdr_saturation": 1.37}
        write_settings(saved)
        before = settings_file().read_bytes()
        reads = self.fail_reads(PermissionError(13, "Permission denied", M.settings_path),
                                OSError(5, "Input/output error", M.settings_path))
        await self.plugin._main()
        self.assertEqual(settings_file().read_bytes(), before)  # retried, never replaced
        await asyncio.gather(*M.state.tasks)
        self.assertEqual(sum(os.path.basename(c.args[0]) == "settings.json" for c in reads.call_args_list), 3)
        self.assertEqual(sum(c.args == (0.013,) for c in sleep.call_args_list), 2)
        self.assertTrue(M.state.settings_loaded)
        self.assertTrue(M.state.public["settings_loaded"])
        for key, value in saved.items():
            self.assertEqual(M.state.public[key], value)
        self.assertEqual(M.state.public["settings_error"], "")
        recovery.assert_awaited_once()

    async def test_damaged_settings_without_a_good_copy_start_on_defaults_and_are_kept(self):
        """Bad content never parses on a retry, so waiting would leave the
        plugin dead with no way out from the menu. The bytes are kept."""
        self.mock(M.Controller, "_save", side_effect=SAVE_SETTINGS)
        recovery = self.mock(self.plugin, "_start_when_ready")
        damaged = (b"{broken settings", b"\xff\xfe not utf-8", b"[1, 2]",
                   b'{"sdr_enabled": true, "sdr_enabled": false}', b"[" * 100000)
        for broken in damaged:
            with self.subTest(content=broken[:24]):
                write_settings(None)
                settings_file().write_bytes(broken)
                M.state.public["settings_notice"] = ""
                # Bounded: a regression that never reads defaults would loop forever.
                await asyncio.wait_for(self.plugin._main(), 5)
                await asyncio.gather(*M.state.tasks)  # the save _main schedules
                self.assertTrue(M.state.public["settings_loaded"])
                self.assertFalse(M.state.public["sdr_enabled"])
                self.assertEqual(M.state.public["settings_error"], "")
                (aside,) = corrupt_copies()
                self.assertEqual(aside.read_bytes(), broken)  # still there after the save
                self.assertEqual(M.state.public["settings_notice"],
                                 f"{LOST} The old file is kept as {aside.name}.")
                self.assertEqual(json.loads(settings_file().read_text()), M.DEFAULT_SETTINGS)
        self.assertEqual(recovery.call_count, len(damaged))

    async def test_damaged_settings_with_a_good_copy_bring_its_values_back(self):
        self.mock(M.Controller, "_save", side_effect=SAVE_SETTINGS)
        self.mock(self.plugin, "_start_when_ready")
        write_settings({"sdr_enabled": True, "sdr_saturation": 2.6, "custom": "keep"})
        settings_file().write_bytes(b"{broken")
        await self.plugin._main()
        await asyncio.gather(*M.state.tasks)
        self.assertTrue(M.state.public["sdr_enabled"])
        self.assertEqual(M.state.public["sdr_saturation"], 2.6)
        self.assertEqual(M.state.public["settings_error"], "")
        (aside,) = corrupt_copies()
        self.assertEqual(aside.read_bytes(), b"{broken")
        self.assertEqual(M.state.public["settings_notice"],
                         "The saved settings were damaged, so the last good copy is in use. "
                         f"The damaged file is kept as {aside.name}.")
        self.assertEqual(json.loads(settings_file().read_text())["custom"], "keep")

    async def test_a_damaged_file_that_cannot_be_kept_aside_stays_and_is_retried(self):
        self.mock(self.plugin, "_start_when_ready")
        settings_file().write_bytes(b"{broken")
        writes = self.fail_aside(forever=lambda: PermissionError(13, "Permission denied",
                                                                 M.settings_path + ".corrupt-1"))
        startup = asyncio.create_task(self.plugin._main())
        try:
            await asyncio.wait_for(self.aside_failed.wait(), 5)
            await self.until(lambda: writes.call_count >= 2 and M.state.public["settings_error"])
            self.assertFalse(M.state.public["settings_loaded"])
            self.assertEqual(M.state.public["settings_error"],
                             "could not load settings: Permission denied: settings.json.corrupt-1")
            self.assertEqual(settings_file().read_bytes(), b"{broken")
            self.assertEqual(corrupt_copies(), [])
        finally:
            M.state.unloading = True
            await asyncio.wait_for(startup, 2)

    async def test_a_lasting_read_failure_is_logged_once_not_every_retry(self):
        self.mock(self.plugin, "_start_when_ready")
        self.mock(M.vibrance_settings, "load", side_effect=[PermissionError("denied")] * 4 + [""])
        warning = self.mock(M.vibrance_state.logger, "warning")
        await self.plugin._main()
        logged = [call for call in warning.call_args_list if "could not load settings" in call.args[0]]
        self.assertEqual(len(logged), 1)

    async def test_a_new_read_failure_reason_is_logged_again(self):
        self.mock(self.plugin, "_start_when_ready")
        self.mock(M, "SETTINGS_RETRY_S", 0)
        self.mock(M.vibrance_settings, "load", side_effect=[PermissionError("denied")] * 2
                  + [OSError("I/O error")] * 2 + [""])
        warning = self.mock(M.vibrance_state.logger, "warning")
        await self.plugin._main()
        logged = [call.args[0] for call in warning.call_args_list if "could not load settings" in call.args[0]]
        self.assertEqual(len(logged), 2)
        self.assertIn("denied", logged[0])
        self.assertIn("I/O error", logged[1])

    async def test_a_load_failure_names_the_file_but_not_the_home_folder(self):
        self.mock(self.plugin, "_start_when_ready")
        self.mock(M, "SETTINGS_RETRY_S", 0)
        home = "/home/deck/homebrew/settings/DeckyVibranceHDR"
        real_load = M.vibrance_settings.load
        for case in ("unreadable", "cannot keep the damaged file"):
            with self.subTest(case=case):
                write_settings(None)
                if case == "unreadable":
                    write_settings({"sdr_enabled": True})
                    self.fail_reads(PermissionError(13, "Permission denied", f"{home}/settings.json"))
                else:
                    settings_file().write_bytes(b"{broken")
                    self.fail_aside(PermissionError(13, "Permission denied", f"{home}/settings.json.corrupt-1"))
                seen = []

                def load(path):
                    if M.state.public["settings_error"]:
                        seen.extend((M.state.public["settings_error"], M.state.public["reason"]))
                    return real_load(path)
                self.mock(M.vibrance_settings, "load", side_effect=load)
                warning = self.mock(M.vibrance_state.logger, "warning")
                await self.plugin._main()
                logged = [call.args[0] for call in warning.call_args_list
                          if "could not load settings" in call.args[0]]
                self.assertEqual(len(logged), 1)
                self.assertTrue(seen)
                for text in logged + seen:
                    self.assertIn("Permission denied: settings.json", text)
                    self.assertNotIn("/home/deck", text)
                self.assertTrue(M.state.public["settings_loaded"])

    async def test_a_lasting_save_failure_is_logged_once_not_every_retry(self):
        self.mock(M.Controller, "_save", side_effect=OSError("read-only file system"))
        error = self.mock(M.vibrance_state.logger, "error")
        for _ in range(3):
            M.state.public["settings_error"] = M.Controller._write_settings()
        self.assertEqual(error.call_count, 1)
        self.assertIn("read-only", M.state.public["settings_error"])

    async def test_replayed_mutations_during_failed_startup_preserve_unread_file(self):
        self.mock(M.Controller, "_save", side_effect=SAVE_SETTINGS)
        self.mock(self.plugin, "_start_when_ready")
        write_settings({"sdr_enabled": True, "hdr_enabled": True,
                        "sdr_saturation": 2.6, "hdr_saturation": 1.4, "custom": "keep"})
        before_file = settings_file().read_bytes()
        self.fail_reads(forever=lambda: PermissionError(13, "Permission denied", M.settings_path))
        startup = asyncio.create_task(self.plugin._main())
        try:
            await self.until(lambda: not M.state.settings_loaded and M.state.public["settings_error"])
            before = dict(M.state.public)
            for method, args in [("set_sdr_enabled", (True, 2.2)),
                                 ("set_hdr_enabled", (True, 1.3)),
                                 ("set_sdr_saturation", (2.3,)),
                                 ("set_hdr_saturation", (1.3,)), ("reset", ())]:
                with self.subTest(method=method):
                    with self.assertRaisesRegex(M.Refused, "settings"):
                        await getattr(self.plugin, method)(*args)
            self.assertEqual(M.state.public, before)
            self.assertFalse(M.state.settings_dirty)
            self.assertEqual(M.state.tasks, [])
            await self.plugin._unload()
            self.assertEqual(settings_file().read_bytes(), before_file)
        finally:
            M.state.unloading = True
            await asyncio.wait_for(startup, 2)

    async def test_save_boundaries_reject_unread_settings_even_if_dirty(self):
        write_settings({"custom": "keep"})
        before = settings_file().read_bytes()
        self.mock(M.state, "settings_loaded", False)
        self.mock(M.state, "settings_dirty", True)
        with self.assertRaisesRegex(RuntimeError, "settings"):
            SAVE_SETTINGS({"hdr_saturation": 1.3})
        self.plugin._schedule_save()
        M.Controller._persist()
        self.assertEqual(M.state.tasks, [])
        await self.plugin._unload()
        M.Controller._save.assert_not_called()
        self.assertEqual(settings_file().read_bytes(), before)

    async def test_loaded_settings_accept_changes_while_gamescope_disconnected(self):
        self.mock(M.state, "settings_loaded", True)
        self.mock(M.state, "disconnected", True)
        await self.plugin.set_hdr_saturation(1.3)
        self.assertEqual(M.state.public["hdr_saturation"], 1.3)
        self.assertTrue(M.state.settings_dirty)


class SettingsWrites(FileCase):
    """Saving never blocks controls, and the latest values always reach disk."""

    async def test_disk_full_does_not_prevent_disable_or_restore(self):
        await self.plugin.set_sdr_enabled(True)
        self.mock(M.Controller, "_save", side_effect=OSError("disk full"))
        await self.plugin.set_sdr_enabled(False)
        await self.drain()
        self.assertFalse(M.state.public["sdr_applied"])
        self.assertEqual(self.live_sdr, 0.8)
        self.assertIn("disk full", M.state.public["settings_error"])
        self.assertTrue(M.state.settings_dirty)

    async def test_disabled_hdr_slider_is_persisted(self):
        await self.plugin.set_hdr_saturation(1.3)
        await self.drain()
        M.Controller._save.assert_called_once()
        self.builder.assert_not_called()

    async def test_unload_flushes_a_pending_slider_value(self):
        self.mock(M, "SDR_COALESCE_S", 5)
        saved = []
        self.mock(M.Controller, "_save", side_effect=lambda values=None: saved.append(dict(M.state.public)))
        await self.plugin.set_hdr_saturation(1.4)
        await self.plugin._unload()
        self.assertEqual(saved[-1]["hdr_saturation"], 1.4)
        self.assertEqual(len(saved), 1)

    async def test_late_rpc_after_unload_cannot_change_flushed_settings(self):
        M.state.unloading = True
        before = dict(M.state.public)
        await self.plugin.set_sdr_saturation(1.4)
        await self.plugin.set_hdr_saturation(1.4)
        await self.plugin.set_sdr_enabled(True, 1.4)
        await self.plugin.set_hdr_enabled(False, 1.4)
        await self.plugin.reset()
        self.assertEqual(M.state.public, before)
        self.assertEqual(M.state.tasks, [])

    async def test_both_sliders_share_one_settings_write(self):
        await self.plugin.set_sdr_saturation(1.4)
        await self.plugin.set_hdr_saturation(1.3)
        await self.drain()
        M.Controller._save.assert_called_once()

    async def test_slow_save_does_not_block_disable_reset_or_other_slider(self):
        await self.plugin.set_sdr_enabled(True)
        await self.drain()
        entered, release = threading.Event(), threading.Event()
        def save(*args):
            entered.set()
            release.wait(10)
        self.mock(M.Controller, "_save", side_effect=save)
        await self.plugin.set_hdr_saturation(1.2)
        pending = []
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            pending = [asyncio.create_task(call) for call in (
                self.plugin.set_sdr_enabled(False), self.plugin.reset(), self.plugin.set_hdr_saturation(1.4))]
            done, _ = await asyncio.wait(pending, timeout=2)
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
        self.mock(M.Controller, "_save", side_effect=save)
        disable = asyncio.create_task(self.plugin.set_sdr_enabled(False))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertTrue(disable.done(), "disable RPC waits for filesystem persistence")
            self.assertFalse(M.state.public["sdr_applied"])
            self.assertEqual(self.live_sdr, 0.8)
        finally:
            release.set()
            await disable
            await self.drain()

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
        self.mock(M.Controller, "_save", side_effect=save)
        await self.plugin.set_hdr_saturation(1.2)
        try:
            self.assertTrue(await asyncio.to_thread(first.wait, 1))
            worker = M.state.workers["settings"]
            await self.plugin.set_hdr_saturation(1.4)
            await self.plugin.set_sdr_saturation(1.5)
            generation = M.state.save_generation
            for _ in range(30):
                await self.plugin.set_hdr_saturation(1.4)
                self.plugin._retry_settings_save()
            self.assertEqual(M.state.save_generation, generation)
            self.assertIs(M.state.workers["settings"], worker)
            release_first.set()
            self.assertTrue(await asyncio.to_thread(second.wait, 1))
            self.assertTrue(M.state.settings_dirty, "older completion acknowledged unsaved edits")
            self.assertEqual(snapshots[0]["hdr_saturation"], 1.2)
            self.assertEqual(snapshots[1]["hdr_saturation"], 1.4)
            self.assertEqual(snapshots[1]["sdr_saturation"], 1.5)
        finally:
            release_first.set()
            release_second.set()
            await self.drain()
        self.assertEqual(len(snapshots), 2)
        self.assertFalse(M.state.settings_dirty)

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
        self.mock(M.Controller, "_save", side_effect=save)
        await self.plugin.set_hdr_saturation(1.2)
        try:
            self.assertTrue(await asyncio.to_thread(first.wait, 1))
            await self.plugin.set_hdr_saturation(1.4)
        finally:
            release.set()
            await self.drain()
        self.assertTrue(M.state.settings_dirty)
        self.assertIn("latest snapshot failed", M.state.public["settings_error"])
        await self.plugin.set_hdr_saturation(1.4)
        await self.drain()
        self.assertEqual([value["hdr_saturation"] for value in snapshots], [1.2, 1.4, 1.4])
        self.assertFalse(M.state.settings_dirty)
        self.assertEqual(M.state.public["settings_error"], "")

    async def test_unload_serializes_behind_an_older_write_and_flushes_latest_values(self):
        entered, release = threading.Event(), threading.Event()
        write_settings({"unrelated": "keep"})
        M.vibrance_settings.load(M.settings_path)
        self.mock(M.os.path, "exists", side_effect=PATH_EXISTS)
        def save(values=None):
            with M.vibrance_settings.lock:
                if values is not None:
                    entered.set()
                    release.wait(2)
                SAVE_SETTINGS(values)
        self.mock(M.Controller, "_save", side_effect=save)
        await self.plugin.set_hdr_saturation(1.2)
        unload = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            await self.plugin.set_hdr_saturation(1.4)
            unload = asyncio.create_task(self.plugin._unload())
            await asyncio.sleep(0)
            self.assertTrue(M.state.unloading)
            late = await self.plugin.set_hdr_saturation(1.5)
            self.assertEqual(late["hdr_saturation"], 1.4)
        finally:
            release.set()
            if unload is not None:
                await unload
            else:
                await self.drain()
        saved = json.loads(settings_file().read_text(encoding="utf-8"))
        self.assertEqual(saved["hdr_saturation"], 1.4)
        self.assertEqual(saved["unrelated"], "keep")
        self.assertFalse(M.state.settings_dirty)


class StartupRecovery(FileCase):
    async def test_recovered_read_clears_its_error_and_saves_migrated_values(self):
        self.mock(self.plugin, "_start_when_ready")
        write_settings({"sdr_enabled": True, "sdr_saturation": 9.0})
        self.fail_reads(OSError(5, "Input/output error", M.settings_path))
        await self.plugin._main()
        self.assertEqual(M.state.public["settings_error"], "")  # before any save runs
        await asyncio.gather(*M.state.tasks)
        saved = M.Controller._save.call_args.args[0]
        self.assertEqual(saved["sdr_saturation"], M.SDR_MAX_WIDE)


class StaleSnapshotAfterFinalFlush(FileCase):
    """B1: a debounced snapshot whose executor job reaches the writer lock only
    after unload's final flush overwrites the newer values on disk."""

    async def test_an_older_snapshot_landing_after_unload_does_not_win(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        write_settings({})
        M.vibrance_settings.load(M.settings_path)
        self.mock(M.os.path, "exists", side_effect=PATH_EXISTS)

        def save(values=None):
            if values is not None:      # the debounced job, preempted before the writer lock
                started.set()
                release.wait(2)
            try:
                SAVE_SETTINGS(values)
            finally:
                if values is not None:
                    finished.set()
        self.mock(M.Controller, "_save", side_effect=save)

        await self.plugin.set_hdr_saturation(1.2)
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        await self.plugin.set_hdr_saturation(1.4)
        await self.plugin._unload()     # the final flush writes 1.4
        release.set()                   # the orphaned older job now gets the lock
        self.assertTrue(await asyncio.to_thread(finished.wait, 2))
        saved = json.loads(settings_file().read_text(encoding="utf-8"))
        self.assertEqual(saved["hdr_saturation"], 1.4)


class SaveFailureLogging(PluginTest):
    """A settings folder that cannot take a new file (read-only remount) fails
    on a temporary file with a fresh random name every time. The panel tick
    retries every 5 s, so a message carrying that name would never match the
    "log once" comparison: a line every 5 s and a reason that changes under
    the user.
    """

    def setUp(self):
        M.state.settings_loaded = True
        write_settings(None)
        self.addCleanup(write_settings, None)

    def retries(self, *errors):
        folder = os.path.dirname(M.settings_path)
        def refuse(path, *args, **kwargs):
            error = errors[min(len(messages), len(errors) - 1)]
            raise error(f"{folder}/.settings.json.{secrets.token_hex(6)}.tmp")
        messages = []
        with patch.object(dk_fs, "write_atomic", side_effect=refuse), \
                patch.object(M.vibrance_state.logger, "error") as error:
            for _ in range(3):                    # three panel-tick retries
                message = M.Controller._write_settings()
                M.state.public["settings_error"] = message
                messages.append(message)
        for message in messages:
            self.assertNotIn(folder, message)
        return messages, error

    def test_a_lasting_save_failure_with_a_random_temp_name_is_logged_once(self):
        messages, error = self.retries(lambda name: PermissionError(13, "Permission denied", name))
        self.assertEqual(error.call_count, 1, [c.args[0] for c in error.call_args_list])
        self.assertEqual(set(messages), {"could not save settings: Permission denied"})
        self.assertFalse(settings_file().exists())

    def test_a_new_save_failure_reason_is_logged_again(self):
        messages, error = self.retries(lambda name: PermissionError(13, "Permission denied", name),
                                       lambda name: OSError(28, "No space left on device", name))
        self.assertEqual(error.call_count, 2, [c.args[0] for c in error.call_args_list])
        self.assertEqual(messages[-1], "could not save settings: No space left on device")
