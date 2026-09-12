"""Local HTTP + fake upstream/control coverage for safe validation feedback."""

from __future__ import annotations

import json
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from rikkahub_gateway.server import _RequestPerformance
from rikkahub_gateway.tests import test_gateway_failure_observability as helpers
from rikkahub_gateway.tool_execution import ToolExecutionBoundaryError


TOOL_NAME = helpers.TOOL_NAME
PRIVATE_SCHEMA = "PRIVATE_SCHEMA_DESCRIPTION_NEVER_REPORT"
PRIVATE_ARGUMENT = "PRIVATE_ARGUMENT_NEVER_REPORT"
PRIVATE_KEY = "SafeLookingPrivateSchemaProperty"


def feedback_schema(*, required_target: bool = False, private_property: bool = False) -> dict:
    field = PRIVATE_KEY if private_property else "summary"
    schema = {
        "type": "object", "description": PRIVATE_SCHEMA,
        "required": ["changes"], "additionalProperties": False,
        "properties": {
            "changes": {"type": "object", "additionalProperties": False,
                        "properties": {field: {"type": "string"}}},
            "target_ref": {"type": "string"},
        },
    }
    if required_target:
        schema["required"].append("target_ref")
    return schema


def invalid_values(*, private_property: bool = False) -> dict:
    field = PRIVATE_KEY if private_property else "summary"
    return {"changes": {field: {"PRIVATE_UNDECLARED_KEY": PRIVATE_ARGUMENT}}}


