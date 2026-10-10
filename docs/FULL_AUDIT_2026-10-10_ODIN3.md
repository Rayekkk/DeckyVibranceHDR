# Odin 3 / Armada quality audit — 10–11 October 2026

## Scope and result

Audited the complete current working tree, including the earlier uncommitted
Legion fixes, and deployed its newly built 1.1.0 ZIP to Odin 3. No staging,
commit, push, tag, release replacement or version change was performed in this
round. The published 1.1.0 release is a separate, older artifact. Legion and
Ayaneo were not accessed or changed by this round.

Two findings were reproduced and repaired: a blocking FIFO EDID read and startup
test fixtures that adopted the developer's live gamescope state. After the EDID
repair, another complete source review and the checks below found no additional
confirmed actionable defect in the plugin. This does not prove all hardware
scenarios are correct. The confirmed Decky 3.2.10 EOF lifecycle defect below
remains a host limitation; the device was recovered and final controls passed.

## Findings and repairs

### EDID metadata could hang startup or monitoring indefinitely

`_read_edid` previously used blocking `open/read` on the path advertised by
gamescope. An isolated named pipe without a writer caused the native Odin test
to exceed its two-second timeout. No actual gamescope EDID atom was changed to
reproduce this problem.

The reader now opens nonblocking, checks the opened descriptor with `fstat`,
accepts only regular files, bounds the read to 32769 bytes and closes the
descriptor on every path. Checking after open avoids a stat/open race. Regular
symlinks and zero-length-reported sysfs files remain supported; the implementation
does not use file size as an eligibility check.

Three regressions cover bounded regular reads, FIFO rejection without descriptor
leaks, and regular symlinks. A temporary RPC in the actual frozen x86 Decky
backend under FEX verified 20 FIFO calls in **3.12 ms**, no leaked descriptors,
and a **256-byte** real EDID read through both its path and a symlink. The RPC was
removed, followed by a clean reload and repeated device control tests.

### Startup tests depended on the developer's display

The initial whole-suite ARM Linux run failed
`StartupSettles.test_a_disabled_half_clears_what_the_last_run_left` after earlier
tests adopted the real Odin G22 look as a baseline. This was a fixture-isolation
defect, not a demonstrated product color defect.

`IsolatedStartup` now patches string/float atom reads and writes, display/session
identity and the relevant shared class state, with cleanup for each patch.
Startup retry, settling and disabled-state tests use that fixture. The full
native suite subsequently passed. The negative run is retained privately.

An additional native-worker deadline regression verifies timeout, kill and reap
without waiting 30 seconds or launching a deliberately stuck device process.

## Fresh source review after the last runtime repair

Reviewed startup and settings loading, mutation/persistence ordering, lifecycle
and ownership restoration, monitoring and session/display recovery, EDID parsing
and metadata caching, native-worker architecture/environment selection, LUT
generation/cache/atomic publishing/cancellation, frontend queue and remount
behavior, updater state/TLS/URL/download/archive validation, package construction,
dependency metadata, tests and CI source.

The earlier local protections remain present: failed settings reads never
overwrite an existing saved file; mutations wait for settings to load; save
work runs outside the control lock; frontend read timeouts retain one transport
request; stale remount replies cannot restore obsolete edits; updater requests
and results survive QAM remounts; stale handlers cannot start work after unmount.

ARM acceleration remains architecture-gated. The ELF measurements on Odin were
loader machine **62** (x86-64), system Python and xprop machine **183** (AArch64).
Ordinary SteamOS retains the in-process path. That fallback is covered by
automated tests; this Odin round did not repeat physical SteamOS checks.

## Automated checks and packaging

| Check | Result |
| --- | --- |
| Windows backend, warnings as errors | 265 discovered; 263 passed, 2 POSIX fixtures skipped |
| Actual Odin native Python 3.14, warnings as errors | All 265 passed in 7.067 s |
| Frontend state and package-failure tests | 23 passed |
| Compiled frontend contract | 1 passed |
| TypeScript typecheck and Rollup build | Passed |
| ZIP CRC, paths, version, source-map, library licensing and payload verification | Passed |
| npm dependency audit at low threshold | 0 vulnerabilities |
| Git whitespace check | Passed |

There are **289 distinct automated tests** across the complementary backend and
frontend suites, not 289 plus another 265 counted twice. Build output contains
21 files in 27 ZIP entries. Final local artifact:

```text
DeckyVibranceHDR-1.1.0.zip
82974 bytes
SHA-256 b679e6b10002190b96f1ecacaf3a76ce462922ed74e92a10305cdba603377e3e
```

