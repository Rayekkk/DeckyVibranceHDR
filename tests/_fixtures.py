# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Fixtures shared by the topic suites.

Importing this installs the loader stand-ins from _harness, so main.py can be
imported next. PluginCase gives each test a fresh Plugin whose gamescope reads,
writes and LUT builds are all faked in memory.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _harness  # noqa: E402
_harness.install()

import main as M  # noqa: E402

# Never the machine's own panel: a laptop running the suite has an eDP EDID.
M.vibrance_host.DRM_SYSFS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "no-such-drm")
# Nor a real Downloads folder: an updater test that loses its stand-in user
# would otherwise write into the host's /home/deck/Downloads.
import lego_updater  # noqa: E402
lego_updater.FALLBACK_DOWNLOADS = os.path.join(tempfile.gettempdir(), "dvh-tests-no-such-downloads")
# Nor its X servers: a desktop session's sockets would answer real xprop calls.
M.vibrance_x11.SOCKET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "no-such-x11")

# Unpatched originals, captured at import before any test replaces them.
READ_STRING_ATOM = M.vibrance_x11.read_string_atom
SAVE_SETTINGS = M.Plugin._save
PATH_EXISTS = M.os.path.exists
WRITE_LOOK = M.vibrance_effects.write_look


def drain(coro):
    """Await a coroutine, then cancel whatever it scheduled.

    Both setters hand the real work to a debounced task; the tests care about
    the value that lands in the state, not about the write that follows.
    """
    async def go():
        # Each asyncio.run creates a new loop, unlike Decky's single runtime.
        # Background saves now contend for the lock too; isolate it and await
        # cancellation before restoring the shared fixture.
        with patch.object(M.state, "mutation_lock", asyncio.Lock()):
            try:
                return await coro
            finally:
                tasks = [task for task in M.state.tasks
                         if task.get_loop() is asyncio.get_running_loop()]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    return asyncio.run(go())


def edid_with_red(red_x: float, red_y: float) -> bytes:
    """A valid base EDID whose only content is the red primary."""
    blob = bytearray(128)
    rx, ry = round(red_x * 1024), round(red_y * 1024)
    blob[0x19] = ((rx & 0x03) << 6) | ((ry & 0x03) << 4)
    blob[0x1B] = rx >> 2
    blob[0x1C] = ry >> 2
    return _harness.valid_edid(blob)


def root_dump(external=None, edid_path=None):
    """`xprop -root` with gamescope's two panel atoms; None leaves one out."""
    lines = ["GAMESCOPE_FOCUSED_APP(CARDINAL) = 0"]
    if external is not None:
        lines.append(f"{M.vibrance_x11.ATOM_IS_EXTERNAL}(CARDINAL) = {int(external)}")
    if edid_path is not None:
        quoted = edid_path.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{M.vibrance_x11.ATOM_EDID_PATH}(UTF8_STRING) = "{quoted}"')
    return "\n".join(lines)


def _snapshot(namespace):
    """Every attribute of a class or module, and the contents of its dicts and lists."""
    entries = {key: value for key, value in vars(namespace).items()
               if not (key.startswith("__") and key.endswith("__"))}
    contents = {key: (dict(value) if isinstance(value, dict) else list(value))
                for key, value in entries.items() if isinstance(value, (dict, list))}
    return namespace, entries, contents


def _restore(snapshot):
    namespace, entries, contents = snapshot
    for key in [k for k in vars(namespace) if k not in entries
                and not (k.startswith("__") and k.endswith("__"))]:
        delattr(namespace, key)
    for key, value in entries.items():
        if key in contents:
            value.clear()
            value.update(contents[key]) if isinstance(value, dict) else value.extend(contents[key])
        if vars(namespace).get(key) is not value:
            setattr(namespace, key, value)


def _fresh_state():
    """A state as a new Decky process builds it: every field gone, then reset().

    Clearing first means a field reset() forgets fails loudly instead of
    keeping the previous test's value.
    """
    for name in type(M.state).__slots__:
        if hasattr(M.state, name):
            delattr(M.state, name)
    M.state.reset()


