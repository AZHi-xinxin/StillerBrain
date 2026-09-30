"""Explicit owner-scoped relationships; never rewrite or automatically recall a body.

Endpoint identity survives body revisions. Pair state has its own append-only
versions, and a disabled pair never resurrects when an old request is retried.
Only emotion/learning/plan are supported; work memory remains unchanged.
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
from .work_memory import ATLAS_TOKEN

CONTRACT = "memory-relations/1"
REF = re.compile(r"(emotion|learning|plan)://([A-Za-z0-9_-]{1,256})@([1-9][0-9]{0,9})\Z")
EDGE_REF = re.compile(r"relation://([0-9a-f]{32})@([1-9][0-9]{0,9})\Z")
TYPES = {"same_event", "continuation_of", "continues", "caused_by", "causes", "related_to", "custom"}
REVERSE = {"same_event": "same_event", "continuation_of": "continues", "continues": "continuation_of",
           "caused_by": "causes", "causes": "caused_by", "related_to": "related_to", "custom": "custom"}
SCOPES = {"emotion": "emotional_memory", "learning": "learning_memory", "plan": "planning_memory"}
ITEM_SQL = """CREATE TABLE IF NOT EXISTS memory_relation_items (
    owner_id TEXT NOT NULL, model_id TEXT NOT NULL, relation_id TEXT NOT NULL,
    relation_key TEXT NOT NULL, from_module TEXT NOT NULL, from_id TEXT NOT NULL,
    to_module TEXT NOT NULL, to_id TEXT NOT NULL, relation_type TEXT NOT NULL,
    label TEXT, reverse_label TEXT,
    current_version INTEGER NOT NULL CHECK(current_version>=1),
    active INTEGER NOT NULL CHECK(active IN (0,1)), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(owner_id,model_id,relation_id), UNIQUE(owner_id,model_id,relation_key)
)"""
VERSION_SQL = """CREATE TABLE IF NOT EXISTS memory_relation_versions (
    owner_id TEXT NOT NULL, model_id TEXT NOT NULL, relation_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version>=1), active INTEGER NOT NULL CHECK(active IN (0,1)),
    from_ref TEXT NOT NULL, to_ref TEXT NOT NULL, operation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(owner_id,model_id,relation_id,version),
    FOREIGN KEY(owner_id,model_id,relation_id) REFERENCES memory_relation_items(owner_id,model_id,relation_id)
)"""
REQUEST_SQL = """CREATE TABLE IF NOT EXISTS memory_relation_requests (
    owner_id TEXT NOT NULL, model_id TEXT NOT NULL, request_key TEXT NOT NULL,
    operation_hash TEXT NOT NULL, relation_id TEXT NOT NULL, result_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(owner_id,model_id,request_key),
    FOREIGN KEY(owner_id,model_id,relation_id,result_version)
        REFERENCES memory_relation_versions(owner_id,model_id,relation_id,version)
)"""
INDEX_SQL = """CREATE INDEX IF NOT EXISTS memory_relation_endpoint_order
ON memory_relation_items(owner_id,model_id,active,updated_at,relation_id)"""
DDL = {"memory_relation_items": ("table", ITEM_SQL), "memory_relation_versions": ("table", VERSION_SQL),
       "memory_relation_requests": ("table", REQUEST_SQL), "memory_relation_endpoint_order": ("index", INDEX_SQL)}


class MemoryRelationError(ValueError):
    """Fixed value-free diagnostics only."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _ddl(value):
    return " ".join(value.replace("IF NOT EXISTS ", "").split())


def parse_memory_ref(value):
    match = REF.fullmatch(value) if isinstance(value, str) else None
    if match is None or int(match[3]) > 2147483647:
        raise MemoryRelationError("invalid_memory_ref")
    return match[1], match[2], int(match[3])


def parse_edge_ref(value):
    match = EDGE_REF.fullmatch(value) if isinstance(value, str) else None
    if match is None or int(match[2]) > 2147483647:
        raise MemoryRelationError("invalid_edge_ref")
    return match[1], int(match[2])


