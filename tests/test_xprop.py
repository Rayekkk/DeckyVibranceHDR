# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Talking to gamescope: atom encoding, xprop calls and X server choice."""

import os
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _fixtures import PluginTest  # installs the loader stand-ins first
import main as M


class AtomEncoding(PluginTest):

    def test_float_round_trip(self):
        for value in (0.0, 0.5, 1.0, 1.3, 1.5):
            raw = M.vibrance_x11.float_to_cardinal(value)
            self.assertAlmostEqual(M.vibrance_x11.cardinal_to_float(raw), value, places=6)

    def test_matches_gamescope_encoding(self):
        # 1.0 as a float is this exact CARDINAL; vibrantDeck writes the same.
        self.assertEqual(M.vibrance_x11.float_to_cardinal(1.0), 1065353216)
        self.assertAlmostEqual(M.vibrance_x11.cardinal_to_float(1065353216), 1.0, places=6)

    def test_negative_values_use_the_unsigned_bit_pattern(self):
        # SDR_UNSET is written back to gamescope, and xprop's 32c is unsigned.
        self.assertEqual(M.vibrance_x11.float_to_cardinal(M.SDR_UNSET), 0xBF800000)
        self.assertEqual(M.vibrance_x11.cardinal_to_float(0xBF800000), M.SDR_UNSET)

    def test_none_does_not_raise(self):
        self.assertIsNone(M.vibrance_x11.cardinal_to_float(None))


class Environment(PluginTest):

    def test_loader_variables_are_stripped(self):
        """Inheriting Steam's LD_LIBRARY_PATH kills any system binary we spawn."""
        os.environ["LD_LIBRARY_PATH"] = "/steam/lib"
        os.environ["LD_PRELOAD"] = "/steam/preload.so"
        try:
            env = M.vibrance_x11.system_env()
            self.assertNotIn("LD_LIBRARY_PATH", env)
            self.assertNotIn("LD_PRELOAD", env)
            self.assertEqual(env["DISPLAY"], M.vibrance_x11.DISPLAY)
            self.assertTrue(env["PATH"])
        finally:
            os.environ.pop("LD_LIBRARY_PATH", None)
            os.environ.pop("LD_PRELOAD", None)


class XpropPaths(PluginTest):
    def test_quoted_paths_are_unescaped_without_corrupting_utf8(self):
        cases = [(r'"/tmp/a\"b\\c.cube"', '/tmp/a"b\\c.cube'),
                 (r'"/tmp/a\011b\012c.cube"', '/tmp/a\tb\nc.cube'),
                 (r'"/tmp/\305\201\303\263d\305\272.cube"', '/tmp/Łódź.cube'),
                 ('"/tmp/kolory-Łódź.cube"', '/tmp/kolory-Łódź.cube')]
        for wire, expected in cases:
            with patch.object(M.vibrance_x11.subprocess, "run", return_value=SimpleNamespace(stdout="ATOM(UTF8_STRING) = " + wire)):
                self.assertEqual(M.vibrance_x11.read_string_atom(M.ATOM_LOOK_G22), expected)

    def test_embedded_null_is_not_a_filename(self):
        with patch.object(M.vibrance_x11.subprocess, "run", return_value=SimpleNamespace(stdout=r'ATOM = "/tmp/a\000b"')):
            self.assertIsNone(M.vibrance_x11.read_string_atom(M.ATOM_EDID_PATH))


class AtomWrites(PluginTest):
    """Every other suite mocks these, so this pins the command gamescope sees:
    a wrong format letter would otherwise go unnoticed until a device run."""

    def setUp(self):
        self.run = patch.object(M.vibrance_x11.subprocess, "run").start()
        self.addCleanup(patch.stopall)
        patch.object(M.vibrance_host, "native_python", return_value=None).start()
        M.vibrance_x11.DISPLAY = ":7"

    def test_the_sdr_float_goes_out_as_a_32_bit_cardinal(self):
        self.assertTrue(M.vibrance_x11.write_float_atom(M.ATOM_SDR_WIDENESS, 1.0))
        command = self.run.call_args.args[0]
        self.assertEqual(command, ["xprop", "-root", "-f", M.ATOM_SDR_WIDENESS, "32c",
                                   "-set", M.ATOM_SDR_WIDENESS, "1065353216"])
        kwargs = self.run.call_args.kwargs
        self.assertTrue(kwargs["check"])
        self.assertEqual(kwargs["env"]["DISPLAY"], ":7")
        self.assertEqual(kwargs["timeout"], M.vibrance_x11.timeout)

    def test_a_look_path_goes_out_as_utf8_unchanged(self):
        path = "/home/deck/.local/share/look dir/żółw.cube"
        self.assertTrue(M.vibrance_x11.write_string_atom(M.ATOM_LOOK_PQ, path))
        self.assertEqual(self.run.call_args.args[0],
                         ["xprop", "-root", "-f", M.ATOM_LOOK_PQ, "8u", "-set", M.ATOM_LOOK_PQ, path])

    def test_a_failing_or_hanging_xprop_is_a_failed_write(self):
        for failure in (M.vibrance_x11.subprocess.CalledProcessError(1, "xprop"),
                        M.vibrance_x11.subprocess.TimeoutExpired("xprop", 3), FileNotFoundError("xprop")):
            with self.subTest(failure=type(failure).__name__):
                self.run.side_effect = failure
                self.assertFalse(M.vibrance_x11.write_string_atom(M.ATOM_LOOK_G22, "/x.cube"))


