# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Pure logic tests. These run anywhere, including on a CI runner."""

import asyncio
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness  # noqa: E402
_harness.install()

import main  # noqa: E402
import vibrance_lut as V  # noqa: E402

# A 10-bit code value is 1/1023. Anything under that cannot be displayed, so it
# is the bar the round trip has to clear rather than some arbitrary epsilon.
CODE_VALUE_10BIT = 1.0 / 1023.0


class PqTransfer(unittest.TestCase):
    """SMPTE ST 2084, with 1.0 encoded meaning 10000 nits."""

    def test_round_trip_across_the_useful_range(self):
        # The last three are this panel's black, full-field and peak.
        for nits in (0.0, 1.0, 100.0, 203.0, 0.0007, 475.683, 1107.128, 10000.0):
            encoded = V.linear_to_pq(nits / 10000.0)
            back = V.pq_to_linear(encoded) * 10000.0
            self.assertAlmostEqual(back, nits, places=6)

    def test_anchors(self):
        self.assertEqual(V.linear_to_pq(0.0), 0.0)
        self.assertAlmostEqual(V.linear_to_pq(1.0), 1.0, places=9)
        self.assertEqual(V.pq_to_linear(0.0), 0.0)

    def test_monotonic(self):
        previous = -1.0
        for i in range(0, 1024):
            value = V.linear_to_pq(i / 1023.0)
            self.assertGreater(value, previous)
            previous = value


class Saturation(unittest.TestCase):

    def _grid(self, step=4):
        size = 33
        for r in range(0, size, step):
            for g in range(0, size, step):
                for b in range(0, size, step):
                    yield (r / (size - 1), g / (size - 1), b / (size - 1))

    def test_unity_is_exactly_identity(self):
        for rgb in self._grid():
            self.assertEqual(V.saturate(rgb, 1.0), rgb)

    def test_round_trip_stays_under_one_code_value(self):
        """Forced through the full path, unity must still be invisible."""
        worst = 0.0
        for rgb in self._grid(step=2):
            out = V.saturate(rgb, 1.0000001)
            worst = max(worst, max(abs(a - b) for a, b in zip(rgb, out)))
        self.assertLess(worst, CODE_VALUE_10BIT)

    def test_black_stays_black(self):
        """gamescope's bRaisesBlackLevelFloor keys off the first LUT entry."""
        for saturation in (0.0, 0.5, 1.0, main.HDR_SOFT_LIMIT, main.HDR_MAX):
            self.assertEqual(V.saturate((0.0, 0.0, 0.0), saturation),
                             (0.0, 0.0, 0.0))

    def test_greys_stay_neutral(self):
        for level in (0.1, 0.25, 0.5, 0.75, 1.0):
            for saturation in (0.0, 0.5, 1.5):
                out = V.saturate((level, level, level), saturation)
                self.assertLess(max(out) - min(out), 1e-4,
                                f"level {level} at saturation {saturation}")

    def test_zero_saturation_is_greyscale(self):
        for rgb in ((0.6, 0.2, 0.2), (0.2, 0.6, 0.3), (0.1, 0.1, 0.7)):
            out = V.saturate(rgb, 0.0)
            self.assertLess(max(out) - min(out), 1e-6)

    def test_saturation_increases_monotonically(self):
        rgb = (0.55, 0.30, 0.30)
        spreads = [max(V.saturate(rgb, s)) - min(V.saturate(rgb, s))
                   for s in (0.4, 0.6, 0.8, 1.0, 1.2)]
        for earlier, later in zip(spreads, spreads[1:]):
            self.assertGreater(later, earlier)

    def test_output_stays_inside_the_cube(self):
        """Tetrahedral interpolation in gamescope expects 0..1.

        Bounded by the range the UI can actually reach, so raising the ceiling
        later keeps the worst selectable case covered instead of quietly
        dropping out of it.
        """
        for saturation in (0.0, main.HDR_MAX):
            for rgb in self._grid(step=3):
                for channel in V.saturate(rgb, saturation):
                    self.assertGreaterEqual(channel, 0.0)
                    self.assertLessEqual(channel, 1.0)


