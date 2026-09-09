"""Wake-bound public facade for optional person-reference rewrite assistance."""

from __future__ import annotations

from typing import Any, Mapping

from runtime import (
    ALIAS_COMPARISON_PROFILE_VERSION,
    AUTHORING_ADVISORY,
    AUTHORING_REWRITE_CONTRACT_VERSION,
    AUTHORING_REWRITE_MODULES,
    AUTHORING_SCHEMA_VERSIONS,
    MENTION_PARSER_RULE_VERSION,
    REWRITE_ELIGIBLE_ALLOWLIST_VERSION,
    AuthoringError,
    AuthoringRewriteStore,
    ModuleOneOnboardingStore,
)


class AuthoringRewriteAccessService:
    """Bind preview/confirmation to the AI's manually opened real wake."""

    def __init__(
        self,
        store: AuthoringRewriteStore,
        *,
        onboarding: ModuleOneOnboardingStore,
        owner_id: str,
        model_id: str,
    ) -> None:
        self.store = store
        self.onboarding = onboarding
        self.owner_id = owner_id.strip()
        self.model_id = model_id.strip()
        if not self.owner_id or not self.model_id:
            raise ValueError("owner_id and model_id must not be empty")

    def status(self) -> dict[str, Any]:
        return self.store.status(owner_id=self.owner_id, model_id=self.model_id)

    def manual(self) -> dict[str, Any]:
        return {
            "contract_version": AUTHORING_REWRITE_CONTRACT_VERSION,
            "purpose": (
                "模块二至四可选的一次性指代词形建议；不会猜人物、改句构、润色或静默改原文。"
            ),
            "authoring_advisory": AUTHORING_ADVISORY,
            "workflow": [
                "每条新草稿的开关都从关闭开始；关闭时直接使用原模块写入工具。",
                "只有我主动开启、显式绑定 mention 与目标别名后才调用 preview_person_reference_rewrite。",
                "零安全命中、含糊、群聊或受保护 span 会返回 continue_original_path，不增加确认步骤。",
                "有改动时只显示 literal mention diff；我确认 exact final 后取得一次性 receipt。",
                "预览绑定当时的源草稿、最终字段、参与者/别名上下文和本轮真实唤醒；这些内容变了就重新 preview。",
                "服务端不会观察我未提交的私下草稿编辑；旧 source_draft_hash 只标识旧快照，不能证明当前草稿未变。",
                "把 receipt 连同完全相同的最终字段交给对应 remember 工具；receipt 与规范写入在同一事务消费。",
            ],
            "modules": {
                module: {
                    "eligible_scalar_field_paths": sorted(paths),
                    "module_schema_version": AUTHORING_SCHEMA_VERSIONS[module],
                }
                for module, paths in AUTHORING_REWRITE_MODULES.items()
            },
            "fixed_versions": {
                "rewrite_eligible_allowlist_version": REWRITE_ELIGIBLE_ALLOWLIST_VERSION,
                "mention_parser_rule_version": MENTION_PARSER_RULE_VERSION,
                "alias_comparison_profile_version": ALIAS_COMPARISON_PROFILE_VERSION,
            },
            "status": self.status(),
        }

    @staticmethod
    def _reject(reason: str, *, status: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "module": "shared_person_authoring_layer",
            "contract_version": AUTHORING_REWRITE_CONTRACT_VERSION,
            "decision": "reject",
            "reason_codes": [reason],
            "status": dict(status),
            "state_changed": False,
        }

    def _binding(self, write_context_ref: str, module: str) -> dict[str, Any] | None:
        if module not in AUTHORING_REWRITE_MODULES:
            return None
        permission = self.onboarding.authorize_other_module_write(
            owner_id=self.owner_id,
            model_id=self.model_id,
            module_name=module,
        )
        if permission.get("decision") != "allowed":
            return None
        binding = self.onboarding.current_open_write_context(
            owner_id=self.owner_id,
            model_id=self.model_id,
            write_context_ref=write_context_ref,
            required_scope="shared_person_authoring",
        )
        return binding if binding.get("write_context_available") is True else None

    def preview(
        self,
        *,
        write_context_ref: str,
        expected_authoring_version: int,
        module: str,
        **fields: Any,
    ) -> dict[str, Any]:
        status = self.status()
        if not isinstance(write_context_ref, str) or not write_context_ref.strip():
            return self._reject("brain_open_required", status=status)
        if module not in AUTHORING_REWRITE_MODULES:
            return self._reject("rewrite_module_invalid", status=status)
        binding = self._binding(write_context_ref.strip(), module)
        if binding is None:
            return self._reject("brain_open_or_module_one_required", status=status)
        if self.onboarding.contains_protected_persistence_value(
            owner_id=self.owner_id,
            model_id=self.model_id,
            value={
                "expected_authoring_version": expected_authoring_version,
                "module": module,
                "fields": fields,
            },
        ):
            return self._reject("credential_or_secret_detected", status=status)
        try:
            result = self.store.preview(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                expected_row_version=expected_authoring_version,
                module=module,
                **fields,
            )
        except AuthoringError as exc:
            return self._reject(str(exc), status=self.status())
        return {
            "module": "shared_person_authoring_layer",
            "contract_version": AUTHORING_REWRITE_CONTRACT_VERSION,
            **result,
        }

    def confirm(
        self,
        *,
        write_context_ref: str,
        expected_authoring_version: int,
        module: str,
        **fields: Any,
    ) -> dict[str, Any]:
        status = self.status()
        if not isinstance(write_context_ref, str) or not write_context_ref.strip():
            return self._reject("brain_open_required", status=status)
        if module not in AUTHORING_REWRITE_MODULES:
            return self._reject("rewrite_module_invalid", status=status)
        binding = self._binding(write_context_ref.strip(), module)
        if binding is None:
            return self._reject("brain_open_or_module_one_required", status=status)
        if self.onboarding.contains_protected_persistence_value(
            owner_id=self.owner_id,
            model_id=self.model_id,
            value={
                "expected_authoring_version": expected_authoring_version,
                "module": module,
                "fields": fields,
            },
        ):
            return self._reject("credential_or_secret_detected", status=status)
        try:
            result = self.store.confirm(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                expected_row_version=expected_authoring_version,
                **fields,
            )
        except AuthoringError as exc:
            return self._reject(str(exc), status=self.status())
        return {
            "module": "shared_person_authoring_layer",
            "contract_version": AUTHORING_REWRITE_CONTRACT_VERSION,
            "target_module": module,
            **result,
        }


__all__ = ["AuthoringRewriteAccessService"]
