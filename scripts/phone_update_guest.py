"""Private, offline preparation for an explicit phone upgrade. Never called by chat."""
from __future__ import annotations
import argparse
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def durable_rename(source, target):
    source.rename(target)
    # Publish directory moves on both sides before advancing the transaction receipt.
    sync_directory(source.parent)
    if target.parent != source.parent:
        sync_directory(target.parent)


def module(name):
    spec = importlib.util.spec_from_file_location("phone_update_" + name, ROOT / "scripts" / (name + ".py"))
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def atomic(path, value):
    temporary = path.with_name(path.name + ".new")
    if temporary.is_symlink() or path.is_symlink():
        raise ValueError("unsafe marker")
    with open(temporary, "w", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def paths(state, operation):
    if not re.fullmatch(r"upgrade-[a-f0-9]{24}", operation):
        raise ValueError("invalid operation")
    state = Path(state).absolute()
    folder = state / "phone-upgrades" / operation
    for item in (state, folder.parent, folder, state / "private", folder / "original", folder / "candidate", folder / "after-upgrade", folder / "transaction.json"):
        if item.is_symlink() or item.resolve() != item:
            raise ValueError("linked update directory")
    return state / "private", folder


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage(state, operation):
    private, folder = paths(state, operation)
    installer = module("install_stiller")
    ops = module("stiller_ops")
    config = installer.read_installation(private)
    ops.check_ports_stopped(config)
    private_size = sum(path.stat().st_size for path in private.rglob("*") if path.is_file() and "venv" not in path.relative_to(private).parts)
    if shutil.disk_usage(state).free < max(3 * 1024**3, private_size * 2 + 1024**3):
        raise ValueError("insufficient space for retained original and verified backup")
    if folder.exists():
        receipt = json.loads((folder / "transaction.json").read_text())
        if receipt.get("phase") == "candidate_active" and (folder / "original").is_dir():
            if file_hash(private / "config.env") != receipt["config_sha256"]:
                raise ValueError("configuration changed during interrupted update")
            ops.verify_backup(folder / "databases")
            return
        raise ValueError("incomplete update; roll back before retrying")
    folder.parent.mkdir(mode=0o700, exist_ok=True)
    folder.mkdir(mode=0o700)
    sync_directory(folder.parent)
    sync_directory(Path(state))
    receipt = {"format": "stiller-phone-upgrade/1", "phase": "preparing", "config_sha256": file_hash(private / "config.env")}
    atomic(folder / "transaction.json", receipt)
    # Uses SQLite backup API and checks all three databases, not a live .db file copy.
    ops.backup(config, folder / "databases", confirm_stopped=True)
    candidate = folder / "candidate"
    def ignore(directory, names):
        omitted = {"venv"} if Path(directory) == private else set()
        for name in names:
            path = Path(directory) / name
            if name not in omitted and (path.is_symlink() or not (path.is_file() or path.is_dir())):
                raise ValueError("unsupported private entry")
        return omitted
    # The original venv remains untouched. The candidate gets fresh locked dependencies.
    shutil.copytree(private, candidate, ignore=ignore)
    database_paths = ops.source_databases(config)
    for role, source in database_paths.items():
        if source.parent != private:
            raise ValueError("database outside instance")
        target = candidate / source.name
        shutil.copy2(folder / "databases" / ops.FILES[role], target)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = candidate / (source.name + suffix)
            if sidecar.exists():
                if sidecar.is_symlink() or not sidecar.is_file():
                    raise ValueError("unsafe candidate sidecar")
                sidecar.unlink()  # Newly copied candidate only, never the original.
        ops.check_database(target)
    if file_hash(candidate / "config.env") != receipt["config_sha256"]:
        raise ValueError("configuration copy mismatch")
    for root, _, files in os.walk(candidate):
        for name in files:
            with open(Path(root) / name, "r+b") as stream:
                os.fsync(stream.fileno())
        sync_directory(Path(root))
    receipt["phase"] = "candidate_ready"
    atomic(folder / "transaction.json", receipt)
    # Standard phone starts hold the host operation lock. Recheck ports/writers immediately before swapping.
    with ops.quiescent_sources(config, True):
        durable_rename(private, folder / "original")
        receipt["phase"] = "original_saved"
        atomic(folder / "transaction.json", receipt)
        durable_rename(candidate, private)
    receipt["phase"] = "candidate_active"
    atomic(folder / "transaction.json", receipt)


def verify_unchanged(state, operation):
    private, folder = paths(state, operation)
    receipt = json.loads((folder / "transaction.json").read_text())
    if receipt.get("phase") != "candidate_active" or file_hash(private / "config.env") != receipt["config_sha256"]:
        raise ValueError("configuration changed")
    for name in ("self-password.json",):
        original = folder / "original" / name
        current = private / name
        if original.exists() != current.exists() or (original.exists() and file_hash(original) != file_hash(current)):
            raise ValueError("authorization changed")
    module("stiller_ops").verify_backup(folder / "databases")


def rollback(state, operation):
    private, folder = paths(state, operation)
    if not folder.is_dir():
        return  # Interrupted before the guest staged anything.
    receipt = json.loads((folder / "transaction.json").read_text())
    original = folder / "original"
    after = folder / "after-upgrade"
    if receipt.get("phase") == "rolled_back":
        return
    if original.exists():
        if private.exists():
            installer = module("install_stiller")
            module("stiller_ops").check_ports_stopped(installer.read_installation(private))
            if after.exists():
                raise ValueError("rollback target already exists")
            durable_rename(private, after)  # Retain all post-upgrade data, including later messages.
        durable_rename(original, private)
    elif receipt.get("phase") in ("original_saved", "candidate_active"):
        # A prior rollback can be interrupted between renaming original and publishing its receipt.
        if not after.is_dir() or not private.is_dir() or file_hash(private / "config.env") != receipt["config_sha256"]:
            raise ValueError("original missing")
    receipt["phase"] = "rolled_back"
    atomic(folder / "transaction.json", receipt)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("stage", "verify-unchanged", "rollback"))
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--operation", required=True)
    args = parser.parse_args(argv)
    try:
        # No config, credentials, filenames inside archives or raw exceptions go to public output.
        with redirect_stdout(io.StringIO()):
            {"stage": stage, "verify-unchanged": verify_unchanged, "rollback": rollback}[args.action](args.state, args.operation)
        print(json.dumps({"ok": True, "action": args.action}))
        return 0
    except (Exception, KeyboardInterrupt):
        print("手机升级步骤未完成。原实例/升级副本保留，可从外层显式执行 rollback；不要删除目录。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
