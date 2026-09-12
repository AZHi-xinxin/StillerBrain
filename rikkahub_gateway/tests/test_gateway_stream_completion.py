from __future__ import annotations

import io
import json
import unittest
from types import SimpleNamespace

import httpx

from rikkahub_gateway.server import GatewayApplication, GatewayError, _GatewayHandler
from rikkahub_gateway.tests.test_gateway import FakeControl, config


def event(delta=None, finish=None):
    choice = {"index": 0, "delta": delta or {}}
    if finish is not None:
        choice["finish_reason"] = finish
    return b"data: " + json.dumps({"choices": [choice]}).encode() + b"\n\n"


class StreamCompletionTests(unittest.TestCase):
    def run_response(self, raw, *, protected=False):
        calls = []
        def upstream(request):
            calls.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=raw)
        client = httpx.Client(transport=httpx.MockTransport(upstream))
        self.addCleanup(client.close)
        control = FakeControl()
        app = GatewayApplication(config(), control=control, upstream=client)
        prepared = app.prepare_turn({
            "model": config().public_model, "stream": True,
            "messages": [{"role": "user", "content": "capability-1" if protected else "synthetic request"}],
        }, {"Authorization": f"Bearer {config().gateway_token}"})
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=app)
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.close_connection = False
        try:
            handler._proxy_stream(prepared)
        except GatewayError as error:
            code = error.code
        else:
            code = None
            for line in handler.wfile.getvalue().splitlines():
                if line.startswith(b"data: {"):
                    payload = json.loads(line[6:])
                    if "error" in payload:
                        code = payload["error"]["code"]
        self.assertEqual(1, len(calls), "incomplete output must never auto-replay tools or a model request")
        self.assertIsNone(app._current_session)
        return code, handler.wfile.getvalue()

    def test_reasoning_eof_is_explicit_incomplete_error(self):
        code, raw = self.run_response(event({"reasoning_content": "thinking only"}))
        self.assertEqual("upstream_incomplete_stream", code)
        self.assertIn(b"thinking only", raw)

    def test_partial_answer_eof_is_explicit_incomplete_error(self):
        code, _ = self.run_response(event({"content": "partial answer"}))
        self.assertEqual("upstream_incomplete_stream", code)

    def test_reasoning_stop_without_answer_is_explicit_empty_error(self):
        code, _ = self.run_response(event({"reasoning_content": "thinking only"}, "stop") + b"data: [DONE]\n\n")
        self.assertEqual("upstream_empty_completion", code)

    def test_reasoning_length_without_answer_explains_budget(self):
        code, raw = self.run_response(event({"reasoning_content": "thinking only"}) + event({}, "length") + b"data: [DONE]\n\n")
        self.assertEqual("upstream_output_limit_reached", code)
        self.assertIn("请调高模型输出上限后重试".encode("utf-8"), raw)

    def test_protected_history_keeps_content_private_even_on_incomplete_eof(self):
        code, raw = self.run_response(event({"reasoning_content": "thinking only"}), protected=True)
        self.assertEqual("upstream_incomplete_stream", code)
        self.assertNotIn(b"thinking only", raw)

    def test_complete_answer_is_not_changed(self):
        code, raw = self.run_response(event({"reasoning_content": "thinking"}) + event({"content": "answer"}, "stop") + b"data: [DONE]\n\n")
        self.assertIsNone(code)
        self.assertIn(b'"content":"answer"', raw)
        self.assertEqual(1, raw.count(b"data: [DONE]"))

    def test_done_then_tool_is_rejected_before_done_or_tool_delivery(self):
        tools = event({"tool_calls": [{"index": 0, "id": "must-not-release", "type": "function", "function": {"name": "stbrain_health", "arguments": "{}"}}]}, "tool_calls")
        for protected in (False, True):
            with self.subTest(protected=protected):
                code, raw = self.run_response(event({"content": "prefix"}) + b"data: [DONE]\n\n" + tools, protected=protected)
                self.assertEqual("upstream_invalid_stream", code)
                self.assertNotIn(b"must-not-release", raw)
                if b"data: [DONE]" in raw:
                    self.assertLess(raw.index(b'"error"'), raw.index(b"data: [DONE]"))

    def test_finish_then_more_same_choice_content_is_rejected(self):
        for protected in (False, True):
            with self.subTest(protected=protected):
                code, raw = self.run_response(event({"content": "prefix"}, "stop") + event({"content": "after-finish"}) + b"data: [DONE]\n\n", protected=protected)
                self.assertEqual("upstream_invalid_stream", code)
                self.assertNotIn(b"after-finish", raw)
                self.assertNotIn(b'"finish_reason":"stop"', raw)

    def test_finish_usage_done_remains_legal(self):
        for protected in (False, True):
            with self.subTest(protected=protected):
                code, raw = self.run_response(event({"content": "answer"}, "stop") + b'data: {"choices":[],"usage":{"completion_tokens":3}}\n\n' + b"data: [DONE]\n\n", protected=protected)
                self.assertIsNone(code)
                self.assertIn(b'"completion_tokens":3', raw)
                self.assertEqual(1, raw.count(b"data: [DONE]"))

    def test_one_finished_choice_does_not_block_other_choice(self):
        raw_second = b'data: {"choices":[{"index":1,"delta":{"content":"second"},"finish_reason":"stop"}]}\n\n'
        code, raw = self.run_response(event({"content": "first"}, "stop") + raw_second + b"data: [DONE]\n\n")
        self.assertIsNone(code)
        self.assertIn(b"first", raw)
        self.assertIn(b"second", raw)


if __name__ == "__main__":
    unittest.main()
