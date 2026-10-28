# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/DeckyVibranceHDR

"""Seeded random use of the whole backend, checked against what it should end as."""

import asyncio
import random

from _fixtures import PluginCase  # installs the decky stand-in first
import vibrance_control as M

PQ, G22 = M.vibrance_x11.ATOM_LOOK_PQ, M.vibrance_x11.ATOM_LOOK_G22


class RandomUse(PluginCase):
    """Random mixes of switches, sliders, reset, docking, watch ticks and panel
    changes, with a fifth of the atom writes failing, must always end - once the
    monitor has run its healing ticks - with gamescope matching the settings,
    a disabled half restored and nothing left building or in error."""

    SEEDS = range(15)  # audit 13 ran 300 with and without failures; this keeps the run short
    FAIL_RATE = 0.2
    fail_rate = 0.0
    def write_float(self, atom, value):
        if self.rng_fail.random() < self.fail_rate: return False
        self.live_sdr = value; return True
    def write_string(self, atom, value):
        if self.rng_fail.random() < self.fail_rate: return False
        self.strings[atom] = value; return True

    async def run_seed(self, seed):
        rng = random.Random(seed)
        self.rng_fail = random.Random(seed + 7)
        self.fail_rate = self.FAIL_RATE
        self.live_sdr = M.state.sdr_baseline = 0.4
        p = self.plugin
        log = []
        external = False
        for step in range(rng.randint(3, 25)):
            op = rng.choice(["sdr_on", "sdr_off", "hdr_on", "hdr_off", "sdr_sat", "hdr_sat",
                             "reset", "dock", "undock", "watch", "reassert", "standard", "wide", "yield"])
            log.append(op)
            if op == "sdr_on": await p.set_sdr_enabled(True, rng.choice([None, 1.0, 1.5, 2.5, 3.0]))
            elif op == "sdr_off": await p.set_sdr_enabled(False)
            elif op == "hdr_on": await p.set_hdr_enabled(True, rng.choice([None, 1.0, 1.2, 1.5]))
            elif op == "hdr_off": await p.set_hdr_enabled(False)
            elif op == "sdr_sat": await p.set_sdr_saturation(rng.choice([1.0, 1.4, 2.0, 2.6, 3.0]))
            elif op == "hdr_sat": await p.set_hdr_saturation(rng.choice([1.0, 1.1, 1.3, 1.5]))
            elif op == "reset": await p.reset()
            elif op in ("dock", "undock"):
                external = op == "dock"
                key = ("external", external, None if external else "/e", None)
                await p._refresh_panel((external, key, None))
            elif op == "watch":
                await p._watch_tick()
            elif op == "reassert":
                async with M.state.mutation_lock:
                    await M._offload(M.vibrance_effects.reassert)
            elif op in ("standard", "wide"):
                async with M.state.mutation_lock:
                    before = M.vibrance_panel.panel_shape()
                    M.vibrance_panel.use_mapping(op == "wide")
                    changed = before != M.vibrance_panel.panel_shape()
                    if changed and not M.state.public["external_display"]:
                        await p._apply_both()
            if rng.random() < 0.5:
                await asyncio.sleep(0)
            if rng.random() < 0.3:
                await self.drain()
        await self.drain()
        self.fail_rate = 0.0
        if not M.state.public['external_display']:
            for _ in range(2 * M.SLOW_CHECK_EVERY):
                await p._watch_tick(); await self.drain()
        else:
            await p._refresh_panel((True, ('external', True, None, None), None)); await self.drain()
        s = M.state.public
        problems = []
        target, look = M.vibrance_effects.sdr_split(s["sdr_saturation"])
        if s["external_display"]:
            if abs(self.live_sdr - 0.4) > 1e-9: problems.append(f"external but SDR atom {self.live_sdr}")
            for atom in (PQ, G22):
                if self.strings.get(atom): problems.append(f"external but {atom}={self.strings.get(atom)}")
        else:
            if s["sdr_enabled"]:
                if abs(self.live_sdr - target) > 1e-9 or not s["sdr_applied"]:
                    problems.append(f"SDR on: atom {self.live_sdr} != {target} applied={s['sdr_applied']}")
            elif abs(self.live_sdr - 0.4) > 1e-9:
                problems.append(f"SDR off but atom {self.live_sdr}")
            for comp, atom, en, want in (("hdr", PQ, s["hdr_enabled"], s["hdr_saturation"]),
                                         ("sdr", G22, s["sdr_enabled"], look)):
                live = self.strings.get(atom)
                if en and want != 1.0:
                    sat = s["hdr_applied_saturation" if comp == "hdr" else "sdr_look_saturation"]
                    if not live or sat != want:
                        problems.append(f"{comp} look wanted {want}, atom {live}, recorded {sat}")
                elif live:
                    problems.append(f"{comp} look not wanted (en={en}, want={want}) but atom {live}")
        for k in ("generating", "sdr_generating"):
            if s[k]: problems.append(f"{k} stuck")
        for k in ("sdr_error", "sdr_look_error", "hdr_error", "settings_error"):
            if s[k]: problems.append(f"{k}={s[k]!r}")
        return log, problems

    async def test_random_use_always_heals_to_the_settings(self):
        failures = []
        for seed in self.SEEDS:
            M.state.reset()  # a fresh start per sequence
            M.state.public.update(ready=True, reason="ready")
            M.state.settings_loaded = True
            self.strings.clear()
            log, problems = await self.run_seed(seed)
            if problems:
                failures.append((seed, log, problems))
        self.assertEqual(failures[:3], [])
