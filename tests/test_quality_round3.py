"""Third review: bounded controls, monitor latency and update metadata."""
import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import _harness
_harness.install()
import main as M
import lego_updater as U
import test_recovery as R


class MonitorLatency(unittest.IsolatedAsyncioTestCase):
    setUp = R.Recovery.setUp
    asyncTearDown = R.Recovery.asyncTearDown
    mock = R.Recovery.mock
    drain = R.Recovery.drain
    write_float = R.Recovery.write_float
    write_string = R.Recovery.write_string

    async def test_other_xserver_probe_does_not_block_slider_changes(self):
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            release.wait(2)
            return False
        self.mock(M, "_other_writer_present", side_effect=probe)
        self.mock(M, "SUSPEND_POLL_S", 0.03)
        self.mock(M, "WATCH_INTERVAL_S", 0)
        self.mock(M, "PANEL_CHECK_INTERVAL_S", 10000)
        M.Plugin._slow_checks = M.SLOW_CHECK_EVERY - 1
        M.Plugin._state.update(sdr_enabled=True, sdr_applied=True)
        real_sleep, ticks = asyncio.sleep, 0
        async def sleep(delay):
            nonlocal ticks
            if delay == 0.03:
                ticks += 1
                if ticks > 1:
                    raise asyncio.CancelledError()
            await real_sleep(0)
        self.mock(M.asyncio, "sleep", side_effect=sleep)
        monitor = asyncio.create_task(self.plugin._monitor_loop())
        change = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            change = asyncio.create_task(self.plugin.set_hdr_saturation(1.2))
            done, _ = await asyncio.wait([change], timeout=0.2)
            self.assertIn(change, done, "another X server held the settings lock")
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await monitor
            if change is not None:
                await change
            await self.drain()

    async def test_accepted_saturation_is_saved_when_enable_is_blocked(self):
        M.Plugin._state.update(external_display=True)
        result = await self.plugin.set_sdr_enabled(True, 1.4)
        self.assertFalse(result["sdr_enabled"])
        self.assertEqual(result["sdr_saturation"], 1.4)
        await self.drain()
        M.Plugin._save.assert_called_once()
        M.Plugin._save.reset_mock()
        M.Plugin._state.update(external_display=False, hdr_supported=False)
        result = await self.plugin.set_hdr_enabled(True, 1.3)
        self.assertFalse(result["hdr_enabled"])
        self.assertEqual(result["hdr_saturation"], 1.3)
        await self.drain()
        M.Plugin._save.assert_called_once()

    async def test_unload_bounds_reachability_probe_already_queued_by_monitor(self):
        entered, release = threading.Event(), threading.Event()
        timeouts = []
        def read(_atom):
            entered.set()
            release.wait(2)
            return None
        def run(command, **kwargs):
            self.assertEqual(command, ["xprop", "-root"])
            timeouts.append(kwargs["timeout"])
            raise M.subprocess.TimeoutExpired(command, kwargs["timeout"])
        self.mock(M, "_read_float_atom", side_effect=read)
        self.mock(M.subprocess, "run", side_effect=run)
        M.Plugin._state.update(sdr_enabled=True, sdr_applied=True)
        M.Plugin._last_sdr_written = 1.0
        watch = asyncio.create_task(self.plugin._watch_tick())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            unload = asyncio.create_task(self.plugin._unload())
            await asyncio.sleep(0)
        finally:
            release.set()
        await watch
        await unload
        self.assertEqual(len(timeouts), 1)
        self.assertLessEqual(timeouts[0], M.UNLOAD_XPROP_TIMEOUT_S,
                             "reachability can consume Decky's entire five-second stop budget")


