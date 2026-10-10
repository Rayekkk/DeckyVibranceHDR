# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Update checks and downloads, originally shared between the LeGo plugins.

Plugin-specific asset selection and archive validation arrive through the
constructor. This repository includes additional audit fixes; other plugin
repositories are maintained independently.

The `lego_` prefix is not decoration. Before running a plugin, the loader
aliases each of its own submodules to a bare name:

    keys = [key for key in sys.modules if key.startswith("decky_loader.")]
    for key in keys:
        sys.modules[key.replace("decky_loader.", "")] = sys.modules[key]

That happens before this module is ever imported, and `import x` consults
sys.modules before sys.path - so a plugin file called `updater.py` never
loads at all. `from updater import Updater` silently returned the loader's
own Updater class and both plugins died on the constructor. The names to
stay away from are browser, enums, helpers, injector, loader, main, settings,
updater, utilities and wsrouter. (`settings` is the exception we want: that
alias is how every plugin reaches SettingsManager.)

Nothing here imports `decky`, so the module can be exercised by the test
suites without the loader present.
"""

import json
import io
import os
import pwd
import re
import ssl
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile

# Only these hosts may be contacted. Some plugins run as root, so an
# unrestricted URL would be an arbitrary-fetch primitive - and in LeGoTDP's
# case the fetched file is then executed.
ALLOWED_HOSTS = frozenset({
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})

# Refuse absurd downloads. Release archives and the RyzenAdj tarball are a
# few MB at most.
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_RELEASE_METADATA_BYTES = 1024 * 1024
HTTP_IDLE_TIMEOUT_S = 5.0

# Decky runs plugins inside a PyInstaller-frozen PluginLoader whose OpenSSL has
# its CA paths baked in from the build machine. They do not exist on the
# device, so ssl.create_default_context() comes back with an empty trust store
# and every request dies with CERTIFICATE_VERIFY_FAILED. That is what the old
# CERT_NONE was working around. Point the context at a real bundle instead.
CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",   # Arch, SteamOS, Debian
    "/etc/ssl/cert.pem",                    # Alpine, macOS, also present on SteamOS
    "/etc/pki/tls/certs/ca-bundle.crt",     # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",               # openSUSE
)


# ── Pure helpers ───────────────────────────────────────────────────────────────

def checked_url(url: str) -> str:
    """Reject anything that is not an https URL on a known GitHub host."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"refusing non-https URL scheme '{parsed.scheme}'")
    if (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError(f"refusing download from untrusted host '{parsed.hostname}'")
    if parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
        raise ValueError("URL credentials and non-HTTPS ports are not allowed")
    return url


class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def version_key(text: str):
    """SemVer precedence, including prereleases and ignoring build metadata."""
    match = re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
        r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", text)
    if not match:
        raise ValueError(f"invalid release version: {text!r}")
    prerelease = match[4]
    identifiers = prerelease.split(".") if prerelease is not None else []
    if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in identifiers):
        raise ValueError(f"invalid prerelease version: {text!r}")
    return (tuple(int(match[i]) for i in (1, 2, 3)), prerelease is None,
            tuple((0, int(part)) if part.isdigit() else (1, part) for part in identifiers))


def real_user() -> pwd.struct_passwd | None:
    """Use Decky's host user; privileged plugins cannot use '~' as a fallback."""
    username = os.environ.get("DECKY_USER")
    if username:
        try:
            user = pwd.getpwnam(username)
            if user is not None and os.path.isdir(user.pw_dir):
                return user
        except KeyError:
            pass
    if hasattr(os, "getuid") and hasattr(pwd, "getpwuid"):
        try:
            user = pwd.getpwuid(os.getuid())
            if user.pw_uid >= 1000 and os.path.isdir(user.pw_dir):
                return user
        except KeyError:
            pass
    return next(
        (p for p in sorted(pwd.getpwall(), key=lambda p: p.pw_uid)
         if p.pw_uid >= 1000 and os.path.isdir(p.pw_dir)),
        None,
    )


