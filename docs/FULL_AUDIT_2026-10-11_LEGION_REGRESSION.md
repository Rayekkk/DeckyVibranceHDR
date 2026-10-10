# Full quality audit — Legion regression and Odin follow-up, 2026-10-11

Status: the fresh review found one additional shared-backend defect, fixed below.
The final code and package passed regression checks on ordinary x86-64 SteamOS
and Armada/FEX. The subsequent source review found **zero additional confirmed
actionable plugin defects** in the reviewed scope. The loader and navigation-input
limitations below remain explicit; this is not a guarantee about untested hardware
or every possible failure mode.

Version remains **1.1.0**. The audit base commit is
`a303084c80d6335c27ea14cf04658c982ca993e7`. At the device-audit checkpoint nothing
was staged, committed, pushed, tagged or released. Independent review of both
devices subsequently passed, authorizing the already requested commit/push step.
Earlier evidence remains in the round-9 and Odin reports; their earlier package
hashes and measurements are historical observations.

## Additional defect and repair

An existing settings path that is a FIFO could block startup indefinitely.
An isolated reader compiled from the installed Legion source blocked past its
one-second subprocess deadline. No live settings path or gamescope atom was
replaced for that negative test.

Inspection of the real [Decky 3.2.10 SettingsManager](https://github.com/SteamDeckHomebrew/decky-loader/blob/v3.2.10/backend/decky_loader/settings.py)
showed a second entry point: its constructor reads an existing file during import.
Fixing only the later startup read would therefore leave the same hang reachable.

The plugin now defers that constructor read, then opens settings with nonblocking
flags and validates the opened descriptor as a regular file. Failures reach the
existing startup-error/retry path without overwriting user settings. Regular-file
symlinks remain supported, unknown JSON fields survive, and descriptors close
on rejection. The Decky class itself is not globally patched.

Four new regressions cover the eager constructor with a FIFO, 100 FIFO rejections
without descriptor growth or settings mutation, regular JSON with unknown/Unicode
fields, and a regular-file symlink. The eager test reproduces the real constructor
dispatch before importing the actual backend. Three POSIX-only tests run on Linux.

Odin additionally exercised the **actual frozen Decky/FEX process**, its real
SettingsManager constructor, and the installed reader code against private
fixtures: 100 rejected FIFO reads with constructor setup took **5.46 ms**, FD
count stayed **52 → 52**, and regular JSON/symlink bytes were preserved. This
required a temporary diagnostic method. The first diagnostic used an unavailable
Decky data-directory attribute and failed; its fixture path was corrected. Both
diagnostic installations were removed, followed by clean source/payload checks
and all final control tests. No diagnostic method is part of the package.

## Fresh review and local validation

Reviewed all backend startup/read/save paths, mutation ordering, monitor/cache
invalidation, SDR/PQ/G22 ownership and restoration, native-worker architecture
selection/environment/deadline/reaping, LUT generation, updater trust/download
validation, frontend synchronization/remount/disposal, package scripts, dependency
lock and CI. Earlier round-9 and EDID fixes remain present. The final settings
repair does not change the ARM/native-worker gate or ordinary SteamOS fallback.

| Check | Final result |
| --- | --- |
| Windows Python, warnings as errors | 269 discovered: 264 passed, 5 POSIX skips |
| Legion native Linux Python 3.14 | All 269 passed, 6.046 s |
| Odin native ARM Python 3.14 | All 269 passed, 7.654 s |
| Node frontend/package tests | All 23 passed |
| Compiled frontend host/manifest contract | Passed |
| Typecheck, compileall, fresh build and ZIP verification | Passed |
| npm dependency audit | 0 vulnerabilities |
| Bandit | 5 reviewed findings, no scan errors |
| Git whitespace check | Passed |

There are **293 distinct automated checks** (269 Python, 23 Node, one compiled
bundle contract); platform repetitions are not counted as additional cases.
Bandit reported four low findings (subprocess imports/calls and an optional
certifi fallback) and one medium finding for the standard `/tmp/.X11-unix` socket
directory constant. Calls use explicit argv without shell evaluation; this
directory is detected, not created as a predictable writable temporary file.
No new dependency or CI-source finding was confirmed. Remote CI was not rerun
at this unpublished checkpoint.

Final ZIP: **83,123 bytes**, 27 entries, 21 runtime files.

* ZIP SHA256: `5ed5ae5237b47ac796cd5ae66e16050aa976fc108a20d079e7c91654f7b1dada`
* `main.py`: `885b418e3930c9a536257017fe894cfc77624759a53ad6c6a65e3a60b580c696`
* `dist/index.js`: `487f2f37605a31c565f06886f36dc8917995d7f7568366b945736c833885c456`

All 21 installed runtime files on both devices match this ZIP after diagnostics
were removed. No version, package metadata, release or tag change was made.

## Ordinary SteamOS: actual Legion Go 2

The final tests used the actual mounted QAM and CDP keyboard input, real backend
RPCs and root-atom readback. The wide-gamut SDR measurement waits for the actual
atom to reach the requested value, rather than timing the optimistic RPC reply.

| Operation | Observed completion |
| --- | --- |
| SDR 300 → 299 → 298 → 297% | 162.7 / 158.5 / 161.8 ms |
| HDR 140 → 139 → 138 → 137% | 480.6 / 422.8 / 438.3 ms |
| Cancel active HDR build | RPC 4.1 ms; no late publication after 0.8 s |

These include CDP/RPC/poll overhead. Earlier 10–15 ms observations only measured
state acknowledgement and are deliberately excluded as apply timings. Different
test protocols are not treated as a like-for-like speed comparison.

Both toggles and reset produced the expected root atoms: SDR off/reset is -1,
HDR off/reset clears PQ, and the wide-gamut branch never writes G22. A rapid SDR
burst followed by disable left no late atom write. Rapid HDR RPC edits followed
by a mounted native slider edit and QAM reopening ended at the latest 137%.
The actual PQ file contains 35,937 finite RGB triples within [0,1], with a 33³
header. No temporary LUT or native ARM-worker process remained.

Actual TLS update checking and mounted UI passed. A captured closed updater
handler sent zero extra calls; one positive-control check survived reopening and
displayed Up to date. The fast request completed before a pending Checking state
could be observed after reopening, so this test does not claim that observation.
Pending updater/download remount cases remain covered by compiled regressions
and the earlier explicitly delayed Odin fixtures.

The actual worker log says in-process. Native and frozen executable ELF machine
values are both x86-64/62; the ARM optimization is not selected. One hundred real
get_state calls kept backend FDs **59 → 59**. A source-AST EDID fixture rejected
100 FIFOs in 0.15 ms, FDs **7 → 7**, read a normal symlink, bounded a large file
at 32,769 bytes, and read the real eDP sysfs EDID as 384 bytes despite stat size 0.
These AST fixture checks are distinguished from real backend RPC measurements.

## Armada: final Odin package follow-up

The same final payload was installed after the user powered Odin back on.
The actual frozen executable is x86-64/62 under `/usr/bin/FEX`, native Python is
ARM/183, and the final backend logs select `/proc/self/root/usr/bin/python3`.
One hundred real get_state calls in the clean final instance kept FDs **56 → 56**.

Final controls were exercised through the **real mounted Steam SliderControl and
Toggle onChange handlers**, and DOM clicks of the mounted reset/updater buttons.
They called the normal frontend queue and actual backend, not substitute handlers
or a mock UI. This method is distinguished from physical controller input and
successful CDP navigation input.

| Operation | Observed completion |
| --- | --- |
| SDR G22 300 → 299 → 298 → 297% | 664.3 / 656.6 / 649.0 ms |
| HDR PQ 150 → 149 → 148 → 147% | 653.0 / 650.6 / 662.9 ms |
| Cancel active HDR / SDR build | RPC 4.9 / 49.7 ms; no late publication |

Both actual files passed the 33³, 35,937-triple finite/[0,1] validation. Toggles,
reset, rapid edits, an actual mounted slider handler followed by QAM close/reopen,
real TLS update checking and the mounted Up to date result passed. Restoration
returned SDR/HDR to 300/150%, with both PQ and G22 baseline file hashes and exact
atom paths restored. The final menu screenshot was inspected.

Repeated simulated ArrowLeft navigation requests did not deliver DOM keydown/up
in this run and the desired value stayed unchanged. A single earlier manual
diagnostic did change the real slider to 299%. Further isolated controls proved
that CDP F24 events *were* delivered as trusted events to both QAM and the Steam
main page; DOM dispatch produced the expected untrusted positive-control events.
Thus a blanket claim that all CDP input is broken would be unsupported. The
remaining boundary is navigation input delivery, whose cause was not established
as a Vibrance defect. No Steam/gamemode restart or unrelated-plugin change was
used to repair the input harness. Historical native UI results supplement, but
do not replace, these tests of the final package.

## Preservation, lifecycle and remaining limits

Fresh backups of the plugin, its settings/data and all Decky settings were taken
on each device with ownership/modes retained. No other-plugin configuration or
hardware-control write was issued. Legion preserved **897/897** unrelated file
hashes, 33 CPU, 7 platform/battery and 7 LED values, EDID bytes, the original
SDR/HDR values and active PQ hash. The Decky core and every recorded other-plugin
PID/start time remained unchanged; only Vibrance was reloaded twice.

Odin preserved **146/146** unrelated hashes and **67/67** hardware values, EDID,
exact saved-settings bytes, baseline atoms and both active LUT hashes. Its Decky
bootstrap/core remained PID 1450/1606 throughout this follow-up. Five scoped
Vibrance reloads were needed: final update, failed diagnostic/removal, corrected
diagnostic/removal. No loader service, Steam, gamemode or system restart was
performed in this follow-up.

One Legion reload completed real unload in 0.1 s; the next took 5.1 s and Decky
logged SIGKILL. All five Odin reloads similarly reached Decky's 5.1 s stop budget.
The previously reproduced Decky 3.2.10 EOF starvation is therefore **not confined
to FEX**. Reload queue acknowledgements are not unload-completion timings.
A Legion get_state sent during replacement timed out; the subsequent final
tests and health readback passed. Odin had transient preservation assertions
while generation was still active; idle final readback matched every baseline
value. These intermediate failures are retained, not presented as all-green runs.
The plugin does not modify Decky internals or install a global signal workaround.

MagicBlack mode/opacity remained 1/1. An independent native thread enforced panel
brightness 0 while the audit protection ran: Legion 124,663 samples/606 corrections
over 640.47 s; Odin 137,878 samples/608 corrections over 722.36 s. Both initial
100-sample checks were zero. This sampled control does not prove that brightness
never changed between polls. Own guards and JS locks/listeners were removed;
no native LUT workers or temporary LUTs remained.

Fresh pre-guard brightness was restored and immediately verified: **472000** on
Legion, **1579** on Odin. A later read-only Odin check saw **1125**, without any
further audit brightness write; the identity of that later writer was not proved.
Old Odin restoration values from the previous audit were not reused. Final
read-only checks found both backends ready with no settings/control errors or
pending generation, clean payloads and unchanged unrelated files/hardware values.

No colorimeter, physical gamepad assessment, HDR game run, subjective image
assessment, suspend/resume or docking test was performed in this follow-up.
Updater download completion/error is covered by compiled tests, not a new write
into the user's Downloads folder. The final clean source review found no further
confirmed repair in the plugin; the explicit loader/input boundaries and physical
validation gaps are not relabelled as successful tests.

Private commands, backups, logs, hashes and screenshots remain in the parent
workspace's `.local-validation/DeckyVibranceHDR/2026-10-11-legion-regression` and
`2026-10-11-odin-settings-followup`, with matching private device directories.
They are not packaged or committed.

## Independent closure before publication

Separate read-only SSH checks at **2026-10-11 01:05:27 Europe/Warsaw**
(`2026-10-10T23:05:27Z`) confirmed both final payloads against the ZIP hashes,
baseline settings/EDID/active LUTs, all unrelated-file and hardware comparisons,
ready backends, inactive audit guards and absence of audit globals/workers/tmp
files. QAM screenshots were inspected independently. The fresh final source
review was also accepted with zero further confirmed plugin defects and the
limits above preserved explicitly.

Later brightness readback was 14160 on Legion and 122 on Odin, with audit guards
inactive and MagicBlack still 1/1. These later observations do not replace the
immediate restoration measurements or establish who subsequently wrote brightness.
No further brightness correction or device mutation was performed for publication.
Logical commits, a normal push to main and CI verification follow this independent
closure; version 1.1.0 and the existing tag/release remain unchanged.
