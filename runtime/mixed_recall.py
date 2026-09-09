"""Budget already-gated projections together, without reading or changing memory.

Relevance numbers are deterministic ranking hints, not calibrated probabilities.
Identity/governance and AI-selected persistent reminders are separate from the
ordinary relevance competition. No text, authority, gate or source is invented.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from typing import Any, Callable, Mapping


_LEARNING_DETAIL_RULE = (
    "有版本化 item_ref 时复制到 target_ref 精确读取；"
    "语义 query 的零结果不能否定该 ref。"
)


def _learning_projection(item: Mapping[str, Any]) -> dict[str, Any]:
    """Compact only known host-authored lookup duplication in automatic recall.

    Explicit recall APIs keep their original compatibility aliases. Unknown
    rules and all content, provenance, disclosure and authority fields survive.
    """
    projected = deepcopy(dict(item))
    lookup = projected.get("detail_lookup")
    if isinstance(lookup, dict) and lookup.get("rule") == _LEARNING_DETAIL_RULE:
        del lookup["rule"]
        arguments = lookup.get("arguments")
        exact = arguments.get("target_ref") if isinstance(arguments, Mapping) else None
        if isinstance(exact, str) and exact == item.get("item_ref"):
            for alias in ("ref", "target_ref"):
                if lookup.get(alias) == exact:
                    del lookup[alias]
    return projected


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _stable_key(item: Mapping[str, Any]) -> str:
    # Content hash breaks equal-score ties without permanently privileging a
    # module or relying on caller iteration order. It never enters the prompt.
    raw = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _compact_persistent(item: Mapping[str, Any]) -> dict[str, Any]:
    # Exact, already-stored <=50-character author reminder; never a generated
    # summary and never a substring of an arbitrary/private body.
    return {
        "item_ref": item["item_ref"],
        "kind": item["kind"],
        "presence_mode": "persistent",
        "state": "active",
        "presentation": "author_reminder",
        "content": {"reminder": item["reminder"]},
        "detail_lookup": deepcopy(item["detail_lookup"]),
        "frame": deepcopy(item["frame"]),
    }


def select_mixed_recall(
    *,
    base: Mapping[str, Any],
    planning: Mapping[str, Any] | None,
    learning: list[dict[str, Any]],
    tool: list[dict[str, Any]],
    emotional: Mapping[str, Any] | None,
    estimate_tokens: Callable[[Any], int],
    budget: int = 1200,
) -> dict[str, Any]:
    """Select only supplied, disclosure-gated entries under the exact shared cap.

    Callers must pass only enabled scopes and already-filtered candidates. This
    pure function has no database, tool invocation or permission-changing path.
    It deliberately does not weaken a presentation in order to fit more prose.
    """
    dynamic = deepcopy(dict(base))
    if estimate_tokens(dynamic) > budget:
        raise ValueError("mixed_recall_base_over_budget")
    planning = planning or {}
    emotional = emotional or {}
    plan_items = list(planning.get("envelopes", []))
    selected_plans: list[dict[str, Any]] = []
    selected_learning: list[dict[str, Any]] = []
    selected_tools: list[dict[str, Any]] = []
    selected_emotion: dict[str, list[dict[str, Any]]] = {
        "pins": [], "memories": [], "ephemeral": [],
    }
    tool_tokens = 0

    def plan_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {"contract": planning["contract"], "frame": deepcopy(planning["frame"]),
                "envelopes": items}

    def emotion_payload(key: str, item: dict[str, Any]) -> dict[str, Any]:
        return {"contract": emotional["contract"], "frame": deepcopy(emotional["frame"]),
                **{name: [*values, item] if name == key else list(values)
                   for name, values in selected_emotion.items()}}

    def accept(module: str, payload: dict[str, Any]) -> bool:
        nonlocal dynamic
        proposed = {**dynamic, module: payload}
        if estimate_tokens(proposed) > budget:
            return False
        dynamic = proposed
        return True

    persistent = [item for item in plan_items
                  if item.get("selection_priority") == 0
                  and item.get("presence_mode") == "persistent"
                  and item.get("state") == "active"
                  and isinstance(item.get("reminder"), str)
                  and 0 < len(item["reminder"]) <= 50][:2]
    # Reserve both short author reminders as one atomic budget decision. Adding
    # a full first envelope must not crowd out the second persistent slot.
    if persistent:
        compact = [_compact_persistent(item) for item in persistent]
        if not accept("planning_memory", plan_payload(compact)):
            # Legitimate base is bounded to 360 governance tokens. Failure here
            # means the invariant/config changed; do not silently claim delivery.
            raise ValueError("persistent_reminder_budget_unavailable")
        selected_plans = compact

    # Preserve approved identity anchors and trusted same-thread short-term
    # continuity as context, not fabricated topic-relevance competitors.
    for key in ("pins", "ephemeral"):
        for item in emotional.get(key, []):
            if accept("emotional_memory", emotion_payload(key, item)):
                selected_emotion[key].append(item)

    reserved_refs = {item["item_ref"] for item in persistent}
    ranked: list[tuple[float, str, str, dict[str, Any]]] = []
    for item in plan_items:
        if item.get("item_ref") not in reserved_refs:
            # Time urgency is a legitimate planning relevance signal, not a
            # universal planning-first allocation order.
            relevance = max(_number(item.get("semantic_score")),
                            0.75 * _number(item.get("temporal_score")))
            ranked.append((-relevance, _stable_key(item), "planning_memory", item))
    for module, items in (("learning_memory", learning), ("tool_guidance", tool)):
        for item in items:
            ranked.append((-_number(item.get("semantic_score")),
                           _stable_key(item), module, item))
    for item in emotional.get("memories", []):
        ranked.append((-_number(item.get("score")),
                       _stable_key(item), "emotional_memory", item))
    # Modules do not expose equivalent secondary salience fields. Using a
    # missing-field zero only for emotion would create a module-specific tie
    # penalty; use the same content-key tie rule for every ordinary candidate.
    ranked.sort(key=lambda entry: entry[:2])
    for _, _, module, item in ranked:
        if module == "planning_memory":
            if len(selected_plans) < 3 and accept(module, plan_payload([*selected_plans, item])):
                selected_plans.append(item)
        elif module == "emotional_memory":
            if accept(module, emotion_payload("memories", item)):
                selected_emotion["memories"].append(item)
        else:
            selected = selected_learning if module == "learning_memory" else selected_tools
            cost = item.get("token_cost")
            if type(cost) is not int or cost < 0:
                cost = estimate_tokens(item)
            if len(selected) >= (3 if module == "learning_memory" else 2):
                continue
            if module == "tool_guidance" and tool_tokens + cost > 240:
                continue
            frame = {"instruction_authority": "none", "permission_authority": "none", "optional": True}
            if module == "tool_guidance":
                frame["execution_authority"] = "none"
            projected_item = _learning_projection(item) if module == "learning_memory" else item
            payload = {"contract": "learning-recall/0.2" if module == "learning_memory" else "tool-guidance-recall/0.2",
                       "frame": frame, "envelopes": [*selected, projected_item]}
            if module == "learning_memory" and (
                dynamic.get(module, {}).get("detail_lookup_rule") == _LEARNING_DETAIL_RULE
                or item.get("detail_lookup", {}).get("rule") == _LEARNING_DETAIL_RULE
            ):
                payload["detail_lookup_rule"] = _LEARNING_DETAIL_RULE
            if accept(module, payload):
                selected.append(projected_item)
                if module == "tool_guidance":
                    tool_tokens += cost

    # Spend remaining space only after all ordinary candidates have competed.
    # Upgrading a reminder preserves its author text; it never adds authority.
    for original in persistent:
        proposed = [original if item["item_ref"] == original["item_ref"] else item
                    for item in selected_plans]
        if accept("planning_memory", plan_payload(proposed)):
            selected_plans = proposed
    if selected_plans:
        refs = {item["item_ref"] for item in selected_plans}
        payload = dict(dynamic["planning_memory"])
        for key in ("next_action", "review_hint", "coordination_hint"):
            hint = planning.get(key)
            if not isinstance(hint, Mapping):
                continue
            if key == "coordination_hint":
                if len(selected_plans) != len(plan_items) or len(selected_plans) < 3:
                    continue
            elif hint.get("plan_ref") not in refs:
                continue
            proposed = {**payload, key: deepcopy(hint)}
            if accept("planning_memory", proposed):
                payload = proposed
    # Reuse (do not reword) the existing optional reminder only if some genuine
    # emotional context survived. It cannot displace relevant memories.
    if "emotional_memory" in dynamic and emotional.get("reminder"):
        accept("emotional_memory", {**dynamic["emotional_memory"], "reminder": emotional["reminder"]})
    elif emotional.get("reminder"):
        # The module's reminder now contains only a pending-pin count notice.
        # Preserve it even when every recalled item was too large for the budget,
        # without manufacturing a surfaced memory or candidate review evidence.
        accept("emotional_memory", {"contract": emotional["contract"],
               "frame": deepcopy(emotional["frame"]), "pins": [], "memories": [],
               "ephemeral": [], "reminder": emotional["reminder"]})
    return dynamic
