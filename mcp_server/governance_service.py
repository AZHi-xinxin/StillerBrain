"""Owner-scoped facade for the optional AI self-governance profile.

The facade binds every mutation to the current authenticated author operation
or an existing open wake. It exposes no host wake capability and provides no path
for a human or developer actor to author governance prose.
"""

from __future__ import annotations

from typing import Any, Literal
from runtime.execution_binding import ExecutionBindingError

from runtime import ModuleOneOnboardingStore
from runtime.self_governance import SelfGovernanceError, SelfGovernanceStore


GovernanceAction = Literal[
    "set",
    "clear",
    "rollback",
    "propose_set",
    "propose_clear",
    "propose_rollback",
    "withdraw",
    "activate",
]
GovernanceQueryView = Literal["status", "manual", "revisions"]


class SelfGovernanceAccessService:
    """Small future-public facade: one mutation entry and one query entry."""

    def __init__(
        self,
        store: SelfGovernanceStore,
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

    def _deny(
        self, reason_code: str, *, validation_help: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result = {
            "decision": "rejected",
            "reason_codes": [reason_code],
            "state_changed": False,
            "active_changed": False,
            "external_permission_changed": False,
        }
        if validation_help is not None:
            result["validation_help"] = validation_help
        if reason_code == "legacy_candidate_requires_open_wake":
            result["next_action"] = "普通更新使用 set/clear/rollback；显式旧候选使用真实已打开上下文及原候选坐标。"
        if reason_code in {"governance_version_conflict", "active_governance_revision_conflict"}:
            result["retryable"] = True
            result["message"] = (
                "这份内容已被另一操作更新，或本次显式提供的内部状态已过期；本次内容尚未保存。"
            )
            result["next_action"] = (
                "普通 set/clear/rollback：读取最新内容并确认后，重新提交内容即可；"
                "省略 expected_profile_version 和 expected_active_revision，无需猜测版本号。"
                "旧候选动作请重新读取 current_action_contract。"
            )
        return result

    def _binding(self, write_context_ref: Any) -> dict[str, Any] | None:
        if not isinstance(write_context_ref, str) or not write_context_ref.strip():
            return None
        binding = self.onboarding.current_open_write_context(
            owner_id=self.owner_id,
            model_id=self.model_id,
            write_context_ref=write_context_ref.strip(),
            required_scope="self_governance",
        )
        return binding if binding.get("write_context_available") is True else None

    def _module_ready(self) -> dict[str, Any] | None:
        result = self.onboarding.authorize_other_module_write(
            owner_id=self.owner_id,
            model_id=self.model_id,
            module_name="self_governance_profile",
        )
        return None if result.get("decision") == "allowed" else result

    def manage(
        self,
        *,
        action: GovernanceAction,
        scope: str,
        write_context_ref: str,
        expected_profile_version: int,
        text: str | None = None,
        trigger_mode: str | None = None,
        scene_tags: list[str] | None = None,
        reason: str | None = None,
        candidate_id: str | None = None,
        expected_candidate_hash: str | None = None,
        expected_active_revision: str | None = None,
        target_revision_id: str | None = None,
        ai_confirmation: bool = False,
    ) -> dict[str, Any]:
        """Run one flat, wake-bound AI mutation without judging its values."""

        try:
            blocked = self._module_ready()
            if blocked is not None:
                return self._deny(str(blocked.get("reason_codes", ["module_one_required"])[0]))
            binding = self._binding(write_context_ref)
        except ExecutionBindingError as exc:
            return self._deny(str(exc))
        if binding is None:
            return self._deny("brain_open_required")
        if action not in {"set", "clear", "rollback"} and binding.get("context_mode") == "ordinary_authenticated":
            return self._deny("legacy_candidate_requires_open_wake")
        try:
            protected = self.onboarding.contains_protected_persistence_value(
                owner_id=self.owner_id,
                model_id=self.model_id,
                value={
                    "action": action,
                    "scope": scope,
                    "expected_profile_version": expected_profile_version,
                    "text": text,
                    "trigger_mode": trigger_mode,
                    "scene_tags": scene_tags,
                    "reason": reason,
                    "candidate_id": candidate_id,
                    "expected_candidate_hash": expected_candidate_hash,
                    "expected_active_revision": expected_active_revision,
                    "target_revision_id": target_revision_id,
                    "ai_confirmation": ai_confirmation,
                },
            )
        except ExecutionBindingError as exc:
            return self._deny(str(exc))
        if protected:
            return self._deny("credential_or_secret_detected")
        if expected_active_revision is not None and (
            not isinstance(expected_active_revision, str)
            or not expected_active_revision.strip()
            or expected_active_revision.strip().casefold() == "null"
        ):
            return self._deny(
                "expected_active_revision_must_be_string_or_null",
                validation_help={
                    "field": "expected_active_revision",
                    "expected": "non-empty revision id or JSON null",
                    "received_type": type(expected_active_revision).__name__,
                    "first_activation": (
                        "omit this optional field or use JSON null; never use false"
                    ),
                },
            )
        try:
            if action in {"set", "clear", "rollback"}:
                if candidate_id is not None or expected_candidate_hash is not None:
                    return self._deny("invalid_governance_action_payload")
                if action != "set" and any(value is not None for value in (text, trigger_mode, scene_tags)):
                    return self._deny("invalid_governance_action_payload")
                content = ({"schema_version": "0.1.0", "text": text,
                            "trigger_mode": trigger_mode, "scene_tags": scene_tags}
                           if action == "set" else None)
                return self.store.commit_revision(
                    owner_id=self.owner_id, model_id=self.model_id, scope=scope,
                    operation=action, content=content, reason=reason,
                    wake_id=binding["wake_id"], wake_seq=binding["wake_seq"],
                    expected_row_version=expected_profile_version,
                    expected_active_revision=expected_active_revision,
                    target_revision_id=target_revision_id, actor="ai",
                )
            if action == "propose_set":
                if any(
                    value is not None
                    for value in (
                        candidate_id,
                        expected_candidate_hash,
                        target_revision_id,
                    )
                ) or ai_confirmation is not False:
                    return self._deny("invalid_governance_action_payload")
                content = {
                    "schema_version": "0.1.0",
                    "text": text,
                    "trigger_mode": trigger_mode,
                    "scene_tags": scene_tags,
                }
                return self.store.propose_candidate(
                    owner_id=self.owner_id,
                    model_id=self.model_id,
                    scope=scope,
                    operation="set",
                    content=content,
                    reason=reason,
                    wake_id=binding["wake_id"],
                    wake_seq=binding["wake_seq"],
                    expected_row_version=expected_profile_version,
                    expected_active_revision=expected_active_revision,
                    actor="ai",
                )
            if action == "propose_clear":
                if any(
                    value is not None
                    for value in (
                        text,
                        trigger_mode,
                        scene_tags,
                        candidate_id,
                        expected_candidate_hash,
                        target_revision_id,
                    )
                ) or ai_confirmation is not False:
                    return self._deny("invalid_governance_action_payload")
                return self.store.propose_candidate(
                    owner_id=self.owner_id,
                    model_id=self.model_id,
                    scope=scope,
                    operation="clear",
                    content=None,
                    reason=reason,
                    wake_id=binding["wake_id"],
                    wake_seq=binding["wake_seq"],
                    expected_row_version=expected_profile_version,
                    expected_active_revision=expected_active_revision,
                    actor="ai",
                )
            if action == "propose_rollback":
                if any(
                    value is not None
                    for value in (
                        text,
                        trigger_mode,
                        scene_tags,
                        candidate_id,
                        expected_candidate_hash,
                    )
                ) or ai_confirmation is not False:
                    return self._deny("invalid_governance_action_payload")
                return self.store.propose_candidate(
                    owner_id=self.owner_id,
                    model_id=self.model_id,
                    scope=scope,
                    operation="rollback",
                    content=None,
                    reason=reason,
                    wake_id=binding["wake_id"],
                    wake_seq=binding["wake_seq"],
                    expected_row_version=expected_profile_version,
                    expected_active_revision=expected_active_revision,
                    actor="ai",
                    target_revision_id=target_revision_id,
                )
            if action == "withdraw":
                if any(
                    value is not None
                    for value in (
                        text,
                        trigger_mode,
                        scene_tags,
                        expected_candidate_hash,
                        expected_active_revision,
                        target_revision_id,
                    )
                ) or ai_confirmation is not False:
                    return self._deny("invalid_governance_action_payload")
                return self.store.withdraw_candidate(
                    owner_id=self.owner_id,
                    model_id=self.model_id,
                    scope=scope,
                    candidate_id=candidate_id,
                    reason=reason,
                    wake_id=binding["wake_id"],
                    wake_seq=binding["wake_seq"],
                    expected_row_version=expected_profile_version,
                    actor="ai",
                )
            if action == "activate":
                if any(
                    value is not None
                    for value in (
                        text,
                        trigger_mode,
                        scene_tags,
                        target_revision_id,
                    )
                ):
                    return self._deny(
                        "invalid_governance_action_payload",
                        validation_help={
                            "action": "activate",
                            "forbidden_non_null_fields": [
                                name
                                for name, value in (
                                    ("text", text),
                                    ("trigger_mode", trigger_mode),
                                    ("scene_tags", scene_tags),
                                    ("target_revision_id", target_revision_id),
                                )
                                if value is not None
                            ],
                            "reason_is_optional": True,
                        },
                    )
                return self.store.activate_candidate(
                    owner_id=self.owner_id,
                    model_id=self.model_id,
                    scope=scope,
                    candidate_id=candidate_id,
                    expected_candidate_hash=expected_candidate_hash,
                    expected_active_revision=expected_active_revision,
                    expected_row_version=expected_profile_version,
                    wake_id=binding["wake_id"],
                    wake_seq=binding["wake_seq"],
                    ai_confirmation=ai_confirmation,
                    reason=reason,
                    actor="ai",
                )
            return self._deny("invalid_governance_action")
        except (SelfGovernanceError, ExecutionBindingError) as exc:
            return self._deny(str(exc))

    def query(
        self,
        *,
        view: GovernanceQueryView,
        scope: str | None = None,
        include_content: bool = False,
    ) -> dict[str, Any]:
        if view == "status":
            result: Any = self.store.status(
                owner_id=self.owner_id,
                model_id=self.model_id,
                include_content=include_content,
            )
        elif view == "manual":
            result = self.store.manual(owner_id=self.owner_id, model_id=self.model_id)
        elif view == "revisions":
            if scope is None:
                return self._deny("governance_scope_required")
            try:
                result = self.store.revisions(
                    owner_id=self.owner_id,
                    model_id=self.model_id,
                    scope=scope,
                    include_content=include_content,
                )
            except SelfGovernanceError as exc:
                return self._deny(str(exc))
        else:
            return self._deny("invalid_governance_query_view")
        return {
            "module": "self_governance_profile",
            "view": view,
            "result": result,
        }

    def status(self) -> dict[str, Any]:
        return self.store.status(
            owner_id=self.owner_id,
            model_id=self.model_id,
            include_content=False,
        )

    def manual(self) -> dict[str, Any]:
        return self.store.manual(owner_id=self.owner_id, model_id=self.model_id)

    def current_action_contract(
        self, *, write_context_ref: str | None
    ) -> dict[str, Any]:
        """Build exact, current-wake activation calls without guessing null/CAS fields."""

        status = self.store.status(
            owner_id=self.owner_id,
            model_id=self.model_id,
            include_content=False,
        )
        pending_activations: list[dict[str, Any]] = []
        blocked_candidates: list[dict[str, Any]] = []
        binding = self._binding(write_context_ref)
        if binding is not None:
            current_wake_seq = int(binding["wake_seq"])
            for scope, scope_state in status["scopes"].items():
                for candidate in scope_state["pending_candidates"]:
                    if current_wake_seq <= int(candidate["created_wake_seq"]):
                        blocked_candidates.append(
                            {
                                "scope": scope,
                                "candidate_id": candidate["candidate_id"],
                                "reason": "later_real_wake_required",
                            }
                        )
                        continue
                    arguments = {
                        "action": "activate",
                        "scope": scope,
                        "write_context_ref": binding["write_context_ref"],
                        "expected_profile_version": scope_state["row_version"],
                        "candidate_id": candidate["candidate_id"],
                        "expected_candidate_hash": candidate["candidate_hash"],
                        "ai_confirmation": True,
                    }
                    if candidate["base_revision_id"] is not None:
                        arguments["expected_active_revision"] = candidate[
                            "base_revision_id"
                        ]
                    pending_activations.append(
                        {
                            "tool": "manage_self_governance_profile",
                            "candidate_id": candidate["candidate_id"],
                            "arguments": arguments,
                            "optional_arguments": {
                                "reason": "可选：本轮由 AI 自己写下的确认理由。"
                            },
                            "copy_exactly": True,
                            "first_activation_rule": (
                                "arguments 中省略 expected_active_revision 即表示首次激活；"
                                "不要自行补成 false、空字符串或字符串 'null'。"
                            ),
                        }
                    )
        return {
            "contract": "self-governance-current-action/1",
            "pending_activations": pending_activations,
            "pending_activation_count": len(pending_activations),
            "blocked_candidates": blocked_candidates,
            "creation_wake_activation_forbidden": True,
        }


__all__ = [
    "GovernanceAction",
    "GovernanceQueryView",
    "SelfGovernanceAccessService",
]
