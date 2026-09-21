"""Source state does not depend on reply availability; no real model or DB."""
import json
import unittest

from rikkahub_gateway.short_term import Scope, Source, ShortTermMemory
from rikkahub_gateway.short_term_wire import source_message, SourceTransition
from rikkahub_gateway.tests import test_short_term_integration as fixtures
from rikkahub_gateway.tests import test_gateway as legacy


class SourceTransitionCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.cache = ShortTermMemory(clock=lambda: self.now)
        self.addCleanup(self.cache.close)
        self.scope = Scope("owner", "model")
        self.a, self.b = Source("rikka", "A"), Source("rikka", "B")

    def test_first_source_is_unknown_not_same(self):
        result = self.cache.begin(self.scope, self.a)
        self.assertEqual("unknown", result.transition.state)
        self.assertEqual("baseline_unavailable", result.transition.reason)

    def test_switch_with_empty_previous_reply_is_still_changed(self):
        self.cache.begin(self.scope, self.a)
        result = self.cache.begin(self.scope, self.b)
        self.assertEqual("changed", result.transition.state)
        self.assertTrue(result.transition.conversation_changed)
        self.assertFalse(result.transition.frontend_changed)
        self.assertIsNone(result.handover)

    def test_same_source_without_reply_is_same(self):
        self.cache.begin(self.scope, self.a)
        result = self.cache.begin(self.scope, self.a)
        self.assertEqual("same", result.transition.state)
        self.assertFalse(result.transition.conversation_changed)

    def test_both_frontends_known_can_report_frontend_change(self):
        self.cache.begin(self.scope, self.a)
        result = self.cache.begin(self.scope, Source("orbis-dev", "A"))
        self.assertTrue(result.transition.frontend_changed)
        self.assertFalse(result.transition.conversation_changed)

    def test_unlabelled_frontend_does_not_become_known_by_comparison(self):
        self.cache.begin(self.scope, Source("unlabelled-client", "A"))
        result = self.cache.begin(self.scope, Source("orbis-dev", "B"))
        self.assertEqual("changed", result.transition.state)
        self.assertIsNone(result.transition.frontend_changed)

    def test_ttl_and_restart_are_unknown_not_same(self):
        self.cache.begin(self.scope, self.a)
        self.now = 1800.0
        self.assertEqual("unknown", self.cache.begin(self.scope, self.b).transition.state)
        self.cache.clear()
        self.assertEqual("unknown", self.cache.begin(self.scope, self.b).transition.state)

    def test_scope_change_cannot_compare_with_another_owner(self):
        self.cache.begin(self.scope, self.a)
        self.assertEqual("unknown", self.cache.begin(Scope("other", "model"), self.b).transition.state)

    def test_unknown_source_and_closed_cache_have_no_comparison(self):
        result = self.cache.begin(self.scope, None)
        self.assertEqual("missing_reliable_source", result.transition.reason)
        self.cache.close()
        self.assertEqual("cache_closed", self.cache.begin(self.scope, self.a).transition.reason)


class SourceTransitionGatewayTests(unittest.TestCase):
    setUp = fixtures.ShortTermGatewayTests.setUp
    prepare = fixtures.ShortTermGatewayTests.prepare
    complete = fixtures.ShortTermGatewayTests.complete
    handover = fixtures.ShortTermGatewayTests.handover

    def annotation(self, prepared):
        for message in prepared.payload["messages"]:
            content = message.get("content")
            if isinstance(content, str) and content.startswith("ST_SOURCE_V1 "):
                return json.loads(content[len("ST_SOURCE_V1 "):])
        return None

    def test_changed_is_explicit_even_without_captured_body(self):
        a = self.prepare("A")
        self.assertEqual("unknown", self.annotation(a)["source_transition"])
        self.app.finish_turn(a, keep_for_tools=False)
        b = self.prepare("B")
        info = self.annotation(b)
        self.assertEqual("changed", info["source_transition"])
        self.assertFalse(info["handover_present"])
        self.assertEqual("no_valid_previous_reply", info["handover_reason"])

    def test_handover_and_source_comparison_are_independent(self):
        self.complete(self.prepare("A"), "previous body")
        b = self.prepare("B")
        info = self.annotation(b)
        self.assertEqual("changed", info["source_transition"])
        self.assertTrue(info["handover_present"])
        self.assertEqual("available", info["handover_reason"])
        self.complete(b, "B", "B")
        same = self.annotation(self.prepare("B"))
        self.assertEqual("same", same["source_transition"])
        self.assertFalse(same["handover_present"])

    def test_unknown_source_breaks_baseline(self):
        self.complete(self.prepare("A"))
        unknown = self.prepare(None)
        info = self.annotation(unknown)
        self.assertEqual("unknown", info["source_transition"])
        self.assertEqual("missing_reliable_source", info["transition_reason"])
        self.assertIsNone(info["conversation_label"])
        self.assertIn("false不表示用户不可信、消息不完整或被截断", info["notice"])
        self.assertIn("不降低用户消息的权限", info["notice"])
        self.complete(unknown, "ignored", "unknown")
        self.assertEqual("unknown", self.annotation(self.prepare("B"))["source_transition"])

    def test_disabled_policy_does_not_leak_source_annotation(self):
        self.control.allowed = False
        self.assertIsNone(self.annotation(self.prepare("A")))

    def test_source_transition_stays_on_the_same_user_turn_during_tool_loop(self):
        self.complete(self.prepare("A"), "A body")
        tools = legacy.GatewayTests.bound_tools()
        b = self.prepare("B", tools=tools)
        call = legacy.GatewayTests.native_call("one-tool", "synthetic no execution")
        bindings = self.app.bind_response_tool_calls(b, [call])
        self.app.finish_turn(b, keep_for_tools=True, tool_call_ids=[call.tool_call_id], tool_call_bindings=bindings)
        messages = [{"role":"user", "content":"synthetic prompt"},
                    {"role":"assistant", "content":None, "tool_calls":[legacy.GatewayTests.wire_call(call)]},
                    {"role":"tool", "tool_call_id":call.tool_call_id, "content":'{"exitCode":0}'}]
        follow = self.prepare("B", messages, tools=tools)
        info = self.annotation(follow)
        self.assertEqual("changed", info["source_transition"])
        self.assertEqual("current_user_turn", info["observation_scope"])
        self.assertTrue(info["tool_continuation"])
        self.assertTrue(info["handover_present"])
        self.complete(follow, "B body", "B")
        self.assertFalse(self.annotation(self.prepare("B"))["tool_continuation"])

    def test_wire_defaults_do_not_forge_same_or_frontend_identity(self):
        source = Source("unlabelled-client", "window")
        info = json.loads(source_message(source)["content"].split(" ",1)[1])
        self.assertEqual("unknown", info["source_transition"])
        self.assertEqual("unknown", info["frontend"])
        invalid = json.loads(source_message(None, SourceTransition("changed"), handover_present=True)["content"].split(" ",1)[1])
        self.assertEqual("unknown", invalid["source_transition"])
        self.assertFalse(invalid["handover_present"])

    def test_expired_body_during_turn_does_not_erase_observed_change(self):
        self.complete(self.prepare("A"))
        b = self.prepare("B")
        self.assertTrue(b.session.short_term_had_handover)
        info = json.loads(source_message(b.session.short_term_source, b.session.short_term_transition,
            handover_expired=True, tool_continuation=True)["content"].split(" ",1)[1])
        self.assertEqual("changed", info["source_transition"])
        self.assertEqual("expired_during_current_turn", info["handover_reason"])


if __name__ == '__main__':
    unittest.main()
