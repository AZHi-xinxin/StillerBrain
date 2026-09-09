from __future__ import annotations

import copy
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer

import httpx

from rikkahub_gateway.server import GatewayApplication, _GatewayHandler, _log_gateway_failure
from rikkahub_gateway.tests.test_gateway import FakeControl, config
from rikkahub_gateway.tool_execution import NativeTransportPolicy


SCHEMA_MARKER = "synthetic-schema-must-not-be-logged"
ARGUMENT_MARKER = "synthetic-argument-must-not-be-logged"
PROMPT_MARKER = "synthetic-prompt-must-not-be-logged"
TOOL_NAME = "mcp__StillerBrain__remember_planning_memory"


class RecordingPolicy(NativeTransportPolicy):
    def __init__(self) -> None:
        self.authorizations = []
        self.receipts = []

    def authorize(self, binding, *, catalog):
        self.authorizations.append(binding)
        return super().authorize(binding, catalog=catalog)

    def observe_receipt(self, receipt):
        self.receipts.append(receipt)


def schema() -> dict:
    return {
        "type": "object",
        "title": SCHEMA_MARKER,
        "additionalProperties": False,
        "required": ["scene_tags", "calm_check"],
        "properties": {
            "scene_tags": {"$ref": "#/$defs/short_list"},
            "calm_check": {
                "type": "object",
                "additionalProperties": False,
                "required": ["authorship_confirmed", "notes"],
                "properties": {
                    "authorship_confirmed": {"const": True},
                    "notes": {"type": "string"},
                },
            },
        },
        "$defs": {"short_list": {"type": "array", "items": {"type": "string"}}},
    }


def arguments() -> dict:
    return {
        "scene_tags": ["synthetic"],
        "calm_check": {"authorship_confirmed": True, "notes": ARGUMENT_MARKER},
    }


def native_call(*, name: str = TOOL_NAME, call_id: str = "call-failure-test", values=None) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            # Whitespace is intentional: gateway must not rewrite model args.
            "arguments": json.dumps(arguments() if values is None else values, indent=1),
        },
    }


