"""Generates the .cube LUTs gamescope applies as a "look".

Where this sits in gamescope's pipeline matters, because it decides what the
numbers in the file actually mean. In color_helpers.cpp, calcColorTransform()
does:

    sourceColorEOTFEncoded = ApplyLut3D_Tetrahedral( *pLook, sourceColorEOTFEncoded )
    sourceColorLinear      = calcEOTFToLinear( sourceColorEOTFEncoded, sourceEOTF, ... )

So the look is applied to values that are still *encoded*, before linearisation
and before any tonemapping or gamut mapping. Which transfer they are encoded in
depends on the path: PQ for HDR content, gamma 2.2 for SDR. Both are supported
here; the encoding argument picks between them and nothing else changes.

That means a saturation LUT cannot just scale distance from grey in place: both
transfers are non-linear, so doing that shifts luminance and hue - badly and
visibly, in exactly the bright saturated areas the adjustment exists for. Each
entry therefore decodes to linear, changes saturation in a space built for it,
and re-encodes.

The space is ICtCp (ITU-R BT.2100). Dolby designed it so that scaling the two
chroma axes leaves lightness and hue where they were - which is precisely the
operation "a saturation slider" is supposed to be. Scaling Ct and Cp is the
whole adjustment; I is untouched. The PQ inside ICtCp is part of ICtCp and stays
put whichever transfer the look itself sits behind.

Black maps to black by construction, which keeps gamescope's
bRaisesBlackLevelFloor false (it drives cv_overlay_unmultiplied_alpha).
"""

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


# Which transfer the encoded values arriving at the look are using. Only the
# outer pair changes; the PQ inside ICtCp is part of ICtCp and stays put.
TRANSFERS = {
    "pq": (pq_to_linear, linear_to_pq),
    "g22": (g22_to_linear, linear_to_g22),
}


def saturate(rgb_encoded, saturation: float, encoding: str = "pq"):
    """One LUT entry, in and out in whichever transfer the look sits behind."""
    if saturation == 1.0:
        return rgb_encoded
    try:
        decode, encode = TRANSFERS[encoding]
    except KeyError:
        raise ValueError(f"unknown encoding {encoding!r}") from None

    linear = tuple(decode(c) for c in rgb_encoded)
    lms = _mul3(_RGB_TO_LMS, linear)
    # Negative LMS can only come from out-of-gamut input; PQ is undefined there.
    lms_pq = tuple(linear_to_pq(max(c, 0.0)) for c in lms)

    i, ct, cp = _mul3(_LMS_TO_ICTCP, lms_pq)
    ct *= saturation
    cp *= saturation

    lms_pq = _mul3(_ICTCP_TO_LMS, (i, ct, cp))
    lms = tuple(pq_to_linear(max(c, 0.0)) for c in lms_pq)
    linear = _mul3(_LMS_TO_RGB, lms)

    # Clamp after the round trip: raising saturation pushes colours outside the
    # cube, and gamescope's tetrahedral interpolation expects 0..1. The ceiling
    # is in linear terms, so it is wherever this transfer puts encoded 1.0.
    ceiling = decode(1.0)
    return tuple(encode(min(max(c, 0.0), ceiling)) for c in linear)


def generate(saturation: float, size: int = 33, encoding: str = "pq") -> str:
    """A complete .cube file as text. R changes fastest, as gamescope expects."""
    if not 2 <= size <= 128:
        raise ValueError("gamescope accepts LUT_3D_SIZE between 2 and 128")
    if encoding not in TRANSFERS:
        raise ValueError(f"unknown encoding {encoding!r}")

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
        for g in range(size):
            for r in range(size):
                v = saturate((r * step, g * step, b * step), saturation, encoding)
                out.append("%.6f %.6f %.6f" % v)
    return "\n".join(out) + "\n"
