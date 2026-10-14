"""Standard-library, synthetic-only POSIX storage probe for the phone installer.

Only a newly created child of an explicitly selected existing parent is used.
Failed probe directories are retained; successful probes remove only their own
known files. No user configuration, database, credentials or network is read.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile


PREFIX = "st-phone-storage-probe-"
CAPABILITIES = ("private_permissions", "atomic_replace_fsync", "sqlite_wal", "process_lock")
OWNED_NAMES = frozenset({"permissions.txt", "atomic-old.txt", "atomic-new.txt",
                         "probe.sqlite3", "probe.sqlite3-wal", "probe.sqlite3-shm",
                         "probe.sqlite3-journal", "probe.lock"})
LOCK_BUSY = 23


class ProbeError(Exception):
    """Fixed categories only; never include OS paths or arbitrary exception text."""


def _posix():
    return os.name == "posix"


def _parent(value):
    path = Path(value)
    if not path.is_absolute():
        raise ProbeError("parent_not_absolute")
    if any(part.is_symlink() or getattr(part, "is_junction", lambda: False)()
           for part in (path, *path.parents)):
        raise ProbeError("parent_linked")
    if not path.is_dir():
        raise ProbeError("parent_not_directory")
    return path.resolve(strict=True)


def _identity(path):
    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode):
        raise ProbeError("probe_directory_changed")
    return value.st_dev, value.st_ino


def _create_file(path, content=b"synthetic storage probe\n"):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _permissions(directory):
    os.chmod(directory, 0o700)
    path = directory / "permissions.txt"
    _create_file(path)
    if (stat.S_IMODE(directory.stat().st_mode) != 0o700
            or stat.S_IMODE(path.stat().st_mode) != 0o600):
        raise ProbeError("private_permissions_unavailable")


def _atomic(directory):
    old, new = directory / "atomic-old.txt", directory / "atomic-new.txt"
    _create_file(old, b"old synthetic value\n")
    _create_file(new, b"new synthetic value\n")
    os.replace(new, old)
    _sync_directory(directory)
    if new.exists() or old.read_bytes() != b"new synthetic value\n":
        raise ProbeError("atomic_replace_readback_failed")


def _sqlite(directory):
    path = directory / "probe.sqlite3"
    _create_file(path, b"")
    with closing(sqlite3.connect(path, timeout=2)) as connection:
        if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
            raise ProbeError("sqlite_wal_unavailable")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO probe VALUES (1, ?)", ("synthetic 中文 🌟",))
        connection.commit()
        if not (directory / "probe.sqlite3-wal").is_file():
            raise ProbeError("sqlite_wal_not_created")
        with closing(sqlite3.connect(path, timeout=2)) as reader:
            if reader.execute("SELECT value FROM probe WHERE id=1").fetchone() != ("synthetic 中文 🌟",):
                raise ProbeError("sqlite_readback_failed")
    with closing(sqlite3.connect(path, timeout=2)) as reopened:
        if (reopened.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                or reopened.execute("SELECT value FROM probe WHERE id=1").fetchone()
                != ("synthetic 中文 🌟",)):
            raise ProbeError("sqlite_reopen_integrity_failed")
    _sync_directory(directory)


def _flock_child(path, device, inode):
    """Private subprocess protocol: 0 acquired, 23 excluded, 24 unsafe/error."""
    if not _posix():
        return 24
    import fcntl
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        value = os.fstat(descriptor)
        if (not stat.S_ISREG(value.st_mode)
                or (value.st_dev, value.st_ino) != (device, inode)):
            return 24
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return LOCK_BUSY
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return 0
    except OSError:
        return 24
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _child_result(path, identity):
    # subprocess.run kills and reaps this exact child on timeout. No shell,
    # process discovery, process-group killing, daemon or grandchildren.
    result = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                             "--_flock-check", str(path), str(identity[0]), str(identity[1])],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=10, check=False,
                            close_fds=True)
    return result.returncode


def _process_lock(directory):
    import fcntl
    path = directory / "probe.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        value = os.fstat(descriptor)
        identity = value.st_dev, value.st_ino
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if _child_result(path, identity) != LOCK_BUSY:
            raise ProbeError("process_lock_not_exclusive")
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        if _child_result(path, identity) != 0:
            raise ProbeError("process_lock_not_released")
    finally:
        os.close(descriptor)


def _cleanup(directory, parent, identity):
    # No recursive deletion. The fresh directory, parent and complete set of
    # known direct children must still match before any removal is attempted.
    if directory.parent != parent or not directory.name.startswith(PREFIX) or _identity(directory) != identity:
        raise ProbeError("cleanup_scope_changed")
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != identity:
            raise ProbeError("cleanup_scope_changed")
        names = os.listdir(descriptor)
        if any(name not in OWNED_NAMES
               or not stat.S_ISREG(os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode)
               for name in names):
            raise ProbeError("cleanup_unexpected_entry")
        for name in names:
            os.unlink(name, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    if _identity(directory) != identity:
        raise ProbeError("cleanup_scope_changed")
    directory.rmdir()


def run_probe(parent):
    """Return capability booleans and fixed error categories; never raw paths."""
    result = {"supported": _posix(), "ok": False,
              "capabilities": {name: False for name in CAPABILITIES}, "errors": []}
    if not result["supported"]:
        result["errors"].append("unsupported_platform")
        return result
    stage = "parent"
    try:
        parent = _parent(parent)
        stage = "create_probe"
        directory = Path(tempfile.mkdtemp(prefix=PREFIX, dir=parent))
        identity = _identity(directory)
        for stage, operation in (("private_permissions", _permissions),
                                 ("atomic_replace_fsync", _atomic),
                                 ("sqlite_wal", _sqlite), ("process_lock", _process_lock)):
            if _identity(directory) != identity:
                raise ProbeError("probe_directory_changed")
            operation(directory)
            result["capabilities"][stage] = True
        stage = "cleanup"
        _cleanup(directory, parent, identity)
        result["ok"] = True
    except ProbeError as error:
        result["errors"].append(str(error))
    except subprocess.TimeoutExpired:
        result["errors"].append("process_lock_child_timeout")
    except (OSError, ValueError, TypeError, ImportError, NotImplementedError,
            sqlite3.Error, subprocess.SubprocessError):
        result["errors"].append(stage + "_failed")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Synthetic phone storage preflight; no network or real data.",
                                     epilog="Failed st-phone-storage-probe-* children are retained under --parent; do not use shared /sdcard storage.")
    parser.add_argument("--parent", help="Explicit existing absolute private parent; a unique probe child is created.")
    parser.add_argument("--_flock-check", nargs=3, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._flock_check is not None:
        if args.parent is not None:
            return 24
        path, device, inode = args._flock_check
        try:
            return _flock_child(path, int(device), int(inode))
        except ValueError:
            return 24
    if args.parent is None:
        parser.error("--parent is required")
    result = run_probe(args.parent)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
