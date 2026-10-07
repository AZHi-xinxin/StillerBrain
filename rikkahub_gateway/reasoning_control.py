"""Narrow upstream-dialect adaptation; no model-name or client-host guessing."""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlsplit

from .routes import GatewayRoute


def apply_upstream_reasoning_control(payload: dict[str, Any], route: GatewayRoute) -> None:
    """Adapt an explicit OpenAI-compatible OFF on an already copied payload.

    DeepSeek Chat Completions uses thinking.type, not reasoning_effort=none,
    to disable thinking. Only the operator-owned, exact official HTTPS route
    establishes this dialect. AUTO (no field), enabled effort levels, other
    hosts and unsupported/malformed custom values are untouched.

    An explicit native enabled/disabled control wins over the generic OFF,
    e.g. a client's advanced customBody override. Remove none in either case
    because DeepSeek's effort field accepts enabled levels, not an OFF token.
    https://api-docs.deepseek.com/guides/thinking_mode/
    """
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
