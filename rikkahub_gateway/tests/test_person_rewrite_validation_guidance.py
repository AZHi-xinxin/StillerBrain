"""Synthetic authoring diagnostics and loopback/mock-upstream transport only."""
from __future__ import annotations

import copy
import json
import unittest

from mcp_server.public_contract import PUBLIC_REWRITE_PREVIEW_INPUT_SCHEMA, PUBLIC_REWRITE_CONFIRM_INPUT_SCHEMA
from rikkahub_gateway.server import _tool_validation_message, _safe_validation_diagnostic
from rikkahub_gateway.tool_execution import ToolExecutionBoundaryError, canonical_hash
from rikkahub_gateway.tests import test_gateway_failure_observability as transport
from tests import test_tool_validation_diagnostics as binding_fixture


PREVIEW = "mcp__StillerBrian__preview_person_reference_rewrite"
CONFIRM = "mcp__StillerBrian__confirm_person_reference_rewrite"
PRIVATE_KEY = "private arbitrary memory key"
PRIVATE_VALUE = "private synthetic memory contents"


def schema_for(container):
    original = (PUBLIC_REWRITE_PREVIEW_INPUT_SCHEMA if container == "draft_fields"
                else PUBLIC_REWRITE_CONFIRM_INPUT_SCHEMA)
    return {"type": "object", "required": [container], "additionalProperties": False,
            "properties": {container: copy.deepcopy(original["properties"][container])}}


class PersonRewriteValidationGuidanceTests(unittest.TestCase):
    setUp = binding_fixture.ToolValidationDiagnosticTests.setUp
    bind = binding_fixture.ToolValidationDiagnosticTests.bind

    def rejection(self, tool, container, values):
        with self.assertRaises(ToolExecutionBoundaryError) as caught:
            self.bind(schema_for(container), {container: values}, name=tool)
        return caught.exception.validation_diagnostic

    def test_bare_preview_key_has_safe_container_and_fixed_correct_example(self):
        diagnostic = self.rejection(PREVIEW, "draft_fields", {"original_text": PRIVATE_VALUE})
        self.assertEqual(["draft_fields", "<field>"], diagnostic["field_path"])
        message = _tool_validation_message(diagnostic)
        self.assertIn('"/original_text":"待写正文"', message)
        self.assertIn("/current_understanding", message)
        self.assertNotIn(PRIVATE_VALUE, message + json.dumps(diagnostic))

    def test_confirm_key_has_receipt_preserving_hint(self):
        diagnostic = self.rejection(CONFIRM, "final_fields", {PRIVATE_KEY: PRIVATE_VALUE})
        self.assertEqual(["final_fields", "<field>"], diagnostic["field_path"])
        message = _tool_validation_message(diagnostic)
        self.assertIn("suggested_fields", message)
        self.assertIn("final_fields_hash", message)
        self.assertNotIn(PRIVATE_KEY, message + json.dumps(diagnostic))
        self.assertNotIn(PRIVATE_VALUE, message + json.dumps(diagnostic))

    def test_public_slash_path_type_error_is_visible_without_literal_value(self):
        diagnostic = self.rejection(PREVIEW, "draft_fields", {"/summary": [PRIVATE_VALUE]})
        self.assertEqual(["draft_fields", "/summary"], diagnostic["field_path"])
        self.assertNotIn("字段片段示例", _tool_validation_message(diagnostic))
        self.assertNotIn(PRIVATE_VALUE, json.dumps(diagnostic))

    def test_arbitrary_schema_property_stays_hidden_and_cannot_trigger_hint(self):
        schema = schema_for("draft_fields")
        schema["properties"]["draft_fields"]["properties"][PRIVATE_KEY] = {"type": "integer"}
        with self.assertRaises(ToolExecutionBoundaryError) as caught:
            self.bind(schema, {"draft_fields": {PRIVATE_KEY: PRIVATE_VALUE}}, name=PREVIEW)
        diagnostic = caught.exception.validation_diagnostic
        self.assertEqual(["draft_fields", "<field>"], diagnostic["field_path"])
        self.assertNotIn("字段片段示例", _tool_validation_message(diagnostic))
        self.assertNotIn(PRIVATE_KEY, json.dumps(diagnostic))

    def test_other_tool_and_untrusted_catalog_names_do_not_get_authoring_hints(self):
        diagnostic = self.rejection("other_tool", "draft_fields", {PRIVATE_KEY: PRIVATE_VALUE})
        self.assertNotIn("字段片段示例", _tool_validation_message(diagnostic))
        self.assertIsNone(_safe_validation_diagnostic(diagnostic, allowed_tool_names=[PREVIEW]))

    def test_protected_value_collision_omits_new_hint(self):
        diagnostic = self.rejection(PREVIEW, "draft_fields", {PRIVATE_KEY: PRIVATE_VALUE})
        message = _tool_validation_message(diagnostic, protected_values=("待写正文",))
        self.assertNotIn("待写正文", message)
        self.assertNotIn("字段片段示例", message)
        self.assertIn("尚未交给客户端执行", message)

    def test_valid_slash_arguments_are_bound_without_rewriting(self):
        values = {"draft_fields": {"/original_text": PRIVATE_VALUE, "/summary": "synthetic"}}
        before = copy.deepcopy(values)
        binding = self.bind(schema_for("draft_fields"), json.dumps(values, indent=2), name=PREVIEW)
        self.assertEqual(before, values)
        self.assertEqual(canonical_hash(before), binding.arguments_hash)


