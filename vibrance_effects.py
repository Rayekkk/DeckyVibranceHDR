# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""What the plugin puts on gamescope, and how it takes it back.

The SDR atom and the two looks: applying them, restoring whatever was there
before, telling our values from somebody else's and claiming our own leftovers
after a reconnect. Everything here is synchronous; main.py decides when it
runs (under the mutation lock, in a worker thread). Nothing imports `decky`.
"""

import os

import vibrance_host
import vibrance_lut
import vibrance_lutfile
import vibrance_state
import vibrance_x11
from vibrance_state import LOG, SAT_DEFAULT, state
from vibrance_x11 import ATOM_LOOK_G22, ATOM_LOOK_PQ, ATOM_SDR_WIDENESS

# Where LUT files go when Decky gives no runtime folder; main.py sets it.
settings_dir = None

# Where the atom's own neutral sits and how much of the slider it can carry
# are state.sdr_neutral_atom and state.sdr_atom_span, set once the panel is known.

# gamescope's own "nobody has set this": both branches of buildSDRColorimetry
# turn a negative wideness into that branch's native gamut. Writing it back is
# the only value that means "off" on either kind of panel - 1.0 is neutral on
# one branch and the harshest setting there is on the other.
SDR_UNSET = -1.0

# Only ever reached on a standard-gamut panel. There cfit() stops responding at
# atom 1.0, which is the slider's 200%, so the top third has nothing left to
# push. The remainder is carried by a look on the SDR path -
# GAMESCOPE_COLOR_LOOK_G22, the twin of the atom the HDR half already uses, in
# the gamma 2.2 domain. A wide-gamut panel never gets here: its atom span is
# the whole slider, so the remainder is always zero and no look is written.
SDR_MAX_STANDARD = 3.0

# Expressed as a rate rather than a ceiling on purpose: it means a given slider
# position keeps asking for the same thing when the top of the range moves, so
# raising the maximum does not quietly weaken every setting below it.
SDR_LOOK_PER_UNIT = 0.5
# Same ceiling as the HDR half, for the same reason: it is the same code and it
# clips in the same place. With the slider ending at 300% the rate reaches this
# exactly at the top, so it is a bound rather than a cut.
SDR_LOOK_CEILING = 1.5

# Where the LUT starts hard-clipping channels, which is the same look value the
# HDR half warns at - the maths is identical, only the transfer differs.
SDR_SOFT_STANDARD = 2.6


# The SDR and HDR looks run through one code path. What differs is the atom,
# the transfer the LUT is encoded in, the LUT file prefix, and which state
# attributes and panel fields hold each look's bookkeeping - all named here,
# so no code builds those names itself. "error" is the <error>_error state
# field; "worker" keys the debounce and the worker queue.
LOOKS = {
    "sdr": {"atom": ATOM_LOOK_G22, "encoding": "g22", "label": "SDR", "prefix": "sdr-look",
            "path": "sdr_look_path", "baseline": "sdr_look_baseline",
            "slot": "sdr_look_slot", "generation": "sdr_look_generation",
            "applied": "sdr_look_applied", "saturation": "sdr_look_saturation",
            "generating": "sdr_generating", "error": "sdr_look", "worker": "sdr_look"},
    "hdr": {"atom": ATOM_LOOK_PQ, "encoding": "pq", "label": "HDR", "prefix": "hdr-look",
            "path": "hdr_look_path", "baseline": "hdr_look_baseline",
            "slot": "hdr_look_slot", "generation": "hdr_generation",
            "applied": "hdr_applied", "saturation": "hdr_applied_saturation",
            "generating": "generating", "error": "hdr", "worker": "hdr"},
}


def sdr_split(value: float):
    """Split a slider position into (atom value, look saturation).

    The atom is free and instant, so it carries everything it can; whatever is
    left over goes to the look. On a wide-gamut panel there is never anything
    left over.
    """
    if value == SAT_DEFAULT:
        # gamescope's own "unset" is neutral on both branches of
        # buildSDRColorimetry, so 100% stays neutral on an unidentified panel.
        return SDR_UNSET, 1.0
    above = value - SAT_DEFAULT
    on_atom = min(above, state.sdr_atom_span)
    remainder = max(0.0, above - state.sdr_atom_span)
    look = min(1.0 + remainder * SDR_LOOK_PER_UNIT, SDR_LOOK_CEILING)
    return state.sdr_neutral_atom + on_atom, look


def runtime_dir() -> str:
    path = os.environ.get("DECKY_PLUGIN_RUNTIME_DIR") or settings_dir
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def write_look(saturation: float, slot: int,
               encoding: str = "pq", prefix: str = "hdr-look",
               source_white=vibrance_lut.D65, cancelled=lambda: False) -> str:
    """Build the .cube and return its path, or "" on failure.

    Two slots alternate rather than one file being rewritten. gamescope reloads
    when the atom changes, and while X does emit a PropertyNotify even for an
    identical value, a changing path removes any doubt about that - and leaves
    the previous LUT intact until the new one is complete.
    """
    path = os.path.join(runtime_dir(), f"{prefix}-{slot}.cube")
    try:
        built = vibrance_lutfile.write(path, saturation, encoding, source_white, cancelled)
    except Exception as e:
        if state.look_errors_logged.get(prefix) != str(e):  # retried every slow tick
            vibrance_state.logger.error(f"{LOG} could not write look: {vibrance_host.loggable(e)}")
            state.look_errors_logged[prefix] = str(e)
        return ""
    state.look_errors_logged.pop(prefix, None)
    return path if built else ""


def own_look_slot(path, prefix: str):
    """The slot number when *path* names one of this plugin's LUTs."""
    if not path:
        return None
    runtime = os.path.realpath(runtime_dir())
    candidate = os.path.realpath(path)
    for slot in (0, 1):
        expected = os.path.join(runtime, f"{prefix}-{slot}.cube")
        if candidate == expected:
            return slot
    return None


