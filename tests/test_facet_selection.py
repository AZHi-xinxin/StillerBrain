"""Synthetic local facet matching; no user data, network or model requests."""
from copy import deepcopy
from contextlib import closing
import json
import sqlite3
import unittest
from unittest import mock

from runtime.facet_selection import append_facet_projection, rank_facet_candidates
from runtime.credential_guard import contains_credential_or_secret
from runtime.onboarding import (
    TAIL_CONTEXT_LAYOUT_CONTRACT, OPTIONAL_BRAIN_NOTICE, ModuleOneOnboardingStore,
    OnboardingError, _canonical, _estimate_tokens, _sha256,
)
from tests import test_onboarding as fixtures


FACETS = {
    "my-close-side": "面对亲近的人，我表达信任与关心，也坦诚自己的感受。",
    "my-public-side": "面对陌生人，我先了解对方，清楚地表达自己。",
    "my-quiet-side": "独处时，我安静地整理自己的想法。",
    "my-working-side": "工作与技术协作时，我重视证据，给出清楚的进展。",
    "my-gentle-side": "亲近的人难过时，我愿意安慰，陪着对方说完。",
    "my-uncertain-side": "拿不准时，我把不确定处说清楚，再寻找证据。",
    "my-awake-side": "刚醒来时，我先确认自己所处的情况，慢慢找回连续感。",
}


class FacetMatchingTests(unittest.TestCase):
    def test_seven_reported_scenes_use_custom_keys_and_chinese_bodies(self):
        cases = (
            ("来和亲近的人聊聊心里话。", "my-close-side"),
            ("现在要和陌生人初次见面。", "my-public-side"),
            ("现在你可以独处一会儿。", "my-quiet-side"),
            ("我们来调试这段代码。", "my-working-side"),
            ("我现在很难过，陪陪我。", "my-gentle-side"),
            ("你拿不准这件事，可以说出想法。", "my-uncertain-side"),
            ("你刚醒来，慢慢来。", "my-awake-side"),
        )
        for query, expected in cases:
            with self.subTest(query=query):
                self.assertIn(expected, rank_facet_candidates(FACETS, query))

    def test_english_key_can_supply_scene_hint_without_required_name(self):
        facets = {"with_close_ones": "我愿意自在地表达自己。", "working": "我习惯把步骤想清楚。"}
        self.assertEqual(["working"], rank_facet_candidates(facets, "现在来写代码吧。"))
        self.assertEqual(["with_close_ones"], rank_facet_candidates(facets, "现在和亲人聊聊。"))

    def test_custom_domain_phrase_works_without_scene_dictionary(self):
        facets = {"Z7": "星轨摄影时，我先理解光线和时间的关系。"}
        self.assertEqual(["Z7"], rank_facet_candidates(facets, "现在一起研究星轨摄影。"))

    def test_exact_key_and_stable_sort(self):
        facets = {"z-key": "我随时留意感受。", "a-key": "我随时留意感受。"}
        self.assertEqual(["z-key"], rank_facet_candidates(facets, "使用 z-key。"))
        self.assertEqual(["a-key", "z-key"], rank_facet_candidates(facets, "我随时留意感受。"))

    def test_no_match_does_not_include_everything_or_pick_default(self):
        for query in ("", "今天的早餐多少钱？", "abcxyz", "这个陌生场景尚无同义词桥接"):
            with self.subTest(query=query):
                self.assertEqual([], rank_facet_candidates({"opaque": "我在北极星测量中重视角度。"}, query))

    def test_past_future_negated_hypothetical_and_quoted_scenes_are_not_current(self):
        for query in (
            "昨天我们工作。", "明天我们工作。", "如果你工作会怎样？", "我没在工作。",
            "我不难过。", "他说他在工作。", "原文写着工作时要认真。", "“我在工作”。",
            '"我在工作"', "‘我在工作’", "'working'", "```我在工作```", "`working`", "I am not working.",
            "yesterday I was working.",
        ):
            with self.subTest(query=query):
                self.assertEqual([], rank_facet_candidates(FACETS, query))

    def test_clause_local_guard_retains_actual_current_clause(self):
        ranked = rank_facet_candidates(FACETS, "昨天我们工作，今天你可以独处一会儿。")
        self.assertIn("my-quiet-side", ranked)
        self.assertNotIn("my-working-side", ranked)

    def test_fixed_polite_requests_are_not_mistaken_for_negation(self):
        for query, expected in (
            ("能不能帮我调试代码？", "my-working-side"),
            ("可不可以陪我聊聊并安慰我？", "my-gentle-side"),
            ("不如我们现在一起工作。", "my-working-side"),
        ):
            with self.subTest(query=query):
                self.assertIn(expected, rank_facet_candidates(FACETS, query))

    def test_polite_request_exception_keeps_real_and_contrast_negations(self):
        for query in (
            "我没在工作。", "我不需要安慰。", "能不能帮我调试代码但我没在工作。",
            "能不能帮我调试代码，但我没在工作。", "可不可以陪我聊聊，不过我不需要安慰。",
            "不如我们工作，可是我不想工作。", "如果能不能帮我调试代码呢？",
            "明天可不可以一起工作？",
        ):
            with self.subTest(query=query):
                self.assertEqual([], rank_facet_candidates(FACETS, query))

    def test_projection_preserves_bodies_and_existing_memory_under_same_budget(self):
        original = deepcopy(FACETS)
        base = {"learning_memory": [{"summary": "已选中的普通记忆"}]}
        result = append_facet_projection(base, FACETS, "我很难过", estimate_tokens=_estimate_tokens)
        self.assertEqual(base["learning_memory"], result["learning_memory"])
        self.assertEqual(FACETS["my-gentle-side"], result["self_facets"]["facets"]["my-gentle-side"])
        self.assertEqual("ai_active_self_revision", result["self_facets"]["frame"]["authorship"])
        self.assertLessEqual(_estimate_tokens(result), 1200)
        self.assertEqual(original, FACETS)
        self.assertEqual({"learning_memory": [{"summary": "已选中的普通记忆"}]}, base)

    def test_oversized_whole_facet_skipped_and_three_maximum(self):
        facets = {f"working-{i}": "我在工作时保持专注。" for i in range(6)}
        result = append_facet_projection({}, facets, "工作", estimate_tokens=_estimate_tokens)
        self.assertEqual(3, len(result["self_facets"]["facets"]))
        facets = {"working-long": "我在工作。" + "专注" * 4000}
        self.assertEqual({}, append_facet_projection({}, facets, "工作", estimate_tokens=_estimate_tokens))
        base = {"existing": "长" * 1190}
        result = append_facet_projection(base, FACETS, "工作", estimate_tokens=_estimate_tokens)
        self.assertEqual(base, result)


class FacetContextIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ModuleOneOnboardingTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store
        self.scope = {"owner_id": self.fixture.owner, "model_id": self.fixture.model}
        content = fixtures.model_content()
        content["facets"] = deepcopy(FACETS)
        with mock.patch.object(fixtures, "model_content", return_value=content):
            self.fixture.bootstrap_live()

    def prepare(self, event, query, **options):
        wake = self.store.issue_wake(**self.scope, host_id="host:facets", thread_id="thread:facets",
                                     source_kind="human_message", source_event_id=event)
        args = {
            **self.scope, "wake_id": wake["wake_id"], "wake_capability": wake["wake_capability"],
            "source_digest": "synthetic:" + event, "host_contract_digest": "synthetic:facets",
            "source_frame": {"query_text": query},
            "context_layout_offer": {"contract": TAIL_CONTEXT_LAYOUT_CONTRACT, "insertion_rule": "after-client-messages"},
            **options,
        }
        return self.store.build_pre_generation_context(**args), args

    def test_default_selects_active_side_dynamically_and_stable_prefix_does_not_change(self):
        first, _ = self.prepare("working", "现在开始工作吧。")
        second, _ = self.prepare("sad", "我现在很难过。")
        one, two = first["context_bundle"], second["context_bundle"]
        self.assertEqual(one["stable_message"], two["stable_message"])
        self.assertEqual({}, json.loads(one["stable_message"]["content"])["facets"])
        self.assertIn("my-working-side", json.loads(one["dynamic_message"]["content"])["self_facets"]["facets"])
        self.assertIn("my-gentle-side", json.loads(two["dynamic_message"]["content"])["self_facets"]["facets"])
        for result in (first, second):
            dynamic = json.loads(result["context_bundle"]["dynamic_message"]["content"])
            self.assertLessEqual(_estimate_tokens(dynamic), 1200)

    def test_explicit_empty_and_named_host_selection_preserve_original_semantics(self):
        empty, _ = self.prepare("empty", "我们现在工作", facet_names=[])
        self.assertNotIn("self_facets", json.loads(empty["message"]["content"]))
        selected, _ = self.prepare("explicit", "我们现在工作", facet_names=["my-quiet-side", "missing"])
        body = json.loads(selected["message"]["content"])
        self.assertEqual({"my-quiet-side": FACETS["my-quiet-side"]}, body["facets"])
        self.assertNotIn("self_facets", body)

    def test_same_wake_snapshot_immutable_even_if_query_or_selection_changes(self):
        first, args = self.prepare("frozen", "我们工作吧。")
        reused = self.store.build_pre_generation_context(**{**args, "source_frame": {"query_text": "现在很难过"}, "facet_names": []})
        self.assertEqual("context_reused", reused["decision"])
        self.assertEqual(first["context_bundle"], reused["context_bundle"])
        self.assertEqual(first["context_hash"], reused["context_hash"])

    def test_unknown_scene_has_no_default_and_query_is_not_persisted(self):
        query = "未匹配的紫色风铃量子盐水问题-PRIVATE-SYNTHETIC"
        result, _ = self.prepare("unknown", query)
        self.assertNotIn("self_facets", json.loads(result["message"]["content"]))
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            rows = connection.execute("SELECT stable_json, dynamic_json FROM brain_context_snapshots").fetchall()
        self.assertNotIn("PRIVATE-SYNTHETIC", json.dumps(rows))

    def test_global_off_prevents_automatic_facets(self):
        self.store.injection_control_store.emergency_off(**self.scope, reason="合成测试暂停", wake_id="synthetic:off", wake_seq=100, expected_row_version=0)
        result, _ = self.prepare("disabled", "我们工作吧。")
        self.assertEqual({}, json.loads(result["message"]["content"]))

    def test_dynamic_facets_reach_existing_final_protected_value_gate(self):
        count = self.fixture.count("brain_context_snapshots")
        def reject_projection(value):
            return isinstance(value, dict) and "self_facets" in value.get("dynamic", {})
        with mock.patch("runtime.onboarding.contains_credential_or_secret", side_effect=reject_projection):
            with self.assertRaisesRegex(OnboardingError, "protected_persistence_value"):
                self.prepare("protected", "我们工作吧。")
        self.assertEqual(count, self.fixture.count("brain_context_snapshots"))

    def test_self_model_scope_off_alone_prevents_automatic_facets(self):
        control = self.store.injection_control_store
        changed = control.commit_revision(
            **self.scope, scope="self_model", operation="set", target_mode="hard_off",
            wake_id="synthetic:self-off", wake_seq=100, expected_row_version=0,
            expected_active_revision=None,
        )
        self.assertEqual("hard_off", changed["mode"])
        self.assertEqual("enabled", control.effective_mode(**self.scope, scope="global"))
        result, _ = self.prepare("self-disabled", "我们工作吧。")
        self.assertNotIn("self_facets", json.loads(result["message"]["content"]))
        self.assertNotIn("facets", json.loads(result["message"]["content"]))

    def test_before_initial_activation_independent_memory_does_not_unlock_facets(self):
        learning = mock.Mock()
        learning.build_envelopes.return_value = []
        store = ModuleOneOnboardingStore(
            self.fixture.database, capability_secret=self.store.capability_secret,
            ordinary_memory_independent=True, learning_store=learning,
        )
        scope = {"owner_id": "owner:synthetic-factory", "model_id": "model:synthetic-factory"}
        wake = store.issue_wake(**scope, host_id="host:factory", thread_id="thread:factory",
                               source_kind="human_message", source_event_id="factory-scene")
        with mock.patch.object(store, "_active_payload", side_effect=AssertionError("factory must not read active self")):
            result = store.build_pre_generation_context(
                **scope, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
                source_digest="synthetic:factory", host_contract_digest="synthetic:facets",
                source_frame={"query_text": "我们现在工作。"},
            )
        learning.build_envelopes.assert_called_once()
        self.assertEqual(OPTIONAL_BRAIN_NOTICE, result["message"]["content"])
        self.assertNotIn("self_facets", result["message"]["content"])

    def replace_synthetic_active_facets(self, facets):
        # Deliberately emulate a historically stored malformed/secret value in
        # this test's temporary database; no bypass is added to production code.
        active = self.store.self_store.active_revision(self.fixture.model)
        content = deepcopy(active["content"])
        content["facets"] = facets
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE self_model_revisions SET content_json=?,content_hash=? WHERE revision_id=?",
                (_canonical(content), _sha256(content), active["revision_id"]),
            )

    def test_auto_selected_invalid_key_hits_original_structure_gate(self):
        self.replace_synthetic_active_facets({"invalid key": "我在工作时保持专注。"})
        count = self.fixture.count("brain_context_snapshots")
        result, _ = self.prepare("bad-structure", "我们工作吧。")
        self.assertEqual("self_model_context_unavailable", result["decision"])
        self.assertEqual(["active_injection_structure_invalid"], result["reason_codes"])
        self.assertFalse(result["may_generate"])
        self.assertNotIn("invalid key", json.dumps(result))
        self.assertEqual(count, self.fixture.count("brain_context_snapshots"))

    def test_synthetic_secret_in_selected_body_is_blocked_by_real_final_guard(self):
        # This fabricated string has never been a usable credential. Its value
        # is not copied to error messages or technical reports.
        sentinel = "synthetic-facet-secret-never-issued-20260912"
        body = "我在工作时保持专注。API Key: " + sentinel
        self.assertTrue(contains_credential_or_secret(body))
        self.replace_synthetic_active_facets({"working": body})
        count = self.fixture.count("brain_context_snapshots")
        with mock.patch("runtime.onboarding.contains_credential_or_secret", wraps=contains_credential_or_secret) as guard:
            with self.assertRaisesRegex(OnboardingError, "protected_persistence_value") as caught:
                self.prepare("secret-final-gate", "我们工作吧。")
        self.assertTrue(any(isinstance(call.args[0], dict) and "self_facets" in call.args[0].get("dynamic", {})
                            for call in guard.call_args_list))
        self.assertNotIn(sentinel, str(caught.exception))
        self.assertEqual(count, self.fixture.count("brain_context_snapshots"))
        with self.store._connect() as connection:
            rows = connection.execute("SELECT stable_json,dynamic_json FROM brain_context_snapshots").fetchall()
        self.assertNotIn(sentinel, json.dumps([tuple(row) for row in rows]))


if __name__ == "__main__":
    unittest.main()
