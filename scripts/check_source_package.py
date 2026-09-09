"""Read-only source-archive integrity check, separate from production readiness."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "SOURCE-PACKAGE-MANIFEST.json"
REQUIRED = {"LICENSE", "NOTICE", "README.md", "COMMERCIAL-LICENSE.md",
            "CONTRIBUTING.md", "SECURITY.md", "requirements.txt",
            "requirements-windows-py314.lock", "scripts/run_component.py"}
DENIED_DIRS = {".venv", "venv", "node_modules", "__pycache__", "data", "real-data",
               "logs", "backups", "recovery", "private", "secrets", "verification",
               "evidence", "private-verification", "build", "dist"}
DENIED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key", ".p12",
                   ".pfx", ".zip", ".7z", ".mp4", ".apk", ".pyc", ".pyo", ".exe"}


def linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def inventory(root: Path) -> tuple[dict[str, Path], list[str]]:
    root = root.absolute()
    if any(linked(part) for part in (root, *root.parents)):
        return {}, ["Linked archive root or ancestor"]
    files, errors = {}, []
    for directory, dirs, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in list(dirs):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if name == ".git" and parent == root:
                dirs.remove(name)  # Git history needs its own review before pushing.
            elif linked(path) or name.casefold() in DENIED_DIRS or name.casefold() == ".git":
                errors.append("Forbidden directory: " + relative)
                dirs.remove(name)
        for name in names:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if linked(path):
                errors.append("Linked file: " + relative)
                continue
            low = name.lower()
            real_env = (low.startswith(".env") or low.endswith(".env")) and not low.endswith(".example")
            if path.suffix.lower() in DENIED_SUFFIXES or real_env or re.search(r"\.(?:db|sqlite\d*)-(?:wal|shm|journal)$", low):
                errors.append("Forbidden artifact: " + relative)
            if path.stat().st_size > 25 * 1024 * 1024:
                errors.append("Exceeds browser per-file upload limit: " + relative)
            files[relative] = path
    return files, errors


def failures(root: Path) -> list[str]:
    files, result = inventory(root)
    if not files:
        return result + ["Source archive is empty or unavailable"]
    for name in sorted(REQUIRED - files.keys()):
        result.append("Required source-package file missing: " + name)
    try:
        manifest = json.loads(files[MANIFEST].read_text(encoding="utf-8"))
        if manifest.get("format") != "stiller-source-package/1" or manifest.get("distribution") != "source-available-development-preview":
            raise ValueError("manifest identity")
        rows = manifest["files"]
        if not isinstance(rows, list) or not rows:
            raise ValueError("manifest rows")
        expected = {}
        for row in rows:
            name, digest = row["path"], row["sha256"]
            path = PurePosixPath(name)
            if (not isinstance(name, str) or path.is_absolute() or ".." in path.parts
                    or "\\" in name or ":" in name or str(path) != name
                    or name in expected or name == MANIFEST
                    or not re.fullmatch(r"[a-f0-9]{64}", digest)):
                raise ValueError("unsafe or duplicate manifest row")
            expected[name] = digest
        actual = set(files) - {MANIFEST}
        for name in sorted(actual ^ set(expected)):
            result.append("Archive manifest file-set mismatch: " + name)
        for name in sorted(actual & set(expected)):
            if hashlib.sha256(files[name].read_bytes()).hexdigest() != expected[name]:
                result.append("Archive content hash mismatch: " + name)
        if "LICENSE" in files and files["LICENSE"].stat().st_size < 200:
            result.append("Complete license text required")
    except (OSError, KeyError, TypeError, ValueError):
        result.append("Source manifest missing or invalid")
    return sorted(set(result))


if __name__ == "__main__":
    errors = failures(ROOT)
    print(json.dumps({"source_archive_integrity_ok": not errors, "failures": errors,
                      "scope": "source files and recorded hashes only; not a legal opinion, secret-history audit, production or model-cost certification"},
                     ensure_ascii=False, indent=2))
    raise SystemExit(1 if errors else 0)
