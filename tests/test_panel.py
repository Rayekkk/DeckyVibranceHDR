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

from _fixtures import PluginCase, PluginTest, drain, edid_with_red, root_dump  # installs the loader stand-ins first
import main as M
import vibrance_edid
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


class PanelDetection(PluginTest):
    """gamescope's own BIsWideGamut test, and the EDID bytes it needs."""

    def test_parses_the_panel_this_was_written_for(self):
        # edid-decode reports 0.6835, 0.3154 for the Legion Go 2 panel.
        red_x, red_y = vibrance_edid.red_primary(edid_with_red(0.6836, 0.3154))
        self.assertAlmostEqual(red_x, 0.6836, places=3)
        self.assertAlmostEqual(red_y, 0.3154, places=3)

    def test_an_edid_without_colorimetry_is_read_as_rec709_like_gamescope(self):
        """gamescope assumes Rec.709 when red x is zero, which fails the wide
        test; keeping the wide default would make 100% the harshest setting."""
        blank = _harness.valid_edid(bytearray(128))
        self.assertEqual(vibrance_edid.red_primary(blank), vibrance_edid.REC709_RED)
        self.assertIs(M.vibrance_panel.panel_is_wide_gamut(blank), False)
        self.assertEqual(vibrance_edid.white_point(blank), M.vibrance_lut.D65)

    def test_wide_gamut_needs_red_past_both_thresholds(self):
        cases = {(0.6836, 0.3154): True,    # Legion Go 2
                 (0.640, 0.330): False,     # Rec.709 red, a plain sRGB panel
                 (0.670, 0.325): False, (0.640, 0.310): False}
        for red, wide in cases.items():
            self.assertIs(M.vibrance_panel.panel_is_wide_gamut(edid_with_red(*red)), wide, red)

    def test_odd_red_is_taken_as_gamescope_takes_it_and_odd_white_is_not(self):
        # gamescope runs BIsWideGamut on any red with x != 0: this one is standard.
        self.assertIs(M.vibrance_panel.panel_is_wide_gamut(edid_with_red(0.700, 0.400)), False)
        blob = bytearray(edid_with_red(0.640, 0.330))
        blob[0x21], blob[0x22] = 0x99, 0x80     # white x + y above 1
        self.assertEqual(vibrance_edid.white_point(_harness.valid_edid(blob)), V.D65)
        self.assertFalse(vibrance_edid.valid(edid_with_red(0.640, 0.330) + b"\0"))

    def test_thresholds_match_gamescope(self):
        """color_helpers.cpp BIsWideGamut: red.x > 0.650 and red.y < 0.320."""
        self.assertEqual(M.vibrance_panel.WIDE_GAMUT_RED_X, 0.650)
        self.assertEqual(M.vibrance_panel.WIDE_GAMUT_RED_Y, 0.320)

    def test_a_truncated_edid_is_not_guessed_at(self):
        self.assertIsNone(vibrance_edid.red_primary(b"\x00" * 16))


class HdrCapability(PluginTest):
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
        self.assertTrue(vibrance_edid.supports_pq(self._edid(0b0000_0101)))

    def test_a_panel_advertising_only_gamma(self):
        self.assertFalse(vibrance_edid.supports_pq(self._edid(0b0000_0001)))

    def test_hlg_alone_is_not_the_transfer_gamescope_drives(self):
        self.assertFalse(vibrance_edid.supports_pq(self._edid(0b0000_1000)))

    def test_a_cta_block_with_no_hdr_metadata_is_an_answer(self):
        self.assertFalse(vibrance_edid.supports_pq(self._edid(None)))

    def test_dtd_offset_zero_still_reads_a_listed_hdr_block(self):
        """CTA-861 d=0 should mean no data blocks. A non-conformant panel that
        lists PQ there is believed on purpose: hiding a working HDR control
        and saving it off is worse than showing one that does nothing."""
        blob = bytearray(self._edid(0b0000_0101))
        blob[128 + 2] = 0
        self.assertTrue(vibrance_edid.supports_pq(_harness.valid_edid(blob)))

    def test_a_base_only_edid_is_a_definite_no(self):
        """HDR metadata has nowhere else to live, so a complete EDID without it
        is an answer rather than a shrug."""
        self.assertFalse(vibrance_edid.supports_pq(self._edid(cta=False)))

    def test_a_displayid_extension_with_no_hdr_in_it_is_a_no(self):
        """The Legion Go S shape: one DisplayID block, no CTA extension. Read as
        "no idea" this left the HDR section on a panel that has no HDR."""
        base = bytearray(128)
        base[126] = 1
        displayid = bytearray(128)
        displayid[0] = 0x70
        self.assertFalse(vibrance_edid.supports_pq(_harness.valid_edid(base + displayid)))

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
        self.assertTrue(vibrance_edid.supports_pq(_harness.valid_edid(base + displayid)))

    def test_a_truncated_edid_is_not_an_answer(self):
        self.assertIsNone(vibrance_edid.supports_pq(b"\x00" * 64))

    def test_a_block_promising_more_than_it_holds_does_not_read_past_it(self):
        edid = bytearray(self._edid(0b0000_0101))
        edid[128 + 2] = 127                   # collection runs to the very end
        self.assertIn(vibrance_edid.supports_pq(_harness.valid_edid(edid)), (True, False))


