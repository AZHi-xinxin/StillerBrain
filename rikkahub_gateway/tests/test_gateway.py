from __future__ import annotations

import json
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

from rikkahub_gateway.server import (
    CONTEXT_MARKER,
    MAX_RETIRED_TOOL_CALL_IDS,
    GatewayApplication,
    GatewayConfig,
    GatewayError,
    _SSEEventBuffer,
    _StreamToolCallScanner,
    _advertised_tool_catalog,
    _buffered_sse_contains_protected_value,
    _contains_protected_value,
    _digest,
    _extract_protected_tool_values,
    _observable_failure_code,
    _parse_sse_event,
    _stream_finish_requires_tools,
)
from rikkahub_gateway.tool_execution import NativeToolCall
from runtime.onboarding import OPTIONAL_BRAIN_NOTICE


class FakeControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.seq = 0
        self.wake_expires_at: object = "2099-01-01T00:00:00Z"

    def post(self, path: str, payload: dict) -> dict:
        self.calls.append((path, dict(payload)))
        if path == "/v1/host/wakes":
            self.seq += 1
            result = {
                "wake_id": f"wake-{self.seq}",
                "wake_seq": self.seq,
                "wake_capability": f"capability-{self.seq}",
            }
            if self.wake_expires_at is not None:
                result["expires_at"] = self.wake_expires_at
            return result
        if path == "/v1/host/context/prepare":
            message = {"role": "system", "content": OPTIONAL_BRAIN_NOTICE}
            return {
                "decision": "context_prepared",
                "message": message,
                "context_hash": _digest(message),
            }
        if path == "/v1/host/context/confirm":
            return {"decision": "injected"}
        if path == "/v1/host/context/close":
            return {"decision": "closed"}
        raise AssertionError(path)


class FakeHumanControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post(self, path: str, payload: dict) -> dict:
        self.calls.append((path, dict(payload)))
        return {
            "grant_ref": "stgrant_test-one-use-value",
            "expires_at": "2099-01-01T00:00:00Z",
            "authorized_scopes": ["learning_memory"],
            "status": "pending",
            "internal_grant_id": "must-not-cross-the-gateway",
        }


def config() -> GatewayConfig:
    return GatewayConfig(
        gateway_token="gateway-token-that-is-at-least-32-characters",
        control_url="http://control.test",
        host_token="host-token-that-is-at-least-32-characters",
        upstream_base_url="http://upstream.test/v1",
        upstream_api_key="upstream-key",
        public_model="stiller-rikka",
        upstream_model="real-model",
    )


