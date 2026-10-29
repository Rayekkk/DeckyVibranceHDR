# Changelog

All notable changes to Decky Vibrance HDR, newest first.

## [Unreleased]

### Changed

**Menu**

- Descriptions in the menu use plain words instead of terms such as LUT, EDID
  or gamut. Control names are unchanged.
- **Last applied** replaces "Last LUT sent" under **Now** and also appears for SDR
  on screens that use the additional colour table above 200%.
- A setting that fails after you close the menu now shows the **Setting failed**
  message instead of the slider silently moving back the next time you open it.
- While an external display is connected, the menu explains that your saved
  settings apply again once you unplug it.
- A switch or reset no longer jumps ahead of a slider change you made before it;
  changes reach the plugin in the order you made them.
- The menu checks for changes every 3 seconds while nothing is happening, and
  every second while colours are being prepared or something needs attention.
- **Updates**: when no release can be offered (none is published yet, or its ZIP
  is not ready), the section says why instead of "Up to date".

### Fixed

**Display and colour**

- Disabling the plugin or restarting Decky puts the original colours back even
  when the plugin was in the middle of another display change at that moment.
- A screen that reports no EDID at all is treated as standard gamut, as gamescope
  treats it, so 101% no longer jumps to the strongest SDR setting there.
- An unusual HDR entry in a screen's EDID no longer hides what the rest of the
  EDID says about HDR.
- When the plugin starts while docked and the display server answers slowly, it
  no longer takes its colour calibration from the external display.
- Slider and switch changes wait less for background display checks when the
  display server answers slowly.

**Settings and updates**

- Settings survive a power cut right after a change, and files left by an
  interrupted save are cleaned up.
- A damaged settings file no longer resets everything: the plugin goes back to
  the last good copy it keeps beside it, keeps the damaged file as
  `settings.json.corrupt-…` and says so in the menu.
- Updates download only the release ZIP GitHub lists for this plugin, with the
  exact size and SHA-256 GitHub reports, and check every file in it before it is
  saved; a download is refused unless the release is newer than the installed one.
- Stopping or updating the plugin no longer waits for a slow or stuck update
  check.
- An update download that ends early is reported as failed instead of being
  kept, and a download in progress stops when the plugin stops.
- Error messages in the menu no longer show the folders that contain your
  user name; the full details are in the Decky log.

## [1.2.0] - 2026-10-21

### Fixed

#### Settings

- An empty settings file no longer keeps the controls paused forever. It can be
  left by an interrupted first start, or by version 1.0.0 after a power loss,
  and now loads as defaults.
- A settings file that cannot be used (broken JSON, wrong content, or nesting
  deep enough to break the reader) no longer stops the plugin or stalls
  startup. It is kept as `settings.json.corrupt-<time>` and the plugin starts
  from defaults; nothing is deleted, and **Settings reset** in the menu names
  the kept file.
- Your last slider change is saved even when a slow display server delays
  shutdown.
