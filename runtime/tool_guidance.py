"""Owner-scoped, append-only runtime for the Stiller Brain tool-guidance module.

This module stores advice *about* callable tools.  It deliberately has no
network client, subprocess runner, generic execute method, or capability
granting API.  A remembered card is historical guidance, never a permission
or evidence that an external action succeeded.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from .credential_guard import contains_credential_or_secret
from .lexical_retrieval import explicit_alias_match, prepare_explicit_alias_query

from .authoring import (
    AuthoringError,
    claim_rewrite_receipt,
    finalize_rewrite_receipt,
    referent_warnings,
    validate_referent_bindings,
)


class ToolGuidanceError(RuntimeError):
    """One stable, value-free validation or concurrency failure."""


@dataclass(frozen=True)
class ToolGuidanceLimits:
    display_label_chars: int = 120
    purpose_chars: int = 300
    documentation_note_chars: int = 500
    call_notes_chars: int = 800
    condition_items: int = 8
    condition_chars: int = 160
    critical_preconditions: int = 4
    tag_items: int = 16
    linked_refs: int = 8
    related_refs: int = 8
    total_content_chars: int = 8000
    precise_query_tokens: int = 2000
    summary_chars: int = 100


CAPABILITY_CLASSES = frozenset({"information_query", "real_world_action"})
RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
CONFIRMATION_POLICIES = frozenset({"none", "contextual", "explicit_each_time"})
CHAIN_ROLES = frozenset({"standalone", "entry", "middle", "terminal"})
AUTO_RECALL_MODES = frozenset({"normal", "downweighted", "never_auto"})
SOURCE_TYPES = frozenset(
    {"ai_firsthand", "external_document", "human_reported", "ai_inferred"}
)
CARD_LIFECYCLES = frozenset({"active", "retired"})
EDIT_CLASSES = frozenset(
    {"typo", "metadata", "source_addition", "salience_downweight", "major"}
)
REVISION_INTENTS = frozenset({"revise", "retire", "restore"})
CANDIDATE_DECISIONS = frozenset({"accept", "keep_pending", "withdraw"})
CORRECTNESS_DECISIONS = frozenset({"correct", "uncertain", "incorrect"})
EXPERIENCE_OUTCOMES = frozenset(
    {
        "success",
        "partial_success",
        "invalid_arguments",
        "permission_denied",
        "unavailable",
        "timeout",
        "network_error",
        "provider_error",
        "user_cancelled",
        "unknown",
    }
)

TOOL_EDIT_CLASSIFICATION_VERSION = "tool-edit-classification/1"
TOOL_GUIDANCE_MODULE_VERSION = "tool-guidance/0.2"
TOOL_GUIDANCE_CONTRACT_VERSION = "tool-brain-tools/2"
TOOL_REFERENT_FIELD_PATHS = frozenset(
    {
        "/display_label",
        "/completion_rule",
        "/purpose",
        "/reminder",
        "/call_notes",
        "/documentation_note",
        "/salience_reason",
        "/handoff_condition",
    }
)

def reminder_recall_guidance() -> dict[str, Any]:
    """Explain catalog diagnostics without granting recall or execution authority."""
    return {
        "catalog_diagnostic_scope": "exact_callable_name",
        "catalog_match_required_for_scene_recall": False,
        "automatic_injection_guaranteed": False,
        "selection_factors": [
            "active_card", "author_recall_settings", "scene_relevance", "shared_budget"
        ],
        "message": (
            "我可以用工具名或 MCP 服务名保存提醒。not_advertised 表示此卡名称未与当前目录中的"
            "具体工具名精确匹配；它本身不阻止场景提醒。是否浮现仍取决于场景、我的浮现设置和"
            "容量；实际调用另按当前工具权限。"
        ),
    }


_PROVENANCE_CLASSES = {
    "ai_firsthand": "firsthand",
    "external_document": "reported",
    "human_reported": "reported",
    "ai_inferred": "inferred",
}
_AUTO_MODE_ORDER = {"normal": 2, "downweighted": 1, "never_auto": 0}
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_OPERATION_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SCENE_TAG = re.compile(r"^(?=.*\S)[^\u0000-\u001f\u007f-\u009f]{1,128}$")
_TOOL_CARD_REF = re.compile(r"^tool-card://(toolcard_[0-9a-f]{32})@([1-9][0-9]*)$")
_RELATED_REF = re.compile(
    r"^(?:tool-card://toolcard_[0-9a-f]{32}@[1-9][0-9]*|"
    r"learning://[A-Za-z0-9._:-]+@[1-9][0-9]*|"
    r"emotion://[A-Za-z0-9._:-]+@[1-9][0-9]*|"
    r"conversation://[A-Za-z0-9._:@/-]+|document://[A-Za-z0-9._:@/-]+)$"
)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")

_RAW_PAYLOAD_PATTERNS = (
    re.compile(r"Traceback \(most recent call last\):", re.I),
    re.compile(r"(?:^|[,{])\s*[\"']?(?:arguments|parameters|request_body|response_body|raw_result|raw_error|headers)[\"']?\s*:", re.I),
    re.compile(r"(?:tool_call|request|response)\s*[:：]\s*\{.{32,}\}", re.I | re.S),
)
_MAJOR_TYPO_SIGNALS = re.compile(
    r"(?:https?://|\d|不|无|禁止|允许|必须|确认|授权|删除|付款|发送|控制|参数|schema|permission|deny|allow|must|never)",
    re.I,
)
_HIGH_RISK_ACTION = re.compile(
    r"(?:支付|付款|购买|下单|删除|清空|发送|外发|发布|上传|控制|开门|关门|家电|设备|账户|权限|安全设置|"
    r"pay|purchase|delete|remove|send|publish|upload|control|device|account|permission)",
    re.I,
)

_SELF_TOOL_NAMES = frozenset(
    {
        "stbrain_open",
        "stbrain_health",
        "submit_self_model_candidate",
        "query_self_model",
        "activate_self_model_candidate",
        "remember_emotional_memory",
        "recall_emotional_memory",
        "revise_emotional_memory",
        "integrate_emotional_memories",
        "manage_brain_pin",
        "veto_ephemeral_memory",
        "remember_learning_item",
        "recall_learning_item",
        "revise_learning_item",
        "review_learning_candidate",
        "remember_tool_guidance",
        "recall_tool_guidance",
        "revise_tool_guidance",
        "review_tool_guidance_candidate",
        "record_tool_experience",
    }
)


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now_dt()).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _card_expired(value: str | None) -> bool:
    """A missing author-selected expiry does not make a card stale."""
    return value is not None and _parse_iso(value) <= _now_dt()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    encoded = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json(value: str | None, default: Any) -> Any:
    return default if value is None else json.loads(value)


def _text(name: str, value: Any, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ToolGuidanceError(f"{name}_must_be_string")
    cleaned = value.strip()
    if not cleaned and not allow_empty:
        raise ToolGuidanceError(f"{name}_required")
    if len(cleaned) > maximum:
        raise ToolGuidanceError(f"{name}_too_long")
    return cleaned


def _enum(name: str, value: Any, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ToolGuidanceError(f"invalid_{name}")
    return value


def _integer(name: str, value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ToolGuidanceError(f"invalid_{name}")
    return value


def _strings(
    name: str,
    values: Any,
    maximum: int,
    *,
    item_chars: int,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ToolGuidanceError(f"{name}_must_be_array")
    if len(values) > maximum:
        raise ToolGuidanceError(f"{name}_too_many")
    result: list[str] = []
    for value in values:
        item = _text(name, value, item_chars)
        if pattern is not None and not pattern.fullmatch(item):
            raise ToolGuidanceError(f"invalid_{name}_item")
        if item in result:
            raise ToolGuidanceError(f"duplicate_{name}_item")
        result.append(item)
    return result


def _all_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _all_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _all_strings(item)


def _forbidden_content(*values: Any) -> str | None:
    if contains_credential_or_secret(values):
        return "credential_or_secret_detected"
    for text in _all_strings(values):
        if any(pattern.search(text) for pattern in _RAW_PAYLOAD_PATTERNS):
            return "raw_payload_forbidden"
    return None


def _normalized(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if not character.isspace()
    )


def _semantic_similarity(query: str, fields: Sequence[str]) -> float:
    q = _normalized(query)
    if not q:
        return 0.0
    best = 0.0
    for field in fields:
        candidate = _normalized(field)
        if not candidate:
            continue
        if q == candidate:
            best = max(best, 1.0)
        elif q in candidate or candidate in q:
            short, long = (query, field) if len(q) <= len(candidate) else (field, query)
            short = unicodedata.normalize("NFKC", short).casefold().strip()
            long = unicodedata.normalize("NFKC", long).casefold()
            if re.fullmatch(r"[a-z0-9._\- ]+", short):
                phrase = r"\s+".join(re.escape(part) for part in short.split())
                if re.search(r"(?<![a-z0-9_])" + phrase + r"(?![a-z0-9_])", long) is None:
                    continue
            ratio = min(len(q), len(candidate)) / max(len(q), len(candidate))
            best = max(best, 0.72 + 0.22 * ratio)
        else:
            best = max(best, SequenceMatcher(None, q, candidate).ratio() * 0.72)
    return min(0.99, best)


def _checked_version_content(version: Mapping[str, Any]) -> dict[str, Any]:
    raw = version["content_json"]
    if not isinstance(raw, str) or _sha256(raw) != version["content_hash"]:
        raise ToolGuidanceError("tool_card_content_hash_mismatch")
    content = _json(raw, None)
    if not isinstance(content, dict):
        raise ToolGuidanceError("invalid_tool_card_content")
    return content


def _scene_score(query: str, content: Mapping[str, Any]) -> float:
    """Local phrase/lexical matching; no embedding or model request is implied."""
    fields = [content["purpose"], content.get("reminder", ""),
              *content["scenario_tags"], *content["scenario_examples"],
              *content["keywords"], *content["aliases"], *content["use_when"]]
    score = _semantic_similarity(query, fields)
    q = _normalized(query)
    # A short author-supplied keyword within a longer user request is a direct
    # scenario hit. Latin words must have word boundaries (fan != infant).
    for phrase in [*content["keywords"], *content["aliases"], *content["scenario_tags"]]:
        term = _normalized(phrase)
        if len(term) < 2:
            continue
        if re.fullmatch(r"[a-z0-9._-]+", term):
            hit = re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])",
                            unicodedata.normalize("NFKC", query).casefold()) is not None
        else:
            hit = term in q
        if hit:
            score = max(score, 0.95)
    return score


def _reminder_text(content: Mapping[str, Any], maximum: int) -> str:
    if content.get("reminder"):
        return str(content["reminder"])[:maximum]
    # Compatibility projection only: quote an existing authored purpose,
    # without changing any stored version or inventing a replacement sentence.
    from .reminder_excerpt import reminder_excerpt

    first_paragraph = re.split(r"[\r\n]+", str(content["purpose"]), maxsplit=1)[0]
    return reminder_excerpt(first_paragraph, maximum)


def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else _canonical(value)
    ascii_count = sum(ord(char) < 128 for char in text)
    non_ascii = len(text) - ascii_count
    return max(1, math.ceil(ascii_count / 4 + non_ascii * 0.9))


def normalize_catalog(catalog: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate one server-derived catalog snapshot without retaining schemas."""

    unavailable = {
        "catalog_complete": False,
        "catalog_hash": None,
        "entries": {},
        "reason_codes": ["live_catalog_unavailable"],
    }
    if not isinstance(catalog, Mapping):
        return unavailable
    complete = (
        catalog.get("contract") == "advertised-tools/1"
        and catalog.get("catalog_complete") is True
    )
    raw_entries = catalog.get("entries")
    supplied_hash = catalog.get("catalog_hash")
    if not isinstance(raw_entries, list) or not isinstance(supplied_hash, str):
        return unavailable
    entries: dict[str, str] = {}
    normalized_entries: list[dict[str, str]] = []
    for raw in raw_entries:
        if not isinstance(raw, Mapping) or set(raw) != {"canonical_name", "schema_hash"}:
            complete = False
            continue
        name, schema_hash = raw.get("canonical_name"), raw.get("schema_hash")
        if (
            not isinstance(name, str)
            or not _TOOL_NAME.fullmatch(name)
            or not isinstance(schema_hash, str)
            or not _SHA256.fullmatch(schema_hash)
            or name in entries
        ):
            complete = False
            continue
        entries[name] = schema_hash
        normalized_entries.append({"canonical_name": name, "schema_hash": schema_hash})
    normalized_entries.sort(key=lambda item: item["canonical_name"])
    calculated = _sha256(normalized_entries)
    if not _SHA256.fullmatch(supplied_hash) or supplied_hash != calculated:
        complete = False
    return {
        "catalog_complete": complete,
        "catalog_hash": supplied_hash if _SHA256.fullmatch(supplied_hash) else None,
        "entries": entries,
        "reason_codes": [] if complete else ["live_catalog_unavailable"],
    }