def apply_sdr() -> None:
    if (state.unloading or not state.public["sdr_enabled"]
            or state.disconnected
            or state.public["external_display"]):
        return
    if not state.can_restore_session():
        # A replaced gamescope is adopted (its own value read) before we write to it.
        state.set_error("sdr", "waiting for gamescope before applying SDR")
        return
    # The slider speaks in "1.0 is neutral"; the atom's neutral moves with
    # the branch, so the offset from neutral is what actually carries over.
    target, _look = sdr_split(state.public["sdr_saturation"])
    # A write reported failed (a timeout) may still have landed.
    state.last_sdr_attempted = target
    if vibrance_x11.write_float_atom(ATOM_SDR_WIDENESS, target):
        state.last_sdr_written = target
        state.public["sdr_applied"] = True
        state.set_error("sdr")
    else:
        state.set_error("sdr", "could not write SDR to gamescope")


def sdr_ours(*extra):
    """Every SDR value of ours that may still be on the atom."""
    return [value for value in (state.last_sdr_written, state.last_sdr_attempted, *extra)
            if value is not None]


def sdr_foreign(live, ours) -> bool:
    """A value somebody else left; a failed read (None) is not one."""
    return live is not None and all(abs(live - value) > 0.001 for value in ours)


def release_sdr() -> bool:
    # A write reported failed (a lost reply) may still have landed, so it
    # is released like an applied one once the atom shows it.
    held = state.public["sdr_applied"] or state.last_sdr_attempted is not None
    if held and not state.can_restore_session():
        state.set_error("sdr", "waiting for gamescope before restoring SDR")
        return False
    look_ok = release_look("sdr")
    if not held:
        state.set_error("sdr")
        return look_ok
    live = vibrance_x11.read_float_atom(ATOM_SDR_WIDENESS)
    ours = sdr_ours()
    if ours and sdr_foreign(live, ours):
        # The output changed or another writer took over. Restoring an old
        # internal-panel baseline here would overwrite their current value.
        state.last_sdr_written = state.last_sdr_attempted = None
        state.public["sdr_applied"] = False
        state.set_error("sdr")
        return look_ok
    target = state.sdr_baseline
    if target is None:
        target = SDR_UNSET
    if not vibrance_x11.write_float_atom(ATOM_SDR_WIDENESS, target):
        state.set_error(
            "sdr", "could not restore the previous gamescope SDR value")
        return False
    state.last_sdr_written = state.last_sdr_attempted = None
    state.public["sdr_applied"] = False
    state.set_error("sdr")
    vibrance_state.logger.info(f"{LOG} SDR released, restored {target:.3f}")
    return look_ok


