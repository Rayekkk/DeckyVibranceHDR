# Armada / Odin 3 responsiveness - 2026-10-10

Decky Loader on this Odin 3 is an x86-64 ELF executable running via FEX on an aarch64 host. Forking this emulated process for xprop caused large delays, including time spent holding the state mutation lock. Python LUT calculation also ran under emulation. A warm pre-change HDR test measured 1377.8 ms; an intermediate native-generator-only measurement was 6566.7 ms, showing that native LUT generation alone did not resolve the problem.

The final implementation selects the ARM worker only when ELF headers identify an ARM system Python and an x86-64 loader. The host interpreter path uses /proc/self/root to bypass FEX guest filesystem redirection without requiring access to PID 1. Ordinary x86-64 SteamOS keeps its original in-process generator and xprop invocation.

ARM LUT jobs run the same vibrance_lut.py with isolated native Python, scrubbed loader/Python environment, cancellable communication, a 30-second deadline, and kill/reap on cancellation. File publication remains atomic and generation-checked. The 33-cube and color mathematics are unchanged. ARM xprop calls use an absolute host executable and close_fds=False to enable posix_spawn rather than forking the emulated Decky process. Python descriptors are non-inheritable by default.

## Measurements on actual Odin 3

- HDR change through real Decky RPC, including debounce and applied-LUT confirmation: 590.4, 569.3, 570.3 ms.
- SDR change through real Decky RPC, including debounce and applied-LUT confirmation: 662.4, 691.0 ms.
- Disable while generation was confirmed active: HDR 41 ms; SDR 121 ms.
- Enable RPC acknowledgement: HDR 3.4 ms; SDR 49.2 ms. A newly required LUT still takes the build time above; acknowledgement is not physical effect completion.
- Menu rendered both SDR/HDR controls and correct restored values (300% SDR, 150% HDR), without Backend unavailable.
- Final backend: ready, both LUTs applied, no errors and no pending generation.

These are limited device measurements, not worst-case guarantees or frame/physical-color measurements. Startup logs changed from roughly 20 seconds between settled gamescope and both LUTs applied to about 1 second. General Steam/Decky initialization is outside this interval.

## Validation

253 Python tests, 21 Node tests, 1 built frontend contract test passed (275 total). Six new regression tests cover native output equivalence for PQ/G22 and white points, architecture gating, worker cancellation/reaping, validation/partial-output failures, unchanged SteamOS fallback, and ARM xprop spawning. Typecheck, build, ZIP verification and device byte comparison passed. SteamOS hardware was not modified for this patch; its path is covered by the existing regressions and explicit fallback tests.

Installed ZIP SHA256: 512bcf00d1fc6a368b8c88cb300728a60e861c7896d9390ba48ac2b57c779514.
Backup: /var/home/armada/homebrew/backups/DeckyVibranceHDR-20261010T002113Z.
No commit or push.
