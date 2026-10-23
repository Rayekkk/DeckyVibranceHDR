# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""DeckyVibranceHDR - saturation control for both SDR and HDR content.

Three gamescope interfaces, picked per display rather than per device.

SDR has a dial of its own: GAMESCOPE_COLOR_SDR_GAMUT_WIDENESS moves the source
colorimetry away from Rec.709. Where its neutral sits and how far it goes depend
on which branch of buildSDRColorimetry() the panel takes, so the slider is
mapped onto that branch instead of assuming one - see the constants below.

Where the atom gives out, GAMESCOPE_COLOR_LOOK_G22 takes over: a .cube file
applied to SDR content. Only ever needed on a standard-gamut panel, where cfit()
clamps the atom long before the slider ends.

HDR has no dial at all. buildPQColorimetry() zeroes the gamut remap on purpose,
but the PQ path does accept a look, so GAMESCOPE_COLOR_LOOK_PQ gets the same
treatment. See vibrance_lut.py for why the maths has to happen in ICtCp.

Nothing here is tied to a particular machine, but it is tied to the *built-in*
panel: the display is measured, and an external one is left alone.
"""

import asyncio
import os
import shutil
import sys
import time
from contextlib import asynccontextmanager

import decky
from settings import SettingsManager

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

# Not `updater`: the loader aliases its own decky_loader.updater to that bare
# name before we are imported, and sys.modules wins over sys.path.
from lego_updater import Updater, cancel_downloads  # noqa: E402
import vibrance_effects  # noqa: E402
import vibrance_host  # noqa: E402
import vibrance_lut  # noqa: E402
import vibrance_lutfile  # noqa: E402
import vibrance_panel  # noqa: E402
import vibrance_settings  # noqa: E402
import vibrance_state  # noqa: E402
from vibrance_state import (  # noqa: E402
    DEFAULT_SETTINGS, HDR_MAX, HDR_MIN, LOG, SAT_DEFAULT, SDR_MAX_WIDE, SDR_MIN, state)
import vibrance_x11  # noqa: E402
from vibrance_x11 import ATOM_SDR_WIDENESS  # noqa: E402

GITHUB_RELEASES_URL = (
    "https://api.github.com/repos/Rayekkk/DeckyVibranceHDR/releases/latest"
)

vibrance_state.logger = decky.logger
vibrance_effects.settings_dir = decky.DECKY_PLUGIN_SETTINGS_DIR

updater = Updater(
    releases_url=GITHUB_RELEASES_URL,
    user_agent="DeckyVibranceHDR",
    log_prefix=LOG,
    plugin_dir=PLUGIN_DIR,
    logger=decky.logger,
    asset_name=lambda version: f"DeckyVibranceHDR-{version}.zip",
    archive_root="DeckyVibranceHDR",
    # Only files that identify this plugin: every installed copy keeps this list,
    # so naming each module would refuse a future release that renames one.
    # verify:package checks a release is complete before it is published.
    archive_files=("lego_updater.py", "vibrance_lut.py"),
)


# A LUT build costs about a third of a second, far too slow to redo on every
# tick of a slider, so a change is only acted on once the value stops moving.
DEBOUNCE_S = 0.25

# The SDR half is one float and used not to need this, but at 0.01 a drag across
# its range is 200 calls. Each one spawned an xprop and rewrote the settings
# file, unserialized, so writes could land out of order and leave the panel on a
# stale value while the UI showed the new one. Short enough to stay invisible.
SDR_COALESCE_S = 0.05

# A look was seen missing after a suspend on a device, though in gamescope's
# source only a property change, gamescopectl's set_look or a gamescope_control
# set_look/unset_look request replaces one; reapplying is cheap insurance.
# CLOCK_MONOTONIC stops while the machine is asleep and CLOCK_BOOTTIME does
# not, so the gap between them is a suspend cycle - no dbus, no root, no
# Steam client. gamescope is not necessarily ready the moment we notice, and can
# clobber us during its own startup, so the values go back several times.
SUSPEND_POLL_S = 1.0
SUSPEND_SLACK_S = 2.0
REASSERT_DELAYS_S = (0.3, 1.0, 2.5, 5.0, 8.0)

# Decky brings plugins up as a system service, which can beat the session to it.
# Finding no gamescope at startup is a race, not a verdict, so we keep looking.
GAMESCOPE_RETRY_FIRST_S = 1.0
GAMESCOPE_RETRY_MAX_S = 15.0

# It publishes its atoms as it comes up, so finding some is not the same as it
# being ready - anything written into a half-built session gets thrown away when
# it finishes. Observed on a Legion Go S: 20 atoms one second in, 44 once up.
GAMESCOPE_SETTLE_S = 1.0
GAMESCOPE_SETTLE_TRIES = 12

# A settings file that cannot be opened is retried this often; one that
# opens but cannot parse is set aside instead.
SETTINGS_RETRY_S = 1.0

# One disagreement is gamescope resetting the atom; a run of them is another
# plugin holding the other end. Only the second is worth telling the user about.
CONFLICT_STRIKES = 3
WATCH_INTERVAL_S = 4.0

# Docking swaps the panel without restarting anything, so the display is
# re-read every PANEL_CHECK_INTERVAL_S. The expensive checks - another plugin
# on a second X server, and whether our looks are still the ones loaded - run
# on every SLOW_CHECK_EVERY-th watch tick (about every 16 s) rather than
# spawning an xprop every few seconds.
PANEL_CHECK_INTERVAL_S = 5.0
SLOW_CHECK_EVERY = 4

# Decky's manager creates an empty file on first install, so whether one
# existed is recorded before it is constructed.
vibrance_settings.note_existing(os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json"))


class _DeferredSettingsManager(SettingsManager):
    def read(self):
        # Decky's constructor calls read() for existing files. Defer that
        # blocking, error-suppressing read until _main can validate the file
        # and report/retry failures through vibrance_settings.read.
        pass


settings = _DeferredSettingsManager(
    name="settings",
    settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR,
)

# xprop's usual timeout is vibrance_x11.timeout; unloading gets this one.
UNLOAD_XPROP_TIMEOUT_S = 0.25


def _offload(fn, *args):
    return asyncio.get_event_loop().run_in_executor(None, fn, *args)


@asynccontextmanager
async def _mutation():
    """The mutation lock for an exported setter; False once unloading has begun."""
    async with state.mutation_lock:
        if state.unloading:
            yield False
            return
        if not state.settings_loaded:
            raise RuntimeError("settings have not loaded; retry after startup")
        yield True


def _boottime() -> float:
    """Seconds since boot, counting time spent suspended.

    Falls back to the monotonic clock where CLOCK_BOOTTIME does not exist, which
    leaves the two readings identical and the suspend detector simply idle.
    """
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()


def _setting(key, default):
    """Treat each damaged field independently; old numeric ranges still migrate."""
    try:
        value = settings.getSetting(key, default)
        if isinstance(default, bool):
            return value if isinstance(value, bool) else default
        return vibrance_state.clamp(value, SDR_MIN if key.startswith("sdr") else HDR_MIN,
                      SDR_MAX_WIDE if key.startswith("sdr") else HDR_MAX)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return default


# ── talking to gamescope ───────────────────────────────────────────────────────

def _pick_display():
    """vibrance_x11.pick_display, logging what it saw only when that changes:
    without gamescope (Desktop Mode) the search repeats every few seconds."""
    best, scan = vibrance_x11.pick_display()
    if scan != state.display_scan_logged:
        state.display_scan_logged = scan
        decky.logger.info(f"{LOG} {scan}")
    return best


# ── the looks ──────────────────────────────────────────────────────────────────

class Plugin:
    # ── exported ───────────────────────────────────────────────────────────────

    async def get_state(self) -> dict:
        return dict(state.public)

    async def get_version(self) -> dict:
        return {"version": updater.plugin_version()}

    async def check_for_updates(self) -> dict:
        # The updater already offers only the exact DeckyVibranceHDR-<v>.zip.
        return await _offload(updater.check)

    async def perform_update(self, download_url: str, asset_name: str) -> dict:
        return await _offload(updater.download, download_url, asset_name)

    async def set_sdr_enabled(self, enabled: bool, saturation=None) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if saturation is not None:
            saturation = vibrance_state.clamp(saturation, SDR_MIN, state.public["sdr_max"])
        async with _mutation() as allowed:
            if not allowed:
                return dict(state.public)
            if saturation is not None:
                state.public["sdr_saturation"] = saturation
            if enabled and state.public["external_display"]:
                if saturation is not None:
                    self._schedule_save()
                return dict(state.public)
            if (enabled and not state.public["sdr_enabled"]
                    and not state.public["sdr_applied"] and state.last_sdr_attempted is None
                    and not state.disconnected):
                # With a write of ours possibly on the atom, it is not a
                # baseline; a failed read keeps the one already known.
                live = await _offload(vibrance_x11.read_float_atom, ATOM_SDR_WIDENESS)
                if live is not None:
                    state.sdr_baseline = live
            state.public["sdr_enabled"] = enabled
            if not enabled:
                Plugin._invalidate_sdr_jobs()
            self._schedule_save()
            if enabled and not state.disconnected:
                await _offload(vibrance_effects.apply_sdr)
                self._schedule_look("sdr")
            elif not enabled:
                await _offload(vibrance_effects.release_sdr)
            return dict(state.public)

    async def set_hdr_enabled(self, enabled: bool, saturation=None) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if saturation is not None:
            saturation = vibrance_state.clamp(saturation, HDR_MIN, HDR_MAX)
        async with _mutation() as allowed:
            if not allowed:
                return dict(state.public)
            if saturation is not None:
                state.public["hdr_saturation"] = saturation
            if enabled and (state.public["external_display"]
                            or not state.public["hdr_supported"]):
                if saturation is not None:
                    self._schedule_save()
                return dict(state.public)
            state.public["hdr_enabled"] = enabled
            if not enabled:
                Plugin._invalidate_hdr_jobs()
            self._schedule_save()
            if enabled and not state.disconnected:
                self._schedule_look("hdr")
            elif not enabled:
                await _offload(vibrance_effects.release_look, "hdr")
            return dict(state.public)

    async def set_sdr_saturation(self, value: float) -> dict:
        async with _mutation() as allowed:
            if not allowed:
                return dict(state.public)
            value = vibrance_state.clamp(value, SDR_MIN, state.public["sdr_max"])
            changed = value != state.public["sdr_saturation"]
            state.public["sdr_saturation"] = value
            if changed:
                self._schedule_save()
                self._schedule_sdr()
                self._schedule_look("sdr")
            else:
                self._retry_settings_save()
                target, _look = vibrance_effects.sdr_split(value)
                if (state.public["sdr_enabled"] and not state.disconnected
                        and not state.public["external_display"]
                        and (state.public["sdr_error"] or not state.public["sdr_applied"]
                             or state.last_sdr_written != target)):
                    self._schedule_sdr()
                self._retry_pending_looks("sdr")
            return dict(state.public)

    async def set_hdr_saturation(self, value: float) -> dict:
        async with _mutation() as allowed:
            if not allowed:
                return dict(state.public)
            value = vibrance_state.clamp(value, HDR_MIN, HDR_MAX)
            changed = value != state.public["hdr_saturation"]
            state.public["hdr_saturation"] = value
            if changed:
                self._schedule_save()
                self._schedule_look("hdr")
            else:
                self._retry_settings_save()
                self._retry_pending_looks("hdr")
            return dict(state.public)

    async def reset(self) -> dict:
        async with _mutation() as allowed:
            if not allowed:
                return dict(state.public)
            state.public["sdr_saturation"] = SAT_DEFAULT
            state.public["hdr_saturation"] = SAT_DEFAULT
            state.public["sdr_enabled"] = False
            state.public["hdr_enabled"] = False
            Plugin._invalidate_sdr_jobs()
            Plugin._invalidate_hdr_jobs()
            self._schedule_save()
            await _offload(vibrance_effects.release_sdr)
            await _offload(vibrance_effects.release_look, "hdr")
            return dict(state.public)

    # ── internals ──────────────────────────────────────────────────────────────

    @staticmethod
    def _save(values=None) -> None:
        with vibrance_settings.lock:
            if not state.settings_loaded:
                raise RuntimeError("settings have not loaded; refusing to overwrite the saved file")
            if values is not None and state.unloading:
                return  # an older snapshot; unload's final flush has the newest values
            vibrance_settings.write(settings, values if values is not None else
                                    {key: state.public[key] for key in DEFAULT_SETTINGS})

    @staticmethod
    def _write_settings(values=None) -> str:
        """Disk work only; a completed older snapshot cannot acknowledge edits."""
        try:
            Plugin._save(values)
            return ""
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            # Not the OSError's filename: mkstemp names a random temporary each try.
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
            message = f"could not save settings: {reason}"
            if message != state.public["settings_error"]:  # retried on every panel tick
                decky.logger.error(f"{LOG} settings save failed: {reason}")
            return message

    @staticmethod
    def _persist() -> None:
        # Unload is the synchronous durability barrier. It never yields and
        # accepts no more edits; the writer lock also drains an older disk
        # snapshot before this final one replaces it.
        if not state.settings_loaded:
            return
        error = Plugin._write_settings()
        state.settings_dirty = bool(error)
        state.set_error("settings", error)

    def _schedule_save(self):
        if not state.settings_loaded:
            return
        state.settings_dirty = True
        state.save_generation += 1
        token = state.save_generation
        async def run():
            await self._debounce("settings")
            async with state.mutation_lock:
                if token != state.save_generation or state.unloading:
                    return
                values = {key: state.public[key] for key in DEFAULT_SETTINGS}
            # fsync/replace may be slow. They must not hold the control lock.
            error = await _offload(Plugin._write_settings, values)
            async with state.mutation_lock:
                if token == state.save_generation and not state.unloading:
                    state.settings_dirty = bool(error)
                    state.set_error("settings", error)
        self._queue_worker("settings", token, run, SDR_COALESCE_S)

    def _retry_settings_save(self):
        worker = state.workers.get("settings")
        if (state.settings_dirty and not state.unloading
                and (worker is None or worker.done() or worker not in state.tasks)):
            self._schedule_save()

    async def _debounce(self, component):
        while True:
            remaining = state.due[component] - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    def _queue_worker(self, component, token, run, delay):
        """One task and one executor job per component, with one latest request."""
        state.jobs[component] = (token, run)
        state.due[component] = time.monotonic() + delay
        worker = state.workers.get(component)
        if worker is not None and not worker.done() and worker in state.tasks:
            return
        async def loop():
            while not state.unloading:
                current, job = state.jobs[component]
                await job()
                if state.jobs[component][0] == current:
                    return
        task = asyncio.create_task(loop())
        state.workers[component] = task
        Plugin._track(task)

    @staticmethod
    def _invalidate_sdr_jobs() -> None:
        state.sdr_generation += 1
        state.sdr_look_generation += 1
        state.public["sdr_generating"] = False

    @staticmethod
    def _invalidate_hdr_jobs() -> None:
        state.hdr_generation += 1
        state.public["generating"] = False

    @staticmethod
    def _track(task) -> None:
        state.tasks.append(task)
        def done(t):
            if t in state.tasks:
                state.tasks.remove(t)
            if not t.cancelled() and t.exception() is not None:
                decky.logger.error(f"{LOG} background task failed: {t.exception()}")
        task.add_done_callback(done)

    def _schedule_sdr(self) -> None:
        """Write once the slider has been still for SDR_COALESCE_S.
        """
        state.sdr_generation += 1
        token = state.sdr_generation

        async def _run():
            try:
                await self._debounce("sdr")
                async with state.mutation_lock:
                    if (token != state.sdr_generation or state.unloading):
                        return  # superseded by a newer move
                    if (state.public["sdr_enabled"]
                            and not state.public["external_display"]):
                        await _offload(vibrance_effects.apply_sdr)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if token == state.sdr_generation and not state.unloading:
                    state.set_error("sdr", f"could not apply SDR: {e}")
                decky.logger.error(f"{LOG} SDR apply failed: {e}")

        self._queue_worker("sdr", token, _run, SDR_COALESCE_S)

    def _schedule_look(self, component) -> None:
        """Rebuild a look once its slider has been still for DEBOUNCE_S.

        A 33-cube takes about a third of a second, far too slow for every tick
        of a slider. Building is deliberately outside the state lock.
        Committing is not: a disable, dock or newer value either wins the lock
        first and invalidates us, or runs after our write and restores the
        previous look.
        """
        look = vibrance_effects.LOOKS[component]
        name, generation, prefix = look["label"], look["generation"], look["prefix"]
        setattr(state, generation, getattr(state, generation) + 1)
        token = getattr(state, generation)
        if vibrance_effects.look_enabled(component) and vibrance_effects.look_wanted(component) != SAT_DEFAULT:
            state.public[look["generating"]] = True

        def current():
            return (token == getattr(state, generation) and not state.unloading
                    and not state.disconnected and vibrance_effects.look_enabled(component)
                    and not state.public["external_display"])

        async def _run():
            try:
                await self._debounce(look["worker"])
                async with state.mutation_lock:
                    if not current():
                        return  # superseded by a newer move
                    value = vibrance_effects.look_wanted(component)
                    if value == SAT_DEFAULT:
                        # Identity: no point loading a LUT that does nothing.
                        await _offload(vibrance_effects.release_look, component)
                        return
                    if getattr(state, look["path"]) is None:
                        await _offload(vibrance_effects.claim_live_look, component)
                    slot = 1 - getattr(state, look["slot"])
                    white_point = (state.sdr_white_point if component == "sdr"
                                   else vibrance_lut.D65)
                path = await _offload(
                    vibrance_effects.write_look, value, slot, look["encoding"], prefix, white_point,
                    lambda: token != getattr(state, generation) or state.unloading)
                if not path:
                    if token == getattr(state, generation):
                        state.look_failures[component] += 1
                        state.set_error(look["error"], f"could not build the {name} LUT")
                    return
                async with state.mutation_lock:
                    if current():
                        await _offload(vibrance_effects.commit_look, component, path, slot, value)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if token == getattr(state, generation) and not state.unloading:
                    state.look_failures[component] += 1
                    state.set_error(look["error"], f"could not apply the {name} LUT: {e}")
                decky.logger.error(f"{LOG} {name} look failed: {e}")
            finally:
                if token == getattr(state, generation):
                    state.public[look["generating"]] = False

        self._queue_worker(look["worker"], token, _run, DEBOUNCE_S)

    def _retry_pending_looks(self, only=None, quick=False) -> None:
        """Retry under the mutation lock on a monitor tick or unchanged edit.

        A failed replacement leaves the old file owned, but that file must not
        be mistaken for the requested value. Never supersede an active worker.
        """
        if state.unloading or state.disconnected or state.public["external_display"]:
            return
        for component, look in vibrance_effects.LOOKS.items():
            if ((only is not None and component != only)
                    or not vibrance_effects.look_enabled(component)
                    or state.public[look["generating"]]
                    or (quick and state.look_failures[component] > 1)):
                continue
            value = vibrance_effects.look_wanted(component)
            path = getattr(state, look["path"])
            pending = (bool(path) if value == SAT_DEFAULT else
                       state.public[look["saturation"]] != value
                       or not path or not os.path.isfile(path))
            if pending or state.public[f"{look['error']}_error"]:
                self._schedule_look(component)

    @staticmethod
    def _foreign_conflict(foreign, slow_tick) -> str:
        """The warning about a writer on another X server, or "".

        Such a writer never shows up in what we read back, so it is checked
        separately, on the slow tick since it costs a process per server. The
        atom lingers after its writer stops, so only a value that changed since
        the last look counts, and the warning fades after CONFLICT_STRIKES
        quiet looks. The first reading is a reference only: it may be our own
        write from before a reconnect moved us.
        """
        if slow_tick and foreign is not None:
            changed = (bool(foreign) and state.foreign_sdr_seen is not None
                       and foreign != state.foreign_sdr_seen)
            state.foreign_active_ticks = (
                CONFLICT_STRIKES if changed else max(0, state.foreign_active_ticks - 1))
            state.foreign_sdr_seen = foreign
        if not state.foreign_active_ticks:
            return ""
        return ("another plugin is driving the same SDR setting from a different X "
                "server (vibrantDeck does) - both writes take effect and the last "
                "one wins, so keep only one enabled")

    @staticmethod
    def _read_sdr():
        """The SDR atom and, should that read fail, whether gamescope is still
        on this display: None also means an unreachable X server."""
        live = vibrance_x11.read_float_atom(ATOM_SDR_WIDENESS)
        return live, live is not None or vibrance_x11.count_gamescope_atoms(vibrance_x11.DISPLAY) > 0

    @staticmethod
    def _sdr_reading_key():
        # A slider move, a write of ours or a new session outdates a reading.
        return state.sdr_generation, state.session_identity, state.last_sdr_attempted

    @staticmethod
    def _watch_once(foreign, sdr=None) -> None:
        """Put our value back when something drops it, and only call it a
        conflict once that keeps happening.

        gamescope forgets these atoms when it re-initialises, so one
        disagreement is routine and heals itself. vibrantDeck writes the same
        SDR atom, and that disagrees every single time we look - which is what
        the strike count is there to tell apart.

        *foreign* is a reading of the other X servers taken outside the lock
        (None when there is none this tick); *sdr* is _read_sdr() taken there
        too, or None to read it here.
        """
        # Look health is independent of the SDR atom. In particular, an HDR-only
        # configuration still needs its slow readback check, and a failed clear
        # needs another chance even though the toggle is already off.
        state.slow_checks += 1
        slow_tick = state.slow_checks >= SLOW_CHECK_EVERY
        if slow_tick:
            state.slow_checks = 0
            if not state.public["sdr_enabled"]:
                vibrance_effects.release_sdr()
            # Also once the panel no longer advertises PQ: the toggle is hidden then.
            if not vibrance_effects.look_enabled("hdr"):
                vibrance_effects.release_look("hdr")
            vibrance_effects.reassert_looks_if_dropped()

        if not state.public["sdr_enabled"]:
            state.public["conflict"] = ""
            state.reassert_strikes = 0
            # Other servers are not read while off; a reference from before
            # would turn any write made meanwhile into a false warning.
            state.foreign_sdr_seen = None
            state.foreign_active_ticks = 0
            return

        if not state.public["sdr_applied"]:
            vibrance_effects.apply_sdr()
            return

        foreign_conflict = Plugin._foreign_conflict(foreign, slow_tick)
        if not state.reassert_strikes:
            state.public["conflict"] = foreign_conflict

        live, present = sdr if sdr is not None else Plugin._read_sdr()
        if live is None:
            # Only recreate the property when gamescope is still present.
            if present:
                vibrance_effects.apply_sdr()
            else:
                state.set_error("sdr", "could not reach gamescope; retrying")
            return
        if state.last_sdr_written is None:
            return
        if abs(live - state.last_sdr_written) <= 0.001:
            target, _ = vibrance_effects.sdr_split(state.public["sdr_saturation"])
            if target != state.last_sdr_written:
                vibrance_effects.apply_sdr()
            else:
                state.set_error("sdr")
            state.public["conflict"] = foreign_conflict
            state.reassert_strikes = 0
            return

        state.reassert_strikes += 1
        decky.logger.info(
            f"{LOG} SDR atom was {live:.3f}, putting "
            f"{vibrance_effects.sdr_split(state.public['sdr_saturation'])[0]:.3f} back "
            f"(strike {state.reassert_strikes})")
        vibrance_effects.apply_sdr()
        if state.reassert_strikes >= CONFLICT_STRIKES:
            state.public["conflict"] = (
                "another plugin keeps overwriting the same SDR setting "
                "(vibrantDeck does) - keep only one of them enabled")

    async def _reassert_repeatedly(self):
        """gamescope is not necessarily up when we notice, and can clobber us
        while it starts, so the values go back more than once.

        Used after a resume and after startup, which are the same problem: the
        session rebuilding its colour management underneath whatever we wrote.
        A look gone missing then shows up as exactly the part of the slider the
        atom cannot reach going missing; writing both again costs little.
        """
        state.reassert_strikes = 0
        state.public["conflict"] = ""
        token = state.reassert_generation
        if state.resume_event is not None:
            # A set event was meant for the ladder this one replaces. One that
            # quit on the token check never consumed it, and leaving it set
            # would end this ladder before its first step.
            state.resume_event.clear()
        for delay in REASSERT_DELAYS_S:
            if state.resume_event is None:
                await asyncio.sleep(delay)
            else:
                try:
                    await asyncio.wait_for(state.resume_event.wait(), timeout=delay)
                    state.resume_event.clear()
                    return  # The same worker restarts with the newest resume.
                except asyncio.TimeoutError:
                    pass
            async with state.mutation_lock:
                if state.unloading or token != state.reassert_generation:
                    return
                await _offload(vibrance_effects.reassert)
        state.reassert_strikes = 0

    async def _watch_tick(self):
        # The atoms are read before the lock: a slow or stalled X server must
        # not hold up the controls. A reading a write of ours has outdated in
        # the meantime is taken again under the lock.
        foreign = sdr = None
        if state.public["sdr_enabled"] and state.public["sdr_applied"]:
            if state.slow_checks + 1 >= SLOW_CHECK_EVERY:
                foreign = await _offload(vibrance_x11.foreign_sdr_values)
            key = Plugin._sdr_reading_key()
            sdr = await _offload(Plugin._read_sdr)
        async with state.mutation_lock:
            if not state.unloading and not state.disconnected:
                if sdr is not None and key != Plugin._sdr_reading_key():
                    sdr = None
                await _offload(Plugin._watch_once, foreign, sdr)
                # A pending look is a full LUT build. A first failure is retried
                # on the next tick; a repeat only on the slow tick (about every
                # 16 s), which _watch_once has just reset the counter for.
                self._retry_pending_looks(quick=state.slow_checks != 0)

    def _schedule_reassert(self):
        state.reassert_generation += 1
        worker = state.workers.get("reassert")
        if worker is not None and not worker.done() and worker in state.tasks:
            state.resume_event.set()
        else:
            state.resume_event = asyncio.Event()
        self._queue_worker("reassert", state.reassert_generation, self._reassert_repeatedly, 0)

    async def _refresh_panel(self, probe=None):
        """Follow the display if it changes underneath us."""
        if state.unloading:
            return
        # Metadata reads can each hit a subprocess timeout. Do not delay user
        # mutations while probing; only the resulting state/atom commit locks.
        if probe is None:
            probe = await _offload(vibrance_panel.probe_panel)
        async with state.mutation_lock:
            if state.unloading:
                return
            was_external = state.public["external_display"]
            changed = vibrance_panel.configure_for_panel(probe)
            if state.public["external_display"]:
                if changed:
                    decky.logger.info(f"{LOG} external display - standing down")
                Plugin._invalidate_sdr_jobs()
                Plugin._invalidate_hdr_jobs()
                # Also retries a failed release on later stable polling ticks.
                await _offload(vibrance_effects.release_sdr)
                await _offload(vibrance_effects.release_look, "hdr")
                return
            if not changed:
                return
            decky.logger.info(f"{LOG} internal panel configuration changed")
            if was_external:
                # Capture the internal value before applying our setting again.
                # If docking failed to release our last write, keep its original
                # baseline instead of adopting that write as the restore value.
                # A failed read says nothing, so it keeps the known baseline,
                # and a value of ours - written or a lost write that landed -
                # is not one either.
                live = await _offload(vibrance_x11.read_float_atom, ATOM_SDR_WIDENESS)
                if vibrance_effects.sdr_foreign(live, vibrance_effects.sdr_ours()):
                    state.sdr_baseline = live
            await self._apply_both()

    async def _apply_both(self, retry_sdr=False, retry_hdr=False):
        """Put both halves where the settings say; retry_* keeps a pending
        look instead of rebuilding it."""
        if state.public["sdr_enabled"]:
            await _offload(vibrance_effects.apply_sdr)
            if retry_sdr:
                self._retry_pending_looks("sdr")
            else:
                self._schedule_look("sdr")
        else:
            await _offload(vibrance_effects.release_sdr)
        if state.public["hdr_enabled"] and state.public["hdr_supported"]:
            if retry_hdr:
                self._retry_pending_looks("hdr")
            else:
                self._schedule_look("hdr")
        else:
            if state.public["hdr_enabled"]:
                Plugin._invalidate_hdr_jobs()
            await _offload(vibrance_effects.release_look, "hdr")

    async def _panel_tick(self) -> bool:
        """Check the session and the display; True when both were healthy.

        A replaced or vanished gamescope - or a reconnect that ended in an
        exception - is connected afresh; an incomplete probe waits for the next
        tick, since a slow xprop is no verdict on the session.
        """
        result = await _offload(vibrance_panel.monitor_probe)
        session_identity = await _offload(vibrance_x11.session_identity)
        changed_session = (state.session_identity is not None and session_identity is not None
                           and session_identity != state.session_identity)
        count, probe = result if result is not None else (None, None)
        if count == 0 or changed_session or state.disconnected:
            async with state.mutation_lock:
                Plugin._disconnect()
            await self._start_when_ready(start_monitor=False)
            return False
        if result is None:
            return False
        await self._refresh_panel(probe)
        async with state.mutation_lock:
            self._retry_settings_save()
        return True

    async def _monitor_loop(self):
        prev_mono = time.monotonic()
        prev_boot = _boottime()
        logged = {}  # stage -> failure last logged, so a lasting one is logged once
        since_watch = 0.0
        since_panel = 0.0
        while True:
            stage = "panel"
            try:
                await asyncio.sleep(SUSPEND_POLL_S)
                mono, boot = time.monotonic(), _boottime()
                elapsed = mono - prev_mono
                asleep = (boot - prev_boot) - (mono - prev_mono)
                prev_mono, prev_boot = mono, boot
                if asleep > SUSPEND_SLACK_S:
                    decky.logger.info(
                        f"{LOG} resumed after ~{asleep:.0f}s suspended")
                    self._schedule_reassert()
                    # The display may have changed while asleep (docked, say):
                    # look at it on the next tick, before any watch runs.
                    since_watch, since_panel = 0.0, PANEL_CHECK_INTERVAL_S
                    continue
                since_watch += elapsed
                since_panel += elapsed
                if since_panel >= PANEL_CHECK_INTERVAL_S:
                    since_panel = 0.0
                    if not await self._panel_tick():
                        continue
                    logged.pop(stage, None)  # healthy again: log its next failure
                if state.public["external_display"]:
                    continue
                if since_watch >= WATCH_INTERVAL_S:
                    since_watch = 0.0
                    stage = "watch"
                    await self._watch_tick()
                    logged.pop(stage, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if logged.get(stage) != str(e):  # a lasting failure repeats every tick
                    decky.logger.error(f"{LOG} monitor failed: {e}")
                    logged[stage] = str(e)
                await asyncio.sleep(2.0)

    async def _main(self):
        decky.logger.info(f"{LOG} startup v{updater.plugin_version()}")
        state.unloading = False
        state.settings_loaded = state.public["settings_loaded"] = False
        state.settings_dirty = False
        vibrance_x11.timeout = 3.0
        worker = vibrance_host.native_python() or "in-process"
        decky.logger.info(f"{LOG} LUT worker: {worker}")
        await _offload(vibrance_lutfile.wait_for_writer)
        if state.unloading:
            return
        vibrance_lutfile.disabled = False
        state.workers = {}
        state.jobs = {}
        state.due = {}
        state.tasks = []
        state.resume_event = None
        state.disconnected = True
        state.public["ready"] = False
        state.public["reason"] = "starting up"
        state.panel_cache_key = None
        state.panel_cache_complete = False
        try:
            await _offload(updater.ssl_context)
        except Exception as e:
            decky.logger.warning(f"{LOG} update checks unavailable: {e}")
        if not await self._load_settings():
            return
        state.public["settings_error"] = ""
        state.public["reason"] = "starting up"
        # _setting() clamps values an older build saved outside today's range
        # to the widest one; vibrance_panel.configure_for_panel narrows them once it knows.
        for key, fallback in DEFAULT_SETTINGS.items():
            state.public[key] = _setting(key, fallback)
        state.settings_loaded = state.public["settings_loaded"] = True
        # Persist repaired/migrated fields even if no gamescope session arrives.
        self._schedule_save()

        # Decky starts plugins as a system service, so we can easily be up
        # before the session is. Finding nothing here is a race, not a verdict,
        # so the rest of startup waits in a task rather than giving up until
        # somebody reinstalls the plugin.
        Plugin._track(asyncio.create_task(self._start_when_ready()))

    async def _load_settings(self) -> bool:
        """Read the settings file until it loads; False when unloading first.

        A file that cannot be opened is retried, never replaced; one whose
        content cannot parse is set aside and the plugin starts from defaults.
        """
        logged = None
        while not state.unloading:
            try:
                await _offload(vibrance_settings.read, settings)
                return not state.unloading  # an unload may have come during the read
            except vibrance_settings.SettingsUnreadable as exc:
                # Content that does not parse never will; waiting would leave
                # the plugin dead. Keep it beside the file, start from defaults.
                try:
                    # The name only: the full path carries the user's home directory.
                    aside = os.path.basename(await _offload(vibrance_settings.set_aside, settings))
                    decky.logger.warning(f"{LOG} settings were unreadable ({exc}); "
                                         f"kept as {aside}, starting from defaults")
                    state.public["settings_notice"] = (
                        f"The saved settings could not be read, so defaults are in use. "
                        f"The old file is kept as {aside}.")
                    continue
                except OSError as move:
                    failure = f"{exc}; could not set the file aside: {move}"
            except Exception as exc:
                # Defaults are not a safe replacement for a file we could not
                # read. Keep the file intact and recover from transient I/O.
                failure = exc
            message = f"could not load settings: {failure}"
            state.public["reason"] = message
            state.public["settings_error"] = message
            if message != logged:  # retried every second; log each new reason once
                decky.logger.warning(f"{LOG} {message}")
                logged = message
            await asyncio.sleep(SETTINGS_RETRY_S)
        return False

    @staticmethod
    def _disconnect():
        state.disconnected = True
        state.public["ready"] = False
        state.public["reason"] = "gamescope disconnected; looking for a new session"
        Plugin._invalidate_sdr_jobs()
        Plugin._invalidate_hdr_jobs()
        state.panel_cache_key = None
        state.panel_cache_complete = False
        state.foreign_sdr_seen = None
        state.foreign_active_ticks = 0
        # A failed read is not proof that the X server restarted. Retain
        # ownership until startup compares the socket identity.

    @staticmethod
    def _forget_session_ownership():
        state.sdr_baseline = None
        state.last_sdr_written = state.last_sdr_attempted = None
        state.hdr_look_path = state.sdr_look_path = None
        state.hdr_look_baseline = state.sdr_look_baseline = None
        state.public["sdr_look_saturation"] = None
        state.public["hdr_applied_saturation"] = None
        for key in ("sdr_applied", "hdr_applied", "sdr_look_applied"):
            state.public[key] = False

    async def _start_when_ready(self, start_monitor=True):
        """Connect to gamescope, then (once) start the monitor.

        The monitor starts even if connecting failed with an exception: it is
        what notices the plugin is still disconnected and tries again.
        """
        try:
            await self._connect()
        finally:
            if start_monitor and not state.unloading:
                Plugin._track(asyncio.create_task(self._monitor_loop()))

    async def _connect(self):
        if not await self._wait_for_gamescope():
            return
        async with state.mutation_lock:
            if state.unloading:
                return
            session_identity = await _offload(vibrance_x11.session_identity)
            preserve = await self._adopt_session(session_identity)

            # Both branches of buildSDRColorimetry are usable; they just start
            # from different places. Do this before exposing the controls.
            panel_changed = await _offload(vibrance_panel.configure_for_panel)

            if (not state.public["hdr_supported"]
                    and state.public["hdr_enabled"]):
                state.public["hdr_enabled"] = False
                Plugin._invalidate_hdr_jobs()
                self._schedule_save()

            # Recorded only now: if anything above raised, a retry must still
            # see this as a new session rather than a preserved one.
            state.session_identity = session_identity
            state.connect_ours = ()
            state.disconnected = False
            state.public["ready"] = True
            state.public["reason"] = "ready"

            # Starting while already docked used to run all three apply paths.
            # Do not apply saved settings until the internal panel comes back.
            if not state.public["external_display"]:
                await self._apply_both(retry_sdr=preserve and not panel_changed, retry_hdr=preserve)

        # Same ladder as after a resume: the session can still be settling.
        if not state.public["external_display"]:
            self._schedule_reassert()

    async def _wait_for_gamescope(self) -> bool:
        """Wait until gamescope's atoms stop appearing; False when unloading."""
        delay = GAMESCOPE_RETRY_FIRST_S
        waited = False
        while not state.unloading:
            while not await _offload(_pick_display):
                if not waited:
                    decky.logger.info(f"{LOG} no gamescope atoms yet - waiting")
                    waited = True
                state.public["reason"] = ("waiting for gamescope to come up" if shutil.which(
                    "xprop", path=vibrance_x11.system_env()["PATH"]) else "xprop is missing; install xorg-xprop")
                await asyncio.sleep(delay)
                delay = min(delay * 2, GAMESCOPE_RETRY_MAX_S)
            previous = -1
            for _ in range(GAMESCOPE_SETTLE_TRIES):
                count = await _offload(vibrance_x11.count_gamescope_atoms, vibrance_x11.DISPLAY)
                if count == 0 or count == previous:
                    break
                previous = count
                await asyncio.sleep(GAMESCOPE_SETTLE_S)
            if count > 0:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, GAMESCOPE_RETRY_MAX_S)
        if state.unloading:
            return False
        decky.logger.info(f"{LOG} gamescope settled at {previous} atoms")
        return True

    async def _adopt_session(self, session_identity) -> bool:
        """Decide what of ours survives on this server and what to restore.

        Returns whether this is the session we already knew. Runs under the
        mutation lock, before anything is written.
        """
        previous_session = state.session_identity
        preserve = session_identity is not None and session_identity == previous_session
        # Our values, kept past the reset below: a changed identity can
        # still be a server we wrote to (a reconnect that moved between
        # gamescope's servers), so the atom there may hold one of them.
        # A failed attempt has already forgotten them, so they are carried
        # to its retry.
        ours = vibrance_effects.sdr_ours(*state.connect_ours)
        state.connect_ours = tuple(dict.fromkeys(ours))  # retries repeat them
        known_baseline = state.sdr_baseline
        if not preserve:
            Plugin._forget_session_ownership()
        # Remember what was there before we touch anything, so switching the
        # plugin off puts the display back rather than to our idea of default.
        if state.public["sdr_enabled"] and (previous_session is None
                                             or session_identity is None):
            # A fresh process: _forget_session_ownership cleared the
            # baseline, and the atom may hold our own write from before the
            # reload; an unreadable identity proves nothing either way. A
            # replaced server (gamescope restarted) cannot hold one, so
            # there the atom is read below like any other value.
            decky.logger.info(f"{LOG} SDR was already on; no baseline to keep")
        elif not state.public["sdr_applied"] and state.last_sdr_attempted is None:
            # Nothing of ours is on this server - also when SDR was
            # switched on while disconnected - so the atom is the value
            # to restore. A failed read leaves the baseline as it is: the
            # known one on this server, none on a new one, where it cannot be
            # told from an absent atom and restoring gamescope's default is right.
            live = await _offload(vibrance_x11.read_float_atom, ATOM_SDR_WIDENESS)
            if vibrance_effects.sdr_foreign(live, ours):
                state.sdr_baseline = live
                decky.logger.info(f"{LOG} SDR baseline {live:.3f}")
            elif live is not None:
                # The atom shows our own write, so this is a server we
                # used; what we found there before is still the restore value.
                state.sdr_baseline = known_baseline
        await _offload(vibrance_effects.capture_existing_looks, preserve)
        return preserve

    async def _unload(self):
        # Decky kills a plugin still alive five seconds after SIGTERM. Keep
        # unavailable X servers from consuming that entire restore window.
        vibrance_x11.timeout = UNLOAD_XPROP_TIMEOUT_S
        # Nothing here gives the event loop back, not even to wait for the
        # mutation lock. Once Decky closes its end of the plugin socket, its
        # listener in this process spins on the EOF without ever yielding, so
        # anything awaited here would only end in the SIGKILL (seen on an Odin
        # 3: Decky 3.3.0 under FEX; SteamDeckHomebrew/decky-loader issue 975).
        # A holder of the lock is parked on an executor call: these flags stop
        # its writes, and its task is cancelled below before it resumes.
        state.unloading = True
        vibrance_lutfile.disabled = True
        Plugin._invalidate_sdr_jobs()
        Plugin._invalidate_hdr_jobs()
        # A download thread would keep the process past Decky's five seconds
        # and leave its .part file behind.
        cancel_downloads()
        # The settings go first: a reload cannot bring back an edit whose
        # debounce has not run yet, and a slow X server must not hold them
        # up. _persist leaves the file alone if startup has not read it.
        for step in (Plugin._persist, vibrance_effects.release_sdr,
                     lambda: vibrance_effects.release_look("hdr")):
            try:
                step()
            except Exception as exc:
                decky.logger.error(f"{LOG} unload step failed: {exc}")
        # A task cannot write after the restore: it meets the cancellation at
        # its next await, the one it is parked on if it holds the lock.
        for task in state.tasks:
            task.cancel()
        state.tasks = []
        decky.logger.info(f"{LOG} unloaded")

    async def _uninstall(self):
        # Decky's shutdown has just run _unload; repeating it would spend the
        # five-second stop budget restoring and saving a second time.
        if not state.unloading:
            await self._unload()
        # A cancelled executor call may still be finishing a file. Wait for the
        # writer lock after disabling new writes so nothing can recreate a LUT
        # behind the cleanup pass. Not awaited, for the reason in _unload.
        vibrance_lutfile.wait_for_writer()
        # The generated LUTs live outside the plugin directory the loader is
        # about to delete.
        for look in vibrance_effects.LOOKS.values():
            for slot in (0, 1):
                try:
                    os.unlink(os.path.join(vibrance_effects.runtime_dir(),
                                           f"{look['prefix']}-{slot}.cube"))
                except OSError:
                    pass