class CubeFile(unittest.TestCase):

    def test_shape_matches_what_gamescope_parses(self):
        size = 9
        text = V.generate(1.2, size)
        lines = text.splitlines()
        self.assertIn(f"LUT_3D_SIZE {size}", lines)
        data = [l for l in lines
                if l and not l.startswith(("#", "LUT_", "DOMAIN_"))]
        self.assertEqual(len(data), size ** 3)
        for line in (data[0], data[-1]):
            self.assertEqual(len(line.split()), 3)

    def test_first_entry_is_black_and_last_is_white(self):
        data = [l for l in V.generate(1.4, 5).splitlines()
                if l and not l.startswith(("#", "LUT_", "DOMAIN_"))]
        self.assertEqual(data[0], "0.000000 0.000000 0.000000")
        self.assertEqual(data[-1], "1.000000 1.000000 1.000000")

    def test_red_changes_fastest(self):
        """gamescope's loader assumes that ordering; getting it wrong scrambles
        the image rather than failing loudly."""
        size = 5
        data = [tuple(map(float, l.split()))
                for l in V.generate(1.0, size).splitlines()
                if l and not l.startswith(("#", "LUT_", "DOMAIN_"))]
        # Unity is identity, so entry n must equal its own grid coordinate.
        step = 1.0 / (size - 1)
        for index, entry in enumerate(data):
            r = index % size
            g = (index // size) % size
            b = index // (size * size)
            self.assertAlmostEqual(entry[0], r * step, places=5)
            self.assertAlmostEqual(entry[1], g * step, places=5)
            self.assertAlmostEqual(entry[2], b * step, places=5)

    def test_size_bounds_are_enforced(self):
        for bad in (1, 0, 129, 200):
            with self.assertRaises(ValueError):
                V.generate(1.0, bad)


class AtomEncoding(unittest.TestCase):

    def test_float_round_trip(self):
        for value in (0.0, 0.5, 1.0, 1.3, 1.5):
            raw = main._float_to_cardinal(value)
            self.assertAlmostEqual(main._cardinal_to_float(raw), value, places=6)

    def test_matches_gamescope_encoding(self):
        # 1.0 as a float is this exact CARDINAL; vibrantDeck writes the same.
        self.assertEqual(main._float_to_cardinal(1.0), 1065353216)
        self.assertAlmostEqual(main._cardinal_to_float(1065353216), 1.0, places=6)

    def test_none_does_not_raise(self):
        self.assertIsNone(main._cardinal_to_float(None))


class Clamping(unittest.TestCase):

    RANGES = (("sdr", "SDR_MIN", "SDR_SOFT_WIDE", "SDR_MAX_WIDE"),
              ("hdr", "HDR_MIN", "HDR_SOFT_LIMIT", "HDR_MAX"))

    def _bounds(self):
        for half, lo, soft, hi in self.RANGES:
            yield half, getattr(main, lo), getattr(main, soft), getattr(main, hi)

    def test_each_range_is_enforced(self):
        for half, lo, _soft, hi in self._bounds():
            self.assertEqual(main._clamp(-1.0, lo, hi), lo, half)
            self.assertEqual(main._clamp(99.0, lo, hi), hi, half)
            self.assertEqual(main._clamp(lo, lo, hi), lo, half)
            self.assertEqual(main._clamp(hi, lo, hi), hi, half)

    def test_neither_half_can_desaturate(self):
        """The floor is neutral on both. Below it the picture is being drained,
        which is the one thing this plugin is not for."""
        for half, lo, _soft, hi in self._bounds():
            self.assertEqual(lo, 1.0, half)
            self.assertEqual(main._clamp(0.0, lo, hi), 1.0, half)
            self.assertEqual(main._clamp(0.999, lo, hi), 1.0, half)

    def test_default_is_the_floor_of_both(self):
        for half, lo, _soft, _hi in self._bounds():
            self.assertEqual(main.SAT_DEFAULT, lo, half)

    def test_soft_limits_sit_inside_their_ranges(self):
        for half, lo, soft, hi in self._bounds():
            self.assertGreater(soft, lo, half)
            self.assertLess(soft, hi, half)

    def test_state_advertises_every_bound(self):
        """The frontend reads its slider bounds from here and nothing else
        checks them, so all six are pinned to their constants. Asserting only
        some of them lets a copy-paste slip in a block of near-identical lines
        through: an sdr_min left at the old 0.0 would draw a slider a third of
        which snaps back the moment it is released."""
        for key, constant in (
            ("sdr_min", main.SDR_MIN),
            ("sdr_max", main.SDR_MAX_WIDE),
            ("sdr_soft_limit", main.SDR_SOFT_WIDE),
            ("hdr_min", main.HDR_MIN),
            ("hdr_max", main.HDR_MAX),
            ("hdr_soft_limit", main.HDR_SOFT_LIMIT),
        ):
            self.assertIn(key, main.Plugin._state)
            self.assertEqual(main.Plugin._state[key], constant, key)

    def test_the_requested_ranges_are_what_shipped(self):
        """Pinned to literals on purpose. Everything else here is relative, so
        without this a tuning pass could hand back half the range and the suite
        would stay green."""
        self.assertEqual(main.SDR_MIN, 1.0)
        self.assertEqual(main.SDR_MAX_WIDE, 3.0)
        self.assertEqual(main.SDR_MAX_STANDARD, 2.0)
        self.assertEqual(main.SDR_MAX_STANDARD_WITH_LOOK, 3.0)
        self.assertEqual(main.HDR_MIN, 1.0)
        self.assertEqual(main.HDR_MAX, 1.5)

    def test_hdr_ceiling_stays_below_the_sdr_one(self):
        """Different mechanisms, different useful ranges - if these ever become
        one number again the LUT half is the one that suffers."""
        self.assertLess(main.HDR_MAX, main.SDR_MAX_WIDE)


def _drain(coro):
    """Await a coroutine, then cancel whatever it scheduled.

    Both setters hand the real work to a debounced task; the tests care about
    the value that lands in the state, not about the write that follows.
    """
    async def go():
        result = await coro
        for task in list(main.Plugin._tasks):
            task.cancel()
        return result

    return asyncio.run(go())


class Setters(unittest.TestCase):
    """_clamp is exercised above in isolation; this is about its call sites.

    Passing one half's bounds to the other half's setter is a single-token slip
    that nothing else in this file would notice.
    """

    def setUp(self):
        self.plugin = main.Plugin()
        main.Plugin._state["sdr_enabled"] = False
        main.Plugin._state["hdr_enabled"] = False
        main.Plugin._state["sdr_max"] = main.SDR_MAX_WIDE

    def test_sdr_setter_uses_the_sdr_ceiling(self):
        state = _drain(self.plugin.set_sdr_saturation(main.SDR_MAX_WIDE))
        self.assertEqual(state["sdr_saturation"], main.SDR_MAX_WIDE)

    def test_hdr_setter_does_not_get_the_sdr_ceiling(self):
        state = _drain(self.plugin.set_hdr_saturation(main.SDR_MAX_WIDE))
        self.assertEqual(state["hdr_saturation"], main.HDR_MAX)

    def test_neither_setter_accepts_desaturation(self):
        self.assertEqual(
            _drain(self.plugin.set_sdr_saturation(0.0))["sdr_saturation"], 1.0)
        self.assertEqual(
            _drain(self.plugin.set_hdr_saturation(0.0))["hdr_saturation"], 1.0)

    def test_saved_values_from_an_older_range_are_lifted_on_load(self):
        """1.0.0 allowed 0.0. Without the migration those users come back to a
        drained picture the new UI cannot even represent."""
        main.settings._store.update({
            "sdr_saturation": 0.4,
            "hdr_saturation": 0.0,
            "sdr_enabled": True,
            "hdr_enabled": True,
        })
        try:
            # _main reaches the clamps and then returns at the unreachable
            # gamescope check, so no subprocess is involved.
            _drain(self.plugin._main())
        finally:
            main.settings._store.clear()
        self.assertEqual(main.Plugin._state["sdr_saturation"], 1.0)
        self.assertEqual(main.Plugin._state["hdr_saturation"], 1.0)


class Reassert(unittest.TestCase):
    """gamescope drops these atoms when it re-initialises, which it does across
    a suspend. Telling that apart from another plugin holding the other end is
    the whole job here - the first heals itself, the second needs saying."""

    def setUp(self):
        self.written = []
        self.live = [2.0]
        self._read, self._write = main._read_float_atom, main._write_float_atom
        self._other = main._other_writer_present

        def fake_write(name, value):
            self.written.append(value)
            self.live[0] = value
            return True

        main._read_float_atom = lambda name: self.live[0]
        main._write_float_atom = fake_write
        main._other_writer_present = lambda: False
        self._looks = main.Plugin._reassert_looks_if_dropped
        main.Plugin._reassert_looks_if_dropped = staticmethod(lambda: None)
        main.Plugin._slow_checks = 0
        main.Plugin._state.update({
            "sdr_enabled": True, "sdr_applied": True,
            "sdr_saturation": 2.0, "conflict": "",
        })
        main.Plugin._last_sdr_written = 2.0
        main.Plugin._reassert_strikes = 0

    def tearDown(self):
        main._read_float_atom, main._write_float_atom = self._read, self._write
        main._other_writer_present = self._other
        main.Plugin._reassert_looks_if_dropped = self._looks
        main.Plugin._slow_checks = 0
        main.Plugin._state.update({
            "sdr_enabled": False, "sdr_applied": False, "conflict": "",
        })
        main.Plugin._last_sdr_written = None
        main.Plugin._reassert_strikes = 0

    def test_a_dropped_value_is_put_back_without_crying_conflict(self):
        self.live[0] = 1.0                      # gamescope re-initialised
        main.Plugin._watch_once()
        self.assertEqual(self.written, [2.0])
        self.assertEqual(main.Plugin._state["conflict"], "")

    def test_something_holding_the_other_end_gets_named(self):
        for _ in range(main.CONFLICT_STRIKES):  # overwritten after every put-back
            self.live[0] = 0.5
            main.Plugin._watch_once()
        self.assertIn("vibrantDeck", main.Plugin._state["conflict"])

    def test_one_strike_short_stays_quiet(self):
        for _ in range(main.CONFLICT_STRIKES - 1):
            self.live[0] = 0.5
            main.Plugin._watch_once()
        self.assertEqual(main.Plugin._state["conflict"], "")

    def test_agreement_clears_the_count(self):
        self.live[0] = 1.0
        main.Plugin._watch_once()
        self.assertEqual(main.Plugin._reassert_strikes, 1)
        main.Plugin._watch_once()               # agrees now, we just wrote it
        self.assertEqual(main.Plugin._reassert_strikes, 0)
        self.assertEqual(main.Plugin._state["conflict"], "")

    def test_a_disabled_half_is_left_alone(self):
        main.Plugin._state["sdr_enabled"] = False
        self.live[0] = 0.5
        main.Plugin._watch_once()
        self.assertEqual(self.written, [])

    def test_a_writer_on_another_server_is_named_even_when_we_agree(self):
        """The case our readback cannot see: gamescope applies a write from any
        root, but we only ever read one of them, so the value we read back can
        agree with us while the picture shows somebody else's."""
        main._other_writer_present = lambda: True
        main.Plugin._reassert_looks_if_dropped = staticmethod(lambda: None)
        main.Plugin._slow_checks = main.SLOW_CHECK_EVERY - 1
        main.Plugin._watch_once()
        self.assertIn("different X server", main.Plugin._state["conflict"])


class CrossServerWriter(unittest.TestCase):

    def setUp(self):
        self.real_run, self.real_display = main.subprocess.run, main.DISPLAY
        self.real_displays = main.DISPLAYS
        main.DISPLAY, main.DISPLAYS = ":0", (":0", ":1")

    def tearDown(self):
        main.subprocess.run, main.DISPLAY = self.real_run, self.real_display
        main.DISPLAYS = self.real_displays

    def _serve(self, by_display):
        class Result:
            def __init__(self, stdout):
                self.stdout, self.returncode = stdout, 0

        main.subprocess.run = lambda cmd, **kw: Result(
            by_display.get(kw["env"]["DISPLAY"], f"{cmd[-1]}:  not found."))

    def test_the_server_we_use_is_not_evidence_against_itself(self):
        self._serve({":0": f"{main.ATOM_SDR_WIDENESS}(CARDINAL) = 1065353216"})
        self.assertFalse(main._other_writer_present())

    def test_the_atom_turning_up_elsewhere_is(self):
        self._serve({":1": f"{main.ATOM_SDR_WIDENESS}(CARDINAL) = 1056964608"})
        self.assertTrue(main._other_writer_present())


class DisplayChoice(unittest.TestCase):
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
        self.real_run = main.subprocess.run
        self.real_display = main.DISPLAY
        self.real_displays = main.DISPLAYS
        self.real_listdir = main.os.listdir

    def tearDown(self):
        main.subprocess.run = self.real_run
        main.DISPLAY = self.real_display
        main.DISPLAYS = self.real_displays
        main.os.listdir = self.real_listdir

    def test_servers_come_from_the_sockets_not_a_guess(self):
        main.os.listdir = lambda path: ["X2", "X3", "Xnope", ".lock"]
        self.assertEqual(main._display_candidates(), (":2", ":3"))

    def test_an_unreadable_socket_directory_falls_back(self):
        def boom(path):
            raise OSError("no such directory")

        main.os.listdir = boom
        self.assertEqual(main._display_candidates(), main.DISPLAY_FALLBACK)

    def test_a_desktop_holding_zero_pushes_gamescope_along(self):
        """The numbering is not fixed, so :0 is not necessarily gamescope."""
        main.os.listdir = lambda path: ["X0", "X1", "X2"]
        self._serve({":0": 'WM_NAME(STRING) = "desktop"',
                     ":1": self.SECONDARY, ":2": self.PRIMARY})
        self.assertEqual(main._pick_display(), ":2")

    def _serve(self, by_display):
        class Result:
            def __init__(self, stdout):
                self.stdout, self.returncode = stdout, 0

        def fake_run(cmd, **kwargs):
            return Result(by_display.get(kwargs["env"]["DISPLAY"], ""))

        main.subprocess.run = fake_run

    def test_picks_the_server_holding_the_controls(self):
        self._serve({":0": self.SECONDARY, ":1": self.PRIMARY})
        self.assertEqual(main._pick_display(), ":1")
        self.assertEqual(main.DISPLAY, ":1")

    def test_picks_the_other_way_round_just_as_happily(self):
        self._serve({":0": self.PRIMARY, ":1": self.SECONDARY})
        self.assertEqual(main._pick_display(), ":0")
        self.assertEqual(main.DISPLAY, ":0")

    def test_a_thin_server_still_counts_as_gamescope(self):
        """gamescope reads the atom off whichever root the event arrived on, so
        a server carrying only feedback atoms is still a working target - it is
        exactly the one vibrantDeck writes to."""
        self._serve({":0": self.SECONDARY, ":1": self.SECONDARY})
        self.assertIsNotNone(main._pick_display())

    def test_no_gamescope_anywhere(self):
        """The failure this replaces: xprop exits 0 on a plain X server too, so
        checking its status read as ready and every write went nowhere."""
        self._serve({":0": 'WM_NAME(STRING) = "x"'})
        self.assertIsNone(main._pick_display())

    def test_no_x_server_at_all(self):
        self._serve({})
        self.assertIsNone(main._pick_display())

    def test_only_gamescope_atoms_are_counted(self):
        self._serve({":0": 'WM_NAME(STRING) = "x"\n_NET_SUPPORTED(ATOM) = 1'})
        self.assertEqual(main._count_gamescope_atoms(":0"), 0)


class SuspendDetection(unittest.TestCase):

    def test_boottime_is_usable_everywhere(self):
        """CLOCK_BOOTTIME is Linux only; the fallback keeps the detector idle
        rather than raising on a dev box."""
        self.assertIsInstance(main._boottime(), float)

    def test_slack_exceeds_the_poll_interval(self):
        """Otherwise the jitter of an ordinary tick reads as a suspend and the
        values get re-pushed every second forever."""
        self.assertGreater(main.SUSPEND_SLACK_S, main.SUSPEND_POLL_S)

    def test_the_retry_ladder_is_staggered_and_reaches_past_startup(self):
        delays = main.REASSERT_DELAYS_S
        self.assertEqual(list(delays), sorted(delays))
        self.assertGreater(sum(delays), 10.0)


class CapabilityGating(unittest.TestCase):
    """The two halves withdraw independently, and only on a definite no."""

    KEYS = ("hdr_supported", "sdr_enabled", "hdr_enabled",
            "sdr_max", "sdr_soft_limit", "sdr_saturation")

    def setUp(self):
        self.saved_state = {k: main.Plugin._state[k] for k in self.KEYS}
        self.saved_fns = {name: getattr(main, name) for name in
                          ("_pick_display", "_panel_is_wide_gamut",
                           "_panel_supports_hdr", "_read_float_atom")}
        # _main rewrites these module globals; leaving them changed makes an
        # unrelated test fail later, which is a horrible way to find out.
        self.saved_mapping = (main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN)
        main._pick_display = lambda: ":0"
        main._read_float_atom = lambda name: 1.0
        self.real_sleep, self.real_count = asyncio.sleep, main._count_gamescope_atoms

        async def no_wait(_seconds):
            return None

        asyncio.sleep = no_wait
        main._count_gamescope_atoms = lambda display: 44

    def tearDown(self):
        for name, fn in self.saved_fns.items():
            setattr(main, name, fn)
        main.Plugin._state.update(self.saved_state)
        main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = self.saved_mapping
        asyncio.sleep, main._count_gamescope_atoms = self.real_sleep, self.real_count
        main.settings._store.clear()

    def _boot(self, wide, pq, hdr_on=True):
        main._panel_is_wide_gamut = lambda: wide
        main._panel_supports_hdr = lambda: pq
        # _main reloads these from the settings, so seeding the state is not
        # enough to get an enabled half into the startup path.
        main.settings._store.update({"sdr_enabled": False, "hdr_enabled": hdr_on})
        main.Plugin._state.update({"hdr_supported": True})
        # _main only loads settings now; the part that reads the panel waits in
        # a task, so it is driven explicitly here.
        _drain(main.Plugin()._main())
        _drain(main.Plugin()._start_when_ready())

    def test_a_panel_without_pq_loses_the_hdr_half_only(self):
        self._boot(wide=True, pq=False)
        self.assertFalse(main.Plugin._state["hdr_supported"])
        self.assertFalse(main.Plugin._state["hdr_enabled"])

    def test_a_standard_gamut_panel_gets_the_other_mapping(self):
        """A Legion Go S keeps a working SDR slider; it just runs against an
        atom whose neutral is 0.0 and which gives out after one unit."""
        self._boot(wide=False, pq=True)
        self.assertEqual(main.SDR_NEUTRAL_ATOM, 0.0)
        self.assertEqual(main.SDR_ATOM_SPAN, 1.0)

    def test_the_look_switch_is_what_decides_the_standard_ceiling(self):
        """SDR_LOOK_ENABLED is the revert switch for the whole experiment, so
        both of its positions are pinned."""
        real = main.SDR_LOOK_ENABLED
        try:
            main.SDR_LOOK_ENABLED = True
            self._boot(wide=False, pq=True)
            self.assertEqual(main.Plugin._state["sdr_max"],
                             main.SDR_MAX_STANDARD_WITH_LOOK)
            self.assertEqual(main.Plugin._state["sdr_soft_limit"],
                             main.SDR_SOFT_STANDARD_WITH_LOOK)

            main.SDR_LOOK_ENABLED = False
            self._boot(wide=False, pq=True)
            self.assertEqual(main.Plugin._state["sdr_max"],
                             main.SDR_MAX_STANDARD)
            self.assertEqual(main.Plugin._state["sdr_soft_limit"],
                             main.SDR_SOFT_STANDARD)
        finally:
            main.SDR_LOOK_ENABLED = real

    def test_a_wide_gamut_panel_is_unaffected_by_the_switch(self):
        """The Legion Go 2 must land on exactly the same numbers either way."""
        real = main.SDR_LOOK_ENABLED
        try:
            for setting in (True, False):
                main.SDR_LOOK_ENABLED = setting
                self._boot(wide=True, pq=True)
                self.assertEqual(main.SDR_NEUTRAL_ATOM, 1.0, str(setting))
                self.assertEqual(main.SDR_ATOM_SPAN, 2.0, str(setting))
                self.assertEqual(main.Plugin._state["sdr_max"],
                                 main.SDR_MAX_WIDE, str(setting))
                self.assertEqual(main.Plugin._state["sdr_soft_limit"],
                                 main.SDR_SOFT_WIDE, str(setting))
        finally:
            main.SDR_LOOK_ENABLED = real

    def test_an_edid_we_cannot_read_withdraws_nothing(self):
        """Ignorance is not a no - a missing atom must not cost a user a
        control that works."""
        self._boot(wide=None, pq=None)
        self.assertEqual(main.SDR_NEUTRAL_ATOM, 1.0)
        self.assertTrue(main.Plugin._state["hdr_supported"])

    def test_a_capable_panel_keeps_both(self):
        self._boot(wide=True, pq=True)
        self.assertEqual(main.SDR_NEUTRAL_ATOM, 1.0)
        self.assertEqual(main.Plugin._state["sdr_max"], main.SDR_MAX_WIDE)
        self.assertTrue(main.Plugin._state["hdr_supported"])

    def test_the_hdr_setter_refuses_once_the_half_is_withdrawn(self):
        """The frontend hides the section, but the callable stays reachable."""
        main.Plugin._state.update({"hdr_supported": False, "hdr_enabled": False})
        state = _drain(main.Plugin().set_hdr_enabled(True))
        self.assertFalse(state["hdr_enabled"])




class PanelDetection(unittest.TestCase):
    """gamescope's own BIsWideGamut test, and the EDID bytes it needs."""

    @staticmethod
    def _edid(red_x: float, red_y: float) -> bytes:
        blob = bytearray(128)
        rx, ry = round(red_x * 1024), round(red_y * 1024)
        blob[0x19] = ((rx & 0x03) << 6) | ((ry & 0x03) << 4)
        blob[0x1B] = rx >> 2
        blob[0x1C] = ry >> 2
        return bytes(blob)

    def test_parses_the_panel_this_was_written_for(self):
        # edid-decode reports 0.6835, 0.3154 for the Legion Go 2 panel.
        red_x, red_y = main._edid_red_primary(self._edid(0.6836, 0.3154))
        self.assertAlmostEqual(red_x, 0.6836, places=3)
        self.assertAlmostEqual(red_y, 0.3154, places=3)

    def test_wide_and_standard_gamut_land_on_opposite_sides(self):
        wide = main._edid_red_primary(self._edid(0.6836, 0.3154))
        self.assertTrue(wide[0] > main.WIDE_GAMUT_RED_X
                        and wide[1] < main.WIDE_GAMUT_RED_Y)
        # Rec.709 red, i.e. a plain sRGB panel.
        plain = main._edid_red_primary(self._edid(0.640, 0.330))
        self.assertFalse(plain[0] > main.WIDE_GAMUT_RED_X
                         and plain[1] < main.WIDE_GAMUT_RED_Y)

    def test_thresholds_match_gamescope(self):
        """color_helpers.cpp BIsWideGamut: red.x > 0.650 and red.y < 0.320."""
        self.assertEqual(main.WIDE_GAMUT_RED_X, 0.650)
        self.assertEqual(main.WIDE_GAMUT_RED_Y, 0.320)

    def test_a_truncated_edid_is_not_guessed_at(self):
        self.assertIsNone(main._edid_red_primary(b"\x00" * 16))


class HdrCapability(unittest.TestCase):
    """Read from the panel's CTA-861 block rather than from
    GAMESCOPE_HDR_OUTPUT_FEEDBACK, whose meaning changed between gamescope
    builds and took a sibling plugin down with it on Bazzite 44."""

    @staticmethod
    def _edid(eotf_mask=None, cta=True):
        base = bytearray(128)
        base[126] = 1 if cta else 0
        if not cta:
            return bytes(base)
        block = bytearray(128)
        block[0] = 0x02                      # CTA-861
        block[1] = 3                          # revision
        pos = 4
        if eotf_mask is not None:
            payload = bytes([0x06, eotf_mask, 0x01])
            block[pos] = (0x07 << 5) | len(payload)
            block[pos + 1:pos + 1 + len(payload)] = payload
            pos += 1 + len(payload)
        block[2] = pos                        # detailed timings start here
        return bytes(base + block)

    def test_a_panel_advertising_st2084(self):
        # Traditional gamma SDR + SMPTE ST 2084, as the Legion Go 2 reports.
        self.assertTrue(main._edid_supports_pq(self._edid(0b0000_0101)))

    def test_a_panel_advertising_only_gamma(self):
        self.assertFalse(main._edid_supports_pq(self._edid(0b0000_0001)))

    def test_hlg_alone_is_not_the_transfer_gamescope_drives(self):
        self.assertFalse(main._edid_supports_pq(self._edid(0b0000_1000)))

    def test_a_cta_block_with_no_hdr_metadata_is_an_answer(self):
        self.assertFalse(main._edid_supports_pq(self._edid(None)))

    def test_a_base_only_edid_is_a_definite_no(self):
        """HDR metadata has nowhere else to live, so a complete EDID without it
        is an answer rather than a shrug."""
        self.assertFalse(main._edid_supports_pq(self._edid(cta=False)))

    def test_a_displayid_extension_with_no_hdr_in_it_is_a_no(self):
        """The Legion Go S shape: one DisplayID block, no CTA extension. Read as
        "no idea" this left the HDR section on a panel that has no HDR."""
        base = bytearray(128)
        base[126] = 1
        displayid = bytearray(128)
        displayid[0] = 0x70
        self.assertFalse(main._edid_supports_pq(bytes(base + displayid)))

    def test_pq_declared_inside_a_displayid_block_is_found(self):
        """CTA data blocks can be carried inside DisplayID, so looking only for
        extension tag 0x02 would miss a panel that does advertise HDR."""
        base = bytearray(128)
        base[126] = 1
        cta_payload = bytes([(0x07 << 5) | 3, 0x06, 0b0000_0101, 0x01])
        displayid = bytearray(128)
        displayid[0] = 0x70
        displayid[2] = 3 + len(cta_payload)          # payload length
        displayid[5] = 0x81                           # CTA-861 data block
        displayid[7] = len(cta_payload)
        displayid[8:8 + len(cta_payload)] = cta_payload
        self.assertTrue(main._edid_supports_pq(bytes(base + displayid)))

    def test_a_truncated_edid_is_not_an_answer(self):
        self.assertIsNone(main._edid_supports_pq(b"\x00" * 64))

    def test_a_block_promising_more_than_it_holds_does_not_read_past_it(self):
        edid = bytearray(self._edid(0b0000_0101))
        edid[128 + 2] = 127                   # collection runs to the very end
        self.assertIn(main._edid_supports_pq(bytes(edid)), (True, False))


class Environment(unittest.TestCase):

    def test_loader_variables_are_stripped(self):
        """Inheriting Steam's LD_LIBRARY_PATH kills any system binary we spawn."""
        os.environ["LD_LIBRARY_PATH"] = "/steam/lib"
        os.environ["LD_PRELOAD"] = "/steam/preload.so"
        try:
            env = main._system_env()
            self.assertNotIn("LD_LIBRARY_PATH", env)
            self.assertNotIn("LD_PRELOAD", env)
            self.assertEqual(env["DISPLAY"], main.DISPLAY)
            self.assertTrue(env["PATH"])
        finally:
            os.environ.pop("LD_LIBRARY_PATH", None)
            os.environ.pop("LD_PRELOAD", None)


if __name__ == "__main__":
    unittest.main()


class SdrLookSplit(unittest.TestCase):
    """EXPERIMENTAL, Legion Go S only. The atom carries what it can and the
    look carries the rest - and on a wide-gamut panel there is never a rest, so
    a Legion Go 2 must come out of this untouched."""

    def setUp(self):
        self.saved = (main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN)

    def tearDown(self):
        main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = self.saved

    def _wide(self):
        main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = 1.0, 2.0

    def _standard(self):
        main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = 0.0, 1.0

    def test_a_wide_gamut_panel_never_gets_a_look(self):
        """The whole point of the gate: nothing about the Go 2 changes."""
        self._wide()
        for value in (1.0, 1.5, 2.0, 2.5, 3.0):
            atom, look = main._sdr_split(value)
            self.assertEqual(look, 1.0, value)
            self.assertAlmostEqual(atom, value, msg=str(value))

    def test_a_standard_panel_fills_the_atom_first(self):
        self._standard()
        for value, expected in ((1.0, 0.0), (1.5, 0.5), (2.0, 1.0)):
            atom, look = main._sdr_split(value)
            self.assertAlmostEqual(atom, expected, msg=str(value))
            self.assertEqual(look, 1.0, value)

    def test_the_look_only_starts_where_cfit_stops(self):
        """cfit() clamps at atom 1.0, so below 200% the look must stay out."""
        self._standard()
        self.assertEqual(main._sdr_split(1.99)[1], 1.0)
        self.assertGreater(main._sdr_split(2.01)[1], 1.0)

    def test_the_atom_never_goes_past_where_it_responds(self):
        self._standard()
        for value in (2.0, 2.5, 3.0):
            self.assertAlmostEqual(main._sdr_split(value)[0], 1.0, msg=str(value))

    def test_the_top_of_the_slider_asks_the_look_for_its_ceiling(self):
        self._standard()
        self.assertAlmostEqual(
            main._sdr_split(main.SDR_MAX_STANDARD_WITH_LOOK)[1],
            main.SDR_LOOK_CEILING)

    def test_raising_the_maximum_did_not_weaken_what_was_below_it(self):
        """The look is a rate, not a fraction of the range, so 300% asks for
        exactly what it asked for when 300% was the top of the slider."""
        self._standard()
        self.assertAlmostEqual(main._sdr_split(3.0)[1], 1.5)
        self.assertAlmostEqual(main._sdr_split(2.5)[1], 1.25)

    def test_the_look_ramps_rather_than_jumping(self):
        self._standard()
        looks = [main._sdr_split(v)[1] for v in (2.0, 2.25, 2.5, 2.75, 3.0)]
        for earlier, later in zip(looks, looks[1:]):
            self.assertGreater(later, earlier)

    def test_the_look_ceiling_matches_the_hdr_one(self):
        """Same code, same clipping, so the same ceiling."""
        self.assertEqual(main.SDR_LOOK_CEILING, main.HDR_MAX)

    def test_the_ceiling_is_never_asked_for_more_than_it_allows(self):
        self._standard()
        for value in (4.0, 6.0, 99.0):
            self.assertLessEqual(main._sdr_split(value)[1],
                                 main.SDR_LOOK_CEILING, str(value))

    def test_the_warning_sits_where_the_hdr_half_warns(self):
        """Identical maths, so the look value that starts clipping is the same;
        only the slider position it corresponds to differs."""
        self._standard()
        at_warning = main._sdr_split(main.SDR_SOFT_STANDARD_WITH_LOOK)[1]
        self.assertAlmostEqual(at_warning, main.HDR_SOFT_LIMIT, places=6)


class G22Transfer(unittest.TestCase):
    """The SDR look runs on gamma-encoded values, not PQ ones."""

    def test_round_trip(self):
        for i in range(0, 256):
            e = i / 255.0
            self.assertAlmostEqual(V.linear_to_g22(V.g22_to_linear(e)), e,
                                   places=9)

    def test_encoded_white_lands_on_sdr_reference_white(self):
        self.assertAlmostEqual(V.g22_to_linear(1.0) * 10000.0,
                               V.SDR_REFERENCE_NITS, places=6)

    def test_black_and_white_survive_a_boost(self):
        self.assertEqual(V.saturate((0.0, 0.0, 0.0), 1.5, "g22"),
                         (0.0, 0.0, 0.0))
        for channel in V.saturate((1.0, 1.0, 1.0), 1.5, "g22"):
            self.assertAlmostEqual(channel, 1.0, places=6)

    def test_greys_stay_neutral(self):
        for level in (0.1, 0.25, 0.5, 0.75):
            out = V.saturate((level, level, level), 1.5, "g22")
            self.assertLess(max(out) - min(out), 1e-6, str(level))
            self.assertAlmostEqual(out[0], level, places=6)

    def test_unity_is_identity(self):
        self.assertEqual(V.saturate((0.3, 0.6, 0.2), 1.0, "g22"),
                         (0.3, 0.6, 0.2))

    def test_saturation_rises_monotonically(self):
        rgb = (0.60, 0.25, 0.25)
        spreads = [max(V.saturate(rgb, s, "g22")) - min(V.saturate(rgb, s, "g22"))
                   for s in (1.0, 1.2, 1.4, 1.5)]
        for earlier, later in zip(spreads, spreads[1:]):
            self.assertGreater(later, earlier)

    def test_output_stays_inside_the_cube(self):
        size = 9
        step = 1.0 / (size - 1)
        for r in range(size):
            for g in range(size):
                for b in range(size):
                    out = V.saturate((r * step, g * step, b * step), 1.5, "g22")
                    for channel in out:
                        self.assertGreaterEqual(channel, 0.0)
                        self.assertLessEqual(channel, 1.0)

    def test_the_pq_path_is_untouched(self):
        """A Legion Go 2 regression here would be invisible until someone
        looked at an HDR game, so it is pinned explicitly."""
        for channel in V.saturate((1.0, 1.0, 1.0), 1.5):
            self.assertAlmostEqual(channel, 1.0, places=6)
        self.assertEqual(V.saturate((0.42, 0.42, 0.42), 1.5)[0],
                         V.saturate((0.42, 0.42, 0.42), 1.5, "pq")[0])

    def test_an_unknown_encoding_is_refused(self):
        with self.assertRaises(ValueError):
            V.saturate((0.5, 0.5, 0.5), 1.5, "srgb")
        with self.assertRaises(ValueError):
            V.generate(1.5, 5, "srgb")

    def test_the_cube_declares_which_domain_it_is_for(self):
        self.assertIn("g22 domain", V.generate(1.2, 5, "g22"))
        self.assertIn("pq domain", V.generate(1.2, 5))


class ReleaseIsReallyOff(unittest.TestCase):
    """Turning the half off has to leave the picture where it was before it was
    ever turned on - on both branches, whose neutral values disagree."""

    def setUp(self):
        self.written = []
        self._write = main._write_float_atom
        self._release_look = main.Plugin._release_sdr_look
        main._write_float_atom = lambda name, value: (
            self.written.append(value) or True)
        main.Plugin._release_sdr_look = staticmethod(lambda: None)
        main.Plugin._state.update({"sdr_applied": True, "sdr_enabled": True})

    def tearDown(self):
        main._write_float_atom = self._write
        main.Plugin._release_sdr_look = self._release_look
        main.Plugin._state.update({"sdr_applied": False, "sdr_enabled": False})
        main.Plugin._sdr_baseline = None

    def test_without_a_baseline_it_hands_the_atom_back_to_gamescope(self):
        """A negative wideness is gamescope's own "unset", and both branches of
        buildSDRColorimetry read it as that branch's native gamut. 1.0 would be
        neutral on one and the harshest setting on the other."""
        main.Plugin._sdr_baseline = None
        main.Plugin._release_sdr()
        self.assertEqual(self.written, [main.SDR_UNSET])
        self.assertLess(main.SDR_UNSET, 0.0)

    def test_a_real_baseline_is_still_restored(self):
        main.Plugin._sdr_baseline = 0.4
        main.Plugin._release_sdr()
        self.assertEqual(self.written, [0.4])

    def test_the_look_goes_first_even_if_the_atom_was_never_applied(self):
        """Both mechanisms are on at once above 200%; releasing one and not the
        other leaves half the saturation behind, which is what shipped."""
        calls = []
        main.Plugin._release_sdr_look = staticmethod(lambda: calls.append(1))
        main.Plugin._state["sdr_applied"] = False
        main.Plugin._release_sdr()
        self.assertEqual(calls, [1])


class BaselineCapture(unittest.TestCase):

    def test_our_own_leftover_is_not_mistaken_for_a_baseline(self):
        """Reloading the plugin with the half already on used to capture what we
        wrote last time, so "off" restored our own saturation."""
        source = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "main.py"), encoding="utf-8").read()
        start = source.index("Remember what was there before")
        window = source[start:start + 1200]
        self.assertIn('if Plugin._state["sdr_enabled"]:', window)
        self.assertIn("Plugin._sdr_baseline = None", window)


