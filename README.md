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
| **Everything is restored** | When a half goes off, the plugin unloads, or it is uninstalled, including the value the SDR control held before it was ever touched |
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

Turning a half off puts back exactly what was there before, so switching between them costs
nothing.

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
axes and leaves intensity alone, then converts back. That operation is what a saturation
slider is supposed to be, and ICtCp is the space built for it.

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
npm install
npm run build        # bundles src/index.tsx into dist/
npm run typecheck    # TypeScript check with no emit
npm run package      # builds the installable zip

python -m unittest discover -s tests -v
```

`tests/test_logic.py` runs anywhere, including CI. It checks the PQ transfer round trip
against a 10-bit code value, that unity saturation is exactly identity, that black stays
black and greys stay neutral, that output never leaves the unit cube, and that the `.cube`
file has the layout gamescope's loader assumes, red changing fastest, which if wrong
scrambles the image rather than failing loudly.

`lego_updater.py` is shared verbatim with all my other plugins, change it in one repo and
copy it to the others.

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
