"""Rejected names remain unexecuted while JSON/SSE explain how to continue.

Only a localhost gateway, fake control and in-memory upstream are used. These
tests do not execute MCP tools, access databases or contact a model provider.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from rikkahub_gateway.tests import test_gateway_failure_observability as helpers
from rikkahub_gateway.tests.test_gateway_execution_recovery import ExecutionControl


UNKNOWN_NAME = "PrivateOldToolNameMustNotBeEchoed"
PRIVATE_ARGUMENT = "PrivateUnadvertisedArgumentMustNotBeEchoed"
REVISE_NAME = "mcp__StillerBrain__revise_memory"


def revision_tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": REVISE_NAME,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target_ref", "changes"],
                "properties": {
                    "target_ref": {"type": "string"},
                    "changes": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"summary": {"type": "string"}},
                    },
                    "execution_ref": {
                        "type": "string",
                        "pattern": "^stexec_[A-Za-z0-9_-]{43}$",
                        "x-stbrain-execution-tool": "revise_memory",
                        "x-stbrain-execution-contract": "st-execution/1",
                    },
                },
            },
        },
    }


def valid_call() -> dict:
    return helpers.native_call(
        name=REVISE_NAME,
        call_id="synthetic-valid-revision",
        values={
            "target_ref": "emotion://emmem_" + "1" * 32 + "@1",
            "changes": {"summary": "Synthetic revised summary."},
        },
    )


class GatewayUnadvertisedFeedbackTests(unittest.TestCase):
    setUp = helpers.GatewayFailureObservabilityTests.setUp
    tearDown = helpers.GatewayFailureObservabilityTests.tearDown
    tools = staticmethod(helpers.GatewayFailureObservabilityTests.tools)
    events = staticmethod(helpers.GatewayFailureObservabilityTests.events)
    use_upstream = helpers.GatewayFailureObservabilityTests.use_upstream
    post = helpers.GatewayFailureObservabilityTests.post
    assert_failed_without_calls = helpers.GatewayFailureObservabilityTests.assert_failed_without_calls

    def configure_binding(self, enabled: bool) -> None:
        self.app.config = replace(
            self.app.config,
            require_execution_binding=enabled,
            execution_epoch="synthetic-rejected-name-epoch" if enabled else "",
        )

    @staticmethod
    def response(calls: list[dict], *, stream: bool, prefix: bool):
        if stream:
            return helpers.sse(calls, prefix=prefix)
        return {
            "choices": [{
                "message": {"role": "assistant", "content": "Synthetic proposal.", "tool_calls": calls},
                "finish_reason": "tool_calls",
            }],
        }

    def rejected(
        self, *, binding: bool, stream: bool, prefix: bool = False,
        name: str = UNKNOWN_NAME, calls: list[dict] | None = None,
        protected_collision: bool = False,
    ):
        self.configure_binding(binding)
        unknown = helpers.native_call(
            name=name, call_id="synthetic-rejected-name-call",
            values={"private_value": PRIVATE_ARGUMENT},
        )
        batch = [unknown] if calls is None else calls
        start = len(self.control.calls)
        self.use_upstream(self.response(batch, stream=stream, prefix=prefix), stream=stream)
        with self.assertLogs("stiller.rikkahub.gateway", level="WARNING") as logs:
            status, raw = self.post(tools=[revision_tool()], stream=stream)
        expected_status = 200 if stream and prefix else 502
        self.assertEqual(expected_status, status)
        error = self.events(raw)[-2]["error"] if status == 200 else json.loads(raw)["error"]
        self.assertEqual("tool_not_advertised", error["code"])
        self.assertEqual({"message", "type", "code"}, set(error))
        self.assertIn("tool_not_advertised", error["message"])
        if protected_collision:
            self.assertEqual("tool_not_advertised", error["message"])
        else:
            for phrase in (
                "本轮工具目录没有的名字", "本批调用尚未交给客户端执行",
                "刷新工具目录", "按当前目录原样使用工具名", "新的用户消息",
                "历史工具名称不代表当前可调用",
            ):
                self.assertIn(phrase, error["message"])
        self.assertEqual(1, len(logs.records))
        record = json.loads(logs.records[0].getMessage())
        self.assertEqual({"event", "at", "code", "streamed_prefix"}, set(record))
        self.assertEqual("tool_not_advertised", record["code"])
        self.assertEqual(stream and prefix, record["streamed_prefix"])
        self.assertIsNone(logs.records[0].exc_info)
        joined = raw.decode("utf-8") + logs.records[0].getMessage()
        for marker in (
            name, PRIVATE_ARGUMENT, "synthetic-rejected-name-call", "synthetic-valid-revision",
            helpers.PROMPT_MARKER, "capability-1",
            self.app.config.gateway_token, self.app.config.upstream_api_key,
        ):
            self.assertNotIn(marker, joined)
        self.assertNotIn('"tool_calls"', joined)
        self.assertNotIn('"finish_reason"', joined)
        if status == 200:
            events = self.events(raw)
            self.assertEqual("I will submit it now.", events[0]["choices"][0]["delta"]["content"])
            self.assertNotIn("choices", events[-2])
            self.assertEqual("[DONE]", events[-1])
            self.assertEqual(1, raw.count(b"data: [DONE]"))
        self.assertFalse(any(
            path.startswith("/v1/host/tool-executions/")
            for path, _ in self.control.calls[start:]
        ))
        self.assert_failed_without_calls()
        return error, raw

    def test_stream_before_and_after_prefix_rejects_unknown_name_on_both_boundaries(self):
        for binding in (False, True):
            for prefix in (False, True):
                with self.subTest(binding=binding, prefix=prefix):
                    self.rejected(binding=binding, stream=True, prefix=prefix)
        self.assertEqual([], self.policy.authorizations)

    def test_nonstream_unknown_name_has_same_actionable_fixed_message(self):
        for binding in (False, True):
            with self.subTest(binding=binding):
                self.rejected(binding=binding, stream=False)
        self.assertEqual([], self.policy.authorizations)

    def test_known_legacy_name_is_not_mapped_or_reintroduced(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.rejected(
                    binding=True, stream=stream, prefix=stream,
                    name="revise_tool_guidance",
                )
                self.assertEqual([REVISE_NAME], [
                    tool["function"]["name"] for tool in self.upstream_requests[0]["tools"]
                ])
        self.assertEqual([], self.policy.authorizations)

    def test_mixed_batch_releases_and_executes_neither_call_in_either_order(self):
        for binding in (False, True):
            for unknown_first in (False, True):
                with self.subTest(binding=binding, unknown_first=unknown_first):
                    unknown = helpers.native_call(
                        name=UNKNOWN_NAME, call_id="synthetic-rejected-name-call",
                        values={"private_value": PRIVATE_ARGUMENT},
                    )
                    calls = [unknown, valid_call()] if unknown_first else [valid_call(), unknown]
                    self.rejected(binding=binding, stream=True, prefix=True, calls=calls)

    def test_rejected_old_name_does_not_block_next_human_valid_revision(self):
        self.control = ExecutionControl()
        self.app.control = self.control
        self.rejected(binding=True, stream=True, prefix=True, name="revise_tool_guidance")
        self.assertEqual(1, self.control.seq)
        self.assertEqual([], self.policy.authorizations)

        proposed = valid_call()
        self.use_upstream(self.response([proposed], stream=False, prefix=False), stream=False)
        captured = []
        original_prepare = self.app.prepare_turn

        def capture_prepared(*args, **kwargs):
            prepared = original_prepare(*args, **kwargs)
            captured.append(prepared)
            return prepared

        self.addCleanup(lambda: self.app.finish_turn(captured[-1], keep_for_tools=False) if captured else None)
        with patch.object(self.app, "prepare_turn", capture_prepared):
            with self.assertNoLogs("stiller.rikkahub.gateway", level="WARNING"):
                status, raw = self.post(tools=[revision_tool()], stream=False)
        self.assertEqual(200, status)
        self.assertEqual(1, len(self.upstream_requests))
        self.assertEqual(2, self.control.seq, "next user message should open one new wake")
        actual = json.loads(raw)["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(proposed["id"], actual["id"])
        self.assertEqual(REVISE_NAME, actual["function"]["name"])
        values = json.loads(actual["function"]["arguments"])
        execution_ref = values.pop("execution_ref")
        self.assertRegex(execution_ref, r"^stexec_[A-Za-z0-9_-]{43}$")
        self.assertEqual(json.loads(proposed["function"]["arguments"]), values)
        self.assertEqual({proposed["id"]}, self.app._current_session.expected_tool_call_ids)
        self.assertEqual(1, len(self.policy.authorizations))
        self.assertEqual([], self.policy.receipts, "no synthetic proposal is an execution receipt")
        issued = [payload for path, payload in self.control.calls if path.endswith("/issue")]
        self.assertEqual(1, len(issued))
        self.assertEqual([proposed["id"]], [call["call_id"] for call in issued[0]["calls"]])
        # No MCP invocation is performed; cleanup closes only this fake wait.

    def test_fixed_message_protected_collision_omits_explanatory_text(self):
        original_post = self.control.post

        def protected_message_fragment(path, payload):
            result = original_post(path, payload)
            if path == "/v1/host/wakes":
                result["wake_capability"] = "本轮工具目录"
            return result

        self.control.post = protected_message_fragment
        for stream in (False, True):
            with self.subTest(stream=stream):
                _, raw = self.rejected(
                    binding=True, stream=stream, prefix=stream, protected_collision=True,
                )
                self.assertNotIn("本轮工具目录", raw.decode("utf-8"))

    def test_existing_protected_output_rejection_takes_priority(self):
        self.configure_binding(True)
        for stream in (False, True):
            with self.subTest(stream=stream):
                # FakeControl assigns this value to the next synthetic wake.
                protected = "capability-" + str(self.control.seq + 1)
                call = helpers.native_call(
                    name=UNKNOWN_NAME, values={"private_value": protected},
                )
                self.use_upstream(self.response([call], stream=stream, prefix=stream), stream=stream)
                with self.assertLogs("stiller.rikkahub.gateway", level="WARNING") as logs:
                    status, raw = self.post(tools=[revision_tool()], stream=stream)
                error = self.events(raw)[-2]["error"] if status == 200 else json.loads(raw)["error"]
                self.assertEqual("upstream_protected_value", error["code"])
                self.assertNotIn(protected, raw.decode("utf-8") + logs.records[0].getMessage())
                self.assert_failed_without_calls()


if __name__ == "__main__":
    unittest.main()
