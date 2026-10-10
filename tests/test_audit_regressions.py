# SPDX-License-Identifier: BSD-3-Clause
"""Audit regressions; no gamescope, network traffic or device changes."""
import asyncio
import io
import json
import hashlib
import os
import tempfile
import threading
import subprocess
import unittest
import urllib.request
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import _harness
_harness.install()
import main as M
import vibrance_lut as V
import lego_updater as U
import test_recovery as R


class Persistence(unittest.IsolatedAsyncioTestCase):
    setUp = R.Recovery.setUp
    asyncTearDown = R.Recovery.asyncTearDown
    mock = R.Recovery.mock
    drain = R.Recovery.drain
    write_float = R.Recovery.write_float
    write_string = R.Recovery.write_string
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
        self.mock(M.Plugin, "_save", side_effect=lambda: saved.append(dict(M.Plugin._state)))
        await self.plugin.set_hdr_saturation(1.4)
        await self.plugin._unload()
        self.assertEqual(saved[-1]["hdr_saturation"], 1.4)
        self.assertEqual(len(saved), 1)

    async def test_unload_restores_before_persistence_and_bounds_xprop_wait(self):
        calls = []
        self.mock(M.Plugin, "_release_sdr", side_effect=lambda: calls.append("sdr"))
        self.mock(M.Plugin, "_release_hdr", side_effect=lambda: calls.append("hdr"))
        self.mock(M.Plugin, "_save", side_effect=lambda: calls.append("save"))
        await self.plugin._unload()
        self.assertEqual(set(calls[:2]), {"sdr", "hdr"})
        self.assertEqual(calls[-1], "save")
        with patch.object(M.subprocess, "run", side_effect=subprocess.TimeoutExpired("xprop", 0.25)) as run:
            self.assertIsNone(M._read_int_atom(M.ATOM_IS_EXTERNAL))
            self.assertLessEqual(run.call_args.kwargs["timeout"], 0.25)

    async def test_two_hundred_requests_use_bounded_workers_and_latest_value(self):
        M.Plugin._state.update(hdr_enabled=True)
        self.mock(M, "DEBOUNCE_S", 0.005)
        for n in range(200):
            await self.plugin.set_hdr_saturation(1 + n / 400)
        self.assertLessEqual(len(M.Plugin._tasks), 2)
        await self.drain()
        self.builder.assert_called_once()
        self.assertAlmostEqual(M.Plugin._state["hdr_applied_saturation"], 1.4975)

    async def test_new_value_cancels_inflight_build_then_same_worker_applies_latest(self):
        started = threading.Event()
        builds = []
        def build(value, slot, *args):
            builds.append(value)
            if len(builds) == 1:
                started.set()
                self.assertTrue(threading.Event().wait(0.001) is False)
                for _ in range(1000):
                    if args[-1]():
                        return ""
                    threading.Event().wait(0.001)
                raise AssertionError("obsolete build was never cancelled")
            return f"/tmp/hdr-look-{slot}.cube"
        self.builder.side_effect = build
        M.Plugin._state.update(hdr_enabled=True)
        await self.plugin.set_hdr_saturation(1.1)
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        worker = M.Plugin._workers["hdr"]
        for n in range(40):
            await self.plugin.set_hdr_saturation(1.2 + n / 200)
        self.assertIs(M.Plugin._workers["hdr"], worker)
        await self.drain()
        self.assertEqual(builds, [1.1, 1.395])
        self.assertEqual(M.Plugin._state["hdr_applied_saturation"], 1.395)
        self.string_writer.assert_called_once()

    async def test_strict_rpc_inputs_and_toggle_carries_pending_value(self):
        for bad in (None, True, "1.3", float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                await self.plugin.set_hdr_saturation(bad)
        with self.assertRaises(ValueError):
            await self.plugin.set_hdr_enabled("false")
        await self.plugin.set_hdr_enabled(False, 1.35)
        self.assertEqual(M.Plugin._state["hdr_saturation"], 1.35)

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

    async def test_same_server_conflict_is_not_relabelled_as_another_server(self):
        M.Plugin._state.update(sdr_enabled=True, sdr_applied=True,
                              conflict="another plugin keeps overwriting", sdr_saturation=1.4)
        M.Plugin._last_sdr_written = self.live_sdr = 1.4
        M.Plugin._watch_once()
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
        self.mock(M, "_count_gamescope_atoms", return_value=44)
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

    async def test_repeated_resume_events_share_one_worker_and_restart_the_ladder(self):
        self.mock(M, "REASSERT_DELAYS_S", (0.02, 0))
        applied = self.mock(M.Plugin, "_reassert")
        self.plugin._schedule_reassert(self.plugin._reassert_repeatedly)
        await asyncio.sleep(0)
        worker = M.Plugin._workers["reassert"]
        for _ in range(100):
            self.plugin._schedule_reassert(self.plugin._reassert_repeatedly)
        self.assertEqual(M.Plugin._tasks, [worker])
        await self.drain()
        self.assertEqual(applied.call_count, 2)

    async def test_zero_atoms_during_settle_never_become_ready(self):
        self.mock(M, "_pick_display", return_value=":0")
        self.mock(M, "_count_gamescope_atoms", side_effect=[0, 44, 44])
        self.mock(M, "GAMESCOPE_SETTLE_S", 0)
        self.mock(M, "GAMESCOPE_RETRY_FIRST_S", 0)
        self.mock(M.Plugin, "_capture_existing_looks")
        self.mock(M.Plugin, "_configure_for_panel", return_value=False)
        self.mock(self.plugin, "_reassert_repeatedly", return_value=None)
        await self.plugin._start_when_ready(start_monitor=False)
        self.assertEqual(M._pick_display.call_count, 2)
        self.assertTrue(M.Plugin._state["ready"])
        await self.drain()


class AtomicSettings(unittest.TestCase):
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


def edid_cta(*masks):
    base = bytearray(128)
    base[126] = 1
    block = bytearray(128)
    block[:3] = bytes((2, 3, 4 + len(masks) * 4))
    for n, mask in enumerate(masks):
        block[4 + n * 4:8 + n * 4] = bytes((0xe3, 6, mask, 1))
    return _harness.valid_edid(base + block)


class Edid(unittest.TestCase):
    def setUp(self):
        for obj, name in ((M.Plugin, "_state"), (M.Plugin, "_sdr_white_point"),
                          (M, "SDR_NEUTRAL_ATOM"), (M, "SDR_ATOM_SPAN")):
            value = getattr(obj, name)
            context = patch.object(obj, name, dict(value) if isinstance(value, dict) else value)
            context.start()
            self.addCleanup(context.stop)
    def test_pq_after_non_pq_metadata_is_found(self):
        self.assertIs(M._edid_supports_pq(edid_cta(1, 4)), True)
        blob = bytearray(edid_cta(1))
        blob[126] = 2
        blob.extend(edid_cta(4)[128:])
        self.assertIs(M._edid_supports_pq(_harness.valid_edid(blob)), True)

    def test_invalid_header_checksum_and_truncated_blocks_are_unknown(self):
        valid = edid_cta(4)
        for index in (0, 20, 150, 255):
            broken = bytearray(valid)
            broken[index] ^= 1
            self.assertIsNone(M._edid_supports_pq(broken))
        self.assertIsNone(M._edid_red_primary(bytes(29)))
        truncated = bytearray(valid)
        truncated[130] = 6
        self.assertIsNone(M._edid_supports_pq(_harness.valid_edid(truncated)))

    def test_displayid_scans_all_embedded_cta_collections(self):
        base, extension = bytearray(128), bytearray(128)
        base[126] = 1
        extension[0], extension[2] = 0x70, 14
        extension[5:19] = bytes((0x81, 0, 4, 0xe3, 6, 1, 1, 0x81, 0, 4, 0xe3, 6, 4, 1))
        self.assertIs(M._edid_supports_pq(_harness.valid_edid(base + extension)), True)

    def test_panel_reads_one_edid_and_invalidates_cache_when_path_changes(self):
        with patch.object(M.Plugin, "_panel_cache_key", None), patch.object(M.Plugin, "_panel_cache_complete", False), \
                patch.object(M, "_display_is_external", return_value=False), \
                patch.object(M, "_read_string_atom", side_effect=["/edid/one", "/edid/one", "/edid/two"]), \
                patch.object(M, "_read_edid", return_value=edid_cta(4)) as reader:
            M.Plugin._configure_for_panel()
            M.Plugin._configure_for_panel()
            M.Plugin._configure_for_panel()
            # Red coordinates in this fixture are unspecified, so classification
            # retries. A valid panel fixture completes and caches below.
            self.assertEqual(reader.call_count, 3)

    def test_valid_panel_cache_detects_an_internal_edid_path_change(self):
        from test_logic import PanelDetection
        blob = bytearray(edid_cta(4))
        red = PanelDetection._edid(0.6836, 0.3154)
        blob[0x19:0x1d] = red[0x19:0x1d]
        blob = _harness.valid_edid(blob)
        with patch.object(M.Plugin, "_panel_cache_key", None), patch.object(M.Plugin, "_panel_cache_complete", False), \
                patch.object(M, "_display_is_external", return_value=False), \
                patch.object(M, "_read_string_atom", side_effect=["/edid/one", "/edid/one", "/edid/two"]), \
                patch.object(M, "_read_edid", return_value=blob) as reader:
            M.Plugin._configure_for_panel()
            M.Plugin._configure_for_panel()
            M.Plugin._configure_for_panel()
            self.assertEqual(reader.call_count, 2)


class Lut(unittest.TestCase):
    def test_production_grids_match_pre_audit_golden_output(self):
        hashes = {"pq": "351335e1d2a649dab1b2f6076f5c4ed27f16e31a286ae5541aa3e879a7f9f4a2",
                  "g22": "4887c2e523a5d3f17fc350364006aac72efa95144004beb0318117e0f9981893"}
        for encoding, expected in hashes.items():
            self.assertEqual(hashlib.sha256(V.generate(1.3, 33, encoding).encode()).hexdigest(), expected)

    def test_grid_cache_is_bounded_and_separates_source_whites(self):
        V._grids.clear()
        for white in (V.D65, (0.296, 0.307), (0.307, 0.322)):
            V.generate(1.2, 33, "g22", white)
            self.assertLessEqual(len(V._grids), 2)
        self.assertEqual({key[2] for key in V._grids}, {(0.296, 0.307), (0.307, 0.322)})
    def test_prepared_grid_matches_direct_math_for_every_supported_step(self):
        for encoding in ("pq", "g22"):
            for white in (V.D65, (0.296, 0.307)):
                for n in range(51):
                    saturation = 1 + n / 100
                    rows = V.generate(saturation, 3, encoding, white).splitlines()[6:]
                    expected = ["%.6f %.6f %.6f" % V.saturate((r/2, g/2, b/2), saturation, encoding, white)
                                for b in range(3) for g in range(3) for r in range(3)]
                    self.assertEqual(rows, expected)

    def test_cancellation_interrupts_build_and_cannot_replace_a_slot(self):
        with self.assertRaises(V.BuildCancelled):
            V.generate(1.3, cancelled=lambda: True)
        with tempfile.TemporaryDirectory() as folder, patch.object(M, "_runtime_dir", return_value=folder), \
                patch.object(M, "_lut_writes_disabled", False):
            path = os.path.join(folder, "hdr-look-1.cube")
            with open(path, "w") as f:
                f.write("old")
            called = 0
            def cancelled():
                nonlocal called
                called += 1
                return called > 3
            self.assertEqual(M._write_look(1.3, 1, cancelled=cancelled), "")
            with open(path) as f:
                self.assertEqual(f.read(), "old")
            self.assertEqual(os.listdir(folder), ["hdr-look-1.cube"])

    def test_job_superseded_while_waiting_for_file_lock_never_generates(self):
        cancelled = threading.Event()
        result = []
        with patch.object(M, "_lut_writes_disabled", False), patch.object(V, "generate") as generate:
            with M._lut_file_lock:
                worker = threading.Thread(target=lambda: result.append(M._write_look(1.3, 1, cancelled=cancelled.is_set)))
                worker.start()
                cancelled.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [""])
            generate.assert_not_called()


