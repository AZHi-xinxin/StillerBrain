"""Host-issued, per-call execution leases for native ST MCP requests.

Only hashes and transport metadata are stored here. No model-authored arguments,
tool outputs, private memory, bearer tokens or wake capabilities are persisted.
Claims commit before business code runs: no SQLite transaction is held while a
facade opens its own connections. Running claims fence replacement/close until
the complete facade call has reached its finally block.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Mapping, Sequence
import uuid


EXECUTION_CONTRACT = "st-execution/1"
EXECUTION_REF_FIELD = "execution_ref"
EXECUTION_TOOLS = frozenset({
    "stbrain_open", "remember_memory", "revise_memory", "advance_plan", "submit_self_model_candidate", "activate_self_model_candidate",
    "query_self_model", "preview_person_reference_rewrite", "confirm_person_reference_rewrite",
    "manage_person_reference_advisory",
    "remember_emotional_memory", "recall_emotional_memory", "revise_emotional_memory",
    "integrate_emotional_memories", "manage_brain_pin", "veto_ephemeral_memory",
    "remember_learning_memory", "remember_learning_contrast_pair", "recall_learning_memory",
    "revise_learning_memory", "integrate_learning_memories", "review_learning_change",
    "preview_learning_recall", "remember_tool_guidance", "recall_tool_guidance",
    "revise_tool_guidance", "review_tool_guidance_candidate", "record_tool_experience",
    "manage_self_governance_profile", "query_self_governance_profile",
    "manage_injection_control", "query_injection_control", "remember_planning_memory",
    "recall_planning_memory", "record_planning_event", "revise_planning_memory",
    "review_planning_change", "hold_hallucination_record", "open_hallucination_vault",
    "transfer_hallucination_record", "review_hallucination_restore",
})
_HASH = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^stexec_[A-Za-z0-9_-]{43}$")


class ExecutionBindingError(RuntimeError):
    """Only fixed, value-free reason codes cross this boundary."""


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        raise ExecutionBindingError("execution_request_invalid")
    return value


@dataclass(frozen=True, repr=False)
class ExecutionClaim:
    execution_ref: str = field(repr=False)
    owner_id: str = field(repr=False)
    model_id: str = field(repr=False)
    wake_id: str = field(repr=False)
    batch_id: str = field(repr=False)
    call_id: str = field(repr=False)
    tool_name: str
    deployment_epoch: str = field(repr=False)
    claim_id: str = field(repr=False)
    database: str = field(repr=False)


_BOUND_CLAIM: ContextVar[ExecutionClaim | None] = ContextVar("st_execution_claim", default=None)


def current_execution_claim() -> ExecutionClaim | None:
    return _BOUND_CLAIM.get()


def _has_tables(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brain_execution_calls'"
    ).fetchone() is not None


def assert_no_running_executions(
    connection: sqlite3.Connection, *, owner_id: str, model_id: str, wake_id: str | None = None,
) -> None:
    """Call inside the transaction that closes/replaces an authoritative wake."""
    if not _has_tables(connection):
        return
    sql = "SELECT 1 FROM brain_execution_calls WHERE owner_id=? AND model_id=? AND status='running'"
    args: list[Any] = [owner_id, model_id]
    if wake_id is not None:
        sql += " AND wake_id=?"
        args.append(wake_id)
    if connection.execute(sql + " LIMIT 1", args).fetchone() is not None:
        raise ExecutionBindingError("execution_calls_running")


def assert_bound_execution(connection: sqlite3.Connection) -> None:
    """Check the active claim in the runtime transaction, without another connection."""
    claim = current_execution_claim()
    if claim is None:
        return
    if not _has_tables(connection):
        raise ExecutionBindingError("execution_registry_unavailable")
    row = connection.execute(
        "SELECT c.status,c.claim_id,c.deployment_epoch,c.wake_id,w.status AS wake_status "
        "FROM brain_execution_calls c JOIN brain_wake_sessions w ON w.wake_id=c.wake_id "
        "WHERE c.ref_hash=? AND c.owner_id=? AND c.model_id=?",
        (hashlib.sha256(claim.execution_ref.encode()).hexdigest(), claim.owner_id, claim.model_id),
    ).fetchone()
    if row is None or tuple(row) != (
        "running", claim.claim_id, claim.deployment_epoch, claim.wake_id, "current",
    ):
        raise ExecutionBindingError("execution_claim_not_current")


def expected_execution_wake(*, owner_id: str, model_id: str, explicit: str | None = None) -> str | None:
    claim = current_execution_claim()
    if claim is None:
        return explicit
    if claim.owner_id != owner_id or claim.model_id != model_id:
        raise ExecutionBindingError("execution_owner_mismatch")
    if explicit is not None and explicit != claim.wake_id:
        raise ExecutionBindingError("execution_wake_mismatch")
    return claim.wake_id


class ExecutionStore:
    def __init__(self, database: str | Path, *, deployment_epoch: str, capability_secret: str | bytes):
        self.database = str(Path(database).resolve())
        self.deployment_epoch = _text(deployment_epoch)
        self.secret = capability_secret.encode() if isinstance(capability_secret, str) else capability_secret
        if not isinstance(self.secret, bytes) or len(self.secret) < 32:
            raise ExecutionBindingError("execution_secret_invalid")
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS brain_execution_batches (
                    batch_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL, model_id TEXT NOT NULL,
                    wake_id TEXT NOT NULL REFERENCES brain_wake_sessions(wake_id),
                    deployment_epoch TEXT NOT NULL, revision INTEGER NOT NULL,
                    request_hash TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    CHECK(status IN ('active','cancelling','closed'))
                );
                CREATE TABLE IF NOT EXISTS brain_execution_calls (
                    ref_hash TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES brain_execution_batches(batch_id),
                    owner_id TEXT NOT NULL, model_id TEXT NOT NULL,
                    wake_id TEXT NOT NULL REFERENCES brain_wake_sessions(wake_id),
                    deployment_epoch TEXT NOT NULL, call_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL, advertised_name TEXT NOT NULL,
                    schema_hash TEXT NOT NULL, catalog_hash TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL, status TEXT NOT NULL,
                    claim_id TEXT, started_at TEXT, finished_at TEXT,
                    UNIQUE(owner_id,model_id,deployment_epoch,call_id),
                    CHECK(status IN ('issued','running','completed','failed','revoked','orphaned'))
                );
                CREATE INDEX IF NOT EXISTS brain_execution_calls_batch_status
                    ON brain_execution_calls(batch_id,status);
                CREATE INDEX IF NOT EXISTS brain_execution_calls_wake_status
                    ON brain_execution_calls(owner_id,model_id,wake_id,status);
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _reference(self, *, owner_id: str, model_id: str, wake_id: str, batch_id: str, call_id: str) -> str:
        message = canonical_hash([EXECUTION_CONTRACT, self.deployment_epoch, owner_id, model_id, wake_id, batch_id, call_id])
        digest = hmac.new(self.secret, message.encode(), hashlib.sha256).digest()
        return "stexec_" + base64.urlsafe_b64encode(digest).decode().rstrip("=")

    @staticmethod
    def _wake(connection: sqlite3.Connection, owner_id: str, model_id: str, wake_id: str,
              wake_capability: str | None = None, *, allow_expired: bool = False):
        wake = connection.execute(
            "SELECT * FROM brain_wake_sessions WHERE wake_id=? AND owner_id=? AND model_id=?",
            (wake_id, owner_id, model_id),
        ).fetchone()
        if wake is None or wake["status"] != "current" or wake["injected_at"] is None:
            raise ExecutionBindingError("execution_wake_not_current")
        if not allow_expired and datetime.fromisoformat(wake["expires_at"]) <= datetime.now(timezone.utc):
            raise ExecutionBindingError("execution_wake_expired")
        if wake_capability is not None and not hmac.compare_digest(wake["capability_hash"], hashlib.sha256(wake_capability.encode()).hexdigest()):
            raise ExecutionBindingError("execution_wake_invalid")
        return wake

    def issue_batch(self, *, owner_id: str, model_id: str, wake_id: str, wake_capability: str,
                    batch_id: str, revision: int, calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        for value in (owner_id, model_id, wake_id, wake_capability, batch_id):
            _text(value)
        if type(revision) is not int or revision < 1 or not isinstance(calls, (list, tuple)) or not 1 <= len(calls) <= 128:
            raise ExecutionBindingError("execution_request_invalid")
        validated = []
        seen = set()
        fields = {"call_id", "advertised_name", "canonical_tool", "schema_hash", "catalog_hash", "arguments_hash"}
        for call in calls:
            if not isinstance(call, Mapping) or set(call) != fields:
                raise ExecutionBindingError("execution_request_invalid")
            item = {key: _text(value) for key, value in call.items()}
            if item["canonical_tool"] not in EXECUTION_TOOLS or item["call_id"] in seen:
                raise ExecutionBindingError("execution_tool_invalid")
            if any(not _HASH.fullmatch(item[key]) for key in ("schema_hash", "catalog_hash", "arguments_hash")):
                raise ExecutionBindingError("execution_request_invalid")
            seen.add(item["call_id"])
            validated.append(item)
        request_hash = canonical_hash([owner_id, model_id, wake_id, batch_id, revision, validated])
        executions = [{"call_id": call["call_id"], "execution_ref": self._reference(
            owner_id=owner_id, model_id=model_id, wake_id=wake_id, batch_id=batch_id, call_id=call["call_id"],
        )} for call in validated]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._wake(connection, owner_id, model_id, wake_id, wake_capability)
            old = connection.execute("SELECT * FROM brain_execution_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if old is not None:
                if (old["request_hash"] != request_hash or old["deployment_epoch"] != self.deployment_epoch
                        or old["status"] != "active"):
                    raise ExecutionBindingError("execution_batch_conflict")
                return {"executions": executions, "batch_revision": revision}
            snapshot = connection.execute(
                "SELECT advertised_tools_json FROM brain_context_snapshots WHERE wake_id=? AND status='injected'",
                (wake_id,),
            ).fetchone()
            catalog = json.loads(snapshot[0]) if snapshot is not None else {}
            entries = {entry.get("canonical_name"): entry.get("schema_hash") for entry in catalog.get("entries", [])}
            for call in validated:
                if (catalog.get("catalog_complete") is not True or catalog.get("catalog_hash") != call["catalog_hash"]
                        or entries.get(call["advertised_name"]) != call["schema_hash"]):
                    raise ExecutionBindingError("execution_catalog_mismatch")
            now = _now()
            connection.execute(
                "INSERT INTO brain_execution_batches VALUES (?,?,?,?,?,?,?,?,?,?)",
                (batch_id, owner_id, model_id, wake_id, self.deployment_epoch, revision, request_hash, "active", now, now),
            )
            for call, execution in zip(validated, executions):
                try:
                    connection.execute(
                        "INSERT INTO brain_execution_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'issued',NULL,NULL,NULL)",
                        (hashlib.sha256(execution["execution_ref"].encode()).hexdigest(), batch_id, owner_id, model_id,
                         wake_id, self.deployment_epoch, call["call_id"], call["canonical_tool"], call["advertised_name"],
                         call["schema_hash"], call["catalog_hash"], call["arguments_hash"],),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ExecutionBindingError("execution_call_reused") from exc
        return {"executions": executions, "batch_revision": revision}

    def claim(self, *, execution_ref: str, owner_id: str, model_id: str, tool_name: str,
              arguments: Mapping[str, Any]) -> ExecutionClaim:
        if not isinstance(execution_ref, str) or not _REF.fullmatch(execution_ref):
            raise ExecutionBindingError("execution_ref_required")
        if not isinstance(arguments, Mapping) or EXECUTION_REF_FIELD in arguments:
            raise ExecutionBindingError("execution_arguments_invalid")
        try:
            arguments_hash = canonical_hash(arguments)
        except (TypeError, ValueError) as exc:
            raise ExecutionBindingError("execution_arguments_invalid") from exc
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT c.*,b.status AS batch_status FROM brain_execution_calls c "
                "JOIN brain_execution_batches b ON c.batch_id=b.batch_id WHERE c.ref_hash=?",
                (hashlib.sha256(execution_ref.encode()).hexdigest(),),
            ).fetchone()
            if row is None or (row["owner_id"], row["model_id"], row["tool_name"], row["arguments_hash"], row["deployment_epoch"]) != (
                    owner_id, model_id, tool_name, arguments_hash, self.deployment_epoch):
                raise ExecutionBindingError("execution_binding_mismatch")
            if row["status"] != "issued" or row["batch_status"] != "active":
                raise ExecutionBindingError("execution_not_available")
            self._wake(connection, owner_id, model_id, row["wake_id"])
            claim_id = uuid.uuid4().hex
            connection.execute("UPDATE brain_execution_calls SET status='running',claim_id=?,started_at=? WHERE ref_hash=?",
                               (claim_id, _now(), row["ref_hash"]))
            return ExecutionClaim(execution_ref, owner_id, model_id, row["wake_id"], row["batch_id"],
                                  row["call_id"], tool_name, self.deployment_epoch, claim_id, self.database)

    @contextmanager
    def bind(self, claim: ExecutionClaim) -> Iterator[ExecutionClaim]:
        if claim.database != self.database or claim.deployment_epoch != self.deployment_epoch:
            raise ExecutionBindingError("execution_claim_invalid")
        token = _BOUND_CLAIM.set(claim)
        try:
            yield claim
        finally:
            _BOUND_CLAIM.reset(token)

    def finish(self, claim: ExecutionClaim, *, failed: bool = False) -> None:
        if claim.database != self.database or claim.deployment_epoch != self.deployment_epoch:
            raise ExecutionBindingError("execution_claim_invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE brain_execution_calls SET status=?,finished_at=? WHERE ref_hash=? "
                "AND claim_id=? AND deployment_epoch=? AND status='running'",
                ("failed" if failed else "completed", _now(), hashlib.sha256(claim.execution_ref.encode()).hexdigest(),
                 claim.claim_id, self.deployment_epoch),
            )

    def batch_status(self, *, owner_id: str, model_id: str, wake_id: str, wake_capability: str,
                     batch_id: str, revision: int) -> dict[str, Any]:
        with self._connect() as connection:
            # Cleanup never grants a new execution. A long-running claim can
            # finish after the wake TTL; the exact owner/capability/batch fences
            # must still permit observing and retiring that expired wait.
            self._wake(connection, owner_id, model_id, wake_id, wake_capability, allow_expired=True)
            return self._batch_status(connection, owner_id, model_id, wake_id, batch_id, revision)

    def _batch_status(self, connection, owner_id, model_id, wake_id, batch_id, revision):
        row = connection.execute("SELECT * FROM brain_execution_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None or (row["owner_id"], row["model_id"], row["wake_id"], row["revision"], row["deployment_epoch"]) != (
                owner_id, model_id, wake_id, revision, self.deployment_epoch):
            raise ExecutionBindingError("execution_batch_conflict")
        counts = {status: 0 for status in ("issued", "running", "completed", "failed", "revoked", "orphaned")}
        for item in connection.execute("SELECT status,COUNT(*) FROM brain_execution_calls WHERE batch_id=? GROUP BY status", (batch_id,)):
            counts[item[0]] = item[1]
        return {"batch_status": row["status"], "batch_revision": row["revision"], "counts": counts}

    def revoke_batch(self, *, owner_id: str, model_id: str, wake_id: str, wake_capability: str,
                     batch_id: str, revision: int) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._wake(connection, owner_id, model_id, wake_id, wake_capability, allow_expired=True)
            self._batch_status(connection, owner_id, model_id, wake_id, batch_id, revision)
            connection.execute("UPDATE brain_execution_calls SET status='revoked',finished_at=? WHERE batch_id=? AND status='issued'", (_now(), batch_id))
            running = connection.execute("SELECT COUNT(*) FROM brain_execution_calls WHERE batch_id=? AND status='running'", (batch_id,)).fetchone()[0]
            connection.execute("UPDATE brain_execution_batches SET status=?,updated_at=? WHERE batch_id=?",
                               ("cancelling" if running else "closed", _now(), batch_id))
            return self._batch_status(connection, owner_id, model_id, wake_id, batch_id, revision)

    def retire_stopped_epochs(self, *, epochs: Sequence[str], stop_receipt_sha256: str) -> dict[str, Any]:
        """Offline installer ONLY, after independently verifying every old process stopped.

        Deliberately has no HTTP/MCP route and is never called by the constructor,
        timer, or normal startup. A receipt hash records installer evidence; it is
        not itself proof of process death. The sealed installer owns that check.
        Never infer death from elapsed time, a failed request, or an old PID alone.
        """
        if (not isinstance(epochs, (list, tuple)) or not epochs or len(set(epochs)) != len(epochs)
                or any(not isinstance(epoch, str) or not epoch or epoch == self.deployment_epoch for epoch in epochs)
                or not isinstance(stop_receipt_sha256, str) or not _HASH.fullmatch(stop_receipt_sha256)):
            raise ExecutionBindingError("execution_epoch_retirement_invalid")
        counts = {"revoked": 0, "orphaned": 0, "closed_batches": 0}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for epoch in epochs:
                counts["revoked"] += connection.execute(
                    "UPDATE brain_execution_calls SET status='revoked',finished_at=? "
                    "WHERE deployment_epoch=? AND status='issued'", (_now(), epoch),
                ).rowcount
                counts["orphaned"] += connection.execute(
                    "UPDATE brain_execution_calls SET status='orphaned',finished_at=? "
                    "WHERE deployment_epoch=? AND status='running'", (_now(), epoch),
                ).rowcount
                counts["closed_batches"] += connection.execute(
                    "UPDATE brain_execution_batches SET status='closed',updated_at=? "
                    "WHERE deployment_epoch=? AND status!='closed'", (_now(), epoch),
                ).rowcount
        return {"contract": EXECUTION_CONTRACT, "decision": "stopped_epochs_retired",
                "epoch_hashes": [canonical_hash(epoch) for epoch in epochs],
                "stop_receipt_sha256": stop_receipt_sha256, "counts": counts,
                "new_epoch_changed": False, "business_content_changed": False}