class StartupRetry(unittest.TestCase):
    """Decky starts plugins as a system service and can win the race against
    the session; giving up permanently meant a reinstall to recover."""

    def test_the_backoff_is_bounded_and_starts_quickly(self):
        self.assertGreater(main.GAMESCOPE_RETRY_FIRST_S, 0.0)
        self.assertLessEqual(main.GAMESCOPE_RETRY_FIRST_S, 2.0)
        self.assertGreater(main.GAMESCOPE_RETRY_MAX_S,
                           main.GAMESCOPE_RETRY_FIRST_S)
        self.assertLessEqual(main.GAMESCOPE_RETRY_MAX_S, 60.0)

    def test_startup_keeps_trying_until_gamescope_turns_up(self):
        attempts = []
        real_pick, real_sleep = main._pick_display, asyncio.sleep
        real_wide, real_pq = main._panel_is_wide_gamut, main._panel_supports_hdr
        real_read = main._read_float_atom
        saved = (main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN)

        def flaky():
            attempts.append(1)
            return ":0" if len(attempts) >= 3 else None

        async def no_wait(_seconds):
            return None

        main._pick_display = flaky
        main._panel_is_wide_gamut = lambda: True
        main._panel_supports_hdr = lambda: True
        main._read_float_atom = lambda name: 1.0
        asyncio.sleep = no_wait
        try:
            _drain(main.Plugin()._start_when_ready())
        finally:
            main._pick_display, asyncio.sleep = real_pick, real_sleep
            main._panel_is_wide_gamut, main._panel_supports_hdr = real_wide, real_pq
            main._read_float_atom = real_read
            main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = saved
        self.assertEqual(len(attempts), 3)
        self.assertTrue(main.Plugin._state["ready"])


