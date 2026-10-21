# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Update checks, downloads, release archives and the packaged ZIP."""

import asyncio
import http.client
import io
import json
import os
import runpy
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import warnings
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _fixtures import PluginTest  # installs the loader stand-ins first
import main as M
import lego_updater as U


class Updater(PluginTest):
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

    def test_as_root_a_download_folder_outside_home_is_refused(self):
        """user-dirs.dirs is the user's file. A root plugin must not create and
        then hand over whatever directory it names, and must change ownership
        through its own descriptor rather than a path the user could swap."""
        updater = self.make()
        name = "DeckyVibranceHDR-9.0.0.zip"
        url = "https://github.com/Rayekkk/DeckyVibranceHDR/releases/download/9.0.0/" + name
        updater._checked_asset = (url, name)
        payload = self.archive()
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as elsewhere, \
                patch.object(U, "real_user", return_value=SimpleNamespace(pw_dir=home, pw_uid=1000, pw_gid=1000)), \
                patch.object(U, "xdg_download_dir", return_value=os.path.join(elsewhere, "target")), \
                patch.object(U.os, "geteuid", create=True, return_value=0), \
                patch.object(U.os, "chown", create=True) as chown, \
                patch.object(U.os, "fchown", create=True) as fchown, \
                patch.object(updater, "download_to", side_effect=lambda _url, out, timeout: out.write(payload)):
            result = updater.download(url, name)
            self.assertTrue(result["success"], result)
            self.assertEqual(result["path"], os.path.join(home, "Downloads", name))
            self.assertEqual(os.listdir(elsewhere), [])
            chown.assert_called_once_with(os.path.join(home, "Downloads"), 1000, 1000)
            (fd, uid, gid), _ = fchown.call_args
            self.assertIsInstance(fd, int)
            self.assertEqual((uid, gid), (1000, 1000))

    def test_as_root_a_downloads_symlink_out_of_home_is_refused(self):
        updater = self.make()
        name = "DeckyVibranceHDR-9.0.0.zip"
        url = "https://github.com/Rayekkk/DeckyVibranceHDR/releases/download/9.0.0/" + name
        updater._checked_asset = (url, name)
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as elsewhere:
            try:
                os.symlink(elsewhere, os.path.join(home, "Downloads"), target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("this platform cannot create the symlink fixture")
            with patch.object(U, "real_user", return_value=SimpleNamespace(pw_dir=home, pw_uid=1000, pw_gid=1000)), \
                    patch.object(U, "xdg_download_dir", return_value=os.path.join(home, "Downloads")), \
                    patch.object(U.os, "geteuid", create=True, return_value=0), \
                    patch.object(U.os, "chown", create=True), patch.object(U.os, "fchown", create=True), \
                    patch.object(updater, "download_to") as download:
                result = updater.download(url, name)
            self.assertFalse(result["success"])
            self.assertIn("outside the user's home", result["error"])
            download.assert_not_called()
            self.assertEqual(os.listdir(elsewhere), [])

    def test_within_follows_the_real_paths(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertTrue(U.within(os.path.join(home, "Downloads"), home))
            self.assertTrue(U.within(home, home))
            self.assertFalse(U.within(os.path.join(home, "..", "other"), home))
            self.assertFalse(U.within(home + "-sibling", home))

    def test_open_url_installs_the_redirect_check(self):
        """The handler is tested on its own elsewhere; this pins that real
        requests go through it, with certificate checks on."""
        updater = self.make()
        opener = SimpleNamespace(open=lambda request, timeout: (request, timeout))
        with patch.object(U.urllib.request, "build_opener", return_value=opener) as build:
            request, timeout = updater.open_url(M.GITHUB_RELEASES_URL, timeout=7)
        handlers = build.call_args.args
        self.assertTrue(any(isinstance(h, U.CheckedRedirect) for h in handlers))
        https = next(h for h in handlers if isinstance(h, U.urllib.request.HTTPSHandler))
        self.assertEqual(https._context.verify_mode, U.ssl.CERT_REQUIRED)
        self.assertEqual((request.full_url, timeout), (M.GITHUB_RELEASES_URL, 7))
        with self.assertRaises(ValueError):
            updater.open_url("https://example.com/release.zip", timeout=7)

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


class ArchiveNames(PluginTest):
    make = Updater.make
    archive = Updater.archive
    attempt = Updater.attempt

    def test_embedded_nul_in_entry_names_cannot_replace_the_previous_archive(self):
        output = io.BytesIO(self.archive())
        name = "DeckyVibranceHDR/fileXtail"
        with zipfile.ZipFile(output, "a") as archive:
            archive.writestr(name, "payload")
        payload = output.getvalue().replace(name.encode(), name.replace("X", "\0").encode())
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            entry = archive.infolist()[-1]
            self.assertNotEqual(entry.orig_filename, entry.filename)
        result, data = self.attempt(payload)
        self.assertFalse(result["success"], "a truncated filename passed archive validation")
        self.assertEqual(data, b"previous")


class UpdateMetadata(PluginTest):
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


class UpdateChecks(PluginTest):
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


class HttpBounds(PluginTest):
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
                    if finished.wait(0.1):
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
            # 16 bytes at 0.1 s take 1.6 s, so this only passes if the deadline cut in.
            self.assertLess(time.monotonic() - started, 1.0)
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


class UpdateAssetValidation(PluginTest):

    def check(self, *names):
        base = "https://github.com/Rayekkk/DeckyVibranceHDR/releases/download/2.0.0/"
        data = {"tag_name": "2.0.0",
                "assets": [{"name": n, "browser_download_url": base + n} for n in names]}
        with patch.object(M.updater, "plugin_version", return_value="1.0.0"), \
                patch.object(M.updater, "open_url",
                             return_value=io.BytesIO(json.dumps(data).encode())):
            return asyncio.run(M.Plugin().check_for_updates())

    def test_update_without_the_expected_zip_is_not_downloadable(self):
        info = self.check("source.zip")
        self.assertTrue(info["update_available"])
        self.assertIsNone(info.get("download_url"))
        self.assertIsNone(info.get("asset_name"))
        self.assertIn("DeckyVibranceHDR-2.0.0.zip", info["error"])

    def test_exact_release_zip_remains_downloadable(self):
        expected = "DeckyVibranceHDR-2.0.0.zip"
        info = self.check("source.zip", expected)
        self.assertNotIn("error", info)
        self.assertEqual(info["asset_name"], expected)


class ReleaseVerifier(PluginTest):
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
                # The first check must fire; a KeyError further on would hide -O skipping it.
                self.assertIn("unsafe ZIP path", result.stderr, flags)

    def test_release_check_refuses_uncommitted_shipped_files(self):
        committed = runpy.run_path(str(Path(__file__).resolve().parent.parent
                                       / "scripts" / "verify_package.py"))["committed"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            committed(root)  # not a checkout: nothing to compare against
            (root / ".git").mkdir()
            with self.assertRaises(subprocess.CalledProcessError):
                committed(root)  # a checkout git cannot read stops the release
            (root / ".git").rmdir()
            def git(*args):
                subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                               cwd=root, check=True, capture_output=True)
            git("init", "-q")
            (root / "main.py").write_text("shipped")
            git("add", "main.py")
            git("commit", "-q", "-m", "release")
            (root / "notes.txt").write_text("not shipped")
            committed(root)
            (root / "main.py").write_text("edited after the commit")
            with self.assertRaisesRegex(ValueError, "uncommitted changes would ship"):
                committed(root)
            with patch.dict(os.environ, {"DVH_ALLOW_DIRTY": "1"}):
                committed(root)

    def test_source_map_ties_the_shipped_frontend_to_src(self):
        check = runpy.run_path(str(Path(__file__).resolve().parent.parent
                                   / "scripts" / "verify_package.py"))["check_source_map"]
        base = "decky://decky/plugin/Decky%20Vibrance%20HDR/"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "src").mkdir()
            (root / "src" / "index.tsx").write_bytes(b"ui\r\n")
            def source_map(prefix=base, text="ui\r\n"):
                return {"sources": [prefix + "node_modules/@decky/api/dist/index.js",
                                    prefix + "src/index.tsx"],
                        "sourcesContent": ["api", text]}
            check(root, source_map())
            with self.assertRaisesRegex(ValueError, "stale frontend build"):
                check(root, source_map(text="ui before the last commit\r\n"))
            with self.assertRaisesRegex(ValueError, "not rewritten"):
                check(root, source_map(prefix="../"))
            (root / "src" / "stateSync.ts").write_text("new file")
            with self.assertRaisesRegex(ValueError, "does not cover"):
                check(root, source_map())


class ArchiveRules(PluginTest):
    make = Updater.make
    archive = Updater.archive
    attempt = Updater.attempt

    def files(self, **changes):
        version = "9.0.0"
        files = {"main.py": "# plugin", "lego_updater.py": "# updater", "vibrance_lut.py": "# lut",
                 "plugin.json": json.dumps({"version": version}),
                 "package.json": json.dumps({"version": version}), "dist/index.js": "export default {}"}
        files.update(changes)
        return {"DeckyVibranceHDR/" + k: v for k, v in files.items() if v is not None}

    def refused(self, payload):
        result, data = self.attempt(payload)
        self.assertFalse(result["success"], result)
        self.assertEqual(data, b"previous")

    def test_each_archive_rule_refuses_on_its_own(self):
        self.refused(self.archive(self.files(**{"dist/index.js": None})))
        self.refused(self.archive({**self.files(), "Elsewhere/readme": "x"}))
        self.refused(self.archive(self.files(**{"plugin.json": json.dumps({"version": "8.0.0"}),
                                                "package.json": json.dumps({"version": "8.0.0"})})))
        self.refused(self.archive(self.files(**{"package.json": json.dumps({"version": "8.0.0"})})))

    def test_symlink_entries_are_refused(self):
        output = io.BytesIO(self.archive())
        with zipfile.ZipFile(output, "a") as archive:
            info = zipfile.ZipInfo("DeckyVibranceHDR/link")
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, "/etc/passwd")
        self.refused(output.getvalue())

    def test_duplicate_entries_are_refused(self):
        output = io.BytesIO(self.archive())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # zipfile warns about the duplicate
            with zipfile.ZipFile(output, "a") as archive:
                archive.writestr("DeckyVibranceHDR/main.py", "# second")
        self.refused(output.getvalue())

    def test_oversized_expansion_is_refused(self):
        with patch.object(U, "MAX_DOWNLOAD_BYTES", 8):
            self.refused(self.archive())

    def test_lookalike_hosts_are_refused(self):
        for url in ("https://evilgithub.com/x", "https://github.com.evil.example/x",
                    "https://api.github.com.evil.example/x"):
            with self.assertRaises(ValueError):
                U.checked_url(url)

    def test_a_failed_recheck_withdraws_the_earlier_authorization(self):
        updater = self.make()
        url = "https://github.com/Rayekkk/DeckyVibranceHDR/releases/download/9.0.0/DeckyVibranceHDR-9.0.0.zip"
        good = {"tag_name": "9.0.0", "assets": [{"name": "DeckyVibranceHDR-9.0.0.zip", "browser_download_url": url}]}
        with patch.object(updater, "plugin_version", return_value="8.0.0"), \
                patch.object(updater, "open_url", return_value=io.BytesIO(json.dumps(good).encode())):
            updater.check()
        self.assertIsNotNone(updater._checked_asset)
        with patch.object(updater, "plugin_version", return_value="8.0.0"), \
                patch.object(updater, "open_url", side_effect=OSError("offline")):
            self.assertIn("error", updater.check())
        self.assertIsNone(updater._checked_asset)

    def test_loader_version_wins_over_plugin_json(self):
        with patch.dict(os.environ, {"DECKY_PLUGIN_VERSION": "7.7.7"}):
            self.assertEqual(M.updater.plugin_version(), "7.7.7")
