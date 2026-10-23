#!/usr/bin/env node
/**
 * Builds the release zip that Decky's "Install Plugin from ZIP" accepts.
 *
 * Files are staged into build/<PLUGIN_NAME>/ first, so the archive always has
 * the right root folder regardless of what the checkout directory is called,
 * and only runtime files ever make it in.
 *
 * Zipping is delegated to 7-Zip on Windows and `zip` elsewhere. PowerShell's
 * Compress-Archive is deliberately not used: it stores backslash separators
 * and the resulting archive fails to extract on Linux.
 */

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, rmSync, cpSync, readFileSync, readdirSync, renameSync, utimesSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(readFileSync(join(repoRoot, "plugin.json"), "utf8"));

// Decky installs the folder as-is; the repo/tag name uses no separator.
const PLUGIN_DIR_NAME = "DeckyVibranceHDR";
const version = manifest.version;

const buildDir = join(repoRoot, "build");
const stageDir = join(buildDir, PLUGIN_DIR_NAME);
const zipPath = join(repoRoot, `${PLUGIN_DIR_NAME}-${version}.zip`);
const pendingZip = join(buildDir, "release.zip");

/** Runtime payload only - no sources, lockfiles, git, node_modules, or tests. */
const CONTENTS = [
  "main.py",
  // Every backend module, so a new one cannot be left out of the package.
  ...readdirSync(repoRoot).filter(name => /^vibrance_\w+\.py$/.test(name)).sort(),
  "lego_updater.py",
  "plugin.json",
  "package.json",
  "README.md",
  "CHANGELOG.md",
  "LICENSE",
  "NOTICE",
  "dist",
];

function fail(message) {
  console.error(`error: ${message}`);
  process.exit(1);
}

if (!existsSync(join(repoRoot, "dist", "index.js"))) {
  fail("dist/index.js is missing - run `npm run build` first");
}

const pkgVersion = JSON.parse(
  readFileSync(join(repoRoot, "package.json"), "utf8"),
).version;
if (pkgVersion !== version) {
  fail(`version mismatch: plugin.json=${version} package.json=${pkgVersion}`);
}

rmSync(buildDir, { recursive: true, force: true });
mkdirSync(stageDir, { recursive: true });

for (const entry of CONTENTS) {
  const from = join(repoRoot, entry);
  if (!existsSync(from)) fail(`required file missing: ${entry}`);
  cpSync(from, join(stageDir, entry), {
    recursive: true,
    // Keep build artefacts and caches out of the archive.
    filter: (src) => !["__pycache__", ".DS_Store", "node_modules"].includes(basename(src))
      && !basename(src).endsWith(".pyc"),
  });
}

// @decky/api is compiled into dist/index.js. Ship its licence and exact source
// so recipients can inspect, modify and rebuild the library portion.
const apiDir = join(repoRoot, "node_modules", "@decky", "api");
const apiStage = join(stageDir, "third_party", "decky-api");
mkdirSync(apiStage, { recursive: true });
for (const entry of ["LICENSE", "package.json", "dist", "src", "README.md"]) {
  if (existsSync(join(apiDir, entry))) cpSync(join(apiDir, entry), join(apiStage, entry), { recursive: true });
  else if (["LICENSE", "package.json", "dist", "src"].includes(entry)) fail(`@decky/api ${entry} is missing`);
}

// The same files give the same archive: fixed timestamps (zip stores local
// time, hence a local date) and one sorted entry list instead of readdir order.
const FIXED_TIME = new Date(2000, 0, 1);
const entries = [PLUGIN_DIR_NAME, ...readdirSync(stageDir, { recursive: true })
  .map(path => `${PLUGIN_DIR_NAME}/${path.replaceAll("\\", "/")}`)].sort();
for (const entry of entries) utimesSync(join(buildDir, entry), FIXED_TIME, FIXED_TIME);

function sevenZip() {
  const candidates = [
    "C:\\Program Files\\7-Zip\\7z.exe",
    "C:\\Program Files (x86)\\7-Zip\\7z.exe",
  ];
  return candidates.find(existsSync);
}

const sevenZipPath = sevenZip();
try {
  if (sevenZipPath) {
    // -mtm=off: no NTFS timestamps, every entry is 1980-01-01 whatever the time zone.
    execFileSync(sevenZipPath, ["a", "-tzip", "-mx=9", "-mtm=off", pendingZip, PLUGIN_DIR_NAME],
      { cwd: buildDir, stdio: "inherit" });
  } else {
    // -X drops the extra timestamps and owners; -@ reads the sorted entry list.
    execFileSync("zip", ["-X", "-9", "-q", pendingZip, "-@"],
      { cwd: buildDir, input: entries.join("\n"), stdio: ["pipe", "inherit", "inherit"] });
  }
} catch (err) {
  fail(
    sevenZipPath
      ? `7-Zip failed: ${err.message}`
      : `no zip tool found. Install 7-Zip (Windows) or the 'zip' package (Linux/macOS): ${err.message}`,
  );
}

renameSync(pendingZip, zipPath);
rmSync(buildDir, { recursive: true, force: true });
console.log(`packaged v${version} -> ${zipPath}`);
