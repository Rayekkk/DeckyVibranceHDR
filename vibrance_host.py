# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""What the plugin needs to know about the machine it runs on.

Nothing here imports `decky`, so the module can be exercised by the test
suites without the loader present. Running programs (and the host's native
ones when Decky is emulated) is dk_process's job.
"""

import os
import stat
import time


def file_identity(path):
    """Detect replacement, edits and deletion at a stable pathname."""
    if not path:
        return None
    try:
        info = os.stat(path)
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    except OSError:
        return None


def loggable(exc) -> str:
    """*exc* without the folders of a path it names: they hold the user name."""
    if isinstance(exc, OSError) and exc.strerror and exc.filename is not None:
        return f"{exc.strerror}: {os.path.basename(str(exc.filename))}"
    return str(exc)


def remove_stale(folder, belongs) -> None:
    """Delete the entries of *folder* that *belongs* names and a killed
    process left behind.

    Only those older than a minute: another instance may still be writing
    one during a reload, and the native LUT worker's deadline is 30 s.
    """
    try:
        entries = os.listdir(folder or ".")
    except OSError:
        return
    for entry in entries:
        if belongs(entry):
            stale = os.path.join(folder, entry)
            try:
                if time.time() - os.lstat(stale).st_mtime > 60:
                    os.unlink(stale)
            except OSError:
                pass


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