All 21 installed payload files were compared to the archive manifest after
removing probes and after the final reload. All hashes matched. Runtime files
are readable, with UID/GID 1000 and mode 0644; Decky changed `plugin.json` to its
normal root-owned 0755 form on reload. Initial hash-only baselines did not record
original ownership, so exact original UID/GID preservation is not claimed.

Bandit produced four low-severity reports and one medium report. Reviewed the
optional certifi import fallback, subprocess imports/calls with argument arrays,
and the fixed X11 socket directory `/tmp/.X11-unix`; these were not confirmed
new vulnerabilities. CI source was reviewed, but no remote CI run of these
uncommitted repairs was triggered.

## Actual device behavior

Tests used mounted Steam QAM controls, focus and keyboard events through CEF,
actual Decky RPCs, root-atom readback and generated file inspection. They did not
replace native controls with a mock UI. The Odin MenuStore API was used.

Final repeated clean-payload measurements include polling/readback overhead:

| Operation | Measurements |
| --- | --- |
| SDR 300 → 299 → 298 → 297%, with standard-gamut G22 LUTs | 638.4, 660.3, 670.8 ms |
| HDR 150 → 149 → 148 → 147%, with PQ LUTs | 548.0, 654.3, 665.9 ms |
| Disable while HDR build is active | RPC returned in 3.6 ms |
| Disable while SDR build is active | RPC returned in 45.4 ms |

Both disable/cancel paths had no late LUT publication after the observation
window and no `.tmp` residue. Native toggles and reset restored the expected
atoms while preserving the other half. Reset cleared both looks and set SDR to
its unset value. A rapid RPC burst followed by a native slider edit and QAM
close/reopen ended at the latest requested **137%**, without resurrecting an
older value. Real certificate-verified update checks returned current/latest
1.1.0 and the native menu displayed "Up to date".

Each actual PQ/G22 LUT contains **35937 finite samples in [0,1]** and a 33³
header. Temporary native-contract measurements earlier in this same round:
full PQ build 645.29 ms / child CPU 232.62 ms; G22 build 634.80 ms / child CPU
251.56 ms; native xprop 18.97/17.76/17.82 ms versus one emulated observation
77.24 ms. Both transfers matched the frozen in-process computation exactly at
9³ for the current white point. Native cancellation took 65.58 ms and left no
live native worker children.

A controlled CEF fixture delayed delivery of one actual state reply by 11 s.
Across the notice timeout and QAM remount there was one underlying read, and a
late stale reply was discarded before a fresh state rendered. A separate 2.5 s
delivery delay around one real TLS updater check preserved "Checking..." across
remount and completed without duplicate checks. These were delayed-reply
fixtures, not physical network outages. Wrappers and diagnostic globals were
removed; later final checks confirmed their absence.

The protected QAM screenshot was inspected: installed/latest 1.1.0, expected
150% applied HDR status, reset and update controls rendered correctly. This
capture is not proof of the physical OLED output under the black overlay.

## Restoration and cleanup

Settings are restored to the actual Odin baseline: **SDR enabled at 3.0** and
**HDR enabled at 1.5**. The backend is ready, reports no component errors, has
both effects applied and no builds pending. Active look hashes match the
original baseline files, despite normal slot alternation:

```text
PQ  fa6a2eab8b63fdb107af2349308bccbe0eb526572054ffb63993e2270f62b3ad
G22 8c111c3d5982e800cdf68df85d68072c98a67b943cd4ed6b7d45b745eb1c7ee0
```

Final readback preserved **145/146 unrelated plugin/settings file hashes**, all
**67 hardware values**, and the 256-byte EDID. The sole changed file was
`settings/decky-autoflatpaks/settings.json`, containing only `lastCheckTimestamp`.
No audit script wrote this file. A normal startup/check timestamp is consistent
with this delta, but baseline hash alone cannot prove the old timestamp or its
exact cause. No trustworthy backup of that other-plugin file was found; it was
preserved rather than replaced with an invented value. Vibrance backups cover
only its own plugin/settings/data directories and do not establish preservation
of original ownership. Final installed ownership was checked explicitly:
runtime files UID/GID 1000, manifest root after Decky's load.

Both audit guard services are inactive and the JS lock/probes are removed.
Brightness was restored and immediately read back as **3756**, from the actual
`pre-guard-brightness-lifecycle.json`; a subsequent read-only check also read
3756. The older 541 and initial inventory 1798 values were not reused.
MagicBlack was verified mode 1, opacity 1 before and after guard cleanup.

