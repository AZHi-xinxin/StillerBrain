"""Loopback HTTP proof: DONE delivers tools while upstream HTTP EOF is withheld."""
from __future__ import annotations

import http.client
import json
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx

from rikkahub_gateway.server import GatewayApplication, _GatewayHandler
from rikkahub_gateway.tests import test_gateway as fixtures
from rikkahub_gateway.tests.test_gateway_stream_completion import event


class DoneHttpTests(unittest.TestCase):
    def test_tool_turn_and_continuation_complete_before_upstream_eof(self):
        for protected in (False, True):
            with self.subTest(protected=protected):
                self.run_exchange(protected)

    def run_exchange(self, protected):
        gate = threading.Event()
        eof_sent = threading.Event()
        call = fixtures.GatewayTests.native_call("wire-original", "synthetic-only")
        wire = fixtures.GatewayTests.wire_call(call)
        delta = {**wire, "index": 0}
        tool_body = event({"tool_calls": [delta]}, "tool_calls") + b"data: [DONE]\n\n"

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                continuing = request["messages"][-1]["role"] == "tool"
                body = event({"content":"synthetic final"}, "stop") + b"data: [DONE]\n\n" if continuing else tool_body
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                self.wfile.write(f"{len(body):X}\r\n".encode() + body + b"\r\n")
                self.wfile.flush()
                if not continuing:
                    gate.wait(timeout=5)
                    eof_sent.set()
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        upstream.daemon_threads = True
        upstream_worker = threading.Thread(target=lambda: upstream.serve_forever(poll_interval=0.02), daemon=True)
        upstream_worker.start()
        config = replace(fixtures.config(), upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1")
        client = httpx.Client(trust_env=False)
        app = GatewayApplication(config, control=fixtures.FakeControl(), upstream=client)
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        gateway.daemon_threads = True
        gateway.application = app
        worker = threading.Thread(target=lambda: gateway.serve_forever(poll_interval=0.02), daemon=True)
        worker.start()
        user = {"role":"user", "content":"capability-1" if protected else "synthetic human"}
        tools = fixtures.GatewayTests.bound_tools()

        def post(messages):
            connection = http.client.HTTPConnection("127.0.0.1", gateway.server_port, timeout=2)
            try:
                connection.request("POST", "/v1/chat/completions",
                    json.dumps(fixtures.GatewayTests.request(messages, tools=tools, stream=True)),
                    {"Authorization":"Bearer " + config.gateway_token, "Content-Type":"application/json",
                     "X-ST-Thread-ID":"synthetic-http-done"})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        try:
            with patch.object(app, "_schedule_tool_result_wait"):
                status, body = post([user])
                self.assertEqual(200, status)
                self.assertIn(b"wire-original", body)
                self.assertEqual(1, body.count(b"data: [DONE]"))
                self.assertNotIn(b'"error"', body)
                self.assertFalse(eof_sent.is_set(), "tool response waited for upstream HTTP EOF")
                history = [user, {"role":"assistant", "tool_calls":[wire]},
                           {"role":"tool", "tool_call_id":"wire-original", "content":'{"ok":true}'}]
                status, body = post(history)
                self.assertEqual(200, status)
                self.assertIn(b"synthetic final", body)
                self.assertNotIn(b'"error"', body)
                self.assertIsNone(app._current_session)
        finally:
            gate.set()
            gateway.shutdown()
            gateway.server_close()
            upstream.shutdown()
            upstream.server_close()
            worker.join(timeout=3)
            upstream_worker.join(timeout=3)
            client.close()


if __name__ == "__main__":
    unittest.main()
