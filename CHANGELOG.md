# Changelog

All notable changes to Decky Vibrance HDR, newest first.

## [Unreleased]

### 🔧 Changed

**Menu**
- Descriptions use plain words instead of terms such as LUT, EDID or gamut. Control names are unchanged.
- **Last applied** replaces "Last LUT sent" under **Now** and also appears for SDR on screens that use the
  additional colour table above 200%.
- While the plugin starts, the menu shows a spinner.
- If the plugin cannot start, it says **Decky Vibrance HDR unavailable** with the reason, a **Try again**
  button and the Updates section, so a broken install can still be updated.
- Texts use American spelling ("colors") and a single "…" character.
- While an external display is connected, it explains that your saved settings apply again once you
  unplug it.
- It checks for changes every 3 seconds while idle, and every second while colors are being prepared or
  something needs attention.

**Errors**
- A setting that fails after you close the menu now shows a **Decky Vibrance HDR** message with the
  reason, instead of the slider silently moving back the next time you open it.
- A change the plugin refuses now also says why under that switch, slider or **Back to defaults**, until
  you use that control again.

**About**
- It is the new last row of the menu. Its page shows the version, author and license, with **Updates** at
  the bottom; Updates is no longer on the main menu.

**Updates**
- Wording now matches the other plugins: **Installed version**, **Checking GitHub…**,
  **Download <version>** and **<version> downloaded**, with the saved file shown as `~/Downloads/…` and a shorter
  install hint.
- A failed check or download shows **Could not complete** with the reason in the section instead of a
  message over the screen.
- When no release can be offered (none published yet, or its ZIP is not ready), the section says why
  instead of "Up to date".

**Sliders**
- A switch or reset no longer jumps ahead of a slider change you made before it; changes reach the
  plugin in the order you made them.

### 🐛 Fixed

**Display**
- Disabling the plugin or restarting Decky puts the original colors back even when the plugin was in the
  middle of another display change.
- A screen that reports no EDID at all is treated as standard gamut, as gamescope does, so 101% no longer
  jumps to the strongest SDR setting there.
- An unusual HDR entry in a screen's EDID no longer hides what the rest of the EDID says about HDR.
- When the plugin starts while docked and the display server answers slowly, it no longer takes its
  color calibration from the external display.

**Sliders**
- Slider and switch changes wait less for background display checks when the display server answers
  slowly.

**Settings**
- Settings survive a power cut right after a change, and files left by an interrupted save are cleaned
  up.
- A damaged settings file no longer resets everything: the plugin goes back to the last good copy it
  keeps beside it, keeps the damaged file as `settings.json.corrupt-…` and says so in the menu.

**Updates**
- Downloads accept only the release ZIP GitHub lists for this plugin, with the exact size and SHA-256
  GitHub reports, and every file in it is checked before saving.
- A download is refused unless the release is newer than the installed one.
- Stopping or updating the plugin no longer waits for a slow or stuck update check.
- A download that ends early is reported as failed instead of being kept, and a download in progress
  stops when the plugin stops.

**Errors**
- Messages in the menu no longer show folders that contain your user name; the full details are in the
  Decky log.

## [1.2.0] - 2026-10-21

### 🔧 Changed

**Menu**
- A backend that is slow to answer is shown as **Waiting for backend** (a change) or
  **Backend unavailable** (a read) instead of a silent spinner, also after the panel was closed and reopened.
- The **Retry** button is gone; the open panel already asks again every second, so it never did anything
  extra.
- Clearer wording for what to do when a setting cannot be applied, and for the SDR and HDR slider
  descriptions.

**Updates**
- The result fields can be reached with the gamepad, and the section keeps its focus when the plugin
  becomes ready or loses its display.

**Install**
- Instructions now match Decky: turn on Developer mode, choose **Browse** next to
  **Install Plugin from ZIP File**, accept Decky's warning and confirm. That replaces the installed version itself,
  so there is no need to uninstall first, and settings are always kept.

### 🐛 Fixed

**Settings**
- An empty settings file no longer keeps the controls paused forever. It can be left by an interrupted
  first start, or by version 1.0.0 after a power loss, and now loads as defaults.
- A settings file that cannot be used (broken JSON, wrong content, or nesting deep enough to break the
  reader) no longer stops the plugin or stalls startup.
- Such a file is kept as `settings.json.corrupt-<time>`, the plugin starts from defaults, and
  **Settings reset** in the menu names the kept file.