def validate_relation(from_ref, to_ref, kind, label=None, reverse_label=None):
    first, second = parse_memory_ref(from_ref), parse_memory_ref(to_ref)
    if first[:2] == second[:2]:
        raise MemoryRelationError("self_relation_forbidden")
    if not isinstance(kind, str) or kind not in TYPES:
        raise MemoryRelationError("invalid_relation_type")
    if kind == "custom":
        for index, text in enumerate((label, reverse_label)):
            if index == 1 and text is None:
                continue
            if (not isinstance(text, str) or not text.strip() or text != text.strip() or len(text) > 80 or
                    any(ord(c) < 32 or ord(c) == 127 for c in text)):
                raise MemoryRelationError("invalid_relation_label")
    elif label is not None or reverse_label is not None:
        raise MemoryRelationError("fixed_relation_label_not_allowed")
    values = [from_ref, to_ref, kind, label, reverse_label]
    try:
        if contains_credential_or_secret(values) or any(ATLAS_TOKEN.search(t) for t in values if isinstance(t, str)):
            raise MemoryRelationError("credential_or_secret_detected")
        _json(values).encode("utf-8")
    except UnicodeError:
        raise MemoryRelationError("invalid_relation_label") from None
    if kind in {"continues", "causes"}:
        first, second, from_ref, to_ref, kind = second, first, to_ref, from_ref, REVERSE[kind]
    elif kind in {"same_event", "related_to"} and first[:2] > second[:2]:
        first, second, from_ref, to_ref = second, first, to_ref, from_ref
    # Custom direction stays authored; absent reverse_label is not invented.
    return first, second, from_ref, to_ref, kind, label, reverse_label


