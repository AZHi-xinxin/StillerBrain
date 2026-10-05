"""Create a private atlas config candidate; never apply it or start a service.

Only an explicitly supplied source config and, when needed, sqlite_schema in
its main DB are read. No memory body, password file or management store is read.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 64 * 1024


def helper(name):
    spec = importlib.util.spec_from_file_location("atlas_prepare_" + name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def read_source(path):
    """Bounded no-follow read; also reject hard-linked secret files."""
    installer = helper("install_stiller")
    path = installer.safe_directory(path)
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > LIMIT:
        raise ValueError("atlas_source_not_private_regular_file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if fingerprint(os.fstat(stream.fileno())) != fingerprint(before):
            raise ValueError("atlas_source_changed")
        raw = stream.read(LIMIT + 1)
        after_handle = os.fstat(stream.fileno())
    installer.safe_directory(path)
    if (len(raw) > LIMIT or fingerprint(before) != fingerprint(after_handle) or
            fingerprint(before) != fingerprint(path.stat(follow_symlinks=False))):
        raise ValueError("atlas_source_changed")
    return raw, fingerprint(before)


def database_state(path):
    """Read metadata only. Non-empty SQLite sidecars require a stopped backup."""
    installer = helper("install_stiller")
    path = installer.safe_directory(path)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        installer.safe_directory(sidecar)
        if sidecar.exists():
            info = sidecar.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_size or info.st_nlink != 1:
                raise ValueError("atlas_database_requires_quiet_snapshot")
    if not path.exists():
        return None
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("atlas_database_not_regular_file")
    return fingerprint(info)


def schema_evidence(path):
    """Immutable SQLite prevents journal/SHM creation; no data-table queries.

    Requires quiet, sidecar-free input. This is candidate preparation, not a
    running-instance stop proof; application still needs native maintenance.
    """
    path = helper("install_stiller").safe_directory(path)
    before = database_state(path)
    if before is None:
        return False, before
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)) as db:
        deadline = time.monotonic() + 2.0
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        found = db.execute("SELECT 1 FROM sqlite_schema WHERE name=? LIMIT 1",
                           ("orbis_atlas_device_grants",)).fetchone() is not None
    if database_state(path) != before:
        raise ValueError("atlas_database_changed")
    return found, before


def create_candidate(source, destination, *, enable=False):
    installer = helper("install_stiller")
    atlas = helper("atlas_config")
    source = installer.safe_directory(source)
    destination = installer.safe_directory(destination)
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("atlas_candidate_requires_new_directory")
    raw, source_state = read_source(source)
    config = helper("run_component").parse_config(raw.decode("utf-8"))
    atlas.validate(config)
    database, before_database, schema_found = None, None, False
    if atlas.ID_KEY not in config and config.get(atlas.ENABLED) != "0":
        if not enable:
            raise ValueError("atlas_enable_requires_explicit_option")
        database = config.get("STBRAIN_DB_PATH")
        if not database:
            raise ValueError("atlas_database_path_required")
        schema_found, before_database = schema_evidence(database)
    candidate = atlas.prepare(config, enable_missing=enable, prior_atlas_schema=schema_found)
    # Preserve comments, ordering, all unknown values and line endings. Only
    # the three atlas keys may be added/replaced in the candidate copy.
    lines, seen = [], set()
    for line in raw.decode("utf-8").splitlines(keepends=True):
        name = line.partition("=")[0]
        if name in atlas.ATLAS_KEYS:
            newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines.append(name + "=" + candidate[name] + newline)
            seen.add(name)
        else:
            lines.append(line)
    rendered = "".join(lines)
    for name in atlas.ATLAS_KEYS:
        if name in candidate and name not in seen:
            if rendered and not rendered.endswith(("\r", "\n")):
                rendered += "\n"
            rendered += name + "=" + candidate[name] + "\n"
    if helper("run_component").parse_config(rendered) != candidate or len(rendered.encode("utf-8")) > LIMIT:
        raise ValueError("atlas_candidate_invalid")

    def unchanged():
        fresh, state = read_source(source)
        if fresh != raw or state != source_state:
            raise ValueError("atlas_source_changed")
        if database is not None and database_state(database) != before_database:
            raise ValueError("atlas_database_changed")

    unchanged()
    destination.mkdir(mode=0o700)
    installer.protect_directory(destination)
    # A failure after creation leaves an incomplete private candidate, never
    # applies it and never deletes evidence. Re-running needs a new directory.
    installer.exclusive_write(destination / "config.env.candidate", rendered)
    unchanged()
    receipt = {
        "schema": "st-atlas-config-candidate/1",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "candidate_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        "changed_keys": [name for name in atlas.ATLAS_KEYS if config.get(name) != candidate.get(name)],
        "enabled": candidate.get(atlas.ENABLED) == "1",
        "existing_id_key_preserved": atlas.ID_KEY in config,
        "schema_checked": database is not None,
        "applied": False, "services_started": False, "databases_modified": False,
    }
    installer.exclusive_write(destination / "candidate.json", json.dumps(receipt, indent=2) + "\n")
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare a private atlas config candidate; never apply it.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new private directory outside source")
    parser.add_argument("--enable", action="store_true", help="enable only when flag is missing; explicit 0 is preserved")
    args = parser.parse_args(argv)
    try:
        result = create_candidate(args.config, args.output, enable=args.enable)
    except (ValueError, OSError, sqlite3.Error):
        # Parse errors and OS exceptions may contain source values or paths.
        print("Atlas candidate refused. Original configuration is unchanged; inspect privately and use docs/ATLAS.md.", file=sys.stderr)
        return 1
    print("Private atlas candidate created; NOT applied. Changed keys: " + ", ".join(result["changed_keys"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
