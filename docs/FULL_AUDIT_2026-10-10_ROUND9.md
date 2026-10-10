# Full quality audit, round 9 — 2026-10-10

Status: all six confirmed findings fixed locally. A fresh review after the final
updater-remount fix found **zero additional confirmed actionable defects** within
the scope below. This is an evidence-based stopping point, not a guarantee that
no undiscovered defect exists.

Version remains **1.1.0**. HEAD remains
`a303084c80d6335c27ea14cf04658c982ca993e7`. Nothing was staged, committed,
pushed, tagged or released. The public 1.1.0 release was not changed.
Only Legion Go 2 received this round's repaired payload and write tests.

## Confirmed findings and repairs

1. **Unresolved read amplification:** a timeout released the frontend barrier
   although Decky retained the underlying call for reconnect replay. Repeated
   polls queued five reads instead of one. A notice timeout now leaves the
   transport barrier intact; a real response/rejection releases it. Reopen/reset
   cannot accept a stale reply, and recovery requests a fresh snapshot.
2. **Unread settings treated as defaults:** startup ignored read exceptions.
   The actual installed Decky SettingsManager also suppresses malformed JSON
   errors, so merely catching its exceptions was insufficient. Read and validate
   the real file directly, distinguish an empty new-install file from an existing
   damaged file, retry transient errors and preserve unknown settings fields.
3. **Old updater handlers could send new RPCs after closing the panel:** check
   and download callbacks now reject calls when their component is unmounted.
   Built-bundle negative regression previously observed eight calls instead of
   six. Actual CEF handler test observed one positive-control request and zero
   additional requests from the closed handler.
4. **Misleading startup-error UI:** an unread settings file was described as a
   gamescope/Game Mode problem and a failed save. The menu now shows the actual
   startup reason and distinguishes a load error from a save error.
5. **Replayed mutation could still overwrite unread settings:** an independently
   reproduced startup PermissionError followed by a saturation RPC and unload
   bypassed the first read repair. All five settings-mutating RPCs now reject
   requests until a settings snapshot has loaded. Save, scheduling, persistence
   and unload also enforce that boundary; startup clears obsolete dirty status.
   Tests cover every RPC, direct save boundaries, unknown-field/file retention
   and continued mutation while gamescope is unavailable but settings are loaded.

Negative regressions failed before their fixes. The startup-RPC reproduction and
private logs are in `.local-validation/DeckyVibranceHDR/2026-10-10-round9` in the
workspace's parent directory. Atomic-save tests now explicitly establish the
loaded-settings state they are testing.

6. **Updater state was lost on QAM remount:** the actual built frontend remount
   regression reproduced loss of the pending check. Checking/downloading state,
   results and download path were component-local, so an accepted download could
   finish while closed without exposing its path after reopening. Shared updater
   state now outlives the panel, deduplicates operations and installed-version
   reads, retains success/error results, and only notifies mounted listeners.
   Stale handlers remain blocked and plugin disposal stops future requests.
   Deferred built-bundle tests cover check remount, download remount, duplicate
   request suppression, success while closed, failure while closed, cached
   version reads and disposal. The previous zero-finding conclusion was withdrawn
   when this additional scenario was raised and reproduced.

## Fresh review after the last repair

Reviewed backend startup, settings, exported RPCs, mutation and filesystem locks,
worker/debounce coalescing, cancellation, LUT slot/cache ownership, atom release,
recovery, monitoring and unload; frontend queue epochs, polling, reopen/disposal,
error rendering and updater mount/busy handling; EDID checksums/CTA/DisplayID,
white adaptation, wide/standard gamut branches; PQ/G22 ICtCp generation, input
validation and bounded caches; ARM/FEX interpreter/xprop selection, cancellation
and ordinary SteamOS fallback; HTTPS/redirect allowlist, verified CA selection,
stream deadlines/size limits, update-check generations, concurrent downloads,
ZIP validation and atomic replacement; Rollup, package staging, manifest
agreement, bundled-library sources/licenses, lockfile and CI workflow.

Backend generation checks protected asset authorization and file writes but did
not protect the reopened menu's operation/result state. That distinct frontend
failure was repaired as finding 6. Existing overlapping-check tests still cover
old-success/new-failure and reversed completion. The final review also examined
the new shared updater snapshot, subscriber cleanup, disposed-result handling,
error retention and duplicate version/check/download guards. No additional
confirmed repair was identified after this final change.

## Automated verification

- `python -W error -m unittest discover -s tests`: **261 passed** on Windows.
- The same current backend suite: **261 passed** with native Linux Python 3.14 on
  Legion, run as `deck` in an isolated directory with the test Decky harness.
  It did not import the live backend or touch production settings/atoms.
- `npm test`: **23 passed**, including bounded timed-out replay/reopen/reset.
- Built frontend contract: **1 passed**, including updater remount/pending/results, unmounted callbacks
  and settings-load error labels. **285 distinct automated tests total**;
  the Linux run repeats the 261 backend tests and is not counted twice.