- Disabling or uninstalling the plugin, or restarting Decky, saves your last
  slider change and puts the original colours back whenever Decky lets the
  plugin shut down. On Odin 3 it usually managed neither: Decky stopped it
  halfway, and the effect stayed on screen until gamescope restarted. Decky can still
  stop a plugin before its shutdown starts
  ([decky-loader#975](https://github.com/SteamDeckHomebrew/decky-loader/issues/975)).
  After a restart the plugin applies your settings again anyway; after
  disabling or uninstalling, the colours can stay until gamescope restarts.

#### Display and colour

- A single slow display query no longer drops the plugin into **Plugin not
  ready** for a moment, and can no longer leave a false "another plugin"
  conflict warning behind. Only a real gamescope restart reconnects.
- A reconnect that fails with an error is retried instead of leaving the plugin
  not ready, and a colour table that keeps failing is retried every 16 s rather
  than rebuilt every 4 s.
- Waking the device twice in quick succession reapplies your colour settings
  again; before, the second wake could skip it until the next one. Waking up in
  a dock no longer sends the built-in screen's settings to the external display
  for the few seconds before the plugin notices the dock.
- The "another plugin is driving the same SDR setting" warning now appears only
  while that plugin is actually writing, fades once it stops, and no longer
  prevents the plugin from restoring an SDR value gamescope dropped.
- Switching an effect off restores the right previous value after a lost reply,
  a brief disconnect, a return from an external display or another tool's
  colour table. A change whose confirmation was lost is still undone; before,
  the boost could stay on. After gamescope restarts, the value another tool set
  on the new session is the one restored.
- Switching an HDR or SDR look on while the display server is slow to answer
  no longer forgets another tool's colour table.
- SDR at 100% writes gamescope's own default, which is neutral on every panel,
  so it stays neutral even when the panel could not be identified.
- The panel is judged the way gamescope judges it: an EDID without colour data
  counts as a standard (Rec.709) panel with D65 white, odd red values are taken
  as they are, and sessions whose gamescope does not publish its EDID path (it
  only does when the session sets `GAMESCOPE_PATCHED_EDID_FILE`) read the
  built-in panel's EDID from the kernel. A standard-gamut panel whose
  information cannot be read for a moment keeps its own mapping. Each of these used to put a standard screen
  on the wide-gamut mapping, where every setting is much stronger.
- Docking is read from the display server that reports it. Lasting errors are
  logged once rather than on every retry, and in Desktop Mode the plugin no
  longer logs every X server every few seconds while it waits for gamescope.
- HDR at 100% no longer briefly shows **building the LUT...** for a table it
  never builds.

#### Menu and updates

- A backend that is slow to answer is shown as **Waiting for backend** (a
  change) or **Backend unavailable** (a read) instead of a silent spinner, also
  after the panel was closed and reopened. The **Retry** button is gone: the
  open panel already asks again every second, so it never did anything extra.
- Updates: a successful download clears the error of an earlier failed attempt,
  and checking again - or a check that fails - keeps a finished download of the
  same release. The result fields can be reached with the gamepad, and the
  section keeps its focus when the plugin becomes ready or loses its display.
- Install and update instructions match Decky: turn on Developer mode, choose
  **Browse** next to **Install Plugin from ZIP File**, accept Decky's warning
  and confirm. That replaces the installed version itself; there is no need to
  uninstall first, and settings are always kept.
- Clearer wording for what to do when a setting cannot be applied and for the
  SDR and HDR slider descriptions.

### Internal

- The menu tells **Could not load settings** and **Settings not saved** apart
  from the backend state rather than from the wording of the message.
- The SDR and HDR colour tables share one code path. `main.py` is split by job
  and keeps the controls, scheduling, monitoring and connecting: what goes onto
  gamescope (`vibrance_effects.py`), reading the panel (`vibrance_panel.py`),
  EDID parsing, gamescope's X11 atoms, LUT files, the settings file and host
  checks each live in their own `vibrance_*.py` module, and everything that
  lived on the `Plugin` class is one `PluginState` object (`vibrance_state.py`). The panel's request
  queue (`stateSync.ts`) exposes only what the panel uses. The panel tests
  share one Decky stand-in, `tests/panel-harness.mjs`.
- Tests are grouped by topic instead of by audit round, and every test passes
  on its own and in any order.
- Audit reports moved to the `1.1.1` tag; [docs/AUDITS.md](docs/AUDITS.md)
  links them.
- The shared updater validates and hands over downloads through the open file,
  never a path the user could swap, which matters for root-run sibling plugins.
- CI uses actions pinned by commit (they run on Node 24; the build uses Node
  22), tests the backend on Python 3.10, 3.11 and 3.13, and checks that a
  release tag matches `plugin.json` and the newest CHANGELOG heading.
  `verify:package` refuses uncommitted changes in shipped files, a bundled
  `@decky/api` that differs from the lockfile, and a frontend built from older
  sources than the checkout. Source maps get `decky://` paths on Windows too.
- README documents the release steps and that on a few devices with their own
  gamescope display script the steps above 100% may map differently.

### Quality checks

- Checked on Legion Go 2 with SteamOS (wide gamut, HDR) and Odin 3 with Armada
  (standard gamut with HDR, Decky running through FEX) through the backend
  calls the menu makes, reading back what reached gamescope: both sliders and
  their colour tables, gamescope dropping a value, another plugin writing the
  same setting, **Back to defaults**, the settings file, the update check,
  restarting Decky, switching to Desktop Mode and back, and sleep/wake. No
  warnings or errors were logged. Docking, physical gamepad input, colour
  measurement and HDR game output were not part of this pass.

## [1.1.1] - 2026-10-11

### Fixed

#### Settings and startup

- Protected saved settings when the plugin cannot read them or Decky stops
  during startup. Controls now wait for settings to load instead of replacing
  your choices with defaults.
- Prevented startup and display checks from getting stuck on invalid settings
  or display-information files.
- Startup errors now explain what failed. A settings-file problem is no longer
  described as a missing Game Mode connection or a failed save.

#### Menu and connection recovery

- Slow or missing backend replies no longer let repeated status checks build up
  a queue of requests.
- Status checking resumes when the pending call finishes, keeping newer choices
  made after reopening the menu or using **Back to defaults**.

#### Updates

- Update checks and downloads keep their progress when you close and reopen
  the Quick Access Menu. Completed download paths and errors remain available.
- Reopening the menu no longer starts duplicate update requests. Buttons from
  a closed menu cannot start another operation, and the installed version is
  reused instead of fetched again each time.

### Quality checks

- Added regressions for startup/settings failures, files that could block reads,
  slow replies, menu reopening and update results.
- Checked the fixes on Legion Go 2 with SteamOS and Odin 3 with Armada, including
  real backend calls and colour-table output. See the [device audit](https://github.com/Rayekkk/DeckyVibranceHDR/blob/1.1.1/docs/FULL_AUDIT_2026-10-11_LEGION_REGRESSION.md)
  for the methods and limits. Version 1.1.1 keeps the same audited runtime;
  these changes do not fix Decky's own reload issue or claim new physical
  controller, sleep/wake or docking tests.

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
