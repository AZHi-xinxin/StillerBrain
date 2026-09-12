"""Synthetic cross-module budget and immutable-wake acceptance."""
from __future__ import annotations

from copy import deepcopy
import json
import unittest
from unittest import mock

from runtime.mixed_recall import _LEARNING_DETAIL_RULE, select_mixed_recall
from runtime.onboarding import _estimate_tokens
from runtime.planning_memory import PlanningMemoryStore
from tests import test_onboarding as onboarding_fixture
from tests import test_emotional_memory as emotional_fixture
from tests.test_planning_memory import calm, content


FRAME = {"instruction_authority": "none", "permission_authority": "none", "optional": True}


def plan(label: str, *, persistent: bool = False, score: float = .5, size: int = 100) -> dict:
    return {"item_ref": "plan://" + label + "@1", "kind": "task", "state": "active",
            "presence_mode": "persistent" if persistent else "relevant",
            "reminder": "原样提醒" + label, "selection_priority": 0 if persistent else 2,
            "semantic_score": score, "temporal_score": 0, "importance_score": .5,
            "content": {"summary": "测" * size}, "frame": deepcopy(FRAME),
            "detail_lookup": {"available": True, "tool": "recall_planning_memory", "ref": "plan://" + label + "@1"}}


def learning(label: str, *, score: float = .8, size: int = 100) -> dict:
    return {"item_ref": "learning://" + label + "@1", "semantic_score": score,
            "salience_score": .5, "presentation": "summary", "token_cost": size,
            "content": {"summary": "知" * size}, "gate_decision": "background_reference"}


def emotion(label: str, *, score: float = .8, size: int = 100) -> dict:
    return {"memory_id": label, "score": score, "presentation": "summary",
            "summary": "忆" * size, "origin_label": "reported", "confidence": 50}


