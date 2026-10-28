# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""CTA-861 data blocks read the way the kernel reads them."""

from _fixtures import PluginTest  # installs the decky stand-in first
import _fixtures
import vibrance_edid

HDR_PQ = bytes((0xe3, 0x06, 0x05, 0x01))          # extended tag 6, SDR + ST 2084


def edid(*extensions, revision=3):
    """A base block plus one CTA extension per run of data blocks."""
    blob = bytearray(128)
    blob[126] = len(extensions)
    for body in extensions:
        ext = bytearray(128)
        ext[:3] = bytes((0x02, revision, 4 + len(body)))
        ext[4:4 + len(body)] = body
        blob += ext
    return _fixtures.valid_edid(blob)


class ShortHdrMetadata(PluginTest):
    def test_a_short_hdr_block_is_skipped_not_fatal(self):
        for short in (bytes((0xe2, 0x06, 0x04)), bytes((0xe1, 0x06))):
            with self.subTest(short=short.hex()):
                self.assertIs(vibrance_edid.supports_pq(edid(short)), False)
                self.assertIs(vibrance_edid.supports_pq(edid(short + HDR_PQ)), True)

    def test_a_three_byte_hdr_block_is_still_read(self):
        self.assertIs(vibrance_edid.supports_pq(edid(bytes((0xe3, 0x06, 0x04, 0x00)))), True)


class CtaRevision(PluginTest):
    def test_revisions_before_3_carry_no_data_blocks(self):
        for revision in (1, 2):
            with self.subTest(revision=revision):
                self.assertIs(vibrance_edid.supports_pq(edid(HDR_PQ, revision=revision)), False)
        self.assertIs(vibrance_edid.supports_pq(edid(HDR_PQ, revision=3)), True)

    def test_an_old_revision_is_skipped_whatever_its_offsets_say(self):
        blob = bytearray(edid(HDR_PQ, revision=1))
        blob[128 + 2] = 2                                # out of range for a data block collection
        self.assertIs(vibrance_edid.supports_pq(_fixtures.valid_edid(blob)), False)

    def test_an_old_revision_does_not_hide_a_later_extension(self):
        old = bytearray(edid(HDR_PQ, HDR_PQ))
        old[128 + 1] = 1
        self.assertIs(vibrance_edid.supports_pq(_fixtures.valid_edid(old)), True)
        old[256 + 1] = 1
        self.assertIs(vibrance_edid.supports_pq(_fixtures.valid_edid(old)), False)
