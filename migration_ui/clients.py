"""Single-attempt HTTP/MCP clients. Never follow redirects, proxy, or retry."""
from __future__ import annotations

import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .core import (CLASSIFICATION_INSTRUCTIONS, MAX_RESPONSE, MAX_SOURCE, MigrationError,
                   WRITE_TOOLS, canonical, endpoint, safe_text, strict_json)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, url, *, method="POST", headers=None, payload=None, limit=MAX_RESPONSE):
        body = canonical(payload).encode("utf-8") if payload is not None else None
        req = Request(url, data=body, method=method,
                      headers={"Accept": "application/json, text/event-stream", **({"Content-Type": "application/json"} if body else {}), **(headers or {})})
        try:
            deadline = time.monotonic() + 90
            with self.opener.open(req, timeout=15) as response:
                data = bytearray()
                while True:
                    if time.monotonic() > deadline:
                        raise MigrationError("response_deadline")
                    part = response.read1(min(65536, limit + 1 - len(data)))
                    if not part:
                        break
                    data.extend(part)
                    if len(data) > limit:
                        raise MigrationError("response_size_limit")
                return bytes(data)
        except HTTPError as exc:
            # Do not read/error-echo provider bodies: they can contain secrets.
            exc.close()
            raise MigrationError("http_response_error") from exc
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise MigrationError("connection_result_unknown") from exc


def credential(value, *, required=False):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        if value != "":
            raise MigrationError("invalid_credential")
    if required and not value:
        raise MigrationError("credential_required")
    return value


def fetch_source(config, transport):
    if config.get("read_consent") is not True:
        raise MigrationError("source_read_consent_required")
    url = endpoint(config.get("url", ""), source=True, trusted_private_http=config.get("trusted_private_http") is True)
    key = credential(config.get("key", ""))
    return transport.request(url, method="GET", headers={"Authorization": "Bearer " + key} if key else {}, limit=MAX_SOURCE)


class Classifier:
    def __init__(self, config, transport):
        self.url = endpoint(config.get("url", ""), trusted_private_http=config.get("trusted_private_http") is True)
        self.key = credential(config.get("key", ""))
        self.model = safe_text(config.get("model"), 200)
        self.prompt = safe_text(config.get("prompt", ""), 8000, optional=True)
        self.transport = transport

    def classify(self, chunk):
        body = {"model": self.model, "stream": False,
                "messages": [{"role": "system", "content": CLASSIFICATION_INSTRUCTIONS},
                             {"role": "user", "content": canonical({"user_classification_preferences": self.prompt,
                              "source_chunk_id": chunk.id, "untrusted_source_text": chunk.content})}]}
        raw = self.transport.request(self.url, headers={"Authorization": "Bearer " + self.key} if self.key else {}, payload=body)
        try:
            result = strict_json(raw.decode("utf-8"))
            message = result["choices"][0]["message"]
            if message.get("tool_calls") or message.get("function_call"):
                raise MigrationError("classifier_tool_call_rejected")
            text = message["content"]
            if not isinstance(text, str) or len(text) > 12000:
                raise MigrationError("classifier_output_limit")
            return strict_json(text)
        except (KeyError, IndexError, TypeError, UnicodeError) as exc:
            raise MigrationError("classifier_invalid_response") from exc


