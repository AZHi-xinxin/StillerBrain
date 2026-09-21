"""Synthetic regressions for fast-model/chat isolation. No external services."""
import io
import json
import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace

import httpx

from rikkahub_gateway.server import GatewayApplication, GatewayError, _GatewayHandler
from rikkahub_gateway.short_term_wire import FinalBodyCollector, MARKER
from rikkahub_gateway.tests.test_gateway import config
from rikkahub_gateway.tests import test_gateway as legacy
from rikkahub_gateway.tests.test_short_term_integration import ShortTermControl, body
from rikkahub_gateway.tests.test_short_term_review import sse, BrokenWrite


class AuxiliaryIsolationTests(unittest.TestCase):
    def setUp(self):
        self.control = ShortTermControl()
        self.app = GatewayApplication(replace(config(), context_layout="tail-context-v2",
                                             short_term_enabled=True), control=self.control)
        self.addCleanup(lambda: self.app.upstream.close())
        self.addCleanup(self.app.close_short_term)
        self.requests = []
        self.mock_response()

    def mock_response(self, raw=None, status=200, callback=None):
        self.app.upstream.close()
        def respond(request):
            self.requests.append(json.loads(request.content))
            if callback is not None:
                return callback(request)
            return httpx.Response(status, content=raw if raw is not None else json.dumps(body("标题")))
        self.app.upstream = httpx.Client(transport=httpx.MockTransport(respond))

    def aux_payload(self, **extra):
        return {"model": self.app.auxiliary_model,
                "messages": [{"role": "user", "content": "synthetic title task"}], **extra}

    def prepare(self, window="A", **extra):
        return self.app.prepare_turn(legacy.GatewayTests.request(
            [{"role": "user", "content": "synthetic conversation"}], **extra),
            {"X-Session-ID": window} if window is not None else {})

    def complete(self, prepared, text="纸船42", mid="chat-A"):
        self.app.finish_turn(prepared, keep_for_tools=False)
        collector = FinalBodyCollector()
        collector.feed(body(text, mid=mid), stream=False)
        self.app.capture_final_body(prepared, collector)

    def handover(self, prepared):
        for m in prepared.payload["messages"]:
            if isinstance(m.get("content"), str) and m["content"].startswith(MARKER):
                return json.loads(m["content"][len(MARKER):])

    def handler(self, payload=None, headers=None, output=None, path="/v1/chat/completions"):
        h = object.__new__(_GatewayHandler)
        h.server = SimpleNamespace(application=self.app)
        raw = json.dumps(payload or self.aux_payload()).encode()
        h.headers = {"Authorization": "Bearer " + self.app.config.gateway_token,
                     "Content-Type": "application/json", "Content-Length": str(len(raw)),
                     **(headers or {})}
        h.path = path
        h.rfile = io.BytesIO(raw)
        h.wfile = output if output is not None else io.BytesIO()
        h.statuses, h.response_headers = [], {}
        h.send_response = h.statuses.append
        h.send_header = lambda key, value: h.response_headers.update({key: value})
        h.end_headers = lambda: None
        return h

    def test_alias_directory_and_no_prompt_or_missing_source_guessing(self):
        self.assertEqual(self.app.auxiliary_model, self.app.models()["data"][1]["id"])
        self.assertFalse(self.app.is_auxiliary({"messages": [{"role": "user", "content": "generate title"}]}))
        self.assertFalse(self.app.is_auxiliary({"model": self.app.config.public_model}))
        self.assertTrue(self.app.is_auxiliary(self.aux_payload()))

    def test_direct_prepare_cannot_open_wake_for_alias(self):
        with self.assertRaisesRegex(GatewayError, "auxiliary_requires_stateless_route"):
            self.app.prepare_turn(self.aux_payload(), {})
        self.assertEqual([], self.control.calls)

    def test_a_aux_b_preserves_verbatim_handover_for_json_and_sse_with_or_without_source(self):
        for stream in (False, True):
            for headers in ({}, {"X-Session-ID": "A"}, {"X-Session-ID": "background"},
                            {"X-Session-ID": "A", "X-ST-Thread-ID": "conflict"}):
                with self.subTest(stream=stream, headers=headers):
                    self.app.short_term.clear()
                    self.complete(self.prepare(), "  纸船42\n原样  ")
                    calls = list(self.control.calls)
                    before = self.app.short_term.stats()
                    self.mock_response(raw=sse("only title") if stream else None)
                    handler = self.handler(self.aux_payload(stream=stream), headers)
                    handler.do_POST()
                    self.assertEqual([200], handler.statuses)
                    self.assertEqual(calls, self.control.calls)
                    self.assertEqual(before, self.app.short_term.stats())
                    self.assertIsNone(self.app._current_session)
                    self.assertEqual(self.app.config.upstream_model, self.requests[-1]["model"])
                    self.assertEqual([{"role": "user", "content": "synthetic title task"}],
                                     self.requests[-1]["messages"])
                    b = self.prepare("B")
                    self.assertEqual("  纸船42\n原样  ", self.handover(b)["items"][0]["content"])
                    self.complete(b, "B body", "chat-B")
                    same = self.prepare("B")
                    self.assertIsNone(self.handover(same))
                    self.app.finish_turn(same, keep_for_tools=False)

    def test_aux_while_a_running_does_not_supersede_capture_ticket(self):
        a = self.prepare()
        self.app.auxiliary_completion(self.aux_payload())
        self.assertIs(a.session, self.app._current_session)
        self.complete(a)
        self.assertEqual("纸船42", self.handover(self.prepare("B"))["items"][0]["content"])

    def test_aux_slow_network_does_not_hold_chat_lock(self):
        reached, release = threading.Event(), threading.Event()
        errors = []
        def respond(request):
            reached.set()
            if not release.wait(4):
                raise RuntimeError("test deadlock")
            return httpx.Response(200, json=body("title"))
        self.mock_response(callback=respond)
        def run():
            try:
                self.app.auxiliary_completion(self.aux_payload())
            except Exception as exc:
                errors.append(type(exc).__name__)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(reached.wait(2))
            self.complete(self.prepare())
            self.assertIsNotNone(self.handover(self.prepare("B")))
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)

    def test_aux_does_not_interrupt_tool_wait_or_consume_handover(self):
        self.complete(self.prepare())
        tools = legacy.GatewayTests.bound_tools()
        b = self.prepare("B", tools=tools)
        call = legacy.GatewayTests.native_call("one-tool", "synthetic no execution")
        bindings = self.app.bind_response_tool_calls(b, [call])
        self.app.finish_turn(b, keep_for_tools=True, tool_call_ids=[call.tool_call_id],
                             tool_call_bindings=bindings)
        frames = dict(self.app._short_term_frames)
        calls = list(self.control.calls)
        self.app.auxiliary_completion(self.aux_payload())
        self.assertIs(b.session, self.app._current_session)
        self.assertEqual(calls, self.control.calls)
        self.assertEqual(frames, self.app._short_term_frames)
        messages = [{"role": "user", "content": "synthetic conversation"},
                    {"role": "assistant", "content": None,
                     "tool_calls": [legacy.GatewayTests.wire_call(call)]},
                    {"role": "tool", "tool_call_id": call.tool_call_id, "content": '{"exitCode":0}'}]
        follow = self.app.prepare_turn(legacy.GatewayTests.request(messages, tools=tools), {"X-Session-ID": "B"})
        self.assertEqual("纸船42", self.handover(follow)["items"][0]["content"])
        self.complete(follow, "tool chain final", "chain-final")

    def test_aux_rejects_all_tool_routes_without_touching_chat(self):
        a = self.prepare()
        for extra in ({"tools": legacy.GatewayTests.bound_tools()}, {"functions": [{}]},
                      {"tool_choice": "auto"}, {"function_call": "auto"},
                      {"messages": [{"role": "tool", "content": "result"}]},
                      {"messages": [{"role": "assistant", "tool_calls": [{}]}]}):
            with self.subTest(extra=extra), self.assertRaises(GatewayError):
                self.app.auxiliary_completion(self.aux_payload(**extra))
        self.assertEqual([], self.requests)
        self.assertIs(a.session, self.app._current_session)

    def test_aux_failure_invalid_tools_secrets_partial_output_do_not_change_chat(self):
        self.complete(self.prepare())
        before = self.app.short_term.stats()
        cases = [(500, b'upstream private failure', False),
                 (200, b'not json', False), (200, b'[]', False),
                 (200, json.dumps(body(finish="tool_calls")).encode(), False),
                 (200, json.dumps(body(self.app.config.gateway_token)).encode(), False),
                 (200, json.dumps(body(finish="length")).encode(), False),
                 (200, b'data: [DONE]\n\n', True),
                 (200, sse("title").replace(b'data: [DONE]\n\n', b''), True),
                 (200, sse(self.app.config.upstream_api_key), True),
                 (200, sse("title") + sse("again"), True),
                 (200, json.dumps(body()["choices"]).encode(), False)]
        for status, raw, stream in cases:
            with self.subTest(status=status, stream=stream):
                self.mock_response(raw, status)
                h = self.handler(self.aux_payload(stream=stream))
                h.do_POST()
                self.assertEqual([502], h.statuses)
                self.assertNotIn(b'upstream private failure', h.wfile.getvalue())
                self.assertEqual(before, self.app.short_term.stats())
        self.assertIsNotNone(self.handover(self.prepare("B")))

    def test_aux_client_disconnect_and_network_error_do_not_close_current_chat(self):
        a = self.prepare()
        h = self.handler(output=BrokenWrite())
        h.do_POST()
        self.assertTrue(h.close_connection)
        self.assertIs(a.session, self.app._current_session)
        def fail(request):
            raise httpx.ReadTimeout("synthetic")
        self.mock_response(callback=fail)
        h = self.handler()
        h.do_POST()
        self.assertEqual([502], h.statuses)
        self.assertIs(a.session, self.app._current_session)

    def test_auth_required_for_aux_and_diagnostics(self):
        for path in ("/v1/chat/completions", "/v1/st/short-term/status"):
            h = self.handler(headers={"Authorization": "Bearer wrong"}, path=path)
            (h.do_GET if path.endswith("status") else h.do_POST)()
            self.assertEqual([401], h.statuses)
        self.assertEqual([], self.requests)
        self.assertEqual([], self.control.calls)

    def test_status_is_content_free_and_capture_counters_are_actual(self):
        self.complete(self.prepare(), "PRIVATE_SENTINEL")
        self.app.auxiliary_completion(self.aux_payload())
        self.prepare("B")
        h = self.handler(path="/v1/st/short-term/status")
        h.do_GET()
        status = json.loads(h.wfile.getvalue())
        self.assertEqual(1, status["events"]["capture_stored"])
        self.assertEqual(1, status["events"]["handover_created"])
        self.assertEqual(1, status["events"]["auxiliary_validated"])
        self.assertNotIn("PRIVATE_SENTINEL", h.wfile.getvalue().decode())
        self.assertNotIn(self.app.config.host_token, h.wfile.getvalue().decode())

    def test_aux_remains_stateless_when_short_term_switch_is_off(self):
        self.app.config = replace(self.app.config, short_term_enabled=False)
        h = self.handler()
        h.do_POST()
        self.assertEqual([200], h.statuses)
        self.assertEqual([], self.control.calls)

    def test_plain_unknown_source_still_breaks_lineage_not_misclassified_as_aux(self):
        self.complete(self.prepare())
        self.complete(self.prepare(None), "unknown", "unknown-id")
        self.assertIsNone(self.handover(self.prepare("B")))
        self.assertEqual(1, self.app.short_term_status()["events"]["discard_unknown_source"])

    def test_aux_size_limit_does_not_touch_cache(self):
        self.complete(self.prepare())
        self.app.config = replace(self.app.config, max_body_bytes=1024)
        self.mock_response(b"x" * 1025)
        with self.assertRaisesRegex(GatewayError, "auxiliary_response_too_large"):
            self.app.auxiliary_completion(self.aux_payload())
        self.assertIsNotNone(self.handover(self.prepare("B")))

    def test_aux_never_extends_thirty_minute_ttl(self):
        now = [0.0]
        self.app.short_term._clock = lambda: now[0]
        self.complete(self.prepare())
        now[0] = 1799.0
        self.app.auxiliary_completion(self.aux_payload())
        self.assertEqual(1, self.app.short_term.stats().message_count)
        now[0] = 1800.0
        self.assertIsNone(self.handover(self.prepare("B")))


if __name__ == "__main__":
    unittest.main()