class Edid(PluginTest):
    def setUp(self):
        for obj, name in ((M.state, "public"), (M.state, "sdr_white_point"),
                          (M.state, "sdr_neutral_atom"), (M.state, "sdr_atom_span")):
            value = getattr(obj, name)
            context = patch.object(obj, name, dict(value) if isinstance(value, dict) else value)
            context.start()
            self.addCleanup(context.stop)
    def test_pq_after_non_pq_metadata_is_found(self):
        self.assertIs(vibrance_edid.supports_pq(edid_cta(1, 4)), True)
        blob = bytearray(edid_cta(1))
        blob[126] = 2
        blob.extend(edid_cta(4)[128:])
        self.assertIs(vibrance_edid.supports_pq(_harness.valid_edid(blob)), True)

    def test_invalid_header_checksum_and_truncated_blocks_are_unknown(self):
        valid = edid_cta(4)
        for index in (0, 20, 150, 255):
            broken = bytearray(valid)
            broken[index] ^= 1
            self.assertIsNone(vibrance_edid.supports_pq(broken))
        self.assertIsNone(vibrance_edid.red_primary(bytes(29)))
        truncated = bytearray(valid)
        truncated[130] = 6
        self.assertIsNone(vibrance_edid.supports_pq(_harness.valid_edid(truncated)))

    def test_malformed_cta_extensions_are_unknown(self):
        short_end = bytearray(edid_cta(4))
        short_end[130] = 3                       # data blocks would end before they start
        short_metadata = bytearray(edid_cta())
        short_metadata[130] = 7
        short_metadata[132:135] = bytes((0xe2, 6, 4))  # HDR metadata cut to two bytes
        for blob in (short_end, short_metadata):
            self.assertIsNone(vibrance_edid.supports_pq(_harness.valid_edid(blob)))

    def test_a_new_white_point_alone_counts_as_a_change(self):
        """The SDR look is built for the panel's white, so it has to be rebuilt."""
        def edid(x, y):
            blob = bytearray(edid_with_red(0.640, 0.330))
            blob[0x21], blob[0x22] = x >> 2, y >> 2
            blob[0x1A] = ((x & 3) << 2) | (y & 3)
            return _harness.valid_edid(blob)
        M.vibrance_panel.configure_for_panel((None, ("edid", "/edid/a", None), edid(303, 314)))
        self.assertTrue(M.vibrance_panel.configure_for_panel((None, ("edid", "/edid/b", None), edid(320, 336))))

    def test_configuring_takes_the_white_point_from_the_edid(self):
        blob = bytearray(edid_with_red(0.640, 0.330))
        x, y = 303, 314
        blob[0x21], blob[0x22] = x >> 2, y >> 2
        blob[0x1A] = ((x & 3) << 2) | (y & 3)
        M.vibrance_panel.configure_for_panel((None, ("edid", "/edid/x", None), _harness.valid_edid(blob)))
        self.assertEqual(M.state.sdr_white_point, (x / 1024, y / 1024))

    def test_displayid_scans_all_embedded_cta_collections(self):
        base, extension = bytearray(128), bytearray(128)
        base[126] = 1
        extension[0], extension[2] = 0x70, 14
        extension[5:19] = bytes((0x81, 0, 4, 0xe3, 6, 1, 1, 0x81, 0, 4, 0xe3, 6, 4, 1))
        self.assertIs(vibrance_edid.supports_pq(_harness.valid_edid(base + extension)), True)

    def test_without_gamescope_s_edid_path_the_kernel_s_panel_edid_is_read(self):
        """gamescope only publishes the path when its session patches the EDID."""
        standard = edid_with_red(0.640, 0.330)
        with tempfile.TemporaryDirectory() as drm:
            for name, status, edid in (("card0-DSI-1", "disconnected", b""),
                                       ("card0-HDMI-A-1", "connected", edid_with_red(0.6836, 0.3154)),
                                       ("card0-eDP-1", "connected", standard)):
                os.mkdir(os.path.join(drm, name))
                Path(drm, name, "status").write_text(status + "\n")
                Path(drm, name, "edid").write_bytes(edid)
            with patch.object(M.vibrance_host, "DRM_SYSFS", drm):
                _, key, blob = M.vibrance_panel.probe_panel((None, None))
                self.assertEqual(blob, standard)
                self.assertEqual(key[1], os.path.join(drm, "card0-eDP-1", "edid"))
                self.assertIsNone(M.vibrance_panel.probe_panel((True, None))[2])   # docked
                Path(drm, "card0-eDP-1", "status").write_text("disconnected\n")
                self.assertIsNone(M.vibrance_panel.probe_panel((None, None))[2])

    def test_panel_reads_one_edid_and_invalidates_cache_when_path_changes(self):
        with patch.object(M.state, "panel_cache_key", None), patch.object(M.state, "panel_cache_complete", False), \
                patch.object(M.vibrance_x11, "read_root_properties", side_effect=[
                    root_dump(False, "/edid/one"), root_dump(False, "/edid/one"), root_dump(False, "/edid/two")]), \
                patch.object(M.vibrance_host, "read_edid", return_value=edid_cta(4)) as reader:
            M.vibrance_panel.configure_for_panel()
            M.vibrance_panel.configure_for_panel()
            M.vibrance_panel.configure_for_panel()
            # Red coordinates in this fixture are unspecified, but the EDID is
            # valid: reading the same bytes again cannot answer that, so only
            # the path change reads again (and nothing is logged every tick).
            self.assertEqual(reader.call_count, 2)

    def test_valid_panel_cache_detects_an_internal_edid_path_change(self):
        blob = bytearray(edid_cta(4))
        red = edid_with_red(0.6836, 0.3154)
        blob[0x19:0x1d] = red[0x19:0x1d]
        blob = _harness.valid_edid(blob)
        with patch.object(M.state, "panel_cache_key", None), patch.object(M.state, "panel_cache_complete", False), \
                patch.object(M.vibrance_x11, "read_root_properties", side_effect=[
                    root_dump(False, "/edid/one"), root_dump(False, "/edid/one"), root_dump(False, "/edid/two")]), \
                patch.object(M.vibrance_host, "read_edid", return_value=blob) as reader:
            M.vibrance_panel.configure_for_panel()
            M.vibrance_panel.configure_for_panel()
            M.vibrance_panel.configure_for_panel()
            self.assertEqual(reader.call_count, 2)