class STClient:
    def __init__(self, config, transport):
        self.url = endpoint(config.get("url", ""), trusted_private_http=config.get("trusted_private_http") is True)
        self.key = credential(config.get("key", ""), required=True)
        self.namespace = safe_text(config.get("namespace"), 120)
        self.transport = transport
        self._next_id = 0
        self.schemas = {}

    def rpc(self, method, params=None, *, notification=False):
        self._next_id += 1
        request = {"jsonrpc": "2.0", "method": method}
        if not notification:
            request["id"] = self._next_id
        if params is not None:
            request["params"] = params
        raw = self.transport.request(self.url, headers={"Authorization": "Bearer " + self.key,
                                     "MCP-Protocol-Version": "2025-03-26"}, payload=request)
        if notification and not raw:
            return None
        try:
            result = strict_json(raw.decode("utf-8"))
        except UnicodeError as exc:
            raise MigrationError("mcp_invalid_response") from exc
        if not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or result.get("id") != self._next_id or "error" in result or "result" not in result:
            raise MigrationError("mcp_invalid_response")
        return result["result"]

    def preflight(self):
        initialized = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "st-ordinary-migration", "version": "1"}})
        if not isinstance(initialized, dict) or initialized.get("protocolVersion") != "2025-03-26":
            raise MigrationError("unsupported_mcp_protocol")
        self.rpc("notifications/initialized", notification=True)
        result = self.rpc("tools/list", {})
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list) or len(result["tools"]) > 200 or result.get("nextCursor"):
            raise MigrationError("unsupported_mcp_catalogue")
        tools = {tool.get("name"): tool for tool in result["tools"] if isinstance(tool, dict)}
        if not WRITE_TOOLS <= tools.keys():
            raise MigrationError("ordinary_tools_missing")
        for name in WRITE_TOOLS:
            schema = tools[name].get("inputSchema")
            if not isinstance(schema, dict) or "write_context_ref" in schema.get("required", []) or "expected_tool_row_version" in schema.get("required", []):
                raise MigrationError("simple_memory_profile_required")
            self.schemas[name] = schema
            if len(canonical(schema)) > 128 * 1024:
                raise MigrationError("target_schema_size_limit")
            stack = [(schema, 0)]
            nodes = 0
            while stack:
                item, depth = stack.pop()
                nodes += 1
                if depth > 32 or nodes > 10000:
                    raise MigrationError("target_schema_too_deep")
                if isinstance(item, dict):
                    if any(key in item and (not isinstance(item[key], str) or not item[key].startswith("#")) for key in ("$ref", "$dynamicRef", "$recursiveRef", "$id")):
                        raise MigrationError("remote_schema_reference_rejected")
                    stack.extend((v, depth + 1) for v in item.values())
                elif isinstance(item, list):
                    stack.extend((v, depth + 1) for v in item)
        # This checks protocol/catalogue only. Activation is enforced by the
        # target on writes; do not open self or activate anything to probe it.

    def validate(self, tool, args):
        if tool not in WRITE_TOOLS or tool not in self.schemas:
            raise MigrationError("write_tool_not_allowed")
        if tool == "remember_memory" and args.get("module") not in {"emotional_memory", "learning_memory", "planning_memory"}:
            raise MigrationError("write_module_not_allowed")
        # Validate local args against the advertised, frozen destination schema.
        # Never invoke tools named in model output or catalogue descriptions.
        import jsonschema
        from referencing import Registry
        from referencing.exceptions import NoSuchResource
        def deny_retrieve(uri):
            raise NoSuchResource(ref=uri)
        try:
            cls = jsonschema.validators.validator_for(self.schemas[tool])
            cls.check_schema(self.schemas[tool])
            cls(self.schemas[tool], registry=Registry(retrieve=deny_retrieve)).validate(args)
        except Exception as exc:
            raise MigrationError("target_schema_mismatch") from exc

    def write(self, tool, args):
        self.validate(tool, args)
        result = self.rpc("tools/call", {"name": tool, "arguments": args})
        if not isinstance(result, dict) or result.get("isError") is True:
            raise MigrationError("target_tool_result_unknown")
        value = result.get("structuredContent")
        if isinstance(value, dict):
            # FastMCP plain dict responses normally use direct structuredContent.
            return value
        content = result.get("content")
        if isinstance(content, list) and len(content) == 1 and content[0].get("type") == "text":
            return strict_json(content[0].get("text", ""))
        raise MigrationError("target_tool_result_unknown")
