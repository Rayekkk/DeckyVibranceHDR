# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Generates the .cube LUTs gamescope applies as a "look".

Where this sits in gamescope's pipeline matters, because it decides what the
numbers in the file actually mean. In color_helpers.cpp, calcColorTransform()
does:

    sourceColorEOTFEncoded = ApplyLut3D_Tetrahedral( *pLook, sourceColorEOTFEncoded )
    sourceColorLinear      = calcEOTFToLinear( sourceColorEOTFEncoded, sourceEOTF, ... )

So the look is applied to values that are still *encoded*, before linearisation
and before any tonemapping or gamut mapping. Which transfer they are encoded in
depends on the path: PQ for HDR content, gamma 2.2 for SDR. Both are supported
here; the encoding argument selects the transfer and source gamut.

That means a saturation LUT cannot just scale distance from grey in place: both
transfers are non-linear, so doing that shifts luminance and hue - badly and
visibly, in exactly the bright saturated areas the adjustment exists for. Each
entry therefore decodes to linear, changes saturation in a space built for it,
and re-encodes.

The space is ICtCp (ITU-R BT.2100). Scaling Ct and Cp preserves I and chroma
direction before clipping; it does not guarantee unchanged perceived lightness
or hue once channels hit the gamut boundary. The PQ inside ICtCp is part of
ICtCp and stays put whichever transfer the look itself sits behind.

The G22 look also converts gamescope's generic wide-gamut source primaries to
BT.2020 before ICtCp, then back before clipping. Its native white is adapted to
D65 so a neutral input remains neutral on panels with a different white point.