class UpdateMetadata(unittest.TestCase):
    def test_semver_precedence_handles_numeric_and_text_identifiers(self):
        ordered = ("1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
                   "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1")
        keys = [U.version_key(version) for version in ordered]
        self.assertEqual(sorted(keys), keys)
        self.assertEqual(len(set(keys)), len(keys))
        for bad in ("", "nightly-20261010", "01.0.0", "1.0.0-01", "1.0.0-alpha..1", "1.0"):
            with self.assertRaises(ValueError):
                U.version_key(bad)

    def test_archive_with_build_metadata_has_an_installable_filename(self):
        version = "9.0.0+build.2"
        name = f"DeckyVibranceHDR-{version}.zip"
        url = f"https://github.com/Rayekkk/DeckyVibranceHDR/releases/download/{version}/{name}"
        updater = U.Updater(releases_url=M.GITHUB_RELEASES_URL, user_agent="test", log_prefix="test",
                            plugin_dir=M.PLUGIN_DIR, logger=M.decky.logger,
                            asset_name=lambda v: f"DeckyVibranceHDR-{v}.zip", archive_root="DeckyVibranceHDR")
        updater._checked_asset = (url, name)
        body = io.BytesIO()
        with zipfile.ZipFile(body, "w") as archive:
            for filename, contents in {"main.py": "# backend", "dist/index.js": "export default {}",
                    "plugin.json": json.dumps({"version": version}),
                    "package.json": json.dumps({"version": version})}.items():
                archive.writestr("DeckyVibranceHDR/" + filename, contents)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(U, "real_user", return_value=SimpleNamespace(pw_dir=folder, pw_uid=1, pw_gid=1)), \
                patch.object(U, "xdg_download_dir", return_value=folder), \
                patch.object(U.os, "chown", create=True), \
                patch.object(updater, "download_to", side_effect=lambda _url, out, timeout: out.write(body.getvalue())):
            result = updater.download(url, name)
            self.assertTrue(result["success"], result)
            self.assertEqual(Path(result["path"]).read_bytes(), body.getvalue())

    def check(self, installed, tag):
        updater = U.Updater(releases_url=M.GITHUB_RELEASES_URL, user_agent="test", log_prefix="test",
                            plugin_dir=M.PLUGIN_DIR, logger=M.decky.logger,
                            asset_name=lambda v: f"DeckyVibranceHDR-{v}.zip")
        version = tag.removeprefix("v")
        name = f"DeckyVibranceHDR-{version}.zip"
        data = {"tag_name": tag, "assets": [{"name": name,
            "browser_download_url": f"https://github.com/Rayekkk/DeckyVibranceHDR/releases/download/{tag}/{name}"}]}
        with patch.object(updater, "plugin_version", return_value=installed), \
                patch.object(updater, "open_url", return_value=io.BytesIO(json.dumps(data).encode())):
            return updater.check()

    def test_stable_release_is_an_update_to_the_same_prerelease_core(self):
        info = self.check("3.0.0-rc.1", "v3.0.0")
        self.assertNotIn("error", info)
        self.assertTrue(info["update_available"])

    def test_prerelease_tag_retains_its_exact_asset_name(self):
        info = self.check("3.0.0-beta.1", "v3.0.0-beta.2")
        self.assertNotIn("error", info)
        self.assertEqual(info["latest_version"], "3.0.0-beta.2")
        self.assertEqual(info["asset_name"], "DeckyVibranceHDR-3.0.0-beta.2.zip")
        self.assertTrue(info["update_available"])

    def test_build_metadata_does_not_make_a_newer_version(self):
        info = self.check("3.0.0+build.1", "v3.0.0+build.2")
        self.assertNotIn("error", info)
        self.assertFalse(info["update_available"])

    def test_download_directory_decodes_quotes_and_preserves_literal_dollars(self):
        with tempfile.TemporaryDirectory() as home:
            config = Path(home) / ".config"
            config.mkdir()
            (config / "user-dirs.dirs").write_text(
                'XDG_DOWNLOAD_DIR="$HOME/Pobrane \\"gry\\"/\\$HOME" # comment\n', encoding="utf-8")
            self.assertEqual(U.xdg_download_dir(home), os.path.join(home, 'Pobrane "gry"', "$HOME"))

    def test_invalid_download_directory_uses_the_home_fallback(self):
        with tempfile.TemporaryDirectory() as home:
            config = Path(home) / ".config"
            config.mkdir()
            for line in ('XDG_DOWNLOAD_DIR="relative/path"', 'XDG_DOWNLOAD_DIR="unterminated',
                         'XDG_DOWNLOAD_DIR="$OTHER/Downloads"', 'XDG_DOWNLOAD_DIR=""'):
                (config / "user-dirs.dirs").write_text(line, encoding="utf-8")
                self.assertEqual(U.xdg_download_dir(home), os.path.join(home, "Downloads"), line)
