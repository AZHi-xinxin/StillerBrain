"""Real gateway processes with temporary configuration and no model requests."""
import json
import subprocess
import sys
import time
import unittest
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from packaging_tests import test_operations as fixtures

ops = fixtures.ops


class GatewayEntrypointsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OperationsTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = self.fixture.config
        self.opener = build_opener(ProxyHandler({}))

    def check_entrypoint(self, module):
        child = subprocess.Popen(
            [sys.executable, "-B", "-m", module], cwd=ops.ROOT,
            env=ops.child_environment(self.config), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        base = "http://127.0.0.1:" + self.config["STBRAIN_GATEWAY_PORT"]
        try:
            deadline = time.monotonic() + 15
            while True:
                self.assertIsNone(child.poll(), "Gateway exited before readiness")
                try:
                    with self.opener.open(base + "/health", timeout=1) as response:
                        health = json.load(response)
                    break
                except (URLError, TimeoutError, ConnectionError):
                    if time.monotonic() >= deadline:
                        self.fail("Gateway readiness timed out")
                    time.sleep(0.05)
            self.assertTrue(health["ok"])
            self.assertEqual("stillerbrain-gateway", health["service"])
            self.assertEqual("StillerBrain 网关", health["display_name"])
            request = Request(base + "/v1/models", headers={
                "Authorization": "Bearer " + self.config["STBRAIN_GATEWAY_TOKEN"],
            })
            with self.opener.open(request, timeout=2) as response:
                models = json.load(response)
            self.assertIn(self.config["STBRAIN_GATEWAY_MODEL"],
                          [model["id"] for model in models["data"]])
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    def test_official_starter_entrypoint(self):
        self.check_entrypoint(ops.starter.COMPONENTS["gateway"])

    def test_package_entrypoint(self):
        self.check_entrypoint("stillerbrain_gateway")

    def test_compatible_entrypoint(self):
        self.check_entrypoint("rikkahub_gateway.server")


if __name__ == "__main__":
    unittest.main()
