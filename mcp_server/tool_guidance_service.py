"""Owner-scoped facade for the module-four tool-guidance runtime.

The facade binds every mutation to an open write context from module one and
to a host-derived live tool catalog.  It intentionally exposes advice and
attempt-gate decisions only; it cannot invoke, proxy, or grant access to any
target tool.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from runtime.tool_guidance import (
    TOOL_GUIDANCE_CONTRACT_VERSION,
    ToolGuidanceError,
    ToolGuidanceStore,
    normalize_catalog,
)


CatalogProvider = Callable[[Mapping[str, Any] | None], Mapping[str, Any] | None]


class ToolGuidanceAccessService:
    """Bind tool-brain reads and writes to one owner/model namespace."""

    def __init__(
        self,
        store: ToolGuidanceStore,
        *,
        onboarding: Any,
        owner_id: str,
        model_id: str,
        catalog_provider: CatalogProvider | Mapping[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.onboarding = onboarding
        self.owner_id = owner_id.strip()
        self.model_id = model_id.strip()
        self.catalog_provider = catalog_provider
        if not self.owner_id or not self.model_id:
            raise ValueError("owner_id and model_id must not be empty")
        self.store.ensure_state(owner_id=self.owner_id, model_id=self.model_id)

    def status(self) -> dict[str, Any]:
        return self.store.status(owner_id=self.owner_id, model_id=self.model_id)

    @staticmethod
    def _reject(reason: str, *, status: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "module": "tool_guidance_module",
            "contract_version": TOOL_GUIDANCE_CONTRACT_VERSION,
            "decision": "reject",
            "reason_codes": [reason],
            "status": status,
            "state_changed": False,
            "active_version_changed": False,
            "execution_performed": False,
        }
        if reason == "risk_below_runtime_floor":
            result["repair_guidance"] = {
                "real_world_action_minimum_risk_level": "high",
                "high_or_critical_confirmation_policy": "explicit_each_time",
                "critical_required": False,
                "message": (
                    "现实动作卡最低为 high，允许更严格的 critical；high/critical 均须 "
                    "explicit_each_time。请根据具体操作重新判断，不要把拒绝误解为必须 critical，"
                    "也不要仅为通过校验改写风险事实。重大候选同样不能低于此下限。"
                ),
                "permission_authority": "none",
            }
        elif reason == "invalid_scenario_tags_item":
            result["repair_guidance"] = {
                "field": "scenario_tags",
                "item_pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
                "message": (
                    "scenario_tags 是机器标签，例如 home.arrival；中文场景描述请放在 "
                    "scenario_examples、keywords 或 aliases，不能丢掉这些自然语言线索。"
                ),
            }
        return result

    def _permission(self) -> Mapping[str, Any]:
        return self.onboarding.authorize_other_module_write(
            owner_id=self.owner_id,
            model_id=self.model_id,
            module_name="tool_guidance_module",
        )

    def _binding(self, write_context_ref: str) -> Mapping[str, Any] | None:
        if self._permission().get("decision") != "allowed":
            return None
        binding = self.onboarding.current_open_write_context(
            owner_id=self.owner_id,
            model_id=self.model_id,
            write_context_ref=write_context_ref,
            required_scope="tool_guidance",
        )
        if binding.get("write_context_available") is not True:
            return None
        if not isinstance(binding.get("wake_id"), str):
            return None
        wake_seq = binding.get("wake_seq")
        if isinstance(wake_seq, bool) or not isinstance(wake_seq, int):
            return None
        return binding

    def _catalog(
        self, binding: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any] | None:
        provider = self.catalog_provider
        if provider is None:
            return None
        if isinstance(provider, Mapping):
            return provider
        return provider(binding)

    def _write(
        self,
        write_context_ref: str,
        model_values: Any,
        callback: Callable[[Mapping[str, Any], Mapping[str, Any] | None], dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(write_context_ref, str) or not write_context_ref.strip():
            return self._reject("brain_open_required", status=self.status())
        binding = self._binding(write_context_ref.strip())
        if binding is None:
            reason = (
                "module_one_required"
                if self._permission().get("decision") != "allowed"
                else "brain_open_required"
            )
            return self._reject(reason, status=self.status())
        if self.onboarding.contains_protected_persistence_value(
            owner_id=self.owner_id,
            model_id=self.model_id,
            value=model_values,
        ):
            return self._reject("credential_or_secret_detected", status=self.status())
        try:
            result = callback(binding, self._catalog(binding))
        except ToolGuidanceError as exc:
            return self._reject(str(exc), status=self.status())
        except TypeError:
            return self._reject("invalid_tool_guidance_fields", status=self.status())
        return {
            "module": "tool_guidance_module",
            "contract_version": TOOL_GUIDANCE_CONTRACT_VERSION,
            **result,
            "execution_performed": False,
        }

    def manual(self, *, write_context_ref: str | None = None) -> dict[str, Any]:
        """Return the manual and, only for a valid open wake, full candidates."""

        pending: list[dict[str, Any]] = []
        reason_codes: list[str] = []
        catalog_snapshot: Mapping[str, Any] | None = None
        if write_context_ref:
            binding = self._binding(write_context_ref.strip())
            if binding is None:
                reason_codes.append(
                    "module_one_required"
                    if self._permission().get("decision") != "allowed"
                    else "brain_open_required"
                )
            else:
                try:
                    catalog_snapshot = self._catalog(binding)
                    pending = self.store.present_pending_candidates(
                        owner_id=self.owner_id,
                        model_id=self.model_id,
                        wake_id=binding["wake_id"],
                        wake_seq=binding["wake_seq"],
                        catalog=catalog_snapshot,
                    )
                except ToolGuidanceError as exc:
                    reason_codes.append(str(exc))
        return {
            "module": "tool_guidance_module",
            "contract_version": TOOL_GUIDANCE_CONTRACT_VERSION,
            "purpose": (
                "我可以详细保存某个具体 callable operation 的历史使用认知；"
                "普通召回只给可选短摘要，工具脑本身绝不执行工具。"
            ),
            "principles": [
                "工具卡是历史建议，不是当前指令、权限、参数或执行回执。",
                "自动召回只输出某场景可考虑某工具的短摘要；需要细节时我可精准查询，也可不做。",
                "信息查询与现实动作分卡；危险现实动作每次仍须当前权限与当前明确确认。",
                "小修追加可回滚版本；用途、安全、场景、联动、Schema、生命周期或扩大显著性的修改先成为重大候选，并跨真实唤醒复核。",
                "新卡先保存单个操作；任何 linked_tool_refs、非 standalone chain_role 或 handoff_condition 必须随后通过重大修订建立，不能在创建时直接成为活动联动。",
                "v0.2 经验固定为 ai_reported，不保存凭证、原始参数、原始结果或伪造的 verified receipt。",
                "写下、计划或获准尝试都不等于完成；只有本轮目标工具的明确成功回执可证明结果。",
            ],
            "tools": {
                "remember_tool_guidance": "创建一张详细、具体操作级的独立 v1 工具卡；初建联动会被要求改走重大修订。",
                "recall_tool_guidance": "精准读取建议、卡片、历史或失败经验。",
                "revise_tool_guidance": "直接追加合法小修，或创建重大候选；不审核候选。",
                "review_tool_guidance_candidate": "在较晚真实唤醒复核已完整展示的重大候选。",
                "record_tool_experience": "追加 AI 对一次调用尝试的非验证经验。",
            },
            "write_rule": (
                "模块一 live 后，每个真实外部唤醒先调用 stbrain_open；写入带本轮 "
                "write_context_ref 与最新 tool_memory.row_version。同一唤醒的后续写入复用 ref，"
                "并使用上一写入返回的新 tool_row_version。"
            ),
            "authoring_constraints": {
                "scenario_tags": (
                    "每项为 1–128 个 ASCII 字母、数字、点、下划线或短横线，首位须为字母或数字；"
                    "如 home.arrival。中文场景写在 scenario_examples、keywords 或 aliases。"
                ),
                "real_world_action": (
                    "最低风险级是 high，不是必须 critical；high/critical 均须 explicit_each_time。"
                    "可以主动记下或提出工具建议，但卡片不是当前执行授权；重大候选也不豁免风险下限。"
                ),
            },
            "pending_candidates": pending,
            "reason_codes": reason_codes,
            "catalog_state": {
                key: value
                for key, value in normalize_catalog(catalog_snapshot).items()
                if key != "entries"
            },
            "status": self.status(),
            "execution_performed": False,
        }

    def remember(
        self,
        *,
        write_context_ref: str,
        expected_tool_row_version: int,
        **fields: Any,
    ) -> dict[str, Any]:
        return self._write(
            write_context_ref,
            {
                "expected_tool_row_version": expected_tool_row_version,
                "fields": fields,
            },
            lambda binding, catalog: self.store.remember(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                expected_row_version=expected_tool_row_version,
                catalog=catalog,
                **fields,
            ),
        )

    def revise(
        self,
        *,
        write_context_ref: str,
        expected_tool_row_version: int,
        card_id: str,
        expected_card_version: int,
        intent: str,
        edit_class: str,
        reason: str,
        **fields: Any,
    ) -> dict[str, Any]:
        return self._write(
            write_context_ref,
            {
                "expected_tool_row_version": expected_tool_row_version,
                "card_id": card_id,
                "expected_card_version": expected_card_version,
                "intent": intent,
                "edit_class": edit_class,
                "reason": reason,
                "fields": fields,
            },
            lambda binding, catalog: self.store.revise(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                wake_seq=binding["wake_seq"],
                expected_row_version=expected_tool_row_version,
                card_id=card_id,
                expected_card_version=expected_card_version,
                intent=intent,
                edit_class=edit_class,
                reason=reason,
                catalog=catalog,
                **fields,
            ),
        )

    def review(
        self,
        *,
        write_context_ref: str,
        expected_tool_row_version: int,
        candidate_id: str,
        candidate_hash: str,
        decision: str,
        correctness_decision: str,
        correctness_assessment: str,
        reason: str,
        ai_confirmation: bool,
        expected_base_version: int,
    ) -> dict[str, Any]:
        return self._write(
            write_context_ref,
            {
                "expected_tool_row_version": expected_tool_row_version,
                "candidate_id": candidate_id,
                "candidate_hash": candidate_hash,
                "decision": decision,
                "correctness_decision": correctness_decision,
                "correctness_assessment": correctness_assessment,
                "reason": reason,
                "ai_confirmation": ai_confirmation,
                "expected_base_version": expected_base_version,
            },
            lambda binding, catalog: self.store.review_candidate(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                wake_seq=binding["wake_seq"],
                expected_row_version=expected_tool_row_version,
                candidate_id=candidate_id,
                candidate_hash=candidate_hash,
                decision=decision,
                correctness_decision=correctness_decision,
                correctness_assessment=correctness_assessment,
                reason=reason,
                ai_confirmation=ai_confirmation,
                expected_base_version=expected_base_version,
                catalog=catalog,
            ),
        )

    def record_experience(
        self,
        *,
        write_context_ref: str,
        expected_tool_row_version: int,
        **fields: Any,
    ) -> dict[str, Any]:
        return self._write(
            write_context_ref,
            {
                "expected_tool_row_version": expected_tool_row_version,
                "fields": fields,
            },
            lambda binding, catalog: self.store.record_experience(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                expected_row_version=expected_tool_row_version,
                catalog=catalog,
                **fields,
            ),
        )

    def recall(self, **fields: Any) -> dict[str, Any]:
        if self._permission().get("decision") != "allowed":
            return self._reject("module_one_required", status=self.status())
        try:
            result = self.store.recall(
                owner_id=self.owner_id,
                model_id=self.model_id,
                catalog=self._catalog(None),
                **fields,
            )
        except ToolGuidanceError as exc:
            return self._reject(str(exc), status=self.status())
        except TypeError:
            return self._reject("invalid_tool_guidance_fields", status=self.status())
        return {
            "module": "tool_guidance_module",
            "contract_version": TOOL_GUIDANCE_CONTRACT_VERSION,
            **result,
            "execution_performed": False,
        }

    def recall_for_injection(self, *, query: str, limit: int = 5) -> dict[str, Any]:
        if self._permission().get("decision") != "allowed":
            return self._reject("module_one_required", status=self.status())
        try:
            result = self.store.build_recall_envelopes(
                owner_id=self.owner_id,
                model_id=self.model_id,
                query=query,
                catalog=self._catalog(None),
                limit=limit,
            )
        except ToolGuidanceError as exc:
            return self._reject(str(exc), status=self.status())
        return {
            "module": "tool_guidance_module",
            "contract_version": TOOL_GUIDANCE_CONTRACT_VERSION,
            **result,
            "execution_performed": False,
        }

    def execution_gate(
        self,
        *,
        card_id: str,
        current_user_intent: bool,
        authorization_verified: bool,
        current_confirmation: bool,
    ) -> dict[str, Any]:
        """Return an attempt decision without invoking the advertised tool."""

        if self._permission().get("decision") != "allowed":
            return self._reject("module_one_required", status=self.status())
        try:
            result = self.store.execution_gate(
                owner_id=self.owner_id,
                model_id=self.model_id,
                card_id=card_id,
                catalog=self._catalog(None),
                current_user_intent=current_user_intent,
                authorization_verified=authorization_verified,
                current_confirmation=current_confirmation,
            )
        except ToolGuidanceError as exc:
            return self._reject(str(exc), status=self.status())
        return {
            "module": "tool_guidance_module",
            "contract_version": TOOL_GUIDANCE_CONTRACT_VERSION,
            **result,
            "execution_performed": False,
        }