class GatewayValidationFeedbackTests(unittest.TestCase):
    # Reuse transport scaffolding, not the old class's tests/strict four-field
    # log assertions. No production server, real MCP, database or model is used.
    setUp = helpers.GatewayFailureObservabilityTests.setUp
    tearDown = helpers.GatewayFailureObservabilityTests.tearDown
    tools = staticmethod(helpers.GatewayFailureObservabilityTests.tools)
    events = staticmethod(helpers.GatewayFailureObservabilityTests.events)
    use_upstream = helpers.GatewayFailureObservabilityTests.use_upstream
    post = helpers.GatewayFailureObservabilityTests.post
    assert_failed_without_calls = helpers.GatewayFailureObservabilityTests.assert_failed_without_calls

    @contextmanager
    def capture_performance(self):
        # The JSON response can reach urllib just before the server's finally
        # emits performance. Wait for that fake request's terminal log rather
        # than relying on a scheduler race or arbitrary sleep.
        emitted = threading.Event()
        original = _RequestPerformance.emit

        def observe(performance):
            try:
                return original(performance)
            finally:
                emitted.set()

        with patch.object(_RequestPerformance, "emit", observe):
            with self.assertLogs("stiller.rikkahub.performance", level="INFO") as logs:
                yield logs
                self.assertTrue(emitted.wait(2), "synthetic request did not finish its performance record")

    def safe_failure(self, *, values=None, selected_schema=None, prefix=False, stream=True):
        values = invalid_values() if values is None else values
        selected_schema = feedback_schema() if selected_schema is None else selected_schema
        call = helpers.native_call(values=values)
        response = helpers.sse([call], prefix=prefix) if stream else {
            "choices": [{"message": {"role": "assistant", "content": "Submitting.", "tool_calls": [call]},
                         "finish_reason": "tool_calls"}],
        }
        self.use_upstream(response, stream=stream)
        with self.assertLogs("stiller.rikkahub.gateway", level="WARNING") as failures:
            with self.capture_performance() as performance:
                status, raw = self.post(tools=self.tools(selected_schema), stream=stream)
        error = self.events(raw)[-2]["error"] if stream and status == 200 else json.loads(raw)["error"]
        records = [json.loads(record.getMessage()) for record in failures.records]
        metrics = [json.loads(record.getMessage()) for record in performance.records]
        self.assertEqual(1, len(records))
        self.assertEqual(1, len(metrics))
        self.assertEqual("gateway_request_performance_failure", metrics[0]["event"])
        self.assertEqual("tool_validation", metrics[0]["stage"])
        self.assertEqual("tool_arguments_schema_invalid", error["code"])
        self.assertEqual(error["code"], records[0]["code"])
        self.assertIn(error["code"], error["message"])
        self.assertIn("尚未交给客户端执行", error["message"])
        self.assertNotIn(b'"tool_calls"', raw)
        self.assertNotIn(call["id"].encode(), raw)
        joined = raw.decode("utf-8") + json.dumps(records, ensure_ascii=False) + json.dumps(metrics, ensure_ascii=False)
        for marker in (
            PRIVATE_SCHEMA, PRIVATE_ARGUMENT, "PRIVATE_UNDECLARED_KEY", PRIVATE_KEY,
            helpers.PROMPT_MARKER, helpers.ARGUMENT_MARKER, "capability-1",
            self.app.config.gateway_token, self.app.config.upstream_api_key,
        ):
            self.assertNotIn(marker, joined)
        self.assertTrue(all(record.exc_info is None for record in failures.records))
        self.assertEqual([], self.policy.authorizations)
        self.assert_failed_without_calls()
        return status, raw, error, records[0], metrics[0]

    def test_stream_schema_error_before_and_after_prefix_has_same_safe_detail(self):
        for prefix in (False, True):
            with self.subTest(prefix=prefix):
                status, raw, error, record, metric = self.safe_failure(prefix=prefix)
                self.assertEqual(200 if prefix else 502, status)
                self.assertEqual({"event", "at", "code", "streamed_prefix", "validation"}, set(record))
                self.assertEqual(prefix, record["streamed_prefix"])
                diagnostic = error["validation"]
                self.assertEqual(diagnostic, record["validation"])
                self.assertEqual(TOOL_NAME, diagnostic["tool_name"])
                self.assertEqual("type", diagnostic["validator"])
                self.assertEqual(["changes", "summary"], diagnostic["field_path"])
                for label in (TOOL_NAME, "changes", "summary"):
                    self.assertIn(label, error["message"])
                self.assertEqual(["tool_calls"], metric["finish_reasons"])
                self.assertFalse(metric["usage_observed"], "finish reasons must be observed without usage metadata")
                if prefix:
                    events = self.events(raw)
                    self.assertEqual("I will submit it now.", events[0]["choices"][0]["delta"]["content"])
                    self.assertEqual("[DONE]", events[-1])
                    self.assertEqual(1, raw.count(b"data: [DONE]"))
                    self.assertNotIn("choices", events[-2])
                    self.assertNotIn(b'"finish_reason"', raw)

    def test_nonstream_schema_error_is_safe_502_with_feedback(self):
        status, _, error, record, metric = self.safe_failure(stream=False)
        self.assertEqual(502, status)
        self.assertEqual(error["validation"], record["validation"])
        self.assertEqual(["changes", "summary"], error["validation"]["field_path"])
        self.assertEqual(["tool_calls"], metric["finish_reasons"])
        self.assertFalse(record["streamed_prefix"])

    def test_missing_required_field_is_named_without_parameter_values(self):
        status, _, error, record, _ = self.safe_failure(
            values={"changes": {"summary": PRIVATE_ARGUMENT}},
            selected_schema=feedback_schema(required_target=True), prefix=True,
        )
        self.assertEqual(200, status)
        self.assertEqual("required", error["validation"]["validator"])
        self.assertEqual(["target_ref"], error["validation"]["required_fields"])
        self.assertIn("target_ref", error["message"])
        self.assertEqual(error["validation"], record["validation"])

    def test_private_schema_field_name_is_redacted_in_message_and_metadata(self):
        _, _, error, record, _ = self.safe_failure(
            values=invalid_values(private_property=True),
            selected_schema=feedback_schema(private_property=True), prefix=True,
        )
        self.assertEqual(["changes", "<field>"], error["validation"]["field_path"])
        self.assertIn("<field>", error["message"])
        self.assertEqual(error["validation"], record["validation"])

    def test_protected_label_collision_omits_whole_diagnostic_and_its_message_detail(self):
        original_post = self.control.post

        def synthetic_protected_field(path, payload):
            result = original_post(path, payload)
            if path == "/v1/host/wakes":
                result["wake_capability"] = "target_ref"
            return result

        self.control.post = synthetic_protected_field
        status, raw, error, record, _ = self.safe_failure(
            values={"changes": {"summary": PRIVATE_ARGUMENT}},
            selected_schema=feedback_schema(required_target=True), stream=False,
        )
        self.assertEqual(502, status)
        self.assertNotIn("validation", error)
        self.assertNotIn("validation", record)
        self.assertNotIn(TOOL_NAME, error["message"])
        self.assertNotIn("target_ref", raw.decode("utf-8") + json.dumps(record))
        self.assertEqual({"event", "at", "code", "streamed_prefix"}, set(record))

    def test_adapter_cannot_publish_diagnostic_tool_name_outside_current_catalog(self):
        class SpoofingPolicy(helpers.RecordingPolicy):
            def authorize(self, binding, *, catalog):
                raise ToolExecutionBoundaryError("tool_arguments_schema_invalid", validation_diagnostic={
                    "tool_name": "SafeLookingPrivateAdapterSecret",
                    "validator": "required", "field_path": ["summary"], "required_fields": ["target_ref"],
                })

        self.policy = SpoofingPolicy()
        self.app.execution_boundary.policy = self.policy
        _, raw, error, record, _ = self.safe_failure(
            values={"changes": {"summary": PRIVATE_ARGUMENT}}, prefix=True,
        )
        self.assertNotIn("validation", error)
        self.assertNotIn("validation", record)
        self.assertNotIn("SafeLookingPrivateAdapterSecret", raw.decode("utf-8") + json.dumps(record))
        self.assertNotIn(TOOL_NAME, error["message"])

    def test_mixed_batch_releases_no_call_and_never_retries_or_receipts(self):
        calls = [
            helpers.native_call(values={"changes": {"summary": PRIVATE_ARGUMENT}}, call_id="valid-unreleased-id"),
            helpers.native_call(values=invalid_values(), call_id="invalid-unreleased-id"),
        ]
        self.use_upstream(helpers.sse(calls, prefix=True))
        with self.assertLogs("stiller.rikkahub.gateway", level="WARNING") as logs:
            status, raw = self.post(tools=self.tools(feedback_schema()))
        self.assertEqual(200, status)
        self.assertEqual("tool_arguments_schema_invalid", self.events(raw)[-2]["error"]["code"])
        self.assertEqual("[DONE]", self.events(raw)[-1])
        for marker in (b'"tool_calls"', b"valid-unreleased-id", b"invalid-unreleased-id", PRIVATE_ARGUMENT.encode()):
            self.assertNotIn(marker, raw)
        self.assertEqual(1, len(self.policy.authorizations))
        self.assert_failed_without_calls()
        encoded = "".join(record.getMessage() for record in logs.records)
        self.assertNotIn(PRIVATE_ARGUMENT, encoded)
        self.assertNotIn(PRIVATE_SCHEMA, encoded)

    def test_finish_reason_projection_is_fixed_bounded_and_independent_of_usage(self):
        performance = _RequestPerformance()
        performance.note_usage({"choices": [
            {"finish_reason": "stop"}, {"finish_reason": "stop"},
            {"finish_reason": "length"}, {"finish_reason": "tool_calls"},
            {"finish_reason": "function_call"}, {"finish_reason": "content_filter"},
            {"finish_reason": "PRIVATE_PROVIDER_REASON"}, {"finish_reason": {"PRIVATE_KEY": "PRIVATE_VALUE"}},
            {"finish_reason": None},
        ]})
        self.assertEqual(["stop", "length", "tool_calls", "function_call", "content_filter", "other"], performance.finish_reasons)
        self.assertFalse(performance.usage_observed)
        with self.assertLogs("stiller.rikkahub.performance", level="INFO") as logs:
            performance.emit()
        self.assertEqual(1, len(logs.records))
        record = json.loads(logs.records[0].getMessage())
        self.assertEqual(performance.finish_reasons, record["finish_reasons"])
        self.assertNotIn("PRIVATE", logs.records[0].getMessage())

    def test_ordinary_stop_response_remains_unchanged_and_logged_as_stop(self):
        wire = b'data: {"choices":[{"index":0,"delta":{"content":"Safe ordinary answer."},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        self.use_upstream(wire)
        with self.assertNoLogs("stiller.rikkahub.gateway", level="WARNING"):
            with self.capture_performance() as logs:
                status, raw = self.post(tools=self.tools(feedback_schema()))
        self.assertEqual(200, status)
        self.assertEqual("Safe ordinary answer.", self.events(raw)[0]["choices"][0]["delta"]["content"])
        self.assertEqual("[DONE]", self.events(raw)[-1])
        self.assertEqual(["stop"], json.loads(logs.records[0].getMessage())["finish_reasons"])
        self.assertEqual([], self.policy.authorizations)
        self.assert_failed_without_calls()


if __name__ == "__main__":
    unittest.main()
