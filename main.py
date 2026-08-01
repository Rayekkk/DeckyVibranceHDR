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
import os
import struct
import subprocess
import sys
import time

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

settings = SettingsManager(
    name="settings",
    settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR,
)


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
    return min(max(float(v), lo), hi)


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


def _float_to_cardinal(value: float) -> int:
    """gamescope stores these floats bit-cast into a CARDINAL."""
    return struct.unpack("I", struct.pack("f", float(value)))[0]


def _cardinal_to_float(raw):
    if raw is None:
        return None
    try:
        return struct.unpack("f", struct.pack("I", raw & 0xFFFFFFFF))[0]
    except struct.error:
        return None


def _read_int_atom(name: str):
    """A plain CARDINAL, not one of the floats gamescope bit-casts."""
    try:
        out = subprocess.run(
            ["xprop", "-root", name],
            capture_output=True, text=True, timeout=3, env=_system_env(),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
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
        out = subprocess.run(
            ["xprop", "-root", name],
            capture_output=True, text=True, timeout=3, env=_system_env(),
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
        subprocess.run(
            ["xprop", "-root", "-f", name, "32c",
             "-set", name, str(_float_to_cardinal(value))],
            capture_output=True, timeout=3, env=_system_env(), check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _write_string_atom(name: str, value: str) -> bool:
    """UTF8_STRING, matching what gamescope writes for its own path atoms."""
    try:
        subprocess.run(
            ["xprop", "-root", "-f", name, "8u", "-set", name, value],
            capture_output=True, timeout=3, env=_system_env(), check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _read_string_atom(name: str):
    try:
        out = subprocess.run(
            ["xprop", "-root", name],
            capture_output=True, text=True, timeout=3, env=_system_env(),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if "= " not in out:
        return None
    value = out.split("= ", 1)[1].strip()
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        value = value[1:-1]
    return value or None


def _edid_red_primary(blob: bytes):
    """Red x,y from the base EDID chromaticity block, or None.

    Byte 0x19 carries the low two bits of each of the first four coordinates in
    its own pairs of bits; 0x1B and 0x1C carry the high eight of red x and y.
    """
    if len(blob) < 0x1D:
        return None
    low = blob[0x19]
    red_x = ((blob[0x1B] << 2) | (low >> 6)) / 1024.0
    red_y = ((blob[0x1C] << 2) | ((low >> 4) & 0x03)) / 1024.0
    return red_x, red_y


def _read_edid():
    """The EDID gamescope publishes for the current output, or None."""
    path = _read_string_atom(ATOM_EDID_PATH)
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


CTA_EXTENSION_TAG = 0x02
DISPLAYID_EXTENSION_TAG = 0x70
DISPLAYID_CTA_DATA_BLOCK = 0x81
CTA_EXTENDED_TAG = 0x07
CTA_HDR_STATIC_METADATA = 0x06
EOTF_PQ_BIT = 1 << 2


def _cta_collection_pq(block: bytes, start: int, end: int):
    """Walk a run of CTA-861 data blocks. None means "not in this one"."""
    pos = start
    while pos < end:
        length = block[pos] & 0x1F
        if length == 0:
            break
        if (block[pos] >> 5) == CTA_EXTENDED_TAG and length >= 2 \
                and pos + 2 < end and block[pos + 1] == CTA_HDR_STATIC_METADATA:
            return bool(block[pos + 2] & EOTF_PQ_BIT)
        pos += 1 + length
    return None


def _displayid_pq(block: bytes):
    """CTA-861 data blocks can also be carried inside a DisplayID extension.

    The Legion Go S does exactly that - one DisplayID block and no standalone
    CTA extension at all - so a parser that only looks for tag 0x02 finds
    nothing and cannot tell "no HDR" from "no idea".
    """
    payload_length = block[2]
    pos, end = 5, min(5 + payload_length, 127)
    while pos + 2 < end:
        tag, body_length = block[pos], block[pos + 2]
        if tag == 0 and body_length == 0:
            break
        body = pos + 3
        if tag == DISPLAYID_CTA_DATA_BLOCK:
            found = _cta_collection_pq(block, body, min(body + body_length, end))
            if found is not None:
                return found
        pos = body + body_length
    return None


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
    if len(blob) < 128:
        return None
    extensions = blob[126]
    if len(blob) < 128 * (1 + extensions):
        return None
    for index in range(1, extensions + 1):
        block = blob[128 * index:128 * (index + 1)]
        found = None
        if block[0] == CTA_EXTENSION_TAG:
            end = block[2]                # where the detailed timings start
            if 4 <= end <= 127:
                found = _cta_collection_pq(block, 4, end)
        elif block[0] == DISPLAYID_EXTENSION_TAG:
            found = _displayid_pq(block)
        if found is not None:
            return found
    return False


def _panel_supports_hdr():
    """Whether the HDR half has anything to act on, or None if unknowable."""
    blob = _read_edid()
    if blob is None:
        return None
    supported = _edid_supports_pq(blob)
    if supported is not None:
        decky.logger.info(
            f"{LOG} panel {'advertises' if supported else 'does not advertise'} "
            f"PQ in its CTA-861 block")
    return supported


def _panel_is_wide_gamut():
    """gamescope's BIsWideGamut, applied to the EDID gamescope itself publishes.

    True, False, or None when the panel could not be identified - in which case
    the caller carries on rather than disabling a working control over a missing
    atom.
    """
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


def _count_gamescope_atoms(display: str) -> int:
    try:
        out = subprocess.run(
            ["xprop", "-root"],
            capture_output=True, text=True, timeout=5,
            env=_system_env(DISPLAY=display),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
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
            out = subprocess.run(
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


def _write_look(saturation: float, slot: int,
                encoding: str = "pq", prefix: str = "hdr-look") -> str:
    """Build the .cube and return its path, or "" on failure.

    Two slots alternate rather than one file being rewritten. gamescope reloads
    when the atom changes, and while X does emit a PropertyNotify even for an
    identical value, a changing path removes any doubt about that - and leaves
    the previous LUT intact until the new one is complete.
    """
    path = os.path.join(_runtime_dir(), f"{prefix}-{slot}.cube")
    tmp = path + ".tmp"
    try:
        text = vibrance_lut.generate(saturation, LUT_SIZE, encoding)
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return path
    except (OSError, ValueError) as e:
        decky.logger.error(f"{LOG} could not write look: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return ""


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
    _reassert_strikes = 0       # consecutive times the atom disagreed with us
    _slow_checks = 0            # ticks since the last of the expensive checks
    _tasks = []

    # ── exported ───────────────────────────────────────────────────────────────

    async def get_state(self) -> dict:
        return dict(Plugin._state)

    async def get_version(self) -> dict:
        return {"version": updater.plugin_version()}

    async def check_for_updates(self) -> dict:
        return await _offload(updater.check)

    async def perform_update(self, download_url: str, asset_name: str) -> dict:
        return await _offload(updater.download, download_url, asset_name)

    async def set_sdr_enabled(self, enabled: bool) -> dict:
        if enabled and Plugin._state["external_display"]:
            return dict(Plugin._state)
        Plugin._state["sdr_enabled"] = bool(enabled)
        await _offload(Plugin._save)
        if enabled:
            await _offload(Plugin._apply_sdr)
            if SDR_LOOK_ENABLED:
                self._schedule_sdr_look()
        else:
            await _offload(Plugin._release_sdr)
        return dict(Plugin._state)

    async def set_hdr_enabled(self, enabled: bool) -> dict:
        if enabled and (Plugin._state["external_display"]
                        or not Plugin._state["hdr_supported"]):
            return dict(Plugin._state)
        Plugin._state["hdr_enabled"] = bool(enabled)
        await _offload(Plugin._save)
        if enabled:
            self._schedule_hdr()
        else:
            await _offload(Plugin._release_hdr)
        return dict(Plugin._state)

    async def set_sdr_saturation(self, value: float) -> dict:
        Plugin._state["sdr_saturation"] = _clamp(
            value, SDR_MIN, Plugin._state["sdr_max"])
        self._schedule_sdr()
        if SDR_LOOK_ENABLED:
            self._schedule_sdr_look()
        return dict(Plugin._state)

    async def set_hdr_saturation(self, value: float) -> dict:
        Plugin._state["hdr_saturation"] = _clamp(value, HDR_MIN, HDR_MAX)
        self._schedule_hdr()
        return dict(Plugin._state)

    async def reset(self) -> dict:
        Plugin._state["sdr_saturation"] = SAT_DEFAULT
        Plugin._state["hdr_saturation"] = SAT_DEFAULT
        await _offload(Plugin._save)
        await _offload(Plugin._release_sdr)
        await _offload(Plugin._release_hdr)
        Plugin._state["sdr_enabled"] = False
        Plugin._state["hdr_enabled"] = False
        await _offload(Plugin._save)
        return dict(Plugin._state)

    # ── internals ──────────────────────────────────────────────────────────────

    @staticmethod
    def _save() -> None:
        for key in DEFAULT_SETTINGS:
            settings.setSetting(key, Plugin._state[key])
        settings.commit()

    @staticmethod
    def _apply_sdr() -> None:
        # The slider speaks in "1.0 is neutral"; the atom's neutral moves with
        # the branch, so the offset from neutral is what actually carries over.
        target, _look = _sdr_split(Plugin._state["sdr_saturation"])
        if _write_float_atom(ATOM_SDR_WIDENESS, target):
            Plugin._last_sdr_written = target
            Plugin._state["sdr_applied"] = True
            Plugin._state["reason"] = "applied"
        else:
            Plugin._state["sdr_applied"] = False
            Plugin._state["reason"] = "could not write to gamescope"

    @staticmethod
    def _release_sdr() -> None:
        Plugin._release_sdr_look()
        if not Plugin._state["sdr_applied"]:
            return
        target = Plugin._sdr_baseline
        if target is None:
            target = SDR_UNSET
        _write_float_atom(ATOM_SDR_WIDENESS, target)
        Plugin._last_sdr_written = None
        Plugin._state["sdr_applied"] = False
        decky.logger.info(f"{LOG} SDR released, restored {target:.3f}")

    @staticmethod
    def _build_and_apply_sdr_look(saturation: float) -> None:
        if saturation == 1.0:
            Plugin._release_sdr_look()
            return
        slot = 1 - Plugin._sdr_look_slot
        path = _write_look(saturation, slot, "g22", "sdr-look")
        if not path:
            Plugin._state["sdr_look_applied"] = False
            return
        if _write_string_atom(ATOM_LOOK_G22, path):
            Plugin._sdr_look_slot = slot
            Plugin._state["sdr_look_applied"] = True
            decky.logger.info(
                f"{LOG} SDR look {saturation:.2f} -> {os.path.basename(path)}")
        else:
            Plugin._state["sdr_look_applied"] = False

    @staticmethod
    def _release_sdr_look() -> None:
        if not Plugin._state["sdr_look_applied"]:
            return
        _write_string_atom(ATOM_LOOK_G22, "")
        Plugin._state["sdr_look_applied"] = False
        decky.logger.info(f"{LOG} SDR look cleared")

    @staticmethod
    def _release_hdr() -> None:
        # An empty path makes gamescope's LoadCubeLut fail to open, which clears
        # the look. That is the documented way back to no look at all.
        _write_string_atom(ATOM_LOOK_PQ, "")
        Plugin._state["hdr_applied"] = False
        decky.logger.info(f"{LOG} HDR look cleared")

    @staticmethod
    def _build_and_apply_hdr(saturation: float) -> None:
        slot = 1 - Plugin._look_slot
        path = _write_look(saturation, slot)
        if not path:
            Plugin._state["hdr_applied"] = False
            Plugin._state["reason"] = "could not build the LUT"
            return
        if _write_string_atom(ATOM_LOOK_PQ, path):
            Plugin._look_slot = slot
            Plugin._state["hdr_applied"] = True
            Plugin._state["reason"] = "applied"
            decky.logger.info(
                f"{LOG} HDR look {saturation:.2f} -> {os.path.basename(path)}")
        else:
            Plugin._state["hdr_applied"] = False
            Plugin._state["reason"] = "could not write to gamescope"

    @staticmethod
    def _track(task) -> None:
        Plugin._tasks.append(task)
        task.add_done_callback(lambda t: Plugin._tasks.remove(t)
                               if t in Plugin._tasks else None)

    def _schedule_sdr(self) -> None:
        """Write once the slider has been still for SDR_COALESCE_S.

        Also where the settings write happens, so a drag rewrites the file once
        instead of once per tick.
        """
        Plugin._sdr_generation += 1
        token = Plugin._sdr_generation

        async def _run():
            try:
                await asyncio.sleep(SDR_COALESCE_S)
                if token != Plugin._sdr_generation:
                    return  # superseded by a newer move
                await _offload(Plugin._save)
                if Plugin._state["sdr_enabled"]:
                    await _offload(Plugin._apply_sdr)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.error(f"{LOG} SDR apply failed: {e}")

        Plugin._track(asyncio.create_task(_run()))

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
                await asyncio.sleep(DEBOUNCE_S)
                if token != Plugin._sdr_look_generation:
                    return
                if not Plugin._state["sdr_enabled"]:
                    return
                _atom, look = _sdr_split(Plugin._state["sdr_saturation"])
                await _offload(Plugin._build_and_apply_sdr_look, look)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.error(f"{LOG} SDR look failed: {e}")
            finally:
                if token == Plugin._sdr_look_generation:
                    Plugin._state["sdr_generating"] = False

        Plugin._track(asyncio.create_task(_run()))

    def _schedule_hdr(self) -> None:
        """Rebuild the LUT once the slider has been still for DEBOUNCE_S."""
        Plugin._hdr_generation += 1
        token = Plugin._hdr_generation
        if Plugin._state["hdr_enabled"]:
            Plugin._state["generating"] = True

        async def _run():
            try:
                await asyncio.sleep(DEBOUNCE_S)
                if token != Plugin._hdr_generation:
                    return  # superseded by a newer move
                await _offload(Plugin._save)
                if not Plugin._state["hdr_enabled"]:
                    return
                value = Plugin._state["hdr_saturation"]
                if value == SAT_DEFAULT:
                    # Identity: no point loading a LUT that does nothing.
                    await _offload(Plugin._release_hdr)
                else:
                    await _offload(Plugin._build_and_apply_hdr, value)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.error(f"{LOG} HDR apply failed: {e}")
            finally:
                if token == Plugin._hdr_generation:
                    Plugin._state["generating"] = False

        Plugin._track(asyncio.create_task(_run()))

    @staticmethod
    def _reassert_looks_if_dropped() -> None:
        """Put a look back if the atom stopped pointing at ours.

        This only catches the visible half of the problem: if gamescope drops
        the look internally while the property still names our file, nothing we
        can read says so. The startup and resume ladders are what cover that.
        """
        for applied, atom, prefix, slot in (
            (Plugin._state["sdr_look_applied"], ATOM_LOOK_G22, "sdr-look",
             Plugin._sdr_look_slot),
            (Plugin._state["hdr_applied"], ATOM_LOOK_PQ, "hdr-look",
             Plugin._look_slot),
        ):
            if not applied:
                continue
            path = os.path.join(_runtime_dir(), f"{prefix}-{slot}.cube")
            if _read_string_atom(atom) == path or not os.path.exists(path):
                continue
            decky.logger.info(f"{LOG} {prefix} was dropped, writing it back")
            _write_string_atom(atom, path)

    @staticmethod
    def _configure_for_panel() -> bool:
        """Line the slider up with the branch this display takes.

        Run at startup and again whenever the display changes underneath us -
        docking swaps the panel without restarting anything, and the two
        branches of buildSDRColorimetry disagree about where neutral is.

        Returns True when something actually moved.
        """
        global SDR_NEUTRAL_ATOM, SDR_ATOM_SPAN
        before = (SDR_NEUTRAL_ATOM, SDR_ATOM_SPAN, Plugin._state["sdr_max"],
                  Plugin._state["hdr_supported"],
                  Plugin._state["external_display"])

        # Everything below is calibrated against the built-in panel: its gamut
        # decides the mapping and its EDID decides whether the HDR half exists.
        # An external display is somebody else's panel, so the plugin steps off
        # it rather than applying numbers that were measured elsewhere.
        Plugin._state["external_display"] = _display_is_external() is True
        if Plugin._state["external_display"]:
            after = (SDR_NEUTRAL_ATOM, SDR_ATOM_SPAN, Plugin._state["sdr_max"],
                     Plugin._state["hdr_supported"], True)
            return before != after

        wide = _panel_is_wide_gamut()

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

        Plugin._state["hdr_supported"] = _panel_supports_hdr() is not False
        Plugin._state["sdr_saturation"] = _clamp(
            Plugin._state["sdr_saturation"], SDR_MIN, Plugin._state["sdr_max"])

        after = (SDR_NEUTRAL_ATOM, SDR_ATOM_SPAN, Plugin._state["sdr_max"],
                 Plugin._state["hdr_supported"], False)
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
        if not Plugin._state["hdr_applied"]:
            return
        path = os.path.join(_runtime_dir(), f"hdr-look-{Plugin._look_slot}.cube")
        if os.path.exists(path):
            _write_string_atom(ATOM_LOOK_PQ, path)

    @staticmethod
    def _reassert() -> None:
        if Plugin._state["sdr_enabled"]:
            Plugin._apply_sdr()
            if Plugin._state["sdr_look_applied"]:
                path = os.path.join(
                    _runtime_dir(), f"sdr-look-{Plugin._sdr_look_slot}.cube")
                if os.path.exists(path):
                    _write_string_atom(ATOM_LOOK_G22, path)
        Plugin._reassert_hdr()

    @staticmethod
    def _watch_once() -> None:
        """Put our value back when something drops it, and only call it a
        conflict once that keeps happening.

        gamescope forgets these atoms when it re-initialises, so one
        disagreement is routine and heals itself. vibrantDeck writes the same
        SDR atom, and that disagrees every single time we look - which is what
        the strike count is there to tell apart.
        """
        if not Plugin._state["sdr_enabled"] or not Plugin._state["sdr_applied"]:
            Plugin._state["conflict"] = ""
            Plugin._reassert_strikes = 0
            return

        # A writer on another X server never shows up in what we read back, so
        # this is checked separately and says so in its own words. It costs a
        # process per server, so it goes on the slow tick.
        Plugin._slow_checks += 1
        if Plugin._slow_checks >= SLOW_CHECK_EVERY:
            Plugin._slow_checks = 0
            Plugin._reassert_looks_if_dropped()
            slow_conflict = _other_writer_present()
        else:
            slow_conflict = bool(Plugin._state["conflict"])
        if slow_conflict:
            Plugin._state["conflict"] = (
                "another plugin is driving the same SDR setting from a "
                "different X server (vibrantDeck does) - both writes take "
                "effect and the last one wins, so keep only one enabled")
            return

        live = _read_float_atom(ATOM_SDR_WIDENESS)
        if live is None or Plugin._last_sdr_written is None:
            return
        if abs(live - Plugin._last_sdr_written) <= 0.001:
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
        for delay in REASSERT_DELAYS_S:
            await asyncio.sleep(delay)
            await _offload(Plugin._reassert)
        Plugin._reassert_strikes = 0

    async def _refresh_panel(self):
        """Follow the display if it changes underneath us."""
        if not await _offload(Plugin._configure_for_panel):
            return
        if Plugin._state["external_display"]:
            decky.logger.info(f"{LOG} external display - standing down")
            await _offload(Plugin._release_sdr)
            await _offload(Plugin._release_hdr)
            return
        decky.logger.info(f"{LOG} back on the internal panel")
        # Whatever the external display left in the atom is not our baseline.
        Plugin._sdr_baseline = None
        if Plugin._state["sdr_enabled"]:
            await _offload(Plugin._apply_sdr)
            if SDR_LOOK_ENABLED:
                self._schedule_sdr_look()
        if Plugin._state["hdr_enabled"]:
            self._schedule_hdr()

    async def _monitor_loop(self):
        prev_mono = time.monotonic()
        prev_boot = _boottime()
        since_watch = 0.0
        since_panel = 0.0
        while True:
            try:
                await asyncio.sleep(SUSPEND_POLL_S)
                mono, boot = time.monotonic(), _boottime()
                asleep = (boot - prev_boot) - (mono - prev_mono)
                prev_mono, prev_boot = mono, boot
                if asleep > SUSPEND_SLACK_S:
                    decky.logger.info(
                        f"{LOG} resumed after ~{asleep:.0f}s suspended")
                    Plugin._track(
                        asyncio.create_task(self._reassert_repeatedly()))
                    since_watch = 0.0
                    continue
                since_watch += SUSPEND_POLL_S
                since_panel += SUSPEND_POLL_S
                if since_panel >= PANEL_CHECK_INTERVAL_S:
                    since_panel = 0.0
                    await self._refresh_panel()
                if Plugin._state["external_display"]:
                    continue
                if since_watch >= WATCH_INTERVAL_S:
                    since_watch = 0.0
                    await _offload(Plugin._watch_once)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.error(f"{LOG} monitor failed: {e}")
                await asyncio.sleep(2.0)

    async def _main(self):
        decky.logger.info(f"{LOG} startup v{updater.plugin_version()}")
        try:
            await _offload(updater.ssl_context)
        except Exception as e:
            decky.logger.warning(f"{LOG} update checks unavailable: {e}")
        try:
            await _offload(settings.read)
        except Exception:
            pass
        for key, fallback in DEFAULT_SETTINGS.items():
            Plugin._state[key] = settings.getSetting(key, fallback)
        # Settings written by an older build can sit outside the current range.
        # Widest possible range here; narrowed again once the panel is known.
        Plugin._state["sdr_saturation"] = _clamp(
            Plugin._state["sdr_saturation"], SDR_MIN, SDR_MAX_WIDE)
        Plugin._state["hdr_saturation"] = _clamp(
            Plugin._state["hdr_saturation"], HDR_MIN, HDR_MAX)

        # Decky starts plugins as a system service, so we can easily be up
        # before the session is. Finding nothing here is a race, not a verdict,
        # so the rest of startup waits in a task rather than giving up until
        # somebody reinstalls the plugin.
        Plugin._track(asyncio.create_task(self._start_when_ready()))

    async def _start_when_ready(self):
        delay = GAMESCOPE_RETRY_FIRST_S
        waited = False
        while not await _offload(_pick_display):
            if not waited:
                decky.logger.info(
                    f"{LOG} no gamescope atoms on any of "
                    f"{', '.join(DISPLAYS)} yet - waiting")
                waited = True
            Plugin._state["reason"] = "waiting for gamescope to come up"
            await asyncio.sleep(delay)
            delay = min(delay * 2, GAMESCOPE_RETRY_MAX_S)
        decky.logger.info(f"{LOG} talking to gamescope on {DISPLAY}")

        previous = -1
        for _ in range(GAMESCOPE_SETTLE_TRIES):
            count = await _offload(_count_gamescope_atoms, DISPLAY)
            if count == previous:
                break
            previous = count
            await asyncio.sleep(GAMESCOPE_SETTLE_S)
        decky.logger.info(f"{LOG} gamescope settled at {previous} atoms")

        # Remember what was there before we touch anything, so switching the
        # plugin off puts the display back rather than to our idea of default.
        #
        # Only worth keeping if it predates us. If the settings say the half was
        # already on, whatever the atom holds now is almost certainly what we
        # left there last time, and treating that as the baseline makes "off"
        # look exactly like "on" - which on a standard-gamut panel means being
        # stuck at the harshest setting gamescope has.
        if Plugin._state["sdr_enabled"]:
            Plugin._sdr_baseline = None
            decky.logger.info(f"{LOG} SDR was already on; no baseline to keep")
        else:
            Plugin._sdr_baseline = await _offload(
                _read_float_atom, ATOM_SDR_WIDENESS)
            if Plugin._sdr_baseline is not None:
                decky.logger.info(
                    f"{LOG} SDR baseline {Plugin._sdr_baseline:.3f}")

        # Both branches of buildSDRColorimetry are usable; they just start from
        # different places. Line the slider up with whichever one this panel
        # takes instead of withdrawing the control on one of them.
        await _offload(Plugin._configure_for_panel)

        # No PQ on the panel means the look would never be reached, so the half
        # is withdrawn rather than left as a control that does nothing. An EDID
        # we cannot read is not a no: it leaves the half alone.
        if not Plugin._state["hdr_supported"] and Plugin._state["hdr_enabled"]:
            Plugin._state["hdr_enabled"] = False
            await _offload(Plugin._save)

        Plugin._state["ready"] = True
        Plugin._state["reason"] = "ready"

        if Plugin._state["sdr_enabled"]:
            await _offload(Plugin._apply_sdr)
            if SDR_LOOK_ENABLED:
                self._schedule_sdr_look()
        else:
            # A look left in the atom by the previous run is still loaded until
            # something says otherwise, so "off" has to be asserted rather than
            # assumed.
            Plugin._state["sdr_look_applied"] = True
            await _offload(Plugin._release_sdr_look)
        if Plugin._state["hdr_enabled"]:
            self._schedule_hdr()
        else:
            await _offload(Plugin._release_hdr)

        # Same ladder as after a resume: the session can still be settling.
        Plugin._track(asyncio.create_task(self._reassert_repeatedly()))
        Plugin._track(asyncio.create_task(self._monitor_loop()))

    async def _unload(self):
        for task in list(Plugin._tasks):
            task.cancel()
        if Plugin._tasks:
            await asyncio.wait(list(Plugin._tasks), timeout=1.0)
        Plugin._tasks = []
        try:
            await _offload(Plugin._release_sdr)
            await _offload(Plugin._release_hdr)
        except Exception:
            pass
        decky.logger.info(f"{LOG} unloaded")

    async def _uninstall(self):
        await self._unload()
        # The generated LUTs live outside the plugin directory the loader is
        # about to delete.
        for prefix in ("hdr-look", "sdr-look"):
            for slot in (0, 1):
                try:
                    os.unlink(os.path.join(_runtime_dir(),
                                           f"{prefix}-{slot}.cube"))
                except OSError:
                    pass