- Your last slider change is saved even when a slow display server delays shutdown.
- Disabling or uninstalling the plugin, or restarting Decky, saves your last slider change and puts the
  original colors back whenever Decky lets the plugin shut down.
- On Odin 3 this usually failed, because Decky stopped the plugin halfway and the effect stayed on
  screen until gamescope restarted. Decky can still stop a plugin before its shutdown starts
  ([decky-loader#975](https://github.com/SteamDeckHomebrew/decky-loader/issues/975)): after a restart the plugin
  applies your settings again anyway, but after disabling or uninstalling the colors can stay until gamescope
  restarts.

**Display**
- A single slow display query no longer drops the plugin into **Plugin not ready** for a moment, and can
  no longer leave a false "another plugin" conflict warning behind. Only a real gamescope restart reconnects.
- A reconnect that fails with an error is retried instead of leaving the plugin not ready, and a color
  table that keeps failing is retried every 16 s rather than rebuilt every 4 s.
- Waking the device twice in quick succession reapplies your color settings again; before, the second
  wake could skip it until the next one.
- Waking up in a dock no longer sends the built-in screen's settings to the external display for the
  few seconds before the plugin notices the dock.
- The "another plugin is driving the same SDR setting" warning now appears only while that plugin is
  actually writing and fades once it stops. It also no longer prevents the plugin from restoring an SDR value
  gamescope dropped.
- Switching an effect off restores the right previous value after a lost reply, a brief disconnect, a
  return from an external display or another tool's color table.
- A change whose confirmation was lost is still undone; before, the boost could stay on.
- After gamescope restarts, the value another tool set on the new session is the one restored.
- Switching an HDR or SDR look on while the display server is slow to answer no longer forgets another
  tool's color table.
- The panel is judged the way gamescope judges it: an EDID without color data counts as a standard
  (Rec.709) panel with D65 white, and odd red values are taken as they are.
- On sessions where gamescope does not publish its EDID path, the built-in panel's EDID is read from the
  kernel. A standard-gamut panel whose information cannot be read for a moment keeps its own mapping.
- The items above used to put a standard screen on the wide-gamut mapping, where every setting is much
  stronger.
- Docking is read from the display server that reports it. Lasting errors are logged once rather than on
  every retry, and in Desktop Mode the plugin no longer logs every X server every few seconds while it waits for
  gamescope.

**SDR**
- 100% writes gamescope's own default, which is neutral on every panel, so it stays neutral even when the
  panel could not be identified.

**HDR**
- 100% no longer briefly shows **building the LUT...** for a table it never builds.

**Updates**
- A successful download clears the error of an earlier failed attempt, and checking again, or a check
  that fails, keeps a finished download of the same release.

## [1.1.1] - 2026-10-11

### 🐛 Fixed

**Settings**
- Saved settings are protected when the plugin cannot read them or Decky stops during startup.
  Controls now wait for settings to load instead of replacing your choices with defaults.
- Startup and display checks no longer get stuck on invalid settings or display-information files.

**Errors**
- Startup errors now explain what failed. A settings-file problem is no longer described as a missing
  Game Mode connection or a failed save.

**Menu**
- Slow or missing backend replies no longer let repeated status checks build up a queue of requests.
- Status checking resumes when the pending call finishes, keeping newer choices made after reopening the
  menu or using **Back to defaults**.

**Updates**
- Update checks and downloads keep their progress when you close and reopen the Quick Access Menu.
  Completed download paths and errors remain available.
- Reopening the menu no longer starts duplicate update requests, buttons from a closed menu cannot start
  another operation, and the installed version is reused instead of fetched again each time.

## [1.1.0] - 2026-10-10

### 🔧 Changed

**Performance**
- Much faster controls on Armada devices running Decky through emulation. On Odin 3, saturation
  changes took about 0.6 seconds for HDR and 0.7 seconds for SDR, and turning an effect off during a build took
  41–121 ms. Results may vary by device and workload; ordinary SteamOS keeps its existing execution path.
- Faster color-LUT generation, and recent results are reused when you return to a previous
  saturation value. LUT resolution and color calculations are unchanged.
- Fewer repeated display checks, unnecessary saves and rebuilds, leaving more room for the controls
  to respond to your next change.

**Errors**
- Connection problems and failed changes are shown more clearly, and the plugin retries operations that
  could not be completed.

**Display**
- Better recovery after sleep, temporary display connection problems and changes between the built-in
  screen and an external monitor.
- Better detection of the panel's color capabilities and HDR support.

**Updates**
- Better update detection, including preview versions, and more reliable downloads on slow connections.
- Stricter checks of update files; failed downloads or packaging attempts keep the previous usable file.

### 🐛 Fixed

**Menu**
- The "Backend unavailable - python exception" message that could prevent the plugin menu from working.

**Sliders**
- Sliders keep your latest selection instead of jumping back to an older value, and closing and
  reopening the menu no longer brings back outdated changes.
- Rapid slider moves and repeated toggles no longer create a long queue of work. Turning an effect off
  or resetting settings cancels changes you no longer want.

**Settings**
- Settings are saved more reliably, including when Decky stops during startup. Slow or failed saves no
  longer hold up the controls or prevent a reset.

**Display**
- Disabling the plugin restores the previous display settings more reliably, including when another tool
  has changed them while the plugin was active.
- Returning saturation to neutral no longer leaves an older color effect active.

**Updates**
- Download folders and filenames containing special characters are handled correctly.

## [1.0.2] - 2026-09-14

### 🔧 Changed
- **HDR:** the status shows the saturation of the last LUT successfully sent, without claiming gamescope has
  confirmed loading it.

### 🐛 Fixed

**Display**
- Failed HDR and SDR look builds or replacements are retried. An older working file no longer counts as
  a completed slider change.

**Errors**
- SDR, SDR look and HDR errors are kept independently. A successful operation on one control no longer
  hides another control's failed apply or reset.

**SDR**
- Enabling again after a failed release preserves the original baseline instead of capturing the plugin's
  own value.
- Missing SDR properties and failed initial SDR writes are retried. A missing property is recreated only
  after confirming gamescope is still reachable.
- The standard-gamut SDR look above 200% now converts gamescope's generic source gamut to BT.2020 for ICtCp
  and back before clipping. Native white from EDID is adapted to D65; unreadable white falls back to D65.

**Sliders**
- Pending edits remain visible, and outdated status responses cannot overwrite newer slider values.

## [1.0.1] - 2026-08-09

### 🔧 Changed
- **Display:** normal monitoring now checks only whether the active display changed, instead of repeatedly reading
  and parsing the same EDID every five seconds.
- **Errors:** a release failure is shown in the plugin panel instead of only in the backend log.

### 🐛 Fixed

**Display**
- Starting the plugin while an external display is already active no longer applies the built-in
  panel's SDR value or either look to that display. The saved settings return after switching back.
- Disabling a look restores the value that was there before the plugin took control. A look owned by
  another plugin is left alone.
- A failed release keeps enough state to retry rather than forgetting what still needs cleanup.
- A failed LUT replacement no longer loses track of the previous working file, so the old look can
  still be released cleanly.

**Sliders**
- Rapid enable, disable and slider calls can no longer finish out of order and put back a setting that
  a newer call removed.

**HDR**
- A LUT build that finishes after disable, unload or docking is discarded instead of restoring the old look.

**Updates**
- A download is offered only when the release contains the exact `DeckyVibranceHDR-<version>.zip`
  asset, instead of accepting a missing or unrelated attachment.

**Install**
- Unloading or uninstalling the plugin waits for any LUT write in progress before generated files are
  removed.

## 1.0.0

### ✨ Added

**Plugin**
- First release.

**SDR**
- Saturation slider for SDR content under gamescope, up to 300%.
- 100% is neutral on both wide-gamut and standard panels, so the slider keeps one meaning on every panel.
- On a standard-gamut panel the last part of the range up to 300% comes from an extra look on the SDR path.
- The SDR value is put back whenever anything drops it. An ordinary gamescope reset heals silently; only a
  run of them is reported as vibrantDeck writing the same setting.

**HDR**
- Saturation slider for HDR content, up to 150%. Past about 130% it hard-clips channels at the gamut
  boundary, and the slider says where its range stops being free.
- The adjustment is computed in ICtCp on PQ-encoded values, so lightness and hue are not dragged along in
  bright saturated areas.
- The HDR section is hidden on a panel that does not advertise SMPTE ST 2084 in its EDID.
- 100% loads no LUT at all, and the HDR LUT is rebuilt only once the slider stops moving.

**Sliders**
- Both sliders bottom out at 100%; this is a vibrance control, not a desaturation one.

**Display**
- Settings apply only to the built-in panel. An external display is left alone, and the settings come
  back when it is unplugged, followed live rather than only at startup.
- Startup waits for gamescope to finish setting up before writing anything, then applies the values in
  several steps, so a look is not discarded by a half-built session.
- Both effects are re-applied after a resume from suspend, repeated several times because gamescope can
  clobber them while it re-initializes.