def sse(calls: list[dict], *, prefix: bool) -> bytes:
    events = []
    if prefix:
        events.append({"choices": [{"index": 0, "delta": {"content": "I will submit it now."}}]})
    events.append(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": i, **call} for i, call in enumerate(calls)]},
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    return (
        "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        + "data: [DONE]\n\n"
    ).encode("utf-8")


class GatewayFailureObservabilityTests(unittest.TestCase):
    """Real local HTTP, fake control/upstream, no MCP execution or databases."""

    def setUp(self) -> None:
        self.control = FakeControl()
        self.app = GatewayApplication(config(), control=self.control)
        self.policy = RecordingPolicy()
        self.app.execution_boundary.policy = self.policy
        self.unhandled = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        self.server.application = self.app
        self.server.handle_error = lambda request, address: self.unhandled.append(
            type(sys.exc_info()[1]).__name__
        )
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)
        self.app.upstream.close()

    @staticmethod
    def tools(tool_schema: dict | None = None) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {"name": TOOL_NAME, "parameters": schema() if tool_schema is None else tool_schema},
            }
        ]

    def use_upstream(self, response_body, *, stream: bool = True) -> None:
        self.app.upstream.close()
        self.upstream_requests = []

        def handle(request: httpx.Request) -> httpx.Response:
            self.upstream_requests.append(json.loads(request.content))
            if stream:
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=response_body)
            return httpx.Response(200, json=response_body)

        self.app.upstream = httpx.Client(transport=httpx.MockTransport(handle))

    def post(self, *, tools: list[dict] | None = None, stream: bool = True) -> tuple[int, bytes]:
        request = urllib.request.Request(
            self.url,
            method="POST",
            data=json.dumps(
                {
                    "model": config().public_model,
                    "messages": [{"role": "user", "content": PROMPT_MARKER}],
                    "tools": self.tools() if tools is None else tools,
                    "stream": stream,
                }
            ).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + config().gateway_token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read()

    @staticmethod
    def events(raw: bytes) -> list:
        return [
            "[DONE]" if line[6:] == "[DONE]" else json.loads(line[6:])
            for line in raw.decode("utf-8").splitlines()
            if line.startswith("data: ")
        ]

    def assert_safe_log(self, records, code: str, prefix: bool) -> None:
        self.assertEqual(1, len(records))
        record = json.loads(records[0].getMessage())
        self.assertEqual({"event", "at", "code", "streamed_prefix"}, set(record))
        self.assertEqual("gateway_request_failed", record["event"])
        self.assertEqual(code, record["code"])
        self.assertEqual(prefix, record["streamed_prefix"])
        self.assertIsNotNone(datetime.fromisoformat(record["at"].replace("Z", "+00:00")).tzinfo)
        text = records[0].getMessage()
        for forbidden in (SCHEMA_MARKER, ARGUMENT_MARKER, PROMPT_MARKER, "capability-1", config().gateway_token, config().upstream_api_key):
            self.assertNotIn(forbidden, text)
        self.assertIsNone(records[0].exc_info)

    def assert_failed_without_calls(self) -> None:
        self.assertIsNone(self.app._current_session)
        self.assertEqual([], self.unhandled)
        self.assertEqual([], self.policy.receipts)
        self.assertEqual(1, len(self.upstream_requests))
        self.assertEqual("/v1/host/context/close", self.control.calls[-1][0])

    def test_schema_and_argument_errors_are_explicit_before_and_after_prefix(self) -> None:
        for failure in ("invalid_arguments", "missing_reference", "recursive_reference"):
            for prefix in (False, True):
                with self.subTest(failure=failure, prefix=prefix):
                    selected_schema = schema()
                    values = arguments()
                    if failure == "missing_reference":
                        selected_schema.pop("$defs")
                        code = "advertised_tool_schema_reference_unresolved"
                    elif failure == "recursive_reference":
                        selected_schema = {"$ref": "#", "title": SCHEMA_MARKER}
                        code = "advertised_tool_schema_recursion_unsupported"
                    else:
                        values["calm_check"]["authorship_confirmed"] = False
                        code = "tool_arguments_schema_invalid"
                    self.use_upstream(sse([native_call(values=values)], prefix=prefix))
                    with self.assertLogs("stiller.rikkahub.gateway", level="WARNING") as logs:
                        status, raw = self.post(tools=self.tools(selected_schema))
                    self.assertEqual(200 if prefix else 502, status)
                    if prefix:
                        events = self.events(raw)
                        self.assertEqual("I will submit it now.", events[0]["choices"][0]["delta"]["content"])
                        self.assertEqual(code, events[-2]["error"]["code"])
                        self.assertEqual("[DONE]", events[-1])
                        self.assertNotIn("choices", events[-2])
                        self.assertNotIn(b'"finish_reason"', raw)
                    else:
                        self.assertEqual(code, json.loads(raw)["error"]["code"])
                    self.assertNotIn(b'"tool_calls"', raw)
                    for value in (SCHEMA_MARKER, ARGUMENT_MARKER, PROMPT_MARKER):
                        self.assertNotIn(value.encode(), raw)
                    self.assertEqual([], self.policy.authorizations)
                    self.assert_failed_without_calls()
                    self.assert_safe_log(logs.records, code, prefix)

    def test_nonstream_reference_errors_are_safe_http_502(self) -> None:
        for recursive in (False, True):
            with self.subTest(recursive=recursive):
                selected_schema = schema()
                selected_schema.pop("$defs")
                code = "advertised_tool_schema_reference_unresolved"
                if recursive:
                    selected_schema = {"$ref": "#", "title": SCHEMA_MARKER}
                    code = "advertised_tool_schema_recursion_unsupported"
                self.use_upstream(
                    {"choices": [{"message": {"role": "assistant", "content": "Submitting.", "tool_calls": [native_call()]}, "finish_reason": "tool_calls"}]},
                    stream=False,
                )
                with self.assertLogs("stiller.rikkahub.gateway", level="WARNING") as logs:
                    status, raw = self.post(tools=self.tools(selected_schema), stream=False)
                self.assertEqual(502, status)
                self.assertEqual(code, json.loads(raw)["error"]["code"])
                self.assertNotIn(b"tool_calls", raw)
                for value in (SCHEMA_MARKER, ARGUMENT_MARKER, PROMPT_MARKER):
                    self.assertNotIn(value.encode(), raw)
                self.assertEqual([], self.policy.authorizations)
                self.assert_failed_without_calls()
                self.assert_safe_log(logs.records, code, False)

    def test_valid_reference_and_native_arguments_are_unchanged(self) -> None:
        call = native_call()
        tools = self.tools()
        self.use_upstream(sse([call], prefix=True))
        status, raw = self.post(tools=tools)
        self.assertEqual(200, status)
        events = self.events(raw)
        self.assertEqual({"index": 0, **call}, events[-2]["choices"][0]["delta"]["tool_calls"][0])
        self.assertEqual("tool_calls", events[-2]["choices"][0]["finish_reason"])
        self.assertEqual("[DONE]", events[-1])
        self.assertEqual(tools, self.upstream_requests[0]["tools"])
        self.assertEqual({call["id"]}, self.app._current_session.expected_tool_call_ids)
        self.assertEqual(1, len(self.policy.authorizations))
        self.assertEqual([], self.policy.receipts)
        self.assertEqual([], self.unhandled)

    def test_unused_broken_tool_does_not_disable_valid_tool_over_http(self) -> None:
        tools = self.tools()
        broken = copy.deepcopy(tools[0])
        broken["function"]["name"] = "unused_broken_tool"
        broken["function"]["parameters"].pop("$defs")
        tools.append(broken)
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "unused_recursive_tool",
                    "parameters": {"$ref": "#", "title": SCHEMA_MARKER},
                },
            }
        )
        self.use_upstream(sse([native_call()], prefix=True))
        status, raw = self.post(tools=tools)
        self.assertEqual(200, status)
        self.assertEqual("tool_calls", self.events(raw)[-2]["choices"][0]["finish_reason"])
        self.assertEqual([], self.unhandled)

    def test_mixed_batch_rejection_releases_neither_tool(self) -> None:
        tools = self.tools()
        broken = copy.deepcopy(tools[0])
        broken["function"]["name"] = "broken_planning_tool"
        broken["function"]["parameters"].pop("$defs")
        tools.append(broken)
        self.use_upstream(sse([
            native_call(call_id="call-valid-must-not-leak"),
            native_call(name="broken_planning_tool", call_id="call-invalid-must-not-leak"),
        ], prefix=True))
        with self.assertLogs("stiller.rikkahub.gateway", level="WARNING") as logs:
            status, raw = self.post(tools=tools)
        self.assertEqual(200, status)
        self.assertEqual("advertised_tool_schema_reference_unresolved", self.events(raw)[-2]["error"]["code"])
        self.assertNotIn(b'"tool_calls"', raw)
        self.assertNotIn(b"call-valid-must-not-leak", raw)
        self.assertNotIn(b"call-invalid-must-not-leak", raw)
        self.assertEqual(1, len(self.policy.authorizations))
        self.assert_failed_without_calls()
        self.assert_safe_log(logs.records, "advertised_tool_schema_reference_unresolved", True)

    def test_protected_suffix_stays_blocked_when_error_becomes_visible(self) -> None:
        wire = (
            'data: {"choices":[{"delta":{"content":"Safe prefix."}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"capability-1"},"finish_reason":"stop"}]}\n\n'
            'data: [DONE]\n\n'
        ).encode()
        self.use_upstream(wire)
        with self.assertLogs("stiller.rikkahub.gateway", level="WARNING") as logs:
            status, raw = self.post()
        self.assertEqual(200, status)
        self.assertNotIn(b"capability-1", raw)
        self.assertEqual("upstream_protected_value", self.events(raw)[-2]["error"]["code"])
        self.assertEqual("[DONE]", self.events(raw)[-1])
        self.assert_failed_without_calls()
        self.assert_safe_log(logs.records, "upstream_protected_value", True)

    def test_upstream_transport_error_is_explicit_without_logging_exception_text(self) -> None:
        for prefix in (False, True):
            with self.subTest(prefix=prefix):
                class FailingStream(httpx.SyncByteStream):
                    def __iter__(self):
                        if prefix:
                            yield b'data: {"choices":[{"delta":{"content":"Safe prefix."}}]}\n\n'
                        raise httpx.ReadError(ARGUMENT_MARKER)

                self.upstream_requests = []

                def handle(request: httpx.Request) -> httpx.Response:
                    self.upstream_requests.append(json.loads(request.content))
                    return httpx.Response(
                        200,
                        headers={"Content-Type": "text/event-stream"},
                        stream=FailingStream(),
                    )

                self.app.upstream.close()
                self.app.upstream = httpx.Client(transport=httpx.MockTransport(handle))
                with self.assertLogs("stiller.rikkahub.gateway", level="WARNING") as logs:
                    status, raw = self.post()
                self.assertEqual(200 if prefix else 502, status)
                error = self.events(raw)[-2]["error"] if prefix else json.loads(raw)["error"]
                self.assertEqual("upstream_unavailable", error["code"])
                self.assertNotIn(ARGUMENT_MARKER.encode(), raw)
                self.assertNotIn(b'"tool_calls"', raw)
                self.assert_failed_without_calls()
                self.assert_safe_log(logs.records, "upstream_unavailable", prefix)

    def test_logger_rejects_unknown_details_and_protected_constant_collision(self) -> None:
        with self.assertLogs("stiller.rikkahub.gateway", level="WARNING") as logs:
            _log_gateway_failure(ARGUMENT_MARKER, streamed_prefix=False)
        self.assert_safe_log(logs.records, "gateway_request_failed", False)
        with self.assertNoLogs("stiller.rikkahub.gateway", level="WARNING"):
            _log_gateway_failure(
                "upstream_protected_value",
                streamed_prefix=True,
                protected_values=("upstream_protected_value",),
            )


if __name__ == "__main__":
    unittest.main()