class MetadataFiles(PluginTest):
    def test_regular_edid_read_remains_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "edid.bin"
            data = bytes(range(256)) * 200
            path.write_bytes(data)
            self.assertEqual(M.vibrance_host.read_edid(str(path)), data[:128 * 256 + 1])

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
 assert main.vibrance_host.read_edid(sys.argv[1]) is None
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
            self.assertEqual(M.vibrance_host.read_edid(str(alias)), target.read_bytes())


class PanelCache(PluginTest):
    def test_missing_edid_path_does_not_spawn_a_duplicate_xprop_read(self):
        with patch.object(M.vibrance_x11, "read_root_properties", return_value=root_dump(False)) as dump, \
                patch.object(M.vibrance_x11, "xprop") as xprop:
            M.vibrance_panel.probe_panel()
        dump.assert_called_once_with(M.vibrance_x11.DISPLAY)
        xprop.assert_not_called()

    def test_a_timed_out_external_flag_does_not_calibrate_on_the_monitor(self):
        """Docked at startup with a slow X server: separate reads could miss
        IS_EXTERNAL yet find the monitor's EDID path, and measure the monitor."""
        M.vibrance_panel.use_mapping(False)                     # a known standard panel
        with patch.object(M.vibrance_x11, "read_root_properties", return_value=None), \
                patch.object(M.vibrance_x11, "display_is_external", return_value=None), \
                patch.object(M.vibrance_x11, "read_string_atom", return_value="/monitor/edid"), \
                patch.object(M.vibrance_host, "read_edid", return_value=edid_with_red(0.708, 0.292)) as read:
            M.vibrance_panel.configure_for_panel()
        read.assert_not_called()
        self.assertEqual((M.state.sdr_neutral_atom, M.state.sdr_atom_span), (0.0, 1.0))

    def test_atomic_edid_replacement_at_the_same_path_changes_panel_mapping(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "panel.bin"
            path.write_bytes(edid_with_red(0.6836, 0.3154))
            with patch.object(M.state, "public", dict(M.state.public)), \
                    patch.object(M.state, "panel_cache_key", None), \
                    patch.object(M.state, "panel_cache_complete", False), \
                    patch.object(M.state, "sdr_neutral_atom", 1.0), \
                    patch.object(M.state, "sdr_atom_span", 2.0), \
                    patch.object(M.vibrance_x11, "read_root_properties", return_value=root_dump(False, str(path))), \
                    patch.object(M.vibrance_host, "read_edid", wraps=M.vibrance_host.read_edid) as read:
                M.vibrance_panel.configure_for_panel()
                M.vibrance_panel.configure_for_panel()
                self.assertEqual(read.call_count, 1, "unchanged file should stay cached")
                replacement = Path(folder) / "new.bin"
                replacement.write_bytes(edid_with_red(0.640, 0.330))
                os.replace(replacement, path)
                self.assertTrue(M.vibrance_panel.configure_for_panel())
                self.assertEqual(M.state.sdr_neutral_atom, 0)
                self.assertEqual(read.call_count, 2)


class SdrColorimetry(PluginTest):
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
        blob = bytearray(edid_with_red(0.640, 0.330))
        x, y = 303, 314
        blob[0x21], blob[0x22] = x >> 2, y >> 2
        blob[0x1A] = ((x & 3) << 2) | (y & 3)
        self.assertEqual(vibrance_edid.white_point(_harness.valid_edid(blob)), (x/1024, y/1024))
        blob[0x1B] = blob[0x19] = 0             # no colorimetry: gamescope's Rec.709 has D65
        self.assertEqual(vibrance_edid.white_point(_harness.valid_edid(blob)), V.D65)
        for invalid in (None, b"", bytes(128)):
            self.assertEqual(vibrance_edid.white_point(invalid), V.D65)


class CapabilityGating(PluginTest):
    """The two halves withdraw independently, and only on a definite no."""

    def setUp(self):
        M._pick_display = lambda: ":0"
        M.vibrance_x11.read_float_atom = lambda name: 1.0
        M.vibrance_x11.count_gamescope_atoms = lambda display: 44

        async def no_wait(_seconds):
            return None

        sleep = patch.object(asyncio, "sleep", no_wait)
        sleep.start()
        self.addCleanup(sleep.stop)
        self.addCleanup(M.settings._store.clear)  # not under StateGuard

    def _boot(self, wide, pq, hdr_on=True):
        M.vibrance_panel.panel_is_wide_gamut = lambda *_args: wide
        M.vibrance_panel.panel_supports_hdr = lambda *_args: pq
        # _main reloads these from the settings, so seeding the state is not
        # enough to get an enabled half into the startup path.
        M.settings._store.update({"sdr_enabled": False, "hdr_enabled": hdr_on})
        # A freshly loaded module: unknowns keep these startup defaults.
        M.state.public.update({"hdr_supported": True, "sdr_max": M.SDR_MAX_WIDE,
                                "sdr_soft_limit": M.vibrance_state.SDR_SOFT_WIDE})
        M.state.sdr_neutral_atom, M.state.sdr_atom_span = 1.0, 2.0
        # _main only loads settings now; the part that reads the panel waits in
        # a task, so it is driven explicitly here.
        drain(M.Plugin()._main())
        drain(M.Plugin()._start_when_ready())

    def test_a_panel_without_pq_loses_the_hdr_half_only(self):
        self._boot(wide=True, pq=False)
        self.assertFalse(M.state.public["hdr_supported"])
        self.assertFalse(M.state.public["hdr_enabled"])

    def test_a_standard_gamut_panel_gets_the_other_mapping(self):
        """A Legion Go S keeps a working SDR slider; it just runs against an
        atom whose neutral is 0.0 and which gives out after one unit."""
        self._boot(wide=False, pq=True)
        self.assertEqual(M.state.sdr_neutral_atom, 0.0)
        self.assertEqual(M.state.sdr_atom_span, 1.0)

    def test_the_look_carries_a_standard_panel_to_the_full_range(self):
        self._boot(wide=False, pq=True)
        self.assertEqual(M.state.public["sdr_max"], M.vibrance_effects.SDR_MAX_STANDARD)
        self.assertEqual(M.state.public["sdr_soft_limit"], M.vibrance_effects.SDR_SOFT_STANDARD)

    def test_a_wide_gamut_panel_keeps_its_own_numbers(self):
        """The Legion Go 2 must not pick up the standard-gamut mapping."""
        self._boot(wide=True, pq=True)
        self.assertEqual(M.state.sdr_neutral_atom, 1.0)
        self.assertEqual(M.state.sdr_atom_span, 2.0)
        self.assertEqual(M.state.public["sdr_max"], M.SDR_MAX_WIDE)
        self.assertEqual(M.state.public["sdr_soft_limit"], M.vibrance_state.SDR_SOFT_WIDE)

    def test_an_edid_we_cannot_read_withdraws_nothing(self):
        """Ignorance is not a no - a missing atom must not cost a user a
        control that works."""
        self._boot(wide=None, pq=None)
        self.assertEqual(M.state.sdr_neutral_atom, 1.0)
        self.assertTrue(M.state.public["hdr_supported"])

    def test_an_unreadable_edid_after_a_known_one_keeps_its_mapping(self):
        """A standard panel read as unknown (an EDID not yet published after a
        gamescope restart, say) must not fall to the wide mapping, where its
        neutral slider would write the harshest atom value."""
        self._boot(wide=False, pq=False)
        white = M.state.sdr_white_point
        M.vibrance_panel.panel_is_wide_gamut = lambda *_args: None
        M.vibrance_panel.panel_supports_hdr = lambda *_args: None
        changed = M.vibrance_panel.configure_for_panel((False, ("edid", "/edid/later", None), None))
        self.assertFalse(changed)
        self.assertEqual((M.state.sdr_neutral_atom, M.state.sdr_atom_span), (0.0, 1.0))
        self.assertEqual(M.state.public["sdr_max"], M.vibrance_effects.SDR_MAX_STANDARD)
        self.assertFalse(M.state.public["hdr_supported"])
        self.assertEqual(M.state.sdr_white_point, white)
        self.assertFalse(M.state.panel_cache_complete)

    def test_a_capable_panel_keeps_both(self):
        self._boot(wide=True, pq=True)
        self.assertEqual(M.state.sdr_neutral_atom, 1.0)
        self.assertEqual(M.state.public["sdr_max"], M.SDR_MAX_WIDE)
        self.assertTrue(M.state.public["hdr_supported"])

    def test_the_hdr_setter_refuses_once_the_half_is_withdrawn(self):
        """The frontend hides the section, but the callable stays reachable."""
        M.state.public.update({"hdr_supported": False, "hdr_enabled": False})
        M.state.settings_loaded = True
        state = drain(M.Plugin().set_hdr_enabled(True))
        self.assertFalse(state["hdr_enabled"])


class ExternalDisplay(PluginTest):
    """Everything here is calibrated against the built-in panel, so anything
    else is somebody else's display and gets left alone."""

    def setUp(self):
        M.state.panel_cache_key = None
        M.state.panel_cache_complete = False
        M.state.settings_loaded = True  # the setters refuse before startup has read settings

    def _configure(self, external):
        M.vibrance_x11.read_root_properties = lambda _display: root_dump(external)
        M.vibrance_panel.panel_is_wide_gamut = lambda *_args: True
        M.vibrance_panel.panel_supports_hdr = lambda *_args: True
        return M.vibrance_panel.configure_for_panel()

    def test_an_external_display_is_recognised(self):
        self._configure(external=True)
        self.assertTrue(M.state.public["external_display"])

    def test_the_internal_panel_is_not(self):
        self._configure(external=False)
        self.assertFalse(M.state.public["external_display"])

    def test_a_missing_atom_is_read_as_internal(self):
        """Older gamescope builds do not publish it, and withdrawing the whole
        plugin over an absent property would be worse than the alternative."""
        self._configure(external=None)
        self.assertFalse(M.state.public["external_display"])

    def test_docking_and_undocking_both_register_as_a_change(self):
        self._configure(external=False)
        self.assertTrue(self._configure(external=True))
        self.assertTrue(self._configure(external=False))

    def test_stable_panel_reuses_its_cached_classification(self):
        calls = []
        M.vibrance_x11.read_root_properties = lambda _display: root_dump(False)
        M.vibrance_panel.panel_is_wide_gamut = lambda *_args: calls.append("wide") or True
        M.vibrance_panel.panel_supports_hdr = lambda *_args: calls.append("hdr") or True
        M.state.panel_cache_key = None
        M.state.panel_cache_complete = False
        M.vibrance_panel.configure_for_panel()
        M.vibrance_panel.configure_for_panel()
        self.assertEqual(calls, ["wide", "hdr"])

    def test_nothing_is_measured_off_an_external_display(self):
        """Its gamut must not choose our mapping - the numbers were calibrated
        against the built-in panel."""
        self._configure(external=False)
        M.state.sdr_neutral_atom, M.state.sdr_atom_span = 1.0, 2.0
        probed = []
        M.vibrance_x11.read_root_properties = lambda _display: root_dump(True)
        M.vibrance_panel.panel_is_wide_gamut = lambda *_args: probed.append(1) or False
        M.vibrance_panel.panel_supports_hdr = lambda *_args: probed.append(1) or False
        M.vibrance_panel.configure_for_panel()
        self.assertEqual(probed, [])
        self.assertEqual((M.state.sdr_neutral_atom, M.state.sdr_atom_span), (1.0, 2.0))

    def test_neither_half_can_be_switched_on_while_docked(self):
        M.state.public.update({"external_display": True,
                                   "sdr_enabled": False, "hdr_enabled": False,
                                   "hdr_supported": True})
        self.assertFalse(
            drain(M.Plugin().set_sdr_enabled(True))["sdr_enabled"])
        self.assertFalse(
            drain(M.Plugin().set_hdr_enabled(True))["hdr_enabled"])


class StartupOnExternalDisplay(PluginTest):

    def test_enabled_settings_do_not_apply_while_already_docked(self):
        calls = []

        async def no_wait(_seconds):
            return None

        M._pick_display = lambda: ":0"
        M.vibrance_x11.count_gamescope_atoms = lambda display: 44
        M.vibrance_x11.read_float_atom = lambda atom: 1.0
        M.vibrance_x11.read_string_atom = lambda atom: None
        M.vibrance_x11.write_float_atom = lambda atom, value: (
            calls.append((atom, value)) or True)
        M.vibrance_x11.write_string_atom = lambda atom, value: (
            calls.append((atom, value)) or True)
        M.vibrance_x11.read_root_properties = lambda _display: root_dump(True)
        M.vibrance_panel.panel_is_wide_gamut = lambda *_args: self.fail(
            "external panel must not be measured")
        M.vibrance_panel.panel_supports_hdr = lambda *_args: self.fail(
            "external panel must not be measured")
        M.vibrance_effects.apply_sdr = staticmethod(lambda: calls.append("apply SDR"))
        M.Plugin._schedule_look = lambda self, component: calls.append(
            f"schedule {component.upper()}")
        M.Plugin._track = staticmethod(lambda task: task.cancel())
        M.state.mutation_lock = asyncio.Lock()
        M.state.panel_cache_key = None
        M.state.panel_cache_complete = False
        M.state.unloading = False
        M.state.public.update({
            "ready": False, "external_display": False,
            "sdr_enabled": True, "hdr_enabled": True,
            "hdr_supported": True,
        })
        with patch.object(asyncio, "sleep", no_wait):
            asyncio.run(M.Plugin()._start_when_ready())
        self.assertEqual(calls, [])


class DisplayTransitions(PluginCase):

    async def test_slow_panel_probe_does_not_block_slider_mutations(self):
        started, release = threading.Event(), threading.Event()
        def dump(_display):
            started.set()
            release.wait(10)
            return root_dump(False)
        self.mock(M.vibrance_x11, "read_root_properties", side_effect=dump)
        panel = asyncio.create_task(self.plugin._refresh_panel())
        change = None
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            change = asyncio.create_task(self.plugin.set_hdr_saturation(1.2))
            done, _ = await asyncio.wait([change], timeout=2)
            self.assertIn(change, done, "panel metadata IO monopolized the mutation lock")
        finally:
            release.set()
            await panel
            if change is not None:
                await change
            await self.drain()

    async def test_internal_metadata_refresh_preserves_the_sdr_restore_value(self):
        await self.plugin.set_sdr_enabled(True)
        self.mock(M.vibrance_panel, "configure_for_panel", return_value=True)
        await self.plugin._refresh_panel()
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)

    async def test_return_to_internal_captures_its_current_sdr_baseline(self):
        M.state.public.update(sdr_enabled=True, external_display=True)
        self.live_sdr = 0.6
        self.mock(M.vibrance_panel, "configure_for_panel", side_effect=lambda *args:
                  M.state.public.update(external_display=False) or True)
        await self.plugin._refresh_panel()
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.6)

    async def test_first_lut_build_does_not_replace_a_live_stale_slot(self):
        self.mock(M.vibrance_effects, "runtime_dir", return_value="/tmp")
        for encoding, atom, slot_key, enabled, saturation, component in (
                ("pq", M.vibrance_x11.ATOM_LOOK_PQ, "hdr_look_slot", "hdr_enabled", "hdr_saturation", "hdr"),
                ("g22", M.vibrance_x11.ATOM_LOOK_G22, "sdr_look_slot", "sdr_enabled", "sdr_saturation", "sdr")):
            M.state.sdr_neutral_atom, M.state.sdr_atom_span = 0, 1
            prefix = "hdr-look" if encoding == "pq" else "sdr-look"
            self.strings[atom] = f"/tmp/{prefix}-1.cube"
            setattr(M.state, slot_key, 0)
            M.state.public.update({enabled: True, saturation: 1.3 if encoding == "pq" else 2.6})
            self.builder.reset_mock()
            self.plugin._schedule_look(component)
            await self.drain()
            self.assertEqual(self.builder.call_args.args[1], 0, encoding)

    async def test_foreign_hdr_look_changed_during_build_is_restored(self):
        self.strings[M.vibrance_x11.ATOM_LOOK_PQ] = "/tmp/foreign-old.cube"
        def build(*args):
            self.strings[M.vibrance_x11.ATOM_LOOK_PQ] = "/tmp/foreign-new.cube"
            return "/tmp/hdr-look-1.cube"
        self.builder.side_effect = build
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3)
        self.plugin._schedule_look("hdr")
        await self.drain()
        await self.plugin.set_hdr_enabled(False)
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], "/tmp/foreign-new.cube")

    async def test_return_after_failed_dock_release_keeps_original_sdr_baseline(self):
        await self.plugin.set_sdr_enabled(True)
        self.float_writer.side_effect = None
        self.float_writer.return_value = False
        M.state.public["external_display"] = True
        M.vibrance_effects.release_sdr()
        self.float_writer.side_effect = self.write_float
        self.mock(M.vibrance_panel, "configure_for_panel", side_effect=lambda *args:
                  M.state.public.update(external_display=False) or True)
        await self.plugin._refresh_panel()
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)

    async def test_missing_sdr_read_on_return_cannot_erase_an_owned_baseline(self):
        await self.plugin.set_sdr_enabled(True)
        M.state.public["external_display"] = True
        self.mock(M.vibrance_panel, "configure_for_panel", side_effect=lambda *args:
                  M.state.public.update(external_display=False) or True)
        with patch.object(M.vibrance_x11, "read_float_atom", return_value=None):
            await self.plugin._refresh_panel()
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)

    async def test_missing_sdr_read_on_return_keeps_the_baseline_after_a_clean_dock(self):
        await self.plugin.set_sdr_enabled(True)
        dock = [True, False]
        self.mock(M.vibrance_panel, "configure_for_panel", side_effect=lambda *args:
                  M.state.public.update(external_display=dock.pop(0)) or True)
        await self.plugin._refresh_panel()          # docked: our write released
        self.assertFalse(M.state.public["sdr_applied"])
        with patch.object(M.vibrance_x11, "read_float_atom", return_value=None):
            await self.plugin._refresh_panel()      # back, but the read failed
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)

    async def test_return_does_not_adopt_a_lost_write_of_ours_as_the_baseline(self):
        def lands_but_times_out(atom, value):
            self.live_sdr = value
            return False
        self.float_writer.side_effect = lands_but_times_out
        await self.plugin.set_sdr_enabled(True, 1.4)       # baseline 0.8, 1.4 lands
        self.float_writer.side_effect = lambda atom, value: False
        dock = [True, False]
        self.mock(M.vibrance_panel, "configure_for_panel", side_effect=lambda *args:
                  M.state.public.update(external_display=dock.pop(0)) or True)
        await self.plugin._refresh_panel()                 # docked: the release fails
        self.float_writer.side_effect = self.write_float
        await self.plugin._refresh_panel()                 # back: 1.4 is still ours
        await self.drain()
        await self.plugin.set_sdr_enabled(False)
        self.assertEqual(self.live_sdr, 0.8)