class StartupSettles(unittest.TestCase):
    """gamescope publishes its atoms as it comes up. Writing into a half-built
    session gets thrown away when it finishes, which showed up as a Legion Go S
    coming back from a restart with the atom applied and the look missing -
    exactly the part of the slider the atom cannot reach."""

    def test_it_waits_for_the_count_to_stop_growing(self):
        counts = iter([20, 30, 44, 44, 44])
        seen = []

        def counter(display):
            value = next(counts)
            seen.append(value)
            return value

        real_count, real_sleep = main._count_gamescope_atoms, asyncio.sleep
        real_pick = main._pick_display
        real_wide, real_pq = main._panel_is_wide_gamut, main._panel_supports_hdr
        real_read, real_write = main._read_float_atom, main._write_float_atom
        real_string = main._write_string_atom
        saved = (main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN)

        async def no_wait(_seconds):
            return None

        main._count_gamescope_atoms = counter
        main._pick_display = lambda: ":0"
        main._panel_is_wide_gamut = lambda: True
        main._panel_supports_hdr = lambda: True
        main._read_float_atom = lambda name: 1.0
        main._write_float_atom = lambda name, value: True
        main._write_string_atom = lambda name, value: True
        asyncio.sleep = no_wait
        try:
            _drain(main.Plugin()._start_when_ready())
        finally:
            main._count_gamescope_atoms, asyncio.sleep = real_count, real_sleep
            main._pick_display = real_pick
            main._panel_is_wide_gamut, main._panel_supports_hdr = real_wide, real_pq
            main._read_float_atom, main._write_float_atom = real_read, real_write
            main._write_string_atom = real_string
            main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = saved
        # Stops on the first repeat rather than reading forever.
        self.assertEqual(seen, [20, 30, 44, 44])

    def test_the_settle_wait_is_bounded(self):
        """A session that never stops growing must not hold startup for ever."""
        self.assertGreater(main.GAMESCOPE_SETTLE_TRIES, 2)
        self.assertLessEqual(
            main.GAMESCOPE_SETTLE_TRIES * main.GAMESCOPE_SETTLE_S, 30.0)

    def test_startup_puts_the_values_back_more_than_once(self):
        """One write into a settling session is not enough; the same ladder the
        resume path uses is what makes it stick."""
        source = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "main.py"), encoding="utf-8").read()
        start = source.index("async def _start_when_ready")
        end = source.index("_monitor_loop()", start)
        self.assertIn("_reassert_repeatedly", source[start:end])

    def test_a_disabled_half_clears_what_the_last_run_left(self):
        """The look atom outlives a plugin reload, and nothing reloads it into
        our state - so "off" has to be written, not assumed."""
        cleared = []
        real_string = main._write_string_atom
        main._write_string_atom = lambda name, value: (
            cleared.append((name, value)) or True)
        main.Plugin._state["sdr_look_applied"] = True
        try:
            main.Plugin._release_sdr_look()
        finally:
            main._write_string_atom = real_string
        self.assertEqual(cleared, [(main.ATOM_LOOK_G22, "")])


