"""Second full audit: display changes, slow HTTP and release validation."""
import asyncio
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import http.client
import subprocess
import sys
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import _harness
_harness.install()
import main as M
import lego_updater as U
import test_recovery as R
import test_logic as L


class PanelCache(unittest.TestCase):
    def test_missing_edid_path_does_not_spawn_a_duplicate_xprop_read(self):
        with patch.object(M, "_display_is_external", return_value=False), \
                patch.object(M, "_read_string_atom", return_value=None) as read:
            M.Plugin._probe_panel()
        read.assert_called_once_with(M.ATOM_EDID_PATH)

    def test_atomic_edid_replacement_at_the_same_path_changes_panel_mapping(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "panel.bin"
            path.write_bytes(L.PanelDetection._edid(0.6836, 0.3154))
            with patch.object(M.Plugin, "_state", dict(M.Plugin._state)), \
                    patch.object(M.Plugin, "_panel_cache_key", None), \
                    patch.object(M.Plugin, "_panel_cache_complete", False), \
                    patch.object(M, "SDR_NEUTRAL_ATOM", 1.0), \
                    patch.object(M, "SDR_ATOM_SPAN", 2.0), \
                    patch.object(M, "_display_is_external", return_value=False), \
                    patch.object(M, "_read_string_atom", return_value=str(path)), \
                    patch.object(M, "_read_edid", wraps=M._read_edid) as read:
                M.Plugin._configure_for_panel()
                M.Plugin._configure_for_panel()
                self.assertEqual(read.call_count, 1, "unchanged file should stay cached")
                replacement = Path(folder) / "new.bin"
                replacement.write_bytes(L.PanelDetection._edid(0.640, 0.330))
                os.replace(replacement, path)
                self.assertTrue(M.Plugin._configure_for_panel())
                self.assertEqual(M.SDR_NEUTRAL_ATOM, 0)
                self.assertEqual(read.call_count, 2)


class DisplayTransitions(unittest.IsolatedAsyncioTestCase):
    setUp = R.Recovery.setUp
    asyncTearDown = R.Recovery.asyncTearDown
    mock = R.Recovery.mock
    drain = R.Recovery.drain
    write_float = R.Recovery.write_float
    write_string = R.Recovery.write_string

    async def test_slow_panel_probe_does_not_block_slider_mutations(self):
        started, release = threading.Event(), threading.Event()
        def external():
            started.set()
            release.wait(2)
            return False
        self.mock(M, "_display_is_external", side_effect=external)
        panel = asyncio.create_task(self.plugin._refresh_panel())
        change = None
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            change = asyncio.create_task(self.plugin.set_hdr_saturation(1.2))
            done, _ = await asyncio.wait([change], timeout=0.2)
            self.assertIn(change, done, "panel metadata IO monopolized the mutation lock")
        finally:
            release.set()
            await panel
            if change is not None:
                await change
            await self.drain()

    async def test_internal_metadata_refresh_preserves_the_sdr_restore_value(self):
        await self.plugin.set_sdr_enabled(True)
        self.mock(M.Plugin, "_configure_for_panel", return_value=True)
        await self.plugin._refresh_panel()
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)

    async def test_return_to_internal_captures_its_current_sdr_baseline(self):
        M.Plugin._state.update(sdr_enabled=True, external_display=True)
        self.live_sdr = 0.6
        self.mock(M.Plugin, "_configure_for_panel", side_effect=lambda *args:
                  M.Plugin._state.update(external_display=False) or True)
        await self.plugin._refresh_panel()
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.6)

    async def test_first_lut_build_does_not_replace_a_live_stale_slot(self):
        self.mock(M, "_runtime_dir", return_value="/tmp")
        for encoding, atom, slot_key, enabled, saturation, schedule in (
                ("pq", M.ATOM_LOOK_PQ, "_look_slot", "hdr_enabled", "hdr_saturation", self.plugin._schedule_hdr),
                ("g22", M.ATOM_LOOK_G22, "_sdr_look_slot", "sdr_enabled", "sdr_saturation", self.plugin._schedule_sdr_look)):
            M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 0, 1
            prefix = "hdr-look" if encoding == "pq" else "sdr-look"
            self.strings[atom] = f"/tmp/{prefix}-1.cube"
            setattr(M.Plugin, slot_key, 0)
            M.Plugin._state.update({enabled: True, saturation: 1.3 if encoding == "pq" else 2.6})
            self.builder.reset_mock()
            schedule()
            await self.drain()
            self.assertEqual(self.builder.call_args.args[1], 0, encoding)

    async def test_foreign_hdr_look_changed_during_build_is_restored(self):
        self.strings[M.ATOM_LOOK_PQ] = "/tmp/foreign-old.cube"
        def build(*args):
            self.strings[M.ATOM_LOOK_PQ] = "/tmp/foreign-new.cube"
            return "/tmp/hdr-look-1.cube"
        self.builder.side_effect = build
        M.Plugin._state.update(hdr_enabled=True, hdr_saturation=1.3)
        self.plugin._schedule_hdr()
        await self.drain()
        await self.plugin.set_hdr_enabled(False)
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/tmp/foreign-new.cube")

    async def test_return_after_failed_dock_release_keeps_original_sdr_baseline(self):
        await self.plugin.set_sdr_enabled(True)
        self.float_writer.side_effect = None
        self.float_writer.return_value = False
        M.Plugin._state["external_display"] = True
        M.Plugin._release_sdr()
        self.float_writer.side_effect = self.write_float
        self.mock(M.Plugin, "_configure_for_panel", side_effect=lambda *args:
                  M.Plugin._state.update(external_display=False) or True)
        await self.plugin._refresh_panel()
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)

    async def test_missing_sdr_read_on_return_cannot_erase_an_owned_baseline(self):
        await self.plugin.set_sdr_enabled(True)
        M.Plugin._state["external_display"] = True
        self.mock(M.Plugin, "_configure_for_panel", side_effect=lambda *args:
                  M.Plugin._state.update(external_display=False) or True)
        with patch.object(M, "_read_float_atom", return_value=None):
            await self.plugin._refresh_panel()
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)


