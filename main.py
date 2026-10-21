# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""DeckyVibranceHDR - saturation control for both SDR and HDR content.

Three gamescope interfaces, picked per display rather than per device.

SDR has a dial of its own: GAMESCOPE_COLOR_SDR_GAMUT_WIDENESS moves the source
colorimetry away from Rec.709. Where its neutral sits and how far it goes depend
on which branch of buildSDRColorimetry() the panel takes, so the slider is
mapped onto that branch instead of assuming one - see the constants below.

Where the atom gives out, GAMESCOPE_COLOR_LOOK_G22 takes over: a .cube file
applied to SDR content. Only ever needed on a standard-gamut panel, where cfit()
clamps the atom long before the slider ends.

HDR has no dial at all. buildPQColorimetry() zeroes the gamut remap on purpose,
but the PQ path does accept a look, so GAMESCOPE_COLOR_LOOK_PQ gets the same
treatment. See vibrance_lut.py for why the maths has to happen in ICtCp.

Nothing here is tied to a particular machine, but it is tied to the *built-in*
panel: the display is measured, and an external one is left alone.
"""

import asyncio
import functools
import json
import math
import re
import os
import shutil
import struct
import stat
import subprocess
import sys
import threading
import tempfile
import time
from collections import OrderedDict

import decky
from settings import SettingsManager

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

# Not `updater`: the loader aliases its own decky_loader.updater to that bare
# name before we are imported, and sys.modules wins over sys.path.
from lego_updater import Updater  # noqa: E402
import vibrance_edid  # noqa: E402
import vibrance_lut  # noqa: E402

LOG = "[deckyvibrancehdr]"

GITHUB_RELEASES_URL = (
    "https://api.github.com/repos/Rayekkk/DeckyVibranceHDR/releases/latest"
)

updater = Updater(
    releases_url=GITHUB_RELEASES_URL,
    user_agent="DeckyVibranceHDR",
    log_prefix=LOG,
    plugin_dir=PLUGIN_DIR,
    logger=decky.logger,
    asset_name=lambda version: f"DeckyVibranceHDR-{version}.zip",
    archive_root="DeckyVibranceHDR",
    archive_files=("lego_updater.py", "vibrance_lut.py", "vibrance_edid.py"),
)

ATOM_SDR_WIDENESS = "GAMESCOPE_COLOR_SDR_GAMUT_WIDENESS"
ATOM_LOOK_PQ = "GAMESCOPE_COLOR_LOOK_PQ"
ATOM_LOOK_G22 = "GAMESCOPE_COLOR_LOOK_G22"
ATOM_EDID_PATH = "GAMESCOPE_DISPLAY_EDID_PATH"
ATOM_IS_EXTERNAL = "GAMESCOPE_DISPLAY_IS_EXTERNAL"

# gamescope runs an X server per XWayland instance and listens on the root
# window of every one of them: handle_property_notify() reads the atom off
# whichever ctx the event arrived on, with no check for a primary. So a write to
# any of them takes effect, which is why vibrantDeck works while defaulting to
# :1 and this plugin works on :0.
#
# What cannot be assumed is that a given server is there at all, and xprop exits
# 0 whether or not the atom it was asked for exists - so its status says
# nothing. The server is picked by looking for gamescope's own atoms, favouring
# the one carrying the fuller surface because that is also where the readbacks
# live. On the Legion Go 2 that is :0 with 45 against 7 on :1.
X11_SOCKET_DIR = "/tmp/.X11-unix"
DISPLAY_FALLBACK = (":0", ":1")
DISPLAY = ":0"          # both replaced at startup by _pick_display()
DISPLAYS = DISPLAY_FALLBACK
_last_display_scan = None  # what _pick_display last logged

# gamescope's own BIsWideGamut test, which is what decides between the two
# branches of buildSDRColorimetry - and therefore which mapping below applies.
# Measured: Legion Go 2 OLED is 0.6836/0.3154 and passes; Legion Go S LCD is
# 0.6523/0.3408 and fails on y after passing x by a hair.
#
# gamescope runs the test on the EDID's primaries - or on Rec.709 when the
# EDID has no colorimetry (red x of zero) - unless one of its bundled
# display scripts supplies colorimetry of its own - a handful of devices
# (Steam Deck LCD, Zotac Zone, OXP F1 and GPD Win 4 at the time of writing).
# The plugin reads the EDID, which is what gamescope uses everywhere else.
# Reproducing gamescope's Lua matching is deliberately not attempted: it would
# turn a panel-driven plugin into a per-device one. Where a script's primaries
# land on the other side of the test than the EDID's, 100% stays neutral (it is
# gamescope's own default) but the steps above it follow the other branch.
WIDE_GAMUT_RED_X = 0.650
WIDE_GAMUT_RED_Y = 0.320

# The number on the slider means the same thing on every machine: 1.0 is
# neutral, up is more saturated, and there is nothing below neutral. What it
# maps to does not, because buildSDRColorimetry() has two branches whose zero
# points disagree.
#
# Wide gamut: 0.0 is Rec.709, 1.0 the panel's native gamut, and nothing clamps
# the lerp - so neutral is 1.0 and there is room well above it.
#
# Anything else: 0.0 is native, 0.5 a generic wide gamut smoothly mapped, 1.0
# the same harshly mapped, and cfit() clamps there. Neutral is 0.0 and the whole
# useful range is one unit wide. That is the branch a Legion Go S takes, and the
# one vibrantDeck has been riding on a Steam Deck LCD all along.
SDR_MIN = 1.0
SDR_MAX_WIDE = 3.0
SDR_SOFT_WIDE = 2.0

# Where the atom's own neutral sits, and how much of the slider it can carry.
# Both replaced at startup once the panel is known.
SDR_NEUTRAL_ATOM = 1.0
SDR_ATOM_SPAN = 2.0

# gamescope's own "nobody has set this": both branches of buildSDRColorimetry
# turn a negative wideness into that branch's native gamut. Writing it back is
# the only value that means "off" on either kind of panel - 1.0 is neutral on
# one branch and the harshest setting there is on the other.
SDR_UNSET = -1.0

# Only ever reached on a standard-gamut panel. There cfit() stops responding at
# atom 1.0, which is the slider's 200%, so the top third has nothing left to
# push. The remainder is carried by a look on the SDR path -
# GAMESCOPE_COLOR_LOOK_G22, the twin of the atom the HDR half already uses, in
# the gamma 2.2 domain. A wide-gamut panel never gets here: its atom span is
# the whole slider, so the remainder is always zero and no look is written.
SDR_MAX_STANDARD = 3.0

# Expressed as a rate rather than a ceiling on purpose: it means a given slider
# position keeps asking for the same thing when the top of the range moves, so
# raising the maximum does not quietly weaken every setting below it.
SDR_LOOK_PER_UNIT = 0.5
# Same ceiling as the HDR half, for the same reason: it is the same code and it
# clips in the same place. With the slider ending at 300% the rate reaches this
# exactly at the top, so it is a bound rather than a cut.
SDR_LOOK_CEILING = 1.5

# Where the LUT starts hard-clipping channels, which is the same look value the
# HDR half warns at - the maths is identical, only the transfer differs.
SDR_SOFT_STANDARD = 2.6

# HDR is our own LUT, so the ceiling is set by where the maths stops paying for
# itself rather than by gamescope. Colours already at the gamut boundary clip
# from the first step; past about 1.3 the clipping becomes visible and detail in
# saturated areas goes with it.
HDR_MIN = 1.0
HDR_MAX = 1.5
HDR_SOFT_LIMIT = 1.3

# Neutral, and now also the bottom of both ranges.
SAT_DEFAULT = 1.0

# 33 is the usual size for a creative LUT and costs about a third of a second to
# build. That is far too slow to redo on every tick of a slider, so a change is
# only acted on once the value has stopped moving.
LUT_SIZE = 33
DEBOUNCE_S = 0.25

# The SDR half is one float and used not to need this, but at 0.01 a drag across
# its range is 200 calls. Each one spawned an xprop and rewrote the settings
# file, unserialized, so writes could land out of order and leave the panel on a
# stale value while the UI showed the new one. Short enough to stay invisible.
SDR_COALESCE_S = 0.05

# A look was seen missing after a suspend on a device, though in gamescope's
# source only a property change replaces one; reapplying is cheap insurance.
# CLOCK_MONOTONIC stops while the machine is asleep and CLOCK_BOOTTIME does
# not, so the gap between them is a suspend cycle - no dbus, no root, no
# Steam client. gamescope is not necessarily ready the moment we notice, and can
# clobber us during its own startup, so the values go back several times.
SUSPEND_POLL_S = 1.0
SUSPEND_SLACK_S = 2.0
REASSERT_DELAYS_S = (0.3, 1.0, 2.5, 5.0, 8.0)

# Decky brings plugins up as a system service, which can beat the session to it.
# Finding no gamescope at startup is a race, not a verdict, so we keep looking.
GAMESCOPE_RETRY_FIRST_S = 1.0
GAMESCOPE_RETRY_MAX_S = 15.0

# It publishes its atoms as it comes up, so finding some is not the same as it
# being ready - anything written into a half-built session gets thrown away when
# it finishes. Observed on a Legion Go S: 20 atoms one second in, 44 once up.
GAMESCOPE_SETTLE_S = 1.0
GAMESCOPE_SETTLE_TRIES = 12

# A settings file that cannot be opened is retried this often; one that
# opens but cannot parse is set aside instead.
SETTINGS_RETRY_S = 1.0

# One disagreement is gamescope resetting the atom; a run of them is another
# plugin holding the other end. Only the second is worth telling the user about.
CONFLICT_STRIKES = 3
WATCH_INTERVAL_S = 4.0

# Docking swaps the panel without restarting anything, so the display is
# re-read every PANEL_CHECK_INTERVAL_S. The expensive checks - another plugin
# on a second X server, and whether our looks are still the ones loaded - run
# on every SLOW_CHECK_EVERY-th watch tick (about every 16 s) rather than
# spawning an xprop every few seconds.
PANEL_CHECK_INTERVAL_S = 5.0
SLOW_CHECK_EVERY = 4

DEFAULT_SETTINGS = {
    "sdr_enabled": False,
    "hdr_enabled": False,
    "sdr_saturation": SAT_DEFAULT,
    "hdr_saturation": SAT_DEFAULT,
}

# Decky's manager creates an empty file on first install and suppresses read
# errors. Record absence before construction, so a file that existed and later
# goes missing (an unmounted card, say) is retried instead of replaced.
try:
    os.stat(os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json"))
    _settings_new = False
except FileNotFoundError:
    _settings_new = True
except OSError:
    _settings_new = False

class _DeferredSettingsManager(SettingsManager):
    def read(self):
        # Decky's constructor calls read() for existing files. Defer that
        # blocking, error-suppressing read until _main can validate the file
        # and report/retry failures through _read_settings.
        pass


settings = _DeferredSettingsManager(
    name="settings",
    settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR,
)

# SettingsManager and the LUT files are both reached through the default thread
# pool.  Slider work and toggle RPCs can overlap there, so neither resource may
# rely on event-loop ordering once it has been offloaded.
_settings_lock = threading.RLock()
_lut_file_lock = threading.RLock()
# Metadata only: the two HDR and two SDR slots already contain the LUT text.
_look_file_cache = OrderedDict()
_lut_writes_disabled = False
_xprop_timeout = 3.0
UNLOAD_XPROP_TIMEOUT_S = 0.25


def _read_settings():
    global _settings_new
    path = getattr(settings, "path", None)
    if path is None:
        settings.read()
        return
    with _settings_lock:
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                                 | getattr(os, "O_CLOEXEC", 0))
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("settings must be a regular file")
            with os.fdopen(descriptor, encoding="utf-8") as source:
                descriptor = None
                text = source.read()
        except FileNotFoundError:
            if not _settings_new:
                raise
            text = ""
        except UnicodeDecodeError as exc:
            raise SettingsUnreadable(f"not UTF-8 text ({exc.reason})") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        # An empty file holds nothing to protect. Decky creates one on first
        # install, and 1.0.0 truncated before writing, so an interrupted first
        # save or power loss left one behind that could never load again.
        try:
            values = {} if not text.strip() else json.loads(text)
        except (ValueError, RecursionError) as exc:  # deep nesting overflows the parser
            raise SettingsUnreadable(f"invalid JSON: {exc}") from exc
        if not isinstance(values, dict):
            raise SettingsUnreadable("settings must contain a JSON object")
        settings.settings = values
        _settings_new = False


class SettingsUnreadable(ValueError):
    """The file was read but its content can never parse; retrying cannot help."""


def _set_aside_settings() -> str:
    """Keep unparseable settings beside the original and let startup continue.

    Nothing is deleted: the user can still recover values by hand, and the
    next read sees no file and starts from defaults.
    """
    global _settings_new
    with _settings_lock:
        aside = f"{settings.path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        os.replace(settings.path, aside)
        _settings_new = True
        return aside


def _offload(fn, *args):
    return asyncio.get_event_loop().run_in_executor(None, fn, *args)


def _boottime() -> float:
    """Seconds since boot, counting time spent suspended.

    Falls back to the monotonic clock where CLOCK_BOOTTIME does not exist, which
    leaves the two readings identical and the suspend detector simply idle.
    """
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()


def _clamp(v: float, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError("saturation must be a finite number")
    return min(max(float(v), lo), hi)


def _setting(key, default):
    """Treat each damaged field independently; old numeric ranges still migrate."""
    try:
        value = settings.getSetting(key, default)
        if isinstance(default, bool):
            return value if isinstance(value, bool) else default
        return _clamp(value, SDR_MIN if key.startswith("sdr") else HDR_MIN,
                      SDR_MAX_WIDE if key.startswith("sdr") else HDR_MAX)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return default


# ── talking to gamescope ───────────────────────────────────────────────────────

# Steam exports LD_LIBRARY_PATH pointing at its own bundled libraries and the
# plugin inherits it, so a system binary spawned from here can load Steam's copy
# of libreadline or libc and die on a missing symbol. Everything we spawn is a
# system tool, so the loader variables go.
_LOADER_VARS = ("LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT")


def _system_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _LOADER_VARS}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env["DISPLAY"] = DISPLAY
    env.update(extra)
    return env


def _run_xprop(command, **kwargs):
    if _native_lut_python() is not None:
        # Absolute host path plus inheritable-FD policy enables posix_spawn,
        # avoiding a fork of the large emulated Decky process on every atom.
        command = ["/proc/self/root/usr/bin/xprop", *command[1:]]
        kwargs["close_fds"] = False
    return subprocess.run(command, **kwargs)


def _float_to_cardinal(value: float) -> int:
    """gamescope stores these floats bit-cast into a CARDINAL."""
    return struct.unpack("I", struct.pack("f", float(value)))[0]


def _cardinal_to_float(raw):
    if raw is None:
        return None
    try:
        value = struct.unpack("f", struct.pack("I", raw & 0xFFFFFFFF))[0]
        return value if math.isfinite(value) else None
    except struct.error:
        return None


def _xprop(*args, display=None):
    """stdout of `xprop -root ...`, or None when xprop could not run or timed out.

    xprop exits 0 with nothing useful when an atom is absent and prints nothing
    when the server is gone, so "" is an answer. None is not: it says nothing
    about gamescope, only that this one read did not complete.
    """
    try:
        return _run_xprop(
            ["xprop", "-root", *args],
            capture_output=True, text=True, timeout=_xprop_timeout,
            env=_system_env() if display is None else _system_env(DISPLAY=display),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _xprop_set(name: str, fmt: str, value: str) -> bool:
    try:
        _run_xprop(
            ["xprop", "-root", "-f", name, fmt, "-set", name, value],
            capture_output=True, timeout=_xprop_timeout, env=_system_env(), check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _read_int_atom(name: str):
    """A plain CARDINAL, not one of the floats gamescope bit-casts."""
    return _parse_int_atom(_xprop(name) or "")


def _parse_int_atom(out):
    if "= " not in out:
        return None
    try:
        return int(out.split("= ", 1)[1].strip())
    except ValueError:
        return None


def _display_is_external():
    """True when gamescope is driving something other than the built-in panel.

    gamescope publishes this itself, so there is no guessing from the EDID. A
    missing atom means an older build and is read as internal: withdrawing the
    whole plugin over an absent property would be worse than the alternative.
    """
    value = _read_int_atom(ATOM_IS_EXTERNAL)
    return None if value is None else bool(value)


def _read_float_atom(name: str):
    raw = _parse_int_atom(_xprop(name) or "")
    return None if raw is None else _cardinal_to_float(raw)


def _write_float_atom(name: str, value: float) -> bool:
    return _xprop_set(name, "32c", str(_float_to_cardinal(value)))


def _write_string_atom(name: str, value: str) -> bool:
    """UTF8_STRING, matching what gamescope writes for its own path atoms."""
    return _xprop_set(name, "8u", value)


def _read_string_atom(name: str):
    return _parse_string_atom(_xprop(name) or "")


def _parse_string_atom(out):
    if "= " not in out:
        return None
    value = out.split("= ", 1)[1].strip()
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        value = value[1:-1]
        # xprop quotes backslashes/quotes, newlines/tabs and octal bytes.
        # Decode only that grammar; unicode_escape would damage UTF-8 paths.
        raw = re.sub(rb'\\([0-7]{3}|[\\"nt])',
                     lambda match: (bytes([int(match[1], 8)]) if len(match[1]) == 3
                                    else {b"n": b"\n", b"t": b"\t"}.get(match[1], match[1])),
                     value.encode("utf-8"))
        value = raw.decode("utf-8", errors="surrogateescape")
    if "\0" in value:
        return None
    return value or None


def _read_edid(path):
    """Read regular EDID metadata without waiting for a FIFO/device producer."""
    descriptor = None
    try:
        # Opening before fstat avoids a stat/open race; O_NONBLOCK prevents an
        # atom pointing to a named pipe from freezing startup or monitoring.
        # Follow regular-file symlinks, including normal sysfs EDID paths.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                             | getattr(os, "O_CLOEXEC", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None  # fdopen owns and closes it from here.
            return source.read(128 * 256 + 1)
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


DRM_SYSFS = "/sys/class/drm"


def _builtin_panel_edid_path():
    """The built-in panel's EDID as the kernel reports it, or None.

    gamescope publishes GAMESCOPE_DISPLAY_EDID_PATH only when its session sets
    GAMESCOPE_PATCHED_EDID_FILE (update_edid_prop in steamcompmgr.cpp); without
    it gamescope reads this same EDID from DRM. Built-in panels sit on eDP or
    DSI connectors, which external outputs do not use.
    """
    try:
        names = sorted(os.listdir(DRM_SYSFS))
    except OSError:
        return None
    for name in names:
        if "-eDP-" not in name and "-DSI-" not in name:
            continue
        try:
            with open(os.path.join(DRM_SYSFS, name, "status"), encoding="ascii") as status:
                if status.read().strip() != "connected":
                    continue
        except OSError:
            continue
        return os.path.join(DRM_SYSFS, name, "edid")
    return None


def _file_identity(path):
    """Detect replacement, edits and deletion at a stable pathname."""
    if not path:
        return None
    try:
        info = os.stat(path)
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    except OSError:
        return None


def _display_session_identity():
    """The local X socket identifies a session even at the same DISPLAY."""
    number = DISPLAY.removeprefix(":").split(".", 1)[0]
    identity = _file_identity(os.path.join(X11_SOCKET_DIR, f"X{number}"))
    return (DISPLAY, identity) if identity is not None else None


def _panel_supports_hdr(blob):
    """Whether the HDR half has anything to act on, or None if unknowable."""
    if blob is None:
        return None
    supported = vibrance_edid.supports_pq(blob)
    if supported is not None:
        decky.logger.info(
            f"{LOG} panel {'advertises' if supported else 'does not advertise'} "
            f"PQ in its CTA-861 block")
    return supported


def _panel_is_wide_gamut(blob):
    """gamescope's BIsWideGamut, applied to the EDID gamescope itself publishes.

    True, False, or None when the panel could not be identified - in which case
    the caller carries on rather than disabling a working control over a missing
    atom.
    """
    if blob is None:
        return None
    primary = vibrance_edid.red_primary(blob)
    if primary is None:
        return None
    red_x, red_y = primary
    wide = red_x > WIDE_GAMUT_RED_X and red_y < WIDE_GAMUT_RED_Y
    decky.logger.info(
        f"{LOG} panel red {red_x:.4f},{red_y:.4f} - "
        f"{'wide gamut' if wide else 'standard gamut'}")
    return wide


def _read_root_properties(display: str):
    return _xprop(display=display)


def _count_gamescope_atoms(display: str) -> int:
    out = _read_root_properties(display) or ""
    return sum(1 for line in out.splitlines() if line.startswith("GAMESCOPE"))


def _display_candidates():
    """Every X server on the box, read off its socket.

    Nothing about the numbering is fixed: how many XWayland servers gamescope
    starts is a command line option (--xwayland-count) and which numbers they
    get depends on what was already taken, so a desktop session holding :0
    pushes them along. Guessing a pair covers this device and little else.
    """
    try:
        names = sorted(name for name in os.listdir(X11_SOCKET_DIR)
                       if name.startswith("X") and name[1:].isdigit())
    except OSError:
        return DISPLAY_FALLBACK
    return tuple(f":{name[1:]}" for name in names) or DISPLAY_FALLBACK


def _pick_display():
    """Settle on an X server gamescope is actually behind, or None.

    gamescope publishes GAMESCOPE_DISPLAY_IS_EXTERNAL on its first server
    only, and docking is detected from that atom, so a server carrying it
    wins; between the rest, the fuller surface does.
    """
    global DISPLAY, DISPLAYS, _last_display_scan
    DISPLAYS = _display_candidates()
    best, best_rank = None, (False, 0)
    seen = []
    for candidate in DISPLAYS:
        lines = (_read_root_properties(candidate) or "").splitlines()
        count = sum(1 for line in lines if line.startswith("GAMESCOPE"))
        root = any(line.startswith(ATOM_IS_EXTERNAL + "(") for line in lines)
        seen.append(f"{candidate}: {count} gamescope atoms" + (", docking state" if root else ""))
        if count and (root, count) > best_rank:
            best, best_rank = candidate, (root, count)
    # Retried every few seconds while gamescope is away (Desktop Mode, say).
    if "; ".join(seen) != _last_display_scan:
        _last_display_scan = "; ".join(seen)
        decky.logger.info(f"{LOG} {_last_display_scan}")
    if best is None:
        return None
    DISPLAY = best
    return best


def _foreign_sdr_values():
    """The SDR atom as found on every X server we are not using.

    A write from any server takes effect, while a read only ever sees one of
    them - so two plugins on different servers fight over the picture without
    either seeing the other in the value it reads back. The atom also outlives
    the writer until gamescope restarts, so its mere presence proves nothing:
    the caller watches for it changing. None when a server could not be read.
    """
    found = []
    for candidate in DISPLAYS:
        if candidate == DISPLAY:
            continue
        out = _xprop(ATOM_SDR_WIDENESS, display=candidate)
        if out is None:
            return None
        if "= " in out:
            found.append((candidate, out.split("= ", 1)[1].strip()))
    return tuple(found)


# ── the looks ──────────────────────────────────────────────────────────────────

def _runtime_dir() -> str:
    path = (os.environ.get("DECKY_PLUGIN_RUNTIME_DIR")
            or decky.DECKY_PLUGIN_SETTINGS_DIR)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _sdr_split(value: float):
    """Split a slider position into (atom value, look saturation).

    The atom is free and instant, so it carries everything it can; whatever is
    left over goes to the look. On a wide-gamut panel there is never anything
    left over.
    """
    if value == SAT_DEFAULT:
        # gamescope's own "unset" is neutral on both branches of
        # buildSDRColorimetry, so 100% stays neutral on an unidentified panel.
        return SDR_UNSET, 1.0
    above = value - SAT_DEFAULT
    on_atom = min(above, SDR_ATOM_SPAN)
    remainder = max(0.0, above - SDR_ATOM_SPAN)
    look = min(1.0 + remainder * SDR_LOOK_PER_UNIT, SDR_LOOK_CEILING)
    return SDR_NEUTRAL_ATOM + on_atom, look


@functools.cache
def _interpreter_headers():
    """The ELF headers of the host Python and of the one running us.

    They cannot change while the plugin runs. An OSError is raised, not
    returned, so functools.cache keeps only a successful read and a passing
    error (too many open files, say) is retried on the next call.
    """
    with open("/proc/self/root/usr/bin/python3", "rb") as f:
        native = f.read(20)
    with open(sys.executable, "rb") as f:
        loader = f.read(20)
    return native, loader


def _native_lut_python():
    """Use ARM Python when Decky's frozen interpreter runs under x86 emulation."""
    try:
        native, loader = _interpreter_headers()
    except OSError:
        return None
    if (native[:5] == b"\x7fELF\x02" and native[5] == 1
            and int.from_bytes(native[18:20], "little") == 183
            and loader[:5] == b"\x7fELF\x02" and loader[5] == 1
            and int.from_bytes(loader[18:20], "little") == 62):
        return "/proc/self/root/usr/bin/python3"
    return None


