# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""What the plugin remembers while it runs, and what its sliders mean.

One PluginState holds it all - the state the panel sees, what is ours on
gamescope, and the workers and locks that keep requests in order - so every
part of the plugin reads and writes the same object. Nothing here imports
`decky`; main.py hands over its logger.
"""

import asyncio
import logging
import math

import vibrance_lut
import vibrance_x11

LOG = "[deckyvibrancehdr]"
# main.py replaces this with Decky's logger; the stand-in keeps tests quiet.
logger = logging.getLogger("deckyvibrancehdr")

# The number on the slider means the same thing on every machine: 1.0 is
# neutral, up is more saturated, and there is nothing below neutral. What it
# maps to does not, because buildSDRColorimetry() has two branches whose zero
# points disagree.
#
# Wide gamut: 0.0 is Rec.709, 1.0 the panel's native gamut, and nothing clamps
# the lerp - so neutral is 1.0 and there is room well above it.
#
# Anything else: 0.0 is native, 0.5 a generic wide gamut smoothly mapped, 1.0
# the same harshly mapped, and cfit() clamps there. Neutral is 0.0 and the whole
# useful range is one unit wide. That is the branch a Legion Go S takes, and the
# one vibrantDeck has been riding on a Steam Deck LCD all along.
SDR_MIN = 1.0
SDR_MAX_WIDE = 3.0
SDR_SOFT_WIDE = 2.0

# HDR is our own LUT, so the ceiling is set by where the maths stops paying for
# itself rather than by gamescope. Colours already at the gamut boundary clip
# from the first step; past about 1.3 the clipping becomes visible and detail in
# saturated areas goes with it.
HDR_MIN = 1.0
HDR_MAX = 1.5
HDR_SOFT_LIMIT = 1.3

# Neutral, and now also the bottom of both ranges.
SAT_DEFAULT = 1.0

DEFAULT_SETTINGS = {
    "sdr_enabled": False,
    "hdr_enabled": False,
    "sdr_saturation": SAT_DEFAULT,
    "hdr_saturation": SAT_DEFAULT,
}


def clamp(v: float, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError("saturation must be a finite number")
    return min(max(float(v), lo), hi)


class PluginState:
    """Everything the plugin remembers while it runs, in one place.

    Decky creates a single Plugin, and this is its memory: the state the
    panel sees, what is ours on gamescope, and the workers and locks that
    keep requests in order. reset() puts every field back to how a fresh
    load starts.
    """

    # A misspelt field is an error, not a new attribute nobody reads.
    __slots__ = (
        'public', 'sdr_baseline', 'last_sdr_written', 'last_sdr_attempted',
        'hdr_look_slot', 'hdr_generation', 'sdr_generation', 'sdr_look_slot',
        'sdr_look_generation', 'sdr_look_path', 'sdr_look_baseline', 'sdr_white_point',
        'hdr_look_path', 'hdr_look_baseline', 'reassert_strikes', 'slow_checks',
        'panel_cache_key', 'panel_cache_complete', 'mutation_lock', 'unloading',
        'tasks', 'workers', 'jobs', 'due',
        'save_generation', 'settings_dirty', 'settings_loaded', 'foreign_sdr_seen',
        'foreign_active_ticks', 'look_failures', 'disconnected', 'session_identity',
        'connect_ours', 'reassert_generation', 'resume_event', 'sdr_neutral_atom',
        'sdr_atom_span', 'display_scan_logged', 'look_errors_logged',
    )

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        # What get_state reports to the panel.
        self.public = {
            "ready": False,
            "reason": "starting up",
            "sdr_enabled": False,
            "hdr_enabled": False,
            "sdr_saturation": SAT_DEFAULT,
            "hdr_saturation": SAT_DEFAULT,
            "sdr_applied": False,
            "sdr_look_applied": False,
            "hdr_applied": False,
            "hdr_applied_saturation": None,
            "sdr_look_saturation": None,
            "sdr_error": "",
            "sdr_look_error": "",
            "hdr_error": "",
            "settings_error": "",
            "settings_notice": "",  # a file set aside this session, shown until reload
            "settings_loaded": False,
            "generating": False,
            "sdr_generating": False,
            "conflict": "",
            "external_display": False,
            "hdr_supported": True,
            "sdr_min": SDR_MIN,
            "sdr_max": SDR_MAX_WIDE,
            "sdr_soft_limit": SDR_SOFT_WIDE,
            "hdr_min": HDR_MIN,
            "hdr_max": HDR_MAX,
            "hdr_soft_limit": HDR_SOFT_LIMIT,
        }
        self.sdr_baseline = None        # whatever the atom held before we touched it
        self.last_sdr_written = None    # to notice someone else writing the same atom
        self.last_sdr_attempted = None  # the newest write, even one reported failed
        self.hdr_look_slot = 0
        self.hdr_generation = 0         # debounce token
        self.sdr_generation = 0         # ditto, much shorter
        self.sdr_look_slot = 0
        self.sdr_look_generation = 0
        self.sdr_look_path = None
        self.sdr_look_baseline = None
        self.sdr_white_point = vibrance_lut.D65
        self.hdr_look_path = None
        self.hdr_look_baseline = None
        self.reassert_strikes = 0       # consecutive times the atom disagreed with us
        self.slow_checks = 0            # ticks since the last of the expensive checks
        self.panel_cache_key = None
        self.panel_cache_complete = False
        self.mutation_lock = asyncio.Lock()
        self.unloading = False
        self.tasks = []
        self.workers = {}
        self.jobs = {}
        self.due = {}
        self.save_generation = 0
        self.settings_dirty = False
        self.settings_loaded = False
        self.foreign_sdr_seen = None    # SDR atom on the other X servers, last look
        self.foreign_active_ticks = 0   # slow ticks left on the cross-server warning
        self.look_failures = {"sdr": 0, "hdr": 0}  # consecutive failed builds or writes
        self.disconnected = False
        self.session_identity = None
        self.connect_ours = ()          # our SDR values, kept until a connect succeeds
        self.reassert_generation = 0
        self.resume_event = None

        # The SDR mapping the panel takes (see vibrance_panel.use_mapping); wide until known.
        self.sdr_neutral_atom, self.sdr_atom_span = 1.0, 2.0
        self.display_scan_logged = None  # what _pick_display last logged
        self.look_errors_logged = {}     # LUT prefix -> build failure last logged

    def set_error(self, component: str, message: str = "") -> None:
        """An operation may clear only its own error, never another half's."""
        self.public[f"{component}_error"] = message
        errors = [self.public.get(f"{part}_error", "")
                  for part in ("sdr", "sdr_look", "hdr", "settings")]
        if self.public["ready"]:
            self.public["reason"] = "; ".join(e for e in errors if e) or "ready"

    def can_restore_session(self) -> bool:
        # Guard restores immediately, including the interval before the next
        # monitor tick discovers a replaced server. Recovery requires a known
        # matching identity; ordinary startup without one retains its fallback.
        if self.session_identity is None:
            return not self.disconnected
        return vibrance_x11.session_identity() == self.session_identity


state = PluginState()
