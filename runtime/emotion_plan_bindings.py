"""AI-authored emotion -> plan links, expanded without a reminder scheduler."""
from __future__ import annotations

import json
import re

PLAN_REF = re.compile(r"plan://(plan_[0-9a-f]{32})@([1-9][0-9]*)\Z")


def normalize_bound_plan_refs(value):
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(ref, str) or not PLAN_REF.fullmatch(ref) for ref in value
    ):
        raise ValueError("invalid_bound_plan_refs")
    # Versions identify what the author read; recall follows that plan's current
    # version. Repeating the same identity does not duplicate its attached body.
    by_id = {}
    for ref in value:
        by_id[PLAN_REF.fullmatch(ref)[1]] = ref
    return list(by_id.values())


def append_bound_plans(connection, *, owner_id, model_id, dynamic, planning_store):
    """Expand only emotions actually present, outside ordinary recall's budget.

    No frequency, progress, plan-state, count or token filter. Plan state is
    context for the AI, never a command. Existing identity/lifecycle boundaries
    still apply; links do not resurrect deleted or quarantined data.
    """
    emotion_ids = {
        item["memory_id"] for item in dynamic.get("emotional_memory", {}).get("memories", [])
        if isinstance(item.get("memory_id"), str)
    }
    for item in [*dynamic.get("force_recall", {}).get("items", []),
                 *dynamic.get("emotion_layer", {}).get("memories", []),
                 *[item for item in dynamic.get("associated_recall", {}).get("items", []) if item.get("presentation") != "neutral_hint"],
                 *dynamic.get("session_continuity", {}).get("recent_memories", [])]:
        ref = item.get("item_ref") or item.get("memory_ref", "")
        if isinstance(ref, str) and ref.startswith("emotion://"):
            emotion_ids.add(ref.split("://", 1)[1].split("@", 1)[0])
    if not emotion_ids:
        return dynamic
    plans = {}
    for emotion_id in sorted(emotion_ids):
        emotion = connection.execute(
            "SELECT current_version,bound_plan_refs_json FROM emotion_memories "
            "WHERE owner_id=? AND model_id=? AND memory_id=? AND lifecycle='active'",
            (owner_id, model_id, emotion_id),
        ).fetchone()
        if emotion is None:
            continue
        source_ref = f"emotion://{emotion_id}@{emotion['current_version']}"
        for ref in json.loads(emotion["bound_plan_refs_json"]):
            plan_id = PLAN_REF.fullmatch(ref)[1]
            if plan_id not in plans:
                row = connection.execute(
                    "SELECT * FROM planning_items WHERE owner_id=? AND model_id=? "
                    "AND plan_id=? AND recall_lifecycle='active'",
                    (owner_id, model_id, plan_id),
                ).fetchone()
                if row is None:
                    continue
                current = planning_store._public_plan(
                    connection, owner_id=owner_id, model_id=model_id, item=row,
                )
                plans[plan_id] = {"item_ref": current["plan_ref"],
                    "content": current["content"], "state": current["state_projection"],
                    "source_emotion_refs": []}
            if source_ref not in plans[plan_id]["source_emotion_refs"]:
                plans[plan_id]["source_emotion_refs"].append(source_ref)
    if not plans:
        return dynamic
    return {**dynamic, "emotion_bound_plans": {
        "items": list(plans.values()),
        "instruction_authority": "none", "permission_authority": "none",
        "notice": "这些计划由 AI 绑定到本轮浮现的情感记忆，附带当前原文与状态；是否使用、回应或行动由 AI 自行判断。",
    }}
