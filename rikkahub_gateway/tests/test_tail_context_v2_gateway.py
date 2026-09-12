"""Tail-context-v2: synthetic Control/wire tests, never a real model request.

The inherited safety suites exercise the real gateway validation path with a
full stable + dynamic v2 bundle. Only assertions about the new frame position
are specialized; no authorization or replay assertion is skipped.
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from unittest.mock import patch

from rikkahub_gateway.server import (
    CONTEXT_LAYOUT_CONTRACT, TAIL_CONTEXT_LAYOUT_CONTRACT, GatewayApplication,
    GatewayError, _RequestPerformance, _canonical, _digest,
)
from rikkahub_gateway.tests import test_gateway as legacy
from rikkahub_gateway.tests import test_cache_layout as v1
from rikkahub_gateway.tests import test_gateway_interleaved_lineage as interleaved
from rikkahub_gateway.tests import test_gateway_interleaved_wire as wire
from rikkahub_gateway.tests import test_gateway_split_tool_adversarial as split
from rikkahub_gateway.tests import test_gateway_execution_recovery as execution
from rikkahub_gateway.tests import test_gateway_execution_projection as projection


TAIL_LAYOUT = {"contract": TAIL_CONTEXT_LAYOUT_CONTRACT,
               "insertion_rule": "after-client-messages"}


class TailControl(legacy.FakeControl):
    """Independently assembled authenticated shape, not a runtime mock method."""
    def __init__(self):
        super().__init__()
        self.wake = None
        self.mutation = None
        self.frames = "both"

    def post(self, path, payload):
        if path.endswith("/wakes"):
            result = super().post(path, payload)
            self.wake = {**result, "host_id": payload["host_id"],
                         "thread_id": payload["thread_id"]}
            return result
        if path.endswith("/prepare") and "context_layout_offer" in payload:
            assert "context_layout" not in payload
            self.calls.append((path, copy.deepcopy(payload)))
            stable = {"active_self": {"content": "synthetic fixed self"}}
            dynamic = {"learning_memory": {"items": ["synthetic recall " + str(self.seq)]}}
            stable_frame = {"role": "system", "content": _canonical(stable)}
            dynamic_frame = {"role": "system", "content": _canonical(dynamic)}
            if self.frames == "stable-only":
                dynamic, dynamic_frame = {}, None
            if self.frames == "dynamic-only":
                stable, stable_frame = {}, None
            if self.frames == "hard-off":
                stable, dynamic = {}, {}
                stable_frame, dynamic_frame = {"role": "system", "content": "{}"}, None
            message = {"role": "system", "content": _canonical({**stable, **dynamic})}
            bundle = {
                "contract": TAIL_CONTEXT_LAYOUT_CONTRACT,
                "layout": copy.deepcopy(payload["context_layout_offer"]),
                "binding": {
                    "owner_id": "synthetic-owner", "model_id": "synthetic-model",
                    "host_id": self.wake["host_id"], "thread_id": self.wake["thread_id"],
                    "wake_id": self.wake["wake_id"], "source_digest": payload["source_digest"],
                    "host_contract_digest": payload["host_contract_digest"],
                },
                "stable_message": stable_frame, "dynamic_message": dynamic_frame,
                "legacy_message_hash": _digest(message),
            }
            result = {"message": message, "context_bundle": bundle,
                      "context_hash": _digest(bundle)}
            if self.mutation:
                self.mutation(result)
            return result
        return super().post(path, payload)


class TailContextV2GatewayTests(unittest.TestCase):
    def setUp(self):
        self.control = TailControl()
        self.app = GatewayApplication(replace(legacy.config(), context_layout="tail-context-v2"),
                                      control=self.control)
        self.addCleanup(self.app.upstream.close)
        self.headers = {"X-ST-Thread-ID": "synthetic-tail-thread"}
        self.messages = [
            {"role": "system", "content": "Synthetic battery: 60%"},
            {"role": "user", "content": "old human text " * 1000},
            {"role": "assistant", "content": "old assistant text " * 1000},
            {"role": "user", "content": "current synthetic question"},
        ]

    def prepare(self, messages=None, **extra):
        return self.app.prepare_turn(legacy.GatewayTests.request(
            self.messages if messages is None else messages, **extra), self.headers)

    def arm(self):
        tools = legacy.GatewayTests.bound_tools()
        call = legacy.GatewayTests.native_call("synthetic-tail-call", "synthetic no execution")
        first = self.prepare(tools=tools)
        bindings = self.app.bind_response_tool_calls(first, [call])
        self.app.finish_turn(first, keep_for_tools=True,
                             tool_call_ids=[call.tool_call_id], tool_call_bindings=bindings)
        history = [*copy.deepcopy(self.messages),
                   {"role": "assistant", "content": None,
                    "tool_calls": [legacy.GatewayTests.wire_call(call)]},
                   {"role": "tool", "tool_call_id": call.tool_call_id, "content": '{"exitCode":0}'}]
        return first, tools, history

    def test_exact_offer_tail_order_and_no_client_or_generation_mutation(self):
        original = copy.deepcopy(self.messages)
        first = self.prepare(temperature=0.3, thinking={"type": "enabled"},
                             tools=legacy.GatewayTests.bound_tools())
        bundle = first.session.context_bundle
        self.assertEqual(TAIL_LAYOUT, bundle["layout"])
        self.assertEqual([bundle["stable_message"], *original, bundle["dynamic_message"]], first.payload["messages"])
        self.assertEqual(original, self.messages)
        self.assertEqual(0.3, first.payload["temperature"])
        self.assertEqual({"type": "enabled"}, first.payload["thinking"])
        prepare = next(body for path, body in self.control.calls if path.endswith("/prepare"))
        self.assertEqual(TAIL_LAYOUT, prepare["context_layout_offer"])
        self.assertNotIn("context_layout", prepare)
        confirm = next(body for path, body in self.control.calls if path.endswith("/confirm"))
        self.assertEqual(_digest(bundle), confirm["context_hash"])
        self.assertNotEqual(_digest(first.session.message), confirm["context_hash"])
        for private in ("synthetic-owner", "synthetic-tail-thread", "source_digest",
                        "context_layout_offer", "context_bundle", "capability-1"):
            self.assertNotIn(private, json.dumps(first.payload))

    def test_default_and_strict_mode_enum_preserve_legacy(self):
        self.assertEqual("legacy", legacy.config().context_layout)
        for invalid in ("tail-context-v1", "tail", "TAIL-CONTEXT-V2", True, ""):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                replace(legacy.config(), context_layout=invalid)
        self.app.config = legacy.config()
        first = self.prepare()
        self.assertIsNone(first.session.context_bundle)
        self.assertEqual([first.session.message, *self.messages], first.payload["messages"])
        prepare = next(body for path, body in self.control.calls if path.endswith("/prepare"))
        self.assertNotIn("context_layout_offer", prepare)
        self.assertNotIn("context_layout", prepare)

    def test_no_human_boundary_needed_for_system_only_request(self):
        messages = [{"role": "system", "content": "synthetic host minimal request"}]
        first = self.prepare(messages)
        self.assertEqual(messages, first.payload["messages"][1:-1])
        self.assertEqual(TAIL_LAYOUT, first.session.context_bundle["layout"])

    def test_old_control_fallback_requires_exact_legacy_hash_and_is_wake_frozen(self):
        self.app.control = legacy.FakeControl()
        first, tools, history = self.arm()
        self.assertIsNone(first.session.context_bundle)
        self.app.control = TailControl()  # Now capable, but no mid-wake reprepare.
        second = self.prepare(history, tools=tools)
        self.assertIs(first.session, second.session)
        self.assertIsNone(second.session.context_bundle)
        self.assertEqual([first.session.message, *history], second.payload["messages"])
        self.assertEqual([], self.app.control.calls)
        self.app.finish_turn(second, keep_for_tools=False)
        third = self.prepare([{"role": "user", "content": "fresh synthetic wake"}])
        self.assertEqual(TAIL_CONTEXT_LAYOUT_CONTRACT, third.session.context_bundle["contract"])

    def test_old_control_wrong_hash_does_not_confirm_or_retry(self):
        original = legacy.FakeControl()
        self.app.control = original
        original_post = original.post
        def tamper(path, body):
            reply = original_post(path, body)
            if path.endswith("/prepare"):
                reply["context_hash"] = "0" * 64
            return reply
        original.post = tamper
        with self.assertRaises(GatewayError) as caught:
            self.prepare()
        self.assertEqual("st_context_hash_mismatch", caught.exception.code)
        self.assertEqual(1, sum(path.endswith("/prepare") for path, _ in original.calls))
        self.assertFalse(any(path.endswith("/confirm") for path, _ in original.calls))
        self.assertTrue(any(path.endswith("/close") for path, _ in original.calls))

    def test_present_null_is_not_old_control_fallback(self):
        self.control.mutation = lambda reply: reply.update(context_bundle=None,
                                                          context_hash=_digest(reply["message"]))
        with self.assertRaises(GatewayError) as caught:
            self.prepare()
        self.assertEqual("st_context_bundle_invalid", caught.exception.code)
        self.assertFalse(any(path.endswith("/confirm") for path, _ in self.control.calls))

    def test_invalid_or_unrequested_bundle_never_confirms(self):
        cases = [
            lambda r: r["context_bundle"].update(contract=CONTEXT_LAYOUT_CONTRACT),
            lambda r: r["context_bundle"]["layout"].update(insertion_rule="before-current-human"),
            lambda r: r["context_bundle"]["layout"].update(initial_message_count=4),
            lambda r: r["context_bundle"].update(extra=True),
            lambda r: r["context_bundle"].update(legacy_message_hash="0" * 64),
            lambda r: r["context_bundle"]["stable_message"].update(role="user"),
            lambda r: r["context_bundle"].update(stable_message=None, dynamic_message=None),
            lambda r: r.update(context_hash=_digest(r["message"])),
        ]
        for index, mutation in enumerate(cases):
            with self.subTest(index=index):
                self.control.calls.clear()
                self.control.mutation = mutation
                with self.assertRaises(GatewayError):
                    self.prepare()
                self.assertIsNone(self.app._current_session)
                self.assertFalse(any(path.endswith("/confirm") for path, _ in self.control.calls))
                self.assertEqual(1, sum(path.endswith("/prepare") for path, _ in self.control.calls))

    def test_all_binding_fields_remain_authenticated(self):
        expected_keys = ("host_id", "thread_id", "wake_id", "source_digest", "host_contract_digest")
        for key in (*expected_keys, "owner_id", "model_id"):
            with self.subTest(key=key):
                self.control.calls.clear()
                def mutate(reply):
                    reply["context_bundle"]["binding"][key] = "different" if key in expected_keys else ""
                    reply["context_hash"] = _digest(reply["context_bundle"])
                self.control.mutation = mutate
                with self.assertRaises(GatewayError) as caught:
                    self.prepare()
                self.assertEqual("st_context_bundle_binding_mismatch", caught.exception.code)
                self.assertFalse(any(path.endswith("/confirm") for path, _ in self.control.calls))

    def test_stable_dynamic_and_hard_off_single_frames_have_exact_positions(self):
        for mode in ("stable-only", "dynamic-only", "hard-off"):
            with self.subTest(mode=mode):
                self.control.frames = mode
                first = self.prepare()
                bundle = first.session.context_bundle
                expected = ([bundle["stable_message"]] if bundle["stable_message"] else [])
                expected += self.messages
                expected += [bundle["dynamic_message"]] if bundle["dynamic_message"] else []
                self.assertEqual(expected, first.payload["messages"])
                self.assertEqual(json.loads(first.session.message["content"]),
                                 {**json.loads(bundle["stable_message"]["content"] if bundle["stable_message"] else "{}"),
                                  **json.loads(bundle["dynamic_message"]["content"] if bundle["dynamic_message"] else "{}")})
                self.app.finish_turn(first, keep_for_tools=False)

    def test_volatile_client_text_is_forwarded_as_supplied_with_fixed_st_frames(self):
        first, tools, history = self.arm()
        initial_bundle = copy.deepcopy(first.session.context_bundle)
        history[0]["content"] = "Synthetic battery: 59%"
        history[1]["content"] = "Changed client user placeholder, not inferred as noise"
        second = self.prepare(history, tools=tools)
        self.assertTrue(second.continuation)
        self.assertIs(first.session, second.session)
        self.assertEqual(initial_bundle, second.session.context_bundle)
        self.assertEqual(history, second.payload["messages"][1:-1])
        self.assertFalse(second.cache_comparison["prior_input_prefix_preserved"])
        self.assertEqual(1, sum(path.endswith("/prepare") for path, _ in self.control.calls))
        self.assertEqual(1, len(second.session.host_receipts))

    def test_same_wake_mode_fixed_and_new_wake_selects_new_config(self):
        first, tools, history = self.arm()
        self.app.config = replace(self.app.config, context_layout="anchored-v1")
        second = self.prepare(history, tools=tools)
        self.assertEqual(TAIL_CONTEXT_LAYOUT_CONTRACT, second.session.context_bundle["contract"])
        self.assertEqual(first.payload["messages"][-1], second.payload["messages"][-1])
        self.app.finish_turn(second, keep_for_tools=False)
        self.app.control = v1.BundleControl()
        third = self.prepare([{"role": "user", "content": "fresh synthetic wake"}])
        self.assertEqual(CONTEXT_LAYOUT_CONTRACT, third.session.context_bundle["contract"])

    def test_tail_authentication_checked_before_consuming_tool_wait(self):
        first, tools, history = self.arm()
        first.session.context_bundle["dynamic_message"]["content"] += "modified"
        before = copy.deepcopy(first.session)
        with self.assertRaises(GatewayError) as caught:
            self.prepare(history, tools=tools)
        self.assertEqual("st_context_hash_mismatch", caught.exception.code)
        self.assertEqual(before, first.session)

    def test_tool_arguments_catalog_and_result_ids_not_relaxed_by_tail(self):
        first, tools, history = self.arm()
        for mutation in ("arguments", "catalog", "result_id"):
            with self.subTest(mutation=mutation):
                changed, changed_tools = copy.deepcopy(history), copy.deepcopy(tools)
                changed[0]["content"] = "Changed client system must not bypass tool checks"
                if mutation == "arguments":
                    changed[-2]["tool_calls"][0]["function"]["arguments"] = '{"command":"different"}'
                elif mutation == "catalog":
                    changed_tools[0]["function"]["parameters"]["properties"]["command"]["type"] = "integer"
                else:
                    changed[-1]["tool_call_id"] = "unissued"
                before = copy.deepcopy(first.session)
                with self.assertRaises(GatewayError) as caught:
                    self.prepare(changed, tools=changed_tools)
                self.assertEqual(409, caught.exception.status)
                self.assertEqual(before, first.session)
        self.assertIs(first.session, self.prepare(history, tools=tools).session)

    def test_long_history_prefix_can_survive_dynamic_recall_change_but_is_not_cache_proof(self):
        first = self.prepare()
        self.app.finish_turn(first, keep_for_tools=False)
        second = self.prepare([*self.messages, {"role": "assistant", "content": "synthetic answer"},
                               {"role": "user", "content": "new synthetic topic"}])
        a, b = [_canonical(turn.payload["messages"]).encode() for turn in (first, second)]
        self.assertGreater(len(os.path.commonprefix([a, b])), 30000)
        self.assertTrue(second.cache_comparison["prior_input_prefix_preserved"])
        self.assertTrue(second.cache_comparison["dynamic_context_changed"])
        # Previous tail is no longer at the same position. No assertion that
        # the whole prior outbound request remains a prefix of the next one.
        self.assertNotEqual(first.payload["messages"], second.payload["messages"][:len(first.payload["messages"])])

    def test_telemetry_unknown_until_prepared_and_uses_client_only_name(self):
        performance = _RequestPerformance()
        performance.client_input_prefix_preserved = True
        with self.assertLogs("stiller.rikkahub.performance", level="INFO") as capture:
            performance.emit()
        record = json.loads(capture.records[0].getMessage())
        self.assertEqual("unknown", record["context_layout"])
        self.assertTrue(record["client_input_prefix_preserved"])
        self.assertNotIn("prior_input_prefix_preserved", record)
        self.assertNotIn("upstream_prefix_preserved", record)


class TailInterleavedLineageTests(interleaved.GatewayInterleavedLineageTests):
    def setUp(self):
        cfg = replace(legacy.config(), context_layout="tail-context-v2")
        with patch.object(legacy, "FakeControl", TailControl), patch.object(legacy, "config", lambda: cfg):
            super().setUp()

    def test_shell_and_native_open_complete_interleaving_keeps_every_message(self):
        messages = self.history()
        original = copy.deepcopy(messages)
        turn = self.prepare(messages)
        self.assertIs(turn.session, self.session)
        self.assertEqual(original, messages)
        self.assertEqual(original, turn.payload["messages"][1:-1])
        self.assertEqual(self.session.context_bundle["dynamic_message"], turn.payload["messages"][-1])
        self.assertEqual([call.tool_call_id for call in self.calls], self.session.settled_tool_call_order)
        self.assertEqual(2, len(self.session.host_receipts))
        self.app.finish_turn(turn, keep_for_tools=False)
        self.assertIsNone(self.app._current_session)

    def test_structured_content_and_extra_metadata_remain_on_their_own_assistant(self):
        messages = self.history()
        messages[1].update(content=[{"type": "text", "text": ""}], name="synthetic-speaker",
                           refusal=None, audio={"id": "synthetic-audio"})
        messages[3]["content"] = []
        original = copy.deepcopy(messages)
        turn = self.prepare(messages)
        self.assertEqual(original, messages)
        self.assertEqual(original, turn.payload["messages"][1:-1])
        self.assertNotIn("name", turn.payload["messages"][-3])

    def test_cancel_text_neither_proves_user_cancel_nor_clears_partial_wait(self):
        messages = self.history()
        messages[2]["content"] = "Generation cancelled by user"
        self.reject(messages[:3])
        turn = self.prepare(messages)
        self.assertIs(self.app._current_session, self.session)
        self.assertEqual("unknown", self.session.host_receipts[0]["completion_state"])
        self.assertEqual(messages, turn.payload["messages"][1:-1])


class TailSplitDeclarationTests(split.SplitToolDeclarationAdversarialTests):
    def setUp(self):
        cfg = replace(legacy.config(), context_layout="tail-context-v2")
        with patch.object(legacy, "FakeControl", TailControl), patch.object(legacy, "config", lambda: cfg):
            super().setUp()


class TailInterleavedWireTests(wire.InterleavedHTTPWireTests):
    def setUp(self):
        cfg = replace(legacy.config(), context_layout="tail-context-v2")
        with patch.object(legacy, "FakeControl", TailControl), patch.object(legacy, "config", lambda: cfg):
            super().setUp()

    def roundtrip(self, first_stream, continuation_stream):
        status, body = self.post([self.user], stream=first_stream)
        self.assertEqual(200, status)
        for call in self.calls:
            self.assertIn(call["id"].encode(), body)
        session = self.app._current_session
        self.assertEqual(TAIL_CONTEXT_LAYOUT_CONTRACT, session.context_bundle["contract"])
        self.assertEqual(2, len(session.expected_tool_calls))
        messages = self.interleaved()
        before = copy.deepcopy(messages)
        status, body = self.post(messages, stream=continuation_stream)
        self.assertEqual(200, status, body.decode())
        self.assertIn(b"synthetic complete", body)
        self.assertEqual(before, messages)
        self.assertEqual(before, self.upstream_payloads[1]["messages"][1:-1])
        self.assertEqual(session.context_bundle["dynamic_message"], self.upstream_payloads[1]["messages"][-1])
        self.assertEqual(2, len(session.host_receipts))
        self.assertIsNone(self.app._current_session)
        self.assertEqual(1, self.control.seq)
        self.assertEqual(200, self.post([{"role": "user", "content": "synthetic next user"}], stream=False)[0])
        self.assertEqual(2, self.control.seq)

    def test_wire_telemetry_reports_actual_tail_and_verified_legacy_fallback(self):
        original_emit = _RequestPerformance.emit
        observed = []
        emitted = threading.Event()
        def capture(performance):
            observed.append(performance.context_layout)
            original_emit(performance)
            emitted.set()
        with patch.object(_RequestPerformance, "emit", capture):
            self.assertEqual(200, self.post([self.user], stream=False)[0])
            self.assertTrue(emitted.wait(3))
            self.assertEqual(TAIL_CONTEXT_LAYOUT_CONTRACT, observed[-1])
            emitted.clear()
            self.assertEqual(200, self.post(self.interleaved(), stream=False)[0])
            self.assertTrue(emitted.wait(3))
            self.assertEqual(TAIL_CONTEXT_LAYOUT_CONTRACT, observed[-1])
            emitted.clear()
            self.app.control = legacy.FakeControl()
            self.assertEqual(200, self.post([self.user], stream=False)[0])
            self.assertTrue(emitted.wait(3))
            self.assertEqual("legacy", observed[-1])


class TailRealControlWireTests(v1.CacheLayoutRealControlWireTests):
    """Real authenticated localhost Control + temporary SQLite, fake provider."""
    def setUp(self):
        super().setUp()
        self.app.config = replace(self.app.config, context_layout="tail-context-v2")

    def test_three_human_wakes_and_tool_continuation_use_real_authenticated_bundle(self):
        messages = [{"role": "system", "content": "Synthetic battery: 60%"},
                    {"role": "user", "content": "Earlier human text."},
                    {"role": "assistant", "content": "Earlier answer."},
                    {"role": "user", "content": "你还记得我们之前读的侍魔嘛？"}]
        first = self.post(messages)
        session = self.app._current_session
        self.assertEqual(TAIL_CONTEXT_LAYOUT_CONTRACT, session.context_bundle["contract"])
        stable = json.loads(session.context_bundle["stable_message"]["content"])
        dynamic = json.loads(session.context_bundle["dynamic_message"]["content"])
        self.assertEqual(json.loads(session.message["content"]), {**stable, **dynamic})
        self.assertIn("learning_memory", dynamic)
        self.assertEqual(session.context_bundle["dynamic_message"], self.upstream_payloads[0]["messages"][-1])
        messages[0]["content"] = "Synthetic battery: 59%"
        messages.extend([first["choices"][0]["message"],
                         {"role": "tool", "tool_call_id": self.call["id"], "content": '{"synthetic":true}'}])
        answer = self.post(messages)
        self.assertEqual(session.context_bundle["dynamic_message"], self.upstream_payloads[1]["messages"][-1])
        self.assertEqual(messages[0], self.upstream_payloads[1]["messages"][1])
        self.assertEqual(1, len(session.host_receipts))
        self.assertIsNone(self.app._current_session)
        for _ in (2, 3):
            messages.extend([answer["choices"][0]["message"],
                             {"role": "user", "content": "你还记得我们之前读的侍魔嘛？"}])
            answer = self.post(messages)
            self.assertIsNone(self.app._current_session)
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            rows = connection.execute("SELECT wake_id,status,context_layout_json FROM brain_context_snapshots WHERE context_layout_json != '{}' ORDER BY rowid").fetchall()
        self.assertEqual(3, len(rows))
        self.assertEqual(3, len({row[0] for row in rows}))
        self.assertEqual(["closed"] * 3, [row[1] for row in rows])
        self.assertEqual([TAIL_LAYOUT] * 3, [json.loads(row[2])["layout"] for row in rows])
        for payload in self.upstream_payloads:
            for private in ("context_bundle", "context_layout_offer", "wake_capability", self.token):
                self.assertNotIn(private, json.dumps(payload))


class TailExecutionControl(TailControl, execution.ExecutionControl):
    """V2 negotiation plus the existing synthetic execution authority."""


class TailExecutionRecoveryTests(execution.GatewayExecutionRecoveryTests):
    """Replay all strict execution/timer/recovery tests using full tail frames."""
    def setUp(self):
        cfg = replace(legacy.config(), context_layout="tail-context-v2")
        with patch.object(execution, "ExecutionControl", TailExecutionControl), patch.object(execution, "config", lambda: cfg):
            super().setUp()
        self.assertEqual(TAIL_CONTEXT_LAYOUT_CONTRACT, self.prepared.session.context_bundle["contract"])


class TailExecutionProjectionHTTPTests(projection.ExecutionProjectionHTTPTests):
    """Full JSON/SSE strict-reference round trips with original tool projection."""
    def setUp(self):
        cfg = replace(legacy.config(), context_layout="tail-context-v2")
        with patch.object(execution, "ExecutionControl", TailExecutionControl), patch.object(legacy, "config", lambda: cfg):
            super().setUp()

    def upstream(self, request):
        payload = json.loads(request.content)
        self.assertEqual(TAIL_CONTEXT_LAYOUT_CONTRACT, self.app._current_session.context_bundle["contract"])
        self.assertEqual(self.app._current_session.context_bundle["dynamic_message"], payload["messages"][-1])
        return super().upstream(request)


if __name__ == "__main__":
    unittest.main()