class Updater(unittest.TestCase):
    def test_decky_host_user_precedes_the_first_passwd_entry(self):
        user = SimpleNamespace(pw_dir="/home/player", pw_uid=1001, pw_gid=1001)
        with patch.dict(U.os.environ, {"DECKY_USER": "player"}), \
                patch.object(U.pwd, "getpwnam", return_value=user), \
                patch.object(U.os.path, "isdir", return_value=True), patch.object(U.pwd, "getpwall") as fallback:
            self.assertIs(U.real_user(), user)
            fallback.assert_not_called()
    def test_parallel_download_is_rejected_without_blocking_an_executor_thread(self):
        updater = self.make(False)
        started, finish = threading.Event(), threading.Event()
        def first(*args):
            started.set()
            finish.wait(2)
            return {"success": True}
        with patch.object(updater, "_download", side_effect=first):
            task = threading.Thread(target=lambda: updater.download("https://github.com/a.zip", "a.zip"))
            task.start()
            try:
                self.assertTrue(started.wait(1))
                result = updater.download("https://github.com/a.zip", "a.zip")
                self.assertFalse(result["success"])
                self.assertIn("in progress", result["error"])
            finally:
                finish.set()
                task.join(2)
    def make(self, specific=True):
        return U.Updater(releases_url=M.GITHUB_RELEASES_URL, user_agent="test", log_prefix="test",
                         plugin_dir=M.PLUGIN_DIR, logger=M.decky.logger,
                         asset_name=(lambda v: f"DeckyVibranceHDR-{v}.zip") if specific else None,
                         archive_root="DeckyVibranceHDR" if specific else None,
                         archive_files=("lego_updater.py", "vibrance_lut.py") if specific else ())

    def test_every_redirect_is_checked_before_contact(self):
        handler = U.CheckedRedirect()
        request = urllib.request.Request("https://github.com/start")
        for url in ("http://github.com/end", "https://example.invalid/end", "https://github.com:444/end", "https://user@github.com/end"):
            with self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, "Found", {}, url)
        next_request = handler.redirect_request(request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/end")
        self.assertEqual(next_request.full_url, "https://release-assets.githubusercontent.com/end")

    def test_expected_asset_is_selected_even_if_another_zip_is_first(self):
        updater = self.make()
        url = "https://github.com/Rayekkk/DeckyVibranceHDR/releases/download/9.0.0/DeckyVibranceHDR-9.0.0.zip"
        data = {"tag_name": "9.0.0", "assets": [{"name": "source.zip", "browser_download_url": url},
                 {"name": "DeckyVibranceHDR-9.0.0.zip", "browser_download_url": url}]}
        with patch.object(updater, "open_url", return_value=io.BytesIO(json.dumps(data).encode())):
            info = updater.check()
        self.assertEqual(info["asset_name"], "DeckyVibranceHDR-9.0.0.zip")
        self.assertEqual(updater._checked_asset, (url, info["asset_name"]))

    def test_asset_url_must_match_the_repository_tag_and_filename(self):
        for url in ("https://github.com/Other/Repo/releases/download/9.0.0/DeckyVibranceHDR-9.0.0.zip",
                    "https://github.com/Rayekkk/DeckyVibranceHDR/releases/download/9.0.0/source.zip"):
            updater = self.make()
            data = {"tag_name": "9.0.0", "assets": [{"name": "DeckyVibranceHDR-9.0.0.zip", "browser_download_url": url}]}
            with patch.object(updater, "open_url", return_value=io.BytesIO(json.dumps(data).encode())):
                self.assertIn("error", updater.check())
            self.assertIsNone(updater._checked_asset)

    def test_failed_download_keeps_previous_archive_and_removes_partial(self):
        updater = self.make(False)
        with tempfile.TemporaryDirectory() as folder, patch.object(U, "real_user", return_value=SimpleNamespace(pw_dir=folder, pw_uid=1, pw_gid=1)), \
                patch.object(U, "xdg_download_dir", return_value=folder), patch.object(updater, "download_to", side_effect=OSError("offline")):
            path = os.path.join(folder, "release.zip")
            with open(path, "wb") as f:
                f.write(b"old")
            result = updater.download("https://github.com/release.zip", "release.zip")
            self.assertFalse(result["success"])
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"old")
            self.assertEqual(os.listdir(folder), ["release.zip"])

    def test_arbitrary_rpc_asset_is_rejected_before_disk_or_network(self):
        updater = self.make()
        with patch.object(U, "real_user") as user, patch.object(updater, "open_url") as opened:
            result = updater.download("https://github.com/other.zip", "other.zip")
        self.assertFalse(result["success"])
        user.assert_not_called()
        opened.assert_not_called()

    def archive(self, entries=None):
        output = io.BytesIO()
        version = "9.0.0"
        default = {"main.py": "# plugin", "lego_updater.py": "# updater", "vibrance_lut.py": "# lut",
                   "plugin.json": json.dumps({"version": version}),
                   "package.json": json.dumps({"version": version}), "dist/index.js": "export default {}"}
        with zipfile.ZipFile(output, "w") as archive:
            for name, data in (entries or {"DeckyVibranceHDR/" + k: v for k, v in default.items()}).items():
                archive.writestr(name, data)
        return output.getvalue()

    def attempt(self, payload, *, specific=True):
        updater = self.make(specific)
        name = "DeckyVibranceHDR-9.0.0.zip"
        url = "https://github.com/Rayekkk/DeckyVibranceHDR/releases/download/9.0.0/" + name
        updater._checked_asset = (url, name)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(U, "real_user", return_value=SimpleNamespace(pw_dir=folder, pw_uid=1, pw_gid=1)), \
                patch.object(U, "xdg_download_dir", return_value=folder), patch.object(U.os, "chown", create=True), \
                patch.object(updater, "download_to", side_effect=lambda _url, out, timeout: out.write(payload)):
            path = os.path.join(folder, name)
            with open(path, "wb") as f:
                f.write(b"previous")
            result = updater.download(url, name)
            with open(path, "rb") as f:
                data = f.read()
            self.assertEqual(os.listdir(folder), [name])
        return result, data

    def test_success_atomically_replaces_the_old_archive(self):
        payload = self.archive()
        result, data = self.attempt(payload)
        self.assertTrue(result["success"], result)
        self.assertEqual(data, payload)

    def test_bad_archives_never_replace_the_previous_download(self):
        for payload in (b"not a zip", self.archive({"../escape": "x"}),
                        self.archive({"OtherPlugin/main.py": "x"}),
                        self.archive({"DeckyVibranceHDR/main.py": "incomplete"})):
            result, data = self.attempt(payload)
            self.assertFalse(result["success"], result)
            self.assertEqual(data, b"previous")

    def test_ambiguous_archive_paths_are_rejected_before_replacement(self):
        for name in ("root/./file", "root//file", "/absolute", "C:/file", "root\\file"):
            # zipfile normalizes backslashes while writing on Windows; construct
            # the raw entry name to exercise validation on either host OS.
            payload = self.archive({name.replace("\\", "/"): "x"})
            payload = payload.replace(name.replace("\\", "/").encode(), name.encode())
            result, data = self.attempt(payload, specific=False)
            self.assertFalse(result["success"], name)
            self.assertEqual(data, b"previous")

    def test_size_and_total_time_limits_are_enforced(self):
        updater = self.make(False)
        with patch.object(updater, "open_url", return_value=io.BytesIO(b"12345")), patch.object(U, "MAX_DOWNLOAD_BYTES", 4):
            with self.assertRaises(ValueError):
                updater.download_to("https://github.com/file", io.BytesIO(), 60)
        with patch.object(updater, "open_url", return_value=io.BytesIO(b"1")), patch.object(U.time, "monotonic", side_effect=[0, 61]):
            with self.assertRaises(TimeoutError):
                updater.download_to("https://github.com/file", io.BytesIO(), 60)
