"""Fixed-scope Orbis atlas adapter, separate from chat/wake/model transports.

External device secrets never reach Control; only verifiers do. Every response
is bounded and revalidated, and neither requests nor failures are logged here.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import hmac
import json
import re
import threading
import time
from urllib.parse import urlsplit

import httpx

from runtime.atlas_device_grants import (
    AtlasGrantError, capabilities, registration, revocation, strict_object,
    token_verifier,
)


CAPABILITIES = "/v1/st/atlas/capabilities"
REGISTER = "/v1/st/atlas/grants"
SNAPSHOT = "/v1/atlas"
REVOKE = "/v1/st/atlas/grants/revoke"
ROUTES = {CAPABILITIES: "GET", REGISTER: "POST", SNAPSHOT: "GET", REVOKE: "POST"}
CONTROL_PATHS = {
    CAPABILITIES: "/v1/host/atlas/capabilities",
    REGISTER: "/v1/host/atlas/register",
    SNAPSHOT: "/v1/host/atlas/snapshot",
    REVOKE: "/v1/host/atlas/revoke",
}
TIMEOUT_SECONDS = 6.0
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
ERRORS = {
    (400, "invalid_request"), (401, "unauthorized"),
    (403, "atlas_disabled"), (409, "request_conflict"),
    (429, "grant_limit_reached"), (503, "atlas_unavailable"),
}


def _valid(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _instant(value):
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    except (ValueError, OverflowError):
        return False


def _unavailable():
    return AtlasGrantError(503, "atlas_unavailable")


def project_response(route, value, request):
    """Exact metadata-only shape: unknown fields cannot become a body channel."""
    invalid = False
    if route == CAPABILITIES:
        invalid = (type(value.get("enabled")) is not bool or
                   value != capabilities(value.get("enabled") is True))
    elif route == REGISTER:
        invalid = (set(value) != {"schema", "requestId", "grantId", "status", "scope", "expiresAt"}
                   or value.get("schema") != "orbis.st.atlas-register-result/1"
                   or value.get("requestId") != request["requestId"]
                   or not _valid(HEX32, value.get("grantId"))
                   or value.get("status") not in ("active", "revoked")
                   or value.get("scope") != "atlas.metadata.read"
                   or value.get("expiresAt") is not None)
    elif route == REVOKE:
        invalid = value != {"schema": "orbis.st.atlas-revoke-result/1", "status": "revoked"}
    elif route == SNAPSHOT:
        invalid = (set(value) != {"schema", "generatedAt", "truncated", "stars", "edges"}
                   or value.get("schema") != "orbis.st.atlas/1"
                   or not _instant(value.get("generatedAt"))
                   or type(value.get("truncated")) is not bool
                   or not isinstance(value.get("stars"), list)
                   or not isinstance(value.get("edges"), list))
        if invalid or len(value["stars"]) > 2000 or len(value["edges"]) > 5000:
            raise _unavailable()
        ids = set()
        for star in value["stars"]:
            if (not isinstance(star, dict) or set(star) != {"id", "type", "storedAt"}
                    or not _valid(HEX64, star.get("id")) or star["id"] in ids
                    or star.get("type") not in ("情感", "学习", "规划")
                    or not _instant(star.get("storedAt"))):
                raise _unavailable()
            ids.add(star["id"])
        edges = set()
        for edge in value["edges"]:
            if (not isinstance(edge, dict) or set(edge) != {"a", "b"}
                    or not _valid(HEX64, edge.get("a")) or not _valid(HEX64, edge.get("b"))
                    or edge["a"] not in ids or edge["b"] not in ids
                    or edge["a"] == edge["b"]):
                raise _unavailable()
            pair = tuple(sorted((edge["a"], edge["b"])))
            if pair in edges:
                raise _unavailable()
            edges.add(pair)
    else:
        invalid = True
    if invalid:
        raise _unavailable()
    return value


class AtlasGateway:
    def __init__(self, control_url, host_token, *, protected_values=(), transport=None):
        self._url = control_url.rstrip("/")
        self._host_token = host_token
        self._protected = tuple(v for v in (*protected_values, host_token) if v)
        self._forbidden = frozenset(hashlib.sha256(v.encode()).hexdigest() for v in self._protected)
        self._transport = transport
        self._slots = threading.BoundedSemaphore(2)

    async def _post(self, route, payload):
        limit = 1024 * 1024 if route == SNAPSHOT else 4096
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(4.0, connect=2.0, pool=1.0), trust_env=False,
            follow_redirects=False, transport=self._transport,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        ) as client:
            async with client.stream(
                "POST", self._url + CONTROL_PATHS[route], json=payload,
                headers={"Authorization": "Bearer " + self._host_token,
                         "Accept": "application/json", "Accept-Encoding": "identity"},
            ) as response:
                if (response.status_code != 200 and response.status_code not in {s for s, _ in ERRORS}):
                    raise _unavailable()
                if (response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json"
                        or response.headers.get("Content-Encoding", "identity").strip().lower() != "identity"):
                    raise _unavailable()
                if response.status_code != 200:
                    limit = 4096
                lengths = response.headers.get_list("Content-Length")
                if lengths and (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0])
                                or int(lengths[0]) > limit):
                    raise _unavailable()
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    if len(raw) + len(chunk) > limit:
                        raise _unavailable()
                    raw.extend(chunk)
                if lengths and int(lengths[0]) != len(raw):
                    raise _unavailable()
                try:
                    result = strict_object(bytes(raw), limit=limit)
                except (AtlasGrantError, ValueError, RecursionError):
                    raise _unavailable() from None
                if response.status_code != 200:
                    error = result.get("error")
                    if (set(result) == {"error"} and isinstance(error, dict) and set(error) == {"code"}
                            and isinstance(error["code"], str)
                            and (response.status_code, error["code"]) in ERRORS):
                        raise AtlasGrantError(response.status_code, error["code"])
                    raise _unavailable()
                result = project_response(route, result, payload)
                encoded = json.dumps(result, ensure_ascii=False)
                if any(secret in encoded for secret in self._protected):
                    raise _unavailable()
                return result

    def request(self, route, payload):
        if route not in CONTROL_PATHS:
            raise _unavailable()
        if not isinstance(payload, dict):
            raise AtlasGrantError(400, "invalid_request")
        if route in {REGISTER, SNAPSHOT, REVOKE}:
            verifier = payload.get("verifier")
            if not isinstance(verifier, str) or not re.fullmatch(r"[0-9a-f]{64}", verifier):
                raise AtlasGrantError(400, "invalid_request")
            if verifier in self._forbidden:
                raise AtlasGrantError(401, "unauthorized")
        if not self._slots.acquire(blocking=False):
            raise _unavailable()
        async def bounded():
            return await asyncio.wait_for(self._post(route, payload), TIMEOUT_SECONDS)
        try:
            return asyncio.run(bounded())
        except AtlasGrantError:
            raise
        except (httpx.HTTPError, OSError, ValueError, RuntimeError, TimeoutError, RecursionError):
            raise _unavailable() from None
        finally:
            self._slots.release()


def handle_atlas_request(handler, method):
    """Returns False only for unrelated paths; never consumes chat payloads."""
    route = urlsplit(handler.path).path
    if route not in ROUTES:
        return False
    handler.close_connection = True
    try:
        if (handler.path != route or handler.headers.get_all("Origin", [])
                or handler.headers.get_all("Cookie", [])
                or handler.headers.get_all("Transfer-Encoding", [])
                or handler.headers.get_all("Content-Encoding", [])):
            raise AtlasGrantError(400, "invalid_request")
        if ROUTES[route] != method:
            raise AtlasGrantError(405, "method_not_allowed")
        auth = handler.headers.get_all("Authorization", [])
        if len(auth) > 1:
            raise AtlasGrantError(401, "unauthorized")
        payload = {}
        token = None
        if route != CAPABILITIES:
            if (len(auth) != 1 or not auth[0].isascii() or len(auth[0]) > 4096
                    or not auth[0].startswith("Bearer ")):
                raise AtlasGrantError(401, "unauthorized")
            token = auth[0][7:]
            if route == REGISTER:
                if not hmac.compare_digest(token, handler.app.config.gateway_token):
                    raise AtlasGrantError(401, "unauthorized")
            else:
                payload["verifier"] = token_verifier(token)
        lengths = handler.headers.get_all("Content-Length", [])
        if method == "GET":
            if lengths not in ([], ["0"]):
                raise AtlasGrantError(400, "invalid_request")
            if route == SNAPSHOT:
                payload["schema"] = "orbis.st.atlas-snapshot/1"
        else:
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,4}", lengths[0]):
                raise AtlasGrantError(400, "invalid_request")
            length = int(lengths[0])
            if not 0 < length <= 2048:
                raise AtlasGrantError(413, "request_too_large")
            content_types = handler.headers.get_all("Content-Type", [])
            if (len(content_types) != 1 or
                    content_types[0].split(";", 1)[0].strip().lower() != "application/json"):
                raise AtlasGrantError(415, "application_json_required")
            previous = handler.connection.gettimeout()
            try:
                deadline = time.monotonic() + 3.0
                body = bytearray()
                while len(body) < length:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    handler.connection.settimeout(min(previous, remaining) if previous else remaining)
                    chunk = handler.rfile.read1(length - len(body))
                    if not chunk:
                        break
                    body.extend(chunk)
                raw = bytes(body)
            finally:
                handler.connection.settimeout(previous)
            if len(raw) != length:
                raise AtlasGrantError(400, "invalid_request")
            value = strict_object(raw, limit=2048)
            if route == REGISTER:
                registration(value)
                payload = value
            else:
                revocation(value)
                payload.update(value)
        handler._json(200, handler.app.atlas.request(route, payload))
    except AtlasGrantError as exc:
        handler._json(exc.status, {"error": {"code": exc.code}})
    except (ValueError, RecursionError):
        handler._json(400, {"error": {"code": "invalid_request"}})
    except (OSError, TimeoutError):
        # No exception text, traceback, credential or request body in logs.
        try:
            handler._json(408, {"error": {"code": "request_timeout"}})
        except OSError:
            pass
    return True