def release_look(component) -> bool:
    """Give a look atom back to whatever held it before us.

    An empty path makes gamescope's LoadCubeLut fail to open, which clears
    the look. That is how gamescope behaves rather than a documented API.
    """
    look = LOOKS[component]
    path_field, baseline_field = look["path"], look["baseline"]
    owned = getattr(state, path_field)
    if owned is not None:
        if not state.can_restore_session():
            state.set_error(look["error"], "waiting for gamescope before restoring "
                            f"the {look['label']} look")
            return False
        live = vibrance_x11.read_string_atom(look["atom"])
        if (live and live != owned
                and own_look_slot(live, look["prefix"]) is None):
            # Somebody else replaced us. Their value is now the baseline and
            # must not be erased merely because our toggle went off.
            setattr(state, baseline_field, live)
        else:
            target = getattr(state, baseline_field) or ""
            if not vibrance_x11.write_string_atom(look["atom"], target):
                state.set_error(look["error"],
                                f"could not restore the previous {look['label']} look")
                return False
            setattr(state, baseline_field, target or None)
            vibrance_state.logger.info(f"{LOG} {look['label']} look restored")
        setattr(state, path_field, None)
    state.public[look["saturation"]] = None
    state.public[look["applied"]] = False
    state.look_failures[component] = 0  # nothing of ours left to fail
    state.set_error(look["error"])
    return True


def claim_live_look(component) -> None:
    """Before the first build: a LUT of ours still in the atom is ours again.

    Anything else there is the look to restore later. A failed read looks
    like an empty atom; neither replaces a foreign look we already know.
    """
    look = LOOKS[component]
    live = vibrance_x11.read_string_atom(look["atom"])
    live_slot = own_look_slot(live, look["prefix"])
    if live_slot is None:
        capture_look_baseline(component, live)
    else:
        setattr(state, look["path"], live)
        setattr(state, look["slot"], live_slot)


def commit_look(component, path, slot, value) -> None:
    """Point gamescope at a finished LUT and record the outcome."""
    look = LOOKS[component]
    if not state.can_restore_session():
        # As for SDR: a replaced gamescope's own look is read before we write.
        state.set_error(look["error"], f"waiting for gamescope before applying the {look['label']} look")
        return
    # A foreign writer may have changed while we built, even when this
    # replaces an already owned LUT.
    capture_look_baseline(component, vibrance_x11.read_string_atom(look["atom"]))
    # Unload restores without waiting for this worker; it must not write after.
    if state.unloading:
        return
    if vibrance_x11.write_string_atom(look["atom"], path):
        setattr(state, look["slot"], slot)
        setattr(state, look["path"], path)
        state.public[look["applied"]] = True
        state.public[look["saturation"]] = value
        state.look_failures[component] = 0
        state.set_error(look["error"])
        vibrance_state.logger.info(f"{LOG} {look['label']} look {value:.2f} -> {os.path.basename(path)}")
        return
    # A write reported failed may still have landed. With no look of ours
    # yet, count this one as ours so that release reads the atom and
    # restores if it did; a newer value is not recorded as applied either way.
    if getattr(state, look["path"]) is None:
        setattr(state, look["slot"], slot)
        setattr(state, look["path"], path)
    state.look_failures[component] += 1
    state.set_error(look["error"], f"could not write the {look['label']} LUT to gamescope")


def look_enabled(component) -> bool:
    if component == "hdr":
        return state.public["hdr_enabled"] and state.public["hdr_supported"]
    return state.public["sdr_enabled"]