class CrossServerWriter(PluginTest):

    def setUp(self):
        self.real_run, self.real_display = M.vibrance_x11.subprocess.run, M.vibrance_x11.DISPLAY
        self.real_displays = M.vibrance_x11.DISPLAYS
        M.vibrance_x11.DISPLAY, M.vibrance_x11.DISPLAYS = ":0", (":0", ":1")

    def tearDown(self):
        M.vibrance_x11.subprocess.run, M.vibrance_x11.DISPLAY = self.real_run, self.real_display
        M.vibrance_x11.DISPLAYS = self.real_displays

    def _serve(self, by_display):
        class Result:
            def __init__(self, stdout):
                self.stdout, self.returncode = stdout, 0

        M.vibrance_x11.subprocess.run = lambda cmd, **kw: Result(
            by_display.get(kw["env"]["DISPLAY"], f"{cmd[-1]}:  not found."))

    def test_the_server_we_use_is_not_evidence_against_itself(self):
        self._serve({":0": f"{M.ATOM_SDR_WIDENESS}(CARDINAL) = 1065353216"})
        self.assertEqual(M.vibrance_x11.foreign_sdr_values(), ())

    def test_the_atom_turning_up_elsewhere_is_reported_with_its_value(self):
        self._serve({":1": f"{M.ATOM_SDR_WIDENESS}(CARDINAL) = 1056964608"})
        self.assertEqual(M.vibrance_x11.foreign_sdr_values(), ((":1", "1056964608"),))

    def test_an_unreadable_server_is_no_reading_at_all(self):
        def run(cmd, **kw):
            raise M.vibrance_x11.subprocess.TimeoutExpired(cmd, kw["timeout"])
        M.vibrance_x11.subprocess.run = run
        self.assertIsNone(M.vibrance_x11.foreign_sdr_values())