class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.control = FakeControl()
        self.app = GatewayApplication(config(), control=self.control)
        self.headers = {
            "Authorization": f"Bearer {config().gateway_token}",
            "X-ST-Thread-ID": "thread:test",
        }

    @staticmethod
    def request(messages: list[dict], **extra: object) -> dict:
        return {"model": "stiller-rikka", "messages": messages, **extra}

    @staticmethod
    def bound_tools() -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "workspace_shell",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

    @staticmethod
    def native_call(call_id: str, command: str) -> NativeToolCall:
        return NativeToolCall(
            tool_call_id=call_id,
            tool_name="workspace_shell",
            arguments_text=json.dumps(
                {"command": command}, ensure_ascii=False, separators=(",", ":")
            ),
        )

    @staticmethod
    def wire_call(call: NativeToolCall) -> dict:
        return {
            "id": call.tool_call_id,
            "type": "function",
            "function": {
                "name": call.tool_name,
                "arguments": call.arguments_text,
            },
        }

    def arm_bound_calls(
        self, prepared, calls: list[NativeToolCall]
    ) -> None:
        bindings = self.app.bind_response_tool_calls(prepared, calls)
        self.app.finish_turn(
            prepared,
            keep_for_tools=True,
            tool_call_ids=[call.tool_call_id for call in calls],
            tool_call_bindings=bindings,
        )

    def settle_then_arm(
        self,
        settled_calls: list[NativeToolCall],
        current_calls: list[NativeToolCall],
    ) -> tuple[list[dict], dict, dict[str, str], object]:
        tools = self.bound_tools()
        user = {"role": "user", "content": "run the bounded tool chain"}
        first = self.app.prepare_turn(
            self.request([user], tools=tools), self.headers
        )
        self.arm_bound_calls(first, settled_calls)
        results = {
            call.tool_call_id: json.dumps(
                {"call": call.tool_call_id, "exitCode": index},
                separators=(",", ":"),
            )
            for index, call in enumerate([*settled_calls, *current_calls])
        }
        first_continuation = self.app.prepare_turn(
            self.request(
                [
                    user,
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [self.wire_call(call) for call in settled_calls],
                    },
                    *[
                        {
                            "role": "tool",
                            "tool_call_id": call.tool_call_id,
                            "content": results[call.tool_call_id],
                        }
                        for call in settled_calls
                    ],
                ],
                tools=tools,
            ),
            self.headers,
        )
        self.arm_bound_calls(first_continuation, current_calls)
        return tools, user, results, first.session

    def merged_continuation_messages(
        self,
        user: dict,
        calls: list[NativeToolCall],
        results: dict[str, str],
        *,
        result_order: list[str] | None = None,
    ) -> list[dict]:
        order = result_order or [call.tool_call_id for call in calls]
        return [
            user,
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [self.wire_call(call) for call in calls],
            },
            *[
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": results[call_id],
                }
                for call_id in order
            ],
        ]

    def test_new_user_turn_injects_exact_context_before_confirm(self) -> None:
        turn = self.app.prepare_turn(
            self.request([{"role": "user", "content": "你好"}]), self.headers
        )
        self.assertEqual("real-model", turn.payload["model"])
        injected = turn.payload["messages"][0]
        self.assertEqual({"role": "system", "content": OPTIONAL_BRAIN_NOTICE}, injected)
        self.assertEqual(injected, turn.session.message)
        self.assertEqual(
            [
                "/v1/host/wakes",
                "/v1/host/context/prepare",
                "/v1/host/context/confirm",
            ],
            [path for path, _ in self.control.calls],
        )
        prepare = next(
            payload
            for path, payload in self.control.calls
            if path == "/v1/host/context/prepare"
        )
        self.assertFalse(prepare["source_frame"]["prior_assistant_present"])
        self.assertTrue(prepare["source_frame"]["first_user_turn"])

    def test_stable_thread_sends_bounded_text_only_source_frame(self) -> None:
        long_text = "灯塔" * 700
        messages = [
            {"role": "user", "content": "old-user"},
            {"role": "assistant", "content": "old-assistant"},
            {"role": "user", "content": "recent-user"},
            {
                "role": "assistant",
                "content": [
                    {"type": "input_text", "text": "recent-assistant"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,secret"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": long_text},
                    {"type": "image_url", "text": "must-not-enter-source-frame"},
                ],
            },
        ]
        turn = self.app.prepare_turn(
            self.request(messages),
            {**self.headers, "X-Request-ID": "event:source-frame"},
        )
        prepare = next(
            payload
            for path, payload in self.control.calls
            if path == "/v1/host/context/prepare"
        )
        frame = prepare["source_frame"]

        self.assertTrue(frame["lineage_stable"])
        self.assertTrue(frame["prior_assistant_present"])
        self.assertFalse(frame["first_user_turn"])
        self.assertEqual("thread:test", frame["thread_id"])
        self.assertEqual("event:source-frame", frame["source_event_id"])
        self.assertEqual(long_text, frame["query_text"])
        self.assertEqual(
            ["assistant", "user"],
            [item["role"] for item in frame["capture_items"]],
        )
        self.assertEqual("recent-assistant", frame["capture_items"][0]["content"])
        self.assertEqual(long_text[:1200], frame["capture_items"][1]["content"])
        self.assertNotIn("old-user", json.dumps(frame["capture_items"], ensure_ascii=False))
        self.assertNotIn("recent-user", json.dumps(frame["capture_items"], ensure_ascii=False))
        self.assertNotIn("must-not-enter-source-frame", json.dumps(frame, ensure_ascii=False))
        self.assertNotIn("base64", json.dumps(frame, ensure_ascii=False))
        self.app.finish_turn(turn, keep_for_tools=False)

    def test_latest_image_only_user_does_not_reuse_older_query_text(self) -> None:
        turn = self.app.prepare_turn(
            self.request(
                [
                    {"role": "user", "content": "older textual question"},
                    {"role": "assistant", "content": "older answer"},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,must-not-enter-recall"
                                },
                            }
                        ],
                    },
                ]
            ),
            {**self.headers, "X-Request-ID": "event:image-only-user"},
        )
        prepare = next(
            payload
            for path, payload in self.control.calls
            if path == "/v1/host/context/prepare"
        )
        frame = prepare["source_frame"]

        self.assertEqual("", frame["query_text"])
        self.assertNotIn("must-not-enter-recall", json.dumps(frame, ensure_ascii=False))
        self.app.finish_turn(turn, keep_for_tools=False)

    def test_missing_explicit_thread_header_disables_ephemeral_capture(self) -> None:
        headers = {"Authorization": f"Bearer {config().gateway_token}"}
        turn = self.app.prepare_turn(
            self.request(
                [
                    {"role": "assistant", "content": "之前的话"},
                    {"role": "user", "content": "仍可用于本轮召回的问题"},
                ]
            ),
            headers,
        )
        prepare = next(
            payload
            for path, payload in self.control.calls
            if path == "/v1/host/context/prepare"
        )
        frame = prepare["source_frame"]

        self.assertFalse(frame["lineage_stable"])
        self.assertIsNone(frame["thread_id"])
        self.assertTrue(frame["prior_assistant_present"])
        self.assertFalse(frame["first_user_turn"])
        self.assertEqual([], frame["capture_items"])
        self.assertEqual("仍可用于本轮召回的问题", frame["query_text"])
        self.assertTrue(frame["source_event_id"].startswith("gateway-"))
        self.app.finish_turn(turn, keep_for_tools=False)

    def test_first_user_turn_is_structural_not_a_greeting_guess(self) -> None:
        cases = (
            ([{"role": "system", "content": "synthetic"},
              {"role": "user", "content": "一段很长的普通请求"}], True),
            ([{"role": "user", "content": "阿止"},
              {"role": "user", "content": "补一句"}], False),
            ([{"role": "user", "content": "阿止"},
              {"role": "assistant", "content": "在"},
              {"role": "user", "content": "继续"}], False),
        )
        for index, (messages, expected) in enumerate(cases):
            with self.subTest(index=index):
                frame = self.app._source_frame(
                    messages,
                    thread_id="thread:test",
                    lineage_stable=True,
                    source_event_id=f"event:first-{index}",
                )
                self.assertIs(expected, frame["first_user_turn"])

    def test_upstream_payload_preserves_reasoning_and_tool_contract(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "stbrain_health",
                    "description": "Read-only health check",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        tool_choice = {
            "type": "function",
            "function": {"name": "stbrain_health"},
        }
        request = self.request(
            [{"role": "user", "content": "检查健康"}],
            stream=True,
            thinking={"type": "enabled"},
            reasoning={"effort": "high"},
            reasoning_effort="high",
            tools=tools,
            tool_choice=tool_choice,
        )

        turn = self.app.prepare_turn(request, self.headers)

        self.assertEqual({"type": "enabled"}, turn.payload["thinking"])
        self.assertEqual({"effort": "high"}, turn.payload["reasoning"])
        self.assertEqual("high", turn.payload["reasoning_effort"])
        self.assertEqual(tools, turn.payload["tools"])
        self.assertEqual(tool_choice, turn.payload["tool_choice"])
        self.assertIs(True, turn.payload["stream"])
        self.assertEqual({"type": "enabled"}, request["thinking"])
        self.assertIn("reasoning", request)
        self.assertIn("reasoning_effort", request)

    def test_advertised_tool_catalog_is_hashed_bound_and_minimal(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "HomeControl",
                    "description": "private prose that must not cross the control plane",
                    "parameters": {
                        "type": "object",
                        "properties": {"room": {"type": "string"}},
                        "required": ["room"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "weather_lookup",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]
        request = self.request(
            [{"role": "user", "content": "我到家了"}], tools=tools
        )

        turn = self.app.prepare_turn(request, self.headers)
        prepare = next(
            payload
            for path, payload in self.control.calls
            if path == "/v1/host/context/prepare"
        )
        catalog = prepare["advertised_tools"]

        self.assertEqual("advertised-tools/1", catalog["contract"])
        self.assertTrue(catalog["catalog_complete"])
        self.assertEqual(
            ["HomeControl", "weather_lookup"],
            [entry["canonical_name"] for entry in catalog["entries"]],
        )
        expected_home_hash = _digest(tools[0]["function"]["parameters"])
        self.assertEqual(expected_home_hash, catalog["entries"][0]["schema_hash"])
        self.assertEqual(_digest(catalog["entries"]), catalog["catalog_hash"])
        self.assertEqual(
            _digest(
                {
                    "messages": [{"role": "user", "content": "我到家了"}],
                    "advertised_tools": catalog,
                }
            ),
            prepare["source_digest"],
        )
        self.assertEqual(catalog, turn.session.advertised_tools)
        self.assertEqual(tools, turn.payload["tools"])
        self.assertEqual(tools, request["tools"])
        self.assertNotIn("description", json.dumps(catalog))
        self.assertNotIn("properties", json.dumps(catalog))

    def test_legacy_functions_share_the_same_catalog_contract(self) -> None:
        functions = [
            {
                "name": "legacy_weather",
                "description": "legacy function",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            }
        ]

        catalog = _advertised_tool_catalog({"functions": functions})

        self.assertTrue(catalog["catalog_complete"])
        self.assertEqual("legacy_weather", catalog["entries"][0]["canonical_name"])
        self.assertEqual(
            _digest(functions[0]["parameters"]),
            catalog["entries"][0]["schema_hash"],
        )

    def test_invalid_or_duplicate_tool_catalog_is_explicitly_incomplete(self) -> None:
        duplicate = {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "same_name",
                        "parameters": {"type": "object"},
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "same_name",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
                {"type": "unsupported-provider-tool"},
            ]
        }

        catalog = _advertised_tool_catalog(duplicate)

        self.assertFalse(catalog["catalog_complete"])
        self.assertEqual(["same_name"], [item["canonical_name"] for item in catalog["entries"]])
        self.assertEqual(_digest(catalog["entries"]), catalog["catalog_hash"])

    def test_tool_continuation_rejects_missing_or_changed_catalog(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "HomeControl",
                    "parameters": {
                        "type": "object",
                        "properties": {"power": {"type": "boolean"}},
                    },
                },
            }
        ]
        first = self.app.prepare_turn(
            self.request([{"role": "user", "content": "开灯"}], tools=tools),
            self.headers,
        )
        self.app.finish_turn(first, keep_for_tools=True, tool_call_ids=["call-home"])
        continuation_messages = [
            {"role": "user", "content": "开灯"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call-home", "type": "function"}],
            },
            {"role": "tool", "tool_call_id": "call-home", "content": "ok"},
        ]

        with self.assertRaises(GatewayError) as missing:
            self.app.prepare_turn(self.request(continuation_messages), self.headers)
        self.assertEqual("tool_continuation_catalog_missing", missing.exception.code)
        self.assertEqual({"call-home"}, first.session.expected_tool_call_ids)

        changed_tools = json.loads(json.dumps(tools))
        changed_tools[0]["function"]["parameters"]["properties"]["power"] = {
            "type": "string"
        }
        with self.assertRaises(GatewayError) as changed:
            self.app.prepare_turn(
                self.request(continuation_messages, tools=changed_tools), self.headers
            )
        self.assertEqual("tool_continuation_catalog_mismatch", changed.exception.code)
        self.assertEqual({"call-home"}, first.session.expected_tool_call_ids)

        continued = self.app.prepare_turn(
            self.request(continuation_messages, tools=tools), self.headers
        )
        self.assertTrue(continued.continuation)
        self.assertEqual(first.session.wake_id, continued.session.wake_id)

    def test_incomplete_catalog_cannot_authorize_a_tool_continuation(self) -> None:
        malformed_tools = [
            {
                "type": "function",
                "function": {"name": "missing_parameters"},
            }
        ]
        first = self.app.prepare_turn(
            self.request(
                [{"role": "user", "content": "try"}], tools=malformed_tools
            ),
            self.headers,
        )
        self.assertFalse(first.session.advertised_tools["catalog_complete"])
        self.app.finish_turn(first, keep_for_tools=True, tool_call_ids=["call-bad"])

        with self.assertRaises(GatewayError) as caught:
            self.app.prepare_turn(
                self.request(
                    [
                        {"role": "user", "content": "try"},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{"id": "call-bad", "type": "function"}],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "call-bad",
                            "content": "ok",
                        },
                    ],
                    tools=malformed_tools,
                ),
                self.headers,
            )
        self.assertEqual("tool_continuation_catalog_incomplete", caught.exception.code)

    def test_real_upstream_model_id_can_be_public_without_changing_injection(self) -> None:
        model_id = "deepseek-v4-flash-vision-exp"
        direct = GatewayApplication(
            replace(config(), public_model=model_id, upstream_model=model_id),
            control=FakeControl(),
        )
        self.addCleanup(direct.upstream.close)

        turn = direct.prepare_turn(
            {"model": model_id, "messages": [{"role": "user", "content": "你好"}]},
            self.headers,
        )

        self.assertEqual(model_id, direct.models()["data"][0]["id"])
        self.assertEqual(model_id, turn.payload["model"])
        self.assertEqual(
            {"role": "system", "content": OPTIONAL_BRAIN_NOTICE},
            turn.payload["messages"][0],
        )

    def test_tool_continuation_reuses_wake_and_next_user_gets_new_wake(self) -> None:
        first = self.app.prepare_turn(
            self.request([{"role": "user", "content": "开始"}]), self.headers
        )
        self.app.finish_turn(
            first,
            keep_for_tools=True,
            tool_call_ids=["call-1"],
        )
        tool_turn = self.app.prepare_turn(
            self.request(
                [
                    {"role": "user", "content": "开始"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "call-1", "type": "function"}],
                    },
                    {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
                ]
            ),
            self.headers,
        )
        self.assertTrue(tool_turn.continuation)
        self.assertEqual(first.session.wake_id, tool_turn.session.wake_id)
        self.assertEqual(1, self.control.seq)

        self.app.finish_turn(tool_turn, keep_for_tools=False)
        second = self.app.prepare_turn(
            self.request(
                [
                    {"role": "user", "content": "开始"},
                    {"role": "assistant", "content": "完成"},
                    {"role": "user", "content": "下一轮"},
                ]
            ),
            self.headers,
        )
        self.assertNotEqual(first.session.wake_id, second.session.wake_id)
        self.assertEqual(2, self.control.seq)

    def test_tool_result_capabilities_join_session_protection_set(self) -> None:
        first = self.app.prepare_turn(
            self.request([{"role": "user", "content": "开始"}]), self.headers
        )
        self.app.finish_turn(first, keep_for_tools=True, tool_call_ids=["call-guard"])
        challenge = "edit-response-that-must-never-be-returned"
        continuation = self.app.prepare_turn(
            self.request(
                [
                    {"role": "user", "content": "开始"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "call-guard", "type": "function"}],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-guard",
                        "content": json.dumps(
                            {
                                "wake_capability": first.session.wake_capability,
                                "challenge_response": challenge,
                            }
                        ),
                    },
                ]
            ),
            self.headers,
        )
        self.assertIn(first.session.wake_capability, continuation.session.protected_values)
        self.assertIn(challenge, continuation.session.protected_values)
        self.assertTrue(
            _contains_protected_value(
                {"choices": [{"message": {"content": "echo " + challenge}}]},
                tuple(continuation.session.protected_values),
            )
        )

    def test_protected_stream_scanner_catches_escaped_and_cross_event_echoes(self) -> None:
        secret = "capability-escaped-secret"
        split = (
            'data: {"choices":[{"delta":{"content":"capability-escaped-"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"secret"}}]}\n\n'
            "data: [DONE]\n\n"
        ).encode("utf-8")
        self.assertTrue(_buffered_sse_contains_protected_value(split, (secret,)))

        escaped = (
            'data: {"choices":[{"delta":{"content":"capability-escaped-\\u0073ecret"}}]}\n\n'
        ).encode("utf-8")
        self.assertTrue(_buffered_sse_contains_protected_value(escaped, (secret,)))
        self.assertEqual(
            {"nested-challenge"},
            _extract_protected_tool_values(
                {"content": '{"challenge_response":"nested-challenge"}'}
            ),
        )

    def test_new_human_turn_is_serialized_while_previous_turn_is_open(self) -> None:
        first_headers = {**self.headers, "X-ST-Thread-ID": "thread:first"}
        second_headers = {**self.headers, "X-ST-Thread-ID": "thread:second"}
        first = self.app.prepare_turn(
            self.request([{"role": "user", "content": "第一框"}]),
            first_headers,
        )
        with self.assertRaises(GatewayError) as caught:
            self.app.prepare_turn(
                self.request([{"role": "user", "content": "第二框"}]),
                second_headers,
            )
        self.assertEqual("human_turn_in_progress", caught.exception.code)
        self.assertEqual(1, self.control.seq)
        self.assertIs(self.app._current_session, first.session)

    def test_expired_unarmed_generation_remains_fail_closed(self) -> None:
        first = self.app.prepare_turn(
            self.request([{"role": "user", "content": "long generation"}]),
            self.headers,
        )
        first.session.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)

        with self.assertRaises(GatewayError) as caught:
            self.app.prepare_turn(
                self.request([{"role": "user", "content": "must not replace"}]),
                self.headers,
            )

        self.assertEqual("human_turn_in_progress", caught.exception.code)
        self.assertIsNone(first.session.tool_wait_armed_at)
        self.assertIs(self.app._current_session, first.session)
        self.assertEqual(1, self.control.seq)

    def test_expired_armed_tool_wait_is_retired_before_new_human_wake(self) -> None:
        first = self.app.prepare_turn(
            self.request([{"role": "user", "content": "tool wait"}]), self.headers
        )
        self.app.finish_turn(first, keep_for_tools=True, tool_call_ids=["call-old"])
        self.assertIsNotNone(first.session.tool_wait_armed_at)
        first.session.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)

        second = self.app.prepare_turn(
            self.request([{"role": "user", "content": "new wake"}]), self.headers
        )

        self.assertEqual(2, self.control.seq)
        self.assertIs(self.app._current_session, second.session)
        self.assertEqual(set(), first.session.expected_tool_call_ids)
        self.assertIsNone(first.session.tool_wait_armed_at)
        paths = [path for path, _ in self.control.calls]
        old_close = paths.index("/v1/host/context/close")
        second_wake = paths.index("/v1/host/wakes", 1)
        self.assertLess(old_close, second_wake)

        with self.assertRaises(GatewayError) as stale:
            self.app.prepare_turn(
                self.request(
                    [
                        {"role": "user", "content": "tool wait"},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{"id": "call-old", "type": "function"}],
                        },
                        {"role": "tool", "tool_call_id": "call-old", "content": "{}"},
                    ]
                ),
                self.headers,
            )
        self.assertEqual("tool_continuation_context_lost", stale.exception.code)
        self.assertIs(self.app._current_session, second.session)

    def test_expired_armed_continuation_is_rejected_and_closed(self) -> None:
        first = self.app.prepare_turn(
            self.request([{"role": "user", "content": "tool wait"}]), self.headers
        )
        self.app.finish_turn(first, keep_for_tools=True, tool_call_ids=["call-old"])
        first.session.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)

        with self.assertRaises(GatewayError) as caught:
            self.app.prepare_turn(
                self.request(
                    [
                        {"role": "user", "content": "tool wait"},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{"id": "call-old", "type": "function"}],
                        },
                        {"role": "tool", "tool_call_id": "call-old", "content": "{}"},
                    ]
                ),
                self.headers,
            )

        self.assertEqual("tool_continuation_context_lost", caught.exception.code)
        self.assertIsNone(self.app._current_session)
        self.assertEqual("/v1/host/context/close", self.control.calls[-1][0])

    def test_multi_step_tool_chain_rearms_with_a_fresh_timestamp(self) -> None:
        first = self.app.prepare_turn(
            self.request([{"role": "user", "content": "tool chain"}]), self.headers
        )
        with patch("rikkahub_gateway.server.time.time", return_value=100.0):
            self.app.finish_turn(first, keep_for_tools=True, tool_call_ids=["call-1"])
        self.assertEqual(100.0, first.session.tool_wait_armed_at)

        continuation = self.app.prepare_turn(
            self.request(
                [
                    {"role": "user", "content": "tool chain"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "call-1", "type": "function"}],
                    },
                    {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
                ]
            ),
            self.headers,
        )
        self.assertIsNone(continuation.session.tool_wait_armed_at)

        with patch("rikkahub_gateway.server.time.time", return_value=200.0):
            self.app.finish_turn(
                continuation,
                keep_for_tools=True,
                tool_call_ids=["call-2"],
            )
        self.assertEqual(200.0, continuation.session.tool_wait_armed_at)
        self.assertEqual({"call-2"}, continuation.session.expected_tool_call_ids)

    def test_rikka_merged_settled_prefix_accepts_exact_replay_and_current_batch(self) -> None:
        settled = [self.native_call("call-a", "python3 read.py")]
        current = [
            self.native_call("call-b", "python3 missing.py"),
            self.native_call("call-c", "ls"),
        ]
        tools, user, results, session = self.settle_then_arm(settled, current)

        merged = self.app.prepare_turn(
            self.request(
                self.merged_continuation_messages(
                    user,
                    [*settled, *current],
                    results,
                    # Tool result order is not lineage; call declaration order is.
                    result_order=["call-c", "call-a", "call-b"],
                ),
                tools=tools,
            ),
            self.headers,
        )

        self.assertTrue(merged.continuation)
        self.assertEqual(["call-a", "call-b", "call-c"], session.settled_tool_call_order)
        self.assertEqual(set(), session.expected_tool_call_ids)
        self.assertEqual(3, len(session.host_receipts))
        self.app.finish_turn(merged, keep_for_tools=False)

    def test_merged_prefix_tampering_is_rejected_without_consuming_current_wait(self) -> None:
        settled = [self.native_call("call-a", "python3 read.py")]
        current = [self.native_call("call-b", "ls")]
        tools, user, results, session = self.settle_then_arm(settled, current)

        tampered_call = self.native_call("call-a", "python3 altered.py")
        with self.assertRaises(GatewayError) as call_error:
            self.app.prepare_turn(
                self.request(
                    self.merged_continuation_messages(
                        user, [tampered_call, *current], results
                    ),
                    tools=tools,
                ),
                self.headers,
            )
        self.assertEqual(
            "tool_continuation_settled_replay_mismatch", call_error.exception.code
        )
        self.assertEqual({"call-b"}, session.expected_tool_call_ids)

        tampered_results = {**results, "call-a": '{"exitCode":999}'}
        with self.assertRaises(GatewayError) as result_error:
            self.app.prepare_turn(
                self.request(
                    self.merged_continuation_messages(
                        user, [*settled, *current], tampered_results
                    ),
                    tools=tools,
                ),
                self.headers,
            )
        self.assertEqual(
            "tool_continuation_settled_replay_mismatch", result_error.exception.code
        )
        self.assertEqual({"call-b"}, session.expected_tool_call_ids)

        original_receipt = dict(session.host_receipts[0])
        session.host_receipts[0]["result_hash"] = "0" * 64
        try:
            with self.assertRaises(GatewayError) as receipt_error:
                self.app.prepare_turn(
                    self.request(
                        self.merged_continuation_messages(
                            user, [*settled, *current], results
                        ),
                        tools=tools,
                    ),
                    self.headers,
                )
            self.assertEqual(
                "tool_continuation_settled_replay_mismatch",
                receipt_error.exception.code,
            )
            self.assertEqual({"call-b"}, session.expected_tool_call_ids)
        finally:
            session.host_receipts[0] = original_receipt

        valid = self.app.prepare_turn(
            self.request(
                self.merged_continuation_messages(
                    user, [*settled, *current], results
                ),
                tools=tools,
            ),
            self.headers,
        )
        self.app.finish_turn(valid, keep_for_tools=False)

    def test_merged_prefix_still_requires_every_current_result(self) -> None:
        settled = [self.native_call("call-a", "python3 read.py")]
        current = [
            self.native_call("call-b", "python3 missing.py"),
            self.native_call("call-c", "ls"),
        ]
        tools, user, results, session = self.settle_then_arm(settled, current)
        messages = self.merged_continuation_messages(
            user, [*settled, *current], results
        )
        messages = [
            message
            for message in messages
            if not (
                message.get("role") == "tool"
                and message.get("tool_call_id") == "call-b"
            )
        ]

        with self.assertRaises(GatewayError) as caught:
            self.app.prepare_turn(
                self.request(messages, tools=tools), self.headers
            )

        self.assertEqual("tool_continuation_lineage_mismatch", caught.exception.code)
        self.assertEqual({"call-b", "call-c"}, session.expected_tool_call_ids)

    def test_merged_prefix_rejects_reordered_inserted_or_interleaved_calls(self) -> None:
        settled = [
            self.native_call("call-a1", "first"),
            self.native_call("call-a2", "second"),
        ]
        current = [
            self.native_call("call-b", "third"),
            self.native_call("call-c", "fourth"),
        ]
        tools, user, results, session = self.settle_then_arm(settled, current)
        inserted = self.native_call("call-x", "not settled")
        variants = (
            [settled[1], settled[0], *current],
            [settled[0], *current],
            [*settled, inserted, *current],
            [*settled, current[1], current[0]],
            [settled[0], current[0], settled[1], current[1]],
        )
        for index, calls in enumerate(variants):
            with self.subTest(index=index):
                variant_results = {
                    **results,
                    inserted.tool_call_id: '{"exitCode":0}',
                }
                with self.assertRaises(GatewayError) as caught:
                    self.app.prepare_turn(
                        self.request(
                            self.merged_continuation_messages(
                                user, calls, variant_results
                            ),
                            tools=tools,
                        ),
                        self.headers,
                    )
                self.assertEqual(
                    "tool_continuation_lineage_mismatch", caught.exception.code
                )
                self.assertEqual(
                    {"call-b", "call-c"}, session.expected_tool_call_ids
                )

        valid = self.app.prepare_turn(
            self.request(
                self.merged_continuation_messages(
                    # Rikka may retain only the latest contiguous settled tail.
                    user, [settled[-1], *current], results
                ),
                tools=tools,
            ),
            self.headers,
        )
        self.app.finish_turn(valid, keep_for_tools=False)

    def test_same_wake_rejects_reusing_a_settled_tool_call_id(self) -> None:
        tools = self.bound_tools()
        user = {"role": "user", "content": "run once"}
        call_a = self.native_call("call-a", "first")
        first = self.app.prepare_turn(
            self.request([user], tools=tools), self.headers
        )
        self.arm_bound_calls(first, [call_a])
        continuation = self.app.prepare_turn(
            self.request(
                [
                    user,
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [self.wire_call(call_a)],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-a",
                        "content": '{"exitCode":0}',
                    },
                ],
                tools=tools,
            ),
            self.headers,
        )

        with self.assertRaises(GatewayError) as reused:
            self.app.bind_response_tool_calls(continuation, [call_a])

        self.assertEqual("tool_call_id_reused", reused.exception.code)
        self.assertEqual({"call-a"}, continuation.session.seen_tool_call_ids)
        self.assertEqual(set(), continuation.session.expected_tool_call_ids)
        call_b = self.native_call("call-b", "second")
        self.arm_bound_calls(continuation, [call_b])
        self.assertEqual({"call-b"}, continuation.session.expected_tool_call_ids)

    def test_expiry_race_never_accepts_new_human_and_stale_continuation(self) -> None:
        first = self.app.prepare_turn(
            self.request([{"role": "user", "content": "race"}]), self.headers
        )
        self.app.finish_turn(first, keep_for_tools=True, tool_call_ids=["call-old"])
        first.session.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def send_human() -> None:
            barrier.wait()
            try:
                results["human"] = self.app.prepare_turn(
                    self.request([{"role": "user", "content": "replacement"}]),
                    self.headers,
                )
            except GatewayError as exc:
                results["human"] = exc.code

        def send_stale_continuation() -> None:
            barrier.wait()
            try:
                results["continuation"] = self.app.prepare_turn(
                    self.request(
                        [
                            {"role": "user", "content": "race"},
                            {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {"id": "call-old", "type": "function"}
                                ],
                            },
                            {
                                "role": "tool",
                                "tool_call_id": "call-old",
                                "content": "{}",
                            },
                        ]
                    ),
                    self.headers,
                )
            except GatewayError as exc:
                results["continuation"] = exc.code

        human_thread = threading.Thread(target=send_human)
        stale_thread = threading.Thread(target=send_stale_continuation)
        human_thread.start()
        stale_thread.start()
        human_thread.join(timeout=5)
        stale_thread.join(timeout=5)

        self.assertFalse(human_thread.is_alive())
        self.assertFalse(stale_thread.is_alive())
        self.assertNotIsInstance(results["human"], str)
        self.assertEqual(
            "tool_continuation_context_lost",
            results["continuation"],
        )
        human_turn = results["human"]
        self.assertIs(self.app._current_session, human_turn.session)  # type: ignore[union-attr]

    def test_missing_or_invalid_wake_expiry_fails_closed(self) -> None:
        invalid_values: tuple[object, ...] = (
            None,
            "not-a-time",
            "2099-01-01T00:00:00",
            "2000-01-01T00:00:00Z",
            4070908800,
        )
        for value in invalid_values:
            with self.subTest(value=value):
                control = FakeControl()
                control.wake_expires_at = value
                app = GatewayApplication(config(), control=control)
                with self.assertRaises(GatewayError) as caught:
                    app.prepare_turn(
                        self.request([{"role": "user", "content": "expiry"}]),
                        self.headers,
                    )
                self.assertEqual("st_wake_expiry_invalid", caught.exception.code)
                self.assertIsNone(app._current_session)
                self.assertEqual("/v1/host/context/close", control.calls[-1][0])

    def test_human_turn_in_progress_is_safely_observable(self) -> None:
        self.assertEqual(
            "human_turn_in_progress",
            _observable_failure_code("human_turn_in_progress"),
        )
        self.assertEqual(
            "gateway_request_failed",
            _observable_failure_code("not-value-free-or-allowlisted"),
        )

    def test_retired_tool_call_tombstones_are_deduplicated_and_bounded(self) -> None:
        ids = [f"retired-{index}" for index in range(MAX_RETIRED_TOOL_CALL_IDS + 3)]
        self.app._remember_retired_tool_calls([ids[0], ids[0], *ids[1:]])

        self.assertEqual(MAX_RETIRED_TOOL_CALL_IDS, len(self.app._retired_tool_call_ids))
        self.assertEqual(MAX_RETIRED_TOOL_CALL_IDS, len(self.app._retired_tool_call_order))
        self.assertNotIn(ids[0], self.app._retired_tool_call_ids)
        self.assertIn(ids[-1], self.app._retired_tool_call_ids)
        before = list(self.app._retired_tool_call_order)
        self.app._remember_retired_tool_calls([ids[-1]])
        self.assertEqual(before, self.app._retired_tool_call_order)

    def test_missing_thread_id_and_stable_user_never_merge_new_frames(self) -> None:
        headers = {"Authorization": f"Bearer {config().gateway_token}"}
        request = self.request(
            [{"role": "user", "content": "相同的首条消息"}],
            user="same-end-user-across-chats",
        )
        first = self.app.prepare_turn(request, headers)
        self.app.finish_turn(first, keep_for_tools=False)
        second = self.app.prepare_turn(request, headers)

        self.assertNotEqual(first.thread_id, second.thread_id)
        self.assertNotEqual(first.session.wake_id, second.session.wake_id)
        self.assertEqual(2, self.control.seq)
        self.assertIs(self.app._current_session, second.session)

    def test_tool_continuation_is_bound_to_upstream_call_lineage(self) -> None:
        headers = {"Authorization": f"Bearer {config().gateway_token}"}
        first = self.app.prepare_turn(
            self.request([{"role": "user", "content": "第一框"}], user="same-user"),
            headers,
        )
        self.app.finish_turn(first, keep_for_tools=True, tool_call_ids=["call-old"])
        with self.assertRaises(GatewayError) as busy:
            self.app.prepare_turn(
                self.request([{"role": "user", "content": "第二框"}], user="same-user"),
                headers,
            )
        self.assertEqual("human_turn_in_progress", busy.exception.code)
        first_continuation = self.app.prepare_turn(
            self.request(
                [
                    {"role": "user", "content": "第一框"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "call-old", "type": "function"}],
                    },
                    {"role": "tool", "tool_call_id": "call-old", "content": "{}"},
                ],
                user="same-user",
            ),
            headers,
        )
        self.app.finish_turn(first_continuation, keep_for_tools=False)
        second = self.app.prepare_turn(
            self.request([{"role": "user", "content": "第二框"}], user="same-user"),
            headers,
        )
        self.app.finish_turn(second, keep_for_tools=True, tool_call_ids=["call-new"])

        with self.assertRaises(GatewayError) as stale:
            self.app.prepare_turn(
                self.request(
                    [
                        {"role": "user", "content": "第一框"},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{"id": "call-old", "type": "function"}],
                        },
                        {"role": "tool", "tool_call_id": "call-old", "content": "{}"},
                    ],
                    user="same-user",
                ),
                headers,
            )
        self.assertEqual("tool_continuation_lineage_mismatch", stale.exception.code)
        self.assertIs(self.app._current_session, second.session)

        valid = self.app.prepare_turn(
            self.request(
                [
                    {"role": "user", "content": "第二框"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "call-new", "type": "function"}],
                    },
                    {"role": "tool", "tool_call_id": "call-new", "content": "{}"},
                ],
                user="same-user",
            ),
            headers,
        )
        self.assertIs(valid.session, second.session)

    def test_tool_continuation_rejects_interleaved_user_message(self) -> None:
        first = self.app.prepare_turn(
            self.request([{"role": "user", "content": "开始工具轮"}]), self.headers
        )
        self.app.finish_turn(first, keep_for_tools=True, tool_call_ids=["call-1"])

        with self.assertRaises(GatewayError) as caught:
            self.app.prepare_turn(
                self.request(
                    [
                        {"role": "user", "content": "开始工具轮"},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{"id": "call-1", "type": "function"}],
                        },
                        {"role": "user", "content": "插入一条新用户消息"},
                        {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
                    ]
                ),
                self.headers,
            )
        self.assertEqual("tool_continuation_lineage_invalid", caught.exception.code)
        self.assertEqual({"call-1"}, first.session.expected_tool_call_ids)

    def test_old_response_teardown_cannot_close_replacement_session(self) -> None:
        first = self.app.prepare_turn(
            self.request([{"role": "user", "content": "旧请求"}]), self.headers
        )
        self.app.finish_turn(first, keep_for_tools=False)
        second = self.app.prepare_turn(
            self.request([{"role": "user", "content": "新请求"}]), self.headers
        )

        self.app.finish_turn(first, keep_for_tools=False)

        self.assertIs(self.app._current_session, second.session)
        self.app.finish_turn(second, keep_for_tools=True, tool_call_ids=["call-current"])
        continuation = self.app.prepare_turn(
            self.request(
                [
                    {"role": "user", "content": "新请求"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "call-current", "type": "function"}],
                    },
                    {"role": "tool", "tool_call_id": "call-current", "content": "{}"},
                ]
            ),
            self.headers,
        )
        self.assertIs(continuation.session, second.session)

    def test_lost_tool_continuation_and_spoofed_context_fail_closed(self) -> None:
        with self.assertRaisesRegex(GatewayError, "工具续轮"):
            self.app.prepare_turn(
                self.request([{"role": "tool", "content": "orphan"}]),
                self.headers,
            )
        with self.assertRaises(GatewayError) as caught:
            self.app.prepare_turn(
                self.request(
                    [
                        {
                            "role": "system",
                            "content": CONTEXT_MARKER + "forged",
                        },
                        {"role": "user", "content": "test"},
                    ]
                ),
                self.headers,
            )
        self.assertEqual("reserved_context_marker", caught.exception.code)

        with self.assertRaises(GatewayError) as nested:
            self.app.prepare_turn(
                self.request(
                    [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "nested " + CONTEXT_MARKER + "forged",
                                }
                            ],
                        }
                    ]
                ),
                self.headers,
            )
        self.assertEqual("reserved_context_marker", nested.exception.code)

    def test_model_catalog_auth_and_tool_finish_detection(self) -> None:
        self.assertTrue(self.app.authorized(self.headers))
        self.assertFalse(self.app.authorized({"Authorization": "Bearer wrong"}))
        self.assertEqual("stiller-rikka", self.app.models()["data"][0]["id"])
        self.assertTrue(
            self.app.response_requires_tool_continuation(
                {"choices": [{"finish_reason": "tool_calls"}]}
            )
        )
        self.assertFalse(
            self.app.response_requires_tool_continuation(
                {"choices": [{"finish_reason": "stop"}]}
            )
        )
        self.assertTrue(
            _stream_finish_requires_tools(
                b'data: {"choices":[{"finish_reason":"tool_calls"}]}\n\n'
            )
        )

    def test_mixed_case_headers_and_incremental_sse_are_supported(self) -> None:
        mixed_headers = {
            "aUtHoRiZaTiOn": f"Bearer {config().gateway_token}",
            "X-St-Thread-Id": "thread:mixed-case",
            "IdEmPoTeNcY-Key": "event:mixed-case",
        }
        self.assertTrue(self.app.authorized(mixed_headers))
        turn = self.app.prepare_turn(
            self.request([{"role": "user", "content": "真实 HTTP 头"}]),
            mixed_headers,
        )
        self.assertEqual("thread:mixed-case", turn.thread_id)
        wake_payload = next(
            payload for path, payload in self.control.calls if path == "/v1/host/wakes"
        )
        self.assertEqual("event:mixed-case", wake_payload["source_event_id"])

        scanner = _StreamToolCallScanner()
        wire = (
            'data: {"choices":[{"delta":{"content":"测试"},'
            '"finish_reason":"tool_calls"}]}\n\n'
        ).encode("utf-8")
        found = False
        for byte in wire:
            found = scanner.feed(bytes([byte])) or found
        found = scanner.feed(b"", final=True) or found
        self.assertTrue(found)

        id_scanner = _StreamToolCallScanner()
        id_wire = (
            'data: {"choices":[{"delta":{"tool_calls":[{"id":"call-sse"}]},'
            '"finish_reason":"tool_calls"}]}\n\n'
        ).encode("utf-8")
        for offset in range(0, len(id_wire), 3):
            id_scanner.feed(id_wire[offset : offset + 3])
        id_scanner.feed(b"", final=True)
        self.assertEqual({"call-sse"}, id_scanner.tool_call_ids)

        event_buffer = _SSEEventBuffer()
        crlf_wire = (
            b'data: {"choices":[{"delta":{"reasoning_content":"one"}}]}\r\n\r\n'
            b'data: [DONE]\r\n\r\n'
        )
        parsed_events = []
        for byte in crlf_wire:
            parsed_events.extend(event_buffer.feed(bytes([byte])))
        parsed_events.extend(event_buffer.feed(b"", final=True))
        self.assertEqual(2, len(parsed_events))
        self.assertEqual(
            "one",
            _parse_sse_event(parsed_events[0]).payload["choices"][0]["delta"][
                "reasoning_content"
            ],
        )
        self.assertTrue(_parse_sse_event(parsed_events[1]).done)

    def test_all_four_credentials_must_be_distinct(self) -> None:
        human = "human-token-that-is-at-least-32-characters"
        collisions = (
            {"host_token": config().gateway_token},
            {"upstream_api_key": config().gateway_token},
            {"upstream_api_key": config().host_token},
            {"human_token": config().gateway_token},
            {"human_token": config().host_token},
            {"human_token": human, "upstream_api_key": human},
        )
        for changes in collisions:
            with self.subTest(changes=tuple(changes)):
                with self.assertRaisesRegex(ValueError, "pairwise distinct"):
                    replacement = {"human_token": human, **changes}
                    replace(config(), **replacement)

    def test_human_direct_grant_proxy_is_separate_and_response_is_minimized(self) -> None:
        human = "human-token-that-is-at-least-32-characters"
        human_control = FakeHumanControl()
        app = GatewayApplication(
            replace(config(), human_token=human),
            control=self.control,
            human_control=human_control,
        )
        request = {
            "request_id": "123e4567-e89b-42d3-a456-426614174000",
            "requested_scopes": ["learning_memory"],
        }

        self.assertFalse(
            app.authorized_human(
                {"Authorization": f"Bearer {app.config.gateway_token}"}
            )
        )
        self.assertFalse(
            app.authorized({"Authorization": f"Bearer {app.config.human_token}"})
        )
        self.assertTrue(
            app.authorized_human(
                {"Authorization": f"Bearer {app.config.human_token}"}
            )
        )
        result = app.issue_direct_grant(request)
        self.assertEqual(
            [
                (
                    "/v1/human/direct-grants",
                    request,
                )
            ],
            human_control.calls,
        )
        self.assertEqual(
            {
                "grant_ref": "stgrant_test-one-use-value",
                "expires_at": "2099-01-01T00:00:00Z",
                "scopes": ["learning_memory"],
                "status": "pending",
            },
            result,
        )
        self.assertNotIn("internal_grant_id", result)

        with self.assertRaisesRegex(GatewayError, "invalid_direct_grant_request"):
            app.issue_direct_grant({**request, "owner_id": "owner:foreign"})
        self.assertEqual(1, len(human_control.calls))

    def test_upstream_request_contains_injection_and_closes_after_stop(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            payload = json.loads(request.content)
            self.assertEqual(
                {"role": "system", "content": OPTIONAL_BRAIN_NOTICE},
                payload["messages"][0],
            )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "model": "real-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "收到"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        self.app.upstream = httpx.Client(transport=httpx.MockTransport(handler))
        turn = self.app.prepare_turn(
            self.request([{"role": "user", "content": "测试"}]), self.headers
        )
        response = self.app.upstream.post(
            self.app.upstream_url(),
            headers=self.app.upstream_headers(),
            json=turn.payload,
        )
        body = response.json()
        self.app.finish_turn(
            turn,
            keep_for_tools=self.app.response_requires_tool_continuation(body),
            tool_call_ids=self.app.response_tool_call_ids(body),
        )
        self.assertEqual(1, len(seen))
        self.assertEqual(
            "/v1/host/context/close", self.control.calls[-1][0]
        )


if __name__ == "__main__":
    unittest.main()