class KernelEdidEdges(PluginTest):
    def test_the_kernel_fallback_takes_a_dsi_panel_on_any_card_and_only_when_connected(self):
        """ARM handhelds drive their panel over DSI, often not on card0. A
        connector whose status cannot be read, or reads "unknown", is skipped."""
        panel = edid_with_red(0.640, 0.330)
        with tempfile.TemporaryDirectory() as drm:
            for name, status, edid in (("card0-eDP-1", None, b""),            # no status file
                                       ("card0-eDP-2", "unknown", b"x"),
                                       ("card1-DSI-1", "connected", panel)):
                os.mkdir(os.path.join(drm, name))
                if status is not None:
                    Path(drm, name, "status").write_text(status + "\n")
                Path(drm, name, "edid").write_bytes(edid)
            with patch.object(M.vibrance_host, "DRM_SYSFS", drm):
                self.assertEqual(M.vibrance_host.builtin_panel_edid_path(),
                                 os.path.join(drm, "card1-DSI-1", "edid"))
                self.assertEqual(M.vibrance_panel.probe_panel((None, None))[2], panel)

    def test_an_edid_that_failed_to_read_is_read_again_at_the_same_identity(self):
        """A transient read failure leaves the cache incomplete; the next probe
        of the same file must read it rather than keep the startup guess."""
        standard = edid_with_red(0.640, 0.330)
        with patch.object(M.state, "panel_cache_key", None), \
                patch.object(M.state, "panel_cache_complete", False), \
                patch.object(M.vibrance_host, "file_identity", return_value=(1, 2, 128, 3, 4)), \
                patch.object(M.vibrance_host, "read_edid", side_effect=[None, standard]) as reader:
            M.vibrance_panel.configure_for_panel(M.vibrance_panel.probe_panel((False, "/edid")))
            self.assertEqual(M.state.sdr_neutral_atom, 1.0)               # nothing learned yet
            M.vibrance_panel.configure_for_panel(M.vibrance_panel.probe_panel((False, "/edid")))
            self.assertEqual(reader.call_count, 2)
            self.assertEqual((M.state.sdr_neutral_atom, M.state.sdr_atom_span), (0.0, 1.0))

    def test_white_needs_red_x_only_nonzero_and_a_usable_white(self):
        def edid(red_low_bits, white):
            blob = bytearray(128)
            blob[0x19] = red_low_bits << 6                  # red x = 1..3 / 1024
            x, y = white
            blob[0x21], blob[0x22] = x >> 2, y >> 2
            blob[0x1A] = ((x & 3) << 2) | (y & 3)
            return _harness.valid_edid(blob)
        # gamescope takes colorimetry for any red x other than zero, even 1/1024.
        self.assertEqual(vibrance_edid.white_point(edid(1, (303, 314))), (303 / 1024, 314 / 1024))
        # A white the LUT maths would refuse falls back to D65 instead of failing every build.
        for white in ((0, 314), (303, 0)):
            point = vibrance_edid.white_point(edid(1, white))
            self.assertEqual(point, V.D65, white)
            V.validate(1.5, 33, "g22", point)


