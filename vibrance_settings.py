# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""The plugin's settings.json on disk: read strictly, never lost, replaced whole.

Decky's SettingsManager swallows read errors and saves defaults over a file it
could not read, so the plugin does the reading and writing itself and only
borrows the manager's path and in-memory dict. Nothing here imports `decky`.
"""

import json
import os
import re
import stat
import tempfile
import threading
import time

import vibrance_host

# The manager is reached through the default thread pool. Slider work and
# toggle RPCs can overlap there, so the file may not rely on event-loop
# ordering once its work has been offloaded.
lock = threading.RLock()
# True while no settings file existed before Decky's manager created its empty
# one; only then may a missing file read as defaults.
new = False
# What tempfile names a write's temporary file; one a killed write left behind
# is removed. settings.json and its .corrupt-* copies never match.
ORPHAN = re.compile(r"settings-[a-z0-9_]{8}")


class SettingsUnreadable(ValueError):
    """The file was read but its content can never parse; retrying cannot help."""


def note_existing(path) -> None:
    """Record, before Decky's manager creates an empty file, whether one existed.

    A file that existed and later goes missing (an unmounted card, say) is then
    retried instead of replaced.
    """
    global new
    try:
        os.stat(path)
        new = False
    except FileNotFoundError:
        new = True
    except OSError:
        new = False


def read(manager) -> None:
    """Load the file into *manager*.settings, or raise.

    OSError and ValueError mean try again later; SettingsUnreadable means the
    content itself is the problem.
    """
    global new
    path = getattr(manager, "path", None)
    if path is None:  # in-memory stand-in; Decky always supplies a path
        manager.read()
        return
    with lock:
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
            if not new:
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
        manager.settings = values
        new = False


def set_aside(manager) -> str:
    """Keep unparseable settings beside the original and let startup continue.

    Nothing is deleted: the user can still recover values by hand, and the
    next read sees no file and starts from defaults.
    """
    global new
    with lock:
        aside = f"{manager.path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        os.replace(manager.path, aside)
        new = True
        return aside


def write(manager, values) -> None:
    """Replace the file with *values* merged over what it holds, all or nothing."""
    with lock:
        snapshot = dict(manager.settings) if isinstance(manager.settings, dict) else {}
        snapshot.update(values)
        path = getattr(manager, "path", None)
        if path is None:  # in-memory stand-in; Decky always supplies a path
            manager.settings.update(snapshot)
            manager.commit()
            return
        folder = os.path.dirname(path)
        vibrance_host.remove_stale(folder, ORPHAN.fullmatch)
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            mode = None
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                             prefix="settings-", delete=False) as f:
                tmp = f.name
                if mode is not None and hasattr(os, "fchmod"):
                    os.fchmod(f.fileno(), mode)
                json.dump(snapshot, f, indent=4, ensure_ascii=False, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            # The rename itself must survive a power cut. Windows cannot open a
            # folder; anywhere it fails the new file is already in place.
            try:
                descriptor = os.open(folder or ".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                pass
            manager.settings = snapshot
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
