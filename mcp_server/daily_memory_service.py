"""One-call ordinary memory facade, separate from self-modification review.

The MCP dispatcher owns execution-lease validation and binding. This facade
requires that bound claim, pins opening to its exact wake, and reuses existing
module permission/secret/context gates. An explicit reference is an exception
only for an already opened, human-attested direct context with matching scope.
No grant, latest-wake fallback, synthetic adoption, or automatic review exists.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from runtime.execution_binding import ExecutionBindingError, ExecutionClaim, canonical_hash, current_execution_claim
from runtime.onboarding import OnboardingError
from runtime.planning_memory import PlanningMemoryError, PLAN_KINDS, PLAN_TRACKS
from runtime.emotional_memory import EMOTION_LABELS, MEMORY_TYPES
from runtime.learning_memory import LEARNING_KINDS, SOURCE_BASES


_KINDS = {"emotional_memory": MEMORY_TYPES, "learning_memory": LEARNING_KINDS,
          "planning_memory": PLAN_KINDS}
_DEFAULT_KINDS = {"emotional_memory": "meaningful_dialogue", "learning_memory": "fact",
                  "planning_memory": "task"}
_NEUTRAL_REASON = "日常记忆直接存入；不代表独立核验、AI 采纳声明或自我修改审核。"
_UNCHECKED_ASSESSMENT = "未执行独立核验；按调用者给出的来源类别保存，不因此标记为已验证。"
_SAFE_FAILURES = frozenset({
    "module_one_required", "credential_or_secret_detected", "credential_content_rejected",
    "brain_open_required", "current_injected_wake_required", "current_wake_required",
    "write_context_expired", "injected_context_required", "write_context_binding_mismatch",
    "write_context_not_opened_or_mismatched", "direct_scope_not_authorized",
    "direct_grant_expired", "direct_grant_invalid", "direct_grant_required",
    "execution_wake_mismatch", "execution_owner_mismatch", "execution_claim_not_current",
    "execution_registry_unavailable", "planning_row_version_conflict",
    "emotion_row_version_conflict", "learning_row_version_conflict",
    "invalid_plan_ref", "plan_not_found", "stale_plan_ref", "quarantined_plan_ref",
    "invalid_plan_hierarchy", "planning_graph_cycle", "idempotency_key_reused",
    "due_before_start", "invalid_datetime", "invalid_direct_context",
})


class DailyMemoryAccessService:
    def __init__(self, onboarding: Any, emotional_service: Any, learning_service: Any,
                 planning_service: Any, owner_id: str, model_id: str) -> None:
        if not isinstance(owner_id, str) or not owner_id.strip() or not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("owner_id and model_id are required")
        self.onboarding = onboarding
        self.services = {"emotional_memory": emotional_service, "learning_memory": learning_service,
                         "planning_memory": planning_service}
        self.owner_id, self.model_id = owner_id.strip(), model_id.strip()
        if any(component is not None and (component.owner_id, component.model_id) != (self.owner_id, self.model_id)
               for component in self.services.values()):
            raise ValueError("daily module owner/model mismatch")

    @staticmethod
    def _reject(module: str, code: str, guidance: str | None = None, **details: Any) -> dict[str, Any]:
        if guidance is None:
            if "credential" in code:
                guidance = "移除凭据或密钥后再保存；不要把它们放入记忆正文。"
            elif "version_conflict" in code or code == "idempotency_key_reused":
                guidance = "本次未写入；请重新发起一次存入请求，服务会读取当前版本，不要并行重复提交。"
            elif code in {"invalid_plan_ref", "plan_not_found", "stale_plan_ref", "quarantined_plan_ref", "invalid_plan_hierarchy"}:
                guidance = "修正父项引用；若只需独立记录，省略 parent_ref 即可，计划类型无需更改。"
            elif code == "module_one_required":
                guidance = "当前记忆模块尚未开通；请先恢复或完成已有模块开通，不要反复提交同一内容。"
            elif code in {"due_before_start", "invalid_datetime"}:
                guidance = "将 due_at 改为明确的 ISO 日期时间；没有截止时间可省略。"
            else:
                guidance = "本次未存入；请恢复准确绑定的当前对话后重新发起请求，不要复用旧回合参数。"
        return {"module": module, "decision": "reject", "stored": False, "count": 0,
                "state_changed": False, "reason_codes": [code], "message": "未存入记忆。",
                "next_step": guidance, **details}

    def _runtime_reject(self, module: str, result: Mapping[str, Any]) -> dict[str, Any]:
        reasons = [result.get("binding_reason_code"), result.get("reason_code")]
        if isinstance(result.get("reason_codes"), list):
            reasons.extend(result["reason_codes"])
        code = next((item for item in reasons if isinstance(item, str) and item in _SAFE_FAILURES),
                    "daily_write_rejected")
        return self._reject(module, code)

    def remember(self, module: str, content: str, title: str | None = None,
                 summary: str | None = None, kind: str | None = None, track: str = "internal",
                 keywords: list[str] | None = None, importance: int = 50, emotion: str = "other",
                 source_basis: str = "reported", confidence: int = 50, reason: str | None = None,
                 write_context_ref: str | None = None, parent_ref: str | None = None,
                 due_at: str | None = None, timezone: str = "UTC") -> dict[str, Any]:
        if not isinstance(module, str) or module not in self.services:
            return self._reject("unknown", "invalid_module", "选择一个支持的日常记忆模块。",
                                allowed_values=sorted(self.services))
        component = self.services[module]
        if component is None:
            return self._reject(module, "module_unavailable", "此模块未配置；请先恢复该模块。")
        if not isinstance(content, str) or not content.strip() or len(content) > 2000:
            return self._reject(module, "invalid_content", "提供 1–2000 字的真实原文；服务不会截断原文。", field="content")
        for name, value, limit in (("title", title, 120), ("summary", summary, 200), ("reason", reason, 2000)):
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > limit):
                return self._reject(module, "invalid_" + name, f"填写非空的 {name}，最多 {limit} 字，或省略该可选字段。", field=name)
        selected_kind = _DEFAULT_KINDS[module] if kind is None else kind
        for name, value, allowed in (("kind", selected_kind, _KINDS[module]), ("source_basis", source_basis, SOURCE_BASES),
                                     ("emotion", emotion, EMOTION_LABELS), ("track", track, PLAN_TRACKS)):
            if not isinstance(value, str) or value not in allowed:
                return self._reject(module, "invalid_" + name, f"将 {name} 改为列出的值。", field=name, allowed_values=sorted(allowed))
        for name, value in (("importance", importance), ("confidence", confidence)):
            if type(value) is not int or not 0 <= value <= 100:
                return self._reject(module, "invalid_" + name, f"将 {name} 设为 0–100 的整数。", field=name)
        if keywords is not None and (not isinstance(keywords, list) or len(keywords) > 16
                                    or any(not isinstance(item, str) or not item.strip() or len(item) > 160 for item in keywords)):
            return self._reject(module, "invalid_keywords", "keywords 使用最多 16 个非空短语，每项不超过 160 字；也可省略。", field="keywords")
        if module != "planning_memory" and (parent_ref is not None or due_at is not None):
            return self._reject(module, "planning_fields_require_planning_module", "有父项或截止时间的计划请选择 planning_memory；其他记忆省略这两个字段。")
        for name, value, limit in (("parent_ref", parent_ref, 96), ("due_at", due_at, 80), ("timezone", timezone, 80)):
            if (value is not None or name == "timezone") and (not isinstance(value, str) or not value.strip() or len(value) > limit):
                return self._reject(module, "invalid_" + name, f"修正 {name} 为非空字符串；无父项或截止时间时省略该可选字段。", field=name)
        claim = current_execution_claim()
        if claim is not None:
            if (not isinstance(claim, ExecutionClaim) or claim.owner_id != self.owner_id
                    or claim.model_id != self.model_id or claim.tool_name != "remember_memory"):
                return self._reject(module, "execution_owner_mismatch")
            if write_context_ref is not None:
                return self._reject(module, "explicit_context_conflicts_with_bound_execution", "当前入口已自动绑定回合，请省略 write_context_ref。")
        elif not isinstance(write_context_ref, str) or not write_context_ref.strip():
            # In particular: never call open_brain_context to guess latest here.
            return self._reject(module, "execution_binding_required")
        elif write_context_ref.strip().startswith("$"):
            return self._reject(module, "invalid_direct_context")

        request = dict(module=module, content=content, title=title, summary=summary,
                       kind=selected_kind, track=track, keywords=keywords, importance=importance,
                       emotion=emotion, source_basis=source_basis, confidence=confidence,
                       reason=reason, parent_ref=parent_ref, due_at=due_at, timezone=timezone)
        try:
            if claim is not None:
                opened = self.onboarding.open_brain_context(
                    owner_id=self.owner_id, model_id=self.model_id, present_details=False,
                    expected_wake_id=claim.wake_id,
                )
                if opened.get("write_context_available") is not True:
                    return self._runtime_reject(module, opened)
                ref = opened.get("write_context_ref")
            else:
                ref = write_context_ref.strip()
            if not isinstance(ref, str) or not ref or ref.startswith("$"):
                return self._reject(module, "brain_open_required")
            binding = self.onboarding.current_open_write_context(
                owner_id=self.owner_id, model_id=self.model_id, write_context_ref=ref,
                required_scope=module, expected_wake_id=claim.wake_id if claim is not None else None,
            )
            if binding.get("write_context_available") is not True:
                return self._runtime_reject(module, binding)
            if (claim is None and binding.get("context_mode") != "human_attested_direct") or (
                claim is not None and (binding.get("context_mode") != "gateway_injected" or binding.get("wake_id") != claim.wake_id)
            ):
                return self._reject(module, "invalid_direct_context" if claim is None else "execution_wake_mismatch")
            if not isinstance(binding.get("wake_id"), str) or type(binding.get("wake_seq")) is not int:
                return self._reject(module, "brain_open_required")
            if claim is None:
                scopes = binding.get("authorized_scopes")
                if not isinstance(scopes, list) or module not in scopes:
                    return self._reject(module, "direct_scope_not_authorized")
            version = component.status().get("row_version")
            if type(version) is not int or version < 0:
                return self._reject(module, "module_version_unavailable")
            display_summary = content.strip()[:200] if summary is None else summary
            audit_reason = _NEUTRAL_REASON if reason is None else reason
            if module == "emotional_memory":
                result = component.remember(
                    write_context_ref=ref, expected_emotion_version=version, memory_type=selected_kind,
                    original_text=content, summary=title if summary is None and title is not None else display_summary,
                    primary_emotion=emotion, keywords=keywords, importance=importance,
                    origin={"observed": "firsthand", "reported": "reported", "inferred": "inferred"}[source_basis],
                    confidence=confidence, reason=audit_reason, preserve_original_text=True,
                )
            elif module == "learning_memory":
                result = component.remember(
                    write_context_ref=ref, expected_learning_version=version, kind=selected_kind,
                    title=content.strip()[:120] if title is None else title, summary=display_summary,
                    current_understanding=content, source_basis=source_basis,
                    claim_review={"status": "ordinary"}, confidence=confidence,
                    correctness_assessment=_UNCHECKED_ASSESSMENT, reason=audit_reason,
                    keywords=keywords, importance=importance, preserve_original_text=True,
                )
            else:
                identity = ([claim.deployment_epoch, claim.wake_id, claim.batch_id, claim.call_id]
                            if claim is not None else ["human_attested_direct", ref, canonical_hash(request)])
                result = component._write(ref, request, lambda verified: component.store.remember_ordinary(
                    owner_id=self.owner_id, model_id=self.model_id, write_context_ref=ref,
                    wake_id=verified["wake_id"], wake_seq=verified["wake_seq"], expected_row_version=version,
                    content=content, title=title, summary=summary, kind=selected_kind, track=track,
                    reason=reason,
                    keywords=keywords, importance=importance, parent_ref=parent_ref, due_at=due_at,
                    timezone=timezone, idempotency_key="daily-" + canonical_hash(identity),
                ))
        except (ExecutionBindingError, OnboardingError, PlanningMemoryError) as exc:
            return self._runtime_reject(module, {"reason_code": str(exc)})
        if not isinstance(result, Mapping):
            return {"module": module, "decision": "unknown", "stored": False, "count": 0,
                    "message": "没有取得可确认的存入回执；请先核查，不要自动重试。"}
        if result.get("decision") == "reject":
            return self._runtime_reject(module, result)
        if result.get("decision") != "stored":
            return {"module": module, "decision": "not_confirmed_stored", "stored": False, "count": 0,
                    "state_changed": result.get("state_changed") is True,
                    "message": "未确认有效记忆已存入；候选提交不等于正式存入，请先核查回执。"}
        if module == "emotional_memory":
            record = result.get("memory")
            record = record if isinstance(record, Mapping) else {}
            item_id, item_version = record.get("memory_id"), record.get("current_version")
            item_ref = f"emotion://{item_id}@{item_version}"
        elif module == "learning_memory":
            item_id, item_ref, item_version = result.get("learning_id"), result.get("item_ref"), result.get("item_version")
        else:
            item_id, item_ref, item_version = result.get("plan_id"), result.get("plan_ref"), result.get("plan_version")
        prefix, scheme = {"emotional_memory": ("emmem", "emotion"), "learning_memory": ("learn", "learning"),
                          "planning_memory": ("plan", "plan")}[module]
        if (not isinstance(item_id, str) or re.fullmatch(prefix + r"_[0-9a-f]{32}", item_id) is None
                or type(item_version) is not int or item_version < 1 or item_ref != f"{scheme}://{item_id}@{item_version}"):
            return {"module": module, "decision": "not_confirmed_stored", "stored": False, "count": 0,
                    "state_changed": result.get("state_changed") is True,
                    "message": "存入回执不完整；可能已有写入，请先核查，不要自动重试。"}
        return {"module": module, "decision": "stored", "stored": True, "count": 1,
                "id": item_id, "ref": item_ref, "version": item_version,
                "state_changed": result.get("state_changed") is True,
                "idempotent_replay": result.get("idempotent_replay") is True,
                "message": "已存入 1 条日常记忆；未执行核心自我修改或候选审核。"}
