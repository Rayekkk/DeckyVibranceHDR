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
import { existsSync, mkdirSync, rmSync, cpSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(readFileSync(join(repoRoot, "plugin.json"), "utf8"));

// Decky installs the folder as-is; the repo/tag name uses no separator.
const PLUGIN_DIR_NAME = "DeckyVibranceHDR";
const version = manifest.version;

const buildDir = join(repoRoot, "build");
const stageDir = join(buildDir, PLUGIN_DIR_NAME);
const zipPath = join(repoRoot, `${PLUGIN_DIR_NAME}-${version}.zip`);

/** Runtime payload only - no sources, lockfiles, git, node_modules, or tests. */
const CONTENTS = [
  "main.py",
  "vibrance_lut.py",
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
rmSync(zipPath, { force: true });
mkdirSync(stageDir, { recursive: true });

for (const entry of CONTENTS) {
  const from = join(repoRoot, entry);
  if (!existsSync(from)) fail(`required file missing: ${entry}`);
  cpSync(from, join(stageDir, entry), {
    recursive: true,
    // Keep build artefacts and caches out of the archive.
    filter: (src) => !/(__pycache__|\.pyc$|\.DS_Store|node_modules)/.test(src),
  });
}

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
    execFileSync(sevenZipPath, ["a", "-tzip", "-mx=9", zipPath, PLUGIN_DIR_NAME],
      { cwd: buildDir, stdio: "inherit" });
  } else {
    execFileSync("zip", ["-r", "-9", "-q", zipPath, PLUGIN_DIR_NAME],
      { cwd: buildDir, stdio: "inherit" });
  }
} catch (err) {
  fail(
    sevenZipPath
      ? `7-Zip failed: ${err.message}`
      : `no zip tool found. Install 7-Zip (Windows) or the 'zip' package (Linux/macOS): ${err.message}`,
  );
}

rmSync(buildDir, { recursive: true, force: true });
console.log(`packaged v${version} -> ${zipPath}`);
