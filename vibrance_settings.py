# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""The plugin's settings.json on disk: read strictly, never lost, replaced whole.

dk_fs does the file work (strict JSON, atomic fsynced writes, a last-good copy,
damaged content kept as .corrupt-*). This adds what is the plugin's own: one
lock across the executor threads, keys the plugin does not know surviving a
save, and the notice the panel shows after a damaged file. Nothing here imports
`decky`.
"""

import threading

import dk_fs

# Reached through the default thread pool. Slider work and toggle RPCs can
# overlap there, so the file may not rely on event-loop ordering.
lock = threading.RLock()
# The file's content as last read or written; a save merges over it.
saved = {}


def load(path) -> str:
    """Read the file into `saved`; the notice to show when it was damaged, else "".

    OSError means try again later: the file is left as it is.
    """
    with lock:
        loaded = dk_fs.load_json(path, {}, validate=dk_fs.require_object)
        saved.clear()
        saved.update(loaded.value)
        if loaded.status == "lost":
            kept = f" The old file is kept as {loaded.kept}." if loaded.kept else ""
            return f"The saved settings could not be read, so defaults are in use.{kept}"
        if loaded.status == "recovered" and loaded.kept:
            return (f"The saved settings were damaged, so the last good copy is in use. "
                    f"The damaged file is kept as {loaded.kept}.")
        return ""  # also a missing file put back from its last good copy: nothing was lost


def write(path, values) -> None:
    """Replace the file with *values* merged over what it holds, all or nothing."""
    with lock:
        snapshot = {**saved, **values}
        dk_fs.write_json(path, snapshot)
        saved.clear()
        saved.update(snapshot)
