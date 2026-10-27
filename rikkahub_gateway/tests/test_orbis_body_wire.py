"""Actual Orbis serializer output, synthetic body values; no production/provider requests."""
import copy
import json
import unittest

from rikkahub_gateway.tests.test_orbis_memory_wire import OrbisMemoryWireTests
from rikkahub_gateway.tool_execution import NativeToolCall


class OrbisBodyWireTests(unittest.TestCase):
    fixtures = OrbisMemoryWireTests.fixtures
    layouts = OrbisMemoryWireTests.layouts
    fixture = OrbisMemoryWireTests.fixture
    gateway = OrbisMemoryWireTests.gateway
    prepare = OrbisMemoryWireTests.prepare
    source = staticmethod(OrbisMemoryWireTests.source)

    def test_current_body_is_delivered_but_is_not_human_query_or_memory_capture(self):
        for layout in self.layouts:
            frames = []
            for name in ("body-a", "body-b"):
                with self.subTest(layout=layout, fixture=name):
                    app, control = self.gateway(layout)
                    body = self.fixture(name)
                    original = copy.deepcopy(body)
                    turn = self.prepare(app, body)
                    frame = self.source(control)
                    self.assertEqual("current question", frame["query_text"])
                    self.assertEqual(["old answer", "current question"],
                                     [item["content"] for item in frame["capture_items"]])
                    self.assertNotIn("当前身体状态", json.dumps(frame, ensure_ascii=False))
                    self.assertIn("当前身体状态", json.dumps(turn.payload, ensure_ascii=False))
                    self.assertEqual(original, body)
                    frames.append((frame["query_text"], frame["capture_items"]))
                    app.finish_turn(turn, keep_for_tools=False)
            self.assertEqual(frames[0], frames[1])

    def test_changing_body_preserves_earlier_prefix_and_original_human(self):
        a, b = self.fixture("body-a"), self.fixture("body-b")
        self.assertEqual(a["messages"][:-2], b["messages"][:-2])
        self.assertNotEqual(a["messages"][-2], b["messages"][-2])
        self.assertEqual(a["messages"][-1], b["messages"][-1])
        for layout in self.layouts:
            with self.subTest(layout=layout):
                app, _ = self.gateway(layout)
                first = self.prepare(app, a)
                app.finish_turn(first, keep_for_tools=False)
                second = self.prepare(app, b)
                if layout != "legacy":
                    self.assertEqual(first.payload["messages"][:4], second.payload["messages"][:4])
                for message in a["messages"][:-2]:
                    self.assertIn(message, second.payload["messages"])
                app.finish_turn(second, keep_for_tools=False)

    def test_body_stays_frozen_through_real_gateway_tool_binding(self):
        initial, continuation = self.fixture("body-first"), self.fixture("body-tool")
        self.assertEqual(initial["messages"], continuation["messages"][:-2])
        call = continuation["messages"][-2]["tool_calls"][0]
        function = call["function"]
        catalog = [{"type": "function", "function": {"name": function["name"],
                    "parameters": {"type": "object", "properties": {}}}}]
        native = NativeToolCall(tool_call_id=call["id"], tool_name=function["name"],
                               arguments_text=function["arguments"])
        for layout in self.layouts:
            with self.subTest(layout=layout):
                app, control = self.gateway(layout)
                first = self.prepare(app, {**initial, "tools": catalog})
                bindings = app.bind_response_tool_calls(first, [native])
                app.finish_turn(first, keep_for_tools=True, tool_call_ids=[call["id"]], tool_call_bindings=bindings)
                second = self.prepare(app, {**continuation, "tools": catalog})
                self.assertTrue(second.continuation)
                self.assertIs(first.session, second.session)
                self.assertEqual(1, control.seq)
                self.assertEqual(1, json.dumps(second.payload, ensure_ascii=False).count("当前身体状态"))
                if layout != "tail-context-v2":
                    self.assertEqual(first.payload["messages"], second.payload["messages"][:-2])
                else:
                    self.assertEqual(continuation["messages"], second.payload["messages"][1:-1])
                app.finish_turn(second, keep_for_tools=False)
