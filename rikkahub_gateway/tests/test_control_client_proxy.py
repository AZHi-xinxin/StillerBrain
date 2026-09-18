from __future__ import annotations

import json
import os
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import call, patch, sentinel

import httpx

from rikkahub_gateway.server import ControlClient, GatewayApplication
from rikkahub_gateway.tests.test_gateway import config


@contextmanager
def local_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def proxy_environment(url):
    # Set both cases so machine-specific NO_PROXY cannot hide a regression.
    values = {name: url for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    values["NO_PROXY"] = ""
    values.update({name.lower(): value for name, value in list(values.items())})
    return values


class ControlClientProxyTests(unittest.TestCase):
    def test_authenticated_control_request_never_reaches_environment_proxy(self):
        control_requests = []
        proxy_requests = []

        class ControlHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                control_requests.append((self.path, self.headers.get("Authorization"), json.loads(body)))
                payload = b'{"decision":"synthetic-ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        class ProxyTrap(BaseHTTPRequestHandler):
            def do_POST(self):
                proxy_requests.append(self.path)
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        with local_server(ControlHandler) as control_url, local_server(ProxyTrap) as proxy_url:
            with patch.dict(os.environ, proxy_environment(proxy_url)):
                control = ControlClient(control_url + "/", "synthetic-control-token", timeout=2.0)
                try:
                    self.assertEqual(control.post("/probe", {"test": True}), {"decision": "synthetic-ok"})
                finally:
                    control.client.close()

        self.assertEqual(control_requests, [("/probe", "Bearer synthetic-control-token", {"test": True})])
        self.assertEqual(proxy_requests, [])

    def test_invalid_environment_proxy_cannot_break_control_client_initialization(self):
        with patch.dict(os.environ, proxy_environment("not-a-proxy://invalid")):
            control = ControlClient("http://127.0.0.1", "synthetic-token", timeout=2.0)
            try:
                self.assertEqual(control.client.timeout.connect, 2.0)
            finally:
                control.client.close()

    def test_explicit_client_transport_and_timeout_are_preserved(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"decision": "injected-client"})

        with httpx.Client(transport=httpx.MockTransport(respond), timeout=7.0, trust_env=False) as injected:
            with patch("rikkahub_gateway.server.httpx.Client", side_effect=AssertionError("must not construct")):
                control = ControlClient("http://control.test", "synthetic-token", client=injected, timeout=2.0)
                self.assertIs(control.client, injected)
                self.assertEqual(control.post("/probe", {"test": True}), {"decision": "injected-client"})
            self.assertEqual(injected.timeout.connect, 7.0)
            self.assertFalse(injected.is_closed)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["Authorization"], "Bearer synthetic-token")

    def test_host_and_human_control_ignore_env_but_upstream_policy_is_unchanged(self):
        settings = replace(config(), human_token="synthetic-human-token-at-least-32-characters", timeout_seconds=120.0)
        with patch("rikkahub_gateway.server.httpx.Client", side_effect=[sentinel.host, sentinel.human, sentinel.upstream]) as factory:
            app = GatewayApplication(settings)
        self.assertIs(app.control.client, sentinel.host)
        self.assertIs(app.human_control.client, sentinel.human)
        self.assertIs(app.upstream, sentinel.upstream)
        self.assertEqual(factory.call_args_list, [
            call(timeout=30.0, trust_env=False),
            call(timeout=30.0, trust_env=False),
            call(timeout=120.0),
        ])


if __name__ == "__main__":
    unittest.main()
