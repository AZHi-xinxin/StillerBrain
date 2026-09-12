"""Supply only mechanical context/CAS fields after a real host execution claim.

No target version, author text, confirmation, evidence or candidate hash is
invented. Explicit old-client fields are checked, never replaced or retried.
"""
from __future__ import annotations

from typing import Any, Mapping

from runtime.execution_binding import ExecutionBindingError, ExecutionClaim
from runtime.ordinary_access import current_ordinary_access


BOUND_REVISION_TOOLS = {
    **dict.fromkeys(("revise_emotional_memory", "integrate_emotional_memories"),
                    ("emotional_memory", "expected_emotion_version")),
    **dict.fromkeys(("revise_learning_memory", "integrate_learning_memories", "review_learning_change"),
                    ("learning_memory", "expected_learning_version")),
    **dict.fromkeys(("remember_tool_guidance", "revise_tool_guidance", "review_tool_guidance_candidate"),
                    ("tool_guidance", "expected_tool_row_version")),
    **dict.fromkeys(("remember_planning_memory", "revise_planning_memory", "review_planning_change"),
                    ("planning_memory", "expected_planning_version")),
    "manage_self_governance_profile": ("self_governance", None),
    "manage_injection_control": ("injection_control", None),
}


def bind_revision_arguments(name: str, arguments: Mapping[str, Any], *, claim: ExecutionClaim,
                            onboarding: Any, services: Mapping[str, Any]) -> dict[str, Any]:
    args = dict(arguments)
    if name not in BOUND_REVISION_TOOLS:
        return args
    scope, version_field = BOUND_REVISION_TOOLS[name]
    if claim.tool_name != name:
        raise ExecutionBindingError("execution_tool_mismatch")
    common = {"owner_id": claim.owner_id, "model_id": claim.model_id,
              "expected_wake_id": claim.wake_id}
    ref = args.get("write_context_ref")
    ordinary = current_ordinary_access(owner_id=claim.owner_id, model_id=claim.model_id, scope=scope)
    if ref is None:
        opened = ordinary if ordinary is not None else onboarding.open_brain_context(**common, present_details=False)
        if ordinary is None and opened.get("write_context_available") is not True:
            raise ExecutionBindingError("current_injected_wake_required")
        ref = opened.get("write_context_ref")
    if not isinstance(ref, str) or not ref or ref.startswith("$"):
        raise ExecutionBindingError("brain_open_required")
    binding = onboarding.current_open_write_context(**common, write_context_ref=ref, required_scope=scope)
    if (binding.get("write_context_available") is not True
            or binding.get("context_mode") not in {"gateway_injected", "ordinary_authenticated"}
            or binding.get("wake_id") != claim.wake_id):
        raise ExecutionBindingError("execution_wake_mismatch")
    args["write_context_ref"] = ref
    if version_field is not None and args.get(version_field) is None:
        component = services.get(scope)
        if (component is None or component.owner_id != claim.owner_id
                or component.model_id != claim.model_id):
            raise ExecutionBindingError("execution_owner_mismatch")
        version = component.status().get("row_version")
        if type(version) is not int or version < 0:
            raise ExecutionBindingError("module_version_unavailable")
        args[version_field] = version
    return args
