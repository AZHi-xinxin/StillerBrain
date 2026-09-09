from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from rikkahub_gateway.server import (
    GatewayApplication,
    GatewayError,
    _advertised_tool_catalog,
    _buffered_sse_tool_calls,
)
from rikkahub_gateway.tests.test_gateway import FakeControl, config
from rikkahub_gateway.tool_execution import (
    HOST_RECEIPT_CONTRACT,
    HostExecutionBoundary,
    NativeToolCall,
    ToolExecutionBinding,
    ToolExecutionBoundaryError,
    ToolExecutionPolicyDecision,
    ToolGuidancePolicyAdapter,
    advertised_schemas,
    canonical_hash,
)
from runtime.tool_guidance import ToolGuidanceStore
from tests.test_tool_guidance import action_card, catalog


def native_payload() -> dict:
    return {
        "model": "stiller-rikka",
        "messages": [{"role": "user", "content": "把客厅灯调整到明确给出的状态"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "home_device_control",
                    "description": "A current native tool description.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["device", "state"],
                        "properties": {
                            "device": {"type": "string"},
                            "state": {"enum": ["on", "off"]},
                        },
                    },
                },
            }
        ],
        "tool_choice": "auto",
    }


class DenyPolicy:
    def authorize(self, binding, *, catalog):
        return ToolExecutionPolicyDecision("denied", ("test_policy_denied",))

    def classify_result(self, binding, *, tool_message):
        return "unknown"

    def observe_receipt(self, receipt):
        return


class ToolExecutionBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = native_payload()
        self.catalog = _advertised_tool_catalog(self.payload)
        self.schemas = advertised_schemas(self.payload)
        self.boundary = HostExecutionBoundary(b"x" * 32)

    def call(self, arguments: str = '{"device":"living-room","state":"on"}') -> NativeToolCall:
        return NativeToolCall("call-home-1", "home_device_control", arguments)

    def test_valid_call_is_bound_without_rewriting_arguments(self) -> None:
        call = self.call('{ "state": "on", "device": "living-room" }')
        binding = self.boundary.bind_call(
            wake_id="wake:one",
            catalog=self.catalog,
            schemas=self.schemas,
            call=call,
        )
        self.assertEqual(
            canonical_hash({"device": "living-room", "state": "on"}),
            binding.arguments_hash,
        )
        self.assertEqual(
            self.catalog["entries"][0]["schema_hash"], binding.schema_hash
        )
        self.assertEqual('{ "state": "on", "device": "living-room" }', call.arguments_text)

    def test_missing_unknown_and_duplicate_arguments_fail_closed(self) -> None:
        for arguments in (
            '{"device":"living-room"}',
            '{"device":"living-room","state":"on","surprise":true}',
            '{"device":"living-room","device":"bedroom","state":"on"}',
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ToolExecutionBoundaryError):
                    self.boundary.bind_call(
                        wake_id="wake:one",
                        catalog=self.catalog,
                        schemas=self.schemas,
                        call=self.call(arguments),
                    )

    def test_policy_denial_happens_before_a_call_can_be_armed(self) -> None:
        denied = HostExecutionBoundary(b"x" * 32, policy=DenyPolicy())
        with self.assertRaisesRegex(
            ToolExecutionBoundaryError, "tool_execution_policy_denied"
        ):
            denied.bind_call(
                wake_id="wake:one",
                catalog=self.catalog,
                schemas=self.schemas,
                call=self.call(),
            )

    def test_missing_local_schema_reference_is_a_value_free_rejection(self) -> None:
        payload = copy.deepcopy(self.payload)
        schema = payload["tools"][0]["function"]["parameters"]
        schema["properties"]["device"] = {"$ref": "#/$defs/missing_private_schema_label"}
        schemas = advertised_schemas(payload)
        with self.assertRaises(ToolExecutionBoundaryError) as caught:
            self.boundary.bind_call(
                wake_id="wake:missing-schema-ref",
                catalog=_advertised_tool_catalog(payload),
                schemas=schemas,
                call=self.call(),
            )
        self.assertEqual(
            "advertised_tool_schema_reference_unresolved", str(caught.exception)
        )
        self.assertNotIn("missing_private_schema_label", str(caught.exception))
        self.assertNotIn("living-room", str(caught.exception))

    def test_unrelated_missing_reference_does_not_block_a_valid_tool(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["tools"].append(
            {
                "type": "function",
                "function": {
                    "name": "unused_broken_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"tags": {"$ref": "#/$defs/missing"}},
                    },
                },
            }
        )
        payload["tools"].append(
            {
                "type": "function",
                "function": {
                    "name": "unused_recursive_tool",
                    "parameters": {"$ref": "#"},
                },
            }
        )
        binding = self.boundary.bind_call(
            wake_id="wake:valid-alongside-broken",
            catalog=_advertised_tool_catalog(payload),
            schemas=advertised_schemas(payload),
            call=self.call(),
        )
        self.assertEqual("home_device_control", binding.tool_name)

    def test_recursive_schema_is_a_value_free_rejection(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["tools"][0]["function"]["parameters"] = {
            "$ref": "#",
            "title": "private-recursive-schema-label",
        }
        with self.assertRaises(ToolExecutionBoundaryError) as caught:
            self.boundary.bind_call(
                wake_id="wake:recursive-schema-ref",
                catalog=_advertised_tool_catalog(payload),
                schemas=advertised_schemas(payload),
                call=self.call(),
            )
        self.assertEqual(
            "advertised_tool_schema_recursion_unsupported", str(caught.exception)
        )
        self.assertNotIn("private-recursive-schema-label", str(caught.exception))
        self.assertNotIn("living-room", str(caught.exception))

    def test_history_reference_failure_is_also_controlled(self) -> None:
        payload = copy.deepcopy(self.payload)
        schema = payload["tools"][0]["function"]["parameters"]
        schema["properties"]["device"] = {"$ref": "#/$defs/missing"}
        current_catalog = _advertised_tool_catalog(payload)
        current_schema_hash = canonical_hash(schema)
        expected = ToolExecutionBinding(
            contract="native-tool-execution-binding/1",
            wake_id="wake:history",
            tool_call_id=self.call().tool_call_id,
            tool_name=self.call().tool_name,
            catalog_hash=current_catalog["catalog_hash"],
            schema_hash=current_schema_hash,
            arguments_hash=canonical_hash({"device": "living-room", "state": "on"}),
            issued_at_ms=1,
        )
        with self.assertRaises(ToolExecutionBoundaryError) as caught:
            self.boundary.verify_history_call(
                expected=expected,
                catalog=current_catalog,
                schemas=advertised_schemas(payload),
                call=self.call(),
            )
        self.assertEqual(
            "advertised_tool_schema_reference_unresolved", str(caught.exception)
        )

    def test_history_recursive_reference_failure_is_also_controlled(self) -> None:
        payload = copy.deepcopy(self.payload)
        schema = {"$ref": "#", "title": "private-recursive-schema-label"}
        payload["tools"][0]["function"]["parameters"] = schema
        current_catalog = _advertised_tool_catalog(payload)
        expected = ToolExecutionBinding(
            contract="native-tool-execution-binding/1",
            wake_id="wake:recursive-history",
            tool_call_id=self.call().tool_call_id,
            tool_name=self.call().tool_name,
            catalog_hash=current_catalog["catalog_hash"],
            schema_hash=canonical_hash(schema),
            arguments_hash=canonical_hash({"device": "living-room", "state": "on"}),
            issued_at_ms=1,
        )
        with self.assertRaises(ToolExecutionBoundaryError) as caught:
            self.boundary.verify_history_call(
                expected=expected,
                catalog=current_catalog,
                schemas=advertised_schemas(payload),
                call=self.call(),
            )
        self.assertEqual(
            "advertised_tool_schema_recursion_unsupported", str(caught.exception)
        )
        self.assertNotIn("private-recursive-schema-label", str(caught.exception))
        self.assertNotIn("living-room", str(caught.exception))

    def test_buffered_stream_reassembles_arguments_before_validation(self) -> None:
        events = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-stream",
                                    "type": "function",
                                    "function": {
                                        "name": "home_device_control",
                                        "arguments": '{"device":"living',
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "arguments": '-room","state":"on"}'
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        raw = (
            "".join(
                "data: " + json.dumps(event, separators=(",", ":")) + "\n\n"
                for event in events
            )
            + "data: [DONE]\n\n"
        ).encode("utf-8")
        calls = _buffered_sse_tool_calls(raw)
        self.assertEqual(1, len(calls))
        self.assertEqual(
            '{"device":"living-room","state":"on"}', calls[0].arguments_text
        )
        self.boundary.bind_call(
            wake_id="wake:stream",
            catalog=self.catalog,
            schemas=self.schemas,
            call=calls[0],
        )

    def test_receipt_binds_call_and_result_hash_but_claims_no_generic_success(self) -> None:
        binding = self.boundary.bind_call(
            wake_id="wake:one",
            catalog=self.catalog,
            schemas=self.schemas,
            call=self.call(),
        )
        raw_result = '{"ok":true,"private_detail":"not persisted"}'
        receipt = self.boundary.witness_result(
            binding=binding,
            tool_message={
                "role": "tool",
                "tool_call_id": binding.tool_call_id,
                "content": raw_result,
            },
        )
        self.assertEqual(HOST_RECEIPT_CONTRACT, receipt["contract"])
        self.assertEqual("unknown", receipt["completion_state"])
        self.assertTrue(self.boundary.verify_receipt(receipt))
        self.assertNotIn(raw_result, json.dumps(receipt, ensure_ascii=False))
        forged = {**receipt, "arguments_hash": "0" * 64}
        self.assertFalse(self.boundary.verify_receipt(forged))

    def test_gateway_rejects_same_call_id_with_changed_arguments_on_continuation(self) -> None:
        app = GatewayApplication(config(), control=FakeControl())
        try:
            prepared = app.prepare_turn(
                self.payload,
                {
                    "Authorization": f"Bearer {config().gateway_token}",
                    "X-ST-Thread-ID": "thread:bound-tool",
                },
            )
            call = self.call()
            bindings = app.bind_response_tool_calls(prepared, [call])
            app.finish_turn(
                prepared,
                keep_for_tools=True,
                tool_call_ids=[call.tool_call_id],
                tool_call_bindings=bindings,
            )
            tampered = {
                **self.payload,
                "messages": [
                    self.payload["messages"][0],
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call.tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": call.tool_name,
                                    "arguments": '{"device":"bedroom","state":"off"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call.tool_call_id,
                        "content": "{}",
                    },
                ],
            }
            with self.assertRaises(GatewayError) as caught:
                app.prepare_turn(
                    tampered,
                    {
                        "Authorization": f"Bearer {config().gateway_token}",
                        "X-ST-Thread-ID": "thread:bound-tool",
                    },
                )
            self.assertEqual("tool_continuation_binding_mismatch", caught.exception.code)
        finally:
            app.upstream.close()


class ToolGuidanceExecutionAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "brain.db"
        self.store = ToolGuidanceStore(self.database)
        self.owner = "owner:execution"
        self.model = "model:execution"
        self.catalog = catalog()
        stored = self.store.remember(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:create",
            expected_row_version=0,
            catalog=self.catalog,
            **action_card(),
        )
        self.card_id = stored["card"]["card_id"]
        self.binding = ToolExecutionBinding(
            contract="native-tool-execution-binding/1",
            wake_id="wake:execute",
            tool_call_id="call-guided",
            tool_name="HomeControl",
            catalog_hash=self.catalog["catalog_hash"],
            schema_hash=self.catalog["entries"][0]["schema_hash"],
            arguments_hash="a" * 64,
            issued_at_ms=1,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_managed_action_needs_real_host_signals_and_can_then_pass(self) -> None:
        closed = ToolGuidancePolicyAdapter(
            self.store, owner_id=self.owner, model_id=self.model
        )
        denied = closed.authorize(self.binding, catalog=self.catalog)
        self.assertEqual("denied", denied.decision)
        self.assertEqual(("host_execution_signals_required",), denied.reason_codes)

        opened = ToolGuidancePolicyAdapter(
            self.store,
            owner_id=self.owner,
            model_id=self.model,
            signal_provider=lambda _binding: {
                "current_user_intent": True,
                "authorization_verified": True,
                "current_confirmation": True,
            },
        )
        allowed = opened.authorize(self.binding, catalog=self.catalog)
        self.assertEqual("allowed_to_attempt", allowed.decision)
        self.assertEqual(self.card_id, allowed.managed_card_id)


if __name__ == "__main__":
    unittest.main()
