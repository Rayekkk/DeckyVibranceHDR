import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cpSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";

test("a cache-like checkout directory name cannot filter runtime files", () => {
  const base = resolve(tmpdir());
  const folder = mkdtempSync(join(base, "node_modules-dvh-package-"));
  try {
    mkdirSync(join(folder, "scripts"));
    mkdirSync(join(folder, "dist"));
    cpSync(new URL("../scripts/package.mjs", import.meta.url), join(folder, "scripts/package.mjs"));
    for (const name of ["plugin.json", "package.json"])
      writeFileSync(join(folder, name), JSON.stringify({ version: "1.1.0" }));
    for (const name of ["main.py", "vibrance_lut.py", "lego_updater.py", "dist/index.js"])
      writeFileSync(join(folder, name), "placeholder");
    const result = spawnSync(process.execPath, [join(folder, "scripts/package.mjs")], { encoding: "utf8" });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /required file missing: README.md/);
    assert.equal(readFileSync(join(folder, "build/DeckyVibranceHDR/main.py"), "utf8"), "placeholder");
  } finally {
    assert.equal(dirname(folder), base);
    assert.ok(folder.startsWith(join(base, "node_modules-dvh-package-")));
    rmSync(folder, { recursive: true, force: true });
  }
});

test("packaging failure keeps an already created release ZIP", () => {
  const base = resolve(tmpdir());
  const folder = mkdtempSync(join(base, "dvh-package-"));
  try {
    mkdirSync(join(folder, "scripts"));
    mkdirSync(join(folder, "dist"));
    cpSync(new URL("../scripts/package.mjs", import.meta.url), join(folder, "scripts/package.mjs"));
    writeFileSync(join(folder, "plugin.json"), JSON.stringify({ version: "1.1.0" }));
    writeFileSync(join(folder, "package.json"), JSON.stringify({ version: "1.1.0" }));
    for (const name of ["main.py", "vibrance_lut.py", "lego_updater.py", "dist/index.js"])
      writeFileSync(join(folder, name), "placeholder");
    const zip = join(folder, "DeckyVibranceHDR-1.1.0.zip");
    writeFileSync(zip, "previous archive");
    const result = spawnSync(process.execPath, [join(folder, "scripts/package.mjs")], { encoding: "utf8" });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /required file missing: README.md/);
    assert.equal(readFileSync(zip, "utf8"), "previous archive");
  } finally {
    // The target is the dedicated directory returned by mkdtemp, within Temp.
    assert.equal(dirname(folder), base);
    assert.ok(folder.startsWith(join(base, "dvh-package-")));
    rmSync(folder, { recursive: true, force: true });
  }
});
