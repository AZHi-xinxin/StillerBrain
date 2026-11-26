"""Narrow upstream-dialect adaptation; no model-name or client-host guessing."""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlsplit

from .routes import GatewayRoute


def apply_upstream_reasoning_control(payload: dict[str, Any], route: GatewayRoute) -> None:
    """Adapt an explicit OpenAI-compatible OFF on an already copied payload.

    This adapter maps explicit OFF to DeepSeek's thinking.type=disabled.
    Current official documentation also accepts reasoning_effort=none; this
    mapping retains the adapter's explicit native-control precedence. Only the
    operator-owned, exact official HTTPS route establishes this dialect. AUTO
    (no field), enabled effort levels, other hosts and unsupported/malformed
    custom values are untouched.

    An explicit native enabled/disabled control wins over the generic OFF,
    e.g. a client's advanced customBody override. Remove the generic OFF in
    either case so only the selected native control is sent.
    https://api-docs.deepseek.com/guides/thinking_mode/
    """
    _include_argon_summary(payload, route)
    upstream = urlsplit(route.upstream_base_url)
    if (upstream.scheme != "https" or upstream.hostname != "api.deepseek.com"
            or upstream.port not in (None, 443)
            or payload.get("reasoning_effort") != "none"):
        return
    if "thinking" not in payload:
        payload["thinking"] = {"type": "disabled"}
    else:
        thinking = payload["thinking"]
        if not isinstance(thinking, Mapping) or thinking.get("type") not in ("enabled", "disabled"):
            return  # Do not repair/interpret an explicit unknown native control.
    del payload["reasoning_effort"]


def _include_argon_summary(payload: dict[str, Any], route: GatewayRoute) -> None:
    """Google requires thinking_config instead of reasoning_effort for summaries.

    Only the operator's official Argon route is adapted. Preserve AUTO and explicit
    levels, custom native settings, all signatures, and the original source frame.
    """
    upstream = urlsplit(route.upstream_base_url)
    if (upstream.scheme != "https" or upstream.hostname != "generativelanguage.googleapis.com"
            or upstream.port not in (None, 443) or route.upstream_model != "gemini-4-argon"
            or route.api_mode != "chat_completions"):
        return
    effort = payload.get("reasoning_effort")
    levels = {"minimal": "low", "low": "low", "medium": "medium", "high": "high"}
    if "reasoning_effort" in payload and (not isinstance(effort, str) or effort not in levels):
        return
    extra = payload.get("extra_body", {})
    if not isinstance(extra, dict):
        return
    google = extra.get("google", {})
    if not isinstance(google, dict):
        return
    thinking = google.get("thinking_config", {})
    if not isinstance(thinking, dict):
        return
    if effort is not None and ("thinking_level" in thinking or "thinking_budget" in thinking):
        return  # Explicit conflicting controls remain an upstream validation error.
    updated = dict(thinking)
    updated.setdefault("include_thoughts", True)
    if effort is not None:
        updated["thinking_level"] = levels[effort]
        del payload["reasoning_effort"]
    payload["extra_body"] = {**extra, "google": {**google, "thinking_config": updated}}
