"""Explicit device delegation for metadata only; no AI tools or automatic recall.

No filesystem access or table creation occurs on import. Credentials are random
device tokens, represented in SQLite only by their SHA-256 verifier.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid
from typing import Mapping

from .atlas_metadata import AtlasMetadataReader, AtlasScope, AtlasUnavailable

SCOPE = "atlas.metadata.read"
MAX_BODY = 2048
MAX_SMALL_RESPONSE = 4096
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
TOKEN = re.compile(r"orb_atlas_[A-Za-z0-9_-]{43}\Z")
TABLE = "orbis_atlas_device_grants"
TABLE_SQL = """CREATE TABLE IF NOT EXISTS orbis_atlas_device_grants (
    grant_id TEXT NOT NULL PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    device_id TEXT NOT NULL,
    verifier TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    CHECK((status='active' AND revoked_at IS NULL) OR
          (status='revoked' AND revoked_at IS NOT NULL))
)"""
INDEX_SQL = """CREATE UNIQUE INDEX IF NOT EXISTS orbis_atlas_one_current_device
ON orbis_atlas_device_grants(owner_id,model_id,device_id) WHERE status='active'"""
COLUMNS = ("grant_id", "request_id", "device_id", "verifier", "owner_id", "model_id",
           "status", "created_at", "revoked_at")
INDEX = "orbis_atlas_one_current_device"


def _canonical_ddl(value: str) -> str:
    # Accept only our own schema, not a lookalike missing constraints. SQLite
    # drops IF NOT EXISTS when recording CREATE statements in sqlite_master.
    return " ".join(value.replace("IF NOT EXISTS ", "").split())


class AtlasGrantError(Exception):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


def enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if env is None else env).get("STBRAIN_ATLAS_BOOTSTRAP_ENABLED")
    if value is None:
        return False
    if value not in {"0", "1"}:
        raise ValueError("atlas_configuration_invalid")
    return value == "1"


def capabilities(enabled: bool) -> dict:
    return {"schema": "orbis.st.atlas-bootstrap/1", "service": "stillerbrain",
            "enabled": bool(enabled), "scope": SCOPE, "atlasSchema": "orbis.st.atlas/1"}


def strict_object(raw: bytes, *, limit: int = MAX_BODY) -> dict:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError()
            out[key] = value
        return out
    try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
            raise ValueError()
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise AtlasGrantError(400, "invalid_request") from None


def registration(body: dict) -> dict:
    if (not isinstance(body, dict) or
            set(body) != {"schema", "requestId", "deviceId", "verifier"} or
            body.get("schema") != "orbis.st.atlas-register/1" or
            not isinstance(body.get("requestId"), str) or not HEX32.fullmatch(body["requestId"]) or
            not isinstance(body.get("deviceId"), str) or not HEX32.fullmatch(body["deviceId"]) or
            not isinstance(body.get("verifier"), str) or not HEX64.fullmatch(body["verifier"])):
        raise AtlasGrantError(400, "invalid_request")
    return body


def revocation(body: dict) -> dict:
    if (not isinstance(body, dict) or set(body) != {"schema", "requestId"} or
            body.get("schema") != "orbis.st.atlas-revoke/1" or
            not isinstance(body.get("requestId"), str) or not HEX32.fullmatch(body["requestId"])):
        raise AtlasGrantError(400, "invalid_request")
    return body


def token_verifier(token: str) -> str:
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise AtlasGrantError(401, "unauthorized")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AtlasDeviceGrants:
    """Main-DB operational metadata; initialization requires explicit enablement.

    The small independent mutex is never the gateway conversation lock. Snapshot
    readers have a separate nonblocking one-reader gate; authorizing a snapshot
    does not update any row or prolong a credential lifetime.
    """
    def __init__(self, database: Path, owner_id: str, model_id: str, *, enabled: bool,
                 id_key: bytes | None, forbidden: frozenset[str] = frozenset(), reader=None):
        if (not isinstance(database, Path) or not database.is_absolute() or
                any(not isinstance(value, str) or not value.strip() or len(value) > 256
                    for value in (owner_id, model_id)) or type(enabled) is not bool or
                (id_key is not None and (not isinstance(id_key, bytes) or len(id_key) != 32)) or
                (enabled and id_key is None) or not isinstance(forbidden, frozenset) or
                any(not isinstance(value, str) or not HEX64.fullmatch(value) for value in forbidden)):
            raise ValueError("atlas_configuration_invalid")
        self.database = database
        self.owner_id, self.model_id = owner_id, model_id
        self.enabled = enabled
        self.forbidden = forbidden
        self._lock = threading.RLock()
        self._reads = threading.BoundedSemaphore(1)
        self._reader = reader or (AtlasMetadataReader(AtlasScope(database, owner_id, model_id, id_key)).read
                                  if id_key is not None else None)
        if enabled:
            self._initialize()

    def _connect(self, *, write=False):
        path = self.database.resolve(strict=True)
        db = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                             uri=True, timeout=2, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA foreign_keys=ON")
            if not write:
                db.execute("PRAGMA query_only=ON")
            return db
        except BaseException:
            db.close()
            raise

    @staticmethod
    def _check_schema(db):
        info = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
        if (info is None or info[0] != "table" or not isinstance(info[1], str) or
                _canonical_ddl(info[1]) != _canonical_ddl(TABLE_SQL)):
            raise AtlasGrantError(503, "atlas_unavailable")
        columns = list(db.execute("PRAGMA table_xinfo(orbis_atlas_device_grants)"))
        expected = [(i, name, "TEXT", int(name != "revoked_at"), None, int(i == 0), 0)
                    for i, name in enumerate(COLUMNS)]
        if [tuple(row) for row in columns] != expected:
            raise AtlasGrantError(503, "atlas_unavailable")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (TABLE,)).fetchone():
            raise AtlasGrantError(503, "atlas_unavailable")
        indices = list(db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name=?", (TABLE,)))
        expected_auto = {f"sqlite_autoindex_{TABLE}_{i}" for i in (1, 2, 3)}
        if ({row[0] for row in indices if row[1] is None} != expected_auto or
                len(indices) != 4 or
                not any(row[0] == INDEX and isinstance(row[1], str) and
                        _canonical_ddl(row[1]) == _canonical_ddl(INDEX_SQL) for row in indices)):
            raise AtlasGrantError(503, "atlas_unavailable")

    def _initialize(self):
        db = None
        try:
            db = self._connect(write=True)
            db.execute("BEGIN IMMEDIATE")
            present = db.execute("SELECT 1 FROM sqlite_master WHERE name IN (?,?)", (TABLE, INDEX)).fetchone()
            if present is None:
                db.execute(TABLE_SQL)
                db.execute(INDEX_SQL)
            # Existing malformed/partial schema is an operator error, never
            # silently repaired or adopted during a server restart.
            self._check_schema(db)
            db.commit()
        except (sqlite3.Error, OSError, ValueError):
            raise AtlasGrantError(503, "atlas_unavailable") from None
        finally:
            if db is not None:
                db.close()

    def _verifier(self, value):
        if not isinstance(value, str) or not HEX64.fullmatch(value) or value in self.forbidden:
            raise AtlasGrantError(401, "unauthorized")

    def _find(self, db, verifier):
        self._verifier(verifier)
        self._check_schema(db)
        row = db.execute("SELECT * FROM orbis_atlas_device_grants WHERE owner_id=? AND model_id=? AND verifier=?",
                         (self.owner_id, self.model_id, verifier)).fetchone()
        if row is None:
            raise AtlasGrantError(401, "unauthorized")
        return row

    @staticmethod
    def _result(row):
        if (not HEX32.fullmatch(row["grant_id"] or "") or
                not HEX32.fullmatch(row["request_id"] or "") or row["status"] not in {"active", "revoked"}):
            raise AtlasGrantError(503, "atlas_unavailable")
        return {"schema": "orbis.st.atlas-register-result/1", "requestId": row["request_id"],
                "grantId": row["grant_id"], "status": row["status"], "scope": SCOPE, "expiresAt": None}

    def register(self, body):
        body = registration(body)
        if not self.enabled:
            raise AtlasGrantError(403, "atlas_disabled")
        self._verifier(body["verifier"])
        db = None
        try:
            with self._lock:
                db = self._connect(write=True)
                db.execute("BEGIN IMMEDIATE")
                self._check_schema(db)
                prior = db.execute("SELECT * FROM orbis_atlas_device_grants WHERE request_id=?", (body["requestId"],)).fetchone()
                if prior is not None:
                    if (prior["owner_id"], prior["model_id"], prior["device_id"], prior["verifier"]) != (
                            self.owner_id, self.model_id, body["deviceId"], body["verifier"]):
                        raise AtlasGrantError(409, "request_conflict")
                    return self._result(prior)
                if db.execute("SELECT 1 FROM orbis_atlas_device_grants WHERE verifier=?", (body["verifier"],)).fetchone():
                    raise AtlasGrantError(409, "request_conflict")
                scope = (self.owner_id, self.model_id)
                total = db.execute("SELECT COUNT(*) FROM orbis_atlas_device_grants WHERE owner_id=? AND model_id=?", scope).fetchone()[0]
                active = db.execute("SELECT COUNT(*) FROM orbis_atlas_device_grants WHERE owner_id=? AND model_id=? AND status='active' AND device_id!=?",
                                    (*scope, body["deviceId"])).fetchone()[0]
                if total >= 512 or active >= 8:
                    raise AtlasGrantError(429, "grant_limit_reached")
                now = _now()
                db.execute("UPDATE orbis_atlas_device_grants SET status='revoked',revoked_at=? WHERE owner_id=? AND model_id=? AND device_id=? AND status='active'",
                           (now, *scope, body["deviceId"]))
                grant = uuid.uuid4().hex
                db.execute("INSERT INTO orbis_atlas_device_grants VALUES(?,?,?,?,?,?,'active',?,NULL)",
                           (grant, body["requestId"], body["deviceId"], body["verifier"], *scope, now))
                row = db.execute("SELECT * FROM orbis_atlas_device_grants WHERE grant_id=?", (grant,)).fetchone()
                db.commit()
                return self._result(row)
        except (sqlite3.Error, OSError, ValueError):
            raise AtlasGrantError(503, "atlas_unavailable") from None
        finally:
            if db is not None:
                db.close()

    def revoke(self, verifier: str, body):
        revocation(body)
        self._verifier(verifier)
        db = None
        try:
            with self._lock:
                db = self._connect(write=True)
                db.execute("BEGIN IMMEDIATE")
                row = self._find(db, verifier)
                if row["status"] == "active":
                    db.execute("UPDATE orbis_atlas_device_grants SET status='revoked',revoked_at=? WHERE grant_id=?",
                               (_now(), row["grant_id"]))
                db.commit()
                return {"schema": "orbis.st.atlas-revoke-result/1", "status": "revoked"}
        except (sqlite3.Error, OSError, ValueError):
            raise AtlasGrantError(503, "atlas_unavailable") from None
        finally:
            if db is not None:
                db.close()

    def snapshot(self, verifier: str):
        self._verifier(verifier)
        if not self.enabled:
            raise AtlasGrantError(403, "atlas_disabled")
        if not self._reads.acquire(blocking=False):
            raise AtlasGrantError(503, "atlas_unavailable")
        db = None
        try:
            with self._lock:
                db = self._connect()
                row = self._find(db, verifier)
                if row["status"] != "active":
                    raise AtlasGrantError(401, "unauthorized")
                current = db.execute("SELECT COUNT(*) FROM orbis_atlas_device_grants WHERE owner_id=? AND model_id=? AND device_id=? AND status='active'",
                                     (self.owner_id, self.model_id, row["device_id"])).fetchone()[0]
                if current != 1 or self._reader is None:
                    raise AtlasGrantError(401, "unauthorized")
                try:
                    result = strict_object(self._reader(), limit=1024 * 1024)
                except AtlasGrantError:
                    raise AtlasGrantError(503, "atlas_unavailable") from None
                # A second process may revoke or rotate while the read-only
                # projection runs. Never return that stale authorized snapshot.
                latest = self._find(db, verifier)
                if latest["status"] != "active" or latest["grant_id"] != row["grant_id"]:
                    raise AtlasGrantError(401, "unauthorized")
                return result
        except (sqlite3.Error, OSError, ValueError, AtlasUnavailable):
            raise AtlasGrantError(503, "atlas_unavailable") from None
        finally:
            if db is not None:
                db.close()
            self._reads.release()


def from_env(database: Path, owner_id: str, model_id: str, env: Mapping[str, str] | None = None):
    values = os.environ if env is None else env
    enabled = enabled_from_env(values)
    flag, key = values.get("STBRAIN_ATLAS_BOOTSTRAP_ENABLED"), values.get("STBRAIN_ATLAS_ID_KEY")
    forbidden_raw = values.get("STBRAIN_ATLAS_FORBIDDEN_VERIFIERS_JSON")
    if flag is None and key is None and forbidden_raw is None:
        return None  # Exactly legacy: no store, new file, schema inspection or key access.
    try:
        if flag not in {"0", "1"} or not isinstance(key, str) or not HEX64.fullmatch(key):
            raise ValueError()
        if not isinstance(forbidden_raw, str) or len(forbidden_raw) > 4096:
            raise ValueError()
        forbidden = json.loads(forbidden_raw)
        if (not isinstance(forbidden, list) or not 1 <= len(forbidden) <= 32 or
                any(not isinstance(item, str) or not HEX64.fullmatch(item) for item in forbidden) or
                len(set(forbidden)) != len(forbidden)):
            raise ValueError()
        if hashlib.sha256(key.encode("ascii")).hexdigest() not in forbidden:
            raise ValueError()
    except (TypeError, ValueError, RecursionError):
        raise ValueError("atlas_configuration_invalid") from None
    return AtlasDeviceGrants(database, owner_id, model_id, enabled=enabled,
                             id_key=bytes.fromhex(key), forbidden=frozenset(forbidden))
