"""Private, single-process durable receipts. No credentials/config are stored."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import threading
import time

from .core import Chunk, MigrationError, canonical


def _link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()) or (
        path.exists() and bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400))


def _windows_acl(root, *, new):
    # Use the Windows ACL API, not grant:r (which leaves unrelated explicit
    # grants). Existing ledgers must already be restricted; never silently
    # repair an exposed directory and then claim it has always been private.
    script = r"""
$ErrorActionPreference = 'Stop'
$p = $env:ST_MIGRATION_PRIVATE_DIR
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed = @($sid, 'S-1-5-18', 'S-1-5-32-544')
if ($env:ST_MIGRATION_PRIVATE_NEW -eq '1') {
  $section = [System.Security.AccessControl.AccessControlSections]::Access
  $a = [System.IO.Directory]::GetAccessControl($p, $section)
  $sddl = 'D:P'
  foreach ($s in $allowed) { $sddl += "(A;OICI;FA;;;$s)" }
  $a.SetSecurityDescriptorSddlForm($sddl, $section)
  [System.IO.Directory]::SetAccessControl($p, $a)
}
$items = @((Get-Item -LiteralPath $p)) + @(Get-ChildItem -LiteralPath $p -Force)
foreach ($item in $items) {
  $a = Get-Acl -LiteralPath $item.FullName
  if ($item.FullName -eq $p -and -not $a.AreAccessRulesProtected) { throw 'private_acl_not_protected' }
  $mine = $false
  foreach ($r in $a.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
    if ($allowed -notcontains $r.IdentityReference.Value -or $r.AccessControlType -ne 'Allow') { throw 'private_acl_unexpected_principal' }
    if ($r.IdentityReference.Value -eq $sid) { $mine = $true }
  }
  if (-not $mine) { throw 'private_acl_owner_missing' }
}
"""
    # A parent PowerShell 7 module path is incompatible with Windows PowerShell
    # 5's security module. Let the child select its own built-in module path.
    env = {key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"}
    env.update(ST_MIGRATION_PRIVATE_DIR=str(root), ST_MIGRATION_PRIVATE_NEW="1" if new else "0")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=env, capture_output=True)
    if result.returncode:
        raise MigrationError("private_acl_verification_failed")


class PrivateLedger:
    def __init__(self, root: Path):
        root = root.absolute()
        if root.name != "migration-ui":
            raise MigrationError("private_directory_name_must_be_migration_ui")
        if any(_link(part) for part in [root, *root.parents]):
            raise MigrationError("private_directory_symlink_rejected")
        existed = root.exists()
        if existed and (not root.is_dir() or (list(root.iterdir()) and not (root / ".migration-ui-v1").is_file())):
            raise MigrationError("private_directory_must_be_new_or_owned")
        if existed:
            for item in root.iterdir():
                if item.name not in {".migration-ui-v1", "ledger.sqlite", "ledger.sqlite-journal", ".instance.lock"} or _link(item) or not item.is_file() or item.stat().st_nlink != 1:
                    raise MigrationError("unexpected_private_directory_entry")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "nt":
            _windows_acl(root, new=not existed)
        else:
            if root.stat().st_uid != os.getuid():
                raise MigrationError("private_directory_wrong_owner")
            if existed and root.stat().st_mode & 0o077:
                raise MigrationError("private_directory_exposed_permissions")
            for item in root.iterdir():
                if item.stat().st_uid != os.getuid() or item.stat().st_mode & 0o077:
                    raise MigrationError("private_file_exposed_permissions")
            root.chmod(0o700)
        for item in root.iterdir():
            if item.name not in {".migration-ui-v1", "ledger.sqlite", "ledger.sqlite-journal", ".instance.lock"} or _link(item) or not item.is_file():
                raise MigrationError("unexpected_private_directory_entry")
        marker = root / ".migration-ui-v1"
        marker.touch(mode=0o600, exist_ok=True)
        self._lockfile = open(root / ".instance.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self._lockfile.seek(0, 2) == 0:
                    self._lockfile.write(b"0")
                    self._lockfile.flush()
                self._lockfile.seek(0)
                msvcrt.locking(self._lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lockfile.close()
            raise MigrationError("another_migration_instance_running") from exc
        self.path = root / "ledger.sqlite"
        self._mutex = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS sources(sha TEXT PRIMARY KEY, raw BLOB NOT NULL, format TEXT NOT NULL, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS items(scope TEXT NOT NULL, chunk TEXT NOT NULL, source TEXT NOT NULL,
             state TEXT NOT NULL, payload_hash TEXT, receipt TEXT, code TEXT, updated REAL NOT NULL,
             PRIMARY KEY(scope,chunk));
        """)
        # A process may have died after the target accepted a request. Never
        # infer absence or automatically replay an in-flight write.
        self.db.execute("UPDATE items SET state='UNKNOWN',code='interrupted_in_flight' WHERE state='SENDING'")
        self.db.commit()
        if os.name != "nt":
            for item in root.iterdir():
                item.chmod(0o600)

    def close(self):
        self.db.close()
        self._lockfile.close()

    def retain(self, sha: str, raw: bytes, fmt: str):
        with self._mutex, self.db:
            self.db.execute("INSERT OR IGNORE INTO sources VALUES(?,?,?,?)", (sha, raw, fmt, time.time()))

    def get(self, scope: str, chunk: str):
        with self._mutex:
            row = self.db.execute("SELECT state,receipt,code,payload_hash FROM items WHERE scope=? AND chunk=?", (scope, chunk)).fetchone()
            return dict(row) if row else None

    def mark(self, scope: str, chunk: Chunk, source: str, state: str, *, payload_hash=None, result=None, code=None):
        if state not in {"PENDING", "REJECTED", "SENDING", "STORED", "UNKNOWN"}:
            raise MigrationError("invalid_ledger_state")
        with self._mutex, self.db:
            previous = self.get(scope, chunk.id)
            if previous and previous["state"] in {"STORED", "UNKNOWN"}:
                raise MigrationError("terminal_receipt_cannot_be_replayed")
            if previous and previous["state"] == "SENDING" and state in {"SENDING", "PENDING", "REJECTED"}:
                # REJECTED is allowed only for a confirmed no-write response;
                # the engine may finalize it, but must never restart SENDING.
                if state != "REJECTED":
                    raise MigrationError("in_flight_receipt_cannot_be_replayed")
            self.db.execute("""INSERT INTO items VALUES(?,?,?,?,?,?,?,?)
                 ON CONFLICT(scope,chunk) DO UPDATE SET state=excluded.state,
                 payload_hash=COALESCE(excluded.payload_hash,items.payload_hash), receipt=excluded.receipt,
                 code=excluded.code, updated=excluded.updated""",
                 (scope, chunk.id, source, state, payload_hash,
                  canonical(result) if result else None, code, time.time()))

    def summary(self):
        with self._mutex:
            return {row[0]: row[1] for row in self.db.execute("SELECT state,count(*) FROM items GROUP BY state")}