- Typecheck, Python compile, fresh build, ZIP/CRC/runtime/source-map/license
  validation and `git diff --check`: passed.
- `npm audit --audit-level=low`: **zero vulnerabilities**.
- Bandit: no scan errors; five reviewed flags (four low, one medium).
  The medium flag is the fixed `/tmp/.X11-unix` X-server discovery path, not a
  created temporary file. Subprocesses use argument arrays and no shell; the
  optional certifi import fallback preserves certificate verification.
- CI workflow reviewed. Existing published main/tag runs are green, but they
  cover the published commit, **not these local changes**. No remote run was
  triggered because publishing is forbidden.

Final local ZIP: `DeckyVibranceHDR-1.1.0.zip`, **82715 bytes**, 27 entries / 21
runtime files. SHA256: `b147115afa5786ec9a3db71621dd6eb7e28efbbd509348b8bd04f4515a768ba7`.
ZIP timestamps can change its hash across rebuilds; final device verification
compared the content hash of every runtime file with this exact final ZIP.

## Actual Legion verification

Tested the real mounted QAM controls using CEF native keyboard events, actual
Decky RPCs, saved JSON, gamescope root atoms and LUT-file reads. No whole Decky,
Steam or gamescope restart was performed; only this plugin was reloaded.

- Native SDR slider 300% → 299%: **69.6 ms** in the final post-fix pass (71–83.7 ms in
  preceding passes), including backend/atom confirmation.
- Native SDR toggle off restored the CARDINAL float to **-1.0**, on restored 2.99.
- Native HDR toggles off/on passed. Immediately after off, PQ was empty, without
  an owned HDR LUT. Native **Back to defaults** restored SDR -1.0 and empty PQ;
  G22 remained absent. These are immediate atom reads, not only backend flags.
- HDR native slider 140% → 139% applied in **472.1 ms** and HDR RPC disable in
  **12.1 ms** earlier in this round. These are individual observations, not
  benchmark percentiles. The final updater change was followed by fresh
  real toggles/reset/SDR/UI/backend/file checks.
- Rapid HDR requests, reset, reopen and restore passed earlier in the round;
  stale read/reconnect ordering is additionally covered by deterministic tests.
- Actual installed SettingsManager's suppressed JSON error was confirmed using
  a disposable private fixture. Temporary diagnostic RPC removed; the final
  payload contains no diagnostic methods or transport instrumentation.
- Advertised HDR LUT: **35937 RGB triples**, all finite and within [0,1], proper
  `LUT_3D_SIZE 33`; **970432 bytes**, SHA256
  `9d1e99d2385201c05e54e2b0a812a28a627e54067155f759f5863a0eab5040d1`.
- Real device TLS update check and native menu check returned installed/latest
  **1.1.0**, no error and **Up to date**.
- Real QAM close/reopen during an actual HTTPS update check retained **Checking...**,
  made **one** real update-check RPC and displayed **Up to date** afterwards.
  A temporary API wrapper delayed only delivery of the real reply by 2.5 seconds
  to guarantee overlap. The wrapper was removed and the plugin reloaded clean
  before repeating the other menu tests. This fixture did not disable TLS or
  fabricate release metadata. Download remount/success/failure behavior was
  exercised in the built-bundle regression, not by replacing files in Downloads.
- Saved DOM confirms both controls and 300%/140%, no backend-unavailable error.
  Private `menu-protected.png` is the actual CEF screenshot. CEF capture shows
  the menu surface; it is not evidence of the physical panel's color output.

After the final deployment/tests, **all 21 payload hashes match the final ZIP**,
settings equal the original SDR-on 3.0 / HDR-on 1.4 JSON, backend ready and all
error/generation fields clear. **897 unrelated plugin/settings files, 33 CPU
values, 7 platform/battery values and 7 LED values match the original baseline**.
Companion and WiFi received no writes. No Auto GPU Bias was added.

MagicBlack remained mode **1**, opacity **1**. All Vibrance reloads ran beneath
an independent temporary guard with panel brightness **0**. That service is now
stopped, its setter lock released, and brightness restored to the actual saved
pre-guard value for the last pass **472000**; immediate readback was 472000.
The first two passes restored their saved **14160**. Steam had naturally raised
brightness to 472000 before the last guard started; that current value was saved
separately, without replacing the original baseline, and restored. Steam remains
free to adjust brightness afterwards. MagicBlack is still 1/1. Temporary ZIP uploads
were removed; private baseline/backups/evidence were retained.

## Limits

No physical controller operation, subjective image/colorimeter comparison,
actual HDR-game output, suspend/resume or docking was performed in this round.
Standard-gamut SDR look and ARM/FEX behavior were reviewed and regression-tested,
but this Legion wide-gamut x86 device does not exercise those physical paths.
No Odin or Ayaneo writes were performed in this round. Reconnect ordering was
simulated deterministically; live shared Decky transport was not interrupted.
The startup read-failure/replayed-mutation repair passed on both platforms in an
isolated fixture; production settings were not deliberately corrupted.