class RedThreshold(PluginTest):
    def test_the_low_bits_of_red_x_count_at_the_threshold(self):
        # 666/1024 = 0.6504 passes gamescope's red.x > 0.650; 664/1024 would not.
        self.assertIs(M.vibrance_panel.panel_is_wide_gamut(edid_with_red(666 / 1024, 0.3154)), True)
        self.assertIs(M.vibrance_panel.panel_is_wide_gamut(edid_with_red(665 / 1024, 0.3154)), False)


class KernelEdidHdr(PluginCase):
    async def test_an_unpatched_kernel_edid_keeps_a_saved_hdr_toggle(self):
        oled = edid_with_red(0.6836, 0.3154)       # base block only, like a raw panel EDID
        with tempfile.TemporaryDirectory() as drm:
            os.mkdir(os.path.join(drm, "card0-eDP-1"))
            Path(drm, "card0-eDP-1", "status").write_text("connected\n")
            Path(drm, "card0-eDP-1", "edid").write_bytes(oled)
            self.mock(M.vibrance_host, "DRM_SYSFS", drm)
            self.mock(M.vibrance_x11, "session_identity", return_value=(":0", (1, 1)))
            self.mock(M, "_pick_display", return_value=":0")
            self.mock(M.vibrance_x11, "count_gamescope_atoms", return_value=44)
            self.mock(M, "GAMESCOPE_SETTLE_S", 0)
            self.mock(M.vibrance_x11, "read_root_properties", return_value=root_dump(False))
            self.mock(M.vibrance_effects, "capture_existing_looks")
            self.mock(self.plugin, "_reassert_repeatedly", return_value=None)
            M.state.panel_cache_key = None
            M.state.panel_cache_complete = False
            M.state.public.update(hdr_enabled=True, hdr_supported=True)
            await self.plugin._start_when_ready(start_monitor=False)
            await self.drain()
        # Without the fallback (blob None) both stayed True.
        self.assertTrue(M.state.public["hdr_supported"], "HDR half hidden")
        self.assertTrue(M.state.public["hdr_enabled"], "saved HDR toggle switched off")


