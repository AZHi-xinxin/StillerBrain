"""Explicit-only body/tag storage in the existing main DB.

This store has no recall injection adapter, learning-card
projection or atlas registration. Versions are immutable; editing needs the
exact previously read version. Retirement is reversible, not deletion.
No transport secret is persisted.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .credential_guard import contains_credential_or_secret
from .execution_binding import assert_bound_execution, expected_execution_wake


MAX_CONTENT_CHARS = 32768
MAX_CONTENT_BYTES = 128 * 1024
MAX_TAG_CHARS = 160
MAX_RESULT_BYTES = 640 * 1024
MAX_OFFSET = 100000
REF = re.compile(r"work://([0-9a-f]{32})@([1-9][0-9]{0,9})\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
# This credential is deliberately isolated from host/MCP keys. Its recognizable
# format must not become work content merely because it lacks a secret label.
ATLAS_TOKEN = re.compile(r"orb_atlas_[A-Za-z0-9_-]{43}")
ITEM_SQL = """CREATE TABLE IF NOT EXISTS work_memory_items (
    owner_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(owner_id,model_id,work_id)
)"""
VERSION_SQL = """CREATE TABLE IF NOT EXISTS work_memory_versions (
    owner_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    content TEXT NOT NULL,
    tag TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_key TEXT,
    operation_hash TEXT NOT NULL,
    PRIMARY KEY(owner_id,model_id,work_id,version),
    UNIQUE(owner_id,model_id,request_key),
    FOREIGN KEY(owner_id,model_id,work_id) REFERENCES work_memory_items(owner_id,model_id,work_id)
)"""
INDEX_SQL = """CREATE INDEX IF NOT EXISTS work_memory_current_order
ON work_memory_items(owner_id,model_id,updated_at DESC,work_id DESC)"""
LIFECYCLE_SQL = """CREATE TABLE IF NOT EXISTS work_memory_lifecycle_versions (
    owner_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','retired')),
    PRIMARY KEY(owner_id,model_id,work_id,version),
    FOREIGN KEY(owner_id,model_id,work_id,version)
        REFERENCES work_memory_versions(owner_id,model_id,work_id,version)
)"""
# Legacy versions have no lifecycle row and mean active. Every new version has
# its own immutable state, including ordinary content/tag edits and restores.
VERSION_SELECT = ("SELECT v.*,COALESCE(l.lifecycle,'active') AS lifecycle "
    "FROM work_memory_versions v LEFT JOIN work_memory_lifecycle_versions l ON "
    "l.owner_id=v.owner_id AND l.model_id=v.model_id AND l.work_id=v.work_id AND l.version=v.version ")


class WorkMemoryError(ValueError):
    """Fixed reason codes only; never echo inputs or SQL errors."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value, field, chars, byte_limit, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > chars:
        raise WorkMemoryError("invalid_" + field)
    try:
        if len(value.encode("utf-8")) > byte_limit:
            raise WorkMemoryError("invalid_" + field)
    except UnicodeError:
        raise WorkMemoryError("invalid_" + field) from None
    return value  # Never normalize, strip or rewrite the author's original.


def validate_content_tag(content, tag):
    _text(content, "content", MAX_CONTENT_CHARS, MAX_CONTENT_BYTES)
    _text(tag, "tag", MAX_TAG_CHARS, MAX_TAG_CHARS * 4)
    if (contains_credential_or_secret({"content": content, "tag": tag}) or
            ATLAS_TOKEN.search(content) or ATLAS_TOKEN.search(tag)):
        raise WorkMemoryError("credential_or_secret_detected")


def parse_ref(value):
    match = REF.fullmatch(value) if isinstance(value, str) else None
    if match is None or int(match[2]) > 2147483647:
        raise WorkMemoryError("invalid_work_ref")
    return match[1], int(match[2])


def validate_revision_fields(content, tag, lifecycle=None):
    if content is None and tag is None and lifecycle is None:
        raise WorkMemoryError("empty_work_revision")
    if lifecycle is not None and (not isinstance(lifecycle, str) or lifecycle not in {"active", "retired"}):
        raise WorkMemoryError("invalid_work_lifecycle")
    if content is not None:
        _text(content, "content", MAX_CONTENT_CHARS, MAX_CONTENT_BYTES)
    if tag is not None:
        _text(tag, "tag", MAX_TAG_CHARS, MAX_TAG_CHARS * 4)
    if (contains_credential_or_secret({"content": content, "tag": tag}) or
            any(ATLAS_TOKEN.search(value) for value in (content, tag) if value is not None)):
        raise WorkMemoryError("credential_or_secret_detected")


def _ddl(value):
    return " ".join(value.replace("IF NOT EXISTS ", "").split())


