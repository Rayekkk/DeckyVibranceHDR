"""Startup disk-read failures must not replace saved settings with defaults."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import _harness
_harness.install()
import main as M
import test_recovery as R

SAVE = M.Plugin._save

class ReadFailure(unittest.IsolatedAsyncioTestCase):
    setUp = R.Recovery.setUp
    asyncTearDown = R.Recovery.asyncTearDown
    mock = R.Recovery.mock
    write_float = R.Recovery.write_float
    write_string = R.Recovery.write_string

    async def test_failed_settings_read_then_unload_preserves_existing_file(self):
        self.mock(M.Plugin, "_save", side_effect=SAVE)
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

    async def test_only_fresh_install_accepts_empty_manager_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text("")
            self.mock(M, "settings", SimpleNamespace(path=str(path), settings={}))
            self.mock(M, "_settings_new", False)
            with self.assertRaises(json.JSONDecodeError):
                M._read_settings()
            M._settings_new = True
            M._read_settings()
            self.assertEqual(M.settings.settings, {})
            self.assertFalse(M._settings_new)
            with self.assertRaises(json.JSONDecodeError):
                M._read_settings()

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
        self.mock(M.Plugin, "_save", side_effect=SAVE)
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
            SAVE({"hdr_saturation": 1.3})
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
