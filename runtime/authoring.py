"""Shared, content-neutral authoring helpers for memory modules two to four.

The helpers in this module deliberately do not decide which narrative voice an
AI should use.  Referent bindings are optional metadata, unresolved bindings
remain valid, and rewrite assistance is an explicit per-draft opt-in.  The
rewrite preview can only apply literal replacements selected by the caller; it
does not infer entities, rewrite grammar, or polish prose.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class AuthoringError(ValueError):
    """Stable validation error for the shared authoring layer."""


AUTHORING_REWRITE_CONTRACT_VERSION = "authoring-rewrite/1"
AUTHORING_REWRITE_MODULES: dict[str, frozenset[str]] = {
    "emotional_memory_module_two": frozenset({"/original_text", "/summary"}),
    "learning_memory_module_three": frozenset(
        {"/title", "/summary", "/current_understanding", "/preceding_context_summary"}
    ),
    "tool_guidance_module": frozenset(
        {
            "/display_label",
            "/completion_rule",
            "/purpose",
            "/call_notes",
            "/documentation_note",
            "/salience_reason",
            "/handoff_condition",
        }
    ),
}
AUTHORING_SCHEMA_VERSIONS = {
    "emotional_memory_module_two": "emotional-memory/0.1.1",
    "learning_memory_module_three": "learning-memory/0.3",
    "tool_guidance_module": "tool-guidance/0.2",
}
REWRITE_ELIGIBLE_ALLOWLIST_VERSION = "person-rewrite-allowlist/1"
MENTION_PARSER_RULE_VERSION = "literal-person-reference/1"
ALIAS_COMPARISON_PROFILE_VERSION = "alias-comparison/nfc-casefold/1"

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\b(?:sk|api|ghp|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b", re.I),
    re.compile(
        r"\b(?:password|passwd|api[_ -]?key|secret|token|cookie)\s*[:=]", re.I
    ),
    re.compile(r"(?:密码|口令|私钥|令牌|密钥)\s*[:：=]", re.I),
)


AUTHORING_ADVISORY: dict[str, Any] = {
    "source": "host_advisory",
    "advisory_strength": "optional",
    "evidence_status": "plausible_not_guaranteed",
    "message": (
        "语言模型可能复用上下文中的词形，含糊的第三人称有时会增加错认人物的概率。"
        "如果我希望降低这种风险，可以考虑使用第一人称自述并配合稳定人物名字；"
        "这不能保证消除错误，我也可以继续使用第三人称或其他叙事方式。"
    ),
    "rewrite_assist_default": False,
    "rewrite_assist_scope": "current_draft_only",
}

REFERENT_RESOLUTION_STATUSES = frozenset({"resolved", "unresolved", "ambiguous"})
_BINDING_FIELDS = frozenset(
    {
        "field_path",
        "surface_form",
        "occurrence_index",
        "entity_ref",
        "resolution_status",
        "confidence",
    }
)
_REWRITE_TARGET_FIELDS = frozenset(
    {
        "field_path",
        "surface_form",
        "occurrence_index",
        "entity_ref",
        "target_surface_form",
    }
)
_BIDI_OR_ISOLATE_CODEPOINTS = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)
_SENTENCE_PUNCTUATION = frozenset("。！？!?；;：:,，\r\n\t")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    payload = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _required_text(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthoringError(f"{name}_required")
    result = value.strip()
    if len(result) > maximum:
        raise AuthoringError(f"{name}_too_long")
    return result


def _field_paths(value: Iterable[str]) -> frozenset[str]:
    try:
        paths = frozenset(value)
    except TypeError as exc:
        raise AuthoringError("allowed_field_paths_required") from exc
    if not paths or any(
        not isinstance(path, str) or not path.startswith("/") or len(path) > 200
        for path in paths
    ):
        raise AuthoringError("invalid_allowed_field_paths")
    return paths


def validate_referent_bindings(
    value: Any, allowed_field_paths: Iterable[str]
) -> list[dict[str, Any]]:
    """Validate optional referent metadata without resolving ambiguous mentions.

    ``occurrence_index`` is zero based and identifies a literal occurrence of
    ``surface_form`` inside ``field_path``.  Structural validation is strict,
    while ``unresolved`` and ``ambiguous`` are ordinary accepted states.  The
    caller may separately check that an occurrence exists in its current draft.
    """

    allowed = _field_paths(allowed_field_paths)
    if value is None:
        return []
    if not isinstance(value, list):
        raise AuthoringError("referent_bindings_must_be_array")
    if len(value) > 64:
        raise AuthoringError("referent_bindings_too_many")

    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise AuthoringError("referent_binding_must_be_object")
        unknown = set(raw) - _BINDING_FIELDS
        if unknown:
            raise AuthoringError("referent_binding_unknown_fields")

        field_path = raw.get("field_path")
        if not isinstance(field_path, str) or field_path not in allowed:
            raise AuthoringError("referent_binding_invalid_field_path")
        surface_form = _required_text(
            "referent_binding_surface_form", raw.get("surface_form"), 200
        )
        occurrence_index = raw.get("occurrence_index")
        if (
            isinstance(occurrence_index, bool)
            or not isinstance(occurrence_index, int)
            or occurrence_index < 0
        ):
            raise AuthoringError("referent_binding_invalid_occurrence_index")
        resolution_status = raw.get("resolution_status")
        if resolution_status not in REFERENT_RESOLUTION_STATUSES:
            raise AuthoringError("referent_binding_invalid_resolution_status")
        confidence = raw.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int)
            or not 0 <= confidence <= 100
        ):
            raise AuthoringError("referent_binding_invalid_confidence")

        entity_ref = raw.get("entity_ref")
        if resolution_status == "resolved":
            entity_ref = _required_text("referent_binding_entity_ref", entity_ref, 300)
        elif entity_ref is not None:
            entity_ref = _required_text("referent_binding_entity_ref", entity_ref, 300)

        identity = (field_path, surface_form, occurrence_index)
        if identity in seen:
            raise AuthoringError("referent_binding_duplicate")
        seen.add(identity)
        result.append(
            {
                "field_path": field_path,
                "surface_form": surface_form,
                "occurrence_index": occurrence_index,
                "entity_ref": entity_ref,
                "resolution_status": resolution_status,
                "confidence": confidence,
            }
        )
    return result


def referent_warnings(bindings: Sequence[Mapping[str, Any]] | None) -> list[str]:
    """Return non-blocking advisory reason codes for unresolved bindings."""

    statuses = {
        binding.get("resolution_status")
        for binding in (bindings or [])
        if isinstance(binding, Mapping)
    }
    warnings: list[str] = []
    if "ambiguous" in statuses:
        warnings.append("ambiguous_referent")
    if "unresolved" in statuses:
        warnings.append("unresolved_referent")
    return warnings


def _literal_occurrence_span(text: str, surface: str, occurrence_index: int) -> tuple[int, int] | None:
    start = 0
    found = -1
    for _ in range(occurrence_index + 1):
        found = text.find(surface, start)
        if found < 0:
            return None
        start = found + len(surface)
    return found, found + len(surface)


def _safe_target_surface(value: Any) -> str:
    target = _required_text("target_surface_form", value, 80)
    if any(character in _SENTENCE_PUNCTUATION for character in target):
        raise AuthoringError("target_surface_form_must_be_single_lexical_form")
    for character in target:
        if character in _BIDI_OR_ISOLATE_CODEPOINTS:
            raise AuthoringError("target_surface_form_contains_unsafe_unicode")
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"}:
            raise AuthoringError("target_surface_form_contains_unsafe_unicode")
    return target


def _validated_draft_fields(
    value: Any, allowed_field_paths: frozenset[str]
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise AuthoringError("draft_fields_must_be_object")
    if set(value) - allowed_field_paths:
        raise AuthoringError("draft_fields_contain_ineligible_path")
    result: dict[str, str] = {}
    for path, text in value.items():
        if not isinstance(text, str):
            raise AuthoringError("draft_field_must_be_string")
        result[path] = text
    return result


def prepare_rewrite_preview(
    *,
    draft_fields: Mapping[str, str],
    referent_bindings: Any = None,
    rewrite_targets: Any = None,
    rewrite_assist: bool = False,
    allowed_field_paths: Iterable[str] = ("/original_text", "/summary"),
) -> dict[str, Any]:
    """Build a deterministic, side-effect-free literal mention preview.

    A target must repeat the exact binding coordinates and entity reference and
    must provide the desired lexical form.  Only ``resolved`` bindings are
    eligible.  Ambiguous and unresolved entries are skipped with advisory codes
    rather than rejected.  With assistance disabled (the default), this
    function intentionally does not inspect the draft or bindings.
    """

    if not isinstance(rewrite_assist, bool):
        raise AuthoringError("rewrite_assist_must_be_boolean")
    if not rewrite_assist:
        return {
            "rewrite_assist": False,
            "rewrite_preview_status": "disabled",
            "patches": [],
            "warnings": [],
        }

    allowed = _field_paths(allowed_field_paths)
    draft = _validated_draft_fields(draft_fields, allowed)
    bindings = validate_referent_bindings(referent_bindings, allowed)
    warnings = referent_warnings(bindings)
    if rewrite_targets is None:
        rewrite_targets = []
    if not isinstance(rewrite_targets, list):
        raise AuthoringError("rewrite_targets_must_be_array")
    if len(rewrite_targets) > 64:
        raise AuthoringError("rewrite_targets_too_many")

    bindings_by_identity = {
        (
            binding["field_path"],
            binding["surface_form"],
            binding["occurrence_index"],
        ): binding
        for binding in bindings
    }
    proposed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for index, raw in enumerate(rewrite_targets):
        if not isinstance(raw, Mapping):
            skipped.append({"target_index": index, "reason_code": "invalid_rewrite_target"})
            continue
        if set(raw) - _REWRITE_TARGET_FIELDS:
            skipped.append({"target_index": index, "reason_code": "invalid_rewrite_target_fields"})
            continue
        field_path = raw.get("field_path")
        surface_form = raw.get("surface_form")
        occurrence_index = raw.get("occurrence_index")
        identity = (field_path, surface_form, occurrence_index)
        binding = bindings_by_identity.get(identity)
        if binding is None:
            skipped.append({"target_index": index, "reason_code": "binding_not_found"})
            continue
        if binding["resolution_status"] != "resolved":
            skipped.append(
                {
                    "target_index": index,
                    "reason_code": f"referent_{binding['resolution_status']}",
                }
            )
            continue
        if raw.get("entity_ref") != binding["entity_ref"]:
            skipped.append({"target_index": index, "reason_code": "entity_ref_mismatch"})
            continue
        source_text = draft.get(binding["field_path"])
        if source_text is None:
            skipped.append({"target_index": index, "reason_code": "draft_field_missing"})
            continue
        span = _literal_occurrence_span(
            source_text, binding["surface_form"], binding["occurrence_index"]
        )
        if span is None:
            skipped.append({"target_index": index, "reason_code": "source_occurrence_missing"})
            continue
        try:
            target_surface = _safe_target_surface(raw.get("target_surface_form"))
        except AuthoringError as exc:
            skipped.append({"target_index": index, "reason_code": str(exc)})
            continue
        if target_surface == binding["surface_form"]:
            skipped.append({"target_index": index, "reason_code": "no_lexical_change"})
            continue
        char_start, char_end = span
        byte_start = len(source_text[:char_start].encode("utf-8"))
        byte_end = len(source_text[:char_end].encode("utf-8"))
        proposed.append(
            {
                "target_index": index,
                "field_path": binding["field_path"],
                "char_start": char_start,
                "char_end": char_end,
                "byte_start": byte_start,
                "byte_end": byte_end,
                "source_span_hash": _sha256(binding["surface_form"]),
                "expected_surface_form": binding["surface_form"],
                "entity_ref": binding["entity_ref"],
                "target_surface_form": target_surface,
            }
        )

    conflicting: set[int] = set()
    for left_index, left in enumerate(proposed):
        for right_index in range(left_index + 1, len(proposed)):
            right = proposed[right_index]
            if left["field_path"] != right["field_path"]:
                continue
            if left["char_start"] < right["char_end"] and right["char_start"] < left["char_end"]:
                conflicting.update({left_index, right_index})
    patches: list[dict[str, Any]] = []
    for index, patch in enumerate(proposed):
        if index in conflicting:
            skipped.append(
                {"target_index": patch["target_index"], "reason_code": "overlapping_patch"}
            )
        else:
            patches.append(patch)

    if not patches:
        return {
            "rewrite_assist": True,
            "rewrite_preview_status": "no_applicable_change",
            "source": "host_rewrite_suggestion",
            "patches": [],
            "skipped": sorted(skipped, key=lambda item: item["target_index"]),
            "warnings": warnings,
        }

    suggested = dict(draft)
    for field_path in sorted({patch["field_path"] for patch in patches}):
        text = suggested[field_path]
        field_patches = sorted(
            (patch for patch in patches if patch["field_path"] == field_path),
            key=lambda patch: patch["char_start"],
            reverse=True,
        )
        for patch in field_patches:
            text = (
                text[: patch["char_start"]]
                + patch["target_surface_form"]
                + text[patch["char_end"] :]
            )
        suggested[field_path] = text

    public_patches = [
        {key: value for key, value in patch.items() if key not in {"target_index", "char_start", "char_end"}}
        for patch in sorted(patches, key=lambda item: (item["field_path"], item["byte_start"]))
    ]
    return {
        "rewrite_assist": True,
        "rewrite_preview_status": "changes_available",
        "source": "host_rewrite_suggestion",
        "source_draft_hash": _sha256(draft),
        "suggestion_hash": _sha256(suggested),
        "suggested_fields": suggested,
        "patches": public_patches,
        "skipped": sorted(skipped, key=lambda item: item["target_index"]),
        "warnings": warnings,
        "approved_span_only": True,
        "requires_ai_confirmation": True,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _walk_text(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_text(item)
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            yield from _walk_text(item)


def _contains_secret(*values: Any) -> bool:
    return any(
        pattern.search(text)
        for value in values
        for text in _walk_text(value)
        for pattern in _SECRET_PATTERNS
    )


def _safe_unicode_lexeme(value: str) -> bool:
    """Fail closed on scalar/control forms unsafe for a literal mention patch."""

    if unicodedata.normalize("NFC", value) != value:
        return False
    for character in value:
        codepoint = ord(character)
        category = unicodedata.category(character)
        if (
            0xD800 <= codepoint <= 0xDFFF
            or character in _BIDI_OR_ISOLATE_CODEPOINTS
            or character == "\u200d"
            or 0xFE00 <= codepoint <= 0xFE0F
            or category in {"Cc", "Cf", "Cs", "Mn", "Mc", "Me"}
        ):
            return False
    return True


def _canonical_validation_context(
    *,
    module: str,
    conversation_mode: Any,
    authenticated_participant_entity_ids: Any,
    alias_collision_scope: Any,
    alias_collision_scope_version: Any,
    protected_spans: Any,
    module_schema_version: Any,
    rewrite_eligible_allowlist_version: Any,
    mention_parser_rule_version: Any,
    alias_comparison_profile_version: Any,
) -> dict[str, Any]:
    if module not in AUTHORING_REWRITE_MODULES:
        raise AuthoringError("rewrite_module_invalid")
    if conversation_mode not in {"one_to_one", "group", "unknown"}:
        raise AuthoringError("conversation_mode_invalid")
    if not isinstance(authenticated_participant_entity_ids, list) or len(
        authenticated_participant_entity_ids
    ) > 32:
        raise AuthoringError("authenticated_participants_invalid")
    participants: list[str] = []
    for value in authenticated_participant_entity_ids:
        item = _required_text("authenticated_participant_entity_id", value, 300)
        if item not in participants:
            participants.append(item)
    collision_scope = _required_text(
        "alias_collision_scope", alias_collision_scope, 300
    )
    if (
        isinstance(alias_collision_scope_version, bool)
        or not isinstance(alias_collision_scope_version, int)
        or alias_collision_scope_version < 1
    ):
        raise AuthoringError("alias_collision_scope_version_invalid")
    if module_schema_version != AUTHORING_SCHEMA_VERSIONS[module]:
        raise AuthoringError("module_schema_version_stale")
    if rewrite_eligible_allowlist_version != REWRITE_ELIGIBLE_ALLOWLIST_VERSION:
        raise AuthoringError("rewrite_allowlist_version_stale")
    if mention_parser_rule_version != MENTION_PARSER_RULE_VERSION:
        raise AuthoringError("mention_parser_rule_version_stale")
    if alias_comparison_profile_version != ALIAS_COMPARISON_PROFILE_VERSION:
        raise AuthoringError("alias_comparison_profile_version_stale")
    if protected_spans is None:
        protected_spans = []
    if not isinstance(protected_spans, list) or len(protected_spans) > 128:
        raise AuthoringError("protected_spans_invalid")
    normalized_spans: list[dict[str, Any]] = []
    for raw in protected_spans:
        if not isinstance(raw, Mapping) or set(raw) != {
            "field_path",
            "byte_start",
            "byte_end",
        }:
            raise AuthoringError("protected_span_shape_invalid")
        path = raw.get("field_path")
        start = raw.get("byte_start")
        end = raw.get("byte_end")
        if path not in AUTHORING_REWRITE_MODULES[module]:
            raise AuthoringError("protected_span_field_invalid")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
        ):
            raise AuthoringError("protected_span_range_invalid")
        normalized_spans.append(
            {"field_path": path, "byte_start": start, "byte_end": end}
        )
    normalized_spans.sort(
        key=lambda item: (item["field_path"], item["byte_start"], item["byte_end"])
    )
    return {
        "conversation_mode": conversation_mode,
        "authenticated_participant_entity_ids": sorted(participants),
        "alias_collision_scope": collision_scope,
        "alias_collision_scope_version": alias_collision_scope_version,
        "protected_spans": normalized_spans,
        "module_schema_version": module_schema_version,
        "rewrite_eligible_allowlist_version": rewrite_eligible_allowlist_version,
        "mention_parser_rule_version": mention_parser_rule_version,
        "alias_comparison_profile_version": alias_comparison_profile_version,
    }


def _strict_targets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AuthoringError("rewrite_targets_must_be_array")
    if len(value) > 64:
        raise AuthoringError("rewrite_targets_too_many")
    required = {
        "field_path",
        "surface_form",
        "occurrence_index",
        "entity_ref",
        "target_surface_form",
        "mention_kind",
        "target_alias_ref",
        "target_alias_version",
        "unique_in_scope",
    }
    result: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != required:
            raise AuthoringError("rewrite_target_shape_invalid")
        if raw.get("mention_kind") not in {
            "pronoun",
            "person_name",
            "relationship_name",
        }:
            raise AuthoringError("mention_kind_invalid")
        unique_in_scope = raw.get("unique_in_scope")
        if not isinstance(unique_in_scope, bool):
            raise AuthoringError("unique_in_scope_must_be_boolean")
        alias_ref = _required_text("target_alias_ref", raw.get("target_alias_ref"), 300)
        alias_version = raw.get("target_alias_version")
        if (
            isinstance(alias_version, bool)
            or not isinstance(alias_version, int)
            or alias_version < 1
        ):
            raise AuthoringError("target_alias_version_invalid")
        source = _required_text("rewrite_surface_form", raw.get("surface_form"), 200)
        target = _safe_target_surface(raw.get("target_surface_form"))
        if not _safe_unicode_lexeme(source) or not _safe_unicode_lexeme(target):
            raise AuthoringError("rewrite_lexeme_contains_unsafe_unicode")
        if any(character.isspace() for character in source) or any(
            character.isspace() for character in target
        ):
            raise AuthoringError("rewrite_lexeme_must_be_single_form")
        result.append(
            {
                "field_path": raw["field_path"],
                "surface_form": source,
                "occurrence_index": raw["occurrence_index"],
                "entity_ref": raw["entity_ref"],
                "target_surface_form": target,
                "mention_kind": raw["mention_kind"],
                "target_alias_ref": alias_ref,
                "target_alias_version": alias_version,
                "unique_in_scope": unique_in_scope,
            }
        )
    return result


class AuthoringRewriteStore:
    """Owner-scoped preview/confirmation evidence for optional literal rewrites.

    Preview rows are isolated from live recall.  Confirmation creates an opaque
    one-use receipt; a module-two/three/four store consumes that receipt in the
    same SQLite transaction that creates the canonical memory.  Failed or stale
    suggestions therefore cannot leave a half-confirmed canonical write.
    """

    def __init__(self, database: str | Path, *, receipt_secret: str | bytes) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        secret = (
            receipt_secret.encode("utf-8")
            if isinstance(receipt_secret, str)
            else bytes(receipt_secret)
        )
        if len(secret) < 32:
            raise ValueError("receipt_secret must contain at least 32 bytes")
        self._receipt_secret = secret
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterable[sqlite3.Connection]:
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
                CREATE TABLE IF NOT EXISTS authoring_rewrite_state (
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    row_version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (owner_id, model_id)
                );

                CREATE TABLE IF NOT EXISTS authoring_rewrite_previews (
                    preview_id TEXT PRIMARY KEY,
                    lineage_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    module TEXT NOT NULL,
                    wake_id TEXT NOT NULL,
                    draft_version INTEGER NOT NULL,
                    source_json TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    suggestion_json TEXT NOT NULL,
                    suggestion_hash TEXT NOT NULL,
                    bindings_json TEXT NOT NULL,
                    bindings_hash TEXT NOT NULL,
                    patches_json TEXT NOT NULL,
                    validation_context_json TEXT NOT NULL,
                    validation_context_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_authoring_preview_owner
                    ON authoring_rewrite_previews(owner_id, model_id, wake_id, module, status);

                CREATE TABLE IF NOT EXISTS authoring_rewrite_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    preview_id TEXT NOT NULL UNIQUE REFERENCES authoring_rewrite_previews(preview_id),
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    module TEXT NOT NULL,
                    wake_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    source_hash TEXT NOT NULL,
                    suggestion_hash TEXT NOT NULL,
                    final_hash TEXT NOT NULL,
                    final_json TEXT NOT NULL,
                    validation_context_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    canonical_ref TEXT,
                    request_hash TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_authoring_receipt_owner
                    ON authoring_rewrite_receipts(owner_id, model_id, module, status);
                """
            )

    @staticmethod
    def _state(
        connection: sqlite3.Connection, *, owner_id: str, model_id: str
    ) -> sqlite3.Row:
        connection.execute(
            "INSERT OR IGNORE INTO authoring_rewrite_state "
            "(owner_id, model_id, row_version, updated_at) VALUES (?, ?, 0, ?)",
            (owner_id, model_id, _utc_now()),
        )
        row = connection.execute(
            "SELECT * FROM authoring_rewrite_state WHERE owner_id = ? AND model_id = ?",
            (owner_id, model_id),
        ).fetchone()
        assert row is not None
        return row

    @staticmethod
    def _advance(
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        expected_row_version: int,
    ) -> int:
        if (
            isinstance(expected_row_version, bool)
            or not isinstance(expected_row_version, int)
            or expected_row_version < 0
        ):
            raise AuthoringError("expected_authoring_version_invalid")
        cursor = connection.execute(
            "UPDATE authoring_rewrite_state SET row_version = row_version + 1, updated_at = ? "
            "WHERE owner_id = ? AND model_id = ? AND row_version = ?",
            (_utc_now(), owner_id, model_id, expected_row_version),
        )
        if cursor.rowcount != 1:
            raise AuthoringError("authoring_version_conflict")
        return expected_row_version + 1

    def status(self, *, owner_id: str, model_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            state = connection.execute(
                "SELECT row_version FROM authoring_rewrite_state "
                "WHERE owner_id = ? AND model_id = ?",
                (owner_id, model_id),
            ).fetchone()
            return {
                "contract_version": AUTHORING_REWRITE_CONTRACT_VERSION,
                "row_version": int(state["row_version"]) if state is not None else 0,
                "rewrite_assist_default": False,
                "rewrite_assist_scope": "current_draft_only",
            }

    def preview(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        expected_row_version: int,
        module: str,
        draft_version: int,
        draft_fields: Mapping[str, str],
        referent_bindings: Any,
        rewrite_targets: Any,
        conversation_mode: Any,
        authenticated_participant_entity_ids: Any,
        alias_collision_scope: Any,
        alias_collision_scope_version: Any,
        protected_spans: Any,
        module_schema_version: Any,
        rewrite_eligible_allowlist_version: Any,
        mention_parser_rule_version: Any,
        alias_comparison_profile_version: Any,
    ) -> dict[str, Any]:
        if (
            isinstance(draft_version, bool)
            or not isinstance(draft_version, int)
            or draft_version < 0
        ):
            raise AuthoringError("draft_version_invalid")
        context = _canonical_validation_context(
            module=module,
            conversation_mode=conversation_mode,
            authenticated_participant_entity_ids=authenticated_participant_entity_ids,
            alias_collision_scope=alias_collision_scope,
            alias_collision_scope_version=alias_collision_scope_version,
            protected_spans=protected_spans,
            module_schema_version=module_schema_version,
            rewrite_eligible_allowlist_version=rewrite_eligible_allowlist_version,
            mention_parser_rule_version=mention_parser_rule_version,
            alias_comparison_profile_version=alias_comparison_profile_version,
        )
        allowed = AUTHORING_REWRITE_MODULES[module]
        draft = _validated_draft_fields(draft_fields, allowed)
        bindings = validate_referent_bindings(referent_bindings, allowed)
        if _contains_secret(draft, bindings, rewrite_targets):
            raise AuthoringError("credential_or_secret_detected")
        targets = _strict_targets(rewrite_targets)

        # Host-authenticated context may veto; it never creates a binding or
        # chooses a target.  Group/unknown scenes and participant mismatches
        # simply skip the suggestion while preserving the ordinary save path.
        participant_set = set(context["authenticated_participant_entity_ids"])
        eligible_targets = []
        context_skips: list[dict[str, Any]] = []
        for index, target in enumerate(targets):
            if target["unique_in_scope"] is not True:
                context_skips.append(
                    {"target_index": index, "reason_code": "alias_not_unique_in_scope"}
                )
                continue
            if context["conversation_mode"] != "one_to_one":
                context_skips.append(
                    {"target_index": index, "reason_code": "conversation_context_not_safe"}
                )
                continue
            if target["entity_ref"] not in participant_set:
                context_skips.append(
                    {"target_index": index, "reason_code": "participant_not_authenticated"}
                )
                continue
            eligible_targets.append(
                {
                    key: target[key]
                    for key in _REWRITE_TARGET_FIELDS
                    if key in target
                }
            )

        preview = prepare_rewrite_preview(
            draft_fields=draft,
            referent_bindings=bindings,
            rewrite_targets=eligible_targets,
            rewrite_assist=True,
            allowed_field_paths=allowed,
        )
        preview.setdefault("skipped", []).extend(context_skips)
        preview["skipped"] = sorted(
            preview["skipped"], key=lambda item: item.get("target_index", 0)
        )
        if preview["rewrite_preview_status"] != "changes_available":
            return {
                **preview,
                "decision": "continue_original_path",
                "receipt_required": False,
                "state_changed": False,
                "authoring_row_version": expected_row_version,
            }

        protected = context["protected_spans"]
        safe_patches: list[dict[str, Any]] = []
        protected_skips: list[dict[str, Any]] = []
        for index, patch in enumerate(preview["patches"]):
            overlap = any(
                patch["field_path"] == span["field_path"]
                and patch["byte_start"] < span["byte_end"]
                and span["byte_start"] < patch["byte_end"]
                for span in protected
            )
            if overlap:
                protected_skips.append(
                    {"target_index": index, "reason_code": "protected_span"}
                )
            else:
                safe_patches.append(patch)
        if len(safe_patches) != len(preview["patches"]):
            # Rebuild from the source using only non-protected literal patches.
            suggested = dict(draft)
            for field_path in sorted({p["field_path"] for p in safe_patches}):
                text = suggested[field_path]
                for patch in sorted(
                    (p for p in safe_patches if p["field_path"] == field_path),
                    key=lambda item: item["byte_start"],
                    reverse=True,
                ):
                    source_bytes = text.encode("utf-8")
                    text = (
                        source_bytes[: patch["byte_start"]]
                        + patch["target_surface_form"].encode("utf-8")
                        + source_bytes[patch["byte_end"] :]
                    ).decode("utf-8")
                suggested[field_path] = text
            preview["patches"] = safe_patches
            preview["suggested_fields"] = suggested
            preview["suggestion_hash"] = _sha256(suggested)
            preview["skipped"].extend(protected_skips)
        if not preview["patches"]:
            return {
                **preview,
                "rewrite_preview_status": "no_applicable_change",
                "decision": "continue_original_path",
                "receipt_required": False,
                "state_changed": False,
                "authoring_row_version": expected_row_version,
            }
        if _contains_secret(preview["suggested_fields"]):
            raise AuthoringError("credential_or_secret_detected")

        preview_id = f"rwprev_{uuid.uuid4().hex}"
        lineage_id = f"rwline_{uuid.uuid4().hex}"
        validation_hash = _sha256(context)
        bindings_hash = _sha256(bindings)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._state(connection, owner_id=owner_id, model_id=model_id)
            new_version = self._advance(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                expected_row_version=expected_row_version,
            )
            connection.execute(
                "INSERT INTO authoring_rewrite_previews "
                "(preview_id, lineage_id, owner_id, model_id, module, wake_id, draft_version, "
                "source_json, source_hash, suggestion_json, suggestion_hash, bindings_json, "
                "bindings_hash, patches_json, validation_context_json, validation_context_hash, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', ?)",
                (
                    preview_id,
                    lineage_id,
                    owner_id,
                    model_id,
                    module,
                    wake_id,
                    draft_version,
                    _canonical(draft),
                    preview["source_draft_hash"],
                    _canonical(preview["suggested_fields"]),
                    preview["suggestion_hash"],
                    _canonical(bindings),
                    bindings_hash,
                    _canonical(preview["patches"]),
                    _canonical(context),
                    validation_hash,
                    _utc_now(),
                ),
            )
        return {
            **preview,
            "preview_id": preview_id,
            "lineage_id": lineage_id,
            "draft_version": draft_version,
            "validation_context_hash": validation_hash,
            "referent_bindings_hash": bindings_hash,
            "decision": "preview_only",
            "receipt_required": True,
            "authoring_row_version": new_version,
            "state_changed": True,
        }

    def _token_for(self, receipt_id: str) -> str:
        proof = hmac.new(
            self._receipt_secret,
            b"authoring-rewrite-receipt/v1\x00" + receipt_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{receipt_id}.{proof}"

    def confirm(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        expected_row_version: int,
        preview_id: str,
        expected_source_draft_hash: str,
        expected_suggestion_hash: str,
        expected_validation_context_hash: str,
        final_fields: Mapping[str, str],
        final_fields_hash: str,
        ai_confirmation: bool,
        conversation_mode: Any,
        authenticated_participant_entity_ids: Any,
        alias_collision_scope: Any,
        alias_collision_scope_version: Any,
        protected_spans: Any,
        module_schema_version: Any,
        rewrite_eligible_allowlist_version: Any,
        mention_parser_rule_version: Any,
        alias_comparison_profile_version: Any,
    ) -> dict[str, Any]:
        if ai_confirmation is not True:
            raise AuthoringError("ai_confirmation_required")
        preview_id = _required_text("preview_id", preview_id, 200)
        if not isinstance(final_fields, Mapping):
            raise AuthoringError("final_fields_must_be_object")
        if _contains_secret(final_fields):
            raise AuthoringError("credential_or_secret_detected")
        computed_final_hash = _sha256(dict(final_fields))
        if final_fields_hash != computed_final_hash:
            raise AuthoringError("final_fields_hash_mismatch")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._state(connection, owner_id=owner_id, model_id=model_id)
            preview = connection.execute(
                "SELECT * FROM authoring_rewrite_previews WHERE preview_id = ? "
                "AND owner_id = ? AND model_id = ?",
                (preview_id, owner_id, model_id),
            ).fetchone()
            if preview is None:
                raise AuthoringError("rewrite_preview_not_found")
            if preview["wake_id"] != wake_id:
                raise AuthoringError("rewrite_preview_wrong_wake")
            current_context = _canonical_validation_context(
                module=preview["module"],
                conversation_mode=conversation_mode,
                authenticated_participant_entity_ids=authenticated_participant_entity_ids,
                alias_collision_scope=alias_collision_scope,
                alias_collision_scope_version=alias_collision_scope_version,
                protected_spans=protected_spans,
                module_schema_version=module_schema_version,
                rewrite_eligible_allowlist_version=rewrite_eligible_allowlist_version,
                mention_parser_rule_version=mention_parser_rule_version,
                alias_comparison_profile_version=alias_comparison_profile_version,
            )
            if _sha256(current_context) != preview["validation_context_hash"]:
                raise AuthoringError("validation_context_stale")
            if preview["source_hash"] != expected_source_draft_hash:
                raise AuthoringError("rewrite_source_stale")
            if preview["suggestion_hash"] != expected_suggestion_hash:
                raise AuthoringError("rewrite_suggestion_stale")
            if preview["validation_context_hash"] != expected_validation_context_hash:
                raise AuthoringError("validation_context_stale")
            if computed_final_hash != preview["suggestion_hash"]:
                raise AuthoringError("final_fields_must_match_preview")
            if preview["status"] == "confirmed":
                receipt = connection.execute(
                    "SELECT * FROM authoring_rewrite_receipts WHERE preview_id = ?",
                    (preview_id,),
                ).fetchone()
                assert receipt is not None
                if receipt["final_hash"] != computed_final_hash:
                    raise AuthoringError("rewrite_confirmation_conflict")
                state = self._state(connection, owner_id=owner_id, model_id=model_id)
                return {
                    "decision": "confirmed",
                    "rewrite_receipt": self._token_for(receipt["receipt_id"]),
                    "receipt_status": receipt["status"],
                    "final_fields_hash": receipt["final_hash"],
                    "authoring_row_version": int(state["row_version"]),
                    "state_changed": False,
                    "idempotent_replay": True,
                }
            if preview["status"] != "available":
                raise AuthoringError("rewrite_preview_not_available")
            new_version = self._advance(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                expected_row_version=expected_row_version,
            )
            receipt_id = f"rwrcpt_{uuid.uuid4().hex}"
            token = self._token_for(receipt_id)
            now = _utc_now()
            connection.execute(
                "INSERT INTO authoring_rewrite_receipts "
                "(receipt_id, preview_id, owner_id, model_id, module, wake_id, token_hash, "
                "source_hash, suggestion_hash, final_hash, final_json, validation_context_hash, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)",
                (
                    receipt_id,
                    preview_id,
                    owner_id,
                    model_id,
                    preview["module"],
                    wake_id,
                    _sha256(token),
                    preview["source_hash"],
                    preview["suggestion_hash"],
                    computed_final_hash,
                    _canonical(dict(final_fields)),
                    preview["validation_context_hash"],
                    now,
                ),
            )
            connection.execute(
                "UPDATE authoring_rewrite_previews SET status = 'confirmed', confirmed_at = ? "
                "WHERE preview_id = ?",
                (now, preview_id),
            )
        return {
            "decision": "confirmed",
            "rewrite_receipt": token,
            "receipt_status": "ready",
            "final_fields_hash": computed_final_hash,
            "authoring_row_version": new_version,
            "state_changed": True,
            "idempotent_replay": False,
        }


def claim_rewrite_receipt(
    connection: sqlite3.Connection,
    *,
    receipt_token: str | None,
    owner_id: str,
    model_id: str,
    wake_id: str,
    module: str,
    final_fields: Mapping[str, str],
    request_payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Claim a ready receipt inside the caller's canonical-write transaction."""

    if receipt_token is None:
        # Exact exposed suggestions cannot silently enter through the ordinary
        # path merely by omitting lineage/receipt metadata.
        suggestion_hash = _sha256(dict(final_fields))
        try:
            exposed = connection.execute(
                "SELECT 1 FROM authoring_rewrite_previews WHERE owner_id = ? AND model_id = ? "
                "AND module = ? AND wake_id = ? AND suggestion_hash = ? "
                "AND status IN ('available', 'confirmed') LIMIT 1",
                (owner_id, model_id, module, wake_id, suggestion_hash),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).casefold():
                raise
            exposed = None
        if exposed is not None:
            raise AuthoringError("rewrite_receipt_required_for_exposed_suggestion")
        return None
    if not isinstance(receipt_token, str) or not receipt_token.strip():
        raise AuthoringError("rewrite_receipt_invalid")
    token = receipt_token.strip()
    receipt = connection.execute(
        "SELECT * FROM authoring_rewrite_receipts WHERE token_hash = ?",
        (_sha256(token),),
    ).fetchone()
    if receipt is None:
        raise AuthoringError("rewrite_receipt_invalid")
    if (
        receipt["owner_id"] != owner_id
        or receipt["model_id"] != model_id
        or receipt["module"] != module
        or receipt["wake_id"] != wake_id
    ):
        raise AuthoringError("rewrite_receipt_binding_mismatch")
    final_hash = _sha256(dict(final_fields))
    request_hash = _sha256(dict(request_payload))
    if final_hash != receipt["final_hash"]:
        raise AuthoringError("rewrite_receipt_final_mismatch")
    if receipt["status"] == "consumed":
        if receipt["request_hash"] != request_hash:
            raise AuthoringError("rewrite_receipt_replay_conflict")
        return {
            "replayed": True,
            "result": json.loads(receipt["result_json"]),
            "receipt_id": receipt["receipt_id"],
        }
    if receipt["status"] != "ready":
        raise AuthoringError("rewrite_receipt_not_ready")
    return {
        "replayed": False,
        "receipt_id": receipt["receipt_id"],
        "request_hash": request_hash,
        "source_hash": receipt["source_hash"],
        "suggestion_hash": receipt["suggestion_hash"],
        "final_hash": receipt["final_hash"],
        "validation_context_hash": receipt["validation_context_hash"],
        "preview_id": receipt["preview_id"],
    }


def finalize_rewrite_receipt(
    connection: sqlite3.Connection,
    *,
    claim: Mapping[str, Any] | None,
    canonical_ref: str,
    result: Mapping[str, Any],
) -> None:
    """Finalize receipt evidence in the same transaction as canonical storage."""

    if claim is None or claim.get("replayed") is True:
        return
    now = _utc_now()
    cursor = connection.execute(
        "UPDATE authoring_rewrite_receipts SET status = 'consumed', canonical_ref = ?, "
        "request_hash = ?, result_json = ?, consumed_at = ? "
        "WHERE receipt_id = ? AND status = 'ready'",
        (
            canonical_ref,
            claim["request_hash"],
            _canonical(dict(result)),
            now,
            claim["receipt_id"],
        ),
    )
    if cursor.rowcount != 1:
        raise AuthoringError("rewrite_receipt_concurrent_consumption")
    connection.execute(
        "UPDATE authoring_rewrite_previews SET status = 'adopted' WHERE preview_id = ?",
        (claim["preview_id"],),
    )


__all__ = [
    "ALIAS_COMPARISON_PROFILE_VERSION",
    "AUTHORING_ADVISORY",
    "AUTHORING_REWRITE_CONTRACT_VERSION",
    "AUTHORING_REWRITE_MODULES",
    "AUTHORING_SCHEMA_VERSIONS",
    "AuthoringRewriteStore",
    "AuthoringError",
    "MENTION_PARSER_RULE_VERSION",
    "REFERENT_RESOLUTION_STATUSES",
    "REWRITE_ELIGIBLE_ALLOWLIST_VERSION",
    "claim_rewrite_receipt",
    "finalize_rewrite_receipt",
    "prepare_rewrite_preview",
    "referent_warnings",
    "validate_referent_bindings",
]
