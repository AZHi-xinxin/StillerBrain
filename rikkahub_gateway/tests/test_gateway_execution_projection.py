"""Host-only execution fields stay out of model input, not out of authority.

Synthetic identities only. HTTP tests bind random localhost and use an in-memory
fake upstream; database and outside connections are never needed.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

import httpx

from rikkahub_gateway.server import GatewayApplication, GatewayError, _buffered_sse_tool_calls
from rikkahub_gateway.tool_execution import NativeToolCall
from rikkahub_gateway.tests import test_gateway as fixtures
from rikkahub_gateway.tests import test_gateway_interleaved_wire as wire
from rikkahub_gateway.tests import test_gateway_execution_recovery as recovery


HOST = "stexec_" + "s" * 43


def assistant(arguments, name="stbrain_open"):
    return {"role": "assistant", "content": "synthetic content", "reasoning_content": "synthetic reasoning",
            "tool_calls": [{"id": "synthetic-call", "type": "function",
                            "function": {"name": name, "arguments": arguments}}]}


class ExecutionProjectionTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "sqlite3.connect"):
            self.enterContext(patch(target, side_effect=AssertionError("external access prohibited")))
        self.control = recovery.ExecutionControl()
        self.app = GatewayApplication(replace(fixtures.config(), require_execution_binding=True,
            execution_epoch="synthetic-projection-epoch"), control=self.control, upstream=object())

    def encode(self, payload):
        original = copy.deepcopy(payload)
        result = json.loads(self.app.encode_upstream_payload(payload))
        self.assertEqual(original, payload, "model projection mutated original authority")
        return result

    def test_schema_hidden_only_in_upstream_copy(self):
        tool = recovery.st_tool()
        tool["function"]["parameters"]["required"] = ["view", "execution_ref"]
        payload = {"tools": [tool], "messages": [], "temperature": 0.4, "stream": True,
                   "parallel_tool_calls": False}
        result = self.encode(payload)
        schema = result["tools"][0]["function"]["parameters"]
        self.assertEqual({"view": {"type": "string"}}, schema["properties"])
        self.assertEqual(["view"], schema["required"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(0.4, result["temperature"])
        self.assertFalse(result["parallel_tool_calls"])

    def test_original_inserted_business_bytes_roundtrip_exact(self):
        for arguments in ('{}', ' \n { "view" : "summary" } \t',
                          '{"view":"中文\\ntext","nested":{"execution_ref":"business"}}'):
            with self.subTest(arguments=arguments):
                bound = self.app._insert_execution_ref(arguments, HOST)
                result = self.encode({"tools": [recovery.st_tool()], "messages": [assistant(bound)]})
                self.assertEqual(arguments, result["messages"][0]["tool_calls"][0]["function"]["arguments"])

    def test_top_level_positions_and_escaped_property_names(self):
        for arguments in ('{"execution_ref":"%s","view":"summary"}',
                          '{"view":"summary", "execution_ref":"%s"}',
                          '{"a":1,"execution_ref":"%s","view":"summary"}',
                          '{"execut\\u0069on_ref":"%s"}'):
            with self.subTest(arguments=arguments):
                original = arguments % HOST
                expected = json.loads(original)
                del expected["execution_ref"]
                actual = self.app._without_host_execution_argument(original)
                self.assertEqual(expected, json.loads(actual))

    def test_nested_business_values_and_text_not_scrubbed(self):
        arguments = json.dumps({"execution_ref": HOST, "view": HOST,
                                "nested": {"execution_ref": HOST}, "content": "execution_ref=" + HOST})
        result = self.encode({"tools": [recovery.st_tool()], "messages": [assistant(arguments),
                {"role": "user", "content": HOST}, {"role": "tool", "tool_call_id": "synthetic-call", "content": HOST}]})
        parsed = json.loads(result["messages"][0]["tool_calls"][0]["function"]["arguments"])
        self.assertNotIn("execution_ref", parsed)
        self.assertEqual(HOST, parsed["nested"]["execution_ref"])
        self.assertEqual(HOST, parsed["view"])
        self.assertEqual("execution_ref=" + HOST, parsed["content"])
        self.assertEqual(HOST, result["messages"][1]["content"])
        self.assertEqual(HOST, result["messages"][2]["content"])
        self.assertEqual("synthetic reasoning", result["messages"][0]["reasoning_content"])

    def test_prefixed_name_identified_by_marker_not_suffix(self):
        name = "mcp__StillerBrain__stbrain_open"
        result = self.encode({"tools": [recovery.st_tool(name)], "messages": [assistant('{"execution_ref":"' + HOST + '"}', name)]})
        self.assertNotIn("execution_ref", result["tools"][0]["function"]["parameters"]["properties"])
        self.assertEqual("{}", result["messages"][0]["tool_calls"][0]["function"]["arguments"])

    def test_unmarked_non_st_same_named_field_is_untouched(self):
        tool = recovery.st_tool("business_tool")
        field = tool["function"]["parameters"]["properties"]["execution_ref"]
        field.pop("x-stbrain-execution-tool")
        field.pop("x-stbrain-execution-contract")
        payload = {"tools": [tool], "messages": [assistant('{"execution_ref":"' + HOST + '"}', "business_tool")]}
        self.assertEqual(payload, self.encode(payload))

    def test_invalid_marker_and_unknown_canonical_never_treated_as_st(self):
        for field, value in (("x-stbrain-execution-contract", "other/1"),
                             ("x-stbrain-execution-tool", "unknown_tool")):
            tool = recovery.st_tool()
            tool["function"]["parameters"]["properties"]["execution_ref"][field] = value
            payload = {"tools": [tool], "messages": [assistant('{"execution_ref":"' + HOST + '"}')]}
            self.assertEqual(payload, self.encode(payload))

    def test_unadvertised_past_tool_not_guessed(self):
        message = assistant('{"execution_ref":"' + HOST + '"}', "old_removed_tool")
        result = self.encode({"tools": [recovery.st_tool()], "messages": [message]})
        self.assertEqual(message, result["messages"][0])

    def test_legacy_functions_marker_projected(self):
        definition = recovery.st_tool()["function"]
        result = self.encode({"functions": [definition], "messages": []})
        self.assertNotIn("execution_ref", result["functions"][0]["parameters"]["properties"])

    def test_disabled_binding_leaves_payload_exact(self):
        self.app.config = replace(self.app.config, require_execution_binding=False)
        payload = {"tools": [recovery.st_tool()], "messages": [assistant('{"execution_ref":"' + HOST + '"}')]}
        self.assertEqual(payload, self.encode(payload))

    def test_invalid_historical_json_not_repaired(self):
        for arguments in ('not-json', '{"execution_ref":null}', '{"execution_ref":"fake"}',
                          '{"execution_ref":"' + HOST + '","execution_ref":"duplicate"}',
                          '{"execution_ref":"' + HOST + '","a":NaN}'):
            self.assertEqual(arguments, self.app._without_host_execution_argument(arguments))

    def test_model_supplied_host_input_still_rejected_before_issue(self):
        payload = {"messages": [{"role": "user", "content": "synthetic only"}], "tools": [recovery.st_tool()]}
        prepared = self.app.prepare_turn(payload, {"X-ST-Thread-ID": "synthetic-projection-thread"})
        for ref in (None, HOST, "fake"):
            with self.subTest(value_type=type(ref).__name__):
                call = NativeToolCall("synthetic-call", "stbrain_open", json.dumps({"execution_ref": ref}))
                with self.assertRaisesRegex(GatewayError, "execution_reference_model_supplied"):
                    self.app.decorate_execution_calls(prepared, [call])
        self.assertFalse(any(path.endswith("/issue") for path, _ in self.control.calls))


class ExecutionProjectionHTTPTests(unittest.TestCase):
    """An upstream that copies visible host fields, reproducing the real failure."""
    post = wire.InterleavedHTTPWireTests.post
    interleaved = wire.InterleavedHTTPWireTests.interleaved

    def setUp(self):
        wire.InterleavedHTTPWireTests.setUp(self)
        self.control = recovery.ExecutionControl()
        self.app.control = self.control
        self.app.config = replace(self.app.config, require_execution_binding=True, execution_epoch="synthetic-wire-epoch")
        self.tools[-1] = recovery.st_tool()
        self.model_copied_reserved_field = False

    def tearDown(self):
        if self.app._current_session is not None:
            self.app._cancel_recovery_timer(self.app._current_session)
        wire.InterleavedHTTPWireTests.tearDown(self)

    def upstream(self, request):
        payload = json.loads(request.content)
        self.upstream_payloads.append(payload)
        visible = any("execution_ref" in tool["function"]["parameters"].get("properties", {})
                      for tool in payload.get("tools", []))
        for message in payload.get("messages", []):
            for call in message.get("tool_calls", []):
                if call["function"]["name"] == "stbrain_open":
                    visible = visible or "execution_ref" in json.loads(call["function"]["arguments"])
        step = len(self.upstream_payloads)
        calls = copy.deepcopy(self.calls) if step == 1 else [{"id": "synthetic-next-open", "type": "function",
                "function": {"name": "stbrain_open", "arguments": "{}"}}]
        if visible:
            self.model_copied_reserved_field = True
            calls[-1]["function"]["arguments"] = json.dumps({"execution_ref": HOST})
        message = {"role": "assistant", "content": "synthetic complete"} if step >= 3 else {
            "role": "assistant", "content": None, "tool_calls": calls}
        finish = "stop" if step >= 3 else "tool_calls"
        if payload.get("stream"):
            delta = {key: value for key, value in message.items() if key != "role"}
            if step < 3:
                delta["tool_calls"] = [{"index": index, **call} for index, call in enumerate(calls)]
            event = {"id": "synthetic-response", "object": "chat.completion.chunk", "model": "real-model",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                content=("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode())
        return httpx.Response(200, json={"id": "synthetic-response", "object": "chat.completion", "model": "real-model",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}]})

    def delivered(self, raw, streaming):
        if streaming:
            return [fixtures.GatewayTests.wire_call(call) for call in _buffered_sse_tool_calls(raw)]
        return json.loads(raw)["choices"][0]["message"]["tool_calls"]

    def roundtrip(self, first_stream, second_stream):
        original_tools = copy.deepcopy(self.tools)
        status, raw = self.post([self.user], stream=first_stream)
        self.assertEqual(200, status, raw)
        self.calls = self.delivered(raw, first_stream)
        self.assertIn("execution_ref", json.loads(self.calls[1]["function"]["arguments"]))
        submitted = self.interleaved()
        original = copy.deepcopy(submitted)
        status, raw = self.post(submitted, stream=second_stream)
        self.assertEqual(200, status, raw)
        next_calls = self.delivered(raw, second_stream)
        self.assertEqual(1, len(next_calls))
        self.assertIn("execution_ref", json.loads(next_calls[0]["function"]["arguments"]))
        messages = [*submitted, {"role": "assistant", "content": None, "tool_calls": next_calls},
                    {"role": "tool", "tool_call_id": next_calls[0]["id"], "content": "synthetic result"}]
        status, raw = self.post(messages, stream=second_stream)
        self.assertEqual(200, status, raw)
        self.assertIn(b"synthetic complete", raw)
        self.assertFalse(self.model_copied_reserved_field)
        self.assertEqual(original_tools, self.tools)
        self.assertEqual(original, submitted)
        self.assertIsNone(self.app._current_session)
        self.assertEqual(2, sum(path.endswith("/issue") for path, _ in self.control.calls))

    def test_json_json(self):
        self.roundtrip(False, False)

    def test_json_sse(self):
        self.roundtrip(False, True)

    def test_sse_json(self):
        self.roundtrip(True, False)

    def test_sse_sse(self):
        self.roundtrip(True, True)


if __name__ == "__main__":
    unittest.main()
