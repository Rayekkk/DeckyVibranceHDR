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
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _fixtures import PluginCase, SAVE_SETTINGS  # installs the loader stand-ins
import main as M  # noqa: E402


class AtomicSettings(unittest.TestCase):
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


class SettingsFileContract(unittest.TestCase):
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
            with self.assertRaises(json.JSONDecodeError):
                M._read_settings()
            self.assertEqual(path.read_text(), "{broken settings")
            self.assertEqual(store.settings, {})

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
