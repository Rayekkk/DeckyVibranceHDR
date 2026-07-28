# Decky Vibrance HDR

Saturation control for **both SDR and HDR** content under gamescope, in one
plugin.

The two halves do the same thing by completely different means, because
gamescope only gives you a dial for one of them.

## SDR

`GAMESCOPE_COLOR_SDR_GAMUT_WIDENESS` lerps the source colorimetry between
Rec.709 at `0.0` and the panel's native gamut at `1.0`. On a wide-gamut panel
that lerp runs in u'v' and nothing clamps the parameter, so above `1.0` it keeps
going in the same direction and places the primaries outside the panel. It is a
single float, and writing it is the whole feature.

That is one of two branches, and the other one numbers itself differently:
`0.0` is native, `0.5` a generic wide gamut with smooth mapping, `1.0` the same
with harsh mapping - and `cfit()` clamps there. Both are usable. What moves is
where neutral sits and how much room is above it.

So the slider always means the same thing - 100% is neutral, up is more
saturated - and the mapping underneath follows the panel:

| panel | slider | atom | look |
|---|---|---|---|
| wide gamut | 100-300% | 1.0-3.0 | never written |
| standard gamut | 100-400% | 0.0-1.0 over the first 100 points | 1.0-2.0 over the rest |

On the standard branch `cfit()` clamps the atom at what the slider calls 200%,
so past that there is nothing left for it to push. The rest of the range is
carried by `GAMESCOPE_COLOR_LOOK_G22`, the SDR twin of the atom the HDR half
uses, generated in the gamma 2.2 domain by the same ICtCp code. The look is
expressed as a rate rather than a fraction of the range, so moving the top of
the slider does not quietly change what every setting below it asks for.

A wide-gamut panel never reaches that: its atom covers the whole slider, and no
look is written at all.

Which branch applies is decided by gamescope's own `BIsWideGamut()` test - red
`x > 0.650` and `y < 0.320` - run against the EDID published in
`GAMESCOPE_DISPLAY_EDID_PATH`. Measured: a Legion Go 2 OLED is 0.6836/0.3154 and
passes; a Legion Go S LCD is 0.6523/0.3408 and fails on `y` after passing `x` by
a hair. An EDID that cannot be read falls back to the wide-gamut mapping.

## HDR

There is no equivalent. In `create_color_mgmt_luts()` the PQ path calls
`buildPQColorimetry()`, which sets the input to BT.2020 and **zeroes the gamut
remap** — there is deliberately nothing to turn.

What the PQ path does accept is a *look*: `GAMESCOPE_COLOR_LOOK_PQ` holds a path
to a `.cube` file that gamescope applies to HDR content only. So this plugin
generates that file itself.

### Why the maths is not trivial

The look is applied where it is, not where you would assume. From
`color_helpers.cpp`:

```c
sourceColorEOTFEncoded = ApplyLut3D_Tetrahedral( *pLook, sourceColorEOTFEncoded );
sourceColorLinear      = calcEOTFToLinear( sourceColorEOTFEncoded, sourceEOTF, ... );
```

The LUT runs on values that are still **encoded**, before linearisation and
before any tonemapping or gamut mapping. On the HDR path that means its domain
and range are both PQ-encoded BT.2020 in `0..1`.

PQ is violently non-linear, so scaling distance from grey in place would drag
lightness and hue around — worst exactly in the bright saturated areas HDR
exists for. Each LUT entry therefore decodes PQ to linear, converts to **ICtCp**
(ITU-R BT.2100), scales the two chroma axes and leaves intensity alone, then
converts back. That operation is what a saturation slider is supposed to be, and
ICtCp is the space built for it.

Black maps to black by construction, which keeps gamescope's
`bRaisesBlackLevelFloor` false.

## What to expect

- **Two independent toggles.** SDR affects the Steam UI and non-HDR games; HDR
  affects only games actually running in HDR.
- **Neither slider goes below 100%.** Both bottom out at neutral. Below that the
  picture is being drained rather than lifted, and that is a different control.
- **The SDR range follows the panel** — 100–300% on a wide-gamut display,
  100–400% on a standard one, with 100% neutral either way. See above.
- **Only the built-in panel.** An external display is somebody else's panel and
  every number here was measured against this one, so plugging one in makes the
  plugin stand down and unplugging it brings the settings back. gamescope says
  which it is driving in `GAMESCOPE_DISPLAY_IS_EXTERNAL`; the check runs on a
  timer, so docking is noticed without a restart.
- **HDR runs 100–150%,** a ceiling set by the maths rather than by gamescope:
  past about 130% a boost hard-clips channels at the gamut boundary and takes
  saturated detail with them. Each slider warns you where its own range stops
  being free rather than pretending it does not.
- **The HDR section is not drawn at all** when the EDID does not advertise
  SMPTE ST 2084, since the look sits on the PQ path and could never be reached.
  That block can appear either in a CTA-861 extension or inside a DisplayID one,
  so both are searched. Only an EDID that cannot be read at all leaves the
  section in place: ignorance is not a no, but a complete EDID that never
  mentions PQ is.
- **A short pause wherever a LUT is involved.** A 33³ cube takes about a fifth
  of a second to build, so it is only rebuilt once the slider stops moving. Both
  halves say so while it happens.
- **100% loads no LUT at all.** Identity is not worth a file.
- **It survives a suspend.** gamescope forgets these atoms when it
  re-initialises, so the plugin watches for a resume — `CLOCK_MONOTONIC` stops
  while the machine sleeps and `CLOCK_BOOTTIME` does not — and puts both halves
  back, several times over, because gamescope can clobber them while it starts.
  A value dropped any other way is restored by the same watch within a few
  seconds; only a value that keeps being overwritten is reported as a conflict.
- **Everything is restored** when a toggle goes off, the plugin unloads, or it
  is uninstalled — including the value the SDR atom held before it was touched.

## Conflicts

vibrantDeck writes the same SDR atom. Two plugins fighting over one value
produces behaviour that looks like a bug in both, so this plugin watches for it
and says so in the panel. Keep one of them enabled, not both.

Its HDR half does not conflict with anything: nothing else uses the PQ look slot.

## Requirements

- gamescope in Game Mode, driving the built-in panel
- DeckyLoader

Runs as the normal user; no root.

## Installation

Download the zip from Releases, then in Decky: gear icon → Developer →
**Install Plugin from ZIP File**.

## Development

```
npm install
npm run build        # bundles src/index.tsx into dist/
npm run typecheck
npm run package      # builds the installable zip
python -m unittest discover -s tests -v
```

`tests/test_logic.py` runs anywhere, including CI. It checks the PQ transfer
round trip against a 10-bit code value, that unity saturation is exactly
identity, that black stays black and greys stay neutral, that output never
leaves the unit cube, and that the `.cube` file has the layout gamescope's
loader assumes — red changing fastest, which if wrong scrambles the image rather
than failing loudly.

## Licence

MIT.
