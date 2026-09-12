"""Owner-scoped facade for the module-four tool-guidance runtime.

The facade binds mutations to their owner/model context. A host-derived tool
catalog is diagnostic for advice, not a prerequisite for reminders. It exposes advice and
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
    reminder_recall_guidance,
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
    def _reject(
        reason: str, *, status: dict[str, Any], confidence_authoring: bool = False
    ) -> dict[str, Any]:
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
        if reason == "invalid_confidence" and confidence_authoring:
            result["repair_guidance"] = {
                "field": "confidence",
                "expected_type": "integer",
                "minimum": 0,
                "maximum": 100,
                "message": "工具卡与工具经验的 confidence 由我填写 0–100 的整数，包含 0 和 100；经验保持 AI 自报，不因分数升为独立核验。",
            }
        elif reason == "risk_below_runtime_floor":
            result["repair_guidance"] = {
                "real_world_action_minimum_risk_level": "high",
                "high_or_critical_confirmation_policy": "explicit_each_time",
                "critical_required": False,
                "message": (
                    "现实动作卡最低为 high，允许更严格的 critical；high/critical 均须 "
                    "explicit_each_time。请根据具体操作重新判断，不要把拒绝误解为必须 critical，"
                    "也不要仅为通过校验改写风险事实。普通修改同样保留执行风险下限。"
                ),
                "permission_authority": "none",
            }
        elif reason == "invalid_scenario_tags_item":
            result["repair_guidance"] = {
                "field": "scenario_tags",
                "item_pattern": r"^(?=.*\S)[^\u0000-\u001f\u007f-\u009f]{1,128}$",
                "message": (
                    "scenario_tags 支持中文或英文自然场景，如 回家了、准备睡觉、home.arrival。"
                    "每项为 1–128 字的非空文本，最多 16 项且不重复；请移除换行等控制字符。"
                ),
            }
        if reason in {"card_not_found", "tool_card_not_found", "tool_card_exists",
                      "tool_card_version_not_found", "candidate_not_found", "card_retired_use_restore",
                      "tool_card_version_conflict", "tool_row_version_conflict", "linked_tool_ref_not_found"}:
            result["lookup_guidance"] = {
                "tool": "recall_tool_guidance", "arguments": {"view": "directory", "limit": 5},
                "message": "先查询工具卡目录，用返回的 card_id 查看原文、版本和历史。",
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
        *,
        confidence_authoring: bool = False,
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
            return self._reject(
                str(exc), status=self.status(), confidence_authoring=confidence_authoring
            )
        except TypeError:
            return self._reject("invalid_tool_guidance_fields", status=self.status())
        return {
            "module": "tool_guidance_module",
            "contract_version": TOOL_GUIDANCE_CONTRACT_VERSION,
            **result,
            "execution_performed": False,
        }

    def manual(self, *, write_context_ref: str | None = None) -> dict[str, Any]:
        """Return the manual and full candidates for a valid trusted binding."""

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
                        ordinary_author=binding.get("context_mode") == "ordinary_authenticated",
                    )
                except ToolGuidanceError as exc:
                    reason_codes.append(str(exc))
        return {
            "module": "tool_guidance_module",
            "contract_version": TOOL_GUIDANCE_CONTRACT_VERSION,
            "purpose": "我可以按自己的场景词写一句工具提醒，并在需要时查询完整原文。",
            "principles": [
                "自动浮现使用我写的 reminder；旧卡兼容引用 purpose 首句，保留原版本。",
                "场景匹配与作者开关决定提醒；服务未广告、Schema 变化、信心、旧有效期和失败冷却用于详情诊断。",
                "具体工具名、参数说明与使用经验在手动详情中查看；提醒与实际执行权限分别处理。",
                "普通作者字段直接追加新版本，历史可以查询；退役保留内容并停止自动提醒。",
                "旧 pending 可以直接撤回或由已认证普通作者明确采纳，保留候选记录；普通修改使用 revise_tool_guidance。",
                "经验为 AI 自述；实际结果由本轮目标工具的回执核对。",
            ],
            "tools": {
                "remember_tool_guidance": "保存工具或服务的用途、可选一句 reminder 和场景关键词；详情可随后补充。",
                "recall_tool_guidance": "view=directory 查目录；view=card 查原文；history 查历史；failures 查经验。",
                "revise_tool_guidance": "直接追加修改版本；intent=retire 退役，intent=restore 恢复历史版本。",
                "review_tool_guidance_candidate": "旧候选可 withdraw 撤回；已认证普通作者 accept/keep_pending 按精确候选与版本决定，真实旧 wake 路径保留原复核检查。",
                "record_tool_experience": "追加 AI 对一次调用尝试的非验证经验。",
            },
            "write_rule": "写入使用服务提供的当前身份与版本。沿用返回的 tool_row_version 和卡片 version，冲突时先查目录与原文。",
            "authoring_constraints": {
                "confidence": (
                    "工具卡 confidence 由我按自己的判断填写 0–100 的整数，source_type 如实记录来源。"
                    "record_tool_experience 的经验 confidence 同样为 0–100，仍是自报、未独立核验。"
                ),
                "expires_at": (
                    "有效期由我决定；新卡省略或 null 表示长期保留。日期使用带时区的 ISO 时间，"
                    "可以如实记录过去日期。修改时省略或 null 保留旧日期，clear_fields=[\"expires_at\"] 明确清除。"
                ),
                "reminder": "可选，一句 1–100 字的自然语言提醒；具体工具标识与调用参数写在原文详情。",
                "scenario_tags": (
                    "支持中文或英文自然场景，如 回家了、准备睡觉、home.arrival；"
                    "每项为 1–128 字的非空文本，最多 16 项且不重复，不含换行等控制字符。"
                ),
                "real_world_action": (
                    "最低风险级是 high，不是必须 critical；high/critical 均须 explicit_each_time。"
                    "可以主动记下工具建议；卡片的风险声明与本轮真实执行授权分别核对。"
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
            confidence_authoring=True,
        )

    def revise(
        self,
        *,
        write_context_ref: str,
        expected_tool_row_version: int,
        card_id: str,
        expected_card_version: int,
        reason: str,
        intent: str = "revise",
        edit_class: str = "major",
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
            confidence_authoring=True,
        )

    def review(
        self,
        *,
        write_context_ref: str,
        expected_tool_row_version: int,
        candidate_id: str,
        candidate_hash: str,
        decision: str,
        reason: str,
        expected_base_version: int,
        correctness_decision: str | None = None,
        correctness_assessment: str | None = None,
        ai_confirmation: bool = False,
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
                ordinary_author=binding.get("context_mode") == "ordinary_authenticated",
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
            confidence_authoring=True,
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
            "reminder_recall_guidance": reminder_recall_guidance(),
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