class HttpBounds(unittest.TestCase):
    def updater(self):
        return U.Updater(releases_url=M.GITHUB_RELEASES_URL, user_agent="test", log_prefix="test",
                         plugin_dir=M.PLUGIN_DIR, logger=M.decky.logger)

    def test_incremental_body_reads_check_the_deadline_before_filling_a_chunk(self):
        # HTTPResponse.read(64 KiB) waits for the entire body even if the peer
        # keeps delivering individual bytes within the socket idle timeout.
        left, right = socket.socketpair()
        finished = threading.Event()
        def send():
            try:
                right.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 16\r\n\r\n")
                for _ in range(16):
                    right.sendall(b"x")
                    if finished.wait(0.03):
                        break
            except OSError:
                pass
            finally:
                right.close()
        worker = threading.Thread(target=send)
        worker.start()
        try:
            left.settimeout(1)
            response = http.client.HTTPResponse(left)
            response.begin()
            started = time.monotonic()
            updater = self.updater()
            with patch.object(updater, "open_url", return_value=response):
                with self.assertRaises(TimeoutError):
                    updater.download_to("https://github.com/test", io.BytesIO(), 0.06)
            self.assertLess(time.monotonic() - started, 0.25)
        finally:
            finished.set()
            left.close()
            worker.join(2)

    def test_update_check_uses_a_bounded_stream_instead_of_one_large_read(self):
        class Response(io.BytesIO):
            def read(self, *args):
                raise AssertionError("unbounded body read")
        updater = self.updater()
        payload = json.dumps({"tag_name": "9.0.0", "assets": []}).encode()
        with patch.object(updater, "open_url", return_value=Response(payload)):
            info = updater.check()
        self.assertNotIn("error", info)

    def test_metadata_size_limit_is_checked_even_for_a_complete_json_body(self):
        updater = self.updater()
        with patch.object(updater, "open_url", return_value=io.BytesIO(b"{}" * 20)), \
                patch.object(U, "MAX_RELEASE_METADATA_BYTES", 32):
            self.assertIn("size limit", updater.check()["error"])


class XpropPaths(unittest.TestCase):
    def test_quoted_paths_are_unescaped_without_corrupting_utf8(self):
        cases = [(r'"/tmp/a\"b\\c.cube"', '/tmp/a"b\\c.cube'),
                 (r'"/tmp/a\011b\012c.cube"', '/tmp/a\tb\nc.cube'),
                 (r'"/tmp/\305\201\303\263d\305\272.cube"', '/tmp/Łódź.cube'),
                 ('"/tmp/kolory-Łódź.cube"', '/tmp/kolory-Łódź.cube')]
        for wire, expected in cases:
            with patch.object(M.subprocess, "run", return_value=SimpleNamespace(stdout="ATOM(UTF8_STRING) = " + wire)):
                self.assertEqual(M._read_string_atom(M.ATOM_LOOK_G22), expected)

    def test_embedded_null_is_not_a_filename(self):
        with patch.object(M.subprocess, "run", return_value=SimpleNamespace(stdout=r'ATOM = "/tmp/a\000b"')):
            self.assertIsNone(M._read_string_atom(M.ATOM_EDID_PATH))


class ReleaseVerifier(unittest.TestCase):
    def test_optimized_python_cannot_skip_archive_validation(self):
        verifier = Path(__file__).resolve().parent.parent / "scripts" / "verify_package.py"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "plugin.json").write_text(json.dumps({"version": "1.1.0"}))
            with zipfile.ZipFile(root / "DeckyVibranceHDR-1.1.0.zip", "w") as archive:
                archive.writestr("outside-the-plugin", "invalid")
                archive.writestr("DeckyVibranceHDR/package.json", '{"version":"wrong"}')
                archive.writestr("DeckyVibranceHDR/third_party/decky-api/LICENSE", "not a licence")
                archive.writestr("DeckyVibranceHDR/dist/index.js.map", '{"sources":[],"sourcesContent":[]}')
            code = "import runpy,sys; runpy.run_path(sys.argv[1])['verify'](sys.argv[2])"
            for flags in ([], ["-O"]):
                result = subprocess.run([sys.executable, *flags, "-c", code, str(verifier), str(root)],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, flags)

