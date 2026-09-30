"""Read-only, owner-scoped metadata projection; never initialize an ST store.

Only active emotional/learning/planning records are visible. No body, title,
summary, evidence, tool card, self-model or quarantine database is read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import time


SCHEMA = "orbis.st.atlas/1"
MAX_STARS = 2000
MAX_EDGES = 5000
MAX_RESPONSE_BYTES = 1024 * 1024


class AtlasUnavailable(Exception):
    """A deliberately non-enumerating failure, safe to expose as a fixed code."""


@dataclass(frozen=True)
class AtlasScope:
    database: Path = field(repr=False)
    owner_id: str = field(repr=False)
    model_id: str = field(repr=False)
    id_key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.database, Path) or not self.database.is_absolute():
            raise ValueError("atlas_configuration_invalid")
        for identity in (self.owner_id, self.model_id):
            if not isinstance(identity, str) or not identity.strip() or len(identity) > 256:
                raise ValueError("atlas_configuration_invalid")
        if not isinstance(self.id_key, bytes) or not 32 <= len(self.id_key) <= 512:
            raise ValueError("atlas_configuration_invalid")


# SQL may use these fields for filtering/joining, but SELECT projects only IDs,
# timestamps and edge endpoint IDs. An authorizer also prevents accidental body
# reads during future edits. sqlite_schema is used only to reject views/absence.
READ_COLUMNS = {
    "sqlite_master": {"name", "type"},
    "emotion_memories": {"memory_id", "owner_id", "model_id", "lifecycle", "created_at"},
    "emotion_edges": {"owner_id", "model_id", "from_memory_id", "to_memory_id", "lifecycle"},
    "learning_items": {"learning_id", "owner_id", "model_id", "lifecycle", "current_version", "created_at"},
    "learning_links": {"owner_id", "model_id", "from_ref", "to_ref"},
    "planning_items": {"plan_id", "owner_id", "model_id", "recall_lifecycle", "current_version", "created_at"},
    "planning_edges": {"owner_id", "model_id", "source_plan_id", "target_plan_id", "active", "source_version"},
    "memory_relation_items": {"owner_id", "model_id", "from_module", "from_id", "to_module", "to_id", "active"},
}

# Optional additive store: old installations remain readable. A partially
# migrated relation store fails closed; the reader never initializes it.
RELATION_TABLES = {"memory_relation_items", "memory_relation_versions", "memory_relation_requests"}

MODULES = (
    ("emotion", "情感", "emotion_memories", "memory_id", "lifecycle"),
    ("learning", "学习", "learning_items", "learning_id", "lifecycle"),
    ("planning", "规划", "planning_items", "plan_id", "recall_lifecycle"),
)


def _authorize(action: int, first: str | None, second: str | None,
               database: str | None, source: str | None) -> int:
    if source is not None:  # No views/triggers even if a name imitates a table.
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ:
        return (sqlite3.SQLITE_OK if database == "main" and
                second in READ_COLUMNS.get(first, set()) else sqlite3.SQLITE_DENY)
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and second == "julianday":
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _instant(raw: object) -> str:
    if not isinstance(raw, str) or len(raw) > 64:
        raise AtlasUnavailable("atlas_unavailable")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone required")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise AtlasUnavailable("atlas_unavailable") from None


def _opaque(scope: AtlasScope, module: str, raw_id: object) -> str:
    if not isinstance(raw_id, str) or not raw_id or len(raw_id) > 1024:
        raise AtlasUnavailable("atlas_unavailable")
    # JSON tuple avoids delimiter ambiguities and scopes identical raw IDs.
    message = json.dumps([SCHEMA, scope.owner_id, scope.model_id, module, raw_id],
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hmac.new(scope.id_key, message, hashlib.sha256).hexdigest()


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


class AtlasMetadataReader:
    """Bounded snapshot over an existing DB, with WAL visible and no migrations.

    Missing/legacy/incompatible modules fail closed. `empty` is only returned
    after all six module tables and their required columns are readable.
    """

    def __init__(self, scope: AtlasScope):
        self.scope = scope

    def read(self) -> bytes:
        connection = None
        try:
            path = self.scope.database.resolve(strict=True)
            if not path.is_file():
                raise AtlasUnavailable("atlas_unavailable")
            # Never use immutable=1: it can silently ignore live WAL contents.
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                         timeout=2, isolation_level=None)
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("BEGIN")
            deadline = time.monotonic() + 3.0
            steps = 0

            def budget() -> int:
                nonlocal steps
                steps += 1000
                return int(steps > 20_000_000 or time.monotonic() > deadline)

            connection.set_progress_handler(budget, 1000)
            connection.set_authorizer(_authorize)
            return self._snapshot(connection)
        except (sqlite3.Error, OSError, ValueError, OverflowError, UnicodeError):
            raise AtlasUnavailable("atlas_unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    def _snapshot(self, connection: sqlite3.Connection) -> bytes:
        required = set(READ_COLUMNS) - {"sqlite_master", "memory_relation_items"}
        inspected = required | RELATION_TABLES
        actual = dict(connection.execute(
            "SELECT name,type FROM sqlite_master WHERE name IN (" + ",".join("?" for _ in inspected) + ")",
            tuple(sorted(inspected)),
        ))
        if (any(actual.get(name) != "table" for name in required) or
                (RELATION_TABLES & actual.keys() and any(actual.get(name) != "table" for name in RELATION_TABLES))):
            raise AtlasUnavailable("atlas_unavailable")
        scope = self.scope
        candidates = []
        learning_refs = {}
        for module, category, table, key, lifecycle in MODULES:
            # At most 2,001 per type suffices to find the latest 2,000 globally.
            # julianday orders ISO timestamps with differing offsets correctly.
            rows = connection.execute(
                f"SELECT {key},created_at{',current_version' if module == 'learning' else ''} FROM {table} "
                f"WHERE owner_id=? AND model_id=? AND {lifecycle}='active' "
                f"ORDER BY julianday(created_at) DESC,{key} LIMIT ?",
                (scope.owner_id, scope.model_id, MAX_STARS + 1),
            )
            for row in rows:
                raw_id, created = row[:2]
                if module == "learning":
                    version = row[2]
                    if type(version) is not int or version < 1:
                        raise AtlasUnavailable("atlas_unavailable")
                    learning_refs[f"learning://{raw_id}@{version}"] = raw_id
                candidates.append((module, raw_id, {
                    "id": _opaque(scope, module, raw_id), "type": category,
                    "storedAt": _instant(created),
                }))
        candidates.sort(key=lambda item: (item[2]["storedAt"], item[2]["id"]), reverse=True)
        truncated = len(candidates) > MAX_STARS
        selected = candidates[:MAX_STARS]
        selected_ids = {(module, raw): star["id"] for module, raw, star in selected}
        edges: set[tuple[str, str]] = set()
        for module, _, _, _, _ in MODULES:
            # Version-bound learning links are not silently applied to newer
            # records; planning only exposes the current active source graph.
            if module == "emotion":
                query = """SELECT e.from_memory_id,e.to_memory_id FROM emotion_edges e
                    JOIN emotion_memories a ON a.memory_id=e.from_memory_id
                    JOIN emotion_memories b ON b.memory_id=e.to_memory_id
                    WHERE e.owner_id=? AND e.model_id=? AND e.lifecycle='active'
                    AND a.owner_id=e.owner_id AND a.model_id=e.model_id AND a.lifecycle='active'
                    AND b.owner_id=e.owner_id AND b.model_id=e.model_id AND b.lifecycle='active'"""
            elif module == "learning":
                # Compare exact versioned refs with the bounded active candidate
                # map instead of concatenation joins (which can become an N²
                # scan of all items even when the edge table is empty).
                query = """SELECT from_ref,to_ref FROM learning_links
                    WHERE owner_id=? AND model_id=?"""
            else:
                query = """SELECT e.source_plan_id,e.target_plan_id FROM planning_edges e
                    JOIN planning_items a ON a.plan_id=e.source_plan_id
                    JOIN planning_items b ON b.plan_id=e.target_plan_id
                    WHERE e.owner_id=? AND e.model_id=? AND e.active=1 AND e.source_version=a.current_version
                    AND a.owner_id=e.owner_id AND a.model_id=e.model_id AND a.recall_lifecycle='active'
                    AND b.owner_id=e.owner_id AND b.model_id=e.model_id AND b.recall_lifecycle='active'"""
            # Cursor streaming, bounded SQL work/time and output allocation; do
            # not cap SQL rows before filtering to selected nodes (would miss
            # valid edges hidden behind older/non-displayed records).
            rows = connection.execute(query, (scope.owner_id, scope.model_id))
            for first, second in rows:
                if module == "learning":
                    first, second = learning_refs.get(first), learning_refs.get(second)
                a = selected_ids.get((module, first))
                b = selected_ids.get((module, second))
                if a is not None and b is not None and a != b:
                    edges.add(tuple(sorted((a, b))))
                    if len(edges) > MAX_EDGES:
                        truncated = True
                        break
            if len(edges) > MAX_EDGES:
                # Still validate remaining modules' required columns, even
                # when their output is already limited.
                edges = set(sorted(edges)[:MAX_EDGES])
        if "memory_relation_items" in actual:
            # Labels, titles, body revisions and request receipts are not read.
            # Resolve both ends only through this owner's selected active stars.
            for left_module, left_id, right_module, right_id in connection.execute(
                "SELECT from_module,from_id,to_module,to_id FROM memory_relation_items "
                "WHERE owner_id=? AND model_id=? AND active=1", (scope.owner_id, scope.model_id)
            ):
                a = selected_ids.get(("planning" if left_module == "plan" else left_module, left_id))
                b = selected_ids.get(("planning" if right_module == "plan" else right_module, right_id))
                if a is not None and b is not None and a != b:
                    edges.add(tuple(sorted((a, b))))
                    if len(edges) > MAX_EDGES:
                        truncated = True
                        break
        result = {
            "schema": SCHEMA,
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "truncated": truncated,
            "stars": [star for _, _, star in selected],
            "edges": [{"a": a, "b": b} for a, b in sorted(edges)[:MAX_EDGES]],
        }
        encoded = _json(result)
        # Defense in depth if metadata width or schema changes in a future edit.
        if len(encoded) > MAX_RESPONSE_BYTES:
            result["truncated"] = True
            pending = result["edges"]
            result["edges"] = []
            available = MAX_RESPONSE_BYTES - len(_json(result))
            kept = []
            for edge in pending:
                size = len(_json(edge)) + (1 if kept else 0)
                if size > available:
                    break
                kept.append(edge)
                available -= size
            result["edges"] = kept
            encoded = _json(result)
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise AtlasUnavailable("atlas_unavailable")
        return encoded
