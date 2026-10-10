# Changelog

All notable changes to Decky Vibrance HDR, newest first.

## [1.1.0] - 2026-10-10

### Fixed

#### Controls and menu

- Fixed the "Backend unavailable - python exception" message that could prevent
  the plugin menu from working.
- Sliders now keep your latest selection instead of jumping back to an older
  value. Closing and reopening the menu no longer brings back outdated changes.
- Rapid slider moves and repeated toggles no longer create a long queue of work.
  Turning an effect off or resetting settings cancels changes you no longer want.
- Connection problems and failed changes are shown more clearly, and the plugin
  retries operations that could not be completed.

#### Settings and display recovery

- Settings are saved more reliably, including when Decky stops during startup.
  Slow or failed saves no longer hold up the controls or prevent a reset.
- Improved recovery after sleep, temporary display connection problems and
  changes between the built-in screen and an external monitor.
- Improved detection of the panel's color capabilities and HDR support.
- Disabling the plugin restores the previous display settings more reliably,
  including when another tool has changed them while the plugin was active.
- Returning saturation to neutral no longer leaves an older color effect active.

#### Updates and installation

- Improved update detection, including preview versions, and made downloads
  more reliable on slow connections.
- Added stricter checks for update files. Failed downloads or packaging attempts
  preserve the previous usable file.
- Fixed handling of download folders and filenames containing special characters.

### Performance improvements

- Much faster controls on Armada devices running Decky through emulation.
  On Odin 3, measured saturation changes took about **0.6 seconds for HDR** and
  **0.7 seconds for SDR**. Turning an effect off during a build took **41–121 ms**.
  Results may vary by device and workload; ordinary SteamOS keeps its existing
  execution path.
- Faster color-LUT generation and reuse of recent results when returning to a
  previous saturation value. LUT resolution and color calculations are unchanged.
- Fewer repeated display checks, unnecessary saves and rebuilds, leaving more
  room for the controls to respond to your next change.

### Build and quality checks

- Updated build dependencies, expanded automated tests and improved checks for
  the installable ZIP. Packages always include a freshly built menu.
- Added the required license and source files for the bundled Decky API library.
- Validated with **275 automated tests**, build and package checks, and device
  checks on SteamOS and Armada. These checks do not replace testing every game
  or display setup.

## [1.0.2] - 2026-09-14

### Fixed

- Failed HDR and SDR look builds or replacements are retried by the monitor.
  Requested saturation is tracked separately from the last successful LUT, so
  an older working file no longer counts as a completed slider change.
- SDR, SDR look and HDR errors are kept independently. A successful operation
  on one control no longer hides another control's failed apply or reset.
- Each SDR enable captures the current baseline before taking control. Enabling
  again after a failed release preserves the original baseline instead of
  capturing the plugin's own value.
- Missing SDR properties and failed initial SDR writes are retried. A missing
  property is recreated only after confirming gamescope is still reachable.
- Frontend mutations are serialized, pending edits remain visible, and outdated
  polling responses cannot overwrite newer slider values.
- The standard-gamut SDR look above 200% converts gamescope's generic source
  gamut to BT.2020 for ICtCp and back before clipping. Native white from EDID is
  adapted to D65; unreadable white metadata falls back to D65.

### Changed

- HDR status shows the saturation of the last LUT successfully sent, without
  claiming gamescope has confirmed loading it.
- Added backend recovery and colorimetry regressions, plus frontend state tests
  run by `npm test` and CI: 131 backend tests and 6 frontend tests.

### Validation

- Confirmed on Legion Go 2: SDR slider changes, SDR and HDR disable/enable,
  HDR saturation changes, suspend/resume, and external-display docking/undocking.
- The standard-gamut SDR look is covered by numerical regression tests; the
  Legion Go 2 wide-gamut panel does not use that path.

## [1.0.1] - 2026-08-09

### Fixed

- Starting the plugin while an external display is already active no longer
  applies the built-in panel's SDR value or either look to that display. The
  saved settings return after switching back to the internal panel.
- Rapid enable, disable and slider calls can no longer finish out of order and
  put back a setting that a newer call removed. LUT work is checked again at the
  point it reaches gamescope, so a build that finishes after disable, unload or
  docking is discarded instead of restoring the old look.
- Disabling a look restores the value that was there before the plugin took
  control. A look owned by another plugin is left alone, and a failed release
  keeps enough state to retry rather than forgetting what still needs cleanup.
- A failed LUT replacement no longer loses ownership of the previous working
  file. The old look remains tracked and can still be released cleanly.
- The updater offers a download only when the release contains the exact
  `DeckyVibranceHDR-<version>.zip` asset, instead of accepting a missing or
  unrelated attachment.

### Changed

- Stable panel identification is cached. Normal monitoring now checks only
  whether the active display changed instead of repeatedly reading and parsing
  the same EDID every five seconds.
- A release failure is shown in the plugin panel instead of being visible only
  in the backend log.
- Unload and uninstall stop new LUT writes and wait for any writer already in
  progress before generated files are removed.

### Internal

- Backend tests up to 118 from 108, covering concurrent mutations, stale LUT
  workers, look ownership, startup while docked, panel caching and update asset
  validation.
- Test files are closed deterministically, allowing the suite to run with Python
  warnings promoted to errors.

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

Both end at 300%, but not by the same route. On a wide-gamut panel the atom
carries all of it. On a standard one it carries the first hundred points and
gives out there, because `cfit()` clamps it, so the rest comes from a look on
the SDR path - `GAMESCOPE_COLOR_LOOK_G22`, generated in the gamma 2.2 domain by
the same ICtCp code as the HDR half.

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
