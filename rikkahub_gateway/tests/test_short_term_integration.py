"""Synthetic only: no real upstream, phone, memory DB or conversation."""
import copy
import io
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

import httpx

from rikkahub_gateway.server import GatewayApplication, _GatewayHandler, _digest
from rikkahub_gateway.short_term_wire import FinalBodyCollector, client_source, MARKER
from rikkahub_gateway.tests.test_gateway import config
from rikkahub_gateway.tests import test_gateway as legacy
from rikkahub_gateway.tests.test_tail_context_v2_gateway import TailControl


class ShortTermControl(TailControl):
    allowed = True

    def post(self, path, payload):
        result = super().post(path, payload)
        if path.endswith("/prepare"):
            result["short_term_policy"] = {"contract": "st-short-term-policy/1", "enabled": self.allowed}
        return result


def body(text="synthetic final", *, finish="stop", mid="test-message"):
    return {"id": mid, "choices": [{"index": 0, "finish_reason": finish,
            "message": {"role": "assistant", "content": text, "reasoning_content": "NEVER_RETAIN_REASONING"}}]}


class FinalCaptureTests(unittest.TestCase):
    def test_json_body_only_and_verbatim(self):
        c = FinalBodyCollector()
        c.feed(body("  正文\n第二行  "), stream=False)
        self.assertEqual(("test-message", "  正文\n第二行  "), c.result())
        self.assertNotIn("NEVER_RETAIN_REASONING", repr(c.__dict__))

    def test_reject_incomplete_tools_errors_multi_choice_and_inline_reasoning(self):
        for item in (body(finish="length"), body(finish=None), body(finish="tool_calls"),
                     body("<think>private</think>body"), {"error": {}},
                     {"choices": body()["choices"] * 2}, body("")):
            with self.subTest(item=item):
                c = FinalBodyCollector()
                c.feed(item, stream=False)
                self.assertIsNone(c.result())

    def test_stream_only_visible_body(self):
        c = FinalBodyCollector()
        for delta, finish in (({"reasoning_content": "NEVER_RETAIN_REASONING"}, None),
                              ({"content": "原文\n"}, None), ({"content": "原样"}, None), ({}, "stop")):
            c.feed({"id": "one", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
        self.assertEqual(("one", "原文\n原样"), c.result())
        self.assertNotIn("NEVER_RETAIN_REASONING", repr(c.__dict__))

    def test_overflow_drops_whole(self):
        c = FinalBodyCollector(max_bytes=4)
        c.feed(body("too long"), stream=False)
        self.assertEqual([], c.parts)
        self.assertIsNone(c.result())

    def test_source_is_explicit_not_content(self):
        self.assertIsNone(client_source({}))
        self.assertIsNone(client_source({"User-Agent": "RikkaHub", "user": "owner"}))
        self.assertIsNone(client_source({"X-ST-Thread-ID": "A", "X-Session-ID": "B"}))
        self.assertIsNone(client_source({"X-Session-ID": "<fake>"}))
        self.assertEqual("A", client_source({"X-Session-ID": "A"}).conversation)


class ShortTermGatewayTests(unittest.TestCase):
    def setUp(self):
        self.control = ShortTermControl()
        self.app = GatewayApplication(replace(config(), context_layout="tail-context-v2", short_term_enabled=True), control=self.control)
        self.addCleanup(self.app.upstream.close)
        self.addCleanup(self.app.close_short_term)

    def prepare(self, window="A", messages=None, **extra):
        return self.app.prepare_turn(legacy.GatewayTests.request(messages or [{"role": "user", "content": "synthetic prompt"}], **extra),
                                     {"X-Session-ID": window} if window is not None else {})

    def complete(self, prepared, text="synthetic final", mid="msg"):
        self.app.finish_turn(prepared, keep_for_tools=False)
        c = FinalBodyCollector()
        c.feed(body(text, mid=mid), stream=False)
        self.app.capture_final_body(prepared, c)

    def handover(self, prepared):
        frames = [m["content"] for m in prepared.payload["messages"] if isinstance(m.get("content"), str) and m["content"].startswith(MARKER)]
        return json.loads(frames[0][len(MARKER):]) if frames else None

    def test_a_to_b_immediately_no_more_a_request(self):
        a = self.prepare("A")
        self.complete(a, "  小鼯鼠，吱一声\n原文  ")
        b = self.prepare("B", [{"role": "user", "content": "吱"}])
        handover = self.handover(b)
        self.assertEqual("  小鼯鼠，吱一声\n原文  ", handover["items"][0]["content"])
        self.assertEqual("none", handover["instruction_authority"])
        self.assertEqual(_digest(b.session.context_bundle), b.session.context_hash)
        self.assertNotIn("小鼯鼠", json.dumps(self.control.calls, ensure_ascii=False))
        self.assertNotIn("小鼯鼠", json.dumps(b.session.context_bundle, ensure_ascii=False))

    def test_same_window_no_handover_and_only_last_three_final_bodies(self):
        for index in range(5):
            p = self.prepare("A")
            self.assertIsNone(self.handover(p))
            self.complete(p, "body" + str(index), "msg" + str(index))
        b = self.prepare("B")
        self.assertEqual(["body2", "body3", "body4"], [i["content"] for i in self.handover(b)["items"]])
        self.complete(b, "B final", "B-msg")
        self.assertIsNone(self.handover(self.prepare("B")))

    def test_unknown_source_disables_and_never_captures_incoming_history(self):
        p = self.prepare(None, [{"role": "user", "content": "USER_SECRET_SENTINEL"},
                                {"role": "assistant", "content": "OLD_HISTORY_SENTINEL"},
                                {"role": "user", "content": "new"}])
        self.complete(p)
        self.assertIsNone(self.handover(self.prepare("B")))
        for path, payload in self.control.calls:
            if path.endswith("/prepare"):
                self.assertEqual([], payload["source_frame"]["capture_items"])

    def test_handover_survives_tool_chain_but_not_new_human_turn(self):
        a = self.prepare("A")
        self.complete(a, "A final")
        tools = legacy.GatewayTests.bound_tools()
        p = self.prepare("B", tools=tools)
        call = legacy.GatewayTests.native_call("one-tool", "synthetic no execution")
        bindings = self.app.bind_response_tool_calls(p, [call])
        self.app.finish_turn(p, keep_for_tools=True, tool_call_ids=[call.tool_call_id], tool_call_bindings=bindings)
        messages = [{"role": "user", "content": "synthetic prompt"},
                    {"role": "assistant", "content": None, "tool_calls": [legacy.GatewayTests.wire_call(call)]},
                    {"role": "tool", "tool_call_id": call.tool_call_id, "content": '{"exitCode":0}'}]
        follow = self.prepare("B", messages, tools=tools)
        self.assertEqual("A final", self.handover(follow)["items"][0]["content"])
        self.complete(follow, "B final", "B-msg")
        self.assertEqual({}, self.app._short_term_frames)
        self.assertIsNone(self.handover(self.prepare("B")))

    def test_handover_copies_expire_and_control_identity_is_required(self):
        a = self.prepare("A")
        self.complete(a)
        b = self.prepare("B")
        started, _, handover = self.app._short_term_frames[b.session.wake_id]
        self.app._short_term_frames[b.session.wake_id] = (started, 0, handover)
        self.app.maintain_short_term()
        self.assertEqual({}, self.app._short_term_frames)

    def test_hard_off_or_old_control_suppresses_and_discards_cache(self):
        a = self.prepare("A")
        self.complete(a, "must disappear")
        self.control.allowed = False
        b = self.prepare("B")
        self.assertIsNone(self.handover(b))
        self.assertNotIn("ST_SOURCE_V1", json.dumps(b.payload))
        self.complete(b, "must not capture", "off")
        self.control.allowed = True
        self.assertIsNone(self.handover(self.prepare("C")))

    def test_wire_stream_and_json_capture_only_after_delivery(self):
        for stream in (False, True):
            self.app.close_short_term()
            self.setUp()
            p = self.prepare("A", stream=stream)
            if stream:
                raw = b""
                for delta, finish in (({"reasoning_content": "SECRET_REASONING"}, None), ({"content": "wire final"}, None), ({}, "stop")):
                    raw += ("data: " + json.dumps({"id": "wire", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n").encode()
                raw += b"data: [DONE]\n\n"
            else:
                raw = json.dumps(body("wire final")).encode()
            self.app.upstream.close()
            self.app.upstream = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw)))
            handler = object.__new__(_GatewayHandler)
            handler.server = SimpleNamespace(application=self.app)
            handler.wfile = io.BytesIO()
            handler.send_response = lambda *a: None
            handler.send_header = lambda *a: None
            handler.end_headers = lambda: None
            (handler._proxy_stream if stream else handler._proxy_json)(p)
            b = self.prepare("B")
            self.assertEqual("wire final", self.handover(b)["items"][0]["content"])


if __name__ == "__main__":
    unittest.main()
