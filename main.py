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
    archive_files=("lego_updater.py", "vibrance_lut.py"),
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
DISPLAY = ":0"          # both replaced at startup by _pick_display()
DISPLAYS = (":0", ":1")
X11_SOCKET_DIR = "/tmp/.X11-unix"
DISPLAY_FALLBACK = (":0", ":1")

# gamescope's own BIsWideGamut test, which is what decides between the two
# branches of buildSDRColorimetry - and therefore which mapping below applies.
# Measured: Legion Go 2 OLED is 0.6836/0.3154 and passes; Legion Go S LCD is
# 0.6523/0.3408 and fails on y after passing x by a hair.
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
SDR_MAX_STANDARD = 2.0
SDR_SOFT_STANDARD = 1.5

# Where the atom's own neutral sits, and how much of the slider it can carry.
# Both replaced at startup once the panel is known.
SDR_NEUTRAL_ATOM = 1.0
SDR_ATOM_SPAN = 2.0

# EXPERIMENTAL, and only ever reached on a standard-gamut panel. There cfit()
# stops responding at atom 1.0, which is the slider's 200%, so the top third has
# nothing left to push. The remainder is carried by a look on the SDR path -
# GAMESCOPE_COLOR_LOOK_G22, the twin of the atom the HDR half already uses, in
# the gamma 2.2 domain.
#
# A wide-gamut panel never gets here: its atom span is the whole slider, so the
# remainder is always zero and no look is ever written. Setting this to False
# puts a standard-gamut panel back to a 100-200% slider and nothing else.
# gamescope's own "nobody has set this": both branches of buildSDRColorimetry
# turn a negative wideness into that branch's native gamut. Writing it back is
# the only value that means "off" on either kind of panel - 1.0 is neutral on
# one branch and the harshest setting there is on the other.
SDR_UNSET = -1.0

SDR_LOOK_ENABLED = True
SDR_MAX_STANDARD_WITH_LOOK = 3.0

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
SDR_SOFT_STANDARD_WITH_LOOK = 2.6

# HDR is our own LUT, so the ceiling is set by where the maths stops paying for
# itself rather than by gamescope. Past about 1.3 a boost hard-clips channels at
# the gamut boundary and detail in saturated areas goes with it.
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

# gamescope drops what we wrote when it re-initialises, which it does across a
# suspend. CLOCK_MONOTONIC stops while the machine is asleep and CLOCK_BOOTTIME
# does not, so the gap between them is a suspend cycle - no dbus, no root, no
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

# One disagreement is gamescope resetting the atom; a run of them is another
# plugin holding the other end. Only the second is worth telling the user about.
CONFLICT_STRIKES = 3
WATCH_INTERVAL_S = 4.0

# Docking swaps the panel without restarting anything, so the display is
# re-read on a slow tick. The expensive checks - another plugin on a second X
# server, and whether our looks are still the ones loaded - ride along with it
# rather than spawning an xprop every few seconds.
PANEL_CHECK_INTERVAL_S = 5.0
SLOW_CHECK_EVERY = 4

DEFAULT_SETTINGS = {
    "sdr_enabled": False,
    "hdr_enabled": False,
    "sdr_saturation": SAT_DEFAULT,
    "hdr_saturation": SAT_DEFAULT,
}

# Decky's manager creates an empty file on first install and suppresses read
# errors. Record absence before construction so an existing damaged file is
# never mistaken for a fresh installation.
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
        finally:
            if descriptor is not None:
                os.close(descriptor)
        values = {} if _settings_new and not text.strip() else json.loads(text)
        if not isinstance(values, dict):
            raise ValueError("settings must contain a JSON object")
        settings.settings = values
        _settings_new = False


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


