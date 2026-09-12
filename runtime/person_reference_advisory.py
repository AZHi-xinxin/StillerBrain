"""Optional, AI-editable person-reference advice, separate from draft rewriting.

Reading an unset preference projects the host suggestion without inserting a
row. Explicit author actions append to one owner/model-scoped history table;
old prose is retained internally but never included in the default projection.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from .credential_guard import contains_credential_or_secret
from .execution_binding import assert_bound_execution, expected_execution_wake
from .ordinary_access import current_ordinary_access


PERSON_REFERENCE_ADVISORY_DEFAULT = (
    "若你用第一人称叙述，尽量写清是谁：‘我和 user 名’胜过‘我和她’。"
    "摘要里的‘她’在以后反复浮现时，可能让你混淆人称。"
)
PERSON_REFERENCE_ADVISORY_TABLE = "person_reference_advisory_history"
PERSON_REFERENCE_ADVISORY_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS person_reference_advisory_history (
        change_id INTEGER PRIMARY KEY,
        owner_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('set','disable','reset')),
        advisory_text TEXT,
        context_mode TEXT NOT NULL,
        wake_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK((action = 'set' AND advisory_text IS NOT NULL
               AND length(advisory_text) BETWEEN 1 AND 2000)
              OR (action IN ('disable','reset') AND advisory_text IS NULL))
    )""",
    """CREATE INDEX IF NOT EXISTS idx_person_reference_advisory_owner
        ON person_reference_advisory_history(owner_id, model_id, change_id)""",
    """CREATE TRIGGER IF NOT EXISTS person_reference_advisory_no_update
        BEFORE UPDATE ON person_reference_advisory_history
        BEGIN SELECT RAISE(ABORT, 'person_reference_advisory_history_append_only'); END""",
    """CREATE TRIGGER IF NOT EXISTS person_reference_advisory_no_delete
        BEFORE DELETE ON person_reference_advisory_history
        BEGIN SELECT RAISE(ABORT, 'person_reference_advisory_history_append_only'); END""",
)


class PersonReferenceAdvisoryError(ValueError):
    """Stable error without echoing model-authored or protected content."""


def initialize_person_reference_advisory_schema(connection: sqlite3.Connection) -> None:
    """Create only this empty schema, preserving the caller's transaction.

    Deployment rehearsal can call this exact helper on a private SQLite backup.
    Individual execute calls intentionally avoid executescript's implicit commit.
    """
    assert_bound_execution(connection)
    for statement in PERSON_REFERENCE_ADVISORY_SCHEMA:
        connection.execute(statement)
    assert_bound_execution(connection)


