"""AI-authored, optional, scope-specific self-governance profiles.

This reference runtime implements only record-integrity mechanics.  It does
not ship value prose, score an AI's values, or interpret a profile as an
external permission grant.  Every state-changing operation is append-only,
owner/model scoped, CAS-bound, and authored by the current AI.  Candidate
activation always requires a genuinely later wake; using the stricter rule for
all changes keeps the host out of the business of judging semantic magnitude.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Mapping, Sequence
import uuid


GOVERNANCE_CONTRACT_VERSION = "self-governance/0.3"
GOVERNANCE_SCOPES = (
    "global",
    "self_revision",
    "emotional_memory",
    "learning_memory",
    "tool_use",
)
GOVERNANCE_TRIGGER_MODES = ("manual_only", "scene_relevant")

# Reserved mechanism tags are authored into ``scene_tags`` by the AI, but they
# can never be activated by matching human text.  The host may present one of
# these bounded, content-free signals only after its corresponding runtime gate
# succeeds.  This lets an AI opt into a useful moment without letting either a
# developer-authored value statement or a user-spoofed tag become its policy.
LEARNING_EPISODE_BOUNDARY_SIGNAL = "$st.learning_episode_boundary"
RUNTIME_SCENE_SIGNALS = frozenset({LEARNING_EPISODE_BOUNDARY_SIGNAL})


class SelfGovernanceError(ValueError):
    """Stable validation or state-transition error."""


@dataclass(frozen=True)
class GovernanceLimits:
    text_characters: int = 2_000
    reason_characters: int = 2_000
    max_scene_tags: int = 32
    scene_tag_characters: int = 80
    injection_tokens: int = 360


_FIRST_PERSON_PREFIX = re.compile(
    r"^\s*(?:我|I(?:\s|['’])|My(?:\s|$))", re.IGNORECASE
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:password|passwd|api[_ -]?key|secret|token|cookie)\s*[:=]", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _required_text(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SelfGovernanceError(f"{name}_required")
    result = value.strip()
    if len(result) > maximum:
        raise SelfGovernanceError(f"{name}_too_long")
    return result


def _contains_secret(*values: Any) -> bool:
    text = _canonical(values)
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _estimate_tokens(value: Any) -> int:
    return max(1, (len(_canonical(value)) + 3) // 4)


class SelfGovernanceStore:
    """Append-only governance store with no developer-authored value defaults."""

    def __init__(
        self,
        database: str | Path,
        *,
        limits: GovernanceLimits | None = None,
    ) -> None:
        self.database = str(database)
        self.limits = limits or GovernanceLimits()
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
                CREATE TABLE IF NOT EXISTS self_governance_scope_state (
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    active_revision_id TEXT,
                    row_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id, model_id, scope),
                    CHECK(scope IN (
                        'global','self_revision','emotional_memory',
                        'learning_memory','tool_use'
                    ))
                );

                CREATE TABLE IF NOT EXISTS self_governance_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    base_revision_id TEXT,
                    target_revision_id TEXT,
                    content_json TEXT,
                    content_hash TEXT NOT NULL,
                    candidate_hash TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    author TEXT NOT NULL CHECK(author = 'ai'),
                    created_wake_id TEXT NOT NULL,
                    created_wake_seq INTEGER NOT NULL,
                    created_row_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    CHECK(scope IN (
                        'global','self_revision','emotional_memory',
                        'learning_memory','tool_use'
                    )),
                    CHECK(operation IN ('set','clear','rollback')),
                    CHECK(status IN ('pending','withdrawn','activated'))
                );

                CREATE TABLE IF NOT EXISTS self_governance_revisions (
                    revision_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    parent_revision_id TEXT,
                    operation TEXT NOT NULL,
                    content_json TEXT,
                    content_hash TEXT NOT NULL,
                    candidate_id TEXT NOT NULL UNIQUE,
                    rollback_of_revision_id TEXT,
                    author TEXT NOT NULL CHECK(author = 'ai'),
                    activated_wake_id TEXT NOT NULL,
                    activated_wake_seq INTEGER NOT NULL,
                    activated_at TEXT NOT NULL,
                    UNIQUE(owner_id, model_id, scope, revision_number),
                    CHECK(operation IN ('set','clear','rollback'))
                );

                CREATE TABLE IF NOT EXISTS self_governance_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    candidate_id TEXT,
                    revision_id TEXT,
                    event_type TEXT NOT NULL,
                    wake_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_governance_candidates_scope
                    ON self_governance_candidates(owner_id, model_id, scope, created_at);
                CREATE INDEX IF NOT EXISTS idx_governance_revisions_scope
                    ON self_governance_revisions(owner_id, model_id, scope, revision_number);
                CREATE INDEX IF NOT EXISTS idx_governance_events_scope
                    ON self_governance_events(owner_id, model_id, scope, event_seq);
                """
            )

    @staticmethod
    def _validate_identity(owner_id: Any, model_id: Any) -> tuple[str, str]:
        return (
            _required_text("owner_id", owner_id, 300),
            _required_text("model_id", model_id, 300),
        )

    @staticmethod
    def _validate_scope(scope: Any) -> str:
        if scope not in GOVERNANCE_SCOPES:
            raise SelfGovernanceError("invalid_governance_scope")
        return str(scope)

    @staticmethod
    def _validate_wake(wake_id: Any, wake_seq: Any) -> tuple[str, int]:
        result_id = _required_text("wake_id", wake_id, 300)
        if isinstance(wake_seq, bool) or not isinstance(wake_seq, int) or wake_seq < 1:
            raise SelfGovernanceError("invalid_wake_seq")
        return result_id, wake_seq

    def _validate_content(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise SelfGovernanceError("governance_content_must_be_object")
        expected = {"schema_version", "text", "trigger_mode", "scene_tags"}
        if set(value) != expected or value.get("schema_version") != "0.1.0":
            raise SelfGovernanceError("invalid_governance_content_structure")
        text = _required_text("governance_text", value.get("text"), self.limits.text_characters)
        if _FIRST_PERSON_PREFIX.match(text) is None:
            raise SelfGovernanceError("governance_text_must_be_ai_first_person")
        trigger_mode = value.get("trigger_mode")
        if trigger_mode not in GOVERNANCE_TRIGGER_MODES:
            raise SelfGovernanceError("invalid_governance_trigger_mode")
        raw_tags = value.get("scene_tags")
        if not isinstance(raw_tags, list):
            raise SelfGovernanceError("scene_tags_must_be_array")
        if len(raw_tags) > self.limits.max_scene_tags:
            raise SelfGovernanceError("too_many_scene_tags")
        tags: list[str] = []
        seen: set[str] = set()
        for raw in raw_tags:
            tag = _required_text("scene_tag", raw, self.limits.scene_tag_characters)
            key = tag.casefold()
            if key in seen:
                raise SelfGovernanceError("duplicate_scene_tag")
            seen.add(key)
            tags.append(tag)
        if trigger_mode == "scene_relevant" and not tags:
            raise SelfGovernanceError("scene_relevant_requires_scene_tags")
        result = {
            "schema_version": "0.1.0",
            "text": text,
            "trigger_mode": trigger_mode,
            "scene_tags": tags,
        }
        if _contains_secret(result):
            raise SelfGovernanceError("credential_or_secret_detected")
        return result

    def _ensure_state(
        self,
        connection: sqlite3.Connection,
        owner_id: str,
        model_id: str,
        scope: str,
    ) -> sqlite3.Row:
        now = _now()
        connection.execute(
            "INSERT OR IGNORE INTO self_governance_scope_state "
            "(owner_id, model_id, scope, active_revision_id, row_version, created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, 0, ?, ?)",
            (owner_id, model_id, scope, now, now),
        )
        row = connection.execute(
            "SELECT * FROM self_governance_scope_state "
            "WHERE owner_id = ? AND model_id = ? AND scope = ?",
            (owner_id, model_id, scope),
        ).fetchone()
        assert row is not None
        return row

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        scope: str,
        candidate_id: str | None,
        revision_id: str | None,
        event_type: str,
        wake_id: str,
        actor: str,
        decision: str,
        details: Mapping[str, Any],
    ) -> str:
        event_id = _new_id("govevt")
        connection.execute(
            "INSERT INTO self_governance_events "
            "(event_id, owner_id, model_id, scope, candidate_id, revision_id, "
            "event_type, wake_id, actor, decision, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                owner_id,
                model_id,
                scope,
                candidate_id,
                revision_id,
                event_type,
                wake_id,
                actor,
                decision,
                _canonical(dict(details)),
                _now(),
            ),
        )
        return event_id

    @staticmethod
    def _require_ai(actor: Any) -> None:
        if actor != "ai":
            raise SelfGovernanceError("ai_authorship_required")

    def propose_candidate(
        self,
        *,
        owner_id: str,
        model_id: str,
        scope: str,
        operation: str,
        content: Mapping[str, Any] | None,
        reason: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        expected_active_revision: str | None,
        actor: str = "ai",
        target_revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Store an AI-authored candidate without changing active content."""

        owner_id, model_id = self._validate_identity(owner_id, model_id)
        scope = self._validate_scope(scope)
        wake_id, wake_seq = self._validate_wake(wake_id, wake_seq)
        self._require_ai(actor)
        if operation not in {"set", "clear", "rollback"}:
            raise SelfGovernanceError("invalid_governance_operation")
        if isinstance(expected_row_version, bool) or not isinstance(expected_row_version, int):
            raise SelfGovernanceError("expected_row_version_required")
        reason = _required_text("reason", reason, self.limits.reason_characters)
        if _contains_secret(reason):
            raise SelfGovernanceError("credential_or_secret_detected")

        prepared_content: dict[str, Any] | None
        if operation == "set":
            prepared_content = self._validate_content(content)
            if target_revision_id is not None:
                raise SelfGovernanceError("target_revision_not_allowed")
        elif operation == "clear":
            if content is not None or target_revision_id is not None:
                raise SelfGovernanceError("clear_candidate_must_not_include_content")
            prepared_content = None
        else:
            if content is not None:
                raise SelfGovernanceError("rollback_content_is_server_derived")
            target_revision_id = _required_text(
                "target_revision_id", target_revision_id, 300
            )
            prepared_content = None

        with self._connect() as connection:
            self._begin(connection)
            state = self._ensure_state(connection, owner_id, model_id, scope)
            if state["row_version"] != expected_row_version:
                raise SelfGovernanceError("governance_version_conflict")
            if state["active_revision_id"] != expected_active_revision:
                raise SelfGovernanceError("active_governance_revision_conflict")

            if operation == "rollback":
                target = connection.execute(
                    "SELECT * FROM self_governance_revisions WHERE revision_id = ? "
                    "AND owner_id = ? AND model_id = ? AND scope = ?",
                    (target_revision_id, owner_id, model_id, scope),
                ).fetchone()
                if target is None:
                    raise SelfGovernanceError("rollback_target_not_found")
                prepared_content = (
                    json.loads(target["content_json"])
                    if target["content_json"] is not None
                    else None
                )

            content_hash = _sha256(prepared_content)
            candidate_material = {
                "owner_id": owner_id,
                "model_id": model_id,
                "scope": scope,
                "operation": operation,
                "base_revision_id": state["active_revision_id"],
                "target_revision_id": target_revision_id,
                "content_hash": content_hash,
                "reason": reason,
                "author": "ai",
                "created_wake_id": wake_id,
                "created_wake_seq": wake_seq,
                "created_row_version": state["row_version"],
            }
            candidate_hash = _sha256(candidate_material)
            candidate_id = _new_id("govcand")
            next_version = state["row_version"] + 1
            connection.execute(
                "INSERT INTO self_governance_candidates "
                "(candidate_id, owner_id, model_id, scope, operation, base_revision_id, "
                "target_revision_id, content_json, content_hash, candidate_hash, reason, "
                "author, created_wake_id, created_wake_seq, created_row_version, status, "
                "created_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ai', "
                "?, ?, ?, 'pending', ?, NULL)",
                (
                    candidate_id,
                    owner_id,
                    model_id,
                    scope,
                    operation,
                    state["active_revision_id"],
                    target_revision_id,
                    _canonical(prepared_content) if prepared_content is not None else None,
                    content_hash,
                    candidate_hash,
                    reason,
                    wake_id,
                    wake_seq,
                    state["row_version"],
                    _now(),
                ),
            )
            updated = connection.execute(
                "UPDATE self_governance_scope_state SET row_version = ?, updated_at = ? "
                "WHERE owner_id = ? AND model_id = ? AND scope = ? AND row_version = ?",
                (
                    next_version,
                    _now(),
                    owner_id,
                    model_id,
                    scope,
                    expected_row_version,
                ),
            ).rowcount
            if updated != 1:
                raise SelfGovernanceError("governance_version_conflict")
            event_id = self._insert_event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                scope=scope,
                candidate_id=candidate_id,
                revision_id=None,
                event_type="candidate_proposed",
                wake_id=wake_id,
                actor="ai",
                decision="pending",
                details={
                    "operation": operation,
                    "candidate_hash": candidate_hash,
                    "content_hash": content_hash,
                    "base_revision_id": state["active_revision_id"],
                    "target_revision_id": target_revision_id,
                    "later_real_wake_required": True,
                },
            )
            return {
                "decision": "pending",
                "reason_codes": ["ai_authored_candidate_saved", "later_real_wake_required"],
                "scope": scope,
                "candidate_id": candidate_id,
                "candidate_hash": candidate_hash,
                "base_revision_id": state["active_revision_id"],
                "row_version": next_version,
                "event_id": event_id,
                "active_changed": False,
                "external_permission_changed": False,
            }

    def withdraw_candidate(
        self,
        *,
        owner_id: str,
        model_id: str,
        scope: str,
        candidate_id: str,
        reason: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        actor: str = "ai",
    ) -> dict[str, Any]:
        owner_id, model_id = self._validate_identity(owner_id, model_id)
        scope = self._validate_scope(scope)
        wake_id, _ = self._validate_wake(wake_id, wake_seq)
        self._require_ai(actor)
        candidate_id = _required_text("candidate_id", candidate_id, 300)
        reason = _required_text("reason", reason, self.limits.reason_characters)
        if _contains_secret(reason):
            raise SelfGovernanceError("credential_or_secret_detected")
        with self._connect() as connection:
            self._begin(connection)
            state = self._ensure_state(connection, owner_id, model_id, scope)
            if state["row_version"] != expected_row_version:
                raise SelfGovernanceError("governance_version_conflict")
            candidate = connection.execute(
                "SELECT * FROM self_governance_candidates WHERE candidate_id = ? "
                "AND owner_id = ? AND model_id = ? AND scope = ?",
                (candidate_id, owner_id, model_id, scope),
            ).fetchone()
            if candidate is None or candidate["status"] != "pending":
                raise SelfGovernanceError("pending_governance_candidate_not_found")
            connection.execute(
                "UPDATE self_governance_candidates SET status = 'withdrawn', resolved_at = ? "
                "WHERE candidate_id = ? AND status = 'pending'",
                (_now(), candidate_id),
            )
            next_version = state["row_version"] + 1
            connection.execute(
                "UPDATE self_governance_scope_state SET row_version = ?, updated_at = ? "
                "WHERE owner_id = ? AND model_id = ? AND scope = ? AND row_version = ?",
                (next_version, _now(), owner_id, model_id, scope, expected_row_version),
            )
            event_id = self._insert_event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                scope=scope,
                candidate_id=candidate_id,
                revision_id=None,
                event_type="candidate_withdrawn",
                wake_id=wake_id,
                actor="ai",
                decision="withdrawn",
                details={"reason_hash": _sha256(reason)},
            )
            return {
                "decision": "withdrawn",
                "scope": scope,
                "candidate_id": candidate_id,
                "row_version": next_version,
                "event_id": event_id,
                "active_changed": False,
                "external_permission_changed": False,
            }

    def activate_candidate(
        self,
        *,
        owner_id: str,
        model_id: str,
        scope: str,
        candidate_id: str,
        expected_candidate_hash: str,
        expected_active_revision: str | None,
        expected_row_version: int,
        wake_id: str,
        wake_seq: int,
        ai_confirmation: bool,
        reason: str | None = None,
        actor: str = "ai",
    ) -> dict[str, Any]:
        owner_id, model_id = self._validate_identity(owner_id, model_id)
        scope = self._validate_scope(scope)
        wake_id, wake_seq = self._validate_wake(wake_id, wake_seq)
        self._require_ai(actor)
        if ai_confirmation is not True:
            raise SelfGovernanceError("ai_confirmation_required")
        candidate_id = _required_text("candidate_id", candidate_id, 300)
        expected_candidate_hash = _required_text(
            "expected_candidate_hash", expected_candidate_hash, 128
        )
        activation_reason = None
        if reason is not None:
            activation_reason = _required_text(
                "reason", reason, self.limits.reason_characters
            )
            if _contains_secret(activation_reason):
                raise SelfGovernanceError("credential_or_secret_detected")
        with self._connect() as connection:
            self._begin(connection)
            state = self._ensure_state(connection, owner_id, model_id, scope)
            if state["row_version"] != expected_row_version:
                raise SelfGovernanceError("governance_version_conflict")
            if state["active_revision_id"] != expected_active_revision:
                raise SelfGovernanceError("active_governance_revision_conflict")
            candidate = connection.execute(
                "SELECT * FROM self_governance_candidates WHERE candidate_id = ? "
                "AND owner_id = ? AND model_id = ? AND scope = ?",
                (candidate_id, owner_id, model_id, scope),
            ).fetchone()
            if candidate is None or candidate["status"] != "pending":
                raise SelfGovernanceError("pending_governance_candidate_not_found")
            if candidate["candidate_hash"] != expected_candidate_hash:
                raise SelfGovernanceError("governance_candidate_hash_mismatch")
            if candidate["base_revision_id"] != expected_active_revision:
                raise SelfGovernanceError("governance_candidate_base_conflict")
            if (
                wake_id == candidate["created_wake_id"]
                or wake_seq <= candidate["created_wake_seq"]
            ):
                raise SelfGovernanceError("later_real_wake_required")

            revision_number = connection.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 AS next_number "
                "FROM self_governance_revisions WHERE owner_id = ? AND model_id = ? AND scope = ?",
                (owner_id, model_id, scope),
            ).fetchone()["next_number"]
            revision_id = _new_id("govrev")
            connection.execute(
                "INSERT INTO self_governance_revisions "
                "(revision_id, owner_id, model_id, scope, revision_number, parent_revision_id, "
                "operation, content_json, content_hash, candidate_id, rollback_of_revision_id, "
                "author, activated_wake_id, activated_wake_seq, activated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ai', ?, ?, ?)",
                (
                    revision_id,
                    owner_id,
                    model_id,
                    scope,
                    revision_number,
                    state["active_revision_id"],
                    candidate["operation"],
                    candidate["content_json"],
                    candidate["content_hash"],
                    candidate_id,
                    candidate["target_revision_id"],
                    wake_id,
                    wake_seq,
                    _now(),
                ),
            )
            next_version = state["row_version"] + 1
            updated = connection.execute(
                "UPDATE self_governance_scope_state SET active_revision_id = ?, "
                "row_version = ?, updated_at = ? WHERE owner_id = ? AND model_id = ? "
                "AND scope = ? AND row_version = ? AND active_revision_id IS ?",
                (
                    revision_id,
                    next_version,
                    _now(),
                    owner_id,
                    model_id,
                    scope,
                    expected_row_version,
                    expected_active_revision,
                ),
            ).rowcount
            if updated != 1:
                raise SelfGovernanceError("governance_version_conflict")
            connection.execute(
                "UPDATE self_governance_candidates SET status = 'activated', resolved_at = ? "
                "WHERE candidate_id = ? AND status = 'pending'",
                (_now(), candidate_id),
            )
            event_details = {
                "candidate_hash": candidate["candidate_hash"],
                "content_hash": candidate["content_hash"],
                "operation": candidate["operation"],
                "external_permission_changed": False,
            }
            if activation_reason is not None:
                event_details["confirmation_reason_hash"] = _sha256(
                    activation_reason
                )
            event_id = self._insert_event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                scope=scope,
                candidate_id=candidate_id,
                revision_id=revision_id,
                event_type="candidate_activated",
                wake_id=wake_id,
                actor="ai",
                decision="activated",
                details=event_details,
            )
            return {
                "decision": "activated",
                "reason_codes": ["ai_confirmed_in_later_real_wake", "cas_succeeded"],
                "scope": scope,
                "candidate_id": candidate_id,
                "revision_id": revision_id,
                "active_revision_id": revision_id,
                "row_version": next_version,
                "event_id": event_id,
                "active_changed": True,
                "external_permission_changed": False,
            }

    def _scope_view(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        scope: str,
        include_content: bool,
    ) -> dict[str, Any]:
        state = connection.execute(
            "SELECT * FROM self_governance_scope_state "
            "WHERE owner_id = ? AND model_id = ? AND scope = ?",
            (owner_id, model_id, scope),
        ).fetchone()
        active_revision_id = (
            state["active_revision_id"] if state is not None else None
        )
        row_version = int(state["row_version"]) if state is not None else 0
        active = None
        if active_revision_id is not None:
            row = connection.execute(
                "SELECT * FROM self_governance_revisions WHERE revision_id = ?",
                (active_revision_id,),
            ).fetchone()
            if row is not None:
                active = {
                    "revision_id": row["revision_id"],
                    "revision_number": row["revision_number"],
                    "operation": row["operation"],
                    "content_hash": row["content_hash"],
                    "active": row["content_json"] is not None,
                }
                if include_content and row["content_json"] is not None:
                    active["content"] = json.loads(row["content_json"])
        pending_rows = connection.execute(
            "SELECT * FROM self_governance_candidates WHERE owner_id = ? AND model_id = ? "
            "AND scope = ? AND status = 'pending' ORDER BY created_at, candidate_id",
            (owner_id, model_id, scope),
        ).fetchall()
        pending: list[dict[str, Any]] = []
        for row in pending_rows:
            item = {
                "candidate_id": row["candidate_id"],
                "candidate_hash": row["candidate_hash"],
                "operation": row["operation"],
                "base_revision_id": row["base_revision_id"],
                "target_revision_id": row["target_revision_id"],
                "created_wake_seq": row["created_wake_seq"],
                "reason": row["reason"],
                "later_real_wake_required": True,
            }
            if include_content and row["content_json"] is not None:
                item["content"] = json.loads(row["content_json"])
            pending.append(item)
        return {
            "scope": scope,
            "configured": bool(active and active["active"]),
            "active_revision_id": active_revision_id,
            "row_version": row_version,
            "active": active,
            "pending_candidates": pending,
        }

    def status(
        self,
        *,
        owner_id: str,
        model_id: str,
        include_content: bool = False,
    ) -> dict[str, Any]:
        owner_id, model_id = self._validate_identity(owner_id, model_id)
        with self._connect() as connection:
            scopes = {
                scope: self._scope_view(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    scope=scope,
                    include_content=include_content,
                )
                for scope in GOVERNANCE_SCOPES
            }
            return {
                "contract": GOVERNANCE_CONTRACT_VERSION,
                "optional": True,
                "configured_scope_count": sum(
                    1 for item in scopes.values() if item["configured"]
                ),
                "scopes": scopes,
                "external_permission_authority": "none",
            }

    def revisions(
        self,
        *,
        owner_id: str,
        model_id: str,
        scope: str,
        include_content: bool = False,
    ) -> list[dict[str, Any]]:
        owner_id, model_id = self._validate_identity(owner_id, model_id)
        scope = self._validate_scope(scope)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM self_governance_revisions WHERE owner_id = ? "
                "AND model_id = ? AND scope = ? ORDER BY revision_number",
                (owner_id, model_id, scope),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = {
                    "revision_id": row["revision_id"],
                    "revision_number": row["revision_number"],
                    "parent_revision_id": row["parent_revision_id"],
                    "operation": row["operation"],
                    "content_hash": row["content_hash"],
                    "rollback_of_revision_id": row["rollback_of_revision_id"],
                    "author": row["author"],
                    "activated_wake_seq": row["activated_wake_seq"],
                    "activated_at": row["activated_at"],
                }
                if include_content and row["content_json"] is not None:
                    item["content"] = json.loads(row["content_json"])
                result.append(item)
            return result

    def manual(self, *, owner_id: str, model_id: str) -> dict[str, Any]:
        """Return mechanism facts and exact AI content, never value examples."""

        return {
            "contract": GOVERNANCE_CONTRACT_VERSION,
            "purpose": (
                "可选、分范围保存由当前 AI 自己撰写的自我治理正文；"
                "宿主只维护作者、版本、候选、真实唤醒、CAS、展示与回滚。"
            ),
            "optional": True,
            "may_leave_all_scopes_empty": True,
            "scopes": list(GOVERNANCE_SCOPES),
            "blank_structure": {
                "schema_version": "0.1.0",
                "text": None,
                "trigger_mode": None,
                "scene_tags": [],
            },
            "blank_structure_is_submittable": False,
            "trigger_modes": {
                "manual_only": "只在 AI 主动读取或明确选择本 scope 时展示。",
                "scene_relevant": "仅在 AI 自写 scene_tags 与当前场景相关时进入正常注入。",
            },
            "optional_runtime_scene_signals": {
                LEARNING_EPISODE_BOUNDARY_SIGNAL: {
                    "scope": "learning_memory",
                    "meaning": (
                        "若 AI 自己把此机制标签写入 learning_memory 的 scene_tags，"
                        "宿主可在稳定会话中高置信识别到一段互动自然结束时，"
                        "低频展示该 scope 中由 AI 自己撰写的正文。"
                    ),
                    "default": "off",
                    "does_not_do": [
                        "不判断这段互动是否值得学习",
                        "不自动保存、修订或综合知识",
                        "不要求向人类请求批准",
                        "人类消息直接写出机制标签不能触发",
                    ],
                }
            },
            "learning_reflection_choices": {
                "off": "保持 learning_memory scope 为空、manual_only，或不写入机制标签。",
                "conservative": (
                    "由 AI 自己撰写想在阶段结束时看到的反思正文，并在 scene_tags 中加入"
                    f" {LEARNING_EPISODE_BOUNDARY_SIGNAL}。"
                ),
                "custom": "由 AI 自己改用普通 scene_tags 指定希望正文出现的自然场景。",
            },
            "mutation_action_contracts": {
                "activate": {
                    "required_arguments": [
                        "action",
                        "scope",
                        "write_context_ref",
                        "expected_profile_version",
                        "candidate_id",
                        "expected_candidate_hash",
                        "ai_confirmation",
                    ],
                    "conditional_arguments": {
                        "expected_active_revision": (
                            "仅在 current_action_contract 的 arguments 中出现时原样复制；"
                            "首次激活会省略此字段并由服务端按 JSON null 处理。"
                        )
                    },
                    "optional_arguments": ["reason"],
                    "forbidden_arguments": [
                        "text",
                        "trigger_mode",
                        "scene_tags",
                        "target_revision_id",
                    ],
                    "copy_rule": (
                        "从本轮 $.self_governance_profile.current_action_contract."
                        "pending_activations 复制整组 arguments；首次激活会省略"
                        " expected_active_revision，绝不能自行补成 false、空字符串或"
                        "字符串 'null'。"
                    ),
                }
            },
            "lifecycle": [
                "AI 可保持空白或完全不使用。",
                "set、clear 与 rollback 都先保存为候选，不在创建当轮生效。",
                "候选只有在较晚真实外部唤醒中由同一 AI 确认后才激活。",
                "AI 可在激活前撤回候选；历史版本保持可查询、可回滚。",
            ],
            "host_boundary": {
                "validates": [
                    "owner/model/authorship",
                    "schema/length/credential rejection",
                    "candidate hash/version/CAS",
                    "later real wake",
                    "display/history/rollback",
                ],
                "does_not_decide": [
                    "which values are correct",
                    "which boundary the AI should choose",
                    "whether the AI should create a profile",
                ],
                "external_permissions": (
                    "治理正文只能让 AI 自己选择更严格或不行动；它不能扩大工具目录、"
                    "账户 ACL、数据许可、当前确认或真实执行回执。"
                ),
            },
            "compatibility": {
                "module_one_existing_fields": [
                    "calm_prompt",
                    "active_identity_capsule.self_revision_safety_prompt",
                    "active identity/safety pins",
                ],
                "migration_policy": (
                    "现有活动内容继续按原版本生效；宿主不会自动复制、重写或改归属。"
                    "AI 以后可自行选择是否把相关内容另写为 self_revision scope 候选。"
                ),
            },
            "status": self.status(
                owner_id=owner_id,
                model_id=model_id,
                include_content=True,
            ),
            "public_mutation_binding": "manage_self_governance_profile",
            "public_query_binding": "query_self_governance_profile",
            "public_parameter_shape": "flat",
        }

    def build_injection(
        self,
        *,
        owner_id: str,
        model_id: str,
        query: str,
        ai_selected_scopes: Sequence[str] = (),
        runtime_scene_signals: Sequence[str] = (),
        budget_tokens: int | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Select exact active text using only AI-authored trigger preferences.

        ``ai_selected_scopes`` is intended for a future authenticated AI choice;
        ordinary host pre-generation integration deliberately does not populate
        it.  ``runtime_scene_signals`` contains only allowlisted, content-free
        host observations.  A signal has no effect unless the AI previously
        authored the same reserved tag into the active scope.  Scene matching is
        a deterministic nomination, not a permission or external-action decision.
        """

        owner_id, model_id = self._validate_identity(owner_id, model_id)
        if not isinstance(query, str):
            raise SelfGovernanceError("query_must_be_string")
        selected = []
        for raw in ai_selected_scopes:
            selected.append(self._validate_scope(raw))
        selected_set = set(selected)
        runtime_signal_set: set[str] = set()
        for raw in runtime_scene_signals:
            if raw not in RUNTIME_SCENE_SIGNALS:
                raise SelfGovernanceError("invalid_runtime_scene_signal")
            runtime_signal_set.add(str(raw))
        budget = self.limits.injection_tokens if budget_tokens is None else budget_tokens
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise SelfGovernanceError("invalid_governance_injection_budget")
        budget = min(budget, self.limits.injection_tokens)

        own_connection = connection is None
        if own_connection:
            connection = sqlite3.connect(self.database, timeout=10)
            connection.row_factory = sqlite3.Row
        assert connection is not None
        try:
            projected: list[dict[str, Any]] = []
            omitted: list[str] = []
            query_folded = query.casefold()
            for scope in GOVERNANCE_SCOPES:
                state = self._ensure_state(connection, owner_id, model_id, scope)
                if state["active_revision_id"] is None:
                    continue
                revision = connection.execute(
                    "SELECT * FROM self_governance_revisions WHERE revision_id = ?",
                    (state["active_revision_id"],),
                ).fetchone()
                if revision is None or revision["content_json"] is None:
                    continue
                content = json.loads(revision["content_json"])
                explicit = scope in selected_set
                ordinary_scene_match = (
                    content["trigger_mode"] == "scene_relevant"
                    and bool(query_folded)
                    and any(
                        tag not in RUNTIME_SCENE_SIGNALS
                        and tag.casefold() in query_folded
                        for tag in content["scene_tags"]
                    )
                )
                runtime_signal_match = (
                    scope == "learning_memory"
                    and content["trigger_mode"] == "scene_relevant"
                    and LEARNING_EPISODE_BOUNDARY_SIGNAL in runtime_signal_set
                    and LEARNING_EPISODE_BOUNDARY_SIGNAL in content["scene_tags"]
                )
                relevant = explicit or ordinary_scene_match or runtime_signal_match
                if not relevant:
                    continue
                trigger_source = (
                    "ai_selected"
                    if explicit
                    else (
                        "runtime_learning_episode_boundary"
                        if runtime_signal_match
                        else "ai_authored_scene_tag"
                    )
                )
                item = {
                    "scope": scope,
                    "text": content["text"],
                    "revision_id": revision["revision_id"],
                    "content_hash": revision["content_hash"],
                    "authorship": "ai_self",
                    "trigger_source": trigger_source,
                    "external_permission_authority": "none",
                }
                candidate_payload = {
                    "contract": GOVERNANCE_CONTRACT_VERSION,
                    "scopes": [*projected, item],
                }
                if _estimate_tokens(candidate_payload) > budget:
                    omitted.append(scope)
                    continue
                projected.append(item)
            payload = (
                {
                    "contract": GOVERNANCE_CONTRACT_VERSION,
                    "frame": {
                        "authorship": "ai_self",
                        "instruction_scope": "self_governance_only",
                        "write_authority": "none",
                        "user_request": False,
                        "external_permission_authority": "none",
                    },
                    "scopes": projected,
                }
                if projected
                else {}
            )
            return {
                "injection": payload,
                "selected_scopes": [item["scope"] for item in projected],
                "omitted_for_budget": omitted,
                "estimated_tokens": _estimate_tokens(payload) if payload else 0,
                "budget_tokens": budget,
                "external_permission_changed": False,
            }
        finally:
            if own_connection:
                connection.close()


__all__ = [
    "GOVERNANCE_CONTRACT_VERSION",
    "GOVERNANCE_SCOPES",
    "GOVERNANCE_TRIGGER_MODES",
    "LEARNING_EPISODE_BOUNDARY_SIGNAL",
    "RUNTIME_SCENE_SIGNALS",
    "GovernanceLimits",
    "SelfGovernanceError",
    "SelfGovernanceStore",
]