def _read_int_atom(name: str):
    """A plain CARDINAL, not one of the floats gamescope bit-casts."""
    try:
        out = _run_xprop(
            ["xprop", "-root", name],
            capture_output=True, text=True, timeout=_xprop_timeout, env=_system_env(),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_int_atom(out)


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
    try:
        out = _run_xprop(
            ["xprop", "-root", name],
            capture_output=True, text=True, timeout=_xprop_timeout, env=_system_env(),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if "= " not in out:
        return None
    try:
        return _cardinal_to_float(int(out.split("= ", 1)[1].strip()))
    except ValueError:
        return None


def _write_float_atom(name: str, value: float) -> bool:
    try:
        _run_xprop(
            ["xprop", "-root", "-f", name, "32c",
             "-set", name, str(_float_to_cardinal(value))],
            capture_output=True, timeout=_xprop_timeout, env=_system_env(), check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _write_string_atom(name: str, value: str) -> bool:
    """UTF8_STRING, matching what gamescope writes for its own path atoms."""
    try:
        _run_xprop(
            ["xprop", "-root", "-f", name, "8u", "-set", name, value],
            capture_output=True, timeout=_xprop_timeout, env=_system_env(), check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _read_string_atom(name: str):
    try:
        out = _run_xprop(
            ["xprop", "-root", name],
            capture_output=True, text=True, timeout=_xprop_timeout, env=_system_env(),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_string_atom(out)


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


def _edid_red_primary(blob: bytes):
    """Red x,y from the base EDID chromaticity block, or None.

    Byte 0x19 carries the low two bits of each of the first four coordinates in
    its own pairs of bits; 0x1B and 0x1C carry the high eight of red x and y.
    """
    if not _valid_edid(blob):
        return None
    low = blob[0x19]
    red_x = ((blob[0x1B] << 2) | (low >> 6)) / 1024.0
    red_y = ((blob[0x1C] << 2) | ((low >> 4) & 0x03)) / 1024.0
    return (red_x, red_y) if red_x > 0 and red_y > 0 and red_x + red_y <= 1 else None


def _read_edid(path=None):
    """Read regular EDID metadata without waiting for a FIFO/device producer."""
    path = path or _read_string_atom(ATOM_EDID_PATH)
    if not path:
        return None
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


def _file_identity(path):
    """Detect replacement, edits and deletion at a stable pathname."""
    if not path:
        return None
    try:
        stat = os.stat(path)
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except OSError:
        return None


def _edid_file_identity(path):
    return _file_identity(path)


def _display_session_identity():
    """The local X socket identifies a session even at the same DISPLAY."""
    number = DISPLAY.removeprefix(":").split(".", 1)[0]
    identity = _file_identity(os.path.join(X11_SOCKET_DIR, f"X{number}"))
    return (DISPLAY, identity) if identity is not None else None


def _edid_white_point(blob):
    """Native white used by gamescope's SDR source, with a D65 fallback."""
    if not _valid_edid(blob):
        return vibrance_lut.D65
    low = blob[0x1A]
    x = ((blob[0x21] << 2) | ((low >> 2) & 3)) / 1024.0
    y = ((blob[0x22] << 2) | (low & 3)) / 1024.0
    if x <= 0.0 or y <= 0.0 or x + y >= 1.0:
        return vibrance_lut.D65
    return x, y


CTA_EXTENSION_TAG = 0x02
DISPLAYID_EXTENSION_TAG = 0x70
DISPLAYID_CTA_DATA_BLOCK = 0x81
CTA_EXTENDED_TAG = 0x07
CTA_HDR_STATIC_METADATA = 0x06
EOTF_PQ_BIT = 1 << 2


def _valid_edid(blob):
    if not blob or len(blob) < 128 or blob[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return False
    size = 128 * (1 + blob[126])
    return len(blob) == size and all(sum(blob[i:i + 128]) % 256 == 0
                                    for i in range(0, size, 128))


def _cta_collection_pq(block: bytes, start: int, end: int):
    """Walk a run of CTA-861 data blocks. None means "not in this one"."""
    pos = start
    found = None
    while pos < end:
        length = block[pos] & 0x1F
        if length == 0:
            break
        if pos + 1 + length > end:
            raise ValueError("truncated CTA data block")
        if (block[pos] >> 5) == CTA_EXTENDED_TAG and block[pos + 1] == CTA_HDR_STATIC_METADATA:
            if length < 3:
                raise ValueError("truncated HDR static metadata")
            if block[pos + 2] & EOTF_PQ_BIT:
                return True
            found = False
        pos += 1 + length
    return found


def _displayid_pq(block: bytes):
    """CTA-861 data blocks can also be carried inside a DisplayID extension.

    The Legion Go S does exactly that - one DisplayID block and no standalone
    CTA extension at all - so a parser that only looks for tag 0x02 finds
    nothing and cannot tell "no HDR" from "no idea".
    """
    payload_length = block[2]
    pos, end = 5, 5 + payload_length
    if end > 127:
        raise ValueError("truncated DisplayID payload")
    result = None
    while pos + 2 < end:
        tag, body_length = block[pos], block[pos + 2]
        if tag == 0 and body_length == 0:
            break
        body = pos + 3
        if body + body_length > end:
            raise ValueError("truncated DisplayID data block")
        if tag == DISPLAYID_CTA_DATA_BLOCK:
            found = _cta_collection_pq(block, body, min(body + body_length, end))
            if found is True:
                return True
            if found is False:
                result = False
        pos = body + body_length
    return result


def _edid_supports_pq(blob: bytes):
    """True if the display advertises SMPTE ST 2084 anywhere in its EDID.

    Read from the EDID rather than from GAMESCOPE_HDR_OUTPUT_FEEDBACK on
    purpose: that atom meant "HDR is on" and now means "HDR is possible"
    depending on the gamescope build, and reading it the wrong way round is what
    broke a sibling plugin on Bazzite 44. What the panel claims to support does
    not move under us.

    None only when the EDID itself cannot be trusted - too short, or shorter
    than the extension count it declares. A complete EDID that never mentions PQ
    is a definite no, because that is the only place it could have been said.
    """
    if not _valid_edid(blob):
        return None
    extensions = blob[126]
    if len(blob) < 128 * (1 + extensions):
        return None
    for index in range(1, extensions + 1):
        block = blob[128 * index:128 * (index + 1)]
        found = None
        try:
            if block[0] == CTA_EXTENSION_TAG:
                end = block[2] or 127
                if not 4 <= end <= 127:
                    return None
                found = _cta_collection_pq(block, 4, end)
            elif block[0] == DISPLAYID_EXTENSION_TAG:
                found = _displayid_pq(block)
        except ValueError:
            return None
        if found is True:
            return True
    return False


_UNREAD_EDID = object()


def _panel_supports_hdr(blob=_UNREAD_EDID):
    """Whether the HDR half has anything to act on, or None if unknowable."""
    if blob is _UNREAD_EDID:
        blob = _read_edid()
    if blob is None:
        return None
    supported = _edid_supports_pq(blob)
    if supported is not None:
        decky.logger.info(
            f"{LOG} panel {'advertises' if supported else 'does not advertise'} "
            f"PQ in its CTA-861 block")
    return supported


def _panel_is_wide_gamut(blob=_UNREAD_EDID):
    """gamescope's BIsWideGamut, applied to the EDID gamescope itself publishes.

    True, False, or None when the panel could not be identified - in which case
    the caller carries on rather than disabling a working control over a missing
    atom.
    """
    if blob is _UNREAD_EDID:
        blob = _read_edid()
    if blob is None:
        return None
    primary = _edid_red_primary(blob)
    if primary is None:
        return None
    red_x, red_y = primary
    wide = red_x > WIDE_GAMUT_RED_X and red_y < WIDE_GAMUT_RED_Y
    decky.logger.info(
        f"{LOG} panel red {red_x:.4f},{red_y:.4f} - "
        f"{'wide gamut' if wide else 'standard gamut'}")
    return wide


def _read_root_properties(display: str):
    try:
        return _run_xprop(
            ["xprop", "-root"],
            capture_output=True, text=True, timeout=_xprop_timeout,
            env=_system_env(DISPLAY=display),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _count_gamescope_atoms(display: str) -> int:
    out = _read_root_properties(display)
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
    """Settle on an X server gamescope is actually behind, or None."""
    global DISPLAY, DISPLAYS
    DISPLAYS = _display_candidates()
    best, best_count = None, 0
    for candidate in DISPLAYS:
        count = _count_gamescope_atoms(candidate)
        decky.logger.info(f"{LOG} {candidate}: {count} gamescope atoms")
        if count > best_count:
            best, best_count = candidate, count
    if best is None:
        return None
    DISPLAY = best
    return best


def _other_writer_present() -> bool:
    """True if the SDR atom exists on a server we are not the one using.

    These atoms do not survive a gamescope restart and nothing creates them on
    write-only servers, so finding one somewhere we never wrote means another
    plugin put it there during this session. That is worth knowing because a
    write from any server takes effect, while a read only ever sees one of them
    - so two plugins on different servers fight over the picture without either
    of them being able to see the other in the value it reads back.
    """
    for candidate in DISPLAYS:
        if candidate == DISPLAY:
            continue
        try:
            out = _run_xprop(
                ["xprop", "-root", ATOM_SDR_WIDENESS],
                capture_output=True, text=True, timeout=3,
                env=_system_env(DISPLAY=candidate),
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if "= " in out:
            return True
    return False


# ── the HDR look ───────────────────────────────────────────────────────────────

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
    above = value - SAT_DEFAULT
    on_atom = min(above, SDR_ATOM_SPAN)
    remainder = max(0.0, above - SDR_ATOM_SPAN)
    look = min(1.0 + remainder * SDR_LOOK_PER_UNIT, SDR_LOOK_CEILING)
    return SDR_NEUTRAL_ATOM + on_atom, look


def _native_lut_python():
    """Use ARM Python when Decky's frozen interpreter runs under x86 emulation."""
    try:
        with open("/proc/self/root/usr/bin/python3", "rb") as f:
            native = f.read(20)
        with open(sys.executable, "rb") as f:
            loader = f.read(20)
        if (native[:5] == b"\x7fELF\x02" and native[5] == 1
                and int.from_bytes(native[18:20], "little") == 183
                and loader[:5] == b"\x7fELF\x02" and loader[5] == 1
                and int.from_bytes(loader[18:20], "little") == 62):
            return "/proc/self/root/usr/bin/python3"
    except OSError:
        pass
    return None


def _generate_lut(saturation, size, encoding, source_white, cancelled):
    interpreter = _native_lut_python()
    if interpreter is None:
        return vibrance_lut.generate(saturation, size, encoding, source_white,
                                    cancelled=cancelled)
    # Preserve validation before converting arguments to CLI strings.
    if not isinstance(size, int) or isinstance(size, bool) or not 2 <= size <= 128:
        raise ValueError("gamescope accepts LUT_3D_SIZE between 2 and 128")
    vibrance_lut.saturate((0, 0, 0), saturation, encoding, source_white)
    if encoding == "g22":
        vibrance_lut._white_xyz(source_white)
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
            text = _generate_lut(
                saturation, LUT_SIZE, encoding, source_white, cancelled)
            if _lut_writes_disabled or cancelled():
                return ""
            with open(tmp, "w") as f:
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
        except (OSError, ValueError, vibrance_lut.BuildCancelled) as e:
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
    _other_writer = False
    _disconnected = False
    _session_identity = None
    _reassert_generation = 0
    _resume_event = None

    # ── exported ───────────────────────────────────────────────────────────────

    async def get_state(self) -> dict:
        return dict(Plugin._state)

    async def get_version(self) -> dict:
        return {"version": updater.plugin_version()}

    async def check_for_updates(self) -> dict:
        info = await _offload(updater.check)
        if info.get("update_available"):
            expected = f"DeckyVibranceHDR-{info.get('latest_version', '')}.zip"
            if (info.get("asset_name") != expected
                    or not info.get("download_url")):
                info["download_url"] = None
                info["asset_name"] = None
                info["error"] = (
                    f"Release v{info.get('latest_version', '?')} has no "
                    f"installable {expected} asset yet")
        return info

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
            if enabled and (Plugin._unloading
                            or Plugin._state["external_display"]):
                if saturation is not None:
                    self._schedule_save()
                return dict(Plugin._state)
            if (enabled and not Plugin._state["sdr_enabled"]
                    and not Plugin._state["sdr_applied"] and not Plugin._disconnected):
                Plugin._sdr_baseline = await _offload(
                    _read_float_atom, ATOM_SDR_WIDENESS)
            Plugin._state["sdr_enabled"] = enabled
            if not enabled:
                Plugin._invalidate_sdr_jobs()
            self._schedule_save()
            if enabled and not Plugin._disconnected:
                await _offload(Plugin._apply_sdr)
                if SDR_LOOK_ENABLED:
                    self._schedule_sdr_look()
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
            if enabled and (Plugin._unloading
                            or Plugin._state["external_display"]
                            or not Plugin._state["hdr_supported"]):
                if saturation is not None:
                    self._schedule_save()
                return dict(Plugin._state)
            Plugin._state["hdr_enabled"] = enabled
            if not enabled:
                Plugin._invalidate_hdr_jobs()
            self._schedule_save()
            if enabled and not Plugin._disconnected:
                self._schedule_hdr()
            elif not enabled:
                await _offload(Plugin._release_hdr)
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
            else:
                self._retry_settings_save()
            if changed:
                self._schedule_sdr()
                if SDR_LOOK_ENABLED:
                    self._schedule_sdr_look()
            else:
                target, _look = _sdr_split(value)
                if (Plugin._state["sdr_enabled"] and not Plugin._disconnected
                        and not Plugin._state["external_display"]
                        and (Plugin._state["sdr_error"] or not Plugin._state["sdr_applied"]
                             or Plugin._last_sdr_written != target)):
                    self._schedule_sdr()
                self._retry_pending_looks("sdr_look")
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
            else:
                self._retry_settings_save()
            if changed:
                self._schedule_hdr()
            else:
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
            await _offload(Plugin._release_hdr)
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
                    stat = os.stat(path)
                    os.chmod(tmp, stat.st_mode & 0o777)
                    if hasattr(os, "geteuid") and os.geteuid() == 0:
                        os.chown(tmp, stat.st_uid, stat.st_gid)
                os.replace(tmp, path)
                settings.settings = snapshot
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)

    @staticmethod
    def _write_settings(values=None) -> str:
        """Disk work only; a completed older snapshot cannot acknowledge edits."""
        try:
            if values is None:
                Plugin._save()
            else:
                Plugin._save(values)
            return ""
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            decky.logger.error(f"{LOG} settings save failed: {exc}")
            return f"could not save settings: {exc}"

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
        if _write_float_atom(ATOM_SDR_WIDENESS, target):
            Plugin._last_sdr_written = target
            Plugin._state["sdr_applied"] = True
            Plugin._set_error("sdr")
        else:
            Plugin._set_error("sdr", "could not write SDR to gamescope")

    @staticmethod
    def _release_sdr() -> bool:
        if Plugin._state["sdr_applied"] and not Plugin._can_restore_session():
            Plugin._set_error("sdr", "waiting for gamescope before restoring SDR")
            return False
        look_ok = Plugin._release_sdr_look()
        if not Plugin._state["sdr_applied"]:
            Plugin._set_error("sdr")
            return look_ok
        live = _read_float_atom(ATOM_SDR_WIDENESS)
        if (live is not None and Plugin._last_sdr_written is not None
                and abs(live - Plugin._last_sdr_written) > 0.001):
            # The output changed or another writer took over. Restoring an old
            # internal-panel baseline here would overwrite their current value.
            Plugin._last_sdr_written = None
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
        Plugin._last_sdr_written = None
        Plugin._state["sdr_applied"] = False
        Plugin._set_error("sdr")
        decky.logger.info(f"{LOG} SDR released, restored {target:.3f}")
        return look_ok

    @staticmethod
    def _release_sdr_look() -> bool:
        owned = Plugin._sdr_look_path
        if owned is None:
            Plugin._state["sdr_look_saturation"] = None
            Plugin._state["sdr_look_applied"] = False
            Plugin._set_error("sdr_look")
            return True
        if not Plugin._can_restore_session():
            Plugin._set_error("sdr_look", "waiting for gamescope before restoring the SDR look")
            return False
        live = _read_string_atom(ATOM_LOOK_G22)
        if (live and live != owned
                and _own_look_slot(live, "sdr-look") is None):
            # Somebody else replaced us. Their value is now the baseline and
            # must not be erased merely because our toggle went off.
            Plugin._sdr_look_path = None
            Plugin._sdr_look_baseline = live
            Plugin._state["sdr_look_saturation"] = None
            Plugin._state["sdr_look_applied"] = False
            Plugin._set_error("sdr_look")
            return True
        target = Plugin._sdr_look_baseline or ""
        if not _write_string_atom(ATOM_LOOK_G22, target):
            Plugin._set_error("sdr_look", "could not restore the previous SDR look")
            return False
        Plugin._sdr_look_path = None
        Plugin._sdr_look_baseline = target or None
        Plugin._state["sdr_look_saturation"] = None
        Plugin._state["sdr_look_applied"] = False
        Plugin._set_error("sdr_look")
        decky.logger.info(f"{LOG} SDR look restored")
        return True

    @staticmethod
    def _release_hdr() -> bool:
        # An empty path makes gamescope's LoadCubeLut fail to open, which clears
        # the look. That is the documented way back to no look at all.
        owned = Plugin._hdr_look_path
        if owned is None:
            Plugin._state["hdr_applied_saturation"] = None
            Plugin._state["hdr_applied"] = False
            Plugin._set_error("hdr")
            return True
        if not Plugin._can_restore_session():
            Plugin._set_error("hdr", "waiting for gamescope before restoring the HDR look")
            return False
        live = _read_string_atom(ATOM_LOOK_PQ)
        if (live and live != owned
                and _own_look_slot(live, "hdr-look") is None):
            Plugin._hdr_look_path = None
            Plugin._hdr_look_baseline = live
            Plugin._state["hdr_applied_saturation"] = None
            Plugin._state["hdr_applied"] = False
            Plugin._set_error("hdr")
            return True
        target = Plugin._hdr_look_baseline or ""
        if not _write_string_atom(ATOM_LOOK_PQ, target):
            Plugin._set_error("hdr", "could not restore the previous HDR look")
            return False
        Plugin._hdr_look_path = None
        Plugin._hdr_look_baseline = target or None
        Plugin._state["hdr_applied_saturation"] = None
        Plugin._state["hdr_applied"] = False
        Plugin._set_error("hdr")
        decky.logger.info(f"{LOG} HDR look restored")
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

    def _schedule_sdr_look(self) -> None:
        """Same debounce as the HDR half, and for the same reason: a 33-cube
        takes about a third of a second and the atom has already moved."""
        Plugin._sdr_look_generation += 1
        token = Plugin._sdr_look_generation
        _atom, wanted = _sdr_split(Plugin._state["sdr_saturation"])
        if Plugin._state["sdr_enabled"] and wanted != 1.0:
            Plugin._state["sdr_generating"] = True

        async def _run():
            try:
                await self._debounce("sdr_look")
                if (token != Plugin._sdr_look_generation
                        or Plugin._disconnected
                        or not Plugin._state["sdr_enabled"]
                        or Plugin._state["external_display"]
                        or Plugin._unloading):
                    return
                _atom, look = _sdr_split(Plugin._state["sdr_saturation"])
                if look == 1.0:
                    async with Plugin._mutation_lock:
                        if (token == Plugin._sdr_look_generation
                                and Plugin._state["sdr_enabled"]
                                and not Plugin._state["external_display"]
                                and not Plugin._unloading):
                            await _offload(Plugin._release_sdr_look)
                    return

                async with Plugin._mutation_lock:
                    if (token != Plugin._sdr_look_generation or Plugin._unloading
                            or Plugin._disconnected or not Plugin._state["sdr_enabled"]
                            or Plugin._state["external_display"]):
                        return
                    if Plugin._sdr_look_path is None:
                        live = await _offload(_read_string_atom, ATOM_LOOK_G22)
                        live_slot = _own_look_slot(live, "sdr-look")
                        if live_slot is None:
                            Plugin._sdr_look_baseline = live
                        else:
                            Plugin._sdr_look_path = live
                            Plugin._sdr_look_slot = live_slot
                    slot = 1 - Plugin._sdr_look_slot
                    white_point = Plugin._sdr_white_point
                path = await _offload(
                    _write_look, look, slot, "g22", "sdr-look",
                    white_point,
                    lambda: token != Plugin._sdr_look_generation or Plugin._unloading)
                if not path:
                    if token == Plugin._sdr_look_generation:
                        Plugin._set_error("sdr_look", "could not build the SDR LUT")
                    return

                # Building is deliberately outside the state lock. Committing
                # is not: a disable, dock or newer slider value either wins the
                # lock first and invalidates us, or runs after our write and
                # restores the previous look.
                async with Plugin._mutation_lock:
                    if (token != Plugin._sdr_look_generation
                            or not Plugin._state["sdr_enabled"]
                            or Plugin._state["external_display"]
                            or Plugin._unloading):
                        return
                    # A foreign writer may have changed while we built, even
                    # when this replaces an already owned LUT.
                    live = await _offload(_read_string_atom, ATOM_LOOK_G22)
                    Plugin._capture_look_baseline("sdr", live)
                    if await _offload(_write_string_atom, ATOM_LOOK_G22, path):
                        Plugin._sdr_look_slot = slot
                        Plugin._sdr_look_path = path
                        Plugin._state["sdr_look_applied"] = True
                        Plugin._state["sdr_look_saturation"] = look
                        Plugin._set_error("sdr_look")
                        decky.logger.info(
                            f"{LOG} SDR look {look:.2f} -> "
                            f"{os.path.basename(path)}")
                    else:
                        Plugin._set_error(
                            "sdr_look", "could not write the SDR LUT to gamescope")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if token == Plugin._sdr_look_generation and not Plugin._unloading:
                    Plugin._set_error("sdr_look", f"could not apply the SDR LUT: {e}")
                decky.logger.error(f"{LOG} SDR look failed: {e}")
            finally:
                if token == Plugin._sdr_look_generation:
                    Plugin._state["sdr_generating"] = False

        self._queue_worker("sdr_look", token, _run, DEBOUNCE_S)

    def _schedule_hdr(self) -> None:
        """Rebuild the LUT once the slider has been still for DEBOUNCE_S."""
        Plugin._hdr_generation += 1
        token = Plugin._hdr_generation
        if Plugin._state["hdr_enabled"]:
            Plugin._state["generating"] = True

        async def _run():
            try:
                await self._debounce("hdr")
                async with Plugin._mutation_lock:
                    if (token != Plugin._hdr_generation
                            or Plugin._disconnected
                            or not Plugin._state["hdr_enabled"]
                            or not Plugin._state["hdr_supported"]
                            or Plugin._state["external_display"]
                            or Plugin._unloading):
                        return  # superseded by a newer move
                    value = Plugin._state["hdr_saturation"]
                if value == SAT_DEFAULT:
                    async with Plugin._mutation_lock:
                        if (token == Plugin._hdr_generation
                                and Plugin._state["hdr_enabled"]
                                and not Plugin._state["external_display"]
                                and not Plugin._unloading):
                            # Identity: no point loading a LUT that does nothing.
                            await _offload(Plugin._release_hdr)
                    return

                async with Plugin._mutation_lock:
                    if (token != Plugin._hdr_generation or Plugin._unloading
                            or Plugin._disconnected or not Plugin._state["hdr_enabled"]
                            or not Plugin._state["hdr_supported"] or Plugin._state["external_display"]):
                        return
                    if Plugin._hdr_look_path is None:
                        live = await _offload(_read_string_atom, ATOM_LOOK_PQ)
                        live_slot = _own_look_slot(live, "hdr-look")
                        if live_slot is None:
                            Plugin._hdr_look_baseline = live
                        else:
                            Plugin._hdr_look_path = live
                            Plugin._look_slot = live_slot
                    slot = 1 - Plugin._look_slot
                path = await _offload(_write_look, value, slot, "pq", "hdr-look",
                                     vibrance_lut.D65,
                                     lambda: token != Plugin._hdr_generation or Plugin._unloading)
                if not path:
                    if token == Plugin._hdr_generation:
                        Plugin._set_error("hdr", "could not build the HDR LUT")
                    return
                async with Plugin._mutation_lock:
                    if (token != Plugin._hdr_generation
                            or not Plugin._state["hdr_enabled"]
                            or not Plugin._state["hdr_supported"]
                            or Plugin._state["external_display"]
                            or Plugin._unloading):
                        return
                    live = await _offload(_read_string_atom, ATOM_LOOK_PQ)
                    Plugin._capture_look_baseline("hdr", live)
                    if await _offload(_write_string_atom, ATOM_LOOK_PQ, path):
                        Plugin._look_slot = slot
                        Plugin._hdr_look_path = path
                        Plugin._state["hdr_applied"] = True
                        Plugin._state["hdr_applied_saturation"] = value
                        Plugin._set_error("hdr")
                        decky.logger.info(
                            f"{LOG} HDR look {value:.2f} -> "
                            f"{os.path.basename(path)}")
                    else:
                        Plugin._set_error(
                            "hdr", "could not write the HDR LUT to gamescope")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if token == Plugin._hdr_generation and not Plugin._unloading:
                    Plugin._set_error("hdr", f"could not apply the HDR LUT: {e}")
                decky.logger.error(f"{LOG} HDR apply failed: {e}")
            finally:
                if token == Plugin._hdr_generation:
                    Plugin._state["generating"] = False

        self._queue_worker("hdr", token, _run, DEBOUNCE_S)

    @staticmethod
    def _capture_look_baseline(component, live) -> None:
        """Keep a new foreign look before reclaiming an already owned atom.

        A dropped atom is not a new foreign baseline. Our own stale slots must
        not become restore targets either.
        """
        owned = getattr(Plugin, f"_{component}_look_path")
        if ((owned is None or (live and live != owned))
                and _own_look_slot(live, f"{component}-look") is None):
            setattr(Plugin, f"_{component}_look_baseline", live)

    @staticmethod
    def _reapply_look(component, atom, path, live) -> None:
        """Report reassert failures without hiding a pending replacement."""
        Plugin._capture_look_baseline(component, live)
        if component == "hdr":
            error_component = "hdr"
            requested = Plugin._state["hdr_saturation"]
            applied = Plugin._state["hdr_applied_saturation"]
        else:
            error_component = "sdr_look"
            requested = _sdr_split(Plugin._state["sdr_saturation"])[1]
            applied = Plugin._state["sdr_look_saturation"]
        if requested == SAT_DEFAULT:
            # A failed clear retains ownership for retry, not permission to
            # revive the old nonneutral look after resume or a dropped atom.
            if component == "hdr":
                Plugin._release_hdr()
            else:
                Plugin._release_sdr_look()
            return
        if not _write_string_atom(atom, path):
            Plugin._set_error(error_component, f"could not reapply the {component.upper()} LUT to gamescope")
        elif requested == applied:
            # A successful old LUT write does not resolve a failed newer build.
            Plugin._set_error(error_component)

    @staticmethod
    def _reassert_looks_if_dropped() -> None:
        """Put a look back if the atom stopped pointing at ours.

        This only catches the visible half of the problem: if gamescope drops
        the look internally while the property still names our file, nothing we
        can read says so. The startup and resume ladders are what cover that.
        """
        for component, enabled, applied, atom, path in (
            ("sdr", Plugin._state["sdr_enabled"],
             Plugin._state["sdr_look_applied"], ATOM_LOOK_G22,
             Plugin._sdr_look_path),
            ("hdr", Plugin._state["hdr_enabled"] and Plugin._state["hdr_supported"],
             Plugin._state["hdr_applied"], ATOM_LOOK_PQ,
             Plugin._hdr_look_path),
        ):
            if not enabled or not applied or not path:
                continue
            live = _read_string_atom(atom)
            neutral = (Plugin._state["hdr_saturation"] if component == "hdr" else
                       _sdr_split(Plugin._state["sdr_saturation"])[1]) == SAT_DEFAULT
            if not os.path.exists(path) or (live == path and not neutral):
                continue
            decky.logger.info(
                f"{LOG} {os.path.basename(path)} needs its look updated")
            Plugin._reapply_look(component, atom, path, live)

    def _retry_pending_looks(self, only=None) -> None:
        """Retry under the mutation lock on a monitor tick or unchanged edit.

        A failed replacement leaves the old file owned, but that file must not
        be mistaken for the requested value. Never supersede an active worker.
        """
        if Plugin._unloading or Plugin._disconnected or Plugin._state["external_display"]:
            return
        for component, enabled, generating, value, applied, path, schedule in (
            ("hdr", Plugin._state["hdr_enabled"] and Plugin._state["hdr_supported"],
             Plugin._state["generating"], Plugin._state["hdr_saturation"],
             Plugin._state["hdr_applied_saturation"], Plugin._hdr_look_path,
             self._schedule_hdr),
            ("sdr_look", SDR_LOOK_ENABLED and Plugin._state["sdr_enabled"],
             Plugin._state["sdr_generating"],
             _sdr_split(Plugin._state["sdr_saturation"])[1],
             Plugin._state["sdr_look_saturation"], Plugin._sdr_look_path,
             self._schedule_sdr_look),
        ):
            if (only is not None and component != only) or not enabled or generating:
                continue
            pending = (bool(path) if value == SAT_DEFAULT else
                       applied != value or not path or not os.path.isfile(path))
            if pending or Plugin._state[f"{component}_error"]:
                schedule()

    @staticmethod
    def _capture_existing_looks(preserve=False) -> None:
        """Remember foreign baselines and adopt only our own stale paths."""
        for component, atom, slot_field, applied_field, saturation_field in (
            ("sdr", ATOM_LOOK_G22, "_sdr_look_slot", "sdr_look_applied", "sdr_look_saturation"),
            ("hdr", ATOM_LOOK_PQ, "_look_slot", "hdr_applied", "hdr_applied_saturation"),
        ):
            path_field, baseline_field = f"_{component}_look_path", f"_{component}_look_baseline"
            owned = getattr(Plugin, path_field)
            live = _read_string_atom(atom)
            slot = _own_look_slot(live, f"{component}-look")
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
        """Reuse one root dump for session health and panel atoms."""
        out = _read_root_properties(DISPLAY)
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
        identity = _edid_file_identity(edid_path)
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

        wide = _panel_is_wide_gamut(blob)
        Plugin._sdr_white_point = _edid_white_point(blob)

        if wide is False:
            SDR_NEUTRAL_ATOM, SDR_ATOM_SPAN = 0.0, 1.0
            if SDR_LOOK_ENABLED:
                # The atom gives out at 200%; the look carries the rest.
                Plugin._state["sdr_max"] = SDR_MAX_STANDARD_WITH_LOOK
                Plugin._state["sdr_soft_limit"] = SDR_SOFT_STANDARD_WITH_LOOK
            else:
                Plugin._state["sdr_max"] = SDR_MAX_STANDARD
                Plugin._state["sdr_soft_limit"] = SDR_SOFT_STANDARD
        else:
            SDR_NEUTRAL_ATOM, SDR_ATOM_SPAN = 1.0, 2.0
            Plugin._state["sdr_max"] = SDR_MAX_WIDE
            Plugin._state["sdr_soft_limit"] = SDR_SOFT_WIDE

        hdr = _panel_supports_hdr(blob)
        Plugin._state["hdr_supported"] = hdr is not False
        Plugin._panel_cache_complete = wide is not None and hdr is not None
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
    def _reassert_hdr() -> None:
        """Point gamescope back at the look already sitting on disk."""
        if (Plugin._unloading or Plugin._state["external_display"]
                or Plugin._disconnected or not Plugin._state["hdr_supported"]
                or not Plugin._state["hdr_enabled"]
                or not Plugin._state["hdr_applied"]
                or not Plugin._hdr_look_path):
            return
        path = Plugin._hdr_look_path
        if os.path.exists(path):
            Plugin._reapply_look("hdr", ATOM_LOOK_PQ, path, _read_string_atom(ATOM_LOOK_PQ))

    @staticmethod
    def _reassert() -> None:
        if Plugin._unloading or Plugin._disconnected or Plugin._state["external_display"]:
            return
        if Plugin._state["sdr_enabled"]:
            Plugin._apply_sdr()
            if (Plugin._state["sdr_look_applied"]
                    and Plugin._sdr_look_path):
                path = Plugin._sdr_look_path
                if os.path.exists(path):
                    Plugin._reapply_look("sdr", ATOM_LOOK_G22, path, _read_string_atom(ATOM_LOOK_G22))
        Plugin._reassert_hdr()

    @staticmethod
    def _watch_once(other_writer=None) -> None:
        """Put our value back when something drops it, and only call it a
        conflict once that keeps happening.

        gamescope forgets these atoms when it re-initialises, so one
        disagreement is routine and heals itself. vibrantDeck writes the same
        SDR atom, and that disagrees every single time we look - which is what
        the strike count is there to tell apart.
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
                Plugin._release_hdr()
            Plugin._reassert_looks_if_dropped()

        if not Plugin._state["sdr_enabled"]:
            Plugin._state["conflict"] = ""
            Plugin._reassert_strikes = 0
            return

        if not Plugin._state["sdr_applied"]:
            Plugin._apply_sdr()
            return

        # A writer on another X server never shows up in what we read back, so
        # this is checked separately and says so in its own words. It costs a
        # process per server, so it goes on the slow tick.
        if slow_tick:
            Plugin._other_writer = (_other_writer_present() if other_writer is None else other_writer)
        slow_conflict = Plugin._other_writer
        if slow_conflict:
            Plugin._state["conflict"] = (
                "another plugin is driving the same SDR setting from a "
                "different X server (vibrantDeck does) - both writes take "
                "effect and the last one wins, so keep only one enabled")
            return

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
            Plugin._state["conflict"] = ""
            Plugin._reassert_strikes = 0
            return

        Plugin._reassert_strikes += 1
        decky.logger.info(
            f"{LOG} SDR atom was {live:.3f}, putting "
            f"{Plugin._state['sdr_saturation']:.3f} back "
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
        The atom survives that - it is re-read from the property - but a look
        does not, which shows up as exactly the part of the slider the atom
        cannot reach going missing.
        """
        Plugin._reassert_strikes = 0
        Plugin._state["conflict"] = ""
        token = Plugin._reassert_generation
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
        other_writer = Plugin._other_writer
        if (Plugin._slow_checks + 1 >= SLOW_CHECK_EVERY
                and Plugin._state["sdr_enabled"] and Plugin._state["sdr_applied"]):
            other_writer = await _offload(_other_writer_present)
        async with Plugin._mutation_lock:
            if not Plugin._unloading and not Plugin._disconnected:
                await _offload(Plugin._watch_once, other_writer)
                self._retry_pending_looks()

    def _schedule_reassert(self, run):
        Plugin._reassert_generation += 1
        worker = Plugin._workers.get("reassert")
        if worker is not None and not worker.done() and worker in Plugin._tasks:
            Plugin._resume_event.set()
        else:
            Plugin._resume_event = asyncio.Event()
        self._queue_worker("reassert", Plugin._reassert_generation, run, 0)

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
                await _offload(Plugin._release_hdr)
                return
            if not changed:
                return
            decky.logger.info(f"{LOG} internal panel configuration changed")
            if was_external:
                # Capture the internal value before applying our setting again.
                # If docking failed to release our last write, keep its original
                # baseline instead of adopting that write as the restore value.
                live = await _offload(_read_float_atom, ATOM_SDR_WIDENESS)
                if (not Plugin._state["sdr_applied"] or (live is not None
                        and (Plugin._last_sdr_written is None
                             or abs(live - Plugin._last_sdr_written) > 0.001))):
                    Plugin._sdr_baseline = live
            if Plugin._state["sdr_enabled"]:
                await _offload(Plugin._apply_sdr)
                if SDR_LOOK_ENABLED:
                    self._schedule_sdr_look()
            else:
                await _offload(Plugin._release_sdr)
            if Plugin._state["hdr_enabled"]:
                if Plugin._state["hdr_supported"]:
                    self._schedule_hdr()
                else:
                    Plugin._invalidate_hdr_jobs()
                    await _offload(Plugin._release_hdr)
            else:
                await _offload(Plugin._release_hdr)

    async def _monitor_loop(self):
        prev_mono = time.monotonic()
        prev_boot = _boottime()
        since_watch = 0.0
        since_panel = 0.0
        while True:
            try:
                await asyncio.sleep(SUSPEND_POLL_S)
                mono, boot = time.monotonic(), _boottime()
                elapsed = mono - prev_mono
                asleep = (boot - prev_boot) - (mono - prev_mono)
                prev_mono, prev_boot = mono, boot
                if asleep > SUSPEND_SLACK_S:
                    decky.logger.info(
                        f"{LOG} resumed after ~{asleep:.0f}s suspended")
                    self._schedule_reassert(self._reassert_repeatedly)
                    since_watch = 0.0
                    continue
                since_watch += elapsed
                since_panel += elapsed
                if since_panel >= PANEL_CHECK_INTERVAL_S:
                    since_panel = 0.0
                    count, probe = await _offload(Plugin._monitor_probe)
                    session_identity = await _offload(_display_session_identity)
                    changed_session = (Plugin._session_identity is not None and session_identity is not None
                                       and session_identity != Plugin._session_identity)
                    if not count or changed_session:
                        async with Plugin._mutation_lock:
                            Plugin._disconnect()
                        await self._start_when_ready(start_monitor=False)
                        continue
                    await self._refresh_panel(probe)
                    async with Plugin._mutation_lock:
                        self._retry_settings_save()
                if Plugin._state["external_display"]:
                    continue
                if since_watch >= WATCH_INTERVAL_S:
                    since_watch = 0.0
                    await self._watch_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.error(f"{LOG} monitor failed: {e}")
                await asyncio.sleep(2.0)

    async def _main(self):
        global _lut_writes_disabled, _xprop_timeout
        decky.logger.info(f"{LOG} startup v{updater.plugin_version()}")
        Plugin._unloading = False
        Plugin._settings_loaded = False
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
        while not Plugin._unloading:
            try:
                await _offload(_read_settings)
                break
            except Exception as exc:
                # Defaults are not a safe replacement for a file we could not
                # read. Keep the file intact and recover from transient I/O.
                message = f"could not load settings: {exc}"
                Plugin._state["reason"] = message
                Plugin._state["settings_error"] = message
                decky.logger.warning(f"{LOG} {message}")
                await asyncio.sleep(1.0)
        if Plugin._unloading:
            return
        Plugin._state["settings_error"] = ""
        Plugin._state["reason"] = "starting up"
        for key, fallback in DEFAULT_SETTINGS.items():
            Plugin._state[key] = _setting(key, fallback)
        # Settings written by an older build can sit outside the current range.
        # Widest possible range here; narrowed again once the panel is known.
        Plugin._state["sdr_saturation"] = _clamp(
            Plugin._state["sdr_saturation"], SDR_MIN, SDR_MAX_WIDE)
        Plugin._state["hdr_saturation"] = _clamp(
            Plugin._state["hdr_saturation"], HDR_MIN, HDR_MAX)
        Plugin._settings_loaded = True
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
        Plugin._other_writer = False
        # A failed read is not proof that the X server restarted. Retain
        # ownership until startup compares the socket identity.

    @staticmethod
    def _forget_session_ownership():
        Plugin._sdr_baseline = None
        Plugin._last_sdr_written = None
        Plugin._hdr_look_path = Plugin._sdr_look_path = None
        Plugin._hdr_look_baseline = Plugin._sdr_look_baseline = None
        Plugin._state["sdr_look_saturation"] = None
        Plugin._state["hdr_applied_saturation"] = None
        for key in ("sdr_applied", "hdr_applied", "sdr_look_applied"):
            Plugin._state[key] = False

    async def _start_when_ready(self, start_monitor=True):
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
            preserve = session_identity is not None and session_identity == Plugin._session_identity
            if not preserve:
                Plugin._forget_session_ownership()
            Plugin._session_identity = session_identity
            # Remember what was there before we touch anything, so switching the
            # plugin off puts the display back rather than to our idea of default.
            if Plugin._state["sdr_enabled"]:
                if not preserve:
                    Plugin._sdr_baseline = None
                    decky.logger.info(f"{LOG} SDR was already on; no baseline to keep")
            elif not (preserve and Plugin._state["sdr_applied"]):
                Plugin._sdr_baseline = await _offload(
                    _read_float_atom, ATOM_SDR_WIDENESS)
                if Plugin._sdr_baseline is not None:
                    decky.logger.info(
                        f"{LOG} SDR baseline {Plugin._sdr_baseline:.3f}")
            await _offload(Plugin._capture_existing_looks, preserve)

            # Both branches of buildSDRColorimetry are usable; they just start
            # from different places. Do this before exposing the controls.
            panel_changed = await _offload(Plugin._configure_for_panel)

            if (not Plugin._state["hdr_supported"]
                    and Plugin._state["hdr_enabled"]):
                Plugin._state["hdr_enabled"] = False
                Plugin._invalidate_hdr_jobs()
                self._schedule_save()

            Plugin._disconnected = False
            Plugin._state["ready"] = True
            Plugin._state["reason"] = "ready"

            # Starting while already docked used to run all three apply paths.
            # Do not apply saved settings until the internal panel comes back.
            if not Plugin._state["external_display"]:
                if Plugin._state["sdr_enabled"]:
                    await _offload(Plugin._apply_sdr)
                    if SDR_LOOK_ENABLED:
                        if preserve and not panel_changed:
                            self._retry_pending_looks("sdr_look")
                        else:
                            self._schedule_sdr_look()
                else:
                    await _offload(Plugin._release_sdr)
                if Plugin._state["hdr_enabled"]:
                    if preserve:
                        self._retry_pending_looks("hdr")
                    else:
                        self._schedule_hdr()
                else:
                    await _offload(Plugin._release_hdr)

        # Same ladder as after a resume: the session can still be settling.
        if not Plugin._state["external_display"]:
            self._schedule_reassert(self._reassert_repeatedly)
        if start_monitor:
            Plugin._track(asyncio.create_task(self._monitor_loop()))

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
            try:
                # Different atoms; restore both halves within the stop budget.
                results = await asyncio.gather(_offload(Plugin._release_sdr),
                                               _offload(Plugin._release_hdr), return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        decky.logger.error(f"{LOG} could not restore on unload: {result}")
            except Exception as exc:
                decky.logger.error(f"{LOG} could not restore on unload: {exc}")
            # Flush desired values even when a debounce has not run yet.
            # Before startup has read the file, the class defaults are not a
            # snapshot of user settings. A quick unload must leave it intact.
            if Plugin._settings_loaded:
                await _offload(Plugin._persist)
        decky.logger.info(f"{LOG} unloaded")

    async def _uninstall(self):
        await self._unload()
        # A cancelled executor call may still be finishing a file. Wait for the
        # writer lock after disabling new writes so nothing can recreate a LUT
        # behind the cleanup pass.
        await _offload(_wait_for_lut_writer)
        # The generated LUTs live outside the plugin directory the loader is
        # about to delete.
        for prefix in ("hdr-look", "sdr-look"):
            for slot in (0, 1):
                try:
                    os.unlink(os.path.join(_runtime_dir(),
                                           f"{prefix}-{slot}.cube"))
                except OSError:
                    pass