The first independent panel guard used a read-after-write assertion that raced
with Steam's brightness writes, restarted and subsequently became inactive.
The existing black guard and previously verified MagicBlack protected that
phase, but this is not proof of uninterrupted independent panel blanking. The
private panel guard was corrected to keep checking and correcting brightness
without crashing on concurrent writes. Its final run recorded **32296 checks,
7 corrections over 171.53 s**, ending at brightness 0 before restoration.
Polling is not proof of zero exposure between checks or physical OLED output.
No further disruptive loader test was performed during this corrected run.

The authorized loader restart exposed `KillMode=process`: old plugin children
survived with PPid 1, and EOF-spinning instances consumed CPU. Cleanup targeted
only recorded children of the original core PID 1623, with explicit PID, PPid,
UID, executable and pre-current-core start checks. Privileged Armada plugins
also matched their exact observed names/start ticks. No broad process-name kill
was used. The final check found none of the original eight plugin PIDs active,
no native LUT workers, no temporary LUT files, no audit JS globals and exact
hashes for all 21 installed payload files. Current plugin instances belonged
to the active replacement core.

Before cleanup, a recovery control test recorded SDR 1534–1900 ms and HDR
1214–1939 ms under the old orphan load. These observations are retained rather
than presented as final plugin latency. The final measurements above, after
cleanup and UI recovery, show no persistence of that regression. Steam/CEF
temporarily became unavailable during recovery and later returned; this audit
issued no Steam, gamemode, system restart or debug-service change.

Private backups and evidence remain under
`.local-validation/DeckyVibranceHDR/2026-10-10-odin3-latest` locally and the
matching private audit directory under Odin's homebrew folder. Diagnostic files
are retained as evidence; no runtime probe remains in the installed plugin.

## Observable host limitation and physical validation limits

Manual Vibrance-only reloads on Decky **3.2.10** took about **5.1 s** and Decky
logged SIGKILL after its stop budget. The cause is now reproduced. Installed
frozen lifecycle bytecode matched the official tag's stop/shutdown/socket logic.
A diagnostic contract established that the SIGTERM callback belonged to the
same sandbox/plugin instance and the timing hook was loaded and writable.

In [Decky 3.2.10 UnixSocket](https://github.com/SteamDeckHomebrew/decky-loader/blob/v3.2.10/backend/decky_loader/localsocket.py),
EOF returns an empty string while the socket remains active. The child listener
can loop without yielding, starving the scheduled shutdown task. The parent's
response listener has a corresponding EOF spin, preventing loader RPCs if a
child exits before its listener is cancelled. Four isolated native ARM
reproductions using the exact source AST showed both original loops starving
signal callbacks; a test-only EOF fix marking the socket inactive allowed both
callbacks and clean exits. System Decky source and binary were not modified.

A direct SIGTERM while the child connection remained open exercised Vibrance's
real `_unload`: restoration completed in **115.10 ms**, with journal and timing
evidence. Child exit then triggered the parent's EOF spin, requiring the
expressly authorized loader-service recovery described above. The plugin's
restoration executes when Decky delivers its lifecycle task; Vibrance cannot
guarantee that callback during the loader's EOF starvation. Native and FEX
minimal fork/signal checks both passed, so these results do not establish a
general FEX signal failure. Production code does not patch Decky internals or
install a global signal-handler workaround.

Control disable/reset, cancellation and baseline restoration were separately
verified through real RPC/readback. Automated regressions cover plugin unload,
native-worker timeout/reaping and settings protection. Temporary lifecycle hooks
were removed and exact clean deployment checked before final UI tests.

No colorimeter, physical gamepad-input assessment, HDR game run, subjective
color assessment, suspend/resume or docking test was performed on Odin in this
round. Pending download completion/error behavior is covered by compiled
frontend regressions, not an actual write into the user's Downloads directory.
No claim is made that every game, display or loader failure mode has been
exhausted.

## Later shared-backend validation

The subsequent Legion audit found an additional settings FIFO/constructor hang.
That repair and the final package were retested on Odin after it was powered on.
See [the 2026-10-11 Legion regression and Odin follow-up](FULL_AUDIT_2026-10-11_LEGION_REGRESSION.md)
for the final package hashes, 269-test runs, actual frozen FEX constructor checks,
mounted-handler measurements, navigation-input limitation and fresh restoration.
The earlier measurements and recovery history in this report remain historical.
