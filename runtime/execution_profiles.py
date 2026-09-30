"""Host-selected, wake-bound reductions of ST tool authority.

Profiles never grant a credential or replace normal tool authorization. Their
allowlists operate on canonical tools after compact envelopes are resolved.
Ordinary private chats retain the existing default behavior.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Mapping

DEFAULT_PROFILE = "default"
ACTIVE_PROFILE = "consultation-active-readonly"
ARCHIVING_PROFILE = "consultation-archiving"
CONSULTATION_PROFILES = (ACTIVE_PROFILE, ARCHIVING_PROFILE)
READ_TOOLS = frozenset({
    "read_memory_relations", "recall_work_memory", "query_self_model",
    "recall_emotional_memory", "recall_learning_memory", "preview_learning_recall",
    "recall_tool_guidance", "query_self_governance_profile",
    "query_injection_control", "recall_planning_memory",
})
ARCHIVE_WRITE_TOOLS = frozenset({"remember_work_memory", "revise_work_memory"})
PROFILE_SQL = """CREATE TABLE IF NOT EXISTS brain_wake_execution_profiles (
    wake_id TEXT PRIMARY KEY REFERENCES brain_wake_sessions(wake_id),
    execution_profile TEXT NOT NULL CHECK(execution_profile IN
        ('consultation-active-readonly','consultation-archiving'))
)"""


class ExecutionProfileError(ValueError):
    pass


def validate_profile(value: Any) -> str:
    if not isinstance(value, str) or value not in (DEFAULT_PROFILE, *CONSULTATION_PROFILES):
        raise ExecutionProfileError("execution_profile_invalid")
    return value


def initialize_profiles(connection: sqlite3.Connection) -> None:
    connection.execute(PROFILE_SQL)


def wake_profile(connection: sqlite3.Connection, wake_id: str) -> str:
    # Old snapshots have no table; an absent restriction is the legacy policy.
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                          "name='brain_wake_execution_profiles'").fetchone() is None:
        return DEFAULT_PROFILE
    row = connection.execute("SELECT execution_profile FROM brain_wake_execution_profiles "
                             "WHERE wake_id=?", (wake_id,)).fetchone()
    return DEFAULT_PROFILE if row is None else validate_profile(row[0])


def bind_new_wake(connection: sqlite3.Connection, wake_id: str, profile: str) -> None:
    validate_profile(profile)
    if profile != DEFAULT_PROFILE:
        connection.execute("INSERT INTO brain_wake_execution_profiles VALUES (?,?)", (wake_id, profile))


def profile_allows_tool(profile: str, canonical_tool: str,
                        arguments: Mapping[str, Any] | None = None) -> bool:
    validate_profile(profile)
    if profile == DEFAULT_PROFILE:
        return True
    if canonical_tool in READ_TOOLS:
        return True
    if canonical_tool == "stbrain_open":
        # Issuance sees only the argument hash; actual claim validates the view.
        return arguments is None or arguments.get("view") == "recall"
    return profile == ARCHIVING_PROFILE and canonical_tool in ARCHIVE_WRITE_TOOLS
