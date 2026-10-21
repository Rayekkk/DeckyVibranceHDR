"""Validate the actual installable ZIP against this checkout, after packaging."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile

# Everything that ends up in, or decides, the ZIP. Docs and tests do not.
SHIPPED = ("main.py", "lego_updater.py", "vibrance_lut.py", "vibrance_edid.py", "plugin.json",
           "package.json", "package-lock.json", "README.md", "CHANGELOG.md", "LICENSE", "NOTICE",
           "src", "rollup.config.js", "tsconfig.json", "scripts/package.mjs")


def require(condition, message):
    # Validation must run under PYTHONOPTIMIZE / python -O as well.
    if not condition:
        raise ValueError(message)


def verify(root):
    root = Path(root)
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    path = root / f"DeckyVibranceHDR-{version}.zip"
    prefix = "DeckyVibranceHDR/"
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "duplicate ZIP paths")
        require(archive.testzip() is None, "ZIP CRC failure")
        require(all(info.orig_filename == info.filename and "\\" not in info.orig_filename
                    for info in archive.infolist()), "noncanonical ZIP path separators")
        for name in names:
            parts = name.rstrip("/").split("/")
            require(name.startswith(prefix) and "\\" not in name and ":" not in name
                    and not any(part in ("", ".", "..") for part in parts), "unsafe ZIP path")
            require(not any(part in parts for part in ("node_modules", "tests", ".git", "__pycache__")),
                    "non-runtime file in ZIP")
        dist_files = {name.removeprefix(prefix + "dist/") for name in names
                      if name.startswith(prefix + "dist/") and not name.endswith("/")}
        require(dist_files == {"index.js", "index.js.map"}, "stale/unexpected frontend build files")
        files = ("main.py", "lego_updater.py", "vibrance_lut.py", "vibrance_edid.py", "plugin.json", "package.json",
                 "README.md", "CHANGELOG.md", "LICENSE", "NOTICE", "dist/index.js", "dist/index.js.map")
        for name in files:
            require(archive.read(prefix + name) == (root / name).read_bytes(), f"stale/missing payload: {name}")
        package = json.loads(archive.read(prefix + "package.json"))
        require(package["version"] == version, "package version mismatch")
        api = root / "node_modules" / "@decky" / "api"
        for name in ("LICENSE", "package.json", "src/index.ts", "src/types.ts", "dist/index.js"):
            require(archive.read(prefix + "third_party/decky-api/" + name) == (api / name).read_bytes(),
                    f"stale/missing library payload: {name}")
        # node_modules can lag the lockfile after a pull without npm ci.
        lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
        locked = lock["packages"]["node_modules/@decky/api"]["version"]
        bundled = json.loads(archive.read(prefix + "third_party/decky-api/package.json"))["version"]
        require(bundled == locked, f"@decky/api {bundled} bundled but {locked} locked; run npm ci")
        licence = archive.read(prefix + "third_party/decky-api/LICENSE")
        require(b"GNU LESSER GENERAL PUBLIC LICENSE" in licence, "missing library licence")
        check_source_map(root, json.loads(archive.read(prefix + "dist/index.js.map")))
    return {"path": str(path), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "entries": len(names)}



def check_source_map(root, source_map):
    """The map must be complete, rewritten, and built from the src/ on disk.

    dist/ is not committed, so the embedded sources are what ties the shipped
    frontend to the checkout: a ZIP built at one commit and verified at
    another, with only src/ changed, would otherwise pass.
    """
    sources, contents = source_map["sources"], source_map["sourcesContent"]
    require(any("@decky/api" in source for source in sources), "library source map missing")
    require(len(contents) == len(sources) and all(isinstance(text, str) for text in contents),
            "incomplete source map")
    require(all(source.startswith("decky://") for source in sources),
            "source map paths were not rewritten to decky:// URLs")
    shipped = {source.split("/src/", 1)[1]: text for source, text in zip(sources, contents)
               if "/src/" in source and "/node_modules/" not in source}
    require(set(shipped) == {path.name for path in (root / "src").iterdir()},
            "the source map does not cover every file in src/")
    for name, text in shipped.items():
        # Bytes, so a CRLF checkout compares like for like.
        require(text == (root / "src" / name).read_bytes().decode("utf-8"),
                f"stale frontend build: src/{name} changed since dist/ was built")

def committed(root):
    """A release ZIP is built locally, so it must match a commit, not a dirty tree."""
    if not (root / ".git").exists():
        return  # not a git checkout: nothing to compare against
    # Inside a checkout a failing git (missing, or "dubious ownership") must
    # stop the release rather than skip the check.
    status = subprocess.run(["git", "status", "--porcelain", "--", *SHIPPED], cwd=root,
                            capture_output=True, text=True, check=True).stdout
    require(not status.strip() or os.environ.get("DVH_ALLOW_DIRTY") == "1",
            "uncommitted changes would ship in the ZIP; commit them first "
            "(or set DVH_ALLOW_DIRTY=1 for a local trial build):\n" + status)


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    committed(root)
    print(json.dumps(verify(root), indent=2))
