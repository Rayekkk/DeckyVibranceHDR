# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Reading and writing the settings file."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _fixtures import PATH_EXISTS, PluginCase, PluginTest, SAVE_SETTINGS  # installs the loader stand-ins first
import main as M


class AtomicSettings(PluginTest):
    def setUp(self):
        loaded = patch.object(M.Plugin, "_settings_loaded", True)
        loaded.start()
        self.addCleanup(loaded.stop)

    def test_nonroot_save_does_not_require_chown_privileges(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.json")
            with open(path, "w") as f:
                f.write("{}")
            manager = SimpleNamespace(path=path, settings={})
            with patch.object(M, "settings", manager), patch.object(M.os, "geteuid", return_value=1000, create=True), \
                    patch.object(M.os, "chown", side_effect=PermissionError("not root"), create=True) as chown:
                M.Plugin._save()
                chown.assert_not_called()
                with open(path) as f:
                    self.assertIn("hdr_saturation", json.load(f))
    def test_nonfinite_xproperty_cannot_become_a_saved_baseline(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            self.assertIsNone(M._cardinal_to_float(M._float_to_cardinal(value)))

    def test_one_snapshot_preserves_other_keys_and_keeps_old_file_on_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.json")
            old = {"unrelated": 17, **M.DEFAULT_SETTINGS}
            with open(path, "w") as f:
                json.dump(old, f)
            manager = SimpleNamespace(path=path, settings=old.copy())
            with patch.object(M, "settings", manager), patch.object(M.Plugin, "_state", {**old, "hdr_saturation": 1.4}):
                with patch.object(M.os, "replace", side_effect=OSError("full")):
                    with self.assertRaises(OSError):
                        M.Plugin._save()
                with open(path) as f:
                    self.assertEqual(json.load(f), old)
                self.assertEqual(manager.settings, old)
                self.assertEqual(os.listdir(folder), ["settings.json"])
                M.Plugin._save()
                with open(path) as f:
                    self.assertEqual(json.load(f), {**old, "hdr_saturation": 1.4})

    def test_corrupt_settings_fields_use_defaults_independently(self):
        for bad in (None, "wrong", [], {}, float("nan"), float("inf"), True):
            with patch.object(M.settings, "getSetting", return_value=bad):
                self.assertEqual(M._setting("hdr_saturation", 1.0), 1.0)
        for bad in ("false", 0, 1, None, []):
            with patch.object(M.settings, "getSetting", return_value=bad):
                self.assertIs(M._setting("hdr_enabled", False), False)


class SettingsFileContract(PluginTest):
    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX named pipes")
    def test_constructor_cannot_read_fifo_before_startup_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            os.mkfifo(Path(directory) / "settings.json")
            code = "\n".join([
                "import sys, os, json",
                "sys.path.insert(0, sys.argv[2])",
                "import _harness; _harness.install(sys.argv[1])",
                "import settings",
                "class EagerManager:",
                " def __init__(self, name, settings_directory):",
                "  self.path = os.path.join(settings_directory, name + '.json')",
                "  self.settings = {}; self.read()",
                " def read(self):",
                "  with open(self.path) as source: self.settings = json.load(source)",
                "settings.SettingsManager = EagerManager",
                "import main as M",
                "assert M.settings.settings == {}",
                "try: M._read_settings()",
                "except ValueError as e: assert 'regular file' in str(e)",
                "else: raise AssertionError('FIFO accepted')",
            ])
            subprocess.run([sys.executable, "-W", "error", "-c", code,
                            directory, str(Path(__file__).parent.resolve())],
                           cwd=Path(M.__file__).parent, check=True,
                           capture_output=True, timeout=3)

    def test_regular_file_retains_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            values = {"sdr_enabled": True, "custom": {"label": "zażółć"}}
            path.write_text(json.dumps(values), encoding="utf-8")
            store = SimpleNamespace(path=str(path), settings={})
            with patch.object(M, "settings", store), patch.object(M, "_settings_new", False):
                M._read_settings()
            self.assertEqual(store.settings, values)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), values)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX named pipes")
    def test_fifo_rejected_promptly_without_fd_leak_or_file_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            os.mkfifo(path)
            code = "\n".join([
                "import os, sys",
                "sys.path.insert(0, sys.argv[2])",
                "import _harness; _harness.install()",
                "import main as M",
                "from types import SimpleNamespace",
                "M.settings = SimpleNamespace(path=sys.argv[1], settings={'custom': 'keep'})",
                "M._settings_new = False",
                "before = len(os.listdir('/proc/self/fd'))",
                "for _ in range(100):",
                " try: M._read_settings()",
                " except ValueError as e: assert 'regular file' in str(e)",
                " else: raise AssertionError('FIFO accepted')",
                "assert len(os.listdir('/proc/self/fd')) == before",
                "assert M.settings.settings == {'custom': 'keep'}",
            ])
            subprocess.run([sys.executable, "-W", "error", "-c", code,
                            str(path), str(Path(__file__).parent.resolve())],
                           cwd=Path(M.__file__).parent, check=True,
                           capture_output=True, timeout=3)
            self.assertTrue(path.exists())

    @unittest.skipUnless(os.name == "posix", "requires POSIX symlinks")
    def test_regular_symlink_remains_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "real.json"
            real.write_text('{"hdr_saturation": 1.4}', encoding="utf-8")
            link = Path(directory) / "settings.json"
            link.symlink_to(real)
            store = SimpleNamespace(path=str(link), settings={})
            with patch.object(M, "settings", store), patch.object(M, "_settings_new", False):
                M._read_settings()
            self.assertEqual(store.settings, {"hdr_saturation": 1.4})


