"""Pure-Python service facade for the module-one self-model runtime.

This module deliberately contains no network or authentication code. It keeps
the MCP surface small, owner-scoped, and testable without starting a server.
The frozen runtime remains the only component allowed to mutate self-model
state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from mcp_server.public_contract import (
    BRAIN_MANUAL_MODULES,
    PUBLIC_CONTRACT_VERSION,
    STAGE_INTENT_ROUTES,
    PublicPayloadValidationError,
    current_action_contract,
    validate_public_payload,
)
from runtime import (
    SERVER_DERIVED_CANDIDATE_MARKER,
    ModuleOneOnboardingStore,
    SelfModelStore,
    SelfRevisionError,
)
from .governance_service import SelfGovernanceAccessService


SearchScope = Literal["active", "candidates", "revisions", "events", "all"]
QueryView = Literal["status", "active", "search", "edit_basis"]

_PRIVATE_BINDING_KEYS = frozenset(
    {
        "wake_id",
        "wake_seq",
        "wake_capability",
        "challenge_response",
        "grant_ref",
        "direct_grant_ref",
        SERVER_DERIVED_CANDIDATE_MARKER,
    }
)

_PUBLIC_INTENT_ACTIONS = {
    "save_calm_prompt": "save_calm_prompt",
    "submit": "submit_candidate",
    "accept_review": "accept_candidate_review",
    "revise": "revise_candidate",
    "respond_to_objection": "respond_to_objection",
    "withdraw": "withdraw_candidate",
    "begin_edit": "begin_edit",
    "confirm_edit": "confirm_edit",
    "cancel_edit": "cancel_edit",
    "recover": "recover_candidate",
}

_CONTENT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "schemas" / "self-model.schema.json"
)

_BLANK_CONTENT_STRUCTURE: dict[str, Any] = {
    "schema_version": "0.1.0",
    "boot_anchor": {"text": None},
    "active_identity_capsule": {
        "name_and_identity": None,
        "personality_foundation": None,
        "expression_style": None,
        "behavioral_principles": [],
        "core_identity_anchors": [],
        "self_revision_safety_prompt": None,
    },
    "facets": {},
    "anchor_references": [],
}


def _load_content_schema() -> dict[str, Any]:
    try:
        return json.loads(_CONTENT_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SelfRevisionError("self-model content schema is unavailable") from exc


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _object_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"type": _value_type(value)}
    return {"type": "object", "keys": sorted(str(key) for key in value)}


def _strip_private_binding_fields(value: Any) -> Any:
    """Remove host-only capability material from every AI-facing result shape."""
    if isinstance(value, dict):
        return {
            key: _strip_private_binding_fields(item)
            for key, item in value.items()
            if key not in _PRIVATE_BINDING_KEYS
        }
    if isinstance(value, list):
        return [_strip_private_binding_fields(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_private_binding_fields(item) for item in value]
    return value


def _array_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, list):
        return {"type": _value_type(value)}
    return {
        "type": "array",
        "item_count": len(value),
        "item_types": sorted({_value_type(item) for item in value}),
    }


def _first_invalid_anchor_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, list):
        return None
    required = {"anchor_id", "memory_ref", "meaning"}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            return {"index": index, "type": _value_type(item)}
        keys = set(item)
        invalid_values = sorted(
            key
            for key in required
            if not isinstance(item.get(key), str) or not item.get(key, "").strip()
        )
        if keys != required or invalid_values:
            return {
                "index": index,
                "type": "object",
                "keys": sorted(str(key) for key in keys),
                "missing_keys": sorted(required - keys),
                "unexpected_keys": sorted(keys - required),
                "empty_or_non_string_fields": invalid_values,
            }
    return None


def _content_validation_help(
    content: Any, reason_codes: list[str]
) -> dict[str, Any] | None:
    content_codes = {
        "invalid_content_structure",
        "invalid_boot_anchor",
        "invalid_identity_capsule",
        "invalid_facets",
        "invalid_anchor_references",
        "boot_anchor_too_long",
        "identity_capsule_too_long",
        "facet_too_long",
        "total_content_too_long",
    }
    relevant = [code for code in reason_codes if code in content_codes]
    if not relevant:
        return None

    errors: list[dict[str, Any]] = []
    content_object = content if isinstance(content, dict) else {}
    required_top = {
        "schema_version",
        "boot_anchor",
        "active_identity_capsule",
        "facets",
        "anchor_references",
    }

    if "invalid_content_structure" in relevant:
        received: dict[str, Any] = {"type": _value_type(content)}
        if isinstance(content, dict):
            keys = set(content)
            received.update(
                {
                    "keys": sorted(str(key) for key in keys),
                    "missing_keys": sorted(required_top - keys),
                    "unexpected_keys": sorted(keys - required_top),
                    "schema_version_type": _value_type(content.get("schema_version")),
                }
            )
        errors.append(
            {
                "reason_code": "invalid_content_structure",
                "field": "content",
                "expected": {
                    "type": "object",
                    "exact_keys": sorted(required_top),
                    "schema_version": "0.1.0",
                    "additional_properties": False,
                },
                "received": received,
                "blank_structure": _BLANK_CONTENT_STRUCTURE,
            }
        )

    if "invalid_boot_anchor" in relevant:
        errors.append(
            {
                "reason_code": "invalid_boot_anchor",
                "field": "content.boot_anchor",
                "expected": {
                    "type": "object",
                    "exact_keys": ["text"],
                    "text": "non-empty string",
                },
                "received": _object_summary(content_object.get("boot_anchor")),
            }
        )

    if "invalid_identity_capsule" in relevant:
        capsule = content_object.get("active_identity_capsule")
        received = _object_summary(capsule)
        if isinstance(capsule, dict):
            required_strings = {
                "name_and_identity",
                "personality_foundation",
                "expression_style",
                "self_revision_safety_prompt",
            }
            required_lists = {"behavioral_principles", "core_identity_anchors"}
            expected_keys = required_strings | required_lists
            received.update(
                {
                    "missing_keys": sorted(expected_keys - set(capsule)),
                    "unexpected_keys": sorted(set(capsule) - expected_keys),
                    "field_types": {
                        key: _value_type(capsule.get(key)) for key in sorted(expected_keys)
                    },
                }
            )
        errors.append(
            {
                "reason_code": "invalid_identity_capsule",
                "field": "content.active_identity_capsule",
                "expected": {
                    "type": "object",
                    "exact_keys": [
                        "behavioral_principles",
                        "core_identity_anchors",
                        "expression_style",
                        "name_and_identity",
                        "personality_foundation",
                        "self_revision_safety_prompt",
                    ],
                    "string_fields": [
                        "name_and_identity",
                        "personality_foundation",
                        "expression_style",
                        "self_revision_safety_prompt",
                    ],
                    "non_empty_string_array_fields": [
                        "behavioral_principles",
                        "core_identity_anchors",
                    ],
                },
                "received": received,
            }
        )

    if "invalid_facets" in relevant:
        errors.append(
            {
                "reason_code": "invalid_facets",
                "field": "content.facets",
                "expected": {
                    "type": "object",
                    "max_properties": 16,
                    "key_pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$",
                    "values": "non-empty strings",
                },
                "received": _object_summary(content_object.get("facets")),
            }
        )

    if "invalid_anchor_references" in relevant:
        anchors = content_object.get("anchor_references")
        received = _array_summary(anchors)
        first_invalid = _first_invalid_anchor_summary(anchors)
        if first_invalid is not None:
            received["first_invalid_item"] = first_invalid
        errors.append(
            {
                "reason_code": "invalid_anchor_references",
                "field": "content.anchor_references",
                "expected": {
                    "type": "array",
                    "max_items": 32,
                    "each_item": {
                        "type": "object",
                        "exact_keys": ["anchor_id", "memory_ref", "meaning"],
                        "all_values": "non-empty strings",
                    },
                },
                "received": received,
            }
        )

    length_fields = {
        "boot_anchor_too_long": ("content.boot_anchor.text", 500),
        "identity_capsule_too_long": (
            "content.active_identity_capsule (canonical JSON)",
            4000,
        ),
        "facet_too_long": ("content.facets.<name>", 2500),
        "total_content_too_long": ("content (canonical JSON)", 16000),
    }
    for code, (field, maximum) in length_fields.items():
        if code in relevant:
            errors.append(
                {
                    "reason_code": code,
                    "field": field,
                    "expected": {"maximum_characters": maximum},
                    "received": "See length_metrics in this response.",
                }
            )

    return {
        "candidate_persisted": False,
        "schema_tool": "stbrain_open",
        "schema_arguments": {"view": "manual", "module": "self_revision"},
        "workspace_required": False,
        "retry_hint": (
            "Correct only the fields identified below; author the wording yourself, "
            "with no required first word. In the same real wake, retain your current "
            "write_context_ref and use the current row_version returned by this result. "
            "A new wake needs its own open context. If you need the schema, call "
            "stbrain_open(view='manual', module='self_revision') directly via MCP; "
            "no shell, workspace file, or public idempotency key is required. "
            "The blank structure is a shape, not text to adopt or submit unchanged."
        ),
        "errors": errors,
    }


def _optional_revision(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SelfRevisionError("expected_active_revision must be a string or null")
    value = value.strip()
    return value or None


def _json_value(value: str, field: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SelfRevisionError(f"stored {field} is invalid JSON") from exc


def _clean_revision(row: dict[str, Any], *, include_content: bool) -> dict[str, Any]:
    cleaned = {key: value for key, value in row.items() if key != "content_json"}
    if not include_content:
        cleaned.pop("content", None)
    return cleaned


def _clean_candidate(row: dict[str, Any], *, include_content: bool) -> dict[str, Any]:
    hidden = {"content_json", "diff_json", "evidence_refs_json", "length_metrics_json"}
    cleaned = {key: value for key, value in row.items() if key not in hidden}
    cleaned["length_metrics"] = _json_value(
        row["length_metrics_json"], "candidate length metrics"
    )
    if not include_content:
        cleaned.pop("content", None)
        cleaned.pop("reason", None)
    else:
        cleaned["diff"] = _json_value(row["diff_json"], "candidate diff")
        cleaned["evidence_refs"] = _json_value(
            row["evidence_refs_json"], "candidate evidence refs"
        )
    return cleaned


def _clean_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if key not in {"reason_codes_json", "details_json"}
    }


def _matches(value: Any, query: str) -> bool:
    if not query:
        return True
    return query.casefold() in json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str
    ).casefold()


class SelfModelAccessService:
    """Owner-scoped facade used by one authenticated MCP principal.

    ``model_id`` and ``owner_id`` are configuration, not tool arguments. This
    prevents an authenticated client from changing identity namespaces by
    supplying a different identifier in a tool call.
    """

    def __init__(
        self,
        store: SelfModelStore,
        *,
        model_id: str,
        owner_id: str,
        onboarding: ModuleOneOnboardingStore | None = None,
        emotional: Any | None = None,
        learning: Any | None = None,
        tool_guidance: Any | None = None,
        authoring_rewrite: Any | None = None,
        governance: Any | None = None,
        injection_control: Any | None = None,
        planning: Any | None = None,
        hallucination_vault: Any | None = None,
        direct_client_principal: str = "official-deepseek-direct",
    ) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must not be empty")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        self.store = store
        self.model_id = model_id.strip()
        self.owner_id = owner_id.strip()
        self.onboarding = onboarding
        self.emotional = emotional
        self.learning = learning
        self.tool_guidance = tool_guidance
        self.authoring_rewrite = authoring_rewrite
        self.governance = governance
        self.injection_control = injection_control
        self.planning = planning
        self.hallucination_vault = hallucination_vault
        if (
            not isinstance(direct_client_principal, str)
            or not direct_client_principal.strip()
        ):
            raise ValueError("direct_client_principal must not be empty")
        self.direct_client_principal = direct_client_principal.strip()
        if (
            self.governance is None
            and self.onboarding is not None
            and getattr(self.onboarding, "governance_store", None) is not None
        ):
            self.governance = SelfGovernanceAccessService(
                self.onboarding.governance_store,
                onboarding=self.onboarding,
                owner_id=self.owner_id,
                model_id=self.model_id,
            )
        if self.onboarding is not None:
            self.onboarding.ensure_state(
                owner_id=self.owner_id,
                model_id=self.model_id,
            )

    def _onboarding_status(self) -> dict[str, Any]:
        if self.onboarding is None:
            raise SelfRevisionError("module-one onboarding gate is not configured")
        return self.onboarding.state(owner_id=self.owner_id, model_id=self.model_id)

    def _progression_required(self, reason: str = "progression_required") -> dict[str, Any]:
        status = self._onboarding_status()
        state = status["state"]
        return {
            "module": status["module"],
            "flow_version": status["flow_version"],
            "model_id": self.model_id,
            "decision": "progression_required",
            "reason_codes": [reason],
            "current_stage": state["stage"],
            "why_locked": (
                "模块一必须按当前阶段继续；旧工具名、checkpoint_id、刷新和重试都不能跳步。"
            ),
            "allowed_actions": status["allowed_actions"],
            "next_action": status["next_action"],
            "wake_boundary_required": status["wake_boundary_required"],
            "state_changed": False,
            "pointer_changed": False,
        }

    def _public_gate_denial(self, reason: str) -> dict[str, Any]:
        result = self._progression_required(reason)
        result["decision"] = reason
        return result

    @staticmethod
    def _action_for_public_intent(stage: str, intent: str) -> str | None:
        """Resolve one reviewed public intent without trusting a client action name."""
        if intent not in STAGE_INTENT_ROUTES.get(stage, ()):
            return None
        if intent == "acknowledge":
            return (
                "confirm_brain_intro"
                if stage == "factory"
                else "confirm_module_intro"
            )
        return _PUBLIC_INTENT_ACTIONS.get(intent)

    def _public_payload_denial(
        self,
        *,
        intent: str,
        issues: tuple[dict[str, Any], ...],
        status: dict[str, Any],
    ) -> dict[str, Any]:
        """Return field-addressed, value-free validation feedback with zero mutation."""
        reason_codes = [
            str(issue.get("code") or "invalid_public_payload") for issue in issues
        ] or ["invalid_public_payload"]
        # Keep the established edit-consent boundary stable while still
        # returning the new field-addressed public-contract issues.
        denial_reason = (
            "edit_consent_required"
            if intent == "confirm_edit"
            else reason_codes[0]
        )
        result = self._public_gate_denial(denial_reason)
        result["reason_codes"] = list(dict.fromkeys(reason_codes))
        result["requested_intent"] = intent
        result["validation_issues"] = [dict(issue) for issue in issues]
        state = status["state"]
        result["current_action_contract"] = current_action_contract(
            str(state["stage"]),
            allowed_actions=status.get("allowed_actions"),
            base_revision_id=state.get("base_revision_id"),
        )
        return _strip_private_binding_fields(result)

    def _bound_public_write(
        self,
        *,
        action: str | None = None,
        intent: str | None = None,
        write_context_ref: str,
        expected_row_version: int | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind first, then validate one public mutation without exposing capabilities."""
        if self.onboarding is None:
            raise SelfRevisionError("module-one onboarding gate is not configured")
        if not isinstance(write_context_ref, str) or not write_context_ref.strip():
            return self._public_gate_denial("brain_open_required")
        binding = self.onboarding.current_open_write_context(
            owner_id=self.owner_id,
            model_id=self.model_id,
            write_context_ref=write_context_ref.strip(),
            required_scope="self_revision",
        )
        if binding.get("write_context_available") is not True:
            return self._public_gate_denial(
                str(binding.get("reason_code") or "brain_open_required")
            )

        # Do not reveal detailed payload feedback until the caller has proved this
        # exact wake was injected and voluntarily opened.  Host-only binding fields
        # and the server-derived candidate marker are never accepted from a client.
        forbidden = sorted(set(payload) & _PRIVATE_BINDING_KEYS)
        if forbidden:
            return self._public_gate_denial("server_bound_context_required")

        action_payload = dict(payload)
        if intent is not None:
            status = self._onboarding_status()
            stage = str(status["state"]["stage"])
            action = self._action_for_public_intent(stage, intent)
            if action is None:
                result = self._progression_required(
                    "real_wake_boundary_required"
                    if stage == "candidate_wait"
                    else "progression_required"
                )
                result["requested_intent"] = intent
                return result
            try:
                action_payload = validate_public_payload(intent, payload)
            except PublicPayloadValidationError as exc:
                return self._public_payload_denial(
                    intent=intent,
                    issues=exc.issues,
                    status=status,
                )

            if intent == "acknowledge":
                action_payload = {"acknowledged": True}
            elif intent in {"submit", "revise"}:
                # Public /7 candidates contain only AI-authored content and reason.
                # Diff, evidence and active-base binding are derived after this ref
                # has been authenticated, inside the authoritative runtime.
                action_payload = {
                    "content": action_payload["content"],
                    "reason": action_payload["reason"],
                    SERVER_DERIVED_CANDIDATE_MARKER: True,
                }

        if action is None:
            raise SelfRevisionError("public write action is not configured")
        if action == "confirm_edit":
            challenge_id = action_payload.get("challenge_id")
            if (
                not isinstance(challenge_id, str)
                or not challenge_id.strip()
                or action_payload.get("ai_confirmation") is not True
            ):
                return self._public_gate_denial("edit_consent_required")
            challenge = self.onboarding.current_edit_challenge_response(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                challenge_id=challenge_id.strip(),
            )
            if challenge.get("challenge_available") is not True:
                return self._public_gate_denial(
                    str(challenge.get("reason_code") or "edit_consent_required")
                )
            action_payload.pop("ai_confirmation", None)
            action_payload["challenge_id"] = challenge_id.strip()
            action_payload["challenge_response"] = challenge["challenge_response"]
        result = self.continue_module_one(
            action=action,
            wake_id=binding["wake_id"],
            wake_capability=binding["wake_capability"],
            expected_row_version=expected_row_version,
            payload=action_payload,
        )
        if intent in {"submit", "revise"} and result.get("decision") in {"reject", "revise"}:
            # The binding and authoritative transaction are checked first. This
            # projection explains a rejection; it cannot create or retry a write.
            validation_help = _content_validation_help(
                action_payload.get("content"), result.get("reason_codes", [])
            )
            if validation_help is not None:
                result["validation_help"] = validation_help
        return _strip_private_binding_fields(result)

    def _current_candidate_matches(self, candidate_id: str) -> bool:
        status = self._onboarding_status()
        return status["state"]["current_candidate_id"] == candidate_id

    def module_one_status(self) -> dict[str, Any]:
        """Return the single authoritative onboarding state and next legal action."""
        if self.onboarding is None:
            prepared = self.prepare()
            return {
                "module": prepared["module"],
                "flow_version": "legacy-runtime/1",
                "model_id": self.model_id,
                "decision": "legacy_mode",
                "state": {"stage": "legacy", "row_version": None},
                "allowed_actions": [],
                "next_action": "Configure the module-one onboarding gate.",
                "wake_boundary_required": False,
                "module_one_unlocked": prepared["active_revision"] is not None,
            }
        return self._onboarding_status()

    def health(self) -> dict[str, Any]:
        """Return a content-free health summary for the owner-scoped brain."""
        status = self.module_one_status()
        result = {
            "service": "stiller-brain",
            "status": "ok",
            "model_id": self.model_id,
            "module_one": {
                "stage": status["state"]["stage"],
                "unlocked": status["module_one_unlocked"],
                "row_version": status["state"]["row_version"],
            },
            "counts": {
                "candidates": len(self.store.list_candidates(self.model_id)),
                "revisions": len(self.store.list_revisions(self.model_id)),
                "events": len(self.store.list_events(self.model_id)),
            },
            "content_exposed": False,
        }
        if self.emotional is not None:
            emotional_status = self.emotional.status()
            result["module_two"] = {
                "status": (
                    emotional_status["status"]
                    if status["module_one_unlocked"]
                    else "locked"
                ),
                "row_version": emotional_status["row_version"],
                "counts": emotional_status["counts"],
            }
        if self.learning is not None:
            learning_status = self.learning.status()
            result["module_three"] = {
                "status": (
                    learning_status["status"]
                    if status["module_one_unlocked"]
                    else "locked"
                ),
                "row_version": learning_status["row_version"],
                "counts": learning_status["counts"],
            }
        if self.tool_guidance is not None:
            tool_status = self.tool_guidance.status()
            result["module_four"] = {
                "status": (
                    tool_status["status"]
                    if status["module_one_unlocked"]
                    else "locked"
                ),
                "row_version": tool_status["row_version"],
                "counts": tool_status["counts"],
            }
        if self.governance is not None:
            governance_status = self.governance.status()
            result["self_governance"] = {
                "status": (
                    "available"
                    if status["module_one_unlocked"]
                    else "locked"
                ),
                "configured_scope_count": governance_status[
                    "configured_scope_count"
                ],
                "scope_versions": {
                    scope: item["row_version"]
                    for scope, item in governance_status["scopes"].items()
                },
                "content_exposed": False,
            }
        if self.authoring_rewrite is not None:
            authoring_status = self.authoring_rewrite.status()
            result["shared_person_authoring"] = {
                "status": "available" if status["module_one_unlocked"] else "locked",
                "row_version": authoring_status["row_version"],
                "rewrite_assist_default": False,
                "content_exposed": False,
            }
        if self.injection_control is not None:
            injection_status = self.injection_control.status()
            result["injection_control"] = {
                "status": "available" if status["module_one_unlocked"] else "locked",
                "global_mode": injection_status["global_mode"],
                "scope_versions": {
                    scope: item["row_version"]
                    for scope, item in injection_status["scopes"].items()
                },
                "content_exposed": False,
            }
        if self.planning is not None:
            planning_status = self.planning.status()
            result["module_five"] = {
                "status": (
                    planning_status["status"]
                    if status["module_one_unlocked"]
                    else "locked"
                ),
                "row_version": planning_status["row_version"],
                "counts": planning_status["counts"],
                "content_exposed": False,
            }
        if self.hallucination_vault is not None:
            vault_status = self.hallucination_vault.status()
            result["hallucination_vault"] = {
                "status": "available" if status["module_one_unlocked"] else "locked",
                "automatic_exposure": vault_status["automatic_exposure"],
                "row_version": vault_status["row_version"],
                "content_exposed": False,
            }
        return result

    def open_brain(
        self,
        *,
        direct_grant_ref: str | None = None,
        view: str = "full",
        module: str = "self_revision",
        page: int = 0,
        expected_material_hash: str | None = None,
    ) -> dict[str, Any]:
        """Open the brain manual and current continuation at the AI's choice.

        ``direct_grant_ref`` is an internal facade argument used only by
        :meth:`open_brain_direct`.  Ordinary ``stbrain_open`` never receives or
        looks up a pending human grant.

        Internal/direct callers retain the legacy full response. The public MCP
        wrapper explicitly defaults to summary. A selected manual presents only
        that module; never build all effectful manuals and then trim the output.
        """
        if view not in {"full", "summary", "manual", "review"}:
            raise SelfRevisionError("invalid_brain_open_view")
        if module not in BRAIN_MANUAL_MODULES:
            raise SelfRevisionError("invalid_brain_manual_module")
        if direct_grant_ref is not None and view != "full":
            raise SelfRevisionError("direct_open_requires_full_view")
        if type(page) is not int or page < 0:
            raise SelfRevisionError("invalid_review_page")
        if view != "review" and (page != 0 or expected_material_hash is not None):
            raise SelfRevisionError("review_page_arguments_require_review_view")
        if view == "review" and module != "self_revision":
            raise SelfRevisionError("review_view_only_supports_self_revision")
        if self.onboarding is None:
            open_context = {
                "continuation": None,
                "write_context_available": False,
                "reason_code": "module_one_onboarding_gate_not_configured",
            }
        else:
            # Opening may intentionally advance candidate_wait to candidate_review.
            # Do it before taking the public status snapshot so one tool result cannot
            # contain two contradictory stages or row versions.
            open_arguments: dict[str, Any] = {
                "owner_id": self.owner_id,
                "model_id": self.model_id,
            }
            if view in {"summary", "review"} or (view == "manual" and module != "self_revision"):
                open_arguments["present_details"] = False
            if view == "review":
                open_arguments.update(review_page=page,
                                      expected_review_material_hash=expected_material_hash)
            if direct_grant_ref is not None:
                open_arguments.update(
                    {
                        "direct_grant_ref": direct_grant_ref,
                        "direct_client_principal": self.direct_client_principal,
                    }
                )
            open_context = self.onboarding.open_brain_context(**open_arguments)
        if view == "review":
            # No other module manuals, complete schema, or growing artifact
            # catalogue should surround a bounded page. The runtime alone binds
            # the page and its presentation receipt to this real wake/version.
            result = {
                "brain": "stiller-brain", "contract_version": PUBLIC_CONTRACT_VERSION,
                "view": "review", "module": "self_revision", "workspace_required": False,
                "write_context_available": open_context.get("write_context_available") is True,
                "review_material_presented": False,
            }
            for key in ("write_context_ref", "row_version", "reason_code", "binding_reason_code"):
                if key in open_context:
                    result[key] = open_context[key]
            projection = open_context.get("review_page")
            if isinstance(projection, dict):
                result["review_page"] = projection
                complete = projection.get("fully_presented") is True
                result["review_material_presented"] = complete
                next_page = projection.get("next_page")
                if not complete and next_page is None:
                    # An out-of-order last page is not full presentation. Start
                    # the bounded sequence again instead of leaving a dead end.
                    next_page = 0
                result["next_arguments"] = ({
                    "view": "review", "module": "self_revision", "page": next_page,
                    "expected_material_hash": projection["material_hash"],
                } if not complete and next_page is not None else None)
                status = open_context["current_status"]
                result["current_stage"] = status["state"]["stage"]
                result["expected_active_revision"] = status["state"].get("base_revision_id")
                result["instruction"] = (
                    "本轮完整材料已展示；是否接受仍由你独立决定。接受复核不会同轮激活，"
                    "激活仍须后续真实唤醒再完整核对。引用与版本只在本轮有效。"
                    if complete else
                    "这只是候选材料的一页，不是摘要，也不是已完成复核。按next_arguments"
                    "继续MCP读取；所有页须在同一真实唤醒且材料哈希一致，无需Shell或工作区。"
                )
            return _strip_private_binding_fields(result)
        # A successful open carries the authoritative status captured in the
        # same runtime transaction as its continuation/ref/row_version.  Falling
        # back is only for unavailable/legacy opens, where there is no write
        # context to accidentally combine with a newer state.
        snapshot_status = open_context.get("current_status")
        current_status = (
            snapshot_status
            if isinstance(snapshot_status, dict)
            else self.module_one_status()
        )
        current_state = current_status["state"]
        if view != "full":
            result = self._compact_open_result(open_context, current_status)
            if view == "manual":
                result = self._selected_open_manual(result, open_context, current_status, module)
            return _strip_private_binding_fields(result)
        result = {
            "brain": "stiller-brain",
            "contract_version": PUBLIC_CONTRACT_VERSION,
            "model_id": self.model_id,
            "module_registry": [
                {
                    "module": "self_revision_module_one",
                    "status": "implemented",
                    "purpose": "AI 自己维护的长期身份锚、冷静词、候选审核与跨唤醒激活。",
                },
                *(
                    [
                        {
                            "module": "injection_control",
                            "status": (
                                "available"
                                if current_status["module_one_unlocked"]
                                else "locked"
                            ),
                            "purpose": "AI 自己控制各模块自动注入的开关、暂停与紧急制动；不删除内容。",
                        }
                    ]
                    if self.injection_control is not None
                    else []
                ),
                *(
                    [
                        {
                            "module": "self_governance_profile",
                            "status": (
                                "available"
                                if current_status["module_one_unlocked"]
                                else "locked"
                            ),
                            "purpose": "可选、分范围、由 AI 自己写作和激活的治理正文。",
                        }
                    ]
                    if self.governance is not None
                    else []
                ),
                *(
                    [
                        {
                            "module": "emotional_memory_module_two",
                            "status": (
                                self.emotional.status()["status"]
                                if current_status["module_one_unlocked"]
                                else "locked"
                            ),
                            "purpose": "AI 自己维护的情感、人际经历、关联召回与短期连续层。",
                        }
                    ]
                    if self.emotional is not None
                    else []
                ),
                *(
                    [
                        {
                            "module": "learning_memory_module_three",
                            "status": (
                                self.learning.status()["status"]
                                if current_status["module_one_unlocked"]
                                else "locked"
                            ),
                            "purpose": "AI 自己维护的可复用理解、证据、对照、合并与隔离创意。",
                        }
                    ]
                    if self.learning is not None
                    else []
                ),
                *(
                    [
                        {
                            "module": "tool_guidance_module_four",
                            "status": (
                                self.tool_guidance.status()["status"]
                                if current_status["module_one_unlocked"]
                                else "locked"
                            ),
                            "purpose": "AI 自己维护的具体工具使用认知、场景联想、版本与失败经验。",
                        }
                    ]
                    if self.tool_guidance is not None
                    else []
                ),
                *(
                    [
                        {
                            "module": "planning_memory_module_five",
                            "status": (
                                self.planning.status()["status"]
                                if current_status["module_one_unlocked"]
                                else "locked"
                            ),
                            "purpose": "AI 自己采纳的计划图、承诺、证据进展与可回滚事件账本。",
                        }
                    ]
                    if self.planning is not None
                    else []
                ),
                *(
                    [
                        {
                            "module": "hallucination_reality_review_vault",
                            "status": (
                                "available"
                                if current_status["module_one_unlocked"]
                                else "locked"
                            ),
                            "purpose": "由 AI 自主判断使用、默认 hard_off 且与普通召回物理隔离的现实复核黑匣子。",
                        }
                    ]
                    if self.hallucination_vault is not None
                    else []
                ),
            ],
            "routing_guide": [
                {
                    "content_kind": "自动注入开关、临时暂停、长期关闭或紧急制动",
                    "module": "injection_control",
                },
                {
                    "content_kind": "跨模块或某一模块的自我治理内容与自选触发偏好",
                    "module": "self_governance_profile",
                },
                {
                    "content_kind": "稳定身份、表达方式、长期原则或自我修订边界",
                    "module": "self_revision_module_one",
                },
                {
                    "content_kind": "带时间线与感受的人际经历",
                    "module": "emotional_memory_module_two",
                },
                {
                    "content_kind": "跨任务可复用的理解、方法、证据或对照知识",
                    "module": "learning_memory_module_three",
                },
                {
                    "content_kind": "具体可调用操作的用途、场景、风险、版本与失败经验",
                    "module": "tool_guidance_module_four",
                },
                {
                    "content_kind": "由我自己采纳的方向、目标、里程碑、任务或关系承诺",
                    "module": "planning_memory_module_five",
                },
                {
                    "content_kind": "由我自己判断不应进入普通回想、需要隔离复核的内容",
                    "module": "hallucination_reality_review_vault",
                },
                {
                    "content_kind": "同时跨多个模块",
                    "module": "按内容所有权拆分并用来源引用关联；不确定时可以先查询或什么都不保存",
                },
            ],
            "epistemic_boundary": (
                "ST 保存的是当前 AI 可复核、可修订的长期自我描述与记忆记录；"
                "它提供跨唤醒连续性线索，但不把这些文本宣称为不间断主观意识或不可质疑的事实。"
            ),
            "public_workflow": [
                "普通情感、学习、规划新增：直接 remember_memory(module, content)；正常网关自动绑定，无需先 open、手填版本或另轮审核。短说明用 stbrain_help。",
                "普通小改用 revise_memory(target_ref, changes)，只改允许的摘要或检索元数据；计划进度用 advance_plan，保留已读目标版本、事件序号和真实证据，不手填模块行版本。",
                "按需要用 recall_* 主动查询记忆；学习全量目录用 recall_learning_memory(view='inventory')。活动自我与审计历史用 query_self_model。",
                "高级操作需要说明或复核材料时，用 stbrain_open(view='manual', module=对应模块)；默认 summary 仅返回上下文摘要与版本，不是已读候选的证明。",
                "专用高级写工具使用本轮实际 write_context_ref、目标版本及对应动作契约；模块一候选审核与激活仍须跨真实唤醒。",
            ],
            "submit_intents": {
                "acknowledge": "确认已读当前简介（factory/module_intro）。",
                "save_calm_prompt": "保存 AI 自己写的冷静词。",
                "submit": "保存新候选；绝不直接激活。",
                "accept_review": "记录完整审核结论；绝不在同一轮激活。",
                "revise": "追加新候选并保留旧候选审计链。",
                "respond_to_objection": "回应当前人类异议。",
                "withdraw": "撤回当前候选。",
                "begin_edit": "从 live 状态主动开始受控修订。",
                "confirm_edit": "提交本轮 challenge_id 与 ai_confirmation=true，确认修订意愿。",
                "cancel_edit": "取消修订。",
                "recover": "重新检查 draft-only 候选。",
            },
            "safety_rules": [
                "日常记忆直接一次 remember_memory；核心自我修改及专用高级写入仍按本轮 write_context_ref 与 row_version 复核。宿主凭据不进入模型上下文。",
                "候选保存、审核接受、正式激活是不同事件；审核与激活之间必须再跨一次真实外部唤醒。",
                "人类可查看、异议和紧急回滚，但不能代写、代审或代激活 AI 的自我模型。",
                "正常生成只注入活动版；候选、旧版、审计和隔离内容不会混入 live 上下文。",
                "候选公开载荷只接受 AI 自写的 content 与 reason；diff、evidence_refs 和活动版绑定由服务端派生且不作为客户端参数。",
            ],
            "current_status": current_status,
            "current_action_contract": current_action_contract(
                str(current_state["stage"]),
                allowed_actions=current_status.get("allowed_actions"),
                base_revision_id=current_state.get("base_revision_id"),
            ),
            "module_one_content_schema": self.content_schema(),
        }
        if self.emotional is not None and current_status["module_one_unlocked"]:
            result["emotional_memory"] = self.emotional.manual()
        if self.learning is not None and current_status["module_one_unlocked"]:
            result["learning_memory"] = self.learning.manual(
                write_context_ref=open_context.get("write_context_ref")
            )
        if self.tool_guidance is not None and current_status["module_one_unlocked"]:
            result["tool_guidance"] = self.tool_guidance.manual(
                write_context_ref=open_context.get("write_context_ref")
            )
        if self.governance is not None and current_status["module_one_unlocked"]:
            governance_manual = self.governance.manual()
            governance_manual["current_action_contract"] = (
                self.governance.current_action_contract(
                    write_context_ref=open_context.get("write_context_ref")
                )
            )
            result["self_governance_profile"] = governance_manual
        if (
            self.injection_control is not None
            and current_status["module_one_unlocked"]
        ):
            result["injection_control"] = self.injection_control.manual(
                write_context_ref=open_context.get("write_context_ref")
            )
        if self.planning is not None and current_status["module_one_unlocked"]:
            result["planning_memory"] = self.planning.manual(
                write_context_ref=open_context.get("write_context_ref")
            )
        if (
            self.hallucination_vault is not None
            and current_status["module_one_unlocked"]
        ):
            result["hallucination_vault"] = self.hallucination_vault.manual(
                write_context_ref=open_context.get("write_context_ref")
            )
        if self.authoring_rewrite is not None and current_status["module_one_unlocked"]:
            result["shared_person_authoring"] = self.authoring_rewrite.manual()
        result.update(open_context)
        return _strip_private_binding_fields(result)

    def _compact_open_result(
        self, open_context: dict[str, Any], current_status: dict[str, Any]
    ) -> dict[str, Any]:
        """Content-free, bounded projection; do not call any module's manual here."""
        state = current_status["state"]
        unlocked = current_status["module_one_unlocked"]
        result: dict[str, Any] = {
            "brain": "stiller-brain",
            "contract_version": PUBLIC_CONTRACT_VERSION,
            "open_response_version": "brain-open/2",
            "view": "summary",
            "write_context_available": open_context.get("write_context_available") is True,
        }
        # Values first, documentation last. Never put JSONPath placeholders here.
        for key in ("write_context_ref", "row_version", "reason_code", "binding_reason_code"):
            if key in open_context:
                result[key] = open_context[key]
        result.setdefault("row_version", state.get("row_version"))
        result["current_status"] = {
            "state": {"stage": state["stage"], "row_version": state.get("row_version")},
            "module_one_unlocked": unlocked,
        }
        result["continuation"] = None
        result["review_material_presented"] = False
        available_modules = ["self_revision"]
        versioned_modules = (
            ("emotional_memory", self.emotional, "row_version"),
            ("learning_memory", self.learning, "learning_row_version"),
            ("tool_guidance", self.tool_guidance, "tool_row_version"),
            ("planning_memory", self.planning, "planning_row_version"),
            ("hallucination_vault", self.hallucination_vault, "vault_row_version"),
            ("shared_person_authoring", self.authoring_rewrite, "row_version"),
        )
        for name, component, version_key in versioned_modules:
            if component is None:
                continue
            available_modules.append(name)
            status = component.status()
            entry: dict[str, Any] = {
                "status": "available" if unlocked else "locked",
                version_key: status["row_version"],
            }
            counts = status.get("counts", {})
            pending = counts.get("pending_changes")
            if type(pending) is int:
                entry["pending_count"] = pending
            result[name] = entry
        for name, component in (
            ("self_governance_profile", self.governance),
            ("injection_control", self.injection_control),
        ):
            if component is None:
                continue
            available_modules.append(name)
            status = component.status()
            # These status objects can contain pending candidate arrays. Expose
            # only integer CAS versions, never full status, counts derived from
            # bodies, candidate text, or action contracts in the summary.
            result[name] = {
                "status": "available" if unlocked else "locked",
                "scope_versions": {
                    scope: item["row_version"]
                    for scope, item in status["scopes"].items()
                },
            }
        result["manual_access"] = {
            "tool": "stbrain_open",
            "view": "manual",
            "modules": available_modules,
            "instruction": (
                "需要说明或复核材料时传 view=manual、module=对应模块名；"
                "仅展示该模块，复用当前唤醒的引用，不需要沙箱。"
            ),
        }
        result["write_usage"] = (
            "日常学习、情感、规划记忆优先直接调用 remember_memory(module, content)，无需先 open、手填版本或另轮审核。"
            "以下仅适用于核心自我修改及专用高级流程：使用根 write_context_ref 的实际值及目标模块版本；不要传取值路径。"
            "同一真实用户回合内复用引用，并使用每次成功写入返回的新版本。"
            "新回合不可复用旧引用。摘要不代表已完整阅读任何候选；复核前请求对应模块手册。"
        )
        return result

    def _selected_open_manual(
        self,
        result: dict[str, Any],
        open_context: dict[str, Any],
        current_status: dict[str, Any],
        module: str,
    ) -> dict[str, Any]:
        """Present one requested module, preserving full review material and proofs."""
        result["view"] = "manual"
        result["manual_module"] = module
        # No blanket claim that an entire brain or every candidate was presented.
        result.pop("review_material_presented", None)
        ref = open_context.get("write_context_ref")
        if module == "self_revision":
            state = current_status["state"]
            result["continuation"] = open_context.get("continuation")
            result["current_status"] = current_status
            result["current_action_contract"] = current_action_contract(
                str(state["stage"]),
                allowed_actions=current_status.get("allowed_actions"),
                base_revision_id=state.get("base_revision_id"),
            )
            result["module_one_content_schema"] = self.content_schema()
            return result
        components = {
            "emotional_memory": self.emotional,
            "learning_memory": self.learning,
            "tool_guidance": self.tool_guidance,
            "planning_memory": self.planning,
            "self_governance_profile": self.governance,
            "injection_control": self.injection_control,
            "hallucination_vault": self.hallucination_vault,
            "shared_person_authoring": self.authoring_rewrite,
        }
        component = components[module]
        if component is None or not current_status["module_one_unlocked"]:
            result["manual_available"] = False
            result["manual_reason_code"] = (
                "module_not_configured" if component is None else "module_one_required"
            )
            return result
        if module in {"emotional_memory", "shared_person_authoring"}:
            manual = component.manual()
        elif module == "self_governance_profile":
            manual = component.manual()
            manual["current_action_contract"] = component.current_action_contract(
                write_context_ref=ref
            )
        else:
            manual = component.manual(write_context_ref=ref)
        # Keep the same explicit version path in summary and manual views. Older
        # emotional/tool manuals nested their version under status, which made
        # a model switch paths merely because it requested instructions.
        entry = {**result.get(module, {}), **manual}
        version_key = {
            "emotional_memory": "row_version",
            "learning_memory": "learning_row_version",
            "tool_guidance": "tool_row_version",
            "planning_memory": "planning_row_version",
            "hallucination_vault": "vault_row_version",
            "shared_person_authoring": "row_version",
        }.get(module)
        manual_status = manual.get("status")
        if version_key and version_key not in manual and isinstance(manual_status, dict):
            if type(manual_status.get("row_version")) is int:
                entry[version_key] = manual_status["row_version"]
        result[module] = entry
        result["manual_available"] = True
        return result

    def open_brain_direct(self, *, grant_ref: str) -> dict[str, Any]:
        """Atomically consume one human-issued grant and open a direct context."""

        if not isinstance(grant_ref, str) or not grant_ref.strip():
            raise SelfRevisionError("grant_ref must be a non-empty string")
        cleaned = grant_ref.strip()
        if len(cleaned) > 256:
            raise SelfRevisionError("grant_ref is too long")
        return self.open_brain(direct_grant_ref=cleaned)

    def submit_self_model_candidate(
        self,
        *,
        intent: str,
        write_context_ref: str,
        expected_row_version: int | None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Route one semantic onboarding/write intent without exposing raw actions."""
        intent = intent.strip()
        body = dict(payload or {})
        return self._bound_public_write(
            intent=intent,
            write_context_ref=write_context_ref,
            expected_row_version=expected_row_version,
            payload=body,
        )

    def activate_self_model_candidate(
        self,
        *,
        candidate_id: str,
        write_context_ref: str,
        expected_row_version: int | None,
        expected_active_revision: str | None,
        ai_confirmation: bool,
    ) -> dict[str, Any]:
        """Activate only after accepted review and a later real wake."""
        # Bind the opened wake first.  The authoritative runtime transaction
        # checks stage, current candidate, base revision, row CAS, and explicit
        # confirmation together; no pre-binding status/candidate oracle lives
        # in this facade.
        return self._bound_public_write(
            action="activate_candidate",
            write_context_ref=write_context_ref,
            expected_row_version=expected_row_version,
            payload={
                "candidate_id": candidate_id,
                "expected_active_revision": expected_active_revision,
                "ai_confirmation": ai_confirmation,
            },
        )

    def query_self_model(
        self,
        *,
        view: QueryView,
        query: str = "",
        scope: SearchScope = "all",
        limit: int = 20,
        include_content: bool = False,
        facet_names: list[str] | None = None,
        include_anchor_references: bool = False,
    ) -> dict[str, Any]:
        """Read one owner-scoped self-model view through the single archive facade."""
        if view == "status":
            result = self.module_one_status()
        elif view in {"active", "edit_basis"} and self.onboarding is not None:
            result = self.onboarding.read_active_self_model(
                owner_id=self.owner_id, model_id=self.model_id,
            )
            if result.get("active_available") is True and view == "active":
                active = result.pop("active")
                content = active["content"]
                names = facet_names or []
                result.update({
                    "boot_anchor": {
                        **content["boot_anchor"], "model_id": self.model_id,
                        "active_revision_id": active["revision_id"],
                        "current_effective_version": active["revision_number"],
                    },
                    "active_identity_capsule": {
                        **content["active_identity_capsule"],
                        "active_revision_id": active["revision_id"],
                        "current_effective_version": active["revision_number"],
                    },
                    "facets": {name: content["facets"][name] for name in names if name in content["facets"]},
                    "missing_facets": [name for name in names if name not in content["facets"]],
                    "anchor_references": content["anchor_references"] if include_anchor_references else [],
                    "active_revision_id": active["revision_id"],
                    "content_hash": active["content_hash"],
                    "scope": "selected_active_layers",
                    "edit_basis_access": {"tool": "query_self_model", "view": "edit_basis"},
                })
            elif result.get("active_available") is True:
                result["scope"] = "complete_active_edit_basis"
                result["instruction"] = (
                    "active.content是完整且已核对哈希的活动五键正文，含原有facets和引用；"
                    "编辑时保留未改部分。读取不是签发写权限、候选复核或自动注入。"
                )
        elif view == "active":
            result = self.get_active(checkpoint_id="mcp-query-active", facet_names=facet_names,
                                     include_anchor_references=include_anchor_references)
        elif view == "edit_basis":
            result = {"decision": "active_self_model_unavailable", "active_available": False,
                      "reason_codes": ["module_one_onboarding_gate_not_configured"],
                      "state_changed": False, "pointer_changed": False}
        elif view == "search":
            result = self.search(
                query=query,
                scope=scope,
                limit=limit,
                include_content=include_content,
            )
        else:
            raise ValueError("view must be status, active, or search; edit_basis is also available")
        return {
            "module": "self_revision_module_one",
            "model_id": self.model_id,
            "view": view,
            "result": result,
        }

    def continue_module_one(
        self,
        *,
        action: str,
        wake_id: str | None,
        wake_capability: str | None,
        expected_row_version: int | None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one AI action through the authoritative wake-bound transition gate."""
        if self.onboarding is None:
            raise SelfRevisionError("module-one onboarding gate is not configured")
        return self.onboarding.advance(
            owner_id=self.owner_id,
            model_id=self.model_id,
            action=action,
            wake_id=wake_id,
            wake_capability=wake_capability,
            expected_row_version=expected_row_version,
            payload=payload or {},
            actor="ai",
        )

    def _candidate_for_this_model(self, candidate_id: str) -> dict[str, Any]:
        for candidate in self.store.list_candidates(self.model_id):
            if candidate["candidate_id"] == candidate_id:
                return candidate
        raise SelfRevisionError("candidate not found for this self-model")

    def content_schema(self) -> dict[str, Any]:
        """Return an opinion-free contract and a deliberately invalid blank shape."""
        schema = _load_content_schema()
        # Developer fixtures may use examples, but onboarding must never hand a
        # blank model value-laden prose that can be copied as its own identity.
        schema.pop("examples", None)
        return {
            "contract": "self_model_content",
            "schema_version": "0.1.0",
            "json_schema": schema,
            "blank_structure": json.loads(json.dumps(_BLANK_CONTENT_STRUCTURE)),
            "blank_structure_is_submittable": False,
            "field_guide": {
                "boot_anchor.text": {
                    "purpose": "由 AI 自己决定的简短启动提醒。",
                    "when_visible": "模块一 live 后的正常生成。",
                },
                "active_identity_capsule": {
                    "purpose": "由 AI 自己决定的稳定身份、性格、表达、原则、身份锚与自我修订边界。",
                    "when_visible": "模块一 live 后的正常生成。",
                },
                "active_identity_capsule.self_revision_safety_prompt": {
                    "purpose": "由 AI 自己给未来自我修订设置的边界；开发者不提供价值内容。",
                    "when_visible": "作为模块一活动稳定层随 live 生成出现；修改时仍显示当前活动版，候选不会同轮替代它。",
                },
                "facets": {
                    "purpose": "由 AI 自选的情境侧面；可以为空对象。",
                    "when_visible": "模块一 live 后由宿主按情境选择。",
                },
                "anchor_references": {
                    "purpose": "可复核来源引用；可以为空数组，不自动注入。",
                    "when_visible": "仅在主动查询或复核时。",
                },
            },
            "important_rules": [
                "The five top-level keys are exact; do not omit, rename, or add keys.",
                "boot_anchor is an object with one text field, not a bare string.",
                "active_identity_capsule contains four non-empty strings and two non-empty string arrays.",
                "anchor_references is an array; every item has exactly anchor_id, memory_ref, and meaning.",
                "facets may be an empty object; anchor_references may be an empty array.",
                "blank_structure contains null or empty placeholders and is intentionally invalid until the AI authors it.",
                "No developer-authored identity, value, relationship, or safety-boundary prose is supplied as candidate text.",
                "The AI may stop onboarding or leave the optional brain unused instead of adopting content it does not choose.",
                "The AI authors and reviews its own wording. No sentence prefix or grammatical person is required; the host preserves the text instead of rewriting it or supplying identity prose.",
            ],
            "runtime_limits": {
                "boot_anchor_text_characters": 500,
                "active_identity_capsule_canonical_json_characters": 4000,
                "facet_value_characters": 2500,
                "total_content_canonical_json_characters": 16000,
                "max_facets": 16,
                "max_anchor_references": 32,
            },
        }

    def prepare(self) -> dict[str, Any]:
        """Return the safety prompt and current lifecycle state before a write."""
        if self.onboarding is not None:
            status = self._onboarding_status()
            return {
                **status,
                "model_id": self.model_id,
                "compatibility_notice": (
                    "checkpoint_id is retained only for old clients and has no authority; "
                    "public clients must open the brain and use its per-wake write_context_ref "
                    "with the current row_version; wake capabilities remain server-side."
                ),
            }
        active = self.store.active_revision(self.model_id)
        candidates = self.store.list_candidates(self.model_id)
        revisions = self.store.list_revisions(self.model_id)
        events = self.store.list_events(self.model_id)
        pending = [
            {
                "candidate_id": item["candidate_id"],
                "state": item["state"],
                "state_checkpoint_id": item["state_checkpoint_id"],
                "base_revision_id": item["base_revision_id"],
                "reason": item["reason"],
                "created_at": item["created_at"],
            }
            for item in candidates
            if item["state"] in {"draft_only", "pending"}
        ]
        return {
            "module": "self_revision_module_one",
            "schema_version": "0.1.0",
            "model_id": self.model_id,
            "safety_prompt": self.store.current_safety_prompt(self.model_id),
            "active_revision": (
                _clean_revision(active, include_content=False) if active else None
            ),
            "pending_candidates": pending,
            "counts": {
                "candidates": len(candidates),
                "revisions": len(revisions),
                "events": len(events),
            },
            "workflow": [
                "Read and follow safety_prompt in the same checkpoint.",
                "Store a candidate; storage never changes the active pointer.",
                "Use a genuinely later checkpoint for activation.",
                "Retrieve defaults to the active revision only.",
            ],
        }

    def store_candidate(
        self,
        *,
        content: dict[str, Any],
        diff: list[dict[str, Any]],
        reason: str,
        evidence_refs: list[str],
        checkpoint_id: str,
        expected_active_revision: str | None,
        idempotency_key: str,
        presented_safety_prompt: str,
        origin: str = "ai_self",
        automatic_state_signal: str = "grounded",
        human_state_signal: str = "grounded",
        ai_authored_reason: bool = True,
        wake_id: str | None = None,
        wake_capability: str | None = None,
        expected_row_version: int | None = None,
    ) -> dict[str, Any]:
        """Store a candidate without making it active."""
        if self.onboarding is not None:
            stage = self._onboarding_status()["state"]["stage"]
            if stage not in {"body_draft", "edit_body_draft"}:
                return self._progression_required()
            result = self.continue_module_one(
                action="submit_candidate",
                wake_id=wake_id,
                wake_capability=wake_capability,
                expected_row_version=expected_row_version,
                payload={
                    "content": content,
                    "diff": diff,
                    "reason": reason,
                    "evidence_refs": evidence_refs,
                    "expected_active_revision": _optional_revision(
                        expected_active_revision
                    ),
                    "origin": origin,
                    "automatic_state_signal": automatic_state_signal,
                    "human_state_signal": human_state_signal,
                    "ai_authored_reason": ai_authored_reason,
                },
            )
            help_payload = _content_validation_help(content, result.get("reason_codes", []))
            if help_payload is not None:
                result = dict(result)
                result["validation_help"] = help_payload
            return result
        result = self.store.propose_candidate(
            model_id=self.model_id,
            owner_id=self.owner_id,
            content=content,
            diff=diff,
            reason=reason,
            evidence_refs=evidence_refs,
            checkpoint_id=checkpoint_id,
            expected_active_revision=_optional_revision(expected_active_revision),
            idempotency_key=idempotency_key,
            presented_safety_prompt=presented_safety_prompt,
            origin=origin,
            automatic_state_signal=automatic_state_signal,
            human_state_signal=human_state_signal,
            ai_authored_reason=ai_authored_reason,
        )
        help_payload = _content_validation_help(content, result.get("reason_codes", []))
        if help_payload is not None:
            result = dict(result)
            result["validation_help"] = help_payload
        return result

    def recheck_candidate(
        self,
        *,
        candidate_id: str,
        checkpoint_id: str,
        idempotency_key: str,
        presented_safety_prompt: str,
        automatic_state_signal: str = "grounded",
        human_state_signal: str = "grounded",
        wake_id: str | None = None,
        wake_capability: str | None = None,
        expected_row_version: int | None = None,
    ) -> dict[str, Any]:
        """Move a recovered draft-only candidate to pending after a new review."""
        if self.onboarding is not None:
            stage = self._onboarding_status()["state"]["stage"]
            if stage != "draft_only_recovery":
                return self._progression_required()
            if not self._current_candidate_matches(candidate_id):
                return self._progression_required("candidate_not_current")
            return self.continue_module_one(
                action="recover_candidate",
                wake_id=wake_id,
                wake_capability=wake_capability,
                expected_row_version=expected_row_version,
                payload={
                    "automatic_state_signal": automatic_state_signal,
                    "human_state_signal": human_state_signal,
                },
            )
        self._candidate_for_this_model(candidate_id)
        return self.store.recheck_candidate(
            candidate_id=candidate_id,
            checkpoint_id=checkpoint_id,
            idempotency_key=idempotency_key,
            presented_safety_prompt=presented_safety_prompt,
            automatic_state_signal=automatic_state_signal,
            human_state_signal=human_state_signal,
        )

    def activate_candidate(
        self,
        *,
        candidate_id: str,
        checkpoint_id: str,
        expected_active_revision: str | None,
        idempotency_key: str,
        presented_safety_prompt: str,
        ai_confirmation: str | bool,
        automatic_state_signal: str = "grounded",
        human_state_signal: str = "grounded",
        wake_id: str | None = None,
        wake_capability: str | None = None,
        expected_row_version: int | None = None,
    ) -> dict[str, Any]:
        """Activate a pending candidate at a later independent checkpoint."""
        if self.onboarding is not None:
            stage = self._onboarding_status()["state"]["stage"]
            if stage not in {"candidate_wait", "candidate_review"}:
                return self._progression_required()
            if not self._current_candidate_matches(candidate_id):
                return self._progression_required("candidate_not_current")
            return self.continue_module_one(
                action="activate_candidate",
                wake_id=wake_id,
                wake_capability=wake_capability,
                expected_row_version=expected_row_version,
                payload={
                    "expected_active_revision": _optional_revision(
                        expected_active_revision
                    ),
                    "ai_confirmation": ai_confirmation is True,
                    "automatic_state_signal": automatic_state_signal,
                    "human_state_signal": human_state_signal,
                },
            )
        self._candidate_for_this_model(candidate_id)
        return self.store.activate_candidate(
            candidate_id=candidate_id,
            checkpoint_id=checkpoint_id,
            expected_active_revision=_optional_revision(expected_active_revision),
            idempotency_key=idempotency_key,
            presented_safety_prompt=presented_safety_prompt,
            ai_confirmation=ai_confirmation,
            automatic_state_signal=automatic_state_signal,
            human_state_signal=human_state_signal,
        )

    def get_active(
        self,
        *,
        checkpoint_id: str,
        facet_names: list[str] | None = None,
        include_anchor_references: bool = False,
    ) -> dict[str, Any]:
        """Retrieve only the current active revision for prompt injection."""
        if self.onboarding is not None:
            status = self._onboarding_status()
            state = status["state"]
            if (
                state["stage"] != "live"
                or state["module_one_status"] != "complete"
                or state["injection_policy"] != "normal"
            ):
                return self._progression_required("active_self_model_not_available")
        return self.store.build_injection(
            model_id=self.model_id,
            checkpoint_id=checkpoint_id,
            facet_names=facet_names or [],
            include_anchor_references=include_anchor_references,
        )

    def search(
        self,
        *,
        query: str = "",
        scope: SearchScope = "all",
        limit: int = 20,
        include_content: bool = False,
    ) -> dict[str, Any]:
        """Search active state, candidates, revisions, and audit events."""
        if scope not in {"active", "candidates", "revisions", "events", "all"}:
            raise ValueError("invalid search scope")
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")

        results: list[dict[str, Any]] = []
        if scope in {"active", "all"}:
            active = self.store.active_revision(self.model_id)
            cleaned_active = (
                _clean_revision(active, include_content=include_content) if active else None
            )
            if cleaned_active and _matches(cleaned_active, query):
                results.append(
                    {
                        "kind": "active_revision",
                        "item": cleaned_active,
                    }
                )

        if scope in {"candidates", "all"}:
            for item in reversed(self.store.list_candidates(self.model_id)):
                cleaned = _clean_candidate(item, include_content=include_content)
                if _matches(cleaned, query):
                    results.append(
                        {
                            "kind": "candidate",
                            "item": cleaned,
                        }
                    )

        if scope in {"revisions", "all"}:
            active = self.store.active_revision(self.model_id)
            active_id = active["revision_id"] if active else None
            for item in reversed(self.store.list_revisions(self.model_id)):
                cleaned = _clean_revision(item, include_content=include_content)
                cleaned["is_active"] = item["revision_id"] == active_id
                if _matches(cleaned, query):
                    results.append({"kind": "revision", "item": cleaned})

        if scope in {"events", "all"}:
            for item in reversed(self.store.list_events(self.model_id)):
                cleaned = _clean_event(item)
                if _matches(cleaned, query):
                    results.append({"kind": "event", "item": cleaned})

        return {
            "module": "self_revision_module_one",
            "model_id": self.model_id,
            "scope": scope,
            "query": query,
            "count": min(len(results), limit),
            "truncated": len(results) > limit,
            "results": results[:limit],
        }