class DisplayChoice(PluginTest):
    """gamescope publishes feedback atoms on every XWayland server but the
    colour controls on only one, and writing to the wrong one fails silently."""

    # What the two servers on the Legion Go 2 actually answer with.
    PRIMARY = "\n".join(["GAMESCOPE_ATOM_%d(CARDINAL) = 1" % i
                         for i in range(45)] + ["_NET_SUPPORTED(ATOM) = 1"])
    SECONDARY = "\n".join(["GAMESCOPE_DISPLAY_EDID_PATH(UTF8_STRING) = \"/x\"",
                           "GAMESCOPE_HDR_OUTPUT_FEEDBACK(CARDINAL) = 1",
                           "GAMESCOPE_PID(CARDINAL) = 1",
                           "GAMESCOPE_XWAYLAND_SERVER_ID(CARDINAL) = 1",
                           "GAMESCOPE_CURSOR_VISIBLE_FEEDBACK(CARDINAL) = 1",
                           "GAMESCOPECTRL_BASELAYER_WINDOW(CARDINAL) = 1",
                           "GAMESCOPE_VROVERLAY_FORWARDING(CARDINAL) = 1"])

    def setUp(self):
        self.real_run = M.vibrance_x11.subprocess.run
        self.real_display = M.vibrance_x11.DISPLAY
        self.real_displays = M.vibrance_x11.DISPLAYS
        self.real_listdir = M.os.listdir
        # The two XWayland sockets gamescope opens. Without this the tests read
        # the real /tmp/.X11-unix and fail on any machine with its own X server.
        M.os.listdir = lambda path: ["X0", "X1"]

    def tearDown(self):
        M.vibrance_x11.subprocess.run = self.real_run
        M.vibrance_x11.DISPLAY = self.real_display
        M.vibrance_x11.DISPLAYS = self.real_displays
        M.os.listdir = self.real_listdir

    def test_servers_come_from_the_sockets_not_a_guess(self):
        M.os.listdir = lambda path: ["X2", "X3", "Xnope", ".lock"]
        self.assertEqual(M.vibrance_x11.display_candidates(), (":2", ":3"))

    def test_an_unreadable_socket_directory_falls_back(self):
        def boom(path):
            raise OSError("no such directory")

        M.os.listdir = boom
        self.assertEqual(M.vibrance_x11.display_candidates(), M.vibrance_x11.FALLBACK)

    def test_a_desktop_holding_zero_pushes_gamescope_along(self):
        """The numbering is not fixed, so :0 is not necessarily gamescope."""
        M.os.listdir = lambda path: ["X0", "X1", "X2"]
        self._serve({":0": 'WM_NAME(STRING) = "desktop"',
                     ":1": self.SECONDARY, ":2": self.PRIMARY})
        self.assertEqual(M._pick_display(), ":2")

    def _serve(self, by_display):
        class Result:
            def __init__(self, stdout):
                self.stdout, self.returncode = stdout, 0

        def fake_run(cmd, **kwargs):
            return Result(by_display.get(kwargs["env"]["DISPLAY"], ""))

        M.vibrance_x11.subprocess.run = fake_run

    def test_picks_the_server_holding_the_controls(self):
        self._serve({":0": self.SECONDARY, ":1": self.PRIMARY})
        self.assertEqual(M._pick_display(), ":1")
        self.assertEqual(M.vibrance_x11.DISPLAY, ":1")

    def test_picks_the_other_way_round_just_as_happily(self):
        self._serve({":0": self.PRIMARY, ":1": self.SECONDARY})
        self.assertEqual(M._pick_display(), ":0")
        self.assertEqual(M.vibrance_x11.DISPLAY, ":0")

    def test_a_repeated_search_logs_only_what_changed(self):
        """Without gamescope (Desktop Mode) the search repeats for hours."""
        self._serve({})
        with patch.object(M.decky.logger, "info") as info:
            for _ in range(3):
                self.assertIsNone(M._pick_display())
            self._serve({":0": self.PRIMARY})
            M._pick_display()
        self.assertEqual(info.call_count, 2)

    def test_the_server_reporting_docking_wins_over_a_fuller_one(self):
        """IS_EXTERNAL exists only on gamescope's first server; reading it from
        another would see it absent and never stand down for a monitor."""
        root = self.SECONDARY + f"\n{M.ATOM_IS_EXTERNAL}(CARDINAL) = 0"
        self._serve({":0": self.PRIMARY, ":1": root})
        self.assertEqual(M._pick_display(), ":1")

    def test_a_thin_server_still_counts_as_gamescope(self):
        """gamescope reads the atom off whichever root the event arrived on, so
        a server carrying only feedback atoms is still a working target - it is
        exactly the one vibrantDeck writes to."""
        self._serve({":0": self.SECONDARY, ":1": self.SECONDARY})
        self.assertIsNotNone(M._pick_display())

    def test_no_gamescope_anywhere(self):
        """The failure this replaces: xprop exits 0 on a plain X server too, so
        checking its status read as ready and every write went nowhere."""
        self._serve({":0": 'WM_NAME(STRING) = "x"'})
        self.assertIsNone(M._pick_display())

    def test_no_x_server_at_all(self):
        self._serve({})
        self.assertIsNone(M._pick_display())

    def test_only_gamescope_atoms_are_counted(self):
        self._serve({":0": 'WM_NAME(STRING) = "x"\n_NET_SUPPORTED(ATOM) = 1'})
        self.assertEqual(M.vibrance_x11.count_gamescope_atoms(":0"), 0)


class SocketIdentity(PluginTest):
    def test_identity_detects_replacement_at_same_display_and_missing_path(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(M.vibrance_x11, "SOCKET_DIR", folder), \
                patch.object(M.vibrance_x11, "DISPLAY", ":12"):
            socket = Path(folder) / "X12"
            socket.write_text("first socket fixture")
            first = M.vibrance_x11.session_identity()
            replacement = Path(folder) / "replacement"
            replacement.write_text("new socket fixture")
            replacement.replace(socket)
            self.assertNotEqual(M.vibrance_x11.session_identity(), first)
            socket.unlink()
            self.assertIsNone(M.vibrance_x11.session_identity())
