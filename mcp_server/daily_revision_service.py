"""One-call, exact-version ordinary revisions; not an advanced review bypass.

The model supplies the version it actually read. Only the module's mechanical
CAS is obtained by the host. No latest-item lookup, automatic retry, fabricated
verification/adoption, direct-grant open, or core-self review is performed.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from runtime.execution_binding import ExecutionBindingError, ExecutionClaim, current_execution_claim
from runtime.onboarding import OnboardingError
from runtime.emotional_memory import EmotionalMemoryError
from runtime.learning_memory import LearningMemoryError
from runtime.planning_memory import PlanningMemoryError


ORDINARY_REVISION_FIELDS = {
    "emotional_memory": frozenset({"summary", "keywords", "entities", "importance"}),
    "learning_memory": frozenset({"title", "summary", "domain", "keywords", "entities", "importance"}),
    "planning_memory": frozenset({"title", "summary", "keywords", "importance"}),
}
_TARGETS = {"emotion": ("emotional_memory", "emmem"),
            "learning": ("learning_memory", "learn"), "plan": ("planning_memory", "plan")}
_ADVANCED = {"emotional_memory": "revise_emotional_memory", "learning_memory": "revise_learning_memory",
             "planning_memory": "revise_planning_memory"}
_NEUTRAL_REASON = "普通摘要或检索元数据修订；保留目标历史，不代表独立核验或高级修订审核。"
_SAFE_FAILURES = frozenset({
    "module_one_required", "credential_or_secret_detected", "credential_content_rejected",
    "brain_open_required", "current_injected_wake_required", "current_wake_required",
    "write_context_expired", "injected_context_required", "write_context_binding_mismatch",
    "write_context_not_opened_or_mismatched", "direct_scope_not_authorized",
    "direct_grant_expired", "direct_grant_invalid", "execution_wake_mismatch",
    "execution_owner_mismatch", "execution_claim_not_current", "execution_registry_unavailable",
    "planning_row_version_conflict", "emotion_row_version_conflict", "learning_row_version_conflict",
    "memory_version_conflict", "learning_item_version_conflict", "plan_version_conflict",
    "expected_plan_version_conflict", "stale_plan_ref", "memory_not_found", "learning_item_not_found",
    "plan_not_found", "ordinary_revision_requires_advanced", "no_effective_change",
    "referent_binding_occurrence_missing", "referent_binding_field_missing",
    "ordinary_revision_referent_change_requires_advanced",
    "pending_plan_revision_conflict", "planning_candidate_conflict", "active_plan_revision_conflict",
    "plan_event_seq_conflict", "invalid_expected_event_seq", "invalid_event_type", "invalid_evidence",
    "event_evidence_required", "evidence_required", "invalid_plan_transition", "plan_not_active",
    "invalid_plan_state_transition", "plan_quarantined", "vision_cannot_complete", "active_children_remaining",
    "invalid_evidence_anchor", "invalid_evidence_source_kind", "invalid_evidence_provenance",
    "invalid_evidence_source_ref", "invalid_evidence_summary", "plan_content_too_long",
    "plan_change_candidate_pending", "plan_not_mutable", "plan_state_conflict",
}) | frozenset(f"{field}_{suffix}" for field in ("title", "summary", "domain", "keywords", "entities", "importance", "reason")
              for suffix in ("required", "too_long", "invalid"))


def parse_revision_target(target_ref: Any) -> tuple[str, str, int]:
    """Pure strict parser, also used to select the existing direct-context scope."""
    if not isinstance(target_ref, str) or len(target_ref) > 100:
        raise ValueError("versioned_target_ref_required")
    match = re.fullmatch(r"(emotion|learning|plan)://([a-z]+_[0-9a-f]{32})@([1-9][0-9]{0,14})", target_ref)
    if match is None:
        raise ValueError("versioned_target_ref_required")
    scheme, item_id, version = match.groups()
    module, prefix = _TARGETS[scheme]
    if not item_id.startswith(prefix + "_"):
        raise ValueError("versioned_target_ref_required")
    return module, item_id, int(version)


class DailyRevisionAccessService:
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
            if "version_conflict" in code or code == "stale_plan_ref":
                guidance = "目标或模块版本已变化；先读回目标并决定是否仍需修改，再使用读到的精确版本；不会自动换成最新版本重试。"
            elif code == "ordinary_revision_requires_advanced":
                guidance = "此改动超出普通小改；请使用该模块原有高级修订流程，不会自动提交候选。"
            elif code == "no_effective_change":
                guidance = "修改值与该版本相同，无需创建新版本。"
            else:
                guidance = "未修改目标；请核对目标版本、字段或当前回合绑定后再决定是否重试。"
        return {"module": module, "decision": "reject", "revised": False, "count": 0,
                "state_changed": False, "reason_codes": [code], "next_step": guidance, **details}

    def _runtime_reject(self, module: str, result: Mapping[str, Any]) -> dict[str, Any]:
        reasons = [result.get("binding_reason_code"), result.get("reason_code")]
        if isinstance(result.get("reason_codes"), list):
            reasons.extend(result["reason_codes"])
        code = next((value for value in reasons if isinstance(value, str) and value in _SAFE_FAILURES), "revision_rejected")
        details = {"advanced_tool": _ADVANCED[module]} if code == "ordinary_revision_requires_advanced" else {}
        return self._reject(module, code, **details)

    def revise(self, target_ref: str, changes: Mapping[str, Any], reason: str | None = None,
               write_context_ref: str | None = None) -> dict[str, Any]:
        return self._operate(target_ref, changes, reason, write_context_ref)

    def advance(self, target_ref: str, expected_event_seq: int, event_type: str, note: str,
                evidence: list[dict[str, Any]] | None = None,
                write_context_ref: str | None = None) -> dict[str, Any]:
        if type(expected_event_seq) is not int or expected_event_seq < 0:
            return self._reject("planning_memory", "invalid_expected_event_seq",
                                "填写你已经读到的计划 event_seq，不能自动取最新值。", field="expected_event_seq")
        if not isinstance(event_type, str) or event_type not in {"progress", "complete", "pause", "resume", "reopen"}:
            return self._reject("planning_memory", "invalid_event_type", field="event_type")
        if not isinstance(note, str) or not note.strip() or len(note) > 2000:
            return self._reject("planning_memory", "note_invalid", field="note")
        if evidence is not None and not isinstance(evidence, list):
            return self._reject("planning_memory", "invalid_evidence", field="evidence")
        if event_type in {"progress", "complete", "reopen"} and not evidence:
            return self._reject("planning_memory", "event_evidence_required",
                                "此进度事件需要实际依据：source_kind、source_ref、evidence_summary、provenance；没有证据时不要编造完成。",
                                field="evidence")
        return self._operate(target_ref, {}, note, write_context_ref,
                             event={"expected_event_seq": expected_event_seq, "event_type": event_type,
                                    "evidence": [] if evidence is None else evidence})

    def _operate(self, target_ref: str, changes: Mapping[str, Any], reason: str | None,
                 write_context_ref: str | None, *, event: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            module, item_id, expected_version = parse_revision_target(target_ref)
        except ValueError:
            return self._reject("unknown", "versioned_target_ref_required",
                                "提供你已经读到的完整版本引用，例如 emotion://…@1；不能省略版本或填写 latest。", field="target_ref")
        component = self.services[module]
        if component is None:
            return self._reject(module, "module_unavailable")
        if event is not None and module != "planning_memory":
            return self._reject(module, "planning_target_required", field="target_ref")
        if event is None and (not isinstance(changes, Mapping) or not changes):
            return self._reject(module, "changes_required", field="changes")
        if set(changes) - ORDINARY_REVISION_FIELDS[module]:
            return self._reject(module, "ordinary_revision_requires_advanced", advanced_tool=_ADVANCED[module],
                                allowed_fields=sorted(ORDINARY_REVISION_FIELDS[module]))
        if reason is not None and (not isinstance(reason, str) or not reason.strip() or len(reason) > 2000):
            return self._reject(module, "reason_invalid", field="reason")
        claim = current_execution_claim()
        if claim is not None:
            if (not isinstance(claim, ExecutionClaim) or claim.owner_id != self.owner_id
                    or claim.model_id != self.model_id
                    or claim.tool_name != ("advance_plan" if event is not None else "revise_memory")):
                return self._reject(module, "execution_owner_mismatch")
            if write_context_ref is not None:
                return self._reject(module, "explicit_context_conflicts_with_bound_execution")
        elif not isinstance(write_context_ref, str) or not write_context_ref.strip():
            return self._reject(module, "execution_binding_required")
        elif write_context_ref.strip().startswith("$"):
            return self._reject(module, "invalid_direct_context")

        request = {"target_ref": target_ref, "changes": dict(changes), "reason": reason}
        if event is not None:
            request["event"] = dict(event)
        try:
            if claim is not None:
                opened = self.onboarding.open_brain_context(
                    owner_id=self.owner_id, model_id=self.model_id, present_details=False,
                    expected_wake_id=claim.wake_id,
                )
                if not isinstance(opened, Mapping) or opened.get("write_context_available") is not True:
                    return self._runtime_reject(module, opened if isinstance(opened, Mapping) else {})
                ref = opened.get("write_context_ref")
            else:
                ref = write_context_ref.strip()
            if not isinstance(ref, str) or not ref or ref.startswith("$"):
                return self._reject(module, "brain_open_required")
            binding = self.onboarding.current_open_write_context(
                owner_id=self.owner_id, model_id=self.model_id, write_context_ref=ref,
                required_scope=module, expected_wake_id=claim.wake_id if claim is not None else None,
            )
            if not isinstance(binding, Mapping) or binding.get("write_context_available") is not True:
                return self._runtime_reject(module, binding if isinstance(binding, Mapping) else {})
            if claim is not None:
                if binding.get("context_mode") != "gateway_injected" or binding.get("wake_id") != claim.wake_id:
                    return self._reject(module, "execution_wake_mismatch")
            else:
                if binding.get("context_mode") != "human_attested_direct":
                    return self._reject(module, "invalid_direct_context")
                scopes = binding.get("authorized_scopes")
                if not isinstance(scopes, list) or module not in scopes:
                    return self._reject(module, "direct_scope_not_authorized")
            if not isinstance(binding.get("wake_id"), str) or not binding["wake_id"] or type(binding.get("wake_seq")) is not int:
                return self._reject(module, "brain_open_required")
            # This is module-level CAS only. Never read the target to select a new version.
            row_version = component.status().get("row_version")
            if type(row_version) is not int or row_version < 0:
                return self._reject(module, "module_version_unavailable")

            def apply(verified: Mapping[str, Any]) -> dict[str, Any]:
                # Re-check the callback's exact binding, not just the earlier observation.
                if (verified.get("wake_id"), verified.get("wake_seq"), verified.get("context_mode")) != (
                        binding["wake_id"], binding["wake_seq"], binding["context_mode"]):
                    raise ExecutionBindingError("execution_wake_mismatch")
                fields = dict(owner_id=self.owner_id, model_id=self.model_id,
                              wake_id=verified["wake_id"], expected_row_version=row_version,
                              changes=dict(changes), reason=_NEUTRAL_REASON if reason is None else reason)
                if event is not None:
                    del fields["changes"]
                    fields.update(plan_id=item_id, expected_plan_version=expected_version,
                                  wake_seq=verified["wake_seq"], **event)
                    return component.store.record_ordinary_event(**fields)
                if module == "emotional_memory":
                    fields.update(memory_id=item_id, expected_memory_version=expected_version)
                elif module == "learning_memory":
                    fields.update(learning_id=item_id, expected_item_version=expected_version, wake_seq=verified["wake_seq"])
                else:
                    fields.update(plan_id=item_id, expected_plan_version=expected_version, wake_seq=verified["wake_seq"])
                return component.store.revise_ordinary(**fields)

            result = component._write(ref, request, apply)
        except (ExecutionBindingError, OnboardingError, EmotionalMemoryError, LearningMemoryError, PlanningMemoryError) as exc:
            return self._runtime_reject(module, {"reason_code": str(exc)})
        if isinstance(result, Mapping) and result.get("decision") == "reject":
            return self._runtime_reject(module, result)
        if event is not None:
            if (not isinstance(result, Mapping) or result.get("decision") != "event_recorded"
                    or result.get("id") != item_id or result.get("ref") != target_ref
                    or type(result.get("version")) is not int or result["version"] != expected_version
                    or type(result.get("event_seq")) is not int
                    or result["event_seq"] <= event["expected_event_seq"]
                    or type(result.get("previous_event_seq")) is not int
                    or result["previous_event_seq"] != event["expected_event_seq"]
                    or not isinstance(result.get("event_id"), str)
                    or re.fullmatch(r"planevt_[0-9a-f]{32}", result["event_id"]) is None
                    or not isinstance(result.get("state"), str)
                    or result["state"] not in {"active", "paused", "completed"}
                    or result.get("state_changed") is not True):
                return {"module": module, "decision": "not_confirmed_event", "event_recorded": False,
                        "message": "事件回执不完整，可能已经写入；请先读回计划状态，不要自动重试。"}
            return {"module": module, "decision": "event_recorded", "event_recorded": True,
                    "count": 1, "id": item_id, "ref": target_ref, "version": expected_version,
                    "event_id": result["event_id"], "event_seq": result["event_seq"],
                    "previous_event_seq": event["expected_event_seq"], "state": result["state"],
                    "state_changed": True, "message": "已追加 1 条计划事件；依据来自本次提供的说明，未执行独立核验。"}
        expected_ref = target_ref.rsplit("@", 1)[0] + "@" + str(expected_version + 1)
        if (not isinstance(result, Mapping) or result.get("decision") != "revised"
                or result.get("id") != item_id or result.get("ref") != expected_ref
                or type(result.get("version")) is not int or result["version"] != expected_version + 1
                or type(result.get("previous_version")) is not int or result["previous_version"] != expected_version
                or result.get("state_changed") is not True):
            return {"module": module, "decision": "not_confirmed_revised", "revised": False, "count": 0,
                    "message": "修订回执不完整，可能已经写入；请先核对目标，不要自动重试。"}
        return {"module": module, "decision": "revised", "revised": True, "count": 1,
                "id": item_id, "ref": expected_ref, "version": expected_version + 1,
                "previous_ref": target_ref, "state_changed": True,
                "message": "已新增 1 个普通修订版本；保留历史，未执行高级修订或独立核验。"}
