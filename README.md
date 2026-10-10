<div align="center">

<img src="docs/logo.png" alt="DeckyVibranceHDR" width="760">

[![Release](https://img.shields.io/github/v/release/Rayekkk/DeckyVibranceHDR?style=for-the-badge&label=release&color=C2410C&labelColor=141417)](https://github.com/Rayekkk/DeckyVibranceHDR/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/Rayekkk/DeckyVibranceHDR/total?style=for-the-badge&label=downloads&color=15803D&labelColor=141417)](https://github.com/Rayekkk/DeckyVibranceHDR/releases)
[![Device](https://img.shields.io/badge/device-any_gamescope_panel-6E40C9?style=for-the-badge&labelColor=141417)](#requirements)
[![Requires](https://img.shields.io/badge/requires-Decky_Loader-0969DA?style=for-the-badge&labelColor=141417)](https://decky.xyz)
[![License](https://img.shields.io/github/license/Rayekkk/DeckyVibranceHDR?style=for-the-badge&label=license&color=424A53&labelColor=141417)](LICENSE)

**Saturation control for both SDR and HDR content under gamescope, in one plugin.**
Two sliders: one for the Steam interface and ordinary games, one for content actually running in HDR.

[Features](#features) · [Requirements](#requirements) · [Installation](#installation) · [Usage](#usage) · [How it works](#how-it-works) · [Troubleshooting](#troubleshooting)

</div>

<!-- Screenshots go here once they exist. Two columns keeps a 16:10 capture
     from swallowing the page - it renders at half width, so half the height.

| | |
|---|---|
| ![SDR](docs/panel1.jpeg) | ![HDR](docs/panel2.jpeg) |
-->

---

## Features

| | |
|---|---|
| **Two independent halves** | SDR affects the Steam interface and non-HDR games; HDR affects only games actually running in HDR |
| **SDR runs 100-300%** | 100% is neutral. The same range on every panel, whatever has to happen underneath to deliver it |
| **HDR runs 100-150%** | A ceiling set by the maths rather than by gamescope |
| **Both sliders show their own limit** | Each marks where its range stops being free, so you see it coming rather than walk into it |
| **It survives a suspend** | gamescope forgets these settings when it re-initialises, so the plugin watches for a resume and puts both halves back |
| **Settings are restored** | Turning a half off restores its captured baseline; failed restores remain visible and are retried while the plugin is running |
| **Neither slider goes below 100%** | Both bottom out at neutral |
| **Built-in panel only** | The plugin stands down while an external display is connected |
| **The HDR half can be absent** | Not drawn at all on a panel whose EDID never mentions PQ, since it could never take effect there |
| **A short pause when a slider settles** | Building the HDR lookup table takes about a fifth of a second, so it happens once you stop moving, and the panel says so |

---

## Requirements

| Requirement | Details |
|---|---|
| Compositor | gamescope in Game Mode, driving the built-in panel |
| Plugin loader | [Decky Loader](https://decky.xyz) |
| Privileges | none, runs as the normal user |

> [!NOTE]
> **Not tied to one handheld.** Which mapping applies is read from the panel's EDID rather
> than from the model name, so any display gamescope drives is handled on its own terms.
> Confirmed on a Legion Go 2 (OLED, wide gamut, HDR) and a Legion Go S (LCD, standard
> gamut), where the HDR half does not appear at all because the panel never declares PQ.

---

## Installation

**1.** Install [Decky Loader](https://decky.xyz) if you haven't already.
**2.** Download `DeckyVibranceHDR-x.x.x.zip` from the [Releases](../../releases) page.
**3.** In Gaming Mode, open the **Quick Access Menu** (the `…` button).
**4.** Open the Decky menu, scroll to the bottom, then **Developer → Install Plugin from ZIP**.
**5.** Select the downloaded zip.

<details>
<summary><b>Building from source</b></summary>

<br>

Requires Node.js 18+.

```bash
git clone https://github.com/Rayekkk/DeckyVibranceHDR
cd DeckyVibranceHDR

npm install
npm run build      # bundles src/index.tsx into dist/
npm run package    # produces DeckyVibranceHDR-<version>.zip
```

Then install the resulting zip through Decky's **Install Plugin from ZIP**, which is the
supported path and avoids permission problems.

</details>

---

## Usage

Open the **Quick Access Menu** and tap the plugin icon. There are two toggles and two
sliders, one pair per half, and nothing else to configure.

Turning a half off restores the settings captured when it took control. SDR captures the
current value on each enable. If SDR was already enabled at plugin startup and the old
baseline is unknown, disabling it returns gamescope to its native-gamut default.

Failed changes are retried automatically. SDR and HDR keep separate error messages, and the
HDR status shows the last LUT successfully sent, which can differ from the requested slider
value while a retry is pending. Sending a LUT is not confirmation that gamescope loaded it.

---

## How it works

gamescope exposes its controls as X11 properties, called *atoms*. Both halves of this plugin
work by writing to them; what differs is how much gamescope is willing to give.

### SDR

This is the easy half, because a dial already exists. The complication is that it does not
mean the same thing on every panel, and sorting that out is most of the work.

`GAMESCOPE_COLOR_SDR_GAMUT_WIDENESS` moves the source colorimetry between Rec.709 at `0.0`
and the panel's native gamut at `1.0`. On a wide-gamut panel that runs in u'v' and nothing
clamps the parameter, so above `1.0` it keeps going in the same direction and places the
primaries outside the panel. It is a single float, and writing it is the whole feature.

That is one of two branches gamescope can take. On the other, the same parameter is numbered
differently: `0.0` is native, `0.5` a generic wide gamut with smooth mapping, `1.0` the same
with harsh mapping, and gamescope clamps it there. Both are usable. The difference that
matters is where neutral sits: at `1.0` on the first branch, at `0.0` on the second.

So the slider always means the same thing, 100% is neutral and up is more saturated, while
the mapping underneath follows the panel:

| Panel | Slider | Atom | Look |
|---|---|---|---|
| **Wide gamut** | 100-300% | 1.0-3.0 | never written |
| **Standard gamut** | 100-300% | 0.0-1.0 over the first 100 points | 1.0-1.5 over the rest |

On the standard branch that clamp lands at what the slider calls 200%, so past it there is
nothing left for the parameter to push. The rest of the range is carried by
`GAMESCOPE_COLOR_LOOK_G22`, the SDR twin of the slot the HDR half uses, generated in the
gamma 2.2 domain by the same ICtCp code.

That look converts gamescope's generic wide-gamut source to BT.2020 before ICtCp and back
afterwards. The panel's white point is read from EDID and adapted to D65 during the
adjustment, with D65 as the fallback when white metadata is unavailable. This preserves
intensity and chroma direction before the final clipping step.

A wide-gamut panel never reaches that: its parameter covers the whole slider, and no look is
written at all.

> [!NOTE]
> Which branch applies is decided by gamescope's own `BIsWideGamut()` test, red `x > 0.650`
> and `y < 0.320`, run against the EDID published in `GAMESCOPE_DISPLAY_EDID_PATH`.
> Measured: a Legion Go 2 OLED is 0.6836/0.3154 and passes; a Legion Go S LCD is
> 0.6523/0.3408 and fails on `y` after passing `x` by a hair. An EDID that cannot be read
> falls back to the wide-gamut mapping.

### HDR

This is the hard half, because there is nothing to turn. In `create_color_mgmt_luts()` the PQ
path calls `buildPQColorimetry()`, which sets the input to BT.2020 and **zeroes the gamut
remap**. That is deliberate, not an oversight.

What the PQ path does accept is a *look*, gamescope's term for a colour lookup table it
applies to the image. `GAMESCOPE_COLOR_LOOK_PQ` holds a path to a `.cube` file used for HDR
content only, so this plugin generates that file itself.

> [!WARNING]
> Past about 130% an HDR boost hard-clips channels at the gamut boundary and takes saturated
> detail with them. That is why the ceiling sits at 150% rather than higher.

<details>
<summary><b>Why the maths is not trivial</b></summary>

<br>

The look is applied where it is, not where you would assume. From `color_helpers.cpp`:

```c
sourceColorEOTFEncoded = ApplyLut3D_Tetrahedral( *pLook, sourceColorEOTFEncoded );
sourceColorLinear      = calcEOTFToLinear( sourceColorEOTFEncoded, sourceEOTF, ... );
```

The LUT runs on values that are still **encoded**, before linearisation and before any
tonemapping or gamut mapping. On the HDR path that means its domain and range are both
PQ-encoded BT.2020 in `0..1`.

PQ is violently non-linear, so scaling distance from grey in place would drag lightness and
hue around, worst exactly in the bright saturated areas HDR exists for. Each LUT entry
therefore decodes PQ to linear, converts to **ICtCp** (ITU-R BT.2100), scales the two chroma
axes and leaves ICtCp intensity and chroma direction alone, then converts back.
Clipping at the gamut boundary can still change perceived lightness or hue.

Black maps to black by construction, which keeps gamescope's `bRaisesBlackLevelFloor` false.

</details>

### Staying applied

gamescope drops these atoms when it re-initialises, so the plugin watches for a resume by
comparing `CLOCK_MONOTONIC`, which stops while the machine sleeps, against `CLOCK_BOOTTIME`,
which does not. Both halves then go back several times over, because gamescope can clobber
them while it is still starting. A value dropped any other way is restored by the same watch
within a few seconds; only a value that keeps being overwritten is reported as a conflict.

---

## Troubleshooting

<details>
<summary><b>The panel says another plugin is fighting for the SDR atom</b></summary>

<br>

vibrantDeck writes the same `GAMESCOPE_COLOR_SDR_GAMUT_WIDENESS` atom. Two plugins pushing
one value produces behaviour that looks like a bug in both, so this plugin watches for it
and says so rather than letting you chase it.

Keep one of them enabled, not both. The HDR half does not conflict with anything, since
nothing else uses the PQ look slot.

</details>

<details>
<summary><b>There is no HDR half at all</b></summary>

<br>

Expected on a panel whose EDID does not advertise SMPTE ST 2084. The HDR half works by
handing gamescope a look on the PQ path, and on such a panel that path is never taken, so the
control would do nothing whatever it was set to.

That block can sit either in a CTA-861 extension or inside a DisplayID one, so both are
searched. An EDID that cannot be read at all is treated differently and leaves the half in
place: ignorance is not a no, but a complete EDID that never mentions PQ is.

</details>

<details>
<summary><b>Nothing happens after plugging in an external display</b></summary>

<br>

Also expected. Every number here was measured against the built-in panel, so the plugin
stands down while an external display is being driven and brings the settings back when it
goes away. `GAMESCOPE_DISPLAY_IS_EXTERNAL` is checked on a timer, so docking and undocking
are noticed without restarting anything.

</details>

---

## Development

```bash
npm ci
npm run build        # bundles src/index.tsx into dist/
npm run typecheck    # TypeScript check with no emit
npm test             # frontend ordering and optimistic-state regressions
npm run package      # rebuilds frontend and creates the installable zip
npm run verify:package # checks ZIP contents and the built Decky API contract
npm audit --audit-level=low

python -m unittest discover -s tests -v
```

`tests/test_logic.py` runs anywhere, including CI. It checks the PQ transfer round trip
against a 10-bit code value, that unity saturation is exactly identity, that black stays
black and greys stay neutral, that output never leaves the unit cube, and that the `.cube`
file has the layout gamescope's loader assumes, red changing fastest, which if wrong
scrambles the image rather than failing loudly.

`tests/test_recovery.py` additionally exercises failed applies and resets, baseline
recapture, missing SDR properties and the SDR source-gamut conversion. These are host-side
tests; they do not replace checking color output, suspend/resume and docking on a handheld.

`tests/test_audit_regressions.py` covers coalesced/cancelled LUT work, atomic settings,
disk failures, unload flush, session rediscovery, EDID validation and safe downloads.
`tests/test_full_audit.py` covers EDID replacement at a stable path, SDR restore values
through panel changes, inactive LUT slots, slow HTTP bodies, escaped filenames and
ZIP verification with optimized Python. Panel metadata is probed outside the control
lock, so slow reads do not block slider mutations. Unchanged EDID files remain cached.
`tests/test_quality_round3.py` covers other-server probe latency, shutdown probe
timeouts, accepted saturation persistence, SemVer precedence and quoted XDG paths.
Other X server probes run outside the control lock. During unload, gamescope
reachability and property operations share the shortened timeout.
`tests/test_quality_round4.py` covers redundant slider values, retries without
value changes, uninterrupted equivalent builds, overlapping update checks and
foreign LUT changes during replacement, monitoring and resume. An unchanged
healthy slider value does not save or reapply; failed settings and applies can
still be retried. Newly observed foreign LUTs are remembered before replacing
or reasserting the plugin's look, so disable/reset restores the latest baseline.
`tests/test_quality_round5.py` covers slow settings writes, generation-aware save
acknowledgements, unload behind an older write, single-query panel monitoring and
completed LUT reuse through SDR/HDR RPCs. Ordinary settings saves use immutable
snapshots outside the control lock, so disk latency does not delay disable/reset
or subsequent slider requests. The UI reports persistence failures on its next
state refresh, and unload waits for the final settings write. Panel monitoring
uses one root-property dump for session health and display metadata.
`tests/test_quality_round6.py` covers failed reasserts, pending neutral clears,
independent production LUT hashes, malformed ZIP names and X session recovery.
The backend retains restore values while rediscovering gamescope and preserves
them when the local X socket identity matches. A replaced socket, even at the
same DISPLAY, starts fresh ownership. Restore checks a known socket identity
immediately, including before the next monitor tick; a disconnected unidentified server cannot
receive a retained restore. Healthy LUTs survive a same-session reconnect without
rebuilding, while a changed panel configuration rebuilds the SDR look. Reassert
failures are reported, and retries respect a pending neutral request.
`tests/test_quality_round7.py` covers unload during startup's writer wait, TLS
setup and settings read. Shutdown preserves settings that startup has not yet
loaded; completing an interrupted startup cannot reenable LUT writes or launch
new background tasks. Once settings are loaded, shutdown still flushes edits.
The frontend queues one write at a time, coalesces unsent slider changes and repeated
toggle intents, and gives disable/reset priority. Reset also removes older unsent
toggles. Slow reads are retried with a visible connection error; a slow
write is shown as pending and retains ordering until Decky reports its result.

The LUT keeps its 33³ grid. A bounded cache holds two input ICtCp grids, keyed by transfer
and source white, so repeated saturation changes reuse the invariant part of the transform.
The two HDR and two SDR files also retain their completed LUTs. Returning to a
previous value can reuse the requested inactive slot if its parameters and file
identity still match. Four metadata records bound this cache; it stores no LUT
text in memory. New values still use the existing debounce and full-resolution build.
Transfer functions handle their own lower boundary, avoiding duplicate clamps
at every LUT node. Independent full-size hashes protect clipping and white-point
adaptation, and comparison with the prechange generator retains identical bytes.
Run `python scripts/benchmark_lut.py` for host timing, or add `--reference-ref <commit>`
to compare byte-for-byte output and paired timings with a local Git revision.

`lego_updater.py` originated in the author's other plugins. Audit fixes in this checkout
are maintained separately. The ZIP includes the exact @decky/api library source and
licence under `third_party/decky-api/`; React and @decky/ui use Decky's host globals.
Update HTTP bodies are read incrementally with time and size checks between reads.
Each socket idle wait is limited to five seconds; release metadata is limited to 1 MiB
and the installable ZIP to 32 MiB. Network setup and the final blocking read can extend
the elapsed time beyond the body deadline.
Release comparison follows SemVer precedence, retaining prerelease and build
suffixes in ZIP names. XDG download paths support quoted escapes and literal dollars;
invalid or unreadable configuration falls back to the host user's Downloads folder.
When update checks overlap across panel openings, only the newest check can
authorize its advertised ZIP for download.

---

## Credits

- [vibrantDeck](https://github.com/libvibrant/vibrantDeck) by Scrumplex, LGPL-3.0 - prior art for the SDR half, no code used; see [NOTICE](NOTICE)

---

## License

BSD 3-Clause - see [LICENSE](LICENSE). Third-party components are listed in [NOTICE](NOTICE).

---

<div align="left">

*Vibe coded with the help of [Claude](https://claude.ai) 🤖*

</div>

### Armada on ARM devices

When Decky runs as an x86-64 executable under FEX on an ARM system, the plugin
uses the native system Python for LUT calculation and native xprop through
posix_spawn. It detects this case from executable ELF architecture headers;
ordinary x86-64 SteamOS retains its existing execution path. No LUT resolution
or color math is changed. Obsolete native workers are killed and reaped.

On Odin 3, measured end-to-end saturation updates took about 0.57-0.59 seconds
for HDR and 0.66-0.69 seconds for SDR, including debounce. Disabling during an
active build took 41 ms for HDR and 121 ms for SDR. These are device measurements,
not worst-case guarantees. See [the device validation report](docs/FIX_ARMADA_RESPONSIVENESS_2026-10-10.md).