def kernel_panel(drm, name, edid):
    os.mkdir(os.path.join(drm, name))
    Path(drm, name, "status").write_text("connected\n")
    Path(drm, name, "edid").write_bytes(edid)


class KernelEdidFallback(PluginTest):
    """Kills CP6, H10, CP5."""

    def test_the_kernel_fallback_runs_when_gamescope_says_internal(self):
        """IS_EXTERNAL = 0 and no EDID path is the usual unpatched session."""
        panel = edid_with_red(0.640, 0.330)
        with tempfile.TemporaryDirectory() as drm, patch.object(M.vibrance_host, "DRM_SYSFS", drm):
            kernel_panel(drm, "card0-eDP-1", panel)
            self.assertEqual(M.vibrance_panel.probe_panel((False, None))[2], panel)

    def test_the_kernel_fallback_takes_an_lvds_panel(self):
        """gamescope counts LVDS as internal too (DRMBackend.cpp GetScreenType)."""
        panel = edid_with_red(0.640, 0.330)
        with tempfile.TemporaryDirectory() as drm, patch.object(M.vibrance_host, "DRM_SYSFS", drm):
            kernel_panel(drm, "card0-LVDS-1", panel)
            self.assertEqual(M.vibrance_host.builtin_panel_edid_path(),
                             os.path.join(drm, "card0-LVDS-1", "edid"))

    def test_a_valid_kernel_edid_without_hdr_is_read_once(self):
        """Its HDR answer is unknown, but re-reading the same bytes cannot change it."""
        with tempfile.TemporaryDirectory() as drm, patch.object(M.vibrance_host, "DRM_SYSFS", drm), \
                patch.object(M.state, "panel_cache_key", None), \
                patch.object(M.state, "panel_cache_complete", False):
            kernel_panel(drm, "card0-eDP-1", edid_with_red(0.640, 0.330))
            with patch.object(M.vibrance_host, "read_edid", wraps=M.vibrance_host.read_edid) as reader:
                for _ in range(3):
                    M.vibrance_panel.configure_for_panel(M.vibrance_panel.probe_panel((False, None)))
            self.assertEqual(reader.call_count, 1)