class StartupAssertsOff(unittest.TestCase):
    """Both look atoms outlive a plugin reload and nothing reloads them into
    our state, so a half that starts disabled has to say so out loud."""

    def _boot_with(self, sdr_on, hdr_on):
        written = []
        real = {name: getattr(main, name) for name in
                ("_pick_display", "_panel_is_wide_gamut", "_panel_supports_hdr",
                 "_read_float_atom", "_write_float_atom", "_write_string_atom",
                 "_count_gamescope_atoms")}
        real_sleep = asyncio.sleep
        saved_mapping = (main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN)
        saved_state = {k: main.Plugin._state[k] for k in
                       ("sdr_enabled", "hdr_enabled", "sdr_look_applied",
                        "hdr_applied", "sdr_applied")}

        async def no_wait(_seconds):
            return None

        main._pick_display = lambda: ":0"
        main._panel_is_wide_gamut = lambda: True
        main._panel_supports_hdr = lambda: True
        main._read_float_atom = lambda name: 1.0
        main._write_float_atom = lambda name, value: True
        main._count_gamescope_atoms = lambda display: 44
        main._write_string_atom = lambda name, value: (
            written.append((name, value)) or True)
        asyncio.sleep = no_wait
        main.settings._store.update({"sdr_enabled": sdr_on,
                                     "hdr_enabled": hdr_on})
        try:
            _drain(main.Plugin()._main())
            _drain(main.Plugin()._start_when_ready())
        finally:
            for name, fn in real.items():
                setattr(main, name, fn)
            asyncio.sleep = real_sleep
            main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = saved_mapping
            main.Plugin._state.update(saved_state)
            main.settings._store.clear()
        return written

    def test_a_disabled_sdr_half_clears_the_g22_look_on_startup(self):
        written = self._boot_with(sdr_on=False, hdr_on=False)
        self.assertIn((main.ATOM_LOOK_G22, ""), written)

    def test_a_disabled_hdr_half_clears_the_pq_look_on_startup(self):
        written = self._boot_with(sdr_on=False, hdr_on=False)
        self.assertIn((main.ATOM_LOOK_PQ, ""), written)

    def test_an_enabled_half_is_not_cleared_out_from_under_itself(self):
        written = self._boot_with(sdr_on=True, hdr_on=False)
        self.assertNotIn((main.ATOM_LOOK_G22, ""), written)