def xdg_download_dir(home_dir: str) -> str:
    try:
        with open(os.path.join(home_dir, ".config", "user-dirs.dirs"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("XDG_DOWNLOAD_DIR="):
                    match = re.fullmatch(r'"((?:[^"\\]|\\.)*)"\s*(?:#.*)?', line.split("=", 1)[1].strip())
                    if not match:
                        break
                    body = match[1]
                    home_relative = body == "$HOME" or body.startswith("$HOME/")
                    if home_relative:
                        body = body[5:]
                    value = re.sub(r'\\([\\"$`])', lambda m: m[1], body)
                    if home_relative:
                        value = home_dir + value
                    if value and "\0" not in value and os.path.isabs(value):
                        return os.path.normpath(value)
                    break
    except (OSError, UnicodeError):
        pass
    return os.path.join(home_dir, "Downloads")


# ── Updater ────────────────────────────────────────────────────────────────────

class Updater:
    """Checks GitHub releases and downloads an asset for the user to install.

    The plugin supplies its own release URL, User-Agent, log prefix, directory
    and logger; everything else is common to both.
    """

    def __init__(self, *, releases_url: str, user_agent: str, log_prefix: str,
                 plugin_dir: str, logger, asset_name=None, archive_root=None, archive_files=()):
        self.releases_url = releases_url
        self.user_agent = user_agent
        self.log_prefix = log_prefix
        self.plugin_dir = plugin_dir
        self.logger = logger
        self._ssl_ctx: ssl.SSLContext | None = None
        self.asset_name = asset_name
        self.archive_root = archive_root
        self.archive_files = tuple(archive_files)
        self._checked_asset = None
        self._check_generation = 0
        self._check_lock = threading.Lock()
        self._download_lock = threading.Lock()

    # ---- logging ----------------------------------------------------- #

    def _info(self, message: str) -> None:
        self.logger.info(f"{self.log_prefix} {message}")

    def _warning(self, message: str) -> None:
        self.logger.warning(f"{self.log_prefix} {message}")

    def _error(self, message: str) -> None:
        self.logger.error(f"{self.log_prefix} {message}")

    # ---- TLS ---------------------------------------------------------- #

    def ssl_context(self) -> ssl.SSLContext:
        if self._ssl_ctx is not None:
            return self._ssl_ctx

        ctx = ssl.create_default_context()
        if ctx.cert_store_stats().get("x509_ca"):
            self._info("TLS: using the default trust store")
            self._ssl_ctx = ctx
            return ctx

        # Prefer the OS bundle (it gets security updates) over the copy of
        # certifi the frozen loader unpacks into a temp dir that changes on
        # every restart.
        candidates = list(CA_BUNDLES)
        try:
            import certifi
            candidates.append(certifi.where())
        except Exception:
            pass

        for path in candidates:
            try:
                if not path or not os.path.exists(path):
                    continue
                ctx.load_verify_locations(cafile=path)
                if ctx.cert_store_stats().get("x509_ca"):
                    self._info(
                        f"TLS: default store was empty, loaded CA bundle {path} "
                        f"({ctx.cert_store_stats()['x509_ca']} certs)")
                    self._ssl_ctx = ctx
                    return ctx
            except OSError as exc:
                self._warning(f"TLS: cannot load {path}: {exc}")

        # Verification stays on. Failing loudly beats silently trusting
        # anything, since this runs as root and what comes back is installed
        # or executed.
        self._error("TLS: no usable CA bundle found, downloads will fail to verify")
        self._ssl_ctx = ctx
        return ctx

    # ---- HTTP --------------------------------------------------------- #

    def open_url(self, url: str, timeout: int, headers: dict | None = None):
        """urlopen with certificate verification left on and the host checked."""
        request = urllib.request.Request(
            checked_url(url),
            headers=headers or {"User-Agent": self.user_agent},
        )
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self.ssl_context()), CheckedRedirect())
        return opener.open(request, timeout=timeout)

    def _stream_to(self, url, out, timeout, *, headers=None, limit=None):
        """Stream a URL into a file object, aborting past the size ceiling.

        Returns the number of bytes written. The caller is responsible for
        removing a partial file, since a truncated archive that looks complete
        is worse than no archive at all.
        """
        limit = MAX_DOWNLOAD_BYTES if limit is None else limit
        written = 0
        deadline = time.monotonic() + timeout
        with self.open_url(url, timeout=min(timeout, HTTP_IDLE_TIMEOUT_S), headers=headers) as resp:
            # HTTPResponse.read(n) can wait indefinitely for n bytes while a
            # peer trickles data inside each socket timeout. read1 checks our
            # overall deadline between underlying reads instead.
            read = resp.read1
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("download exceeded its time limit")
                chunk = read(min(64 * 1024, limit - written + 1))
                if time.monotonic() > deadline:
                    raise TimeoutError("download exceeded its time limit")
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise ValueError("download exceeded the size limit")
                out.write(chunk)
        return written

    def download_to(self, url: str, out, timeout: int) -> int:
        return self._stream_to(url, out, timeout)

    # ---- RPC bodies ---------------------------------------------------- #

    def plugin_version(self) -> str:
        """The installed version, as the loader itself understands it.

        DECKY_PLUGIN_VERSION is authoritative: PluginWrapper takes the version
        from package.json, never from plugin.json, so that is the number Decky
        shows in its own plugin list. Reading it here means the panel and the
        loader can never disagree about what is installed.

        Falls back to parsing plugin.json, which is what keeps this module
        importable by the test suites with no loader in the environment.
        """
        version = os.environ.get("DECKY_PLUGIN_VERSION", "")
        if version:
            return version
        try:
            with open(os.path.join(self.plugin_dir, "plugin.json")) as f:
                manifest = json.load(f)
                version = manifest.get("version") if isinstance(manifest, dict) else None
                return version if isinstance(version, str) and version else "0.0.0"
        except (OSError, ValueError):
            return "0.0.0"

    def check(self) -> dict:
        """Ask GitHub for the latest release. Never raises."""
        current = self.plugin_version()
        # QAM may reopen and start a fresh check before an old request finishes.
        # Only the newest check may authorize its advertised download.
        with self._check_lock:
            self._check_generation += 1
            generation = self._check_generation
            self._checked_asset = None
        try:
            body = io.BytesIO()
            self._stream_to(self.releases_url, body, timeout=10, headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": self.user_agent,
            }, limit=MAX_RELEASE_METADATA_BYTES)
            data = json.loads(body.getvalue())
            tag = str(data.get("tag_name", ""))
            if not tag:
                return {"current_version": current,
                        "error": data.get("message", "Unexpected GitHub API response")}
            latest = tag[1:] if tag.startswith(("v", "V")) else tag
            available = version_key(latest) > version_key(current)
            expected = self.asset_name(latest) if self.asset_name else None
            asset = next((a for a in data.get("assets", [])
                          if (a.get("name") == expected if expected else
                              str(a.get("name", "")).endswith(".zip"))), None)
            checked_asset = None
            if asset:
                url = checked_url(asset.get("browser_download_url", ""))
                if expected:
                    repo = urllib.parse.urlparse(self.releases_url).path.split("/repos/")[-1].split("/releases")[0]
                    parsed = urllib.parse.urlparse(url)
                    expected_path = f"/{repo}/releases/download/{tag}/{expected}"
                    if parsed.hostname != "github.com" or urllib.parse.unquote(parsed.path) != expected_path:
                        raise ValueError("release asset URL does not belong to this repository")
                checked_asset = (url, asset["name"])
            if available and expected and not asset:
                return {"current_version": current, "latest_version": latest,
                        "update_available": True, "error": f"Release has no installable {expected} asset yet"}
            info = {
                "current_version":  current,
                "latest_version":   latest,
                "update_available": available,
                "download_url":     asset.get("browser_download_url") if asset else None,
                "asset_name":       asset.get("name") if asset else None,
            }
            with self._check_lock:
                if generation != self._check_generation:
                    return {"current_version": current, "error": "update check superseded by a newer request"}
                self._checked_asset = checked_asset
            return info
        except Exception as e:
            self._error(f"check_for_updates: {e}")
            return {"current_version": current, "error": str(e)}

    def download(self, download_url: str, asset_name: str) -> dict:
        """Fetch a release asset into the desktop user's download directory."""
        if not self._download_lock.acquire(blocking=False):
            return {"success": False, "error": "a download is already in progress"}
        try:
            return self._download(download_url, asset_name)
        finally:
            self._download_lock.release()

    def _download(self, download_url, asset_name):
        tmp = None
        try:
            checked_url(download_url)
            if not isinstance(asset_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.zip", asset_name):
                raise ValueError("invalid release asset filename")
            if self.asset_name and self._checked_asset != (download_url, asset_name):
                raise ValueError("check for updates before downloading this release asset")
            user = real_user()
            downloads_dir = xdg_download_dir(user.pw_dir) if user else "/home/deck/Downloads"
            created_dir = not os.path.isdir(downloads_dir)
            os.makedirs(downloads_dir, exist_ok=True)
            # If we had to create it, it is owned by root and the user would not
            # be able to manage their own download directory.
            if user and created_dir and hasattr(os, "geteuid") and os.geteuid() == 0:
                os.chown(downloads_dir, user.pw_uid, user.pw_gid)
            dest = os.path.join(downloads_dir, asset_name)

            with tempfile.NamedTemporaryFile(mode="wb", dir=downloads_dir,
                                             prefix=".decky-update-", suffix=".part", delete=False) as f:
                tmp = f.name
                written = self.download_to(download_url, f, timeout=60)
                f.flush()
                os.fsync(f.fileno())

            with zipfile.ZipFile(tmp) as archive:
                if len(archive.namelist()) != len(set(archive.namelist())):
                    raise ValueError("duplicate release archive paths")
                if not archive.infolist() or sum(i.file_size for i in archive.infolist()) > MAX_DOWNLOAD_BYTES * 8:
                    raise ValueError("empty or oversized release archive")
                for entry in archive.infolist():
                    path = entry.orig_filename
                    parts = path.rstrip("/").split("/")
                    if (path != entry.filename or "\\" in path or path.startswith("/")
                            or any(p in ("", ".", "..") for p in parts) or ":" in path):
                        raise ValueError("unsafe release archive path")
                    if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("release archive contains a symbolic link")
                if archive.testzip() is not None:
                    raise ValueError("release archive failed its CRC check")
                if self.archive_root:
                    root = self.archive_root + "/"
                    if any(not name.startswith(root) for name in archive.namelist()):
                        raise ValueError("release archive contains files outside the plugin directory")
                    required = ("main.py", "plugin.json", "package.json", "dist/index.js") + self.archive_files
                    if any(root + name not in archive.namelist() for name in required):
                        raise ValueError("release archive is missing plugin runtime files")
                    manifest = json.loads(archive.read(root + "plugin.json"))
                    package = json.loads(archive.read(root + "package.json"))
                    if package.get("version") != manifest.get("version") or self.asset_name(manifest.get("version")) != asset_name:
                        raise ValueError("release archive version does not match its filename")

            # Written as root, so hand it back to the desktop user - otherwise
            # they cannot move or delete their own download.
            if user and hasattr(os, "geteuid") and os.geteuid() == 0:
                os.chown(tmp, user.pw_uid, user.pw_gid)
            os.chmod(tmp, 0o644)
            os.replace(tmp, dest)

            self._info(f"update downloaded to {dest} ({written} bytes)")
            return {"success": True, "path": dest}
        except Exception as e:
            self._error(f"perform_update: {e}")
            # Never leave a truncated or oversized file behind.
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return {"success": False, "error": str(e)}
