#!/usr/bin/env python3
"""OpenAI-compatible RikkaHub gateway with authenticated ST context injection.

The gateway is a host adapter, not an LLM and not an MCP server.  It opens a
real ST wake for a new user turn and confirms either the legacy exact message
or the versioned ordered context bundle. Exact authored role/content frames
are injected only at their authenticated positions before provider forwarding.

Tool-call continuations deliberately reuse the same wake.  Therefore an AI
cannot turn an MCP tool result into a fake cross-wake boundary for activating a
self-model candidate.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from .tool_execution import (
    EXECUTION_BINDING_CONTRACT,
    HOST_RECEIPT_CONTRACT,
    HostExecutionBoundary,
    NativeToolCall,
    ToolExecutionBinding,
    ToolExecutionBoundaryError,
    ToolExecutionPolicyAdapter,
    advertised_schemas,
    canonical_hash,
    normalize_validation_diagnostic,
    parse_arguments,
)
from runtime.execution_binding import EXECUTION_CONTRACT, EXECUTION_TOOLS
from runtime.compact_tool_routes import MANAGE_TOOL, CompactRouteError, resolve_compact_action


CONTEXT_MARKER = "STILLER_BRAIN_PRE_GENERATION_CONTEXT_V1\n"
CONTEXT_LAYOUT_CONTRACT = "stbrain-context-layout/1"
TAIL_CONTEXT_LAYOUT_CONTRACT = "stbrain-context-layout/2"
# DeepSeek Vision accepts request bodies up to 48 MiB.  Keep requests bounded
# at that same limit so long, image-bearing RikkaHub histories are not rejected
# locally before the provider can apply its own 1M-token context rules.  Keep
# response/SSE buffering on its smaller historical bound: request capacity must
# not silently multiply the protected-output scan buffer.
MAX_REQUEST_BODY_BYTES = 48 * 1024 * 1024
DEFAULT_MAX_REQUEST_BODY_BYTES = MAX_REQUEST_BODY_BYTES
DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024
# SSE wire framing repeats JSON metadata for every token.  Its cumulative
# transfer limit is distinct from the bounded event/quarantine/tool-tail memory
# buffers below; a long, progressively delivered answer is not a 2 MiB object.
DEFAULT_MAX_STREAM_BYTES = 64 * 1024 * 1024
DIRECT_GRANT_ROUTE = "/v1/human/direct-grants"
MAX_DIRECT_GRANT_BODY_BYTES = 64 * 1024
PROTECTED_TOOL_RESULT_KEYS = frozenset({"wake_capability", "challenge_response"})
ADVERTISED_TOOLS_CONTRACT = "advertised-tools/1"
MAX_ADVERTISED_TOOLS = 512
MAX_RETIRED_TOOL_CALL_IDS = 4096
_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_FAILURE_LOGGER = logging.getLogger("stiller.rikkahub.gateway")
_PERFORMANCE_LOGGER = logging.getLogger("stiller.rikkahub.performance")
_STREAM_FAILURE_MESSAGES = {
    "upstream_incomplete_stream": "上游连接在回复完成前结束，本轮未完成。请重试。",
    "upstream_empty_completion": "模型只返回了思考过程，尚未生成正文或工具调用，本轮未完成。请重试。",
    "upstream_output_limit_reached": "模型用完了本轮输出额度，尚未生成正文。请调高模型输出上限后重试。",
    "upstream_stream_limit_reached": "本轮流式传输已达到 64 MiB 上限或配置的更低上限。请缩小本轮任务后重试。",
    "upstream_buffer_limit_reached": "本轮某个待检查片段超过了缓冲上限。请缩小本轮内容后重试。",
}
# Only internal, value-free codes may enter the small diagnostic log or a late
# SSE error.  Exception details, arbitrary adapter text and source schemas are
# never logged.  Unknown future codes remain a generic, safe failure.
_OBSERVABLE_FAILURE_CODES = frozenset(
    {
        "advertised_tool_catalog_incomplete",
        "advertised_tool_schema_invalid",
        "advertised_tool_schema_recursion_unsupported",
        "advertised_tool_schema_reference_unresolved",
        "advertised_tool_schema_remote_ref_forbidden",
        "client_disconnected",
        "gateway_request_failed",
        "human_turn_in_progress",
        "legacy_function_call_not_receiptable",
        "tool_arguments_duplicate_key",
        "tool_arguments_invalid_json",
        "tool_arguments_must_be_json_string",
        "tool_arguments_must_be_object",
        "tool_arguments_non_finite_number",
        "tool_arguments_not_canonicalizable",
        "tool_arguments_schema_invalid",
        "tool_call_id_required",
        "tool_call_id_reused",
        "tool_call_name_invalid",
        "tool_continuation_binding_mismatch",
        "tool_continuation_catalog_incomplete",
        "tool_continuation_catalog_mismatch",
        "tool_continuation_catalog_missing",
        "tool_continuation_context_lost",
        "tool_continuation_lineage_invalid",
        "tool_continuation_lineage_mismatch",
        "tool_continuation_lineage_missing",
        "tool_continuation_settled_replay_mismatch",
        "tool_continuation_thread_mismatch",
        "tool_execution_confirmation_required",
        "tool_execution_policy_denied",
        "tool_not_advertised",
        "tool_schema_hash_mismatch",
        "tool_schema_not_available",
        "upstream_error",
        "upstream_invalid_response",
        "upstream_invalid_stream",
        "upstream_protected_value",
        "upstream_response_too_large",
        "upstream_stream_limit_reached",
        "upstream_buffer_limit_reached",
        "upstream_incomplete_stream",
        "upstream_empty_completion",
        "upstream_output_limit_reached",
        "upstream_tool_calls_invalid",
        "upstream_tool_calls_missing",
        "upstream_unavailable",
        "execution_binding_required",
        "execution_reference_model_supplied",
        "execution_reference_invalid",
        "execution_batch_invalid",
        "tool_wait_recovery_in_progress",
        "st_context_bundle_invalid",
        "st_context_bundle_binding_mismatch",
        "st_context_hash_mismatch",
        "tool_continuation_context_history_mismatch",
    }
)


def _observable_failure_code(code: str) -> str:
    return code if code in _OBSERVABLE_FAILURE_CODES else "gateway_request_failed"


def _log_gateway_failure(
    code: str, *, streamed_prefix: bool, protected_values: Sequence[str] = (),
    validation_diagnostic: Mapping[str, Any] | None = None,
) -> None:
    """Emit no prompt, schema, argument, tool result, URL or credential data."""

    record = {
        "event": "gateway_request_failed",
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "code": _observable_failure_code(code),
        "streamed_prefix": streamed_prefix,
    }
    if code == "tool_arguments_schema_invalid" and validation_diagnostic is not None:
        diagnostic = _safe_validation_diagnostic(validation_diagnostic, protected_values=protected_values)
        if diagnostic is not None:
            record["validation"] = diagnostic
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
    if any(value and value in encoded for value in protected_values):
        # Even a contrived legacy capability equal to a fixed diagnostic code
        # must not escape into a log.  Real random capabilities cannot normally
        # collide with these constants.
        return
    _FAILURE_LOGGER.warning("%s", encoded)


def _safe_validation_diagnostic(
    value: Any, *, protected_values: Sequence[str] = (),
    allowed_tool_names: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """Re-project value-free metadata; catalog membership is checked at binding."""
    diagnostic = normalize_validation_diagnostic(value, allowed_tool_names=allowed_tool_names)
    if diagnostic is None:
        return None
    encoded = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
    if any(value and value in encoded for value in protected_values):
        return None
    return diagnostic


def _tool_validation_message(
    diagnostic: Mapping[str, Any] | None, *, protected_values: Sequence[str] = (),
) -> str:
    message = "[ST 网关 · tool_arguments_schema_invalid] 本批工具参数与当前工具说明不匹配。"
    if diagnostic is not None:
        message += "工具：" + diagnostic["tool_name"] + "；"
        path = ".".join(diagnostic["field_path"]) or "参数对象"
        message += "字段：" + path + "；检查：" + diagnostic["validator"] + "。"
        if diagnostic.get("required_fields"):
            message += "缺少字段：" + "、".join(diagnostic["required_fields"]) + "。"
        if "unexpected_fields" in diagnostic:
            field_details = (
                "未被当前字段表接受的键：" + "、".join(diagnostic["unexpected_fields"])
                + "；共 " + str(diagnostic["unexpected_count"]) + " 个，其中 "
                + str(diagnostic["unknown_count"]) + " 个以 <field> 隐去。"
            )
            if diagnostic["unexpected_fields_truncated"]:
                field_details += "多余键的公开标签仅显示前几项。"
            field_details += "当前字段表共允许 " + str(diagnostic["allowed_field_count"]) + " 个键。"
            if diagnostic["allowed_fields"]:
                field_details += "可公开的允许键：" + "、".join(diagnostic["allowed_fields"]) + "。"
            if diagnostic["allowed_fields_truncated"]:
                field_details += "允许键列表已省略超限或不可公开的名称。"
            if not any(value and value in field_details for value in protected_values):
                message += field_details
        # Fixed correction text only. Never interpolate a rejected key, value,
        # schema description, or jsonschema's raw error into the explanation.
        tool = diagnostic["tool_name"].rsplit("__", 1)[-1]
        field = diagnostic["field_path"]
        hint = ""
        if diagnostic["validator"] == "additionalProperties":
            if tool == "preview_person_reference_rewrite" and field == ["draft_fields", "<field>"]:
                hint = (
                    "draft_fields 使用带 / 的字段名。情感字段片段示例："
                    '{"/original_text":"待写正文","/summary":"摘要"}。'
                    "学习 content 对应 /current_understanding；工具卡按说明书使用 /purpose、/call_notes 等适用路径。"
                )
            elif tool == "confirm_person_reference_rewrite" and field == ["final_fields", "<field>"]:
                hint = (
                    "final_fields 保留本次预览 suggested_fields 的带 / 字段名及确认文本，"
                    "例如情感 /original_text、/summary；final_fields_hash 按完整对象计算。草稿改变时重新预览。"
                )
        if not any(value and value in hint for value in protected_values):
            message += hint
    return message + "本批调用尚未交给客户端执行。请按工具说明修正参数后再试。"


def _tool_not_advertised_message(protected_values: Sequence[str] = ()) -> str:
    # The rejected name is model-authored and may itself contain private data.
    # Give an actionable, fixed explanation without echoing names or arguments.
    message = (
        "[ST 网关 · tool_not_advertised] 模型调用了本轮工具目录没有的名字。"
        "本批调用尚未交给客户端执行。请刷新工具目录，按当前目录原样使用工具名，"
        "再从新的用户消息继续。历史工具名称不代表当前可调用。"
    )
    if any(value and value in message for value in protected_values):
        return "tool_not_advertised"
    return message


_LINEAGE_DIAGNOSTIC_REASONS = frozenset(
    {
        "declaration_missing",
        "declaration_invalid",
        "tail_not_tool_results",
        "result_id_missing",
        "result_id_duplicate",
        "result_id_noncanonical",
        "declaration_result_set_mismatch",
        "current_batch_order_mismatch",
        "settled_prefix_order_mismatch",
        "expected_id_set_mismatch",
    }
)


def _lineage_error(
    code: str,
    reason: str,
    *,
    protected_values: Sequence[str] = (),
    declared_count: int | None = None,
    result_count: int | None = None,
    missing_result_count: int | None = None,
    unexpected_result_count: int | None = None,
    expected_count: int | None = None,
    terminal_declared_count: int | None = None,
    segment_declared_count: int | None = None,
    segment_result_count: int | None = None,
) -> GatewayError:
    """Record a fixed branch and bounded counts; never accept payload values."""

    record: dict[str, Any] = {
        "event": "tool_continuation_rejected",
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "code": _observable_failure_code(code),
        "reason": (
            reason if reason in _LINEAGE_DIAGNOSTIC_REASONS
            else "lineage_validation_failed"
        ),
    }
    for key, value in (
        ("declared_count", declared_count),
        ("result_count", result_count),
        ("missing_result_count", missing_result_count),
        ("unexpected_result_count", unexpected_result_count),
        ("expected_count", expected_count),
        ("terminal_declared_count", terminal_declared_count),
        ("segment_declared_count", segment_declared_count),
        ("segment_result_count", segment_result_count),
    ):
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            record[key] = min(value, MAX_RETIRED_TOOL_CALL_IDS)
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
    if not any(value and value in encoded for value in protected_values):
        _FAILURE_LOGGER.warning("%s", encoded)
    return GatewayError(409, code)


@dataclass
class _RequestPerformance:
    """One value-free timing record for an authorized chat request.

    Only the fixed fields assembled by :meth:`emit` are logged. In
    particular, this object never accepts request text, headers, URLs, model
    output, tool definitions/arguments, credentials, wake capabilities or
    hashes. ``stage`` is assigned only from constants in this module.
    """

    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float = field(default_factory=time.perf_counter)
    request_started_at_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    observed_model: str | None = field(default=None, repr=False)
    continuation: bool | None = None
    usage_observed: bool = False
    usage_snapshot: dict[str, int] = field(default_factory=dict, repr=False)
    usage_snapshot_invalid_fields: list[str] = field(default_factory=list, repr=False)
    finish_reasons: list[str] = field(default_factory=list)
    # Not known until authenticated prepare/confirm completes. Configured mode
    # is an offer, not evidence of what an older Control actually accepted.
    context_layout: str = "unknown"
    stable_context_bytes: int = 0
    dynamic_context_bytes: int = 0
    stable_context_changed: bool | None = None
    dynamic_context_changed: bool | None = None
    wire_tools_changed: bool | None = None
    client_input_prefix_preserved: bool | None = None
    stage: str = "body_validation"
    failed: bool = False
    emitted: bool = False
    stream: bool = False
    full_buffer: bool = False
    tool_tail: bool = False
    body_bytes: int = 0
    message_count: int = 0
    tool_count: int = 0
    injected_message_bytes: int = 0
    upstream_request_bytes: int = 0
    upstream_response_bytes: int = 0
    client_response_bytes: int = 0
    http_status: int | None = None
    upstream_status: int | None = None
    body_read_ms: float | None = None
    json_parse_ms: float | None = None
    prepare_turn_ms: float | None = None
    upstream_header_ms: float | None = None
    upstream_first_chunk_ms: float | None = None
    client_first_byte_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    _upstream_started_at: float | None = field(default=None, repr=False)
    _upstream_finished_at: float | None = field(default=None, repr=False)

    @staticmethod
    def _milliseconds(started_at: float, ended_at: float | None = None) -> float:
        end = time.perf_counter() if ended_at is None else ended_at
        return round(max(0.0, end - started_at) * 1000.0, 3)

    def start_upstream(self) -> None:
        self.stage = "upstream_headers"
        self._upstream_started_at = time.perf_counter()

    def note_upstream_headers(self, status: int) -> None:
        self.upstream_status = int(status)
        if self._upstream_started_at is not None:
            self.upstream_header_ms = self._milliseconds(self._upstream_started_at)
        self.stage = "upstream_body"

    def note_upstream_chunk(self, size: int) -> None:
        if self.upstream_first_chunk_ms is None and self._upstream_started_at is not None:
            self.upstream_first_chunk_ms = self._milliseconds(self._upstream_started_at)
        self.upstream_response_bytes += max(0, int(size))

    def finish_upstream(self) -> None:
        if self._upstream_started_at is not None and self._upstream_finished_at is None:
            self._upstream_finished_at = time.perf_counter()

    def note_client_bytes(self, size: int) -> None:
        if self.client_first_byte_ms is None:
            self.client_first_byte_ms = self._milliseconds(self.started_at)
        self.client_response_bytes += max(0, int(size))

    def note_model(self, value: Any) -> None:
        """Accept only fixed public model labels, never arbitrary config text."""
        allowed = {
            "deepseek-v4-flash-vision-exp", "deepseek-v4-flash", "deepseek-v4-pro",
        }
        self.observed_model = value if type(value) is str and value in allowed else None

    def note_usage(self, payload: Any) -> None:
        """Copy bounded counters and fixed completion labels, never provider prose."""

        if not isinstance(payload, Mapping):
            return
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices[:16]:
                reason = choice.get("finish_reason") if isinstance(choice, Mapping) else None
                if reason is None:
                    continue
                label = reason if type(reason) is str and reason in {
                    "stop", "length", "tool_calls", "function_call", "content_filter",
                } else "other"
                if label not in self.finish_reasons and len(self.finish_reasons) < 6:
                    self.finish_reasons.append(label)
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            return
        # Snapshot one provider object; never manufacture billing counts by
        # merging incomplete usage objects from separate SSE events.
        self.usage_observed = True
        self.usage_snapshot = {}
        self.usage_snapshot_invalid_fields = []
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        ):
            value = usage.get(key)
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= 10_000_000
            ):
                setattr(self, key, value)
                self.usage_snapshot[key] = value
            elif key in usage:
                self.usage_snapshot_invalid_fields.append(key)

    def mark_failed(self) -> None:
        self.failed = True

    def emit(self) -> None:
        if self.emitted:
            return
        self.emitted = True
        upstream_total_ms = (
            self._milliseconds(
                self._upstream_started_at,
                self._upstream_finished_at,
            )
            if self._upstream_started_at is not None
            else None
        )
        record = {
            "event": (
                "gateway_request_performance_failure"
                if self.failed
                else "gateway_request_performance"
            ),
            "request_id": self.request_id,
            "observation_contract": "stbrain-anonymous-usage/1",
            "request_started_at_utc": self.request_started_at_utc,
            "request_finished_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "model": self.observed_model,
            "model_is_known": self.observed_model is not None,
            "continuation": self.continuation,
            "context_layout": self.context_layout,
            "stable_context_bytes": self.stable_context_bytes,
            "dynamic_context_bytes": self.dynamic_context_bytes,
            "stable_context_changed": self.stable_context_changed,
            "dynamic_context_changed": self.dynamic_context_changed,
            "wire_tools_changed": self.wire_tools_changed,
            # Client input only, before ST injection and execution projection;
            # this is not a full upstream prefix or provider cache-hit metric.
            "client_input_prefix_preserved": self.client_input_prefix_preserved,
            "usage_observed": self.usage_observed,
            "finish_reasons": list(self.finish_reasons),
            "usage_snapshot": dict(self.usage_snapshot),
            "usage_snapshot_complete": len(self.usage_snapshot) == 4,
            "usage_snapshot_consistent": (
                self.usage_snapshot["prompt_tokens"] == self.usage_snapshot["prompt_cache_hit_tokens"] + self.usage_snapshot["prompt_cache_miss_tokens"]
                if len(self.usage_snapshot) == 4 else None
            ),
            "usage_snapshot_invalid_fields": list(self.usage_snapshot_invalid_fields),
            "stage": self.stage if self.failed else "complete",
            "stream": self.stream,
            "full_buffer": self.full_buffer,
            "tool_tail": self.tool_tail,
            "body_bytes": self.body_bytes,
            "message_count": self.message_count,
            "tool_count": self.tool_count,
            "injected_message_bytes": self.injected_message_bytes,
            "upstream_request_bytes": self.upstream_request_bytes,
            "upstream_response_bytes": self.upstream_response_bytes,
            "client_response_bytes": self.client_response_bytes,
            "http_status": self.http_status,
            "upstream_status": self.upstream_status,
            "body_read_ms": self.body_read_ms,
            "json_parse_ms": self.json_parse_ms,
            "prepare_turn_ms": self.prepare_turn_ms,
            "upstream_header_ms": self.upstream_header_ms,
            "upstream_first_chunk_ms": self.upstream_first_chunk_ms,
            "client_first_byte_ms": self.client_first_byte_ms,
            "upstream_total_ms": upstream_total_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "prompt_cache_hit_tokens": self.prompt_cache_hit_tokens,
            "prompt_cache_miss_tokens": self.prompt_cache_miss_tokens,
            "total_ms": self._milliseconds(self.started_at),
        }
        _PERFORMANCE_LOGGER.info(
            "%s", json.dumps(record, sort_keys=True, separators=(",", ":"))
        )


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _wake_expiry(value: Any) -> datetime:
    """Parse the control-plane wake deadline without guessing a timezone."""

    if not isinstance(value, str) or not value.strip():
        raise GatewayError(502, "st_wake_expiry_invalid")
    encoded = value.strip()
    if encoded.endswith("Z"):
        encoded = encoded[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(encoded)
        offset = parsed.utcoffset()
    except (OverflowError, TypeError, ValueError) as exc:
        raise GatewayError(502, "st_wake_expiry_invalid") from exc
    if parsed.tzinfo is None or offset is None:
        raise GatewayError(502, "st_wake_expiry_invalid")
    return parsed.astimezone(timezone.utc)


def _advertised_tool_catalog(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the host-authenticated tool catalog from the forwarded request.

    Only names and hashes cross into the ST control plane. The original full
    ``tools``/``functions`` values remain the host's validation authority. A
    separate upstream-only projection hides reserved host execution metadata.
    A malformed, duplicate, unsupported, or oversized catalog is represented
    as incomplete instead of being silently treated as an authoritative list.
    """

    entries: list[dict[str, str]] = []
    complete = True
    definitions: list[Any] = []

    if "tools" in payload:
        tools = payload.get("tools")
        if not isinstance(tools, list):
            complete = False
        else:
            for tool in tools:
                if (
                    not isinstance(tool, Mapping)
                    or tool.get("type") != "function"
                    or not isinstance(tool.get("function"), Mapping)
                ):
                    complete = False
                    continue
                definitions.append(tool["function"])

    if "functions" in payload:
        functions = payload.get("functions")
        if not isinstance(functions, list):
            complete = False
        else:
            definitions.extend(functions)

    if len(definitions) > MAX_ADVERTISED_TOOLS:
        # Do not publish a truncated list that could make an omitted tool look
        # definitively unavailable.  An empty incomplete catalog means unknown.
        definitions = []
        complete = False

    seen: set[str] = set()
    for definition in definitions:
        if not isinstance(definition, Mapping):
            complete = False
            continue
        name = definition.get("name")
        parameters = definition.get("parameters")
        if (
            not isinstance(name, str)
            or _TOOL_NAME.fullmatch(name) is None
            or name in seen
            or not isinstance(parameters, Mapping)
        ):
            complete = False
            continue
        try:
            # Parameters are the executable invocation contract.  Descriptions
            # are deliberately excluded so prose-only edits do not stale cards.
            encoded_schema = json.dumps(
                parameters,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            complete = False
            continue
        seen.add(name)
        entries.append(
            {
                "canonical_name": name,
                "schema_hash": hashlib.sha256(
                    encoded_schema.encode("utf-8")
                ).hexdigest(),
            }
        )

    entries.sort(key=lambda item: item["canonical_name"])
    return {
        "contract": ADVERTISED_TOOLS_CONTRACT,
        "catalog_complete": complete,
        "catalog_hash": _digest(entries),
        "entries": entries,
    }


def _extract_protected_tool_values(value: Any) -> set[str]:
    """Collect capability-like values from native MCP tool results.

    RikkaHub normally serializes a tool result into the ``content`` string of a
    role=tool message. Parse only JSON objects/arrays found there and never log
    or return the collected values.
    """

    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                key in PROTECTED_TOOL_RESULT_KEYS
                and isinstance(item, str)
                and item
            ):
                found.add(item)
            found.update(_extract_protected_tool_values(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_extract_protected_tool_values(item))
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                decoded = json.loads(stripped)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, (dict, list)):
                found.update(_extract_protected_tool_values(decoded))
    return found


