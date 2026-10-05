"""Pure, bounded daily-memory input compatibility; never grants module access.

Routing uses explicit metadata only, not the body's meaning or available grants.
Callers must authenticate the original payload and check *all original* values
for protected credentials before persistence, including labels not retained.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any, Mapping

from runtime.emotional_memory import EMOTION_LABELS, MEMORY_TYPES
from runtime.learning_memory import LEARNING_KINDS
from runtime.planning_memory import PLAN_KINDS


_KINDS = {"emotional_memory": MEMORY_TYPES, "learning_memory": LEARNING_KINDS,
          "planning_memory": PLAN_KINDS}
_DEFAULTS = {"emotional_memory": "unclassified", "learning_memory": "fact",
             "planning_memory": "task"}
_MODULE_ALIASES = {"emotion": "emotional_memory", "emotional": "emotional_memory",
                   "learn": "learning_memory", "learning": "learning_memory",
                   "plan": "planning_memory", "planning": "planning_memory"}
_KIND_ALIASES = {"moment": "shared_event", "event": "shared_event"}
_KEYWORD_SEPARATORS = re.compile(r"\r\n|[,，、;；\r\n]")


class DailyMemoryInputError(ValueError):
    """Only fixed codes/field names; never echoes submitted values."""

    def __init__(self, code: str, field: str) -> None:
        super().__init__(code)
        self.code, self.field = code, field


@dataclass(frozen=True)
class DailyMemoryInput:
    fields: dict[str, Any]
    warnings: tuple[dict[str, str], ...] = ()


def resolve_daily_memory_module(arguments: Mapping[str, Any]) -> str:
    """Return one ordinary module, without authorizing or opening it.

    ``auto`` means a deterministic metadata default, not semantic classification.
    Explicit unknown, self, tool and permission-module names are never upgraded
    or silently redirected. Receipt-bearing writes require the exact module.
    """
    module = arguments.get("module", "auto")
    if arguments.get("rewrite_receipt") is not None:
        if not isinstance(module, str) or module not in _KINDS:
            raise DailyMemoryInputError("invalid_module", "module")
        return module
    if module is not None and not isinstance(module, str):
        raise DailyMemoryInputError("invalid_module", "module")
    name = module.strip().casefold() if module is not None else "auto"
    if name not in {"", "auto"}:
        result = _MODULE_ALIASES.get(name, name)
        if result not in _KINDS:
            raise DailyMemoryInputError("invalid_module", "module")
        return result
    kind = arguments.get("kind")
    kind = kind.strip().casefold() if isinstance(kind, str) else None
    if (arguments.get("parent_ref") is not None or arguments.get("due_at") is not None
            or kind in PLAN_KINDS):
        return "planning_memory"
    if kind in LEARNING_KINDS:
        return "learning_memory"
    return "emotional_memory"


def _keywords(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if len(value) > 32_768:
        raise DailyMemoryInputError("invalid_keywords", "keywords")
    text = value.strip()
    if not text:
        return None
    if text.startswith("["):
        # Only a flat JSON array is supported. Bound nesting before json.loads.
        in_string, escaped, depth = False, False, 0
        for character in text:
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
            elif character == '"':
                in_string = True
            elif character in "[{":
                depth += 1
                if depth > 2:
                    raise DailyMemoryInputError("invalid_keywords", "keywords")
            elif character in "]}":
                depth -= 1
        try:
            result = json.loads(text)
        except (ValueError, RecursionError):
            raise DailyMemoryInputError("invalid_keywords", "keywords") from None
        if not isinstance(result, list):
            raise DailyMemoryInputError("invalid_keywords", "keywords")
        return result
    if text[0] in "{}]\"'`" or any(mark in text for mark in "[]{}"):
        raise DailyMemoryInputError("invalid_keywords", "keywords")
    # No truncation, deduplication, sorting or splitting of array elements.
    return [item.strip() for item in _KEYWORD_SEPARATORS.split(text)]


def normalize_daily_memory_input(arguments: Mapping[str, Any]) -> DailyMemoryInput:
    """Copy compatible optional labels, preserving body/security fields exactly.

    Receipt-bearing fields are not coerced, including explicit null/blank values.
    The facade retains its strict validation and receipt's final-field binding.
    Unknown labels produce fixed warnings, not new keywords or invented facts.
    """
    result = dict(arguments)
    module = resolve_daily_memory_module(arguments)
    result["module"] = module
    if arguments.get("rewrite_receipt") is not None:
        return DailyMemoryInput(result)
    for field in ("title", "summary", "reason"):
        value = result.get(field)
        if isinstance(value, str) and not value.strip():
            result[field] = None
    warnings: list[dict[str, str]] = []
    for field, allowed, default in (("kind", _KINDS[module], _DEFAULTS[module]),
                                    ("emotion", EMOTION_LABELS, "other")):
        value = result.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            result[field] = default
            continue
        if not isinstance(value, str) or len(value) > 160:
            raise DailyMemoryInputError("invalid_" + field, field)
        normalized = value.strip().casefold()
        if field == "kind":
            normalized = _KIND_ALIASES.get(normalized, normalized)
        if normalized in allowed:
            result[field] = normalized
        else:
            result[field] = default
            warnings.append({"code": field + "_unrecognized_defaulted", "field": field,
                             "message": ("已按该模块默认分类保存，正文保持原样；未知标签未另存，无需重复存入。"
                                         if field == "kind" else
                                         "已按未标注情绪保存，正文保持原样；未推测情绪，无需重复存入。")})
    value = result.get("importance", 50)
    if value is None or (isinstance(value, str) and not value.strip()):
        value = 50
    elif isinstance(value, str):
        text = value.strip()
        if len(text) > 3 or re.fullmatch(r"[0-9]{1,3}", text) is None:
            raise DailyMemoryInputError("invalid_importance", "importance")
        value = int(text)
    elif type(value) is float:
        if not math.isfinite(value) or not value.is_integer():
            raise DailyMemoryInputError("invalid_importance", "importance")
        value = int(value)
    if type(value) is not int or not 0 <= value <= 100:
        raise DailyMemoryInputError("invalid_importance", "importance")
    result["importance"] = value
    result["keywords"] = _keywords(result.get("keywords"))
    return DailyMemoryInput(result, tuple(warnings))
