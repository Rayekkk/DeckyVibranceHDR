<div align="center">

<img src="docs/logo.png" alt="DeckyVibranceHDR" width="760">

[![Release](https://img.shields.io/github/v/release/Rayekkk/DeckyVibranceHDR?style=for-the-badge&label=release&color=C2410C&labelColor=141417)](https://github.com/Rayekkk/DeckyVibranceHDR/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/Rayekkk/DeckyVibranceHDR/total?style=for-the-badge&label=downloads&color=15803D&labelColor=141417)](https://github.com/Rayekkk/DeckyVibranceHDR/releases)
[![Environment](https://img.shields.io/badge/environment-gamescope_Game_Mode-6E40C9?style=for-the-badge&labelColor=141417)](#requirements)
[![Requires](https://img.shields.io/badge/requires-Decky_Loader-0969DA?style=for-the-badge&labelColor=141417)](https://decky.xyz)
[![License](https://img.shields.io/github/license/Rayekkk/DeckyVibranceHDR?style=for-the-badge&label=license&color=424A53&labelColor=141417)](LICENSE)

**Adjust SDR and HDR saturation from the Steam overlay.**
Use separate controls for the Steam interface and ordinary games, and for games already running in HDR.

[Features](#features) · [Requirements](#requirements) · [Installation](#installation) · [Quick start](#usage) · [Technical details](#how-it-works) · [Troubleshooting](#troubleshooting)

</div>

---

## Features

Decky Vibrance HDR changes colour saturation on the built-in screen through
[gamescope](https://github.com/ValveSoftware/gamescope). SDR and HDR have separate
saved choices. Enabled settings continue working while the Quick Access Menu is closed.

| Control | What it does |
|---|---|
| **SDR saturation** | Adjusts the Steam interface and games not running in HDR, from 100–300% |
| **HDR saturation** | Adjusts games running in HDR, from 100–150%; shown when the panel is not known to lack HDR support |
| **Enabled** | Turns each effect on or off independently and releases its display adjustment when switched off |
| **Back to defaults** | Switches both effects off and returns both sliders to 100% |
| **Updates** | Checks for a release and downloads its installable ZIP; keeps progress and results when the menu is reopened |

The menu explains clipping limits and failed changes. Background checks reapply
enabled settings after detected gamescope/session recovery. Adjustments pause while
an external display is active and return when the built-in screen is active again.

## Requirements

| Requirement | Details |
|---|---|
| Environment | SteamOS or a compatible gamescope-based Game Mode, driving the built-in screen |
| Plugin loader | [Decky Loader](https://decky.xyz) |
| Display interfaces | gamescope's colour controls and `xprop`; panel metadata determines the SDR mapping and HDR availability |
| Privileges | Runs as the normal user; no root flag is required |

The plugin chooses its display mapping from panel information rather than the device
name. It does not enable HDR in a game or apply its built-in-screen settings to an
external monitor.

The latest fixes were checked on these devices:

| Device | Environment and coverage |
|---|---|
| **Legion Go 2** | Ordinary x86-64 SteamOS; wide-gamut SDR, HDR, real menu input and backend/display readback |
| **Odin 3** | Armada with Decky running through FEX; SDR/HDR colour tables, native ARM calculation and mounted menu handlers |

Earlier project checks also covered **Legion Go S** with a standard-gamut LCD.
Its panel does not advertise HDR, so the HDR section is hidden. It was not retested
in the latest fix audit. See [validation and device limits](#validation-and-device-limits)
for the exact scope of the latest checks.

## Installation

1. Install [Decky Loader](https://decky.xyz) if you do not already have it.
2. Download `DeckyVibranceHDR-<version>.zip` from [Releases](https://github.com/Rayekkk/DeckyVibranceHDR/releases). Choose the attached plugin ZIP.
3. In Game Mode, open **Quick Access Menu → Decky → Settings → Developer**.
4. Choose **Install Plugin from ZIP** and select the archive.
5. Open **Decky Vibrance HDR** in Decky.

Normal use needs no terminal commands. To make your own package, follow
[Development](#development).

**Version 1.1.1:** see [CHANGELOG.md](CHANGELOG.md) for the fixes in this version
and earlier releases. The release badge and download links follow published releases.

### Updating

1. Open **Updates → Check for updates**.
2. If a newer release is available, choose **Download v<version>**.
3. Wait for **Downloaded** and note the completed file path.
4. Follow the menu's instructions: uninstall the existing plugin in Decky while keeping its settings, then use **Developer → Install Plugin from ZIP** to install that file.
5. Reopen the plugin and check your saved choices.

**Downloading does not install the update.** Settings are stored outside the installed
plugin folder and are retained when that folder is replaced. Keep the saved settings
when uninstalling for an update.

Checks and downloads continue if you leave the panel. Reopening shows the pending
operation, result or error without starting another request. You can also download
and install the release ZIP manually. The updater offers published releases.

## Usage

### Quick start

1. Open **Decky Vibrance HDR** in the Quick Access Menu.
2. Turn on **Enabled** under **SDR saturation** or **HDR saturation**.
3. Start at **100%**, then increase **Saturation** until you like the result.
4. Let the change finish and check **Now**, the HDR **Last LUT sent** status and any error.

The slider shows your latest selection immediately. Applying it can take longer,
especially when a colour table is being built. The menu shows **building the LUT...**
while that work is running. Recent edits are grouped so an older selection does not
keep delaying the one you want.

### SDR saturation

SDR affects the Steam interface and games that are not using HDR. **100%** adds no
saturation boost from this plugin; larger values make colours more saturated.
These percentages are adjustment levels, not a measurement of the panel's gamut.

The current build provides **100–300%**. The useful range depends on the panel:
wide-gamut screens show a clipping warning above **200%**, while standard-gamut
screens using the additional colour table warn above **260%**. Beyond the warning,
saturated colours can lose detail. Use the message beside the slider as your guide.

### HDR saturation

HDR affects only content already running in HDR. The range is **100–150%**, with a
clipping warning above **130%**. It does not turn an SDR game into an HDR game.

Above 100%, the plugin builds a colour lookup table (LUT) for gamescope. **Now** is
your requested value; **Last LUT sent** is the last value successfully handed to
gamescope. They can differ while building or retrying. This status confirms the
handoff, not a physical measurement of the game's image. Build time varies with
the device and workload.

The section is hidden when complete panel information says HDR/PQ is unsupported.
If panel information is unavailable, the control may remain visible; its presence
alone is not proof that the game or screen is displaying HDR.

### Turning effects off and resetting

Turning **Enabled** off releases that effect and restores its captured display
setting. A newer colour table supplied by another tool is preserved. If SDR was
already enabled at startup and its earlier baseline is unknown, it returns to
gamescope's native-gamut default.

**Back to defaults** switches both effects off and sets both sliders to 100%.
It also cancels older changes that have not yet been sent. Restoration failures
stay visible and are retried while the plugin is running.

### Status and recovery

**Backend unavailable** describes a connection problem; **Plugin not ready** shows
why startup cannot finish. **Could not load settings** keeps controls paused and
preserves the saved file. **Settings not saved** means the latest choices could not
be written to disk. **Could not apply the setting** reports SDR/HDR display errors
separately.

Let an operation finish before judging the image. If a request is still waiting
for Decky, reopening the panel keeps that request's place rather than sending a
second copy. Use **Retry** when offered once the backend can respond again.

## Troubleshooting

| What you see | What to do |
|---|---|
| Backend unavailable or Plugin not ready | Read the reason, allow startup to finish and confirm Game Mode is active. Use Retry when offered. If Decky itself no longer responds, restart Decky; that also reloads its other plugins. |
| Could not load settings | Keep the saved file. Resolve the reported file-access or invalid-JSON problem; the plugin retries loading instead of saving defaults over it. |
| Settings not saved | Check the reported storage/permission error. The current selection may still be active, but it has not been confirmed saved. |
| Could not apply the setting | Read the SDR, SDR look or HDR error. Allow recovery to retry, then check the applied status. |
| Conflict or colours keep changing | Avoid overlapping colour controls such as vibrantDeck. Another tool can overwrite SDR settings or an SDR/HDR colour table; keep one tool controlling the same effect. |
| The HDR section is missing | Check whether the built-in panel advertises HDR/PQ. This is expected on the standard Legion Go S LCD. |
| HDR changes do not affect a game | Confirm the game is actually running in HDR and check Last LUT sent. The plugin does not enable a game's HDR mode. |
| External display → Standing down | Adjustments are paused for the external screen. Return to the built-in display to resume the saved choices. |
| building the LUT... or an older Last LUT sent value | Wait for the latest build. If it stays pending, check the error and backend connection; build time is not fixed across devices. |
| An update check or download failed | Read the error and try again, or download the attached plugin ZIP from Releases and install it through Decky. |
| Reloading takes several seconds | Decky 3.2.10 showed a loader shutdown issue during audits on both devices. Version 1.1.1 does not patch that loader issue. See the device report for the reproduced limitation. |

When reporting a problem, include the plugin version, device, SteamOS/Armada and
Decky versions, the affected control and its exact status/error.

## How it works

<details>
<summary><b>Display interfaces, colour tables and recovery</b></summary>

The plugin reads gamescope's panel metadata and adjusts X11 properties rather than
changing panel firmware.

| Path | Interface | Behaviour |
|---|---|---|
| SDR on a wide-gamut panel | `GAMESCOPE_COLOR_SDR_GAMUT_WIDENESS` | The scalar control carries the 100–300% range |
| SDR on a standard-gamut panel | The same scalar plus `GAMESCOPE_COLOR_LOOK_G22` | The scalar carries 100–200%; a gamma-2.2 colour table carries the rest |
| HDR | `GAMESCOPE_COLOR_LOOK_PQ` | A PQ colour table adjusts HDR content |

Both colour tables keep a **33³ grid**. HDR calculations use PQ-encoded BT.2020
and ICtCp to adjust chroma while preserving intensity and chroma direction before
clipping. SDR uses the corresponding gamma-2.2 path, source-gamut conversion and
panel white-point adaptation. See [vibrance_lut.py](vibrance_lut.py) for the maths.

Recent completed tables and invariant conversion work are cached. Generation runs
outside the control lock; obsolete jobs are cancelled, and files are published
atomically. Panel information is cached and refreshed when its identity changes.
Background monitoring detects session/display recovery and retries failed applies
or restorations while retaining ownership information.

On Armada systems with x86-64 Decky under FEX, executable architecture checks select
native system Python and `xprop` where supported. Ordinary x86-64 SteamOS keeps its
existing path. This changes execution overhead, not LUT resolution or colour maths.

Settings live in Decky's settings directory, outside the installed plugin folder.
Startup validates them before accepting edits; a failed read keeps the file intact.
The frontend keeps one outstanding read and one write at a time, coalesces unsent
slider edits and prioritises disable/reset over older unsent changes.

</details>

## Development

<details>
<summary><b>Build, test and package from source</b></summary>

Use Node.js **22** (the CI version), Python **3.10+** for backend tests, and a ZIP
tool: `zip` on Linux/macOS or 7-Zip on Windows.

```bash
git clone https://github.com/Rayekkk/DeckyVibranceHDR
cd DeckyVibranceHDR
npm ci
npm run typecheck
npm test
python -W error -m unittest discover -s tests -v
npm run package
npm run verify:package
npm audit --audit-level=low
```

**Package rebuilds the frontend** and creates `DeckyVibranceHDR-<version>.zip`.
Install that ZIP through Decky. The package includes the bundled `@decky/api`
source, license and source map; React and `@decky/ui` come from the Decky host.
See [NOTICE](NOTICE) for component provenance.

GitHub Actions uses Linux/Python 3.13 and Node.js 22 for backend/frontend tests,
version checks, dependency audit, typecheck, build and ZIP verification. It uploads
a build artifact; it does not publish a release. POSIX-specific tests skip on Windows.
To measure LUT generation locally, use `python scripts/benchmark_lut.py`.

</details>

### Validation and device limits

<details>
<summary><b>Audit reports and what was tested</b></summary>

- [Final Legion regression and renewed Odin validation](docs/FULL_AUDIT_2026-10-11_LEGION_REGRESSION.md): settings/startup fixes, ordinary SteamOS and Armada checks, final payload comparisons, menu/backend measurements and independent device verification.
- [Round-9 quality audit](docs/FULL_AUDIT_2026-10-10_ROUND9.md): frontend request ordering, saved settings and updater remount regressions.
- [Odin audit](docs/FULL_AUDIT_2026-10-10_ODIN3.md): blocking metadata reads, native-worker checks and Decky lifecycle diagnosis.
- [Earlier Armada performance work](docs/FIX_ARMADA_RESPONSIVENESS_2026-10-10.md): native ARM LUT and `xprop` execution.

The underlying fixes were checked on the devices before the version-only 1.1.1
bump. The latest Odin menu checks used the actual mounted controls' handlers;
Legion also used native simulated keyboard input. These are not physical gamepad,
colorimeter or HDR-game output checks. Sleep/wake and docking were not exercised
in the latest round. Automated tests and readable LUT files do not replace those
physical scenarios. Earlier report versions, hashes and timings are historical.

</details>

---

## Credits

- [vibrantDeck](https://github.com/libvibrant/vibrantDeck) by Scrumplex, LGPL-3.0 — prior art for the SDR path and resume detection; no code used. See [NOTICE](NOTICE).
- [gamescope](https://github.com/ValveSoftware/gamescope) and [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) — the display interfaces and plugin host.

## License

BSD 3-Clause — see [LICENSE](LICENSE). Third-party components, including the bundled
LGPL-2.1 `@decky/api`, are listed in [NOTICE](NOTICE).

---

<div align="left">

*Vibe coded with the help of [Claude](https://claude.ai) 🤖*

</div>
