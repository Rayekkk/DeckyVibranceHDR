# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Which panel the plugin is driving, and what that means for the sliders.

Reads the EDID gamescope publishes (or the kernel's), decides the SDR mapping
and whether the HDR half exists, and steps aside on an external display.
Nothing here imports `decky`.
"""

import os

import vibrance_edid
import vibrance_effects
import vibrance_host
import vibrance_state
import vibrance_x11
from vibrance_state import LOG, SDR_MIN, SDR_SOFT_WIDE, state
from vibrance_x11 import ATOM_EDID_PATH, ATOM_IS_EXTERNAL

# gamescope's own BIsWideGamut test, which is what decides between the two
# branches of buildSDRColorimetry - and therefore which mapping below applies.
# Measured: Legion Go 2 OLED is 0.6836/0.3154 and passes; Legion Go S LCD is
# 0.6523/0.3408 and fails on y after passing x by a hair.
#
# gamescope runs the test on the EDID's primaries - or on Rec.709 when there
# is no EDID or it has no colorimetry (red x of zero) - unless one of its bundled
# display scripts supplies colorimetry of its own - a handful of devices
# (Steam Deck LCD, Zotac Zone, OXP F1 and GPD Win 4 at the time of writing).
# The plugin reads the EDID, which is what gamescope uses everywhere else.
# Reproducing gamescope's Lua matching is deliberately not attempted: it would
# turn a panel-driven plugin into a per-device one. Where a script's primaries
# land on the other side of the test than the EDID's, 100% stays neutral (it is
# gamescope's own default) but the steps above it follow the other branch.
WIDE_GAMUT_RED_X = 0.650
WIDE_GAMUT_RED_Y = 0.320


def panel_supports_hdr(blob):
    """Whether the HDR half has anything to act on, or None if unknowable."""
    if blob is None:
        return None
    supported = vibrance_edid.supports_pq(blob)
    if supported is not None:
        vibrance_state.logger.info(
            f"{LOG} panel {'advertises' if supported else 'does not advertise'} "
            f"PQ in its CTA-861 block")
    return supported

def panel_is_wide_gamut(blob):
    """gamescope's BIsWideGamut, applied to the EDID gamescope itself publishes.

    True, False, or None when the panel could not be identified - in which case
    the caller carries on rather than disabling a working control over a missing
    atom.
    """
    if blob is None:
        return None
    primary = vibrance_edid.red_primary(blob)
    if primary is None:
        return None
    red_x, red_y = primary
    wide = red_x > WIDE_GAMUT_RED_X and red_y < WIDE_GAMUT_RED_Y
    vibrance_state.logger.info(
        f"{LOG} panel red {red_x:.4f},{red_y:.4f} - "
        f"{'wide gamut' if wide else 'standard gamut'}")
    return wide

def monitor_probe():
    """Reuse one root dump for session health and panel atoms.

    None when the dump did not complete: a slow xprop is no verdict on the
    session, and treating it as one disconnects a perfectly healthy one.
    """
    out = vibrance_x11.read_root_properties(vibrance_x11.DISPLAY)
    if out is None:
        return None
    count = sum(1 for line in out.splitlines() if line.startswith("GAMESCOPE"))
    if not count:
        return 0, None
    atoms = {}
    for line in out.splitlines():
        name = line.partition("(")[0].split("=", 1)[0].strip()
        if name in (ATOM_IS_EXTERNAL, ATOM_EDID_PATH):
            atoms[name] = line
    external = vibrance_x11.parse_int_atom(atoms.get(ATOM_IS_EXTERNAL, ""))
    external_value = None if external is None else bool(external)
    edid_path = None if external_value is True else vibrance_x11.parse_string_atom(atoms.get(ATOM_EDID_PATH, ""))
    return count, probe_panel((external_value, edid_path))

def probe_panel(metadata=None):
    """Read display metadata without mutating settings or owning atoms."""
    if metadata is None:
        # One root dump, as the monitor takes. Two reads let a timed-out
        # IS_EXTERNAL pass for an absent one beside an external EDID path.
        _count, probe = monitor_probe() or (0, None)
        if probe is not None:
            return probe
        metadata = (None, None)
    external_value, edid_path = metadata
    if not edid_path and external_value is not True:
        edid_path = vibrance_host.builtin_panel_edid_path()
    identity = vibrance_host.file_identity(edid_path)
    cache_key = (("edid", edid_path, identity) if external_value is None else
                 ("external", bool(external_value), edid_path, identity))
    cached = cache_key == state.panel_cache_key and state.panel_cache_complete
    blob = None if external_value is True or cached or not edid_path else vibrance_host.read_edid(edid_path)
    return external_value, cache_key, blob

def panel_shape():
    """Everything the panel decides, to tell whether a probe moved any of it."""
    return (state.sdr_neutral_atom, state.sdr_atom_span,
            state.public["hdr_supported"], state.public["external_display"],
            state.sdr_white_point)

def use_mapping(wide) -> None:
    """Map the slider onto the branch of buildSDRColorimetry the panel takes."""
    if wide:
        state.sdr_neutral_atom, state.sdr_atom_span = 1.0, 2.0
        state.public["sdr_soft_limit"] = SDR_SOFT_WIDE
    else:
        state.sdr_neutral_atom, state.sdr_atom_span = 0.0, 1.0
        # The atom gives out at 200%; the look carries the rest to the same
        # sdr_max, which is why PluginState.reset sets that once for both.
        state.public["sdr_soft_limit"] = vibrance_effects.SDR_SOFT_STANDARD

def configure_for_panel(probe=None) -> bool:
    """Line the slider up with the branch this display takes.

    Run at startup and again whenever the display changes underneath us -
    docking swaps the panel without restarting anything, and the two
    branches of buildSDRColorimetry disagree about where neutral is.

    Returns True when something actually moved.
    """
    external_value, cache_key, blob = probe if probe is not None else probe_panel()
    if (cache_key == state.panel_cache_key
            and state.panel_cache_complete):
        return False

    before = panel_shape()
    state.panel_cache_key = cache_key

    # Everything below is calibrated against the built-in panel: its gamut
    # decides the mapping and its EDID decides whether the HDR half exists.
    # An external display is somebody else's panel, so the plugin steps off
    # it rather than applying numbers that were measured elsewhere.
    state.public["external_display"] = external_value is True
    if state.public["external_display"]:
        state.panel_cache_complete = True
        return before != panel_shape()

    # An EDID that cannot be read says nothing about the panel, so every
    # unknown below keeps what was last known. Guessing wide instead would
    # turn a standard panel's neutral slider into its harshest atom value.
    # Before anything is known that is the startup default: the wide
    # mapping, D65 and a visible HDR half.
    kernel_edid = str(cache_key[-2]).startswith(os.path.join(vibrance_host.DRM_SYSFS, ""))
    wide = panel_is_wide_gamut(blob)
    if blob == b"" and kernel_edid:
        # The connector has no EDID at all, and gamescope then assumes Rec.709
        # just as for a red x of zero. Only the kernel's file is trusted to be
        # empty: one at gamescope's own path may not be written yet.
        wide = False
    if vibrance_edid.valid(blob):
        state.sdr_white_point = vibrance_edid.white_point(blob)

    if wide is not None:
        use_mapping(wide)

    hdr = panel_supports_hdr(blob)
    if hdr is False and kernel_edid:
        # gamescope adds HDR for a display script's panel (Steam Deck OLED,
        # say) only to its patched EDID copy; the kernel's never says so.
        hdr = None
    if hdr is not None:
        state.public["hdr_supported"] = hdr
    # Re-reading the same bytes cannot change an answer, so a valid EDID
    # completes the cache even if part of it is unknown; its file identity
    # is in the cache key, so a rewritten EDID is still read again.
    state.panel_cache_complete = ((wide is not None and hdr is not None)
                                    or vibrance_edid.valid(blob))
    state.public["sdr_saturation"] = vibrance_state.clamp(
        state.public["sdr_saturation"], SDR_MIN, state.public["sdr_max"])

    after = panel_shape()
    if before != after:
        vibrance_state.logger.info(
            f"{LOG} SDR neutral atom {state.sdr_neutral_atom:.1f}, atom span "
            f"{state.sdr_atom_span:.1f}, slider up to "
            f"{state.public['sdr_max']:.1f}, HDR half "
            f"{'on' if state.public['hdr_supported'] else 'off'}")
    return before != after