Black maps to black by construction, which keeps gamescope's
bRaisesBlackLevelFloor false (it drives cv_overlay_unmultiplied_alpha).
"""

from functools import lru_cache
from collections import OrderedDict
import math
import threading

# ── PQ (SMPTE ST 2084) ─────────────────────────────────────────────────────────
# Absolute, with 1.0 encoded meaning 10000 nits.

_M1 = 2610.0 / 16384.0
_M2 = 2523.0 / 4096.0 * 128.0
_C1 = 3424.0 / 4096.0
_C2 = 2413.0 / 4096.0 * 32.0
_C3 = 2392.0 / 4096.0 * 32.0


def pq_to_linear(e: float) -> float:
    """PQ-encoded 0..1 -> linear, 1.0 = 10000 nits."""
    if e <= 0.0:
        return 0.0
    ep = e ** (1.0 / _M2)
    num = max(ep - _C1, 0.0)
    den = _C2 - _C3 * ep
    if den <= 0.0:
        return 1.0
    return (num / den) ** (1.0 / _M1)


# 1e-10 of 10000 nits is a millionth of a nit - below anything a display can do,
# and below the LUT's own quantisation. Snapping it keeps values that should be
# black exactly black: PQ is so steep near zero that a 1e-14 rounding crumb left
# by the round trip would otherwise encode as 4e-6 instead of 0.
_LINEAR_EPSILON = 1e-10


def linear_to_pq(y: float) -> float:
    """Linear (1.0 = 10000 nits) -> PQ-encoded 0..1."""
    if y <= _LINEAR_EPSILON:
        return 0.0
    ym = y ** _M1
    return ((_C1 + _C2 * ym) / (1.0 + _C3 * ym)) ** _M2


# ── Gamma 2.2, for the SDR look ────────────────────────────────────────────────
# The same look mechanism exists on the SDR path as GAMESCOPE_COLOR_LOOK_G22, and
# it sits in the same place in the pipeline - but there sourceEOTF is Gamma22, so
# the domain is gamma-encoded and *relative*, not absolute like PQ.
#
# ICtCp is defined against absolute luminance, so relative values have to be
# pinned somewhere on the PQ curve before the chroma scaling means anything.
# 203 nits is BT.2408 diffuse white, and also gamescope's own default for how
# bright SDR content sits on an HDR output - so it is where SDR white already is
# as far as the rest of the pipeline is concerned.

SDR_REFERENCE_NITS = 203.0
_GAMMA = 2.2


def g22_to_linear(e: float) -> float:
    """Gamma 2.2 encoded 0..1 -> linear on the same scale PQ uses."""
    if e <= 0.0:
        return 0.0
    return (e ** _GAMMA) * (SDR_REFERENCE_NITS / 10000.0)


def linear_to_g22(y: float) -> float:
    """Linear (1.0 = 10000 nits) -> gamma 2.2 encoded 0..1."""
    if y <= 0.0:
        return 0.0
    return (y * (10000.0 / SDR_REFERENCE_NITS)) ** (1.0 / _GAMMA)


# ── ICtCp (ITU-R BT.2100) ──────────────────────────────────────────────────────
# BT.2020 linear RGB -> LMS, then PQ on LMS, then the ICtCp matrix.

_RGB_TO_LMS = (
    (1688.0 / 4096.0, 2146.0 / 4096.0,  262.0 / 4096.0),
    ( 683.0 / 4096.0, 2951.0 / 4096.0,  462.0 / 4096.0),
    (  99.0 / 4096.0,  309.0 / 4096.0, 3688.0 / 4096.0),
)

_LMS_TO_ICTCP = (
    (0.5,              0.5,               0.0            ),
    (6610.0 / 4096.0, -13613.0 / 4096.0,  7003.0 / 4096.0),
    (17933.0 / 4096.0, -17390.0 / 4096.0, -543.0 / 4096.0),
)


def _invert3(m):
    (a, b, c), (d, e, f), (g, h, i) = m
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if det == 0.0:
        raise ValueError("singular matrix")
    return (
        ((e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det),
        ((f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det),
        ((d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det),
    )


_LMS_TO_RGB = _invert3(_RGB_TO_LMS)
_ICTCP_TO_LMS = _invert3(_LMS_TO_ICTCP)


def _mul3(m, v):
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


D65 = (0.3127, 0.3290)
# gamescope color_helpers.h: displaycolorimetry_widegamutgeneric. This is the
# G22 source once the standard-gamut SDR atom has reached its ceiling of 1.0.
G22_PRIMARIES = ((0.6825, 0.3165), (0.241, 0.719), (0.138, 0.050))
BT2020_PRIMARIES = ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046))
_BRADFORD = ((0.8951, 0.2664, -0.1614),
             (-0.7502, 1.7135, 0.0367),
             (0.0389, -0.0685, 1.0296))


def _matmul(a, b):
    return tuple(tuple(sum(x * y for x, y in zip(row, col))
                       for col in zip(*b)) for row in a)


def _white_xyz(white):
    x, y = white
    if not (math.isfinite(x) and math.isfinite(y)
            and x > 0.0 and y > 0.0 and x + y <= 1.0):
        raise ValueError("invalid source white point")
    return (x / y, 1.0, (1.0 - x - y) / y)


def _rgb_to_xyz(primaries, white):
    unscaled = tuple(zip(*(_white_xyz(p) for p in primaries)))
    scale = _mul3(_invert3(unscaled), _white_xyz(white))
    return tuple(tuple(row[j] * scale[j] for j in range(3)) for row in unscaled)


@lru_cache(maxsize=16)
def _g22_matrices(source_white):
    source_lms = _mul3(_BRADFORD, _white_xyz(source_white))
    dest_lms = _mul3(_BRADFORD, _white_xyz(D65))
    scaled = tuple(tuple(row[j] * dest_lms[i] / source_lms[i]
                         for j in range(3)) for i, row in enumerate(_BRADFORD))
    adaptation = _matmul(_invert3(_BRADFORD), scaled)
    to_2020 = _matmul(_invert3(_rgb_to_xyz(BT2020_PRIMARIES, D65)),
                      _matmul(adaptation, _rgb_to_xyz(G22_PRIMARIES, source_white)))
    return to_2020, _invert3(to_2020)


# Which transfer the encoded values arriving at the look are using. Only the
# outer pair changes; the PQ inside ICtCp is part of ICtCp and stays put.
TRANSFERS = {
    "pq": (pq_to_linear, linear_to_pq),
    "g22": (g22_to_linear, linear_to_g22),
}


def saturate(rgb_encoded, saturation: float, encoding: str = "pq",
             source_white=D65):
    """One LUT entry, in and out in whichever transfer the look sits behind."""
    if not isinstance(saturation, (int, float)) or isinstance(saturation, bool) \
            or not math.isfinite(saturation) or saturation < 0:
        raise ValueError("saturation must be a finite nonnegative number")
    if encoding not in TRANSFERS:
        raise ValueError(f"unknown encoding {encoding!r}")
    if saturation == 1.0:
        return rgb_encoded
    return _from_ictcp(_to_ictcp(rgb_encoded, encoding, source_white),
                       saturation, encoding, source_white)


def _to_ictcp(rgb_encoded, encoding, source_white):
    decode, _encode = TRANSFERS[encoding]
    linear = tuple(decode(c) for c in rgb_encoded)
    to_2020 = _g22_matrices(tuple(source_white))[0] if encoding == "g22" else None
    return _linear_to_ictcp(linear, to_2020)


def _linear_to_ictcp(linear, to_2020):
    if to_2020 is not None:
        linear = _mul3(to_2020, linear)
    lms = _mul3(_RGB_TO_LMS, linear)
    # The encoder already maps negative LMS (out-of-gamut input) to black.
    lms_pq = tuple(linear_to_pq(c) for c in lms)

    i, ct, cp = _mul3(_LMS_TO_ICTCP, lms_pq)
    return i, ct, cp


def _output_transform(encoding, source_white):
    decode, encode = TRANSFERS[encoding]
    from_2020 = _g22_matrices(tuple(source_white))[1] if encoding == "g22" else None
    return encode, decode(1.0), from_2020


def _from_ictcp(value, saturation, encoding, source_white, transform=None):
    encode, ceiling, from_2020 = transform or _output_transform(encoding, source_white)
    i, ct, cp = value
    ct *= saturation
    cp *= saturation

    lms_pq = _mul3(_ICTCP_TO_LMS, (i, ct, cp))
    lms = tuple(pq_to_linear(c) for c in lms_pq)
    linear = _mul3(_LMS_TO_RGB, lms)
    if from_2020 is not None:
        linear = _mul3(from_2020, linear)

    # Clamp after the round trip: raising saturation pushes colours outside the
    # cube, and gamescope's tetrahedral interpolation expects 0..1. The ceiling
    # is in linear terms, so it is wherever this transfer puts encoded 1.0.
    # Both encoders already map nonpositive linear values to black. Keep the
    # upper clamp here; duplicating the lower clamp costs three calls per node.
    return tuple(encode(min(c, ceiling)) for c in linear)


class BuildCancelled(Exception):
    """A newer request superseded this build; no file should be published."""


_grids = OrderedDict()
_grid_lock = threading.Lock()


def _prepared_grid(size, encoding, source_white, cancelled):
    key = (size, encoding, tuple(source_white))
    with _grid_lock:
        if key in _grids:
            _grids.move_to_end(key)
            return _grids[key]
    step = 1.0 / (size - 1)
    decoded = [TRANSFERS[encoding][0](c * step) for c in range(size)]
    to_2020 = _g22_matrices(tuple(source_white))[0] if encoding == "g22" else None
    nodes = []
    for b in range(size):
        if cancelled():
            raise BuildCancelled()
        for g in range(size):
            for r in range(size):
                nodes.append(_linear_to_ictcp((decoded[r], decoded[g], decoded[b]), to_2020))
    # Only production-size grids are retained. Two cover PQ and the current G22
    # white point, with bounded memory even when panels or sizes change.
    if size == 33:
        with _grid_lock:
            _grids[key] = nodes
            _grids.move_to_end(key)
            while len(_grids) > 2:
                _grids.popitem(last=False)
    return nodes


def generate(saturation: float, size: int = 33, encoding: str = "pq",
             source_white=D65, *, cancelled=lambda: False) -> str:
    """A complete .cube file as text. R changes fastest, as gamescope expects."""
    if not isinstance(size, int) or isinstance(size, bool) or not 2 <= size <= 128:
        raise ValueError("gamescope accepts LUT_3D_SIZE between 2 and 128")
    if encoding not in TRANSFERS:
        raise ValueError(f"unknown encoding {encoding!r}")
    saturate((0, 0, 0), saturation, encoding, source_white)
    if encoding == "g22":
        _white_xyz(source_white)
    grid = None if saturation == 1 else _prepared_grid(size, encoding, source_white, cancelled)
    transform = _output_transform(encoding, source_white)

    step = 1.0 / (size - 1)
    out = [
        "# Generated by DeckyVibranceHDR",
        f"# saturation {saturation:.3f} in ICtCp, {encoding} domain",
        f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
        "",
    ]
    for b in range(size):
        if cancelled():
            raise BuildCancelled()
        for g in range(size):
            for r in range(size):
                v = ((r * step, g * step, b * step) if grid is None else
                     _from_ictcp(grid[b * size * size + g * size + r],
                                 saturation, encoding, source_white, transform))
                out.append("%.6f %.6f %.6f" % v)
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(newline="\n")
    if len(sys.argv) != 6:
        raise SystemExit("expected saturation, size, encoding, white x, white y")
    sys.stdout.write(generate(float(sys.argv[1]), int(sys.argv[2]), sys.argv[3],
                              (float(sys.argv[4]), float(sys.argv[5]))))
