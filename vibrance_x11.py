# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Talking to gamescope: its X servers and the root-window atoms on them.

Every read and write goes through xprop, the one X client every gamescope
session ships. Nothing here imports `decky`.
"""

import math
import os
import re
import struct
import subprocess

import vibrance_host

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
SOCKET_DIR = "/tmp/.X11-unix"
FALLBACK = (":0", ":1")
DISPLAY = ":0"          # both replaced by pick_display()
DISPLAYS = FALLBACK
# Seconds one xprop may take; the plugin shortens it while unloading.
timeout = 3.0


def system_env(**extra) -> dict:
    return vibrance_host.system_env(**{"DISPLAY": DISPLAY, **extra})


def run_xprop(command, **kwargs):
    if vibrance_host.native_python() is not None:
        # Absolute host path plus inheritable-FD policy enables posix_spawn,
        # avoiding a fork of the large emulated Decky process on every atom.
        command = ["/proc/self/root/usr/bin/xprop", *command[1:]]
        kwargs["close_fds"] = False
    return subprocess.run(command, **kwargs)


def float_to_cardinal(value: float) -> int:
    """gamescope stores these floats bit-cast into a CARDINAL."""
    return struct.unpack("I", struct.pack("f", float(value)))[0]


def cardinal_to_float(raw):
    if raw is None:
        return None
    try:
        value = struct.unpack("f", struct.pack("I", raw & 0xFFFFFFFF))[0]
        return value if math.isfinite(value) else None
    except struct.error:
        return None


def xprop(*args, display=None):
    """stdout of `xprop -root ...`, or None when xprop could not run or timed out.

    xprop exits 0 with nothing useful when an atom is absent and prints nothing
    when the server is gone, so "" is an answer. None is not: it says nothing
    about gamescope, only that this one read did not complete.
    """
    try:
        return run_xprop(
            ["xprop", "-root", *args],
            capture_output=True, text=True, timeout=timeout,
            env=system_env() if display is None else system_env(DISPLAY=display),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def xprop_set(name: str, fmt: str, value: str) -> bool:
    try:
        run_xprop(
            ["xprop", "-root", "-f", name, fmt, "-set", name, value],
            capture_output=True, timeout=timeout, env=system_env(), check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def parse_int_atom(out):
    if "= " not in out:
        return None
    try:
        return int(out.split("= ", 1)[1].strip())
    except ValueError:
        return None


def read_int_atom(name: str):
    """A plain CARDINAL, not one of the floats gamescope bit-casts."""
    return parse_int_atom(xprop(name) or "")


def display_is_external():
    """True when gamescope is driving something other than the built-in panel.

    gamescope publishes this itself, so there is no guessing from the EDID. A
    missing atom means an older build and is read as internal: withdrawing the
    whole plugin over an absent property would be worse than the alternative.
    """
    value = read_int_atom(ATOM_IS_EXTERNAL)
    return None if value is None else bool(value)


def read_float_atom(name: str):
    raw = parse_int_atom(xprop(name) or "")
    return None if raw is None else cardinal_to_float(raw)


def write_float_atom(name: str, value: float) -> bool:
    return xprop_set(name, "32c", str(float_to_cardinal(value)))


def write_string_atom(name: str, value: str) -> bool:
    """UTF8_STRING, matching what gamescope writes for its own path atoms."""
    return xprop_set(name, "8u", value)


def read_string_atom(name: str):
    return parse_string_atom(xprop(name) or "")


def parse_string_atom(out):
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


def session_identity():
    """The local X socket identifies a session even at the same DISPLAY."""
    number = DISPLAY.removeprefix(":").split(".", 1)[0]
    identity = vibrance_host.file_identity(os.path.join(SOCKET_DIR, f"X{number}"))
    return (DISPLAY, identity) if identity is not None else None


def read_root_properties(display: str):
    return xprop(display=display)


def count_gamescope_atoms(display: str) -> int:
    out = read_root_properties(display) or ""
    return sum(1 for line in out.splitlines() if line.startswith("GAMESCOPE"))


def display_candidates():
    """Every X server on the box, read off its socket.

    Nothing about the numbering is fixed: how many XWayland servers gamescope
    starts is a command line option (--xwayland-count) and which numbers they
    get depends on what was already taken, so a desktop session holding :0
    pushes them along. Guessing a pair covers this device and little else.
    """
    try:
        names = sorted(name for name in os.listdir(SOCKET_DIR)
                       if name.startswith("X") and name[1:].isdigit())
    except OSError:
        return FALLBACK
    return tuple(f":{name[1:]}" for name in names) or FALLBACK


def pick_display():
    """Settle on an X server gamescope is actually behind.

    gamescope publishes GAMESCOPE_DISPLAY_IS_EXTERNAL on its first server
    only, and docking is detected from that atom, so a server carrying it
    wins; between the rest, the fuller surface does. Returns the server (or
    None) and what each one showed, for the log.
    """
    global DISPLAY, DISPLAYS
    DISPLAYS = display_candidates()
    best, best_rank = None, (False, 0)
    seen = []
    for candidate in DISPLAYS:
        lines = (read_root_properties(candidate) or "").splitlines()
        count = sum(1 for line in lines if line.startswith("GAMESCOPE"))
        root = any(line.startswith(ATOM_IS_EXTERNAL + "(") for line in lines)
        seen.append(f"{candidate}: {count} gamescope atoms" + (", docking state" if root else ""))
        if count and (root, count) > best_rank:
            best, best_rank = candidate, (root, count)
    if best is not None:
        DISPLAY = best
    return best, "; ".join(seen)


def foreign_sdr_values():
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
        out = xprop(ATOM_SDR_WIDENESS, display=candidate)
        if out is None:
            return None
        if "= " in out:
            found.append((candidate, out.split("= ", 1)[1].strip()))
    return tuple(found)
