"""Read-only attachment hints for already-disclosed memory; never load image blobs."""
from copy import deepcopy
import sqlite3

from .memory_images import MemoryImageStore
from .memory_relations import MemoryRelationError, parse_memory_ref


def image_attachment_hint(connection, *, owner_id, model_id, target_ref):
    """Optional older databases have no image tables. Their read path stays unchanged."""
    try:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_image_items'").fetchone() is None:
            return None
        endpoint = parse_memory_ref(target_ref)
        MemoryImageStore._schema(connection)
        MemoryImageStore._endpoint(connection, (owner_id, model_id), endpoint)
        count = connection.execute(
            "SELECT COUNT(*) FROM memory_image_items WHERE owner_id=? AND model_id=? AND target_module=? AND target_id=? AND active=1",
            (owner_id, model_id, *endpoint[:2])).fetchone()[0]
        if not count:
            return None
        return {"target_ref": target_ref, "count": count,
                "lookup": {"tool": "list_memory_images", "arguments": {"target_ref": target_ref, "limit": 4}},
                "image_bytes_included": False}
    except (MemoryRelationError, sqlite3.Error, ValueError, TypeError):
        # An unavailable attachment must not suppress otherwise valid textual recall.
        return None


def append_image_hints(connection, *, owner_id, model_id, dynamic, estimate_tokens, budget=1200):
    refs = []
    # Only host-authored envelope IDs, never arbitrary references inside stored prose.
    for module, field in (("emotional_memory", "memories"), ("learning_memory", "envelopes"),
                          ("planning_memory", "envelopes"), ("emotion_layer", "memories")):
        payload = dynamic.get(module, {})
        if not isinstance(payload, dict):
            continue
        for item in payload.get(field, []):
            if not isinstance(item, dict) or item.get("content_stub") or item.get("restricted_stub") or item.get("presentation") == "neutral_hint":
                continue
            ref = item.get("item_ref") or item.get("memory_ref")
            if module == "emotional_memory" and isinstance(item.get("memory_id"), str):
                try:
                    row = connection.execute("SELECT current_version FROM emotion_memories WHERE owner_id=? AND model_id=? AND memory_id=?",
                                             (owner_id, model_id, item["memory_id"])).fetchone()
                    ref = f"emotion://{item['memory_id']}@{row[0]}" if row else None
                except sqlite3.Error:
                    ref = None
            if isinstance(ref, str) and ref not in refs:
                refs.append(ref)
    hints = []
    result = dynamic
    for ref in refs[:12]:
        hint = image_attachment_hint(connection, owner_id=owner_id, model_id=model_id, target_ref=ref)
        if hint is None:
            continue
        candidate = {**dynamic, "memory_images": {"attachments": [*hints, hint],
            "usage": "这些已召回记忆带有图片。需要查看时按 lookup 列出，再用 read_memory_image(include_data=true) 读取一张；无需每轮读取。",
            "permission_authority": "none"}}
        if estimate_tokens(candidate) <= budget:
            hints.append(hint)
            result = candidate
        if len(hints) == 3:
            break
    return deepcopy(result) if result is not dynamic else dynamic