class ExternalDisplay(unittest.TestCase):
    """Everything here is calibrated against the built-in panel, so anything
    else is somebody else's display and gets left alone."""

    def setUp(self):
        self.saved = {k: main.Plugin._state[k] for k in
                      ("external_display", "sdr_max", "hdr_supported",
                       "sdr_enabled", "hdr_enabled")}
        self.saved_mapping = (main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN)
        self.real = {n: getattr(main, n) for n in
                     ("_display_is_external", "_panel_is_wide_gamut",
                      "_panel_supports_hdr")}

    def tearDown(self):
        for name, fn in self.real.items():
            setattr(main, name, fn)
        main.Plugin._state.update(self.saved)
        main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = self.saved_mapping

    def _configure(self, external):
        main._display_is_external = lambda: external
        main._panel_is_wide_gamut = lambda: True
        main._panel_supports_hdr = lambda: True
        return main.Plugin._configure_for_panel()

    def test_an_external_display_is_recognised(self):
        self._configure(external=True)
        self.assertTrue(main.Plugin._state["external_display"])

    def test_the_internal_panel_is_not(self):
        self._configure(external=False)
        self.assertFalse(main.Plugin._state["external_display"])

    def test_a_missing_atom_is_read_as_internal(self):
        """Older gamescope builds do not publish it, and withdrawing the whole
        plugin over an absent property would be worse than the alternative."""
        self._configure(external=None)
        self.assertFalse(main.Plugin._state["external_display"])

    def test_docking_and_undocking_both_register_as_a_change(self):
        self._configure(external=False)
        self.assertTrue(self._configure(external=True))
        self.assertTrue(self._configure(external=False))

    def test_nothing_is_measured_off_an_external_display(self):
        """Its gamut must not choose our mapping - the numbers were calibrated
        against the built-in panel."""
        self._configure(external=False)
        main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN = 1.0, 2.0
        probed = []
        main._display_is_external = lambda: True
        main._panel_is_wide_gamut = lambda: probed.append(1) or False
        main._panel_supports_hdr = lambda: probed.append(1) or False
        main.Plugin._configure_for_panel()
        self.assertEqual(probed, [])
        self.assertEqual((main.SDR_NEUTRAL_ATOM, main.SDR_ATOM_SPAN), (1.0, 2.0))

    def test_neither_half_can_be_switched_on_while_docked(self):
        main.Plugin._state.update({"external_display": True,
                                   "sdr_enabled": False, "hdr_enabled": False,
                                   "hdr_supported": True})
        self.assertFalse(
            _drain(main.Plugin().set_sdr_enabled(True))["sdr_enabled"])
        self.assertFalse(
            _drain(main.Plugin().set_hdr_enabled(True))["hdr_enabled"])
