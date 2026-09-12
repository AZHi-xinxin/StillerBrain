"""Physically isolated, AI-owned hallucination quarantine for Stiller Brain.

The vault is deliberately absent from every automatic recall path.  It stores
AI-authored isolation decisions, not server truth verdicts.  The server checks
only structure, ownership, wake/CAS bindings and migration integrity.

Learning-card transfer uses a persistent saga instead of claiming that an
``ATTACH`` transaction is crash-atomic while the main database is in WAL mode.
At every durable intermediate phase the source is either still intact or hidden
behind a fail-closed redirect, and startup recovery can finish or roll back.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterator, Mapping, Protocol
from .credential_guard import contains_credential_or_secret
import uuid


HALLUCINATION_VAULT_CONTRACT_VERSION = "hallucination-vault/0.1"
HALLUCINATION_VAULT_MODULE = "hallucination_vault"
DEFAULT_AUTOMATIC_EXPOSURE = "hard_off"

_RECORD_LIFECYCLES = frozenset(
    {"staged", "quarantined", "restore_pending", "restored"}
)
_UNCERTAINTY_STATUSES = frozenset({"ai_isolated", "still_uncertain"})
_RESTORE_ACTIONS = frozenset({"activate", "reject", "withdraw"})
_FIRST_PERSON = re.compile(r"^\s*(?:我|I(?:\s|['’])|My(?:\s|$))", re.I)
_LEARNING_REF = re.compile(r"^learning://(?P<id>[A-Za-z0-9_-]+)@(?P<version>[1-9][0-9]*)$")


class HallucinationVaultError(ValueError):
    """Stable validation or transition failure."""


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now_dt()).isoformat(timespec="milliseconds")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _secure_text_equal(left: str, right: str) -> bool:
    """Constant-time comparison that also supports non-ASCII AI-authored text."""

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _text(name: str, value: Any, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise HallucinationVaultError(f"{name}_required")
    result = value.strip()
    if not result and not allow_empty:
        raise HallucinationVaultError(f"{name}_required")
    if len(result) > maximum:
        raise HallucinationVaultError(f"{name}_too_long")
    return result


def _integer(name: str, value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HallucinationVaultError(f"invalid_{name}")
    return value


def _true(name: str, value: Any) -> None:
    if value is not True:
        raise HallucinationVaultError(f"{name}_must_be_true")


def _contains_secret(value: Any) -> bool:
    return contains_credential_or_secret(value)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


@contextmanager
def _sqlite(database: str, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(database, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 20000")
    try:
        if immediate:
            connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


class QuarantineSourceAdapter(Protocol):
    """Adapter contract for a normal-memory module.

    Implementations return opaque row snapshots.  The vault never interprets a
    source module's semantic content or decides that it is false.
    """

    scheme: str

    def inspect(
        self, *, owner_id: str, model_id: str, source_ref: str
    ) -> dict[str, Any]: ...

    def detach(
        self,
        *,
        owner_id: str,
        model_id: str,
        snapshot: Mapping[str, Any],
        expected_row_version: int,
        vault_record_id: str,
    ) -> int: ...

    def restore(
        self,
        *,
        owner_id: str,
        model_id: str,
        snapshot: Mapping[str, Any],
        expected_row_version: int,
        vault_record_id: str,
    ) -> int: ...

    def source_present(
        self, *, owner_id: str, model_id: str, source_ref: str
    ) -> bool: ...

    def redirect_status(
        self, *, owner_id: str, model_id: str, source_ref: str
    ) -> str | None: ...

    def mark_redirect_committed(
        self, *, owner_id: str, model_id: str, source_ref: str
    ) -> None: ...


class LearningQuarantineAdapter:
    """Physical transfer adapter for current ``learning://...@version`` cards.

    The complete card, its versions/evidence/links and directly owned audit rows
    move into the vault snapshot.  A pending change or an integration that still
    depends on the card is rejected instead of being guessed through.
    """

    scheme = "learning"

    _TABLE_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("learning_evidence", ("evidence_id",)),
        ("learning_versions", ("version_id",)),
        ("learning_links", ("link_id",)),
        ("learning_integrations", ("candidate_id", "source_learning_id")),
        ("learning_change_candidates", ("candidate_id",)),
        ("learning_merge_suggestions", ("suggestion_id",)),
        ("learning_verification_events", ("event_id",)),
        ("learning_audit_events", ("event_seq",)),
        ("idempotency_records", ("operation", "idempotency_key")),
    )

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._initialize_redirects()

    def _initialize_redirects(self) -> None:
        with _sqlite(self.database) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_quarantine_redirects (
                    source_ref TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    source_module TEXT NOT NULL,
                    source_version INTEGER NOT NULL,
                    source_hash TEXT NOT NULL,
                    vault_record_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(status IN ('staging','committed','restored'))
                );
                CREATE INDEX IF NOT EXISTS idx_memory_redirect_owner
                    ON memory_quarantine_redirects(owner_id, model_id, status);
                """
            )

    @staticmethod
    def _parse_ref(source_ref: str) -> tuple[str, int]:
        match = _LEARNING_REF.fullmatch(source_ref)
        if match is None:
            raise HallucinationVaultError("unsupported_source_ref")
        return match.group("id"), int(match.group("version"))

    @staticmethod
    def _state(connection: sqlite3.Connection, owner_id: str, model_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM learning_module_state WHERE owner_id=? AND model_id=?",
            (owner_id, model_id),
        ).fetchone()
        if row is None:
            raise HallucinationVaultError("learning_state_not_found")
        return row

    @staticmethod
    def _fetch_all(
        connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        return [_row_dict(row) for row in connection.execute(sql, params).fetchall()]

    def inspect(
        self, *, owner_id: str, model_id: str, source_ref: str
    ) -> dict[str, Any]:
        learning_id, requested_version = self._parse_ref(source_ref)
        with _sqlite(self.database) as connection:
            state = self._state(connection, owner_id, model_id)
            redirect = connection.execute(
                "SELECT status, vault_record_id FROM memory_quarantine_redirects "
                "WHERE source_ref=? AND owner_id=? AND model_id=?",
                (source_ref, owner_id, model_id),
            ).fetchone()
            if redirect is not None and redirect["status"] in {"staging", "committed"}:
                raise HallucinationVaultError("source_already_quarantined")
            item = connection.execute(
                "SELECT * FROM learning_items WHERE learning_id=? AND owner_id=? AND model_id=?",
                (learning_id, owner_id, model_id),
            ).fetchone()
            if item is None:
                raise HallucinationVaultError("source_not_found")
            if int(item["current_version"]) != requested_version:
                raise HallucinationVaultError("source_version_conflict")
            pending = connection.execute(
                "SELECT COUNT(*) FROM learning_change_candidates "
                "WHERE owner_id=? AND model_id=? AND status='pending' AND "
                "(target_ref LIKE ? OR source_snapshot_json LIKE ? OR proposed_json LIKE ?)",
                (owner_id, model_id, f"learning://{learning_id}@%", f"%{learning_id}%", f"%{learning_id}%"),
            ).fetchone()[0]
            if pending:
                raise HallucinationVaultError("source_has_pending_change")
            dependent_integrations = connection.execute(
                "SELECT COUNT(*) FROM learning_integrations WHERE source_learning_id=?",
                (learning_id,),
            ).fetchone()[0]
            if dependent_integrations:
                raise HallucinationVaultError("source_has_integration_dependency")

            current = json.loads(item["current_json"])
            if not isinstance(current, dict):
                raise HallucinationVaultError("source_content_invalid")
            title = str(current.get("title") or f"学习卡 {learning_id}")[:200]
            summary = str(current.get("summary") or current.get("current_understanding") or "")[:500]
            ref_prefix = f"learning://{learning_id}@"
            candidates = self._fetch_all(
                connection,
                "SELECT * FROM learning_change_candidates WHERE owner_id=? AND model_id=? "
                "AND (target_ref LIKE ? OR source_snapshot_json LIKE ? OR proposed_json LIKE ?)",
                (owner_id, model_id, f"{ref_prefix}%", f"%{learning_id}%", f"%{learning_id}%"),
            )
            candidate_ids = [str(row["candidate_id"]) for row in candidates]
            integrations: list[dict[str, Any]] = []
            if candidate_ids:
                marks = ",".join("?" for _ in candidate_ids)
                integrations = self._fetch_all(
                    connection,
                    f"SELECT * FROM learning_integrations WHERE candidate_id IN ({marks})",
                    tuple(candidate_ids),
                )
            suggestion_rows = self._fetch_all(
                connection,
                "SELECT * FROM learning_merge_suggestions WHERE owner_id=? AND model_id=? "
                "AND member_refs_json LIKE ?",
                (owner_id, model_id, f"%{learning_id}%"),
            )
            snapshot = {
                "snapshot_version": "learning-quarantine/0.1",
                "source_ref": source_ref,
                "source_module": "learning_memory",
                "source_version": requested_version,
                "module_row_version": int(state["row_version"]),
                "learning_id": learning_id,
                "item": _row_dict(item),
                "learning_evidence": self._fetch_all(
                    connection, "SELECT * FROM learning_evidence WHERE learning_id=?", (learning_id,)
                ),
                "learning_versions": self._fetch_all(
                    connection, "SELECT * FROM learning_versions WHERE learning_id=?", (learning_id,)
                ),
                "learning_links": self._fetch_all(
                    connection,
                    "SELECT * FROM learning_links WHERE owner_id=? AND model_id=? "
                    "AND (from_ref LIKE ? OR to_ref LIKE ?)",
                    (owner_id, model_id, f"{ref_prefix}%", f"{ref_prefix}%"),
                ),
                "learning_integrations": integrations,
                "learning_change_candidates": candidates,
                "learning_merge_suggestions": suggestion_rows,
                "learning_verification_events": self._fetch_all(
                    connection,
                    "SELECT * FROM learning_verification_events WHERE owner_id=? AND model_id=? "
                    "AND learning_ref LIKE ?",
                    (owner_id, model_id, f"{ref_prefix}%"),
                ),
                "learning_audit_events": self._fetch_all(
                    connection,
                    "SELECT * FROM learning_audit_events WHERE owner_id=? AND model_id=? AND learning_id=?",
                    (owner_id, model_id, learning_id),
                ),
                "idempotency_records": self._fetch_all(
                    connection,
                    "SELECT * FROM idempotency_records WHERE response_json LIKE ?",
                    (f"%{learning_id}%",),
                ),
            }
            snapshot_hash = _sha256(snapshot)
            return {
                "source_ref": source_ref,
                "source_module": "learning_memory",
                "source_version": requested_version,
                "source_title": title,
                "source_summary": summary,
                "source_hash": snapshot_hash,
                "source_row_version": int(state["row_version"]),
                "attachment_count": 0,
                "protected": False,
                "snapshot": snapshot,
            }

    @staticmethod
    def _delete_rows(
        connection: sqlite3.Connection,
        table: str,
        keys: tuple[str, ...],
        rows: list[Mapping[str, Any]],
    ) -> None:
        if not rows:
            return
        clause = " AND ".join(f"{key}=?" for key in keys)
        for row in rows:
            connection.execute(
                f"DELETE FROM {table} WHERE {clause}",
                tuple(row[key] for key in keys),
            )

    @staticmethod
    def _insert_rows(
        connection: sqlite3.Connection, table: str, rows: list[Mapping[str, Any]]
    ) -> None:
        for row in rows:
            columns = list(row)
            connection.execute(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES "
                f"({','.join('?' for _ in columns)})",
                tuple(row[column] for column in columns),
            )

    def detach(
        self,
        *,
        owner_id: str,
        model_id: str,
        snapshot: Mapping[str, Any],
        expected_row_version: int,
        vault_record_id: str,
    ) -> int:
        source_ref = str(snapshot.get("source_ref") or "")
        inspected = self.inspect(owner_id=owner_id, model_id=model_id, source_ref=source_ref)
        if inspected["source_hash"] != _sha256(snapshot):
            raise HallucinationVaultError("source_changed_since_preview")
        if inspected["source_row_version"] != expected_row_version:
            raise HallucinationVaultError("source_row_version_conflict")
        learning_id = str(snapshot["learning_id"])
        now = _iso()
        with _sqlite(self.database, immediate=True) as connection:
            state = self._state(connection, owner_id, model_id)
            if int(state["row_version"]) != expected_row_version:
                raise HallucinationVaultError("source_row_version_conflict")
            item = connection.execute(
                "SELECT current_hash, current_version FROM learning_items "
                "WHERE learning_id=? AND owner_id=? AND model_id=?",
                (learning_id, owner_id, model_id),
            ).fetchone()
            if item is None:
                raise HallucinationVaultError("source_not_found")
            connection.execute(
                "INSERT INTO memory_quarantine_redirects "
                "(source_ref, owner_id, model_id, source_module, source_version, source_hash, "
                "vault_record_id, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,'staging',?,?)",
                (
                    source_ref,
                    owner_id,
                    model_id,
                    "learning_memory",
                    int(snapshot["source_version"]),
                    _sha256(snapshot),
                    vault_record_id,
                    now,
                    now,
                ),
            )
            for table, keys in self._TABLE_KEYS:
                self._delete_rows(connection, table, keys, list(snapshot.get(table, [])))
            connection.execute("DELETE FROM learning_items WHERE learning_id=?", (learning_id,))
            cursor = connection.execute(
                "UPDATE learning_module_state SET row_version=row_version+1, updated_at=? "
                "WHERE owner_id=? AND model_id=? AND row_version=?",
                (now, owner_id, model_id, expected_row_version),
            )
            if cursor.rowcount != 1:
                raise HallucinationVaultError("source_row_version_conflict")
        return expected_row_version + 1

    def restore(
        self,
        *,
        owner_id: str,
        model_id: str,
        snapshot: Mapping[str, Any],
        expected_row_version: int,
        vault_record_id: str,
    ) -> int:
        source_ref = str(snapshot.get("source_ref") or "")
        learning_id = str(snapshot.get("learning_id") or "")
        if not source_ref or not learning_id:
            raise HallucinationVaultError("source_snapshot_invalid")
        now = _iso()
        try:
            with _sqlite(self.database, immediate=True) as connection:
                state = self._state(connection, owner_id, model_id)
                if int(state["row_version"]) != expected_row_version:
                    raise HallucinationVaultError("destination_row_version_conflict")
                redirect = connection.execute(
                    "SELECT * FROM memory_quarantine_redirects WHERE source_ref=? "
                    "AND owner_id=? AND model_id=? AND vault_record_id=?",
                    (source_ref, owner_id, model_id, vault_record_id),
                ).fetchone()
                if redirect is None or redirect["status"] not in {"staging", "committed"}:
                    raise HallucinationVaultError("quarantine_redirect_not_found")
                if connection.execute(
                    "SELECT 1 FROM learning_items WHERE learning_id=?", (learning_id,)
                ).fetchone() is not None:
                    raise HallucinationVaultError("destination_source_conflict")
                self._insert_rows(connection, "learning_items", [dict(snapshot["item"])])
                # Parents first, then dependent and relation rows. A collision in
                # any audit/idempotency key aborts the entire primary transaction;
                # existing history is never overwritten or silently renamed.
                for table in (
                    "learning_evidence",
                    "learning_versions",
                    "learning_change_candidates",
                    "learning_integrations",
                    "learning_links",
                    "learning_merge_suggestions",
                    "learning_verification_events",
                    "learning_audit_events",
                    "idempotency_records",
                ):
                    self._insert_rows(connection, table, list(snapshot.get(table, [])))
                connection.execute(
                    "UPDATE memory_quarantine_redirects SET status='restored', updated_at=? "
                    "WHERE source_ref=?",
                    (now, source_ref),
                )
                cursor = connection.execute(
                    "UPDATE learning_module_state SET row_version=row_version+1, updated_at=? "
                    "WHERE owner_id=? AND model_id=? AND row_version=?",
                    (now, owner_id, model_id, expected_row_version),
                )
                if cursor.rowcount != 1:
                    raise HallucinationVaultError("destination_row_version_conflict")
        except sqlite3.IntegrityError as exc:
            raise HallucinationVaultError("restore_snapshot_conflict") from exc
        return expected_row_version + 1

    def source_present(
        self, *, owner_id: str, model_id: str, source_ref: str
    ) -> bool:
        learning_id, _ = self._parse_ref(source_ref)
        with _sqlite(self.database) as connection:
            return connection.execute(
                "SELECT 1 FROM learning_items WHERE learning_id=? AND owner_id=? AND model_id=?",
                (learning_id, owner_id, model_id),
            ).fetchone() is not None

    def redirect_status(
        self, *, owner_id: str, model_id: str, source_ref: str
    ) -> str | None:
        with _sqlite(self.database) as connection:
            row = connection.execute(
                "SELECT status FROM memory_quarantine_redirects WHERE source_ref=? "
                "AND owner_id=? AND model_id=?",
                (source_ref, owner_id, model_id),
            ).fetchone()
            return None if row is None else str(row["status"])

    def mark_redirect_committed(
        self, *, owner_id: str, model_id: str, source_ref: str
    ) -> None:
        with _sqlite(self.database, immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE memory_quarantine_redirects SET status='committed', updated_at=? "
                "WHERE source_ref=? AND owner_id=? AND model_id=? AND status='staging'",
                (_iso(), source_ref, owner_id, model_id),
            )
            if cursor.rowcount == 0:
                status = connection.execute(
                    "SELECT status FROM memory_quarantine_redirects WHERE source_ref=? "
                    "AND owner_id=? AND model_id=?",
                    (source_ref, owner_id, model_id),
                ).fetchone()
                if status is None or status["status"] != "committed":
                    raise HallucinationVaultError("quarantine_redirect_commit_conflict")


class HallucinationVaultStore:
    """Independent vault with no automatic-recall method by design."""

    def __init__(
        self,
        database: str | Path,
        *,
        source_adapters: Mapping[str, QuarantineSourceAdapter] | None = None,
        transfer_preview_ttl_seconds: int = 900,
    ) -> None:
        if transfer_preview_ttl_seconds < 60:
            raise ValueError("transfer_preview_ttl_seconds must be at least 60")
        self.database = str(database)
        self.transfer_preview_ttl_seconds = transfer_preview_ttl_seconds
        self.source_adapters = dict(source_adapters or {})
        self._operation_lock = threading.RLock()
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.recover_incomplete_transfers()

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with _sqlite(self.database, immediate=immediate) as connection:
            yield connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS vault_owner_state (
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    automatic_exposure TEXT NOT NULL DEFAULT 'hard_off',
                    warning_text TEXT,
                    warning_suffix TEXT,
                    warning_version INTEGER NOT NULL DEFAULT 0,
                    row_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id, model_id),
                    CHECK(automatic_exposure IN ('hard_off','status_only'))
                );
                CREATE TABLE IF NOT EXISTS vault_records (
                    record_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    neutral_title TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    uncertainty_status TEXT NOT NULL,
                    current_version INTEGER NOT NULL,
                    current_json TEXT NOT NULL,
                    current_hash TEXT NOT NULL,
                    source_module TEXT,
                    source_ref TEXT,
                    source_version INTEGER,
                    source_hash TEXT,
                    created_wake_id TEXT NOT NULL,
                    created_wake_seq INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(lifecycle IN ('staged','quarantined','restore_pending','restored')),
                    CHECK(review_status IN ('unreviewed','reviewed')),
                    CHECK(uncertainty_status IN ('ai_isolated','still_uncertain')),
                    UNIQUE(owner_id, model_id, source_ref)
                );
                CREATE TABLE IF NOT EXISTS vault_record_versions (
                    version_id TEXT PRIMARY KEY,
                    record_id TEXT NOT NULL REFERENCES vault_records(record_id),
                    version INTEGER NOT NULL,
                    content_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    wake_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(record_id, version)
                );
                CREATE TABLE IF NOT EXISTS vault_source_snapshots (
                    record_id TEXT PRIMARY KEY REFERENCES vault_records(record_id),
                    source_module TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    source_version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vault_transfer_journals (
                    journal_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    source_module TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    source_version INTEGER NOT NULL,
                    source_row_version INTEGER NOT NULL,
                    source_hash TEXT NOT NULL,
                    source_snapshot_json TEXT,
                    preview_hash TEXT NOT NULL,
                    record_id TEXT,
                    record_content_json TEXT,
                    pending_warning_text TEXT,
                    pending_warning_suffix TEXT,
                    expected_vault_version INTEGER,
                    destination_row_version INTEGER,
                    created_wake_id TEXT,
                    created_wake_seq INTEGER,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(direction IN ('quarantine','restore')),
                    CHECK(phase IN ('preview','vault_staged','source_detached','committed','rolled_back'))
                );
                CREATE TABLE IF NOT EXISTS vault_restore_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    record_id TEXT NOT NULL REFERENCES vault_records(record_id),
                    candidate_hash TEXT NOT NULL,
                    candidate_version INTEGER NOT NULL,
                    base_record_version INTEGER NOT NULL,
                    destination_module TEXT NOT NULL,
                    destination_row_version INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_wake_id TEXT NOT NULL,
                    created_wake_seq INTEGER NOT NULL,
                    presented_wake_id TEXT,
                    presented_wake_seq INTEGER,
                    presented_candidate_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(status IN ('pending','activating','activated','rejected','withdrawn'))
                );
                CREATE TABLE IF NOT EXISTS vault_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    record_id TEXT,
                    candidate_id TEXT,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL CHECK(actor IN ('ai','system')),
                    wake_id TEXT,
                    decision TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    details_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vault_idempotency (
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(operation, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_vault_records_owner
                    ON vault_records(owner_id, model_id, lifecycle, created_at);
                CREATE INDEX IF NOT EXISTS idx_vault_candidates_owner
                    ON vault_restore_candidates(owner_id, model_id, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_vault_journals_phase
                    ON vault_transfer_journals(direction, phase, updated_at);
                CREATE INDEX IF NOT EXISTS idx_vault_events_owner
                    ON vault_events(owner_id, model_id, event_seq);
                """
            )

    @staticmethod
    def _identity(owner_id: Any, model_id: Any) -> tuple[str, str]:
        return _text("owner_id", owner_id, 300), _text("model_id", model_id, 300)

    def ensure_state(self, *, owner_id: str, model_id: str) -> None:
        owner_id, model_id = self._identity(owner_id, model_id)
        now = _iso()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO vault_owner_state "
                "(owner_id, model_id, automatic_exposure, warning_text, warning_suffix, "
                "warning_version, row_version, created_at, updated_at) "
                "VALUES (?,?,'hard_off',NULL,NULL,0,0,?,?)",
                (owner_id, model_id, now, now),
            )

    @staticmethod
    def _state(
        connection: sqlite3.Connection, owner_id: str, model_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM vault_owner_state WHERE owner_id=? AND model_id=?",
            (owner_id, model_id),
        ).fetchone()
        if row is None:
            raise HallucinationVaultError("vault_state_not_found")
        return row

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        action: str,
        decision: str,
        wake_id: str | None,
        record_id: str | None = None,
        candidate_id: str | None = None,
        actor: str = "ai",
        details: Mapping[str, Any] | None = None,
    ) -> str:
        event_id = _new_id("hvevt")
        payload = dict(details or {})
        connection.execute(
            "INSERT INTO vault_events "
            "(event_id, owner_id, model_id, record_id, candidate_id, action, actor, wake_id, "
            "decision, details_json, details_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                owner_id,
                model_id,
                record_id,
                candidate_id,
                action,
                actor,
                wake_id,
                decision,
                _canonical(payload),
                _sha256(payload),
                _iso(),
            ),
        )
        return event_id

    @staticmethod
    def _validate_warning(warning_text: Any, warning_suffix: Any) -> tuple[str, str]:
        text = _text("warning_text", warning_text, 2000)
        suffix = _text("warning_suffix", warning_suffix, 1000)
        if _FIRST_PERSON.match(text) is None or _FIRST_PERSON.match(suffix) is None:
            raise HallucinationVaultError("warning_must_be_ai_first_person")
        if _contains_secret({"warning_text": text, "warning_suffix": suffix}):
            raise HallucinationVaultError("credential_or_secret_detected")
        return text, suffix

    @staticmethod
    def _validate_content(
        *,
        neutral_title: Any,
        isolated_content: Any,
        current_account: Any,
        basis: Any,
        reflection: Any = "",
        uncertainty_status: Any = "ai_isolated",
    ) -> dict[str, Any]:
        if uncertainty_status not in _UNCERTAINTY_STATUSES:
            raise HallucinationVaultError("invalid_uncertainty_status")
        content = {
            "schema_version": "0.1.0",
            "neutral_title": _text("neutral_title", neutral_title, 200),
            "isolated_content": _text("isolated_content", isolated_content, 20_000),
            "current_account": _text("current_account", current_account, 20_000),
            "basis": _text("basis", basis, 10_000),
            "reflection": _text("reflection", reflection, 20_000, allow_empty=True),
            "uncertainty_status": str(uncertainty_status),
        }
        if _contains_secret(content):
            raise HallucinationVaultError("credential_or_secret_detected")
        return content

    def status(self, *, owner_id: str, model_id: str) -> dict[str, Any]:
        owner_id, model_id = self._identity(owner_id, model_id)
        with self._connect() as connection:
            state = connection.execute(
                "SELECT * FROM vault_owner_state WHERE owner_id=? AND model_id=?",
                (owner_id, model_id),
            ).fetchone()
            counts = {
                lifecycle: int(
                    connection.execute(
                        "SELECT COUNT(*) FROM vault_records WHERE owner_id=? AND model_id=? "
                        "AND lifecycle=?",
                        (owner_id, model_id, lifecycle),
                    ).fetchone()[0]
                )
                for lifecycle in ("quarantined", "restore_pending", "restored")
            }
            counts["pending_restore_candidates"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM vault_restore_candidates WHERE owner_id=? "
                    "AND model_id=? AND status='pending'",
                    (owner_id, model_id),
                ).fetchone()[0]
            )
            return {
                "contract_version": HALLUCINATION_VAULT_CONTRACT_VERSION,
                "status": "available",
                # A fresh vault has an implicit, content-free hard-off state.
                # Health/status probes must remain genuinely read-only; the
                # first mutation will materialize this row through ensure_state.
                "automatic_exposure": (
                    state["automatic_exposure"] if state is not None else DEFAULT_AUTOMATIC_EXPOSURE
                ),
                "automatic_injection": False,
                "warning_configured": bool(state["warning_text"]) if state is not None else False,
                "warning_version": int(state["warning_version"]) if state is not None else 0,
                "row_version": int(state["row_version"]) if state is not None else 0,
                "counts": counts,
                "content_exposed": False,
            }

    def hold(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        neutral_title: Any,
        isolated_content: Any,
        current_account: Any,
        basis: Any,
        reflection: Any = "",
        uncertainty_status: Any = "ai_isolated",
        reason: Any,
        ai_confirmation: Any,
        warning_text: Any = None,
        warning_suffix: Any = None,
    ) -> dict[str, Any]:
        owner_id, model_id = self._identity(owner_id, model_id)
        wake_id = _text("wake_id", wake_id, 300)
        wake_seq = _integer("wake_seq", wake_seq, minimum=1)
        expected_row_version = _integer("expected_row_version", expected_row_version)
        _true("ai_confirmation", ai_confirmation)
        reason_text = _text("reason", reason, 2000)
        content = self._validate_content(
            neutral_title=neutral_title,
            isolated_content=isolated_content,
            current_account=current_account,
            basis=basis,
            reflection=reflection,
            uncertainty_status=uncertainty_status,
        )
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._operation_lock, self._connect(immediate=True) as connection:
            state = self._state(connection, owner_id, model_id)
            if int(state["row_version"]) != expected_row_version:
                raise HallucinationVaultError("vault_row_version_conflict")
            setup = not bool(state["warning_text"])
            if setup:
                if warning_text is None or warning_suffix is None:
                    return {
                        "decision": "setup_required",
                        "reason_codes": ["ai_authored_warning_required"],
                        "record_persisted": False,
                        "vault_row_version": int(state["row_version"]),
                        "state_changed": False,
                    }
                warning, suffix = self._validate_warning(warning_text, warning_suffix)
            else:
                if warning_text is not None or warning_suffix is not None:
                    raise HallucinationVaultError("warning_already_configured")
                warning, suffix = str(state["warning_text"]), str(state["warning_suffix"])
            record_id = _new_id("hvrec")
            version_id = _new_id("hvver")
            now = _iso()
            content_hash = _sha256(content)
            connection.execute(
                "INSERT INTO vault_records "
                "(record_id, owner_id, model_id, neutral_title, lifecycle, review_status, "
                "uncertainty_status, current_version, current_json, current_hash, source_module, "
                "source_ref, source_version, source_hash, created_wake_id, created_wake_seq, "
                "created_at, updated_at) VALUES (?,?,?,?,'quarantined','unreviewed',?,1,?,?,"
                "NULL,NULL,NULL,NULL,?,?,?,?)",
                (
                    record_id,
                    owner_id,
                    model_id,
                    content["neutral_title"],
                    content["uncertainty_status"],
                    _canonical(content),
                    content_hash,
                    wake_id,
                    wake_seq,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO vault_record_versions "
                "(version_id, record_id, version, content_json, content_hash, reason, wake_id, created_at) "
                "VALUES (?,?,1,?,?,?,?,?)",
                (version_id, record_id, _canonical(content), content_hash, reason_text, wake_id, now),
            )
            if setup:
                connection.execute(
                    "UPDATE vault_owner_state SET warning_text=?, warning_suffix=?, "
                    "warning_version=1, row_version=row_version+1, updated_at=? "
                    "WHERE owner_id=? AND model_id=? AND row_version=?",
                    (warning, suffix, now, owner_id, model_id, expected_row_version),
                )
            else:
                connection.execute(
                    "UPDATE vault_owner_state SET row_version=row_version+1, updated_at=? "
                    "WHERE owner_id=? AND model_id=? AND row_version=?",
                    (now, owner_id, model_id, expected_row_version),
                )
            event_id = self._insert_event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                record_id=record_id,
                action="hold",
                decision="stored_quarantined",
                wake_id=wake_id,
                details={"setup_applied": setup, "content_hash": content_hash},
            )
            return {
                "decision": "stored_quarantined",
                "record_id": record_id,
                "lifecycle": "quarantined",
                "automatic_recall_eligible": False,
                "vault_row_version": expected_row_version + 1,
                "warning_version": 1 if setup else int(state["warning_version"]),
                "event_id": event_id,
                "state_changed": True,
            }

    def update_warning(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        expected_row_version: int,
        expected_warning_version: int,
        warning_text: Any,
        warning_suffix: Any,
        reason: Any,
        ai_confirmation: Any,
    ) -> dict[str, Any]:
        owner_id, model_id = self._identity(owner_id, model_id)
        _true("ai_confirmation", ai_confirmation)
        warning, suffix = self._validate_warning(warning_text, warning_suffix)
        reason_text = _text("reason", reason, 2000)
        expected_row_version = _integer("expected_row_version", expected_row_version)
        expected_warning_version = _integer("expected_warning_version", expected_warning_version)
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect(immediate=True) as connection:
            state = self._state(connection, owner_id, model_id)
            if int(state["row_version"]) != expected_row_version:
                raise HallucinationVaultError("vault_row_version_conflict")
            if int(state["warning_version"]) != expected_warning_version:
                raise HallucinationVaultError("warning_version_conflict")
            if not state["warning_text"]:
                raise HallucinationVaultError("warning_not_configured")
            now = _iso()
            connection.execute(
                "UPDATE vault_owner_state SET warning_text=?, warning_suffix=?, "
                "warning_version=warning_version+1, row_version=row_version+1, updated_at=? "
                "WHERE owner_id=? AND model_id=? AND row_version=?",
                (warning, suffix, now, owner_id, model_id, expected_row_version),
            )
            event_id = self._insert_event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                action="update_warning",
                decision="updated",
                wake_id=wake_id,
                details={"reason_hash": _sha256(reason_text)},
            )
            return {
                "decision": "warning_updated",
                "warning_version": expected_warning_version + 1,
                "vault_row_version": expected_row_version + 1,
                "event_id": event_id,
                "state_changed": True,
            }

    def open(
        self,
        *,
        owner_id: str,
        model_id: str,
        record_id: str | None = None,
        warning_confirmation: str | None = None,
        expected_warning_version: int | None = None,
        offset: int = 0,
        limit: int = 50,
        wake_id: str | None = None,
        wake_seq: int | None = None,
    ) -> dict[str, Any]:
        owner_id, model_id = self._identity(owner_id, model_id)
        offset = _integer("offset", offset)
        limit = _integer("limit", limit, minimum=1)
        if limit > 200:
            raise HallucinationVaultError("limit_too_large")
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect(immediate=bool(record_id and wake_id)) as connection:
            state = self._state(connection, owner_id, model_id)
            if record_id is None:
                total = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM vault_records WHERE owner_id=? AND model_id=? "
                        "AND lifecycle!='staged'",
                        (owner_id, model_id),
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    "SELECT record_id, neutral_title, created_at, lifecycle, review_status, "
                    "uncertainty_status FROM vault_records WHERE owner_id=? AND model_id=? "
                    "AND lifecycle!='staged' ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (owner_id, model_id, limit, offset),
                ).fetchall()
                return {
                    "decision": "directory",
                    "automatic_injection": False,
                    "count": len(rows),
                    "total": total,
                    "offset": offset,
                    "limit": limit,
                    "entries": [_row_dict(row) for row in rows],
                    "content_exposed": False,
                }
            record_id = _text("record_id", record_id, 200)
            row = connection.execute(
                "SELECT * FROM vault_records WHERE record_id=? AND owner_id=? AND model_id=? "
                "AND lifecycle!='staged'",
                (record_id, owner_id, model_id),
            ).fetchone()
            if row is None:
                raise HallucinationVaultError("record_not_found")
            if not state["warning_text"]:
                raise HallucinationVaultError("warning_not_configured")
            if warning_confirmation is None or expected_warning_version is None:
                return {
                    "decision": "warning_confirmation_required",
                    "record_id": record_id,
                    "warning_text": state["warning_text"],
                    "warning_version": int(state["warning_version"]),
                    "content_exposed": False,
                }
            if _integer("expected_warning_version", expected_warning_version) != int(
                state["warning_version"]
            ):
                raise HallucinationVaultError("warning_version_conflict")
            if not isinstance(warning_confirmation, str) or not _secure_text_equal(
                warning_confirmation, str(state["warning_text"])
            ):
                raise HallucinationVaultError("warning_confirmation_mismatch")
            source_snapshot = connection.execute(
                "SELECT source_module, source_ref, source_version, snapshot_json, snapshot_hash "
                "FROM vault_source_snapshots WHERE record_id=?",
                (record_id,),
            ).fetchone()
            if wake_id is not None and wake_seq is not None:
                current_wake_id = _text("wake_id", wake_id, 300)
                current_wake_seq = _integer("wake_seq", wake_seq, minimum=1)
                connection.execute(
                    "UPDATE vault_restore_candidates SET presented_wake_id=?, presented_wake_seq=?, "
                    "presented_candidate_hash=candidate_hash, updated_at=? WHERE owner_id=? AND model_id=? "
                    "AND record_id=? AND status='pending' AND created_wake_seq < ?",
                    (
                        current_wake_id,
                        current_wake_seq,
                        _iso(),
                        owner_id,
                        model_id,
                        record_id,
                        current_wake_seq,
                    ),
                )
            content = json.loads(row["current_json"])
            return {
                "decision": "record_opened",
                "record_id": record_id,
                "lifecycle": row["lifecycle"],
                "review_status": row["review_status"],
                "uncertainty_status": row["uncertainty_status"],
                "version": int(row["current_version"]),
                "content": content,
                "source_snapshot": (
                    {
                        "source_module": source_snapshot["source_module"],
                        "source_ref": source_snapshot["source_ref"],
                        "source_version": source_snapshot["source_version"],
                        "snapshot": json.loads(source_snapshot["snapshot_json"]),
                        "snapshot_hash": source_snapshot["snapshot_hash"],
                    }
                    if source_snapshot is not None
                    else None
                ),
                "warning_suffix": state["warning_suffix"],
                "content_exposed": True,
            }

    def _adapter(self, source_ref: str) -> QuarantineSourceAdapter:
        if "://" not in source_ref:
            raise HallucinationVaultError("unsupported_source_ref")
        scheme = source_ref.split("://", 1)[0]
        adapter = self.source_adapters.get(scheme)
        if adapter is None:
            raise HallucinationVaultError("unsupported_source_module")
        return adapter

    def preview_transfer(
        self,
        *,
        owner_id: str,
        model_id: str,
        source_ref: Any,
        expected_source_row_version: int,
    ) -> dict[str, Any]:
        owner_id, model_id = self._identity(owner_id, model_id)
        source_ref = _text("source_ref", source_ref, 500)
        expected_source_row_version = _integer(
            "expected_source_row_version", expected_source_row_version
        )
        adapter = self._adapter(source_ref)
        inspected = adapter.inspect(owner_id=owner_id, model_id=model_id, source_ref=source_ref)
        if inspected["source_row_version"] != expected_source_row_version:
            raise HallucinationVaultError("source_row_version_conflict")
        if int(inspected.get("attachment_count", 0)):
            raise HallucinationVaultError("attachments_not_supported")
        if _contains_secret(inspected["snapshot"]):
            raise HallucinationVaultError("credential_or_secret_detected_in_source")
        journal_id = _new_id("hvxfer")
        expires_at = _iso(_now_dt() + timedelta(seconds=self.transfer_preview_ttl_seconds))
        preview_payload = {
            "journal_id": journal_id,
            "source_ref": source_ref,
            "source_version": inspected["source_version"],
            "source_hash": inspected["source_hash"],
            "source_row_version": inspected["source_row_version"],
            "source_title": inspected["source_title"],
            "source_summary": inspected["source_summary"],
            "attachment_count": inspected["attachment_count"],
            "protected": bool(inspected.get("protected")),
            "expires_at": expires_at,
        }
        preview_hash = _sha256(preview_payload)
        now = _iso()
        with self._connect(immediate=True) as connection:
            connection.execute(
                "INSERT INTO vault_transfer_journals "
                "(journal_id, owner_id, model_id, direction, phase, source_module, source_ref, "
                "source_version, source_row_version, source_hash, source_snapshot_json, preview_hash, "
                "record_id, record_content_json, pending_warning_text, pending_warning_suffix, "
                "expected_vault_version, destination_row_version, created_wake_id, created_wake_seq, "
                "expires_at, created_at, updated_at) VALUES "
                "(?,?,?,'quarantine','preview',?,?,?,?,?,NULL,?,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,?,?,?)",
                (
                    journal_id,
                    owner_id,
                    model_id,
                    inspected["source_module"],
                    source_ref,
                    int(inspected["source_version"]),
                    int(inspected["source_row_version"]),
                    inspected["source_hash"],
                    preview_hash,
                    expires_at,
                    now,
                    now,
                ),
            )
        return {
            "decision": "transfer_preview",
            **preview_payload,
            "preview_hash": preview_hash,
            "state_changed": False,
        }

    def commit_transfer(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        expected_source_row_version: int,
        journal_id: Any,
        expected_preview_hash: Any,
        neutral_title: Any,
        isolated_content: Any,
        current_account: Any,
        basis: Any,
        reflection: Any = "",
        uncertainty_status: Any = "ai_isolated",
        reason: Any,
        ai_confirmation: Any,
        warning_text: Any = None,
        warning_suffix: Any = None,
    ) -> dict[str, Any]:
        owner_id, model_id = self._identity(owner_id, model_id)
        wake_id = _text("wake_id", wake_id, 300)
        wake_seq = _integer("wake_seq", wake_seq, minimum=1)
        expected_row_version = _integer("expected_row_version", expected_row_version)
        expected_source_row_version = _integer(
            "expected_source_row_version", expected_source_row_version
        )
        journal_id = _text("journal_id", journal_id, 200)
        expected_preview_hash = _text("expected_preview_hash", expected_preview_hash, 128)
        _true("ai_confirmation", ai_confirmation)
        reason_text = _text("reason", reason, 2000)
        content = self._validate_content(
            neutral_title=neutral_title,
            isolated_content=isolated_content,
            current_account=current_account,
            basis=basis,
            reflection=reflection,
            uncertainty_status=uncertainty_status,
        )
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._operation_lock:
            with self._connect() as connection:
                journal = connection.execute(
                    "SELECT * FROM vault_transfer_journals WHERE journal_id=? AND owner_id=? AND model_id=?",
                    (journal_id, owner_id, model_id),
                ).fetchone()
                if journal is None:
                    raise HallucinationVaultError("transfer_preview_not_found")
                if journal["phase"] != "preview":
                    raise HallucinationVaultError("transfer_preview_already_used")
                if _parse_iso(journal["expires_at"]) <= _now_dt():
                    raise HallucinationVaultError("transfer_preview_expired")
                if not _secure_text_equal(str(journal["preview_hash"]), expected_preview_hash):
                    raise HallucinationVaultError("transfer_preview_hash_mismatch")
                source_ref = str(journal["source_ref"])
                source_hash = str(journal["source_hash"])
            adapter = self._adapter(source_ref)
            inspected = adapter.inspect(owner_id=owner_id, model_id=model_id, source_ref=source_ref)
            if inspected["source_hash"] != source_hash:
                raise HallucinationVaultError("source_changed_since_preview")
            if inspected["source_row_version"] != expected_source_row_version:
                raise HallucinationVaultError("source_row_version_conflict")
            if _contains_secret(inspected["snapshot"]):
                raise HallucinationVaultError("credential_or_secret_detected_in_source")
            record_id = _new_id("hvrec")
            version_id = _new_id("hvver")
            now = _iso()
            content_hash = _sha256(content)
            with self._connect(immediate=True) as connection:
                state = self._state(connection, owner_id, model_id)
                if int(state["row_version"]) != expected_row_version:
                    raise HallucinationVaultError("vault_row_version_conflict")
                setup = not bool(state["warning_text"])
                if setup:
                    if warning_text is None or warning_suffix is None:
                        raise HallucinationVaultError("ai_authored_warning_required")
                    pending_warning, pending_suffix = self._validate_warning(
                        warning_text, warning_suffix
                    )
                else:
                    if warning_text is not None or warning_suffix is not None:
                        raise HallucinationVaultError("warning_already_configured")
                    pending_warning = pending_suffix = None
                connection.execute(
                    "INSERT INTO vault_records "
                    "(record_id, owner_id, model_id, neutral_title, lifecycle, review_status, "
                    "uncertainty_status, current_version, current_json, current_hash, source_module, "
                    "source_ref, source_version, source_hash, created_wake_id, created_wake_seq, "
                    "created_at, updated_at) VALUES (?,?,?,?,'staged','unreviewed',?,1,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record_id,
                        owner_id,
                        model_id,
                        content["neutral_title"],
                        content["uncertainty_status"],
                        _canonical(content),
                        content_hash,
                        inspected["source_module"],
                        source_ref,
                        int(inspected["source_version"]),
                        inspected["source_hash"],
                        wake_id,
                        wake_seq,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO vault_record_versions "
                    "(version_id, record_id, version, content_json, content_hash, reason, wake_id, created_at) "
                    "VALUES (?,?,1,?,?,?,?,?)",
                    (version_id, record_id, _canonical(content), content_hash, reason_text, wake_id, now),
                )
                connection.execute(
                    "INSERT INTO vault_source_snapshots "
                    "(record_id, source_module, source_ref, source_version, snapshot_json, snapshot_hash, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        record_id,
                        inspected["source_module"],
                        source_ref,
                        int(inspected["source_version"]),
                        _canonical(inspected["snapshot"]),
                        inspected["source_hash"],
                        now,
                    ),
                )
                cursor = connection.execute(
                    "UPDATE vault_transfer_journals SET phase='vault_staged', source_snapshot_json=?, "
                    "record_id=?, record_content_json=?, pending_warning_text=?, pending_warning_suffix=?, "
                    "expected_vault_version=?, created_wake_id=?, created_wake_seq=?, updated_at=? "
                    "WHERE journal_id=? AND phase='preview'",
                    (
                        _canonical(inspected["snapshot"]),
                        record_id,
                        _canonical(content),
                        pending_warning,
                        pending_suffix,
                        expected_row_version,
                        wake_id,
                        wake_seq,
                        now,
                        journal_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise HallucinationVaultError("transfer_preview_already_used")
            try:
                new_source_version = adapter.detach(
                    owner_id=owner_id,
                    model_id=model_id,
                    snapshot=inspected["snapshot"],
                    expected_row_version=expected_source_row_version,
                    vault_record_id=record_id,
                )
            except Exception:
                self._roll_back_staged_transfer(journal_id)
                raise
            with self._connect(immediate=True) as connection:
                connection.execute(
                    "UPDATE vault_transfer_journals SET phase='source_detached', "
                    "destination_row_version=?, updated_at=? WHERE journal_id=?",
                    (new_source_version, _iso(), journal_id),
                )
            self._finalize_quarantine_journal(journal_id)
            adapter.mark_redirect_committed(
                owner_id=owner_id, model_id=model_id, source_ref=source_ref
            )
            status = self.status(owner_id=owner_id, model_id=model_id)
            return {
                "decision": "transferred_quarantined",
                "record_id": record_id,
                "source_ref": source_ref,
                "redirect_status": "committed",
                "lifecycle": "quarantined",
                "automatic_recall_eligible": False,
                "vault_row_version": status["row_version"],
                "source_row_version": new_source_version,
                "state_changed": True,
            }

    def _roll_back_staged_transfer(self, journal_id: str) -> None:
        with self._connect(immediate=True) as connection:
            journal = connection.execute(
                "SELECT * FROM vault_transfer_journals WHERE journal_id=?", (journal_id,)
            ).fetchone()
            if journal is None or journal["phase"] != "vault_staged":
                return
            record_id = journal["record_id"]
            connection.execute("DELETE FROM vault_source_snapshots WHERE record_id=?", (record_id,))
            connection.execute("DELETE FROM vault_record_versions WHERE record_id=?", (record_id,))
            connection.execute("DELETE FROM vault_records WHERE record_id=? AND lifecycle='staged'", (record_id,))
            connection.execute(
                "UPDATE vault_transfer_journals SET phase='rolled_back', updated_at=? WHERE journal_id=?",
                (_iso(), journal_id),
            )

    def _finalize_quarantine_journal(self, journal_id: str) -> None:
        with self._connect(immediate=True) as connection:
            journal = connection.execute(
                "SELECT * FROM vault_transfer_journals WHERE journal_id=?", (journal_id,)
            ).fetchone()
            if journal is None:
                raise HallucinationVaultError("transfer_journal_not_found")
            if journal["phase"] == "committed":
                return
            if journal["phase"] != "source_detached":
                raise HallucinationVaultError("transfer_not_ready_to_finalize")
            state = self._state(connection, journal["owner_id"], journal["model_id"])
            warning_text = state["warning_text"]
            warning_suffix = state["warning_suffix"]
            warning_version = int(state["warning_version"])
            if not warning_text:
                warning_text = journal["pending_warning_text"]
                warning_suffix = journal["pending_warning_suffix"]
                if not warning_text or not warning_suffix:
                    raise HallucinationVaultError("pending_warning_missing")
                warning_version += 1
            now = _iso()
            connection.execute(
                "UPDATE vault_records SET lifecycle='quarantined', updated_at=? "
                "WHERE record_id=? AND lifecycle='staged'",
                (now, journal["record_id"]),
            )
            connection.execute(
                "UPDATE vault_owner_state SET warning_text=?, warning_suffix=?, warning_version=?, "
                "row_version=row_version+1, updated_at=? WHERE owner_id=? AND model_id=?",
                (
                    warning_text,
                    warning_suffix,
                    warning_version,
                    now,
                    journal["owner_id"],
                    journal["model_id"],
                ),
            )
            connection.execute(
                "UPDATE vault_transfer_journals SET phase='committed', updated_at=? WHERE journal_id=?",
                (now, journal_id),
            )
            self._insert_event(
                connection,
                owner_id=journal["owner_id"],
                model_id=journal["model_id"],
                record_id=journal["record_id"],
                action="transfer",
                decision="transferred_quarantined",
                wake_id=journal["created_wake_id"],
                details={"source_ref": journal["source_ref"], "source_hash": journal["source_hash"]},
            )

    def recover_incomplete_transfers(self) -> dict[str, int]:
        """Recover staged quarantine and activating restore sagas fail-closed."""
        recovered = 0
        rolled_back = 0
        with self._operation_lock:
            with self._connect() as connection:
                journals = connection.execute(
                    "SELECT * FROM vault_transfer_journals WHERE direction='quarantine' "
                    "AND phase IN ('vault_staged','source_detached','committed')"
                ).fetchall()
            for journal in journals:
                adapter = self.source_adapters.get(str(journal["source_module"]).split("_", 1)[0])
                if adapter is None:
                    continue
                source_present = adapter.source_present(
                    owner_id=journal["owner_id"],
                    model_id=journal["model_id"],
                    source_ref=journal["source_ref"],
                )
                redirect = adapter.redirect_status(
                    owner_id=journal["owner_id"],
                    model_id=journal["model_id"],
                    source_ref=journal["source_ref"],
                )
                if (
                    journal["phase"] == "committed"
                    and not source_present
                    and redirect == "staging"
                ):
                    # Crash window: the vault transaction committed before the
                    # independent primary redirect advanced from staging. Finishing
                    # the pointer is idempotent and does not expose source content.
                    adapter.mark_redirect_committed(
                        owner_id=journal["owner_id"],
                        model_id=journal["model_id"],
                        source_ref=journal["source_ref"],
                    )
                    recovered += 1
                elif journal["phase"] == "committed":
                    # committed+committed is already complete. Contradictory
                    # combinations remain fail-closed for operator inspection.
                    continue
                elif not source_present and redirect in {"staging", "committed"}:
                    if journal["phase"] == "vault_staged":
                        with self._connect(immediate=True) as connection:
                            connection.execute(
                                "UPDATE vault_transfer_journals SET phase='source_detached', updated_at=? "
                                "WHERE journal_id=?",
                                (_iso(), journal["journal_id"]),
                            )
                    self._finalize_quarantine_journal(journal["journal_id"])
                    adapter.mark_redirect_committed(
                        owner_id=journal["owner_id"],
                        model_id=journal["model_id"],
                        source_ref=journal["source_ref"],
                    )
                    recovered += 1
                elif source_present and redirect is None:
                    self._roll_back_staged_transfer(journal["journal_id"])
                    rolled_back += 1
                # Any contradictory intermediate combination stays hidden for
                # operator review instead of guessing a truth-bearing outcome.
            self._recover_activating_restores()
        return {"recovered": recovered, "rolled_back": rolled_back}

    def pending_restore_candidates(
        self, *, owner_id: str, model_id: str
    ) -> list[dict[str, Any]]:
        owner_id, model_id = self._identity(owner_id, model_id)
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT c.*, r.neutral_title, r.lifecycle, r.current_hash, s.source_ref, "
                "s.source_version, s.snapshot_hash FROM vault_restore_candidates c "
                "JOIN vault_records r ON r.record_id=c.record_id "
                "JOIN vault_source_snapshots s ON s.record_id=c.record_id "
                "WHERE c.owner_id=? AND c.model_id=? AND c.status='pending' "
                "ORDER BY c.created_at",
                (owner_id, model_id),
            ).fetchall()
            return [
                {
                    "candidate_id": row["candidate_id"],
                    "candidate_hash": row["candidate_hash"],
                    "candidate_version": int(row["candidate_version"]),
                    "base_record_version": int(row["base_record_version"]),
                    "record_id": row["record_id"],
                    "neutral_title": row["neutral_title"],
                    "record_lifecycle": row["lifecycle"],
                    "destination_module": row["destination_module"],
                    "destination_row_version": int(row["destination_row_version"]),
                    "reason": row["reason"],
                    "source_ref": row["source_ref"],
                    "source_version": int(row["source_version"]),
                    "snapshot_hash": row["snapshot_hash"],
                    "created_wake_seq": int(row["created_wake_seq"]),
                    "presented_wake_seq": row["presented_wake_seq"],
                    "full_body_requires_warning_open": True,
                }
                for row in rows
            ]

    def propose_restore(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        record_id: Any,
        expected_record_version: int,
        destination_module: Any,
        destination_row_version: int,
        reason: Any,
        ai_confirmation: Any,
    ) -> dict[str, Any]:
        owner_id, model_id = self._identity(owner_id, model_id)
        wake_id = _text("wake_id", wake_id, 300)
        wake_seq = _integer("wake_seq", wake_seq, minimum=1)
        expected_row_version = _integer("expected_row_version", expected_row_version)
        record_id = _text("record_id", record_id, 200)
        expected_record_version = _integer("expected_record_version", expected_record_version, minimum=1)
        destination_module = _text("destination_module", destination_module, 100)
        destination_row_version = _integer("destination_row_version", destination_row_version)
        reason_text = _text("reason", reason, 2000)
        _true("ai_confirmation", ai_confirmation)
        if destination_module != "learning_memory":
            raise HallucinationVaultError("unsupported_restore_destination")
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect(immediate=True) as connection:
            state = self._state(connection, owner_id, model_id)
            if int(state["row_version"]) != expected_row_version:
                raise HallucinationVaultError("vault_row_version_conflict")
            record = connection.execute(
                "SELECT * FROM vault_records WHERE record_id=? AND owner_id=? AND model_id=?",
                (record_id, owner_id, model_id),
            ).fetchone()
            if record is None or record["lifecycle"] != "quarantined":
                raise HallucinationVaultError("record_not_restorable")
            if int(record["current_version"]) != expected_record_version:
                raise HallucinationVaultError("record_version_conflict")
            source = connection.execute(
                "SELECT * FROM vault_source_snapshots WHERE record_id=?", (record_id,)
            ).fetchone()
            if source is None or source["source_module"] != "learning_memory":
                raise HallucinationVaultError("source_snapshot_not_restorable")
            if connection.execute(
                "SELECT 1 FROM vault_restore_candidates WHERE record_id=? AND status IN ('pending','activating')",
                (record_id,),
            ).fetchone() is not None:
                raise HallucinationVaultError("restore_candidate_already_pending")
            candidate_id = _new_id("hvrestore")
            candidate_version = 1
            candidate_payload = {
                "record_id": record_id,
                "base_record_version": expected_record_version,
                "record_hash": record["current_hash"],
                "destination_module": destination_module,
                "destination_row_version": destination_row_version,
                "source_ref": source["source_ref"],
                "source_version": source["source_version"],
                "snapshot_hash": source["snapshot_hash"],
                "reason": reason_text,
            }
            candidate_hash = _sha256(candidate_payload)
            now = _iso()
            connection.execute(
                "INSERT INTO vault_restore_candidates "
                "(candidate_id, owner_id, model_id, record_id, candidate_hash, candidate_version, "
                "base_record_version, destination_module, destination_row_version, reason, status, "
                "created_wake_id, created_wake_seq, presented_wake_id, presented_wake_seq, "
                "presented_candidate_hash, created_at, updated_at) VALUES "
                "(?,?,?,?,?,1,?,?,?,?, 'pending',?,?,NULL,NULL,NULL,?,?)",
                (
                    candidate_id,
                    owner_id,
                    model_id,
                    record_id,
                    candidate_hash,
                    expected_record_version,
                    destination_module,
                    destination_row_version,
                    reason_text,
                    wake_id,
                    wake_seq,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE vault_owner_state SET row_version=row_version+1, updated_at=? "
                "WHERE owner_id=? AND model_id=? AND row_version=?",
                (now, owner_id, model_id, expected_row_version),
            )
            event_id = self._insert_event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                record_id=record_id,
                candidate_id=candidate_id,
                action="propose_restore",
                decision="candidate_pending",
                wake_id=wake_id,
                details={"candidate_hash": candidate_hash, "destination_module": destination_module},
            )
            return {
                "decision": "candidate_pending",
                "candidate_id": candidate_id,
                "candidate_hash": candidate_hash,
                "candidate_version": candidate_version,
                "base_record_version": expected_record_version,
                "vault_row_version": expected_row_version + 1,
                "review_requires_later_wake": True,
                "event_id": event_id,
                "state_changed": True,
            }

    def review_restore(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        candidate_id: Any,
        expected_candidate_version: int,
        expected_candidate_hash: Any,
        expected_base_record_version: int,
        action: Any,
        reason: Any,
        ai_confirmation: Any,
        expected_destination_row_version: int | None = None,
    ) -> dict[str, Any]:
        owner_id, model_id = self._identity(owner_id, model_id)
        wake_id = _text("wake_id", wake_id, 300)
        wake_seq = _integer("wake_seq", wake_seq, minimum=1)
        expected_row_version = _integer("expected_row_version", expected_row_version)
        candidate_id = _text("candidate_id", candidate_id, 200)
        expected_candidate_version = _integer(
            "expected_candidate_version", expected_candidate_version, minimum=1
        )
        expected_candidate_hash = _text(
            "expected_candidate_hash", expected_candidate_hash, 128
        )
        expected_base_record_version = _integer(
            "expected_base_record_version", expected_base_record_version, minimum=1
        )
        if action not in _RESTORE_ACTIONS:
            raise HallucinationVaultError("invalid_restore_action")
        reason_text = _text("reason", reason, 2000)
        _true("ai_confirmation", ai_confirmation)
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._operation_lock:
            with self._connect(immediate=True) as connection:
                state = self._state(connection, owner_id, model_id)
                if int(state["row_version"]) != expected_row_version:
                    raise HallucinationVaultError("vault_row_version_conflict")
                candidate = connection.execute(
                    "SELECT * FROM vault_restore_candidates WHERE candidate_id=? AND owner_id=? AND model_id=?",
                    (candidate_id, owner_id, model_id),
                ).fetchone()
                if candidate is None or candidate["status"] != "pending":
                    raise HallucinationVaultError("restore_candidate_not_pending")
                if int(candidate["candidate_version"]) != expected_candidate_version:
                    raise HallucinationVaultError("candidate_version_conflict")
                if not _secure_text_equal(str(candidate["candidate_hash"]), expected_candidate_hash):
                    raise HallucinationVaultError("candidate_hash_conflict")
                if int(candidate["base_record_version"]) != expected_base_record_version:
                    raise HallucinationVaultError("base_record_version_conflict")
                if wake_seq <= int(candidate["created_wake_seq"]):
                    raise HallucinationVaultError("later_real_wake_required")
                record = connection.execute(
                    "SELECT * FROM vault_records WHERE record_id=? AND owner_id=? AND model_id=?",
                    (candidate["record_id"], owner_id, model_id),
                ).fetchone()
                if record is None or int(record["current_version"]) != expected_base_record_version:
                    raise HallucinationVaultError("record_version_conflict")
                if action == "activate":
                    if (
                        candidate["presented_wake_id"] != wake_id
                        or candidate["presented_wake_seq"] != wake_seq
                        or candidate["presented_candidate_hash"] != candidate["candidate_hash"]
                    ):
                        raise HallucinationVaultError("restore_candidate_open_required")
                    if expected_destination_row_version is None:
                        raise HallucinationVaultError("expected_destination_row_version_required")
                    destination_version = _integer(
                        "expected_destination_row_version", expected_destination_row_version
                    )
                    if destination_version != int(candidate["destination_row_version"]):
                        raise HallucinationVaultError("destination_row_version_conflict")
                    now = _iso()
                    connection.execute(
                        "UPDATE vault_restore_candidates SET status='activating', updated_at=? "
                        "WHERE candidate_id=? AND status='pending'",
                        (now, candidate_id),
                    )
                    connection.execute(
                        "UPDATE vault_records SET lifecycle='restore_pending', updated_at=? "
                        "WHERE record_id=? AND lifecycle='quarantined'",
                        (now, candidate["record_id"]),
                    )
                    connection.execute(
                        "UPDATE vault_owner_state SET row_version=row_version+1, updated_at=? "
                        "WHERE owner_id=? AND model_id=? AND row_version=?",
                        (now, owner_id, model_id, expected_row_version),
                    )
                else:
                    now = _iso()
                    new_status = "rejected" if action == "reject" else "withdrawn"
                    connection.execute(
                        "UPDATE vault_restore_candidates SET status=?, updated_at=? WHERE candidate_id=?",
                        (new_status, now, candidate_id),
                    )
                    connection.execute(
                        "UPDATE vault_owner_state SET row_version=row_version+1, updated_at=? "
                        "WHERE owner_id=? AND model_id=? AND row_version=?",
                        (now, owner_id, model_id, expected_row_version),
                    )
                    event_id = self._insert_event(
                        connection,
                        owner_id=owner_id,
                        model_id=model_id,
                        record_id=candidate["record_id"],
                        candidate_id=candidate_id,
                        action=f"restore_{action}",
                        decision=new_status,
                        wake_id=wake_id,
                        details={"reason_hash": _sha256(reason_text)},
                    )
                    return {
                        "decision": new_status,
                        "candidate_id": candidate_id,
                        "record_lifecycle": "quarantined",
                        "vault_row_version": expected_row_version + 1,
                        "event_id": event_id,
                        "state_changed": True,
                    }
            # The activating marker is durable before touching the normal DB.
            with self._connect() as connection:
                source = connection.execute(
                    "SELECT * FROM vault_source_snapshots WHERE record_id=?",
                    (candidate["record_id"],),
                ).fetchone()
                if source is None:
                    raise HallucinationVaultError("source_snapshot_not_found")
                snapshot = json.loads(source["snapshot_json"])
                source_ref = str(source["source_ref"])
            adapter = self._adapter(source_ref)
            try:
                new_destination_version = adapter.restore(
                    owner_id=owner_id,
                    model_id=model_id,
                    snapshot=snapshot,
                    expected_row_version=int(expected_destination_row_version),
                    vault_record_id=candidate["record_id"],
                )
            except Exception:
                with self._connect(immediate=True) as connection:
                    connection.execute(
                        "UPDATE vault_restore_candidates SET status='pending', updated_at=? "
                        "WHERE candidate_id=? AND status='activating'",
                        (_iso(), candidate_id),
                    )
                    connection.execute(
                        "UPDATE vault_records SET lifecycle='quarantined', updated_at=? "
                        "WHERE record_id=? AND lifecycle='restore_pending'",
                        (_iso(), candidate["record_id"]),
                    )
                    connection.execute(
                        "UPDATE vault_owner_state SET row_version=row_version+1, updated_at=? "
                        "WHERE owner_id=? AND model_id=?",
                        (_iso(), owner_id, model_id),
                    )
                raise
            with self._connect(immediate=True) as connection:
                connection.execute(
                    "UPDATE vault_restore_candidates SET status='activated', updated_at=? "
                    "WHERE candidate_id=? AND status='activating'",
                    (_iso(), candidate_id),
                )
                connection.execute(
                    "UPDATE vault_records SET lifecycle='restored', review_status='reviewed', updated_at=? "
                    "WHERE record_id=? AND lifecycle='restore_pending'",
                    (_iso(), candidate["record_id"]),
                )
                event_id = self._insert_event(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    record_id=candidate["record_id"],
                    candidate_id=candidate_id,
                    action="restore_activate",
                    decision="restored",
                    wake_id=wake_id,
                    details={
                        "reason_hash": _sha256(reason_text),
                        "destination_row_version": new_destination_version,
                    },
                )
            return {
                "decision": "restored",
                "candidate_id": candidate_id,
                "record_id": candidate["record_id"],
                "record_lifecycle": "restored",
                "vault_row_version": self.status(owner_id=owner_id, model_id=model_id)["row_version"],
                "destination_row_version": new_destination_version,
                "event_id": event_id,
                "state_changed": True,
            }

    def _recover_activating_restores(self) -> None:
        with self._connect() as connection:
            candidates = connection.execute(
                "SELECT c.*, s.source_ref FROM vault_restore_candidates c "
                "JOIN vault_source_snapshots s ON s.record_id=c.record_id "
                "WHERE c.status='activating'"
            ).fetchall()
        for candidate in candidates:
            adapter = self._adapter(candidate["source_ref"])
            present = adapter.source_present(
                owner_id=candidate["owner_id"],
                model_id=candidate["model_id"],
                source_ref=candidate["source_ref"],
            )
            redirect = adapter.redirect_status(
                owner_id=candidate["owner_id"],
                model_id=candidate["model_id"],
                source_ref=candidate["source_ref"],
            )
            with self._connect(immediate=True) as connection:
                if present and redirect == "restored":
                    connection.execute(
                        "UPDATE vault_restore_candidates SET status='activated', updated_at=? "
                        "WHERE candidate_id=? AND status='activating'",
                        (_iso(), candidate["candidate_id"]),
                    )
                    connection.execute(
                        "UPDATE vault_records SET lifecycle='restored', review_status='reviewed', "
                        "updated_at=? WHERE record_id=? AND lifecycle='restore_pending'",
                        (_iso(), candidate["record_id"]),
                    )
                elif not present and redirect in {"staging", "committed"}:
                    connection.execute(
                        "UPDATE vault_restore_candidates SET status='pending', updated_at=? "
                        "WHERE candidate_id=? AND status='activating'",
                        (_iso(), candidate["candidate_id"]),
                    )
                    connection.execute(
                        "UPDATE vault_records SET lifecycle='quarantined', updated_at=? "
                        "WHERE record_id=? AND lifecycle='restore_pending'",
                        (_iso(), candidate["record_id"]),
                    )