class MemoryRelationStore:
    def __init__(self, database: str | Path):
        self.database = str(Path(database).resolve())
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            present = db.execute("SELECT name FROM sqlite_master WHERE name IN (?,?,?,?)", tuple(DDL)).fetchall()
            if not present:
                for _, sql in DDL.values():
                    db.execute(sql)
            self._schema(db)
            db.commit()

    @contextmanager
    def _connection(self, *, write=False):
        db = None
        try:
            path = Path(self.database).resolve(strict=True)
            db = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=2, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            if not write:
                db.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + 3
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            yield db
        except (sqlite3.Error, OSError, UnicodeError):
            raise MemoryRelationError("relation_storage_unavailable") from None
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _schema(db):
        for name, (kind, sql) in DDL.items():
            row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if row is None or row[0] != kind or not isinstance(row[1], str) or _ddl(row[1]) != _ddl(sql):
                raise MemoryRelationError("relation_storage_unavailable")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?,?,?)", tuple(DDL)[:3]).fetchone():
            raise MemoryRelationError("relation_storage_unavailable")

    @staticmethod
    def _identity(owner_id, model_id):
        if any(not isinstance(t, str) or not t or t != t.strip() or len(t) > 256 for t in (owner_id, model_id)):
            raise MemoryRelationError("invalid_identity")
        expected_execution_wake(owner_id=owner_id, model_id=model_id)

    @staticmethod
    def _key(key):
        if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)):
            raise MemoryRelationError("invalid_request_key")

    @staticmethod
    def _endpoint(db, scope, endpoint, *, current=False):
        module, identity, version = endpoint
        tables = {"emotion": ("emotion_memories",), "learning": ("learning_items",),
                  "plan": ("planning_items", "planning_versions")}[module]
        for table in tables:
            row = db.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchone()
            if row is None or row[0] != "table":
                raise MemoryRelationError("relation_endpoint_unavailable")
        # Project fields individually. Never return original_text/summary/current_json/content_json.
        if module == "emotion":
            row = db.execute("SELECT current_version,lifecycle,NULL AS title,sensitivity,memory_type AS kind "
                "FROM emotion_memories WHERE owner_id=? AND model_id=? AND memory_id=?", (*scope, identity)).fetchone()
        elif module == "learning":
            row = db.execute("SELECT current_version,lifecycle,json_extract(current_json,'$.title') AS title,"
                "json_extract(current_json,'$.sensitivity') AS sensitivity,kind FROM learning_items "
                "WHERE owner_id=? AND model_id=? AND learning_id=?", (*scope, identity)).fetchone()
        else:
            row = db.execute("SELECT i.current_version,i.recall_lifecycle AS lifecycle,"
                "json_extract(v.content_json,'$.title') AS title,"
                "'internal' AS sensitivity,i.kind "
                "FROM planning_items i JOIN planning_versions v ON v.plan_id=i.plan_id AND v.version=i.current_version "
                "WHERE i.owner_id=? AND i.model_id=? AND i.plan_id=?", (*scope, identity)).fetchone()
        if row is None or row["lifecycle"] != "active":
            raise MemoryRelationError("relation_endpoint_unavailable")
        if type(row["current_version"]) is not int or version > row["current_version"] or (current and version != row["current_version"]):
            raise MemoryRelationError("memory_version_conflict")
        if row["title"] is not None and (not isinstance(row["title"], str) or len(row["title"]) > 120):
            raise MemoryRelationError("relation_endpoint_unavailable")
        if (module != "emotion" and row["title"] is None) or not isinstance(row["kind"], str) or len(row["kind"]) > 80:
            raise MemoryRelationError("relation_endpoint_unavailable")
        restricted = row["sensitivity"] in {"intimate", "restricted"}
        # Missing sensitivity is unavailable, not permission to disclose a title.
        if row["sensitivity"] not in {"public", "internal", "intimate", "restricted", "private"}:
            restricted = True
        return {"target_ref": f"{module}://{identity}@{row['current_version']}",
                "target_title": None if module == "emotion" else ("受限记忆" if restricted else row["title"]),
                "target_module": module, "target_type": row["kind"], "restricted_stub": restricted}

    @staticmethod
    def _item(db, scope, identity):
        row = db.execute("SELECT * FROM memory_relation_items WHERE owner_id=? AND model_id=? AND relation_id=?",
                         (*scope, identity)).fetchone()
        if row is None:
            raise MemoryRelationError("relation_not_found")
        return row

    @staticmethod
    def _endpoints(row):
        return (row["from_module"], row["from_id"], 1), (row["to_module"], row["to_id"], 1)

    @staticmethod
    def _result(row, *, changed, replay=False):
        return {"decision": "attached" if row["active"] else "detached", "state_changed": changed,
                "edge_ref": f"relation://{row['relation_id']}@{row['current_version']}",
                "active": bool(row["active"]), "idempotent_replay": replay, "automatic_recall_eligible": False}

    def _replay(self, db, scope, key, operation):
        if key is None:
            return None
        row = db.execute("SELECT * FROM memory_relation_requests WHERE owner_id=? AND model_id=? AND request_key=?",
                         (*scope, key)).fetchone()
        if row is None:
            return None
        if row["operation_hash"] != operation:
            raise MemoryRelationError("request_conflict")
        current = self._item(db, scope, row["relation_id"])
        return self._result(current, changed=False, replay=True)

    @staticmethod
    def _receipt(db, scope, key, operation, row):
        if key is not None:
            db.execute("INSERT INTO memory_relation_requests VALUES(?,?,?,?,?,?,?)",
                       (*scope, key, operation, row["relation_id"], row["current_version"], _now()))

    def relation_scopes(self, *, owner_id, model_id, edge_ref):
        self._identity(owner_id, model_id)
        identity, _ = parse_edge_ref(edge_ref)
        with self._connection() as db:
            self._schema(db)
            assert_bound_execution(db)
            row = self._item(db, (owner_id, model_id), identity)
            return {SCOPES[row["from_module"]], SCOPES[row["to_module"]]}

    def attach(self, *, owner_id, model_id, from_ref, to_ref, type, label=None, reverse_label=None,
               request_key=None, _write_guard=None):
        self._identity(owner_id, model_id); self._key(request_key)
        first, second, left_ref, right_ref, kind, label, reverse_label = validate_relation(from_ref, to_ref, type, label, reverse_label)
        identity_key = _hash([first[:2], second[:2], kind, label, reverse_label])
        operation = _hash(["attach", left_ref, right_ref, kind, label, reverse_label])
        scope = owner_id, model_id
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE"); self._schema(db); assert_bound_execution(db)
            if _write_guard is not None:
                _write_guard(db)
            replay = self._replay(db, scope, request_key, operation)
            if replay is not None:
                return replay
            self._endpoint(db, scope, first, current=True); self._endpoint(db, scope, second, current=True)
            row = db.execute("SELECT * FROM memory_relation_items WHERE owner_id=? AND model_id=? AND relation_key=?",
                             (*scope, identity_key)).fetchone()
            now = _now(); changed = row is None or not row["active"]
            if row is None:
                relation_id, version = uuid.uuid4().hex, 1
                db.execute("INSERT INTO memory_relation_items VALUES(?,?,?,?,?,?,?,?,?,?,?,1,1,?,?)",
                    (*scope, relation_id, identity_key, *first[:2], *second[:2], kind, label, reverse_label, now, now))
            else:
                relation_id, version = row["relation_id"], row["current_version"]
                if changed:
                    # A retry without a distinct operation identity cannot resurrect a tombstone.
                    if request_key is None:
                        raise MemoryRelationError("reattach_requires_new_request")
                    if version >= 2147483647:
                        raise MemoryRelationError("edge_version_conflict")
                    version += 1
                    db.execute("UPDATE memory_relation_items SET current_version=?,active=1,updated_at=? "
                               "WHERE owner_id=? AND model_id=? AND relation_id=?", (version, now, *scope, relation_id))
            if changed:
                db.execute("INSERT INTO memory_relation_versions VALUES(?,?,?,?,1,?,?,?,?)",
                           (*scope, relation_id, version, left_ref, right_ref, "attach", now))
            row = self._item(db, scope, relation_id)
            self._receipt(db, scope, request_key, operation, row)
            db.commit()
            return self._result(row, changed=changed)

    def detach(self, *, owner_id, model_id, edge_ref, request_key=None, _write_guard=None):
        self._identity(owner_id, model_id); self._key(request_key)
        identity, version = parse_edge_ref(edge_ref)
        scope, operation = (owner_id, model_id), _hash(["detach", edge_ref])
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE"); self._schema(db); assert_bound_execution(db)
            if _write_guard is not None:
                _write_guard(db)
            replay = self._replay(db, scope, request_key, operation)
            if replay is not None:
                return replay
            row = self._item(db, scope, identity)
            if not row["active"] and version in {row["current_version"], row["current_version"] - 1}:
                return self._result(row, changed=False, replay=True)
            if version != row["current_version"] or version >= 2147483647:
                raise MemoryRelationError("edge_version_conflict")
            previous = db.execute("SELECT from_ref,to_ref FROM memory_relation_versions WHERE owner_id=? AND model_id=? "
                                  "AND relation_id=? AND version=?", (*scope, identity, version)).fetchone()
            now = _now()
            db.execute("UPDATE memory_relation_items SET current_version=?,active=0,updated_at=? WHERE owner_id=? AND model_id=? AND relation_id=?",
                       (version + 1, now, *scope, identity))
            db.execute("INSERT INTO memory_relation_versions VALUES(?,?,?,?,0,?,?,?,?)",
                       (*scope, identity, version + 1, *previous, "detach", now))
            row = self._item(db, scope, identity)
            self._receipt(db, scope, request_key, operation, row); db.commit()
            return self._result(row, changed=True)

    def read(self, *, owner_id, model_id, target_ref, limit=30, offset=0, allowed_scopes=None):
        self._identity(owner_id, model_id)
        endpoint = parse_memory_ref(target_ref)
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or not 0 <= offset <= 100000:
            raise MemoryRelationError("invalid_relation_page")
        allowed = set(SCOPES.values()) if allowed_scopes is None else set(allowed_scopes)
        if SCOPES[endpoint[0]] not in allowed:
            raise MemoryRelationError("relation_scope_not_authorized")
        scope = owner_id, model_id
        with self._connection() as db:
            db.execute("BEGIN"); self._schema(db); assert_bound_execution(db)
            source = self._endpoint(db, scope, endpoint)
            rows = db.execute("SELECT * FROM memory_relation_items WHERE owner_id=? AND model_id=? AND active=1 "
                "AND ((from_module=? AND from_id=?) OR (to_module=? AND to_id=?)) ORDER BY relation_id LIMIT ? OFFSET ?",
                (*scope, *endpoint[:2], *endpoint[:2], limit + 1, offset)).fetchall()
            results = []
            for row in rows[:limit]:
                forward = (row["from_module"], row["from_id"]) == endpoint[:2]
                other = (row["to_module"], row["to_id"], 1) if forward else (row["from_module"], row["from_id"], 1)
                if SCOPES[other[0]] not in allowed:
                    continue
                try:
                    target = self._endpoint(db, scope, other)
                except MemoryRelationError as exc:
                    if str(exc) == "relation_endpoint_unavailable":
                        continue
                    raise
                restricted = source["restricted_stub"] or target["restricted_stub"]
                kind = row["relation_type"] if forward else REVERSE[row["relation_type"]]
                results.append({"edge_ref": f"relation://{row['relation_id']}@{row['current_version']}",
                    "type": kind, "label": None if restricted else (row["label"] if forward else row["reverse_label"]),
                    "direction": "outgoing" if forward else "incoming", "stored_at": row["created_at"], **target})
            return {"decision": "read", "state_changed": False, "source_ref": source["target_ref"], "relations": results,
                    "next_offset": offset + limit if len(rows) > limit else None, "automatic_recall_eligible": False}