class KernelHdrAnswer(PluginTest):
    """test_panel.py, next to KernelEdidHdr. Kills CP2."""

    def test_a_kernel_edid_that_advertises_pq_is_believed(self):
        M.state.public["hdr_supported"] = False
        with tempfile.TemporaryDirectory() as drm, patch.object(M.vibrance_host, "DRM_SYSFS", drm), \
                patch.object(M.state, "panel_cache_key", None), \
                patch.object(M.state, "panel_cache_complete", False):
            kernel_panel(drm, "card0-eDP-1", edid_cta(4))
            M.vibrance_panel.configure_for_panel(M.vibrance_panel.probe_panel((False, None)))
        self.assertTrue(M.state.public["hdr_supported"])


class PanelShapeChanges(PluginTest):
    """Kills PS2, CP4."""

    def setUp(self):
        M.state.panel_cache_key = None
        M.state.panel_cache_complete = False
        M.vibrance_panel.use_mapping(True)
        M.state.sdr_white_point = V.D65

    def test_hdr_support_changing_alone_is_a_panel_change(self):
        """Startup could not read the EDID; a later read says no PQ on the same
        gamut. _refresh_panel only re-applies (releasing the HDR look) on change."""
        M.state.public["hdr_supported"] = True
        wide_sdr_only = edid_with_red(0.6836, 0.3154)       # base block only: no PQ
        self.assertTrue(M.vibrance_panel.configure_for_panel(
            (False, ("external", False, "/edid", (1,)), wide_sdr_only)))
        self.assertFalse(M.state.public["hdr_supported"])

    def test_an_unreadable_edid_keeps_the_known_white(self):
        M.state.sdr_white_point = (0.30, 0.32)
        M.vibrance_panel.configure_for_panel((False, ("external", False, "/edid", (1,)), b"\x00" * 128))
        self.assertEqual(M.state.sdr_white_point, (0.30, 0.32))


def _with_extension(block):
    base = bytearray(128)
    base[126] = 1
    return _harness.valid_edid(base + bytes(block))


def _displayid(*blocks, payload=None):
    ext = bytearray(128)
    ext[0] = 0x70
    body = b"".join(blocks)
    ext[2] = len(body) if payload is None else payload
    ext[5:5 + len(body)] = body
    return _with_extension(ext)