class WorkMemoryStore:
    def __init__(self, database: str | Path):
        self.database = str(Path(database).resolve())
        # Existing main DB only: do not create a fourth database or substitute
        # an empty file when deployment configuration is wrong.
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            names = db.execute("SELECT name FROM sqlite_master WHERE name IN ('work_memory_items','work_memory_versions','work_memory_current_order')").fetchall()
            if not names:
                db.execute(ITEM_SQL)
                db.execute(VERSION_SQL)
                db.execute(INDEX_SQL)
            # Validate the exact old contract before the sole additive DDL.
            # No old row, original text, index or table is rewritten.
            self._schema(db, legacy=True)
            db.execute(LIFECYCLE_SQL)
            self._schema(db)
            db.commit()

    @contextmanager
    def _connection(self, *, write=False):
        db = None
        try:
            path = Path(self.database).resolve(strict=True)
            db = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=True, timeout=2, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            if not write:
                db.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + 3
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            yield db
        except (sqlite3.Error, OSError):
            raise WorkMemoryError("work_memory_unavailable") from None
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _schema(db, *, legacy=False):
        entries = [("work_memory_items", "table", ITEM_SQL),
                                     ("work_memory_versions", "table", VERSION_SQL),
                                     ("work_memory_current_order", "index", INDEX_SQL)]
        if not legacy:
            entries.append(("work_memory_lifecycle_versions", "table", LIFECYCLE_SQL))
        for name, kind, expected in entries:
            row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if row is None or row[0] != kind or not isinstance(row[1], str) or _ddl(row[1]) != _ddl(expected):
                raise WorkMemoryError("work_memory_unavailable")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN "
                      "('work_memory_items','work_memory_versions','work_memory_lifecycle_versions')").fetchone():
            raise WorkMemoryError("work_memory_unavailable")

    @staticmethod
    def _identity(owner_id, model_id):
        for value in (owner_id, model_id):
            _text(value, "identity", 256, 1024)
        expected_execution_wake(owner_id=owner_id, model_id=model_id)

    @staticmethod
    def _key(request_key):
        if request_key is not None and (not isinstance(request_key, str) or not HEX64.fullmatch(request_key)):
            raise WorkMemoryError("invalid_request_key")

    @staticmethod
    def _write_result(row, *, changed, replay=False):
        return {"decision": "stored" if row["version"] == 1 else "revised", "stored": True,
                "state_changed": changed, "idempotent_replay": replay,
                "target_ref": f'work://{row["work_id"]}@{row["version"]}', "version": row["version"],
                "lifecycle": row["lifecycle"],
                "automatic_recall_eligible": False}

    @staticmethod
    def _replay(db, scope, request_key, operation_hash):
        if request_key is None:
            return None
        row = db.execute(VERSION_SELECT + "WHERE v.owner_id=? AND v.model_id=? AND v.request_key=?",
                         (*scope, request_key)).fetchone()
        if row is not None and row["operation_hash"] != operation_hash:
            raise WorkMemoryError("request_conflict")
        return row

    def remember(self, *, owner_id, model_id, content, tag, request_key=None, _write_guard=None):
        self._identity(owner_id, model_id)
        validate_content_tag(content, tag)
        self._key(request_key)
        operation_hash = hashlib.sha256(_json(["remember", content, tag])).hexdigest()
        scope = (owner_id, model_id)
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            assert_bound_execution(db)
            if _write_guard is not None:
                _write_guard(db)
            previous = self._replay(db, scope, request_key, operation_hash)
            if previous is not None:
                return self._write_result(previous, changed=False, replay=True)
            work_id, now = uuid.uuid4().hex, _now()
            db.execute("INSERT INTO work_memory_items VALUES(?,?,?,1,?,?)", (*scope, work_id, now, now))
            db.execute("INSERT INTO work_memory_versions VALUES(?,?,?,1,?,?,?,?,?)",
                       (*scope, work_id, content, tag, now, request_key, operation_hash))
            db.execute("INSERT INTO work_memory_lifecycle_versions VALUES(?,?,?,1,'active')", (*scope, work_id))
            row = db.execute(VERSION_SELECT + "WHERE v.owner_id=? AND v.model_id=? AND v.work_id=? AND v.version=1",
                             (*scope, work_id)).fetchone()
            db.commit()
            return self._write_result(row, changed=True)

    def revise(self, *, owner_id, model_id, target_ref, content=None, tag=None, lifecycle=None, request_key=None, _write_guard=None):
        self._identity(owner_id, model_id)
        work_id, version = parse_ref(target_ref)
        validate_revision_fields(content, tag, lifecycle)
        self._key(request_key)
        # Preserve pre-upgrade idempotency hashes when the new field is omitted.
        operation = ["revise", target_ref, content, tag]
        if lifecycle is not None:
            operation.append(lifecycle)
        operation_hash = hashlib.sha256(_json(operation)).hexdigest()
        scope = (owner_id, model_id)
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            self._schema(db)
            assert_bound_execution(db)
            if _write_guard is not None:
                _write_guard(db)
            replay = self._replay(db, scope, request_key, operation_hash)
            if replay is not None:
                return self._write_result(replay, changed=False, replay=True)
            previous = db.execute(VERSION_SELECT + "JOIN work_memory_items i ON "
                "v.owner_id=i.owner_id AND v.model_id=i.model_id AND v.work_id=i.work_id AND v.version=i.current_version "
                "WHERE i.owner_id=? AND i.model_id=? AND i.work_id=?", (*scope, work_id)).fetchone()
            if previous is None:
                raise WorkMemoryError("work_memory_not_found")
            if previous["version"] != version or version >= 2147483647:
                raise WorkMemoryError("work_version_conflict")
            new_content = previous["content"] if content is None else content
            new_tag = previous["tag"] if tag is None else tag
            validate_content_tag(new_content, new_tag)
            now = _now()
            updated = db.execute("UPDATE work_memory_items SET current_version=?,updated_at=? "
                                 "WHERE owner_id=? AND model_id=? AND work_id=? AND current_version=?",
                                 (version + 1, now, *scope, work_id, version))
            if updated.rowcount != 1:
                raise WorkMemoryError("work_version_conflict")
            db.execute("INSERT INTO work_memory_versions VALUES(?,?,?,?,?,?,?,?,?)",
                       (*scope, work_id, version + 1, new_content, new_tag, now, request_key, operation_hash))
            db.execute("INSERT INTO work_memory_lifecycle_versions VALUES(?,?,?,?,?)",
                       (*scope, work_id, version + 1, previous["lifecycle"] if lifecycle is None else lifecycle))
            row = db.execute(VERSION_SELECT + "WHERE v.owner_id=? AND v.model_id=? AND v.work_id=? AND v.version=?",
                             (*scope, work_id, version + 1)).fetchone()
            db.commit()
            return self._write_result(row, changed=True)

    def recall(self, *, owner_id, model_id, query="", target_ref="", limit=5, offset=0, include_retired=False):
        self._identity(owner_id, model_id)
        _text(query, "query", MAX_TAG_CHARS, MAX_TAG_CHARS * 4, empty=True)
        if (type(limit) is not int or not 1 <= limit <= 5 or type(offset) is not int or not 0 <= offset <= MAX_OFFSET or
                not isinstance(target_ref, str) or (target_ref and (query or offset)) or type(include_retired) is not bool):
            raise WorkMemoryError("invalid_work_query")
        scope = (owner_id, model_id)
        with self._connection() as db:
            db.execute("BEGIN")
            self._schema(db)
            assert_bound_execution(db)
            # Visibility follows the CURRENT item, even when an older version
            # is explicitly requested. History access never silently restores it.
            select = ("SELECT v.*,i.current_version,COALESCE(l.lifecycle,'active') AS lifecycle,"
                "COALESCE(c.lifecycle,'active') AS current_lifecycle FROM work_memory_items i "
                "JOIN work_memory_versions v ON v.owner_id=i.owner_id AND v.model_id=i.model_id AND v.work_id=i.work_id "
                "LEFT JOIN work_memory_lifecycle_versions l ON l.owner_id=v.owner_id AND l.model_id=v.model_id "
                "AND l.work_id=v.work_id AND l.version=v.version "
                "LEFT JOIN work_memory_lifecycle_versions c ON c.owner_id=i.owner_id AND c.model_id=i.model_id "
                "AND c.work_id=i.work_id AND c.version=i.current_version ")
            visible = "" if include_retired else " AND COALESCE(c.lifecycle,'active')='active' "
            if target_ref:
                work_id, version = parse_ref(target_ref)
                rows = db.execute(select + "WHERE i.owner_id=? AND i.model_id=? AND i.work_id=? AND v.version=?" + visible,
                                  (*scope, work_id, version)).fetchall()
                if not rows:
                    raise WorkMemoryError("work_memory_not_found")
            else:
                rows = db.execute(select +
                    "WHERE i.owner_id=? AND i.model_id=? AND v.version=i.current_version AND instr(v.tag,?)>0 " + visible +
                    "ORDER BY i.updated_at DESC,i.work_id DESC LIMIT ? OFFSET ?",
                    (*scope, query, limit + 1, offset)).fetchall()
            result = {"decision": "recalled", "state_changed": False, "automatic_recall_eligible": False,
                      "results": [], "count": 0, "more": False, "next_offset": None}
            for row in rows[:limit]:
                validate_content_tag(row["content"], row["tag"])
                item = {"target_ref": f'work://{row["work_id"]}@{row["version"]}', "version": row["version"],
                        "lifecycle": row["lifecycle"], "current_lifecycle": row["current_lifecycle"],
                        "current_target_ref": f'work://{row["work_id"]}@{row["current_version"]}',
                        "content": row["content"], "tag": row["tag"], "stored_at": row["created_at"]}
                candidate = {**result, "results": [*result["results"], item], "count": len(result["results"]) + 1,
                             "more": True, "next_offset": offset + len(result["results"]) + 1}
                # Reserve a small fixed envelope for the service/MCP contract;
                # never truncate a record to fit a response.
                if len(_json(candidate)) > MAX_RESULT_BYTES - 2048:
                    break
                result["results"].append(item)
            result["count"] = len(result["results"])
            result["more"] = len(rows) > result["count"]
            result["next_offset"] = offset + result["count"] if result["more"] else None
            return result
