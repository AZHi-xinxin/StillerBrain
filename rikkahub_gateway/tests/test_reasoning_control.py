"""Synthetic request/HTTP serialization only; no model, DB, env or private data."""
from __future__ import annotations

import copy
import io
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from rikkahub_gateway.reasoning_control import apply_upstream_reasoning_control
from rikkahub_gateway.routes import GatewayRoute
from rikkahub_gateway.server import GatewayApplication, _GatewayHandler
from rikkahub_gateway.tests import test_gateway as fixtures


class ReasoningControlTests(unittest.TestCase):
    def setUp(self):
        guard = patch("sqlite3.connect", side_effect=AssertionError("database prohibited"))
        guard.start()
        self.addCleanup(guard.stop)
        self.controls = []
        self.sent = []

    def app(self, base="https://api.deepseek.com/v1", **changes):
        control = fixtures.FakeControl()
        self.controls.append(control)

        def respond(request):
            payload = json.loads(request.content)
            self.sent.append((str(request.url), payload))
            message = {"role": "assistant", "content": "synthetic complete"}
            choice = {"index": 0, "message": message, "finish_reason": "stop"}
            if payload.get("stream"):
                event = {"id": "synthetic", "model": "provider-model", "choices": [
                    {"index": 0, "delta": {"content": "synthetic complete"}, "finish_reason": "stop"}]}
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                    content=("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode())
            return httpx.Response(200, json={"id": "synthetic", "model": "provider-model", "choices": [choice]})

        upstream = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)
        self.addCleanup(upstream.close)
        app = GatewayApplication(replace(fixtures.config(), upstream_base_url=base,
            upstream_model="deepseek-v4-flash", **changes), control=control, upstream=upstream)
        self.addCleanup(app.close_short_term)
        return app

    def post(self, app, *, model=None, **extra):
        payload = {"model": model or app.config.public_model,
            "messages": [{"role": "user", "content": "synthetic"}], **extra}
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=app)
        raw = json.dumps(payload).encode()
        handler.headers = {"Authorization": "Bearer " + app.config.gateway_token,
            "Content-Type": "application/json", "Content-Length": str(len(raw)),
            "X-ST-Thread-ID": "synthetic-reasoning-control"}
        handler.path = "/v1/chat/completions"
        handler.rfile = io.BytesIO(raw)
        handler.wfile = io.BytesIO()
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.do_POST()
        self.assertEqual([200], statuses)
        self.assertIn(b"synthetic complete", handler.wfile.getvalue())
        return self.sent[-1][1]

    def test_off_adapts_final_main_and_auxiliary_wire_payload_in_both_modes(self):
        for stream in (False, True):
            for auxiliary in (False, True):
                with self.subTest(stream=stream, auxiliary=auxiliary):
                    app = self.app(short_term_enabled=True)
                    model = app.auxiliary_model if auxiliary else app.config.public_model
                    sent = self.post(app, model=model, stream=stream, reasoning_effort="none")
                    self.assertEqual({"type": "disabled"}, sent["thinking"])
                    self.assertNotIn("reasoning_effort", sent)
                    self.assertEqual(stream, sent["stream"])
                    self.assertEqual("deepseek-v4-flash", sent["model"])
                    if auxiliary:
                        self.assertEqual([], self.controls[-1].calls)

    def test_auto_and_enabled_efforts_remain_distinct(self):
        for extra in ({}, {"reasoning_effort": "low"}, {"reasoning_effort": "high"},
                      {"reasoning_effort": "max"}, {"reasoning_effort": None}):
            with self.subTest(extra=extra):
                sent = self.post(self.app(), **extra)
                self.assertNotIn("thinking", sent)
                self.assertEqual(extra.get("reasoning_effort"), sent.get("reasoning_effort"))
                self.assertEqual("reasoning_effort" in extra, "reasoning_effort" in sent)

    def test_custom_native_switch_wins_and_keeps_its_other_fields(self):
        for native in ("enabled", "disabled"):
            with self.subTest(native=native):
                thinking = {"type": native, "synthetic_extension": "preserved"}
                sent = self.post(self.app(), reasoning_effort="none", thinking=thinking)
                self.assertEqual(thinking, sent["thinking"])
                self.assertNotIn("reasoning_effort", sent)

    def test_native_disabled_without_generic_off_is_unchanged(self):
        sent = self.post(self.app(), thinking={"type": "disabled"})
        self.assertEqual({"type": "disabled"}, sent["thinking"])
        self.assertNotIn("reasoning_effort", sent)

    def test_malformed_explicit_native_control_is_not_interpreted(self):
        route = GatewayRoute("synthetic", "https://api.deepseek.com/v1", "deepseek-v4-flash", "synthetic-key")
        for native in (None, False, "disabled", {}, {"type": "adaptive"}):
            with self.subTest(native=native):
                payload = {"thinking": native, "reasoning_effort": "none"}
                before = copy.deepcopy(payload)
                apply_upstream_reasoning_control(payload, route)
                self.assertEqual(before, payload)

    def test_model_name_or_lookalike_host_cannot_select_deepseek_dialect(self):
        for base in ("https://relay.invalid/v1", "https://api.openai.com/v1",
                     "https://api.deepseek.com.evil.invalid/v1", "https://deepseek.com/v1",
                     "http://api.deepseek.com/v1", "https://api.deepseek.com:8443/v1"):
            with self.subTest(base=base):
                sent = self.post(self.app(base), reasoning_effort="none")
                self.assertEqual("none", sent["reasoning_effort"])
                self.assertNotIn("thinking", sent)

    def test_selected_extra_route_not_default_route_controls_dialect(self):
        deepseek = GatewayRoute("chosen-alias", "https://api.deepseek.com/v1", "deepseek-v4-pro", "synthetic-key")
        app = self.app("https://relay.invalid/v1", extra_routes=(deepseek,))
        sent = self.post(app, model=deepseek.public_model, reasoning_effort="none")
        self.assertEqual({"type": "disabled"}, sent["thinking"])
        self.assertNotIn("reasoning_effort", sent)
        other = GatewayRoute("other-alias", "https://relay.invalid/v1", "deepseek-v4-pro", "synthetic-key")
        sent = self.post(self.app(extra_routes=(other,)), model=other.public_model, reasoning_effort="none")
        self.assertEqual("none", sent["reasoning_effort"])
        self.assertNotIn("thinking", sent)

    def test_continuation_preserves_tool_history_and_does_not_mutate_input(self):
        app = self.app()
        tools = fixtures.GatewayTests.bound_tools()
        user = {"role": "user", "content": "synthetic"}
        headers = {"X-ST-Thread-ID": "synthetic-reasoning-control"}
        first_input = {"model": app.config.public_model, "messages": [user],
            "tools": tools, "reasoning_effort": "none"}
        snapshot = copy.deepcopy(first_input)
        first = app.prepare_turn(first_input, headers)
        self.assertEqual(snapshot, first_input)
        self.assertEqual({"type": "disabled"}, first.payload["thinking"])
        call = fixtures.GatewayTests.native_call("synthetic-tool-call", "synthetic-not-executed")
        bindings = app.bind_response_tool_calls(first, [call])
        app.finish_turn(first, keep_for_tools=True, tool_call_ids=[call.tool_call_id], tool_call_bindings=bindings)
        history = [user, {"role": "assistant", "content": "synthetic visible text",
            "reasoning_content": "synthetic old reasoning", "tool_calls": [fixtures.GatewayTests.wire_call(call)]},
            {"role": "tool", "tool_call_id": call.tool_call_id, "content": '{"synthetic":true}'}]
        next_input = {**first_input, "messages": history, "tool_choice": "auto"}
        snapshot = copy.deepcopy(next_input)
        continued = app.prepare_turn(next_input, headers)
        wire = json.loads(app.encode_upstream_payload(continued.payload))
        self.assertEqual(snapshot, next_input)
        self.assertIs(first.session, continued.session)
        self.assertEqual(1, self.controls[-1].seq)
        self.assertEqual({"type": "disabled"}, wire["thinking"])
        self.assertNotIn("reasoning_effort", wire)
        self.assertEqual(history, wire["messages"][-len(history):])
        self.assertEqual(tools, wire["tools"])
        self.assertEqual("auto", wire["tool_choice"])


if __name__ == "__main__":
    unittest.main()
