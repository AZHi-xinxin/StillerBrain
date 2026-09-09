"""Owner-scoped facade for AI-owned automatic-injection controls."""

from __future__ import annotations

from typing import Any, Literal

from runtime import ModuleOneOnboardingStore
from runtime.injection_control import InjectionControlError, InjectionControlStore


InjectionControlAction = Literal[
    "emergency_off",
    "propose_mode",
    "propose_rollback",
    "withdraw",
    "activate",
]
InjectionControlView = Literal["status", "manual", "history"]


class InjectionControlAccessService:
    """Bind injection changes to the current AI-opened real wake."""

    def __init__(
        self,
        store: InjectionControlStore,
        *,
        onboarding: ModuleOneOnboardingStore,
        owner_id: str,
        model_id: str,
    ) -> None:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must not be empty")
        self.store = store
        self.onboarding = onboarding
        self.owner_id = owner_id.strip()
        self.model_id = model_id.strip()
        self.store.ensure_state(owner_id=self.owner_id, model_id=self.model_id)

    @staticmethod
    def _deny(reason: str, *, help: dict[str, Any] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "module": "injection_control",
            "decision": "rejected",
            "reason_codes": [reason],
            "state_changed": False,
            "active_changed": False,
            "storage_changed": False,
            "explicit_query_changed": False,
        }
        if help is not None:
            result["validation_help"] = help
        return result

    def _binding(self, write_context_ref: Any) -> dict[str, Any] | None:
        if not isinstance(write_context_ref, str) or not write_context_ref.strip():
            return None
        binding = self.onboarding.current_open_write_context(
            owner_id=self.owner_id,
            model_id=self.model_id,
            write_context_ref=write_context_ref.strip(),
            required_scope="injection_control",
        )
        return binding if binding.get("write_context_available") is True else None

    def _ready(self) -> bool:
        permission = self.onboarding.authorize_other_module_write(
            owner_id=self.owner_id,
            model_id=self.model_id,
            module_name="injection_control",
        )
        return permission.get("decision") == "allowed"

    def manage(
        self,
        *,
        action: InjectionControlAction,
        scope: str,
        write_context_ref: str,
        expected_control_version: int,
        target_mode: str | None = None,
        reason: str | None = None,
        candidate_id: str | None = None,
        expected_candidate_hash: str | None = None,
        expected_active_revision: str | None = None,
        target_revision_id: str | None = None,
        ai_confirmation: bool = False,
    ) -> dict[str, Any]:
        if not self._ready():
            return self._deny("module_one_required")
        binding = self._binding(write_context_ref)
        if binding is None:
            return self._deny("brain_open_required")
        if self.onboarding.contains_protected_persistence_value(
            owner_id=self.owner_id,
            model_id=self.model_id,
            value={
                "action": action,
                "scope": scope,
                "expected_control_version": expected_control_version,
                "target_mode": target_mode,
                "reason": reason,
                "candidate_id": candidate_id,
                "expected_candidate_hash": expected_candidate_hash,
                "expected_active_revision": expected_active_revision,
                "target_revision_id": target_revision_id,
                "ai_confirmation": ai_confirmation,
            },
        ):
            return self._deny("credential_or_secret_detected")
        if ai_confirmation is not True:
            return self._deny("ai_confirmation_required")
        if expected_active_revision is not None and (
            not isinstance(expected_active_revision, str)
            or not expected_active_revision.strip()
            or expected_active_revision.strip().casefold() == "null"
        ):
            return self._deny(
                "expected_active_revision_must_be_string_or_null",
                help={
                    "expected": "non-empty revision id or JSON null",
                    "first_revision": "omit the optional field or use JSON null",
                },
            )
        try:
            if action == "emergency_off":
                if scope != "global" or any(
                    value is not None
                    for value in (
                        target_mode,
                        candidate_id,
                        expected_candidate_hash,
                        expected_active_revision,
                        target_revision_id,
                    )
                ):
                    return self._deny("invalid_emergency_off_payload")
                return self.store.emergency_off(
                    owner_id=self.owner_id,
                    model_id=self.model_id,
                    reason=reason,
                    wake_id=binding["wake_id"],
                    wake_seq=binding["wake_seq"],
                    expected_row_version=expected_control_version,
                    actor="ai",
                )
            if action in {"propose_mode", "propose_rollback"}:
                if any(
                    value is not None
                    for value in (candidate_id, expected_candidate_hash)
                ):
                    return self._deny("invalid_injection_candidate_payload")
                if action == "propose_mode" and target_revision_id is not None:
                    return self._deny("target_revision_only_for_rollback")
                if action == "propose_rollback" and target_revision_id is None:
                    return self._deny("target_revision_required")
                return self.store.propose_mode(
                    owner_id=self.owner_id,
                    model_id=self.model_id,
                    scope=scope,
                    target_mode=target_mode,
                    reason=reason,
                    wake_id=binding["wake_id"],
                    wake_seq=binding["wake_seq"],
                    expected_row_version=expected_control_version,
                    expected_active_revision=expected_active_revision,
                    target_revision_id=target_revision_id,
                    actor="ai",
                )
            if action == "withdraw":
                if any(
                    value is not None
                    for value in (
                        target_mode,
                        expected_candidate_hash,
                        expected_active_revision,
                        target_revision_id,
                    )
                ):
                    return self._deny("invalid_injection_withdraw_payload")
                return self.store.withdraw_candidate(
                    owner_id=self.owner_id,
                    model_id=self.model_id,
                    scope=scope,
                    candidate_id=candidate_id,
                    reason=reason,
                    expected_row_version=expected_control_version,
                    wake_id=binding["wake_id"],
                    wake_seq=binding["wake_seq"],
                    actor="ai",
                )
            if action == "activate":
                if any(
                    value is not None
                    for value in (target_mode, reason, target_revision_id)
                ):
                    return self._deny("invalid_injection_activation_payload")
                return self.store.activate_candidate(
                    owner_id=self.owner_id,
                    model_id=self.model_id,
                    scope=scope,
                    candidate_id=candidate_id,
                    expected_candidate_hash=expected_candidate_hash,
                    expected_active_revision=expected_active_revision,
                    expected_row_version=expected_control_version,
                    wake_id=binding["wake_id"],
                    wake_seq=binding["wake_seq"],
                    ai_confirmation=ai_confirmation,
                    actor="ai",
                )
            return self._deny("invalid_injection_control_action")
        except InjectionControlError as exc:
            return self._deny(str(exc))

    def status(self) -> dict[str, Any]:
        return self.store.status(owner_id=self.owner_id, model_id=self.model_id)

    def manual(self, *, write_context_ref: str | None = None) -> dict[str, Any]:
        result = self.store.manual(owner_id=self.owner_id, model_id=self.model_id)
        result["current_action_contract"] = self.current_action_contract(
            write_context_ref=write_context_ref
        )
        return result

    def query(self, *, view: InjectionControlView) -> dict[str, Any]:
        if view == "manual":
            result: Any = self.store.manual(
                owner_id=self.owner_id, model_id=self.model_id
            )
        elif view == "history":
            result = self.store.status(
                owner_id=self.owner_id,
                model_id=self.model_id,
                include_history=True,
            )
        elif view == "status":
            result = self.status()
        else:
            return self._deny("invalid_injection_control_view")
        return {"module": "injection_control", "view": view, "result": result}

    def current_action_contract(
        self, *, write_context_ref: str | None
    ) -> dict[str, Any]:
        status = self.status()
        binding = self._binding(write_context_ref)
        allowed: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        if binding is not None:
            for scope, item in status["scopes"].items():
                for candidate in item["pending_candidates"]:
                    if int(binding["wake_seq"]) <= int(candidate["created_wake_seq"]):
                        blocked.append(
                            {
                                "scope": scope,
                                "candidate_id": candidate["candidate_id"],
                                "reason": "later_real_wake_required",
                            }
                        )
                        continue
                    arguments: dict[str, Any] = {
                        "action": "activate",
                        "scope": scope,
                        "write_context_ref": binding["write_context_ref"],
                        "expected_control_version": item["row_version"],
                        "candidate_id": candidate["candidate_id"],
                        "expected_candidate_hash": candidate["candidate_hash"],
                        "ai_confirmation": True,
                    }
                    if candidate["base_revision_id"] is not None:
                        arguments["expected_active_revision"] = candidate[
                            "base_revision_id"
                        ]
                    allowed.append(
                        {
                            "tool": "manage_injection_control",
                            "arguments": arguments,
                            "copy_exactly": True,
                        }
                    )
        return {
            "contract_version": "injection-control-current-action/1",
            "pending_activations": allowed,
            "blocked_candidates": blocked,
            "creation_wake_activation_forbidden": True,
        }


__all__ = [
    "InjectionControlAccessService",
    "InjectionControlAction",
    "InjectionControlView",
]
