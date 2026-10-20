# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""EDID parsing, panel detection, HDR gating and external displays."""

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from _fixtures import PluginCase, drain, edid_with_red  # installs the loader stand-ins first
import main as M
import vibrance_lut as V
import _harness


def edid_cta(*masks):
    base = bytearray(128)
    base[126] = 1
    block = bytearray(128)
    block[:3] = bytes((2, 3, 4 + len(masks) * 4))
    for n, mask in enumerate(masks):
        block[4 + n * 4:8 + n * 4] = bytes((0xe3, 6, mask, 1))
    return _harness.valid_edid(base + block)


class PanelDetection(unittest.TestCase):
    """gamescope's own BIsWideGamut test, and the EDID bytes it needs."""

    def test_parses_the_panel_this_was_written_for(self):
        # edid-decode reports 0.6835, 0.3154 for the Legion Go 2 panel.
        red_x, red_y = M.vibrance_edid.red_primary(edid_with_red(0.6836, 0.3154))
        self.assertAlmostEqual(red_x, 0.6836, places=3)
        self.assertAlmostEqual(red_y, 0.3154, places=3)

    def test_wide_and_standard_gamut_land_on_opposite_sides(self):
        wide = M.vibrance_edid.red_primary(edid_with_red(0.6836, 0.3154))
        self.assertTrue(wide[0] > M.WIDE_GAMUT_RED_X
                        and wide[1] < M.WIDE_GAMUT_RED_Y)
        # Rec.709 red, i.e. a plain sRGB panel.
        plain = M.vibrance_edid.red_primary(edid_with_red(0.640, 0.330))
        self.assertFalse(plain[0] > M.WIDE_GAMUT_RED_X
                         and plain[1] < M.WIDE_GAMUT_RED_Y)

    def test_thresholds_match_gamescope(self):
        """color_helpers.cpp BIsWideGamut: red.x > 0.650 and red.y < 0.320."""
        self.assertEqual(M.WIDE_GAMUT_RED_X, 0.650)
        self.assertEqual(M.WIDE_GAMUT_RED_Y, 0.320)

    def test_a_truncated_edid_is_not_guessed_at(self):
        self.assertIsNone(M.vibrance_edid.red_primary(b"\x00" * 16))


class HdrCapability(unittest.TestCase):
    """Read from the panel's CTA-861 block rather than from
    GAMESCOPE_HDR_OUTPUT_FEEDBACK, whose meaning changed between gamescope
    builds and took a sibling plugin down with it on Bazzite 44."""

    @staticmethod
    def _edid(eotf_mask=None, cta=True):
        base = bytearray(128)
        base[126] = 1 if cta else 0
        if not cta:
            return _harness.valid_edid(base)
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
        return _harness.valid_edid(base + block)

    def test_a_panel_advertising_st2084(self):
        # Traditional gamma SDR + SMPTE ST 2084, as the Legion Go 2 reports.
        self.assertTrue(M.vibrance_edid.supports_pq(self._edid(0b0000_0101)))

    def test_a_panel_advertising_only_gamma(self):
        self.assertFalse(M.vibrance_edid.supports_pq(self._edid(0b0000_0001)))

    def test_hlg_alone_is_not_the_transfer_gamescope_drives(self):
        self.assertFalse(M.vibrance_edid.supports_pq(self._edid(0b0000_1000)))

    def test_a_cta_block_with_no_hdr_metadata_is_an_answer(self):
        self.assertFalse(M.vibrance_edid.supports_pq(self._edid(None)))

    def test_dtd_offset_zero_means_no_data_blocks(self):
        """CTA-861 d=0: no data block collection, so bytes after the header -
        here an HDR block left over - must not be read as one."""
        blob = bytearray(self._edid(0b0000_0101))
        blob[128 + 2] = 0
        self.assertFalse(M.vibrance_edid.supports_pq(_harness.valid_edid(blob)))

    def test_a_base_only_edid_is_a_definite_no(self):
        """HDR metadata has nowhere else to live, so a complete EDID without it
        is an answer rather than a shrug."""
        self.assertFalse(M.vibrance_edid.supports_pq(self._edid(cta=False)))

    def test_a_displayid_extension_with_no_hdr_in_it_is_a_no(self):
        """The Legion Go S shape: one DisplayID block, no CTA extension. Read as
        "no idea" this left the HDR section on a panel that has no HDR."""
        base = bytearray(128)
        base[126] = 1
        displayid = bytearray(128)
        displayid[0] = 0x70
        self.assertFalse(M.vibrance_edid.supports_pq(_harness.valid_edid(base + displayid)))

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
        self.assertTrue(M.vibrance_edid.supports_pq(_harness.valid_edid(base + displayid)))

    def test_a_truncated_edid_is_not_an_answer(self):
        self.assertIsNone(M.vibrance_edid.supports_pq(b"\x00" * 64))

    def test_a_block_promising_more_than_it_holds_does_not_read_past_it(self):
        edid = bytearray(self._edid(0b0000_0101))
        edid[128 + 2] = 127                   # collection runs to the very end
        self.assertIn(M.vibrance_edid.supports_pq(_harness.valid_edid(edid)), (True, False))


