"""Owner-scoped plan graph and append-only event ledger for module five.

The runtime deliberately separates an AI-authored plan from its mutable-looking
state.  Plan text is immutable by version, progress is an append-only event, and
the current state is a deterministic fold over those events.  The explicit
ordinary-record route creates an active record directly, without claiming an
AI adoption statement or a completed review. Explicit advanced submissions now
append the AI's final plan or change directly, without a mechanical calm form,
another wake, or a fabricated review. Older pending candidates remain pending
until the AI explicitly accepts their exact hash or rejects them. All changes
remain wake-bound, CAS-protected, versioned and auditable; progress still needs
evidence and neither editing nor rollback can restore quarantined records.

This module contains no scheduler, notification sender, model call, or external
tool executor.  ``next_action`` and automatic recall are read-only advice.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Sequence
from uuid import uuid4
from .credential_guard import contains_credential_or_secret
from .lexical_retrieval import explicit_alias_match, prepare_explicit_alias_query
from .reminder_excerpt import reminder_excerpt


PLANNING_MODULE = "planning_memory_module_five"
PLANNING_VERSION = "planning-memory/0.1"
PLANNING_RECALL_CONTRACT = "planning-recall/0.1"
PLANNING_PENDING_BATCH_LIMIT = 3

PLAN_KINDS = frozenset({"vision", "goal", "milestone", "task", "commitment"})
PLAN_TRACKS = frozenset({"internal", "relational"})
PRESENCE_MODES = frozenset({"relevant", "session_start", "persistent"})
PLAN_STATES = frozenset({"active", "paused", "completed", "abandoned", "archived"})
DIRECT_EVENT_TYPES = frozenset(
    {"progress", "complete", "pause", "resume", "reopen", "defer_review"}
)
CANDIDATE_INTENTS = frozenset(
    {"create", "revise", "abandon", "archive", "revive", "rollback"}
)
CANDIDATE_DECISIONS = frozenset({"accept", "reject"})
EVIDENCE_SOURCE_KINDS = frozenset(
    {
        "current_conversation",
        "tool_result",
        "document",
        "human_report",
        "ai_observation",
        "cross_module_ref",
    }
)
EVIDENCE_PROVENANCE = frozenset({"verified", "registered", "reported", "claimed"})

_PLAN_ID = re.compile(r"^plan_[0-9a-f]{32}$")
_PLAN_REF = re.compile(r"^plan://(plan_[0-9a-f]{32})@([1-9][0-9]*)$")
_CANDIDATE_HASH = re.compile(r"^[0-9a-f]{64}$")
_FIRST_PERSON = re.compile(r"^(?:我|I(?:\b|['’])|My\b)", re.I)
_CONTENT_FIELDS = frozenset(
    {
        "kind",
        "track",
        "title",
        "original_text",
        "summary",
        "reminder",
        "importance",
        "presence_mode",
        "scene_tags",
        "keywords",
        "start_at",
        "due_at",
        "timezone",
        "review_after",
        "allow_coordination_hint",
        "parent_ref",
        "dependency_refs",
        "ai_adoption_statement",
    }
)
_ORDINARY_CONTENT_FIELDS = (_CONTENT_FIELDS - {"ai_adoption_statement"}) | {"write_mode"}


class PlanningMemoryError(RuntimeError):
    """One stable, content-free rejection reason."""


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now_dt()).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PlanningMemoryError("invalid_datetime") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _json(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _walk(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _walk(item)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        for item in value:
            yield from _walk(item)


def _contains_secret(*values: Any) -> bool:
    return contains_credential_or_secret(values)


def _text(name: str, value: Any, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise PlanningMemoryError(f"invalid_{name}")
    value = value.strip()
    if not value and not allow_empty:
        raise PlanningMemoryError(f"invalid_{name}")
    if len(value) > maximum:
        raise PlanningMemoryError(f"{name}_too_long")
    return value


def _string_list(name: str, value: Any, *, maximum: int, item_maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise PlanningMemoryError(f"invalid_{name}")
    result: list[str] = []
    for item in value:
        text = _text(name, item, item_maximum)
        if text not in result:
            result.append(text)
    return result


def _enum(name: str, value: Any, choices: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise PlanningMemoryError(f"invalid_{name}")
    return value


def _integer(name: str, value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PlanningMemoryError(f"invalid_{name}")
    return value


def _boolean(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise PlanningMemoryError(f"invalid_{name}")
    return value


def _optional_datetime(name: str, value: Any) -> str | None:
    if value is None:
        return None
    value = _text(name, value, 80)
    return _iso(_parse_iso(value))


def _parse_plan_ref(value: str) -> tuple[str, int]:
    if not isinstance(value, str):
        raise PlanningMemoryError("invalid_plan_ref")
    match = _PLAN_REF.fullmatch(value.strip())
    if not match:
        raise PlanningMemoryError("invalid_plan_ref")
    return match.group(1), int(match.group(2))


def _plan_ref(plan_id: str, version: int) -> str:
    return f"plan://{plan_id}@{version}"


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value.casefold())


def _diff(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> dict[str, Any]:
    previous = dict(before or {})
    changed: dict[str, Any] = {}
    for key in sorted(set(previous) | set(after)):
        if previous.get(key) != after.get(key):
            changed[key] = {"before": previous.get(key), "after": after.get(key)}
    return changed


def _validate_calm_check(value: Any) -> dict[str, Any]:
    required = {
        "authorship_confirmed",
        "current_state_checked",
        "dependencies_checked",
        "consequences_reviewed",
        "rollback_understood",
        "notes",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise PlanningMemoryError("calm_check_incomplete")
    for key in required - {"notes"}:
        if value.get(key) is not True:
            raise PlanningMemoryError("calm_check_incomplete")
    return {
        **{key: True for key in sorted(required - {"notes"})},
        "notes": _text("calm_check_notes", value.get("notes"), 1000),
    }


def _validate_evidence(value: Any, *, required: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 16 or (required and not value):
        raise PlanningMemoryError("evidence_required" if required else "invalid_evidence")
    result: list[dict[str, Any]] = []
    for anchor in value:
        if not isinstance(anchor, Mapping) or set(anchor) != {
            "source_kind",
            "source_ref",
            "evidence_summary",
            "provenance",
        }:
            raise PlanningMemoryError("invalid_evidence")
        result.append(
            {
                "source_kind": _enum(
                    "evidence_source_kind", anchor.get("source_kind"), EVIDENCE_SOURCE_KINDS
                ),
                "source_ref": _text("evidence_source_ref", anchor.get("source_ref"), 500),
                "evidence_summary": _text(
                    "evidence_summary", anchor.get("evidence_summary"), 1000
                ),
                "provenance": _enum(
                    "evidence_provenance", anchor.get("provenance"), EVIDENCE_PROVENANCE
                ),
            }
        )
    if _contains_secret(result):
        raise PlanningMemoryError("credential_content_rejected")
    return result


def fold_plan_events(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Fold one ordered ledger into a deterministic public state projection."""

    state: str | None = None
    defer_count = 0
    last_defer_wake: str | None = None
    last_progress_at: str | None = None
    last_event_at: str | None = None
    progress_summary: str | None = None
    for event in events:
        event_type = event.get("event_type")
        wake_id = event.get("wake_id")
        created_at = event.get("created_at")
        if isinstance(created_at, str):
            last_event_at = created_at
        if event_type == "created":
            state = "active"
            defer_count = 0
        elif event_type == "progress":
            defer_count = 0
            last_defer_wake = None
            if isinstance(created_at, str):
                last_progress_at = created_at
            reason = event.get("reason")
            if isinstance(reason, str):
                progress_summary = reason[:200]
        elif event_type == "complete":
            state = "completed"
            defer_count = 0
            last_defer_wake = None
        elif event_type == "pause":
            state = "paused"
        elif event_type in {"resume", "reopen", "revive"}:
            state = "active"
            defer_count = 0
            last_defer_wake = None
        elif event_type == "abandon":
            state = "abandoned"
            defer_count = 0
            last_defer_wake = None
        elif event_type == "archive":
            state = "archived"
            defer_count = 0
            last_defer_wake = None
        elif event_type == "defer_review" and state == "active":
            if isinstance(wake_id, str) and wake_id != last_defer_wake:
                defer_count += 1
                last_defer_wake = wake_id
            if defer_count >= 2:
                state = "paused"
        if state not in PLAN_STATES and state is not None:
            raise PlanningMemoryError("invalid_event_fold")
    if state is None:
        raise PlanningMemoryError("missing_created_event")
    return {
        "state": state,
        "defer_count": defer_count,
        "last_progress_at": last_progress_at,
        "last_event_at": last_event_at,
        "progress_summary": progress_summary,
    }


