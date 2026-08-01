# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Minimal stand-ins for the two modules DeckyLoader injects at runtime.

Importing main.py outside the loader fails on `import decky` and
`from settings import SettingsManager`, so tests put these on sys.modules first.
Mirrors the harness used by the sibling plugins.
"""

import os
import sys
import types


def install(settings_dir: str | None = None) -> None:
    # lego_updater imports pwd, which only exists on Unix. Stubbing it keeps the
    # logic suite runnable on a Windows dev box without the shared updater having
    # to differ from the copy in the sibling plugins.
    if "pwd" not in sys.modules:
        try:
            import pwd  # noqa: F401
        except ModuleNotFoundError:
            pwd_mod = types.ModuleType("pwd")
            pwd_mod.struct_passwd = tuple
            pwd_mod.getpwall = lambda: []
            pwd_mod.getpwnam = lambda name: None
            sys.modules["pwd"] = pwd_mod

    if "decky" not in sys.modules:
        decky = types.ModuleType("decky")

        class _Logger:
            @staticmethod
            def info(msg):
                print(f"[info ] {msg}")

            @staticmethod
            def warning(msg):
                print(f"[warn ] {msg}")

            @staticmethod
            def error(msg):
                print(f"[error] {msg}")

        decky.logger = _Logger()
        decky.DECKY_PLUGIN_SETTINGS_DIR = settings_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "_settings"
        )
        os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
        sys.modules["decky"] = decky

    if "settings" not in sys.modules:
        settings_mod = types.ModuleType("settings")

        class SettingsManager:
            def __init__(self, name, settings_directory):
                self._store = {}

            def read(self):
                return self._store

            def getSetting(self, key, default=None):
                return self._store.get(key, default)

            def setSetting(self, key, value):
                self._store[key] = value

            def commit(self):
                pass

        settings_mod.SettingsManager = SettingsManager
        sys.modules["settings"] = settings_mod