def _contains_protected_value(value: Any, protected_values: Sequence[str]) -> bool:
    if isinstance(value, str):
        return any(secret and secret in value for secret in protected_values)
    if isinstance(value, Mapping):
        return any(
            _contains_protected_value(key, protected_values)
            or _contains_protected_value(item, protected_values)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(
            _contains_protected_value(item, protected_values) for item in value
        )
    return False


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _normalize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a case-insensitive, whitespace-trimmed HTTP header view.

    Real HTTP clients are free to change field-name casing.  Converting an
    ``HTTPMessage`` to a plain dict and then looking up one or two spellings is
    therefore not sufficient (urllib commonly sends ``X-St-Thread-Id``).
    """

    normalized: dict[str, str] = {}
    for name, value in headers.items():
        if isinstance(name, str) and isinstance(value, str):
            normalized[name.strip().lower()] = value.strip()
    return normalized


def _bearer(headers: Mapping[str, str]) -> str | None:
    value = _normalize_headers(headers).get("authorization")
    if not isinstance(value, str) or not value.startswith("Bearer "):
        return None
    token = value[7:].strip()
    return token or None


class GatewayError(RuntimeError):
    """A client-safe gateway error with an HTTP status and stable code."""

    def __init__(self, status: int, code: str, detail: str = "", *,
                 validation_diagnostic: Mapping[str, Any] | None = None) -> None:
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail or code
        self.validation_diagnostic = (
            normalize_validation_diagnostic(validation_diagnostic)
            if code == "tool_arguments_schema_invalid" else None
        )

    def payload(self) -> dict[str, Any]:
        result = {
            "error": {
                "message": self.detail,
                "type": "stiller_gateway_error",
                "code": self.code,
            }
        }
        diagnostic = normalize_validation_diagnostic(self.validation_diagnostic)
        if diagnostic is not None:
            result["error"]["validation"] = diagnostic
        return result


@dataclass(frozen=True)
class GatewayConfig:
    gateway_token: str
    control_url: str
    host_token: str
    upstream_base_url: str
    upstream_api_key: str
    public_model: str
    upstream_model: str
    host_id: str = "rikkahub-gateway"
    host_contract_digest: str = "rikkahub-openai-gateway/2"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8796
    timeout_seconds: float = 120.0
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    max_stream_bytes: int = DEFAULT_MAX_STREAM_BYTES
    max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES
    human_token: str = ""
    max_direct_grant_body_bytes: int = MAX_DIRECT_GRANT_BODY_BYTES
    require_execution_binding: bool = False
    execution_epoch: str = ""
    abnormal_wait_seconds: float = 180.0
    tool_result_wait_seconds: float = 300.0
    context_layout: str = "legacy"

    def __post_init__(self) -> None:
        if self.context_layout not in {"legacy", "anchored-v1", "tail-context-v2"}:
            raise ValueError("context layout must be legacy, anchored-v1 or tail-context-v2")
        if self.require_execution_binding and not self.execution_epoch.strip():
            raise ValueError("execution epoch is required for strict execution binding")
        if not 0 < self.abnormal_wait_seconds <= 180:
            raise ValueError("abnormal wait must be positive and at most 180 seconds")
        if (type(self.tool_result_wait_seconds) not in {int, float}
                or not math.isfinite(self.tool_result_wait_seconds)
                or not 0 < self.tool_result_wait_seconds <= threading.TIMEOUT_MAX):
            raise ValueError("tool result wait must be finite, positive and within the platform timer limit")
        if len(self.gateway_token) < 32 or len(self.host_token) < 32:
            raise ValueError("gateway and host tokens must contain at least 32 characters")
        if self.human_token and len(self.human_token) < 32:
            raise ValueError("human token must contain at least 32 characters")
        credentials = [
            self.gateway_token,
            self.host_token,
            self.upstream_api_key,
        ]
        if self.human_token:
            credentials.append(self.human_token)
        for index, left in enumerate(credentials):
            for right in credentials[index + 1 :]:
                if hmac.compare_digest(left, right):
                    raise ValueError(
                        "gateway, host, upstream, and human credentials must be pairwise distinct"
                    )
        if not 1 <= self.bind_port <= 65535:
            raise ValueError("bind_port must be a valid TCP port")
        if self.max_body_bytes < 1024:
            raise ValueError("max_body_bytes is too small")
        if not 1024 <= self.max_stream_bytes <= DEFAULT_MAX_STREAM_BYTES:
            raise ValueError("max_stream_bytes must be between 1 KiB and 64 MiB")
        if self.max_request_body_bytes < 1024:
            raise ValueError("max_request_body_bytes is too small")
        if self.max_request_body_bytes > MAX_REQUEST_BODY_BYTES:
            raise ValueError(
                "max_request_body_bytes exceeds the reviewed 48 MiB boundary"
            )
        if not 1024 <= self.max_direct_grant_body_bytes <= MAX_DIRECT_GRANT_BODY_BYTES:
            raise ValueError(
                "max_direct_grant_body_bytes must be between 1 KiB and 64 KiB"
            )
        for name in (
            "control_url",
            "upstream_base_url",
            "upstream_api_key",
            "public_model",
            "upstream_model",
            "host_id",
            "host_contract_digest",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        return cls(
            gateway_token=_required_env("STBRAIN_GATEWAY_TOKEN"),
            control_url=_required_env("STBRAIN_CONTROL_URL"),
            host_token=_required_env("STBRAIN_HOST_TOKEN"),
            upstream_base_url=_required_env("STBRAIN_UPSTREAM_BASE_URL"),
            upstream_api_key=_required_env("STBRAIN_UPSTREAM_API_KEY"),
            public_model=_required_env("STBRAIN_GATEWAY_MODEL"),
            upstream_model=_required_env("STBRAIN_UPSTREAM_MODEL"),
            host_id=os.environ.get("STBRAIN_GATEWAY_HOST_ID", "rikkahub-gateway"),
            host_contract_digest=os.environ.get(
                "STBRAIN_GATEWAY_CONTRACT", "rikkahub-openai-gateway/2"
            ),
            bind_host=os.environ.get("STBRAIN_GATEWAY_HOST", "127.0.0.1"),
            bind_port=int(os.environ.get("STBRAIN_GATEWAY_PORT", "8796")),
            timeout_seconds=float(
                os.environ.get("STBRAIN_GATEWAY_TIMEOUT_SECONDS", "120")
            ),
            max_body_bytes=int(
                os.environ.get(
                    "STBRAIN_GATEWAY_MAX_BODY_BYTES",
                    str(DEFAULT_MAX_BODY_BYTES),
                )
            ),
            max_stream_bytes=int(os.environ.get(
                "STBRAIN_GATEWAY_MAX_STREAM_BYTES", str(DEFAULT_MAX_STREAM_BYTES)
            )),
            max_request_body_bytes=int(
                os.environ.get(
                    "STBRAIN_GATEWAY_MAX_REQUEST_BODY_BYTES",
                    str(DEFAULT_MAX_REQUEST_BODY_BYTES),
                )
            ),
            human_token=_required_env("STBRAIN_HUMAN_TOKEN"),
            max_direct_grant_body_bytes=int(
                os.environ.get(
                    "STBRAIN_DIRECT_GRANT_MAX_BODY_BYTES",
                    str(MAX_DIRECT_GRANT_BODY_BYTES),
                )
            ),
            require_execution_binding=os.environ.get("STBRAIN_REQUIRE_EXECUTION_BINDING", "0") == "1",
            execution_epoch=os.environ.get("STBRAIN_EXECUTION_EPOCH", ""),
            tool_result_wait_seconds=float(os.environ.get("STBRAIN_GATEWAY_TOOL_RESULT_WAIT_SECONDS", "300")),
            context_layout=os.environ.get("STBRAIN_GATEWAY_CONTEXT_LAYOUT", "legacy"),
        )


class ControlClient:
    """Minimal authenticated client for the separate ST host control plane."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        # The control plane is a loopback/private service selected by the host,
        # never an Internet upstream.  Inheriting HTTP(S)_PROXY can route these
        # authenticated calls through a desktop proxy and turn a healthy local
        # Control service into opaque 502 responses.
        self.client = client or httpx.Client(timeout=timeout, trust_env=False)

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.post(
                self.base_url + path,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=dict(payload),
            )
        except httpx.RequestError as exc:
            raise GatewayError(502, "st_control_unavailable") from exc
        if response.status_code != 200:
            raise GatewayError(502, "st_control_rejected")
        try:
            result = response.json()
        except ValueError as exc:
            raise GatewayError(502, "st_control_invalid_response") from exc
        if not isinstance(result, dict):
            raise GatewayError(502, "st_control_invalid_response")
        return result


@dataclass
class TurnSession:
    thread_id: str
    wake_id: str
    wake_capability: str
    message: dict[str, str]
    context_hash: str
    source_digest: str
    advertised_tools: dict[str, Any]
    created_at: float
    expires_at: datetime
    tool_wait_armed_at: float | None = None
    tool_result_wait_deadline: float | None = None
    tool_result_wait_timer: threading.Timer | None = field(default=None, repr=False)
    expected_tool_call_ids: set[str] = field(default_factory=set)
    expected_tool_call_order: list[str] = field(default_factory=list)
    expected_tool_calls: dict[str, ToolExecutionBinding] = field(default_factory=dict)
    host_receipts: list[dict[str, Any]] = field(default_factory=list, repr=False)
    settled_tool_call_order: list[str] = field(default_factory=list, repr=False)
    seen_tool_call_ids: set[str] = field(default_factory=set, repr=False)
    protected_values: set[str] = field(default_factory=set, repr=False)
    execution_batch_id: str | None = field(default=None, repr=False)
    execution_revision: int = 0
    execution_batch_closed: bool = False
    execution_call_ids: set[str] = field(default_factory=set, repr=False)
    delivered_at_monotonic: float | None = None
    abnormal_wait_started: float | None = None
    recovery_timer: threading.Timer | None = field(default=None, repr=False)
    recovery_pending: bool = False
    recovery_audit_stage: str = ""
    abandon_requested: bool = False
    request_generation: int = 0
    history_anchor_count: int = 0
    history_anchor_digest: str = field(default="", repr=False)
    # Confirmed by authenticated Control as a complete ordered bundle. Metadata
    # and hashes stay in the host; only exact role/content frames reach a model.
    context_bundle: dict[str, Any] | None = field(default=None, repr=False)


@dataclass
class PreparedTurn:
    thread_id: str
    payload: dict[str, Any]
    session: TurnSession
    continuation: bool
    issued_tool_calls: dict[str, ToolExecutionBinding] = field(default_factory=dict)
    request_generation: int = 0
    cache_comparison: dict[str, bool | None] = field(default_factory=dict, repr=False)


class GatewayApplication:
    """Pure gateway orchestration shared by the HTTP handler and tests."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        control: ControlClient | Any | None = None,
        human_control: ControlClient | Any | None = None,
        upstream: httpx.Client | None = None,
        execution_policy: ToolExecutionPolicyAdapter | None = None,
    ) -> None:
        self.config = config
        self.control = control or ControlClient(
            config.control_url,
            config.host_token,
            timeout=min(config.timeout_seconds, 30.0),
        )
        self.human_control = human_control
        if self.human_control is None and config.human_token:
            self.human_control = ControlClient(
                config.control_url,
                config.human_token,
                timeout=min(config.timeout_seconds, 30.0),
            )
        self.upstream = upstream or httpx.Client(timeout=config.timeout_seconds)
        self.execution_boundary = HostExecutionBoundary(
            config.host_token.encode("utf-8"),
            policy=execution_policy,
        )
        # Module one has one authoritative current wake per owner/model.  Keep
        # the gateway transport state equally singular and bind continuations
        # to upstream-issued tool_call IDs, never to a guessed chat identity.
        self._current_session: TurnSession | None = None
        # One content-free comparison anchor in process memory, never persisted
        # or logged. Raw digests/identifiers never cross into telemetry; only
        # same-thread changed/unchanged facts do. No history text is retained.
        self._last_cache_comparison: dict[str, Any] | None = None
        # An expired tool result must not be rebound to a replacement wake even
        # if the client retries after the next human message has opened it.
        self._retired_tool_call_ids: set[str] = set()
        self._retired_tool_call_order: list[str] = []
        self._lock = threading.RLock()

    @staticmethod
    def _execution_tool(schema: Mapping[str, Any]) -> str | None:
        properties = schema.get("properties")
        marker = properties.get("execution_ref") if isinstance(properties, Mapping) else None
        if not isinstance(marker, Mapping):
            return None
        name = marker.get("x-stbrain-execution-tool")
        if marker.get("x-stbrain-execution-contract") != EXECUTION_CONTRACT or name not in EXECUTION_TOOLS:
            return None
        return str(name)

    @staticmethod
    def _insert_execution_ref(arguments: str, ref: str) -> str:
        values = parse_arguments(arguments)
        if "execution_ref" in values:
            raise GatewayError(502, "execution_reference_model_supplied")
        if not isinstance(ref, str) or re.fullmatch(r"stexec_[A-Za-z0-9_-]{43}", ref) is None:
            raise GatewayError(502, "execution_reference_invalid")
        position = arguments.rfind("}")
        if position < 0:
            raise GatewayError(502, "execution_reference_invalid")
        insertion = ("," if values else "") + '"execution_ref":' + json.dumps(ref)
        return arguments[:position] + insertion + arguments[position:]

    def decorate_execution_calls(self, prepared: PreparedTurn, calls: Sequence[NativeToolCall]) -> list[NativeToolCall]:
        """Add only a reserved host reference; business arguments remain byte-identical."""
        if not self.config.require_execution_binding:
            with self._lock:
                if not self._request_is_current(prepared):
                    raise GatewayError(409, "tool_continuation_context_lost")
            return list(calls)
        schemas = advertised_schemas(prepared.payload)
        managed = []
        for call in calls:
            schema = schemas.get(call.tool_name)
            if schema is None:
                raise GatewayError(
                    502, "tool_not_advertised",
                    _tool_not_advertised_message(tuple(prepared.session.protected_values)),
                )
            canonical_tool = self._execution_tool(schema)
            if canonical_tool is None:
                # Old canonical ST definitions cannot silently fall back to unbound mode.
                if call.tool_name in EXECUTION_TOOLS:
                    raise GatewayError(502, "execution_binding_required")
                continue
            arguments = parse_arguments(call.arguments_text)
            if "execution_ref" in arguments:
                raise GatewayError(502, "execution_reference_model_supplied")
            if canonical_tool == MANAGE_TOOL:
                # The transport receipt still binds the original outer call.
                # The executor lease binds the exact guarded child operation.
                # MCP repeats this deterministic route before claiming it.
                try:
                    canonical_tool, arguments = resolve_compact_action(arguments)
                except CompactRouteError:
                    raise GatewayError(502, "compact_action_invalid") from None
                if canonical_tool not in EXECUTION_TOOLS:
                    continue  # Health/help/password-grant paths have their own original guards.
            managed.append({
                "call_id": call.tool_call_id, "advertised_name": call.tool_name,
                "canonical_tool": canonical_tool, "schema_hash": canonical_hash(schema),
                "catalog_hash": prepared.session.advertised_tools["catalog_hash"],
                "arguments_hash": canonical_hash(arguments),
            })
        with self._lock:
            session = prepared.session
            if not self._request_is_current(prepared):
                raise GatewayError(409, "tool_continuation_context_lost")
            session.execution_revision += 1
            session.recovery_audit_stage = ""
            session.execution_batch_closed = False
            session.delivered_at_monotonic = None
            session.execution_call_ids = set()
            session.execution_batch_id = None
            if not managed:
                return list(calls)
            batch_id = "stbatch_" + uuid.uuid4().hex
            reply = self.control.post("/v1/host/tool-executions/issue", {
                "wake_id": session.wake_id, "wake_capability": session.wake_capability,
                "batch_id": batch_id, "revision": session.execution_revision, "calls": managed,
                "deployment_epoch": self.config.execution_epoch,
            })
            items = reply.get("executions")
            if (reply.get("batch_revision") != session.execution_revision or not isinstance(items, list)
                    or len(items) != len(managed) or any(not isinstance(item, Mapping) for item in items)):
                raise GatewayError(502, "execution_batch_invalid")
            if [item.get("call_id") for item in items] != [item["call_id"] for item in managed]:
                raise GatewayError(502, "execution_batch_invalid")
            refs = {item["call_id"]: item.get("execution_ref") for item in items}
            decorated = [NativeToolCall(call.tool_call_id, call.tool_name,
                         self._insert_execution_ref(call.arguments_text, refs[call.tool_call_id]))
                         if call.tool_call_id in refs else call for call in calls]
            session.execution_batch_id = batch_id
            session.execution_call_ids = set(refs)
            return decorated

    def mark_response_delivered(self, prepared: PreparedTurn) -> None:
        with self._lock:
            if (self._request_is_current(prepared) and prepared.session.expected_tool_call_ids
                    and prepared.session.delivered_at_monotonic is None):
                # Start only after the tool-call response was actually written,
                # never at the beginning of a long model generation. Repeated
                # delivery notifications cannot extend this batch's deadline.
                session = prepared.session
                session.delivered_at_monotonic = time.monotonic()
                session.tool_result_wait_deadline = session.delivered_at_monotonic + self.config.tool_result_wait_seconds
                self._schedule_tool_result_wait(session, self.config.tool_result_wait_seconds)

    def _schedule_tool_result_wait(self, session: TurnSession, delay: float) -> None:
        if session.tool_result_wait_timer is not None:
            session.tool_result_wait_timer.cancel()
        timer = threading.Timer(delay, self._recover_tool_result_wait,
                                (session, session.execution_revision, session.request_generation,
                                 session.tool_result_wait_deadline))
        timer.daemon = True
        session.tool_result_wait_timer = timer
        timer.start()

    def _retire_tool_result_wait(self, session: TurnSession) -> bool:
        """Confirmed transport retirement, not cancellation of external tools.

        Caller holds the app lock. Never replay the tool or accept a new wake
        while ST reports a running claim or Control cannot confirm closure.
        Uninstrumented external tools may still be running; their late result
        lineage is retired, but their real-world execution is not declared done.
        """
        if self._current_session is not session:
            return True
        try:
            if self.config.require_execution_binding and session.execution_batch_id:
                self._close_execution_session(session)
            else:
                closed = self.control.post("/v1/host/context/close", {
                    "wake_id": session.wake_id, "wake_capability": session.wake_capability})
                if closed.get("decision") != "closed":
                    raise GatewayError(409, "tool_wait_recovery_in_progress")
        except GatewayError:
            session.recovery_pending = True
            self._audit_recovery(session, "quarantined", "tool_result_wait_cleanup_pending")
            return False
        self._remember_retired_tool_calls(tuple(session.expected_tool_call_ids))
        self._audit_recovery(session, "retired", "tool_result_wait_timed_out")
        self._cancel_recovery_timer(session)
        session.request_generation += 1
        session.expected_tool_call_ids.clear()
        session.expected_tool_call_order.clear()
        session.expected_tool_calls.clear()
        session.tool_wait_armed_at = None
        self._current_session = None
        return True

    def _recover_tool_result_wait(self, session: TurnSession, revision: int,
                                  generation: int, deadline: float | None) -> None:
        with self._lock:
            if (self._current_session is not session or session.execution_revision != revision
                    or session.request_generation != generation
                    or deadline is None or session.tool_result_wait_deadline != deadline
                    or session.tool_wait_armed_at is None or not session.expected_tool_call_ids):
                return
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self._schedule_tool_result_wait(session, remaining)
                return
            if not self._retire_tool_result_wait(session):
                # The five-minute wait already elapsed. Retry cleanup shortly;
                # do not add the separate 180-second partial-result grace period.
                self._schedule_tool_result_wait(session, 5.0)

    def _request_is_current(self, prepared: PreparedTurn) -> bool:
        return (self._current_session is prepared.session
                and prepared.request_generation == prepared.session.request_generation)

    def assert_response_current(self, prepared: PreparedTurn) -> None:
        # Normal final responses close their wake before writing. They may finish
        # that write, but never write into a later generation or replacement.
        with self._lock:
            if (prepared.request_generation != prepared.session.request_generation
                    or (self._current_session is not None
                        and self._current_session is not prepared.session)):
                raise GatewayError(409, "tool_continuation_context_lost")

    def _anchored_new_human(self, session: TurnSession, messages: Sequence[Mapping[str, Any]],
                            payload: Mapping[str, Any], headers: Mapping[str, str],
                            catalog: Mapping[str, Any]) -> bool:
        """Identify an exact delivered execution branch, not a cancellation receipt.

        Rikka has no stop notification after a completed tool-call response. A
        subsequent human message may abandon that wait only when it carries the
        exact request history and signed current calls. Complete A/T units may
        contain a proper ordered subset (Rikka can omit an unexecuted tool card).
        Their results are NOT witnessed here, including local cancellation text.
        Missing declarations/results, changed history/catalog or another explicit
        thread fail closed. With no stable thread, an exact cloned branch is
        indistinguishable from the original physical window; no identity is guessed.
        """
        count = session.history_anchor_count
        if (not self.config.require_execution_binding
                or session.delivered_at_monotonic is None
                or not session.expected_tool_calls or not session.history_anchor_digest
                or len(messages) < 3 or messages[-1].get("role") != "user"
                or catalog != session.advertised_tools
                or (headers.get("x-st-thread-id")
                    and headers["x-st-thread-id"] != session.thread_id)):
            return False
        try:
            history = list(messages[:-1])
            # Parse only complete terminal A/T units; missing results are not
            # invented. Then remove CURRENT calls/results in a comparison-only
            # projection. This also accepts Rikka merging a new tool group into
            # the previous A(calls)/T group, without relaxing any older content.
            all_calls, results = self._bound_continuation_tool_call_batch(history)
            current_ids = session.expected_tool_call_ids
            calls = [call for call in all_calls if call.tool_call_id in current_ids]
            projected = []
            raw_current_order = []
            for message in history:
                if message.get("role") == "assistant" and message.get("tool_calls"):
                    if (message.get("function_call") is not None
                            or not isinstance(message["tool_calls"], list)):
                        return False
                    remaining = []
                    for raw in message["tool_calls"]:
                        if not isinstance(raw, Mapping) or not isinstance(raw.get("function"), Mapping):
                            return False
                        if raw.get("id") in current_ids:
                            raw_name = raw.get("function", {}).get("name")
                            if not isinstance(raw_name, str) or raw_name != raw_name.strip():
                                return False
                            raw_current_order.append(raw["id"])
                        else:
                            remaining.append(raw)
                    if len(remaining) != len(message["tool_calls"]):
                        if remaining:
                            projected.append({**message, "tool_calls": remaining})
                        continue
                if message.get("role") == "tool" and message.get("tool_call_id") in current_ids:
                    if "content" not in message or not isinstance(message["content"], (str, list)):
                        return False
                    continue
                projected.append(message)
            if len(projected) != count or _digest(projected) != session.history_anchor_digest:
                return False
            schemas = advertised_schemas(payload)
            ids = [call.tool_call_id for call in calls]
            if not ids or len(set(ids)) != len(ids) or ids != raw_current_order:
                return False
            if ids != [call_id for call_id in session.expected_tool_call_order if call_id in set(ids)]:
                return False
            for call in calls:
                self.execution_boundary.verify_history_call(
                    expected=session.expected_tool_calls[call.tool_call_id],
                    catalog=catalog, schemas=schemas, call=call)
        except (GatewayError, ToolExecutionBoundaryError, KeyError, TypeError, ValueError):
            return False
        return True

    def _abandon_delivered_wait(self, session: TurnSession) -> None:
        # Called under _lock after the branch proof. Issued ST refs are revoked;
        # running calls must drain. External tools remain execution-status unknown.
        # A new wake is never created on a best-effort/ambiguous close.
        try:
            if session.execution_batch_id:
                self._close_execution_session(session)
            else:
                closed = self.control.post("/v1/host/context/close", {
                    "wake_id": session.wake_id, "wake_capability": session.wake_capability})
                if closed.get("decision") != "closed":
                    raise GatewayError(409, "tool_wait_recovery_in_progress")
        except GatewayError as exc:
            session.recovery_pending = True
            self._audit_recovery(session, "quarantined", "new_human_close_confirmation_pending")
            # The human branch has already requested abandonment. Retry only
            # confirmed cleanup, never the user's tool or message, so another
            # failed-send user bubble cannot trap the lane behind its old anchor.
            self._cancel_recovery_timer(session)
            session.abandon_requested = True
            session.abnormal_wait_started = time.monotonic() - self.config.abnormal_wait_seconds
            self._schedule_recovery(session, 5.0)
            raise GatewayError(409, "tool_wait_recovery_in_progress",
                               "旧工具等待正在安全收尾；尚未确认执行结束，请稍后再发送。") from exc
        self._remember_retired_tool_calls(tuple(session.expected_tool_call_ids))
        self._audit_recovery(session, "retired", "anchored_new_human_abandoned_wait")
        self._cancel_recovery_timer(session)
        session.request_generation += 1
        session.expected_tool_call_ids.clear()
        session.expected_tool_call_order.clear()
        session.expected_tool_calls.clear()
        session.tool_wait_armed_at = None
        self._current_session = None

    @staticmethod
    def _cancel_recovery_timer(session: TurnSession) -> None:
        if session.recovery_timer is not None:
            session.recovery_timer.cancel()
        if session.tool_result_wait_timer is not None:
            session.tool_result_wait_timer.cancel()
        session.tool_result_wait_timer = None
        session.tool_result_wait_deadline = None
        session.recovery_timer = None
        session.abnormal_wait_started = None
        session.abandon_requested = False

    def _execution_request(self, session: TurnSession, operation: str) -> dict[str, Any]:
        return self.control.post("/v1/host/tool-executions/" + operation, {
            "wake_id": session.wake_id, "wake_capability": session.wake_capability,
            "batch_id": session.execution_batch_id, "revision": session.execution_revision,
            "deployment_epoch": self.config.execution_epoch,
        })

    def _close_execution_session(self, session: TurnSession) -> None:
        """Require a confirmed drain, then retry only the idempotent exact close.

        If close committed but its response was lost, the wake is already closed
        and registry status correctly refuses it. Remember the prior confirmed
        batch retirement locally instead of broadening the registry's current-
        wake gate. A new batch always clears this confirmation.
        """
        if not session.execution_batch_closed:
            status = self._execution_request(session, "revoke")
            counts = status.get("counts")
            if (status.get("batch_revision") != session.execution_revision
                    or status.get("batch_status") != "closed"
                    or not isinstance(counts, Mapping)
                    or type(counts.get("running")) is not int or counts["running"] != 0):
                raise GatewayError(409, "tool_wait_recovery_in_progress")
            session.execution_batch_closed = True
        closed = self.control.post("/v1/host/context/close", {
            "wake_id": session.wake_id, "wake_capability": session.wake_capability,
        })
        if closed.get("decision") != "closed":
            raise GatewayError(409, "tool_wait_recovery_in_progress")

    def _schedule_recovery(self, session: TurnSession, delay: float) -> None:
        revision = session.execution_revision
        timer = threading.Timer(delay, self._recover_abnormal_wait, (session, revision))
        timer.daemon = True
        session.recovery_timer = timer
        timer.start()

    @staticmethod
    def _audit_recovery(session: TurnSession, stage: str, reason: str, *, running_count: int | None = None) -> None:
        if session.recovery_audit_stage == stage:
            return
        record = {"event": "abnormal_tool_wait_recovery", "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                  "stage": stage, "reason": reason, "expected_count": len(session.expected_tool_call_ids),
                  "managed_count": len(session.execution_call_ids), "running_count": running_count}
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        if not any(value and value in encoded for value in session.protected_values):
            _FAILURE_LOGGER.warning("%s", encoded)
        session.recovery_audit_stage = stage

    def _mark_verified_partial(self, session: TurnSession, calls: Sequence[NativeToolCall],
                               results: Sequence[Mapping[str, Any]], payload: Mapping[str, Any]) -> None:
        if (not self.config.require_execution_binding or not session.execution_batch_id
                or session.delivered_at_monotonic is None or session.abnormal_wait_started is not None):
            return
        expected = session.expected_tool_call_ids
        # Rikka can replay a settled suffix before an incomplete later batch.
        # Split at the first current ID, then require the entire remaining tail
        # to be current. Never skip/reorder an unknown or interleaved old call.
        boundary = next((index for index, call in enumerate(calls)
                         if call.tool_call_id in expected), None)
        if boundary is None:
            return
        settled_calls = calls[:boundary]
        current_calls = calls[boundary:]
        settled_ids = [call.tool_call_id for call in settled_calls]
        if settled_ids and settled_ids != session.settled_tool_call_order[-len(settled_ids):]:
            return
        ids = [call.tool_call_id for call in current_calls]
        present = set(ids)
        if not present or not present < expected or expected - present - session.execution_call_ids:
            return  # Unknown/uninstrumented long-running tools are never timed out.
        if ids != [call_id for call_id in session.expected_tool_call_order if call_id in present]:
            return
        all_ids = [call.tool_call_id for call in calls]
        if (len(all_ids) != len(set(all_ids)) or len(results) != len(all_ids)
                or {item.get("tool_call_id") for item in results} != set(all_ids)):
            return
        try:
            schemas = advertised_schemas(payload)
            # Validate result structure without witnessing/consuming any subset.
            for item in results:
                if item.get("role") != "tool" or "content" not in item:
                    return
            by_id = {item["tool_call_id"]: item for item in results}
            for call in settled_calls:
                self._verify_settled_tool_replay(session=session, call=call,
                    tool_message=by_id[call.tool_call_id], schemas=schemas)
            for call in current_calls:
                self.execution_boundary.verify_history_call(expected=session.expected_tool_calls[call.tool_call_id],
                    catalog=session.advertised_tools, schemas=schemas, call=call)
        except (KeyError, ToolExecutionBoundaryError):
            return
        session.abnormal_wait_started = time.monotonic()
        self._audit_recovery(session, "waiting", "verified_incomplete_batch")
        self._schedule_recovery(session, self.config.abnormal_wait_seconds)

    def _recover_abnormal_wait(self, session: TurnSession, revision: int) -> None:
        with self._lock:
            if (self._current_session is not session or session.execution_revision != revision
                    or session.abnormal_wait_started is None
                    or not (session.execution_batch_id or session.abandon_requested)):
                return
            remaining = session.abnormal_wait_started + self.config.abnormal_wait_seconds - time.monotonic()
            if remaining > 0:
                self._schedule_recovery(session, remaining)
                return
            try:
                if session.execution_batch_id and not session.execution_batch_closed:
                    status = self._execution_request(session, "status")
                    counts = status.get("counts")
                    if (status.get("batch_revision") != session.execution_revision
                            or not isinstance(counts, Mapping) or type(counts.get("running")) is not int):
                        raise GatewayError(502, "execution_batch_invalid")
                    if counts["running"]:
                        self._audit_recovery(session, "deferred", "execution_calls_running", running_count=counts["running"])
                        self._schedule_recovery(session, 5.0)
                        return
                if session.execution_batch_id:
                    self._close_execution_session(session)
                else:
                    closed = self.control.post("/v1/host/context/close", {
                        "wake_id": session.wake_id, "wake_capability": session.wake_capability})
                    if closed.get("decision") != "closed":
                        raise GatewayError(409, "tool_wait_recovery_in_progress")
            except GatewayError:
                session.recovery_pending = True
                self._audit_recovery(session, "quarantined", "control_confirmation_pending")
                self._schedule_recovery(session, 5.0)
                return
            self._remember_retired_tool_calls(tuple(session.expected_tool_call_ids))
            self._audit_recovery(session, "retired", "abnormal_wait_closed", running_count=0)
            self._cancel_recovery_timer(session)
            session.request_generation += 1
            session.expected_tool_call_ids.clear()
            session.expected_tool_call_order.clear()
            session.expected_tool_calls.clear()
            session.tool_wait_armed_at = None
            self._current_session = None

    def authorized(self, headers: Mapping[str, str]) -> bool:
        token = _bearer(headers)
        return token is not None and hmac.compare_digest(
            token, self.config.gateway_token
        )

    def authorized_human(self, headers: Mapping[str, str]) -> bool:
        """Authenticate the human-only grant route with a distinct credential."""

        token = _bearer(headers)
        return (
            bool(self.config.human_token)
            and token is not None
            and hmac.compare_digest(token, self.config.human_token)
        )

    def issue_direct_grant(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Proxy one bounded human request and expose only the delivery fields."""

        if set(payload) != {"request_id", "requested_scopes"}:
            raise GatewayError(400, "invalid_direct_grant_request")
        request_id = payload.get("request_id")
        requested_scopes = payload.get("requested_scopes")
        if (
            not isinstance(request_id, str)
            or not request_id.strip()
            or len(request_id) > 64
            or not isinstance(requested_scopes, list)
            or not requested_scopes
            or len(requested_scopes) > 16
            or any(
                not isinstance(scope, str)
                or not scope.strip()
                or len(scope.strip()) > 64
                for scope in requested_scopes
            )
        ):
            raise GatewayError(400, "invalid_direct_grant_request")
        try:
            parsed_request_id = uuid.UUID(request_id)
        except (ValueError, AttributeError) as exc:
            raise GatewayError(400, "invalid_direct_grant_request") from exc
        cleaned_scopes = [scope.strip() for scope in requested_scopes]
        if (
            parsed_request_id.version != 4
            or str(parsed_request_id) != request_id.lower()
            or len(set(cleaned_scopes)) != len(cleaned_scopes)
        ):
            raise GatewayError(400, "invalid_direct_grant_request")
        if self.human_control is None:
            raise GatewayError(503, "direct_grants_unavailable")
        result = self.human_control.post(
            DIRECT_GRANT_ROUTE,
            {
                "request_id": str(parsed_request_id),
                "requested_scopes": cleaned_scopes,
            },
        )
        grant_ref = result.get("grant_ref")
        expires_at = result.get("expires_at")
        scopes = result.get("scopes", result.get("authorized_scopes"))
        status = result.get("status")
        if (
            not isinstance(grant_ref, str)
            or not grant_ref
            or len(grant_ref) > 256
            or not isinstance(expires_at, str)
            or not expires_at
            or len(expires_at) > 64
            or not isinstance(scopes, list)
            or not scopes
            or len(scopes) > 16
            or any(not isinstance(scope, str) or not scope for scope in scopes)
            or not isinstance(status, str)
            or not status
            or len(status) > 32
        ):
            raise GatewayError(502, "st_control_invalid_response")
        return {
            "grant_ref": grant_ref,
            "expires_at": expires_at,
            "scopes": list(scopes),
            "status": status,
        }

    def models(self) -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": self.config.public_model,
                    "object": "model",
                    "created": 0,
                    "owned_by": "stiller-gateway",
                }
            ],
        }

    @staticmethod
    def _contains_reserved_context(value: Any) -> bool:
        if isinstance(value, str):
            return CONTEXT_MARKER in value
        if isinstance(value, Mapping):
            return any(
                GatewayApplication._contains_reserved_context(item)
                for item in value.values()
            )
        if isinstance(value, (list, tuple)):
            return any(
                GatewayApplication._contains_reserved_context(item)
                for item in value
            )
        return False

    @staticmethod
    def _messages(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise GatewayError(400, "messages_required")
        cleaned: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                raise GatewayError(400, "invalid_message")
            role = message.get("role")
            if role not in {"system", "developer", "user", "assistant", "tool", "function"}:
                raise GatewayError(400, "invalid_message_role")
            if GatewayApplication._contains_reserved_context(message):
                raise GatewayError(400, "reserved_context_marker")
            cleaned.append(copy.deepcopy(message))
        return cleaned

    @staticmethod
    def _is_tool_continuation(messages: list[dict[str, Any]]) -> bool:
        for message in reversed(messages):
            role = message.get("role")
            if role in {"system", "developer"}:
                continue
            return role in {"tool", "function"}
        return False

    @staticmethod
    def _thread_id(payload: Mapping[str, Any], headers: Mapping[str, str]) -> str:
        supplied = _normalize_headers(headers).get("x-st-thread-id")
        if isinstance(supplied, str) and supplied.strip():
            value = supplied.strip()
            if len(value) > 256:
                raise GatewayError(400, "thread_id_too_long")
            return value
        # OpenAI ``user`` identifies an end user, not a chat, and identical first
        # messages can occur in unrelated frames.  Without an explicit header use
        # a fresh opaque transport label; authorization never depends on this label.
        return "rikkahub-turn:" + uuid.uuid4().hex

    @staticmethod
    def _native_tool_calls(value: Any) -> list[NativeToolCall]:
        if not isinstance(value, list) or not value:
            raise GatewayError(502, "upstream_tool_calls_invalid")
        result: list[NativeToolCall] = []
        seen: set[str] = set()
        for raw in value:
            if (
                not isinstance(raw, Mapping)
                or raw.get("type") != "function"
                or not isinstance(raw.get("id"), str)
                or not raw.get("id", "").strip()
                or not isinstance(raw.get("function"), Mapping)
            ):
                raise GatewayError(502, "upstream_tool_calls_invalid")
            call_id = raw["id"].strip()
            function = raw["function"]
            name = function.get("name")
            arguments = function.get("arguments")
            if (
                call_id in seen
                or not isinstance(name, str)
                or not name.strip()
                or not isinstance(arguments, str)
            ):
                raise GatewayError(502, "upstream_tool_calls_invalid")
            seen.add(call_id)
            result.append(
                NativeToolCall(
                    tool_call_id=call_id,
                    tool_name=name.strip(),
                    arguments_text=arguments,
                )
            )
        return result

    @staticmethod
    def _normalize_split_tool_declarations(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Join only adjacent empty tool declarations with their exact result tail.

        This is a shape normalization, not authorization: the resulting batch
        still passes the original current-order, catalog, binding and receipt
        checks. No result is synthesized, removed, reordered or rewritten.
        """

        tail_start = len(messages)
        while tail_start and messages[tail_start - 1].get("role") == "tool":
            tail_start -= 1
        if tail_start == len(messages):
            return messages
        group_start = tail_start
        while group_start:
            message = messages[group_start - 1]
            if (
                message.get("role") != "assistant"
                or not isinstance(message.get("tool_calls"), list)
                or not message["tool_calls"]
            ):
                break
            group_start -= 1
        group = messages[group_start:tail_start]
        if len(group) < 2:
            return messages
        required = {"role", "content", "tool_calls"}
        allowed = required | {"reasoning_content"}
        if any(
            not required.issubset(message)
            or not set(message).issubset(allowed)
            or message["content"] not in (None, "")
            or message.get("reasoning_content") not in (None, "")
            for message in group
        ):
            return messages
        raw_calls = [call for message in group for call in message["tool_calls"]]
        try:
            declared = GatewayApplication._native_tool_calls(raw_calls)
        except GatewayError:
            return messages
        declared_ids = [call.tool_call_id for call in declared]
        # Do not turn an incomplete/ambiguous batch into a different request.
        if any(raw["id"] != call_id for raw, call_id in zip(raw_calls, declared_ids)):
            return messages
        returned_ids = [message.get("tool_call_id") for message in messages[tail_start:]]
        if (
            any(not isinstance(value, str) or not value or value != value.strip()
                for value in returned_ids)
            or len(set(returned_ids)) != len(returned_ids)
            or set(returned_ids) != set(declared_ids)
        ):
            return messages
        merged = copy.deepcopy(group[0])
        merged["tool_calls"] = copy.deepcopy(raw_calls)
        return [*messages[:group_start], merged, *messages[tail_start:]]

    @staticmethod
    def _continuation_tool_call_batch(
        messages: list[dict[str, Any]],
        *,
        protected_values: Sequence[str] = (),
    ) -> tuple[list[NativeToolCall], list[dict[str, Any]]]:
        assistant_index = -1
        declared_calls: list[NativeToolCall] = []
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") != "assistant":
                continue
            calls = message.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                continue
            try:
                declared_calls = GatewayApplication._native_tool_calls(calls)
            except GatewayError as exc:
                raise _lineage_error(
                    "tool_continuation_lineage_invalid", "declaration_invalid",
                    protected_values=protected_values,
                ) from exc
            assistant_index = index
            break
        if assistant_index < 0:
            raise _lineage_error(
                "tool_continuation_lineage_missing", "declaration_missing",
                protected_values=protected_values,
            )
        tail = messages[assistant_index + 1 :]
        if not tail or any(message.get("role") != "tool" for message in tail):
            raise _lineage_error(
                "tool_continuation_lineage_invalid", "tail_not_tool_results",
                protected_values=protected_values,
            )
        returned_list = [
            message.get("tool_call_id", "").strip()
            if isinstance(message.get("tool_call_id"), str)
            else ""
            for message in tail
        ]
        if any(not value for value in returned_list):
            raise _lineage_error(
                "tool_continuation_lineage_invalid", "result_id_missing",
                protected_values=protected_values,
            )
        returned = set(returned_list)
        if len(returned) != len(returned_list):
            raise _lineage_error(
                "tool_continuation_lineage_invalid", "result_id_duplicate",
                protected_values=protected_values,
                result_count=len(returned_list),
            )
        declared = {call.tool_call_id for call in declared_calls}
        if returned != declared:
            raise _lineage_error(
                "tool_continuation_lineage_mismatch", "declaration_result_set_mismatch",
                protected_values=protected_values,
                declared_count=len(declared), result_count=len(returned),
                missing_result_count=len(declared - returned),
                unexpected_result_count=len(returned - declared),
            )
        return declared_calls, tail

    @staticmethod
    def _bound_continuation_tool_call_batch(
        messages: list[dict[str, Any]],
        *,
        protected_values: Sequence[str] = (),
    ) -> tuple[list[NativeToolCall], list[dict[str, Any]]]:
        """Read complete adjacent A(calls)/T(results) units without rewriting them.

        A client may serialize one parallel native batch into several assistant
        messages, each followed by that message's exact result set. Text and
        reasoning on those assistant messages remain untouched. This parser
        only reconstructs the transport batch for the existing signed binding,
        current-order, catalog and settled-receipt checks in ``prepare_turn``.

        No unit may borrow another unit's result. Human/system/developer and
        tool-less assistant messages stop the scan; adjacent declarations with
        no intervening results do not become a new compatibility permission.
        The older exact-empty normalization remains a separate, narrow path.
        """
        cursor = len(messages)
        units: list[tuple[list[NativeToolCall], list[dict[str, Any]]]] = []
        seen_ids: set[str] = set()
        while cursor:
            result_start = cursor
            while result_start and messages[result_start - 1].get("role") == "tool":
                result_start -= 1
            if result_start == cursor:
                break
            declaration_index = result_start - 1
            if declaration_index < 0:
                break
            declaration = messages[declaration_index]
            if (
                declaration.get("role") != "assistant"
                or not isinstance(declaration.get("tool_calls"), list)
                or not declaration["tool_calls"]
            ):
                break
            unit = messages[declaration_index:cursor]
            calls, results = GatewayApplication._continuation_tool_call_batch(
                unit, protected_values=protected_values,
            )
            if any(
                raw["id"] != call.tool_call_id
                for raw, call in zip(declaration["tool_calls"], calls)
            ):
                raise _lineage_error(
                    "tool_continuation_lineage_invalid", "declaration_invalid",
                    protected_values=protected_values,
                )
            if any(result["tool_call_id"] != result["tool_call_id"].strip() for result in results):
                raise _lineage_error(
                    "tool_continuation_lineage_invalid", "result_id_noncanonical",
                    protected_values=protected_values,
                    result_count=len(results),
                )
            ids = {call.tool_call_id for call in calls}
            if ids & seen_ids:
                raise _lineage_error(
                    "tool_continuation_lineage_invalid", "result_id_duplicate",
                    protected_values=protected_values,
                    result_count=len(seen_ids) + len(results),
                )
            seen_ids.update(ids)
            units.append((calls, results))
            cursor = declaration_index
        if not units:
            # Preserve the established malformed/missing-tail diagnostics.
            return GatewayApplication._continuation_tool_call_batch(
                messages, protected_values=protected_values,
            )
        return (
            [call for calls, _ in reversed(units) for call in calls],
            [result for _, results in reversed(units) for result in results],
        )

    def _verify_settled_tool_replay(
        self,
        *,
        session: TurnSession,
        call: NativeToolCall,
        tool_message: Mapping[str, Any],
        schemas: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """Verify one Rikka-replayed, already-settled call without witnessing it again.

        RikkaHub keeps consecutive tool-only assistant steps in one UI message.
        Its next OpenAI request can therefore replay the signed tail of an older
        batch before the newly expected calls.  Only an exact call/result replay
        from this wake is compatible; the old result is never consumed twice.
        """

        matches = [
            receipt
            for receipt in session.host_receipts
            if receipt.get("tool_call_id") == call.tool_call_id
        ]
        if len(matches) != 1:
            raise ToolExecutionBoundaryError(
                "tool_continuation_settled_replay_mismatch"
            )
        receipt = matches[0]
        required_strings = (
            "wake_id",
            "tool_call_id",
            "tool_name",
            "catalog_hash",
            "schema_hash",
            "arguments_hash",
            "result_hash",
        )
        if (
            receipt.get("contract") != HOST_RECEIPT_CONTRACT
            or any(not isinstance(receipt.get(key), str) for key in required_strings)
            or receipt.get("wake_id") != session.wake_id
            or receipt.get("tool_call_id") != call.tool_call_id
            or receipt.get("tool_name") != call.tool_name
            or receipt.get("catalog_hash")
            != session.advertised_tools.get("catalog_hash")
            or not self.execution_boundary.verify_receipt(receipt)
        ):
            raise ToolExecutionBoundaryError(
                "tool_continuation_settled_replay_mismatch"
            )
        expected = ToolExecutionBinding(
            contract=EXECUTION_BINDING_CONTRACT,
            wake_id=str(receipt["wake_id"]),
            tool_call_id=str(receipt["tool_call_id"]),
            tool_name=str(receipt["tool_name"]),
            catalog_hash=str(receipt["catalog_hash"]),
            schema_hash=str(receipt["schema_hash"]),
            arguments_hash=str(receipt["arguments_hash"]),
            issued_at_ms=0,
        )
        try:
            self.execution_boundary.verify_history_call(
                expected=expected,
                catalog=session.advertised_tools,
                schemas=schemas,
                call=call,
            )
            result_hash = canonical_hash(
                {
                    "tool_call_id": call.tool_call_id,
                    "content": tool_message.get("content"),
                }
            )
        except (ToolExecutionBoundaryError, TypeError, ValueError) as exc:
            raise ToolExecutionBoundaryError(
                "tool_continuation_settled_replay_mismatch"
            ) from exc
        if not hmac.compare_digest(str(receipt["result_hash"]), result_hash):
            raise ToolExecutionBoundaryError(
                "tool_continuation_settled_replay_mismatch"
            )

    @staticmethod
    def _continuation_tool_call_ids(
        messages: list[dict[str, Any]], *, protected_values: Sequence[str] = ()
    ) -> set[str]:
        assistant_index = -1
        declared: set[str] = set()
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") != "assistant":
                continue
            calls = message.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                continue
            candidate_ids = {
                call.get("id")
                for call in calls
                if isinstance(call, Mapping)
                and isinstance(call.get("id"), str)
                and call.get("id", "").strip()
            }
            if len(candidate_ids) != len(calls):
                raise _lineage_error(
                    "tool_continuation_lineage_invalid", "declaration_invalid",
                    protected_values=protected_values,
                )
            assistant_index = index
            declared = {str(value) for value in candidate_ids}
            break
        if assistant_index < 0:
            raise _lineage_error(
                "tool_continuation_lineage_missing", "declaration_missing",
                protected_values=protected_values,
            )
        tail = messages[assistant_index + 1 :]
        if not tail or any(message.get("role") != "tool" for message in tail):
            raise _lineage_error(
                "tool_continuation_lineage_invalid", "tail_not_tool_results",
                protected_values=protected_values,
            )
        returned_list = [
            message.get("tool_call_id", "").strip()
            if isinstance(message.get("tool_call_id"), str)
            else ""
            for message in tail
        ]
        if any(not value for value in returned_list):
            raise _lineage_error(
                "tool_continuation_lineage_invalid", "result_id_missing",
                protected_values=protected_values,
            )
        returned = set(returned_list)
        if len(returned) != len(returned_list):
            raise _lineage_error(
                "tool_continuation_lineage_invalid", "result_id_duplicate",
                protected_values=protected_values,
                result_count=len(returned_list),
            )
        if returned != declared:
            raise _lineage_error(
                "tool_continuation_lineage_mismatch", "declaration_result_set_mismatch",
                protected_values=protected_values,
                declared_count=len(declared), result_count=len(returned),
                missing_result_count=len(declared - returned),
                unexpected_result_count=len(returned - declared),
            )
        return returned

    @staticmethod
    def _source_kind(messages: list[dict[str, Any]]) -> str:
        return "tool_result" if GatewayApplication._is_tool_continuation(messages) else "human_message"

    @staticmethod
    def _text_content(content: Any) -> str:
        """Extract only bounded textual content; image/audio payloads never enter recall."""

        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for part in content:
            if not isinstance(part, Mapping):
                continue
            kind = part.get("type")
            text = part.get("text")
            if kind in {"text", "input_text"} and isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _source_frame(
        messages: list[dict[str, Any]],
        *,
        thread_id: str,
        lineage_stable: bool,
        source_event_id: str,
    ) -> dict[str, Any]:
        # Recall belongs to the newest user turn even when that turn has no
        # textual modality. Never fall back to an older user's text merely
        # because the latest message contains only image/audio parts.
        query_text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                query_text = GatewayApplication._text_content(
                    message.get("content")
                ).strip()[:4000]
                break
        captures: list[dict[str, str]] = []
        latest_user_index = -1
        previous_user_index = -1
        for message in messages:
            role = message.get("role")
            if role not in {"user", "assistant"}:
                continue
            text = GatewayApplication._text_content(message.get("content")).strip()
            if not text:
                continue
            bounded = text[:1200]
            if role == "user":
                previous_user_index = latest_user_index
                latest_user_index = len(captures)
            captures.append({"role": str(role), "content": bounded})
        prior_assistant_present = any(
            item["role"] == "assistant"
            for item in captures[:latest_user_index]
        ) if latest_user_index >= 0 else False
        user_message_count = sum(
            1 for message in messages if message.get("role") == "user"
        )
        conversational_history_present = any(
            message.get("role") in {"assistant", "tool", "function"}
            for message in messages
        )
        first_user_turn = (
            user_message_count == 1 and not conversational_history_present
        )
        # Chat-completions clients normally resend the entire conversation on
        # every turn.  Capture only the new suffix after the previous user
        # message (typically the last assistant reply plus the current user
        # input).  Re-capturing older history under a new event id would renew
        # its expiry and violate the fixed, non-sliding 30-minute TTL.
        if previous_user_index >= 0:
            captures = captures[previous_user_index + 1 :]
        return {
            "query_text": query_text,
            "thread_id": thread_id if lineage_stable else None,
            "lineage_stable": lineage_stable,
            # This content-free fact is safe even when the client cannot send a
            # stable conversation id.  It may gate a one-turn boundary hint,
            # but never enables capture or inferred cross-window lineage.
            "prior_assistant_present": prior_assistant_present,
            # Exact structural fact only: never guess from a name or greeting.
            "first_user_turn": first_user_turn,
            "source_event_id": source_event_id,
            "capture_items": captures[-4:] if lineage_stable else [],
        }

    @staticmethod
    def _injected_message(message: Mapping[str, Any]) -> dict[str, str]:
        """Validate and copy the runtime's exact role/content pair without wrapping it."""
        if set(message) != {"role", "content"}:
            raise GatewayError(502, "st_context_message_invalid")
        role = message.get("role")
        content = message.get("content")
        if role != "system" or not isinstance(content, str) or not content:
            raise GatewayError(502, "st_context_message_invalid")
        return {"role": role, "content": content}

    @staticmethod
    def _context_layout(messages: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        """Anchor the current human boundary once, before tool continuation."""
        indexes = [index for index, item in enumerate(messages) if item.get("role") == "user"]
        if not indexes:
            # Preserve existing non-human/minimal context behavior, without
            # inventing a human boundary for system-only host requests.
            return None
        return {
            "contract": CONTEXT_LAYOUT_CONTRACT,
            "insertion_rule": "before-current-human",
            "human_message_index": indexes[-1],
            "initial_message_count": len(messages),
            "initial_messages_digest": _digest(messages),
        }

    def _verified_context_bundle(
        self, prepared: Mapping[str, Any], *, layout: dict[str, Any] | None,
        wake: Mapping[str, Any], thread_id: str, source_digest: str,
        message: dict[str, str], context_hash: str,
    ) -> dict[str, Any] | None:
        bundle = prepared.get("context_bundle")
        if bundle is None:
            # Old Control omits this field entirely. An explicitly present but
            # null v2 bundle is malformed, not a reason to downgrade this wake.
            if (layout is not None and layout.get("contract") == TAIL_CONTEXT_LAYOUT_CONTRACT
                    and "context_bundle" in prepared):
                raise GatewayError(502, "st_context_bundle_invalid")
            # Old Control may ignore the optional version negotiation. Its
            # legacy hash is still checked; a bundle hash cannot enter here.
            if _digest(message) != context_hash:
                raise GatewayError(502, "st_context_hash_mismatch")
            return None
        if (layout is None or not isinstance(bundle, dict)
                or set(bundle) != {"contract", "layout", "binding", "stable_message",
                                   "dynamic_message", "legacy_message_hash"}
                or layout.get("contract") not in {CONTEXT_LAYOUT_CONTRACT, TAIL_CONTEXT_LAYOUT_CONTRACT}
                or bundle.get("contract") != layout["contract"]
                or _canonical(bundle.get("layout")) != _canonical(layout)
                or bundle.get("legacy_message_hash") != _digest(message)):
            raise GatewayError(502, "st_context_bundle_invalid")
        binding = bundle.get("binding")
        expected = {
            "host_id": self.config.host_id, "thread_id": thread_id,
            "wake_id": wake["wake_id"], "source_digest": source_digest,
            "host_contract_digest": self.config.host_contract_digest,
        }
        if (not isinstance(binding, dict)
                or set(binding) != {*expected, "owner_id", "model_id"}
                or any(binding.get(key) != value for key, value in expected.items())
                or any(not isinstance(binding.get(key), str) or not binding[key]
                       for key in ("owner_id", "model_id"))):
            raise GatewayError(502, "st_context_bundle_binding_mismatch")
        for key in ("stable_message", "dynamic_message"):
            if bundle[key] is not None:
                if not isinstance(bundle[key], dict):
                    raise GatewayError(502, "st_context_bundle_invalid")
                self._injected_message(bundle[key])
        if bundle["stable_message"] is None and bundle["dynamic_message"] is None:
            raise GatewayError(502, "st_context_bundle_invalid")
        if _digest(bundle) != context_hash:
            raise GatewayError(502, "st_context_hash_mismatch")
        return copy.deepcopy(bundle)

    @staticmethod
    def _validate_context_history(session: TurnSession, messages: Sequence[Mapping[str, Any]]) -> None:
        if session.context_bundle is None:
            return
        if _digest(session.context_bundle) != session.context_hash:
            raise GatewayError(502, "st_context_hash_mismatch")
        layout = session.context_bundle["layout"]
        if session.context_bundle["contract"] == TAIL_CONTEXT_LAYOUT_CONTRACT:
            if layout != {"contract": TAIL_CONTEXT_LAYOUT_CONTRACT,
                          "insertion_rule": "after-client-messages"}:
                raise GatewayError(502, "st_context_bundle_invalid")
            # V2 authenticates/fixes ST frames, not the entire client history.
            # Existing lineage, catalog, issued-call and settled-receipt checks
            # still run before a continuation is accepted. Never normalize
            # changed client text back to an older value for cache purposes.
            return
        if session.context_bundle["contract"] != CONTEXT_LAYOUT_CONTRACT:
            raise GatewayError(502, "st_context_bundle_invalid")
        count = layout["initial_message_count"]
        boundary = layout["human_message_index"]
        if (len(messages) < count or not 0 <= boundary < count
                or messages[boundary].get("role") != "user"
                or _digest(messages[:count]) != layout["initial_messages_digest"]
                or any(item.get("role") == "user" for item in messages[count:])):
            raise GatewayError(409, "tool_continuation_context_history_mismatch",
                               "工具续轮之前的聊天历史发生了改写或截断，无法复用本轮 ST 上下文；请结束本轮后从新消息重试。")

    @classmethod
    def _apply_context(cls, session: TurnSession, messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if session.context_bundle is None:
            return [copy.deepcopy(session.message), *copy.deepcopy(messages)]
        cls._validate_context_history(session, messages)
        bundle = session.context_bundle
        boundary = (len(messages) if bundle["contract"] == TAIL_CONTEXT_LAYOUT_CONTRACT
                    else bundle["layout"]["human_message_index"])
        result = []
        if bundle["stable_message"] is not None:
            result.append(copy.deepcopy(bundle["stable_message"]))
        result.extend(copy.deepcopy(messages[:boundary]))
        if bundle["dynamic_message"] is not None:
            result.append(copy.deepcopy(bundle["dynamic_message"]))
        result.extend(copy.deepcopy(messages[boundary:]))
        return result

    def _compare_cache_segments(self, session: TurnSession, messages: Sequence[Mapping[str, Any]],
                                payload: Mapping[str, Any]) -> dict[str, bool | None]:
        bundle = session.context_bundle
        current = {
            "thread": session.thread_id,
            "segmented": bundle is not None,
            "stable": _digest(bundle["stable_message"] if bundle else session.message),
            "dynamic": _digest(bundle["dynamic_message"] if bundle else None),
            # Preserve actual wire order, unlike semantic authorization catalog.
            "tools": _digest({key: payload[key] for key in ("tools", "functions") if key in payload}),
            "count": len(messages), "history": _digest(messages),
        }
        previous = self._last_cache_comparison
        comparable = previous is not None and previous["thread"] == current["thread"]
        segmented = comparable and current["segmented"] and previous["segmented"]
        result = {
            "stable_context_changed": current["stable"] != previous["stable"] if segmented else None,
            "dynamic_context_changed": current["dynamic"] != previous["dynamic"] if segmented else None,
            "wire_tools_changed": current["tools"] != previous["tools"] if comparable else None,
            # Deprecated internal name retained for existing adapter callers.
            # Only client_input_prefix_preserved is exported in telemetry.
            "prior_input_prefix_preserved": (
                len(messages) >= previous["count"] and _digest(messages[:previous["count"]]) == previous["history"]
            ) if comparable else None,
        }
        self._last_cache_comparison = current
        return result

    def _close_session(self, session: TurnSession) -> None:
        try:
            self.control.post(
                "/v1/host/context/close",
                {
                    "wake_id": session.wake_id,
                    "wake_capability": session.wake_capability,
                },
            )
        except GatewayError:
            # Closing is best-effort. Wake TTL and superseding wakes remain authoritative.
            pass

    @contextmanager
    def _fresh_context_wake(self, wake: Mapping[str, Any]):
        """Retire only a fresh, unforwarded wake if context negotiation fails."""
        try:
            yield
        except GatewayError:
            try:
                self.control.post("/v1/host/context/close", {
                    "wake_id": wake["wake_id"], "wake_capability": wake["wake_capability"],
                })
            except GatewayError:
                # Preserve the original failure; never retry the request/tool.
                # Authoritative wake TTL remains the fallback on Control loss.
                pass
            raise

    @staticmethod
    def _expired_armed_tool_wait(session: TurnSession) -> bool:
        """Only armed tool waits expire; model generation is never timed here.

        The delivery-based monotonic deadline is independent of the global wake
        TTL. Keep wake expiry as an additional authorization boundary, not as
        the normal half-hour fallback for a lost tool-result response.
        """

        return (
            session.tool_wait_armed_at is not None
            and bool(session.expected_tool_call_ids)
            and (datetime.now(timezone.utc) >= session.expires_at
                 or (session.tool_result_wait_deadline is not None
                     and time.monotonic() >= session.tool_result_wait_deadline))
        )

    def _remember_retired_tool_calls(self, call_ids: Sequence[str]) -> None:
        for call_id in call_ids:
            if call_id in self._retired_tool_call_ids:
                continue
            self._retired_tool_call_ids.add(call_id)
            self._retired_tool_call_order.append(call_id)
        excess = len(self._retired_tool_call_order) - MAX_RETIRED_TOOL_CALL_IDS
        if excess > 0:
            for call_id in self._retired_tool_call_order[:excess]:
                self._retired_tool_call_ids.discard(call_id)
            del self._retired_tool_call_order[:excess]

    def _retire_expired_tool_wait(self, session: TurnSession) -> None:
        """Invalidate one expired armed lineage while the app lock is held."""

        if self._current_session is not session:
            return
        if (session.tool_result_wait_deadline is not None
                and time.monotonic() >= session.tool_result_wait_deadline):
            if not self._retire_tool_result_wait(session):
                self._schedule_tool_result_wait(session, 5.0)
                raise GatewayError(409, "tool_wait_recovery_in_progress",
                    "工具结果等待已超过时限，旧轮正在安全收尾；执行仍在进行或结束状态未确认，请稍后再发送。")
            return
        strict_execution = self.config.require_execution_binding and session.execution_batch_id
        if strict_execution:
            try:
                self._close_execution_session(session)
            except GatewayError:
                session.recovery_pending = True
                self._audit_recovery(session, "quarantined", "expired_wake_cleanup_pending")
                if session.abnormal_wait_started is None:
                    session.abnormal_wait_started = time.monotonic()
                    self._schedule_recovery(session, self.config.abnormal_wait_seconds)
                raise GatewayError(409, "tool_wait_recovery_in_progress")
        self._remember_retired_tool_calls(tuple(session.expected_tool_call_ids))
        self._cancel_recovery_timer(session)
        self._current_session = None
        session.request_generation += 1
        session.expected_tool_call_ids.clear()
        session.expected_tool_call_order.clear()
        session.expected_tool_calls.clear()
        session.tool_wait_armed_at = None
        # Close and replacement wake creation stay in one critical section so a
        # concurrent continuation cannot observe a half-retired turn.
        if not strict_execution:
            self._close_session(session)

    def prepare_turn(
        self,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> PreparedTurn:
        normalized_headers = _normalize_headers(headers)
        messages = self._messages(payload)
        # Anchor the actual client history, before continuation normalization.
        # A later human turn must carry this exact prefix to abandon its wait.
        history_anchor_count = len(messages)
        history_anchor_digest = _digest(messages)
        advertised_tools = _advertised_tool_catalog(payload)
        continuation = self._is_tool_continuation(messages)
        if continuation:
            messages = self._normalize_split_tool_declarations(messages)
        source_digest = _digest(
            {"messages": messages, "advertised_tools": advertised_tools}
        )

        with self._lock:
            if continuation:
                current = self._current_session
                if current is None:
                    raise GatewayError(
                        409,
                        "tool_continuation_context_lost",
                        "工具续轮的 ST 上下文已丢失，请从新的用户消息重新开始。",
                    )
                if current.recovery_pending:
                    raise GatewayError(409, "tool_wait_recovery_in_progress")
                if self._expired_armed_tool_wait(current):
                    self._retire_expired_tool_wait(current)
                    raise GatewayError(409, "tool_continuation_context_lost")
                supplied_thread = normalized_headers.get("x-st-thread-id")
                if supplied_thread and supplied_thread != current.thread_id:
                    raise GatewayError(409, "tool_continuation_thread_mismatch")
                # Check before consuming any signed tool batch or receipt. A
                # rejected history must remain retryable with the real branch.
                self._validate_context_history(current, messages)
                if current.expected_tool_calls:
                    all_declared_calls, tool_messages = self._bound_continuation_tool_call_batch(
                        messages, protected_values=tuple(current.protected_values)
                    )
                    continuation_call_ids = {call.tool_call_id for call in all_declared_calls}
                else:
                    continuation_call_ids = self._continuation_tool_call_ids(
                        messages, protected_values=tuple(current.protected_values)
                    )
                if continuation_call_ids & self._retired_tool_call_ids:
                    raise GatewayError(409, "tool_continuation_context_lost")
                if advertised_tools != current.advertised_tools:
                    if (
                        current.advertised_tools.get("entries")
                        and "tools" not in payload
                        and "functions" not in payload
                    ):
                        raise GatewayError(
                            409,
                            "tool_continuation_catalog_missing",
                            "工具续轮缺少本轮原始工具目录，请从新的用户消息重新开始。",
                        )
                    raise GatewayError(
                        409,
                        "tool_continuation_catalog_mismatch",
                        "工具续轮的工具目录或参数结构已改变，请从新的用户消息重新开始。",
                    )
                if not advertised_tools["catalog_complete"]:
                    raise GatewayError(
                        409,
                        "tool_continuation_catalog_incomplete",
                        "工具续轮目录无法完整验证，请从新的用户消息重新开始。",
                    )
                if current.expected_tool_calls:
                    expected_order = list(current.expected_tool_call_order)
                    if not expected_order:
                        expected_order = list(current.expected_tool_calls)
                    current_count = len(expected_order)
                    if (
                        current_count == 0
                        or len(all_declared_calls) < current_count
                        or [
                            call.tool_call_id
                            for call in all_declared_calls[-current_count:]
                        ]
                        != expected_order
                    ):
                        self._mark_verified_partial(current, all_declared_calls, tool_messages, payload)
                        raise _lineage_error(
                            "tool_continuation_lineage_mismatch", "current_batch_order_mismatch",
                            protected_values=tuple(current.protected_values),
                            declared_count=len(all_declared_calls),
                            expected_count=current_count,
                            terminal_declared_count=next(
                                len(message["tool_calls"]) for message in reversed(messages)
                                if message.get("role") == "assistant"
                                and isinstance(message.get("tool_calls"), list)
                                and message["tool_calls"]
                            ),
                            segment_declared_count=len(all_declared_calls),
                            segment_result_count=len(tool_messages),
                        )
                    settled_calls = all_declared_calls[:-current_count]
                    declared_calls = all_declared_calls[-current_count:]
                    settled_ids = [call.tool_call_id for call in settled_calls]
                    if settled_ids and settled_ids != current.settled_tool_call_order[
                        -len(settled_ids) :
                    ]:
                        raise _lineage_error(
                            "tool_continuation_lineage_mismatch", "settled_prefix_order_mismatch",
                            protected_values=tuple(current.protected_values),
                            declared_count=len(settled_ids),
                            expected_count=len(current.settled_tool_call_order),
                        )
                    call_ids = {call.tool_call_id for call in declared_calls}
                else:
                    settled_calls = []
                    declared_calls = []
                    call_ids = self._continuation_tool_call_ids(
                        messages, protected_values=tuple(current.protected_values)
                    )
                    last_assistant = max(
                        index
                        for index, message in enumerate(messages)
                        if message.get("role") == "assistant"
                        and isinstance(message.get("tool_calls"), list)
                        and message.get("tool_calls")
                    )
                    tool_messages = messages[last_assistant + 1 :]
                if call_ids != current.expected_tool_call_ids:
                    raise _lineage_error(
                        "tool_continuation_lineage_mismatch", "expected_id_set_mismatch",
                        protected_values=tuple(current.protected_values),
                        declared_count=len(call_ids),
                        expected_count=len(current.expected_tool_call_ids),
                    )
                # HTTP responses always arm exact bindings before bytes are
                # released.  The empty-map branch preserves the older pure
                # orchestration test API only; the network handler cannot reach
                # it because ``bind_response_tool_calls`` is mandatory there.
                if current.expected_tool_calls:
                    try:
                        schemas = advertised_schemas(payload)
                        by_id = {
                            str(message["tool_call_id"]): message
                            for message in tool_messages
                        }
                        for call in settled_calls:
                            self._verify_settled_tool_replay(
                                session=current,
                                call=call,
                                tool_message=by_id[call.tool_call_id],
                                schemas=schemas,
                            )
                        for call in declared_calls:
                            expected = current.expected_tool_calls.get(call.tool_call_id)
                            if expected is None:
                                raise ToolExecutionBoundaryError(
                                    "tool_continuation_binding_mismatch"
                                )
                            self.execution_boundary.verify_history_call(
                                expected=expected,
                                catalog=advertised_tools,
                                schemas=schemas,
                                call=call,
                            )
                    except ToolExecutionBoundaryError as exc:
                        raise GatewayError(
                            409,
                            str(exc),
                            "工具续轮中的名称、参数或结构与本网关签发的调用不一致。",
                        ) from exc
                # Consume the batch before forwarding so concurrent replays cannot
                # reuse the same tool result lineage.
                if current.abnormal_wait_started is not None:
                    self._audit_recovery(current, "disarmed", "complete_batch_received")
                self._cancel_recovery_timer(current)
                current.delivered_at_monotonic = None
                current.expected_tool_call_ids.clear()
                current.expected_tool_call_order.clear()
                expected_bindings = dict(current.expected_tool_calls)
                current.expected_tool_calls.clear()
                current.tool_wait_armed_at = None
                session = current
                thread_id = current.thread_id
                try:
                    by_id = {
                        str(message["tool_call_id"]): message
                        for message in tool_messages
                    }
                    if expected_bindings:
                        for call in declared_calls:
                            call_id = call.tool_call_id
                            receipt = self.execution_boundary.witness_result(
                                binding=expected_bindings[call_id],
                                tool_message=by_id[call_id],
                            )
                            session.host_receipts.append(receipt)
                            session.settled_tool_call_order.append(call_id)
                    for message in tool_messages:
                        session.protected_values.update(
                            _extract_protected_tool_values(message)
                        )
                except (KeyError, ToolExecutionBoundaryError, TypeError, ValueError) as exc:
                    self._current_session = None
                    self._close_session(current)
                    raise GatewayError(
                        409,
                        "tool_result_receipt_rejected",
                        "工具结果无法绑定为本轮宿主回执，请从新的用户消息重新开始。",
                    ) from exc
            else:
                # Module one has exactly one authoritative current wake per owner/model.
                # Do not let a new frame replace a wake while its upstream response
                # or native tool loop is unfinished.  Otherwise a delayed
                # parameterless ``stbrain_open`` from the old frame could operate on
                # the replacement wake before transport lineage rejects its result.
                # The E32 deployment is deliberately single-instance and serializes
                # human turns until the current response/tool chain is closed.
                current = self._current_session
                if current is not None and self._expired_armed_tool_wait(current):
                    self._retire_expired_tool_wait(current)
                    current = None
                if current is not None and self._anchored_new_human(
                        current, messages, payload, normalized_headers, advertised_tools):
                    self._abandon_delivered_wait(current)
                    current = None
                if current is not None:
                    raise GatewayError(
                        409,
                        "human_turn_in_progress",
                        "上一轮仍在生成或等待工具结果，请完成后再发送新的用户消息。",
                    )
                thread_id = self._thread_id(payload, normalized_headers)
                lineage_stable = bool(normalized_headers.get("x-st-thread-id"))
                source_event_id = (
                    normalized_headers.get("x-request-id")
                    or normalized_headers.get("idempotency-key")
                    or f"gateway-{uuid.uuid4().hex}"
                )
                wake = self.control.post(
                    "/v1/host/wakes",
                    {
                        "host_id": self.config.host_id,
                        "thread_id": thread_id,
                        "source_kind": self._source_kind(messages),
                        "source_event_id": str(source_event_id),
                    },
                )
                try:
                    expires_at = _wake_expiry(wake.get("expires_at"))
                    if expires_at <= datetime.now(timezone.utc):
                        raise GatewayError(502, "st_wake_expiry_invalid")
                except GatewayError:
                    wake_id = wake.get("wake_id")
                    wake_capability = wake.get("wake_capability")
                    if (
                        isinstance(wake_id, str)
                        and wake_id
                        and isinstance(wake_capability, str)
                        and wake_capability
                    ):
                        try:
                            self.control.post(
                                "/v1/host/context/close",
                                {
                                    "wake_id": wake_id,
                                    "wake_capability": wake_capability,
                                },
                            )
                        except GatewayError:
                            pass
                    raise
                context_layout = (self._context_layout(messages)
                                  if self.config.context_layout == "anchored-v1" else None)
                if self.config.context_layout == "tail-context-v2":
                    context_layout = {"contract": TAIL_CONTEXT_LAYOUT_CONTRACT,
                                      "insertion_rule": "after-client-messages"}
                layout_request_key = ("context_layout_offer"
                                      if self.config.context_layout == "tail-context-v2"
                                      else "context_layout")
                with self._fresh_context_wake(wake):
                    prepared = self.control.post(
                        "/v1/host/context/prepare",
                        {
                            "wake_id": wake["wake_id"],
                            "wake_capability": wake["wake_capability"],
                            "source_digest": source_digest,
                            "host_contract_digest": self.config.host_contract_digest,
                            "advertised_tools": advertised_tools,
                            **({layout_request_key: context_layout} if context_layout is not None else {}),
                            "source_frame": self._source_frame(
                                messages,
                                thread_id=thread_id,
                                lineage_stable=lineage_stable,
                                source_event_id=str(source_event_id),
                            ),
                        },
                    )
                    message = prepared.get("message")
                    context_hash = prepared.get("context_hash")
                    if not isinstance(message, dict) or not isinstance(context_hash, str):
                        raise GatewayError(502, "st_context_unavailable")
                    exact_message = self._injected_message(message)
                    context_bundle = self._verified_context_bundle(
                        prepared, layout=context_layout, wake=wake, thread_id=thread_id,
                        source_digest=source_digest, message=exact_message, context_hash=context_hash,
                    )
                    confirmed = self.control.post(
                        "/v1/host/context/confirm",
                        {
                            "wake_id": wake["wake_id"],
                            "wake_capability": wake["wake_capability"],
                            "context_hash": context_hash,
                        },
                    )
                    if confirmed.get("decision") != "injected":
                        raise GatewayError(502, "st_context_not_confirmed")
                session = TurnSession(
                    thread_id=thread_id,
                    wake_id=wake["wake_id"],
                    wake_capability=wake["wake_capability"],
                    message=exact_message,
                    context_hash=context_hash,
                    source_digest=source_digest,
                    advertised_tools=copy.deepcopy(advertised_tools),
                    created_at=time.time(),
                    expires_at=expires_at,
                    protected_values={wake["wake_capability"]},
                    context_bundle=context_bundle,
                )
                self._current_session = session

            session.request_generation += 1
            session.history_anchor_count = history_anchor_count
            session.history_anchor_digest = history_anchor_digest
            forwarded = copy.deepcopy(dict(payload))
            forwarded["model"] = self.config.upstream_model
            forwarded["messages"] = self._apply_context(session, messages)
            # Keep provider/client generation controls transparent.  The gateway
            # owns only the upstream model selection and the exact ST system
            # message; thinking/reasoning, tools, tool_choice, image input and
            # streaming controls remain byte-for-byte-equivalent JSON values.
            return PreparedTurn(
                thread_id=thread_id,
                payload=forwarded,
                session=session,
                continuation=continuation,
                request_generation=session.request_generation,
                cache_comparison=self._compare_cache_segments(session, messages, payload),
            )

    @staticmethod
    def response_requires_tool_continuation(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return False
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason") in {"tool_calls", "function_call"}:
                return True
            message = choice.get("message")
            if isinstance(message, dict) and (
                message.get("tool_calls") or message.get("function_call")
            ):
                return True
            delta = choice.get("delta")
            if isinstance(delta, dict) and (
                delta.get("tool_calls") or delta.get("function_call")
            ):
                return True
        return False

    @staticmethod
    def response_tool_call_ids(payload: Any) -> set[str]:
        if not isinstance(payload, Mapping):
            return set()
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return set()
        result: set[str] = set()
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            for container_name in ("message", "delta"):
                container = choice.get(container_name)
                if not isinstance(container, Mapping):
                    continue
                calls = container.get("tool_calls")
                if not isinstance(calls, list):
                    continue
                for call in calls:
                    if not isinstance(call, Mapping):
                        continue
                    call_id = call.get("id")
                    if isinstance(call_id, str) and call_id.strip():
                        result.add(call_id.strip())
        return result

    @staticmethod
    def response_tool_calls(payload: Any) -> list[NativeToolCall]:
        """Extract a complete non-stream native call batch.

        Legacy ``function_call`` has no stable call id and therefore cannot be
        bound to a replay-safe receipt.  It is rejected instead of being guessed.
        """

        if not isinstance(payload, Mapping):
            return []
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return []
        calls: list[NativeToolCall] = []
        seen: set[str] = set()
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            message = choice.get("message")
            if not isinstance(message, Mapping):
                continue
            if message.get("function_call") is not None:
                raise GatewayError(502, "legacy_function_call_not_receiptable")
            raw_calls = message.get("tool_calls")
            if raw_calls is None:
                continue
            batch = GatewayApplication._native_tool_calls(raw_calls)
            if any(call.tool_call_id in seen for call in batch):
                raise GatewayError(502, "upstream_tool_calls_invalid")
            seen.update(call.tool_call_id for call in batch)
            calls.extend(batch)
        return calls

    def bind_response_tool_calls(
        self,
        prepared: PreparedTurn,
        calls: Sequence[NativeToolCall],
    ) -> dict[str, ToolExecutionBinding]:
        """Validate and bind a call batch before any byte reaches RikkaHub."""

        if not calls:
            raise GatewayError(502, "upstream_tool_calls_missing")
        candidate_ids = {call.tool_call_id for call in calls}
        with self._lock:
            if not self._request_is_current(prepared):
                raise GatewayError(409, "tool_continuation_context_lost")
            if candidate_ids & (
                self._retired_tool_call_ids | prepared.session.seen_tool_call_ids
            ):
                raise GatewayError(502, "tool_call_id_reused")
        schemas: dict[str, Mapping[str, Any]] = {}
        try:
            schemas = advertised_schemas(prepared.payload)
            bindings: dict[str, ToolExecutionBinding] = {}
            for call in calls:
                if call.tool_call_id in bindings:
                    raise ToolExecutionBoundaryError("tool_call_id_reused")
                bindings[call.tool_call_id] = self.execution_boundary.bind_call(
                    wake_id=prepared.session.wake_id,
                    catalog=prepared.session.advertised_tools,
                    schemas=schemas,
                    call=call,
                )
        except ToolExecutionBoundaryError as exc:
            code = str(exc)
            status = 409 if code.startswith("tool_execution_") else 502
            allowed_names = [
                entry["canonical_name"]
                for entry in prepared.session.advertised_tools.get("entries", [])
                if isinstance(entry, Mapping) and entry.get("canonical_name") in schemas
            ]
            diagnostic = _safe_validation_diagnostic(
                exc.validation_diagnostic,
                allowed_tool_names=allowed_names,
                protected_values=tuple(prepared.session.protected_values),
            ) if code == "tool_arguments_schema_invalid" else None
            raise GatewayError(
                status,
                code,
                _tool_validation_message(diagnostic, protected_values=tuple(prepared.session.protected_values)) if code == "tool_arguments_schema_invalid"
                else _tool_not_advertised_message(tuple(prepared.session.protected_values))
                if code == "tool_not_advertised"
                else "原生工具调用未通过本轮目录、参数或宿主执行边界校验。",
                validation_diagnostic=diagnostic,
            ) from exc
        with self._lock:
            if not self._request_is_current(prepared):
                raise GatewayError(409, "tool_continuation_context_lost")
            call_ids = set(bindings)
            if call_ids & (
                self._retired_tool_call_ids | prepared.session.seen_tool_call_ids
            ):
                raise GatewayError(502, "tool_call_id_reused")
            prepared.session.seen_tool_call_ids.update(call_ids)
        prepared.issued_tool_calls = bindings
        return bindings

    def finish_turn(
        self,
        prepared: PreparedTurn,
        *,
        keep_for_tools: bool,
        tool_call_ids: Sequence[str] = (),
        tool_call_bindings: Mapping[str, ToolExecutionBinding] | None = None,
    ) -> None:
        close: TurnSession | None = None
        with self._lock:
            # An older upstream response must never close or arm a replacement
            # session that happens to use the same client thread label.
            if not self._request_is_current(prepared):
                return
            call_ids = {
                value.strip()
                for value in tool_call_ids
                if isinstance(value, str) and value.strip()
            }
            if keep_for_tools and call_ids:
                bindings = dict(tool_call_bindings or prepared.issued_tool_calls)
                expected_order = (
                    list(bindings)
                    if bindings
                    else list(dict.fromkeys(
                        value.strip()
                        for value in tool_call_ids
                        if isinstance(value, str) and value.strip()
                    ))
                )
                prepared.session.expected_tool_call_ids = call_ids
                prepared.session.expected_tool_call_order = expected_order
                prepared.session.expected_tool_calls = bindings
                prepared.session.tool_wait_armed_at = time.time()
                return
            else:
                if self.config.require_execution_binding and prepared.session.execution_batch_id:
                    try:
                        self._close_execution_session(prepared.session)
                    except GatewayError:
                        prepared.session.recovery_pending = True
                        self._audit_recovery(prepared.session, "quarantined", "terminated_response_cleanup_pending")
                        if prepared.session.abnormal_wait_started is None:
                            prepared.session.abnormal_wait_started = time.monotonic()
                            self._schedule_recovery(prepared.session, self.config.abnormal_wait_seconds)
                        return
                self._cancel_recovery_timer(prepared.session)
                self._current_session = None
                prepared.session.expected_tool_call_ids.clear()
                prepared.session.expected_tool_call_order.clear()
                prepared.session.expected_tool_calls.clear()
                prepared.session.tool_wait_armed_at = None
                if not (self.config.require_execution_binding and prepared.session.execution_batch_id):
                    close = prepared.session
        if close is not None:
            self._close_session(close)

    def upstream_url(self) -> str:
        return self.config.upstream_base_url.rstrip("/") + "/chat/completions"

    def upstream_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.upstream_api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/event-stream",
        }

    @staticmethod
    def _without_host_execution_argument(arguments: str) -> str:
        """Hide one top-level host field, preserving every business JSON byte.

        Current continuations have already passed exact lineage validation.
        Unknown/unfinished older history is not repaired or interpreted as a
        new call. Only a valid argument object with the reserved host-ref shape
        can be projected; strings inside business arguments are never searched.
        """
        try:
            values = parse_arguments(arguments)
        except ToolExecutionBoundaryError:
            return arguments
        ref = values.get("execution_ref")
        if not isinstance(ref, str) or re.fullmatch(r"stexec_[A-Za-z0-9_-]{43}", ref) is None:
            return arguments
        decoder = json.JSONDecoder()
        position = arguments.index("{") + 1
        previous_comma: int | None = None

        def whitespace(offset: int) -> int:
            while offset < len(arguments) and arguments[offset] in " \t\r\n":
                offset += 1
            return offset

        while True:
            start = whitespace(position)
            key, key_end = decoder.raw_decode(arguments, start)
            colon = whitespace(key_end)
            value_start = whitespace(colon + 1)
            _, value_end = decoder.raw_decode(arguments, value_start)
            delimiter = whitespace(value_end)
            if key == "execution_ref":
                if arguments[delimiter] == ",":
                    projected = arguments[:start] + arguments[delimiter + 1:]
                elif previous_comma is not None:
                    projected = arguments[:previous_comma] + arguments[value_end:]
                else:
                    projected = arguments[:start] + arguments[value_end:]
                expected = {key: value for key, value in values.items() if key != "execution_ref"}
                if parse_arguments(projected) != expected:
                    raise GatewayError(502, "execution_reference_invalid")
                return projected
            if arguments[delimiter] != ",":
                raise GatewayError(502, "execution_reference_invalid")
            previous_comma, position = delimiter, delimiter + 1

    def _upstream_execution_projection(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Keep host authority intact; omit its reserved input from the model.

        Never strip by token-looking text, by a tool-name suffix, or from an
        unmarked/non-ST schema. A past tool absent from the current catalog is
        deliberately not guessed to be host-managed.
        """
        if not self.config.require_execution_binding:
            return payload
        managed = {name for name, schema in advertised_schemas(payload).items()
                   if self._execution_tool(schema) is not None}
        if not managed:
            return payload
        projected = copy.deepcopy(payload)
        definitions = [item["function"] for item in projected.get("tools", [])]
        definitions.extend(projected.get("functions", []))
        for definition in definitions:
            if definition["name"] not in managed:
                continue
            schema = definition["parameters"]
            del schema["properties"]["execution_ref"]
            if isinstance(schema.get("required"), list):
                schema["required"] = [name for name in schema["required"] if name != "execution_ref"]
        for message in projected.get("messages", []):
            if not isinstance(message, Mapping) or message.get("role") != "assistant":
                continue
            calls = message.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, Mapping) or call.get("type") != "function":
                    continue
                function = call.get("function")
                if (isinstance(function, dict) and function.get("name") in managed
                        and isinstance(function.get("arguments"), str)):
                    function["arguments"] = self._without_host_execution_argument(function["arguments"])
        return projected

    def encode_upstream_payload(self, payload: Mapping[str, Any]) -> bytes:
        encoded = json.dumps(
            self._upstream_execution_projection(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > self.config.max_request_body_bytes:
            raise GatewayError(413, "request_too_large")
        return encoded


class _StreamToolCallScanner:
    """Incrementally inspect complete SSE events for native tool calls."""

    def __init__(self) -> None:
        self._events = _SSEEventBuffer()
        self.tool_call_ids: set[str] = set()

    def feed(self, chunk: bytes, *, final: bool = False) -> bool:
        requires_tools = False
        for raw_event in self._events.feed(chunk, final=final):
            event = _parse_sse_event(raw_event)
            if event.payload is None:
                continue
            self.tool_call_ids.update(
                GatewayApplication.response_tool_call_ids(event.payload)
            )
            if GatewayApplication.response_requires_tool_continuation(
                event.payload
            ):
                requires_tools = True
        return requires_tools


def _stream_finish_requires_tools(chunk: bytes) -> bool:
    scanner = _StreamToolCallScanner()
    return scanner.feed(chunk, final=True)


def _string_leaves(
    value: Any,
    path: tuple[str | int, ...] = (),
) -> list[tuple[tuple[str | int, ...], str]]:
    leaves: list[tuple[tuple[str | int, ...], str]] = []
    if isinstance(value, str):
        leaves.append((path, value))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            leaves.extend(_string_leaves(item, (*path, str(key))))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            leaves.extend(_string_leaves(item, (*path, index)))
    return leaves


_SSE_FRAGMENTABLE_FIELDS = frozenset(
    {"content", "reasoning_content", "arguments"}
)
_PROTECTED_PREFIX_MIN_CHARS = 8


def _fragmentable_string_leaves(
    value: Any,
) -> list[tuple[tuple[str | int, ...], str]]:
    """Return string paths whose values providers legitimately split by delta."""

    return [
        (path, text)
        for path, text in _string_leaves(value)
        if path and path[-1] in _SSE_FRAGMENTABLE_FIELDS
    ]


def _model_visible_output_fragments(
    payload: Mapping[str, Any],
) -> list[str]:
    """Return reasoning/content text in the order a chat client can expose it.

    Per-path suffix tracking is not sufficient for model-visible text because a
    provider can move from ``reasoning_content`` to ``content`` between SSE
    events.  RikkaHub exposes both channels, so their fragments share one
    quarantine sequence for protected-value detection.
    """

    fragments: list[str] = []
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return fragments
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        for container_name in ("delta", "message"):
            container = choice.get(container_name)
            if not isinstance(container, Mapping):
                continue
            for field_name in ("reasoning_content", "content"):
                if field_name not in container:
                    continue
                fragments.extend(
                    text for _, text in _string_leaves(container[field_name])
                )
    return fragments


def _protected_prefix_suffix(
    value: str,
    protected_values: Sequence[str],
) -> str:
    """Return the longest suffix that is a proper protected-value prefix."""

    best = ""
    for secret in protected_values:
        upper = min(len(value), len(secret) - 1)
        for length in range(upper, len(best), -1):
            if value.endswith(secret[:length]):
                best = secret[:length]
                break
    return best


def _has_material_protected_prefix(
    value: str,
    protected_values: Sequence[str],
) -> bool:
    """Whether ``value`` ends in enough credential prefix to quarantine.

    Real host capabilities are 43-character random base64url values.  Eight
    characters expose 48 random bits and are the shortest prefix treated as
    material.  Short test/legacy values use half their length so splitting one
    exactly in half still fails closed.  Briefer fragments remain tracked for
    later exact-match detection, but do not cause ordinary one-character line
    endings to fail randomly.
    """

    for secret in protected_values:
        threshold = min(
            _PROTECTED_PREFIX_MIN_CHARS,
            max(1, len(secret) // 2),
        )
        upper = min(len(value), len(secret) - 1)
        for length in range(upper, threshold - 1, -1):
            if value.endswith(secret[:length]):
                return True
    return False


def _buffered_sse_contains_protected_value(
    raw: bytes,
    protected_values: Sequence[str],
) -> bool:
    """Inspect a complete SSE response before releasing any byte to the client.

    Exact raw matches cover ordinary output. Parsed JSON catches escaped values,
    while bounded per-path, cross-fragmentable and model-visible suffixes catch
    capabilities split over content, reasoning or function-argument events.
    """

    protected = tuple(secret for secret in protected_values if secret)
    if not protected:
        return False
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GatewayError(502, "upstream_invalid_stream") from exc
    if any(secret in text for secret in protected):
        return True

    longest = max(len(secret) for secret in protected)
    suffixes: dict[tuple[str | int, ...], str] = {}
    cross_path_suffix = ""
    visible_suffix = ""
    saw_data = False
    parser = _SSEEventBuffer()
    raw_events = parser.feed(raw, final=True)
    for raw_event in raw_events:
        event = _parse_sse_event(raw_event)
        payload = event.payload
        if payload is None:
            continue
        saw_data = True
        if _contains_protected_value(payload, protected):
            return True
        fragmentable = _fragmentable_string_leaves(payload)
        for path, fragment in fragmentable:
            combined = suffixes.get(path, "") + fragment
            if any(secret in combined for secret in protected):
                return True
            suffixes[path] = combined[-(longest - 1) :] if longest > 1 else ""
        for _, fragment in fragmentable:
            combined = cross_path_suffix + fragment
            if any(secret in combined for secret in protected):
                return True
            cross_path_suffix = (
                combined[-(longest - 1) :] if longest > 1 else ""
            )
        for fragment in _model_visible_output_fragments(payload):
            combined = visible_suffix + fragment
            if any(secret in combined for secret in protected):
                return True
            visible_suffix = (
                combined[-(longest - 1) :] if longest > 1 else ""
            )
    if any(
        _has_material_protected_prefix(suffix, protected)
        for suffix in (*suffixes.values(), cross_path_suffix, visible_suffix)
    ):
        return True
    if raw and not saw_data:
        raise GatewayError(502, "upstream_invalid_stream")
    return False


def _buffered_sse_tool_calls(raw: bytes) -> list[NativeToolCall]:
    """Reassemble a complete native call batch from a buffered SSE response."""

    fragments: dict[tuple[int, int], dict[str, str]] = {}
    parser = _SSEEventBuffer()
    for raw_event in parser.feed(raw, final=True):
        event = _parse_sse_event(raw_event)
        payload = event.payload
        if payload is None:
            continue
        choices = payload.get("choices") if isinstance(payload, Mapping) else None
        if not isinstance(choices, list):
            continue
        for fallback_choice_index, choice in enumerate(choices):
            if not isinstance(choice, Mapping):
                continue
            message = choice.get("message")
            if isinstance(message, Mapping) and (
                message.get("tool_calls") is not None
                or message.get("function_call") is not None
            ):
                # Streaming chat completions must declare calls through delta.
                # Accepting a second message-style batch alongside a validated
                # delta batch would release unbound native calls to the client.
                raise GatewayError(502, "upstream_tool_calls_invalid")
            choice_index = choice.get("index", fallback_choice_index)
            if isinstance(choice_index, bool) or not isinstance(choice_index, int):
                raise GatewayError(502, "upstream_tool_calls_invalid")
            delta = choice.get("delta")
            if not isinstance(delta, Mapping):
                continue
            if delta.get("function_call") is not None:
                raise GatewayError(502, "legacy_function_call_not_receiptable")
            raw_calls = delta.get("tool_calls")
            if raw_calls is None:
                continue
            if not isinstance(raw_calls, list):
                raise GatewayError(502, "upstream_tool_calls_invalid")
            for raw_call in raw_calls:
                if not isinstance(raw_call, Mapping):
                    raise GatewayError(502, "upstream_tool_calls_invalid")
                call_index = raw_call.get("index")
                if isinstance(call_index, bool) or not isinstance(call_index, int):
                    raise GatewayError(502, "upstream_tool_calls_invalid")
                key = (choice_index, call_index)
                state = fragments.setdefault(
                    key,
                    {"id": "", "type": "", "name": "", "arguments": ""},
                )
                call_id = raw_call.get("id")
                if call_id is not None:
                    if not isinstance(call_id, str) or (
                        state["id"] and state["id"] != call_id
                    ):
                        raise GatewayError(502, "upstream_tool_calls_invalid")
                    state["id"] = call_id
                call_type = raw_call.get("type")
                if call_type is not None:
                    if not isinstance(call_type, str) or (
                        state["type"] and state["type"] != call_type
                    ):
                        raise GatewayError(502, "upstream_tool_calls_invalid")
                    state["type"] = call_type
                function = raw_call.get("function")
                if function is not None:
                    if not isinstance(function, Mapping):
                        raise GatewayError(502, "upstream_tool_calls_invalid")
                    name = function.get("name")
                    if name is not None:
                        if not isinstance(name, str) or (
                            state["name"] and state["name"] != name
                        ):
                            raise GatewayError(502, "upstream_tool_calls_invalid")
                        state["name"] = name
                    arguments = function.get("arguments")
                    if arguments is not None:
                        if not isinstance(arguments, str):
                            raise GatewayError(502, "upstream_tool_calls_invalid")
                        state["arguments"] += arguments
    calls: list[NativeToolCall] = []
    seen: set[str] = set()
    for key in sorted(fragments):
        state = fragments[key]
        if (
            not state["id"]
            or state["id"] in seen
            or state["type"] != "function"
            or not state["name"]
        ):
            raise GatewayError(502, "upstream_tool_calls_invalid")
        seen.add(state["id"])
        calls.append(
            NativeToolCall(
                tool_call_id=state["id"],
                tool_name=state["name"],
                arguments_text=state["arguments"],
            )
        )
    return calls


def _native_tool_calls_contain_protected_value(
    calls: Sequence[NativeToolCall],
    protected_values: Sequence[str],
) -> bool:
    """Scan fully reassembled call fields and decoded argument JSON."""

    protected = tuple(value for value in protected_values if value)
    if not protected:
        return False
    for call in calls:
        if _contains_protected_value(
            {
                "id": call.tool_call_id,
                "name": call.tool_name,
                "arguments": call.arguments_text,
            },
            protected,
        ):
            return True
        try:
            decoded = json.loads(call.arguments_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if _contains_protected_value(decoded, protected):
            return True
    return False


def _execution_sse_events(events: Sequence[bytes], original: Sequence[NativeToolCall],
                          decorated: Sequence[NativeToolCall]) -> list[bytes]:
    """Insert reserved references at the original final brace, across SSE fragments.

    No text/reasoning, call ID/name, business argument character, or event order
    is removed. Only the fragment containing that brace receives extra bytes.
    """
    insertions: dict[str, tuple[int, str]] = {}
    for before, after in zip(original, decorated):
        if before.arguments_text == after.arguments_text:
            continue
        position = before.arguments_text.rfind("}")
        length = len(after.arguments_text) - len(before.arguments_text)
        if (length <= 0 or before.arguments_text[:position] != after.arguments_text[:position]
                or before.arguments_text[position:] != after.arguments_text[position + length:]):
            raise GatewayError(502, "execution_reference_invalid")
        insertions[before.tool_call_id] = (position, after.arguments_text[position:position + length])
    if not insertions:
        return list(events)
    parsed = [_parse_sse_event(raw) for raw in events]
    identities: dict[tuple[int, int], str] = {}
    for event in parsed:
        if event.payload is None:
            continue
        choices = event.payload.get("choices")
        if not isinstance(choices, list):
            continue
        for fallback, choice in enumerate(choices):
            if not isinstance(choice, Mapping) or not isinstance(choice.get("delta"), Mapping):
                continue
            for call in choice["delta"].get("tool_calls", []):
                if isinstance(call.get("id"), str):
                    identities[(choice.get("index", fallback), call["index"])] = call["id"]
    offsets: dict[tuple[int, int], int] = {}
    inserted = set()
    output = []
    for event in parsed:
        if event.payload is None:
            output.append(event.raw)
            continue
        payload = copy.deepcopy(event.payload)
        changed = False
        choices = payload.get("choices")
        for fallback, choice in enumerate(choices if isinstance(choices, list) else []):
            if not isinstance(choice, Mapping) or not isinstance(choice.get("delta"), Mapping):
                continue
            for call in choice["delta"].get("tool_calls", []):
                key = (choice.get("index", fallback), call["index"])
                function = call.get("function")
                fragment = function.get("arguments") if isinstance(function, Mapping) else None
                if not isinstance(fragment, str):
                    continue
                start = offsets.get(key, 0)
                offsets[key] = start + len(fragment)
                call_id = identities.get(key)
                if call_id not in insertions:
                    continue
                position, insertion = insertions[call_id]
                if start <= position < start + len(fragment):
                    local = position - start
                    function["arguments"] = fragment[:local] + insertion + fragment[local:]
                    inserted.add(call_id)
                    changed = True
        if changed:
            canonical = _canonical_client_sse_event(_ParsedSSEEvent(raw=b"", payload=payload))
            assert canonical is not None
            output.append(canonical.raw)
        else:
            output.append(event.raw)
    if inserted != set(insertions):
        raise GatewayError(502, "execution_reference_invalid")
    return output


# Match two SSE line endings without allowing regex backtracking to reinterpret
# one CRLF as the two independent endings CR + LF.
_SSE_EVENT_BOUNDARY = re.compile(br"\r\n\r\n|\r\n\n|\n\r\n|\n\n|\r\r")


@dataclass(frozen=True)
class _ParsedSSEEvent:
    raw: bytes
    payload: Mapping[str, Any] | None
    done: bool = False


class _SSEEventBuffer:
    """Split arbitrary network chunks into complete SSE events."""

    def __init__(self, max_event_bytes: int | None = None) -> None:
        self._buffer = bytearray()
        self._max_event_bytes = max_event_bytes

    def feed(self, chunk: bytes, *, final: bool = False) -> list[bytes]:
        self._buffer.extend(chunk)
        events: list[bytes] = []
        start = 0
        while match := _SSE_EVENT_BOUNDARY.search(self._buffer, start):
            end = match.end()
            if self._max_event_bytes is not None and end - start > self._max_event_bytes:
                raise GatewayError(502, "upstream_buffer_limit_reached")
            events.append(bytes(self._buffer[start:end]))
            start = end
        if start:
            del self._buffer[:start]
        if self._max_event_bytes is not None and len(self._buffer) > self._max_event_bytes:
            raise GatewayError(502, "upstream_buffer_limit_reached")
        if final and self._buffer:
            events.append(bytes(self._buffer))
            self._buffer.clear()
        return events


def _parse_sse_event(raw: bytes) -> _ParsedSSEEvent:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GatewayError(502, "upstream_invalid_stream") from exc
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        elif line == "data":
            data_lines.append("")
    if not data_lines:
        return _ParsedSSEEvent(raw=raw, payload=None)
    data = "\n".join(data_lines).strip()
    if data == "[DONE]":
        return _ParsedSSEEvent(raw=raw, payload=None, done=True)
    if not data:
        return _ParsedSSEEvent(raw=raw, payload=None)
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise GatewayError(502, "upstream_invalid_stream") from exc
    if not isinstance(payload, Mapping):
        raise GatewayError(502, "upstream_invalid_stream")
    return _ParsedSSEEvent(raw=raw, payload=payload)


def _canonical_client_sse_event(
    event: _ParsedSSEEvent,
) -> _ParsedSSEEvent | None:
    """Strip non-data SSE fields and emit one canonical client-safe event."""

    if event.done:
        return _ParsedSSEEvent(
            raw=b"data: [DONE]\n\n",
            payload=None,
            done=True,
        )
    if event.payload is None:
        # Upstream comments, event/id/retry metadata and empty data fields are
        # not part of the OpenAI chat payload.  Dropping them also prevents a
        # non-JSON encoding from bypassing protected-value inspection.
        return None
    try:
        encoded = json.dumps(
            event.payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GatewayError(502, "upstream_invalid_stream") from exc
    return _ParsedSSEEvent(
        raw=b"data: " + encoded + b"\n\n",
        payload=event.payload,
    )


def _canonical_client_sse_events(raw: bytes) -> list[_ParsedSSEEvent]:
    parser = _SSEEventBuffer()
    result: list[_ParsedSSEEvent] = []
    for raw_event in parser.feed(raw, final=True):
        event = _canonical_client_sse_event(_parse_sse_event(raw_event))
        if event is not None:
            result.append(event)
    return result


def _sse_payload_has_finish(payload: Mapping[str, Any] | None) -> bool:
    if payload is None:
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    return any(
        isinstance(choice, Mapping) and choice.get("finish_reason") is not None
        for choice in choices
    )


class _ProtectedSSEQuarantine:
    """Hold material secret prefixes until a later event proves them harmless.

    A protected value can be JSON-escaped or split over several deltas on the
    same or different fragmentable JSON paths.  Prefixes remain tracked from
    their first character for later exact detection; only a material prefix
    (eight characters for real 43-character capabilities) delays delivery.
    """

    def __init__(self, protected_values: Sequence[str], max_pending_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        self._protected = tuple(value for value in protected_values if value)
        self._suffixes: dict[tuple[str | int, ...], str] = {}
        self._cross_path_suffix = ""
        self._visible_suffix = ""
        self._pending: list[_ParsedSSEEvent] = []
        self._pending_bytes = 0
        self._max_pending_bytes = max_pending_bytes

    def _prefix_suffix(self, value: str) -> str:
        return _protected_prefix_suffix(value, self._protected)

    def feed(
        self,
        event: _ParsedSSEEvent,
    ) -> list[_ParsedSSEEvent]:
        if self._protected:
            try:
                event_text = event.raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise GatewayError(502, "upstream_invalid_stream") from exc
            if any(secret in event_text for secret in self._protected):
                raise GatewayError(502, "upstream_protected_value")
            if event.payload is not None:
                if _contains_protected_value(event.payload, self._protected):
                    raise GatewayError(502, "upstream_protected_value")
                fragmentable = _fragmentable_string_leaves(event.payload)
                for path, fragment in fragmentable:
                    combined = self._suffixes.get(path, "") + fragment
                    if any(secret in combined for secret in self._protected):
                        raise GatewayError(502, "upstream_protected_value")
                    suffix = self._prefix_suffix(combined)
                    if suffix:
                        self._suffixes[path] = suffix
                    else:
                        self._suffixes.pop(path, None)
                    if len(self._suffixes) > 4096:
                        raise GatewayError(502, "upstream_buffer_limit_reached")
                for _, fragment in fragmentable:
                    combined = self._cross_path_suffix + fragment
                    if any(secret in combined for secret in self._protected):
                        raise GatewayError(502, "upstream_protected_value")
                    self._cross_path_suffix = self._prefix_suffix(combined)
                for fragment in _model_visible_output_fragments(event.payload):
                    combined = self._visible_suffix + fragment
                    if any(secret in combined for secret in self._protected):
                        raise GatewayError(502, "upstream_protected_value")
                    self._visible_suffix = self._prefix_suffix(combined)
        self._pending_bytes += len(event.raw)
        if self._pending_bytes > self._max_pending_bytes:
            raise GatewayError(502, "upstream_buffer_limit_reached")
        self._pending.append(event)
        if any(
            _has_material_protected_prefix(suffix, self._protected)
            for suffix in (
                *self._suffixes.values(),
                self._cross_path_suffix,
                self._visible_suffix,
            )
        ):
            return []
        ready, self._pending = self._pending, []
        self._pending_bytes = 0
        return ready

    def finish(self) -> list[_ParsedSSEEvent]:
        if any(
            _has_material_protected_prefix(suffix, self._protected)
            for suffix in (
                *self._suffixes.values(),
                self._cross_path_suffix,
                self._visible_suffix,
            )
        ):
            # A truncated stream ending in a material protected-value prefix
            # is not proof that the prefix was harmless.  Fail closed instead
            # of releasing 48 or more bits of a host capability.
            self._pending.clear()
            self._suffixes.clear()
            self._cross_path_suffix = ""
            self._visible_suffix = ""
            raise GatewayError(502, "upstream_protected_value")
        ready, self._pending = self._pending, []
        self._pending_bytes = 0
        self._suffixes.clear()
        self._cross_path_suffix = ""
        self._visible_suffix = ""
        return ready


class _StreamCompletionState:
    """Retain only terminal/output facts, never generated thinking or text."""

    def __init__(self) -> None:
        self.done = False
        self.finished = False
        self.reasoning = False
        self.visible = False
        self.tools = False
        self.error = False
        self.length_limited = False
        self._ended_choices: set[int] = set()

    def feed(self, event: _ParsedSSEEvent) -> None:
        self.done = self.done or event.done
        payload = event.payload
        if payload is None:
            return
        self.error = self.error or "error" in payload
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return
        for fallback_index, choice in enumerate(choices):
            if not isinstance(choice, Mapping):
                continue
            choice_index = choice.get("index", fallback_index)
            if type(choice_index) is not int:
                raise GatewayError(502, "upstream_invalid_stream")
            finish = choice.get("finish_reason")
            generated = any(
                isinstance(choice.get(key), Mapping)
                and any(value not in (None, "", [], {}) for value in choice[key].values())
                for key in ("delta", "message")
            )
            if (self.done or choice_index in self._ended_choices) and (generated or finish is not None):
                raise GatewayError(502, "upstream_invalid_stream", "上游在结束标记后又返回了新的生成内容，本轮未完成。")
            self.finished = self.finished or finish is not None
            self.length_limited = self.length_limited or finish == "length"
            if finish is not None:
                self._ended_choices.add(choice_index)
                if len(self._ended_choices) > 4096:
                    raise GatewayError(502, "upstream_buffer_limit_reached")
            for key in ("delta", "message"):
                value = choice.get(key)
                if not isinstance(value, Mapping):
                    continue
                self.reasoning = self.reasoning or bool(value.get("reasoning_content"))
                self.visible = self.visible or any(bool(value.get(field)) for field in ("content", "refusal", "audio"))
                self.tools = self.tools or bool(value.get("tool_calls")) or bool(value.get("function_call"))

    def validate(self) -> None:
        if self.error:
            return  # Preserve a provider's explicit error event.
        if not (self.done or self.finished):
            raise GatewayError(502, "upstream_incomplete_stream", "上游连接在回复完成前结束，本轮未完成。")
        if self.reasoning and not (self.visible or self.tools):
            if self.length_limited:
                raise GatewayError(502, "upstream_output_limit_reached", "模型用完了本轮输出额度，尚未生成正文。请调高模型输出上限后重试。")
            raise GatewayError(502, "upstream_empty_completion", "模型只返回了思考过程，尚未生成正文或工具调用，本轮未完成。")


class _IncrementalSSEInspector:
    """Bounded independent equivalent of the whole-response protection scan.

    Parsed event fields and fixed-size suffixes provide the second check without
    retaining already delivered reasoning.  Only possible credential prefixes
    need survive event boundaries, including changes of JSON path/channel.
    """

    def __init__(self, protected: Sequence[str]) -> None:
        self.protected = tuple(value for value in protected if value)
        self.paths: dict[tuple[str | int, ...], str] = {}
        self.cross = ""
        self.visible = ""
        self.raw_suffix = b""
        self.longest = max((len(value.encode("utf-8")) for value in self.protected), default=1)

    def feed_raw(self, chunk: bytes) -> None:
        text = self.raw_suffix + chunk
        if any(value.encode("utf-8") in text for value in self.protected):
            raise GatewayError(502, "upstream_protected_value")
        self.raw_suffix = text[-(self.longest - 1):] if self.longest > 1 else b""

    def _append(self, suffix: str, text: str) -> str:
        combined = suffix + text
        if any(value in combined for value in self.protected):
            raise GatewayError(502, "upstream_protected_value")
        return _protected_prefix_suffix(combined, self.protected)

    def feed(self, event: _ParsedSSEEvent) -> None:
        if event.payload is None:
            return
        if _contains_protected_value(event.payload, self.protected):
            raise GatewayError(502, "upstream_protected_value")
        for path, fragment in _fragmentable_string_leaves(event.payload):
            suffix = self._append(self.paths.get(path, ""), fragment)
            if suffix:
                self.paths[path] = suffix
            else:
                self.paths.pop(path, None)
            if len(self.paths) > 4096:
                raise GatewayError(502, "upstream_buffer_limit_reached")
            self.cross = self._append(self.cross, fragment)
        for fragment in _model_visible_output_fragments(event.payload):
            self.visible = self._append(self.visible, fragment)

    def finish(self) -> None:
        if any(_has_material_protected_prefix(value, self.protected) for value in (*self.paths.values(), self.cross, self.visible)):
            raise GatewayError(502, "upstream_protected_value")


def _model_visible_protected_values(
    prepared: PreparedTurn,
) -> tuple[str, ...]:
    """Return protected values that are actually present in the model request."""

    return tuple(
        value
        for value in prepared.session.protected_values
        if value and _contains_protected_value(prepared.payload, (value,))
    )


class _GatewayHandler(BaseHTTPRequestHandler):
    server_version = "StillerRikkaGateway/0.1.1"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> GatewayApplication:
        return self.server.application  # type: ignore[attr-defined]

    @contextmanager
    def _client_write_boundary(self, prepared: PreparedTurn):
        # Fence each write against replacement/generation advancement without
        # allowing an unresponsive client to hold the global turn lock forever.
        # Only the write is timed; preserve the request socket's prior read policy.
        with self.app._lock:
            self.app.assert_response_current(prepared)
            connection = getattr(self, "connection", None)
            previous_timeout = None
            timeout_changed = False
            try:
                if connection is not None:
                    previous_timeout = connection.gettimeout()
                    write_timeout = min(previous_timeout, 10.0) if previous_timeout is not None else 10.0
                    connection.settimeout(write_timeout)
                    timeout_changed = True
                yield
            finally:
                if timeout_changed:
                    try:
                        connection.settimeout(previous_timeout)
                    except OSError:
                        # The client may already have closed; do not hide the
                        # original write failure with a timeout-restoration error.
                        pass

    def _json(self, status: int, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        performance = getattr(self, "_request_performance", None)
        if performance is not None:
            performance.http_status = int(status)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)
        if performance is not None:
            performance.note_client_bytes(len(encoded))

    def _read_json(self, *, max_body_bytes: int | None = None) -> dict[str, Any]:
        performance = getattr(self, "_request_performance", None)
        if performance is not None:
            performance.stage = "body_validation"
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise GatewayError(400, "invalid_content_length") from exc
        if length <= 0:
            raise GatewayError(400, "json_body_required")
        body_limit = (
            self.app.config.max_request_body_bytes
            if max_body_bytes is None
            else max_body_bytes
        )
        if length > body_limit:
            # The rejected body is intentionally not consumed.  Close the
            # persistent connection after the error so unread bytes cannot be
            # interpreted as a second HTTP request.
            self.close_connection = True
            raise GatewayError(413, "request_too_large")
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise GatewayError(415, "application_json_required")
        read_started_at = time.perf_counter()
        try:
            raw = self.rfile.read(length)
        finally:
            if performance is not None:
                performance.body_read_ms = performance._milliseconds(read_started_at)
        if performance is not None:
            performance.body_bytes = len(raw)
            performance.stage = "json_parse"
        parse_started_at = time.perf_counter()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayError(400, "invalid_utf8_json") from exc
        finally:
            if performance is not None:
                performance.json_parse_ms = performance._milliseconds(parse_started_at)
        if not isinstance(payload, dict):
            raise GatewayError(400, "json_object_required")
        if performance is not None:
            messages = payload.get("messages")
            tools = payload.get("tools")
            functions = payload.get("functions")
            performance.message_count = len(messages) if isinstance(messages, list) else 0
            performance.tool_count = (
                (len(tools) if isinstance(tools, list) else 0)
                + (len(functions) if isinstance(functions, list) else 0)
            )
            performance.stream = payload.get("stream") is True
        return payload

    def do_GET(self) -> None:  # noqa: N802
        route = urlsplit(self.path).path
        if route == "/health":
            self._json(200, {"ok": True, "service": "stiller-rikkahub-gateway"})
            return
        headers = _normalize_headers(dict(self.headers.items()))
        if not self.app.authorized(headers):
            self._json(401, GatewayError(401, "unauthorized").payload())
            return
        if route in {"/models", "/v1/models"}:
            self._json(200, self.app.models())
            return
        self._json(404, GatewayError(404, "not_found").payload())

    def do_POST(self) -> None:  # noqa: N802
        route = urlsplit(self.path).path
        if route == DIRECT_GRANT_ROUTE:
            headers = _normalize_headers(dict(self.headers.items()))
            if not self.app.authorized_human(headers):
                # The body has not been read.  Close HTTP/1.1 so its bytes can
                # never be parsed as a pipelined second request.
                self.close_connection = True
                self._json(401, GatewayError(401, "unauthorized").payload())
                return
            try:
                payload = self._read_json(
                    max_body_bytes=self.app.config.max_direct_grant_body_bytes
                )
                self._json(200, self.app.issue_direct_grant(payload))
            except GatewayError as exc:
                # This human-only path intentionally emits no failure or
                # performance log: a future grant format must not accidentally
                # turn an opaque reference into telemetry.
                self._json(exc.status, exc.payload())
            return
        if route not in {"/chat/completions", "/v1/chat/completions"}:
            self._json(404, GatewayError(404, "not_found").payload())
            return
        headers = _normalize_headers(dict(self.headers.items()))
        if not self.app.authorized(headers):
            self._json(401, GatewayError(401, "unauthorized").payload())
            return
        performance = _RequestPerformance()
        performance.note_model(self.app.config.upstream_model)
        self._request_performance = performance
        prepared: PreparedTurn | None = None
        completed = False
        try:
            payload = self._read_json()
            performance.stage = "prepare_turn"
            prepare_started_at = time.perf_counter()
            try:
                prepared = self.app.prepare_turn(payload, headers)
                performance.continuation = prepared.continuation
                for key in ("stable_context_changed", "dynamic_context_changed", "wire_tools_changed"):
                    setattr(performance, key, prepared.cache_comparison[key])
                performance.client_input_prefix_preserved = prepared.cache_comparison["prior_input_prefix_preserved"]
            finally:
                performance.prepare_turn_ms = performance._milliseconds(
                    prepare_started_at
                )
            performance.injected_message_bytes = len(
                _canonical(prepared.session.message).encode("utf-8")
            )
            bundle = prepared.session.context_bundle
            performance.context_layout = bundle["contract"] if bundle is not None else "legacy"
            if bundle is not None:
                performance.stable_context_bytes = (
                    len(_canonical(bundle["stable_message"]).encode("utf-8"))
                    if bundle["stable_message"] is not None else 0
                )
                performance.dynamic_context_bytes = (
                    len(_canonical(bundle["dynamic_message"]).encode("utf-8"))
                    if bundle["dynamic_message"] is not None else 0
                )
                performance.injected_message_bytes = (
                    performance.stable_context_bytes + performance.dynamic_context_bytes
                )
            if payload.get("stream") is True:
                self._proxy_stream(prepared)
            else:
                self._proxy_json(prepared)
            completed = not performance.failed
        except GatewayError as exc:
            performance.mark_failed()
            if prepared is not None:
                self.app.finish_turn(prepared, keep_for_tools=False)
            _log_gateway_failure(
                exc.code,
                streamed_prefix=False,
                validation_diagnostic=exc.validation_diagnostic,
                protected_values=(
                    tuple(prepared.session.protected_values) if prepared is not None else ()
                ),
            )
            self._json(exc.status, exc.payload())
        finally:
            if not completed:
                performance.mark_failed()
            performance.emit()
            del self._request_performance

    def _proxy_json(self, prepared: PreparedTurn) -> None:
        performance = getattr(self, "_request_performance", None)
        if performance is not None:
            performance.stage = "upstream_encode"
        encoded_payload = self.app.encode_upstream_payload(prepared.payload)
        if performance is not None:
            performance.upstream_request_bytes = len(encoded_payload)
            performance.start_upstream()
        try:
            with self.app.upstream.stream(
                "POST",
                self.app.upstream_url(),
                headers=self.app.upstream_headers(),
                content=encoded_payload,
            ) as response:
                if performance is not None:
                    performance.note_upstream_headers(response.status_code)
                raw_buffer = bytearray()
                for chunk in response.iter_bytes():
                    if performance is not None:
                        performance.note_upstream_chunk(len(chunk))
                    if len(raw_buffer) + len(chunk) > self.app.config.max_body_bytes:
                        raise GatewayError(502, "upstream_response_too_large")
                    raw_buffer.extend(chunk)
                if performance is not None:
                    performance.finish_upstream()
                response_status = response.status_code
        except httpx.RequestError as exc:
            self.app.finish_turn(prepared, keep_for_tools=False)
            raise GatewayError(502, "upstream_unavailable") from exc
        raw = bytes(raw_buffer)
        if performance is not None:
            performance.stage = "response_validation"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {
                "error": {
                    "message": "上游返回了非 JSON 响应。",
                    "type": "upstream_error",
                    "code": "upstream_invalid_response",
                }
            }
        if performance is not None:
            performance.note_usage(body)
        if _contains_protected_value(
            body,
            tuple(prepared.session.protected_values),
        ):
            raise GatewayError(502, "upstream_protected_value")
        keep = response_status < 400 and self.app.response_requires_tool_continuation(body)
        bindings: dict[str, ToolExecutionBinding] = {}
        if keep:
            calls = self.app.response_tool_calls(body)
            decorated = self.app.decorate_execution_calls(prepared, calls)
            replacements = {call.tool_call_id: call.arguments_text for call in decorated}
            for choice in body.get("choices", []):
                message = choice.get("message", {})
                for call in message.get("tool_calls", []):
                    call["function"]["arguments"] = replacements[call["id"]]
            if performance is not None:
                performance.stage = "tool_validation"
            bindings = self.app.bind_response_tool_calls(prepared, decorated)
        if isinstance(body, dict) and response_status < 400:
            body["model"] = self.app.config.public_model
        delivered = False
        self.app.finish_turn(
            prepared,
            keep_for_tools=keep,
            tool_call_ids=self.app.response_tool_call_ids(body),
            tool_call_bindings=bindings,
        )
        try:
            if performance is not None:
                performance.stage = "client_write"
            with self._client_write_boundary(prepared):
                self._json(response_status, body if isinstance(body, dict) else {})
            delivered = True
            self.app.mark_response_delivered(prepared)
            if performance is not None:
                performance.stage = "complete"
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            # A native tool call is usable only after its complete response has
            # reached the client.  If delivery is aborted, close the wake instead
            # of leaving the gateway permanently armed for a result the client
            # could never execute.
            if performance is not None:
                performance.mark_failed()
        finally:
            if keep and not delivered:
                self.app.finish_turn(prepared, keep_for_tools=False)

    def _proxy_stream(self, prepared: PreparedTurn) -> None:
        keep = False
        delivered = False
        headers_sent = False
        bindings: dict[str, ToolExecutionBinding] = {}
        performance = getattr(self, "_request_performance", None)

        def send_sse_headers() -> None:
            nonlocal headers_sent
            if headers_sent:
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            headers_sent = True
            if performance is not None:
                performance.http_status = 200

        def write_sse(raw: bytes) -> None:
            with self._client_write_boundary(prepared):
                send_sse_headers()
                self.wfile.write(raw)
                self.wfile.flush()
            if performance is not None:
                performance.note_client_bytes(len(raw))

        def write_completion_event(event: _ParsedSSEEvent) -> None:
            # Reconcile only the public model identifier after the original
            # event passed protected-value checks and any tool commit barrier.
            # Nested data/arguments, usage and provider error events are kept.
            if event.payload is not None and "error" not in event.payload:
                payload = dict(event.payload)
                payload["model"] = self.app.config.public_model
                canonical = _canonical_client_sse_event(
                    _ParsedSSEEvent(raw=b"", payload=payload)
                )
                assert canonical is not None
                write_sse(canonical.raw)
            else:
                write_sse(event.raw)

        def fail_stream(error: GatewayError) -> None:
            # HTTP status is already committed, but a native OpenAI error event
            # remains machine-readable.  Do not fabricate a successful choice,
            # a tool result or a receipt, and never release the deferred batch.
            self.close_connection = True
            if performance is not None:
                performance.mark_failed()
            self.app.finish_turn(prepared, keep_for_tools=False)
            protected = tuple(prepared.session.protected_values)
            _log_gateway_failure(
                error.code, streamed_prefix=True, protected_values=protected,
                validation_diagnostic=error.validation_diagnostic,
            )
            diagnostic = _safe_validation_diagnostic(
                error.validation_diagnostic, protected_values=protected
            )
            code = _observable_failure_code(error.code)
            payload = GatewayError(
                error.status,
                code,
                _tool_validation_message(diagnostic, protected_values=protected) if code == "tool_arguments_schema_invalid"
                else _tool_not_advertised_message(protected) if code == "tool_not_advertised"
                else "[ST 网关 · " + code + "] " + _STREAM_FAILURE_MESSAGES.get(code, "本轮响应未完成，请根据错误码检查后重试。"),
                validation_diagnostic=diagnostic,
            ).payload()
            raw_error = (
                b"data: "
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                + b"\n\ndata: [DONE]\n\n"
            )
            if any(value and value in raw_error.decode("utf-8") for value in protected):
                return
            try:
                write_sse(raw_error)
            except GatewayError:
                # A superseded request must not emit even a late error event.
                return
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                _log_gateway_failure(
                    "client_disconnected", streamed_prefix=True, protected_values=protected
                )

        if performance is not None:
            performance.stage = "upstream_encode"
        encoded_payload = self.app.encode_upstream_payload(prepared.payload)
        if performance is not None:
            performance.upstream_request_bytes = len(encoded_payload)
            performance.start_upstream()
        try:
            with self.app.upstream.stream(
                "POST",
                self.app.upstream_url(),
                headers=self.app.upstream_headers(),
                content=encoded_payload,
            ) as response:
                if performance is not None:
                    performance.note_upstream_headers(response.status_code)

                def upstream_chunks():
                    transferred = 0
                    for chunk in response.iter_bytes():
                        if performance is not None:
                            performance.note_upstream_chunk(len(chunk))
                        transferred += len(chunk)
                        if transferred > self.app.config.max_stream_bytes:
                            raise GatewayError(502, "upstream_stream_limit_reached")
                        # Split an already arrived network chunk locally. Do
                        # not use iter_bytes(chunk_size=...), which can wait
                        # for more tokens before yielding the first SSE event.
                        for offset in range(0, len(chunk), 16 * 1024):
                            yield chunk[offset:offset + 16 * 1024]

                raw_buffer = bytearray()
                if response.status_code >= 400:
                    for chunk in upstream_chunks():
                        if len(raw_buffer) + len(chunk) > self.app.config.max_body_bytes:
                            raise GatewayError(502, "upstream_response_too_large")
                        raw_buffer.extend(chunk)
                    if performance is not None:
                        performance.finish_upstream()
                    raw = bytes(raw_buffer)
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        body = GatewayError(502, "upstream_error").payload()
                    if _contains_protected_value(
                        body,
                        tuple(prepared.session.protected_values),
                    ):
                        raise GatewayError(502, "upstream_protected_value")
                    self.app.finish_turn(prepared, keep_for_tools=False)
                    if performance is not None:
                        performance.stage = "client_write"
                    with self._client_write_boundary(prepared):
                        self._json(response.status_code, body)
                    if performance is not None:
                        performance.stage = "complete"
                    return

                protected = tuple(prepared.session.protected_values)
                if _model_visible_protected_values(prepared):
                    if performance is not None:
                        performance.full_buffer = True
                    # A challenge/capability present in model-visible history can
                    # be repeated at any later point.  Preserve the strict
                    # all-or-nothing response boundary for those rare turns.
                    for chunk in upstream_chunks():
                        if len(raw_buffer) + len(chunk) > self.app.config.max_body_bytes:
                            raise GatewayError(502, "upstream_buffer_limit_reached")
                        raw_buffer.extend(chunk)
                    if performance is not None:
                        performance.finish_upstream()
                    raw = bytes(raw_buffer)
                    if _buffered_sse_contains_protected_value(raw, protected):
                        raise GatewayError(502, "upstream_protected_value")
                    client_events = _canonical_client_sse_events(raw)
                    if performance is not None:
                        for event in client_events:
                            performance.note_usage(event.payload)
                    if not any(event.payload is not None for event in client_events):
                        raise GatewayError(502, "upstream_invalid_stream")
                    completion = _StreamCompletionState()
                    for event in client_events:
                        completion.feed(event)
                    completion.validate()
                    scanner = _StreamToolCallScanner()
                    if scanner.feed(raw, final=True):
                        keep = True
                        calls = _buffered_sse_tool_calls(raw)
                        if _native_tool_calls_contain_protected_value(
                            calls,
                            protected,
                        ):
                            raise GatewayError(502, "upstream_protected_value")
                        if scanner.tool_call_ids != {
                            call.tool_call_id for call in calls
                        }:
                            raise GatewayError(502, "upstream_tool_calls_invalid")
                        decorated = self.app.decorate_execution_calls(prepared, calls)
                        client_events = [_parse_sse_event(item) for item in _execution_sse_events(
                            [event.raw for event in client_events], calls, decorated)]
                        if performance is not None:
                            performance.stage = "tool_validation"
                        bindings = self.app.bind_response_tool_calls(prepared, decorated)
                    self.app.finish_turn(
                        prepared,
                        keep_for_tools=keep,
                        tool_call_ids=scanner.tool_call_ids,
                        tool_call_bindings=bindings,
                    )
                    for event in client_events:
                        write_completion_event(event)
                    delivered = True
                    self.app.mark_response_delivered(prepared)
                    if performance is not None:
                        performance.stage = "complete"
                    return

                # The normal path streams reasoning/content event-by-event.  A
                # possible protected-value prefix is quarantined, and the first
                # native tool-call event starts a tail buffer.  That tail is not
                # released until the complete call batch passes Schema, policy,
                # argument-hash and replay binding.
                parser = _SSEEventBuffer(self.app.config.max_body_bytes)
                quarantine = _ProtectedSSEQuarantine(protected, self.app.config.max_body_bytes)
                inspector = _IncrementalSSEInspector(protected)
                completion = _StreamCompletionState()
                deferred_tail: list[bytes] = []
                deferred_bytes = 0
                tool_tail_started = False
                defer_started = False
                saw_data = False

                def release_events(events: Sequence[_ParsedSSEEvent]) -> None:
                    nonlocal defer_started, tool_tail_started, saw_data, deferred_bytes
                    for event in events:
                        if event.payload is not None:
                            saw_data = True
                            if performance is not None:
                                performance.note_usage(event.payload)
                        tool_related = (
                            event.payload is not None
                            and self.app.response_requires_tool_continuation(
                                event.payload
                            )
                        )
                        terminal = event.done or _sse_payload_has_finish(event.payload)
                        if tool_related:
                            tool_tail_started = True
                            if performance is not None:
                                performance.tool_tail = True
                        if tool_tail_started or defer_started or terminal:
                            # From the first tool/terminal event onward preserve
                            # exact upstream ordering behind one commit barrier.
                            defer_started = True
                            deferred_bytes += len(event.raw)
                            if deferred_bytes > self.app.config.max_body_bytes:
                                raise GatewayError(502, "upstream_buffer_limit_reached")
                            deferred_tail.append(event.raw)
                        else:
                            write_completion_event(event)

                for chunk in upstream_chunks():
                    for raw_event in parser.feed(chunk):
                        inspector.feed_raw(raw_event)
                        event = _canonical_client_sse_event(
                            _parse_sse_event(raw_event)
                        )
                        if event is not None:
                            inspector.feed(event)
                            completion.feed(event)
                            release_events(quarantine.feed(event))
                if performance is not None:
                    performance.finish_upstream()
                for raw_event in parser.feed(b"", final=True):
                    inspector.feed_raw(raw_event)
                    event = _canonical_client_sse_event(
                        _parse_sse_event(raw_event)
                    )
                    if event is not None:
                        inspector.feed(event)
                        completion.feed(event)
                        release_events(quarantine.feed(event))
                release_events(quarantine.finish())

                if not saw_data:
                    raise GatewayError(502, "upstream_invalid_stream")
                # The independent rolling inspector and quarantine must both
                # finish cleanly before committing the bounded terminal/tool
                # tail. Already delivered thinking is never retained here.
                inspector.finish()
                completion.validate()
                raw = b"".join(deferred_tail)
                scanner = _StreamToolCallScanner()
                keep = scanner.feed(raw, final=True)
                if keep:
                    calls = _buffered_sse_tool_calls(raw)
                    if _native_tool_calls_contain_protected_value(
                        calls,
                        protected,
                    ):
                        raise GatewayError(502, "upstream_protected_value")
                    if scanner.tool_call_ids != {
                        call.tool_call_id for call in calls
                    }:
                        raise GatewayError(502, "upstream_tool_calls_invalid")
                    decorated = self.app.decorate_execution_calls(prepared, calls)
                    deferred_tail = _execution_sse_events(deferred_tail, calls, decorated)
                    if performance is not None:
                        performance.stage = "tool_validation"
                    bindings = self.app.bind_response_tool_calls(prepared, decorated)
                self.app.finish_turn(
                    prepared,
                    keep_for_tools=keep,
                    tool_call_ids=scanner.tool_call_ids,
                    tool_call_bindings=bindings,
                )
                for raw_event in deferred_tail:
                    write_completion_event(_parse_sse_event(raw_event))
                if not headers_sent:
                    send_sse_headers()
                delivered = True
                self.app.mark_response_delivered(prepared)
                if performance is not None:
                    performance.stage = "complete"
        except GatewayError as exc:
            if not headers_sent:
                raise
            fail_stream(exc)
        except httpx.RequestError as exc:
            failure = GatewayError(502, "upstream_unavailable")
            if not headers_sent:
                raise failure from exc
            fail_stream(failure)
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            TimeoutError,
        ):
            keep = False
            if performance is not None:
                performance.stage = "client_write"
                performance.mark_failed()
            _log_gateway_failure(
                "client_disconnected",
                streamed_prefix=headers_sent,
                protected_values=tuple(prepared.session.protected_values),
            )
        finally:
            if not delivered:
                self.app.finish_turn(prepared, keep_for_tools=False)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never log request paths, headers, model prompts, or credentials.
        return


def build_application_from_env() -> GatewayApplication:
    return GatewayApplication(GatewayConfig.from_env())


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    # Keep normal libraries at WARNING while emitting the dedicated, value-free
    # request timing record at INFO.
    _PERFORMANCE_LOGGER.setLevel(logging.INFO)
    application = build_application_from_env()
    server = ThreadingHTTPServer(
        (application.config.bind_host, application.config.bind_port),
        _GatewayHandler,
    )
    server.application = application  # type: ignore[attr-defined]
    server.serve_forever()


if __name__ == "__main__":
    main()
