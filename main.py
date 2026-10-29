# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""DeckyVibranceHDR - saturation control for both SDR and HDR content.

Decky's entry point. Every RPC answers {ok, error?, state?} (DeckyKit D2,
dk_rpc) and hands the work to vibrance_control, which holds the plugin's
logic and never imports `decky`; this file gives it Decky's logger and folders.
"""

import asyncio
import json
import os
import sys

import decky

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import dk_rpc  # noqa: E402
import dk_update  # noqa: E402
import vibrance_control  # noqa: E402
import vibrance_effects  # noqa: E402
import vibrance_state  # noqa: E402
from vibrance_state import LOG, state  # noqa: E402

REPOSITORY = "Rayekkk/DeckyVibranceHDR"


def plugin_version() -> str:
    """The installed version as the loader understands it.

    DECKY_PLUGIN_VERSION is authoritative: Decky takes it from package.json,
    so the panel and Decky's own plugin list can never disagree. plugin.json
    is the fallback outside the loader.
    """
    version = os.environ.get("DECKY_PLUGIN_VERSION", "")
    if version:
        return version
    try:
        with open(os.path.join(PLUGIN_DIR, "plugin.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        version = manifest.get("version") if isinstance(manifest, dict) else None
        return version if isinstance(version, str) and version else "0.0.0"
    except (OSError, ValueError):
        return "0.0.0"


# The state the RPCs read exists from import; _main only has to hand over
# Decky's logger and folders before the first call is answered.
_ready = asyncio.Event()
# A refusal (bad input, or settings not loaded yet) answers with the state as it is; any other error is a bug.
rpc = dk_rpc.boundary(decky.logger, _ready, shown=(vibrance_state.Refused,), state=lambda _: dict(state.public))
controller = vibrance_control.Controller()


class Plugin:
    @rpc
    async def get_state(self) -> dict:
        return dk_rpc.ok(await controller.get_state())

    @rpc
    async def get_version(self) -> dict:
        # home: the About page shows a saved update under it as ~/…
        return dk_rpc.ok(version=plugin_version(), home=decky.DECKY_USER_HOME)

    @rpc
    async def check_for_updates(self) -> dict:
        return await asyncio.to_thread(self.updates.check, True)

    @rpc
    async def perform_update(self, expected_version: str) -> dict:
        return await asyncio.to_thread(self.updates.download, expected_version)

    @rpc
    async def set_sdr_enabled(self, enabled: bool, saturation=None) -> dict:
        return dk_rpc.ok(await controller.set_sdr_enabled(enabled, saturation))

    @rpc
    async def set_hdr_enabled(self, enabled: bool, saturation=None) -> dict:
        return dk_rpc.ok(await controller.set_hdr_enabled(enabled, saturation))

    @rpc
    async def set_sdr_saturation(self, value: float) -> dict:
        return dk_rpc.ok(await controller.set_sdr_saturation(value))

    @rpc
    async def set_hdr_saturation(self, value: float) -> dict:
        return dk_rpc.ok(await controller.set_hdr_saturation(value))

    @rpc
    async def reset(self) -> dict:
        return dk_rpc.ok(await controller.reset())

    async def _main(self):
        try:
            vibrance_state.logger = decky.logger
            vibrance_effects.settings_dir = decky.DECKY_PLUGIN_SETTINGS_DIR
            vibrance_control.settings_path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
            self.updates = dk_update.Updates(
                repo=REPOSITORY, folder="DeckyVibranceHDR", plugin_name="Decky Vibrance HDR",
                package_name="deckyvibrancehdr", current=plugin_version(), home=decky.DECKY_USER_HOME,
                required=(*dk_update.REQUIRED, "vibrance_lut.py"), logger=decky.logger)
            decky.logger.info(f"{LOG} startup v{plugin_version()}")
        finally:
            _ready.set()  # the controller answers from here on, "starting up" until it is
        await controller._main()

    async def _unload(self):
        # Nothing here may yield (vibrance_control.Controller._unload says why): the controller's _unload awaits
        # nothing and saves and restores first; close() never waits for the network.
        await controller._unload()
        updates = getattr(self, "updates", None)
        if updates is not None:
            updates.close()

    async def _uninstall(self):
        # Decky's shutdown has just run _unload; repeating it would spend the
        # five-second stop budget restoring and saving a second time.
        if not state.unloading:
            await self._unload()
        await controller._uninstall()