class StateGuard:
    """Start each test from a fresh plugin state and undo what it patches.

    The plugin's memory is one object, so each test begins and ends with
    state.reset(). Module globals - main's tunables, the other modules' own
    state and functions a test replaces directly - are snapshotted and put
    back, so results never depend on the order tests run in. Wrapping run()
    covers setUp, the test and every cleanup.

    It restores the attributes themselves and the contents of dicts and lists
    held directly in them, not deeper objects they point to: the stand-in
    settings manager's store, for one, is still reset by the tests using it.
    """

    def run(self, result=None):
        saved = [_snapshot(M.Plugin), _snapshot(M), _snapshot(M.vibrance_lutfile),
                 _snapshot(M.vibrance_x11), _snapshot(M.vibrance_host),
                 _snapshot(M.vibrance_settings), _snapshot(M.vibrance_effects),
                 _snapshot(M.vibrance_panel), _snapshot(M.vibrance_state), _snapshot(M.updater)]
        _fresh_state()
        try:
            return super().run(result)
        finally:
            for snapshot in saved:
                _restore(snapshot)
            _fresh_state()


class PluginTest(StateGuard, unittest.TestCase):
    """A synchronous test whose changes to Plugin and main are undone."""


class PluginCase(StateGuard, unittest.IsolatedAsyncioTestCase):
    """A ready Plugin on the built-in panel with every external effect faked."""

    def setUp(self):
        self.patches = []
        self.plugin = M.Plugin()
        # A fresh state (StateGuard reset it) that has loaded its settings and
        # connected to gamescope on the built-in panel.
        M.state.public.update(ready=True, reason="ready")
        M.state.settings_loaded = True
        M.state.sdr_baseline = 0.4
        # Speed, not blind spots: no disk writes and no debounce here. The real
        # save and the debounce have tests of their own (SaveWrites, Debounce).
        self.mock(M.Plugin, "_save")
        self.mock(M.vibrance_lutfile, "disabled", False)
        self.mock(M.vibrance_x11, "timeout", 3.0)
        self.mock(M, "DEBOUNCE_S", 0)
        self.mock(M, "SDR_COALESCE_S", 0)
        self.mock(M, "SETTINGS_RETRY_S", 0.01)
        self.mock(M.vibrance_x11, "foreign_sdr_values", return_value=())
        self.live_sdr = 0.8
        self.strings = {}
        self.float_writer = self.mock(M.vibrance_x11, "write_float_atom", side_effect=self.write_float)
        self.mock(M.vibrance_x11, "read_float_atom", side_effect=lambda _: self.live_sdr)
        self.string_writer = self.mock(M.vibrance_x11, "write_string_atom", side_effect=self.write_string)
        self.mock(M.vibrance_x11, "read_string_atom", side_effect=lambda atom: self.strings.get(atom))
        self.mock(M.os.path, "isfile", return_value=True)
        self.mock(M.os.path, "exists", return_value=True)
        # The fake builder's files live here, so the plugin knows them as its own.
        self.mock(M.vibrance_effects, "runtime_dir", return_value="/tmp")
        self.builder = self.mock(M.vibrance_effects, "write_look", side_effect=lambda value, slot, *args:
                                 f"/tmp/{'sdr' if args and args[0] == 'g22' else 'hdr'}-look-{slot}.cube")

    def mock(self, obj, key, *args, **kwargs):
        item = patch.object(obj, key, *args, **kwargs)
        self.patches.append(item)
        return item.start()

    async def asyncTearDown(self):
        tasks = list(M.state.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for item in reversed(self.patches):
            item.stop()

    def write_float(self, atom, value):
        self.live_sdr = value
        return True

    def write_string(self, atom, value):
        self.strings[atom] = value
        return True

    async def drain(self):
        await asyncio.gather(*list(M.state.tasks))
