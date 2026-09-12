"""Physically isolated idea box for module-three learning memory.

Ideas and hypotheses are intentionally stored outside the main learning database.
They are never joined into normal recall or injection and always carry a runtime
generated, non-removable unverified label.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Mapping, Sequence
from .credential_guard import contains_credential_or_secret
from uuid import uuid4


IDEA_BOX_LABEL = "【未验证的想法，不是事实】"
IDEA_KINDS = frozenset({"idea", "hypothesis"})


class LearningIdeaBoxError(RuntimeError):
    """A stable, content-free idea-box rejection reason."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _contains_secret(*values: Any) -> bool:
    return contains_credential_or_secret(values)


def _text(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LearningIdeaBoxError(f"{name}_required")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise LearningIdeaBoxError(f"{name}_too_long")
    return cleaned


def _strings(name: str, value: Any, maximum: int, item_chars: int = 300) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise LearningIdeaBoxError(f"{name}_invalid")
    result: list[str] = []
    for item in value:
        result.append(_text(name, item, item_chars))
    return result


class LearningIdeaBox:
    """Small independent SQLite store with no automatic recall API."""

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS learning_idea_box (
                    idea_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    forced_label TEXT NOT NULL,
                    text TEXT NOT NULL,
                    trigger_learning_refs_json TEXT NOT NULL,
                    source_learning_refs_json TEXT NOT NULL,
                    inference_chain_json TEXT NOT NULL,
                    uncertainties_json TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    current_version INTEGER NOT NULL,
                    created_wake_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(kind IN ('idea','hypothesis')),
                    CHECK(lifecycle IN ('active','archived','promoted')),
                    CHECK(current_version >= 1)
                );

                CREATE TABLE IF NOT EXISTS learning_idea_versions (
                    version_id TEXT PRIMARY KEY,
                    idea_id TEXT NOT NULL REFERENCES learning_idea_box(idea_id),
                    version INTEGER NOT NULL,
                    content_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    wake_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(idea_id, version)
                );

                CREATE TABLE IF NOT EXISTS learning_idea_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    idea_id TEXT NOT NULL REFERENCES learning_idea_box(idea_id),
                    evidence_ref TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS learning_idea_audit (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    idea_id TEXT,
                    action TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    details_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_learning_idea_owner
                    ON learning_idea_box(owner_id, model_id, lifecycle, updated_at);
                """
            )

    @staticmethod
    def _content(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "kind": row["kind"],
            "text": row["text"],
            "trigger_learning_refs": json.loads(row["trigger_learning_refs_json"]),
            "source_learning_refs": json.loads(row["source_learning_refs_json"]),
            "inference_chain": json.loads(row["inference_chain_json"]),
            "uncertainties": json.loads(row["uncertainties_json"]),
            "lifecycle": row["lifecycle"],
        }

    def create(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        kind: str,
        text: str,
        trigger_learning_refs: Sequence[str],
        source_learning_refs: Sequence[str] = (),
        inference_chain: Sequence[str] = (),
        uncertainties: Sequence[str] = (),
        reason: str,
    ) -> dict[str, Any]:
        owner_id = _text("owner_id", owner_id, 200)
        model_id = _text("model_id", model_id, 200)
        wake_id = _text("wake_id", wake_id, 200)
        if kind not in IDEA_KINDS:
            raise LearningIdeaBoxError("idea_kind_invalid")
        body = _text("idea_text", text, 4000)
        triggers = _strings("trigger_learning_refs", list(trigger_learning_refs), 20, 300)
        if not triggers:
            raise LearningIdeaBoxError("trigger_learning_refs_required")
        sources = _strings("source_learning_refs", list(source_learning_refs), 20, 300)
        chain = _strings("inference_chain", list(inference_chain), 20, 500)
        doubts = _strings("uncertainties", list(uncertainties), 12, 500)
        reason = _text("reason", reason, 1000)
        # This check deliberately runs before IDs, hashes, audit rows, or any
        # SQLite transaction are created.  Rejected secret material therefore
        # leaves no reversible or ordinary-hash residue in the idea store.
        if _contains_secret(body, triggers, sources, chain, doubts, reason):
            raise LearningIdeaBoxError("credential_or_secret_detected")
        idea_id = _new_id("idea")
        now = _now()
        content = {
            "kind": kind,
            "text": body,
            "trigger_learning_refs": triggers,
            "source_learning_refs": sources,
            "inference_chain": chain,
            "uncertainties": doubts,
            "lifecycle": "active",
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO learning_idea_box "
                "(idea_id, owner_id, model_id, kind, forced_label, text, "
                " trigger_learning_refs_json, source_learning_refs_json, "
                " inference_chain_json, uncertainties_json, lifecycle, current_version, "
                " created_wake_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?)",
                (
                    idea_id,
                    owner_id,
                    model_id,
                    kind,
                    IDEA_BOX_LABEL,
                    body,
                    _canonical(triggers),
                    _canonical(sources),
                    _canonical(chain),
                    _canonical(doubts),
                    wake_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO learning_idea_versions "
                "(version_id, idea_id, version, content_json, content_hash, reason, wake_id, created_at) "
                "VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                (_new_id("ideaver"), idea_id, _canonical(content), _sha256(content), reason, wake_id, now),
            )
            details = {"kind": kind, "trigger_count": len(triggers)}
            connection.execute(
                "INSERT INTO learning_idea_audit "
                "(event_id, owner_id, model_id, idea_id, action, decision, details_json, details_hash, created_at) "
                "VALUES (?, ?, ?, ?, 'create', 'stored_isolated', ?, ?, ?)",
                (_new_id("ideaevt"), owner_id, model_id, idea_id, _canonical(details), _sha256(details), now),
            )
        return {
            "decision": "stored_isolated",
            "idea_id": idea_id,
            "idea_ref": f"idea://{idea_id}@1",
            "forced_label": IDEA_BOX_LABEL,
            "lifecycle": "active",
        }

    def query(
        self,
        *,
        owner_id: str,
        model_id: str,
        query: str = "",
        idea_id: str | None = None,
        include_archived: bool = False,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise LearningIdeaBoxError("limit_invalid")
        has_query = isinstance(query, str) and bool(query.strip())
        has_id = isinstance(idea_id, str) and bool(idea_id.strip())
        if has_query == has_id:
            raise LearningIdeaBoxError("provide_exactly_one_query_or_idea_id")
        with self._connect() as connection:
            clauses = ["owner_id = ?", "model_id = ?"]
            params: list[Any] = [owner_id, model_id]
            if not include_archived:
                clauses.append("lifecycle = 'active'")
            if has_id:
                clauses.append("idea_id = ?")
                params.append(idea_id.strip())
            rows = connection.execute(
                "SELECT * FROM learning_idea_box WHERE " + " AND ".join(clauses) +
                " ORDER BY updated_at DESC, idea_id LIMIT ?",
                (*params, limit * 4),
            ).fetchall()
        needle = query.strip().casefold() if has_query else ""
        results: list[dict[str, Any]] = []
        for row in rows:
            content = self._content(row)
            if needle and needle not in _canonical(content).casefold():
                continue
            results.append(
                {
                    "idea_id": row["idea_id"],
                    "idea_ref": f"idea://{row['idea_id']}@{row['current_version']}",
                    "forced_label": IDEA_BOX_LABEL,
                    "content": content,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
            if len(results) >= limit:
                break
        return results

    def promote_marker(
        self,
        *,
        owner_id: str,
        model_id: str,
        idea_id: str,
        evidence_refs: Sequence[str],
    ) -> None:
        """Mark an idea promoted only after the main-store promotion transaction succeeds."""

        refs = _strings("evidence_refs", list(evidence_refs), 16, 300)
        if not refs:
            raise LearningIdeaBoxError("promotion_evidence_required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE learning_idea_box SET lifecycle = 'promoted', updated_at = ? "
                "WHERE owner_id = ? AND model_id = ? AND idea_id = ? AND lifecycle = 'active'",
                (_now(), owner_id, model_id, idea_id),
            )
            if cursor.rowcount != 1:
                raise LearningIdeaBoxError("idea_not_active")
