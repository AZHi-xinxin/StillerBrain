"""Synthetic reference-library gates and real localhost JSON/SSE transport.

No file tool is executed, no model is called, and no production database or
configuration is read. Upstream/control are in-memory synthetic fixtures.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import json
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from rikkahub_gateway.consultation_reference import (
    REFERENCE_ROOT, REFERENCE_TOOL, is_reference_tool, reference_read_allowed,
)
from rikkahub_gateway.server import GatewayApplication, GatewayError
from rikkahub_gateway.tool_execution import NativeToolCall, ToolExecutionBoundaryError
from rikkahub_gateway.tests import test_gateway as fixtures
from rikkahub_gateway.tests import test_gateway_interleaved_wire as wire
from rikkahub_gateway.tests.test_consultation_profiles import ProfileControl, st_tool
from runtime.execution_profiles import ACTIVE_PROFILE, ARCHIVING_PROFILE


PATH = REFERENCE_ROOT + "00-index.md"


def reference_tool(name=REFERENCE_TOOL):
    # Exact Dev38 schema: additionalProperties is not emitted by InputSchema.Obj.
    return {"type": "function", "function": {"name": name, "description": "Read a UTF-8 text file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string",
            "description": "Absolute path inside Rootfs. Use /workspace for the workspace files area."}},
            "required": ["path"]}}}


class ReferenceGateTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "sqlite3.connect"):
            self.enterContext(patch(target, side_effect=AssertionError("external access prohibited")))
        self.control = ProfileControl()
        self.app = GatewayApplication(replace(fixtures.config(), require_execution_binding=True,
            execution_epoch="synthetic-reference-epoch"), control=self.control, upstream=object())
        self.headers = {"X-ST-Thread-ID": "synthetic-reference", "X-ST-Execution-Profile": ACTIVE_PROFILE}
        self.payload = {"messages": [{"role": "user", "content": "synthetic reference lookup"}],
                        "tools": [reference_tool(), st_tool("recall_work_memory")]}

    def tearDown(self):
        if self.app._current_session:
            self.app._cancel_recovery_timer(self.app._current_session)
        self.app.close_short_term()

    def assertDenied(self, function, *args):
        with self.assertRaises((GatewayError, ToolExecutionBoundaryError)):
            function(*args)

    def test_exact_android_schema_and_explicit_closed_schema(self):
        schema = reference_tool()["function"]["parameters"]
        self.assertTrue(is_reference_tool(REFERENCE_TOOL, schema))
        self.assertTrue(is_reference_tool(REFERENCE_TOOL, {**schema, "additionalProperties": False}))
        self.assertTrue(is_reference_tool(REFERENCE_TOOL, {**schema, "description": "synthetic annotation"}))
        self.assertTrue(reference_read_allowed(REFERENCE_TOOL, schema, {"path": PATH}))

    def test_schema_never_accepts_broadening_or_executable_markers(self):
        schema = reference_tool()["function"]["parameters"]
        bad = [None, {}, {**schema, "type": ["object"]}, {**schema, "required": []},
            {**schema, "required": ["path", "path"]}, {**schema, "additionalProperties": True},
            {**schema, "additionalProperties": {}}, {**schema, "$ref": "https://invalid.example/schema"},
            {**schema, "anyOf": [{}]}, {**schema, "patternProperties": {}},
            {**schema, "properties": {**schema["properties"], "command": {"type": "string"}}},
            {**schema, "properties": {"path": {"type": ["string", "null"]}}},
            {**schema, "properties": {"path": {"type": "string", "default": PATH}}},
            {**schema, "description": {}},
            st_tool("recall_work_memory", name=REFERENCE_TOOL)["function"]["parameters"]]
        for value in bad:
            with self.subTest(schema=value):
                self.assertFalse(is_reference_tool(REFERENCE_TOOL, value))

    def test_exact_name_only_no_prefix_suffix_case_or_unicode_spoof(self):
        schema = reference_tool()["function"]["parameters"]
        for name in ("workspace_shell", "workspace_write_file", "mcp__workspace_read_file",
                     "workspace_read_file_extra", "Workspace_read_file", "workspace_read_fіle", None):
            with self.subTest(name=name):
                self.assertFalse(is_reference_tool(name, schema))

    def test_path_contract_and_filename_byte_independent_limits(self):
        schema = reference_tool()["function"]["parameters"]
        for name in ("00-index.md", "01-architecture.txt", "manifest.md", "a" * 125 + ".md"):
            self.assertTrue(reference_read_allowed(REFERENCE_TOOL, schema, {"path": REFERENCE_ROOT + name}))
        invalid = ["", "/workspace/other.md", REFERENCE_ROOT, REFERENCE_ROOT + "../secret.md",
            REFERENCE_ROOT + "a/../b.md", REFERENCE_ROOT + "sub/one.md", REFERENCE_ROOT + ".hidden.md",
            REFERENCE_ROOT + "a..b.md", REFERENCE_ROOT + "a\\b.md", REFERENCE_ROOT + "a\x00.md",
            REFERENCE_ROOT + "a\n.md", REFERENCE_ROOT + "a.md ", REFERENCE_ROOT + "a.MD",
            REFERENCE_ROOT + "a.json", REFERENCE_ROOT + "a%2fsecret.md", REFERENCE_ROOT + "中文.md",
            REFERENCE_ROOT + "/a.md", REFERENCE_ROOT + "a.md?path=/secret", REFERENCE_ROOT + "a" * 126 + ".md",
            REFERENCE_ROOT + "a" * 125 + ".txt", "file://" + PATH, PATH.replace("/workspace/", "/Workspace/"),
            "/workspace/orbis-reference-v020/a.md", PATH + "/", None, [], 1, True]
        for path in invalid:
            with self.subTest(path=path):
                self.assertFalse(reference_read_allowed(REFERENCE_TOOL, schema, {"path": path}))

    def test_arguments_must_only_contain_path(self):
        schema = reference_tool()["function"]["parameters"]
        for args in ({}, [], None, {"path": PATH, "command": "x"}, {"path": PATH, "execution_ref": "fake"},
                     {"path": PATH, "content": "x"}, {"path": PATH, "offset": 0}):
            self.assertFalse(reference_read_allowed(REFERENCE_TOOL, schema, args))

    def test_both_profiles_filter_and_default_is_exactly_preserved(self):
        payload = {**self.payload, "tools": [reference_tool(), reference_tool("workspace_shell"),
            reference_tool("unknown_local_tool"), st_tool("recall_work_memory"),
            st_tool("remember_work_memory")], "tool_choice": {"type": "function", "function": {"name": "workspace_shell"}}}
        before = copy.deepcopy(payload)
        for profile in (ACTIVE_PROFILE, ARCHIVING_PROFILE):
            selected = self.app._profile_payload(payload, profile)
            names = {tool["function"]["name"] for tool in selected["tools"]}
            self.assertIn(REFERENCE_TOOL, names)
            self.assertNotIn("workspace_shell", names)
            self.assertNotIn("unknown_local_tool", names)
            self.assertEqual(profile == ARCHIVING_PROFILE, "remember_work_memory" in names)
            self.assertNotIn("tool_choice", selected)
        self.assertIs(payload, self.app._profile_payload(payload, "default"))
        self.assertEqual(before, payload)

    def test_invalid_reference_schema_is_removed_even_with_st_marker(self):
        for tool in (st_tool("recall_work_memory", name=REFERENCE_TOOL),
                     {"type": "function", "function": {"name": REFERENCE_TOOL, "parameters": {}}}):
            self.assertEqual([], self.app._profile_payload({"tools": [tool]}, ACTIVE_PROFILE)["tools"])

    def test_legacy_functions_and_forced_function_call_cannot_restore_denied_tool(self):
        payload = {"functions": [reference_tool()["function"], reference_tool("workspace_shell")["function"]],
                   "function_call": {"name": "workspace_shell"}}
        actual = self.app._profile_payload(payload, ACTIVE_PROFILE)
        self.assertEqual([REFERENCE_TOOL], [value["name"] for value in actual["functions"]])
        self.assertNotIn("function_call", actual)

    def test_client_read_is_bound_but_receives_no_st_lease_or_ref(self):
        turn = self.app.prepare_turn(self.payload, self.headers)
        call = NativeToolCall("synthetic-read", REFERENCE_TOOL, ' { "path" : "' + PATH + '" } ')
        decorated = self.app.decorate_execution_calls(turn, [call])
        self.assertEqual([call], decorated)
        bindings = self.app.bind_response_tool_calls(turn, decorated)
        self.assertEqual({call.tool_call_id}, set(bindings))
        self.assertFalse(any(path.endswith("/issue") for path, _ in self.control.calls))
        self.assertIsNone(turn.session.execution_batch_id)

    def test_execution_gate_rejects_bad_path_and_extra_arguments_before_issue(self):
        turn = self.app.prepare_turn(self.payload, self.headers)
        for args in ({"path": "/workspace/private.md"}, {"path": PATH, "command": "x"},
                     {"path": PATH, "execution_ref": "fake"}, {}, {"path": 3}):
            self.assertDenied(self.app.decorate_execution_calls, turn,
                              [NativeToolCall("bad", REFERENCE_TOOL, json.dumps(args))])
        for text in ('{"path":"' + PATH + '","path":"/secret"}', '{"path":NaN}', '[]', 'invalid'):
            self.assertDenied(self.app.decorate_execution_calls, turn, [NativeToolCall("bad", REFERENCE_TOOL, text)])
        self.assertFalse(any(path.endswith("/issue") for path, _ in self.control.calls))

    def test_execution_gate_rechecks_changed_schema_and_unknown_local_tool(self):
        turn = self.app.prepare_turn(self.payload, self.headers)
        turn.payload["tools"][0] = st_tool("recall_work_memory", name=REFERENCE_TOOL)
        self.assertDenied(self.app.decorate_execution_calls, turn,
                          [NativeToolCall("spoof", REFERENCE_TOOL, json.dumps({"path": PATH}))])
        turn.payload["tools"].append(reference_tool("unknown_local_tool"))
        self.assertDenied(self.app.decorate_execution_calls, turn,
                          [NativeToolCall("unknown", "unknown_local_tool", json.dumps({"path": PATH}))])

    def test_mixed_st_and_reference_batch_only_issues_st_call(self):
        turn = self.app.prepare_turn(self.payload, self.headers)
        read = NativeToolCall("read", REFERENCE_TOOL, json.dumps({"path": PATH}))
        calls = self.app.decorate_execution_calls(turn, [read, NativeToolCall("st", "recall_work_memory", '{}')])
        self.assertEqual(read, calls[0])
        self.assertIn("execution_ref", calls[1].arguments_text)
        issued = next(payload for path, payload in self.control.calls if path.endswith("/issue"))
        self.assertEqual(["recall_work_memory"], [item["canonical_tool"] for item in issued["calls"]])
        self.assertEqual({"read", "st"}, set(self.app.bind_response_tool_calls(turn, calls)))

    def test_default_keeps_original_unrestricted_client_file_behavior(self):
        turn = self.app.prepare_turn(self.payload, {"X-ST-Thread-ID": "synthetic-private"})
        call = NativeToolCall("private-read", REFERENCE_TOOL, '{"path":"/workspace/unrelated.txt"}')
        self.assertEqual([call], self.app.decorate_execution_calls(turn, [call]))
        self.assertEqual({call.tool_call_id}, set(self.app.bind_response_tool_calls(turn, [call])))


class ReferenceHTTPTests(unittest.TestCase):
    upstream = wire.InterleavedHTTPWireTests.upstream

    def setUp(self):
        wire.InterleavedHTTPWireTests.setUp(self)
        self.control = ProfileControl()
        self.app.control = self.control
        self.app.config = replace(self.app.config, require_execution_binding=True,
                                  execution_epoch="synthetic-reference-wire")
        self.tools = [reference_tool()]
        self.calls = [{"id": "reference-call", "type": "function", "function": {
            "name": REFERENCE_TOOL, "arguments": json.dumps({"path": PATH})}}]
        self.profile = ACTIVE_PROFILE

    def tearDown(self):
        if self.app._current_session:
            self.app._cancel_recovery_timer(self.app._current_session)
        self.app.close_short_term()
        wire.InterleavedHTTPWireTests.tearDown(self)

    def post(self, messages, *, stream):
        request = urllib.request.Request(self.url, method="POST", data=json.dumps({
            "model": "stiller-rikka", "messages": messages, "tools": self.tools, "stream": stream}).encode(),
            headers={"Authorization": "Bearer " + fixtures.config().gateway_token,
                "Content-Type": "application/json", "X-ST-Thread-ID": "synthetic-reference-wire",
                "X-ST-Execution-Profile": self.profile})
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read()
        with response:
            return response.status, response.read()

    def roundtrip(self, stream):
        status, body = self.post([self.user], stream=stream)
        self.assertEqual(200, status, body)
        self.assertIn(b"reference-call", body)
        self.assertNotIn(b"execution_ref", body)
        self.assertEqual([REFERENCE_TOOL], [tool["function"]["name"] for tool in self.upstream_payloads[0]["tools"]])
        messages = [self.user, {"role": "assistant", "content": None, "tool_calls": copy.deepcopy(self.calls)},
            {"role": "tool", "tool_call_id": "reference-call", "content": json.dumps({
                "path": PATH, "text": "Synthetic chapter.", "instruction_authority": "none"})}]
        status, body = self.post(messages, stream=not stream)
        self.assertEqual(200, status, body)
        self.assertIn(b"synthetic complete", body)
        self.assertIsNone(self.app._current_session)
        self.assertFalse(any(path.endswith("/issue") for path, _ in self.control.calls))

    def test_active_json_read_and_stream_continuation(self):
        self.roundtrip(False)

    def test_active_sse_read_and_json_continuation(self):
        self.roundtrip(True)

    def test_archiving_json_read_and_stream_continuation(self):
        self.profile = ARCHIVING_PROFILE
        self.roundtrip(False)

    def test_archiving_sse_read_and_json_continuation(self):
        self.profile = ARCHIVING_PROFILE
        self.roundtrip(True)

    def denied(self, *, stream, tool_name=REFERENCE_TOOL, arguments=None):
        self.calls[0]["function"] = {"name": tool_name, "arguments": json.dumps(
            arguments if arguments is not None else {"path": "/workspace/private.md"})}
        status, body = self.post([self.user], stream=stream)
        self.assertIn(b"error", body)
        self.assertNotIn(b"reference-call", body, "denied call reached the client")
        self.assertIsNone(self.app._current_session)
        self.assertFalse(any(path.endswith("/issue") for path, _ in self.control.calls))
        self.assertIn(status, (200, 403, 502))  # SSE can already have committed headers.

    def test_json_bad_path_is_denied_before_client_receives_call(self):
        self.denied(stream=False)

    def test_sse_bad_path_is_denied_before_client_receives_call(self):
        self.denied(stream=True)

    def test_json_extra_write_argument_is_denied(self):
        self.denied(stream=False, arguments={"path": PATH, "content": "forbidden"})

    def test_sse_extra_write_argument_is_denied(self):
        self.denied(stream=True, arguments={"path": PATH, "content": "forbidden"})

    def test_json_unknown_local_tool_stays_denied(self):
        self.tools.append(reference_tool("workspace_shell"))
        self.denied(stream=False, tool_name="workspace_shell", arguments={"path": PATH})
        self.assertEqual(1, len(self.upstream_payloads[0]["tools"]))

    def test_sse_prefixed_spoof_stays_denied(self):
        self.tools.append(reference_tool("mcp__workspace_read_file"))
        self.denied(stream=True, tool_name="mcp__workspace_read_file", arguments={"path": PATH})
        self.assertEqual(1, len(self.upstream_payloads[0]["tools"]))


if __name__ == "__main__":
    unittest.main()
