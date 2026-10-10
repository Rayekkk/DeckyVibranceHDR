"""Validate the actual installable ZIP against this checkout, after packaging."""
import hashlib
import json
from pathlib import Path
import zipfile


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
        files = ("main.py", "lego_updater.py", "vibrance_lut.py", "plugin.json", "package.json",
                 "README.md", "CHANGELOG.md", "LICENSE", "NOTICE", "dist/index.js", "dist/index.js.map")
        for name in files:
            require(archive.read(prefix + name) == (root / name).read_bytes(), f"stale/missing payload: {name}")
        package = json.loads(archive.read(prefix + "package.json"))
        require(package["version"] == version, "package version mismatch")
        api = root / "node_modules" / "@decky" / "api"
        for name in ("LICENSE", "package.json", "src/index.ts", "src/types.ts", "dist/index.js"):
            require(archive.read(prefix + "third_party/decky-api/" + name) == (api / name).read_bytes(),
                    f"stale/missing library payload: {name}")
        licence = archive.read(prefix + "third_party/decky-api/LICENSE")
        require(b"GNU LESSER GENERAL PUBLIC LICENSE" in licence, "missing library licence")
        source_map = json.loads(archive.read(prefix + "dist/index.js.map"))
        require(any("@decky/api" in source for source in source_map["sources"]), "library source map missing")
        require(len(source_map["sourcesContent"]) == len(source_map["sources"])
                and all(isinstance(source, str) for source in source_map["sourcesContent"]), "incomplete source map")
    return {"path": str(path), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "entries": len(names)}


if __name__ == "__main__":
    print(json.dumps(verify(Path(__file__).resolve().parent.parent), indent=2))
