"""Local ST operations: one-terminal services and explicitly offline private snapshots."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager, ExitStack, closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("stiller_existing_starter", ROOT / "scripts" / "run_component.py")
starter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(starter)
DATABASES = dict(zip(("main", "ideas", "vault"), starter.DB_KEYS))
FILES = {role: role + ".sqlite3" for role in DATABASES}
FORMAT = "stiller-offline-backup/1"
MANIFEST = "backup-manifest.json"


class OperationError(ValueError):
    """A fixed, credential-free explanation safe for the local operator."""


def outside_source(value: Path, root: Path = ROOT) -> Path:
    if value.expanduser().is_symlink():
        raise OperationError("Private input/output must use a direct path, not a symbolic link")
    path = value.expanduser().resolve()
    resolved_root = root.resolve()
    if path == resolved_root or resolved_root in path.parents:
        raise OperationError("Private input/output must be outside the source repository")
    return path


def load_private_config(path: Path, root: Path = ROOT) -> dict[str, str]:
    path = outside_source(path, root)
    try:
        if not path.is_file() or path.stat().st_size > 65536:
            raise OperationError("Private config must be a file of at most 64 KiB")
        config = starter.parse_config(path.read_text(encoding="utf-8-sig"))
        for component in starter.COMPONENTS:
            starter.validate(config, component, root=root)
        return config
    except (OSError, UnicodeError):
        raise OperationError("Private config could not be read safely") from None


def check_ports_stopped(config: dict[str, str]) -> None:
    for name in ("CONTROL", "MCP", "GATEWAY"):
        # validate() has already restricted these to 127.0.0.1.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            if probe.connect_ex(("127.0.0.1", int(config[f"STBRAIN_{name}_PORT"]))) == 0:
                raise OperationError("A configured service port is occupied; stop services first")


def await_ports_stopped(config: dict[str, str]) -> None:
    deadline = time.monotonic() + 5
    while True:
        try:
            check_ports_stopped(config)
            return
        except OperationError:
            if time.monotonic() >= deadline:
                raise OperationError("Service processes ended but a configured port is still occupied; check before backup") from None
            time.sleep(0.1)


def child_environment(config: dict[str, str]) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("STBRAIN_")}
    environment.update(config)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def stop_children(children: list) -> None:
    for child in reversed(children):
        if child.poll() is None:
            try:
                child.terminate()
            except OSError:
                pass
    incomplete = False
    for child in reversed(children):
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                child.kill()
                child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                incomplete = True
        except OSError:
            incomplete = True
    if incomplete:
        raise OperationError("A child process could not be confirmed stopped; check this command's local service processes")


def create_process_job():
    if os.name != "nt":
        return None
    spec = importlib.util.spec_from_file_location("stiller_windows_job", ROOT / "scripts" / "windows_process_job.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.WindowsProcessJob()


def start_stack(config: dict[str, str], *, root: Path = ROOT, popen=None, sleeper=None, job_factory=None) -> int:
    """Supervise only children started by this invocation; never stop unrelated PIDs."""
    popen = subprocess.Popen if popen is None else popen
    sleeper = time.sleep if sleeper is None else sleeper
    for component in starter.COMPONENTS:
        starter.validate(config, component, root=root)
    check_ports_stopped(config)
    children = []
    environment = child_environment(config)
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0
    job = (create_process_job if job_factory is None else job_factory)()
    if job is not None:
        flags |= job.creationflags
    try:
        for component in ("control", "mcp", "gateway"):
            children.append(popen([sys.executable, "-B", "-m", starter.COMPONENTS[component]],
                                  cwd=root, env=environment, creationflags=flags))
            if job is not None:
                job.assign_and_resume(children[-1])
        print("Three service processes launched; Ctrl+C stops this group. Startup health is not yet verified.", flush=True)
        while True:
            if any(child.poll() is not None for child in children):
                raise OperationError("A service process exited; the remaining processes from this command were stopped")
            sleeper(0.25)
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            if job is not None:
                job.close()
        finally:
            stop_children(children)
        await_ports_stopped(config)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


@contextmanager
def database_connection(path: Path, mode: str = "ro"):
    connection = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=1)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        yield connection
    finally:
        connection.close()


def check_database(path: Path) -> dict:
    with database_connection(path) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise OperationError("SQLite integrity check failed")
        tables = connection.execute("SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0]
        return {"user_version": connection.execute("PRAGMA user_version").fetchone()[0], "table_count": tables}


def source_databases(config: dict[str, str], root: Path = ROOT) -> dict[str, Path]:
    paths = {role: outside_source(Path(config[key]), root) for role, key in DATABASES.items()}
    if any(not path.is_file() for path in paths.values()):
        raise OperationError("All three existing SQLite databases are required; no empty database will be invented")
    values = list(paths.values())
    if any(os.path.samefile(left, right) for index, left in enumerate(values) for right in values[index + 1:]):
        raise OperationError("Main, ideas and vault databases must be different files")
    return paths


@contextmanager
def quiescent_sources(config: dict[str, str], confirmed: bool, root: Path = ROOT):
    if not confirmed:
        raise OperationError("Offline operation requires --confirm-stopped after stopping every writer")
    for component in starter.COMPONENTS:
        starter.validate(config, component, root=root)
    check_ports_stopped(config)
    paths = source_databases(config, root)
    with ExitStack() as stack:
        # This prevents later writes while copying. Sequential locks cannot repair
        # a pre-existing cross-database operation: stopping ALL writers is mandatory.
        for path in paths.values():
            connection = stack.enter_context(database_connection(path, "rw"))
            connection.execute("BEGIN IMMEDIATE")
        yield paths


@contextmanager
def new_output_directory(destination: Path, root: Path = ROOT):
    destination = outside_source(destination, root)
    if destination.exists() or destination.is_symlink():
        raise OperationError("Destination already exists; choose a new directory")
    if not destination.parent.is_dir():
        raise OperationError("Destination parent directory must already exist")
    lock = destination.parent / ("." + destination.name + ".stiller-publish-lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise OperationError("Another operation owns this destination; choose another name or inspect the stale lock") from None
    os.close(descriptor)
    try:
        with tempfile.TemporaryDirectory(prefix=".stiller-stage-", dir=destination.parent) as temporary:
            stage = Path(temporary)
            yield stage
            if destination.exists() or destination.is_symlink():
                raise OperationError("Destination appeared during validation; publishing was refused")
            stage.rename(destination)
    finally:
        lock.unlink(missing_ok=True)


def write_json_private(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def backup(config: dict[str, str], destination: Path, *, confirm_stopped: bool, root: Path = ROOT) -> dict:
    with quiescent_sources(config, confirm_stopped, root) as paths:
        destination = outside_source(destination, root)
        if any(path.parent == destination or path.parent in destination.parents for path in paths.values()):
            raise OperationError("Backup destination must be outside every source database directory")
        with new_output_directory(destination, root) as stage:
            entries = {}
            for role, path in paths.items():
                target = stage / FILES[role]
                with database_connection(path) as source:
                    with closing(sqlite3.connect(target)) as copied:
                        source.backup(copied)
                        copied.execute("PRAGMA journal_mode=DELETE")
                os.chmod(target, 0o600)
                entries[role] = {"file": FILES[role], "sha256": digest(target), "bytes": target.stat().st_size,
                                 **check_database(target)}
            manifest = {"format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
                        "consistency": "operator-confirmed-stopped-all-writers-plus-held-reserved-locks",
                        "configuration_included": False, "databases": entries}
            write_json_private(stage / MANIFEST, manifest)
            verify_backup(stage, root=root)
    return {"operation": "backup", "verified_databases": 3, "configuration_included": False}


def verify_backup(directory: Path, root: Path = ROOT) -> dict:
    directory = outside_source(directory, root)
    expected_names = {MANIFEST, *FILES.values()}
    if not directory.is_dir() or {path.name for path in directory.iterdir()} != expected_names:
        raise OperationError("Backup must contain exactly its manifest and three SQLite files")
    if any(path.is_symlink() or not path.is_file() for path in directory.iterdir()):
        raise OperationError("Backup entries must be plain files")
    manifest_path = directory / MANIFEST
    if manifest_path.stat().st_size > 65536:
        raise OperationError("Backup manifest is too large")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT or
                manifest.get("configuration_included") is not False or
                not isinstance(manifest.get("databases"), dict) or set(manifest["databases"]) != set(FILES)):
            raise OperationError("Backup manifest identity is invalid")
        for role, filename in FILES.items():
            entry = manifest["databases"][role]
            if (not isinstance(entry, dict) or entry.get("file") != filename or
                    not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) or
                    type(entry.get("bytes")) is not int or entry["bytes"] < 0):
                raise OperationError("Backup manifest entry is invalid")
            path = directory / filename
            if path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]:
                raise OperationError("Backup file hash or size mismatch")
            metadata = check_database(path)
            if any(type(entry.get(key)) is not int or entry[key] != value for key, value in metadata.items()):
                raise OperationError("Backup database metadata mismatch")
            if digest(path) != entry["sha256"]:
                raise OperationError("Backup changed during validation")
        return manifest
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise OperationError("Backup manifest could not be validated") from None


def restore(directory: Path, destination: Path, root: Path = ROOT) -> dict:
    directory = outside_source(directory, root)
    destination = outside_source(destination, root)
    if directory == destination or directory in destination.parents:
        raise OperationError("Restore destination must be outside the backup directory")
    verify_backup(directory, root)
    with new_output_directory(destination, root) as stage:
        for name in (MANIFEST, *FILES.values()):
            shutil.copyfile(directory / name, stage / name)
            os.chmod(stage / name, 0o600)
        verify_backup(stage, root)
    return {"operation": "restore", "verified_databases": 3, "existing_instance_modified": False,
            "next_step": "Validate the restored copy separately; select its paths in a separate private config"}


def export_table(directory: Path, role: str, table: str, output: Path, *, ack_private_data: bool, root: Path = ROOT) -> dict:
    if not ack_private_data:
        raise OperationError("Export requires --ack-private-data; output may contain original private memory")
    manifest = verify_backup(directory, root)
    directory = outside_source(directory, root)
    if role not in FILES:
        raise OperationError("Select main, ideas or vault")
    output = outside_source(output, root)
    if directory == output or directory in output.parents:
        raise OperationError("Export must be outside the verified backup directory")
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise OperationError("Export requires a new filename in an existing directory")
    database = directory / FILES[role]
    count = 0
    with database_connection(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if table not in tables:
            raise OperationError("Selected table does not exist in that database")
        escaped_table = '"' + table.replace('"', '""') + '"'
        cursor = connection.execute("SELECT * FROM " + escaped_table)
        columns = [description[0] for description in cursor.description]
        with tempfile.TemporaryDirectory(prefix=".stiller-export-", dir=output.parent) as temporary:
            stage = Path(temporary) / "selected.jsonl"
            with stage.open("x", encoding="utf-8", newline="\n") as stream:
                os.chmod(stage, 0o600)
                stream.write(json.dumps({"format": "stiller-table-export/1", "database": role, "table": table,
                                         "columns": columns, "source_sha256": manifest["databases"][role]["sha256"]}, ensure_ascii=False) + "\n")
                for row in cursor:
                    cells = [{"$type": "bytes", "base64": base64.b64encode(value).decode("ascii")} if isinstance(value, bytes) else value for value in row]
                    stream.write(json.dumps({"row": cells}, ensure_ascii=False, allow_nan=False) + "\n")
                    count += 1
            verify_backup(directory, root)
            # Hard-link publication is no-clobber on both supported platforms;
            # staging is on the same filesystem. Fail rather than fall back to overwrite.
            try:
                os.link(stage, output)
            except FileExistsError:
                raise OperationError("Export destination appeared; publishing was refused") from None
    return {"operation": "export", "exported_rows": count, "contains_private_data": True,
            "format": "stiller-table-export/1", "import_supported": False}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("check", "start"):
        commands.add_parser(name).add_argument("--config", required=True, type=Path)
    command = commands.add_parser("backup")
    command.add_argument("--config", required=True, type=Path)
    command.add_argument("--output", required=True, type=Path)
    command.add_argument("--confirm-stopped", action="store_true")
    commands.add_parser("verify").add_argument("--backup", required=True, type=Path)
    command = commands.add_parser("restore")
    command.add_argument("--backup", required=True, type=Path)
    command.add_argument("--output", required=True, type=Path)
    command = commands.add_parser("export")
    command.add_argument("--backup", required=True, type=Path)
    command.add_argument("--database", required=True, choices=FILES)
    command.add_argument("--table", required=True)
    command.add_argument("--output", required=True, type=Path)
    command.add_argument("--ack-private-data", action="store_true")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in {"check", "start", "backup"}:
            config = load_private_config(args.config)
        if args.command == "check":
            result = {"operation": "check", "valid_components": 3, "services_started": False,
                      "health_checked": False, "database_files_present": all(Path(config[key]).is_file() for key in DATABASES.values())}
        elif args.command == "start":
            return start_stack(config)
        elif args.command == "backup":
            result = backup(config, args.output, confirm_stopped=args.confirm_stopped)
        elif args.command == "verify":
            verify_backup(args.backup)
            result = {"operation": "verify", "verified_databases": 3}
        elif args.command == "restore":
            result = restore(args.backup, args.output)
        else:
            result = export_table(args.backup, args.database, args.table, args.output, ack_private_data=args.ack_private_data)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OperationError, ValueError, OSError, sqlite3.Error) as error:
        # SQLite/OS exception text can include private filenames, SQL or values.
        message = str(error) if isinstance(error, OperationError) else "Operation failed safely; inspect private paths/configuration and retry with a new target"
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
