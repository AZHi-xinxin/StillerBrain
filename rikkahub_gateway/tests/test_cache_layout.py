"""Synthetic ordered-context integration; no remote model calls or private data."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from contextlib import closing
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import httpx

from rikkahub_gateway.server import (
    CONTEXT_LAYOUT_CONTRACT, GatewayApplication, GatewayError,
    _GatewayHandler, _RequestPerformance, _canonical, _digest,
)
from rikkahub_gateway.tests.test_gateway import FakeControl, config
from rikkahub_gateway.tests import test_gateway as legacy_gateway
from rikkahub_gateway.tests import test_gateway_interleaved_lineage as interleaved
from rikkahub_gateway.tests import test_gateway_interleaved_wire as interleaved_wire
from rikkahub_gateway.tests import test_gateway_split_tool_adversarial as split_declarations
from mcp_server.control_server import ControlApplication, _ControlHandler
from runtime.onboarding import ModuleOneOnboardingStore
from tests import test_onboarding as onboarding_fixtures


class BundleControl(FakeControl):
    def __init__(self):
        super().__init__()
        self.last_wake = None
        self.last_bundle = None
        self.mutation = None

    def post(self, path, payload):
        if path == "/v1/host/wakes":
            result = super().post(path, payload)
            self.last_wake = {
                **result, "host_id": payload["host_id"], "thread_id": payload["thread_id"],
                "owner_id": "synthetic-owner", "model_id": "synthetic-model",
            }
            return result
        if path == "/v1/host/context/prepare" and "context_layout" in payload:
            self.calls.append((path, copy.deepcopy(payload)))
            stable = {"active_self": {"content": "synthetic authored identity"}}
            dynamic = {"learning_memory": {"items": ["synthetic recall " + str(self.seq)]}}
            message = ModuleOneOnboardingStore._context_message(stable=stable, dynamic=dynamic)
            bundle = ModuleOneOnboardingStore._context_bundle(
                stable=stable, dynamic=dynamic, message=message,
                layout=payload["context_layout"], wake=self.last_wake,
                source_digest=payload["source_digest"],
                host_contract_digest=payload["host_contract_digest"],
            )
            result = {"message": message, "context_bundle": bundle, "context_hash": _digest(bundle)}
            if self.mutation:
                self.mutation(result)
            self.last_bundle = copy.deepcopy(bundle)
            return result
        return super().post(path, payload)


class CacheLayoutTests(unittest.TestCase):
    def setUp(self):
        self.control = BundleControl()
        self.app = GatewayApplication(replace(config(), context_layout="anchored-v1"), control=self.control)
        self.headers = {"X-ST-Thread-ID": "synthetic-thread"}
        self.messages = [
            {"role": "system", "content": "synthetic frontend preferences"},
            {"role": "user", "content": "historical question " * 1000},
            {"role": "assistant", "content": "historical response " * 1000},
            {"role": "user", "content": "current question"},
        ]

    def tearDown(self):
        self.app.upstream.close()

    def prepare(self, messages=None, **extra):
        return self.app.prepare_turn(
            {"model": config().public_model, "messages": self.messages if messages is None else messages, **extra},
            self.headers,
        )

    def test_layout_at_current_not_first_human_preserves_original_objects(self):
        original = copy.deepcopy(self.messages)
        first = self.prepare(temperature=0.5, thinking={"type": "enabled"})
        bundle = first.session.context_bundle
        self.assertEqual(3, bundle["layout"]["human_message_index"])
        self.assertEqual([bundle["stable_message"], *original[:3], bundle["dynamic_message"], original[3]], first.payload["messages"])
        self.assertEqual(original, self.messages)
        self.assertEqual(0.5, first.payload["temperature"])
        self.assertEqual({"type": "enabled"}, first.payload["thinking"])
        for marker in ["synthetic-owner", "synthetic-thread", "wake-1", "capability-1", "initial_messages_digest"]:
            self.assertNotIn(marker, json.dumps(first.payload))
        confirmations = [payload for path, payload in self.control.calls if path.endswith("/confirm")]
        self.assertEqual(_digest(bundle), confirmations[0]["context_hash"])
        self.assertNotEqual(_digest(first.session.message), confirmations[0]["context_hash"])

    def test_changed_dynamic_keeps_long_history_prefix_in_next_human_wake(self):
        first = self.prepare()
        self.app.finish_turn(first, keep_for_tools=False)
        later_messages = [*self.messages, {"role": "assistant", "content": "answer"}, {"role": "user", "content": "next topic"}]
        second = self.prepare(later_messages)
        self.assertNotEqual(first.session.wake_id, second.session.wake_id)
        self.assertEqual(first.session.context_bundle["stable_message"], second.session.context_bundle["stable_message"])
        a, b = [_canonical(item.payload["messages"]).encode() for item in [first, second]]
        self.assertGreater(len(os.path.commonprefix([a, b])), 35000)
        # This checks bytes only; it does not assert provider token cache hits.
        self.assertEqual(second.session.context_bundle["dynamic_message"], second.payload["messages"][-2])
        self.assertEqual({"stable_context_changed": False, "dynamic_context_changed": True,
                          "wire_tools_changed": False, "prior_input_prefix_preserved": True}, second.cache_comparison)

    def test_frames_and_anchor_frozen_across_real_bound_tool_continuation(self):
        tools = legacy_gateway.GatewayTests.bound_tools()
        call = legacy_gateway.GatewayTests.native_call("synthetic-call", "echo synthetic")
        first = self.prepare(tools=tools)
        bindings = self.app.bind_response_tool_calls(first, [call])
        self.app.finish_turn(first, keep_for_tools=True, tool_call_ids=[call.tool_call_id], tool_call_bindings=bindings)
        history = [*self.messages,
                   {"role": "assistant", "content": None, "tool_calls": [legacy_gateway.GatewayTests.wire_call(call)]},
                   {"role": "tool", "tool_call_id": call.tool_call_id, "content": '{"exitCode":0}'}]
        second = self.prepare(history, tools=tools)
        self.assertTrue(second.continuation)
        self.assertIs(first.session, second.session)
        self.assertEqual(first.payload["messages"], second.payload["messages"][:-2])
        self.assertEqual(1, self.control.seq)
        self.assertEqual(tools, second.payload["tools"])

    def test_modified_or_truncated_history_rejected_before_receipts_consumed(self):
        tools = legacy_gateway.GatewayTests.bound_tools()
        call = legacy_gateway.GatewayTests.native_call("synthetic-call", "echo synthetic")
        first = self.prepare(tools=tools)
        bindings = self.app.bind_response_tool_calls(first, [call])
        self.app.finish_turn(first, keep_for_tools=True, tool_call_ids=[call.tool_call_id], tool_call_bindings=bindings)
        tail = [{"role": "assistant", "content": None, "tool_calls": [legacy_gateway.GatewayTests.wire_call(call)]},
                {"role": "tool", "tool_call_id": call.tool_call_id, "content": '{"exitCode":0}'}]
        for bad in [self.messages[1:], [{**self.messages[0], "content": "changed"}, *self.messages[1:]],
                    [*self.messages, {"role": "user", "content": "new human hidden before tools"}]]:
            with self.subTest(kind=len(bad)), self.assertRaises(GatewayError) as caught:
                self.prepare([*bad, *tail], tools=tools)
            self.assertEqual("tool_continuation_context_history_mismatch", caught.exception.code)
            self.assertEqual({call.tool_call_id}, first.session.expected_tool_call_ids)
            self.assertEqual([], first.session.host_receipts)
        recovered = self.prepare([*self.messages, *tail], tools=tools)
        self.assertTrue(recovered.continuation)

    def test_wrong_boundaries_bindings_frames_or_old_hash_rejected(self):
        cases = [
            lambda reply: reply["context_bundle"]["layout"].update(human_message_index=1),
            lambda reply: reply["context_bundle"]["binding"].update(thread_id="foreign-thread"),
            lambda reply: reply["context_bundle"]["binding"].update(wake_id="old-wake"),
            lambda reply: reply["context_bundle"]["binding"].update(source_digest="0" * 64),
            lambda reply: reply["context_bundle"]["stable_message"].update(role="user"),
            lambda reply: reply["context_bundle"].update(dynamic_message=None),
            lambda reply: reply.update(context_hash=_digest(reply["message"])),
            lambda reply: reply["context_bundle"].update(contract="stbrain-context-layout/99"),
        ]
        for mutation in cases:
            with self.subTest(mutation=cases.index(mutation)):
                self.control.mutation = mutation
                with self.assertRaises(GatewayError):
                    self.prepare()
                self.assertFalse(any(path.endswith("/confirm") for path, _ in self.control.calls))
                self.assertIsNone(self.app._current_session)

    def test_legacy_control_retains_exact_single_message(self):
        self.app.control = FakeControl()
        first = self.prepare()
        self.assertIsNone(first.session.context_bundle)
        self.assertEqual([first.session.message, *self.messages], first.payload["messages"])

    def test_comparison_exports_only_flags_and_resets_across_threads(self):
        first = self.prepare()
        self.assertTrue(all(value is None for value in first.cache_comparison.values()))
        self.app.finish_turn(first, keep_for_tools=False)
        self.headers = {"X-ST-Thread-ID": "different-synthetic-thread"}
        second = self.prepare()
        self.assertTrue(all(value is None for value in second.cache_comparison.values()))
        self.assertNotIn("digest", json.dumps(second.cache_comparison))

    def test_legacy_flag_is_default_and_exact_old_payload_is_preserved(self):
        self.app.config = config()
        original = copy.deepcopy(self.messages)
        first = self.prepare(tools=legacy_gateway.GatewayTests.bound_tools(),
                             reasoning_effort="high", stream=False)
        self.assertEqual("legacy", config().context_layout)
        self.assertIsNone(first.session.context_bundle)
        self.assertEqual([first.session.message, *original], first.payload["messages"])
        self.assertEqual("high", first.payload["reasoning_effort"])
        prepared_request = next(payload for path, payload in self.control.calls if path.endswith("/prepare"))
        self.assertNotIn("context_layout", prepared_request)
        for invalid in ("unknown", "true", "anchored-v2", "", True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                replace(config(), context_layout=invalid)

    def test_mode_change_does_not_change_an_existing_wake(self):
        first = self.prepare()
        self.app.finish_turn(first, keep_for_tools=True, tool_call_ids=["synthetic-mode-call"])
        self.app.config = replace(self.app.config, context_layout="legacy")
        messages = [*self.messages,
                    {"role": "assistant", "content": None, "tool_calls": [{"id": "synthetic-mode-call", "type": "function",
                     "function": {"name": "synthetic_tool", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "synthetic-mode-call", "content": "synthetic"}]
        second = self.prepare(messages)
        self.assertIs(first.session, second.session)
        self.assertEqual(first.payload["messages"], second.payload["messages"][:-2])
        self.app.finish_turn(second, keep_for_tools=False)
        third = self.prepare([*messages, {"role": "assistant", "content": "done"}, {"role": "user", "content": "new"}])
        self.assertIsNone(third.session.context_bundle)

    def test_volatile_frontend_system_change_does_not_relax_anchor_or_consume_tools(self):
        self.messages[0]["content"] = "Synthetic phone battery: 60%"
        first = self.prepare()
        self.app.finish_turn(first, keep_for_tools=True, tool_call_ids=["synthetic-volatile-call"])
        changed = copy.deepcopy(self.messages)
        changed[0]["content"] = "Synthetic phone battery: 59%"
        changed.extend([
            {"role": "assistant", "content": None, "tool_calls": [{"id": "synthetic-volatile-call", "type": "function",
             "function": {"name": "synthetic_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "synthetic-volatile-call", "content": "synthetic"}])
        with self.assertRaises(GatewayError) as caught:
            self.prepare(changed)
        self.assertEqual("tool_continuation_context_history_mismatch", caught.exception.code)
        self.assertEqual({"synthetic-volatile-call"}, first.session.expected_tool_call_ids)
        self.assertIsNotNone(first.session.context_bundle)

    def test_fresh_negotiation_failure_closes_only_its_wake_and_preserves_original_error(self):
        for stage in ("prepare", "hash", "confirm"):
            for close_fails in (False, True):
                with self.subTest(stage=stage, close_fails=close_fails):
                    self.control = BundleControl()
                    self.app.control = self.control
                    original = self.control.post
                    def fail(path, payload):
                        if path.endswith("/" + stage) and stage != "hash":
                            raise GatewayError(502, "synthetic_" + stage + "_failure")
                        if path.endswith("/close") and close_fails:
                            self.control.calls.append((path, dict(payload)))
                            raise GatewayError(502, "synthetic_close_failure")
                        result = original(path, payload)
                        if stage == "hash" and path.endswith("/prepare"):
                            result["context_hash"] = "0" * 64
                        return result
                    self.control.post = fail
                    with self.assertRaises(GatewayError) as caught:
                        self.prepare()
                    self.assertEqual("st_context_hash_mismatch" if stage == "hash" else "synthetic_" + stage + "_failure", caught.exception.code)
                    closes = [payload for path, payload in self.control.calls if path.endswith("/close")]
                    self.assertEqual([{"wake_id": "wake-1", "wake_capability": "capability-1"}], closes)
                    self.assertIsNone(self.app._current_session)

    def test_existing_inflight_wake_never_closed_by_a_new_request(self):
        first = self.prepare()
        before = copy.deepcopy(self.control.calls)
        with self.assertRaises(GatewayError) as caught:
            self.prepare([{"role": "user", "content": "another human"}])
        self.assertEqual("human_turn_in_progress", caught.exception.code)
        self.assertIs(first.session, self.app._current_session)
        self.assertEqual(before, self.control.calls)


class CacheLayoutInterleavedTests(interleaved.GatewayInterleavedLineageTests):
    """Replay all existing A/T and reasoning-shape proofs with the new bundle."""
    def setUp(self):
        with patch.object(legacy_gateway, "FakeControl", BundleControl), patch.object(
                legacy_gateway, "config", lambda: replace(config(), context_layout="anchored-v1")):
            super().setUp()


class CacheLayoutSplitDeclarationTests(split_declarations.SplitToolDeclarationAdversarialTests):
    def setUp(self):
        with patch.object(legacy_gateway, "FakeControl", BundleControl), patch.object(
                legacy_gateway, "config", lambda: replace(config(), context_layout="anchored-v1")):
            super().setUp()


class CacheLayoutInterleavedWireTests(interleaved_wire.InterleavedHTTPWireTests):
    """Real localhost gateway JSON/SSE protocol, synthetic Control/provider."""
    def setUp(self):
        with patch.object(legacy_gateway, "FakeControl", BundleControl), patch.object(
                legacy_gateway, "config", lambda: replace(config(), context_layout="anchored-v1")):
            super().setUp()


class CacheLayoutRealControlWireTests(unittest.TestCase):
    """Gateway + authenticated Control HTTP + SQLite, provider stays mock-only."""
    def setUp(self):
        self.fixture = onboarding_fixtures.ModuleOneOnboardingTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.seed_tagless_named_learning()
        self.token = "synthetic-host-token-that-is-at-least-32"
        control = ControlApplication(
            self.fixture.store, owner_id=self.fixture.owner, model_id=self.fixture.model,
            host_token=self.token, human_token="synthetic-human-token-that-is-at-least-32",
            human_actor_id="synthetic-human",
        )
        self.control_server = ThreadingHTTPServer(("127.0.0.1", 0), _ControlHandler)
        self.control_server.application = control
        self.control_thread = threading.Thread(target=self.control_server.serve_forever, daemon=True)
        self.control_thread.start()
        self.addCleanup(self.close_server, self.control_server, self.control_thread)
        self.upstream_payloads = []
        self.call = {"id": "synthetic-http-call", "type": "function", "function": {
            "name": "workspace_shell", "arguments": '{"command":"synthetic-no-execution"}'}}
        self.app = GatewayApplication(
            replace(config(), control_url=f"http://127.0.0.1:{self.control_server.server_port}", host_token=self.token,
                    context_layout="anchored-v1"),
            upstream=httpx.Client(transport=httpx.MockTransport(self.upstream), trust_env=False),
        )
        self.addCleanup(self.app.upstream.close)
        self.addCleanup(self.app.control.client.close)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        self.server.application = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server, self.server, self.thread)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def close_server(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    def upstream(self, request):
        self.upstream_payloads.append(json.loads(request.content))
        message = ({"role": "assistant", "content": None, "tool_calls": [self.call]}
                   if len(self.upstream_payloads) == 1 else {"role": "assistant", "content": "synthetic response"})
        return httpx.Response(200, json={"choices": [{"index": 0, "message": message,
                               "finish_reason": "tool_calls" if len(self.upstream_payloads) == 1 else "stop"}]})

    def post(self, messages):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions", method="POST",
            data=json.dumps({"model": config().public_model, "messages": messages,
                             "tools": legacy_gateway.GatewayTests.bound_tools()}).encode(),
            headers={"Authorization": "Bearer " + config().gateway_token,
                     "Content-Type": "application/json", "X-ST-Thread-ID": "synthetic-http-thread"},
        )
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            with error:
                self.fail(f"synthetic gateway HTTP failed: {error.code} {error.read().decode()}")
        with response:
            self.assertEqual(200, response.status)
            return json.loads(response.read())

    def test_three_human_wakes_and_tool_continuation_use_real_authenticated_bundle(self):
        messages = [{"role": "system", "content": "Synthetic frontend preferences."},
                    {"role": "user", "content": "Earlier human text."},
                    {"role": "assistant", "content": "Earlier answer."},
                    {"role": "user", "content": "你还记得我们之前读的侍魔嘛？"}]
        first = self.post(messages)
        session = self.app._current_session
        self.assertIsNotNone(session.context_bundle["dynamic_message"])
        stable = json.loads(session.context_bundle["stable_message"]["content"])
        dynamic = json.loads(session.context_bundle["dynamic_message"]["content"])
        self.assertEqual(json.loads(session.message["content"]), {**stable, **dynamic})
        self.assertIn("learning_memory", dynamic)
        self.assertEqual(session.context_bundle["dynamic_message"], self.upstream_payloads[0]["messages"][-2])
        wake_ids = [session.wake_id]
        messages.extend([first["choices"][0]["message"],
                         {"role": "tool", "tool_call_id": self.call["id"], "content": '{"synthetic":true}'}])
        answer = self.post(messages)
        self.assertEqual(self.upstream_payloads[0]["messages"], self.upstream_payloads[1]["messages"][:-2])
        self.assertIsNone(self.app._current_session)
        for number in (2, 3):
            messages.extend([answer["choices"][0]["message"],
                             {"role": "user", "content": "你还记得我们之前读的侍魔嘛？"}])
            answer = self.post(messages)
            self.assertIsNone(self.app._current_session)
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            snapshots = connection.execute(
                "SELECT wake_id,status,context_layout_json FROM brain_context_snapshots WHERE context_layout_json != '{}' ORDER BY rowid"
            ).fetchall()
        self.assertEqual(3, len(snapshots))
        self.assertEqual(3, len({row[0] for row in snapshots}))
        self.assertEqual(wake_ids[0], snapshots[0][0])
        self.assertEqual(["closed"] * 3, [row[1] for row in snapshots])
        self.assertEqual([3, 7, 9], [json.loads(row[2])["layout"]["human_message_index"] for row in snapshots])
        for payload in self.upstream_payloads:
            for forbidden in ("context_bundle", "initial_messages_digest", "wake_capability", self.token):
                self.assertNotIn(forbidden, json.dumps(payload))

    def test_failed_fresh_context_does_not_erase_existing_owner_or_block_next_wake(self):
        scope = {"owner_id": self.fixture.owner, "model_id": self.fixture.model}
        before_state = self.fixture.store.state(**scope)["state"]
        original = self.app.control.post
        def tamper(path, payload):
            result = original(path, payload)
            if path.endswith("/prepare"):
                result["context_hash"] = "0" * 64
            return result
        self.app.control.post = tamper
        payload = {"model": config().public_model, "messages": [{"role": "user", "content": "synthetic next question"}]}
        with self.assertRaises(GatewayError) as caught:
            self.app.prepare_turn(payload, {"X-ST-Thread-ID": "synthetic-http-thread"})
        self.assertEqual("st_context_hash_mismatch", caught.exception.code)
        self.assertIsNone(self.app._current_session)
        self.assertEqual(before_state, self.fixture.store.state(**scope)["state"])
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            status = connection.execute("SELECT status FROM brain_context_snapshots WHERE context_layout_json != '{}' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertEqual("closed", status)
        self.app.control.post = original
        recovered = self.app.prepare_turn(payload, {"X-ST-Thread-ID": "synthetic-http-thread"})
        self.assertIsNotNone(recovered.session.context_bundle)
        self.app.finish_turn(recovered, keep_for_tools=False)


class UsageSnapshotTests(unittest.TestCase):
    def record(self, perf):
        with self.assertLogs("stiller.rikkahub.performance", level="INFO") as captured:
            perf.emit()
        return json.loads(captured.records[0].getMessage())

    def test_utc_known_model_and_original_usage_snapshot(self):
        perf = _RequestPerformance()
        perf.note_model("deepseek-v4-flash-vision-exp")
        usage = {"prompt_tokens": 100, "completion_tokens": 2, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}
        perf.note_usage({"usage": usage})
        row = self.record(perf)
        self.assertEqual(usage, row["usage_snapshot"])
        self.assertTrue(row["usage_snapshot_complete"])
        self.assertTrue(row["usage_snapshot_consistent"])
        self.assertEqual(timezone.utc, datetime.fromisoformat(row["request_started_at_utc"].replace("Z", "+00:00")).tzinfo)

    def test_partial_usage_never_merges_into_fabricated_complete_snapshot(self):
        perf = _RequestPerformance()
        perf.note_usage({"usage": {"prompt_tokens": 100, "completion_tokens": 2, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}})
        perf.note_usage({"usage": {"completion_tokens": 3}})
        perf.note_usage({"choices": []})
        row = self.record(perf)
        self.assertEqual({"completion_tokens": 3}, row["usage_snapshot"])
        self.assertFalse(row["usage_snapshot_complete"])
        self.assertIsNone(row["usage_snapshot_consistent"])

    def test_secret_fields_invalid_values_unknown_model_never_logged(self):
        secret = "synthetic-private-value-no-log"
        perf = _RequestPerformance()
        perf.note_model(secret)
        perf.note_usage({"usage": {"prompt_tokens": secret, "completion_tokens": True,
                                  "prompt_cache_hit_tokens": -1, "prompt_cache_miss_tokens": 10_000_001,
                                  "private": secret}, "model": secret})
        row = self.record(perf)
        self.assertNotIn(secret, json.dumps(row))
        self.assertIsNone(row["model"])
        self.assertEqual({}, row["usage_snapshot"])
        self.assertEqual(4, len(row["usage_snapshot_invalid_fields"]))

    def test_inconsistent_usage_and_missing_usage_are_not_silently_fixed(self):
        empty = self.record(_RequestPerformance())
        self.assertFalse(empty["usage_observed"])
        self.assertIsNone(empty["prompt_tokens"])
        perf = _RequestPerformance()
        usage = {"prompt_tokens": 100, "completion_tokens": 0, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 99}
        perf.note_usage({"usage": usage})
        row = self.record(perf)
        self.assertEqual(usage, row["usage_snapshot"])
        self.assertFalse(row["usage_snapshot_consistent"])


if __name__ == "__main__":
    unittest.main()
