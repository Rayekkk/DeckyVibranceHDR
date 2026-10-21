# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""What the built-in panel's EDID says, as pure functions over its bytes.

Nothing here reads files, talks to gamescope or logs, so every rule can be
tested with a synthetic EDID. main.py decides what to do with the answers.

The `vibrance_` prefix keeps clear of the bare names Decky aliases its own
modules to before a plugin is imported (see lego_updater.py).
"""

from vibrance_lut import D65

CTA_EXTENSION_TAG = 0x02
DISPLAYID_EXTENSION_TAG = 0x70
DISPLAYID_CTA_DATA_BLOCK = 0x81
CTA_EXTENDED_TAG = 0x07
CTA_HDR_STATIC_METADATA = 0x06
EOTF_PQ_BIT = 1 << 2
# gamescope's displaycolorimetry_709, used when an EDID has no chromaticity.
REC709_RED = (0.640, 0.330)


def valid(blob) -> bool:
    """A complete EDID: header, declared extension count and every checksum."""
    if not blob or len(blob) < 128 or blob[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return False
    size = 128 * (1 + blob[126])
    return len(blob) == size and all(sum(blob[i:i + 128]) % 256 == 0
                                    for i in range(0, size, 128))


def red_primary(blob: bytes):
    """Red x,y as gamescope uses it from the EDID, or None when unreadable.

    Byte 0x19 carries the low two bits of each of the first four coordinates in
    its own pairs of bits; 0x1B and 0x1C carry the high eight of red x and y.
    A red x of zero means the EDID carries no colorimetry, and gamescope then
    assumes Rec.709 (DRMBackend.cpp), so that is what the wide-gamut test sees.
    """
    if not valid(blob):
        return None
    low = blob[0x19]
    red_x = ((blob[0x1B] << 2) | (low >> 6)) / 1024.0
    red_y = ((blob[0x1C] << 2) | ((low >> 4) & 0x03)) / 1024.0
    if red_x == 0:
        return REC709_RED
    return red_x, red_y  # gamescope takes any other value as it is


def white_point(blob):
    """Native white used by gamescope's SDR source, with a D65 fallback.

    Without colorimetry (red x of zero) gamescope uses Rec.709, white and all.
    """
    if not valid(blob) or not (blob[0x1B] or blob[0x19] >> 6):  # red x is zero
        return D65
    low = blob[0x1A]
    x = ((blob[0x21] << 2) | ((low >> 2) & 3)) / 1024.0
    y = ((blob[0x22] << 2) | (low & 3)) / 1024.0
    if x <= 0.0 or y <= 0.0 or x + y >= 1.0:
        return D65
    return x, y


def _cta_collection_pq(block: bytes, start: int, end: int):
    """Whether a run of CTA-861 data blocks advertises PQ."""
    pos = start
    while pos < end:
        length = block[pos] & 0x1F
        if length == 0:
            break
        if pos + 1 + length > end:
            raise ValueError("truncated CTA data block")
        if (block[pos] >> 5) == CTA_EXTENDED_TAG and block[pos + 1] == CTA_HDR_STATIC_METADATA:
            if length < 3:
                raise ValueError("truncated HDR static metadata")
            if block[pos + 2] & EOTF_PQ_BIT:
                return True
        pos += 1 + length
    return False


def _displayid_pq(block: bytes):
    """CTA-861 data blocks can also be carried inside a DisplayID extension.

    The Legion Go S does exactly that - one DisplayID block and no standalone
    CTA extension at all - so a parser that only looks for tag 0x02 finds
    nothing and cannot tell "no HDR" from "no idea".
    """
    payload_length = block[2]
    pos, end = 5, 5 + payload_length
    if end > 127:
        raise ValueError("truncated DisplayID payload")
    while pos + 2 < end:
        tag, body_length = block[pos], block[pos + 2]
        if tag == 0 and body_length == 0:
            break
        body = pos + 3
        if body + body_length > end:
            raise ValueError("truncated DisplayID data block")
        if tag == DISPLAYID_CTA_DATA_BLOCK and _cta_collection_pq(
                block, body, min(body + body_length, end)):
            return True
        pos = body + body_length
    return False


def supports_pq(blob: bytes):
    """True if the display advertises SMPTE ST 2084 anywhere in its EDID.

    Read from the EDID rather than from GAMESCOPE_HDR_OUTPUT_FEEDBACK on
    purpose: that atom meant "HDR is on" and now means "HDR is possible"
    depending on the gamescope build, and reading it the wrong way round is what
    broke a sibling plugin on Bazzite 44. What the panel claims to support does
    not move under us.

    None only when the EDID itself cannot be trusted - incomplete, failing a
    checksum or malformed inside an extension. A complete EDID that never
    mentions PQ is a definite no for that EDID. gamescope's patched copy is
    where a display script's HDR gets added; the kernel's own EDID of such a
    panel never says it, which the caller allows for. gamescope itself also
    wants BT.2020 colorimetry before it turns HDR on from an EDID; that is not
    required here: display scripts can force HDR on whatever the EDID says
    (Legion Go 2, ROG Ally X), and a visible control that does nothing is the
    safer mistake.
    """
    if not valid(blob):
        return None
    for index in range(1, blob[126] + 1):
        block = blob[128 * index:128 * (index + 1)]
        found = False
        try:
            if block[0] == CTA_EXTENSION_TAG:
                # CTA-861 says d=0 means no data blocks, but a panel that lists
                # them anyway is believed: a false yes only shows an inert HDR
                # control, a false no hides a working one and saves it off.
                end = block[2] or 127
                if not 4 <= end <= 127:
                    return None
                found = _cta_collection_pq(block, 4, end)
            elif block[0] == DISPLAYID_EXTENSION_TAG:
                found = _displayid_pq(block)
        except ValueError:
            return None
        if found:
            return True
    return False