class PersonRewriteTransportGuidanceTests(unittest.TestCase):
    setUp = transport.GatewayFailureObservabilityTests.setUp
    tearDown = transport.GatewayFailureObservabilityTests.tearDown
    use_upstream = transport.GatewayFailureObservabilityTests.use_upstream
    post = transport.GatewayFailureObservabilityTests.post
    events = staticmethod(transport.GatewayFailureObservabilityTests.events)

    def run_failure(self, *, stream, prefix=False, confirm=False):
        container, name = ("final_fields", CONFIRM) if confirm else ("draft_fields", PREVIEW)
        call = transport.native_call(name=name, values={container: {PRIVATE_KEY: PRIVATE_VALUE}})
        response = transport.sse([call], prefix=prefix) if stream else {
            "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]},
                         "finish_reason": "tool_calls"}]}
        self.use_upstream(response, stream=stream)
        tools = [{"type": "function", "function": {"name": name, "parameters": schema_for(container)}}]
        with self.assertLogs("stiller.rikkahub.gateway", level="WARNING") as logs:
            status, raw = self.post(tools=tools, stream=stream)
        error = self.events(raw)[-2]["error"] if stream and prefix else json.loads(raw)["error"]
        self.assertEqual(200 if prefix else 502, status)
        self.assertEqual([container, "<field>"], error["validation"]["field_path"])
        self.assertIn("suggested_fields" if confirm else "字段片段示例", error["message"])
        joined = raw.decode("utf-8") + "".join(record.getMessage() for record in logs.records)
        for secret in (PRIVATE_KEY, PRIVATE_VALUE, self.app.config.gateway_token, self.app.config.upstream_api_key):
            self.assertNotIn(secret, joined)
        self.assertNotIn(b'"tool_calls"', raw)
        self.assertEqual([], self.policy.authorizations)
        self.assertIsNone(self.app._current_session)
        self.assertEqual([], self.unhandled)

    def test_json_preview_error(self):
        self.run_failure(stream=False)

    def test_sse_before_prefix_preview_error(self):
        self.run_failure(stream=True)

    def test_sse_after_prefix_preview_error(self):
        self.run_failure(stream=True, prefix=True)

    def test_sse_after_prefix_confirm_error(self):
        self.run_failure(stream=True, prefix=True, confirm=True)


if __name__ == "__main__":
    unittest.main()
