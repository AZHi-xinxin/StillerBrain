"""Orbis ChatCompletionsAPI exports, synthetic only; no provider or production calls.

Regenerate fixtures with Orbis MemoryProviderWireTest, then copy app/build/reports/
memory-wire/*.json here. Keep the actual serializer output, not a hand-built layout.
"""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
import unittest

from rikkahub_gateway.server import GatewayApplication, GatewayError
from rikkahub_gateway.tool_execution import NativeToolCall
from rikkahub_gateway.tests.test_gateway import config
from rikkahub_gateway.tests.test_cache_layout import BundleControl
from rikkahub_gateway.tests.test_tail_context_v2_gateway import TailControl


class OrbisMemoryWireTests(unittest.TestCase):
    fixtures = Path(__file__).with_name("fixtures") / "orbis-memory-wire"
    layouts = ("legacy", "anchored-v1", "tail-context-v2")

    def fixture(self, name):
        return json.loads((self.fixtures / (name + ".json")).read_text())

    def gateway(self, layout):
        control = TailControl() if layout == "tail-context-v2" else BundleControl()
        app = GatewayApplication(replace(config(), context_layout=layout), control=control)
        self.addCleanup(app.upstream.close)
        return app, control

    def prepare(self, app, body):
        turn = app.prepare_turn({**body, "model": config().public_model},
                                {"X-ST-Thread-ID": "synthetic-orbis-memory"})
        if app.config.context_layout != "legacy":
            self.assertIsNotNone(turn.session.context_bundle, "must test negotiated layout, not legacy fallback")
        return turn

    @staticmethod
    def source(control):
        return [p["source_frame"] for path, p in control.calls
                if path == "/v1/host/context/prepare"][-1]

    def test_recall_does_not_become_human_query_or_capture(self):
        for layout in self.layouts:
            frames = []
            for name in ("plain", "recall-a", "recall-b"):
                with self.subTest(layout=layout, fixture=name):
                    app, control = self.gateway(layout)
                    body = self.fixture(name)
                    original = copy.deepcopy(body)
                    turn = self.prepare(app, body)
                    frame = self.source(control)
                    self.assertEqual("current question", frame["query_text"])
                    self.assertEqual(["old answer", "current question"],
                                     [item["content"] for item in frame["capture_items"]])
                    self.assertNotIn("memory A", json.dumps(frame))
                    self.assertNotIn("historical excerpt", json.dumps(frame))
                    self.assertEqual(original, body)
                    # Every original wire message remains present and ordered.
                    positions = [turn.payload["messages"].index(m) for m in body["messages"]]
                    self.assertEqual(sorted(positions), positions)
                    frames.append((frame["query_text"], frame["capture_items"]))
                    app.finish_turn(turn, keep_for_tools=False)
            self.assertEqual(frames[0], frames[1])
            self.assertEqual(frames[1], frames[2])

    def test_first_turn_and_image_only_classification_survive_recall(self):
        for layout in self.layouts:
            for name, query, first in (("first-turn", "current question", True),
                                       ("image-only", "", False)):
                with self.subTest(layout=layout, fixture=name):
                    app, control = self.gateway(layout)
                    turn = self.prepare(app, self.fixture(name))
                    frame = self.source(control)
                    self.assertEqual(query, frame["query_text"])
                    self.assertEqual(first, frame["first_user_turn"])
                    self.assertNotIn("recall", json.dumps(frame))
                    app.finish_turn(turn, keep_for_tools=False)

    def test_changed_recall_preserves_stable_wire_prefix_after_gateway(self):
        a, b = self.fixture("recall-a"), self.fixture("recall-b")
        self.assertEqual(a["messages"][:3], b["messages"][:3])
        self.assertNotEqual(a["messages"][3], b["messages"][3])
        self.assertEqual(a["messages"][4:], b["messages"][4:])
        for layout in self.layouts:
            with self.subTest(layout=layout):
                app, _ = self.gateway(layout)
                first = self.prepare(app, a)
                app.finish_turn(first, keep_for_tools=False)
                second = self.prepare(app, b)
                # Legacy ST owns a changing leading frame; its stable-prefix
                # limitation is explicit. Orbis still preserves its own prefix.
                if layout != "legacy":
                    self.assertEqual(first.payload["messages"][:4], second.payload["messages"][:4])
                for turn in (first, second):
                    for message in a["messages"][:3]:
                        self.assertIn(message, turn.payload["messages"])
                app.finish_turn(second, keep_for_tools=False)

    def test_real_tool_continuation_keeps_frozen_memory_and_call_binding(self):
        initial, continuation = self.fixture("recall-a"), self.fixture("tool-continuation")
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
                app.finish_turn(first, keep_for_tools=True, tool_call_ids=[call["id"]],
                                tool_call_bindings=bindings)
                if layout == "anchored-v1":
                    changed = copy.deepcopy(continuation)
                    changed["messages"][3] = self.fixture("recall-b")["messages"][3]
                    with self.assertRaises(GatewayError) as caught:
                        self.prepare(app, {**changed, "tools": catalog})
                    self.assertEqual("tool_continuation_context_history_mismatch", caught.exception.code)
                second = self.prepare(app, {**continuation, "tools": catalog})
                self.assertTrue(second.continuation)
                self.assertIs(first.session, second.session)
                if layout == "tail-context-v2":
                    # V2 deliberately appends the frozen ST frame after all client
                    # messages. Client tool continuation stays exact and ordered.
                    self.assertEqual(first.payload["messages"][0], second.payload["messages"][0])
                    self.assertEqual(first.payload["messages"][-1], second.payload["messages"][-1])
                    self.assertEqual(continuation["messages"], second.payload["messages"][1:-1])
                else:
                    self.assertEqual(first.payload["messages"], second.payload["messages"][:-2])
                self.assertEqual(1, control.seq)
                self.assertEqual(1, sum(path == "/v1/host/context/prepare" for path, _ in control.calls))
                app.finish_turn(second, keep_for_tools=False)

    def test_human_literal_memory_marker_is_not_stripped(self):
        text = "Please explain [本机历史资料； this is a human quotation, not host metadata."
        for layout in self.layouts:
            with self.subTest(layout=layout):
                app, control = self.gateway(layout)
                body = self.fixture("plain")
                body["messages"][-1]["content"] = text
                turn = self.prepare(app, body)
                self.assertEqual(text, self.source(control)["query_text"])
                self.assertEqual(text, self.source(control)["capture_items"][-1]["content"])
                app.finish_turn(turn, keep_for_tools=False)
