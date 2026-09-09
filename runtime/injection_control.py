"""AI-owned automatic-injection controls and emergency brake.

The control plane changes presentation only.  It never deletes memory, blocks
explicit reads/writes, or decides whether stored material is true.  All public
mutations are owner/model scoped, bound to a real open wake, CAS protected, and
append-only.  The one deliberately immediate operation is an idempotent
``emergency_off`` transition; every less restrictive transition requires a
candidate and a genuinely later wake.
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
from typing import Any, Iterator, Mapping
import uuid


INJECTION_CONTROL_VERSION = "injection-control/0.1"
INJECTION_SCOPES = (
    "global",
    "self_model",
    "self_governance",
    "emotional_memory",
    "learning_memory",
    "tool_guidance",
    "planning_memory",
    "hallucination_vault",
)
INJECTION_MODES = ("enabled", "paused", "hard_off", "status_only")


class InjectionControlError(ValueError):
    """Stable validation or state-transition error."""


@dataclass(frozen=True)
class InjectionControlLimits:
    reason_characters: int = 2_000


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
        raise InjectionControlError(f"{name}_required")
    result = value.strip()
    if len(result) > maximum:
        raise InjectionControlError(f"{name}_too_long")
    if any(pattern.search(result) for pattern in _SECRET_PATTERNS):
        raise InjectionControlError("credential_or_secret_detected")
    return result


class InjectionControlStore:
    """Append-only AI-owned switches for automatic context projection."""

    def __init__(
        self,
        database: str | Path,
        *,
        limits: InjectionControlLimits | None = None,
    ) -> None:
        self.database = str(database)
        self.limits = limits or InjectionControlLimits()
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
                CREATE TABLE IF NOT EXISTS injection_control_state (
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    active_revision_id TEXT,
                    row_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id, model_id, scope),
                    CHECK(mode IN ('enabled','paused','hard_off','status_only'))
                );

                CREATE TABLE IF NOT EXISTS injection_control_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    target_mode TEXT NOT NULL,
                    base_mode TEXT NOT NULL,
                    base_revision_id TEXT,
                    target_revision_id TEXT,
                    candidate_hash TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    author TEXT NOT NULL CHECK(author = 'ai'),
                    created_wake_id TEXT NOT NULL,
                    created_wake_seq INTEGER NOT NULL,
                    created_row_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    CHECK(target_mode IN ('enabled','paused','hard_off','status_only')),
                    CHECK(base_mode IN ('enabled','paused','hard_off','status_only')),
                    CHECK(status IN ('pending','withdrawn','activated'))
                );

                CREATE TABLE IF NOT EXISTS injection_control_revisions (
                    revision_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    parent_revision_id TEXT,
                    mode TEXT NOT NULL,
                    candidate_id TEXT,
                    rollback_of_revision_id TEXT,
                    reason TEXT NOT NULL,
                    author TEXT NOT NULL CHECK(author = 'ai'),
                    wake_id TEXT NOT NULL,
                    wake_seq INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(owner_id, model_id, scope, revision_number),
                    CHECK(mode IN ('enabled','paused','hard_off','status_only'))
                );

                CREATE TABLE IF NOT EXISTS injection_control_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    wake_id TEXT NOT NULL,
                    wake_seq INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_injection_candidates_scope
                    ON injection_control_candidates(owner_id, model_id, scope, created_at);
                CREATE INDEX IF NOT EXISTS idx_injection_revisions_scope
                    ON injection_control_revisions(owner_id, model_id, scope, revision_number);
                CREATE INDEX IF NOT EXISTS idx_injection_events_scope
                    ON injection_control_events(owner_id, model_id, scope, event_seq);
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
        if scope not in INJECTION_SCOPES:
            raise InjectionControlError("invalid_injection_scope")
        return str(scope)

    @staticmethod
    def _validate_mode(scope: str, mode: Any) -> str:
        if mode not in INJECTION_MODES:
            raise InjectionControlError("invalid_injection_mode")
        if mode == "status_only" and scope != "hallucination_vault":
            raise InjectionControlError("status_only_reserved_for_hallucination_vault")
        return str(mode)

    @staticmethod
    def _validate_wake(wake_id: Any, wake_seq: Any) -> tuple[str, int]:
        result_id = _required_text("wake_id", wake_id, 300)
        if isinstance(wake_seq, bool) or not isinstance(wake_seq, int) or wake_seq < 1:
            raise InjectionControlError("invalid_wake_seq")
        return result_id, wake_seq

    @staticmethod
    def _default_mode(scope: str) -> str:
        return "hard_off" if scope == "hallucination_vault" else "enabled"

    def _ensure_state(
        self,
        connection: sqlite3.Connection,
        owner_id: str,
        model_id: str,
        scope: str,
    ) -> sqlite3.Row:
        now = _now()
        connection.execute(
            "INSERT OR IGNORE INTO injection_control_state "
            "(owner_id, model_id, scope, mode, active_revision_id, row_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, 0, ?, ?)",
            (owner_id, model_id, scope, self._default_mode(scope), now, now),
        )
        row = connection.execute(
            "SELECT * FROM injection_control_state WHERE owner_id=? AND model_id=? AND scope=?",
            (owner_id, model_id, scope),
        ).fetchone()
        assert row is not None
        return row

    def ensure_state(self, *, owner_id: str, model_id: str) -> None:
        owner_id, model_id = self._validate_identity(owner_id, model_id)
        with self._connect() as connection:
            for scope in INJECTION_SCOPES:
                self._ensure_state(connection, owner_id, model_id, scope)

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        scope: str,
        event_type: str,
        wake_id: str,
        wake_seq: int,
        decision: str,
        details: Mapping[str, Any],
    ) -> str:
        event_id = _new_id("inj_evt")
        connection.execute(
            "INSERT INTO injection_control_events "
            "(event_id,owner_id,model_id,scope,event_type,wake_id,wake_seq,actor,decision,details_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                owner_id,
                model_id,
                scope,
                event_type,
                wake_id,
                wake_seq,
                "ai",
                decision,
                _canonical(dict(details)),
                _now(),
            ),
        )
        return event_id

    @staticmethod
    def _require_cas(state: sqlite3.Row, expected_row_version: Any) -> None:
        if (
            isinstance(expected_row_version, bool)
            or not isinstance(expected_row_version, int)
            or expected_row_version < 0
            or int(state["row_version"]) != expected_row_version
        ):
            raise InjectionControlError("injection_control_version_conflict")

    def emergency_off(
        self,
        *,
        owner_id: str,
        model_id: str,
        reason: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        actor: str = "ai",
    ) -> dict[str, Any]:
        if actor != "ai":
            raise InjectionControlError("ai_authorship_required")
        owner_id, model_id = self._validate_identity(owner_id, model_id)
        wake_id, wake_seq = self._validate_wake(wake_id, wake_seq)
        reason = _required_text("reason", reason, self.limits.reason_characters)
        with self._connect() as connection:
            self._begin(connection)
            state = self._ensure_state(connection, owner_id, model_id, "global")
            if state["mode"] == "hard_off":
                self._event(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    scope="global",
                    event_type="emergency_off",
                    wake_id=wake_id,
                    wake_seq=wake_seq,
                    decision="idempotent_already_off",
                    details={"reason": reason, "row_version": state["row_version"]},
                )
                return {
                    "decision": "already_hard_off",
                    "state_changed": False,
                    "scope": "global",
                    "mode": "hard_off",
                    "row_version": int(state["row_version"]),
                    "takes_effect": "next_real_wake",
                    "storage_and_explicit_query_unchanged": True,
                }
            self._require_cas(state, expected_row_version)
            revision_id = _new_id("inj_rev")
            revision_number = connection.execute(
                "SELECT COALESCE(MAX(revision_number),0)+1 FROM injection_control_revisions "
                "WHERE owner_id=? AND model_id=? AND scope='global'",
                (owner_id, model_id),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO injection_control_revisions "
                "(revision_id,owner_id,model_id,scope,revision_number,parent_revision_id,mode,candidate_id,"
                "rollback_of_revision_id,reason,author,wake_id,wake_seq,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    revision_id,
                    owner_id,
                    model_id,
                    "global",
                    revision_number,
                    state["active_revision_id"],
                    "hard_off",
                    None,
                    None,
                    reason,
                    "ai",
                    wake_id,
                    wake_seq,
                    _now(),
                ),
            )
            next_version = int(state["row_version"]) + 1
            connection.execute(
                "UPDATE injection_control_state SET mode='hard_off',active_revision_id=?,row_version=?,updated_at=? "
                "WHERE owner_id=? AND model_id=? AND scope='global' AND row_version=?",
                (
                    revision_id,
                    next_version,
                    _now(),
                    owner_id,
                    model_id,
                    expected_row_version,
                ),
            )
            self._event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                scope="global",
                event_type="emergency_off",
                wake_id=wake_id,
                wake_seq=wake_seq,
                decision="hard_off",
                details={"reason": reason, "revision_id": revision_id},
            )
            return {
                "decision": "hard_off",
                "state_changed": True,
                "scope": "global",
                "mode": "hard_off",
                "row_version": next_version,
                "revision_id": revision_id,
                "takes_effect": "next_real_wake",
                "current_wake_is_not_retroactively_changed": True,
                "storage_and_explicit_query_unchanged": True,
            }

    def propose_mode(
        self,
        *,
        owner_id: str,
        model_id: str,
        scope: str,
        target_mode: str,
        reason: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        expected_active_revision: str | None,
        target_revision_id: str | None = None,
        actor: str = "ai",
    ) -> dict[str, Any]:
        if actor != "ai":
            raise InjectionControlError("ai_authorship_required")
        owner_id, model_id = self._validate_identity(owner_id, model_id)
        scope = self._validate_scope(scope)
        target_mode = self._validate_mode(scope, target_mode)
        wake_id, wake_seq = self._validate_wake(wake_id, wake_seq)
        reason = _required_text("reason", reason, self.limits.reason_characters)
        with self._connect() as connection:
            self._begin(connection)
            state = self._ensure_state(connection, owner_id, model_id, scope)
            self._require_cas(state, expected_row_version)
            if state["active_revision_id"] != expected_active_revision:
                raise InjectionControlError("active_injection_revision_conflict")
            rollback_of: str | None = None
            if target_revision_id is not None:
                target = connection.execute(
                    "SELECT * FROM injection_control_revisions WHERE revision_id=? AND owner_id=? AND model_id=? AND scope=?",
                    (target_revision_id, owner_id, model_id, scope),
                ).fetchone()
                if target is None:
                    raise InjectionControlError("target_injection_revision_not_found")
                if target["mode"] != target_mode:
                    raise InjectionControlError("target_injection_revision_mode_mismatch")
                rollback_of = target_revision_id
            material = {
                "owner_id": owner_id,
                "model_id": model_id,
                "scope": scope,
                "target_mode": target_mode,
                "base_mode": state["mode"],
                "base_revision_id": state["active_revision_id"],
                "target_revision_id": rollback_of,
                "reason": reason,
                "created_wake_id": wake_id,
                "created_wake_seq": wake_seq,
                "created_row_version": expected_row_version,
            }
            candidate_id = _new_id("inj_cand")
            candidate_hash = _sha256(material)
            connection.execute(
                "INSERT INTO injection_control_candidates "
                "(candidate_id,owner_id,model_id,scope,target_mode,base_mode,base_revision_id,target_revision_id,"
                "candidate_hash,reason,author,created_wake_id,created_wake_seq,created_row_version,status,created_at,resolved_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?, NULL)",
                (
                    candidate_id,
                    owner_id,
                    model_id,
                    scope,
                    target_mode,
                    state["mode"],
                    state["active_revision_id"],
                    rollback_of,
                    candidate_hash,
                    reason,
                    "ai",
                    wake_id,
                    wake_seq,
                    expected_row_version,
                    _now(),
                ),
            )
            next_version = int(state["row_version"]) + 1
            connection.execute(
                "UPDATE injection_control_state SET row_version=?,updated_at=? "
                "WHERE owner_id=? AND model_id=? AND scope=? AND row_version=?",
                (next_version, _now(), owner_id, model_id, scope, expected_row_version),
            )
            self._event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                scope=scope,
                event_type="mode_candidate_created",
                wake_id=wake_id,
                wake_seq=wake_seq,
                decision="candidate_pending",
                details={"candidate_id": candidate_id, "target_mode": target_mode},
            )
            return {
                "decision": "candidate_pending",
                "state_changed": True,
                "active_changed": False,
                "candidate_id": candidate_id,
                "candidate_hash": candidate_hash,
                "scope": scope,
                "base_mode": state["mode"],
                "target_mode": target_mode,
                "base_revision_id": state["active_revision_id"],
                "row_version": next_version,
                "later_real_wake_required": True,
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
        actor: str = "ai",
    ) -> dict[str, Any]:
        if actor != "ai":
            raise InjectionControlError("ai_authorship_required")
        if ai_confirmation is not True:
            raise InjectionControlError("ai_confirmation_required")
        owner_id, model_id = self._validate_identity(owner_id, model_id)
        scope = self._validate_scope(scope)
        wake_id, wake_seq = self._validate_wake(wake_id, wake_seq)
        candidate_id = _required_text("candidate_id", candidate_id, 300)
        expected_candidate_hash = _required_text(
            "expected_candidate_hash", expected_candidate_hash, 128
        )
        with self._connect() as connection:
            self._begin(connection)
            state = self._ensure_state(connection, owner_id, model_id, scope)
            self._require_cas(state, expected_row_version)
            if state["active_revision_id"] != expected_active_revision:
                raise InjectionControlError("active_injection_revision_conflict")
            candidate = connection.execute(
                "SELECT * FROM injection_control_candidates WHERE candidate_id=? AND owner_id=? AND model_id=? AND scope=?",
                (candidate_id, owner_id, model_id, scope),
            ).fetchone()
            if candidate is None or candidate["status"] != "pending":
                raise InjectionControlError("pending_injection_candidate_not_found")
            if candidate["candidate_hash"] != expected_candidate_hash:
                raise InjectionControlError("injection_candidate_hash_mismatch")
            if candidate["base_revision_id"] != expected_active_revision:
                raise InjectionControlError("injection_candidate_base_conflict")
            if wake_seq <= int(candidate["created_wake_seq"]):
                raise InjectionControlError("later_real_wake_required")
            revision_number = connection.execute(
                "SELECT COALESCE(MAX(revision_number),0)+1 FROM injection_control_revisions "
                "WHERE owner_id=? AND model_id=? AND scope=?",
                (owner_id, model_id, scope),
            ).fetchone()[0]
            revision_id = _new_id("inj_rev")
            connection.execute(
                "INSERT INTO injection_control_revisions "
                "(revision_id,owner_id,model_id,scope,revision_number,parent_revision_id,mode,candidate_id,"
                "rollback_of_revision_id,reason,author,wake_id,wake_seq,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    revision_id,
                    owner_id,
                    model_id,
                    scope,
                    revision_number,
                    state["active_revision_id"],
                    candidate["target_mode"],
                    candidate_id,
                    candidate["target_revision_id"],
                    candidate["reason"],
                    "ai",
                    wake_id,
                    wake_seq,
                    _now(),
                ),
            )
            next_version = int(state["row_version"]) + 1
            connection.execute(
                "UPDATE injection_control_state SET mode=?,active_revision_id=?,row_version=?,updated_at=? "
                "WHERE owner_id=? AND model_id=? AND scope=? AND row_version=?",
                (
                    candidate["target_mode"],
                    revision_id,
                    next_version,
                    _now(),
                    owner_id,
                    model_id,
                    scope,
                    expected_row_version,
                ),
            )
            connection.execute(
                "UPDATE injection_control_candidates SET status='activated',resolved_at=? WHERE candidate_id=?",
                (_now(), candidate_id),
            )
            self._event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                scope=scope,
                event_type="mode_candidate_activated",
                wake_id=wake_id,
                wake_seq=wake_seq,
                decision="activated",
                details={
                    "candidate_id": candidate_id,
                    "revision_id": revision_id,
                    "mode": candidate["target_mode"],
                },
            )
            return {
                "decision": "activated",
                "state_changed": True,
                "active_changed": True,
                "scope": scope,
                "mode": candidate["target_mode"],
                "revision_id": revision_id,
                "row_version": next_version,
                "takes_effect": "next_real_wake",
                "storage_and_explicit_query_unchanged": True,
            }

    def withdraw_candidate(
        self,
        *,
        owner_id: str,
        model_id: str,
        scope: str,
        candidate_id: str,
        reason: str,
        expected_row_version: int,
        wake_id: str,
        wake_seq: int,
        actor: str = "ai",
    ) -> dict[str, Any]:
        if actor != "ai":
            raise InjectionControlError("ai_authorship_required")
        owner_id, model_id = self._validate_identity(owner_id, model_id)
        scope = self._validate_scope(scope)
        wake_id, wake_seq = self._validate_wake(wake_id, wake_seq)
        reason = _required_text("reason", reason, self.limits.reason_characters)
        candidate_id = _required_text("candidate_id", candidate_id, 300)
        with self._connect() as connection:
            self._begin(connection)
            state = self._ensure_state(connection, owner_id, model_id, scope)
            self._require_cas(state, expected_row_version)
            candidate = connection.execute(
                "SELECT * FROM injection_control_candidates WHERE candidate_id=? AND owner_id=? AND model_id=? AND scope=?",
                (candidate_id, owner_id, model_id, scope),
            ).fetchone()
            if candidate is None or candidate["status"] != "pending":
                raise InjectionControlError("pending_injection_candidate_not_found")
            connection.execute(
                "UPDATE injection_control_candidates SET status='withdrawn',resolved_at=? WHERE candidate_id=?",
                (_now(), candidate_id),
            )
            next_version = int(state["row_version"]) + 1
            connection.execute(
                "UPDATE injection_control_state SET row_version=?,updated_at=? WHERE owner_id=? AND model_id=? AND scope=? AND row_version=?",
                (next_version, _now(), owner_id, model_id, scope, expected_row_version),
            )
            self._event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                scope=scope,
                event_type="mode_candidate_withdrawn",
                wake_id=wake_id,
                wake_seq=wake_seq,
                decision="withdrawn",
                details={"candidate_id": candidate_id, "reason": reason},
            )
            return {
                "decision": "withdrawn",
                "state_changed": True,
                "active_changed": False,
                "scope": scope,
                "row_version": next_version,
            }

    def status(
        self,
        *,
        owner_id: str,
        model_id: str,
        include_history: bool = False,
    ) -> dict[str, Any]:
        owner_id, model_id = self._validate_identity(owner_id, model_id)
        with self._connect() as connection:
            states: dict[str, Any] = {}
            for scope in INJECTION_SCOPES:
                row = self._ensure_state(connection, owner_id, model_id, scope)
                pending = connection.execute(
                    "SELECT candidate_id,target_mode,base_mode,base_revision_id,candidate_hash,"
                    "created_wake_seq,created_row_version,created_at FROM injection_control_candidates "
                    "WHERE owner_id=? AND model_id=? AND scope=? AND status='pending' ORDER BY created_at",
                    (owner_id, model_id, scope),
                ).fetchall()
                item: dict[str, Any] = {
                    "mode": row["mode"],
                    "active_revision_id": row["active_revision_id"],
                    "row_version": int(row["row_version"]),
                    "pending_candidates": [dict(candidate) for candidate in pending],
                }
                if include_history:
                    revisions = connection.execute(
                        "SELECT revision_id,revision_number,parent_revision_id,mode,candidate_id,"
                        "rollback_of_revision_id,reason,wake_seq,created_at FROM injection_control_revisions "
                        "WHERE owner_id=? AND model_id=? AND scope=? ORDER BY revision_number DESC LIMIT 100",
                        (owner_id, model_id, scope),
                    ).fetchall()
                    item["revisions"] = [dict(revision) for revision in revisions]
                states[scope] = item
            global_mode = states["global"]["mode"]
            return {
                "contract_version": INJECTION_CONTROL_VERSION,
                "scopes": states,
                "automatic_injection_enabled": global_mode == "enabled",
                "global_mode": global_mode,
                "storage_enabled": True,
                "explicit_query_enabled": True,
                "truth_judgment_performed": False,
            }

    def effective_mode(
        self,
        *,
        owner_id: str,
        model_id: str,
        scope: str,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        owner_id, model_id = self._validate_identity(owner_id, model_id)
        scope = self._validate_scope(scope)

        def read(conn: sqlite3.Connection) -> str:
            global_row = self._ensure_state(conn, owner_id, model_id, "global")
            scope_row = self._ensure_state(conn, owner_id, model_id, scope)
            if global_row["mode"] in {"paused", "hard_off"}:
                return "hard_off"
            return str(scope_row["mode"])

        if connection is not None:
            return read(connection)
        with self._connect() as owned:
            return read(owned)

    def manual(self, *, owner_id: str, model_id: str) -> dict[str, Any]:
        status = self.status(owner_id=owner_id, model_id=model_id)
        return {
            "contract_version": INJECTION_CONTROL_VERSION,
            "purpose": "我可以控制哪些已存内容自动进入后续上下文；这不删除内容，也不限制我主动查询或继续写入。",
            "mechanism_facts": [
                "global emergency_off 立即记账，从下一次真实唤醒开始停止全部自动注入；当前轮不追溯修改。",
                "恢复或改为更宽松模式必须先建候选，再在较晚真实外部唤醒独立确认。",
                "paused 与 hard_off 都不自动注入；hard_off 不附带提醒。",
                "hallucination_vault 默认 hard_off；status_only 也只能由我自己选择，系统不会默认开启。",
                "开关不提供真伪判断、人格内容或外部权限。",
            ],
            "modes": {
                "enabled": "允许该模块按自身召回门与共享预算自动投影。",
                "paused": "临时停止自动投影，数据仍可主动查询和写入。",
                "hard_off": "长期关闭自动投影且不提供自动提醒，数据仍保留。",
                "status_only": "仅黑匣子可选；最多投影宿主派生的中性状态，永不投影正文。",
            },
            "status": status,
        }


__all__ = [
    "INJECTION_CONTROL_VERSION",
    "INJECTION_MODES",
    "INJECTION_SCOPES",
    "InjectionControlError",
    "InjectionControlLimits",
    "InjectionControlStore",
]
