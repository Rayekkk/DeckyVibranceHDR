"""Fifth review: disk latency, monitor queries and completed LUT reuse."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from collections import OrderedDict
from unittest.mock import patch

import _harness
_harness.install()
import main as M
import vibrance_lut as V
import test_recovery as R
import test_logic as L

READ_STRING_ATOM = M._read_string_atom
SAVE_SETTINGS = M.Plugin._save
PATH_EXISTS = M.os.path.exists
WRITE_LOOK = M._write_look


class Latency(unittest.IsolatedAsyncioTestCase):
    setUp = R.Recovery.setUp
    asyncTearDown = R.Recovery.asyncTearDown
    mock = R.Recovery.mock
    drain = R.Recovery.drain
    write_float = R.Recovery.write_float
    write_string = R.Recovery.write_string

    async def test_slow_save_does_not_block_disable_reset_or_other_slider(self):
        await self.plugin.set_sdr_enabled(True)
        await self.drain()
        entered, release = threading.Event(), threading.Event()
        def save(*args):
            entered.set()
            release.wait(2)
        self.mock(M.Plugin, "_save", side_effect=save)
        await self.plugin.set_hdr_saturation(1.2)
        pending = []
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            pending = [asyncio.create_task(call) for call in (
                self.plugin.set_sdr_enabled(False), self.plugin.reset(), self.plugin.set_hdr_saturation(1.4))]
            done, _ = await asyncio.wait(pending, timeout=0.2)
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
        self.mock(M.subprocess, "run", side_effect=run)
        self.mock(M, "_read_string_atom", side_effect=READ_STRING_ATOM)
        self.mock(M, "_read_edid", return_value=L.PanelDetection._edid(0.640, 0.330))
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

    async def test_monitor_external_or_missing_metadata_never_adds_atom_queries(self):
        read = self.mock(M, "_read_edid")
        run = self.mock(M.subprocess, "run")
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
        run = self.mock(M.subprocess, "run", return_value=SimpleNamespace(stdout=
            f'{M.ATOM_IS_EXTERNAL}(CARDINAL) = 0\n'
            f'{M.ATOM_EDID_PATH}(UTF8_STRING) = "/tmp/a\\\"b\\\\c.bin"'))
        self.mock(M, "_edid_file_identity", return_value=(1, 2, 128, 3, 4))
        read = self.mock(M, "_read_edid", return_value=L.PanelDetection._edid(0.640, 0.330))
        self.mock(M.Plugin, "_panel_cache_key", None)
        self.mock(M.Plugin, "_panel_cache_complete", False)
        _, probe = M.Plugin._monitor_probe()
        M.Plugin._configure_for_panel(probe)
        _, cached = M.Plugin._monitor_probe()
        self.assertEqual(run.call_count, 2)
        read.assert_called_once_with('/tmp/a"b\\c.bin')
        self.assertIsNone(cached[2])

    async def test_failed_monitor_root_read_does_not_read_an_edid(self):
        self.mock(M.subprocess, "run", side_effect=OSError("X unavailable"))
        read = self.mock(M, "_read_edid")
        self.assertEqual(M.Plugin._monitor_probe(), (0, None))
        read.assert_not_called()

    async def check_slot_reuse_through_rpc(self, component, values, atom):
        with tempfile.TemporaryDirectory() as folder:
            self.mock(M, "_runtime_dir", return_value=folder)
            self.mock(M, "LUT_SIZE", 5)
            self.mock(M, "_look_file_cache", OrderedDict())
            self.mock(M, "_write_look", side_effect=WRITE_LOOK)
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
        await self.check_slot_reuse_through_rpc("hdr", (1.1, 1.3), M.ATOM_LOOK_PQ)
        self.assertEqual(M.Plugin._state["hdr_applied_saturation"], 1.1)
        self.assertFalse(M.Plugin._state["generating"])

    async def test_sdr_rpc_reuses_previous_inactive_slot_and_applies_it(self):
        self.mock(M, "SDR_NEUTRAL_ATOM", 0.0)
        self.mock(M, "SDR_ATOM_SPAN", 1.0)
        await self.check_slot_reuse_through_rpc("sdr", (2.2, 2.6), M.ATOM_LOOK_G22)
        self.assertAlmostEqual(M.Plugin._state["sdr_look_saturation"], 1.1)
        self.assertFalse(M.Plugin._state["sdr_generating"])


class CompletedSlots(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, name, value in ((M, "_runtime_dir", lambda: folder.name),
                                    (M, "LUT_SIZE", 5),
                                    (M, "_lut_writes_disabled", False),
                                    (M, "_look_file_cache", OrderedDict())):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_returning_to_a_previous_saturation_reuses_its_unchanged_file(self):
        with patch.object(V, "generate", wraps=V.generate) as generate:
            for encoding in ("pq", "g22"):
                with self.subTest(encoding=encoding):
                    generate.reset_mock()
                    prefix = "hdr-look" if encoding == "pq" else "sdr-look"
                    first = M._write_look(1.1, 0, encoding, prefix)
                    original = Path(first).read_bytes()
                    M._write_look(1.3, 1, encoding, prefix)
                    returned = M._write_look(1.1, 0, encoding, prefix)
                    self.assertEqual(Path(returned).read_bytes(), original)
                    self.assertEqual(generate.call_count, 2, "existing requested LUT was rebuilt")

    def test_encoding_white_point_and_size_invalidate_a_completed_slot(self):
        with patch.object(V, "generate", wraps=V.generate) as generate:
            M._write_look(1.2, 0, "g22")
            M._write_look(1.2, 0, "g22", source_white=(0.30, 0.32))
            M._write_look(1.2, 0, "pq", source_white=(0.30, 0.32))
            with patch.object(M, "LUT_SIZE", 6):
                path = M._write_look(1.2, 0, "pq", source_white=(0.30, 0.32))
            self.assertIn("LUT_3D_SIZE 6", Path(path).read_text())
            self.assertEqual(generate.call_count, 4)

    def test_replaced_or_deleted_files_are_regenerated(self):
        with patch.object(V, "generate", wraps=V.generate) as generate:
            path = Path(M._write_look(1.2, 0))
            original = path.read_bytes()
            replacement = path.with_suffix(".replacement")
            replacement.write_text("damaged")
            replacement.replace(path)
            self.assertEqual(M._write_look(1.2, 0), str(path))
            self.assertEqual(path.read_bytes(), original)
            path.unlink()
            self.assertEqual(M._write_look(1.2, 0), str(path))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(generate.call_count, 3)

    def test_cached_hit_honors_cancellation_and_unload_without_writing(self):
        path = Path(M._write_look(1.2, 0))
        original = M._file_identity(path)
        with patch.object(V, "generate", wraps=V.generate) as generate, \
                patch.object(M.os, "replace") as replace:
            self.assertEqual(M._write_look(1.2, 0, cancelled=lambda: True), "")
            with patch.object(M, "_lut_writes_disabled", True):
                self.assertEqual(M._write_look(1.2, 0), "")
            checks = iter((False, True))
            self.assertEqual(M._write_look(1.2, 0, cancelled=lambda: next(checks)), "")
            self.assertEqual(M._write_look(1.2, 0), str(path))
            self.assertEqual(M._file_identity(path), original)
            generate.assert_not_called()
            replace.assert_not_called()

    def test_metadata_cache_is_bounded_and_eviction_rebuilds(self):
        with patch.object(V, "generate", wraps=V.generate) as generate:
            for i in range(6):
                M._write_look(1.2, 0, prefix=f"slot-{i}")
            self.assertEqual(len(M._look_file_cache), 4)
            M._write_look(1.2, 0, prefix="slot-5")
            self.assertEqual(generate.call_count, 6)
            M._write_look(1.2, 0, prefix="slot-0")
            self.assertEqual(generate.call_count, 7)
            self.assertEqual(len(M._look_file_cache), 4)

    def test_cache_does_not_bypass_saturation_or_size_type_validation(self):
        path = Path(M._write_look(1.0, 0))
        original = path.read_bytes()
        self.assertEqual(M._write_look(True, 0), "")
        with patch.object(M, "LUT_SIZE", 5.0):
            self.assertEqual(M._write_look(1.0, 0), "")
        self.assertEqual(path.read_bytes(), original)
