# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Building the .cube files gamescope loads, and putting them on disk safely.

vibrance_lut.py does the maths; this module decides where it runs (in-process,
or in the host's native Python when Decky itself is emulated) and makes sure
gamescope only ever sees complete files. Nothing here imports `decky`.
"""

import contextlib
import os
import subprocess
import threading
from collections import OrderedDict

import dk_fs
import dk_process
import vibrance_host
import vibrance_lut

# 33 is the usual size for a creative LUT and costs about a third of a second to
# build. That is far too slow to redo on every tick of a slider, so a change is
# only acted on once the value has stopped moving (the plugin's debounce).
SIZE = 33

# A cancelled run_in_executor job keeps running. Serialising writes prevents it
# from sharing the process's temporary name with its replacement.
lock = threading.RLock()
# Metadata only: the two HDR and two SDR slots already contain the LUT text.
_cache = OrderedDict()
# Set while the plugin unloads: no build may publish a file after that.
disabled = False


# The native worker's deadline; a build takes about a third of a second.
WORKER_S = 30


def generate(saturation, size, encoding, source_white, cancelled) -> str:
    """The .cube text, built natively when Decky runs under emulation.

    Emulated Python is far too slow for the maths, so the host's own
    interpreter runs vibrance_lut.py as a script (dk_process: python -I, a
    deadline, killed on cancel or unload).
    """
    if not dk_process.emulated():
        return vibrance_lut.generate(saturation, size, encoding, source_white,
                                     cancelled=cancelled)
    # Same validation as generate(), before arguments become CLI strings.
    vibrance_lut.validate(saturation, size, encoding, source_white)
    try:
        code, output, error = dk_process.run_helper(
            [os.path.realpath(vibrance_lut.__file__), repr(saturation), str(size), encoding,
             repr(source_white[0]), repr(source_white[1])],
            WORKER_S, lambda: cancelled() or disabled, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except InterruptedError:
        raise vibrance_lut.BuildCancelled() from None
    except TimeoutError:
        raise ValueError("native LUT worker timed out") from None
    if code:
        raise ValueError("native LUT worker failed: " + error.decode(errors="replace")[:300])
    return output.decode("utf-8")


def write(path, saturation, encoding, source_white, cancelled) -> bool:
    """Build the LUT into *path*; False when cancelled, an exception on failure.

    The file is written beside its target and renamed over it, so gamescope
    never loads half a table. An identical request whose file is unchanged on
    disk is not built again.
    """
    def build(f):
        text = generate(saturation, SIZE, encoding, source_white, cancelled)
        if disabled or cancelled():
            raise vibrance_lut.BuildCancelled()
        f.write(text.encode("utf-8"))

    @contextlib.contextmanager
    def publish():  # the last moment a cancel keeps the new table from gamescope
        if disabled or cancelled():
            raise vibrance_lut.BuildCancelled()
        yield

    with lock:
        if disabled or cancelled():
            return False
        try:
            # Include types so invalid True/33.0 cannot hit valid 1.0/33
            # records and bypass generate()'s input validation.
            request = (type(saturation), saturation, type(SIZE), SIZE,
                       encoding, tuple(source_white))
            cached = _cache.get(path)
            if (cached is not None and cached[0] == request
                    and cached[1] == vibrance_host.file_identity(path)):
                if disabled or cancelled():
                    return False
                _cache.move_to_end(path)
                return True
            # Temporaries of versions before dk_fs.write_atomic, which sweeps its own.
            folder, name = os.path.split(path)
            vibrance_host.remove_stale(folder, lambda entry: entry.startswith(name + ".")
                                       and entry.endswith(".tmp"))
            # The temporary is opened before build runs: a folder that cannot be
            # written fails before a full 33-cube on every retry.
            dk_fs.write_atomic(path, build, 0o644, commit=publish)
        except vibrance_lut.BuildCancelled:
            return False
        identity = vibrance_host.file_identity(path)
        if identity is not None:
            _cache[path] = (request, identity)
            _cache.move_to_end(path)
            while len(_cache) > 4:
                _cache.popitem(last=False)
        else:
            _cache.pop(path, None)
        return True


def wait_for_writer() -> None:
    """Return once no build holds the files."""
    with lock:
        pass