class PlanningMemoryStore:
    """SQLite/WAL store for ordinary records and advanced AI-adopted plans."""

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _begin(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS planning_module_state (
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    module_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    row_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id, model_id)
                );

                CREATE TABLE IF NOT EXISTS planning_items (
                    plan_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    track TEXT NOT NULL,
                    current_version INTEGER NOT NULL,
                    recall_lifecycle TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner_id, model_id, plan_id),
                    CHECK(recall_lifecycle IN ('active','quarantined'))
                );

                CREATE TABLE IF NOT EXISTS planning_versions (
                    plan_id TEXT NOT NULL REFERENCES planning_items(plan_id),
                    version INTEGER NOT NULL,
                    previous_version INTEGER,
                    content_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    wake_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(plan_id, version)
                );

                CREATE TABLE IF NOT EXISTS planning_edges (
                    edge_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    source_plan_id TEXT NOT NULL REFERENCES planning_items(plan_id),
                    target_plan_id TEXT NOT NULL REFERENCES planning_items(plan_id),
                    edge_type TEXT NOT NULL,
                    source_version INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    retired_at TEXT,
                    CHECK(edge_type IN ('parent','dependency')),
                    CHECK(active IN (0,1))
                );

                CREATE TABLE IF NOT EXISTS planning_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL REFERENCES planning_items(plan_id),
                    event_type TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    wake_id TEXT NOT NULL,
                    wake_seq INTEGER NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS planning_change_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    candidate_version INTEGER NOT NULL,
                    base_version INTEGER NOT NULL,
                    proposed_content_json TEXT NOT NULL,
                    proposed_content_hash TEXT NOT NULL,
                    diff_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    calm_check_json TEXT NOT NULL,
                    candidate_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    submitted_wake_id TEXT NOT NULL,
                    submitted_wake_seq INTEGER NOT NULL,
                    presented_wake_id TEXT,
                    presented_wake_seq INTEGER,
                    reviewed_wake_id TEXT,
                    reviewed_wake_seq INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(status IN ('pending','accepted','rejected','withdrawn','stale'))
                );

                CREATE TABLE IF NOT EXISTS planning_audit_events (
                    audit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    wake_id TEXT,
                    plan_id TEXT,
                    candidate_id TEXT,
                    reason_codes_json TEXT NOT NULL,
                    details_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS planning_idempotency_records (
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id, model_id, action, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS idx_planning_items_owner
                    ON planning_items(owner_id, model_id, recall_lifecycle);
                CREATE INDEX IF NOT EXISTS idx_planning_events_plan
                    ON planning_events(owner_id, model_id, plan_id, event_seq);
                CREATE INDEX IF NOT EXISTS idx_planning_edges_source
                    ON planning_edges(owner_id, model_id, source_plan_id, active);
                CREATE INDEX IF NOT EXISTS idx_planning_candidates_owner
                    ON planning_change_candidates(owner_id, model_id, status, created_at);
                """
            )

    def ensure_state(self, *, owner_id: str, model_id: str) -> None:
        owner_id = _text("owner_id", owner_id, 200)
        model_id = _text("model_id", model_id, 200)
        now = _iso()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO planning_module_state "
                "(owner_id, model_id, module_version, status, row_version, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', 0, ?, ?) "
                "ON CONFLICT(owner_id, model_id) DO NOTHING",
                (owner_id, model_id, PLANNING_VERSION, now, now),
            )

    @staticmethod
    def _state(connection: sqlite3.Connection, owner_id: str, model_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM planning_module_state WHERE owner_id = ? AND model_id = ?",
            (owner_id, model_id),
        ).fetchone()
        if row is None:
            raise PlanningMemoryError("planning_state_missing")
        return row

    @staticmethod
    def _item(
        connection: sqlite3.Connection, owner_id: str, model_id: str, plan_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM planning_items WHERE owner_id = ? AND model_id = ? AND plan_id = ?",
            (owner_id, model_id, plan_id),
        ).fetchone()
        if row is None:
            raise PlanningMemoryError("plan_not_found")
        return row

    @staticmethod
    def _version(
        connection: sqlite3.Connection, plan_id: str, version: int
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM planning_versions WHERE plan_id = ? AND version = ?",
            (plan_id, version),
        ).fetchone()
        if row is None:
            raise PlanningMemoryError("plan_version_not_found")
        return row

    @staticmethod
    def _current_content(connection: sqlite3.Connection, item: sqlite3.Row) -> dict[str, Any]:
        row = PlanningMemoryStore._version(
            connection, item["plan_id"], int(item["current_version"])
        )
        content = _json(row["content_json"], None)
        if not isinstance(content, dict):
            raise PlanningMemoryError("stored_plan_invalid")
        return content

    @staticmethod
    def _event_rows(
        connection: sqlite3.Connection, owner_id: str, model_id: str, plan_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM planning_events WHERE owner_id = ? AND model_id = ? AND plan_id = ? "
            "ORDER BY event_seq",
            (owner_id, model_id, plan_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def _projection(
        self, connection: sqlite3.Connection, owner_id: str, model_id: str, plan_id: str
    ) -> dict[str, Any]:
        return fold_plan_events(self._event_rows(connection, owner_id, model_id, plan_id))

    @staticmethod
    def _advance_state(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        expected_row_version: int,
    ) -> int:
        if isinstance(expected_row_version, bool) or not isinstance(expected_row_version, int):
            raise PlanningMemoryError("invalid_expected_planning_version")
        cursor = connection.execute(
            "UPDATE planning_module_state SET row_version = row_version + 1, updated_at = ? "
            "WHERE owner_id = ? AND model_id = ? AND row_version = ?",
            (_iso(), owner_id, model_id, expected_row_version),
        )
        if cursor.rowcount != 1:
            raise PlanningMemoryError("planning_row_version_conflict")
        return expected_row_version + 1

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        action: str,
        decision: str,
        wake_id: str | None,
        plan_id: str | None = None,
        candidate_id: str | None = None,
        reason_codes: Sequence[str] = (),
        details: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO planning_audit_events "
            "(audit_id, owner_id, model_id, action, decision, wake_id, plan_id, candidate_id, "
            " reason_codes_json, details_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _new_id("planaudit"),
                owner_id,
                model_id,
                action,
                decision,
                wake_id,
                plan_id,
                candidate_id,
                _canonical(list(reason_codes)),
                _sha256(dict(details or {})),
                _iso(),
            ),
        )

    @staticmethod
    def _idempotency_get(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        action: str,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT request_hash, response_json FROM planning_idempotency_records "
            "WHERE owner_id = ? AND model_id = ? AND action = ? AND idempotency_key = ?",
            (owner_id, model_id, action, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise PlanningMemoryError("idempotency_key_reused")
        response = _json(row["response_json"], None)
        if not isinstance(response, dict):
            raise PlanningMemoryError("idempotency_record_invalid")
        return response

    @staticmethod
    def _idempotency_put(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        action: str,
        idempotency_key: str,
        request_hash: str,
        response: Mapping[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO planning_idempotency_records "
            "(owner_id, model_id, action, idempotency_key, request_hash, response_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                owner_id,
                model_id,
                action,
                idempotency_key,
                request_hash,
                _canonical(dict(response)),
                _iso(),
            ),
        )

    def status(self, *, owner_id: str, model_id: str) -> dict[str, Any]:
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            state = self._state(connection, owner_id, model_id)
            rows = connection.execute(
                "SELECT plan_id, recall_lifecycle FROM planning_items "
                "WHERE owner_id = ? AND model_id = ?",
                (owner_id, model_id),
            ).fetchall()
            counts = {key: 0 for key in sorted(PLAN_STATES)}
            quarantined = 0
            for row in rows:
                if row["recall_lifecycle"] == "quarantined":
                    quarantined += 1
                projection = self._projection(
                    connection, owner_id, model_id, row["plan_id"]
                )
                counts[projection["state"]] += 1
            pending = connection.execute(
                "SELECT COUNT(*) AS n FROM planning_change_candidates "
                "WHERE owner_id = ? AND model_id = ? AND status = 'pending'",
                (owner_id, model_id),
            ).fetchone()["n"]
            return {
                "module": PLANNING_MODULE,
                "module_version": state["module_version"],
                "status": state["status"],
                "row_version": state["row_version"],
                "counts": {
                    "plans": len(rows),
                    **counts,
                    "quarantined": quarantined,
                    "pending_changes": pending,
                },
            }

    def _normalize_content(
        self, fields: Mapping[str, Any], *, ordinary: bool = False
    ) -> dict[str, Any]:
        required = _ORDINARY_CONTENT_FIELDS if ordinary else _CONTENT_FIELDS
        if not isinstance(fields, Mapping) or (
            set(fields) != required
            and not (ordinary and set(fields) == required | {"ai_adoption_statement"})
        ):
            raise PlanningMemoryError("invalid_plan_content")
        if ordinary and fields.get("write_mode") != "ordinary_record":
            raise PlanningMemoryError("invalid_plan_write_mode")
        kind = _enum("plan_kind", fields.get("kind"), PLAN_KINDS)
        track = _enum("plan_track", fields.get("track"), PLAN_TRACKS)
        reminder = _text("reminder", fields.get("reminder"), 50, allow_empty=True)
        if not ordinary and track == "internal" and not reminder:
            raise PlanningMemoryError("internal_reminder_required")
        if not ordinary or "ai_adoption_statement" in fields:
            adoption = _text(
                "ai_adoption_statement", fields.get("ai_adoption_statement"), 500
            )
        original = fields.get("original_text")
        _text("original_text", original, 2000)
        if ordinary and len(original) > 2000:
            raise PlanningMemoryError("original_text_too_long")
        parent_ref = fields.get("parent_ref")
        if parent_ref is not None:
            parent_ref = _text("parent_ref", parent_ref, 96)
            _parse_plan_ref(parent_ref)
        dependencies = _string_list(
            "dependency_refs", fields.get("dependency_refs"), maximum=8, item_maximum=96
        )
        for ref in dependencies:
            _parse_plan_ref(ref)
        content = {
            "kind": kind,
            "track": track,
            "title": _text("title", fields.get("title"), 120),
            "original_text": original,
            "summary": _text("summary", fields.get("summary"), 200),
            "reminder": reminder,
            "importance": _integer("importance", fields.get("importance"), 0, 100),
            "presence_mode": _enum(
                "presence_mode", fields.get("presence_mode"), PRESENCE_MODES
            ),
            "scene_tags": _string_list(
                "scene_tags", fields.get("scene_tags"), maximum=16, item_maximum=160
            ),
            "keywords": _string_list(
                "keywords", fields.get("keywords"), maximum=16, item_maximum=160
            ),
            "start_at": _optional_datetime("start_at", fields.get("start_at")),
            "due_at": _optional_datetime("due_at", fields.get("due_at")),
            "timezone": _text("timezone", fields.get("timezone"), 80),
            "review_after": _optional_datetime(
                "review_after", fields.get("review_after")
            ),
            "allow_coordination_hint": _boolean(
                "allow_coordination_hint", fields.get("allow_coordination_hint")
            ),
            "parent_ref": parent_ref,
            "dependency_refs": dependencies,
        }
        if ordinary:
            content["write_mode"] = "ordinary_record"
        if not ordinary or "ai_adoption_statement" in fields:
            content["ai_adoption_statement"] = adoption
        if content["start_at"] and content["due_at"]:
            if _parse_iso(content["due_at"]) < _parse_iso(content["start_at"]):
                raise PlanningMemoryError("due_before_start")
        if len(_canonical(content)) > 8000:
            raise PlanningMemoryError("plan_content_too_long")
        if _contains_secret(content):
            raise PlanningMemoryError("credential_content_rejected")
        return content

    def _normalize_stored_content(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Keep the recorded mode through advanced revisions and rollback.

        Advanced creation uses the AI-adopted content shape; ordinary records
        retain their honest ordinary-record provenance when later edited.
        """
        return self._normalize_content(
            fields,
            ordinary=isinstance(fields, Mapping)
            and fields.get("write_mode") == "ordinary_record",
        )

    @staticmethod
    def _hierarchy_valid(child_kind: str, parent_kind: str) -> bool:
        allowed = {
            "vision": set(),
            "goal": {"vision"},
            "milestone": {"goal"},
            "task": {"goal", "milestone"},
            "commitment": {"vision", "goal"},
        }
        return parent_kind in allowed[child_kind]

    def _validate_ref_target(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        ref: str,
    ) -> sqlite3.Row:
        plan_id, version = _parse_plan_ref(ref)
        row = self._item(connection, owner_id, model_id, plan_id)
        if int(row["current_version"]) != version:
            raise PlanningMemoryError("stale_plan_ref")
        if row["recall_lifecycle"] != "active":
            raise PlanningMemoryError("quarantined_plan_ref")
        return row

    def _validate_graph(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        source_plan_id: str,
        content: Mapping[str, Any],
    ) -> None:
        targets: list[tuple[str, str]] = []
        parent_ref = content.get("parent_ref")
        if isinstance(parent_ref, str):
            parent = self._validate_ref_target(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                ref=parent_ref,
            )
            if not self._hierarchy_valid(str(content["kind"]), str(parent["kind"])):
                raise PlanningMemoryError("invalid_plan_hierarchy")
            targets.append(("parent", parent["plan_id"]))
        # Every kind can stand alone.  An explicit parent still has to exist,
        # match the author's exact version and be a compatible acyclic link.
        for ref in content.get("dependency_refs", []):
            target = self._validate_ref_target(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                ref=ref,
            )
            targets.append(("dependency", target["plan_id"]))
        if any(target == source_plan_id for _kind, target in targets):
            raise PlanningMemoryError("planning_graph_cycle")

        adjacency: dict[str, set[str]] = {}
        rows = connection.execute(
            "SELECT source_plan_id, target_plan_id FROM planning_edges "
            "WHERE owner_id = ? AND model_id = ? AND active = 1 AND source_plan_id != ?",
            (owner_id, model_id, source_plan_id),
        ).fetchall()
        for row in rows:
            adjacency.setdefault(row["source_plan_id"], set()).add(row["target_plan_id"])
        adjacency[source_plan_id] = {target for _kind, target in targets}

        def reaches(start: str, wanted: str) -> bool:
            stack = [start]
            seen: set[str] = set()
            while stack:
                node = stack.pop()
                if node == wanted:
                    return True
                if node in seen:
                    continue
                seen.add(node)
                stack.extend(adjacency.get(node, ()))
            return False

        for _edge_type, target in targets:
            if reaches(target, source_plan_id):
                raise PlanningMemoryError("planning_graph_cycle")

    def _persistent_count(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        exclude_plan_id: str | None = None,
    ) -> int:
        count = 0
        rows = connection.execute(
            "SELECT * FROM planning_items WHERE owner_id = ? AND model_id = ? "
            "AND recall_lifecycle = 'active'",
            (owner_id, model_id),
        ).fetchall()
        for item in rows:
            if item["plan_id"] == exclude_plan_id:
                continue
            projection = self._projection(connection, owner_id, model_id, item["plan_id"])
            if projection["state"] not in {"active", "paused"}:
                continue
            content = self._current_content(connection, item)
            if content.get("presence_mode") == "persistent":
                count += 1
        return count

    def _validate_persistent_slot(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        plan_id: str,
        content: Mapping[str, Any],
    ) -> None:
        if content.get("presence_mode") != "persistent":
            return
        if self._persistent_count(
            connection,
            owner_id=owner_id,
            model_id=model_id,
            exclude_plan_id=plan_id,
        ) >= 2:
            raise PlanningMemoryError("persistent_plan_limit")

    @staticmethod
    def _insert_version(
        connection: sqlite3.Connection,
        *,
        plan_id: str,
        version: int,
        previous_version: int | None,
        content: Mapping[str, Any],
        reason: str,
        wake_id: str,
    ) -> None:
        connection.execute(
            "INSERT INTO planning_versions "
            "(plan_id, version, previous_version, content_json, content_hash, reason, wake_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                version,
                previous_version,
                _canonical(dict(content)),
                _sha256(dict(content)),
                reason,
                wake_id,
                _iso(),
            ),
        )

    @staticmethod
    def _replace_edges(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        source_plan_id: str,
        source_version: int,
        content: Mapping[str, Any],
    ) -> None:
        now = _iso()
        connection.execute(
            "UPDATE planning_edges SET active = 0, retired_at = ? "
            "WHERE owner_id = ? AND model_id = ? AND source_plan_id = ? AND active = 1",
            (now, owner_id, model_id, source_plan_id),
        )
        targets: list[tuple[str, str]] = []
        parent_ref = content.get("parent_ref")
        if isinstance(parent_ref, str):
            targets.append(("parent", _parse_plan_ref(parent_ref)[0]))
        targets.extend(
            ("dependency", _parse_plan_ref(ref)[0])
            for ref in content.get("dependency_refs", [])
        )
        for edge_type, target in targets:
            connection.execute(
                "INSERT INTO planning_edges "
                "(edge_id, owner_id, model_id, source_plan_id, target_plan_id, edge_type, "
                " source_version, active, created_at, retired_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, NULL)",
                (
                    _new_id("planedge"),
                    owner_id,
                    model_id,
                    source_plan_id,
                    target,
                    edge_type,
                    source_version,
                    now,
                ),
            )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        plan_id: str,
        event_type: str,
        reason: str,
        evidence: Sequence[Mapping[str, Any]],
        wake_id: str,
        wake_seq: int,
    ) -> dict[str, Any]:
        event_id = _new_id("planevt")
        created_at = _iso()
        material = {
            "event_id": event_id,
            "owner_id": owner_id,
            "model_id": model_id,
            "plan_id": plan_id,
            "event_type": event_type,
            "reason": reason,
            "evidence": list(evidence),
            "wake_id": wake_id,
            "wake_seq": wake_seq,
            "created_at": created_at,
        }
        connection.execute(
            "INSERT INTO planning_events "
            "(event_id, owner_id, model_id, plan_id, event_type, reason, evidence_json, "
            " wake_id, wake_seq, event_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                owner_id,
                model_id,
                plan_id,
                event_type,
                reason,
                _canonical(list(evidence)),
                wake_id,
                wake_seq,
                _sha256(material),
                created_at,
            ),
        )
        return {
            "event_id": event_id,
            "plan_id": plan_id,
            "event_type": event_type,
            "reason": reason,
            "evidence": list(evidence),
            "wake_seq": wake_seq,
            "created_at": created_at,
        }

    def _create_candidate(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        plan_id: str,
        intent: str,
        base_version: int,
        proposed_content: Mapping[str, Any],
        before: Mapping[str, Any] | None,
        reason: str,
        calm_check: Mapping[str, Any],
    ) -> dict[str, Any]:
        existing = connection.execute(
            "SELECT candidate_id FROM planning_change_candidates "
            "WHERE owner_id = ? AND model_id = ? AND plan_id = ? AND status = 'pending'",
            (owner_id, model_id, plan_id),
        ).fetchone()
        if existing is not None:
            raise PlanningMemoryError("planning_candidate_pending")
        candidate_id = _new_id("plancand")
        diff = _diff(before, proposed_content)
        material = {
            "candidate_id": candidate_id,
            "candidate_version": 1,
            "plan_id": plan_id,
            "intent": intent,
            "base_version": base_version,
            "proposed_content": dict(proposed_content),
            "canonical_diff": diff,
            "reason": reason,
            "calm_check": dict(calm_check),
            "submitted_wake_id": wake_id,
            "submitted_wake_seq": wake_seq,
        }
        candidate_hash = _sha256(material)
        now = _iso()
        connection.execute(
            "INSERT INTO planning_change_candidates "
            "(candidate_id, owner_id, model_id, plan_id, intent, candidate_version, base_version, "
            " proposed_content_json, proposed_content_hash, diff_json, reason, calm_check_json, "
            " candidate_hash, status, submitted_wake_id, submitted_wake_seq, presented_wake_id, "
            " presented_wake_seq, reviewed_wake_id, reviewed_wake_seq, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, NULL, NULL, ?, ?)",
            (
                candidate_id,
                owner_id,
                model_id,
                plan_id,
                intent,
                base_version,
                _canonical(dict(proposed_content)),
                _sha256(dict(proposed_content)),
                _canonical(diff),
                reason,
                _canonical(dict(calm_check)),
                candidate_hash,
                wake_id,
                wake_seq,
                now,
                now,
            ),
        )
        row_version = self._advance_state(
            connection,
            owner_id=owner_id,
            model_id=model_id,
            expected_row_version=expected_row_version,
        )
        self._audit(
            connection,
            owner_id=owner_id,
            model_id=model_id,
            action=f"propose_{intent}",
            decision="candidate_pending",
            wake_id=wake_id,
            plan_id=plan_id,
            candidate_id=candidate_id,
            reason_codes=["later_real_wake_review_required"],
            details={"candidate_hash": candidate_hash, "base_version": base_version},
        )
        return {
            "decision": "candidate_pending",
            "candidate_id": candidate_id,
            "candidate_hash": candidate_hash,
            "candidate_version": 1,
            "plan_id": plan_id,
            "base_version": base_version,
            "intent": intent,
            "planning_row_version": row_version,
            "state_changed": True,
            "active_plan_changed": False,
            "review_requires_later_wake": True,
        }

    def remember_ordinary(
        self,
        *,
        owner_id: str,
        model_id: str,
        write_context_ref: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        content: str,
        idempotency_key: str,
        reason: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        reminder: str | None = None,
        kind: str = "task",
        track: str = "internal",
        importance: int = 50,
        parent_ref: str | None = None,
        dependency_refs: list[str] | None = None,
        scene_tags: list[str] | None = None,
        keywords: list[str] | None = None,
        start_at: str | None = None,
        due_at: str | None = None,
        timezone: str = "UTC",
    ) -> dict[str, Any]:
        """Store an ordinary plan once, without creating/reviewing a candidate.

        The service MUST first validate the exact request-bound reference and
        planning scope, then pass its wake identity here. Text validation of a
        reference here is not authentication, and this store never discovers a
        latest wake or creates a grant. The host supplies a stable idempotency
        key; module CAS remains enforced in the same write transaction.

        The original content is retained verbatim. Omitted title, summary and
        reminder are display excerpts, not AI-authored adoption statements.
        Defaults are a flat task on the internal track, relevant-only presence
        and UTC dates; caller-supplied kind/track are never silently changed.
        Ordinary goals/milestones may be flat; supplied graph edges stay strict.
        """
        owner_id = _text("owner_id", owner_id, 200)
        model_id = _text("model_id", model_id, 200)
        write_context_ref = _text("write_context_ref", write_context_ref, 300)
        if write_context_ref.startswith("$"):
            raise PlanningMemoryError("write_context_ref_placeholder")
        wake_id = _text("wake_id", wake_id, 300)
        if isinstance(wake_seq, bool) or not isinstance(wake_seq, int) or wake_seq < 0:
            raise PlanningMemoryError("invalid_wake_seq")
        if (
            isinstance(expected_row_version, bool)
            or not isinstance(expected_row_version, int)
            or expected_row_version < 0
        ):
            raise PlanningMemoryError("invalid_expected_planning_version")
        idempotency_key = _text("idempotency_key", idempotency_key, 200)
        excerpt = _text("original_text", content, 2000)
        if len(content) > 2000:
            raise PlanningMemoryError("original_text_too_long")
        display_summary = excerpt[:200] if summary is None else summary
        normalized = self._normalize_content(
            {
                "write_mode": "ordinary_record",
                "kind": kind,
                "track": track,
                "title": excerpt[:120] if title is None else title,
                "original_text": content,
                "summary": display_summary,
                "reminder": (
                    reminder_excerpt(_text("summary", display_summary, 200), 50)
                    if reminder is None else reminder
                ),
                "importance": importance,
                "presence_mode": "relevant",
                "scene_tags": [] if scene_tags is None else scene_tags,
                "keywords": [] if keywords is None else keywords,
                "start_at": start_at,
                "due_at": due_at,
                "timezone": timezone,
                "review_after": None,
                "allow_coordination_hint": False,
                "parent_ref": parent_ref,
                "dependency_refs": [] if dependency_refs is None else dependency_refs,
            },
            ordinary=True,
        )
        reason = "ordinary_record" if reason is None else _text("reason", reason, 2000)
        if _contains_secret(reason):
            raise PlanningMemoryError("credential_content_rejected")
        request_hash = _sha256({
            "write_context_ref": write_context_ref,
            "wake_id": wake_id,
            "wake_seq": wake_seq,
            "content": normalized,
            "reason": reason,
        })
        # No initialization or mutation until all supplied content is validated.
        with self._connect() as connection:
            self._begin(connection)
            prior = self._idempotency_get(
                connection, owner_id=owner_id, model_id=model_id,
                action="remember_ordinary", idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if prior is not None:
                return {
                    **prior,
                    "planning_row_version": self._state(connection, owner_id, model_id)["row_version"],
                    "state_changed": False,
                    "active_plan_changed": False,
                    "idempotent_replay": True,
                }
            plan_id = _new_id("plan")
            self._validate_graph(
                connection, owner_id=owner_id, model_id=model_id,
                source_plan_id=plan_id, content=normalized,
            )
            now = _iso()
            connection.execute(
                "INSERT INTO planning_module_state "
                "(owner_id, model_id, module_version, status, row_version, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', 0, ?, ?) ON CONFLICT(owner_id, model_id) DO NOTHING",
                (owner_id, model_id, PLANNING_VERSION, now, now),
            )
            row_version = self._advance_state(
                connection, owner_id=owner_id, model_id=model_id,
                expected_row_version=expected_row_version,
            )
            connection.execute(
                "INSERT INTO planning_items "
                "(plan_id, owner_id, model_id, kind, track, current_version, recall_lifecycle, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, 'active', ?, ?)",
                (plan_id, owner_id, model_id, kind, track, now, now),
            )
            self._insert_version(
                connection, plan_id=plan_id, version=1, previous_version=None,
                content=normalized, reason=reason, wake_id=wake_id,
            )
            self._replace_edges(
                connection, owner_id=owner_id, model_id=model_id,
                source_plan_id=plan_id, source_version=1, content=normalized,
            )
            event = self._append_event(
                connection, owner_id=owner_id, model_id=model_id, plan_id=plan_id,
                event_type="created", reason=reason, evidence=[],
                wake_id=wake_id, wake_seq=wake_seq,
            )
            self._audit(
                connection, owner_id=owner_id, model_id=model_id,
                action="remember_ordinary", decision="stored", wake_id=wake_id,
                plan_id=plan_id, reason_codes=["ordinary_plan_stored"],
                details={"content_hash": _sha256(normalized), "write_mode": "ordinary_record"},
            )
            result = {
                "decision": "stored",
                "reason_codes": ["ordinary_plan_stored"],
                "write_mode": "ordinary_record",
                "plan_id": plan_id,
                "plan_ref": _plan_ref(plan_id, 1),
                "plan_version": 1,
                "planning_row_version": row_version,
                "state": "active",
                "state_changed": True,
                "active_plan_changed": True,
                "candidate_created": False,
                "review_performed": False,
                "event_id": event["event_id"],
                "event": event,
                "display_excerpt_fields": [
                    name for name, supplied in (
                        ("title", title), ("summary", summary), ("reminder", reminder)
                    ) if supplied is None
                ],
                "idempotent_replay": False,
            }
            self._idempotency_put(
                connection, owner_id=owner_id, model_id=model_id,
                action="remember_ordinary", idempotency_key=idempotency_key,
                request_hash=request_hash, response=result,
            )
            return result

    def revise_ordinary(
        self, *, owner_id: str, model_id: str, wake_id: str, wake_seq: int,
        expected_row_version: int, plan_id: str, expected_plan_version: int,
        changes: Mapping[str, Any], reason: str,
    ) -> dict[str, Any]:
        """Append author content through the same exact-version direct path."""
        result = self.revise_direct(
            owner_id=owner_id, model_id=model_id, wake_id=wake_id, wake_seq=wake_seq,
            expected_row_version=expected_row_version, plan_id=plan_id,
            expected_plan_version=expected_plan_version, intent="revise",
            changes=changes, reason=reason, idempotency_key=_new_id("ordinaryrevision"),
        )
        return {**result, "decision": "revised", "id": plan_id,
                "ref": _plan_ref(plan_id, expected_plan_version + 1),
                "version": expected_plan_version + 1, "previous_version": expected_plan_version}

    def _apply_submitted_change(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        plan_id: str,
        base_version: int,
        intent: str,
        proposed: Mapping[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """Append a validated change inside the caller's transaction, not a review."""
        rollback_ref = None
        if intent == "create":
            now = _iso()
            connection.execute(
                "INSERT INTO planning_items "
                "(plan_id, owner_id, model_id, kind, track, current_version, recall_lifecycle, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, 'active', ?, ?)",
                (plan_id, owner_id, model_id, proposed["kind"], proposed["track"], now, now),
            )
            version, event_type = 1, "created"
        elif intent in {"revise", "rollback"}:
            version = base_version + 1
            changed = connection.execute(
                "UPDATE planning_items SET kind=?, track=?, current_version=?, updated_at=? "
                "WHERE owner_id=? AND model_id=? AND plan_id=? AND current_version=? AND recall_lifecycle='active'",
                (proposed["kind"], proposed["track"], version, _iso(), owner_id, model_id, plan_id, base_version),
            ).rowcount
            if changed != 1:
                raise PlanningMemoryError("plan_version_conflict")
            event_type = "content_rollback" if intent == "rollback" else "revised"
            rollback_ref = _plan_ref(plan_id, base_version)
        else:
            version, event_type = base_version, intent
        if intent in {"create", "revise", "rollback"}:
            self._insert_version(
                connection, plan_id=plan_id, version=version,
                previous_version=base_version or None, content=proposed,
                reason=reason, wake_id=wake_id,
            )
            self._replace_edges(
                connection, owner_id=owner_id, model_id=model_id,
                source_plan_id=plan_id, source_version=version, content=proposed,
            )
        event = self._append_event(
            connection, owner_id=owner_id, model_id=model_id, plan_id=plan_id,
            event_type=event_type, reason=reason, evidence=[],
            wake_id=wake_id, wake_seq=wake_seq,
        )
        event_seq = int(connection.execute(
            "SELECT event_seq FROM planning_events WHERE event_id=?", (event["event_id"],),
        ).fetchone()[0])
        return {
            "plan_id": plan_id, "plan_ref": _plan_ref(plan_id, version),
            "plan_version": version, "previous_version": base_version or None,
            "plan_state": self._projection(connection, owner_id, model_id, plan_id),
            "event": event, "event_id": event["event_id"], "event_seq": event_seq,
            "rollback_ref": rollback_ref,
        }

    def remember_direct(
        self, *, owner_id: str, model_id: str, wake_id: str, wake_seq: int,
        expected_row_version: int, content: Mapping[str, Any], reason: str,
        idempotency_key: str, calm_check: Mapping[str, Any] | None = None,
        ai_confirmation: bool | None = None,
    ) -> dict[str, Any]:
        """Commit the submitted final plan; the facade authenticates its author.

        Optional legacy calm input is checked for secrets, never interpreted as
        completed reflection. An explicit false confirmation is never overridden.
        """
        return self._submit_direct(
            owner_id=owner_id, model_id=model_id, wake_id=wake_id, wake_seq=wake_seq,
            expected_row_version=expected_row_version, intent="create", content=content,
            reason=reason, idempotency_key=idempotency_key,
            calm_check=calm_check, ai_confirmation=ai_confirmation,
        )

    def revise_direct(
        self, *, owner_id: str, model_id: str, wake_id: str, wake_seq: int,
        expected_row_version: int, plan_id: str, expected_plan_version: int,
        intent: str, reason: str, idempotency_key: str,
        changes: Mapping[str, Any] | None = None, rollback_to_version: int | None = None,
        expected_event_seq: int | None = None,
        calm_check: Mapping[str, Any] | None = None, ai_confirmation: bool | None = None,
    ) -> dict[str, Any]:
        """Commit one exact-version edit/state transition without creating a candidate.

        The observed module CAS remains required even for old clients which do
        not supply the additional event-sequence coordinate. No version is
        refreshed and no pending candidate is silently accepted or rewritten.
        """
        return self._submit_direct(
            owner_id=owner_id, model_id=model_id, wake_id=wake_id, wake_seq=wake_seq,
            expected_row_version=expected_row_version, plan_id=plan_id,
            expected_plan_version=expected_plan_version, expected_event_seq=expected_event_seq,
            intent=intent, changes=changes, rollback_to_version=rollback_to_version,
            reason=reason, idempotency_key=idempotency_key,
            calm_check=calm_check, ai_confirmation=ai_confirmation,
        )

    def _submit_direct(
        self, *, owner_id: str, model_id: str, wake_id: str, wake_seq: int,
        expected_row_version: int, intent: str, reason: str, idempotency_key: str,
        content: Mapping[str, Any] | None = None, plan_id: str | None = None,
        expected_plan_version: int = 0, expected_event_seq: int | None = None,
        changes: Mapping[str, Any] | None = None, rollback_to_version: int | None = None,
        calm_check: Mapping[str, Any] | None = None, ai_confirmation: bool | None = None,
    ) -> dict[str, Any]:
        from .execution_binding import assert_bound_execution, expected_execution_wake

        owner_id, model_id = _text("owner_id", owner_id, 200), _text("model_id", model_id, 200)
        wake_id = _text("wake_id", wake_id, 300)
        if type(wake_seq) is not int or wake_seq < 0:
            raise PlanningMemoryError("invalid_wake_seq")
        if type(expected_row_version) is not int or expected_row_version < 0:
            raise PlanningMemoryError("invalid_expected_planning_version")
        if ai_confirmation is not None and ai_confirmation is not True:
            raise PlanningMemoryError("ai_confirmation_required")
        if calm_check is not None and not isinstance(calm_check, Mapping):
            raise PlanningMemoryError("invalid_legacy_calm_check")
        if expected_event_seq is not None and (type(expected_event_seq) is not int or expected_event_seq < 0):
            raise PlanningMemoryError("invalid_expected_event_seq")
        intent = _enum("planning_intent", intent, CANDIDATE_INTENTS)
        reason, idempotency_key = _text("reason", reason, 2000), _text("idempotency_key", idempotency_key, 200)
        if _contains_secret(reason, calm_check, changes):
            raise PlanningMemoryError("credential_content_rejected")
        if intent == "create":
            proposed = self._normalize_content(content)
        else:
            plan_id = _text("plan_id", plan_id, 80)
            if not _PLAN_ID.fullmatch(plan_id):
                raise PlanningMemoryError("invalid_plan_id")
            if type(expected_plan_version) is not int or expected_plan_version < 1:
                raise PlanningMemoryError("plan_version_conflict")
            if intent == "revise":
                if not isinstance(changes, Mapping) or not changes:
                    raise PlanningMemoryError("changes_required")
                if not set(changes).issubset(_CONTENT_FIELDS) or rollback_to_version is not None:
                    raise PlanningMemoryError("invalid_plan_changes")
            elif changes is not None:
                raise PlanningMemoryError("invalid_plan_changes")
            if intent == "rollback":
                if type(rollback_to_version) is not int or rollback_to_version < 1 or rollback_to_version == expected_plan_version:
                    raise PlanningMemoryError("invalid_rollback_version")
            elif rollback_to_version is not None:
                raise PlanningMemoryError("invalid_rollback_version")
            proposed = None
        expected_execution_wake(owner_id=owner_id, model_id=model_id, explicit=wake_id)
        request_hash = _sha256({
            "wake_id": wake_id, "wake_seq": wake_seq, "expected_row_version": expected_row_version,
            "intent": intent, "content": proposed, "plan_id": plan_id,
            "expected_plan_version": expected_plan_version, "expected_event_seq": expected_event_seq,
            "changes": dict(changes) if changes is not None else None,
            "rollback_to_version": rollback_to_version, "reason": reason,
        })
        with self._connect() as connection:
            self._begin(connection)
            assert_bound_execution(connection)
            prior = self._idempotency_get(
                connection, owner_id=owner_id, model_id=model_id, action=f"direct_{intent}",
                idempotency_key=idempotency_key, request_hash=request_hash,
            )
            if prior is not None:
                return {**prior, "state_changed": False, "active_plan_changed": False, "idempotent_replay": True}
            if intent == "create":
                plan_id = _new_id("plan")
                now = _iso()
                connection.execute(
                    "INSERT INTO planning_module_state "
                    "(owner_id, model_id, module_version, status, row_version, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'active', 0, ?, ?) ON CONFLICT(owner_id, model_id) DO NOTHING",
                    (owner_id, model_id, PLANNING_VERSION, now, now),
                )
            else:
                item = self._item(connection, owner_id, model_id, plan_id)
                if int(item["current_version"]) != expected_plan_version:
                    raise PlanningMemoryError("plan_version_conflict")
                if item["recall_lifecycle"] != "active":
                    raise PlanningMemoryError("plan_quarantined")
                events = self._event_rows(connection, owner_id, model_id, plan_id)
                if expected_event_seq is not None and expected_event_seq != max((int(event["event_seq"]) for event in events), default=0):
                    raise PlanningMemoryError("plan_event_seq_conflict")
                current_state = fold_plan_events(events)["state"]
                allowed = {"revise": {"active", "paused"}, "rollback": {"active", "paused"},
                           "abandon": {"active", "paused"}, "archive": {"paused", "completed", "abandoned"},
                           "revive": {"abandoned"}}
                if current_state not in allowed[intent]:
                    raise PlanningMemoryError("invalid_plan_state_transition")
                current = self._current_content(connection, item)
                proposed = current
                if intent == "revise":
                    proposed = self._normalize_stored_content({**current, **dict(changes)})
                elif intent == "rollback":
                    proposed = self._normalize_stored_content(_json(self._version(connection, plan_id, rollback_to_version)["content_json"], {}))
                if intent in {"revise", "rollback"} and not _diff(current, proposed):
                    raise PlanningMemoryError("no_effective_change")
            if intent in {"create", "revise", "rollback", "revive"}:
                self._validate_graph(connection, owner_id=owner_id, model_id=model_id, source_plan_id=plan_id, content=proposed)
                self._validate_persistent_slot(connection, owner_id=owner_id, model_id=model_id, plan_id=plan_id, content=proposed)
            row_version = self._advance_state(connection, owner_id=owner_id, model_id=model_id, expected_row_version=expected_row_version)
            result = self._apply_submitted_change(
                connection, owner_id=owner_id, model_id=model_id, wake_id=wake_id, wake_seq=wake_seq,
                plan_id=plan_id, base_version=expected_plan_version, intent=intent, proposed=proposed, reason=reason,
            )
            response = {
                **result, "decision": "stored" if intent == "create" else "revised", "intent": intent,
                "planning_row_version": row_version, "state_changed": True, "active_plan_changed": True,
                "candidate_created": False, "review_performed": False, "review_requires_later_wake": False,
                "idempotent_replay": False, "reason_codes": ["explicit_plan_submission_applied"],
            }
            self._audit(
                connection, owner_id=owner_id, model_id=model_id, action=f"direct_{intent}",
                decision=response["decision"], wake_id=wake_id, plan_id=plan_id,
                reason_codes=["explicit_plan_submission_applied"],
                details={"base_version": expected_plan_version, "content_hash": _sha256(proposed), "review_performed": False},
            )
            self._idempotency_put(connection, owner_id=owner_id, model_id=model_id, action=f"direct_{intent}",
                                  idempotency_key=idempotency_key, request_hash=request_hash, response=response)
            return response

    def propose_create(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        content: Mapping[str, Any],
        reason: str,
        calm_check: Mapping[str, Any],
        ai_confirmation: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Legacy explicit staging primitive, not the public remember route."""
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        wake_id = _text("wake_id", wake_id, 300)
        if isinstance(wake_seq, bool) or not isinstance(wake_seq, int) or wake_seq < 0:
            raise PlanningMemoryError("invalid_wake_seq")
        if ai_confirmation is not True:
            raise PlanningMemoryError("ai_confirmation_required")
        normalized = self._normalize_content(content)
        reason = _text("reason", reason, 2000)
        calm = _validate_calm_check(calm_check)
        idempotency_key = _text("idempotency_key", idempotency_key, 200)
        if _contains_secret(reason, calm):
            raise PlanningMemoryError("credential_content_rejected")
        plan_id = _new_id("plan")
        request = {
            "wake_id": wake_id,
            "wake_seq": wake_seq,
            "expected_row_version": expected_row_version,
            "content": normalized,
            "reason": reason,
            "calm_check": calm,
            "ai_confirmation": True,
        }
        request_hash = _sha256(request)
        with self._connect() as connection:
            self._begin(connection)
            prior = self._idempotency_get(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                action="propose_create",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if prior is not None:
                return prior
            self._validate_graph(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                source_plan_id=plan_id,
                content=normalized,
            )
            self._validate_persistent_slot(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                plan_id=plan_id,
                content=normalized,
            )
            response = self._create_candidate(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                wake_id=wake_id,
                wake_seq=wake_seq,
                expected_row_version=expected_row_version,
                plan_id=plan_id,
                intent="create",
                base_version=0,
                proposed_content=normalized,
                before=None,
                reason=reason,
                calm_check=calm,
            )
            self._idempotency_put(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                action="propose_create",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def propose_revision(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        plan_id: str,
        expected_plan_version: int,
        intent: str,
        reason: str,
        calm_check: Mapping[str, Any],
        ai_confirmation: bool,
        idempotency_key: str,
        changes: Mapping[str, Any] | None = None,
        rollback_to_version: int | None = None,
    ) -> dict[str, Any]:
        """Legacy explicit staging primitive; public revisions use revise_direct."""
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        wake_id = _text("wake_id", wake_id, 300)
        if isinstance(wake_seq, bool) or not isinstance(wake_seq, int) or wake_seq < 0:
            raise PlanningMemoryError("invalid_wake_seq")
        plan_id = _text("plan_id", plan_id, 80)
        if not _PLAN_ID.fullmatch(plan_id):
            raise PlanningMemoryError("invalid_plan_id")
        intent = _enum("planning_intent", intent, CANDIDATE_INTENTS - {"create"})
        reason = _text("reason", reason, 2000)
        calm = _validate_calm_check(calm_check)
        idempotency_key = _text("idempotency_key", idempotency_key, 200)
        if ai_confirmation is not True:
            raise PlanningMemoryError("ai_confirmation_required")
        if _contains_secret(reason, calm, changes):
            raise PlanningMemoryError("credential_content_rejected")
        request_hash = _sha256(
            {
                "wake_id": wake_id,
                "wake_seq": wake_seq,
                "expected_row_version": expected_row_version,
                "plan_id": plan_id,
                "expected_plan_version": expected_plan_version,
                "intent": intent,
                "reason": reason,
                "calm_check": calm,
                "changes": changes,
                "rollback_to_version": rollback_to_version,
            }
        )
        with self._connect() as connection:
            self._begin(connection)
            prior = self._idempotency_get(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                action="propose_revision",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if prior is not None:
                return prior
            item = self._item(connection, owner_id, model_id, plan_id)
            if int(item["current_version"]) != expected_plan_version:
                raise PlanningMemoryError("plan_version_conflict")
            current = self._current_content(connection, item)
            projection = self._projection(connection, owner_id, model_id, plan_id)
            state = projection["state"]
            proposed = current
            if intent == "revise":
                if state not in {"active", "paused"}:
                    raise PlanningMemoryError("invalid_plan_state_transition")
                if not isinstance(changes, Mapping) or not changes:
                    raise PlanningMemoryError("changes_required")
                if not set(changes).issubset(_CONTENT_FIELDS):
                    raise PlanningMemoryError("invalid_plan_changes")
                proposed = self._normalize_stored_content({**current, **dict(changes)})
            elif intent == "rollback":
                if (
                    isinstance(rollback_to_version, bool)
                    or not isinstance(rollback_to_version, int)
                    or rollback_to_version < 1
                    or rollback_to_version == expected_plan_version
                ):
                    raise PlanningMemoryError("invalid_rollback_version")
                old = self._version(connection, plan_id, rollback_to_version)
                proposed = self._normalize_stored_content(_json(old["content_json"], {}))
            elif intent == "abandon":
                if state not in {"active", "paused"}:
                    raise PlanningMemoryError("invalid_plan_state_transition")
            elif intent == "archive":
                if state not in {"paused", "completed", "abandoned"}:
                    raise PlanningMemoryError("invalid_plan_state_transition")
            elif intent == "revive":
                if state != "abandoned":
                    raise PlanningMemoryError("invalid_plan_state_transition")
            if intent in {"revise", "rollback"}:
                self._validate_graph(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    source_plan_id=plan_id,
                    content=proposed,
                )
                self._validate_persistent_slot(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    plan_id=plan_id,
                    content=proposed,
                )
            response = self._create_candidate(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                wake_id=wake_id,
                wake_seq=wake_seq,
                expected_row_version=expected_row_version,
                plan_id=plan_id,
                intent=intent,
                base_version=expected_plan_version,
                proposed_content=proposed,
                before=current,
                reason=reason,
                calm_check=calm,
            )
            self._idempotency_put(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                action="propose_revision",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def present_pending_candidates(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        limit: int = PLANNING_PENDING_BATCH_LIMIT,
    ) -> list[dict[str, Any]]:
        """Present a bounded, wake-stable batch without starving older candidates.

        Keep this wake's batch pinned and rotate unshown legacy candidates on
        later reads in another wake. Presentation is a UI convenience, not an
        authorization token or a requirement to wait for another wake.
        """
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        wake_id = _text("wake_id", wake_id, 300)
        if isinstance(wake_seq, bool) or not isinstance(wake_seq, int) or wake_seq < 0:
            raise PlanningMemoryError("invalid_wake_seq")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise PlanningMemoryError("invalid_limit")
        with self._connect() as connection:
            self._begin(connection)
            rows = connection.execute(
                "SELECT * FROM planning_change_candidates WHERE owner_id = ? AND model_id = ? "
                "AND status = 'pending' ORDER BY "
                "CASE WHEN presented_wake_id = ? AND presented_wake_seq = ? THEN 0 ELSE 1 END, "
                "CASE WHEN submitted_wake_seq < ? THEN 0 ELSE 1 END, "
                "COALESCE(presented_wake_seq, -1), created_at, candidate_id LIMIT ?",
                (owner_id, model_id, wake_id, wake_seq, wake_seq, limit),
            ).fetchall()
            # Updating presentation stamps must not reorder a repeated response
            # in the same wake (including its current_action_contract paths).
            rows = sorted(rows, key=lambda row: (row["created_at"], row["candidate_id"]))
            result: list[dict[str, Any]] = []
            for row in rows:
                projection: dict[str, Any] = {
                    "candidate_id": row["candidate_id"],
                    "candidate_hash": row["candidate_hash"],
                    "candidate_version": row["candidate_version"],
                    "plan_id": row["plan_id"],
                    "intent": row["intent"],
                    "base_version": row["base_version"],
                    "status": row["status"],
                    "review_requires_later_wake": False,
                    "fully_presented": True,
                    "proposed_content_hash": row["proposed_content_hash"],
                }
                if (
                    row["presented_wake_id"] != wake_id
                    or row["presented_wake_seq"] != wake_seq
                ):
                    connection.execute(
                        "UPDATE planning_change_candidates SET presented_wake_id = ?, "
                        "presented_wake_seq = ?, updated_at = ? WHERE candidate_id = ?",
                        (wake_id, wake_seq, _iso(), row["candidate_id"]),
                    )
                projection.update(
                    {
                        "proposed_content": _json(row["proposed_content_json"], {}),
                        "canonical_diff": _json(row["diff_json"], {}),
                        "reason": row["reason"],
                        "calm_check": _json(row["calm_check_json"], {}),
                        "presentation_bound_to_current_wake": True,
                        "presentation_required_for_review": False,
                    }
                )
                result.append(projection)
            return result

    def _current_children(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        plan_id: str,
    ) -> list[str]:
        rows = connection.execute(
            "SELECT source_plan_id FROM planning_edges WHERE owner_id = ? AND model_id = ? "
            "AND target_plan_id = ? AND edge_type = 'parent' AND active = 1",
            (owner_id, model_id, plan_id),
        ).fetchall()
        return [row["source_plan_id"] for row in rows]

    def _active_children_remaining(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        plan_id: str,
    ) -> bool:
        for child in self._current_children(
            connection,
            owner_id=owner_id,
            model_id=model_id,
            plan_id=plan_id,
        ):
            state = self._projection(connection, owner_id, model_id, child)["state"]
            if state in {"active", "paused"}:
                return True
        return False

    def record_ordinary_event(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        plan_id: str,
        expected_plan_version: int,
        expected_event_seq: int,
        event_type: str,
        reason: str,
        evidence: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Append a caller-evidenced event against the state the caller has read.

        Content version and event sequence are independent CAS coordinates. The
        service authenticates execution and passes the explicit observed sequence;
        this method never refreshes it, discovers a latest wake, manufactures
        evidence, or treats the absence of evidence as a completion assertion.
        """
        from .execution_binding import assert_bound_execution, expected_execution_wake

        owner_id = _text("owner_id", owner_id, 200)
        model_id = _text("model_id", model_id, 200)
        wake_id = _text("wake_id", wake_id, 300)
        if type(wake_seq) is not int or wake_seq < 0:
            raise PlanningMemoryError("invalid_wake_seq")
        if type(expected_row_version) is not int or expected_row_version < 0:
            raise PlanningMemoryError("invalid_expected_planning_version")
        if type(expected_plan_version) is not int or expected_plan_version < 1:
            raise PlanningMemoryError("plan_version_conflict")
        if type(expected_event_seq) is not int or expected_event_seq < 0:
            raise PlanningMemoryError("invalid_expected_event_seq")
        plan_id = _text("plan_id", plan_id, 80)
        if not _PLAN_ID.fullmatch(plan_id):
            raise PlanningMemoryError("invalid_plan_id")
        event_type = _enum(
            "planning_event_type", event_type,
            frozenset({"progress", "complete", "pause", "resume", "reopen"}),
        )
        reason = _text("reason", reason, 2000)
        if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes, bytearray)):
            raise PlanningMemoryError("invalid_evidence")
        validated_evidence = _validate_evidence(
            list(evidence), required=event_type in {"progress", "complete", "reopen"}
        )
        if _contains_secret(reason):
            raise PlanningMemoryError("credential_content_rejected")
        expected_execution_wake(owner_id=owner_id, model_id=model_id, explicit=wake_id)

        with self._connect() as connection:
            self._begin(connection)
            assert_bound_execution(connection)
            item = self._item(connection, owner_id, model_id, plan_id)
            if int(item["current_version"]) != expected_plan_version:
                raise PlanningMemoryError("plan_version_conflict")
            if item["recall_lifecycle"] != "active":
                raise PlanningMemoryError("plan_quarantined")
            events = self._event_rows(connection, owner_id, model_id, plan_id)
            event_seq = max((int(event["event_seq"]) for event in events), default=0)
            if event_seq != expected_event_seq:
                raise PlanningMemoryError("plan_event_seq_conflict")
            current = fold_plan_events(events)["state"]
            valid = {
                "progress": {"active", "paused"},
                "complete": {"active", "paused"},
                "pause": {"active"},
                "resume": {"paused"},
                "reopen": {"completed"},
            }
            if current not in valid[event_type]:
                raise PlanningMemoryError("invalid_plan_state_transition")
            if event_type == "complete":
                content = self._current_content(connection, item)
                if content["kind"] == "vision":
                    raise PlanningMemoryError("vision_cannot_complete")
                if self._active_children_remaining(
                    connection, owner_id=owner_id, model_id=model_id, plan_id=plan_id,
                ):
                    raise PlanningMemoryError("active_children_remaining")
            row_version = self._advance_state(
                connection, owner_id=owner_id, model_id=model_id,
                expected_row_version=expected_row_version,
            )
            event = self._append_event(
                connection, owner_id=owner_id, model_id=model_id, plan_id=plan_id,
                event_type=event_type, reason=reason, evidence=validated_evidence,
                wake_id=wake_id, wake_seq=wake_seq,
            )
            new_event_seq = int(connection.execute(
                "SELECT event_seq FROM planning_events WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()[0])
            projection = self._projection(connection, owner_id, model_id, plan_id)
            self._audit(
                connection, owner_id=owner_id, model_id=model_id,
                action="record_ordinary_event", decision="event_recorded", wake_id=wake_id,
                plan_id=plan_id, reason_codes=["append_only_event", "observed_event_seq_checked"],
                details={"event_id": event["event_id"], "event_type": event_type,
                         "previous_event_seq": expected_event_seq, "event_seq": new_event_seq},
            )
            return {
                "decision": "event_recorded", "id": plan_id,
                "ref": _plan_ref(plan_id, expected_plan_version),
                "version": expected_plan_version, "event_id": event["event_id"],
                "event_seq": new_event_seq, "previous_event_seq": expected_event_seq,
                "state": projection["state"],
                "planning_row_version": row_version, "state_changed": True,
            }

    def record_event(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        plan_id: str,
        expected_plan_version: int,
        event_type: str,
        reason: str,
        evidence: Sequence[Mapping[str, Any]],
        ai_confirmation: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        wake_id = _text("wake_id", wake_id, 300)
        if isinstance(wake_seq, bool) or not isinstance(wake_seq, int) or wake_seq < 0:
            raise PlanningMemoryError("invalid_wake_seq")
        event_type = _enum("planning_event_type", event_type, DIRECT_EVENT_TYPES)
        plan_id = _text("plan_id", plan_id, 80)
        if not _PLAN_ID.fullmatch(plan_id):
            raise PlanningMemoryError("invalid_plan_id")
        if ai_confirmation is not True:
            raise PlanningMemoryError("ai_confirmation_required")
        reason = _text("reason", reason, 2000)
        validated_evidence = _validate_evidence(
            list(evidence), required=event_type in {"progress", "complete", "reopen"}
        )
        if _contains_secret(reason):
            raise PlanningMemoryError("credential_content_rejected")
        idempotency_key = _text("idempotency_key", idempotency_key, 200)
        request_hash = _sha256(
            {
                "wake_id": wake_id,
                "wake_seq": wake_seq,
                "expected_row_version": expected_row_version,
                "plan_id": plan_id,
                "expected_plan_version": expected_plan_version,
                "event_type": event_type,
                "reason": reason,
                "evidence": validated_evidence,
            }
        )
        with self._connect() as connection:
            self._begin(connection)
            prior = self._idempotency_get(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                action="record_event",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if prior is not None:
                return prior
            item = self._item(connection, owner_id, model_id, plan_id)
            if int(item["current_version"]) != expected_plan_version:
                raise PlanningMemoryError("plan_version_conflict")
            if item["recall_lifecycle"] != "active":
                raise PlanningMemoryError("plan_quarantined")
            content = self._current_content(connection, item)
            current = self._projection(connection, owner_id, model_id, plan_id)["state"]
            valid = {
                "progress": {"active", "paused"},
                "complete": {"active", "paused"},
                "pause": {"active"},
                "resume": {"paused"},
                "reopen": {"completed"},
                "defer_review": {"active", "paused"},
            }
            if current not in valid[event_type]:
                raise PlanningMemoryError("invalid_plan_state_transition")
            if event_type == "complete":
                if content["kind"] == "vision":
                    raise PlanningMemoryError("vision_cannot_complete")
                if self._active_children_remaining(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    plan_id=plan_id,
                ):
                    raise PlanningMemoryError("active_children_remaining")
            event = self._append_event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                plan_id=plan_id,
                event_type=event_type,
                reason=reason,
                evidence=validated_evidence,
                wake_id=wake_id,
                wake_seq=wake_seq,
            )
            row_version = self._advance_state(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                expected_row_version=expected_row_version,
            )
            projection = self._projection(connection, owner_id, model_id, plan_id)
            response = {
                "decision": "event_recorded",
                "event": event,
                "plan_ref": _plan_ref(plan_id, expected_plan_version),
                "plan_state": projection,
                "planning_row_version": row_version,
                "state_changed": True,
                "active_plan_changed": True,
            }
            self._audit(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                action="record_planning_event",
                decision="event_recorded",
                wake_id=wake_id,
                plan_id=plan_id,
                reason_codes=["append_only_event"],
                details={"event_id": event["event_id"], "event_type": event_type},
            )
            self._idempotency_put(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                action="record_event",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def review_change(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        candidate_id: str,
        expected_candidate_version: int,
        expected_candidate_hash: str,
        expected_base_version: int,
        decision: str,
        correctness_assessment: str | None = None,
        calm_check: Mapping[str, Any] | None = None,
        reason: str,
        ai_confirmation: bool,
        expected_event_seq: int | None = None,
    ) -> dict[str, Any]:
        """Explicitly resolve a legacy candidate, never auto-activate old pending.

        The exact candidate hash binds its complete content, diff and original
        author submission. Confirmation is current; calm text and presentation
        stamps are not treated as evidence of reflection or authority.
        """
        from .execution_binding import assert_bound_execution, expected_execution_wake

        owner_id, model_id = _text("owner_id", owner_id, 200), _text("model_id", model_id, 200)
        wake_id = _text("wake_id", wake_id, 300)
        if isinstance(wake_seq, bool) or not isinstance(wake_seq, int) or wake_seq < 0:
            raise PlanningMemoryError("invalid_wake_seq")
        for value, minimum, code in (
            (expected_row_version, 0, "invalid_expected_planning_version"),
            (expected_candidate_version, 1, "candidate_version_mismatch"),
            (expected_base_version, 0, "candidate_base_mismatch"),
        ):
            if type(value) is not int or value < minimum:
                raise PlanningMemoryError(code)
        decision = _enum("planning_candidate_decision", decision, CANDIDATE_DECISIONS)
        correctness = None if correctness_assessment is None else _text("correctness_assessment", correctness_assessment, 2000)
        review_reason = _text("reason", reason, 2000)
        if calm_check is not None and not isinstance(calm_check, Mapping):
            raise PlanningMemoryError("invalid_legacy_calm_check")
        if ai_confirmation is not True:
            raise PlanningMemoryError("ai_confirmation_required")
        if expected_event_seq is not None and (type(expected_event_seq) is not int or expected_event_seq < 0):
            raise PlanningMemoryError("invalid_expected_event_seq")
        if not isinstance(expected_candidate_hash, str) or not _CANDIDATE_HASH.fullmatch(
            expected_candidate_hash
        ):
            raise PlanningMemoryError("candidate_hash_mismatch")
        if _contains_secret(correctness, review_reason, calm_check):
            raise PlanningMemoryError("credential_content_rejected")
        expected_execution_wake(owner_id=owner_id, model_id=model_id, explicit=wake_id)
        with self._connect() as connection:
            self._begin(connection)
            assert_bound_execution(connection)
            row = connection.execute(
                "SELECT * FROM planning_change_candidates WHERE owner_id = ? AND model_id = ? "
                "AND candidate_id = ?",
                (owner_id, model_id, candidate_id),
            ).fetchone()
            if row is None:
                raise PlanningMemoryError("candidate_not_found")
            if row["status"] != "pending":
                raise PlanningMemoryError("candidate_not_pending")
            if int(row["candidate_version"]) != expected_candidate_version:
                raise PlanningMemoryError("candidate_version_mismatch")
            if row["candidate_hash"] != expected_candidate_hash:
                raise PlanningMemoryError("candidate_hash_mismatch")
            if int(row["base_version"]) != expected_base_version:
                raise PlanningMemoryError("candidate_base_mismatch")
            proposed_raw = _json(row["proposed_content_json"], {})
            material = {
                "candidate_id": row["candidate_id"], "candidate_version": row["candidate_version"],
                "plan_id": row["plan_id"], "intent": row["intent"], "base_version": row["base_version"],
                "proposed_content": proposed_raw, "canonical_diff": _json(row["diff_json"], {}),
                "reason": row["reason"], "calm_check": _json(row["calm_check_json"], {}),
                "submitted_wake_id": row["submitted_wake_id"], "submitted_wake_seq": row["submitted_wake_seq"],
            }
            if _sha256(proposed_raw) != row["proposed_content_hash"] or _sha256(material) != expected_candidate_hash:
                raise PlanningMemoryError("candidate_hash_mismatch")

            if decision == "reject":
                connection.execute(
                    "UPDATE planning_change_candidates SET status = 'rejected', reviewed_wake_id = ?, "
                    "reviewed_wake_seq = ?, updated_at = ? WHERE candidate_id = ?",
                    (wake_id, wake_seq, _iso(), candidate_id),
                )
                row_version = self._advance_state(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    expected_row_version=expected_row_version,
                )
                self._audit(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    action="review_planning_change",
                    decision="candidate_rejected",
                    wake_id=wake_id,
                    plan_id=row["plan_id"],
                    candidate_id=candidate_id,
                    reason_codes=["ai_rejected_candidate"],
                    details={"correctness_hash": _sha256(correctness) if correctness is not None else None},
                )
                return {
                    "decision": "candidate_rejected",
                    "candidate_id": candidate_id,
                    "candidate_lifecycle": "rejected",
                    "planning_row_version": row_version,
                    "state_changed": True,
                    "active_plan_changed": False,
                }

            intent = row["intent"]
            plan_id = row["plan_id"]
            proposed = self._normalize_stored_content(_json(row["proposed_content_json"], {}))
            base_version = int(row["base_version"])
            item: sqlite3.Row | None = None
            if intent == "create":
                exists = connection.execute(
                    "SELECT 1 FROM planning_items WHERE plan_id = ?", (plan_id,)
                ).fetchone()
                if exists is not None or base_version != 0:
                    raise PlanningMemoryError("candidate_base_changed")
            else:
                item = self._item(connection, owner_id, model_id, plan_id)
                if int(item["current_version"]) != base_version:
                    raise PlanningMemoryError("candidate_base_changed")
                if item["recall_lifecycle"] != "active":
                    raise PlanningMemoryError("plan_quarantined")
                events = self._event_rows(connection, owner_id, model_id, plan_id)
                if expected_event_seq is not None and expected_event_seq != max((int(event["event_seq"]) for event in events), default=0):
                    raise PlanningMemoryError("plan_event_seq_conflict")
                if intent in {"revise", "rollback"} and fold_plan_events(events)["state"] not in {"active", "paused"}:
                    raise PlanningMemoryError("invalid_plan_state_transition")
            if intent in {"create", "revise", "rollback", "revive"}:
                self._validate_graph(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    source_plan_id=plan_id,
                    content=proposed,
                )
                self._validate_persistent_slot(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    plan_id=plan_id,
                    content=proposed,
                )

            active_changed = True
            rollback_ref: str | None = None
            if intent == "create":
                now = _iso()
                connection.execute(
                    "INSERT INTO planning_items "
                    "(plan_id, owner_id, model_id, kind, track, current_version, recall_lifecycle, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, 'active', ?, ?)",
                    (plan_id, owner_id, model_id, proposed["kind"], proposed["track"], now, now),
                )
                self._insert_version(
                    connection,
                    plan_id=plan_id,
                    version=1,
                    previous_version=None,
                    content=proposed,
                    reason=row["reason"],
                    wake_id=wake_id,
                )
                self._replace_edges(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    source_plan_id=plan_id,
                    source_version=1,
                    content=proposed,
                )
                self._append_event(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    plan_id=plan_id,
                    event_type="created",
                    reason=row["reason"],
                    evidence=[],
                    wake_id=wake_id,
                    wake_seq=wake_seq,
                )
                current_version = 1
            elif intent in {"revise", "rollback"}:
                assert item is not None
                current_version = base_version + 1
                self._insert_version(
                    connection,
                    plan_id=plan_id,
                    version=current_version,
                    previous_version=base_version,
                    content=proposed,
                    reason=row["reason"],
                    wake_id=wake_id,
                )
                connection.execute(
                    "UPDATE planning_items SET kind = ?, track = ?, current_version = ?, updated_at = ? "
                    "WHERE plan_id = ?",
                    (
                        proposed["kind"],
                        proposed["track"],
                        current_version,
                        _iso(),
                        plan_id,
                    ),
                )
                self._replace_edges(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    source_plan_id=plan_id,
                    source_version=current_version,
                    content=proposed,
                )
                self._append_event(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    plan_id=plan_id,
                    event_type="content_rollback" if intent == "rollback" else "revised",
                    reason=row["reason"],
                    evidence=[],
                    wake_id=wake_id,
                    wake_seq=wake_seq,
                )
                rollback_ref = _plan_ref(plan_id, base_version)
            else:
                assert item is not None
                current_version = base_version
                current_state = self._projection(
                    connection, owner_id, model_id, plan_id
                )["state"]
                allowed = {
                    "abandon": {"active", "paused"},
                    "archive": {"paused", "completed", "abandoned"},
                    "revive": {"abandoned"},
                }
                if current_state not in allowed[intent]:
                    raise PlanningMemoryError("candidate_base_changed")
                self._append_event(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    plan_id=plan_id,
                    event_type={
                        "abandon": "abandon",
                        "archive": "archive",
                        "revive": "revive",
                    }[intent],
                    reason=row["reason"],
                    evidence=[],
                    wake_id=wake_id,
                    wake_seq=wake_seq,
                )

            connection.execute(
                "UPDATE planning_change_candidates SET status = 'accepted', reviewed_wake_id = ?, "
                "reviewed_wake_seq = ?, updated_at = ? WHERE candidate_id = ?",
                (wake_id, wake_seq, _iso(), candidate_id),
            )
            row_version = self._advance_state(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                expected_row_version=expected_row_version,
            )
            projection = self._projection(connection, owner_id, model_id, plan_id)
            self._audit(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                action="review_planning_change",
                decision="candidate_accepted",
                wake_id=wake_id,
                plan_id=plan_id,
                candidate_id=candidate_id,
                reason_codes=["legacy_candidate_explicitly_accepted"],
                details={
                    "correctness_hash": _sha256(correctness) if correctness is not None else None,
                    "review_reason_hash": _sha256(review_reason),
                    "intent": intent,
                    "confirmation_basis": "current_ai_confirmation_of_exact_candidate_hash",
                },
            )
            return {
                "decision": "candidate_accepted",
                "candidate_id": candidate_id,
                "candidate_lifecycle": "accepted",
                "plan_ref": _plan_ref(plan_id, current_version),
                "plan_state": projection,
                "rollback_ref": rollback_ref,
                "planning_row_version": row_version,
                "state_changed": True,
                "active_plan_changed": active_changed,
            }

    def _public_plan(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        item: sqlite3.Row,
        requested_version: int | None = None,
        include_history: bool = False,
    ) -> dict[str, Any]:
        version = requested_version or int(item["current_version"])
        version_row = self._version(connection, item["plan_id"], version)
        content = _json(version_row["content_json"], {})
        # Use the same ledger read for both the state and its CAS coordinate.
        # A second latest-sequence query could pair old state with a newer event.
        events = self._event_rows(connection, owner_id, model_id, item["plan_id"])
        projection = fold_plan_events(events)
        result: dict[str, Any] = {
            "plan_id": item["plan_id"],
            "plan_ref": _plan_ref(item["plan_id"], version),
            "current_plan_ref": _plan_ref(item["plan_id"], int(item["current_version"])),
            "current_version": item["current_version"],
            "requested_version": version,
            "recall_lifecycle": item["recall_lifecycle"],
            "content": content,
            "state_projection": projection,
            "event_seq": max((int(event["event_seq"]) for event in events), default=0),
            "content_hash": version_row["content_hash"],
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
        }
        if include_history:
            result["versions"] = [
                {
                    "version": row["version"],
                    "previous_version": row["previous_version"],
                    "content_hash": row["content_hash"],
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                }
                for row in connection.execute(
                    "SELECT * FROM planning_versions WHERE plan_id = ? ORDER BY version",
                    (item["plan_id"],),
                ).fetchall()
            ]
            result["events"] = [
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "reason": row["reason"],
                    "evidence": _json(row["evidence_json"], []),
                    "wake_seq": row["wake_seq"],
                    "created_at": row["created_at"],
                }
                for row in events
            ]
        return result

    @staticmethod
    def _semantic_score(content: Mapping[str, Any], query: str) -> float:
        q = _normalized(query)
        if not q:
            return 0.0
        candidates = [
            str(content.get("title", "")),
            str(content.get("summary", "")),
            *[str(item) for item in content.get("scene_tags", [])],
            *[str(item) for item in content.get("keywords", [])],
        ]
        best = 0.0
        for raw in candidates:
            candidate = _normalized(raw)
            if not candidate:
                continue
            if candidate in q or q in candidate:
                ratio = min(len(q), len(candidate)) / max(len(q), len(candidate))
                best = max(best, 0.72 + min(0.25, ratio * 0.25))
            else:
                best = max(best, SequenceMatcher(None, q, candidate).ratio() * 0.68)
        return min(1.0, best)

    @staticmethod
    def _temporal_score(content: Mapping[str, Any], now: datetime) -> float:
        due_at = content.get("due_at")
        if not isinstance(due_at, str):
            return 0.0
        due = _parse_iso(due_at)
        seconds = (due - now).total_seconds()
        if seconds < 0:
            return 1.0
        days = seconds / 86400
        if days <= 3:
            return 0.95
        if days <= 7:
            return 0.75
        if days <= 30:
            return 0.35
        return 0.05

    def _dependencies_satisfied(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        content: Mapping[str, Any],
    ) -> bool:
        for ref in content.get("dependency_refs", []):
            dependency_id, _version = _parse_plan_ref(ref)
            try:
                state = self._projection(connection, owner_id, model_id, dependency_id)["state"]
            except PlanningMemoryError:
                return False
            if state != "completed":
                return False
        return True

    def next_action(
        self,
        *,
        owner_id: str,
        model_id: str,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        if connection is None:
            self.ensure_state(owner_id=owner_id, model_id=model_id)
        scope = nullcontext(connection) if connection is not None else self._connect()
        with scope as active_connection:
            assert active_connection is not None
            choices: list[tuple[float, sqlite3.Row, dict[str, Any]]] = []
            now = _now_dt()
            rows = active_connection.execute(
                "SELECT * FROM planning_items WHERE owner_id = ? AND model_id = ? "
                "AND recall_lifecycle = 'active'",
                (owner_id, model_id),
            ).fetchall()
            for item in rows:
                projection = self._projection(
                    active_connection, owner_id, model_id, item["plan_id"]
                )
                if projection["state"] != "active":
                    continue
                content = self._current_content(active_connection, item)
                if content["kind"] not in {"task", "milestone", "commitment"}:
                    continue
                if self._current_children(
                    active_connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    plan_id=item["plan_id"],
                ):
                    continue
                if not self._dependencies_satisfied(
                    active_connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    content=content,
                ):
                    continue
                score = self._temporal_score(content, now) * 0.6 + content["importance"] / 250
                choices.append((score, item, content))
            if not choices:
                return None
            choices.sort(key=lambda item: (-item[0], item[1]["plan_id"]))
            _score, item, content = choices[0]
            return {
                "plan_ref": _plan_ref(item["plan_id"], int(item["current_version"])),
                "summary": content["summary"],
                "advisory_only": True,
                "instruction_authority": "none",
                "mutates_plan": False,
            }

    def recall(
        self,
        *,
        owner_id: str,
        model_id: str,
        query: str = "",
        plan_ref: str | None = None,
        limit: int = 10,
        include_terminal: bool = False,
        include_history: bool = False,
        include_quarantined: bool = False,
    ) -> dict[str, Any]:
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        has_query = isinstance(query, str) and bool(query.strip())
        has_ref = isinstance(plan_ref, str) and bool(plan_ref.strip())
        if has_query == has_ref:
            raise PlanningMemoryError("provide_exactly_one_query_or_plan_ref")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise PlanningMemoryError("invalid_limit")
        with self._connect() as connection:
            if has_ref:
                plan_id, version = _parse_plan_ref(plan_ref or "")
                item = self._item(connection, owner_id, model_id, plan_id)
                if item["recall_lifecycle"] == "quarantined" and not include_quarantined:
                    raise PlanningMemoryError("plan_quarantined")
                plans = [
                    self._public_plan(
                        connection,
                        owner_id=owner_id,
                        model_id=model_id,
                        item=item,
                        requested_version=version,
                        include_history=include_history,
                    )
                ]
            else:
                candidates: list[tuple[float, sqlite3.Row]] = []
                alias_query = prepare_explicit_alias_query(query)
                alias_matches: dict[str, dict[str, object]] = {}
                alias_candidates: list[tuple[float, sqlite3.Row]] = []
                rows = connection.execute(
                    "SELECT * FROM planning_items WHERE owner_id = ? AND model_id = ?",
                    (owner_id, model_id),
                ).fetchall()
                for item in rows:
                    if item["recall_lifecycle"] == "quarantined" and not include_quarantined:
                        continue
                    projection = self._projection(connection, owner_id, model_id, item["plan_id"])
                    if projection["state"] in {"completed", "abandoned", "archived"} and not include_terminal:
                        continue
                    content = self._current_content(connection, item)
                    score = self._semantic_score(content, query)
                    if score >= 0.15:
                        candidates.append((score, item))
                    else:
                        alias_match = explicit_alias_match(alias_query, [
                            content.get("title", ""), content.get("summary", ""),
                            *content.get("scene_tags", []), *content.get("keywords", []),
                        ])
                        if alias_match:
                            alias_matches[item["plan_id"]] = alias_match
                            alias_candidates.append((score, item))
                candidates.sort(key=lambda item: (-item[0], item[1]["plan_id"]))
                alias_candidates.sort(key=lambda pair: (
                    -float(alias_matches[pair[1]["plan_id"]]["score"]), -pair[0], pair[1]["plan_id"],
                ))
                candidates.extend(alias_candidates)
                plans = [
                    {
                        "semantic_score": round(score, 4),
                        **({"retrieval_evidence": alias_matches[item["plan_id"]],
                            "retrieval_match": "lexical_alias_candidate", "candidate_only": True,
                            "candidate_score": alias_matches[item["plan_id"]]["score"]}
                           if item["plan_id"] in alias_matches else {}),
                        **self._public_plan(
                            connection,
                            owner_id=owner_id,
                            model_id=model_id,
                            item=item,
                            include_history=include_history,
                        ),
                    }
                    for score, item in candidates[:limit]
                ]
            return {
                "decision": "recalled" if plans else "no_candidate",
                "plans": plans,
                "next_action": self.next_action(
                    owner_id=owner_id,
                    model_id=model_id,
                    connection=connection,
                ),
                "state_changed": False,
                "active_plan_changed": False,
                "status": self._status_in_connection(connection, owner_id, model_id),
            }

    def _status_in_connection(
        self, connection: sqlite3.Connection, owner_id: str, model_id: str
    ) -> dict[str, Any]:
        state = self._state(connection, owner_id, model_id)
        return {
            "status": state["status"],
            "row_version": state["row_version"],
            "module_version": state["module_version"],
        }

    @staticmethod
    def _due_hint(content: Mapping[str, Any], now: datetime) -> str | None:
        due_at = content.get("due_at")
        if not isinstance(due_at, str):
            return None
        due = _parse_iso(due_at)
        seconds = (due - now).total_seconds()
        if seconds < 0:
            return f"overdue_since:{due_at}"
        if seconds <= 3 * 86400:
            return f"due_soon:{due_at}"
        return None

    def build_injection(
        self,
        *,
        owner_id: str,
        model_id: str,
        query: str,
        session_start: bool = False,
        limit: int = 3,
        recent_nudge_refs: Sequence[str] = (),
        excluded_plan_ids: Sequence[str] = (),
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Build at most three summary envelopes without mutating planning state."""

        if connection is None:
            self.ensure_state(owner_id=owner_id, model_id=model_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 3:
            raise PlanningMemoryError("invalid_limit")
        excluded = {item for item in excluded_plan_ids if isinstance(item, str)}
        recent = {item for item in recent_nudge_refs if isinstance(item, str)}
        scope = nullcontext(connection) if connection is not None else self._connect()
        with scope as active_connection:
            assert active_connection is not None
            now = _now_dt()
            ranked: list[tuple[int, float, sqlite3.Row, dict[str, Any], dict[str, Any], float, float]] = []
            rows = active_connection.execute(
                "SELECT * FROM planning_items WHERE owner_id = ? AND model_id = ? "
                "AND recall_lifecycle = 'active'",
                (owner_id, model_id),
            ).fetchall()
            for item in rows:
                if item["plan_id"] in excluded:
                    continue
                projection = self._projection(
                    active_connection, owner_id, model_id, item["plan_id"]
                )
                if projection["state"] not in {"active", "paused"}:
                    continue
                content = self._current_content(active_connection, item)
                semantic = self._semantic_score(content, query)
                temporal = self._temporal_score(content, now)
                review_due = isinstance(content.get("review_after"), str) and _parse_iso(
                    content["review_after"]
                ) <= now
                persistent_candidate = (
                    projection["state"] == "active"
                    and content["track"] == "internal"
                    and content["presence_mode"] == "persistent"
                )
                session_candidate = (
                    session_start
                    and content["track"] == "internal"
                    and content["presence_mode"] == "session_start"
                )
                deadline_candidate = temporal >= 0.75 and (
                    content["kind"] == "commitment" or content["track"] == "relational"
                )
                if projection["state"] == "paused" and not (semantic >= 0.2 or review_due):
                    continue
                if not (
                    persistent_candidate or semantic >= 0.2 or session_candidate
                    or deadline_candidate or review_due
                ):
                    continue
                score = semantic * 0.55 + temporal * 0.25 + content["importance"] / 500
                # Persistent presence is an AI-selected, active internal reminder,
                # not a relevance claim or permission to execute.  Paused items
                # keep their existing relevance/review gate and are not reserved.
                band = 0 if persistent_candidate else 1 if session_candidate else 2
                ranked.append(
                    (band, score, item, content, projection, semantic, temporal)
                )
            ranked.sort(key=lambda entry: (entry[0], -entry[1], entry[2]["plan_id"]))
            selected: list[tuple[int, float, sqlite3.Row, dict[str, Any], dict[str, Any], float, float]] = []
            persistent_selected = 0
            session_top_used = False
            for entry in ranked:
                band, _score, _item, content, _projection, _semantic, _temporal = entry
                if content["presence_mode"] == "persistent":
                    if persistent_selected >= 2:
                        continue
                    persistent_selected += 1
                if band == 1:
                    if session_top_used:
                        continue
                    session_top_used = True
                selected.append(entry)
                if len(selected) >= limit:
                    break
            envelopes: list[dict[str, Any]] = []
            for band, score, item, content, projection, semantic, temporal in selected:
                ref = _plan_ref(item["plan_id"], int(item["current_version"]))
                envelopes.append(
                    {
                        "envelope_version": "planning-recall-envelope/0.1",
                        "module": PLANNING_MODULE,
                        "item_ref": ref,
                        "kind": content["kind"],
                        # Mechanical selection metadata lets the shared budget
                        # preserve the author's short reminder without inventing
                        # text or turning a paused plan into a persistent one.
                        "presence_mode": content["presence_mode"],
                        "reminder": content["reminder"],
                        "selection_priority": band,
                        "selection_score": score,
                        "semantic_score": round(semantic, 4),
                        "temporal_score": round(temporal, 4),
                        "importance_score": round(content["importance"] / 100, 4),
                        "state": projection["state"],
                        "presentation": "summary_only",
                        "content": {
                            "summary": content["summary"],
                            "due_hint": self._due_hint(content, now),
                            "progress_hint": projection["progress_summary"],
                        },
                        "detail_lookup": {
                            "available": True,
                            "tool": "recall_planning_memory",
                            "ref": ref,
                        },
                        "frame": {
                            "instruction_authority": "none",
                            "permission_authority": "none",
                            "optional": True,
                            "mutates_plan": False,
                        },
                    }
                )
            review_hint = None
            for _band, _score, item, content, _projection, _semantic, _temporal in selected:
                ref = _plan_ref(item["plan_id"], int(item["current_version"]))
                if (
                    isinstance(content.get("review_after"), str)
                    and _parse_iso(content["review_after"]) <= now
                    and ref not in recent
                ):
                    review_hint = {
                        "plan_ref": ref,
                        "text": "A review time has arrived; I may continue, adjust, pause, or do nothing.",
                        "optional": True,
                        "mutates_plan": False,
                    }
                    break
            coordination_hint = None
            if len(envelopes) >= 3 and sum(
                1 for _a, _b, _c, content, _d, _e, _f in selected if content["allow_coordination_hint"]
            ) >= 2:
                coordination_hint = {
                    "text": "Several related plans surfaced; I may connect their execution or leave them separate.",
                    "optional": True,
                    "mutates_plan": False,
                }
            injection = None
            if envelopes:
                injection = {
                    "contract": PLANNING_RECALL_CONTRACT,
                    "frame": {
                        "instruction_authority": "none",
                        "permission_authority": "none",
                        "optional": True,
                    },
                    "envelopes": envelopes,
                    "next_action": self.next_action(
                        owner_id=owner_id,
                        model_id=model_id,
                        connection=active_connection,
                    ),
                    "coordination_hint": coordination_hint,
                    "review_hint": review_hint,
                }
            return {
                "injection": injection,
                "envelopes": envelopes,
                "candidate_count": len(ranked),
                "truncated": len(ranked) > len(envelopes),
                "state_changed": False,
                "active_plan_changed": False,
            }