def _cta(*data_blocks):
    ext = bytearray(128)
    ext[:2] = b"\x02\x03"
    body = b"".join(data_blocks)
    ext[4:4 + len(body)] = body
    ext[2] = 4 + len(body)
    return _with_extension(ext)


HDR_PQ = bytes((0xe3, 0x06, 0x05, 0x01))          # extended tag 6, SDR + ST 2084


class HdrCapabilityEdges(PluginTest):
    def test_a_displayid_cta_block_without_pq_is_a_no(self):
        self.assertIs(vibrance_edid.supports_pq(
            _displayid(bytes((0x81, 0, 4, 0xe3, 0x06, 0x01, 0x01)))), False)

    def test_only_displayid_cta_blocks_are_read_as_cta(self):
        self.assertIs(vibrance_edid.supports_pq(
            _displayid(bytes((0x22, 0, 4)) + HDR_PQ)), False)

    def test_a_displayid_payload_or_block_past_the_extension_is_not_an_answer(self):
        self.assertIsNone(vibrance_edid.supports_pq(_displayid(payload=123)))
        self.assertIsNone(vibrance_edid.supports_pq(
            _displayid(bytes((0x81, 0, 9)) + HDR_PQ, payload=7)))

    def test_a_payload_that_fills_the_extension_is_still_read(self):
        self.assertIs(vibrance_edid.supports_pq(_displayid(payload=122)), False)

    def test_an_unknown_extension_is_skipped(self):
        block = bytearray(128)
        block[0] = 0xF0                                  # a block map, say
        self.assertIs(vibrance_edid.supports_pq(_with_extension(block)), False)

    def test_only_the_hdr_static_metadata_block_counts(self):
        colorimetry = bytes((0xe3, 0x05, 0x04, 0x00))    # extended tag 5, sYCC601
        vendor = bytes((0x63, 0x06, 0x04, 0x00))         # tag 3 whose payload starts 06
        self.assertIs(vibrance_edid.supports_pq(_cta(colorimetry)), False)
        self.assertIs(vibrance_edid.supports_pq(_cta(vendor)), False)
        self.assertIs(vibrance_edid.supports_pq(_cta(colorimetry, HDR_PQ)), True)

    def test_a_short_blob_with_a_valid_header_is_not_valid(self):
        self.assertFalse(vibrance_edid.valid(b"\x00\xff\xff\xff\xff\xff\xff\x00" + bytes(60)))


class MissingEdid(PluginTest):
    def test_a_missing_edid_file_reads_as_nothing(self):
        self.assertIsNone(M.vibrance_host.read_edid(os.path.join(tempfile.gettempdir(), "no-such-edid-a11")))


class PanelTransitionEdges(PluginCase):
    async def test_docking_gives_the_hdr_look_back(self):
        M.state.public.update(hdr_enabled=True, hdr_saturation=1.3)
        self.plugin._schedule_look("hdr")
        await self.drain()
        self.assertTrue(self.strings[M.vibrance_x11.ATOM_LOOK_PQ])
        self.mock(M.vibrance_panel, "configure_for_panel", side_effect=lambda *args:
                  M.state.public.update(external_display=True) or True)
        await self.plugin._refresh_panel()
        await self.drain()
        self.assertEqual(self.strings[M.vibrance_x11.ATOM_LOOK_PQ], "")

    async def test_an_unchanged_panel_writes_nothing(self):
        M.state.public.update(sdr_enabled=True, sdr_saturation=1.5, hdr_enabled=True, hdr_saturation=1.3)
        M.state.panel_cache_key, M.state.panel_cache_complete = None, False
        probe = (False, ("external", False, "/edid", (1, 2)), edid_with_red(0.68, 0.31))
        await self.plugin._refresh_panel(probe)
        await self.drain()
        floats, strings = self.float_writer.call_count, self.string_writer.call_count
        await self.plugin._refresh_panel(probe)
        await self.drain()
        self.assertEqual((self.float_writer.call_count, self.string_writer.call_count), (floats, strings))


class HdrSwitchedOffForAPanelWithoutPq(PluginCase):
    async def test_the_switched_off_hdr_half_is_saved(self):
        M.state.public.update(hdr_enabled=True, hdr_supported=False)
        self.mock(M.vibrance_x11, "session_identity", return_value=None)
        self.mock(M, "_pick_display", return_value=":0")
        self.mock(M.vibrance_x11, "count_gamescope_atoms", return_value=44)
        self.mock(M, "GAMESCOPE_SETTLE_S", 0)
        self.mock(M.vibrance_panel, "configure_for_panel", return_value=True)
        self.mock(M.vibrance_effects, "capture_existing_looks")
        self.mock(self.plugin, "_reassert_repeatedly", return_value=None)
        await self.plugin._start_when_ready(start_monitor=False)
        await self.drain()
        self.assertFalse(M.state.public["hdr_enabled"])
        M.Plugin._save.assert_called()
        self.assertFalse(M.Plugin._save.call_args.args[0]["hdr_enabled"])


class MappingSwitchesBack(PluginTest):
    """Every test starts on the wide mapping (reset()), so nothing checks that a
    standard panel followed by a wide one gets the wide numbers back."""

    def test_standard_then_wide_restores_the_wide_mapping(self):
        M.vibrance_panel.use_mapping(False)
        M.vibrance_panel.use_mapping(True)
        fresh = M.vibrance_state.PluginState()
        self.assertEqual((M.state.sdr_neutral_atom, M.state.sdr_atom_span),
                         (fresh.sdr_neutral_atom, fresh.sdr_atom_span))
        for key in ("sdr_max", "sdr_soft_limit"):
            self.assertEqual(M.state.public[key], fresh.public[key])
