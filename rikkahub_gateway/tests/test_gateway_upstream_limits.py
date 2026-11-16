"""Provider-owned content size, without disabling auth or execution validation."""
import io
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
import httpx
from rikkahub_gateway import server
from rikkahub_gateway.tests import test_gateway as fixtures
from rikkahub_gateway.tests import test_gateway_tool_tail_limits as tails

class UpstreamLimitsTests(unittest.TestCase):
    def test_default_and_explicit_legacy_policy(self):
        values = dict(vars(fixtures.config()))
        values.pop("upstream_limits_only")
        self.assertTrue(server.GatewayConfig(**values).upstream_limits_only)
        self.assertFalse(server.GatewayConfig(**values, upstream_limits_only=False).upstream_limits_only)

    def test_large_input_and_nonstream_output_are_complete_and_tokens_unchanged(self):
        cfg = replace(fixtures.config(), upstream_limits_only=True,
            max_request_body_bytes=1024, max_body_bytes=1024)
        answer = "完整回答" * 1024
        incoming = "完整附件" * 1024
        seen = []
        def respond(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"choices":[{"index":0,"message":{
                "role":"assistant","content":answer},"finish_reason":"stop"}]})
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            app = server.GatewayApplication(cfg, control=fixtures.FakeControl(), upstream=client)
            turn = app.prepare_turn({"model":cfg.public_model,"messages":[{"role":"user","content":incoming}],
                "max_tokens":123456}, {"Authorization":"Bearer " + cfg.gateway_token})
            handler = object.__new__(server._GatewayHandler)
            handler.server = SimpleNamespace(application=app)
            handler.wfile = io.BytesIO()
            handler.send_response = lambda status: self.assertEqual(200, status)
            handler.send_header = lambda *args: None
            handler.end_headers = lambda: None
            handler._proxy_json(turn)
            self.assertEqual(answer, json.loads(handler.wfile.getvalue())["choices"][0]["message"]["content"])
            self.assertEqual(incoming, seen[0]["messages"][-1]["content"])
            self.assertEqual(123456, seen[0]["max_tokens"])
            self.assertEqual(1, len(seen))

    def test_large_tool_arguments_cross_legacy_caps_and_bind_once(self):
        helper = tails.ToolTailLimitTests()
        self.addCleanup(helper.doCleanups)
        arguments = tails.html_arguments(8192)
        cfg = replace(fixtures.config(), upstream_limits_only=True, max_body_bytes=1024,
            max_stream_bytes=1024, max_tool_tail_bytes=1024)
        failure, writer, _ = helper.run_stream(tails.SyntheticStream(
            lambda: tails.tool_events(arguments, 200)), cfg=cfg)
        self.assertIsNone(failure)
        self.assertEqual(arguments, "".join(writer.arguments))
        self.assertEqual(1, writer.done)