class Edid(unittest.TestCase):
    def setUp(self):
        for obj, name in ((M.Plugin, "_state"), (M.Plugin, "_sdr_white_point"),
                          (M, "SDR_NEUTRAL_ATOM"), (M, "SDR_ATOM_SPAN")):
            value = getattr(obj, name)
            context = patch.object(obj, name, dict(value) if isinstance(value, dict) else value)
            context.start()
            self.addCleanup(context.stop)
    def test_pq_after_non_pq_metadata_is_found(self):
        self.assertIs(M.vibrance_edid.supports_pq(edid_cta(1, 4)), True)
        blob = bytearray(edid_cta(1))
        blob[126] = 2
        blob.extend(edid_cta(4)[128:])
        self.assertIs(M.vibrance_edid.supports_pq(_harness.valid_edid(blob)), True)

    def test_invalid_header_checksum_and_truncated_blocks_are_unknown(self):
        valid = edid_cta(4)
        for index in (0, 20, 150, 255):
            broken = bytearray(valid)
            broken[index] ^= 1
            self.assertIsNone(M.vibrance_edid.supports_pq(broken))
        self.assertIsNone(M.vibrance_edid.red_primary(bytes(29)))
        truncated = bytearray(valid)
        truncated[130] = 6
        self.assertIsNone(M.vibrance_edid.supports_pq(_harness.valid_edid(truncated)))

    def test_displayid_scans_all_embedded_cta_collections(self):
        base, extension = bytearray(128), bytearray(128)
        base[126] = 1
        extension[0], extension[2] = 0x70, 14
        extension[5:19] = bytes((0x81, 0, 4, 0xe3, 6, 1, 1, 0x81, 0, 4, 0xe3, 6, 4, 1))
        self.assertIs(M.vibrance_edid.supports_pq(_harness.valid_edid(base + extension)), True)

    def test_panel_reads_one_edid_and_invalidates_cache_when_path_changes(self):
        with patch.object(M.Plugin, "_panel_cache_key", None), patch.object(M.Plugin, "_panel_cache_complete", False), \
                patch.object(M, "_display_is_external", return_value=False), \
                patch.object(M, "_read_string_atom", side_effect=["/edid/one", "/edid/one", "/edid/two"]), \
                patch.object(M, "_read_edid", return_value=edid_cta(4)) as reader:
            M.Plugin._configure_for_panel()
            M.Plugin._configure_for_panel()
            M.Plugin._configure_for_panel()
            # Red coordinates in this fixture are unspecified, so classification
            # retries. A valid panel fixture completes and caches below.
            self.assertEqual(reader.call_count, 3)

    def test_valid_panel_cache_detects_an_internal_edid_path_change(self):
        blob = bytearray(edid_cta(4))
        red = edid_with_red(0.6836, 0.3154)
        blob[0x19:0x1d] = red[0x19:0x1d]
        blob = _harness.valid_edid(blob)
        with patch.object(M.Plugin, "_panel_cache_key", None), patch.object(M.Plugin, "_panel_cache_complete", False), \
                patch.object(M, "_display_is_external", return_value=False), \
                patch.object(M, "_read_string_atom", side_effect=["/edid/one", "/edid/one", "/edid/two"]), \
                patch.object(M, "_read_edid", return_value=blob) as reader:
            M.Plugin._configure_for_panel()
            M.Plugin._configure_for_panel()
            M.Plugin._configure_for_panel()
            self.assertEqual(reader.call_count, 2)


