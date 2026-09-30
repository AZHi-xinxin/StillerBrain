"""Read-only official DeepSeek balance, isolated from model and wake transports."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Sequence
from urllib.parse import urlsplit

import httpx


BALANCE_URL = "https://api.deepseek.com/user/balance"
BALANCE_TIMEOUT_SECONDS = 8.0
MAX_BALANCE_BYTES = 16 * 1024
_AMOUNT = re.compile(r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,18})?\Z")


class BalanceError(RuntimeError):
    """Only fixed client-safe status/code pairs leave this transport."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _official_base(base_url: str) -> bool:
    try:
        parsed = urlsplit(base_url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "api.deepseek.com"
            and parsed.port in (None, 443)
            and parsed.username is None and parsed.password is None
            and parsed.path in ("", "/", "/v1", "/v1/")
            and not parsed.query and not parsed.fragment
            and not any(char.isspace() or ord(char) < 32 for char in base_url)
        )
    except (ValueError, TypeError):
        return False


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _project(raw: bytes, protected_values: Sequence[str]) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise BalanceError(502, "balance_invalid_response") from None
    if not isinstance(value, dict) or type(value.get("is_available")) is not bool:
        raise BalanceError(502, "balance_invalid_response")
    infos = value.get("balance_infos")
    if not isinstance(infos, list) or not 1 <= len(infos) <= 2:
        raise BalanceError(502, "balance_invalid_response")
    result = {"is_available": value["is_available"], "balance_infos": []}
    currencies: set[str] = set()
    for info in infos:
        if not isinstance(info, dict) or info.get("currency") not in ("CNY", "USD"):
            raise BalanceError(502, "balance_invalid_response")
        currency = info["currency"]
        if currency in currencies:
            raise BalanceError(502, "balance_invalid_response")
        currencies.add(currency)
        safe = {"currency": currency}
        for key in ("total_balance", "granted_balance", "topped_up_balance"):
            amount = info.get(key)
            if not isinstance(amount, str) or not _AMOUNT.fullmatch(amount):
                raise BalanceError(502, "balance_invalid_response")
            safe[key] = amount
        result["balance_infos"].append(safe)
    encoded = json.dumps(result, ensure_ascii=False)
    if any(secret and secret in encoded for secret in protected_values):
        raise BalanceError(502, "balance_invalid_response")
    return result


async def _fetch(api_key: str, protected_values: Sequence[str],
                 transport: httpx.AsyncBaseTransport | None) -> dict[str, Any]:
    # Dedicated client has no model cookies, client headers, proxy environment,
    # default authorization, redirects, or caller-selected URL/query/body.
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(5.0, connect=3.0, write=3.0, pool=3.0),
        follow_redirects=False, trust_env=False, transport=transport,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    ) as client:
        async with client.stream(
            "GET", BALANCE_URL, follow_redirects=False,
            headers={"Authorization": "Bearer " + api_key,
                     "Accept": "application/json", "Accept-Encoding": "identity"},
        ) as response:
            if 300 <= response.status_code < 400:
                raise BalanceError(502, "balance_upstream_redirect_rejected")
            if response.status_code in (401, 403):
                raise BalanceError(502, "balance_upstream_auth_failed")
            if response.status_code == 429:
                raise BalanceError(503, "balance_upstream_rate_limited")
            if response.status_code != 200:
                raise BalanceError(502, "balance_upstream_unavailable")
            if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise BalanceError(502, "balance_invalid_response")
            if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                raise BalanceError(502, "balance_invalid_response")
            length = response.headers.get("content-length")
            if length is not None:
                if not length.isdecimal() or len(length) > 8 or int(length) > MAX_BALANCE_BYTES:
                    raise BalanceError(502, "balance_response_too_large")
            body = bytearray()
            async for chunk in response.aiter_raw():
                if len(body) + len(chunk) > MAX_BALANCE_BYTES:
                    raise BalanceError(502, "balance_response_too_large")
                body.extend(chunk)
            return _project(bytes(body), protected_values)


def read_balance(base_url: str, api_key: str, *, protected_values: Sequence[str] = (),
                 transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    if not _official_base(base_url):
        raise BalanceError(503, "balance_official_upstream_required")
    if not isinstance(api_key, str) or not api_key or any(ord(char) < 33 or ord(char) > 126 for char in api_key):
        raise BalanceError(503, "balance_upstream_key_invalid")

    async def bounded() -> dict[str, Any]:
        # Cancellation includes DNS/connect/response headers and streaming body,
        # so a slow header stream cannot reset a per-read timeout indefinitely.
        return await asyncio.wait_for(
            _fetch(api_key, (*protected_values, api_key), transport),
            timeout=BALANCE_TIMEOUT_SECONDS,
        )

    try:
        return asyncio.run(bounded())
    except BalanceError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise BalanceError(504, "balance_upstream_timeout") from None
    except (httpx.HTTPError, OSError, ValueError, RuntimeError):
        raise BalanceError(502, "balance_upstream_unavailable") from None
