# Changelog

## 1.0.0

Initial release.

Saturation control for both SDR and HDR content under gamescope, in one plugin.

**SDR** writes `GAMESCOPE_COLOR_SDR_GAMUT_WIDENESS`, which lerps the source
colorimetry between Rec.709 and the panel's native gamut, and extrapolates past
it above 1.0.

**HDR** has no equivalent dial: the PQ path calls `buildPQColorimetry()`, which
zeroes the gamut remap on purpose. What it does accept is a look, so the plugin
generates a `.cube` LUT and hands it to `GAMESCOPE_COLOR_LOOK_PQ`.

The LUT is applied to values that are still PQ-encoded, before linearisation, so
the adjustment is computed in ICtCp rather than in place — scaling distance from
grey in PQ would drag lightness and hue with it, worst in exactly the bright
saturated areas HDR exists for.

Both sliders bottom out at 100%: this is a vibrance control, not a desaturation
one. The ceilings differ because the mechanisms do - SDR runs to 300%, since
nothing in gamescope clamps that lerp, while the HDR LUT stops at 150% because
past about 130% it hard-clips channels at the gamut boundary and takes saturated
detail with them. Each slider says where its own range stops being free.

The HDR section is not drawn at all on a panel whose CTA-861 block does not
advertise SMPTE ST 2084. The look sits on the PQ path, so on such a display it
could never be reached. This is read from the EDID rather than from
`GAMESCOPE_HDR_OUTPUT_FEEDBACK`, whose meaning changed between gamescope builds;
what the panel claims to support does not move underneath us.

100% is neutral on both branches of `buildSDRColorimetry()`, but not at the same
atom value: 1.0 on a wide-gamut panel, 0.0 on any other. The slider keeps one
meaning and the mapping follows the panel, chosen by gamescope's own
`BIsWideGamut()` test against the published EDID.

A wide-gamut panel gets 100-300%, all of it from the atom. A standard one gets
100-400%: the atom carries the first hundred points and gives out there, because
`cfit()` clamps it, so the rest comes from a look on the SDR path -
`GAMESCOPE_COLOR_LOOK_G22`, generated in the gamma 2.2 domain by the same ICtCp
code as the HDR half.

Everything is applied to the built-in panel only. An external display is left
alone and the settings come back when it is unplugged, followed live through
`GAMESCOPE_DISPLAY_IS_EXTERNAL` rather than only at startup.

Startup waits for gamescope's atom count to stop growing before writing
anything, then puts the values back on the same staggered ladder the resume path
uses: a look written into a half-built session is discarded when it finishes,
which showed up as the atom applying and the LUT going missing.

100% loads no LUT at all. The HDR LUT is rebuilt only once the slider stops
moving, since a 33³ build takes about a third of a second.

Both halves are re-applied after a resume from suspend, detected by the gap that
opens between `CLOCK_MONOTONIC` and `CLOCK_BOOTTIME` while the machine sleeps,
and pushed back several times because gamescope can clobber them during its own
re-initialisation.

The same watch puts the SDR value back whenever anything drops it. One
disagreement is gamescope re-initialising and heals itself silently; only a run
of them is reported as vibrantDeck writing the same atom, so a plugin conflict
is no longer confused with an ordinary reset.
