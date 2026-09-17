#!/usr/bin/env python3
"""Independent authenticated host/human control plane for module one.

This service is deliberately separate from the AI-facing MCP server.  The host
token can only issue/prepare/confirm/close wake context.  The human token can
record an objection, move the active pointer to an earlier AI-approved
ancestor, or issue a short-lived one-use direct-write grant.  Neither token nor
any grant request body is ever included in an application log.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import os
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from runtime import (
    EmotionalMemoryStore,
    HallucinationVaultStore,
    InjectionControlStore,
    LearningQuarantineAdapter,
    ModuleOneOnboardingStore,
    OnboardingError,
    PlanningMemoryStore,
)
from runtime.learning_memory import LearningMemoryStore
from runtime.tool_guidance import ToolGuidanceStore
from runtime.execution_binding import ExecutionBindingError, ExecutionStore


MAX_BODY_BYTES = 64 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _required_text(body: Mapping[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _direct_grant_request(body: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Validate the complete human grant payload without accepting identities.

    Owner, model, human actor, direct client principal and TTL are deliberately
    absent from this payload.  They are server configuration, not authority the
    phone may choose for itself.
    """

    if set(body) != {"request_id", "requested_scopes"}:
        raise ValueError(
            "direct grant body must contain exactly request_id and requested_scopes"
        )
    request_id = _required_text(body, "request_id")
    try:
        parsed_request_id = uuid.UUID(request_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("request_id must be a UUIDv4 string") from exc
    if parsed_request_id.version != 4 or str(parsed_request_id) != request_id.lower():
        raise ValueError("request_id must be a canonical UUIDv4 string")

    requested_scopes = body.get("requested_scopes")
    if (
        not isinstance(requested_scopes, list)
        or not requested_scopes
        or len(requested_scopes) > 16
    ):
        raise ValueError("requested_scopes must contain between 1 and 16 strings")
    cleaned: list[str] = []
    for value in requested_scopes:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > 64
        ):
            raise ValueError("requested_scopes contains an invalid scope")
        scope = value.strip()
        if scope in cleaned:
            raise ValueError("requested_scopes must not contain duplicates")
        cleaned.append(scope)
    return str(parsed_request_id), cleaned


