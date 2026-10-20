# Audit and validation history

The full audit and device reports written while preparing versions 1.0.3 to
1.1.1 are kept at the [`1.1.1` tag](https://github.com/Rayekkk/DeckyVibranceHDR/tree/1.1.1/docs)
rather than on `main`. They record the state of the code at that time; versions,
hashes and timings in them are historical. The fixes they describe are covered
by the test suites in [`tests/`](../tests) and summarised in [CHANGELOG.md](../CHANGELOG.md).

## Device validation (latest)

| Report | Covers |
|---|---|
| [Legion regression and Odin follow-up, 2026-10-11](https://github.com/Rayekkk/DeckyVibranceHDR/blob/1.1.1/docs/FULL_AUDIT_2026-10-11_LEGION_REGRESSION.md) | Settings/startup fixes, ordinary SteamOS and Armada checks, final payload comparisons, menu/backend measurements and independent device verification |
| [Odin 3 / Armada audit, 2026-10-10](https://github.com/Rayekkk/DeckyVibranceHDR/blob/1.1.1/docs/FULL_AUDIT_2026-10-10_ODIN3.md) | Blocking metadata reads, native-worker checks and the Decky lifecycle diagnosis |
| [Armada responsiveness, 2026-10-10](https://github.com/Rayekkk/DeckyVibranceHDR/blob/1.1.1/docs/FIX_ARMADA_RESPONSIVENESS_2026-10-10.md) | Native ARM LUT generation and `xprop` execution under FEX, with measurements |
| [Decky RPC fix, 2026-10-10](https://github.com/Rayekkk/DeckyVibranceHDR/blob/1.1.1/docs/FIX_DECKY_RPC_2026-10-10.md) | Menu-to-backend connection on Legion Go 2 (in Polish) |

## Code quality audits

| Report | Covers |
|---|---|
| [Round 9](https://github.com/Rayekkk/DeckyVibranceHDR/blob/1.1.1/docs/FULL_AUDIT_2026-10-10_ROUND9.md) | Frontend request ordering, saved settings and updater remount |
| [Round 8](https://github.com/Rayekkk/DeckyVibranceHDR/blob/1.1.1/docs/FULL_AUDIT_ROUND8_2026-10-10.md) | Clean re-review after round 7 (in Polish) |
| [Round 7](https://github.com/Rayekkk/DeckyVibranceHDR/blob/1.1.1/docs/FULL_AUDIT_ROUND7_2026-10-10.md) | Startup/unload races (in Polish) |
| [Round 6](https://github.com/Rayekkk/DeckyVibranceHDR/blob/1.1.1/docs/FULL_AUDIT_ROUND6_2026-10-10.md) | Failed LUT reasserts and pending replacements (in Polish) |
| [Round 5](https://github.com/Rayekkk/DeckyVibranceHDR/blob/1.1.1/docs/FULL_AUDIT_ROUND5_2026-10-10.md) | Disk latency, monitor queries and LUT reuse (in Polish) |
| [Round 4](https://github.com/Rayekkk/DeckyVibranceHDR/blob/1.1.1/docs/FULL_AUDIT_ROUND4_2026-10-10.md) | Redundant work, concurrent update checks (in Polish) |
| [Round 3](https://github.com/Rayekkk/DeckyVibranceHDR/blob/1.1.1/docs/FULL_AUDIT_ROUND3_2026-10-10.md) | Bounded controls, monitor latency, update metadata (in Polish) |
| [Second full audit](https://github.com/Rayekkk/DeckyVibranceHDR/blob/1.1.1/docs/FULL_AUDIT_2026-10-10.md) | Display changes, slow HTTP, release validation (in Polish) |
| [1.0.3 repair report](https://github.com/Rayekkk/DeckyVibranceHDR/blob/1.1.1/docs/REPAIR_REPORT.md) | The 14 findings of the first audit (in Polish) |

## What automated checks cannot replace

The suites fake gamescope, the filesystem and the network. They do not cover
physical gamepad input, colorimeter measurements, HDR game output, sleep/wake
or docking on real hardware; those still need a device pass before a release.
