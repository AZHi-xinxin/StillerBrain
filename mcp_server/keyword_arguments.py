"""Bounded keyword-only compatibility, after authentication/execution claiming.

Canonical arrays are unchanged. This does not parse general tool arguments,
modify caller-owned objects, or change the signed raw execution payload.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

from runtime.credential_guard import contains_credential_or_secret


MAX_KEYWORD_STRING = 32_768
_SEPARATORS = re.compile(r"\r\n|[,，、;；\r\n]")
_NESTED_TOOLS = frozenset({"revise_memory", "revise_planning_memory"})


class KeywordCredentialError(ValueError):
    """Fixed marker only; never store or expose the supplied content."""


def _keyword_list(value: str) -> list[str]:
    if len(value) > MAX_KEYWORD_STRING:
        raise ValueError("keyword_string_too_long")
    # Keep complete assignment/reference context before delimiter splitting.
    if contains_credential_or_secret(value):
        raise KeywordCredentialError("credential_or_secret_detected")
    text = value.strip()
    if not text:
        raise ValueError("keyword_string_empty")
    if text.startswith("["):
        try:
            result = json.loads(text)
        except (ValueError, RecursionError):
            raise ValueError("keyword_json_invalid") from None
        if (not isinstance(result, list)
                or any(not isinstance(item, str) or not item.strip() for item in result)):
            raise ValueError("keyword_json_array_required")
    else:
        # JSON/Python structures, quoted scalars and fenced snippets have no
        # unambiguous plain-list interpretation. Never eval or repeatedly decode.
        if text[0] in "{}]\"'`" or any(mark in text for mark in "[]{}"):
            raise ValueError("keyword_structure_ambiguous")
        result = [item.strip() for item in _SEPARATORS.split(text)]
        if any(not item for item in result):
            raise ValueError("keyword_empty_item")
    if contains_credential_or_secret(result):
        raise KeywordCredentialError("credential_or_secret_detected")
    # Runtime keeps the existing per-tool count/length limits and semantics.
    # In particular, do not truncate, deduplicate, sort or split array elements.
    return result


def normalize_keyword_arguments(
    name: str, arguments: Mapping[str, Any], fields: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Copy and normalize only registered keywords / two exact changes paths.

    Safe error fields are literals. Values and arbitrary input keys never enter
    error messages; format failures retain strict validation of other fields.
    """
    args = dict(arguments)
    errors: list[dict[str, str]] = []

    def convert(value: str, *, nested: bool) -> Any:
        try:
            return _keyword_list(value)
        except KeywordCredentialError:
            raise
        except ValueError:
            errors.append({"field": "changes" if nested else "keywords",
                           "issue": "keywords_format" if nested else "list_type"})
            return value

    if "keywords" in fields and isinstance(args.get("keywords"), str):
        args["keywords"] = convert(args["keywords"], nested=False)
    if (name in _NESTED_TOOLS and "changes" in fields
            and isinstance(args.get("changes"), dict)
            and isinstance(args["changes"].get("keywords"), str)):
        changes = dict(args["changes"])
        changes["keywords"] = convert(changes["keywords"], nested=True)
        args["changes"] = changes
    return args, errors