class MetadataFiles(unittest.TestCase):
    def test_regular_edid_read_remains_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "edid.bin"
            data = bytes(range(256)) * 200
            path.write_bytes(data)
            self.assertEqual(M._read_edid(str(path)), data[:128 * 256 + 1])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO fixture")
    def test_fifo_metadata_cannot_hang_or_leak_descriptors(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "edid-fifo"
            os.mkfifo(path)
            code = """import sys,os
sys.path.insert(0, 'tests')
import _harness
_harness.install()
import main
before=len(os.listdir('/proc/self/fd'))
for _ in range(20):
 assert main._read_edid(sys.argv[1]) is None
assert len(os.listdir('/proc/self/fd'))==before
"""
            try:
                result = subprocess.run([sys.executable, "-W", "error", "-c", code, str(path)],
                                        cwd=Path(M.__file__).parent, capture_output=True,
                                        text=True, timeout=2)
            except subprocess.TimeoutExpired:
                self.fail("EDID FIFO blocked startup instead of returning unknown metadata")
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture")
    def test_regular_edid_target_is_read_through_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "edid.bin"
            target.write_bytes(b"display metadata")
            alias = Path(folder) / "edid-link"
            alias.symlink_to(target)
            self.assertEqual(M._read_edid(str(alias)), target.read_bytes())


class PanelCache(unittest.TestCase):
    def test_missing_edid_path_does_not_spawn_a_duplicate_xprop_read(self):
        with patch.object(M, "_display_is_external", return_value=False), \
                patch.object(M, "_read_string_atom", return_value=None) as read:
            M.Plugin._probe_panel()
        read.assert_called_once_with(M.ATOM_EDID_PATH)

    def test_atomic_edid_replacement_at_the_same_path_changes_panel_mapping(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "panel.bin"
            path.write_bytes(edid_with_red(0.6836, 0.3154))
            with patch.object(M.Plugin, "_state", dict(M.Plugin._state)), \
                    patch.object(M.Plugin, "_panel_cache_key", None), \
                    patch.object(M.Plugin, "_panel_cache_complete", False), \
                    patch.object(M, "SDR_NEUTRAL_ATOM", 1.0), \
                    patch.object(M, "SDR_ATOM_SPAN", 2.0), \
                    patch.object(M, "_display_is_external", return_value=False), \
                    patch.object(M, "_read_string_atom", return_value=str(path)), \
                    patch.object(M, "_read_edid", wraps=M._read_edid) as read:
                M.Plugin._configure_for_panel()
                M.Plugin._configure_for_panel()
                self.assertEqual(read.call_count, 1, "unchanged file should stay cached")
                replacement = Path(folder) / "new.bin"
                replacement.write_bytes(edid_with_red(0.640, 0.330))
                os.replace(replacement, path)
                self.assertTrue(M.Plugin._configure_for_panel())
                self.assertEqual(M.SDR_NEUTRAL_ATOM, 0)
                self.assertEqual(read.call_count, 2)


class SdrColorimetry(unittest.TestCase):
    # Independent reference: exact rational Gaussian elimination of the RGB
    # primary matrices from gamescope's generic gamut and BT.2020, both D65.
    TO_2020 = ((0.8421220396036041, 0.1405712459507854, 0.017306714445610463),
               (0.045033388222038326, 0.9528639745224308, 0.0021026372555309094),
               (-0.00044189761592675655, 0.010632628934500689, 0.989809268681426))

    def ictcp(self, rgb):
        linear = V._mul3(self.TO_2020, tuple(V.g22_to_linear(c) for c in rgb))
        lms = V._mul3(V._RGB_TO_LMS, linear)
        return V._mul3(V._LMS_TO_ICTCP, tuple(V.linear_to_pq(c) for c in lms))

    def test_sdr_chroma_scaling_preserves_intensity_in_the_real_source_gamut(self):
        for rgb in ((0.6, 0.4, 0.2), (0.4, 0.2, 0.6), (0.4, 0.5, 0.3)):
            original = self.ictcp(rgb)
            out = V.saturate(rgb, 1.25, "g22")
            self.assertTrue(all(0 < c < 1 for c in out), "reference must not clip")
            adjusted = self.ictcp(out)
            for actual, expected in zip(adjusted,
                                        (original[0], original[1]*1.25, original[2]*1.25)):
                self.assertAlmostEqual(actual, expected, places=9)

    def test_non_d65_white_keeps_greys_neutral(self):
        for white in ((0.296, 0.307), (0.307, 0.322), V.D65):
            for grey in (0.0, 0.1, 0.5, 1.0):
                out = V.saturate((grey,)*3, 1.5, "g22", white)
                for channel in out:
                    self.assertAlmostEqual(channel, grey, places=6)

    def test_edid_white_bits_and_invalid_fallback(self):
        blob = bytearray(128)
        x, y = 303, 314
        blob[0x21], blob[0x22] = x >> 2, y >> 2
        blob[0x1A] = ((x & 3) << 2) | (y & 3)
        self.assertEqual(M.vibrance_edid.white_point(_harness.valid_edid(blob)), (x/1024, y/1024))
        for invalid in (None, b"", bytes(128)):
            self.assertEqual(M.vibrance_edid.white_point(invalid), V.D65)


class CapabilityGating(unittest.TestCase):
    """The two halves withdraw independently, and only on a definite no."""

    KEYS = ("hdr_supported", "sdr_enabled", "hdr_enabled",
            "sdr_max", "sdr_soft_limit", "sdr_saturation")

    def setUp(self):
        self.saved_state = {k: M.Plugin._state[k] for k in self.KEYS}
        self.saved_fns = {name: getattr(M, name) for name in
                          ("_pick_display", "_panel_is_wide_gamut",
                           "_panel_supports_hdr", "_read_float_atom")}
        # _main rewrites these module globals; leaving them changed makes an
        # unrelated test fail later, which is a horrible way to find out.
        self.saved_mapping = (M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN)
        M._pick_display = lambda: ":0"
        M._read_float_atom = lambda name: 1.0
        self.real_sleep, self.real_count = asyncio.sleep, M._count_gamescope_atoms

        async def no_wait(_seconds):
            return None

        asyncio.sleep = no_wait
        M._count_gamescope_atoms = lambda display: 44

    def tearDown(self):
        for name, fn in self.saved_fns.items():
            setattr(M, name, fn)
        M.Plugin._state.update(self.saved_state)
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = self.saved_mapping
        asyncio.sleep, M._count_gamescope_atoms = self.real_sleep, self.real_count
        M.settings._store.clear()

    def _boot(self, wide, pq, hdr_on=True):
        M._panel_is_wide_gamut = lambda *_args: wide
        M._panel_supports_hdr = lambda *_args: pq
        # _main reloads these from the settings, so seeding the state is not
        # enough to get an enabled half into the startup path.
        M.settings._store.update({"sdr_enabled": False, "hdr_enabled": hdr_on})
        M.Plugin._state.update({"hdr_supported": True})
        # _main only loads settings now; the part that reads the panel waits in
        # a task, so it is driven explicitly here.
        drain(M.Plugin()._main())
        drain(M.Plugin()._start_when_ready())

    def test_a_panel_without_pq_loses_the_hdr_half_only(self):
        self._boot(wide=True, pq=False)
        self.assertFalse(M.Plugin._state["hdr_supported"])
        self.assertFalse(M.Plugin._state["hdr_enabled"])

    def test_a_standard_gamut_panel_gets_the_other_mapping(self):
        """A Legion Go S keeps a working SDR slider; it just runs against an
        atom whose neutral is 0.0 and which gives out after one unit."""
        self._boot(wide=False, pq=True)
        self.assertEqual(M.SDR_NEUTRAL_ATOM, 0.0)
        self.assertEqual(M.SDR_ATOM_SPAN, 1.0)

    def test_the_look_carries_a_standard_panel_to_the_full_range(self):
        self._boot(wide=False, pq=True)
        self.assertEqual(M.Plugin._state["sdr_max"], M.SDR_MAX_STANDARD)
        self.assertEqual(M.Plugin._state["sdr_soft_limit"], M.SDR_SOFT_STANDARD)

    def test_a_wide_gamut_panel_keeps_its_own_numbers(self):
        """The Legion Go 2 must not pick up the standard-gamut mapping."""
        self._boot(wide=True, pq=True)
        self.assertEqual(M.SDR_NEUTRAL_ATOM, 1.0)
        self.assertEqual(M.SDR_ATOM_SPAN, 2.0)
        self.assertEqual(M.Plugin._state["sdr_max"], M.SDR_MAX_WIDE)
        self.assertEqual(M.Plugin._state["sdr_soft_limit"], M.SDR_SOFT_WIDE)

    def test_an_edid_we_cannot_read_withdraws_nothing(self):
        """Ignorance is not a no - a missing atom must not cost a user a
        control that works."""
        self._boot(wide=None, pq=None)
        self.assertEqual(M.SDR_NEUTRAL_ATOM, 1.0)
        self.assertTrue(M.Plugin._state["hdr_supported"])

    def test_a_capable_panel_keeps_both(self):
        self._boot(wide=True, pq=True)
        self.assertEqual(M.SDR_NEUTRAL_ATOM, 1.0)
        self.assertEqual(M.Plugin._state["sdr_max"], M.SDR_MAX_WIDE)
        self.assertTrue(M.Plugin._state["hdr_supported"])

    def test_the_hdr_setter_refuses_once_the_half_is_withdrawn(self):
        """The frontend hides the section, but the callable stays reachable."""
        M.Plugin._state.update({"hdr_supported": False, "hdr_enabled": False})
        state = drain(M.Plugin().set_hdr_enabled(True))
        self.assertFalse(state["hdr_enabled"])


class ExternalDisplay(unittest.TestCase):
    """Everything here is calibrated against the built-in panel, so anything
    else is somebody else's display and gets left alone."""

    def setUp(self):
        self.saved = {k: M.Plugin._state[k] for k in
                      ("external_display", "sdr_max", "hdr_supported",
                       "sdr_enabled", "hdr_enabled")}
        self.saved_mapping = (M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN)
        self.saved_cache = (M.Plugin._panel_cache_key,
                            M.Plugin._panel_cache_complete)
        M.Plugin._panel_cache_key = None
        M.Plugin._panel_cache_complete = False
        self.real = {n: getattr(M, n) for n in
                     ("_display_is_external", "_panel_is_wide_gamut",
                      "_panel_supports_hdr")}

    def tearDown(self):
        for name, fn in self.real.items():
            setattr(M, name, fn)
        M.Plugin._state.update(self.saved)
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = self.saved_mapping
        (M.Plugin._panel_cache_key,
         M.Plugin._panel_cache_complete) = self.saved_cache

    def _configure(self, external):
        M._display_is_external = lambda: external
        M._panel_is_wide_gamut = lambda *_args: True
        M._panel_supports_hdr = lambda *_args: True
        return M.Plugin._configure_for_panel()

    def test_an_external_display_is_recognised(self):
        self._configure(external=True)
        self.assertTrue(M.Plugin._state["external_display"])

    def test_the_internal_panel_is_not(self):
        self._configure(external=False)
        self.assertFalse(M.Plugin._state["external_display"])

    def test_a_missing_atom_is_read_as_internal(self):
        """Older gamescope builds do not publish it, and withdrawing the whole
        plugin over an absent property would be worse than the alternative."""
        self._configure(external=None)
        self.assertFalse(M.Plugin._state["external_display"])

    def test_docking_and_undocking_both_register_as_a_change(self):
        self._configure(external=False)
        self.assertTrue(self._configure(external=True))
        self.assertTrue(self._configure(external=False))

    def test_stable_panel_reuses_its_cached_classification(self):
        calls = []
        M._display_is_external = lambda: False
        M._panel_is_wide_gamut = lambda *_args: calls.append("wide") or True
        M._panel_supports_hdr = lambda *_args: calls.append("hdr") or True
        M.Plugin._panel_cache_key = None
        M.Plugin._panel_cache_complete = False
        M.Plugin._configure_for_panel()
        M.Plugin._configure_for_panel()
        self.assertEqual(calls, ["wide", "hdr"])

    def test_nothing_is_measured_off_an_external_display(self):
        """Its gamut must not choose our mapping - the numbers were calibrated
        against the built-in panel."""
        self._configure(external=False)
        M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 1.0, 2.0
        probed = []
        M._display_is_external = lambda: True
        M._panel_is_wide_gamut = lambda *_args: probed.append(1) or False
        M._panel_supports_hdr = lambda *_args: probed.append(1) or False
        M.Plugin._configure_for_panel()
        self.assertEqual(probed, [])
        self.assertEqual((M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN), (1.0, 2.0))

    def test_neither_half_can_be_switched_on_while_docked(self):
        M.Plugin._state.update({"external_display": True,
                                   "sdr_enabled": False, "hdr_enabled": False,
                                   "hdr_supported": True})
        self.assertFalse(
            drain(M.Plugin().set_sdr_enabled(True))["sdr_enabled"])
        self.assertFalse(
            drain(M.Plugin().set_hdr_enabled(True))["hdr_enabled"])


class StartupOnExternalDisplay(unittest.TestCase):

    def test_enabled_settings_do_not_apply_while_already_docked(self):
        calls = []
        names = (
            "_pick_display", "_count_gamescope_atoms", "_read_float_atom",
            "_read_string_atom", "_write_float_atom", "_write_string_atom",
            "_display_is_external", "_panel_is_wide_gamut",
            "_panel_supports_hdr",
        )
        real = {name: getattr(M, name) for name in names}
        real_sleep = asyncio.sleep
        real_track = M.Plugin._track
        real_schedule = M.Plugin._schedule_look
        real_apply = M.Plugin._apply_sdr
        real_lock = M.Plugin._mutation_lock
        saved_state = dict(M.Plugin._state)
        saved_unloading = M.Plugin._unloading
        saved_cache = (M.Plugin._panel_cache_key,
                       M.Plugin._panel_cache_complete)
        saved_looks = (
            M.Plugin._sdr_look_path, M.Plugin._sdr_look_baseline,
            M.Plugin._hdr_look_path, M.Plugin._hdr_look_baseline,
        )

        async def no_wait(_seconds):
            return None

        M._pick_display = lambda: ":0"
        M._count_gamescope_atoms = lambda display: 44
        M._read_float_atom = lambda atom: 1.0
        M._read_string_atom = lambda atom: None
        M._write_float_atom = lambda atom, value: (
            calls.append((atom, value)) or True)
        M._write_string_atom = lambda atom, value: (
            calls.append((atom, value)) or True)
        M._display_is_external = lambda: True
        M._panel_is_wide_gamut = lambda *_args: self.fail(
            "external panel must not be measured")
        M._panel_supports_hdr = lambda *_args: self.fail(
            "external panel must not be measured")
        M.Plugin._apply_sdr = staticmethod(lambda: calls.append("apply SDR"))
        M.Plugin._schedule_look = lambda self, component: calls.append(
            f"schedule {component.upper()}")
        M.Plugin._track = staticmethod(lambda task: task.cancel())
        M.Plugin._mutation_lock = asyncio.Lock()
        M.Plugin._panel_cache_key = None
        M.Plugin._panel_cache_complete = False
        M.Plugin._unloading = False
        M.Plugin._state.update({
            "ready": False, "external_display": False,
            "sdr_enabled": True, "hdr_enabled": True,
            "hdr_supported": True,
        })
        asyncio.sleep = no_wait
        try:
            asyncio.run(M.Plugin()._start_when_ready())
        finally:
            for name, fn in real.items():
                setattr(M, name, fn)
            asyncio.sleep = real_sleep
            M.Plugin._track = real_track
            M.Plugin._schedule_look = real_schedule
            M.Plugin._apply_sdr = real_apply
            M.Plugin._mutation_lock = real_lock
            M.Plugin._unloading = saved_unloading
            (M.Plugin._panel_cache_key,
             M.Plugin._panel_cache_complete) = saved_cache
            (M.Plugin._sdr_look_path, M.Plugin._sdr_look_baseline,
             M.Plugin._hdr_look_path,
             M.Plugin._hdr_look_baseline) = saved_looks
            M.Plugin._state.update(saved_state)
        self.assertEqual(calls, [])


class DisplayTransitions(PluginCase):

    async def test_slow_panel_probe_does_not_block_slider_mutations(self):
        started, release = threading.Event(), threading.Event()
        def external():
            started.set()
            release.wait(2)
            return False
        self.mock(M, "_display_is_external", side_effect=external)
        panel = asyncio.create_task(self.plugin._refresh_panel())
        change = None
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            change = asyncio.create_task(self.plugin.set_hdr_saturation(1.2))
            done, _ = await asyncio.wait([change], timeout=0.2)
            self.assertIn(change, done, "panel metadata IO monopolized the mutation lock")
        finally:
            release.set()
            await panel
            if change is not None:
                await change
            await self.drain()

    async def test_internal_metadata_refresh_preserves_the_sdr_restore_value(self):
        await self.plugin.set_sdr_enabled(True)
        self.mock(M.Plugin, "_configure_for_panel", return_value=True)
        await self.plugin._refresh_panel()
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)

    async def test_return_to_internal_captures_its_current_sdr_baseline(self):
        M.Plugin._state.update(sdr_enabled=True, external_display=True)
        self.live_sdr = 0.6
        self.mock(M.Plugin, "_configure_for_panel", side_effect=lambda *args:
                  M.Plugin._state.update(external_display=False) or True)
        await self.plugin._refresh_panel()
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.6)

    async def test_first_lut_build_does_not_replace_a_live_stale_slot(self):
        self.mock(M, "_runtime_dir", return_value="/tmp")
        for encoding, atom, slot_key, enabled, saturation, component in (
                ("pq", M.ATOM_LOOK_PQ, "_look_slot", "hdr_enabled", "hdr_saturation", "hdr"),
                ("g22", M.ATOM_LOOK_G22, "_sdr_look_slot", "sdr_enabled", "sdr_saturation", "sdr")):
            M.SDR_NEUTRAL_ATOM, M.SDR_ATOM_SPAN = 0, 1
            prefix = "hdr-look" if encoding == "pq" else "sdr-look"
            self.strings[atom] = f"/tmp/{prefix}-1.cube"
            setattr(M.Plugin, slot_key, 0)
            M.Plugin._state.update({enabled: True, saturation: 1.3 if encoding == "pq" else 2.6})
            self.builder.reset_mock()
            self.plugin._schedule_look(component)
            await self.drain()
            self.assertEqual(self.builder.call_args.args[1], 0, encoding)

    async def test_foreign_hdr_look_changed_during_build_is_restored(self):
        self.strings[M.ATOM_LOOK_PQ] = "/tmp/foreign-old.cube"
        def build(*args):
            self.strings[M.ATOM_LOOK_PQ] = "/tmp/foreign-new.cube"
            return "/tmp/hdr-look-1.cube"
        self.builder.side_effect = build
        M.Plugin._state.update(hdr_enabled=True, hdr_saturation=1.3)
        self.plugin._schedule_look("hdr")
        await self.drain()
        await self.plugin.set_hdr_enabled(False)
        self.assertEqual(self.strings[M.ATOM_LOOK_PQ], "/tmp/foreign-new.cube")

    async def test_return_after_failed_dock_release_keeps_original_sdr_baseline(self):
        await self.plugin.set_sdr_enabled(True)
        self.float_writer.side_effect = None
        self.float_writer.return_value = False
        M.Plugin._state["external_display"] = True
        M.Plugin._release_sdr()
        self.float_writer.side_effect = self.write_float
        self.mock(M.Plugin, "_configure_for_panel", side_effect=lambda *args:
                  M.Plugin._state.update(external_display=False) or True)
        await self.plugin._refresh_panel()
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)

    async def test_missing_sdr_read_on_return_cannot_erase_an_owned_baseline(self):
        await self.plugin.set_sdr_enabled(True)
        M.Plugin._state["external_display"] = True
        self.mock(M.Plugin, "_configure_for_panel", side_effect=lambda *args:
                  M.Plugin._state.update(external_display=False) or True)
        with patch.object(M, "_read_float_atom", return_value=None):
            await self.plugin._refresh_panel()
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)