class ReadFailure(PluginCase):

    async def test_failed_settings_read_then_unload_preserves_existing_file(self):
        self.mock(M.Plugin, "_save", side_effect=SAVE_SETTINGS)
        self.mock(M.updater, "ssl_context", return_value=None)
        self.mock(self.plugin, "_start_when_ready")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            saved = {"sdr_enabled": True, "hdr_enabled": True,
                     "sdr_saturation": 2.6, "hdr_saturation": 1.4, "custom": "keep"}
            path.write_text(json.dumps(saved))
            failed = asyncio.Event()
            self.mock(M, "settings", SimpleNamespace(path=str(path), settings={}, read=None))
            loop = asyncio.get_running_loop()
            original_open = M.os.open
            def open_file(name, *args, **kwargs):
                if str(name) == str(path):
                    loop.call_soon_threadsafe(failed.set)
                    raise PermissionError("temporary disk read failure")
                return original_open(name, *args, **kwargs)
            self.mock(M.os, "open", side_effect=open_file)
            startup = asyncio.create_task(self.plugin._main())
            try:
                await failed.wait()
                await asyncio.sleep(.03)
                self.assertFalse(M.Plugin._settings_loaded)
                self.assertIn("settings", M.Plugin._state["reason"])
                await self.plugin._unload()
                self.assertEqual(json.loads(path.read_text()), saved)
            finally:
                M.Plugin._unloading = True
                await asyncio.wait_for(startup, 2)

    async def test_transient_settings_read_failure_recovers_saved_values(self):
        self.mock(M.updater, "ssl_context", return_value=None)
        recovery = self.mock(self.plugin, "_start_when_ready")
        saved = {"sdr_enabled": True, "hdr_enabled": True,
                 "sdr_saturation": 2.7, "hdr_saturation": 1.37}
        store = SimpleNamespace(settings={}, path=None)
        store.getSetting = lambda key, default: store.settings.get(key, default)
        calls = 0
        def read():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary disk failure")
            store.settings = saved
        store.read = read
        self.mock(M, "settings", store)
        await self.plugin._main()
        await asyncio.gather(*M.Plugin._tasks)
        self.assertEqual(calls, 2)
        self.assertTrue(M.Plugin._settings_loaded)
        self.assertTrue(M.Plugin._state["settings_loaded"])
        for key, value in saved.items():
            self.assertEqual(M.Plugin._state[key], value)
        self.assertEqual(M.Plugin._state["settings_error"], "")
        recovery.assert_awaited_once()

    async def test_manager_that_swallows_errors_cannot_overwrite_corrupt_file(self):
        self.mock(M, "_settings_new", False)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text("{broken settings")
            store = SimpleNamespace(path=str(path), settings={}, read=lambda: None)
            self.mock(M, "settings", store)
            with self.assertRaises(M.SettingsUnreadable):
                M._read_settings()
            self.assertEqual(path.read_text(), "{broken settings")
            # Nesting deep enough to overflow the parser is a RecursionError,
            # not a ValueError, and must still count as unparseable.
            path.write_text("[" * 100000)
            with self.assertRaises(M.SettingsUnreadable):
                M._read_settings()
            self.assertEqual(store.settings, {})

    async def test_unparseable_settings_are_set_aside_and_startup_continues(self):
        """Bad content never parses on a retry, so waiting would leave the
        plugin dead with no way out from the menu. The bytes are kept."""
        self.mock(M.updater, "ssl_context", return_value=None)
        recovery = self.mock(self.plugin, "_start_when_ready")
        self.mock(M, "_settings_new", False)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            for broken in (b"{broken settings", b"\xff\xfe not utf-8", b"[1, 2]"):
                with self.subTest(content=broken):
                    for leftover in Path(folder).iterdir():
                        leftover.unlink()
                    path.write_bytes(broken)
                    M._settings_new = False
                    store = SimpleNamespace(path=str(path), settings={"sdr_enabled": True})
                    store.getSetting = lambda key, default: store.settings.get(key, default)
                    self.mock(M, "settings", store)
                    await self.plugin._main()
                    self.assertTrue(M.Plugin._state["settings_loaded"])
                    self.assertFalse(M.Plugin._state["sdr_enabled"])
                    self.assertEqual(M.Plugin._state["settings_error"], "")
                    self.assertFalse(path.exists())
                    (aside,) = Path(folder).glob("settings.json.corrupt-*")
                    self.assertEqual(aside.read_bytes(), broken)
                    self.assertIn(aside.name, M.Plugin._state["settings_notice"])  # the menu says so
        self.assertEqual(recovery.call_count, 3)

    async def test_a_file_that_cannot_be_set_aside_is_kept_and_retried(self):
        self.mock(M.updater, "ssl_context", return_value=None)
        self.mock(self.plugin, "_start_when_ready")
        self.mock(M, "_settings_new", False)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text("{broken")
            self.mock(M, "settings", SimpleNamespace(path=str(path), settings={}))
            refused = asyncio.Event()
            loop = asyncio.get_running_loop()
            def refuse():
                loop.call_soon_threadsafe(refused.set)
                raise PermissionError("read-only settings folder")
            self.mock(M, "_set_aside_settings", side_effect=refuse)
            startup = asyncio.create_task(self.plugin._main())
            try:
                await asyncio.wait_for(refused.wait(), 5)
                for _ in range(500):  # the error is published after the thread returns
                    if "could not set the file aside" in M.Plugin._state["settings_error"]:
                        break
                    await asyncio.sleep(0.01)
                self.assertFalse(M.Plugin._state["settings_loaded"])
                self.assertIn("could not set the file aside", M.Plugin._state["settings_error"])
                self.assertEqual(path.read_text(), "{broken")
            finally:
                M.Plugin._unloading = True
                startup.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await startup

    async def test_a_lasting_read_failure_is_logged_once_not_every_retry(self):
        self.mock(M.updater, "ssl_context", return_value=None)
        self.mock(self.plugin, "_start_when_ready")
        self.mock(M, "_read_settings", side_effect=[PermissionError("denied")] * 4 + [None])
        warning = self.mock(M.decky.logger, "warning")
        await self.plugin._main()
        logged = [call for call in warning.call_args_list if "could not load settings" in call.args[0]]
        self.assertEqual(len(logged), 1)

    async def test_a_new_read_failure_reason_is_logged_again(self):
        self.mock(M.updater, "ssl_context", return_value=None)
        self.mock(self.plugin, "_start_when_ready")
        self.mock(M, "SETTINGS_RETRY_S", 0)
        self.mock(M, "_read_settings", side_effect=[PermissionError("denied")] * 2
                  + [OSError("I/O error")] * 2 + [None])
        warning = self.mock(M.decky.logger, "warning")
        await self.plugin._main()
        logged = [call.args[0] for call in warning.call_args_list if "could not load settings" in call.args[0]]
        self.assertEqual(len(logged), 2)
        self.assertIn("denied", logged[0])
        self.assertIn("I/O error", logged[1])

    async def test_a_lasting_save_failure_is_logged_once_not_every_retry(self):
        self.mock(M.Plugin, "_save", side_effect=OSError("read-only file system"))
        error = self.mock(M.decky.logger, "error")
        for _ in range(3):
            M.Plugin._state["settings_error"] = M.Plugin._write_settings()
        self.assertEqual(error.call_count, 1)
        self.assertIn("read-only", M.Plugin._state["settings_error"])

    async def test_empty_file_loads_as_defaults_but_missing_file_is_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            self.mock(M, "settings", SimpleNamespace(path=str(path), settings={"stale": 1}))
            self.mock(M, "_settings_new", False)
            # A previously existing file that is now missing is not a fresh install.
            with self.assertRaises(FileNotFoundError):
                M._read_settings()
            # An empty file - first-install manager file, or 1.0.0's truncating
            # write cut short - has nothing to protect and must not block startup.
            for empty in ("", "  \n"):
                path.write_text(empty)
                M._read_settings()
                self.assertEqual(M.settings.settings, {})
                self.assertEqual(path.read_text(), empty)

    async def test_invalid_json_shape_is_rejected_and_unknown_fields_survive(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            self.mock(M, "settings", SimpleNamespace(path=str(path), settings={}))
            self.mock(M, "_settings_new", False)
            for invalid in ("null", "[]", "42"):
                path.write_text(invalid)
                with self.assertRaises(ValueError):
                    M._read_settings()
            saved = {"sdr_saturation": 2.6, "custom": "preserve"}
            path.write_text(json.dumps(saved))
            M._read_settings()
            self.assertEqual(M.settings.settings, saved)

    async def test_replayed_mutations_during_failed_startup_preserve_unread_file(self):
        self.mock(M.Plugin, "_save", side_effect=SAVE_SETTINGS)
        self.mock(M.updater, "ssl_context", return_value=None)
        self.mock(self.plugin, "_start_when_ready")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            saved = {"sdr_enabled": True, "hdr_enabled": True,
                     "sdr_saturation": 2.6, "hdr_saturation": 1.4, "custom": "keep"}
            path.write_text(json.dumps(saved))
            self.mock(M, "settings", SimpleNamespace(path=str(path), settings={}))
            self.mock(M, "_read_settings", side_effect=PermissionError("unavailable"))
            startup = asyncio.create_task(self.plugin._main())
            try:
                for _ in range(100):
                    if not M.Plugin._settings_loaded and M.Plugin._state.get("settings_error"):
                        break
                    await asyncio.sleep(.01)
                before = dict(M.Plugin._state)
                for method, args in [("set_sdr_enabled", (True, 2.2)),
                                     ("set_hdr_enabled", (True, 1.3)),
                                     ("set_sdr_saturation", (2.3,)),
                                     ("set_hdr_saturation", (1.3,)), ("reset", ())]:
                    with self.subTest(method=method):
                        with self.assertRaisesRegex(RuntimeError, "settings"):
                            await getattr(self.plugin, method)(*args)
                self.assertEqual(M.Plugin._state, before)
                self.assertFalse(M.Plugin._settings_dirty)
                self.assertEqual(M.Plugin._tasks, [])
                await self.plugin._unload()
                self.assertEqual(json.loads(path.read_text()), saved)
            finally:
                M.Plugin._unloading = True
                await asyncio.wait_for(startup, 2)

    async def test_save_boundaries_reject_unread_settings_even_if_dirty(self):
        self.mock(M.Plugin, "_settings_loaded", False)
        self.mock(M.Plugin, "_settings_dirty", True)
        store = self.mock(M, "settings", SimpleNamespace(settings={}))
        with self.assertRaisesRegex(RuntimeError, "settings"):
            SAVE_SETTINGS({"hdr_saturation": 1.3})
        self.plugin._schedule_save()
        M.Plugin._persist()
        self.assertEqual(store.settings, {})
        self.assertEqual(M.Plugin._tasks, [])
        await self.plugin._unload()
        M.Plugin._save.assert_not_called()

    async def test_loaded_settings_accept_changes_while_gamescope_disconnected(self):
        self.mock(M.Plugin, "_settings_loaded", True)
        self.mock(M.Plugin, "_disconnected", True)
        await self.plugin.set_hdr_saturation(1.3)
        self.assertEqual(M.Plugin._state["hdr_saturation"], 1.3)
        self.assertTrue(M.Plugin._settings_dirty)


class SettingsWrites(PluginCase):
    """Saving never blocks controls, and the latest values always reach disk."""

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
        self.mock(M.Plugin, "_save", side_effect=lambda values=None: saved.append(dict(M.Plugin._state)))
        await self.plugin.set_hdr_saturation(1.4)
        await self.plugin._unload()
        self.assertEqual(saved[-1]["hdr_saturation"], 1.4)
        self.assertEqual(len(saved), 1)

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

    async def test_slow_save_does_not_block_disable_reset_or_other_slider(self):
        await self.plugin.set_sdr_enabled(True)
        await self.drain()
        entered, release = threading.Event(), threading.Event()
        def save(*args):
            entered.set()
            release.wait(10)
        self.mock(M.Plugin, "_save", side_effect=save)
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


class StartupRecovery(PluginCase):
    async def test_recovered_read_clears_its_error_and_saves_migrated_values(self):
        self.mock(M.updater, "ssl_context", return_value=None)
        self.mock(self.plugin, "_start_when_ready")
        store = SimpleNamespace(settings={}, path=None)
        store.getSetting = lambda key, default: store.settings.get(key, default)
        calls = 0
        def read():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary disk failure")
            store.settings = {"sdr_enabled": True, "sdr_saturation": 9.0}
        store.read = read
        self.mock(M, "settings", store)
        await self.plugin._main()
        self.assertEqual(M.Plugin._state["settings_error"], "")  # before any save runs
        await asyncio.gather(*M.Plugin._tasks)
        saved = M.Plugin._save.call_args.args[0]
        self.assertEqual(saved["sdr_saturation"], M.SDR_MAX_WIDE)
