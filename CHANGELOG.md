# Changelog

All notable changes to Decky Vibrance HDR, newest first.

## [1.0.3] - Unreleased

### Fixed

- Preserve unread settings when unload interrupts startup; late completion of
  writer/TLS/settings initialization cannot reenable LUT writes or start tasks
  after shutdown. Normal shutdown still flushes accepted edits.
- Call zero-argument backend reads without the implicit Promise callback value;
  JSON encoding turned that value into a positional null and Decky rejected
  get_state, leaving the panel on "Backend unavailable - python exception".
- Coalesce unsent slider RPCs and prioritize disable/reset; preserve optimistic
  values across stale replies and QAM reopenings. Stop queued RPCs on plugin
  disposal. Poll one request at a time and show connection errors.
- Coalesce repeated unsent toggles and resets. Reset cancels obsolete toggle
  intents as well as sliders, while later controls and generic RPC side effects
  retain their ordering.
- Bound LUT workers and interrupt obsolete builds before publication, including
  jobs waiting for the file lock. Flush pending settings on unload.
- Save all settings in one atomic snapshot, validate stored/RPC values, and keep
  restore/reset functional when persistence fails. Retry dirty settings separately.
- Write settings snapshots outside the control lock, including toggles and reset;
  slow fsync no longer delays restoring the display or subsequent RPCs. Only the
  latest generation acknowledges persistence, and unload flushes the final values.
- Rediscover gamescope after a lost session, including HDR-only configurations,
  and reconfigure the panel before applying. Reject zero-atom startup and release
  unsupported HDR looks. Separate same-server and other-server conflict tracking.
  Coalesce resume ladders and shorten unavailable-server waits during unload.
- Preserve owned settings and foreign restore values across a transient probe
  failure on the same X socket. Detect replacement at the same DISPLAY, defer
  old restores on a different/unidentified server and avoid healthy LUT rebuilds
  on reconnect. Rebuild SDR when reconnect changes panel configuration.
- Probe other X servers outside the control lock; include gamescope reachability
  probes in the shortened unload timeout. Save accepted saturation values when
  the panel temporarily prevents enabling the control.
- Skip redundant saves, SDR writes and LUT rebuilds for an unchanged healthy
  slider value; retain retries after failed applies or saves and keep an
  equivalent build already in flight.
- Validate EDID headers, checksums and data boundaries; scan every PQ declaration.
  Read one EDID per panel classification and invalidate cache when its path or
  file identity changes. Probe panel metadata outside the mutation lock.
- Preserve SDR restore values across metadata refreshes and recapture them when
  returning to the internal panel. Choose an inactive LUT slot before generating.
- Remember newly observed foreign LUTs before replacing or reasserting an owned
  look, so disable/reset restores the latest foreign value. Dropped atoms and
  the plugin's own stale slots do not replace that restore value.
- Report failed LUT reasserts and retry them. Successful reassertion of an older
  look does not hide a failed replacement; a pending neutral request clears the
  plugin's old look instead of reasserting it after resume or an atom drop.
- Decode xprop path escapes, including UTF-8 octal bytes, and reject embedded NUL.
- Select the exact release ZIP, validate every HTTPS redirect and checked asset,
  preserve previous downloads on failure, and validate archives before replacing them.
  Use Decky's actual host user and avoid unnecessary privileged ownership changes.
  Read HTTP bodies incrementally, check time/size limits between reads and bound
  release metadata to 1 MiB.
- Reject ZIP entry names truncated by an embedded NUL before replacing a download.
- Compare releases using SemVer prerelease precedence and ignore build metadata
  for ordering. Preserve the complete release version in asset names. Decode
  quoted XDG download paths without expanding escaped literal dollars.
- Ignore superseded update checks when authorizing downloads, including checks
  started by a panel that has since closed.
- Rebuild before packaging, remove stale frontend output, preserve the old ZIP
  on packaging failure, and include @decky/api licensing and corresponding source.
  Keep runtime files when checkout ancestors resemble cache directory names and
  enforce ZIP validation even when Python optimization is enabled.

### Performance and validation

- On ARM hosts running Decky x86-64 through FEX, build LUTs with native Python
  and launch native xprop through posix_spawn. Cancel and reap obsolete workers;
  keep the existing generator and process-launch path on ordinary SteamOS.
  Odin 3 measurements: HDR 0.57-0.59 s, SDR 0.66-0.69 s; disabling during a
  build takes 41 ms HDR / 121 ms SDR. LUT mathematics and resolution are unchanged.
- Cache two invariant 33³ ICtCp input grids, with the source white and transfer in
  the key. Retain the original node values, file precision and grid resolution.
- Remove redundant lower clamps already handled by transfer functions, reducing
  LUT generation work while retaining byte-identical reference output.
- Reuse an unchanged completed LUT in the requested inactive slot when returning
  to its saturation. Keep only four metadata records; changed parameters, edited
  or missing files and cancelled requests cannot reuse an invalid slot.
- Reuse one root-property dump for monitor health and panel classification,
  eliminating two redundant xprop processes from an internal-panel check.
- Replace unused build preset plugins to remove vulnerable glob dependencies;
  add dependency auditing, backend/frontend regressions and ZIP validation to CI.
- Build once during CI packaging instead of running the frontend build twice.

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
