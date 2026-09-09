"""Synthetic localhost HTTP JSON/SSE coverage of an interleaved tool batch.

No private config/database, real model, real MCP or actual tool execution.
"""
from __future__ import annotations

import copy
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import httpx

from rikkahub_gateway.server import GatewayApplication, _GatewayHandler
from rikkahub_gateway.tests import test_gateway as fixtures


class InterleavedHTTPWireTests(unittest.TestCase):
    def setUp(self):
        self.db_guard = patch("sqlite3.connect", side_effect=AssertionError("database prohibited"))
        self.db_guard.start()
        self.addCleanup(self.db_guard.stop)
        self.user = {"role": "user", "content": "synthetic mixed-tool user"}
        self.calls = [
            {"id": "wire-synthetic-shell", "type": "function", "function": {"name": "workspace_shell", "arguments": '{"command":"synthetic-no-execution"}'}},
            {"id": "wire-synthetic-open", "type": "function", "function": {"name": "stbrain_open", "arguments": "{}"}},
        ]
        self.tools = fixtures.GatewayTests.bound_tools() + [{"type": "function", "function": {"name": "stbrain_open", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]
        self.upstream_payloads = []
        self.control = fixtures.FakeControl()
        upstream = httpx.Client(transport=httpx.MockTransport(self.upstream), trust_env=False)
        self.app = GatewayApplication(fixtures.config(), control=self.control, upstream=upstream)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        self.server.application = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.upstream.close()

    def upstream(self, request):
        payload = json.loads(request.content)
        self.upstream_payloads.append(payload)
        first = len(self.upstream_payloads) == 1
        message = {"role": "assistant", "content": None, "tool_calls": copy.deepcopy(self.calls)} if first else {"role": "assistant", "content": "synthetic complete"}
        finish = "tool_calls" if first else "stop"
        if payload.get("stream"):
            delta = {key: value for key, value in message.items() if key != "role"}
            if first:
                delta["tool_calls"] = [{"index": index, **call} for index, call in enumerate(self.calls)]
            event = {"id": "synthetic-response", "object": "chat.completion.chunk", "model": "real-model", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            content = ("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode()
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=content)
        return httpx.Response(200, json={"id": "synthetic-response", "object": "chat.completion", "model": "real-model", "choices": [{"index": 0, "message": message, "finish_reason": finish}]})

    def post(self, messages, *, stream):
        request = urllib.request.Request(self.url, method="POST",
            data=json.dumps({"model": "stiller-rikka", "messages": messages, "tools": self.tools, "stream": stream}).encode(),
            headers={"Authorization": "Bearer " + fixtures.config().gateway_token, "Content-Type": "application/json", "X-ST-Thread-ID": "synthetic-wire-thread"})
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read()
        with response:
            return response.status, response.read()

    def interleaved(self):
        return [copy.deepcopy(self.user),
                {"role": "assistant", "content": "synthetic first segment", "reasoning_content": "synthetic reasoning one", "tool_calls": [copy.deepcopy(self.calls[0])]},
                {"role": "tool", "tool_call_id": self.calls[0]["id"], "content": '{"synthetic_shell":true}'},
                {"role": "assistant", "content": "synthetic second segment", "reasoning_content": "synthetic reasoning two", "tool_calls": [copy.deepcopy(self.calls[1])]},
                {"role": "tool", "tool_call_id": self.calls[1]["id"], "content": '{"synthetic_open":true}'}]

    def roundtrip(self, first_stream, continuation_stream):
        status, body = self.post([self.user], stream=first_stream)
        self.assertEqual(200, status)
        for call in self.calls:
            self.assertIn(call["id"].encode(), body)
        session = self.app._current_session
        self.assertEqual(2, len(session.expected_tool_calls))
        messages = self.interleaved()
        before = copy.deepcopy(messages)
        status, body = self.post(messages, stream=continuation_stream)
        self.assertEqual(200, status, body.decode())
        self.assertIn(b"synthetic complete", body)
        self.assertEqual(before, messages)
        self.assertEqual(before, self.upstream_payloads[1]["messages"][-len(before):])
        self.assertEqual(2, len(session.host_receipts))
        self.assertIsNone(self.app._current_session)
        self.assertEqual(1, self.control.seq)
        # A real next-user-shaped request is accepted after proper completion.
        status, _ = self.post([{"role": "user", "content": "synthetic next user"}], stream=False)
        self.assertEqual(200, status)
        self.assertEqual(2, self.control.seq)

    def test_json_then_json(self):
        self.roundtrip(False, False)

    def test_json_then_sse(self):
        self.roundtrip(False, True)

    def test_sse_then_json(self):
        self.roundtrip(True, False)

    def test_sse_then_sse(self):
        self.roundtrip(True, True)

    def test_partial_rejected_without_new_upstream_or_cancelled_wait(self):
        self.assertEqual(200, self.post([self.user], stream=True)[0])
        session = self.app._current_session
        expected = dict(session.expected_tool_calls)
        status, body = self.post(self.interleaved()[:3], stream=True)
        self.assertEqual(409, status)
        self.assertEqual("tool_continuation_lineage_mismatch", json.loads(body)["error"]["code"])
        self.assertEqual(1, len(self.upstream_payloads))
        self.assertEqual(expected, session.expected_tool_calls)
        self.assertIs(session, self.app._current_session)
        self.assertEqual([], session.host_receipts)
        self.assertEqual(409, self.post([{"role": "user", "content": "synthetic invalid replacement"}], stream=False)[0])
        # Only the complete, correct original result sequence can settle this batch.
        self.assertEqual(200, self.post(self.interleaved(), stream=False)[0])
        self.assertIsNone(self.app._current_session)


if __name__ == "__main__":
    unittest.main()