def _identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersonReferenceAdvisoryError(f"{name}_required")
    result = value.strip()
    if len(result) > 300:
        raise PersonReferenceAdvisoryError(f"{name}_too_long")
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class PersonReferenceAdvisoryStore:
    """A flat preference with internal history, not a memory-writing prerequisite."""

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            initialize_person_reference_advisory_schema(connection)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                assert_bound_execution(connection)
                yield connection
                assert_bound_execution(connection)
        finally:
            connection.close()

    @staticmethod
    def _projection(connection: sqlite3.Connection, owner_id: str, model_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT action, advisory_text, created_at FROM person_reference_advisory_history "
            "WHERE owner_id=? AND model_id=? ORDER BY change_id DESC LIMIT 1",
            (owner_id, model_id),
        ).fetchone()
        count = int(connection.execute(
            "SELECT COUNT(*) FROM person_reference_advisory_history WHERE owner_id=? AND model_id=?",
            (owner_id, model_id),
        ).fetchone()[0])
        action = row["action"] if row is not None else None
        result: dict[str, Any] = {
            "enabled": action != "disable",
            "source": "ai_authored" if action == "set" else "ai_disabled" if action == "disable" else "host_advisory",
            "optional": True,
            "advisory_strength": "optional",
            "manage_tool": "manage_person_reference_advisory",
            "can_edit_or_disable": True,
            "narrative_person": "由 AI 自选，适用于第一、第三人称或其他叙事方式。",
            "control_note": "AI 可用 set 自写、disable 关闭、reset 恢复默认；此设置与一次性草稿改写开关独立。",
            "rewrite_assist_default": False,
            "rewrite_assist_scope": "current_draft_only",
            "history": {"change_count": count, "last_action": action,
                        "updated_at": row["created_at"] if row is not None else None},
        }
        if action != "disable":
            result["message"] = row["advisory_text"] if action == "set" else PERSON_REFERENCE_ADVISORY_DEFAULT
        return result

    def read(self, *, owner_id: str, model_id: str) -> dict[str, Any]:
        owner_id, model_id = _identity(owner_id, "owner_id"), _identity(model_id, "model_id")
        expected_execution_wake(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            connection.execute("BEGIN")
            return self._projection(connection, owner_id, model_id)

    def _legacy_binding(self, *, onboarding: Any, owner_id: str, model_id: str,
                        write_context_ref: str) -> dict[str, Any]:
        # Model-supplied dictionaries/booleans are never accepted as authority.
        from .onboarding import ModuleOneOnboardingStore
        if (not isinstance(onboarding, ModuleOneOnboardingStore)
                or Path(onboarding.database).resolve() != self.database.resolve()):
            raise PersonReferenceAdvisoryError("authenticated_authoring_context_required")
        permission = onboarding.authorize_other_module_write(
            owner_id=owner_id, model_id=model_id, module_name="shared_person_authoring")
        if permission.get("decision") != "allowed":
            raise PersonReferenceAdvisoryError("module_one_required")
        binding = onboarding.current_open_write_context(
            owner_id=owner_id, model_id=model_id,
            write_context_ref=write_context_ref, required_scope="shared_person_authoring")
        if binding.get("write_context_available") is not True:
            raise PersonReferenceAdvisoryError("brain_open_required")
        return binding

    @staticmethod
    def _assert_legacy_still_current(connection: sqlite3.Connection, *, onboarding: Any,
                                    owner_id: str, model_id: str, binding: dict[str, Any]) -> None:
        """Recheck the validated context while the write lock is held."""
        wake, reason = onboarding._validate_wake(
            connection, owner_id=owner_id, model_id=model_id,
            wake_id=binding["wake_id"], wake_capability=binding["wake_capability"])
        if reason or wake is None:
            raise PersonReferenceAdvisoryError("brain_open_required")
        state = onboarding._state_row(connection, owner_id, model_id)
        if state is None or state["row_version"] != binding["row_version"]:
            raise PersonReferenceAdvisoryError("authoring_context_changed_retry")
        opened = connection.execute(
            "SELECT content_json FROM brain_onboarding_artifacts WHERE artifact_id=? "
            "AND owner_id=? AND model_id=? AND kind='brain_manual_opened' "
            "AND status='active' AND created_wake_id=?",
            (binding["write_context_ref"], owner_id, model_id, binding["wake_id"]),
        ).fetchone()
        try:
            opened_content = json.loads(opened["content_json"]) if opened is not None else None
        except (TypeError, json.JSONDecodeError):
            opened_content = None
        # Keep the actual runtime's mode string rather than deriving it from AI text.
        from .onboarding import DIRECT_CONTEXT_MODE
        direct = binding["context_mode"] == DIRECT_CONTEXT_MODE
        snapshot = connection.execute(
            "SELECT context_hash FROM brain_context_snapshots WHERE wake_id=? AND status=?",
            (binding["wake_id"], "prepared" if direct else "injected"),
        ).fetchone()
        if (not isinstance(opened_content, dict)
                or opened_content.get("opened_by") != ("stbrain_open_direct" if direct else "stbrain_open")
                or opened_content.get("context_hash") != binding["context_hash"]
                or wake["context_hash"] != binding["context_hash"]
                or snapshot is None or snapshot["context_hash"] != binding["context_hash"]
                or (not direct and wake["injected_at"] is None)):
            raise PersonReferenceAdvisoryError("brain_open_required")
        if direct:
            grant = connection.execute(
                "SELECT expires_at,scopes_json FROM brain_direct_grants WHERE opened_wake_id=? "
                "AND owner_id=? AND model_id=? AND status='consumed'",
                (binding["wake_id"], owner_id, model_id),
            ).fetchone()
            try:
                scopes = json.loads(grant["scopes_json"]) if grant is not None else None
                expires = datetime.fromisoformat(grant["expires_at"]) if grant is not None else None
            except (TypeError, ValueError, json.JSONDecodeError):
                scopes, expires = None, None
            if (not isinstance(scopes, list) or "shared_person_authoring" not in scopes
                    or opened_content.get("authorized_scopes") != scopes
                    or opened_content.get("context_mode") != DIRECT_CONTEXT_MODE
                    or expires is None or expires.tzinfo is None
                    or expires <= datetime.now(timezone.utc)):
                raise PersonReferenceAdvisoryError("brain_open_required")

    def manage(self, *, owner_id: str, model_id: str, action: str,
               text: str | None = None, write_context_ref: str = "",
               onboarding: Any = None) -> dict[str, Any]:
        owner_id, model_id = _identity(owner_id, "owner_id"), _identity(model_id, "model_id")
        expected_execution_wake(owner_id=owner_id, model_id=model_id)
        if not isinstance(action, str) or action not in {"set", "disable", "reset"}:
            raise PersonReferenceAdvisoryError("invalid_person_reference_advisory_action")
        if action == "set":
            if not isinstance(text, str) or not text.strip():
                raise PersonReferenceAdvisoryError("advisory_text_required")
            text = text.strip()
            if len(text) > 2000:
                raise PersonReferenceAdvisoryError("advisory_text_too_long_max_2000")
            if contains_credential_or_secret(text):
                raise PersonReferenceAdvisoryError("credential_or_secret_detected")
        elif text is not None:
            raise PersonReferenceAdvisoryError("text_only_allowed_for_set")
        ordinary = current_ordinary_access(
            owner_id=owner_id, model_id=model_id, scope="shared_person_authoring")
        if ordinary is not None:
            if write_context_ref and write_context_ref != ordinary["write_context_ref"]:
                raise PersonReferenceAdvisoryError("write_context_binding_mismatch")
            binding = ordinary
        else:
            if not isinstance(write_context_ref, str) or not write_context_ref.strip():
                raise PersonReferenceAdvisoryError("authenticated_authoring_context_required")
            binding = self._legacy_binding(
                onboarding=onboarding, owner_id=owner_id, model_id=model_id,
                write_context_ref=write_context_ref.strip())
        expected_execution_wake(owner_id=owner_id, model_id=model_id, explicit=binding["wake_id"])
        if onboarding is not None and onboarding.contains_protected_persistence_value(
                owner_id=owner_id, model_id=model_id, value=text):
            raise PersonReferenceAdvisoryError("credential_or_secret_detected")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            assert_bound_execution(connection)
            if ordinary is None:
                self._assert_legacy_still_current(
                    connection, onboarding=onboarding, owner_id=owner_id,
                    model_id=model_id, binding=binding)
            connection.execute(
                "INSERT INTO person_reference_advisory_history "
                "(owner_id,model_id,action,advisory_text,context_mode,wake_id,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (owner_id, model_id, action, text, binding["context_mode"], binding["wake_id"], _utc_now()),
            )
            result = self._projection(connection, owner_id, model_id)
        return {"decision": "saved", "state_changed": True, "authoring_advisory": result,
                "rewrite_assist_changed": False, "memory_content_changed": False}


__all__ = [
    "PERSON_REFERENCE_ADVISORY_DEFAULT", "PERSON_REFERENCE_ADVISORY_TABLE",
    "PERSON_REFERENCE_ADVISORY_SCHEMA", "PersonReferenceAdvisoryError",
    "PersonReferenceAdvisoryStore", "initialize_person_reference_advisory_schema",
]