class ToolGuidanceStore:
    """SQLite store for detailed cards, candidates, experience and recall advice."""

    def __init__(
        self,
        database: str | Path,
        *,
        limits: ToolGuidanceLimits | None = None,
        detail_lookup_schema_hash: str | None = None,
    ) -> None:
        self.database = str(database)
        self.limits = limits or ToolGuidanceLimits()
        if detail_lookup_schema_hash is not None and not _SHA256.fullmatch(
            detail_lookup_schema_hash
        ):
            raise ValueError("detail_lookup_schema_hash must be a SHA-256 hex digest")
        self.detail_lookup_schema_hash = detail_lookup_schema_hash
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

    @staticmethod
    def _begin(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _migrate_experience_confidence(connection: sqlite3.Connection) -> None:
        """Widen the legacy experience CHECK without rewriting any saved value.

        SQLite cannot alter a CHECK in place. Rebuild only this table under one
        write transaction; keep its rowids, columns, constraints, indexes and
        triggers. Foreign-key actions stay disabled on this connection during
        replacement, and are checked before commit. No business/audit row is
        synthesized by the migration.
        """
        legacy_check = re.compile(
            r"CHECK\s*\(\s*confidence\s+BETWEEN\s+0\s+AND\s+80\s*\)", re.I
        )
        table_prefix = re.compile(
            r'\ACREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
            r'(?:"tool_experiences"|`tool_experiences`|\[tool_experiences\]|tool_experiences)'
            r'(?=\s*\()', re.I,
        )
        table_query = "SELECT sql FROM sqlite_master WHERE type='table' AND name='tool_experiences'"
        row = connection.execute(table_query).fetchone()
        if row is None or not legacy_check.search(row[0] or ""):
            return
        if connection.in_transaction:
            raise ToolGuidanceError("experience_confidence_migration_requires_idle_connection")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        legacy_alter = connection.execute("PRAGMA legacy_alter_table").fetchone()[0]
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA legacy_alter_table = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            # Another process may have completed the upgrade while we waited.
            row = connection.execute(table_query).fetchone()
            if row is None or not legacy_check.search(row[0] or ""):
                connection.commit()
                return
            ddl, replacements = legacy_check.subn("CHECK(confidence BETWEEN 0 AND 100)", row[0])
            ddl, table_replacements = table_prefix.subn(
                'CREATE TABLE "tool_experiences_confidence_100_migration"', ddl
            )
            if replacements != 1 or table_replacements != 1:
                raise ToolGuidanceError("unsupported_experience_confidence_schema")
            objects = connection.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name='tool_experiences' "
                "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"
            ).fetchall()
            columns = [item[1] for item in connection.execute("PRAGMA table_xinfo(tool_experiences)")
                       if item[6] == 0]
            # The historical table is a rowid table with a text primary key.
            # Quoting schema-derived names also preserves any additive columns.
            selected = "rowid," + ",".join('"' + name.replace('"', '""') + '"' for name in columns)
            connection.execute(ddl)
            connection.execute(
                f"INSERT INTO tool_experiences_confidence_100_migration ({selected}) "
                f"SELECT {selected} FROM tool_experiences"
            )
            for old, new in (("tool_experiences", "tool_experiences_confidence_100_migration"),
                             ("tool_experiences_confidence_100_migration", "tool_experiences")):
                if connection.execute(
                    f"SELECT {selected} FROM {old} EXCEPT SELECT {selected} FROM {new} LIMIT 1"
                ).fetchone() is not None:
                    raise ToolGuidanceError("experience_confidence_migration_row_mismatch")
            connection.execute("DROP TABLE tool_experiences")
            connection.execute(
                "ALTER TABLE tool_experiences_confidence_100_migration RENAME TO tool_experiences"
            )
            for item in objects:
                connection.execute(item[0])
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ToolGuidanceError("experience_confidence_migration_foreign_key_failure")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute(f"PRAGMA legacy_alter_table = {int(legacy_alter)}")
            connection.execute(f"PRAGMA foreign_keys = {int(foreign_keys)}")

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            self._migrate_experience_confidence(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tool_module_state (
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    module_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    row_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id, model_id),
                    CHECK(status IN ('available','active'))
                );

                CREATE TABLE IF NOT EXISTS tool_cards (
                    card_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    canonical_tool_name TEXT NOT NULL,
                    operation_key TEXT NOT NULL,
                    current_version INTEGER NOT NULL,
                    lifecycle TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner_id, model_id, canonical_tool_name, operation_key),
                    CHECK(current_version >= 1),
                    CHECK(lifecycle IN ('active','retired'))
                );

                CREATE TABLE IF NOT EXISTS tool_card_versions (
                    version_id TEXT PRIMARY KEY,
                    card_id TEXT NOT NULL REFERENCES tool_cards(card_id),
                    version INTEGER NOT NULL,
                    previous_version INTEGER,
                    capability_class TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    confirmation_policy TEXT NOT NULL,
                    completion_rule TEXT NOT NULL,
                    critical_preconditions_json TEXT NOT NULL,
                    display_label TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    use_when_json TEXT NOT NULL,
                    avoid_when_json TEXT NOT NULL,
                    call_notes TEXT NOT NULL,
                    documentation_note TEXT NOT NULL,
                    scenario_tags_json TEXT NOT NULL,
                    scenario_examples_json TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    aliases_json TEXT NOT NULL,
                    salience INTEGER NOT NULL,
                    auto_recall_mode TEXT NOT NULL,
                    salience_reason TEXT NOT NULL,
                    linked_tool_refs_json TEXT NOT NULL,
                    chain_role TEXT NOT NULL,
                    handoff_condition TEXT NOT NULL,
                    related_refs_json TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT,
                    additional_source_refs_json TEXT NOT NULL,
                    claimed_confidence INTEGER NOT NULL,
                    effective_confidence INTEGER NOT NULL,
                    observed_schema_hash TEXT,
                    valid_from TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    wake_id TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    diff_json TEXT NOT NULL,
                    classification_subject_json TEXT NOT NULL,
                    classification_proposer TEXT NOT NULL,
                    classification_decider TEXT NOT NULL,
                    requested_edit_class TEXT NOT NULL,
                    effective_edit_class TEXT NOT NULL,
                    classification_reason_codes_json TEXT NOT NULL,
                    classification_rule_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(card_id, version)
                );

                CREATE TABLE IF NOT EXISTS tool_guidance_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    card_id TEXT NOT NULL REFERENCES tool_cards(card_id),
                    intent TEXT NOT NULL,
                    target_version INTEGER,
                    base_version INTEGER NOT NULL,
                    proposed_content_json TEXT NOT NULL,
                    diff_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    correctness_assessment TEXT NOT NULL,
                    calm_check_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    submitted_wake_id TEXT NOT NULL,
                    submitted_wake_seq INTEGER NOT NULL,
                    presented_wake_id TEXT,
                    presented_wake_seq INTEGER,
                    reviewed_wake_id TEXT,
                    reviewed_wake_seq INTEGER,
                    creation_tool_row_version INTEGER NOT NULL,
                    candidate_hash TEXT NOT NULL,
                    catalog_hash TEXT,
                    observed_schema_hash TEXT,
                    classification_subject_json TEXT NOT NULL,
                    classification_proposer TEXT NOT NULL,
                    classification_decider TEXT NOT NULL,
                    requested_edit_class TEXT NOT NULL,
                    effective_edit_class TEXT NOT NULL,
                    classification_reason_codes_json TEXT NOT NULL,
                    classification_rule_version TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(status IN ('pending','accepted','withdrawn','superseded','expired'))
                );

                CREATE TABLE IF NOT EXISTS tool_experiences (
                    experience_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    card_id TEXT NOT NULL REFERENCES tool_cards(card_id),
                    card_version INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    attempt_summary TEXT NOT NULL,
                    lesson TEXT NOT NULL,
                    confidence INTEGER NOT NULL,
                    provenance TEXT NOT NULL,
                    evidence_ref TEXT,
                    observed_schema_hash TEXT,
                    catalog_hash TEXT,
                    occurred_at TEXT NOT NULL,
                    cooldown_until TEXT,
                    wake_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    CHECK(provenance = 'ai_reported'),
                    CHECK(evidence_ref IS NULL),
                    CHECK(confidence BETWEEN 0 AND 100)
                );

                CREATE TABLE IF NOT EXISTS tool_audit_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    wake_id TEXT,
                    card_id TEXT,
                    candidate_id TEXT,
                    experience_id TEXT,
                    decision TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    details_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tool_cards_owner
                    ON tool_cards(owner_id, model_id, lifecycle, updated_at);
                CREATE INDEX IF NOT EXISTS idx_tool_versions_card
                    ON tool_card_versions(card_id, version);
                CREATE INDEX IF NOT EXISTS idx_tool_candidates_owner
                    ON tool_guidance_candidates(owner_id, model_id, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_tool_experiences_card
                    ON tool_experiences(owner_id, model_id, card_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_tool_audit_owner
                    ON tool_audit_events(owner_id, model_id, event_seq);
                """
            )

    def ensure_state(self, *, owner_id: str, model_id: str) -> None:
        owner_id = _text("owner_id", owner_id, 200)
        model_id = _text("model_id", model_id, 200)
        now = _iso()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO tool_module_state "
                "(owner_id, model_id, module_version, status, row_version, created_at, updated_at) "
                "VALUES (?, ?, ?, 'available', 0, ?, ?)",
                (owner_id, model_id, TOOL_GUIDANCE_MODULE_VERSION, now, now),
            )

    @staticmethod
    def _ensure_state_in_connection(
        connection: sqlite3.Connection, *, owner_id: str, model_id: str
    ) -> None:
        now = _iso()
        connection.execute(
            "INSERT OR IGNORE INTO tool_module_state "
            "(owner_id, model_id, module_version, status, row_version, created_at, updated_at) "
            "VALUES (?, ?, ?, 'available', 0, ?, ?)",
            (owner_id, model_id, TOOL_GUIDANCE_MODULE_VERSION, now, now),
        )

    @staticmethod
    def _state_row(connection: sqlite3.Connection, owner_id: str, model_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM tool_module_state WHERE owner_id = ? AND model_id = ?",
            (owner_id, model_id),
        ).fetchone()
        if row is None:
            raise ToolGuidanceError("tool_module_state_missing")
        return row

    @staticmethod
    def _card_row(
        connection: sqlite3.Connection, owner_id: str, model_id: str, card_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM tool_cards WHERE owner_id = ? AND model_id = ? AND card_id = ?",
            (owner_id, model_id, card_id),
        ).fetchone()
        if row is None:
            raise ToolGuidanceError("tool_card_not_found")
        return row

    @staticmethod
    def _version_row(connection: sqlite3.Connection, card_id: str, version: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM tool_card_versions WHERE card_id = ? AND version = ?",
            (card_id, version),
        ).fetchone()
        if row is None:
            raise ToolGuidanceError("tool_card_version_not_found")
        return row

    def _current_version_row(
        self, connection: sqlite3.Connection, owner_id: str, model_id: str, card_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        card = self._card_row(connection, owner_id, model_id, card_id)
        return card, self._version_row(connection, card_id, int(card["current_version"]))

    @staticmethod
    def _advance_state(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        expected_row_version: int,
        activate: bool = True,
    ) -> int:
        if isinstance(expected_row_version, bool) or not isinstance(expected_row_version, int):
            raise ToolGuidanceError("invalid_expected_tool_row_version")
        now = _iso()
        cursor = connection.execute(
            "UPDATE tool_module_state SET row_version = row_version + 1, "
            "status = CASE WHEN ? THEN 'active' ELSE status END, updated_at = ? "
            "WHERE owner_id = ? AND model_id = ? AND row_version = ?",
            (1 if activate else 0, now, owner_id, model_id, expected_row_version),
        )
        if cursor.rowcount != 1:
            raise ToolGuidanceError("tool_row_version_conflict")
        return expected_row_version + 1

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        action: str,
        actor: str,
        decision: str,
        reason_codes: Sequence[str],
        details: Mapping[str, Any],
        wake_id: str | None = None,
        card_id: str | None = None,
        candidate_id: str | None = None,
        experience_id: str | None = None,
    ) -> str:
        event_id = _new_id("toolevt")
        safe_details = dict(details)
        connection.execute(
            "INSERT INTO tool_audit_events "
            "(event_id, owner_id, model_id, action, actor, wake_id, card_id, candidate_id, "
            " experience_id, decision, reason_codes_json, details_json, details_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                owner_id,
                model_id,
                action,
                actor,
                wake_id,
                card_id,
                candidate_id,
                experience_id,
                decision,
                _canonical(list(reason_codes)),
                _canonical(safe_details),
                _sha256(safe_details),
                _iso(),
            ),
        )
        return event_id

    def status(self, *, owner_id: str, model_id: str) -> dict[str, Any]:
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            state = self._state_row(connection, owner_id, model_id)
            counts = {
                "active_cards": connection.execute(
                    "SELECT COUNT(*) FROM tool_cards WHERE owner_id = ? AND model_id = ? AND lifecycle = 'active'",
                    (owner_id, model_id),
                ).fetchone()[0],
                "retired_cards": connection.execute(
                    "SELECT COUNT(*) FROM tool_cards WHERE owner_id = ? AND model_id = ? AND lifecycle = 'retired'",
                    (owner_id, model_id),
                ).fetchone()[0],
                "pending_candidates": connection.execute(
                    "SELECT COUNT(*) FROM tool_guidance_candidates WHERE owner_id = ? AND model_id = ? AND status = 'pending'",
                    (owner_id, model_id),
                ).fetchone()[0],
                "experiences": connection.execute(
                    "SELECT COUNT(*) FROM tool_experiences WHERE owner_id = ? AND model_id = ?",
                    (owner_id, model_id),
                ).fetchone()[0],
            }
            return {
                "module": "tool_guidance_module",
                "module_version": state["module_version"],
                "status": state["status"],
                "row_version": state["row_version"],
                "counts": counts,
            }

    def _normalize_card_content(
        self,
        *,
        catalog: Mapping[str, Any] | None,
        tool_name: Any,
        purpose: Any,
        operation_key: Any = "general",
        display_label: Any = None,
        capability_class: Any = "real_world_action",
        risk_level: Any = "high",
        confirmation_policy: Any = "explicit_each_time",
        completion_rule: Any = "",
        critical_preconditions: Any = None,
        use_when: Any = None,
        avoid_when: Any = None,
        scenario_tags: Any = None,
        scenario_examples: Any = None,
        call_notes: Any = "",
        keywords: Any = None,
        aliases: Any = None,
        reminder: Any = None,
        salience: Any = 50,
        auto_recall_mode: Any = "normal",
        salience_reason: Any = "",
        linked_tool_refs: Any = None,
        chain_role: Any = "standalone",
        handoff_condition: Any = "",
        related_refs: Any = None,
        source_type: Any = "ai_inferred",
        source_ref: Any = None,
        additional_source_refs: Any = None,
        confidence: Any = 50,
        documentation_note: Any = "",
        referent_bindings: Any = None,
        expires_at: Any = None,
        lifecycle: Any = "active",
    ) -> dict[str, Any]:
        canonical_name = _text("tool_name", tool_name, 128)
        # Service labels are advice identifiers, not necessarily callable names.
        # The transport catalog keeps its separate strict callable-name check.
        if any(unicodedata.category(c).startswith("C") for c in canonical_name):
            raise ToolGuidanceError("invalid_tool_name")
        tool_basename = canonical_name.rsplit("__", 1)[-1].casefold()
        if tool_basename in _SELF_TOOL_NAMES or tool_basename.startswith("stbrain_"):
            raise ToolGuidanceError("self_tool_excluded")
        operation = _text("operation_key", operation_key, 128)
        if not _OPERATION_KEY.fullmatch(operation):
            raise ToolGuidanceError("invalid_operation_key")
        cap_class = _enum("capability_class", capability_class, CAPABILITY_CLASSES)
        risk = _enum("risk_level", risk_level, RISK_LEVELS)
        confirmation = _enum(
            "confirmation_policy", confirmation_policy, CONFIRMATION_POLICIES
        )
        recall_mode = _enum("auto_recall_mode", auto_recall_mode, AUTO_RECALL_MODES)
        source = _enum("source_type", source_type, SOURCE_TYPES)
        claimed = _integer("confidence", confidence, 0, 100)
        source_value = None if source_ref is None else _text("source_ref", source_ref, 2000)
        if source in {"external_document", "human_reported"} and not source_value:
            raise ToolGuidanceError("source_ref_required")
        extra_sources = _strings(
            "additional_source_refs", additional_source_refs, 16, item_chars=2000
        )
        if source_value and source_value in extra_sources:
            raise ToolGuidanceError("duplicate_source_ref")
        content = {
            "canonical_tool_name": canonical_name,
            "operation_key": operation,
            "display_label": _text(
                "display_label", canonical_name if display_label is None else display_label, self.limits.display_label_chars
            ),
            "capability_class": cap_class,
            "risk_level": risk,
            "confirmation_policy": confirmation,
            "completion_rule": _text("completion_rule", completion_rule, 300, allow_empty=True),
            "critical_preconditions": _strings(
                "critical_preconditions",
                critical_preconditions,
                self.limits.critical_preconditions,
                item_chars=self.limits.condition_chars,
            ),
            "purpose": _text("purpose", purpose, self.limits.purpose_chars),
            "use_when": _strings(
                "use_when",
                use_when,
                self.limits.condition_items,
                item_chars=self.limits.condition_chars,
            ),
            "avoid_when": _strings(
                "avoid_when",
                avoid_when,
                self.limits.condition_items,
                item_chars=self.limits.condition_chars,
            ),
            "scenario_tags": _strings(
                "scenario_tags",
                scenario_tags,
                self.limits.tag_items,
                item_chars=128,
                pattern=_SCENE_TAG,
            ),
            "scenario_examples": _strings(
                "scenario_examples", scenario_examples, 8, item_chars=160
            ),
            "call_notes": _text(
                "call_notes", call_notes, self.limits.call_notes_chars, allow_empty=True
            ),
            "documentation_note": _text(
                "documentation_note",
                documentation_note,
                self.limits.documentation_note_chars,
                allow_empty=True,
            ),
            "referent_bindings": [],
            "keywords": _strings("keywords", keywords, 16, item_chars=160),
            "aliases": _strings("aliases", aliases, 16, item_chars=160),
            "salience": _integer("salience", salience, 0, 100),
            "auto_recall_mode": recall_mode,
            "salience_reason": _text(
                "salience_reason", salience_reason, 300, allow_empty=True
            ),
            "linked_tool_refs": _strings(
                "linked_tool_refs", linked_tool_refs, self.limits.linked_refs, item_chars=256
            ),
            "chain_role": _enum("chain_role", chain_role, CHAIN_ROLES),
            "handoff_condition": _text(
                "handoff_condition", handoff_condition, 300, allow_empty=True
            ),
            "related_refs": _strings(
                "related_refs", related_refs, self.limits.related_refs, item_chars=512
            ),
            "source_type": source,
            "source_ref": source_value,
            "additional_source_refs": extra_sources,
            "claimed_confidence": claimed,
            "effective_confidence": claimed,
            "valid_from": _iso(),
            "expires_at": None,
            "observed_schema_hash": None,
            "lifecycle": _enum("lifecycle", lifecycle, CARD_LIFECYCLES),
        }
        # Omission preserves the exact legacy content shape and its hashes.
        # A supplied reminder is the author's text, never a host-written promise.
        if reminder is not None:
            content["reminder"] = _text("reminder", reminder, self.limits.summary_chars)
        try:
            content["referent_bindings"] = validate_referent_bindings(
                referent_bindings, TOOL_REFERENT_FIELD_PATHS
            )
        except AuthoringError as exc:
            raise ToolGuidanceError(str(exc)) from exc
        for ref in content["linked_tool_refs"]:
            if not _TOOL_CARD_REF.fullmatch(ref):
                raise ToolGuidanceError("invalid_linked_tool_ref")
        for ref in content["related_refs"]:
            if not _RELATED_REF.fullmatch(ref):
                raise ToolGuidanceError("invalid_related_ref")
        if recall_mode != "normal" and not content["salience_reason"]:
            raise ToolGuidanceError("salience_reason_required")
        if cap_class == "real_world_action":
            high_signal = _HIGH_RISK_ACTION.search(
                " ".join(
                    [canonical_name, operation, content["purpose"], *content["use_when"]]
                )
            )
            # A real-world action without a confidently safe classifier is kept at
            # the conservative floor.  Cards may request a stricter level.
            if high_signal or risk in {"low", "medium"}:
                if _RISK_ORDER[risk] < _RISK_ORDER["high"] or confirmation != "explicit_each_time":
                    raise ToolGuidanceError("risk_below_runtime_floor")
        if risk in {"high", "critical"} and confirmation != "explicit_each_time":
            raise ToolGuidanceError("risk_below_runtime_floor")
        if expires_at is not None:
            expiry_text = _text("expires_at", expires_at, 64)
            try:
                expiry = _parse_iso(expiry_text)
            except (ValueError, OverflowError) as exc:
                raise ToolGuidanceError("invalid_expires_at") from exc
            if expiry.tzinfo is None or expiry.utcoffset() is None:
                raise ToolGuidanceError("invalid_expires_at")
            content["expires_at"] = _iso(expiry)
        catalog_state = normalize_catalog(catalog)
        content["observed_schema_hash"] = (
            catalog_state["entries"].get(canonical_name)
            if catalog_state["catalog_complete"]
            else None
        )
        forbidden = _forbidden_content(content)
        if forbidden:
            raise ToolGuidanceError(forbidden)
        if len(_canonical(content)) > self.limits.total_content_chars:
            raise ToolGuidanceError("tool_card_content_too_long")
        return content

    def _validate_linked_refs(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        refs: Sequence[str],
    ) -> None:
        for ref in refs:
            match = _TOOL_CARD_REF.fullmatch(ref)
            if match is None:
                raise ToolGuidanceError("invalid_linked_tool_ref")
            card_id, version_text = match.groups()
            row = connection.execute(
                "SELECT 1 FROM tool_cards c JOIN tool_card_versions v ON v.card_id = c.card_id "
                "WHERE c.owner_id = ? AND c.model_id = ? AND c.card_id = ? AND v.version = ?",
                (owner_id, model_id, card_id, int(version_text)),
            ).fetchone()
            if row is None:
                raise ToolGuidanceError("linked_tool_ref_not_found")

    def _insert_version(
        self,
        connection: sqlite3.Connection,
        *,
        card_id: str,
        version: int,
        previous_version: int | None,
        content: Mapping[str, Any],
        diff: Sequence[Mapping[str, Any]],
        reason: str,
        wake_id: str,
        requested_edit_class: str,
        effective_edit_class: str,
        classification_reason_codes: Sequence[str],
        classification_subject: Mapping[str, Any],
    ) -> sqlite3.Row:
        content_json = _canonical(content)
        now = _iso()
        connection.execute(
            "INSERT INTO tool_card_versions "
            "(version_id, card_id, version, previous_version, capability_class, risk_level, "
            " confirmation_policy, completion_rule, critical_preconditions_json, display_label, "
            " purpose, use_when_json, avoid_when_json, call_notes, documentation_note, "
            " scenario_tags_json, scenario_examples_json, keywords_json, aliases_json, salience, "
            " auto_recall_mode, salience_reason, linked_tool_refs_json, chain_role, handoff_condition, "
            " related_refs_json, source_type, source_ref, additional_source_refs_json, "
            " claimed_confidence, effective_confidence, observed_schema_hash, valid_from, expires_at, "
            " reason, wake_id, content_json, content_hash, diff_json, classification_subject_json, "
            " classification_proposer, classification_decider, requested_edit_class, "
            " effective_edit_class, classification_reason_codes_json, classification_rule_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ai', 'runtime_gate', ?, ?, ?, ?, ?)",
            (
                _new_id("toolv"),
                card_id,
                version,
                previous_version,
                content["capability_class"],
                content["risk_level"],
                content["confirmation_policy"],
                content["completion_rule"],
                _canonical(content["critical_preconditions"]),
                content["display_label"],
                content["purpose"],
                _canonical(content["use_when"]),
                _canonical(content["avoid_when"]),
                content["call_notes"],
                content["documentation_note"],
                _canonical(content["scenario_tags"]),
                _canonical(content["scenario_examples"]),
                _canonical(content["keywords"]),
                _canonical(content["aliases"]),
                content["salience"],
                content["auto_recall_mode"],
                content["salience_reason"],
                _canonical(content["linked_tool_refs"]),
                content["chain_role"],
                content["handoff_condition"],
                _canonical(content["related_refs"]),
                content["source_type"],
                content["source_ref"],
                _canonical(content["additional_source_refs"]),
                content["claimed_confidence"],
                content["effective_confidence"],
                content["observed_schema_hash"],
                content["valid_from"],
                # Compatibility encoding for the historical NOT NULL mirror.
                # content_json remains authoritative and stores JSON null;
                # readers and expiry gates consume that content, not this column.
                content["expires_at"] if content["expires_at"] is not None else "",
                reason,
                wake_id,
                content_json,
                _sha256(content_json),
                _canonical(list(diff)),
                _canonical(dict(classification_subject)),
                requested_edit_class,
                effective_edit_class,
                _canonical(list(classification_reason_codes)),
                TOOL_EDIT_CLASSIFICATION_VERSION,
                now,
            ),
        )
        return self._version_row(connection, card_id, version)

    @staticmethod
    def _public_card(
        card: sqlite3.Row,
        version: sqlite3.Row,
        *,
        catalog: Mapping[str, Any] | None,
        call_notes_available: bool = True,
    ) -> dict[str, Any]:
        content = _json(version["content_json"], {})
        catalog_state = normalize_catalog(catalog)
        observed = content.get("observed_schema_hash")
        current_hash = catalog_state["entries"].get(content["canonical_tool_name"])
        if not catalog_state["catalog_complete"]:
            availability, schema_status = "unknown", "unknown"
        elif current_hash is None:
            availability, schema_status = "not_advertised", "unknown"
        elif observed is None:
            availability, schema_status = "advertised", "unobserved"
        elif observed != current_hash:
            availability, schema_status = "advertised", "stale_schema"
        else:
            availability, schema_status = "advertised", "matched"
        expired = _card_expired(content["expires_at"])
        if card["lifecycle"] == "retired" or content["lifecycle"] == "retired":
            effective_status = "retired"
        elif expired:
            effective_status = "expired"
        elif schema_status == "stale_schema":
            effective_status = "stale_schema"
        elif availability == "not_advertised":
            effective_status = "not_advertised"
        else:
            effective_status = "current"
        reason_codes: list[str] = []
        if availability == "unknown":
            reason_codes.append("live_catalog_unavailable")
        elif availability == "not_advertised":
            reason_codes.append("tool_not_advertised")
        if schema_status == "stale_schema":
            reason_codes.append("schema_hash_mismatch")
            call_notes_available = False
        if expired:
            reason_codes.append("tool_guidance_expired")
        if content["auto_recall_mode"] != "normal":
            reason_codes.append("salience_downweighted")
        # Manual details return the original authored notes even when transport
        # diagnostics are stale. Their presence grants no replay/execute authority.
        return {
            "card_id": card["card_id"],
            "version": version["version"],
            "lifecycle": card["lifecycle"],
            "content": content,
            "content_hash": version["content_hash"],
            "availability": availability,
            "schema_status": schema_status,
            "effective_status": effective_status,
            "catalog_hash": catalog_state["catalog_hash"],
            "reason_codes": reason_codes,
            "guidance_authority": "historical_advice_only",
            "permission_authority": "none",
            "call_notes_available": True,
            "call_notes_current": bool(call_notes_available and schema_status == "matched"
                                       and not expired and card["lifecycle"] == "active"),
            "reminder_source": "authored_reminder" if content.get("reminder") else "legacy_purpose_excerpt",
            "reminder_recall_guidance": reminder_recall_guidance(),
        }

    def remember(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        expected_row_version: int,
        reason: str,
        catalog: Mapping[str, Any] | None = None,
        rewrite_receipt: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        reason = _text("reason", reason, 2000)
        forbidden = _forbidden_content(fields, reason)
        if forbidden:
            raise ToolGuidanceError(forbidden)
        try:
            content = self._normalize_card_content(catalog=catalog, **fields)
        except TypeError as exc:
            raise ToolGuidanceError("invalid_tool_card_fields") from exc
        # Validate all AI-authored text before even creating the owner/model
        # state row.  A credential/raw-payload rejection is therefore truly
        # zero-side-effect and cannot leave a misleading initialized module.
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        card_id = _new_id("toolcard")
        now = _iso()
        with self._connect() as connection:
            self._begin(connection)
            self._validate_linked_refs(connection, owner_id=owner_id, model_id=model_id,
                                       refs=content["linked_tool_refs"])
            try:
                receipt_request_content = {
                    key: value
                    for key, value in content.items()
                    if key not in {"valid_from", "expires_at"}
                }
                receipt_request_content["requested_expires_at"] = fields.get(
                    "expires_at"
                )
                rewrite_claim = claim_rewrite_receipt(
                    connection,
                    receipt_token=rewrite_receipt,
                    owner_id=owner_id,
                    model_id=model_id,
                    wake_id=wake_id,
                    module="tool_guidance_module",
                    final_fields={
                        "/display_label": content["display_label"],
                        "/completion_rule": content["completion_rule"],
                        "/purpose": content["purpose"],
                        **({"/reminder": content["reminder"]} if "reminder" in content else {}),
                        "/call_notes": content["call_notes"],
                        "/documentation_note": content["documentation_note"],
                        "/salience_reason": content["salience_reason"],
                        "/handoff_condition": content["handoff_condition"],
                    },
                    request_payload={
                        "content": receipt_request_content,
                        "reason": reason,
                    },
                )
            except AuthoringError as exc:
                raise ToolGuidanceError(str(exc)) from exc
            if rewrite_claim is not None and rewrite_claim["replayed"]:
                return dict(rewrite_claim["result"])
            duplicate = connection.execute(
                "SELECT card_id, current_version FROM tool_cards WHERE owner_id = ? AND model_id = ? "
                "AND canonical_tool_name = ? AND operation_key = ?",
                (
                    owner_id,
                    model_id,
                    content["canonical_tool_name"],
                    content["operation_key"],
                ),
            ).fetchone()
            if duplicate is not None:
                return {
                    "decision": "reject",
                    "reason_codes": ["tool_card_exists"],
                    "card_id": duplicate["card_id"],
                    "card_version": duplicate["current_version"],
                    "state_changed": False,
                }
            self._validate_linked_refs(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                refs=content["linked_tool_refs"],
            )
            connection.execute(
                "INSERT INTO tool_cards "
                "(card_id, owner_id, model_id, canonical_tool_name, operation_key, current_version, "
                " lifecycle, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, 'active', ?, ?)",
                (
                    card_id,
                    owner_id,
                    model_id,
                    content["canonical_tool_name"],
                    content["operation_key"],
                    now,
                    now,
                ),
            )
            version = self._insert_version(
                connection,
                card_id=card_id,
                version=1,
                previous_version=None,
                content=content,
                diff=[{"op": "add", "path": "/"}],
                reason=reason,
                wake_id=wake_id,
                requested_edit_class="create",
                effective_edit_class="create",
                classification_reason_codes=["new_card"],
                classification_subject={
                    "card_id": card_id,
                    "fields": sorted(content),
                    "direction": "create",
                },
            )
            row_version = self._advance_state(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                expected_row_version=expected_row_version,
            )
            card = self._card_row(connection, owner_id, model_id, card_id)
            event_id = self._insert_audit(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                action="remember_tool_guidance",
                actor="ai",
                wake_id=wake_id,
                card_id=card_id,
                decision="stored",
                reason_codes=["detailed_card_stored", "guidance_not_permission"],
                details={"version": 1, "content_hash": version["content_hash"]},
            )
            result = {
                "decision": "stored",
                "card": self._public_card(card, version, catalog=catalog),
                "tool_row_version": row_version,
                "event_id": event_id,
                "state_changed": True,
                "execution_performed": False,
            }
            if rewrite_claim is not None:
                result["authoring_provenance"] = {
                    "adoption_mode": "machine_assisted_mention_patch",
                    "receipt_consumed": True,
                }
            try:
                finalize_rewrite_receipt(
                    connection,
                    claim=rewrite_claim,
                    canonical_ref=f"tool-card://{card_id}@1",
                    result=result,
                )
            except AuthoringError as exc:
                raise ToolGuidanceError(str(exc)) from exc
            return result

    @staticmethod
    def _diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {"op": "replace", "path": f"/{key}"}
            for key in sorted(set(before) | set(after))
            if before.get(key) != after.get(key)
        ]

    def _content_as_input(self, content: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "tool_name": content["canonical_tool_name"],
            "operation_key": content["operation_key"],
            "display_label": content["display_label"],
            "capability_class": content["capability_class"],
            "risk_level": content["risk_level"],
            "confirmation_policy": content["confirmation_policy"],
            "completion_rule": content["completion_rule"],
            "critical_preconditions": content["critical_preconditions"],
            "purpose": content["purpose"],
            **({"reminder": content["reminder"]} if "reminder" in content else {}),
            "use_when": content["use_when"],
            "avoid_when": content["avoid_when"],
            "scenario_tags": content["scenario_tags"],
            "scenario_examples": content["scenario_examples"],
            "call_notes": content["call_notes"],
            "keywords": content["keywords"],
            "aliases": content["aliases"],
            "salience": content["salience"],
            "auto_recall_mode": content["auto_recall_mode"],
            "salience_reason": content["salience_reason"],
            "linked_tool_refs": content["linked_tool_refs"],
            "chain_role": content["chain_role"],
            "handoff_condition": content["handoff_condition"],
            "related_refs": content["related_refs"],
            "source_type": content["source_type"],
            "source_ref": content["source_ref"],
            "additional_source_refs": content["additional_source_refs"],
            "confidence": content["claimed_confidence"],
            "documentation_note": content["documentation_note"],
            "referent_bindings": content.get("referent_bindings", []),
            "expires_at": content["expires_at"],
            "lifecycle": content["lifecycle"],
        }

    def _classification_reject(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        card_id: str,
        requested: str,
        fields: Sequence[str],
        reason_codes: Sequence[str],
    ) -> dict[str, Any]:
        event_id = self._insert_audit(
            connection,
            owner_id=owner_id,
            model_id=model_id,
            action="classify_tool_guidance_revision",
            actor="runtime_gate",
            wake_id=wake_id,
            card_id=card_id,
            decision="reject",
            reason_codes=reason_codes,
            details={
                "classification_subject": {
                    "card_id": card_id,
                    "fields": sorted(fields),
                    "direction": "proposed_change",
                },
                "classification_proposer": "ai",
                "classification_decider": "runtime_gate",
                "requested_edit_class": requested,
                "effective_edit_class": "major",
                "classification_rule_version": TOOL_EDIT_CLASSIFICATION_VERSION,
            },
        )
        return {
            "decision": "reject",
            "reason_codes": list(reason_codes),
            "event_id": event_id,
            "state_changed": False,
            "effective_edit_class": "major",
        }

    def revise(
        self, *, owner_id: str, model_id: str, wake_id: str, wake_seq: int,
        expected_row_version: int, card_id: str, expected_card_version: int,
        reason: str, intent: str = "revise", edit_class: str = "major",
        catalog: Mapping[str, Any] | None = None, target_version: int | None = None,
        correctness_assessment: str | None = None,
        calm_check_stability: str | None = None, calm_check_necessity: str | None = None,
        calm_check_consequences: str | None = None, calm_check_alternatives: str | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        """Commit the author's exact change once; no fabricated review or wake.

        Legacy classification/calm arguments remain readable by old clients but
        do not grant permissions or serve as evidence of independent validation.
        """
        card_id = _text("card_id", card_id, 200)
        reason = _text("reason", reason, 2000)
        intent = _enum("revision_intent", intent, REVISION_INTENTS)
        requested = _enum("edit_class", edit_class, EDIT_CLASSES)
        forbidden = _forbidden_content(changes, reason, correctness_assessment,
                                       calm_check_stability, calm_check_necessity,
                                       calm_check_consequences, calm_check_alternatives)
        if forbidden:
            raise ToolGuidanceError(forbidden)
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            self._begin(connection)
            state = self._state_row(connection, owner_id, model_id)
            if type(expected_row_version) is not int or expected_row_version < 0:
                raise ToolGuidanceError("invalid_expected_tool_row_version")
            if state["row_version"] != expected_row_version:
                raise ToolGuidanceError("tool_row_version_conflict")
            card, version = self._current_version_row(connection, owner_id, model_id, card_id)
            if type(expected_card_version) is not int or version["version"] != expected_card_version:
                raise ToolGuidanceError("tool_card_version_conflict")
            before = _checked_version_content(version)
            basis = before
            if intent == "restore":
                if type(target_version) is not int or target_version < 1:
                    raise ToolGuidanceError("target_version_required")
                basis = _checked_version_content(self._version_row(connection, card_id, target_version))
            elif target_version is not None:
                raise ToolGuidanceError("target_version_requires_restore")
            proposed_input = self._content_as_input(basis)
            change_values = dict(changes)
            if requested == "typo":
                if set(change_values) != {"field_name", "before_text", "after_text"}:
                    raise ToolGuidanceError("invalid_revision_fields")
                field = change_values["field_name"]
                old = _text("before_text", change_values["before_text"], 1000)
                new = _text("after_text", change_values["after_text"], 1000, allow_empty=True)
                if field not in proposed_input or not isinstance(proposed_input[field], str):
                    raise ToolGuidanceError("invalid_revision_fields")
                if proposed_input[field].count(old) != 1:
                    raise ToolGuidanceError("typo_source_mismatch")
                change_values = {field: proposed_input[field].replace(old, new, 1)}
            elif requested == "source_addition":
                if set(change_values) != {"source_ref"}:
                    raise ToolGuidanceError("invalid_revision_fields")
                added = _text("source_ref", change_values["source_ref"], 2000)
                if added == basis.get("source_ref") or added in basis["additional_source_refs"]:
                    raise ToolGuidanceError("no_effective_change")
                change_values = {"additional_source_refs": [*basis["additional_source_refs"], added]}
            # Never accept internal provenance/hash/lifecycle fields through a
            # catch-all dictionary. Retirement/restoration has an explicit intent.
            allowed = (set(proposed_input) | {"reminder"}) - {"lifecycle"}
            if requested == "source_addition":
                allowed.add("additional_source_refs")
            if set(change_values) - allowed:
                raise ToolGuidanceError("invalid_revision_fields")
            if intent == "revise" and before["lifecycle"] == "retired":
                raise ToolGuidanceError("card_retired_use_restore")
            if intent == "revise" and not change_values:
                raise ToolGuidanceError("no_effective_change")
            if requested == "salience_downweight":
                if set(change_values) - {"salience", "auto_recall_mode", "salience_reason"}:
                    raise ToolGuidanceError("invalid_revision_fields")
                level = _integer("salience", change_values.get("salience", basis["salience"]), 0, 100)
                mode = _enum("auto_recall_mode", change_values.get("auto_recall_mode", basis["auto_recall_mode"]), AUTO_RECALL_MODES)
                if level > basis["salience"] or _AUTO_MODE_ORDER[mode] > _AUTO_MODE_ORDER[basis["auto_recall_mode"]]:
                    raise ToolGuidanceError("not_a_downweight")
            proposed_input.update(change_values)
            if intent in {"retire", "restore"}:
                proposed_input["lifecycle"] = "retired" if intent == "retire" else "active"
            # Editing old advice must not silently renew its expiry or claim a
            # fresh schema observation. Explicit tool/schema changes remain stale
            # unless the host really supplied that exact target in this catalog.
            preserve_expiry = "expires_at" not in change_values
            if preserve_expiry:
                proposed_input["expires_at"] = None
            proposed = self._normalize_card_content(catalog=catalog, **proposed_input)
            if preserve_expiry:
                proposed["expires_at"] = basis["expires_at"]
            if "tool_name" not in change_values:
                proposed["observed_schema_hash"] = basis["observed_schema_hash"]
            # Time is version metadata, not an author-written modification.
            proposed["valid_from"] = before["valid_from"]
            if not self._diff(before, proposed):
                raise ToolGuidanceError("no_effective_change")
            proposed["valid_from"] = _iso()
            self._validate_linked_refs(connection, owner_id=owner_id, model_id=model_id,
                                       refs=proposed["linked_tool_refs"])
            duplicate = connection.execute(
                "SELECT card_id FROM tool_cards WHERE owner_id = ? AND model_id = ? "
                "AND canonical_tool_name = ? AND operation_key = ? AND card_id != ?",
                (owner_id, model_id, proposed["canonical_tool_name"], proposed["operation_key"], card_id),
            ).fetchone()
            if duplicate is not None:
                raise ToolGuidanceError("tool_card_exists")
            diff = self._diff(before, proposed)
            new_version = expected_card_version + 1
            now = _iso()
            connection.execute(
                "UPDATE tool_cards SET canonical_tool_name = ?, operation_key = ?, current_version = ?, "
                "lifecycle = ?, updated_at = ? WHERE card_id = ? AND current_version = ?",
                (proposed["canonical_tool_name"], proposed["operation_key"], new_version,
                 proposed["lifecycle"], now, card_id, expected_card_version),
            )
            reasons = ["author_revision_committed", "guidance_not_permission"]
            inserted = self._insert_version(
                connection, card_id=card_id, version=new_version, previous_version=expected_card_version,
                content=proposed, diff=diff, reason=reason, wake_id=wake_id,
                requested_edit_class=requested, effective_edit_class="direct_revision",
                classification_reason_codes=reasons,
                classification_subject={"card_id": card_id, "fields": [d["path"][1:] for d in diff],
                                        "direction": intent, "independent_review_performed": False},
            )
            row_version = self._advance_state(connection, owner_id=owner_id, model_id=model_id,
                                              expected_row_version=expected_row_version)
            event_id = self._insert_audit(
                connection, owner_id=owner_id, model_id=model_id, action="revise_tool_guidance",
                actor="ai", wake_id=wake_id, card_id=card_id, decision="version_appended",
                reason_codes=reasons, details={"version": new_version, "diff": diff,
                                              "submission_mode": "direct_revision"},
            )
            updated = self._card_row(connection, owner_id, model_id, card_id)
            return {"decision": "version_appended", "submission_mode": "direct_revision",
                    "card": self._public_card(updated, inserted, catalog=catalog), "diff": diff,
                    "rollback_ref": f"tool-card://{card_id}@{expected_card_version}",
                    "tool_row_version": row_version, "event_id": event_id,
                    "state_changed": True, "active_version_changed": True,
                    "review_requires_later_wake": False, "execution_performed": False}


    def propose_revision(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        card_id: str,
        expected_card_version: int,
        intent: str,
        edit_class: str,
        reason: str,
        catalog: Mapping[str, Any] | None = None,
        target_version: int | None = None,
        correctness_assessment: str | None = None,
        calm_check_stability: str | None = None,
        calm_check_necessity: str | None = None,
        calm_check_consequences: str | None = None,
        calm_check_alternatives: str | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        card_id = _text("card_id", card_id, 200)
        intent = _enum("revision_intent", intent, REVISION_INTENTS)
        requested = _enum("edit_class", edit_class, EDIT_CLASSES)
        reason = _text("reason", reason, 2000)
        allowed_change_fields = {
            "tool_name",
            "operation_key",
            "field_name",
            "before_text",
            "after_text",
            "display_label",
            "documentation_note",
            "purpose",
            "use_when",
            "avoid_when",
            "scenario_tags",
            "scenario_examples",
            "call_notes",
            "keywords",
            "aliases",
            "capability_class",
            "risk_level",
            "confirmation_policy",
            "completion_rule",
            "critical_preconditions",
            "linked_tool_refs",
            "chain_role",
            "handoff_condition",
            "related_refs",
            "source_type",
            "source_ref",
            "confidence",
            "salience",
            "auto_recall_mode",
            "salience_reason",
            "expires_at",
            "referent_bindings",
        }
        if set(changes) - allowed_change_fields:
            raise ToolGuidanceError("invalid_revision_fields")
        if _forbidden_content(changes, reason, correctness_assessment):
            raise ToolGuidanceError(_forbidden_content(changes, reason, correctness_assessment) or "credential_or_secret_detected")
        with self._connect() as connection:
            self._begin(connection)
            state = self._state_row(connection, owner_id, model_id)
            if (
                isinstance(expected_row_version, bool)
                or not isinstance(expected_row_version, int)
            ):
                raise ToolGuidanceError("invalid_expected_tool_row_version")
            if state["row_version"] != expected_row_version:
                raise ToolGuidanceError("tool_row_version_conflict")
            card, version = self._current_version_row(
                connection, owner_id, model_id, card_id
            )
            if isinstance(expected_card_version, bool) or version["version"] != expected_card_version:
                raise ToolGuidanceError("tool_card_version_conflict")
            before = _json(version["content_json"], {})

            if intent in {"retire", "restore"} and requested != "major":
                return self._classification_reject(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    wake_id=wake_id,
                    card_id=card_id,
                    requested=requested,
                    fields=list(changes),
                    reason_codes=["major_review_required", "edit_class_upgraded"],
                )

            direct_content: dict[str, Any] | None = None
            direct_reason_codes: list[str] = []
            if requested == "typo" and intent == "revise":
                allowed = {"field_name", "before_text", "after_text"}
                if set(changes) != allowed:
                    return self._classification_reject(
                        connection,
                        owner_id=owner_id,
                        model_id=model_id,
                        wake_id=wake_id,
                        card_id=card_id,
                        requested=requested,
                        fields=list(changes),
                        reason_codes=["major_review_required", "edit_class_upgraded"],
                    )
                field_name = changes["field_name"]
                old = _text("before_text", changes["before_text"], 32)
                new = _text("after_text", changes["after_text"], 32)
                if field_name not in {"display_label", "documentation_note"}:
                    return self._classification_reject(
                        connection,
                        owner_id=owner_id,
                        model_id=model_id,
                        wake_id=wake_id,
                        card_id=card_id,
                        requested=requested,
                        fields=[str(field_name)],
                        reason_codes=["major_review_required", "edit_class_upgraded"],
                    )
                if _MAJOR_TYPO_SIGNALS.search(old + new) or before[field_name].count(old) != 1:
                    return self._classification_reject(
                        connection,
                        owner_id=owner_id,
                        model_id=model_id,
                        wake_id=wake_id,
                        card_id=card_id,
                        requested=requested,
                        fields=[field_name],
                        reason_codes=["major_review_required", "edit_class_upgraded"],
                    )
                direct_content = dict(before)
                direct_content[field_name] = before[field_name].replace(old, new, 1)
                direct_reason_codes = ["bounded_typo_applied"]
            elif requested == "metadata" and intent == "revise":
                if not changes or set(changes) - {"display_label", "documentation_note"}:
                    return self._classification_reject(
                        connection,
                        owner_id=owner_id,
                        model_id=model_id,
                        wake_id=wake_id,
                        card_id=card_id,
                        requested=requested,
                        fields=list(changes),
                        reason_codes=["major_review_required", "edit_class_upgraded"],
                    )
                direct_content = dict(before)
                if "display_label" in changes:
                    direct_content["display_label"] = _text(
                        "display_label", changes["display_label"], self.limits.display_label_chars
                    )
                if "documentation_note" in changes:
                    direct_content["documentation_note"] = _text(
                        "documentation_note",
                        changes["documentation_note"],
                        self.limits.documentation_note_chars,
                        allow_empty=True,
                    )
                direct_reason_codes = ["non_retrieval_metadata_updated"]
            elif requested == "source_addition" and intent == "revise":
                if set(changes) != {"source_ref"}:
                    return self._classification_reject(
                        connection,
                        owner_id=owner_id,
                        model_id=model_id,
                        wake_id=wake_id,
                        card_id=card_id,
                        requested=requested,
                        fields=list(changes),
                        reason_codes=["major_review_required", "edit_class_upgraded"],
                    )
                added = _text("source_ref", changes["source_ref"], 2000)
                if added == before.get("source_ref") or added in before["additional_source_refs"]:
                    raise ToolGuidanceError("no_effective_change")
                direct_content = dict(before)
                direct_content["additional_source_refs"] = [
                    *before["additional_source_refs"],
                    added,
                ]
                if len(direct_content["additional_source_refs"]) > 16:
                    raise ToolGuidanceError("additional_source_refs_too_many")
                direct_reason_codes = ["source_appended_without_confidence_change"]
            elif requested == "salience_downweight" and intent == "revise":
                if not changes or set(changes) - {
                    "salience",
                    "auto_recall_mode",
                    "salience_reason",
                }:
                    return self._classification_reject(
                        connection,
                        owner_id=owner_id,
                        model_id=model_id,
                        wake_id=wake_id,
                        card_id=card_id,
                        requested=requested,
                        fields=list(changes),
                        reason_codes=["major_review_required", "edit_class_upgraded"],
                    )
                proposed_salience = changes.get("salience", before["salience"])
                proposed_mode = changes.get("auto_recall_mode", before["auto_recall_mode"])
                if (
                    _integer("salience", proposed_salience, 0, 100) > before["salience"]
                    or _enum("auto_recall_mode", proposed_mode, AUTO_RECALL_MODES)
                    not in AUTO_RECALL_MODES
                    or _AUTO_MODE_ORDER[proposed_mode]
                    > _AUTO_MODE_ORDER[before["auto_recall_mode"]]
                ):
                    return self._classification_reject(
                        connection,
                        owner_id=owner_id,
                        model_id=model_id,
                        wake_id=wake_id,
                        card_id=card_id,
                        requested=requested,
                        fields=list(changes),
                        reason_codes=["major_review_required", "edit_class_upgraded"],
                    )
                salience_reason = _text(
                    "salience_reason", changes.get("salience_reason"), 300
                )
                direct_content = dict(before)
                direct_content.update(
                    {
                        "salience": proposed_salience,
                        "auto_recall_mode": proposed_mode,
                        "salience_reason": salience_reason,
                    }
                )
                direct_reason_codes = ["salience_downweighted"]

            if direct_content is not None:
                if _forbidden_content(direct_content):
                    raise ToolGuidanceError(_forbidden_content(direct_content) or "credential_or_secret_detected")
                diff = self._diff(before, direct_content)
                if not diff:
                    raise ToolGuidanceError("no_effective_change")
                new_version = int(version["version"]) + 1
                now = _iso()
                connection.execute(
                    "UPDATE tool_cards SET current_version = ?, lifecycle = ?, updated_at = ? "
                    "WHERE card_id = ? AND current_version = ?",
                    (
                        new_version,
                        direct_content["lifecycle"],
                        now,
                        card_id,
                        expected_card_version,
                    ),
                )
                inserted = self._insert_version(
                    connection,
                    card_id=card_id,
                    version=new_version,
                    previous_version=expected_card_version,
                    content=direct_content,
                    diff=diff,
                    reason=reason,
                    wake_id=wake_id,
                    requested_edit_class=requested,
                    effective_edit_class=requested,
                    classification_reason_codes=direct_reason_codes,
                    classification_subject={
                        "card_id": card_id,
                        "fields": [item["path"][1:] for item in diff],
                        "direction": "narrow_or_correct",
                    },
                )
                row_version = self._advance_state(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    expected_row_version=expected_row_version,
                )
                event_id = self._insert_audit(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    action="revise_tool_guidance",
                    actor="ai",
                    wake_id=wake_id,
                    card_id=card_id,
                    decision="version_appended",
                    reason_codes=direct_reason_codes,
                    details={"version": new_version, "diff": diff},
                )
                updated_card = self._card_row(connection, owner_id, model_id, card_id)
                return {
                    "decision": "version_appended",
                    "card": self._public_card(updated_card, inserted, catalog=catalog),
                    "diff": diff,
                    "rollback_ref": f"tool-card://{card_id}@{expected_card_version}",
                    "tool_row_version": row_version,
                    "event_id": event_id,
                    "state_changed": True,
                    "execution_performed": False,
                }

            if requested != "major":
                return self._classification_reject(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    wake_id=wake_id,
                    card_id=card_id,
                    requested=requested,
                    fields=list(changes),
                    reason_codes=["major_review_required", "edit_class_upgraded"],
                )

            calm_values = {
                "stability": calm_check_stability,
                "necessity": calm_check_necessity,
                "consequences": calm_check_consequences,
                "alternatives": calm_check_alternatives,
            }
            if any(not isinstance(value, str) or len(value.strip()) < 12 for value in calm_values.values()):
                raise ToolGuidanceError("calm_check_required")
            calm = {key: _text(f"calm_check_{key}", value, 1000) for key, value in calm_values.items()}
            if len({_normalized(value) for value in calm.values()}) != 4:
                raise ToolGuidanceError("calm_check_required")
            correctness = _text(
                "correctness_assessment", correctness_assessment, 2000
            )
            if len(correctness) < 20:
                raise ToolGuidanceError("correctness_assessment_required")

            if intent == "restore":
                if isinstance(target_version, bool) or not isinstance(target_version, int):
                    raise ToolGuidanceError("target_version_required")
                target = self._version_row(connection, card_id, target_version)
                proposed_input = self._content_as_input(_json(target["content_json"], {}))
                proposed_input["lifecycle"] = "active"
                proposed_input.update(changes)
            else:
                proposed_input = self._content_as_input(before)
                proposed_input.update(changes)
                if intent == "retire":
                    proposed_input["lifecycle"] = "retired"
            try:
                proposed = self._normalize_card_content(catalog=catalog, **proposed_input)
            except TypeError as exc:
                raise ToolGuidanceError("invalid_revision_fields") from exc
            self._validate_linked_refs(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                refs=proposed["linked_tool_refs"],
            )
            diff = self._diff(before, proposed)
            if not diff:
                raise ToolGuidanceError("no_effective_change")
            existing = connection.execute(
                "SELECT candidate_id FROM tool_guidance_candidates WHERE owner_id = ? AND model_id = ? "
                "AND card_id = ? AND status = 'pending'",
                (owner_id, model_id, card_id),
            ).fetchone()
            if existing is not None:
                raise ToolGuidanceError("candidate_pending")
            catalog_state = normalize_catalog(catalog)
            candidate_id = _new_id("toolcand")
            creation_row_version = self._state_row(connection, owner_id, model_id)["row_version"]
            candidate_material = {
                "proposed_content": proposed,
                "diff": diff,
                "correctness_assessment": correctness,
                "calm_check": calm,
                "base_version": expected_card_version,
                "creation_tool_row_version": creation_row_version,
                "classification": {
                    "requested": requested,
                    "effective": "major",
                    "rule_version": TOOL_EDIT_CLASSIFICATION_VERSION,
                },
            }
            candidate_hash = _sha256(candidate_material)
            now = _iso()
            connection.execute(
                "INSERT INTO tool_guidance_candidates "
                "(candidate_id, owner_id, model_id, card_id, intent, target_version, base_version, "
                " proposed_content_json, diff_json, reason, correctness_assessment, calm_check_json, "
                " status, submitted_wake_id, submitted_wake_seq, presented_wake_id, presented_wake_seq, "
                " reviewed_wake_id, reviewed_wake_seq, creation_tool_row_version, candidate_hash, "
                " catalog_hash, observed_schema_hash, classification_subject_json, classification_proposer, "
                " classification_decider, requested_edit_class, effective_edit_class, "
                " classification_reason_codes_json, classification_rule_version, expires_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, "
                "'ai', 'runtime_gate', ?, 'major', ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    owner_id,
                    model_id,
                    card_id,
                    intent,
                    target_version,
                    expected_card_version,
                    _canonical(proposed),
                    _canonical(diff),
                    reason,
                    correctness,
                    _canonical(calm),
                    wake_id,
                    wake_seq,
                    creation_row_version,
                    candidate_hash,
                    catalog_state["catalog_hash"],
                    proposed["observed_schema_hash"],
                    _canonical(
                        {
                            "card_id": card_id,
                            "fields": [item["path"][1:] for item in diff],
                            "direction": "major_change",
                        }
                    ),
                    requested,
                    _canonical(["major_review_required"]),
                    TOOL_EDIT_CLASSIFICATION_VERSION,
                    _iso(_now_dt() + timedelta(days=30)),
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
            event_id = self._insert_audit(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                action="revise_tool_guidance",
                actor="ai",
                wake_id=wake_id,
                card_id=card_id,
                candidate_id=candidate_id,
                decision="candidate_pending",
                reason_codes=["major_review_required", "later_real_wake_required"],
                details={
                    "candidate_hash": candidate_hash,
                    "base_version": expected_card_version,
                    "diff": diff,
                },
            )
            return {
                "decision": "candidate_pending",
                "candidate_id": candidate_id,
                "candidate_hash": candidate_hash,
                "base_version": expected_card_version,
                "diff": diff,
                "tool_row_version": row_version,
                "event_id": event_id,
                "state_changed": True,
                "active_version_changed": False,
                "execution_performed": False,
            }

    def present_pending_candidates(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        catalog: Mapping[str, Any] | None,
        ordinary_author: bool = False,
    ) -> list[dict[str, Any]]:
        """Read candidates; only the legacy route binds a real-wake presentation.

        ordinary_author is supplied by the trusted service binding, never by an
        AI-facing tool parameter. An ordinary read does not expire old proposals
        or manufacture real-wake presentation evidence.
        """

        self.ensure_state(owner_id=owner_id, model_id=model_id)
        catalog_state = normalize_catalog(catalog)
        with self._connect() as connection:
            self._begin(connection)
            rows = connection.execute(
                "SELECT * FROM tool_guidance_candidates WHERE owner_id = ? AND model_id = ? "
                "AND status = 'pending' ORDER BY created_at, candidate_id",
                (owner_id, model_id),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                expired = _parse_iso(row["expires_at"]) <= _now_dt()
                if expired and ordinary_author is not True:
                    connection.execute(
                        "UPDATE tool_guidance_candidates SET status = 'expired', updated_at = ? "
                        "WHERE candidate_id = ? AND status = 'pending'",
                        (_iso(), row["candidate_id"]),
                    )
                    continue
                proposed = _json(row["proposed_content_json"], {})
                current_schema = catalog_state["entries"].get(
                    proposed["canonical_tool_name"]
                )
                schema_status = (
                    "unknown"
                    if not catalog_state["catalog_complete"]
                    else "not_advertised"
                    if current_schema is None
                    else "matched"
                    if current_schema == row["observed_schema_hash"]
                    else "stale_schema"
                )
                if ordinary_author is not True:
                    connection.execute(
                        "UPDATE tool_guidance_candidates SET presented_wake_id = ?, "
                        "presented_wake_seq = ?, updated_at = ? WHERE candidate_id = ?",
                        (wake_id, wake_seq, _iso(), row["candidate_id"]),
                    )
                result.append(
                    {
                        "candidate_id": row["candidate_id"],
                        "card_id": row["card_id"],
                        "intent": row["intent"],
                        "target_version": row["target_version"],
                        "base_version": row["base_version"],
                        "proposed_content": proposed,
                        "diff": _json(row["diff_json"], []),
                        "reason": row["reason"],
                        "correctness_assessment": row["correctness_assessment"],
                        "calm_check": _json(row["calm_check_json"], {}),
                        "candidate_hash": row["candidate_hash"],
                        "creation_tool_row_version": row["creation_tool_row_version"],
                        "catalog_hash_at_submit": row["catalog_hash"],
                        "current_catalog_hash": catalog_state["catalog_hash"],
                        "schema_status": schema_status,
                        "review_requires_later_wake": (
                            ordinary_author is not True and wake_seq <= row["submitted_wake_seq"]
                        ),
                        **({"review_mode": "author_confirmation",
                            "presentation_binding_required": False,
                            "time_limit_elapsed": expired} if ordinary_author is True else {}),
                    }
                )
            return result

    def withdraw_candidate(
        self, *, owner_id: str, model_id: str, wake_id: str,
        expected_row_version: int, candidate_id: str, candidate_hash: str,
        expected_base_version: int, reason: str,
    ) -> dict[str, Any]:
        """Exit an old pending proposal, including a stale/offline one."""
        candidate_id = _text("candidate_id", candidate_id, 200)
        reason = _text("reason", reason, 2000)
        if not isinstance(candidate_hash, str) or not _SHA256.fullmatch(candidate_hash):
            raise ToolGuidanceError("candidate_hash_mismatch")
        _integer("expected_base_version", expected_base_version, 1, 2**63 - 1)
        forbidden = _forbidden_content(reason)
        if forbidden:
            raise ToolGuidanceError(forbidden)
        with self._connect() as connection:
            self._begin(connection)
            candidate = connection.execute(
                "SELECT * FROM tool_guidance_candidates WHERE owner_id = ? AND model_id = ? AND candidate_id = ?",
                (owner_id, model_id, candidate_id),
            ).fetchone()
            if candidate is None:
                raise ToolGuidanceError("candidate_not_found")
            if candidate["candidate_hash"] != candidate_hash:
                raise ToolGuidanceError("candidate_hash_mismatch")
            if candidate["base_version"] != expected_base_version:
                raise ToolGuidanceError("candidate_base_changed")
            if candidate["status"] != "pending":
                raise ToolGuidanceError("candidate_not_pending")
            # CAS and namespace checks remain. No current-card equality, catalog,
            # presentation, expiry or later wake is needed to decline a proposal.
            row_version = self._advance_state(connection, owner_id=owner_id, model_id=model_id,
                                              expected_row_version=expected_row_version)
            connection.execute(
                "UPDATE tool_guidance_candidates SET status = 'withdrawn', updated_at = ? WHERE candidate_id = ?",
                (_iso(), candidate_id),
            )
            event_id = self._insert_audit(
                connection, owner_id=owner_id, model_id=model_id,
                action="review_tool_guidance_candidate", actor="ai", wake_id=wake_id,
                card_id=candidate["card_id"], candidate_id=candidate_id, decision="withdraw",
                reason_codes=["author_withdrew_legacy_candidate"],
                details={"reason_hash": _sha256(reason), "independent_review_performed": False},
            )
            return {"decision": "withdraw", "candidate_id": candidate_id,
                    "reason_codes": ["author_withdrew_legacy_candidate"],
                    "tool_row_version": row_version, "event_id": event_id,
                    "state_changed": True, "active_version_changed": False,
                    "execution_performed": False}

    def review_candidate(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_seq: int,
        expected_row_version: int,
        candidate_id: str,
        candidate_hash: str,
        decision: str,
        reason: str,
        expected_base_version: int,
        correctness_decision: str | None = None,
        correctness_assessment: str | None = None,
        ai_confirmation: bool = False,
        catalog: Mapping[str, Any] | None = None,
        ordinary_author: bool = False,
    ) -> dict[str, Any]:
        if decision == "withdraw":
            return self.withdraw_candidate(
                owner_id=owner_id, model_id=model_id, wake_id=wake_id,
                expected_row_version=expected_row_version, candidate_id=candidate_id,
                candidate_hash=candidate_hash, expected_base_version=expected_base_version, reason=reason,
            )
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        candidate_id = _text("candidate_id", candidate_id, 200)
        if not isinstance(candidate_hash, str) or not _SHA256.fullmatch(candidate_hash):
            raise ToolGuidanceError("candidate_hash_mismatch")
        decision = _enum("candidate_decision", decision, CANDIDATE_DECISIONS)
        ordinary_author = ordinary_author is True
        if ordinary_author:
            # accept / keep_pending is the authenticated author's explicit choice.
            # The old correctness fields may be retained as authored commentary,
            # but neither a separate essay nor a synthetic later wake grants it.
            _integer("expected_base_version", expected_base_version, 1, 2**63 - 1)
            assessment = "" if correctness_assessment is None else _text(
                "correctness_assessment", correctness_assessment, 2000, allow_empty=True
            )
        else:
            correctness_decision = _enum(
                "correctness_decision", correctness_decision, CORRECTNESS_DECISIONS
            )
            assessment = _text("correctness_assessment", correctness_assessment, 2000)
        reason = _text("reason", reason, 2000)
        if not ordinary_author and len(assessment) < 20:
            raise ToolGuidanceError("correctness_assessment_required")
        forbidden = _forbidden_content(assessment, reason)
        if forbidden:
            raise ToolGuidanceError(forbidden)
        mapping_valid = (
            (correctness_decision == "correct" and decision == "accept")
            or (correctness_decision == "uncertain" and decision == "keep_pending")
            or (correctness_decision == "incorrect" and decision == "withdraw")
        )
        if not ordinary_author and not mapping_valid:
            raise ToolGuidanceError("correctness_decision_mismatch")
        if not ordinary_author and decision == "accept" and ai_confirmation is not True:
            raise ToolGuidanceError("ai_confirmation_required")

        with self._connect() as connection:
            self._begin(connection)
            candidate = connection.execute(
                "SELECT * FROM tool_guidance_candidates WHERE owner_id = ? AND model_id = ? "
                "AND candidate_id = ?",
                (owner_id, model_id, candidate_id),
            ).fetchone()
            if candidate is None:
                raise ToolGuidanceError("candidate_not_found")
            if candidate["status"] != "pending":
                raise ToolGuidanceError("candidate_not_pending")
            if not ordinary_author and _parse_iso(candidate["expires_at"]) <= _now_dt():
                connection.execute(
                    "UPDATE tool_guidance_candidates SET status = 'expired', updated_at = ? "
                    "WHERE candidate_id = ?",
                    (_iso(), candidate_id),
                )
                return {
                    "decision": "reject",
                    "reason_codes": ["candidate_expired"],
                    "state_changed": False,
                }
            if candidate["candidate_hash"] != candidate_hash:
                raise ToolGuidanceError("candidate_hash_mismatch")
            card, current = self._current_version_row(
                connection, owner_id, model_id, candidate["card_id"]
            )
            if (
                candidate["base_version"] != expected_base_version
                or current["version"] != expected_base_version
            ):
                raise ToolGuidanceError("candidate_base_changed")
            if not ordinary_author and wake_seq <= candidate["submitted_wake_seq"]:
                raise ToolGuidanceError("later_real_wake_required")
            if not ordinary_author and (
                candidate["presented_wake_id"] != wake_id
                or candidate["presented_wake_seq"] != wake_seq
            ):
                raise ToolGuidanceError("candidate_not_fully_presented")
            proposed = _json(candidate["proposed_content_json"], {})
            catalog_state = normalize_catalog(catalog)
            current_schema = catalog_state["entries"].get(
                proposed["canonical_tool_name"]
            )
            if not ordinary_author and not catalog_state["catalog_complete"]:
                raise ToolGuidanceError("live_catalog_unavailable")
            if not ordinary_author and current_schema is None:
                raise ToolGuidanceError("tool_not_advertised")
            if not ordinary_author and current_schema != candidate["observed_schema_hash"]:
                raise ToolGuidanceError("schema_hash_mismatch")
            if not ordinary_author and candidate["catalog_hash"] != catalog_state["catalog_hash"]:
                raise ToolGuidanceError("catalog_hash_mismatch")

            row_version = self._advance_state(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                expected_row_version=expected_row_version,
            )
            if decision == "keep_pending":
                if ordinary_author:
                    connection.execute(
                        "UPDATE tool_guidance_candidates SET updated_at = ? WHERE candidate_id = ?",
                        (_iso(), candidate_id),
                    )
                else:
                    connection.execute(
                        "UPDATE tool_guidance_candidates SET reviewed_wake_id = ?, reviewed_wake_seq = ?, "
                        "presented_wake_id = NULL, presented_wake_seq = NULL, updated_at = ? WHERE candidate_id = ?",
                        (wake_id, wake_seq, _iso(), candidate_id),
                    )
                event_id = self._insert_audit(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    action="review_tool_guidance_candidate",
                    actor="ai",
                    wake_id=wake_id,
                    card_id=card["card_id"],
                    candidate_id=candidate_id,
                    decision="keep_pending",
                    reason_codes=(["author_kept_pending"] if ordinary_author else ["correctness_uncertain"]),
                    details={"assessment_hash": _sha256(assessment),
                             **({"independent_review_performed": False,
                                 "authorization_basis": "ordinary_authenticated"} if ordinary_author else {})},
                )
                return {
                    "decision": "keep_pending",
                    "reason_codes": (["author_kept_pending"] if ordinary_author else ["correctness_uncertain"]),
                    "tool_row_version": row_version,
                    "event_id": event_id,
                    "state_changed": True,
                    "active_version_changed": False,
                    **({"submission_mode": "ordinary_candidate_decision",
                        "independent_review_performed": False} if ordinary_author else {}),
                }
            if decision == "withdraw":
                connection.execute(
                    "UPDATE tool_guidance_candidates SET status = 'withdrawn', reviewed_wake_id = ?, "
                    "reviewed_wake_seq = ?, updated_at = ? WHERE candidate_id = ?",
                    (wake_id, wake_seq, _iso(), candidate_id),
                )
                event_id = self._insert_audit(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    action="review_tool_guidance_candidate",
                    actor="ai",
                    wake_id=wake_id,
                    card_id=card["card_id"],
                    candidate_id=candidate_id,
                    decision="withdraw",
                    reason_codes=["correctness_incorrect"],
                    details={"assessment_hash": _sha256(assessment)},
                )
                return {
                    "decision": "withdraw",
                    "reason_codes": ["correctness_incorrect"],
                    "tool_row_version": row_version,
                    "event_id": event_id,
                    "state_changed": True,
                    "active_version_changed": False,
                }

            new_version = int(current["version"]) + 1
            diff = _json(candidate["diff_json"], [])
            now = _iso()
            connection.execute(
                "UPDATE tool_cards SET canonical_tool_name = ?, operation_key = ?, current_version = ?, "
                "lifecycle = ?, updated_at = ? WHERE card_id = ? AND current_version = ?",
                (
                    proposed["canonical_tool_name"],
                    proposed["operation_key"],
                    new_version,
                    proposed["lifecycle"],
                    now,
                    card["card_id"],
                    expected_base_version,
                ),
            )
            inserted = self._insert_version(
                connection,
                card_id=card["card_id"],
                version=new_version,
                previous_version=expected_base_version,
                content=proposed,
                diff=diff,
                reason=candidate["reason"],
                wake_id=wake_id,
                requested_edit_class=candidate["requested_edit_class"],
                effective_edit_class="major",
                classification_reason_codes=([
                    "ordinary_candidate_accepted"
                ] if ordinary_author else ["cross_wake_review_accepted"]),
                classification_subject=_json(
                    candidate["classification_subject_json"], {}
                ),
            )
            if ordinary_author:
                connection.execute(
                    "UPDATE tool_guidance_candidates SET status = 'accepted', updated_at = ? WHERE candidate_id = ?",
                    (now, candidate_id),
                )
            else:
                connection.execute(
                    "UPDATE tool_guidance_candidates SET status = 'accepted', reviewed_wake_id = ?, "
                    "reviewed_wake_seq = ?, updated_at = ? WHERE candidate_id = ?",
                    (wake_id, wake_seq, now, candidate_id),
                )
            event_id = self._insert_audit(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                action="review_tool_guidance_candidate",
                actor="ai",
                wake_id=wake_id,
                card_id=card["card_id"],
                candidate_id=candidate_id,
                decision="accepted",
                reason_codes=(["author_confirmed", "ordinary_candidate_accepted", "version_appended"]
                              if ordinary_author else ["correctness_confirmed", "version_appended"]),
                details={
                    "version": new_version,
                    "candidate_hash": candidate_hash,
                    "assessment_hash": _sha256(assessment),
                    **({"independent_review_performed": False,
                        "authorization_basis": "ordinary_authenticated"} if ordinary_author else {}),
                },
            )
            updated_card = self._card_row(
                connection, owner_id, model_id, card["card_id"]
            )
            return {
                "decision": "accepted",
                "card": self._public_card(updated_card, inserted, catalog=catalog),
                "rollback_ref": f"tool-card://{card['card_id']}@{expected_base_version}",
                "tool_row_version": row_version,
                "event_id": event_id,
                "state_changed": True,
                "active_version_changed": True,
                "execution_performed": False,
                **({"submission_mode": "ordinary_candidate_acceptance", "author_confirmed": True,
                    "independent_review_performed": False} if ordinary_author else {}),
            }

    def record_experience(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        expected_row_version: int,
        card_id: str,
        outcome: str,
        reason_code: str,
        attempt_summary: str,
        lesson: str | None = None,
        confidence: int = 50,
        catalog: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        card_id = _text("card_id", card_id, 200)
        outcome = _enum("experience_outcome", outcome, EXPERIENCE_OUTCOMES)
        reason_code = _text("reason_code", reason_code, 160)
        attempt = _text("attempt_summary", attempt_summary, 500)
        lesson_value = "" if lesson is None else _text("lesson", lesson, 500, allow_empty=True)
        confidence = _integer("confidence", confidence, 0, 100)
        forbidden = _forbidden_content(reason_code, attempt, lesson_value)
        if forbidden:
            raise ToolGuidanceError(forbidden)
        catalog_state = normalize_catalog(catalog)
        now_dt = _now_dt()
        cooldown: datetime | None = None
        if outcome in {"timeout", "network_error", "provider_error"}:
            cooldown = now_dt + timedelta(minutes=15)
        elif outcome == "unknown":
            cooldown = now_dt + timedelta(minutes=5)
        with self._connect() as connection:
            self._begin(connection)
            card, version = self._current_version_row(
                connection, owner_id, model_id, card_id
            )
            experience_id = _new_id("toolexp")
            observed = catalog_state["entries"].get(card["canonical_tool_name"])
            material = {
                "card_id": card_id,
                "card_version": version["version"],
                "outcome": outcome,
                "reason_code": reason_code,
                "attempt_summary": attempt,
                "lesson": lesson_value,
                "confidence": confidence,
                "provenance": "ai_reported",
                "observed_schema_hash": observed,
                "catalog_hash": catalog_state["catalog_hash"],
                "occurred_at": _iso(now_dt),
                "cooldown_until": _iso(cooldown) if cooldown else None,
            }
            connection.execute(
                "INSERT INTO tool_experiences "
                "(experience_id, owner_id, model_id, card_id, card_version, outcome, reason_code, "
                " attempt_summary, lesson, confidence, provenance, evidence_ref, observed_schema_hash, "
                " catalog_hash, occurred_at, cooldown_until, wake_id, content_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ai_reported', NULL, ?, ?, ?, ?, ?, ?, ?)",
                (
                    experience_id,
                    owner_id,
                    model_id,
                    card_id,
                    version["version"],
                    outcome,
                    reason_code,
                    attempt,
                    lesson_value,
                    confidence,
                    observed,
                    catalog_state["catalog_hash"],
                    material["occurred_at"],
                    material["cooldown_until"],
                    wake_id,
                    _sha256(material),
                    _iso(),
                ),
            )
            row_version = self._advance_state(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                expected_row_version=expected_row_version,
            )
            event_id = self._insert_audit(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                action="record_tool_experience",
                actor="ai",
                wake_id=wake_id,
                card_id=card_id,
                experience_id=experience_id,
                decision="recorded",
                reason_codes=["ai_reported", f"outcome_{outcome}"],
                details={
                    "outcome": outcome,
                    "card_version": version["version"],
                    "cooldown_until": material["cooldown_until"],
                },
            )
            return {
                "decision": "recorded",
                "experience": {
                    "experience_id": experience_id,
                    **material,
                    "evidence_ref": None,
                    "verified": False,
                },
                "tool_row_version": row_version,
                "event_id": event_id,
                "state_changed": True,
                "execution_performed": False,
            }

    @staticmethod
    def _latest_experience_state(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        card_id: str,
        card_version: int,
        catalog_hash: str | None,
    ) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT * FROM tool_experiences WHERE owner_id = ? AND model_id = ? AND card_id = ? "
            "ORDER BY occurred_at DESC, experience_id DESC",
            (owner_id, model_id, card_id),
        ).fetchall()
        if not rows:
            return {
                "suppressed": False,
                "reason_codes": [],
                "call_notes_available": True,
                "failure_factor": 1.0,
            }
        latest = rows[0]
        outcome = latest["outcome"]
        if outcome in {"success", "partial_success", "user_cancelled"}:
            return {
                "suppressed": False,
                "reason_codes": [],
                "call_notes_available": True,
                "failure_factor": 1.0,
            }
        if outcome in {"timeout", "network_error", "provider_error", "unknown"}:
            if latest["cooldown_until"] and _parse_iso(latest["cooldown_until"]) > _now_dt():
                return {
                    "suppressed": True,
                    "reason_codes": ["tool_in_cooldown"],
                    "call_notes_available": True,
                    "failure_factor": 0.0,
                }
            return {
                "suppressed": False,
                "reason_codes": ["recent_failure_downweighted"],
                "call_notes_available": True,
                "failure_factor": 0.6,
            }
        if outcome in {"permission_denied", "unavailable"} and latest["catalog_hash"] == catalog_hash:
            return {
                "suppressed": True,
                "reason_codes": ["catalog_epoch_suppressed"],
                "call_notes_available": True,
                "failure_factor": 0.0,
            }
        if outcome == "invalid_arguments" and latest["card_version"] == card_version:
            return {
                "suppressed": False,
                "reason_codes": ["old_call_notes_suppressed"],
                "call_notes_available": False,
                "failure_factor": 0.6,
            }
        return {
            "suppressed": False,
            "reason_codes": [],
            "call_notes_available": True,
            "failure_factor": 1.0,
        }

    def _candidate_rows(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
    ) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
        cards = connection.execute(
            "SELECT * FROM tool_cards WHERE owner_id = ? AND model_id = ?",
            (owner_id, model_id),
        ).fetchall()
        return [
            (card, self._version_row(connection, card["card_id"], card["current_version"]))
            for card in cards
        ]

    def recall(
        self,
        *,
        owner_id: str,
        model_id: str,
        query: str = "",
        tool_name: str | None = None,
        card_id: str | None = None,
        view: str = "suggestions",
        include_stale: bool = False,
        include_downweighted: bool = False,
        limit: int = 5,
        catalog: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        if view not in {"suggestions", "directory", "card", "history", "failures"}:
            raise ToolGuidanceError("invalid_recall_view")
        limit = _integer("limit", limit, 1, 5)
        query = _text("query", query, 4000, allow_empty=True)
        exact_card = card_id.strip() if isinstance(card_id, str) and card_id.strip() else None
        exact_tool = tool_name.strip() if isinstance(tool_name, str) and tool_name.strip() else None
        exact_version: int | None = None
        alias_query = prepare_explicit_alias_query(query) if not (exact_card or exact_tool) else None

        def alias_evidence(content: Mapping[str, Any]) -> dict[str, Any] | None:
            if alias_query is None:
                return None
            match = explicit_alias_match(alias_query, [
                content.get("purpose", ""), content.get("reminder", ""),
                *content.get("scenario_tags", []), *content.get("scenario_examples", []),
                *content.get("keywords", []), *content.get("aliases", []), *content.get("use_when", []),
            ])
            return ({"retrieval_evidence": match, "retrieval_match": "lexical_alias_candidate",
                     "candidate_only": True, "candidate_score": match["score"]} if match else None)

        with self._connect() as connection:
            if exact_card:
                match = _TOOL_CARD_REF.fullmatch(exact_card)
                if match:
                    exact_card = match.group(1)
                    exact_version = int(match.group(2))
            if view == "directory" or (view == "suggestions" and not query and not exact_card and not exact_tool):
                rows = self._candidate_rows(connection, owner_id=owner_id, model_id=model_id)
                results = []
                alias_results = []
                for card, version in rows:
                    content = _checked_version_content(version)
                    if exact_card and card["card_id"] != exact_card:
                        continue
                    if exact_tool and content["canonical_tool_name"] != exact_tool:
                        continue
                    evidence = None
                    if query and _scene_score(query, content) < 0.5:
                        evidence = alias_evidence(content)
                        if evidence is None:
                            continue
                    item = {"card_id": card["card_id"], "version": version["version"],
                            "ref": f"tool-card://{card['card_id']}@{version['version']}",
                            "display_label": content["display_label"], "lifecycle": card["lifecycle"],
                            "reminder": _reminder_text(content, self.limits.summary_chars)}
                    if evidence:
                        alias_results.append({**item, **evidence})
                    else:
                        results.append(item)
                alias_results.sort(key=lambda item: (-float(item["candidate_score"]), item["card_id"]))
                results.extend(alias_results)
                return {"decision": "directory", "view": "directory", "results": results[:limit],
                        "total": len(results), "truncated": len(results) > limit,
                        "tool_row_version": self._state_row(connection, owner_id, model_id)["row_version"],
                        "next_step": "Use card_id with view=card for original details; narrow query for more cards.",
                        "execution_performed": False}
            if view == "history":
                if not exact_card:
                    raise ToolGuidanceError("card_id_required")
                card = self._card_row(connection, owner_id, model_id, exact_card)
                versions = connection.execute(
                    "SELECT * FROM tool_card_versions WHERE card_id = ? ORDER BY version DESC LIMIT ?",
                    (exact_card, limit),
                ).fetchall()
                return {
                    "decision": "precise_result",
                    "view": view,
                    "card_id": card["card_id"],
                    "versions": [
                        {
                            "version": row["version"],
                            "previous_version": row["previous_version"],
                            "content": _json(row["content_json"], {}),
                            "content_hash": row["content_hash"],
                            "diff": _json(row["diff_json"], []),
                            "reason": row["reason"],
                            "created_at": row["created_at"],
                        }
                        for row in versions
                    ],
                    "guidance_authority": "historical_advice_only",
                    "execution_performed": False,
                }
            if view == "failures":
                if not exact_card:
                    raise ToolGuidanceError("card_id_required")
                self._card_row(connection, owner_id, model_id, exact_card)
                rows = connection.execute(
                    "SELECT * FROM tool_experiences WHERE owner_id = ? AND model_id = ? AND card_id = ? "
                    "AND outcome NOT IN ('success','partial_success') ORDER BY occurred_at DESC LIMIT ?",
                    (owner_id, model_id, exact_card, limit),
                ).fetchall()
                return {
                    "decision": "precise_result",
                    "view": view,
                    "card_id": exact_card,
                    "experiences": [
                        {
                            "experience_id": row["experience_id"],
                            "outcome": row["outcome"],
                            "reason_code": row["reason_code"],
                            "attempt_summary": row["attempt_summary"],
                            "lesson": row["lesson"],
                            "provenance": "ai_reported",
                            "verified": False,
                            "occurred_at": row["occurred_at"],
                        }
                        for row in rows
                    ],
                    "guidance_authority": "historical_advice_only",
                    "execution_performed": False,
                }

            catalog_state = normalize_catalog(catalog)
            candidates: list[tuple[float, sqlite3.Row, sqlite3.Row, dict[str, Any]]] = []
            alias_candidates: list[tuple[float, sqlite3.Row, sqlite3.Row, dict[str, Any]]] = []
            for card, version in self._candidate_rows(
                connection, owner_id=owner_id, model_id=model_id
            ):
                content = _json(version["content_json"], {})
                if exact_card and card["card_id"] != exact_card:
                    continue
                if exact_tool and card["canonical_tool_name"] != exact_tool:
                    continue
                if exact_card and exact_version is not None:
                    version = self._version_row(connection, exact_card, exact_version)
                    content = _checked_version_content(version)
                score = 1.0 if exact_card or exact_tool else _scene_score(query, content)
                evidence = None
                if not (exact_card or exact_tool) and score <= 0:
                    evidence = alias_evidence(content)
                    if evidence is None:
                        continue
                experience = self._latest_experience_state(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    card_id=card["card_id"],
                    card_version=version["version"],
                    catalog_hash=catalog_state["catalog_hash"],
                )
                public = self._public_card(
                    card,
                    version,
                    catalog=catalog,
                    call_notes_available=experience["call_notes_available"],
                )
                if (
                    view == "suggestions"
                    and not (exact_card or exact_tool)
                    and (
                        content["auto_recall_mode"] == "never_auto"
                        or (
                            content["auto_recall_mode"] == "downweighted"
                            and not include_downweighted
                        )
                    )
                ):
                    continue
                if (
                    not (exact_card or exact_tool)
                    and public["effective_status"]
                    in {"retired"}
                    and not include_stale
                ):
                    continue
                if evidence:
                    alias_candidates.append((score, card, version, {**public, **evidence}))
                else:
                    candidates.append((score, card, version, public))
            candidates.sort(
                key=lambda item: (-item[0], -item[3]["content"]["salience"], item[1]["card_id"])
            )
            alias_candidates.sort(key=lambda item: (-float(item[3]["candidate_score"]), item[1]["card_id"]))
            candidates.extend(alias_candidates)
            results = [
                {"semantic_score": round(score, 4), **public}
                for score, _card, _version, public in candidates[:limit]
            ]
            if view == "suggestions" and not (exact_card or exact_tool):
                results = [{"card_id": item["card_id"], "version": item["version"],
                            "ref": f"tool-card://{item['card_id']}@{item['version']}",
                            "reminder": _reminder_text(item["content"], self.limits.summary_chars),
                            **{key: item[key] for key in ("retrieval_evidence", "retrieval_match",
                                                         "candidate_only", "candidate_score") if key in item}}
                           for item in results]
            # A precise card is an explicit request for the original. Never drop
            # that one card silently merely because its complete notes are long.
            if not (exact_card or exact_tool):
                while results and estimate_tokens(results) > self.limits.precise_query_tokens:
                    results.pop()
            return {
                "decision": "precise_result" if exact_card or exact_tool or view == "card" else "suggestions",
                "view": view,
                "results": results,
                "lookup_guidance": {"view": "directory", "query": "", "limit": 5} if not results else None,
                "catalog_complete": catalog_state["catalog_complete"],
                "catalog_hash": catalog_state["catalog_hash"],
                "guidance_authority": "historical_advice_only",
                "permission_authority": "none",
                "execution_performed": False,
            }

    def build_recall_envelopes(
        self,
        *,
        owner_id: str,
        model_id: str,
        query: str,
        catalog: Mapping[str, Any] | None,
        limit: int = 5,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Produce short candidates only; a shared router owns the final budget."""

        external_connection = connection
        if external_connection is None:
            self.ensure_state(owner_id=owner_id, model_id=model_id)
        else:
            self._ensure_state_in_connection(
                external_connection, owner_id=owner_id, model_id=model_id
            )
        query = _text("query", query, 4000, allow_empty=True)
        catalog_state = normalize_catalog(catalog)
        limit = _integer("limit", limit, 1, 5)
        connection_scope = (
            self._connect()
            if external_connection is None
            else nullcontext(external_connection)
        )
        with connection_scope as connection:
            eligible: list[tuple[float, float, sqlite3.Row, sqlite3.Row, dict[str, Any]]] = []
            now = _now_dt()
            for card, version in self._candidate_rows(
                connection, owner_id=owner_id, model_id=model_id
            ):
                content = _json(version["content_json"], {})
                if (card["lifecycle"] != "active" or content["lifecycle"] != "active"
                        or content["auto_recall_mode"] == "never_auto"):
                    continue
                semantic = _scene_score(query, content)
                salience = content["salience"] / 100
                if content["auto_recall_mode"] == "downweighted":
                    salience *= 0.55
                eligible.append((semantic, salience, card, version, content))
            eligible.sort(key=lambda item: (-item[0], -item[1], item[2]["card_id"]))
            selected: list[tuple[float, float, sqlite3.Row, sqlite3.Row, dict[str, Any]]] = []
            for index, item in enumerate(eligible):
                semantic = item[0]
                if semantic >= 0.8:
                    selected.append(item)
                elif (
                    index == 0
                    and semantic >= 0.5
                    and (len(eligible) == 1 or semantic - eligible[1][0] >= 0.12)
                ):
                    selected.append(item)
                if len(selected) >= min(limit, 2):
                    break
            envelopes: list[dict[str, Any]] = []
            for semantic, salience, card, version, content in selected:
                envelope = {
                    "module": "tool_guidance_module",
                    "item_ref": f"tool-card://{card['card_id']}@{version['version']}",
                    "kind": "tool_card_summary",
                    "semantic_score": round(semantic, 4),
                    "salience_score": round(salience, 4),
                    "content": {"scene_summary": _reminder_text(content, self.limits.summary_chars)},
                    "summary_source": "authored_reminder" if content.get("reminder") else "legacy_purpose_excerpt",
                }
                envelope["token_cost"] = estimate_tokens(envelope)
                envelopes.append(envelope)
            if not envelopes:
                self._insert_audit(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    action="tool_guidance_recall_gate",
                    actor="runtime_gate",
                    decision="defer",
                    reason_codes=["no_candidate"],
                    details={
                        "query_hash": _sha256(query),
                        "catalog_hash": catalog_state["catalog_hash"],
                    },
                )
                return {
                    "decision": "defer",
                    "reason_codes": ["no_candidate"],
                    "envelopes": [],
                    "execution_performed": False,
                }
            self._insert_audit(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                action="tool_guidance_recall_gate",
                actor="runtime_gate",
                decision="surface_short_summary",
                reason_codes=["recall_gate_passed"],
                details={
                    "query_hash": _sha256(query),
                    "catalog_hash": catalog_state["catalog_hash"],
                    "item_refs": [item["item_ref"] for item in envelopes],
                },
            )
            return {
                "decision": "surface_short_summary",
                "reason_codes": ["recall_gate_passed"],
                "envelopes": envelopes,
                "execution_performed": False,
            }

    def execution_gate(
        self,
        *,
        owner_id: str,
        model_id: str,
        card_id: str,
        catalog: Mapping[str, Any] | None,
        current_user_intent: bool,
        authorization_verified: bool,
        current_confirmation: bool,
    ) -> dict[str, Any]:
        """Read-only attempt gate.  It never calls or proxies the target tool."""

        self.ensure_state(owner_id=owner_id, model_id=model_id)
        catalog_state = normalize_catalog(catalog)
        with self._connect() as connection:
            card, version = self._current_version_row(
                connection, owner_id, model_id, card_id
            )
            content = _json(version["content_json"], {})
            current_schema = catalog_state["entries"].get(
                content["canonical_tool_name"]
            )
            if (
                not catalog_state["catalog_complete"]
                or current_schema is None
                or current_schema != content["observed_schema_hash"]
                or card["lifecycle"] != "active"
                or _card_expired(content["expires_at"])
            ):
                return {
                    "decision": "denied",
                    "reason_codes": ["execution_gate_denied"],
                    "execution_performed": False,
                }
            if not current_user_intent or not authorization_verified:
                return {
                    "decision": "denied",
                    "reason_codes": ["execution_gate_denied"],
                    "execution_performed": False,
                }
            if (
                content["confirmation_policy"] == "explicit_each_time"
                and not current_confirmation
            ):
                return {
                    "decision": "confirmation_required",
                    "reason_codes": ["execution_gate_confirmation_required"],
                    "execution_performed": False,
                }
            return {
                "decision": "allowed_to_attempt",
                "reason_codes": ["current_catalog_schema_permission_checked"],
                "tool_name": content["canonical_tool_name"],
                "operation_key": content["operation_key"],
                "execution_performed": False,
            }
