"""Real loopback HTTP cancellation; synthetic model/control, no private I/O."""
from dataclasses import replace
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from rikkahub_gateway.server import GatewayApplication, _GatewayHandler
from rikkahub_gateway.tests.test_gateway import FakeControl, config


class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.server.started.set()
        if self.server.before_headers:
            self.server.release.wait(8)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.server.before_prefix:
            self.server.release.wait(8)
        if self.server.invalid:
            self.wfile.write(b"data: {invalid-json}\n\n")
            self.wfile.flush()
            return
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"prefix"},"finish_reason":null}]}\n\n')
        self.wfile.flush()
        if self.server.block:
            self.server.release.wait(8)
        try:
            tail = (b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"synthetic-call","type":"function","function":{"name":"safe_read","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n'
                    if self.server.tool else
                    b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
            self.wfile.write(tail)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *_):
        pass


class _KeepAliveUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        with self.server.guard:
            self.server.ports.append(self.client_address[1])
            number = len(self.server.ports)
        body = (b'data: {"choices":[{"delta":{"content":"prefix"},"finish_reason":null}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                if number == 1 else b'{"safe":"second-response"}')
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if number == 2:
            self.server.second_started.set()
            self.server.release.wait(5)
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, *_):
        pass


class GatewayClientDisconnectTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        self.upstream.daemon_threads = True
        self.upstream.started = threading.Event()
        self.upstream.release = threading.Event()
        self.upstream.block = True
        self.upstream.tool = False
        self.upstream.before_prefix = False
        self.upstream.before_headers = False
        self.upstream.invalid = False
        self.upstream_worker = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_worker.start()
        self.app = GatewayApplication(replace(config(), upstream_base_url=f"http://127.0.0.1:{self.upstream.server_port}/v1", timeout_seconds=6), control=FakeControl())
        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        self.gateway.daemon_threads = True
        self.gateway.application = self.app
        self.gateway_worker = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.gateway_worker.start()
        self.thread = "synthetic-video-chat"
        self.nonce = "a" * 32
        self.headers = {"Authorization": "Bearer " + self.app.config.gateway_token,
                        "Content-Type": "application/json", "X-ST-Thread-ID": self.thread,
                        "X-ST-Request-ID": self.nonce}
        self.clients = []

    def tearDown(self):
        self.upstream.release.set()
        for client in self.clients:
            client.close()
        self.gateway.shutdown()
        self.gateway.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.gateway_worker.join(2)
        self.upstream_worker.join(2)
        self.app.upstream.close()
        self.app.close_short_term()

    def stream(self):
        client = HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=3)
        self.clients.append(client)
        payload = {"model": self.app.config.public_model, "stream": True,
                   "tools": [{"type": "function", "function": {"name": "safe_read", "parameters": {"type": "object", "properties": {}}}}],
                   "messages": [{"role": "user", "content": [
                       {"type": "text", "text": "synthetic periodic frame"},
                       {"type": "image_url", "image_url": {"url": "data:image/png;base64,c3ludGhldGlj"}}]}]}
        client.request("POST", "/v1/chat/completions", json.dumps(payload), self.headers)
        response = client.getresponse()
        self.assertEqual(200, response.status)
        self.assertIn(b"prefix", response.fp.readline())
        response.fp.readline()
        return client, response

    def status(self, *, nonce=None, thread=None):
        client = HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=3)
        self.clients.append(client)
        payload = {"model": self.app.config.public_model, "request_id": nonce or self.nonce}
        client.request("POST", "/v1/turns/status", json.dumps(payload),
                       {**self.headers, "X-ST-Thread-ID": thread or self.thread})
        response = client.getresponse()
        return response.status, json.loads(response.read())

    def thread_status(self, *, thread=None):
        client = HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=2)
        self.clients.append(client)
        client.request("POST", "/v1/turns/thread-status", json.dumps({
            "model": self.app.config.public_model, "challenge": "c" * 32}),
            {key: (thread or self.thread) if key == "X-ST-Thread-ID" else value
             for key, value in self.headers.items() if key != "X-ST-Request-ID"})
        response = client.getresponse()
        return response.status, json.loads(response.read())

    def test_first_status_after_real_disconnect_waits_for_real_handler_cleanup(self):
        client, response = self.stream()
        # Abort both directions exactly as a cancelled mobile HTTP call does.
        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        response.close()
        client.close()
        started = time.monotonic()
        status, record = self.status()
        self.assertEqual((200, "not_current"), (status, record["state"]))
        self.assertLess(time.monotonic() - started, 2)
        self.upstream.block = False
        self.headers["X-ST-Request-ID"] = "b" * 32
        _, next_response = self.stream()
        self.assertIn(b"done", next_response.read())

    def test_cancel_after_upstream_headers_before_first_client_body_is_released(self):
        self.upstream.before_prefix = True
        client = HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=3)
        self.clients.append(client)
        client.request("POST", "/v1/chat/completions", json.dumps({
            "model": self.app.config.public_model, "stream": True,
            "messages": [{"role": "user", "content": "synthetic frame"}]}), self.headers)
        self.assertTrue(self.upstream.started.wait(2))
        client.sock.shutdown(socket.SHUT_RDWR)
        client.close()
        started = time.monotonic()
        status, record = self.status()
        self.assertEqual((200, "not_current"), (status, record["state"]))
        self.assertLess(time.monotonic() - started, 2)

    def test_first_thread_probe_after_prefix_disconnect_is_real_idle(self):
        client, response = self.stream()
        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        response.close()
        client.close()
        started = time.monotonic()
        status, record = self.thread_status()
        self.assertEqual((200, "gateway_lane_idle"), (status, record["state"]))
        self.assertEqual("c" * 32, record["challenge"])
        self.assertLess(time.monotonic() - started, 1.5)

    def test_first_thread_probe_without_capability_after_cancel_is_real_idle(self):
        self.upstream.before_prefix = True
        client = HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=3)
        self.clients.append(client)
        client.request("POST", "/v1/chat/completions", json.dumps({
            "model": self.app.config.public_model, "stream": True,
            "messages": [{"role": "user", "content": "synthetic first frame"}]}), self.headers)
        self.assertTrue(self.upstream.started.wait(2))
        client.sock.shutdown(socket.SHUT_RDWR)
        client.close()
        started = time.monotonic()
        status, record = self.thread_status()
        self.assertEqual((200, "gateway_lane_idle"), (status, record["state"]))
        self.assertFalse(record["can_stop"])
        self.assertLess(time.monotonic() - started, 1.5)

    def test_cancel_before_upstream_headers_is_bounded_and_does_not_invent_idle(self):
        self.upstream.before_headers = True
        client = HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=3)
        self.clients.append(client)
        client.request("POST", "/v1/chat/completions", json.dumps({
            "model": self.app.config.public_model, "stream": True,
            "messages": [{"role": "user", "content": "synthetic first frame"}]}), self.headers)
        self.assertTrue(self.upstream.started.wait(2))
        watch = self.app._current_session.client_disconnect_watch
        client.sock.shutdown(socket.SHUT_RDWR)
        client.close()
        started = time.monotonic()
        status, record = self.status()
        self.assertEqual((200, "generating"), (status, record["state"]))
        self.assertFalse(record["can_stop"])
        self.assertLess(time.monotonic() - started, 1.5)
        # Only real handler completion, after upstream exits its header wait,
        # can make the registry idle. There is no synthetic retirement.
        self.upstream.release.set()
        self.assertTrue(watch.finished.wait(3))
        self.assertEqual("not_current", self.status()[1]["state"])

    def test_half_closed_live_client_with_early_error_has_one_response_header(self):
        self.upstream.invalid = True
        self.upstream.before_prefix = True
        client = socket.create_connection(("127.0.0.1", self.gateway.server_port), timeout=3)
        self.clients.append(client)
        body = json.dumps({"model": self.app.config.public_model, "stream": True,
                           "messages": [{"role": "user", "content": "synthetic"}]}).encode()
        headers = "\r\n".join(f"{key}: {value}" for key, value in self.headers.items())
        client.sendall(("POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n" + headers +
                        f"\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
        client.shutdown(socket.SHUT_WR)
        # Let an EOF probe interleave with first-body validation. The client is
        # still reading, so its write-half-close must not be called cancellation.
        timer = threading.Timer(0.1, self.upstream.release.set)
        timer.start()
        data = bytearray()
        while chunk := client.recv(8192):
            data.extend(chunk)
        timer.cancel()
        self.assertEqual(1, data.count(b"HTTP/1.1 "))
        self.assertIn(b"upstream_invalid_stream", data)
        if b"text/event-stream" in data:
            self.assertIn(b"data: [DONE]", data)

    def test_live_client_remains_generating_and_other_owner_cannot_cancel(self):
        _, response = self.stream()
        self.assertEqual("generating", self.status()[1]["state"])
        self.assertEqual("not_current", self.status(nonce="b" * 32)[1]["state"])
        self.assertEqual("not_current", self.status(thread="unrelated-chat")[1]["state"])
        self.assertEqual("generating", self.status()[1]["state"])
        self.upstream.release.set()
        self.assertIn(b"done", response.read())

    def test_write_half_close_is_not_cancellation_while_response_is_readable(self):
        _, response = self.stream()
        response.fp.raw._sock.shutdown(socket.SHUT_WR)
        status, record = self.status()
        self.assertEqual((200, "generating"), (status, record["state"]))
        self.assertFalse(record["can_stop"])
        self.upstream.release.set()
        self.assertIn(b"done", response.read())

    def test_delivered_tool_wait_survives_normal_stream_connection_close(self):
        self.upstream.tool = True
        self.upstream.block = False
        client, response = self.stream()
        self.assertIn(b"tool_calls", response.read())
        response.close()
        client.close()
        status, record = self.status()
        self.assertEqual((200, "waiting_tool_results"), (status, record["state"]))
        self.assertTrue(record["can_stop"])
        self.assertEqual({"synthetic-call"}, self.app._current_session.expected_tool_call_ids)
        self.app._cancel_recovery_timer(self.app._current_session)

    def test_old_watcher_never_closes_upstream_socket_reused_by_another_request(self):
        pooled = ThreadingHTTPServer(("127.0.0.1", 0), _KeepAliveUpstream)
        pooled.daemon_threads = True
        pooled.ports, pooled.guard = [], threading.Lock()
        pooled.second_started, pooled.release = threading.Event(), threading.Event()
        worker = threading.Thread(target=pooled.serve_forever, daemon=True)
        worker.start()
        self.app.upstream.close()
        self.app.upstream = httpx.Client(limits=httpx.Limits(max_connections=1), trust_env=False)
        route = replace(self.app.default_route, upstream_base_url=f"http://127.0.0.1:{pooled.server_port}/v1")
        self.app.default_route = route
        self.app.routes = {route.public_model: route}
        at_commit, release_commit = threading.Event(), threading.Event()
        original_commit = _GatewayHandler._commit_final_delivery
        result = []

        def hold_commit(handler, *args, **kwargs):
            at_commit.set()
            release_commit.wait(5)
            return original_commit(handler, *args, **kwargs)

        def second_request():
            try:
                result.append(self.app.upstream.post(route.completion_url, content=b"{}").json())
            except Exception as exc:
                result.append(type(exc).__name__)

        try:
            with patch.object(_GatewayHandler, "_commit_final_delivery", hold_commit):
                client, response = self.stream()
                self.assertTrue(at_commit.wait(2))
                watch = self.app._current_session.client_disconnect_watch
                second_worker = threading.Thread(target=second_request, daemon=True)
                second_worker.start()
                self.assertTrue(pooled.second_started.wait(2))
                self.assertEqual(pooled.ports[0], pooled.ports[1], "test must reuse the real TCP socket")
                response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
                response.close()
                client.close()
                self.assertTrue(watch.disconnected.wait(2))
                pooled.release.set()
                second_worker.join(2)
                self.assertEqual([{"safe": "second-response"}], result)
        finally:
            pooled.release.set()
            release_commit.set()
            pooled.shutdown()
            pooled.server_close()
            worker.join(2)


if __name__ == "__main__":
    unittest.main()
