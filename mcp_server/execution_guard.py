"""Validate native MCP arguments before defaults or a business tool can run."""
from __future__ import annotations

from typing import Any

from runtime.execution_binding import (
    EXECUTION_CONTRACT, EXECUTION_TOOLS, ExecutionBindingError, ExecutionStore,
)
from .daily_revision_service import parse_revision_target

READ_ONLY_TOOLS = frozenset({
    "query_self_model", "recall_emotional_memory", "recall_learning_memory",
    "preview_learning_recall", "recall_tool_guidance", "query_self_governance_profile",
    "query_injection_control", "recall_planning_memory", "open_hallucination_vault",
})
_DIRECT_SCOPES = {
    **dict.fromkeys(("submit_self_model_candidate", "activate_self_model_candidate"), "self_revision"),
    **dict.fromkeys(("preview_person_reference_rewrite", "confirm_person_reference_rewrite"), "shared_person_authoring"),
    **dict.fromkeys(("remember_emotional_memory", "revise_emotional_memory", "integrate_emotional_memories", "manage_brain_pin", "veto_ephemeral_memory"), "emotional_memory"),
    **dict.fromkeys(("remember_learning_memory", "remember_learning_contrast_pair", "revise_learning_memory", "integrate_learning_memories", "review_learning_change"), "learning_memory"),
    **dict.fromkeys(("remember_tool_guidance", "revise_tool_guidance", "review_tool_guidance_candidate", "record_tool_experience"), "tool_guidance"),
    **dict.fromkeys(("remember_planning_memory", "record_planning_event", "revise_planning_memory", "review_planning_change"), "planning_memory"),
    "manage_self_governance_profile": "self_governance",
    "manage_injection_control": "injection_control",
    **dict.fromkeys(("open_hallucination_vault", "hold_hallucination_record", "transfer_hallucination_record", "review_hallucination_restore"), "hallucination_vault"),
}


def install_execution_guard(mcp: Any, *, store: ExecutionStore, onboarding: Any,
                            owner_id: str, model_id: str, required: bool = True) -> None:
    manager = mcp._tool_manager
    original = manager.call_tool
    if getattr(manager, "_st_execution_guard", False):
        raise RuntimeError("execution guard already installed")
    manager._st_execution_guard = True
    for name in EXECUTION_TOOLS:
        tool = manager.get_tool(name)
        if tool is None:
            continue
        tool.parameters.setdefault("properties", {})["execution_ref"] = {
            "type": "string", "pattern": "^stexec_[A-Za-z0-9_-]{43}$",
            "description": "Reserved host binding. The gateway fills this field; do not invent or copy it.",
            "x-stbrain-execution-tool": name,
            "x-stbrain-execution-contract": EXECUTION_CONTRACT,
        }

    def reject(tool: Any, code: str, convert: bool) -> Any:
        result = {"decision": "reject", "reason_code": code, "reason_codes": [code],
                  "state_changed": False,
                  "message": "本次工具未执行：调用没有有效的本轮绑定，或已结束。不要重放旧调用。",
                  "next_action": "从新消息发起一次新调用；持续失败请检查网关连接。"}
        return tool.fn_metadata.convert_result(result) if convert else result

    def is_direct(name: str, args: dict[str, Any]) -> bool:
        ref = args.get("write_context_ref")
        scope = args.get("module") if name == "remember_memory" else _DIRECT_SCOPES.get(name)
        if name in {"revise_memory", "advance_plan"}:
            try:
                scope, _, _ = parse_revision_target(args.get("target_ref"))
            except ValueError:
                return False
            if name == "advance_plan" and scope != "planning_memory":
                return False
        if not isinstance(ref, str) or not ref or not isinstance(scope, str):
            return False
        if name == "remember_memory" and scope not in {"emotional_memory", "learning_memory", "planning_memory"}:
            return False
        binding = onboarding.current_open_write_context(
            owner_id=owner_id, model_id=model_id, write_context_ref=ref, required_scope=scope,
        )
        return (binding.get("write_context_available") is True
                and binding.get("context_mode") == "human_attested_direct")

    async def guarded(name: str, arguments: dict[str, Any], context: Any = None,
                      convert_result: bool = False) -> Any:
        tool = manager.get_tool(name)
        if tool is None or name not in EXECUTION_TOOLS:
            return await original(name, arguments, context=context, convert_result=convert_result)
        args = dict(arguments)
        ref = args.pop("execution_ref", None)
        if ref is None:
            if not required or (name in READ_ONLY_TOOLS and not args.get("write_context_ref")) or is_direct(name, args):
                return await original(name, args, context=context, convert_result=convert_result)
            return reject(tool, "execution_binding_required", convert_result)
        try:
            claim = store.claim(execution_ref=ref, owner_id=owner_id, model_id=model_id,
                                tool_name=name, arguments=args)
        except ExecutionBindingError:
            return reject(tool, "execution_binding_invalid_or_finished", convert_result)
        failed = True
        try:
            with store.bind(claim):
                result = await original(name, args, context=context, convert_result=False)
                failed = isinstance(result, dict) and result.get("decision") == "reject"
                return tool.fn_metadata.convert_result(result) if convert_result else result
        finally:
            store.finish(claim, failed=failed)

    manager.call_tool = guarded