def _generate_lut(saturation, size, encoding, source_white, cancelled):
    interpreter = _native_lut_python()
    if interpreter is None:
        return vibrance_lut.generate(saturation, size, encoding, source_white,
                                    cancelled=cancelled)
    # Same validation as generate(), before arguments become CLI strings.
    vibrance_lut.validate(saturation, size, encoding, source_white)
    if cancelled() or _lut_writes_disabled:
        raise vibrance_lut.BuildCancelled()
    env = _system_env()
    # PyInstaller's library path must not reach the system Python.
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    with subprocess.Popen(
            [interpreter, "-I", os.path.realpath(vibrance_lut.__file__),
             repr(saturation), str(size), encoding,
             repr(source_white[0]), repr(source_white[1])],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            close_fds=(os.name == "nt")) as worker:
        deadline = time.monotonic() + 30
        try:
            while True:
                if cancelled() or _lut_writes_disabled:
                    raise vibrance_lut.BuildCancelled()
                if time.monotonic() >= deadline:
                    raise ValueError("native LUT worker timed out")
                try:
                    output, error = worker.communicate(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if worker.returncode:
                raise ValueError("native LUT worker failed: " + error.decode(errors="replace")[:300])
            return output.decode("utf-8")
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.communicate()


def _write_look(saturation: float, slot: int,
                encoding: str = "pq", prefix: str = "hdr-look",
                source_white=vibrance_lut.D65, cancelled=lambda: False) -> str:
    """Build the .cube and return its path, or "" on failure.

    Two slots alternate rather than one file being rewritten. gamescope reloads
    when the atom changes, and while X does emit a PropertyNotify even for an
    identical value, a changing path removes any doubt about that - and leaves
    the previous LUT intact until the new one is complete.
    """
    path = os.path.join(_runtime_dir(), f"{prefix}-{slot}.cube")
    tmp = path + ".tmp"
    # A cancelled run_in_executor job keeps running. Serialising here prevents
    # it from sharing the fixed temporary name with its replacement.
    with _lut_file_lock:
        if _lut_writes_disabled or cancelled():
            return ""
        try:
            # Include types so invalid True/33.0 cannot hit valid 1.0/33
            # records and bypass generate()'s input validation.
            request = (type(saturation), saturation, type(LUT_SIZE), LUT_SIZE,
                       encoding, tuple(source_white))
            cached = _look_file_cache.get(path)
            if (cached is not None and cached[0] == request
                    and cached[1] == _file_identity(path)):
                if _lut_writes_disabled or cancelled():
                    return ""
                _look_file_cache.move_to_end(path)
                return path
            # Opened first: a folder that cannot be written fails here, before
            # the build, instead of after a full 33-cube on every retry.
            with open(tmp, "w") as f:
                text = _generate_lut(
                    saturation, LUT_SIZE, encoding, source_white, cancelled)
                if _lut_writes_disabled or cancelled():
                    raise vibrance_lut.BuildCancelled()
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            if _lut_writes_disabled or cancelled():
                os.unlink(tmp)
                return ""
            os.replace(tmp, path)
            identity = _file_identity(path)
            if identity is not None:
                _look_file_cache[path] = (request, identity)
                _look_file_cache.move_to_end(path)
                while len(_look_file_cache) > 4:
                    _look_file_cache.popitem(last=False)
            else:
                _look_file_cache.pop(path, None)
            return path
        except Exception as e:  # anything: the opened temporary file must go
            if not isinstance(e, vibrance_lut.BuildCancelled):
                decky.logger.error(f"{LOG} could not write look: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return ""


def _own_look_slot(path, prefix: str):
    """The slot number when *path* names one of this plugin's LUTs."""
    if not path:
        return None
    runtime = os.path.realpath(_runtime_dir())
    candidate = os.path.realpath(path)
    for slot in (0, 1):
        expected = os.path.join(runtime, f"{prefix}-{slot}.cube")
        if candidate == expected:
            return slot
    return None


def _wait_for_lut_writer() -> None:
    with _lut_file_lock:
        pass


# Default for Plugin._watch_once: read the other X servers there and then.
_PROBE_NOW = object()


# The SDR and HDR looks run through one code path. What differs is the atom,
# the transfer the LUT is encoded in, the LUT file prefix, and which Plugin
# attributes and state fields hold each look's bookkeeping - all named here,
# so no code builds those names itself. "error" is the <error>_error state
# field; "worker" keys the debounce and the worker queue.
_LOOKS = {
    "sdr": {"atom": ATOM_LOOK_G22, "encoding": "g22", "label": "SDR", "prefix": "sdr-look",
            "path": "_sdr_look_path", "baseline": "_sdr_look_baseline",
            "slot": "_sdr_look_slot", "generation": "_sdr_look_generation",
            "applied": "sdr_look_applied", "saturation": "sdr_look_saturation",
            "generating": "sdr_generating", "error": "sdr_look", "worker": "sdr_look"},
    "hdr": {"atom": ATOM_LOOK_PQ, "encoding": "pq", "label": "HDR", "prefix": "hdr-look",
            "path": "_hdr_look_path", "baseline": "_hdr_look_baseline",
            "slot": "_look_slot", "generation": "_hdr_generation",
            "applied": "hdr_applied", "saturation": "hdr_applied_saturation",
            "generating": "generating", "error": "hdr", "worker": "hdr"},
}


class Plugin:
    _state = {
        "ready": False,
        "reason": "starting up",
        "sdr_enabled": False,
        "hdr_enabled": False,
        "sdr_saturation": SAT_DEFAULT,
        "hdr_saturation": SAT_DEFAULT,
        "sdr_applied": False,
        "sdr_look_applied": False,
        "hdr_applied": False,
        "hdr_applied_saturation": None,
        "sdr_look_saturation": None,
        "sdr_error": "",
        "sdr_look_error": "",
        "hdr_error": "",
        "settings_error": "",
        "settings_notice": "",  # a file set aside this session, shown until reload
        "settings_loaded": False,
        "generating": False,
        "sdr_generating": False,
        "conflict": "",
        "external_display": False,
        "hdr_supported": True,
        "sdr_min": SDR_MIN,
        "sdr_max": SDR_MAX_WIDE,
        "sdr_soft_limit": SDR_SOFT_WIDE,
        "hdr_min": HDR_MIN,
        "hdr_max": HDR_MAX,
        "hdr_soft_limit": HDR_SOFT_LIMIT,
    }
    _sdr_baseline = None        # whatever the atom held before we touched it
    _last_sdr_written = None    # to notice someone else writing the same atom
    _last_sdr_attempted = None  # the newest write, even one reported failed
    _look_slot = 0
    _hdr_generation = 0         # debounce token
    _sdr_generation = 0         # ditto, much shorter
    _sdr_look_slot = 0
    _sdr_look_generation = 0
    _sdr_look_path = None
    _sdr_look_baseline = None
    _sdr_white_point = vibrance_lut.D65
    _hdr_look_path = None
    _hdr_look_baseline = None
    _reassert_strikes = 0       # consecutive times the atom disagreed with us
    _slow_checks = 0            # ticks since the last of the expensive checks
    _panel_cache_key = None
    _panel_cache_complete = False
    _mutation_lock = asyncio.Lock()
    _unloading = False
    _tasks = []
    _workers = {}
    _jobs = {}
    _due = {}
    _save_generation = 0
    _settings_dirty = False
    _settings_loaded = False
    _foreign_sdr_seen = None    # SDR atom on the other X servers, last look
    _foreign_active_ticks = 0   # slow ticks left on the cross-server warning
    _look_failures = {"sdr": 0, "hdr": 0}  # consecutive failed builds or writes
    _disconnected = False
    _session_identity = None
    _connect_ours = ()          # our SDR values, kept until a connect succeeds
    _reassert_generation = 0
    _resume_event = None

    # ── exported ───────────────────────────────────────────────────────────────

    async def get_state(self) -> dict:
        return dict(Plugin._state)

    async def get_version(self) -> dict:
        return {"version": updater.plugin_version()}

    async def check_for_updates(self) -> dict:
        # The updater already offers only the exact DeckyVibranceHDR-<v>.zip.
        return await _offload(updater.check)

    async def perform_update(self, download_url: str, asset_name: str) -> dict:
        return await _offload(updater.download, download_url, asset_name)

    async def set_sdr_enabled(self, enabled: bool, saturation=None) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if saturation is not None:
            saturation = _clamp(saturation, SDR_MIN, Plugin._state["sdr_max"])
        async with Plugin._mutation_lock:
            if Plugin._unloading:
                return dict(Plugin._state)
            if not Plugin._settings_loaded:
                raise RuntimeError("settings have not loaded; retry after startup")
            if saturation is not None:
                Plugin._state["sdr_saturation"] = saturation
            if enabled and Plugin._state["external_display"]:
                if saturation is not None:
                    self._schedule_save()
                return dict(Plugin._state)
            if (enabled and not Plugin._state["sdr_enabled"]
                    and not Plugin._state["sdr_applied"] and Plugin._last_sdr_attempted is None
                    and not Plugin._disconnected):
                # With a write of ours possibly on the atom, it is not a
                # baseline; a failed read keeps the one already known.
                live = await _offload(_read_float_atom, ATOM_SDR_WIDENESS)
                if live is not None:
                    Plugin._sdr_baseline = live
            Plugin._state["sdr_enabled"] = enabled
            if not enabled:
                Plugin._invalidate_sdr_jobs()
            self._schedule_save()
            if enabled and not Plugin._disconnected:
                await _offload(Plugin._apply_sdr)
                self._schedule_look("sdr")
            elif not enabled:
                await _offload(Plugin._release_sdr)
            return dict(Plugin._state)

    async def set_hdr_enabled(self, enabled: bool, saturation=None) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if saturation is not None:
            saturation = _clamp(saturation, HDR_MIN, HDR_MAX)
        async with Plugin._mutation_lock:
            if Plugin._unloading:
                return dict(Plugin._state)
            if not Plugin._settings_loaded:
                raise RuntimeError("settings have not loaded; retry after startup")
            if saturation is not None:
                Plugin._state["hdr_saturation"] = saturation
            if enabled and (Plugin._state["external_display"]
                            or not Plugin._state["hdr_supported"]):
                if saturation is not None:
                    self._schedule_save()
                return dict(Plugin._state)
            Plugin._state["hdr_enabled"] = enabled
            if not enabled:
                Plugin._invalidate_hdr_jobs()
            self._schedule_save()
            if enabled and not Plugin._disconnected:
                self._schedule_look("hdr")
            elif not enabled:
                await _offload(Plugin._release_look, "hdr")
            return dict(Plugin._state)

    async def set_sdr_saturation(self, value: float) -> dict:
        async with Plugin._mutation_lock:
            if Plugin._unloading:
                return dict(Plugin._state)
            if not Plugin._settings_loaded:
                raise RuntimeError("settings have not loaded; retry after startup")
            value = _clamp(value, SDR_MIN, Plugin._state["sdr_max"])
            changed = value != Plugin._state["sdr_saturation"]
            Plugin._state["sdr_saturation"] = value
            if changed:
                self._schedule_save()
                self._schedule_sdr()
                self._schedule_look("sdr")
            else:
                self._retry_settings_save()
                target, _look = _sdr_split(value)
                if (Plugin._state["sdr_enabled"] and not Plugin._disconnected
                        and not Plugin._state["external_display"]
                        and (Plugin._state["sdr_error"] or not Plugin._state["sdr_applied"]
                             or Plugin._last_sdr_written != target)):
                    self._schedule_sdr()
                self._retry_pending_looks("sdr")
            return dict(Plugin._state)

    async def set_hdr_saturation(self, value: float) -> dict:
        async with Plugin._mutation_lock:
            if Plugin._unloading:
                return dict(Plugin._state)
            if not Plugin._settings_loaded:
                raise RuntimeError("settings have not loaded; retry after startup")
            value = _clamp(value, HDR_MIN, HDR_MAX)
            changed = value != Plugin._state["hdr_saturation"]
            Plugin._state["hdr_saturation"] = value
            if changed:
                self._schedule_save()
                self._schedule_look("hdr")
            else:
                self._retry_settings_save()
                self._retry_pending_looks("hdr")
            return dict(Plugin._state)

    async def reset(self) -> dict:
        async with Plugin._mutation_lock:
            if Plugin._unloading:
                return dict(Plugin._state)
            if not Plugin._settings_loaded:
                raise RuntimeError("settings have not loaded; retry after startup")
            Plugin._state["sdr_saturation"] = SAT_DEFAULT
            Plugin._state["hdr_saturation"] = SAT_DEFAULT
            Plugin._state["sdr_enabled"] = False
            Plugin._state["hdr_enabled"] = False
            Plugin._invalidate_sdr_jobs()
            Plugin._invalidate_hdr_jobs()
            self._schedule_save()
            await _offload(Plugin._release_sdr)
            await _offload(Plugin._release_look, "hdr")
            return dict(Plugin._state)

    # ── internals ──────────────────────────────────────────────────────────────

    @staticmethod
    def _set_error(component: str, message: str = "") -> None:
        """An operation may clear only its own error, never another half's."""
        Plugin._state[f"{component}_error"] = message
        errors = [Plugin._state.get(f"{part}_error", "")
                  for part in ("sdr", "sdr_look", "hdr", "settings")]
        if Plugin._state["ready"]:
            Plugin._state["reason"] = "; ".join(e for e in errors if e) or "ready"

    @staticmethod
    def _save(values=None) -> None:
        with _settings_lock:
            if not Plugin._settings_loaded:
                raise RuntimeError("settings have not loaded; refusing to overwrite the saved file")
            snapshot = dict(settings.settings) if isinstance(settings.settings, dict) else {}
            snapshot.update(values if values is not None else
                            {key: Plugin._state[key] for key in DEFAULT_SETTINGS})
            path = getattr(settings, "path", None)
            if path is None:  # In-memory harness; Decky always supplies a path.
                settings.settings.update(snapshot)
                settings.commit()
                return
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                                 dir=os.path.dirname(path),
                                                 prefix="settings-", delete=False) as f:
                    tmp = f.name
                    json.dump(snapshot, f, indent=4, ensure_ascii=False, allow_nan=False)
                    f.flush()
                    os.fsync(f.fileno())
                if os.path.exists(path):
                    info = os.stat(path)
                    os.chmod(tmp, info.st_mode & 0o777)
                    if hasattr(os, "geteuid") and os.geteuid() == 0:
                        os.chown(tmp, info.st_uid, info.st_gid)
                os.replace(tmp, path)
                settings.settings = snapshot
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)

    @staticmethod
    def _write_settings(values=None) -> str:
        """Disk work only; a completed older snapshot cannot acknowledge edits."""
        try:
            Plugin._save(values)
            return ""
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            message = f"could not save settings: {exc}"
            if message != Plugin._state["settings_error"]:  # retried on every panel tick
                decky.logger.error(f"{LOG} settings save failed: {exc}")
            return message

    @staticmethod
    def _persist() -> None:
        # Unload is the synchronous durability barrier. It holds the mutation
        # lock and accepts no more edits; the writer lock also drains an older
        # disk snapshot before this final one replaces it.
        if not Plugin._settings_loaded:
            return
        error = Plugin._write_settings()
        Plugin._settings_dirty = bool(error)
        Plugin._set_error("settings", error)

    def _schedule_save(self):
        if not Plugin._settings_loaded:
            return
        Plugin._settings_dirty = True
        Plugin._save_generation += 1
        token = Plugin._save_generation
        async def run():
            await self._debounce("settings")
            async with Plugin._mutation_lock:
                if token != Plugin._save_generation or Plugin._unloading:
                    return
                values = {key: Plugin._state[key] for key in DEFAULT_SETTINGS}
            # fsync/replace may be slow. They must not hold the control lock.
            error = await _offload(Plugin._write_settings, values)
            async with Plugin._mutation_lock:
                if token == Plugin._save_generation and not Plugin._unloading:
                    Plugin._settings_dirty = bool(error)
                    Plugin._set_error("settings", error)
        self._queue_worker("settings", token, run, SDR_COALESCE_S)

    def _retry_settings_save(self):
        worker = Plugin._workers.get("settings")
        if (Plugin._settings_dirty and not Plugin._unloading
                and (worker is None or worker.done() or worker not in Plugin._tasks)):
            self._schedule_save()

    async def _debounce(self, component):
        while True:
            remaining = Plugin._due[component] - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    def _queue_worker(self, component, token, run, delay):
        """One task and one executor job per component, with one latest request."""
        Plugin._jobs[component] = (token, run)
        Plugin._due[component] = time.monotonic() + delay
        worker = Plugin._workers.get(component)
        if worker is not None and not worker.done() and worker in Plugin._tasks:
            return
        async def loop():
            while not Plugin._unloading:
                current, job = Plugin._jobs[component]
                await job()
                if Plugin._jobs[component][0] == current:
                    return
        task = asyncio.create_task(loop())
        Plugin._workers[component] = task
        Plugin._track(task)

    @staticmethod
    def _invalidate_sdr_jobs() -> None:
        Plugin._sdr_generation += 1
        Plugin._sdr_look_generation += 1
        Plugin._state["sdr_generating"] = False

    @staticmethod
    def _invalidate_hdr_jobs() -> None:
        Plugin._hdr_generation += 1
        Plugin._state["generating"] = False

    @staticmethod
    def _apply_sdr() -> None:
        if (Plugin._unloading or not Plugin._state["sdr_enabled"]
                or Plugin._disconnected
                or Plugin._state["external_display"]):
            return
        # The slider speaks in "1.0 is neutral"; the atom's neutral moves with
        # the branch, so the offset from neutral is what actually carries over.
        target, _look = _sdr_split(Plugin._state["sdr_saturation"])
        # A write reported failed (a timeout) may still have landed.
        Plugin._last_sdr_attempted = target
        if _write_float_atom(ATOM_SDR_WIDENESS, target):
            Plugin._last_sdr_written = target
            Plugin._state["sdr_applied"] = True
            Plugin._set_error("sdr")
        else:
            Plugin._set_error("sdr", "could not write SDR to gamescope")

    @staticmethod
    def _sdr_ours(*extra):
        """Every SDR value of ours that may still be on the atom."""
        return [value for value in (Plugin._last_sdr_written, Plugin._last_sdr_attempted, *extra)
                if value is not None]

    @staticmethod
    def _sdr_foreign(live, ours) -> bool:
        """A value somebody else left; a failed read (None) is not one."""
        return live is not None and all(abs(live - value) > 0.001 for value in ours)

    @staticmethod
    def _release_sdr() -> bool:
        # A write reported failed (a lost reply) may still have landed, so it
        # is released like an applied one once the atom shows it.
        held = Plugin._state["sdr_applied"] or Plugin._last_sdr_attempted is not None
        if held and not Plugin._can_restore_session():
            Plugin._set_error("sdr", "waiting for gamescope before restoring SDR")
            return False
        look_ok = Plugin._release_look("sdr")
        if not held:
            Plugin._set_error("sdr")
            return look_ok
        live = _read_float_atom(ATOM_SDR_WIDENESS)
        ours = Plugin._sdr_ours()
        if ours and Plugin._sdr_foreign(live, ours):
            # The output changed or another writer took over. Restoring an old
            # internal-panel baseline here would overwrite their current value.
            Plugin._last_sdr_written = Plugin._last_sdr_attempted = None
            Plugin._state["sdr_applied"] = False
            Plugin._set_error("sdr")
            return look_ok
        target = Plugin._sdr_baseline
        if target is None:
            target = SDR_UNSET
        if not _write_float_atom(ATOM_SDR_WIDENESS, target):
            Plugin._set_error(
                "sdr", "could not restore the previous gamescope SDR value")
            return False
        Plugin._last_sdr_written = Plugin._last_sdr_attempted = None
        Plugin._state["sdr_applied"] = False
        Plugin._set_error("sdr")
        decky.logger.info(f"{LOG} SDR released, restored {target:.3f}")
        return look_ok

    @staticmethod
    def _release_look(component) -> bool:
        """Give a look atom back to whatever held it before us.

        An empty path makes gamescope's LoadCubeLut fail to open, which clears
        the look. That is how gamescope behaves rather than a documented API.
        """
        look = _LOOKS[component]
        path_field, baseline_field = _LOOKS[component]["path"], _LOOKS[component]["baseline"]
        owned = getattr(Plugin, path_field)
        if owned is not None:
            if not Plugin._can_restore_session():
                Plugin._set_error(look["error"], "waiting for gamescope before restoring "
                                  f"the {look['label']} look")
                return False
            live = _read_string_atom(look["atom"])
            if (live and live != owned
                    and _own_look_slot(live, _LOOKS[component]["prefix"]) is None):
                # Somebody else replaced us. Their value is now the baseline and
                # must not be erased merely because our toggle went off.
                setattr(Plugin, baseline_field, live)
            else:
                target = getattr(Plugin, baseline_field) or ""
                if not _write_string_atom(look["atom"], target):
                    Plugin._set_error(look["error"],
                                      f"could not restore the previous {look['label']} look")
                    return False
                setattr(Plugin, baseline_field, target or None)
                decky.logger.info(f"{LOG} {look['label']} look restored")
            setattr(Plugin, path_field, None)
        Plugin._state[look["saturation"]] = None
        Plugin._state[look["applied"]] = False
        Plugin._look_failures[component] = 0  # nothing of ours left to fail
        Plugin._set_error(look["error"])
        return True

    @staticmethod
    def _can_restore_session() -> bool:
        # Guard restores immediately, including the interval before the next
        # monitor tick discovers a replaced server. Recovery requires a known
        # matching identity; ordinary startup without one retains its fallback.
        if Plugin._session_identity is None:
            return not Plugin._disconnected
        return _display_session_identity() == Plugin._session_identity

    @staticmethod
    def _track(task) -> None:
        Plugin._tasks.append(task)
        def done(t):
            if t in Plugin._tasks:
                Plugin._tasks.remove(t)
            if not t.cancelled() and t.exception() is not None:
                decky.logger.error(f"{LOG} background task failed: {t.exception()}")
        task.add_done_callback(done)

    def _schedule_sdr(self) -> None:
        """Write once the slider has been still for SDR_COALESCE_S.
        """
        Plugin._sdr_generation += 1
        token = Plugin._sdr_generation

        async def _run():
            try:
                await self._debounce("sdr")
                async with Plugin._mutation_lock:
                    if (token != Plugin._sdr_generation or Plugin._unloading):
                        return  # superseded by a newer move
                    if (Plugin._state["sdr_enabled"]
                            and not Plugin._state["external_display"]):
                        await _offload(Plugin._apply_sdr)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if token == Plugin._sdr_generation and not Plugin._unloading:
                    Plugin._set_error("sdr", f"could not apply SDR: {e}")
                decky.logger.error(f"{LOG} SDR apply failed: {e}")

        self._queue_worker("sdr", token, _run, SDR_COALESCE_S)

    def _schedule_look(self, component) -> None:
        """Rebuild a look once its slider has been still for DEBOUNCE_S.

        A 33-cube takes about a third of a second, far too slow for every tick
        of a slider. Building is deliberately outside the state lock.
        Committing is not: a disable, dock or newer value either wins the lock
        first and invalidates us, or runs after our write and restores the
        previous look.
        """
        look = _LOOKS[component]
        name, generation = look["label"], look["generation"]
        path_field, prefix = _LOOKS[component]["path"], _LOOKS[component]["prefix"]
        setattr(Plugin, generation, getattr(Plugin, generation) + 1)
        token = getattr(Plugin, generation)
        if Plugin._look_enabled(component) and Plugin._look_wanted(component) != SAT_DEFAULT:
            Plugin._state[look["generating"]] = True

        def current():
            return (token == getattr(Plugin, generation) and not Plugin._unloading
                    and not Plugin._disconnected and Plugin._look_enabled(component)
                    and not Plugin._state["external_display"])

        async def _run():
            try:
                await self._debounce(look["worker"])
                async with Plugin._mutation_lock:
                    if not current():
                        return  # superseded by a newer move
                    value = Plugin._look_wanted(component)
                    if value == SAT_DEFAULT:
                        # Identity: no point loading a LUT that does nothing.
                        await _offload(Plugin._release_look, component)
                        return
                    if getattr(Plugin, path_field) is None:
                        live = await _offload(_read_string_atom, look["atom"])
                        live_slot = _own_look_slot(live, prefix)
                        if live_slot is None:
                            # A failed read looks like an empty atom; neither
                            # replaces a foreign look we already know about.
                            Plugin._capture_look_baseline(component, live)
                        else:
                            setattr(Plugin, path_field, live)
                            setattr(Plugin, look["slot"], live_slot)
                    slot = 1 - getattr(Plugin, look["slot"])
                    white_point = (Plugin._sdr_white_point if component == "sdr"
                                   else vibrance_lut.D65)
                path = await _offload(
                    _write_look, value, slot, look["encoding"], prefix, white_point,
                    lambda: token != getattr(Plugin, generation) or Plugin._unloading)
                if not path:
                    if token == getattr(Plugin, generation):
                        Plugin._look_failures[component] += 1
                        Plugin._set_error(look["error"], f"could not build the {name} LUT")
                    return
                async with Plugin._mutation_lock:
                    if not current():
                        return
                    # A foreign writer may have changed while we built, even
                    # when this replaces an already owned LUT.
                    live = await _offload(_read_string_atom, look["atom"])
                    Plugin._capture_look_baseline(component, live)
                    if await _offload(_write_string_atom, look["atom"], path):
                        setattr(Plugin, look["slot"], slot)
                        setattr(Plugin, path_field, path)
                        Plugin._state[look["applied"]] = True
                        Plugin._state[look["saturation"]] = value
                        Plugin._look_failures[component] = 0
                        Plugin._set_error(look["error"])
                        decky.logger.info(
                            f"{LOG} {name} look {value:.2f} -> {os.path.basename(path)}")
                    else:
                        # A write reported failed may still have landed. With
                        # no look of ours yet, count this one as ours so that
                        # release reads the atom and restores if it did; a
                        # newer value is not recorded as applied either way.
                        if getattr(Plugin, path_field) is None:
                            setattr(Plugin, look["slot"], slot)
                            setattr(Plugin, path_field, path)
                        Plugin._look_failures[component] += 1
                        Plugin._set_error(
                            look["error"], f"could not write the {name} LUT to gamescope")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if token == getattr(Plugin, generation) and not Plugin._unloading:
                    Plugin._look_failures[component] += 1
                    Plugin._set_error(look["error"], f"could not apply the {name} LUT: {e}")
                decky.logger.error(f"{LOG} {name} look failed: {e}")
            finally:
                if token == getattr(Plugin, generation):
                    Plugin._state[look["generating"]] = False

        self._queue_worker(look["worker"], token, _run, DEBOUNCE_S)

    @staticmethod
    def _look_enabled(component) -> bool:
        if component == "hdr":
            return Plugin._state["hdr_enabled"] and Plugin._state["hdr_supported"]
        return Plugin._state["sdr_enabled"]

    @staticmethod
    def _look_wanted(component) -> float:
        """The look saturation the slider asks for; 1.0 means no look."""
        if component == "hdr":
            return Plugin._state["hdr_saturation"]
        return _sdr_split(Plugin._state["sdr_saturation"])[1]

    @staticmethod
    def _capture_look_baseline(component, live) -> None:
        """Keep a new foreign look before reclaiming an already owned atom.

        A dropped atom is not a new foreign baseline - and a read that failed
        looks exactly like one, so an empty reading never replaces what the
        first look at the atom found. Our own stale slots must not become
        restore targets either.
        """
        owned = getattr(Plugin, _LOOKS[component]["path"])
        if (live and live != owned
                and _own_look_slot(live, _LOOKS[component]["prefix"]) is None):
            setattr(Plugin, _LOOKS[component]["baseline"], live)

    @staticmethod
    def _reapply_look(component, path, live) -> None:
        """Report reassert failures without hiding a pending replacement."""
        look = _LOOKS[component]
        Plugin._capture_look_baseline(component, live)
        requested = Plugin._look_wanted(component)
        if requested == SAT_DEFAULT:
            # A failed clear retains ownership for retry, not permission to
            # revive the old nonneutral look after resume or a dropped atom.
            Plugin._release_look(component)
            return
        if not _write_string_atom(look["atom"], path):
            Plugin._set_error(look["error"],
                              f"could not reapply the {look['label']} LUT to gamescope")
        elif requested == Plugin._state[look["saturation"]]:
            # A successful old LUT write does not resolve a failed newer build.
            Plugin._set_error(look["error"])

    @staticmethod
    def _reassert_looks_if_dropped() -> None:
        """Put a look back if the atom stopped pointing at ours.

        This only catches the visible half of the problem: if gamescope drops
        the look internally while the property still names our file, nothing we
        can read says so. The startup and resume ladders are what cover that.
        """
        for component, look in _LOOKS.items():
            path = getattr(Plugin, _LOOKS[component]["path"])
            if (not Plugin._look_enabled(component)
                    or not Plugin._state[look["applied"]] or not path):
                continue
            live = _read_string_atom(look["atom"])
            neutral = Plugin._look_wanted(component) == SAT_DEFAULT
            if not os.path.exists(path) or (live == path and not neutral):
                continue
            decky.logger.info(
                f"{LOG} {os.path.basename(path)} needs its look updated")
            Plugin._reapply_look(component, path, live)

    def _retry_pending_looks(self, only=None, quick=False) -> None:
        """Retry under the mutation lock on a monitor tick or unchanged edit.

        A failed replacement leaves the old file owned, but that file must not
        be mistaken for the requested value. Never supersede an active worker.
        """
        if Plugin._unloading or Plugin._disconnected or Plugin._state["external_display"]:
            return
        for component, look in _LOOKS.items():
            if ((only is not None and component != only)
                    or not Plugin._look_enabled(component)
                    or Plugin._state[look["generating"]]
                    or (quick and Plugin._look_failures[component] > 1)):
                continue
            value = Plugin._look_wanted(component)
            path = getattr(Plugin, _LOOKS[component]["path"])
            pending = (bool(path) if value == SAT_DEFAULT else
                       Plugin._state[look["saturation"]] != value
                       or not path or not os.path.isfile(path))
            if pending or Plugin._state[f"{look['error']}_error"]:
                self._schedule_look(component)

    @staticmethod
    def _capture_existing_looks(preserve=False) -> None:
        """Remember foreign baselines and adopt only our own stale paths."""
        for component, look in _LOOKS.items():
            atom, slot_field = look["atom"], look["slot"]
            applied_field, saturation_field = look["applied"], look["saturation"]
            path_field, baseline_field = _LOOKS[component]["path"], _LOOKS[component]["baseline"]
            owned = getattr(Plugin, path_field)
            live = _read_string_atom(atom)
            slot = _own_look_slot(live, _LOOKS[component]["prefix"])
            if preserve and owned is not None and (not live or slot is not None):
                # A missing atom on the same server does not erase its foreign
                # restore value. Resume can republish the retained file.
                if live and live != owned:
                    setattr(Plugin, path_field, live)
                    setattr(Plugin, slot_field, slot)
                    Plugin._state[saturation_field] = None
                continue
            # An adopted stale path in a new session has unknown saturation.
            Plugin._state[saturation_field] = None
            setattr(Plugin, baseline_field, live if slot is None else None)
            setattr(Plugin, path_field, None if slot is None else live)
            Plugin._state[applied_field] = slot is not None
            if slot is not None:
                setattr(Plugin, slot_field, slot)

    @staticmethod
    def _monitor_probe():
        """Reuse one root dump for session health and panel atoms.

        None when the dump did not complete: a slow xprop is no verdict on the
        session, and treating it as one disconnects a perfectly healthy one.
        """
        out = _read_root_properties(DISPLAY)
        if out is None:
            return None
        count = sum(1 for line in out.splitlines() if line.startswith("GAMESCOPE"))
        if not count:
            return 0, None
        atoms = {}
        for line in out.splitlines():
            name = line.partition("(")[0].split("=", 1)[0].strip()
            if name in (ATOM_IS_EXTERNAL, ATOM_EDID_PATH):
                atoms[name] = line
        external = _parse_int_atom(atoms.get(ATOM_IS_EXTERNAL, ""))
        external_value = None if external is None else bool(external)
        edid_path = None if external_value is True else _parse_string_atom(atoms.get(ATOM_EDID_PATH, ""))
        return count, Plugin._probe_panel((external_value, edid_path))

    @staticmethod
    def _probe_panel(metadata=None):
        """Read display metadata without mutating settings or owning atoms."""
        if metadata is None:
            external_value = _display_is_external()
            edid_path = None if external_value is True else _read_string_atom(ATOM_EDID_PATH)
        else:
            external_value, edid_path = metadata
        if not edid_path and external_value is not True:
            edid_path = _builtin_panel_edid_path()
        identity = _file_identity(edid_path)
        cache_key = (("edid", edid_path, identity) if external_value is None else
                     ("external", bool(external_value), edid_path, identity))
        cached = cache_key == Plugin._panel_cache_key and Plugin._panel_cache_complete
        blob = None if external_value is True or cached or not edid_path else _read_edid(edid_path)
        return external_value, cache_key, blob

    @staticmethod
    def _configure_for_panel(probe=None) -> bool:
        """Line the slider up with the branch this display takes.

        Run at startup and again whenever the display changes underneath us -
        docking swaps the panel without restarting anything, and the two
        branches of buildSDRColorimetry disagree about where neutral is.

        Returns True when something actually moved.
        """
        global SDR_NEUTRAL_ATOM, SDR_ATOM_SPAN
        external_value, cache_key, blob = probe if probe is not None else Plugin._probe_panel()
        if (cache_key == Plugin._panel_cache_key
                and Plugin._panel_cache_complete):
            return False

        before = (SDR_NEUTRAL_ATOM, SDR_ATOM_SPAN, Plugin._state["sdr_max"],
                  Plugin._state["hdr_supported"],
                  Plugin._state["external_display"], Plugin._sdr_white_point)
        Plugin._panel_cache_key = cache_key

        # Everything below is calibrated against the built-in panel: its gamut
        # decides the mapping and its EDID decides whether the HDR half exists.
        # An external display is somebody else's panel, so the plugin steps off
        # it rather than applying numbers that were measured elsewhere.
        Plugin._state["external_display"] = external_value is True
        if Plugin._state["external_display"]:
            Plugin._panel_cache_complete = True
            after = (SDR_NEUTRAL_ATOM, SDR_ATOM_SPAN, Plugin._state["sdr_max"],
                     Plugin._state["hdr_supported"], True, Plugin._sdr_white_point)
            return before != after

        # An EDID that cannot be read says nothing about the panel, so every
        # unknown below keeps what was last known. Guessing wide instead would
        # turn a standard panel's neutral slider into its harshest atom value.
        # Before anything is known that is the startup default: the wide
        # mapping, D65 and a visible HDR half.
        wide = _panel_is_wide_gamut(blob)
        if vibrance_edid.valid(blob):
            Plugin._sdr_white_point = vibrance_edid.white_point(blob)

        if wide is False:
            SDR_NEUTRAL_ATOM, SDR_ATOM_SPAN = 0.0, 1.0
            # The atom gives out at 200%; the look carries the rest.
            Plugin._state["sdr_max"] = SDR_MAX_STANDARD
            Plugin._state["sdr_soft_limit"] = SDR_SOFT_STANDARD
        elif wide is True:
            SDR_NEUTRAL_ATOM, SDR_ATOM_SPAN = 1.0, 2.0
            Plugin._state["sdr_max"] = SDR_MAX_WIDE
            Plugin._state["sdr_soft_limit"] = SDR_SOFT_WIDE

        hdr = _panel_supports_hdr(blob)
        if hdr is not None:
            Plugin._state["hdr_supported"] = hdr
        # Re-reading the same bytes cannot change an answer, so a valid EDID
        # completes the cache even if part of it is unknown; its file identity
        # is in the cache key, so a rewritten EDID is still read again.
        Plugin._panel_cache_complete = ((wide is not None and hdr is not None)
                                        or vibrance_edid.valid(blob))
        Plugin._state["sdr_saturation"] = _clamp(
            Plugin._state["sdr_saturation"], SDR_MIN, Plugin._state["sdr_max"])

        after = (SDR_NEUTRAL_ATOM, SDR_ATOM_SPAN, Plugin._state["sdr_max"],
                 Plugin._state["hdr_supported"], False, Plugin._sdr_white_point)
        if before != after:
            decky.logger.info(
                f"{LOG} SDR neutral atom {SDR_NEUTRAL_ATOM:.1f}, atom span "
                f"{SDR_ATOM_SPAN:.1f}, slider up to "
                f"{Plugin._state['sdr_max']:.1f}, HDR half "
                f"{'on' if Plugin._state['hdr_supported'] else 'off'}")
        return before != after

    @staticmethod
    def _reassert() -> None:
        """Write the SDR atom again and point gamescope back at the looks on disk."""
        if Plugin._unloading or Plugin._disconnected or Plugin._state["external_display"]:
            return
        # Woken up in a dock: the panel check that notices may be seconds away,
        # and these values belong to the built-in screen.
        if _display_is_external():
            return
        if Plugin._state["sdr_enabled"]:
            Plugin._apply_sdr()
        for component, look in _LOOKS.items():
            path = getattr(Plugin, _LOOKS[component]["path"])
            if (Plugin._look_enabled(component) and Plugin._state[look["applied"]]
                    and path and os.path.exists(path)):
                Plugin._reapply_look(component, path, _read_string_atom(look["atom"]))

    @staticmethod
    def _watch_once(foreign=_PROBE_NOW) -> None:
        """Put our value back when something drops it, and only call it a
        conflict once that keeps happening.

        gamescope forgets these atoms when it re-initialises, so one
        disagreement is routine and heals itself. vibrantDeck writes the same
        SDR atom, and that disagrees every single time we look - which is what
        the strike count is there to tell apart.

        *foreign* is a reading of the other X servers taken outside the lock
        (None when there is none this tick); by default it is read here.
        """
        # Look health is independent of the SDR atom. In particular, an HDR-only
        # configuration still needs its slow readback check, and a failed clear
        # needs another chance even though the toggle is already off.
        Plugin._slow_checks += 1
        slow_tick = Plugin._slow_checks >= SLOW_CHECK_EVERY
        if slow_tick:
            Plugin._slow_checks = 0
            if not Plugin._state["sdr_enabled"]:
                Plugin._release_sdr()
            if not Plugin._state["hdr_enabled"]:
                Plugin._release_look("hdr")
            Plugin._reassert_looks_if_dropped()

        if not Plugin._state["sdr_enabled"]:
            Plugin._state["conflict"] = ""
            Plugin._reassert_strikes = 0
            # Other servers are not read while off; a reference from before
            # would turn any write made meanwhile into a false warning.
            Plugin._foreign_sdr_seen = None
            Plugin._foreign_active_ticks = 0
            return

        if not Plugin._state["sdr_applied"]:
            Plugin._apply_sdr()
            return

        # A writer on another X server never shows up in what we read back, so
        # it is checked separately, on the slow tick since it costs a process
        # per server. The atom lingers after its writer stops, so only a value
        # that changed since the last look counts, and the warning fades after
        # CONFLICT_STRIKES quiet looks. The first reading is a reference only:
        # it may be our own write from before a reconnect moved us.
        if slow_tick:
            seen = _foreign_sdr_values() if foreign is _PROBE_NOW else foreign
            if seen is not None:
                changed = (bool(seen) and Plugin._foreign_sdr_seen is not None
                           and seen != Plugin._foreign_sdr_seen)
                Plugin._foreign_active_ticks = (
                    CONFLICT_STRIKES if changed else max(0, Plugin._foreign_active_ticks - 1))
                Plugin._foreign_sdr_seen = seen
        foreign_conflict = (
            "another plugin is driving the same SDR setting from a different X "
            "server (vibrantDeck does) - both writes take effect and the last "
            "one wins, so keep only one enabled") if Plugin._foreign_active_ticks else ""
        if not Plugin._reassert_strikes:
            Plugin._state["conflict"] = foreign_conflict

        live = _read_float_atom(ATOM_SDR_WIDENESS)
        if live is None:
            # None also means an unreachable X server. Only recreate the
            # property when gamescope is still present on this display.
            if _count_gamescope_atoms(DISPLAY) > 0:
                Plugin._apply_sdr()
            else:
                Plugin._set_error("sdr", "could not reach gamescope; retrying")
            return
        if Plugin._last_sdr_written is None:
            return
        if abs(live - Plugin._last_sdr_written) <= 0.001:
            target, _ = _sdr_split(Plugin._state["sdr_saturation"])
            if target != Plugin._last_sdr_written:
                Plugin._apply_sdr()
            else:
                Plugin._set_error("sdr")
            Plugin._state["conflict"] = foreign_conflict
            Plugin._reassert_strikes = 0
            return

        Plugin._reassert_strikes += 1
        decky.logger.info(
            f"{LOG} SDR atom was {live:.3f}, putting "
            f"{_sdr_split(Plugin._state['sdr_saturation'])[0]:.3f} back "
            f"(strike {Plugin._reassert_strikes})")
        Plugin._apply_sdr()
        if Plugin._reassert_strikes >= CONFLICT_STRIKES:
            Plugin._state["conflict"] = (
                "another plugin keeps overwriting the same SDR setting "
                "(vibrantDeck does) - keep only one of them enabled")

    async def _reassert_repeatedly(self):
        """gamescope is not necessarily up when we notice, and can clobber us
        while it starts, so the values go back more than once.

        Used after a resume and after startup, which are the same problem: the
        session rebuilding its colour management underneath whatever we wrote.
        A look gone missing then shows up as exactly the part of the slider the
        atom cannot reach going missing; writing both again costs little.
        """
        Plugin._reassert_strikes = 0
        Plugin._state["conflict"] = ""
        token = Plugin._reassert_generation
        if Plugin._resume_event is not None:
            # A set event was meant for the ladder this one replaces. One that
            # quit on the token check never consumed it, and leaving it set
            # would end this ladder before its first step.
            Plugin._resume_event.clear()
        for delay in REASSERT_DELAYS_S:
            if Plugin._resume_event is None:
                await asyncio.sleep(delay)
            else:
                try:
                    await asyncio.wait_for(Plugin._resume_event.wait(), timeout=delay)
                    Plugin._resume_event.clear()
                    return  # The same worker restarts with the newest resume.
                except asyncio.TimeoutError:
                    pass
            async with Plugin._mutation_lock:
                if Plugin._unloading or token != Plugin._reassert_generation:
                    return
                await _offload(Plugin._reassert)
        Plugin._reassert_strikes = 0

    async def _watch_tick(self):
        # Reading other X servers is independent of our settings. A stalled
        # secondary server must not hold the control mutation lock.
        foreign = None
        if (Plugin._slow_checks + 1 >= SLOW_CHECK_EVERY
                and Plugin._state["sdr_enabled"] and Plugin._state["sdr_applied"]):
            foreign = await _offload(_foreign_sdr_values)
        async with Plugin._mutation_lock:
            if not Plugin._unloading and not Plugin._disconnected:
                await _offload(Plugin._watch_once, foreign)
                # A pending look is a full LUT build. A first failure is retried
                # on the next tick; a repeat only on the slow tick (about every
                # 16 s), which _watch_once has just reset the counter for.
                self._retry_pending_looks(quick=Plugin._slow_checks != 0)

    def _schedule_reassert(self):
        Plugin._reassert_generation += 1
        worker = Plugin._workers.get("reassert")
        if worker is not None and not worker.done() and worker in Plugin._tasks:
            Plugin._resume_event.set()
        else:
            Plugin._resume_event = asyncio.Event()
        self._queue_worker("reassert", Plugin._reassert_generation, self._reassert_repeatedly, 0)

    async def _refresh_panel(self, probe=None):
        """Follow the display if it changes underneath us."""
        if Plugin._unloading:
            return
        # Metadata reads can each hit a subprocess timeout. Do not delay user
        # mutations while probing; only the resulting state/atom commit locks.
        if probe is None:
            probe = await _offload(Plugin._probe_panel)
        async with Plugin._mutation_lock:
            if Plugin._unloading:
                return
            was_external = Plugin._state["external_display"]
            changed = Plugin._configure_for_panel(probe)
            if Plugin._state["external_display"]:
                if changed:
                    decky.logger.info(f"{LOG} external display - standing down")
                Plugin._invalidate_sdr_jobs()
                Plugin._invalidate_hdr_jobs()
                # Also retries a failed release on later stable polling ticks.
                await _offload(Plugin._release_sdr)
                await _offload(Plugin._release_look, "hdr")
                return
            if not changed:
                return
            decky.logger.info(f"{LOG} internal panel configuration changed")
            if was_external:
                # Capture the internal value before applying our setting again.
                # If docking failed to release our last write, keep its original
                # baseline instead of adopting that write as the restore value.
                # A failed read says nothing, so it keeps the known baseline,
                # and a value of ours - written or a lost write that landed -
                # is not one either.
                live = await _offload(_read_float_atom, ATOM_SDR_WIDENESS)
                if Plugin._sdr_foreign(live, Plugin._sdr_ours()):
                    Plugin._sdr_baseline = live
            await self._apply_both()

    async def _apply_both(self, retry_sdr=False, retry_hdr=False):
        """Put both halves where the settings say; retry_* keeps a pending
        look instead of rebuilding it."""
        if Plugin._state["sdr_enabled"]:
            await _offload(Plugin._apply_sdr)
            if retry_sdr:
                self._retry_pending_looks("sdr")
            else:
                self._schedule_look("sdr")
        else:
            await _offload(Plugin._release_sdr)
        if Plugin._state["hdr_enabled"] and Plugin._state["hdr_supported"]:
            if retry_hdr:
                self._retry_pending_looks("hdr")
            else:
                self._schedule_look("hdr")
        else:
            if Plugin._state["hdr_enabled"]:
                Plugin._invalidate_hdr_jobs()
            await _offload(Plugin._release_look, "hdr")

    async def _monitor_loop(self):
        prev_mono = time.monotonic()
        prev_boot = _boottime()
        logged = {}  # stage -> failure last logged, so a lasting one is logged once
        since_watch = 0.0
        since_panel = 0.0
        while True:
            stage = "panel"
            try:
                await asyncio.sleep(SUSPEND_POLL_S)
                mono, boot = time.monotonic(), _boottime()
                elapsed = mono - prev_mono
                asleep = (boot - prev_boot) - (mono - prev_mono)
                prev_mono, prev_boot = mono, boot
                if asleep > SUSPEND_SLACK_S:
                    decky.logger.info(
                        f"{LOG} resumed after ~{asleep:.0f}s suspended")
                    self._schedule_reassert()
                    # The display may have changed while asleep (docked, say):
                    # look at it on the next tick, before any watch runs.
                    since_watch, since_panel = 0.0, PANEL_CHECK_INTERVAL_S
                    continue
                since_watch += elapsed
                since_panel += elapsed
                if since_panel >= PANEL_CHECK_INTERVAL_S:
                    since_panel = 0.0
                    result = await _offload(Plugin._monitor_probe)
                    session_identity = await _offload(_display_session_identity)
                    changed_session = (Plugin._session_identity is not None and session_identity is not None
                                       and session_identity != Plugin._session_identity)
                    count, probe = result if result is not None else (None, None)
                    # Also retry a reconnect that ended in an exception.
                    if count == 0 or changed_session or Plugin._disconnected:
                        async with Plugin._mutation_lock:
                            Plugin._disconnect()
                        await self._start_when_ready(start_monitor=False)
                        continue
                    if result is None:
                        continue  # retried on the next panel tick
                    await self._refresh_panel(probe)
                    logged.pop(stage, None)  # healthy again: log its next failure
                    async with Plugin._mutation_lock:
                        self._retry_settings_save()
                if Plugin._state["external_display"]:
                    continue
                if since_watch >= WATCH_INTERVAL_S:
                    since_watch = 0.0
                    stage = "watch"
                    await self._watch_tick()
                    logged.pop(stage, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if logged.get(stage) != str(e):  # a lasting failure repeats every tick
                    decky.logger.error(f"{LOG} monitor failed: {e}")
                    logged[stage] = str(e)
                await asyncio.sleep(2.0)

    async def _main(self):
        global _lut_writes_disabled, _xprop_timeout
        decky.logger.info(f"{LOG} startup v{updater.plugin_version()}")
        Plugin._unloading = False
        Plugin._settings_loaded = Plugin._state["settings_loaded"] = False
        Plugin._settings_dirty = False
        _xprop_timeout = 3.0
        worker = _native_lut_python() or "in-process"
        decky.logger.info(f"{LOG} LUT worker: {worker}")
        await _offload(_wait_for_lut_writer)
        if Plugin._unloading:
            return
        _lut_writes_disabled = False
        Plugin._workers = {}
        Plugin._jobs = {}
        Plugin._due = {}
        Plugin._tasks = []
        Plugin._resume_event = None
        Plugin._disconnected = True
        Plugin._state["ready"] = False
        Plugin._state["reason"] = "starting up"
        Plugin._panel_cache_key = None
        Plugin._panel_cache_complete = False
        try:
            await _offload(updater.ssl_context)
        except Exception as e:
            decky.logger.warning(f"{LOG} update checks unavailable: {e}")
        if Plugin._unloading:
            return
        logged = None
        while not Plugin._unloading:
            try:
                await _offload(_read_settings)
                break
            except SettingsUnreadable as exc:
                # Content that does not parse never will; waiting would leave
                # the plugin dead. Keep it beside the file, start from defaults.
                try:
                    aside = await _offload(_set_aside_settings)
                    decky.logger.warning(f"{LOG} settings were unreadable ({exc}); "
                                         f"kept as {aside}, starting from defaults")
                    Plugin._state["settings_notice"] = (
                        f"The saved settings could not be read, so defaults are in use. "
                        f"The old file is kept as {aside}.")
                    continue
                except OSError as move:
                    failure = f"{exc}; could not set the file aside: {move}"
            except Exception as exc:
                # Defaults are not a safe replacement for a file we could not
                # read. Keep the file intact and recover from transient I/O.
                failure = exc
            message = f"could not load settings: {failure}"
            Plugin._state["reason"] = message
            Plugin._state["settings_error"] = message
            if message != logged:  # retried every second; log each new reason once
                decky.logger.warning(f"{LOG} {message}")
                logged = message
            await asyncio.sleep(SETTINGS_RETRY_S)
        if Plugin._unloading:
            return
        Plugin._state["settings_error"] = ""
        Plugin._state["reason"] = "starting up"
        # _setting() clamps values an older build saved outside today's range
        # to the widest one; _configure_for_panel narrows them once it knows.
        for key, fallback in DEFAULT_SETTINGS.items():
            Plugin._state[key] = _setting(key, fallback)
        Plugin._settings_loaded = Plugin._state["settings_loaded"] = True
        # Persist repaired/migrated fields even if no gamescope session arrives.
        self._schedule_save()

        # Decky starts plugins as a system service, so we can easily be up
        # before the session is. Finding nothing here is a race, not a verdict,
        # so the rest of startup waits in a task rather than giving up until
        # somebody reinstalls the plugin.
        Plugin._track(asyncio.create_task(self._start_when_ready()))

    @staticmethod
    def _disconnect():
        Plugin._disconnected = True
        Plugin._state["ready"] = False
        Plugin._state["reason"] = "gamescope disconnected; looking for a new session"
        Plugin._invalidate_sdr_jobs()
        Plugin._invalidate_hdr_jobs()
        Plugin._panel_cache_key = None
        Plugin._panel_cache_complete = False
        Plugin._foreign_sdr_seen = None
        Plugin._foreign_active_ticks = 0
        # A failed read is not proof that the X server restarted. Retain
        # ownership until startup compares the socket identity.

    @staticmethod
    def _forget_session_ownership():
        Plugin._sdr_baseline = None
        Plugin._last_sdr_written = Plugin._last_sdr_attempted = None
        Plugin._hdr_look_path = Plugin._sdr_look_path = None
        Plugin._hdr_look_baseline = Plugin._sdr_look_baseline = None
        Plugin._state["sdr_look_saturation"] = None
        Plugin._state["hdr_applied_saturation"] = None
        for key in ("sdr_applied", "hdr_applied", "sdr_look_applied"):
            Plugin._state[key] = False

    async def _start_when_ready(self, start_monitor=True):
        """Connect to gamescope, then (once) start the monitor.

        The monitor starts even if connecting failed with an exception: it is
        what notices the plugin is still disconnected and tries again.
        """
        try:
            await self._connect()
        finally:
            if start_monitor and not Plugin._unloading:
                Plugin._track(asyncio.create_task(self._monitor_loop()))

    async def _connect(self):
        delay = GAMESCOPE_RETRY_FIRST_S
        waited = False
        while not Plugin._unloading:
            while not await _offload(_pick_display):
                if not waited:
                    decky.logger.info(f"{LOG} no gamescope atoms yet - waiting")
                    waited = True
                Plugin._state["reason"] = ("waiting for gamescope to come up" if shutil.which(
                    "xprop", path=_system_env()["PATH"]) else "xprop is missing; install xorg-xprop")
                await asyncio.sleep(delay)
                delay = min(delay * 2, GAMESCOPE_RETRY_MAX_S)
            previous = -1
            for _ in range(GAMESCOPE_SETTLE_TRIES):
                count = await _offload(_count_gamescope_atoms, DISPLAY)
                if count == 0 or count == previous:
                    break
                previous = count
                await asyncio.sleep(GAMESCOPE_SETTLE_S)
            if count > 0:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, GAMESCOPE_RETRY_MAX_S)
        if Plugin._unloading:
            return
        decky.logger.info(f"{LOG} gamescope settled at {previous} atoms")

        async with Plugin._mutation_lock:
            if Plugin._unloading:
                return
            session_identity = await _offload(_display_session_identity)
            previous_session = Plugin._session_identity
            preserve = session_identity is not None and session_identity == previous_session
            # Our values, kept past the reset below: a changed identity can
            # still be a server we wrote to (a reconnect that moved between
            # gamescope's servers), so the atom there may hold one of them.
            # A failed attempt has already forgotten them, so they are carried
            # to its retry.
            ours = Plugin._sdr_ours(*Plugin._connect_ours)
            Plugin._connect_ours = tuple(dict.fromkeys(ours))  # retries repeat them
            known_baseline = Plugin._sdr_baseline
            if not preserve:
                Plugin._forget_session_ownership()
            # Remember what was there before we touch anything, so switching the
            # plugin off puts the display back rather than to our idea of default.
            if Plugin._state["sdr_enabled"] and (previous_session is None
                                                 or session_identity is None):
                # A fresh process: _forget_session_ownership cleared the
                # baseline, and the atom may hold our own write from before the
                # reload; an unreadable identity proves nothing either way. A
                # replaced server (gamescope restarted) cannot hold one, so
                # there the atom is read below like any other value.
                decky.logger.info(f"{LOG} SDR was already on; no baseline to keep")
            elif not Plugin._state["sdr_applied"] and Plugin._last_sdr_attempted is None:
                # Nothing of ours is on this server - also when SDR was
                # switched on while disconnected - so the atom is the value
                # to restore. A failed read leaves the baseline as it is: the
                # known one on this server, none on a new one, where it cannot be
                # told from an absent atom and restoring gamescope's default is right.
                live = await _offload(_read_float_atom, ATOM_SDR_WIDENESS)
                if Plugin._sdr_foreign(live, ours):
                    Plugin._sdr_baseline = live
                    decky.logger.info(f"{LOG} SDR baseline {live:.3f}")
                elif live is not None:
                    # The atom shows our own write, so this is a server we
                    # used; what we found there before is still the restore value.
                    Plugin._sdr_baseline = known_baseline
            await _offload(Plugin._capture_existing_looks, preserve)

            # Both branches of buildSDRColorimetry are usable; they just start
            # from different places. Do this before exposing the controls.
            panel_changed = await _offload(Plugin._configure_for_panel)

            if (not Plugin._state["hdr_supported"]
                    and Plugin._state["hdr_enabled"]):
                Plugin._state["hdr_enabled"] = False
                Plugin._invalidate_hdr_jobs()
                self._schedule_save()

            # Recorded only now: if anything above raised, a retry must still
            # see this as a new session rather than a preserved one.
            Plugin._session_identity = session_identity
            Plugin._connect_ours = ()
            Plugin._disconnected = False
            Plugin._state["ready"] = True
            Plugin._state["reason"] = "ready"

            # Starting while already docked used to run all three apply paths.
            # Do not apply saved settings until the internal panel comes back.
            if not Plugin._state["external_display"]:
                await self._apply_both(retry_sdr=preserve and not panel_changed, retry_hdr=preserve)

        # Same ladder as after a resume: the session can still be settling.
        if not Plugin._state["external_display"]:
            self._schedule_reassert()

    async def _unload(self):
        global _lut_writes_disabled, _xprop_timeout
        # Decky kills a plugin still alive five seconds after SIGTERM. Keep
        # unavailable X servers from consuming that entire restore window.
        _xprop_timeout = UNLOAD_XPROP_TIMEOUT_S
        async with Plugin._mutation_lock:
            Plugin._unloading = True
            _lut_writes_disabled = True
            Plugin._invalidate_sdr_jobs()
            Plugin._invalidate_hdr_jobs()
        for task in list(Plugin._tasks):
            task.cancel()
        if Plugin._tasks:
            await asyncio.wait(list(Plugin._tasks), timeout=0.1)
        Plugin._tasks = []
        async with Plugin._mutation_lock:
            # Different atoms, and the disk does not wait on X: restore both
            # halves and flush the desired values side by side, so a slow X
            # server cannot push the save past Decky's stop budget. The flush
            # covers edits whose debounce has not run yet. Before startup has
            # read the file the class defaults are not the user's settings,
            # so a quick unload leaves it intact.
            jobs = [_offload(Plugin._release_sdr), _offload(Plugin._release_look, "hdr")]
            if Plugin._settings_loaded:
                jobs.append(_offload(Plugin._persist))
            for result in await asyncio.gather(*jobs, return_exceptions=True):
                if isinstance(result, Exception):
                    decky.logger.error(f"{LOG} unload step failed: {result}")
        decky.logger.info(f"{LOG} unloaded")

    async def _uninstall(self):
        # Decky's shutdown has just run _unload; repeating it would spend the
        # five-second stop budget restoring and saving a second time.
        if not Plugin._unloading:
            await self._unload()
        # A cancelled executor call may still be finishing a file. Wait for the
        # writer lock after disabling new writes so nothing can recreate a LUT
        # behind the cleanup pass.
        await _offload(_wait_for_lut_writer)
        # The generated LUTs live outside the plugin directory the loader is
        # about to delete.
        for look in _LOOKS.values():
            for slot in (0, 1):
                try:
                    os.unlink(os.path.join(_runtime_dir(),
                                           f"{look['prefix']}-{slot}.cube"))
                except OSError:
                    pass