def _source_frame(body: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate the bounded, host-derived situation without logging its text."""

    value = body.get("source_frame")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("source_frame must be an object")
    allowed = {
        "query_text",
        "thread_id",
        "lineage_stable",
        "prior_assistant_present",
        "first_user_turn",
        "source_event_id",
        "capture_items",
    }
    if set(value) - allowed:
        raise ValueError("source_frame contains unknown fields")
    query_text = value.get("query_text", "")
    if not isinstance(query_text, str) or len(query_text) > 4000:
        raise ValueError("source_frame.query_text must be a string of at most 4000 characters")
    lineage_stable = value.get("lineage_stable", False)
    if not isinstance(lineage_stable, bool):
        raise ValueError("source_frame.lineage_stable must be boolean")
    prior_assistant_present = value.get("prior_assistant_present", False)
    if not isinstance(prior_assistant_present, bool):
        raise ValueError("source_frame.prior_assistant_present must be boolean")
    first_user_turn = value.get("first_user_turn", False)
    if not isinstance(first_user_turn, bool):
        raise ValueError("source_frame.first_user_turn must be boolean")
    thread_id = value.get("thread_id")
    if thread_id is not None and (
        not isinstance(thread_id, str) or not thread_id.strip() or len(thread_id) > 256
    ):
        raise ValueError("source_frame.thread_id is invalid")
    source_event_id = value.get("source_event_id")
    if source_event_id is not None and (
        not isinstance(source_event_id, str)
        or not source_event_id.strip()
        or len(source_event_id) > 256
    ):
        raise ValueError("source_frame.source_event_id is invalid")
    capture_items = value.get("capture_items", [])
    if not isinstance(capture_items, list) or len(capture_items) > 4:
        raise ValueError("source_frame.capture_items must contain at most four messages")
    cleaned_items: list[dict[str, str]] = []
    for item in capture_items:
        if not isinstance(item, Mapping) or set(item) != {"role", "content"}:
            raise ValueError("source_frame.capture_items entries are invalid")
        role, content = item.get("role"), item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            raise ValueError("source_frame.capture_items entries are invalid")
        if len(content) > 1200:
            raise ValueError("source_frame capture content is too long")
        cleaned_items.append({"role": role, "content": content})
    if lineage_stable and (not thread_id or not source_event_id):
        raise ValueError("stable source_frame requires thread_id and source_event_id")
    return {
        "query_text": query_text,
        "thread_id": thread_id,
        "lineage_stable": lineage_stable,
        "prior_assistant_present": prior_assistant_present,
        "first_user_turn": first_user_turn,
        "source_event_id": source_event_id,
        "capture_items": cleaned_items,
    }


def _advertised_tools(body: Mapping[str, Any]) -> dict[str, Any]:
    """Accept only the gateway's schema-hash catalog, never raw tool schemas."""

    value = body.get(
        "advertised_tools",
        {
            "contract": "advertised-tools/1",
            "catalog_complete": True,
            "catalog_hash": hashlib.sha256(b"[]").hexdigest(),
            "entries": [],
        },
    )
    if not isinstance(value, Mapping) or set(value) != {
        "contract",
        "catalog_complete",
        "catalog_hash",
        "entries",
    }:
        raise ValueError("advertised_tools has an invalid shape")
    if value.get("contract") != "advertised-tools/1":
        raise ValueError("advertised_tools.contract is invalid")
    if not isinstance(value.get("catalog_complete"), bool):
        raise ValueError("advertised_tools.catalog_complete must be boolean")
    supplied_hash = value.get("catalog_hash")
    entries = value.get("entries")
    if not isinstance(supplied_hash, str) or not _SHA256.fullmatch(supplied_hash):
        raise ValueError("advertised_tools.catalog_hash is invalid")
    if not isinstance(entries, list) or len(entries) > 512:
        raise ValueError("advertised_tools.entries is invalid")
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"canonical_name", "schema_hash"}:
            raise ValueError("advertised_tools entry has an invalid shape")
        name, schema_hash = entry.get("canonical_name"), entry.get("schema_hash")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
            or name in seen
            or not isinstance(schema_hash, str)
            or not _SHA256.fullmatch(schema_hash)
        ):
            raise ValueError("advertised_tools entry is invalid")
        seen.add(name)
        cleaned.append({"canonical_name": name, "schema_hash": schema_hash})
    cleaned.sort(key=lambda item: item["canonical_name"])
    calculated = hashlib.sha256(
        json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if calculated != supplied_hash:
        raise ValueError("advertised_tools.catalog_hash does not match entries")
    return {
        "contract": "advertised-tools/1",
        "catalog_complete": value["catalog_complete"],
        "catalog_hash": supplied_hash,
        "entries": cleaned,
    }


def _bearer(headers: Mapping[str, str]) -> str | None:
    value = headers.get("authorization") or headers.get("Authorization")
    if not isinstance(value, str) or not value.startswith("Bearer "):
        return None
    token = value[7:].strip()
    return token or None


class ControlApplication:
    """Pure request router used by the HTTP handler and unit tests."""

    def __init__(
        self,
        onboarding: ModuleOneOnboardingStore,
        *,
        owner_id: str,
        model_id: str,
        host_token: str,
        human_token: str,
        human_actor_id: str,
        direct_client_principal: str = "official-deepseek-direct",
        execution_store: ExecutionStore | None = None,
    ) -> None:
        if len(host_token) < 32 or len(human_token) < 32:
            raise ValueError("control-plane tokens must contain at least 32 characters")
        if hmac.compare_digest(host_token, human_token):
            raise ValueError("host and human tokens must be different")
        self.onboarding = onboarding
        self.owner_id = owner_id.strip()
        self.model_id = model_id.strip()
        self.host_token = host_token
        self.human_token = human_token
        self.human_actor_id = human_actor_id.strip()
        self.direct_client_principal = direct_client_principal.strip()
        self.execution_store = execution_store
        if (
            not self.owner_id
            or not self.model_id
            or not self.human_actor_id
            or not self.direct_client_principal
        ):
            raise ValueError(
                "owner_id, model_id, human_actor_id, and direct_client_principal "
                "must not be empty"
            )
        self.onboarding.ensure_state(owner_id=self.owner_id, model_id=self.model_id)

    @staticmethod
    def _response(status: int, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        return status, dict(payload)

    def _authorized(self, headers: Mapping[str, str], expected: str) -> bool:
        token = _bearer(headers)
        return token is not None and hmac.compare_digest(token, expected)

    def handle(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> tuple[int, dict[str, Any]]:
        """Handle one request without exposing HTTP-server implementation details."""
        headers = headers or {}
        route = urlsplit(path).path
        method = method.upper()

        if route == "/health" and method == "GET":
            state = self.onboarding.state(owner_id=self.owner_id, model_id=self.model_id)
            return self._response(
                200,
                {
                    "ok": True,
                    "module": state["module"],
                    "flow_version": state["flow_version"],
                    "stage": state["state"]["stage"],
                },
            )

        if method != "POST":
            return self._response(405, {"error": "method_not_allowed"})
        if len(body) > MAX_BODY_BYTES:
            return self._response(413, {"error": "request_too_large"})
        content_type = headers.get("content-type") or headers.get("Content-Type") or ""
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            return self._response(415, {"error": "application_json_required"})

        required_token = (
            self.host_token
            if route.startswith("/v1/host/")
            else self.human_token
            if route.startswith("/v1/human/")
            else None
        )
        if required_token is None:
            return self._response(404, {"error": "not_found"})
        if not self._authorized(headers, required_token):
            return self._response(401, {"error": "unauthorized"})

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._response(400, {"error": "invalid_utf8_json"})
        if not isinstance(payload, dict):
            return self._response(400, {"error": "json_object_required"})

        try:
            result = self._dispatch(route, payload)
        except ExecutionBindingError as exc:
            return self._response(409, {"error": str(exc)})
        except (ValueError, OnboardingError) as exc:
            return self._response(
                400,
                {"error": "invalid_request", "detail": str(exc)},
            )
        return self._response(200, result)

    def _dispatch(self, route: str, body: Mapping[str, Any]) -> dict[str, Any]:
        common = {"owner_id": self.owner_id, "model_id": self.model_id}
        if route.startswith("/v1/host/tool-executions/"):
            if self.execution_store is None:
                raise ExecutionBindingError("execution_binding_not_enabled")
            names = {"wake_id", "wake_capability", "batch_id", "revision", "deployment_epoch"}
            if route == "/v1/host/tool-executions/issue":
                names.add("calls")
            if set(body) != names or type(body.get("revision")) is not int or body["revision"] < 1:
                raise ExecutionBindingError("execution_request_invalid")
            if body["deployment_epoch"] != self.execution_store.deployment_epoch:
                raise ExecutionBindingError("execution_epoch_mismatch")
            arguments = {
                **common,
                "wake_id": _required_text(body, "wake_id"),
                "wake_capability": _required_text(body, "wake_capability"),
                "batch_id": _required_text(body, "batch_id"),
                "revision": body["revision"],
            }
            if route == "/v1/host/tool-executions/issue":
                return self.execution_store.issue_batch(**arguments, calls=body["calls"])
            if route == "/v1/host/tool-executions/status":
                return self.execution_store.batch_status(**arguments)
            if route == "/v1/host/tool-executions/revoke":
                return self.execution_store.revoke_batch(**arguments)
            raise ExecutionBindingError("execution_route_invalid")
        if route == "/v1/host/wakes":
            return self.onboarding.issue_wake(
                **common,
                host_id=_required_text(body, "host_id"),
                thread_id=_required_text(body, "thread_id"),
                source_kind=_required_text(body, "source_kind"),
                source_event_id=_required_text(body, "source_event_id"),
            )
        if route == "/v1/host/context/prepare":
            # Preserve absent vs null. Old Control ignores the new offer field;
            # new Control must never silently select a layout from two inputs.
            if "context_layout" in body and "context_layout_offer" in body:
                raise ValueError("context_layout_fields_conflict")
            layout_arguments = {
                key: body[key] for key in ("context_layout", "context_layout_offer") if key in body
            }
            # Absent means local situation selection; [] deliberately selects none.
            # Explicit null is malformed, not a request to change host policy.
            facet_names = body.get("facet_names")
            if "facet_names" in body and (not isinstance(facet_names, list) or not all(
                isinstance(item, str) for item in facet_names
            )):
                raise ValueError("facet_names must be an array of strings")
            return self.onboarding.build_pre_generation_context(
                **common,
                wake_id=_required_text(body, "wake_id"),
                wake_capability=_required_text(body, "wake_capability"),
                source_digest=_required_text(body, "source_digest"),
                host_contract_digest=_required_text(body, "host_contract_digest"),
                facet_names=facet_names,
                source_frame=_source_frame(body),
                advertised_tools=_advertised_tools(body),
                **layout_arguments,
            )
        if route == "/v1/host/context/confirm":
            return self.onboarding.confirm_context_injected(
                **common,
                wake_id=_required_text(body, "wake_id"),
                wake_capability=_required_text(body, "wake_capability"),
                context_hash=_required_text(body, "context_hash"),
            )
        if route == "/v1/host/context/close":
            return self.onboarding.close_context_snapshot(
                **common,
                wake_id=_required_text(body, "wake_id"),
                wake_capability=_required_text(body, "wake_capability"),
            )
        if route == "/v1/human/objections":
            return self.onboarding.record_human_objection(
                **common,
                candidate_id=_required_text(body, "candidate_id"),
                reason=_required_text(body, "reason"),
                release_condition=_required_text(body, "release_condition"),
                actor_id=self.human_actor_id,
                request_id=_required_text(body, "request_id"),
            )
        if route == "/v1/human/rollback":
            return self.onboarding.emergency_rollback(
                **common,
                target_revision_id=_required_text(body, "target_revision_id"),
                reason=_required_text(body, "reason"),
                actor_id=self.human_actor_id,
                request_id=_required_text(body, "request_id"),
            )
        if route == "/v1/human/direct-grants":
            request_id, requested_scopes = _direct_grant_request(body)
            issued = self.onboarding.issue_direct_grant(
                **common,
                actor_id=self.human_actor_id,
                client_principal=self.direct_client_principal,
                request_id=request_id,
                requested_scopes=requested_scopes,
            )
            scopes = issued.get("scopes", issued.get("authorized_scopes"))
            return {
                "grant_ref": issued.get("grant_ref"),
                "expires_at": issued.get("expires_at"),
                "scopes": scopes,
                "status": issued.get("status"),
            }
        raise ValueError("unknown route")


class _ControlHandler(BaseHTTPRequestHandler):
    server_version = "StillerBrainControl/0.1"

    def _handle(self) -> None:
        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError:
            length = -1
        if length < 0:
            status, payload = 400, {"error": "invalid_content_length"}
        elif length > MAX_BODY_BYTES:
            status, payload = 413, {"error": "request_too_large"}
        else:
            body = self.rfile.read(length) if length else b""
            status, payload = self.server.application.handle(  # type: ignore[attr-defined]
                self.command,
                self.path,
                dict(self.headers.items()),
                body,
            )
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, fmt: str, *args: Any) -> None:
        # Access logs are intentionally disabled: request bodies and credentials must
        # never reach logs. Operational monitoring should use the secret-free /health.
        return


def build_application_from_env() -> ControlApplication:
    database = Path(_required_env("STBRAIN_DB_PATH"))
    wake_secret = _required_env("STBRAIN_WAKE_SECRET")
    if len(wake_secret) < 32:
        raise RuntimeError("STBRAIN_WAKE_SECRET must contain at least 32 characters")
    emotional_store = EmotionalMemoryStore(database)
    learning_store = LearningMemoryStore(
        database,
        idea_database=os.environ.get(
            "STBRAIN_LEARNING_IDEA_DB_PATH",
            str(database.with_name(f"{database.stem}.learning-ideas.sqlite")),
        ),
    )
    tool_store = ToolGuidanceStore(database)
    planning_store = PlanningMemoryStore(database)
    injection_control_store = InjectionControlStore(database)
    vault_database = Path(
        os.environ.get(
            "STBRAIN_HALLUCINATION_VAULT_DB_PATH",
            str(
                database.with_name(
                    f"{database.stem}-hallucination-vault.sqlite3"
                )
            ),
        )
    )
    hallucination_store = HallucinationVaultStore(
        vault_database,
        source_adapters={"learning": LearningQuarantineAdapter(database)},
    )
    onboarding = ModuleOneOnboardingStore(
        database,
        ordinary_memory_independent=os.environ.get('STBRAIN_ACCESS_PROFILE', '') == 'simple-memory-v1',
        capability_secret=wake_secret,
        wake_ttl_seconds=int(os.environ.get("STBRAIN_WAKE_TTL_SECONDS", "1800")),
        edit_challenge_ttl_seconds=int(
            os.environ.get("STBRAIN_EDIT_CHALLENGE_TTL_SECONDS", "600")
        ),
        direct_grant_ttl_seconds=int(
            os.environ.get("STBRAIN_DIRECT_GRANT_TTL_SECONDS", "300")
        ),
        emotional_store=emotional_store,
        learning_store=learning_store,
        tool_store=tool_store,
        injection_control_store=injection_control_store,
        planning_store=planning_store,
        hallucination_vault=hallucination_store,
    )
    return ControlApplication(
        onboarding,
        owner_id=_required_env("STBRAIN_OWNER_ID"),
        model_id=_required_env("STBRAIN_MODEL_ID"),
        host_token=_required_env("STBRAIN_HOST_TOKEN"),
        human_token=_required_env("STBRAIN_HUMAN_TOKEN"),
        human_actor_id=_required_env("STBRAIN_HUMAN_ACTOR_ID"),
        direct_client_principal=os.environ.get(
            "STBRAIN_DIRECT_CLIENT_PRINCIPAL", "official-deepseek-direct"
        ),
        execution_store=(
            ExecutionStore(
                database,
                deployment_epoch=_required_env("STBRAIN_EXECUTION_EPOCH"),
                capability_secret=wake_secret,
            )
            if os.environ.get("STBRAIN_REQUIRE_EXECUTION_BINDING", "0") == "1" else None
        ),
    )


def main() -> None:
    host = os.environ.get("STBRAIN_CONTROL_HOST", "127.0.0.1")
    port = int(os.environ.get("STBRAIN_CONTROL_PORT", "8795"))
    if not 1 <= port <= 65535:
        raise RuntimeError("STBRAIN_CONTROL_PORT must be a valid TCP port")
    server = ThreadingHTTPServer((host, port), _ControlHandler)
    server.application = build_application_from_env()  # type: ignore[attr-defined]
    server.serve_forever()


if __name__ == "__main__":
    main()