def look_wanted(component) -> float:
    """The look saturation the slider asks for; 1.0 means no look."""
    if component == "hdr":
        return state.public["hdr_saturation"]
    return sdr_split(state.public["sdr_saturation"])[1]


def capture_look_baseline(component, live) -> None:
    """Keep a new foreign look before reclaiming an already owned atom.

    A dropped atom is not a new foreign baseline - and a read that failed
    looks exactly like one, so an empty reading never replaces what the
    first look at the atom found. Our own stale slots must not become
    restore targets either.
    """
    look = LOOKS[component]
    owned = getattr(state, look["path"])
    if (live and live != owned
            and own_look_slot(live, look["prefix"]) is None):
        setattr(state, look["baseline"], live)


def reapply_look(component, path, live) -> None:
    """Report reassert failures without hiding a pending replacement."""
    look = LOOKS[component]
    capture_look_baseline(component, live)
    requested = look_wanted(component)
    if requested == SAT_DEFAULT:
        # A failed clear retains ownership for retry, not permission to
        # revive the old nonneutral look after resume or a dropped atom.
        release_look(component)
        return
    if not state.can_restore_session():
        state.set_error(look["error"], f"waiting for gamescope before applying the {look['label']} look")
        return
    if state.unloading:
        return
    if not vibrance_x11.write_string_atom(look["atom"], path):
        state.set_error(look["error"],
                        f"could not reapply the {look['label']} LUT to gamescope")
    elif requested == state.public[look["saturation"]]:
        # A successful old LUT write does not resolve a failed newer build.
        state.set_error(look["error"])


def reassert_looks_if_dropped() -> None:
    """Put a look back if the atom stopped pointing at ours.

    This only catches the visible half of the problem: if gamescope drops
    the look internally while the property still names our file, nothing we
    can read says so. The startup and resume ladders are what cover that.
    """
    for component, look in LOOKS.items():
        path = getattr(state, look["path"])
        if (not look_enabled(component)
                or not state.public[look["applied"]] or not path):
            continue
        live = vibrance_x11.read_string_atom(look["atom"])
        neutral = look_wanted(component) == SAT_DEFAULT
        if not os.path.exists(path) or (live == path and not neutral):
            continue
        vibrance_state.logger.info(
            f"{LOG} {os.path.basename(path)} needs its look updated")
        reapply_look(component, path, live)


def capture_existing_looks(preserve=False) -> None:
    """Remember foreign baselines and adopt only our own stale paths."""
    for component, look in LOOKS.items():
        atom, slot_field = look["atom"], look["slot"]
        applied_field, saturation_field = look["applied"], look["saturation"]
        path_field, baseline_field = look["path"], look["baseline"]
        owned = getattr(state, path_field)
        live = vibrance_x11.read_string_atom(atom)
        slot = own_look_slot(live, look["prefix"])
        if preserve and owned is not None and (not live or slot is not None):
            # A missing atom on the same server does not erase its foreign
            # restore value. Resume can republish the retained file.
            if live and live != owned:
                setattr(state, path_field, live)
                setattr(state, slot_field, slot)
                state.public[saturation_field] = None
            continue
        # An adopted stale path in a new session has unknown saturation.
        state.public[saturation_field] = None
        setattr(state, baseline_field, live if slot is None else None)
        setattr(state, path_field, None if slot is None else live)
        state.public[applied_field] = slot is not None
        if slot is not None:
            setattr(state, slot_field, slot)


def reassert() -> None:
    """Write the SDR atom again and point gamescope back at the looks on disk."""
    if state.unloading or state.disconnected or state.public["external_display"]:
        return
    # Woken up in a dock: the panel check that notices may be seconds away,
    # and these values belong to the built-in screen.
    if vibrance_x11.display_is_external():
        return
    if state.public["sdr_enabled"]:
        apply_sdr()
    for component, look in LOOKS.items():
        path = getattr(state, look["path"])
        if (look_enabled(component) and state.public[look["applied"]]
                and path and os.path.exists(path)):
            reapply_look(component, path, vibrance_x11.read_string_atom(look["atom"]))
