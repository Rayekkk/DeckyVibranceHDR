"""Startup/unload races identified in the seventh local audit."""
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

SAVE_SETTINGS = M.Plugin._save


class StartupShutdown(unittest.IsolatedAsyncioTestCase):
    setUp = R.Recovery.setUp
    asyncTearDown = R.Recovery.asyncTearDown
    mock = R.Recovery.mock
    write_float = R.Recovery.write_float
    write_string = R.Recovery.write_string

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
