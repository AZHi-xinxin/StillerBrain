"""Fixed ST-only action routing shared by host leases and MCP dispatch.

Discovery is a presentation choice, not a new authorization authority. The
resolved existing operation still passes every existing execution/access guard.
No arbitrary tool, import, URL or external executor can be selected here.
"""
from __future__ import annotations

from typing import Any, Mapping

MANAGE_TOOL = "stbrain_manage"
DISCOVERY_TOOL = "stbrain_tools"
CATEGORIES = {
    "system": ("状态与手册", ("stbrain_health", "stbrain_help", "stbrain_open", "stbrain_open_direct")),
    "memory": ("通用存入、查询与修改", ("remember_memory", "revise_memory", "advance_plan")),
    "self": ("模块一自我定义", ("authorize_self_model", "query_self_model", "submit_self_model_candidate", "activate_self_model_candidate")),
    "person": ("人称、存前提醒与改写", ("manage_person_reference_advisory", "preview_person_reference_rewrite", "confirm_person_reference_rewrite")),
    "emotion": ("情感记忆与常驻", ("remember_emotional_memory", "recall_emotional_memory", "revise_emotional_memory", "integrate_emotional_memories", "manage_brain_pin", "veto_ephemeral_memory")),
    "learning": ("学习、对照与归纳", ("remember_learning_memory", "remember_learning_contrast_pair", "recall_learning_memory", "revise_learning_memory", "integrate_learning_memories", "review_learning_change", "preview_learning_recall")),
    "tools": ("工具卡与调用经验", ("remember_tool_guidance", "recall_tool_guidance", "revise_tool_guidance", "review_tool_guidance_candidate", "record_tool_experience")),
    "planning": ("计划与进展", ("remember_planning_memory", "recall_planning_memory", "record_planning_event", "revise_planning_memory", "review_planning_change")),
    "diy": ("自写提示词、安全阀与浮现开关", ("manage_self_governance_profile", "query_self_governance_profile", "manage_injection_control", "query_injection_control")),
    "vault": ("幻觉黑匣子", ("hold_hallucination_record", "open_hallucination_vault", "transfer_hallucination_record", "review_hallucination_restore")),
}
ACTION_CATEGORIES = {name: key for key, (_, names) in CATEGORIES.items() for name in names}
if len(ACTION_CATEGORIES) != sum(len(names) for _, names in CATEGORIES.values()):
    raise RuntimeError("compact_action_mapping_duplicate")


class CompactRouteError(ValueError):
    """Only fixed diagnostics; never reflect model arguments or credentials."""


def resolve_compact_action(envelope: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(envelope, Mapping) or set(envelope) - {"action", "arguments"}:
        raise CompactRouteError("compact_envelope_invalid")
    action = envelope.get("action")
    arguments = envelope.get("arguments", {})
    if not isinstance(action, str) or action not in ACTION_CATEGORIES:
        raise CompactRouteError("compact_action_unknown")
    if not isinstance(arguments, dict) or any(not isinstance(key, str) for key in arguments):
        raise CompactRouteError("compact_arguments_object_required")
    if "execution_ref" in arguments:
        raise CompactRouteError("compact_nested_execution_ref_forbidden")
    return action, dict(arguments)
