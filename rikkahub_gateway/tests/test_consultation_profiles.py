"""Wake-bound profile gateway tests, synthetic Control and no upstream requests."""
import copy
import io
import json
import unittest
from dataclasses import replace
from email.message import Message
from types import SimpleNamespace

from rikkahub_gateway.server import GatewayApplication, GatewayError, _execution_profile, _GatewayHandler
from rikkahub_gateway.tool_execution import NativeToolCall
from rikkahub_gateway.tests.test_gateway import config
from rikkahub_gateway.tests import test_gateway as fixtures
from rikkahub_gateway.tests.test_gateway_execution_recovery import ExecutionControl
from runtime.execution_profiles import ACTIVE_PROFILE, ARCHIVING_PROFILE


def st_tool(canonical, *, name=None):
    return {"type": "function", "function": {"name": name or canonical,
        "parameters": {"type": "object", "properties": {
            "execution_ref": {"type": "string", "x-stbrain-execution-tool": canonical,
                              "x-stbrain-execution-contract": "st-execution/1"},
            "view": {"type": "string"}, "action": {"type": "string"},
            "arguments": {"type": "object"}}, "additionalProperties": True}}}


class ProfileControl(ExecutionControl):
    def post(self, path, payload):
        result = super().post(path, payload)
        if path == "/v1/host/wakes" and "execution_profile" in payload:
            result["execution_profile"] = payload["execution_profile"]
        return result


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.control = ProfileControl()
        self.app = GatewayApplication(replace(config(), require_execution_binding=True,
                                              execution_epoch="synthetic-epoch"), control=self.control, upstream=object())
        self.headers = {"X-ST-Thread-ID": "consultation:test", "X-ST-Execution-Profile": ACTIVE_PROFILE}
        self.payload = {"messages": [{"role": "user", "content": "synthetic consultation"}],
                        "tools": [st_tool("recall_work_memory"), st_tool("remember_work_memory"),
                                  st_tool("remember_memory"), st_tool("stbrain_manage"), st_tool("stbrain_open"),
                                  *fixtures.GatewayTests.bound_tools()]}

    def tearDown(self):
        if self.app._current_session is not None:
            self.app._cancel_recovery_timer(self.app._current_session)
        self.app.close_short_term()

    def assertCode(self, code, action):
        with self.assertRaises(GatewayError) as caught:
            action()
        self.assertEqual(code, caught.exception.code)

    def test_absent_header_default_and_unknown_or_case_duplicate_rejected(self):
        self.assertEqual("default", _execution_profile({}))
        for headers in [{"X-ST-Execution-Profile": "default"}, {"X-ST-Execution-Profile": "unknown"},
                        {"X-ST-Execution-Profile": ACTIVE_PROFILE, "x-st-execution-profile": ACTIVE_PROFILE},
                        {"X-ST-Execution-Profile": " " + ACTIVE_PROFILE}]:
            self.assertCode("execution_profile_invalid", lambda: _execution_profile(headers))
        self.assertEqual(ACTIVE_PROFILE, _execution_profile(self.headers))

    def test_default_preserves_catalog_and_does_not_add_control_field(self):
        turn = self.app.prepare_turn(self.payload, {"X-ST-Thread-ID": "private"})
        self.assertEqual(self.payload["tools"], turn.payload["tools"])
        self.assertEqual("default", turn.session.execution_profile)
        self.assertNotIn("execution_profile", self.control.calls[0][1])

    def test_profile_output_budget_passthrough_without_implicit_increase(self):
        # Gateway has no 1024/2048 consultation token cap. Client/relay owns
        # that policy; do not silently enlarge ordinary chat or omitted budgets.
        for profile in (ACTIVE_PROFILE, ARCHIVING_PROFILE, "default"):
            for stream in (False, True):
                for field in ("max_tokens", "max_completion_tokens"):
                    for budget in (1024, 2048, 8192, 16384, 32768):
                        with self.subTest(profile=profile, stream=stream, field=field, budget=budget):
                            payload = {**self.payload, field: budget, "stream": stream}
                            original = copy.deepcopy(payload)
                            filtered = self.app._profile_payload(payload, profile)
                            encoded = json.loads(self.app.encode_upstream_payload(filtered, profile))
                            self.assertEqual(budget, encoded[field])
                            self.assertEqual(stream, encoded["stream"])
                            self.assertEqual(original, payload)
                omitted = json.loads(self.app.encode_upstream_payload(self.app._profile_payload(self.payload, profile), profile))
                self.assertNotIn("max_tokens", omitted)
                self.assertNotIn("max_completion_tokens", omitted)

    def test_active_filters_catalog_and_confirms_control_restriction(self):
        original = copy.deepcopy(self.payload)
        turn = self.app.prepare_turn(self.payload, self.headers)
        self.assertEqual(ACTIVE_PROFILE, turn.session.execution_profile)
        self.assertEqual(ACTIVE_PROFILE, self.control.calls[0][1]["execution_profile"])
        self.assertEqual({"recall_work_memory", "stbrain_manage", "stbrain_open"},
                         {item["function"]["name"] for item in turn.payload["tools"]})
        self.assertEqual(original, self.payload)
        self.assertIsNone(turn.session.short_term_ticket)

    def test_archiving_adds_only_work_memory_writes(self):
        turn = self.app.prepare_turn(self.payload, {**self.headers, "X-ST-Execution-Profile": ARCHIVING_PROFILE})
        names = {item["function"]["name"] for item in turn.payload["tools"]}
        self.assertIn("remember_work_memory", names)
        self.assertNotIn("remember_memory", names)

    def test_old_control_cannot_silently_drop_profile(self):
        self.app.control = ExecutionControl()
        self.assertCode("execution_profile_unconfirmed", lambda: self.app.prepare_turn(self.payload, self.headers))
        self.assertEqual("/v1/host/context/close", self.app.control.calls[-1][0])
        self.assertIsNone(self.app._current_session)

    def test_legacy_unbound_gateway_refuses_profile(self):
        self.app.config = replace(self.app.config, require_execution_binding=False)
        self.assertCode("execution_profile_unavailable", lambda: self.app.prepare_turn(self.payload, self.headers))
        self.assertEqual([], self.control.calls)
        self.assertEqual([], self.app.models()["execution_profiles"])

    def test_model_catalog_declares_profiles_only_when_enforceable(self):
        self.assertEqual([ACTIVE_PROFILE, ARCHIVING_PROFILE], self.app.models()["execution_profiles"])

    def test_compact_write_denied_before_issue_and_read_allowed(self):
        turn = self.app.prepare_turn(self.payload, self.headers)
        write = NativeToolCall("write", "stbrain_manage", json.dumps({"action": "remember_work_memory", "arguments": {}}))
        self.assertCode("execution_profile_tool_denied", lambda: self.app.decorate_execution_calls(turn, [write]))
        self.assertFalse(any(path.endswith("/issue") for path, _ in self.control.calls))
        read = NativeToolCall("read", "stbrain_manage", json.dumps({"action": "recall_work_memory", "arguments": {}}))
        result = self.app.decorate_execution_calls(turn, [read])
        self.assertIn("execution_ref", result[0].arguments_text)

    def test_stbrain_open_non_recall_views_denied(self):
        turn = self.app.prepare_turn(self.payload, self.headers)
        for view in ["summary", "manual", "full"]:
            self.assertCode("execution_profile_tool_denied", lambda: self.app.decorate_execution_calls(turn,
                [NativeToolCall("open", "stbrain_open", json.dumps({"view": view}))]))
        result = self.app.decorate_execution_calls(turn, [NativeToolCall("recall", "stbrain_open", '{"view":"recall"}')])
        self.assertIn("execution_ref", result[0].arguments_text)

    def test_upstream_compact_schema_reduced_but_validation_original_preserved(self):
        turn = self.app.prepare_turn(self.payload, self.headers)
        original = copy.deepcopy(turn.payload)
        projected = json.loads(self.app.encode_upstream_payload(turn.payload, turn.session.execution_profile))
        compact = next(item["function"]["parameters"] for item in projected["tools"] if item["function"]["name"] == "stbrain_manage")
        self.assertIn("recall_work_memory", compact["properties"]["action"]["enum"])
        self.assertNotIn("remember_work_memory", compact["properties"]["action"]["enum"])
        self.assertNotIn("execution_ref", compact["properties"])
        self.assertEqual(original, turn.payload)

    def test_continuation_cannot_upgrade_or_drop_profile(self):
        turn = self.app.prepare_turn(self.payload, self.headers)
        read = NativeToolCall("read", "recall_work_memory", '{}')
        decorated = self.app.decorate_execution_calls(turn, [read])
        bindings = self.app.bind_response_tool_calls(turn, decorated)
        self.app.finish_turn(turn, keep_for_tools=True, tool_call_ids=["read"], tool_call_bindings=bindings)
        follow = {**self.payload, "messages": [*self.payload["messages"],
            {"role": "assistant", "tool_calls": [fixtures.GatewayTests.wire_call(decorated[0])]},
            {"role": "tool", "tool_call_id": "read", "content": "done"}]}
        for headers in [{"X-ST-Thread-ID": "consultation:test"}, {**self.headers, "X-ST-Execution-Profile": ARCHIVING_PROFILE}]:
            self.assertCode("tool_continuation_profile_mismatch", lambda: self.app.prepare_turn(follow, headers))
        self.assertEqual({"read"}, turn.session.expected_tool_call_ids)

    def test_http_duplicate_profile_rejected_before_reading_body(self):
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=self.app)
        handler.path = "/v1/chat/completions"
        handler.headers = Message()
        handler.headers["Authorization"] = "Bearer " + config().gateway_token
        handler.headers["X-ST-Execution-Profile"] = ACTIVE_PROFILE
        handler.headers["X-ST-Execution-Profile"] = ACTIVE_PROFILE
        handler.rfile = io.BytesIO(b"MUST NOT READ")
        output = []
        handler._json = lambda status, result: output.append((status, result))
        handler.do_POST()
        self.assertEqual(400, output[0][0])
        self.assertEqual(0, handler.rfile.tell())


if __name__ == "__main__":
    unittest.main()
