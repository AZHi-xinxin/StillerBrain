"""Wake-bound public facade for optional person-reference rewrite assistance."""

from __future__ import annotations

from typing import Any, Mapping

from runtime import (
    ALIAS_COMPARISON_PROFILE_VERSION,
    AUTHORING_REWRITE_CONTRACT_VERSION,
    AUTHORING_REWRITE_MODULES,
    AUTHORING_SCHEMA_VERSIONS,
    MENTION_PARSER_RULE_VERSION,
    REWRITE_ELIGIBLE_ALLOWLIST_VERSION,
    AuthoringError,
    AuthoringRewriteStore,
    ModuleOneOnboardingStore,
)
from runtime.execution_binding import ExecutionBindingError
from runtime.authoring import AUTHOR_DECLARED_REWRITE_MODE
from runtime.person_reference_advisory import (
    PersonReferenceAdvisoryError,
    PersonReferenceAdvisoryStore,
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
        advisory_store: PersonReferenceAdvisoryStore | None = None,
    ) -> None:
        self.store = store
        self.onboarding = onboarding
        self.owner_id = owner_id.strip()
        self.model_id = model_id.strip()
        if not self.owner_id or not self.model_id:
            raise ValueError("owner_id and model_id must not be empty")
        self.advisory_store = advisory_store
        if self.advisory_store is None and isinstance(store, AuthoringRewriteStore):
            self.advisory_store = PersonReferenceAdvisoryStore(store.database)

    def advisory_status(self) -> dict[str, Any]:
        """Read the effective optional advice, without creating a preference row."""
        if self.advisory_store is None:
            raise RuntimeError("person_reference_advisory_store_unavailable")
        return self.advisory_store.read(owner_id=self.owner_id, model_id=self.model_id)

    def manage_advisory(self, *, action: str, text: str | None = None,
                        write_context_ref: str = "") -> dict[str, Any]:
        """Apply one author-requested preference change, independently of rewriting."""
        if self.advisory_store is None:
            raise RuntimeError("person_reference_advisory_store_unavailable")
        try:
            result = self.advisory_store.manage(
                owner_id=self.owner_id, model_id=self.model_id, action=action, text=text,
                write_context_ref=write_context_ref, onboarding=self.onboarding)
        except (PersonReferenceAdvisoryError, ExecutionBindingError) as exc:
            return {"module": "shared_person_authoring_layer", "decision": "reject",
                    "reason_codes": [str(exc)], "state_changed": False,
                    "rewrite_assist_changed": False, "memory_content_changed": False}
        return {"module": "shared_person_authoring_layer", **result}

    def status(self) -> dict[str, Any]:
        return self.store.status(owner_id=self.owner_id, model_id=self.model_id)

    def manual(self) -> dict[str, Any]:
        return {
            "contract_version": AUTHORING_REWRITE_CONTRACT_VERSION,
            "purpose": (
                "AI 指明称呼指谁、替换成什么，先看这份草稿的字面修改，再由 AI 确认。人物来源是作者声明，服务不推断身份或冒充宿主认证。"
            ),
            "validation_mode": AUTHOR_DECLARED_REWRITE_MODE,
            "minimal_input": {
                "preview": ["module", "draft_fields", "rewrite_targets"],
                "target": ["field_path", "surface_form", "entity_ref", "target_surface_form"],
                "confirm": ["preview_id", "ai_confirmation"],
                "occurrence_index": "同一字段内原词只出现一次可省；出现多次时，明确填写从0开始的某一次，服务不会猜位置或整段全替。",
                "entity_ref": "作者给人物的名称即可；不需要人物认证、别名登记或另交一份 referent_bindings。",
                "legacy_fields": "旧绑定的状态或confidence不会变成新审批；本轮rewrite_targets是作者明确指定，若同时传入旧绑定，其人物名称不能与目标矛盾。旧别名/unique字段兼容接收，不作为认证或别名裁决。",
            },
            "authoring_advisory": self.advisory_status(),
            "version_help": {
                "field": "module_schema_version",
                "rule": "版本可省略，服务按当前模块或保存的预览填写；显式提供的版本仍检查。这与工具返回的 contract_version、public-tools 版本和草稿/状态版本不同。",
                "current_module_schema_versions": dict(AUTHORING_SCHEMA_VERSIONS),
                "manual_tool": "stbrain_open",
                "manual_arguments": {"view": "manual", "module": "shared_person_authoring"},
                "manual_path": "shared_person_authoring.modules.<目标模块>.module_schema_version",
                "usage": "通常省略所有版本与hash即可；旧客户端显式提供时仍校验，旧模块版本的预览需重新生成。",
            },
            "field_format": {
                "rule": "draft_fields 和 final_fields 的键使用带 / 的完整字段路径；绑定、目标和保护范围中的 field_path 使用同一路径。",
                "ordinary_writer_mapping": {
                    "emotional_memory": {"content": "/original_text", "summary": "/summary"},
                    "learning_memory": {"content": "/current_understanding", "summary": "/summary", "title": "/title"},
                    "tool_guidance": {"purpose": "/purpose", "call_notes": "/call_notes"},
                },
                "emotional_draft_fields_example": {"/original_text": "我和她完成了练习。", "/summary": "共同练习。"},
                "learning_draft_fields_example": {"/title": "一起练习", "/summary": "共同练习。", "/current_understanding": "我和她完成了练习。", "/preceding_context_summary": ""},
                "example_scope": "示例展示完整存入所需字段映射；预览只改指定词形，带回执存入的字段集合和文本须与完整预览相同。",
                "confirm": "只需 preview_id 和 ai_confirmation=true；服务使用已展示的 suggested_fields，返回完整 final_fields。显式提交字段或hash时仍精确校验；草稿变化时重新预览。",
                "learning_unified_writer": "学习通过 remember_memory 存入时，/preceding_context_summary 对应空字符串；其他取值走专用学习写入入口。",
            },
            "workflow": [
                "每条新草稿的开关都从关闭开始；关闭时直接使用原模块写入工具。",
                "我提供 module、完整 draft_fields 和 rewrite_targets（称呼指谁、替换成什么）即可主动预览。",
                "群聊、未知会话模式和历史人物也可明确指定；零命中、位置不明确或受保护 span 返回 continue_original_path，不增加确认步骤。",
                "有改动时只显示 literal mention diff；我确认 exact final 后取得一次性 receipt。",
                "新预览保留作者声明、原稿和最终字段；普通已认证连接可跨请求确认，其余连接沿原唤醒绑定。旧预览继续原有上下文规则，不自动转成新模式。",
                "protected_spans 可选且生效；服务没有自动识别引号或代码区域的解析器，需要保护的区域由作者显式提供。",
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
    def _reject(reason: str, *, status: Mapping[str, Any], target_module: str | None = None) -> dict[str, Any]:
        result = {
            "module": "shared_person_authoring_layer",
            "contract_version": AUTHORING_REWRITE_CONTRACT_VERSION,
            "decision": "reject",
            "reason_codes": [reason],
            "status": dict(status),
            "state_changed": False,
        }
        if (reason == "module_schema_version_stale" and type(target_module) is str
                and target_module in AUTHORING_SCHEMA_VERSIONS):
            # Public server constants only: never echo the supplied version or
            # imply that checks after this failed validation already passed.
            result["schema_version_help"] = {
                "field": "module_schema_version",
                "target_module": target_module,
                "expected_module_schema_version": AUTHORING_SCHEMA_VERSIONS[target_module],
                "manual_tool": "stbrain_open",
                "manual_arguments": {"view": "manual", "module": "shared_person_authoring"},
                "manual_path": f"shared_person_authoring.modules.{target_module}.module_schema_version",
                "message": (
                    "这里填写记忆模块的 schema 版本，按 expected_module_schema_version 的当前值重试。"
                    "这与工具的 contract_version 不同；此错误只说明当前版本检查失败，其他参数仍须按原流程检查。"
                    "手册导航供查阅，当前所需版本已直接列出。"
                    "已有预览若按旧模块版本生成，应重新 preview；不要仅替换确认参数中的版本。"
                ),
            }
        return result

    def _binding(self, write_context_ref: str, module: str) -> dict[str, Any] | None:
        if module not in AUTHORING_REWRITE_MODULES:
            return None
        from runtime.ordinary_access import current_ordinary_access
        if current_ordinary_access(owner_id=self.owner_id, model_id=self.model_id, scope='shared_person_authoring'):
            binding = self.onboarding.current_open_write_context(
                owner_id=self.owner_id, model_id=self.model_id,
                write_context_ref=write_context_ref, required_scope='shared_person_authoring')
            return binding if binding.get('write_context_available') is True else None
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
        expected_authoring_version: int | None = None,
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
            prepared = {key: value for key, value in fields.items() if value is not None}
            defaults = {
                "draft_version": 0, "referent_bindings": [], "conversation_mode": None,
                "authenticated_participant_entity_ids": None, "alias_collision_scope": None,
                "alias_collision_scope_version": None, "protected_spans": [],
                "module_schema_version": AUTHORING_SCHEMA_VERSIONS[module],
                "rewrite_eligible_allowlist_version": REWRITE_ELIGIBLE_ALLOWLIST_VERSION,
                "mention_parser_rule_version": MENTION_PARSER_RULE_VERSION,
                "alias_comparison_profile_version": ALIAS_COMPARISON_PROFILE_VERSION,
            }
            prepared = {**defaults, **prepared}
            result = self.store.preview(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                expected_row_version=status["row_version"] if expected_authoring_version is None else expected_authoring_version,
                module=module,
                validation_mode=AUTHOR_DECLARED_REWRITE_MODE,
                **prepared,
            )
        except AuthoringError as exc:
            return self._reject(str(exc), status=self.status(), target_module=module)
        return {
            "module": "shared_person_authoring_layer",
            "contract_version": AUTHORING_REWRITE_CONTRACT_VERSION,
            **result,
        }

    def confirm(
        self,
        *,
        write_context_ref: str,
        expected_authoring_version: int | None = None,
        module: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        status = self.status()
        if not isinstance(write_context_ref, str) or not write_context_ref.strip():
            return self._reject("brain_open_required", status=status)
        if fields.get("ai_confirmation") is not True:
            return self._reject("ai_confirmation_required", status=status)
        try:
            snapshot = self.store.confirmation_inputs(owner_id=self.owner_id, model_id=self.model_id,
                                                     preview_id=fields.get("preview_id"))
        except AuthoringError as exc:
            return self._reject(str(exc), status=status)
        if module is None:
            module = snapshot["module"]
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
            prepared = {**snapshot["arguments"],
                        **{key: value for key, value in fields.items() if value is not None}}
            result = self.store.confirm(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                expected_row_version=status["row_version"] if expected_authoring_version is None else expected_authoring_version,
                expected_module=module,
                **prepared,
            )
        except AuthoringError as exc:
            return self._reject(str(exc), status=self.status(), target_module=module)
        return {
            "module": "shared_person_authoring_layer",
            "contract_version": AUTHORING_REWRITE_CONTRACT_VERSION,
            "target_module": module,
            **result,
            "final_fields": prepared["final_fields"],
            "confirmation_scope": "exact_saved_preview_only",
            "next_step": "采用本次预览的完整 final_fields，并带 rewrite_receipt 交给原模块存入；草稿改动后重新 preview。本次确认只对应已保存的这份预览。",
        }


__all__ = ["AuthoringRewriteAccessService"]
