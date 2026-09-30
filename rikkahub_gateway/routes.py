"""Immutable, operator-owned model routes. No client URL or secret is accepted."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit

AUXILIARY_SUFFIX = "--auxiliary-no-memory"
MAX_ROUTES_BYTES = 65536
MAX_EXTRA_ROUTES = 16
PUBLIC_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
UPSTREAM_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}\Z")
ROUTE_KEY_ENV = re.compile(r"STBRAIN_UPSTREAM_ROUTE_[A-Z][A-Z0-9_]{0,63}_API_KEY\Z")


@dataclass(frozen=True)
class GatewayRoute:
    public_model: str
    upstream_base_url: str = field(repr=False)
    upstream_model: str
    api_key: str = field(repr=False)
    managed: bool = False

    @property
    def auxiliary_model(self) -> str:
        return self.public_model + AUXILIARY_SUFFIX

    @property
    def completion_url(self) -> str:
        return self.upstream_base_url.rstrip("/") + "/chat/completions"


def valid_route_base(value: str, *, legacy_http: bool = False) -> bool:
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 2048:
            return False
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 or char in "\\%" for char in value):
            return False
        url = urlsplit(value)
        return bool(url.hostname and url.username is None and url.password is None
                    and not url.query and not url.fragment and "?" not in value and "#" not in value
                    and (url.port is None or 1 <= url.port <= 65535)
                    and all(part not in {".", ".."} for part in url.path.split("/"))
                    and (url.scheme == "https" or (url.scheme == "http" and
                         (legacy_http or url.hostname in {"127.0.0.1", "::1", "localhost"}))))
    except (TypeError, ValueError):
        return False


def validate_route(route: GatewayRoute, *, legacy_http: bool = False, legacy_labels: bool = False) -> None:
    def valid_label(value: str, pattern: re.Pattern) -> bool:
        if not isinstance(value, str):
            return False
        if legacy_labels:
            # Preserve already-installed human-facing/Unicode legacy aliases verbatim.
            return bool(value.strip()) and all(ord(char) >= 32 and ord(char) != 127 for char in value)
        return bool(pattern.fullmatch(value))
    if (not isinstance(route, GatewayRoute)
            or not valid_label(route.public_model, PUBLIC_MODEL)
            or (not legacy_labels and route.public_model.endswith(AUXILIARY_SUFFIX))
            or not valid_label(route.upstream_model, UPSTREAM_MODEL)
            or not valid_route_base(route.upstream_base_url, legacy_http=legacy_http)
            or not isinstance(route.api_key, str) or not 1 <= len(route.api_key) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in route.api_key)):
        raise ValueError("invalid_gateway_route")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_gateway_routes")
        result[key] = value
    return result


def routes_from_env(env: Mapping[str, str]) -> tuple[GatewayRoute, ...]:
    """Optional additional routes. Only named dedicated secret variables may resolve keys."""
    raw = env.get("STBRAIN_GATEWAY_ROUTES_JSON")
    if raw is None:
        return ()
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_ROUTES_BYTES:
            raise ValueError()
        records = json.loads(raw, object_pairs_hook=_unique_pairs)
        if not isinstance(records, list) or len(records) > MAX_EXTRA_ROUTES:
            raise ValueError()
        result = []
        for record in records:
            if (not isinstance(record, dict) or set(record) != {
                    "public_model", "upstream_base_url", "upstream_model", "api_key_env"}
                    or any(not isinstance(value, str) for value in record.values())
                    or not ROUTE_KEY_ENV.fullmatch(record["api_key_env"])):
                raise ValueError()
            route = GatewayRoute(record["public_model"], record["upstream_base_url"],
                                 record["upstream_model"], env[record["api_key_env"]])
            validate_route(route)
            if any(route.api_key == env.get(name) for name in (
                    "STBRAIN_MCP_TOKEN", "STBRAIN_WAKE_SECRET", "STBRAIN_WAKE_TOKEN", "STBRAIN_HOST_TOKEN",
                    "STBRAIN_HUMAN_TOKEN", "STBRAIN_GATEWAY_TOKEN")):
                raise ValueError()
            result.append(route)
        protected = {value for name in ("STBRAIN_MCP_TOKEN", "STBRAIN_WAKE_SECRET", "STBRAIN_WAKE_TOKEN",
                     "STBRAIN_HOST_TOKEN", "STBRAIN_HUMAN_TOKEN", "STBRAIN_GATEWAY_TOKEN", "STBRAIN_UPSTREAM_API_KEY")
                     if isinstance(value := env.get(name), str) and value}
        protected.update(route.api_key for route in result)
        if any(secret in value for row in records for value in row.values() for secret in protected):
            raise ValueError()
        return tuple(result)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError("invalid_gateway_routes") from None