class MixedRecallTests(unittest.TestCase):
    def select(self, *, plans=(), learns=(), tools=(), emotions=(), pins=(), ephemeral=(), base=None, **kwargs):
        return select_mixed_recall(
            base=base or {},
            planning={"contract": "planning-recall/0.1", "frame": deepcopy(FRAME), "envelopes": list(plans)},
            learning=list(learns), tool=list(tools),
            emotional={"contract": "emotional-recall/1", "frame": deepcopy(FRAME),
                       "memories": list(emotions), "pins": list(pins), "ephemeral": list(ephemeral)},
            estimate_tokens=_estimate_tokens, **kwargs)

    def test_more_relevant_emotion_is_not_starved_by_earlier_learning_module(self):
        result = self.select(learns=[learning("lower", score=.51, size=800)],
                             emotions=[emotion("higher", score=.95, size=800)])
        self.assertIn("emotional_memory", result)
        self.assertNotIn("learning_memory", result)
        self.assertLessEqual(_estimate_tokens(result), 1200)

    def test_more_relevant_learning_can_win_instead(self):
        result = self.select(learns=[learning("higher", score=.95, size=800)],
                             emotions=[emotion("lower", score=.51, size=800)])
        self.assertIn("learning_memory", result)
        self.assertNotIn("emotional_memory", result)

    def test_more_relevant_plan_wins_over_learning_and_reverses_with_scores(self):
        for plan_score, learning_score in ((.95, .51), (.51, .95)):
            result = self.select(plans=[plan("p", score=plan_score, size=800)],
                                 learns=[learning("l", score=learning_score, size=800)])
            self.assertEqual(plan_score > learning_score, "planning_memory" in result)
            self.assertEqual(learning_score > plan_score, "learning_memory" in result)

    def test_two_persistent_reminders_reserved_under_saturated_budget(self):
        first, second = plan("a", persistent=True, size=800), plan("b", persistent=True, size=800)
        result = self.select(plans=[first, second], base={"self_governance_profile": "规" * 350},
                             learns=[learning("crowded", size=800)], emotions=[emotion("crowded", size=800)])
        selected = result["planning_memory"]["envelopes"]
        self.assertEqual([first["item_ref"], second["item_ref"]], [item["item_ref"] for item in selected])
        self.assertEqual([first["reminder"], second["reminder"]], [item["content"]["reminder"] for item in selected])
        self.assertLessEqual(_estimate_tokens(result), 1200)
        self.assertNotIn("测" * 100, json.dumps(result, ensure_ascii=False))

    def test_gates_frames_and_inputs_are_not_rewritten(self):
        entry = emotion("private-hint", size=30)
        entry.update(presentation="neutral_hint", gate_decision="ask_first", confidence=23)
        before = deepcopy(entry)
        result = self.select(emotions=[entry])
        self.assertEqual(before, entry)
        self.assertEqual(before, result["emotional_memory"]["memories"][0])
        self.assertEqual(FRAME, result["emotional_memory"]["frame"])

    def test_no_candidate_means_no_fabricated_memory(self):
        self.assertEqual({}, self.select())

    def test_existing_reminder_only_pending_pin_notice_survives_if_space_remains(self):
        payload = {"contract": "emotional-recall/1", "frame": deepcopy(FRAME),
                   "pins": [], "memories": [], "ephemeral": [],
                   "reminder": "合成：现有待审申请提示，非候选审核证明。"}
        result = select_mixed_recall(base={}, planning=None, learning=[], tool=[],
            emotional=payload, estimate_tokens=_estimate_tokens)
        self.assertEqual(payload, result["emotional_memory"])
        full = select_mixed_recall(base={"reserved": "固" * 1190}, planning=None,
            learning=[], tool=[], emotional=payload, estimate_tokens=_estimate_tokens)
        self.assertNotIn("emotional_memory", full)

    def test_pending_fact_survives_when_all_genuine_emotion_items_exceed_budget(self):
        payload = {"contract": "emotional-recall/1", "frame": deepcopy(FRAME),
            "pins": [], "memories": [emotion("oversize", size=1700)], "ephemeral": [],
            "reminder": "有 1 条常驻申请等待跨唤醒复核。"}
        original = deepcopy(payload)
        result = select_mixed_recall(base={}, planning=None, learning=[], tool=[],
            emotional=payload, estimate_tokens=_estimate_tokens)
        self.assertEqual(payload["reminder"], result["emotional_memory"]["reminder"])
        self.assertFalse(result["emotional_memory"]["memories"])
        self.assertNotIn("忆" * 100, json.dumps(result, ensure_ascii=False))
        self.assertEqual(original, payload)
        self.assertLessEqual(_estimate_tokens(result), 1200)

    def test_overlarge_candidate_does_not_remove_other_fitting_candidates(self):
        result = self.select(learns=[learning("oversize", score=.99, size=1600), learning("small", score=.8)])
        self.assertEqual(["learning://small@1"], [item["item_ref"] for item in result["learning_memory"]["envelopes"]])

    def test_no_fixed_module_tie_order_or_input_mutation(self):
        entries = [learning("a", score=.8, size=750), learning("b", score=.8, size=750)]
        before = deepcopy(entries)
        self.assertEqual(self.select(learns=entries), self.select(learns=list(reversed(entries))))
        self.assertEqual(before, entries)

    def test_tool_token_and_count_cap_remain(self):
        entries = [learning(str(index), size=130) for index in range(3)]
        result = self.select(tools=entries)
        self.assertEqual(1, len(result["tool_guidance"]["envelopes"]))
        self.assertEqual("none", result["tool_guidance"]["frame"]["execution_authority"])

    def test_learning_lookup_teaching_is_once_and_exact_arguments_survive(self):
        entries = [learning(str(index), size=30) for index in range(3)]
        for item in entries:
            ref = item["item_ref"]
            item["detail_lookup"] = {"available": True, "tool": "recall_learning_memory",
                "ref": ref, "target_ref": ref, "arguments": {"target_ref": ref},
                "rule": _LEARNING_DETAIL_RULE}
            item["provenance_class"] = "reported"
            item["content"]["uncertainties"] = ["合成未验证内容"]
        before = deepcopy(entries)
        result = self.select(learns=entries)
        payload = result["learning_memory"]
        self.assertEqual(_LEARNING_DETAIL_RULE, payload["detail_lookup_rule"])
        by_ref = {item["item_ref"]: item for item in entries}
        for projected in payload["envelopes"]:
            original = by_ref[projected["item_ref"]]
            expected = deepcopy(original)
            for alias in ("ref", "target_ref", "rule"):
                del expected["detail_lookup"][alias]
            self.assertEqual(expected, projected)
        self.assertEqual(before, entries)
        self.assertEqual(1, json.dumps(result, ensure_ascii=False).count(_LEARNING_DETAIL_RULE))
        old_payload = {**payload, "envelopes": entries}
        del old_payload["detail_lookup_rule"]
        self.assertLess(_estimate_tokens(payload), _estimate_tokens(old_payload))

    def test_unknown_lookup_rule_is_not_rewritten_as_host_boilerplate(self):
        item = learning("unknown", size=30)
        item["detail_lookup"] = {"available": True, "tool": "recall_learning_memory",
            "ref": item["item_ref"], "arguments": {"target_ref": item["item_ref"]},
            "rule": "合成的不同规则，必须原样保留。"}
        result = self.select(learns=[item])
        self.assertEqual(item, result["learning_memory"]["envelopes"][0])
        self.assertNotIn("detail_lookup_rule", result["learning_memory"])

    def test_cross_module_equal_scores_use_the_same_tie_rule(self):
        entries = {"planning_memory": plan("p", score=.8, size=800),
                   "learning_memory": learning("l", score=.8, size=800),
                   "emotional_memory": emotion("e", score=.8, size=800)}
        for winner_module, winner in entries.items():
            with self.subTest(winner=winner_module), mock.patch(
                "runtime.mixed_recall._stable_key",
                side_effect=lambda item: "0" if item is winner else "1",
            ):
                result = self.select(plans=[entries["planning_memory"]],
                    learns=[entries["learning_memory"]],
                    emotions=[entries["emotional_memory"]])
                self.assertEqual({winner_module}, set(result))

    def test_disabled_scope_absence_cannot_be_recreated(self):
        result = self.select(learns=[learning("only-enabled")])
        self.assertEqual({"learning_memory"}, set(result))

    def test_invalid_scores_are_not_treated_as_high_priority(self):
        result = self.select(learns=[learning("nan", score=float("nan"), size=800)],
                             emotions=[emotion("valid", score=.8, size=800)])
        self.assertIn("emotional_memory", result)
        self.assertNotIn("learning_memory", result)


class EmotionalQuietProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = emotional_fixture.EmotionalMemoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_real_recall_has_content_but_no_generic_save_or_reaction_instruction(self):
        f = self.fixture
        f.remember("中性合成标记", original_text="合成材料测试记录。",
            summary="中性合成标记", entities=[])
        result = f.store.build_injection(owner_id=f.owner, model_id=f.model,
            query="中性合成标记", budget_tokens=1200)
        self.assertTrue(result["injection"]["memories"])
        self.assertFalse(result["injection"].get("reminder"))
        self.assertIn("frame", result["injection"])

    def test_real_pending_pin_fact_survives_without_fabricated_recall(self):
        f = self.fixture
        f.store.manage_pin(owner_id=f.owner, model_id=f.model,
            wake_id="synthetic-pin", wake_seq=10, expected_row_version=f.version(),
            action="request", reason="合成测试待审事实", pin_kind="safety_boundary",
            display_text="我会把人的安全与自主边界放在优先位置。",
            source_ref=f"self-model-revision://{f.active_revision_id}/behavioral_principles/0")
        before = f.store.status(owner_id=f.owner, model_id=f.model)
        payload = f.store.build_injection(owner_id=f.owner, model_id=f.model,
            query="zzzzzzz", budget_tokens=1200)["injection"]
        # The aggregate reminder can contain ordinary author-confirmed pins
        # and legacy wake-bound pins; each record describes its own mode.
        self.assertEqual("有 1 条常驻申请等待 AI 确认；具体确认方式见申请记录。", payload["reminder"])
        self.assertFalse(payload["memories"])
        result = select_mixed_recall(base={}, planning=None, learning=[], tool=[],
            emotional=payload, estimate_tokens=_estimate_tokens)
        self.assertEqual(payload, result["emotional_memory"])
        self.assertEqual(before, f.store.status(owner_id=f.owner, model_id=f.model))


class PersistentWakeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = onboarding_fixture.ModuleOneOnboardingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.bootstrap_live()
        self.store = self.fixture.store
        self.planning = PlanningMemoryStore(self.fixture.database)
        self.store.planning_store = self.planning
        self.ids = []
        for i in range(2):
            payload = content("中性常驻" + str(i), presence_mode="persistent")
            pending = self.planning.propose_create(
                owner_id=self.fixture.owner, model_id=self.fixture.model, wake_id=f"synthetic-create-{i}",
                wake_seq=100 + i * 2, expected_row_version=i * 2, content=payload,
                reason="synthetic acceptance", calm_check=calm(), ai_confirmation=True, idempotency_key=f"persistent-{i}")
            candidate = self.planning.present_pending_candidates(owner_id=self.fixture.owner,
                model_id=self.fixture.model, wake_id=f"synthetic-review-{i}", wake_seq=101 + i * 2)[0]
            accepted = self.planning.review_change(owner_id=self.fixture.owner, model_id=self.fixture.model,
                wake_id=f"synthetic-review-{i}", wake_seq=101 + i * 2,
                expected_row_version=pending["planning_row_version"], candidate_id=candidate["candidate_id"],
                expected_candidate_version=candidate["candidate_version"], expected_candidate_hash=candidate["candidate_hash"],
                expected_base_version=candidate["base_version"], decision="accept", correctness_assessment="synthetic only",
                calm_check=calm(), reason="synthetic acceptance", ai_confirmation=True)
            self.ids.append(accepted["plan_ref"])

    def prepare(self, event: str):
        wake = self.store.issue_wake(owner_id=self.fixture.owner, model_id=self.fixture.model,
            host_id="host:test", thread_id="thread:unrelated", source_kind="human_message", source_event_id=event)
        args = dict(owner_id=self.fixture.owner, model_id=self.fixture.model, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], source_digest=f"source:{event}", host_contract_digest="host-contract:v1",
            source_frame={"query_text": "zzzzzzzz", "prior_assistant_present": True, "lineage_stable": True,
                          "thread_id": "thread:unrelated", "source_event_id": event, "capture_items": []})
        return wake, args, self.store.build_pre_generation_context(**args)

    def test_two_actual_new_wakes_both_deliver_without_plan_state_change(self):
        before = self.planning.status(owner_id=self.fixture.owner, model_id=self.fixture.model)
        for i in range(2):
            wake, args, result = self.prepare(f"persistent-new-{i}")
            projected = json.loads(result["message"]["content"])
            self.assertEqual(set(self.ids), {item["item_ref"] for item in projected["planning_memory"]["envelopes"]})
            self.assertLessEqual(_estimate_tokens({"planning_memory": projected["planning_memory"]}), 1200)
            self.store.confirm_context_injected(owner_id=self.fixture.owner, model_id=self.fixture.model,
                wake_id=wake["wake_id"], wake_capability=wake["wake_capability"], context_hash=result["context_hash"])
            with mock.patch.object(self.planning, "build_injection", side_effect=AssertionError("must reuse snapshot")):
                reused = self.store.build_pre_generation_context(**args)
            self.assertEqual(result["context_hash"], reused["context_hash"])
            self.store.close_context_snapshot(owner_id=self.fixture.owner, model_id=self.fixture.model,
                wake_id=wake["wake_id"], wake_capability=wake["wake_capability"])
        self.assertEqual(before, self.planning.status(owner_id=self.fixture.owner, model_id=self.fixture.model))

    def test_global_off_does_not_leak_persistent(self):
        with mock.patch.object(self.store.injection_control_store, "effective_mode", return_value="hard_off"):
            _, _, result = self.prepare("persistent-disabled")
        self.assertNotIn("planning_memory", json.loads(result["message"]["content"]))

    def test_planning_scope_off_does_not_leak_persistent(self):
        def mode(**kwargs):
            return "hard_off" if kwargs["scope"] == "planning_memory" else "enabled"
        with mock.patch.object(self.store.injection_control_store, "effective_mode", side_effect=mode):
            _, _, result = self.prepare("planning-disabled")
        self.assertNotIn("planning_memory", json.loads(result["message"]["content"]))


if __name__ == "__main__":
    unittest.main()
