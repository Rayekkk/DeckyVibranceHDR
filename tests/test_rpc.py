# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""main.py: every RPC answers {ok, error?, state?} (DeckyKit D2) and hands the work to the controller."""

import json
import os
from unittest.mock import MagicMock, patch

from _fixtures import M, PluginCase  # installs the decky stand-in first
import dk_update
import main


class RpcAnswers(PluginCase):
    def setUp(self):
        super().setUp()
        main._ready.set()
        self.rpc = main.Plugin()
        self.rpc.updates = MagicMock()
        self.mock(main, "controller", self.plugin)  # PluginCase's controller, every effect faked

    async def test_state_and_changes_come_back_as_d2_answers(self):
        answer = await self.rpc.get_state()
        self.assertEqual(answer, {"ok": True, "state": M.state.public})
        self.assertIsNot(answer["state"], M.state.public, "a copy, never the live state")
        answer = await self.rpc.set_sdr_saturation(1.5)
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["state"]["sdr_saturation"], 1.5)
        for call in (self.rpc.set_sdr_enabled(False), self.rpc.set_hdr_enabled(True, 1.2),
                     self.rpc.set_hdr_saturation(1.3), self.rpc.reset()):
            self.assertEqual(set(await call), {"ok", "state"})

    async def test_bad_input_and_unloaded_settings_are_refused_with_the_state(self):
        for call, why in ((self.rpc.set_sdr_enabled("yes"), "enabled must be a boolean"),
                          (self.rpc.set_hdr_saturation(float("nan")), "finite number"),
                          (self.rpc.set_sdr_saturation("1.2"), "finite number")):
            with self.assertLogs(main.decky.logger, "WARNING") as logged:
                answer = await call
            self.assertIn("refused: ", logged.output[0])
            self.assertIn(why, logged.output[0])
            self.assertFalse(answer["ok"])
            self.assertIn(why, answer["error"])
            self.assertEqual(answer["state"], M.state.public, "the panel shows the real state at once")
        M.state.settings_loaded = False
        answer = await self.rpc.reset()
        self.assertEqual((answer["ok"], answer["error"]), (False, "settings have not loaded; retry after startup"))

    async def test_an_unexpected_failure_never_reaches_the_panel(self):
        self.mock(self.plugin, "set_sdr_saturation", side_effect=RuntimeError("/home/someone/x"))
        with self.assertLogs(main.decky.logger, "ERROR"):
            answer = await self.rpc.set_sdr_saturation(1.2)
        self.assertFalse(answer["ok"])
        self.assertIn("RuntimeError", answer["error"])
        self.assertNotIn("/home", answer["error"])

    async def test_a_value_error_from_a_bug_is_not_taken_for_a_refusal(self):
        self.mock(self.plugin, "reset", side_effect=ValueError("singular matrix"))
        with self.assertLogs(main.decky.logger, "ERROR") as logged:
            answer = await self.rpc.reset()
        self.assertEqual(answer["error"], "Unexpected error (ValueError). Details are in the Decky log.")
        self.assertNotIn("state", answer)
        self.assertIn("Traceback", logged.output[0])

    async def test_version_and_updates_go_to_the_shared_updater(self):
        with patch.dict(os.environ, {"DECKY_PLUGIN_VERSION": "9.9.9"}):
            self.assertEqual(await self.rpc.get_version(), {"ok": True, "version": "9.9.9"})
        with patch.dict(os.environ, {"DECKY_PLUGIN_VERSION": ""}):
            self.assertRegex((await self.rpc.get_version())["version"], r"^\d+\.\d+\.\d+$", "plugin.json's")
        self.rpc.updates.check.return_value = {"ok": True, "update_available": False}
        self.assertEqual(await self.rpc.check_for_updates(), {"ok": True, "update_available": False})
        self.rpc.updates.check.assert_called_once_with(True)
        self.rpc.updates.download.return_value = {"ok": True, "path": "/x.zip", "version": "2.0.0"}
        self.assertEqual((await self.rpc.perform_update("2.0.0"))["path"], "/x.zip")
        self.rpc.updates.download.assert_called_once_with("2.0.0")

    async def test_main_hands_over_decky_and_answers_before_the_controller_is_up(self):
        main._ready.clear()
        self.mock(self.plugin, "_main")
        await self.rpc._main()
        self.assertTrue(main._ready.is_set())
        self.assertIs(M.vibrance_state.logger, main.decky.logger)
        self.assertEqual(M.settings_path, os.path.join(main.decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json"))
        self.assertIsInstance(self.rpc.updates, dk_update.Updates)
        self.assertEqual(self.rpc.updates.required[-1], "vibrance_lut.py")
        self.plugin._main.assert_awaited_once()

    async def test_unload_saves_and_restores_then_stops_a_download_and_never_gives_the_loop_back(self):
        order = []
        self.rpc.updates.close.side_effect = lambda: order.append("updates")
        self.mock(M.Controller, "_persist", side_effect=lambda: order.append("save"))
        self.mock(M.vibrance_effects, "release_sdr")
        self.mock(M.vibrance_effects, "release_look")
        with self.assertRaises(StopIteration):
            self.rpc._unload().send(None)
        self.assertEqual(order, ["save", "updates"])
        with self.assertRaises(StopIteration):  # before _main ever ran: no updater yet
            main.Plugin()._unload().send(None)


class OlderVersionsCanUpdate(PluginCase):
    async def test_releases_keep_the_file_older_updaters_look_for(self):
        """1.0.0-1.2.0 offer a release only when its ZIP has lego_updater.py; dropping it strands those users."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
            shipped = json.load(f)["decky"]["files"]
        self.assertIn("lego_updater.py", shipped)
        self.assertTrue(os.path.isfile(os.path.join(root, "lego_updater.py")))
