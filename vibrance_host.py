# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""What the plugin needs to know about the machine it runs on.

Nothing here imports `decky`, so the module can be exercised by the test
suites without the loader present.
"""

import functools
import os
import stat
import sys

# The host's own interpreter, reached past the emulator's view of the root.
NATIVE_PYTHON = "/proc/self/root/usr/bin/python3"

# Steam exports LD_LIBRARY_PATH pointing at its own bundled libraries and the
# plugin inherits it, so a system binary spawned from here can load Steam's copy
# of libreadline or libc and die on a missing symbol. Everything we spawn is a
# system tool, so the loader variables go.
LOADER_VARS = ("LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT")


def system_env(**extra) -> dict:
    """The environment for a system tool, without Steam's loader variables."""
    env = {k: v for k, v in os.environ.items() if k not in LOADER_VARS}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env.update(extra)
    return env


@functools.cache
def interpreter_headers():
    """The ELF headers of the host Python and of the one running us.

    They cannot change while the plugin runs. An OSError is raised, not
    returned, so functools.cache keeps only a successful read and a passing
    error (too many open files, say) is retried on the next call.
    """
    with open(NATIVE_PYTHON, "rb") as f:
        native = f.read(20)
    with open(sys.executable, "rb") as f:
        loader = f.read(20)
    return native, loader


def native_python():
    """The host's ARM Python when Decky's frozen interpreter runs under x86
    emulation (Armada's FEX), else None."""
    try:
        native, loader = interpreter_headers()
    except OSError:
        return None
    if (native[:5] == b"\x7fELF\x02" and native[5] == 1
            and int.from_bytes(native[18:20], "little") == 183
            and loader[:5] == b"\x7fELF\x02" and loader[5] == 1
            and int.from_bytes(loader[18:20], "little") == 62):
        return NATIVE_PYTHON
    return None


def file_identity(path):
    """Detect replacement, edits and deletion at a stable pathname."""
    if not path:
        return None
    try:
        info = os.stat(path)
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    except OSError:
        return None


def read_edid(path):
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


def builtin_panel_edid_path():
    """The built-in panel's EDID as the kernel reports it, or None.

    gamescope publishes GAMESCOPE_DISPLAY_EDID_PATH only when its session sets
    GAMESCOPE_PATCHED_EDID_FILE (update_edid_prop in steamcompmgr.cpp); without
    it gamescope reads this same EDID from DRM. Built-in panels sit on eDP,
    DSI or LVDS connectors, which external outputs do not use (gamescope's own
    test in DRMBackend.cpp).
    """
    try:
        names = sorted(os.listdir(DRM_SYSFS))
    except OSError:
        return None
    for name in names:
        if not any(kind in name for kind in ("-eDP-", "-DSI-", "-LVDS-")):
            continue
        try:
            with open(os.path.join(DRM_SYSFS, name, "status"), encoding="ascii") as status:
                if status.read().strip() != "connected":
                    continue
        except OSError:
            continue
        return os.path.join(DRM_SYSFS, name, "edid")
    return None
