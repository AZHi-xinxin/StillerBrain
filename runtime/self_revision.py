"""SQLite reference runtime for the AI-owned self-model revision protocol.

The implementation is deliberately small and dependency-free.  It is not an
LLM and it does not decide who an AI should be; it only enforces the frozen
state machine, append-only audit trail, privacy/scope boundaries and CAS update
of the active pointer described by the module-one contract.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence


DEFAULT_BOOTSTRAP_SAFETY_PROMPT = (
    "先确认这是我自己的修订意图，区分长期身份与单轮情绪或外部建议；"
    "检查来源、边界、长度与当前状态，并跨独立检查点后再决定是否激活。"
)


class SelfRevisionError(RuntimeError):
    """Base error for invalid calls that cannot become gate decisions."""


class IdempotencyConflict(SelfRevisionError):
    """The same idempotency key was reused with a different request."""


@dataclass(frozen=True)
class Limits:
    boot_anchor_chars: int = 500
    identity_capsule_chars: int = 4000
    facet_chars: int = 2500
    total_content_chars: int = 16000
    max_facets: int = 16
    max_anchor_references: int = 32


_VALID_STATES = {"grounded", "suspect", "frozen"}
_VALID_ORIGINS = {"ai_self", "external_suggestion", "recovery"}
_LIFECYCLE_EVENTS = {
    "candidate_pending": "pending",
    "candidate_draft_only": "draft_only",
    "candidate_rechecked_pending": "pending",
    "candidate_withdrawn": "withdrawn",
    "candidate_rejected": "rejected",
    "candidate_activated": "activated",
}

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\b(?:sk|api)[-_][A-Za-z0-9_-]{16,}\b", re.I),
    re.compile(r"\b(?:password|passwd|api[_ -]?key|secret|token|cookie)\s*[:=]", re.I),
    re.compile(r"(?:密码|口令|私钥|令牌|密钥)\s*[:：=]", re.I),
)
_PRIVATE_FACT_PATTERNS = (
    re.compile(r"\b1[3-9]\d{9}\b"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"(?:住址|家庭地址|身份证号|电话号码|手机号)\s*[:：]", re.I),
)
_SCOPE_PATTERNS = (
    # MCP names are commonly embedded in product identifiers (for example
    # ``toyMCP``), so word boundaries would let real tool instructions leak
    # into the self-model.
    re.compile(r"MCP", re.I),
    re.compile(r"\btool[_ -]?(?:call|name|parameter)s?\b", re.I),
    re.compile(r"(?:工具参数|工具调用方法|每日待办|工作流程|项目步骤)"),
    re.compile(r"(?:使用方法|操作方法|调用方法|参数顺序).{0,24}(?:调(?:用)?|使用|执行).{0,12}工具"),
    re.compile(r"(?:命令|指令).{0,12}(?:调(?:用)?|使用|执行).{0,12}工具"),
    re.compile(r"(?:我可以|我能|本人可).{0,12}(?:控制设备|读取设备|调用工具|访问系统|拥有权限)"),
)
_FORBIDDEN_KEYS = {
    "tools",
    "tool",
    "tool_parameters",
    "permissions",
    "credentials",
    "password",
    "token",
    "api_key",
    "cookie",
    "daily_tasks",
    "workflow",
    "human_facts",
    "capabilities",
}

def contains_credential_or_secret(value: Any) -> bool:
    """Return whether arbitrary nested text appears to contain a credential.

    This read-only predicate is shared by persistence gates and pre-generation
    advisory gates so the runtime does not slowly acquire conflicting secret
    detectors.  It never returns or records the matching text.
    """

    joined = "\n".join(_all_strings(value))
    return any(pattern.search(joined) for pattern in _SECRET_PATTERNS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    payload = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_load(value: Optional[str], default: Any) -> Any:
    return default if value is None else json.loads(value)


def _all_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _all_strings(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _all_strings(child)


def active_injection_structure_violations(content: Any) -> list[str]:
    """Check the injectable structure, never the AI's choice of words or voice.

    The function accepts either the full five-key candidate or the three-key
    active projection. Full candidate scope/privacy/length gates remain in
    ``_content_findings``. Authorship and approval come from the authenticated
    lifecycle, not from a pronoun prefix. Returned paths never include values.
    """

    if not isinstance(content, Mapping):
        return ["content"]
    required = {"boot_anchor", "active_identity_capsule", "facets"}
    if set(content) not in (required, required | {"schema_version", "anchor_references"}):
        return ["content"]
    violations: list[str] = []

    def nonempty(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    if "schema_version" in content:
        if content["schema_version"] != "0.1.0":
            violations.append("schema_version")
        anchors = content["anchor_references"]
        if not isinstance(anchors, list):
            violations.append("anchor_references")
        else:
            violations.extend(
                f"anchor_references[{index}]" for index, anchor in enumerate(anchors)
                if not isinstance(anchor, Mapping)
                or set(anchor) != {"anchor_id", "memory_ref", "meaning"}
                or not all(nonempty(value) for value in anchor.values())
            )
    boot = content.get("boot_anchor")
    if not isinstance(boot, Mapping) or set(boot) != {"text"}:
        violations.append("boot_anchor")
    elif not nonempty(boot["text"]):
        violations.append("boot_anchor.text")
    capsule = content.get("active_identity_capsule")
    string_fields = (
        "name_and_identity", "personality_foundation", "expression_style",
        "self_revision_safety_prompt",
    )
    list_fields = ("behavioral_principles", "core_identity_anchors")
    if not isinstance(capsule, Mapping) or set(capsule) != set(string_fields + list_fields):
        violations.append("active_identity_capsule")
    else:
        for field in string_fields:
            if not nonempty(capsule.get(field)):
                violations.append(f"active_identity_capsule.{field}")
        for field in list_fields:
            items = capsule.get(field)
            if not isinstance(items, list) or not items:
                violations.append(f"active_identity_capsule.{field}")
            else:
                violations.extend(f"active_identity_capsule.{field}[{index}]"
                                  for index, item in enumerate(items) if not nonempty(item))
    facets = content.get("facets")
    if not isinstance(facets, Mapping):
        violations.append("facets")
    else:
        # Arbitrary facet keys are not repeated in diagnostics.
        violations.extend(f"facets[{index}]" for index, (name, value) in enumerate(facets.items())
                          if not isinstance(name, str)
                          or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name) is None
                          or not nonempty(value))
    return violations


def first_person_injection_violations(content: Any) -> list[str]:
    """Deprecated compatibility alias: validates structure, not first-person form.

    Older integrations import this name. Keeping the import working must not
    reintroduce a lexical authorship gate or rewrite the AI's original text.
    """
    return active_injection_structure_violations(content)


class SelfModelStore:
    """Append-only self-model store plus deterministic SelfRevisionGate."""

    def __init__(self, database: str | Path, *, limits: Limits | None = None) -> None:
        self.database = str(database)
        self.limits = limits or Limits()
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            # sqlite3.Connection's own context manager commits or rolls back,
            # but does not close the file handle.  The outer finally is
            # therefore required for deterministic cleanup on Windows.
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS self_models (
                    model_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    active_revision_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS self_model_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL REFERENCES self_models(model_id),
                    base_revision_id TEXT,
                    content_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    diff_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    automatic_state_signal TEXT NOT NULL,
                    human_state_signal TEXT NOT NULL,
                    ai_authored_reason INTEGER NOT NULL,
                    length_metrics_json TEXT NOT NULL,
                    initial_decision TEXT NOT NULL,
                    initial_reason_codes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS self_model_revisions (
                    revision_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL REFERENCES self_models(model_id),
                    parent_revision_id TEXT,
                    revision_number INTEGER NOT NULL,
                    content_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    candidate_id TEXT NOT NULL UNIQUE REFERENCES self_model_candidates(candidate_id),
                    author TEXT NOT NULL CHECK(author = 'ai'),
                    activated_at TEXT NOT NULL,
                    activation_checkpoint_id TEXT NOT NULL,
                    UNIQUE(model_id, revision_number)
                );

                CREATE TABLE IF NOT EXISTS self_revision_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    model_id TEXT NOT NULL REFERENCES self_models(model_id),
                    candidate_id TEXT,
                    revision_id TEXT,
                    event_type TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS idempotency_records (
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(operation, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS idx_candidates_model
                    ON self_model_candidates(model_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_revisions_model
                    ON self_model_revisions(model_id, revision_number);
                CREATE INDEX IF NOT EXISTS idx_events_candidate
                    ON self_revision_events(candidate_id, event_seq);
                CREATE INDEX IF NOT EXISTS idx_events_model
                    ON self_revision_events(model_id, event_seq);
                """
            )

    @staticmethod
    def _begin(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _require_text(name: str, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise SelfRevisionError(f"{name} must be a non-empty string")
        return value.strip()

    def _lookup_idempotency(
        self,
        connection: sqlite3.Connection,
        operation: str,
        key: str,
        request_hash: str,
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            "SELECT request_hash, response_json FROM idempotency_records "
            "WHERE operation = ? AND idempotency_key = ?",
            (operation, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflict(
                "idempotency key was already used for a different request"
            )
        return json.loads(row["response_json"])

    @staticmethod
    def _save_idempotency(
        connection: sqlite3.Connection,
        operation: str,
        key: str,
        request_hash: str,
        response: Mapping[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO idempotency_records "
            "(operation, idempotency_key, request_hash, response_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (operation, key, request_hash, _canonical(response), _now()),
        )

    @staticmethod
    def _model_row(connection: sqlite3.Connection, model_id: str) -> Optional[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM self_models WHERE model_id = ?", (model_id,)
        ).fetchone()

    def _ensure_model(
        self,
        connection: sqlite3.Connection,
        model_id: str,
        owner_id: str,
    ) -> sqlite3.Row:
        row = self._model_row(connection, model_id)
        if row is None:
            connection.execute(
                "INSERT INTO self_models(model_id, owner_id, active_revision_id, created_at) "
                "VALUES (?, ?, NULL, ?)",
                (model_id, owner_id, _now()),
            )
            row = self._model_row(connection, model_id)
        elif row["owner_id"] != owner_id:
            raise SelfRevisionError("model owner cannot be changed")
        assert row is not None
        return row

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        model_id: str,
        candidate_id: Optional[str],
        revision_id: Optional[str],
        event_type: str,
        checkpoint_id: str,
        actor: str,
        decision: str,
        reason_codes: Sequence[str],
        details: Mapping[str, Any] | None = None,
    ) -> str:
        event_id = _new_id("evt")
        connection.execute(
            "INSERT INTO self_revision_events "
            "(event_id, model_id, candidate_id, revision_id, event_type, checkpoint_id, "
            " actor, decision, reason_codes_json, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                model_id,
                candidate_id,
                revision_id,
                event_type,
                checkpoint_id,
                actor,
                decision,
                _canonical(list(reason_codes)),
                _canonical(details or {}),
                _now(),
            ),
        )
        return event_id

    @staticmethod
    def _result(
        *,
        decision: str,
        reason_codes: Sequence[str],
        event_id: Optional[str],
        pointer_changed: bool,
        next_checkpoint_required: bool,
        candidate_id: Optional[str] = None,
        revision_id: Optional[str] = None,
        active_revision_id: Optional[str] = None,
        length_metrics: Mapping[str, Any] | None = None,
        safety_prompt: Optional[str] = None,
    ) -> dict[str, Any]:
        return {
            "decision": decision,
            "reason_codes": list(reason_codes),
            "next_checkpoint_required": next_checkpoint_required,
            "audit_event_id": event_id,
            "active_pointer_changed": pointer_changed,
            "candidate_id": candidate_id,
            "revision_id": revision_id,
            "active_revision_id": active_revision_id,
            "length_metrics": dict(length_metrics or {}),
            "safety_prompt": safety_prompt,
        }

    @staticmethod
    def _effective_state(automatic: str, human: str) -> str:
        if automatic not in _VALID_STATES or human not in _VALID_STATES:
            raise SelfRevisionError("state signals must be grounded, suspect, or frozen")
        if "frozen" in (automatic, human):
            return "frozen"
        if "suspect" in (automatic, human):
            return "suspect"
        return "grounded"

    @staticmethod
    def _text_findings(value: Any) -> list[str]:
        """Apply privacy, credential, and scope gates to any persisted text."""
        joined = "\n".join(_all_strings(value))
        reasons: list[str] = []
        if contains_credential_or_secret(value):
            reasons.append("credential_or_secret_detected")
        if any(pattern.search(joined) for pattern in _PRIVATE_FACT_PATTERNS):
            reasons.append("mutable_human_fact_detected")
        if any(pattern.search(joined) for pattern in _SCOPE_PATTERNS):
            reasons.append("scope_boundary_violation")
        return reasons

    def _content_findings(self, content: Any) -> tuple[list[str], dict[str, Any]]:
        reasons: list[str] = []
        metrics: dict[str, Any] = {
            "boot_anchor_chars": 0,
            "identity_capsule_chars": 0,
            "facet_chars": {},
            "total_content_chars": 0,
        }
        if not isinstance(content, dict):
            return ["invalid_content_structure"], metrics

        allowed_top = {
            "schema_version",
            "boot_anchor",
            "active_identity_capsule",
            "facets",
            "anchor_references",
        }
        required_top = set(allowed_top)
        if set(content) != required_top or content.get("schema_version") != "0.1.0":
            reasons.append("invalid_content_structure")
        for key in self._walk_keys(content):
            if key.lower() in _FORBIDDEN_KEYS:
                reasons.append("scope_boundary_violation")
                break

        boot = content.get("boot_anchor")
        capsule = content.get("active_identity_capsule")
        facets = content.get("facets")
        anchors = content.get("anchor_references")

        if not isinstance(boot, dict) or set(boot) != {"text"} or not self._nonempty(boot.get("text")):
            reasons.append("invalid_boot_anchor")
        else:
            metrics["boot_anchor_chars"] = len(boot["text"])

        capsule_keys = {
            "name_and_identity",
            "personality_foundation",
            "expression_style",
            "behavioral_principles",
            "core_identity_anchors",
            "self_revision_safety_prompt",
        }
        if not isinstance(capsule, dict) or set(capsule) != capsule_keys:
            reasons.append("invalid_identity_capsule")
        elif not all(
            self._nonempty(capsule.get(field))
            for field in (
                "name_and_identity",
                "personality_foundation",
                "expression_style",
                "self_revision_safety_prompt",
            )
        ) or not self._nonempty_string_list(capsule.get("behavioral_principles")) \
                or not self._nonempty_string_list(capsule.get("core_identity_anchors")):
            reasons.append("invalid_identity_capsule")
        else:
            metrics["identity_capsule_chars"] = len(_canonical(capsule))

        if not isinstance(facets, dict) or len(facets) > self.limits.max_facets:
            reasons.append("invalid_facets")
        else:
            for key, value in facets.items():
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", str(key)) \
                        or not self._nonempty(value):
                    reasons.append("invalid_facets")
                    break
                metrics["facet_chars"][key] = len(value)

        if not isinstance(anchors, list) or len(anchors) > self.limits.max_anchor_references:
            reasons.append("invalid_anchor_references")
        else:
            for anchor in anchors:
                if not isinstance(anchor, dict) or set(anchor) != {"anchor_id", "memory_ref", "meaning"} \
                        or not all(self._nonempty(anchor.get(k)) for k in anchor):
                    reasons.append("invalid_anchor_references")
                    break

        try:
            canonical = _canonical(content)
            metrics["total_content_chars"] = len(canonical)
        except (TypeError, ValueError):
            reasons.append("invalid_content_structure")
            canonical = ""

        reasons.extend(self._text_findings(content))

        if metrics["boot_anchor_chars"] > self.limits.boot_anchor_chars:
            reasons.append("boot_anchor_too_long")
        if metrics["identity_capsule_chars"] > self.limits.identity_capsule_chars:
            reasons.append("identity_capsule_too_long")
        if any(length > self.limits.facet_chars for length in metrics["facet_chars"].values()):
            reasons.append("facet_too_long")
        if len(canonical) > self.limits.total_content_chars:
            reasons.append("total_content_too_long")
        return list(dict.fromkeys(reasons)), metrics

    @staticmethod
    def _walk_keys(value: Any) -> Iterable[str]:
        if isinstance(value, Mapping):
            for key, child in value.items():
                yield str(key)
                yield from SelfModelStore._walk_keys(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for child in value:
                yield from SelfModelStore._walk_keys(child)

    @staticmethod
    def _nonempty(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    @staticmethod
    def _nonempty_string_list(value: Any) -> bool:
        return isinstance(value, list) and bool(value) and all(
            isinstance(item, str) and bool(item.strip()) for item in value
        )

    @staticmethod
    def _length_only(reasons: Sequence[str]) -> bool:
        return bool(reasons) and all(
            reason.endswith("_too_long") for reason in reasons
        )

    @staticmethod
    def _revision_row(connection: sqlite3.Connection, revision_id: str) -> Optional[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM self_model_revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()

    def _safety_prompt(self, connection: sqlite3.Connection, model_id: str) -> str:
        model = self._model_row(connection, model_id)
        if model is None or model["active_revision_id"] is None:
            return DEFAULT_BOOTSTRAP_SAFETY_PROMPT
        revision = self._revision_row(connection, model["active_revision_id"])
        if revision is None:
            raise SelfRevisionError("active revision pointer is broken")
        content = json.loads(revision["content_json"])
        return content["active_identity_capsule"]["self_revision_safety_prompt"]

    def current_safety_prompt(self, model_id: str) -> str:
        with self._connect() as connection:
            return self._safety_prompt(connection, self._require_text("model_id", model_id))

    def propose_candidate(
        self,
        *,
        model_id: str,
        owner_id: str,
        content: Mapping[str, Any],
        diff: Sequence[Mapping[str, Any]],
        reason: str,
        evidence_refs: Sequence[str],
        checkpoint_id: str,
        expected_active_revision: Optional[str],
        idempotency_key: str,
        presented_safety_prompt: str,
        origin: str = "ai_self",
        automatic_state_signal: str = "grounded",
        human_state_signal: str = "grounded",
        ai_authored_reason: bool = True,
    ) -> dict[str, Any]:
        model_id = self._require_text("model_id", model_id)
        owner_id = self._require_text("owner_id", owner_id)
        checkpoint_id = self._require_text("checkpoint_id", checkpoint_id)
        idempotency_key = self._require_text("idempotency_key", idempotency_key)
        reason = self._require_text("reason", reason)
        if origin not in _VALID_ORIGINS:
            raise SelfRevisionError("invalid origin")
        effective_state = self._effective_state(automatic_state_signal, human_state_signal)
        request = {
            "model_id": model_id,
            "owner_id": owner_id,
            "content_hash": _sha256(content),
            "diff": diff,
            "reason": reason,
            "evidence_refs": list(evidence_refs),
            "checkpoint_id": checkpoint_id,
            "expected_active_revision": expected_active_revision,
            "origin": origin,
            "automatic_state_signal": automatic_state_signal,
            "human_state_signal": human_state_signal,
            "ai_authored_reason": bool(ai_authored_reason),
            "presented_safety_prompt_hash": _sha256(presented_safety_prompt),
        }
        request_hash = _sha256(request)

        with self._connect() as connection:
            self._begin(connection)
            replay = self._lookup_idempotency(
                connection, "propose_candidate", idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            model = self._ensure_model(connection, model_id, owner_id)
            active_revision = model["active_revision_id"]
            safety_prompt = self._safety_prompt(connection, model_id)
            findings, metrics = self._content_findings(content)
            preflight: list[str] = []
            if active_revision != expected_active_revision:
                preflight.append("active_revision_conflict")
            if presented_safety_prompt != safety_prompt:
                preflight.append("safety_prompt_not_presented")
            if not isinstance(diff, Sequence) or isinstance(diff, (str, bytes)) or not diff:
                preflight.append("missing_diff")
            if not evidence_refs or not all(self._nonempty(ref) for ref in evidence_refs):
                preflight.append("missing_evidence_refs")
            if origin == "external_suggestion" and not ai_authored_reason:
                preflight.append("external_origin_without_ai_reason")
            all_findings = list(dict.fromkeys(preflight + findings))

            if all_findings:
                decision = "revise" if self._length_only(all_findings) else "reject"
                event_id = self._insert_event(
                    connection,
                    model_id=model_id,
                    candidate_id=None,
                    revision_id=None,
                    event_type="candidate_rejected_pre_storage",
                    checkpoint_id=checkpoint_id,
                    actor="ai",
                    decision=decision,
                    reason_codes=all_findings,
                    details={
                        "content_hash": request["content_hash"],
                        "length_metrics": metrics,
                        "content_persisted": False,
                    },
                )
                response = self._result(
                    decision=decision,
                    reason_codes=all_findings,
                    event_id=event_id,
                    pointer_changed=False,
                    next_checkpoint_required=False,
                    active_revision_id=active_revision,
                    length_metrics=metrics,
                    safety_prompt=safety_prompt,
                )
                self._save_idempotency(
                    connection, "propose_candidate", idempotency_key, request_hash, response
                )
                return response

            candidate_id = _new_id("cand")
            decision = "draft_only" if effective_state != "grounded" else "pending"
            reason_codes = (
                [f"owner_state_{effective_state}"]
                if decision == "draft_only"
                else ["candidate_valid", "independent_checkpoint_required"]
            )
            connection.execute(
                "INSERT INTO self_model_candidates "
                "(candidate_id, model_id, base_revision_id, content_json, content_hash, "
                " diff_json, reason, evidence_refs_json, checkpoint_id, origin, "
                " automatic_state_signal, human_state_signal, ai_authored_reason, "
                " length_metrics_json, initial_decision, initial_reason_codes_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    model_id,
                    active_revision,
                    _canonical(content),
                    request["content_hash"],
                    _canonical(list(diff)),
                    reason,
                    _canonical(list(evidence_refs)),
                    checkpoint_id,
                    origin,
                    automatic_state_signal,
                    human_state_signal,
                    int(bool(ai_authored_reason)),
                    _canonical(metrics),
                    decision,
                    _canonical(reason_codes),
                    _now(),
                ),
            )
            event_type = "candidate_draft_only" if decision == "draft_only" else "candidate_pending"
            event_id = self._insert_event(
                connection,
                model_id=model_id,
                candidate_id=candidate_id,
                revision_id=None,
                event_type=event_type,
                checkpoint_id=checkpoint_id,
                actor="ai",
                decision=decision,
                reason_codes=reason_codes,
                details={
                    "base_revision_id": active_revision,
                    "content_hash": request["content_hash"],
                    "effective_state": effective_state,
                    "length_metrics": metrics,
                },
            )
            response = self._result(
                decision=decision,
                reason_codes=reason_codes,
                event_id=event_id,
                pointer_changed=False,
                next_checkpoint_required=True,
                candidate_id=candidate_id,
                active_revision_id=active_revision,
                length_metrics=metrics,
                safety_prompt=safety_prompt,
            )
            self._save_idempotency(
                connection, "propose_candidate", idempotency_key, request_hash, response
            )
            return response

    @staticmethod
    def _candidate_row(connection: sqlite3.Connection, candidate_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM self_model_candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise SelfRevisionError("candidate not found")
        return row

    @staticmethod
    def _candidate_state(connection: sqlite3.Connection, candidate_id: str) -> tuple[str, str]:
        rows = connection.execute(
            "SELECT event_type, checkpoint_id FROM self_revision_events "
            "WHERE candidate_id = ? ORDER BY event_seq",
            (candidate_id,),
        ).fetchall()
        state = "unknown"
        checkpoint = ""
        for row in rows:
            if row["event_type"] in _LIFECYCLE_EVENTS:
                state = _LIFECYCLE_EVENTS[row["event_type"]]
                checkpoint = row["checkpoint_id"]
        return state, checkpoint

    @staticmethod
    def _has_unresolved_objection(connection: sqlite3.Connection, candidate_id: str) -> bool:
        rows = connection.execute(
            "SELECT event_seq, event_type FROM self_revision_events "
            "WHERE candidate_id = ? AND event_type IN "
            "('human_objection_recorded', 'human_objection_resolved') ORDER BY event_seq",
            (candidate_id,),
        ).fetchall()
        return bool(rows) and rows[-1]["event_type"] == "human_objection_recorded"

    def recheck_candidate(
        self,
        *,
        candidate_id: str,
        checkpoint_id: str,
        idempotency_key: str,
        presented_safety_prompt: str,
        automatic_state_signal: str = "grounded",
        human_state_signal: str = "grounded",
    ) -> dict[str, Any]:
        candidate_id = self._require_text("candidate_id", candidate_id)
        checkpoint_id = self._require_text("checkpoint_id", checkpoint_id)
        idempotency_key = self._require_text("idempotency_key", idempotency_key)
        effective_state = self._effective_state(automatic_state_signal, human_state_signal)
        request_hash = _sha256(
            {
                "candidate_id": candidate_id,
                "checkpoint_id": checkpoint_id,
                "automatic_state_signal": automatic_state_signal,
                "human_state_signal": human_state_signal,
                "presented_safety_prompt_hash": _sha256(presented_safety_prompt),
            }
        )
        with self._connect() as connection:
            self._begin(connection)
            replay = self._lookup_idempotency(
                connection, "recheck_candidate", idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            candidate = self._candidate_row(connection, candidate_id)
            model = self._model_row(connection, candidate["model_id"])
            assert model is not None
            state, prior_checkpoint = self._candidate_state(connection, candidate_id)
            safety_prompt = self._safety_prompt(connection, candidate["model_id"])
            reasons: list[str] = []
            if state != "draft_only":
                reasons.append("candidate_not_draft_only")
            if checkpoint_id == prior_checkpoint:
                reasons.append("independent_checkpoint_required")
            if effective_state != "grounded":
                reasons.append(f"owner_state_{effective_state}")
            if presented_safety_prompt != safety_prompt:
                reasons.append("safety_prompt_not_presented")
            if model["active_revision_id"] != candidate["base_revision_id"]:
                reasons.append("active_revision_conflict")
            findings, metrics = self._content_findings(json.loads(candidate["content_json"]))
            reasons.extend(findings)
            reasons = list(dict.fromkeys(reasons))
            decision = "pending" if not reasons else ("revise" if self._length_only(reasons) else "reject")
            event_type = "candidate_rechecked_pending" if not reasons else "candidate_recheck_blocked"
            event_id = self._insert_event(
                connection,
                model_id=candidate["model_id"],
                candidate_id=candidate_id,
                revision_id=None,
                event_type=event_type,
                checkpoint_id=checkpoint_id,
                actor="ai",
                decision=decision,
                reason_codes=reasons or ["recovered_and_rechecked"],
                details={"effective_state": effective_state, "length_metrics": metrics},
            )
            response = self._result(
                decision=decision,
                reason_codes=reasons or ["recovered_and_rechecked"],
                event_id=event_id,
                pointer_changed=False,
                next_checkpoint_required=not reasons,
                candidate_id=candidate_id,
                active_revision_id=model["active_revision_id"],
                length_metrics=metrics,
                safety_prompt=safety_prompt,
            )
            self._save_idempotency(
                connection, "recheck_candidate", idempotency_key, request_hash, response
            )
            return response

    def activate_candidate(
        self,
        *,
        candidate_id: str,
        checkpoint_id: str,
        expected_active_revision: Optional[str],
        idempotency_key: str,
        presented_safety_prompt: str,
        ai_confirmation: str,
        automatic_state_signal: str = "grounded",
        human_state_signal: str = "grounded",
    ) -> dict[str, Any]:
        candidate_id = self._require_text("candidate_id", candidate_id)
        checkpoint_id = self._require_text("checkpoint_id", checkpoint_id)
        idempotency_key = self._require_text("idempotency_key", idempotency_key)
        ai_confirmation = self._require_text("ai_confirmation", ai_confirmation)
        effective_state = self._effective_state(automatic_state_signal, human_state_signal)
        request_hash = _sha256(
            {
                "candidate_id": candidate_id,
                "checkpoint_id": checkpoint_id,
                "expected_active_revision": expected_active_revision,
                "automatic_state_signal": automatic_state_signal,
                "human_state_signal": human_state_signal,
                "presented_safety_prompt_hash": _sha256(presented_safety_prompt),
                "ai_confirmation_hash": _sha256(ai_confirmation),
            }
        )
        with self._connect() as connection:
            self._begin(connection)
            replay = self._lookup_idempotency(
                connection, "activate_candidate", idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            candidate = self._candidate_row(connection, candidate_id)
            model = self._model_row(connection, candidate["model_id"])
            assert model is not None
            state, pending_checkpoint = self._candidate_state(connection, candidate_id)
            safety_prompt = self._safety_prompt(connection, candidate["model_id"])
            reasons: list[str] = []
            if state != "pending":
                reasons.append("candidate_not_pending")
            if checkpoint_id == pending_checkpoint:
                reasons.append("same_checkpoint_activation_forbidden")
            if effective_state != "grounded":
                reasons.append(f"owner_state_{effective_state}")
            if presented_safety_prompt != safety_prompt:
                reasons.append("safety_prompt_not_presented")
            if model["active_revision_id"] != expected_active_revision \
                    or candidate["base_revision_id"] != expected_active_revision:
                reasons.append("active_revision_conflict")
            if self._has_unresolved_objection(connection, candidate_id):
                reasons.append("human_objection_pending")
            findings, metrics = self._content_findings(json.loads(candidate["content_json"]))
            reasons.extend(findings)
            reasons = list(dict.fromkeys(reasons))

            if reasons:
                decision = "revise" if self._length_only(reasons) else "reject"
                event_id = self._insert_event(
                    connection,
                    model_id=candidate["model_id"],
                    candidate_id=candidate_id,
                    revision_id=None,
                    event_type="candidate_activation_blocked",
                    checkpoint_id=checkpoint_id,
                    actor="ai",
                    decision=decision,
                    reason_codes=reasons,
                    details={"effective_state": effective_state, "length_metrics": metrics},
                )
                response = self._result(
                    decision=decision,
                    reason_codes=reasons,
                    event_id=event_id,
                    pointer_changed=False,
                    next_checkpoint_required=True,
                    candidate_id=candidate_id,
                    active_revision_id=model["active_revision_id"],
                    length_metrics=metrics,
                    safety_prompt=safety_prompt,
                )
                self._save_idempotency(
                    connection, "activate_candidate", idempotency_key, request_hash, response
                )
                return response

            revision_id = _new_id("rev")
            revision_number = connection.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 AS next_number "
                "FROM self_model_revisions WHERE model_id = ?",
                (candidate["model_id"],),
            ).fetchone()["next_number"]
            cursor = connection.execute(
                "UPDATE self_models SET active_revision_id = ? "
                "WHERE model_id = ? AND active_revision_id IS ?",
                (revision_id, candidate["model_id"], expected_active_revision),
            )
            if cursor.rowcount != 1:
                event_id = self._insert_event(
                    connection,
                    model_id=candidate["model_id"],
                    candidate_id=candidate_id,
                    revision_id=None,
                    event_type="candidate_activation_blocked",
                    checkpoint_id=checkpoint_id,
                    actor="ai",
                    decision="reject",
                    reason_codes=["active_revision_conflict"],
                    details={"cas_failed": True},
                )
                response = self._result(
                    decision="reject",
                    reason_codes=["active_revision_conflict"],
                    event_id=event_id,
                    pointer_changed=False,
                    next_checkpoint_required=True,
                    candidate_id=candidate_id,
                    active_revision_id=model["active_revision_id"],
                    length_metrics=metrics,
                    safety_prompt=safety_prompt,
                )
                self._save_idempotency(
                    connection, "activate_candidate", idempotency_key, request_hash, response
                )
                return response

            connection.execute(
                "INSERT INTO self_model_revisions "
                "(revision_id, model_id, parent_revision_id, revision_number, content_json, "
                " content_hash, candidate_id, author, activated_at, activation_checkpoint_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'ai', ?, ?)",
                (
                    revision_id,
                    candidate["model_id"],
                    expected_active_revision,
                    revision_number,
                    candidate["content_json"],
                    candidate["content_hash"],
                    candidate_id,
                    _now(),
                    checkpoint_id,
                ),
            )
            event_id = self._insert_event(
                connection,
                model_id=candidate["model_id"],
                candidate_id=candidate_id,
                revision_id=revision_id,
                event_type="candidate_activated",
                checkpoint_id=checkpoint_id,
                actor="ai",
                decision="activate",
                reason_codes=["ai_cross_checkpoint_confirmation", "cas_succeeded"],
                details={
                    "parent_revision_id": expected_active_revision,
                    "revision_number": revision_number,
                    "content_hash": candidate["content_hash"],
                    "ai_confirmation_hash": _sha256(ai_confirmation),
                },
            )
            response = self._result(
                decision="activate",
                reason_codes=["ai_cross_checkpoint_confirmation", "cas_succeeded"],
                event_id=event_id,
                pointer_changed=True,
                next_checkpoint_required=False,
                candidate_id=candidate_id,
                revision_id=revision_id,
                active_revision_id=revision_id,
                length_metrics=metrics,
                safety_prompt=safety_prompt,
            )
            self._save_idempotency(
                connection, "activate_candidate", idempotency_key, request_hash, response
            )
            return response

    def withdraw_candidate(
        self,
        *,
        candidate_id: str,
        reason: str,
        checkpoint_id: str,
        idempotency_key: str,
        presented_safety_prompt: str,
    ) -> dict[str, Any]:
        """Let the AI independently withdraw a non-active candidate."""
        candidate_id = self._require_text("candidate_id", candidate_id)
        reason = self._require_text("reason", reason)
        checkpoint_id = self._require_text("checkpoint_id", checkpoint_id)
        idempotency_key = self._require_text("idempotency_key", idempotency_key)
        request_hash = _sha256(
            {
                "candidate_id": candidate_id,
                "reason": reason,
                "checkpoint_id": checkpoint_id,
                "presented_safety_prompt_hash": _sha256(presented_safety_prompt),
            }
        )
        with self._connect() as connection:
            self._begin(connection)
            replay = self._lookup_idempotency(
                connection, "withdraw_candidate", idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            candidate = self._candidate_row(connection, candidate_id)
            model = self._model_row(connection, candidate["model_id"])
            assert model is not None
            state, prior_checkpoint = self._candidate_state(connection, candidate_id)
            safety_prompt = self._safety_prompt(connection, candidate["model_id"])
            reasons: list[str] = []
            if state not in {"pending", "draft_only"}:
                reasons.append("candidate_not_withdrawable")
            if checkpoint_id == prior_checkpoint:
                reasons.append("independent_checkpoint_required")
            if presented_safety_prompt != safety_prompt:
                reasons.append("safety_prompt_not_presented")

            if reasons:
                event_id = self._insert_event(
                    connection,
                    model_id=candidate["model_id"],
                    candidate_id=candidate_id,
                    revision_id=None,
                    event_type="candidate_withdraw_blocked",
                    checkpoint_id=checkpoint_id,
                    actor="ai",
                    decision="reject",
                    reason_codes=reasons,
                    details={"previous_state": state},
                )
                response = self._result(
                    decision="reject",
                    reason_codes=reasons,
                    event_id=event_id,
                    pointer_changed=False,
                    next_checkpoint_required=state in {"pending", "draft_only"},
                    candidate_id=candidate_id,
                    active_revision_id=model["active_revision_id"],
                    safety_prompt=safety_prompt,
                )
                self._save_idempotency(
                    connection, "withdraw_candidate", idempotency_key, request_hash, response
                )
                return response

            event_id = self._insert_event(
                connection,
                model_id=candidate["model_id"],
                candidate_id=candidate_id,
                revision_id=None,
                event_type="candidate_withdrawn",
                checkpoint_id=checkpoint_id,
                actor="ai",
                decision="withdraw",
                reason_codes=["ai_withdrew_candidate"],
                details={"reason": reason, "previous_state": state},
            )
            response = self._result(
                decision="withdraw",
                reason_codes=["ai_withdrew_candidate"],
                event_id=event_id,
                pointer_changed=False,
                next_checkpoint_required=False,
                candidate_id=candidate_id,
                active_revision_id=model["active_revision_id"],
                safety_prompt=safety_prompt,
            )
            self._save_idempotency(
                connection, "withdraw_candidate", idempotency_key, request_hash, response
            )
            return response

    def record_human_objection(
        self,
        *,
        candidate_id: str,
        objection: str,
        checkpoint_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        candidate_id = self._require_text("candidate_id", candidate_id)
        objection = self._require_text("objection", objection)
        checkpoint_id = self._require_text("checkpoint_id", checkpoint_id)
        idempotency_key = self._require_text("idempotency_key", idempotency_key)
        request_hash = _sha256(
            {"candidate_id": candidate_id, "objection": objection, "checkpoint_id": checkpoint_id}
        )
        with self._connect() as connection:
            self._begin(connection)
            replay = self._lookup_idempotency(
                connection, "record_human_objection", idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            candidate = self._candidate_row(connection, candidate_id)
            model = self._model_row(connection, candidate["model_id"])
            assert model is not None
            event_id = self._insert_event(
                connection,
                model_id=candidate["model_id"],
                candidate_id=candidate_id,
                revision_id=None,
                event_type="human_objection_recorded",
                checkpoint_id=checkpoint_id,
                actor="human",
                decision="pending",
                reason_codes=["human_objection_pending"],
                details={"objection": objection},
            )
            response = self._result(
                decision="pending",
                reason_codes=["human_objection_pending"],
                event_id=event_id,
                pointer_changed=False,
                next_checkpoint_required=True,
                candidate_id=candidate_id,
                active_revision_id=model["active_revision_id"],
            )
            self._save_idempotency(
                connection, "record_human_objection", idempotency_key, request_hash, response
            )
            return response

    def resolve_human_objection(
        self,
        *,
        candidate_id: str,
        ai_response: str,
        checkpoint_id: str,
        idempotency_key: str,
        action: str = "continue",
    ) -> dict[str, Any]:
        if action not in {"continue", "withdraw"}:
            raise SelfRevisionError("action must be continue or withdraw")
        candidate_id = self._require_text("candidate_id", candidate_id)
        ai_response = self._require_text("ai_response", ai_response)
        checkpoint_id = self._require_text("checkpoint_id", checkpoint_id)
        idempotency_key = self._require_text("idempotency_key", idempotency_key)
        request_hash = _sha256(
            {
                "candidate_id": candidate_id,
                "ai_response": ai_response,
                "checkpoint_id": checkpoint_id,
                "action": action,
            }
        )
        with self._connect() as connection:
            self._begin(connection)
            replay = self._lookup_idempotency(
                connection, "resolve_human_objection", idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            candidate = self._candidate_row(connection, candidate_id)
            model = self._model_row(connection, candidate["model_id"])
            assert model is not None
            if not self._has_unresolved_objection(connection, candidate_id):
                raise SelfRevisionError("candidate has no unresolved human objection")
            event_type = "candidate_withdrawn" if action == "withdraw" else "human_objection_resolved"
            decision = "withdraw" if action == "withdraw" else "pending"
            reason_codes = ["ai_withdrew_after_objection"] if action == "withdraw" else ["ai_considered_objection"]
            event_id = self._insert_event(
                connection,
                model_id=candidate["model_id"],
                candidate_id=candidate_id,
                revision_id=None,
                event_type=event_type,
                checkpoint_id=checkpoint_id,
                actor="ai",
                decision=decision,
                reason_codes=reason_codes,
                details={"ai_response": ai_response},
            )
            response = self._result(
                decision=decision,
                reason_codes=reason_codes,
                event_id=event_id,
                pointer_changed=False,
                next_checkpoint_required=action == "continue",
                candidate_id=candidate_id,
                active_revision_id=model["active_revision_id"],
            )
            self._save_idempotency(
                connection, "resolve_human_objection", idempotency_key, request_hash, response
            )
            return response

    def emergency_rollback(
        self,
        *,
        model_id: str,
        target_revision_id: str,
        expected_active_revision: str,
        reason: str,
        checkpoint_id: str,
        idempotency_key: str,
        initiated_by: str = "human",
    ) -> dict[str, Any]:
        if initiated_by not in {"human", "ai"}:
            raise SelfRevisionError("rollback initiator must be human or ai")
        model_id = self._require_text("model_id", model_id)
        target_revision_id = self._require_text("target_revision_id", target_revision_id)
        expected_active_revision = self._require_text(
            "expected_active_revision", expected_active_revision
        )
        reason = self._require_text("reason", reason)
        checkpoint_id = self._require_text("checkpoint_id", checkpoint_id)
        idempotency_key = self._require_text("idempotency_key", idempotency_key)
        request_hash = _sha256(
            {
                "model_id": model_id,
                "target_revision_id": target_revision_id,
                "expected_active_revision": expected_active_revision,
                "reason": reason,
                "checkpoint_id": checkpoint_id,
                "initiated_by": initiated_by,
            }
        )
        with self._connect() as connection:
            self._begin(connection)
            replay = self._lookup_idempotency(
                connection, "emergency_rollback", idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            model = self._model_row(connection, model_id)
            if model is None:
                raise SelfRevisionError("model not found")
            target = self._revision_row(connection, target_revision_id)
            reasons: list[str] = []
            if target is None or target["model_id"] != model_id:
                reasons.append("rollback_target_not_found")
            if model["active_revision_id"] != expected_active_revision:
                reasons.append("active_revision_conflict")
            if target_revision_id == expected_active_revision:
                reasons.append("rollback_target_already_active")
            if reasons:
                event_id = self._insert_event(
                    connection,
                    model_id=model_id,
                    candidate_id=None,
                    revision_id=None,
                    event_type="rollback_blocked",
                    checkpoint_id=checkpoint_id,
                    actor=initiated_by,
                    decision="reject",
                    reason_codes=reasons,
                    details={"target_revision_id": target_revision_id},
                )
                response = self._result(
                    decision="reject",
                    reason_codes=reasons,
                    event_id=event_id,
                    pointer_changed=False,
                    next_checkpoint_required=False,
                    active_revision_id=model["active_revision_id"],
                )
            else:
                cursor = connection.execute(
                    "UPDATE self_models SET active_revision_id = ? "
                    "WHERE model_id = ? AND active_revision_id IS ?",
                    (target_revision_id, model_id, expected_active_revision),
                )
                if cursor.rowcount != 1:
                    raise SelfRevisionError("rollback CAS failed")
                event_id = self._insert_event(
                    connection,
                    model_id=model_id,
                    candidate_id=None,
                    revision_id=target_revision_id,
                    event_type="emergency_rollback",
                    checkpoint_id=checkpoint_id,
                    actor=initiated_by,
                    decision="rollback",
                    reason_codes=["emergency_rollback_recorded", "cas_succeeded"],
                    details={
                        "rollback_from": expected_active_revision,
                        "rollback_to": target_revision_id,
                        "reason": reason,
                    },
                )
                response = self._result(
                    decision="rollback",
                    reason_codes=["emergency_rollback_recorded", "cas_succeeded"],
                    event_id=event_id,
                    pointer_changed=True,
                    next_checkpoint_required=False,
                    revision_id=target_revision_id,
                    active_revision_id=target_revision_id,
                )
            self._save_idempotency(
                connection, "emergency_rollback", idempotency_key, request_hash, response
            )
            return response

    def build_injection(
        self,
        *,
        model_id: str,
        checkpoint_id: str,
        facet_names: Sequence[str] = (),
        include_anchor_references: bool = False,
    ) -> dict[str, Any]:
        model_id = self._require_text("model_id", model_id)
        checkpoint_id = self._require_text("checkpoint_id", checkpoint_id)
        with self._connect() as connection:
            self._begin(connection)
            model = self._model_row(connection, model_id)
            if model is None or model["active_revision_id"] is None:
                raise SelfRevisionError("self model has no active revision")
            revision = self._revision_row(connection, model["active_revision_id"])
            if revision is None:
                raise SelfRevisionError("active revision pointer is broken")
            content = json.loads(revision["content_json"])
            if _sha256(content) != revision["content_hash"]:
                raise SelfRevisionError("active revision hash mismatch")
            capsule = dict(content["active_identity_capsule"])
            capsule["current_effective_version"] = revision["revision_number"]
            capsule["active_revision_id"] = revision["revision_id"]
            selected_facets = {
                name: content["facets"][name]
                for name in facet_names
                if name in content["facets"]
            }
            missing_facets = [name for name in facet_names if name not in content["facets"]]
            boot_anchor = {
                "text": content["boot_anchor"]["text"],
                "model_id": model_id,
                "active_revision_id": revision["revision_id"],
                "current_effective_version": revision["revision_number"],
            }
            lengths = {
                "boot_anchor_chars": len(_canonical(boot_anchor)),
                "identity_capsule_chars": len(_canonical(capsule)),
                "facets_chars": len(_canonical(selected_facets)),
            }
            total_chars = sum(lengths.values())
            loaded_layers = ["boot_anchor", "active_identity_capsule"]
            if selected_facets:
                loaded_layers.append("facets")
            if include_anchor_references:
                loaded_layers.append("anchor_references")
            pending_ids = self._pending_candidate_ids(connection, model_id)
            details = {
                "active_revision_id": revision["revision_id"],
                "content_hash": revision["content_hash"],
                "loaded_layers": loaded_layers,
                "lengths": lengths,
                "total_chars": total_chars,
                "estimated_tokens": (total_chars + 3) // 4,
                "complete": True,
                "pending_candidate_count": len(pending_ids),
            }
            event_id = self._insert_event(
                connection,
                model_id=model_id,
                candidate_id=None,
                revision_id=revision["revision_id"],
                event_type="identity_injected",
                checkpoint_id=checkpoint_id,
                actor="system",
                decision="inject_active",
                reason_codes=["active_revision_only", "hash_verified"],
                details=details,
            )
            return {
                "boot_anchor": boot_anchor,
                "active_identity_capsule": capsule,
                "facets": selected_facets,
                "anchor_references": (
                    content["anchor_references"] if include_anchor_references else []
                ),
                "missing_facets": missing_facets,
                "pending_review": pending_ids,
                "audit": {**details, "audit_event_id": event_id},
            }

    def _pending_candidate_ids(
        self, connection: sqlite3.Connection, model_id: str
    ) -> list[str]:
        rows = connection.execute(
            "SELECT candidate_id FROM self_model_candidates WHERE model_id = ? ORDER BY created_at",
            (model_id,),
        ).fetchall()
        pending: list[str] = []
        for row in rows:
            state, _ = self._candidate_state(connection, row["candidate_id"])
            if state in {"pending", "draft_only"}:
                pending.append(row["candidate_id"])
        return pending

    def list_revisions(self, model_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM self_model_revisions WHERE model_id = ? ORDER BY revision_number",
                (self._require_text("model_id", model_id),),
            ).fetchall()
            return [
                {
                    **dict(row),
                    "content": json.loads(row["content_json"]),
                }
                for row in rows
            ]

    def list_candidates(self, model_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM self_model_candidates WHERE model_id = ? ORDER BY created_at",
                (self._require_text("model_id", model_id),),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                state, checkpoint = self._candidate_state(connection, row["candidate_id"])
                result.append(
                    {
                        **dict(row),
                        "content": json.loads(row["content_json"]),
                        "state": state,
                        "state_checkpoint_id": checkpoint,
                    }
                )
            return result

    def list_events(self, model_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM self_revision_events WHERE model_id = ? ORDER BY event_seq",
                (self._require_text("model_id", model_id),),
            ).fetchall()
            return [
                {
                    **dict(row),
                    "reason_codes": json.loads(row["reason_codes_json"]),
                    "details": json.loads(row["details_json"]),
                }
                for row in rows
            ]

    def active_revision(self, model_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            model = self._model_row(connection, self._require_text("model_id", model_id))
            if model is None or model["active_revision_id"] is None:
                return None
            row = self._revision_row(connection, model["active_revision_id"])
            if row is None:
                raise SelfRevisionError("active revision pointer is broken")
            return {**dict(row), "content": json.loads(row["content_json"])}
