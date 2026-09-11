"""Opt-in real local services; synthetic databases, loopback only, no model generation."""
import contextlib
import io
import json
import os
import socket
import subprocess
import time
import unittest
from urllib.error import URLError
from urllib.request import Request, ProxyHandler, build_opener

from packaging_tests import test_operations as fixtures

ops = fixtures.ops


@unittest.skipUnless(os.environ.get("STBRAIN_RUN_LOCAL_SERVICE_SMOKE") == "1",
                     "Set STBRAIN_RUN_LOCAL_SERVICE_SMOKE=1 for isolated real-process smoke")
class LocalServicesSmokeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OperationsTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = self.fixture.config
        self.children = []
        self.opener = build_opener(ProxyHandler({}))
        self.upstream = socket.socket()
        self.upstream.bind(("127.0.0.1", 0))
        self.upstream.listen()
        self.upstream.setblocking(False)
        self.addCleanup(self.upstream.close)
        self.config["STBRAIN_UPSTREAM_BASE_URL"] = "https://127.0.0.1:" + str(self.upstream.getsockname()[1]) + "/v1"

    def launch(self, command, **kwargs):
        child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        self.children.append(child)
        return child

    def request(self, component, path, payload=None, token_key=None):
        url = "http://127.0.0.1:" + self.config["STBRAIN_" + component + "_PORT"] + path
        headers = {"Accept": "application/json, text/event-stream"}
        if token_key:
            headers["Authorization"] = "Bearer " + self.config[token_key]
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
        with self.opener.open(request, timeout=1) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def ready_and_readonly_contract(self):
        deadline = time.monotonic() + 30
        while True:
            if any(child.poll() is not None for child in self.children):
                self.fail("An isolated synthetic service exited before readiness")
            try:
                control = self.request("CONTROL", "/health")
                gateway = self.request("GATEWAY", "/health")
                initialized = self.request("MCP", "/mcp", {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "synthetic-operations-smoke", "version": "1"}}
                }, "STBRAIN_MCP_TOKEN")
                break
            except (URLError, TimeoutError, ConnectionError):
                if time.monotonic() >= deadline:
                    self.fail("Isolated synthetic services did not become healthy within 30 seconds")
                time.sleep(0.15)
        self.assertTrue(control["ok"])
        self.assertTrue(gateway["ok"])
        self.assertIn("result", initialized)
        self.request("MCP", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, "STBRAIN_MCP_TOKEN")
        tools = self.request("MCP", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, "STBRAIN_MCP_TOKEN")
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertEqual(43, len(names))
        self.assertIn("stbrain_health", names)
        self.assertIn("remember_memory", names)
        health = self.request("MCP", "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                       "params": {"name": "stbrain_health", "arguments": {}}}, "STBRAIN_MCP_TOKEN")
        self.assertFalse(health["result"].get("isError", False))

    def assert_stopped_and_no_upstream(self):
        self.assertEqual(3, len(self.children))
        self.assertTrue(all(child.poll() is not None for child in self.children))
        deadline = time.monotonic() + 5
        while True:
            try:
                ops.check_ports_stopped(self.config)
                break
            except ops.OperationError:
                if time.monotonic() >= deadline:
                    ports = {name: self.config["STBRAIN_" + name + "_PORT"] for name in ("CONTROL", "MCP", "GATEWAY")}
                    self.fail("Synthetic service ports remained occupied: " + str(ports))
                time.sleep(0.1)
        try:
            connection, _ = self.upstream.accept()
        except BlockingIOError:
            return
        else:
            connection.close()
            self.fail("Read-only smoke attempted an upstream connection")

    def test_real_children_health_tool_directory_and_interrupt_cleanup(self):
        def interrupt_after_health(_):
            self.ready_and_readonly_contract()
            # Exercise the same cleanup path used by terminal Ctrl+C; no global
            # OS keyboard event or process-group signal is sent by this test.
            raise KeyboardInterrupt()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, ops.start_stack(self.config, popen=self.launch, sleeper=interrupt_after_health))
        self.assert_stopped_and_no_upstream()

    def test_real_child_exit_stops_other_owned_children(self):
        checked = False
        def fail_one_child(_):
            nonlocal checked
            if not checked:
                self.ready_and_readonly_contract()
                checked = True
                self.children[1].terminate()
                self.children[1].wait(timeout=5)
        with self.assertRaisesRegex(ops.OperationError, "service process exited"), contextlib.redirect_stdout(io.StringIO()):
            ops.start_stack(self.config, popen=self.launch, sleeper=fail_one_child)
        self.assert_stopped_and_no_upstream()


if __name__ == "__main__":
    unittest.main()
