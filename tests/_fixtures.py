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
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _harness  # noqa: E402
_harness.install()

import main as M  # noqa: E402

# Unpatched originals, captured at import before any test replaces them.
READ_STRING_ATOM = M._read_string_atom
SAVE_SETTINGS = M.Plugin._save
PATH_EXISTS = M.os.path.exists
WRITE_LOOK = M._write_look


def drain(coro):
    """Await a coroutine, then cancel whatever it scheduled.

    Both setters hand the real work to a debounced task; the tests care about
    the value that lands in the state, not about the write that follows.
    """
    async def go():
        # Each asyncio.run creates a new loop, unlike Decky's single runtime.
        # Background saves now contend for the lock too; isolate it and await
        # cancellation before restoring the shared fixture.
        with patch.object(M.Plugin, "_mutation_lock", asyncio.Lock()):
            try:
                return await coro
            finally:
                tasks = [task for task in M.Plugin._tasks
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


class PluginCase(unittest.IsolatedAsyncioTestCase):
    """A ready Plugin on the built-in panel with every external effect faked."""

    def setUp(self):
        self.patches = []
        self.plugin = M.Plugin()
        state = dict(M.Plugin._state)
        state.update(M.DEFAULT_SETTINGS)
        state.update(ready=True, external_display=False, hdr_supported=True,
                     sdr_applied=False, sdr_look_applied=False, hdr_applied=False,
                     sdr_error="", sdr_look_error="", hdr_error="", reason="ready",
                     sdr_generating=False, generating=False,
                     hdr_applied_saturation=None, sdr_look_saturation=None)
        for key, value in dict(
            _state=state, _mutation_lock=asyncio.Lock(), _tasks=[],
            _unloading=False, _disconnected=False, _session_identity=None, _workers={}, _jobs={}, _due={},
            _settings_dirty=False, _settings_loaded=True, _other_writer=False, _sdr_baseline=0.4, _last_sdr_written=None,
            _resume_event=None, _reassert_generation=0,
            _sdr_look_path=None, _hdr_look_path=None,
            _sdr_look_baseline=None, _hdr_look_baseline=None,
            _sdr_generation=0, _sdr_look_generation=0, _hdr_generation=0,
            _slow_checks=0, _reassert_strikes=0, _look_slot=0, _sdr_look_slot=0,
        ).items():
            self.mock(M.Plugin, key, value)
        self.mock(M.Plugin, "_save")
        self.mock(M, "_lut_writes_disabled", False)
        self.mock(M, "_xprop_timeout", 3.0)
        self.mock(M, "DEBOUNCE_S", 0)
        self.mock(M, "SDR_COALESCE_S", 0)
        self.mock(M, "SDR_NEUTRAL_ATOM", 1.0)
        self.mock(M, "SDR_ATOM_SPAN", 2.0)
        self.mock(M, "_other_writer_present", return_value=False)
        self.live_sdr = 0.8
        self.strings = {}
        self.float_writer = self.mock(M, "_write_float_atom", side_effect=self.write_float)
        self.mock(M, "_read_float_atom", side_effect=lambda _: self.live_sdr)
        self.string_writer = self.mock(M, "_write_string_atom", side_effect=self.write_string)
        self.mock(M, "_read_string_atom", side_effect=lambda atom: self.strings.get(atom))
        self.mock(M.os.path, "isfile", return_value=True)
        self.mock(M.os.path, "exists", return_value=True)
        self.builder = self.mock(M, "_write_look", side_effect=lambda value, slot, *args:
                                 f"/tmp/{'sdr' if args and args[0] == 'g22' else 'hdr'}-look-{slot}.cube")

    def mock(self, obj, key, *args, **kwargs):
        item = patch.object(obj, key, *args, **kwargs)
        self.patches.append(item)
        return item.start()

    async def asyncTearDown(self):
        tasks = list(M.Plugin._tasks)
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
        await asyncio.gather(*list(M.Plugin._tasks))
