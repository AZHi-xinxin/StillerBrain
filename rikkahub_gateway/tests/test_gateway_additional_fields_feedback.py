"""Extra-field evidence reaches JSON/SSE and safe logs; mock upstream only."""
from __future__ import annotations
import copy
import json
import unittest
from unittest.mock import patch

from rikkahub_gateway.tests import test_person_rewrite_validation_guidance as fixtures
from rikkahub_gateway.tests import test_gateway_failure_observability as transport
from rikkahub_gateway.tool_execution import ToolExecutionBoundaryError

NAME = fixtures.PREVIEW
PRIVATE_KEY = "PrivateExtraKeyNeverReport"
PRIVATE_VALUE = "PrivateMemoryNeverReport"


class GatewayAdditionalFieldsFeedbackTests(unittest.TestCase):
    setUp = transport.GatewayFailureObservabilityTests.setUp
    tearDown = transport.GatewayFailureObservabilityTests.tearDown
    use_upstream = transport.GatewayFailureObservabilityTests.use_upstream
    post = transport.GatewayFailureObservabilityTests.post
    events = staticmethod(transport.GatewayFailureObservabilityTests.events)

    def failure(self, *, stream, prefix=False, missing_slash_in_schema=False):
        selected = fixtures.schema_for("draft_fields")
        if missing_slash_in_schema:
            selected["properties"]["draft_fields"]["properties"] = {"/title": {"type": "string"}}
            fields = {"/summary": PRIVATE_VALUE}
        else:
            fields = {"summary": PRIVATE_VALUE, PRIVATE_KEY: PRIVATE_VALUE}
        call = transport.native_call(name=NAME, values={"draft_fields": fields})
        response = transport.sse([call], prefix=prefix) if stream else {
            "choices": [{"message": {"role": "assistant", "content": "Synthetic prefix", "tool_calls": [call]}, "finish_reason": "tool_calls"}]}
        self.use_upstream(response, stream=stream)
        tools = [{"type": "function", "function": {"name": NAME, "parameters": selected}}]
        with self.assertLogs("stiller.rikkahub.gateway", level="WARNING") as logs:
            status, raw = self.post(tools=tools, stream=stream)
        error = self.events(raw)[-2]["error"] if stream and prefix else json.loads(raw)["error"]
        self.assertEqual(200 if prefix else 502, status)
        record = json.loads(logs.records[0].getMessage())
        diagnostic = error["validation"]
        self.assertEqual(diagnostic, record["validation"])
        self.assertEqual("tool_arguments_schema_invalid", error["code"])
        self.assertEqual(["draft_fields", "<field>"], diagnostic["field_path"])
        if missing_slash_in_schema:
            self.assertEqual(["/summary"], diagnostic["unexpected_fields"])
            self.assertEqual(["/title"], diagnostic["allowed_fields"])
        else:
            self.assertEqual(["summary", "<field>"], diagnostic["unexpected_fields"])
            self.assertEqual((2, 1, 12), (diagnostic["unexpected_count"], diagnostic["unknown_count"], diagnostic["allowed_field_count"]))
        self.assertIn("未被当前字段表接受的键", error["message"])
        joined = raw.decode("utf-8") + logs.records[0].getMessage()
        for secret in (PRIVATE_KEY, PRIVATE_VALUE, self.app.config.gateway_token, self.app.config.upstream_api_key, "capability-1"):
            self.assertNotIn(secret, joined)
        self.assertNotIn(b'"tool_calls"', raw)
        self.assertEqual([], self.policy.authorizations)
        self.assertIsNone(self.app._current_session)

    def test_json_shows_safe_difference(self):
        self.failure(stream=False)

    def test_sse_before_prefix_shows_safe_difference(self):
        self.failure(stream=True)

    def test_sse_after_prefix_shows_safe_difference(self):
        self.failure(stream=True, prefix=True)

    def test_actual_missing_slash_schema_is_reported_without_guessing_expected_schema(self):
        self.failure(stream=True, prefix=True, missing_slash_in_schema=True)


if __name__ == "__main__":
    unittest.main()
