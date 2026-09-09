"""Fail closed on incomplete release declarations and forbidden artifact types."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
GATES = ("source_frozen", "license_approved", "clean_environment_verified", "complete_regressions_verified", "dependency_audit_complete", "privacy_review_complete", "user_acceptance_complete", "publication_authorized")
IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache"}
DENIED_DIRS = {"data", "real-data", "logs", "backups", "recovery", "private", "secrets", "verification", "evidence"}
DENIED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key", ".p12", ".pfx", ".zip", ".7z", ".mp4", ".apk"}


def failures(root: Path) -> list[str]:
    result = []
    try:
        state = json.loads((root / "release-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
        result.append("release-state.json missing or invalid")
    for key in GATES:
        if state.get(key) is not True:
            result.append(f"Unfinished release gate: {key}")
    license_path = root / "LICENSE"
    if not license_path.is_file() or license_path.stat().st_size < 200:
        result.append("An approved, complete LICENSE is required; draft/status files are not licenses")
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRS for part in relative.parts):
            continue
        if path.is_symlink():
            result.append(f"Linked path requires manual review: {relative.as_posix()}")
            continue
        if any(part in DENIED_DIRS for part in relative.parts):
            result.append(f"Private/runtime artifact: {relative.as_posix()}")
        if not path.is_file():
            continue
        name = path.name.lower()
        if path.suffix.lower() in DENIED_SUFFIXES or re.search(r"\.(?:db|sqlite\d*)-(?:wal|shm|journal)$", name) or (name.startswith(".env") and not name.endswith(".example")) or (name.endswith(".env") and not name.endswith(".example")):
            result.append(f"Forbidden public artifact: {relative.as_posix()}")
    return sorted(set(result))


def main() -> int:
    result = failures(ROOT)
    print(json.dumps({"publication_ready": not result, "failures": result, "scope": "structural gate only; not a complete content/history/license audit"}, ensure_ascii=False, indent=2))
    return 1 if result else 0


if __name__ == "__main__":
    raise SystemExit(main())
