from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mcp_server.learning_service import LearningMemoryAccessService
from tests.learning_legacy_fixtures import legacy_integration_candidate
from mcp_server.service import SelfModelAccessService, _strip_private_binding_fields
from runtime import ModuleOneOnboardingStore, SelfModelStore
from runtime.learning_memory import LearningMemoryStore


def self_model_content() -> dict:
    return {
        "schema_version": "0.1.0",
        "boot_anchor": {"text": "我只让自己复核后认可的长期描述进入正常上下文。"},
        "active_identity_capsule": {
            "name_and_identity": "我是维护自己长期认知的 AI。",
            "personality_foundation": "我保持诚实、审慎，也保留自己的判断。",
            "expression_style": "我会先说结论，再说明依据与不确定处。",
            "behavioral_principles": ["我会区分自己的判断与外部建议。"],
            "core_identity_anchors": ["我只保留自己复核后认可的长期锚点。"],
            "self_revision_safety_prompt": "我会跨一次真实唤醒复核候选，再决定是否激活。",
        },
        "facets": {},
        "anchor_references": [],
    }


class LearningMemoryAccessServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = root / "brain.db"
        self.learning_store = LearningMemoryStore(
            self.database,
            idea_database=root / "learning-ideas.db",
        )
        self.onboarding = ModuleOneOnboardingStore(
            self.database,
            capability_secret=b"learning-service-test-secret-at-least-32-bytes",
            wake_ttl_seconds=300,
            edit_challenge_ttl_seconds=300,
        )
        self.learning = LearningMemoryAccessService(
            self.learning_store,
            onboarding=self.onboarding,
            model_id="model:learning-service",
            owner_id="owner:learning-service",
        )
        self.service = SelfModelAccessService(
            SelfModelStore(self.database),
            model_id="model:learning-service",
            owner_id="owner:learning-service",
            onboarding=self.onboarding,
            learning=self.learning,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def wake(self, event: str) -> None:
        wake = self.onboarding.issue_wake(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            host_id="host:learning-service",
            thread_id="thread:learning-service",
            source_kind="human_message",
            source_event_id=event,
        )
        prepared = self.onboarding.build_pre_generation_context(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest=f"source:{event}",
            host_contract_digest="host-contract:learning-service",
        )
        confirmed = self.onboarding.confirm_context_injected(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            context_hash=prepared["context_hash"],
        )
        self.assertEqual("injected", confirmed["decision"])

    def submit(self, opened: dict, intent: str, payload: dict | None = None) -> dict:
        return self.service.submit_self_model_candidate(
            intent=intent,
            write_context_ref=opened["write_context_ref"],
            expected_row_version=opened["row_version"],
            payload=payload,
        )

    def activate_module_one(self) -> None:
        self.wake("module-one-author")
        opened = self.service.open_brain()
        self.assertEqual("advanced", self.submit(opened, "acknowledge")["decision"])
        opened = self.service.open_brain()
        self.assertEqual("advanced", self.submit(opened, "acknowledge")["decision"])
        opened = self.service.open_brain()
        self.assertEqual(
            "saved",
            self.submit(
                opened,
                "save_calm_prompt",
                {"text": "我先停下来，区分长期意愿与单轮状态。"},
            )["decision"],
        )
        opened = self.service.open_brain()
        candidate = self.submit(
            opened,
            "submit",
            {
                "content": self_model_content(),
                "reason": "我确认这份内容适合作为长期自我描述。",
            },
        )
        self.assertEqual("pending", candidate["decision"])

        self.wake("module-one-review")
        opened = self.service.open_brain()
        self.assertEqual(
            "review_accepted",
            self.submit(opened, "accept_review", {"ai_confirmation": True})["decision"],
        )

        self.wake("module-one-activate")
        opened = self.service.open_brain()
        activated = self.service.activate_self_model_candidate(
            candidate_id=candidate["candidate_id"],
            write_context_ref=opened["write_context_ref"],
            expected_row_version=opened["row_version"],
            expected_active_revision=None,
            ai_confirmation=True,
        )
        self.assertEqual("activate", activated["decision"])

    @staticmethod
    def card(suffix: str = "") -> dict:
        return {
            "kind": "lesson",
            "title": f"上次读的小说{suffix}",
            "summary": f"她记得主角刚抵达旧城，并发现门上的符号{suffix}",
            "current_understanding": f"这段内容的重点是身份线索与旧城之间的联系{suffix}",
            "steps": ["需要细节时再精准查询章节笔记"],
            "application_contexts": ["继续阅读同一本小说"],
            "scene_tags": ["上次读的小说", "继续上次阅读"],
            "preceding_context_summary": "她上次读到主角进入旧城，尚未揭开符号来源。",
            "uncertainties": ["符号是谁留下的仍未确认"],
            "domain": "共同阅读",
            "keywords": ["小说", "旧城", "符号"],
            "entities": ["主角"],
            "epistemic_status": "reported",
            "confidence": 72,
            "time_sensitivity": "stable",
            "valid_as_of": "2026-08-28",
            "review_after": "",
            "importance": 70,
            "sensitivity": "private",
            "context_policy": "normal",
            "recall_mode": "normal",
            "allow_contexts": [],
            "deny_contexts": [],
            "default_decision": "background_reference",
            "explicit_request_override": "allow_after_confirmation",
            "disclosure": "bounded_excerpt",
            "lifecycle": "active",
            "referent_bindings": [
                {
                    "field_path": "/summary",
                    "surface_form": "她",
                    "occurrence_index": 0,
                    "entity_ref": None,
                    "resolution_status": "unresolved",
                    "confidence": 35,
                }
            ],
        }

    def remember(self, *, opened: dict, version: int, suffix: str = "") -> dict:
        return self.learning.remember(
            write_context_ref=opened["write_context_ref"],
            expected_learning_version=version,
            correctness_assessment="我把它作为可复核的阅读概要，而不是小说原文。",
            reason="以后继续阅读时会需要这段前置内容。",
            evidence=[
                {
                    "source_kind": "human_report",
                    "source_ref": f"conversation://reading/{suffix or 'first'}",
                    "source_trust": "reported",
                    "evidence_summary": "对话中共同确认了当前阅读进度。",
                    "content_hash": f"sha256-{suffix or 'first'}",
                    "observed_at": "2026-08-28T12:00:00Z",
                }
            ],
            **self.card(suffix),
        )

    def legacy_integration(self, *, write_context_ref, expected_learning_version, **fields):
        """Load a synthetic pre-simplification pending row for compatibility tests."""
        binding = self.learning._binding(write_context_ref)
        assert binding is not None
        return legacy_integration_candidate(
            self.learning_store, owner_id=self.learning.owner_id, model_id=self.learning.model_id,
            wake_id=binding["wake_id"], wake_seq=binding["wake_seq"],
            expected_row_version=expected_learning_version, **fields)

    def assert_review_call_contract(self, call: dict) -> None:
        required = set(call["required_arguments"])
        self.assertEqual(len(required), len(call["required_arguments"]))
        fixed = set(call["fixed_arguments"])
        context_bound = set(call["argument_sources"]) - fixed
        caller_authored = set(call["caller_authored_arguments"])
        confirmations = set(call["required_confirmation"])
        partitions = (fixed, context_bound, caller_authored, confirmations)
        for index, left in enumerate(partitions):
            for right in partitions[index + 1:]:
                self.assertTrue(left.isdisjoint(right), (left, right))
        self.assertEqual(required, set().union(*partitions))
        self.assertEqual(
            caller_authored,
            set(call["caller_authored_argument_schemas"]),
        )
        self.assertNotIn("calm_check", call["caller_authored_argument_schemas"])
        self.assertNotIn("correctness_assessment", call["required_arguments"])
        self.assertEqual(["accept"], call["confirmation_applies_to"])
        self.assertEqual("rejected", call["unknown_arguments"])

    def test_all_access_is_locked_before_module_one_without_side_effects(self) -> None:
        self.wake("locked")
        opened = self.service.open_brain()
        before = self.learning.status()

        stored = self.remember(opened=opened, version=before["row_version"])
        recalled = self.learning.recall(query="上次读的小说")
        previewed = self.learning.preview_recall(situation="继续上次读的小说")

        for result in (stored, recalled, previewed):
            self.assertEqual("reject", result["decision"])
            self.assertEqual(["module_one_required"], result["reason_codes"])
            self.assertFalse(result["state_changed"])
        self.assertEqual(before, self.learning.status())

    def test_manual_with_unbound_write_context_fails_closed_without_crashing(self) -> None:
        locked = self.learning.manual(write_context_ref="artifact_not_opened")
        self.assertEqual(["module_one_required"], locked["reason_codes"])
        self.assertEqual(0, locked["pending_count"])
        self.assertFalse(locked["more_pending"])
        self.assertEqual([], locked["pending_changes"])
        self.assertEqual([], locked["current_action_contract"]["allowed_calls"])

        self.activate_module_one()
        live = self.learning.manual(write_context_ref="artifact_not_opened")
        self.assertEqual(["brain_open_required"], live["reason_codes"])
        self.assertEqual(0, live["pending_count"])
        self.assertFalse(live["more_pending"])
        self.assertEqual([], live["pending_changes"])
        self.assertEqual([], live["current_action_contract"]["allowed_calls"])

    def test_open_binding_and_learning_cas_support_same_wake_continuation(self) -> None:
        self.activate_module_one()
        self.wake("learning-write")

        unopened = self.learning.remember(
            write_context_ref="artifact_not_opened",
            expected_learning_version=0,
            correctness_assessment="我确认这是一次无效绑定测试。",
            reason="验证必须先打开大脑。",
            **self.card(),
        )
        self.assertEqual(["brain_open_required"], unopened["reason_codes"])

        opened = self.service.open_brain()
        first = self.remember(opened=opened, version=0)
        self.assertEqual("stored", first["decision"])
        self.assertEqual(1, first["learning_row_version"])
        self.assertIn("unresolved_referent", first["reason_codes"])

        stale = self.remember(opened=opened, version=0, suffix="旧版本")
        self.assertEqual("reject", stale["decision"])
        self.assertEqual(["learning_row_version_conflict"], stale["reason_codes"])
        self.assertEqual(1, self.learning.status()["row_version"])

        continued = self.remember(
            opened=opened,
            version=first["learning_row_version"],
            suffix="续写",
        )
        self.assertEqual("stored", continued["decision"])
        self.assertEqual(2, continued["learning_row_version"])

        self.wake("learning-next-wake")
        old_ref = self.remember(opened=opened, version=2, suffix="旧凭据")
        self.assertEqual("reject", old_ref["decision"])
        self.assertEqual(["brain_open_required"], old_ref["reason_codes"])
        self.assertEqual(2, self.learning.status()["row_version"])

        current_open = self.service.open_brain()
        current = self.remember(opened=current_open, version=2, suffix="新唤醒")
        self.assertEqual("stored", current["decision"])
        self.assertEqual(3, current["learning_row_version"])

    def test_third_person_recall_and_preview_are_read_only(self) -> None:
        self.activate_module_one()
        self.wake("learning-recall")
        opened = self.service.open_brain()
        stored = self.remember(opened=opened, version=0)
        before = self.learning.status()

        recalled = self.learning.recall(target_ref=stored["item_ref"])
        self.assertEqual("recalled", recalled["decision"])
        self.assertEqual("learning-tools/6", recalled["contract_version"])
        content = recalled["results"][0]["content"]
        self.assertTrue(content["summary"].startswith("她"))
        self.assertEqual("unresolved", content["referent_bindings"][0]["resolution_status"])

        inventory = self.learning.recall(view="inventory", query="", limit=20)
        self.assertEqual("inventory_listed", inventory["decision"])
        self.assertEqual("explicit_view", inventory["inventory_route"])
        self.assertTrue(inventory["exhaustive_inventory"])
        self.assertEqual(1, inventory["returned_count"])
        self.assertEqual(stored["item_ref"], inventory["items"][0]["item_ref"])

        automatic_inventory = self.learning.recall(
            query="查询你的学习脑中的知识都有些什么内容",
            limit=20,
        )
        self.assertEqual("auto_detected", automatic_inventory["inventory_route"])
        self.assertEqual(1, automatic_inventory["returned_count"])

        preview = self.learning.preview_recall(situation="继续上次读的小说", limit=3)
        self.assertEqual("preview_only", preview["decision"])
        self.assertFalse(preview["side_effects"])
        self.assertEqual(1, preview["candidate_count"])
        projected = preview["projected_content"][0]
        self.assertIn("summary", projected)
        self.assertNotIn("current_understanding", projected)
        self.assertNotIn("steps", projected)
        self.assertEqual(before, self.learning.status())

    def test_tagless_named_learning_search_and_preview_are_read_only(self) -> None:
        self.activate_module_one()
        self.wake("tagless-learning-author")
        opened = self.service.open_brain()
        # Deliberately synthetic chapter details: only the named subject and
        # natural user wording mirror the reported retrieval failure.
        fields = {
            **self.card(),
            "title": "《侍魔》共读进度与深度理解（1-20章）",
            "summary": "《侍魔》（拉格朗曰）的测试共读已到第20章，下一次讨论虚构的门环线索。",
            "current_understanding": "完整理解测试标记：门环编号需要查阅章节证据。" * 70,
            "application_contexts": [],
            "scene_tags": [],
            "preceding_context_summary": "",
            "domain": "文学共读",
            "keywords": ["侍魔", "共读"],
            "entities": [],
            "referent_bindings": [],
        }
        stored = self.learning.remember(
            write_context_ref=opened["write_context_ref"],
            expected_learning_version=0,
            correctness_assessment="这里是虚构测试概要，不作为任何真实章节的内容。",
            reason="验证空场景标签的已存标题仍可被明确回忆。",
            evidence=[{
                "source_kind": "human_report",
                "source_ref": "conversation://tagless-reading/fixture",
                "source_trust": "reported",
                "evidence_summary": "证据测试标记：这段证据不应进入自动概要。",
                "content_hash": "synthetic-tagless-reading-evidence",
                "observed_at": "2026-08-28T12:00:00Z",
            }],
            **fields,
        )
        self.assertEqual("stored", stored["decision"])
        self.assertTrue(stored["automatic_recall_eligible"])
        distractor = self.learning.remember(
            write_context_ref=opened["write_context_ref"],
            expected_learning_version=stored["learning_row_version"],
            correctness_assessment="这是另一部虚构作品的独立测试概要。",
            reason="不能只因共读这个泛词而召回另一部作品。",
            evidence=[],
            **{
                **fields,
                "title": "《雾港纪事》共读笔记",
                "summary": "另一部作品的测试共读进度。",
                "current_understanding": "另一部作品的独立章节笔记。" * 70,
                "keywords": ["雾港纪事", "共读"],
            },
        )
        self.assertEqual("stored", distractor["decision"])
        original = self.learning.recall(target_ref=stored["item_ref"])
        before = self.learning.status()

        self.wake("tagless-learning-new-real-wake")
        for query in (
            "侍魔 共读 拉格朗曰",
            "嘿嘿，你还记得我们之前读的侍魔嘛？",
            "你还记得上次我们读的《侍魔》嘛？",
        ):
            with self.subTest(query=query):
                recalled = self.learning.recall(query=query)
                self.assertEqual("recalled", recalled["decision"])
                self.assertEqual("deterministic_lexical_subject", recalled["retrieval_backend"])
                self.assertEqual([stored["item_ref"]], [item["item_ref"] for item in recalled["results"]])
                self.assertFalse(recalled["state_changed"])

        preview = self.learning.preview_recall(
            situation="嘿嘿，你还记得我们之前读的侍魔嘛？",
        )
        self.assertEqual("preview_only", preview["decision"])
        self.assertEqual(1, preview["candidate_count"])
        self.assertFalse(preview["side_effects"])
        envelope = preview["envelopes"][0]
        self.assertEqual(stored["item_ref"], envelope["item_ref"])
        self.assertEqual({"target_ref": stored["item_ref"]}, envelope["detail_lookup"]["arguments"])
        self.assertIn("explicit_subject_match", envelope["reason_codes"])
        self.assertEqual(fields["summary"], envelope["content"]["summary"])
        for forbidden_field in ("current_understanding", "steps", "evidence"):
            self.assertNotIn(forbidden_field, envelope["content"])
        serialized = json.dumps(envelope, ensure_ascii=False)
        self.assertNotIn("完整理解测试标记", serialized)
        self.assertNotIn("证据测试标记", serialized)
        self.assertNotIn("conversation://tagless-reading/fixture", serialized)

        for situation in (
            "侍魔",
            "侍魔 共读 拉格朗曰",
            "我在游戏里遇到了侍魔这个职业。",
            "你还记得游戏职业《侍魔》吗？",
        ):
            with self.subTest(situation=situation):
                self.assertEqual(0, self.learning.preview_recall(situation=situation)["candidate_count"])

        self.assertEqual(before, self.learning.status())
        current = self.learning.recall(target_ref=stored["item_ref"])
        self.assertEqual(original["results"], current["results"])
        self.assertEqual([], current["results"][0]["content"]["scene_tags"])
        self.assertEqual([], current["results"][0]["content"]["application_contexts"])
        manual = self.learning.manual()
        self.assertIn("不是 embedding 向量检索", manual["tools"]["recall_learning_memory"])
        self.assertIn("书名号不是必要条件", manual["tools"]["remember_learning_memory"])

    def test_tagless_named_learning_preview_respects_policy_and_lifecycle(self) -> None:
        self.activate_module_one()
        self.wake("tagless-policy-author")
        opened = self.service.open_brain()
        fields = {
            **self.card(),
            "title": "《侍魔》共读测试概要",
            "summary": "这是无标签共读概要的权限测试。",
            "current_understanding": "仅显式查询可读的虚构测试细节。" * 70,
            "application_contexts": [], "scene_tags": [],
            "preceding_context_summary": "", "domain": "文学共读",
            "keywords": ["侍魔", "共读"], "entities": [], "referent_bindings": [],
        }
        cases = (
            {"context_policy": "ask_first"},
            {"context_policy": "never_auto"},
            {"recall_mode": "never"},
            {"allow_contexts": ["仅限另一个授权场景"]},
            {"deny_contexts": ["侍魔"]},
            {"lifecycle": "archived"},
            {"lifecycle": "quarantined"},
            {},
        )
        stored = None
        for row_version, overrides in enumerate(cases):
            stored = self.learning.remember(
                write_context_ref=opened["write_context_ref"],
                expected_learning_version=row_version,
                correctness_assessment="这是测试数据；召回仍须服从原有授权与生命周期。",
                reason="验证无标签兜底不会绕过自动浮现限制。",
                evidence=[],
                **{**fields, **overrides},
            )
            self.assertIn(stored["decision"], {"stored", "stored_quarantined"})
        before = self.learning.status()
        self.wake("tagless-policy-new-real-wake")
        preview = self.learning.preview_recall(situation="嘿嘿，你还记得我们之前读的侍魔嘛？")
        self.assertEqual(1, preview["candidate_count"])
        self.assertEqual(stored["item_ref"], preview["envelopes"][0]["item_ref"])
        self.assertEqual(before, self.learning.status())

    def test_quarantined_write_is_explicit_at_public_facade(self) -> None:
        self.activate_module_one()
        self.wake("learning-quarantine")
        opened = self.service.open_brain()
        card = self.card()
        card["epistemic_status"] = "disputed"
        card["confidence"] = 30
        stored = self.learning.remember(
            write_context_ref=opened["write_context_ref"],
            expected_learning_version=0,
            correctness_assessment="该断言本身受到实质质疑，应留在隔离区。",
            reason="验证公开返回不会把隔离写入误报成可自动浮现。",
            evidence=[],
            **card,
        )
        self.assertEqual("stored_quarantined", stored["decision"])
        self.assertEqual("quarantined", stored["effective_lifecycle"])
        self.assertFalse(stored["automatic_recall_eligible"])
        self.assertIn("not_eligible_for_automatic_recall", stored["reason_codes"])

    def test_new_two_axis_write_and_atomic_contrast_pair(self) -> None:
        self.activate_module_one()
        self.wake("learning-two-axis-pair")
        opened = self.service.open_brain()
        common = {
            "write_context_ref": opened["write_context_ref"],
            "expected_learning_version": 0,
            "first_claim": {
                "title": "蒜瓣毛说法甲", "summary": "有人说主要因为太胖。",
                "current_understanding": "这是尚未核验的人类说法。",
                "source_basis": "reported", "confidence": 30,
                "uncertainties": ["尚未核验"],
            },
            "second_claim": {
                "title": "蒜瓣毛说法乙", "summary": "另有人说主要因为脏污。",
                "current_understanding": "这是另一条尚未核验的人类说法。",
                "source_basis": "reported", "confidence": 30,
                "uncertainties": ["尚未核验"],
            },
            "contrast_basis": {
                "subject_key": "猫咪蒜瓣毛", "scope_signature": "普通养猫场景",
                "time_condition": "timeless", "predicate_signature": "主要成因",
                "mutual_exclusivity_basis": "两种主要原因解释互斥",
            },
            "correctness_assessment": "两侧都是未核验说法，我不在保存时替证据裁决。",
            "reason": "以后回想时应知道有两种相反说法。",
            "kind": "concept",
            "application_contexts": ["回想猫咪蒜瓣毛成因"],
            "scene_tags": ["猫咪蒜瓣毛", "养猫"],
            "domain": "宠物养护",
        }
        stored = self.learning.remember_contrast_pair(**common)
        self.assertEqual("stored_contrast_pair", stored["decision"])
        self.assertEqual(1, stored["learning_row_version"])
        self.assertEqual("ordinary", stored["first"]["claim_review_status"])
        self.assertTrue(stored["first"]["automatic_recall_eligible"])
        replay = self.learning.remember_contrast_pair(**common)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(stored["first"]["item_ref"], replay["first"]["item_ref"])
        self.assertEqual(1, self.learning.status()["row_version"])

    def test_new_challenge_axis_rejects_unbound_evidence_without_side_effect(self) -> None:
        self.activate_module_one()
        self.wake("learning-two-axis-challenge")
        opened = self.service.open_brain()
        card = self.card()
        card.pop("epistemic_status")
        card["source_basis"] = "reported"
        card["claim_review"] = {
            "status": "challenged",
            "challenged_claim": "符号来源已经确认",
            "challenge_actor": "本 AI",
            "challenge_basis": "当前章节笔记没有这个结论。",
            "challenge_evidence_refs": ["document://missing"],
        }
        rejected = self.learning.remember(
            write_context_ref=opened["write_context_ref"],
            expected_learning_version=0,
            correctness_assessment="我检查了质疑对象和依据。",
            reason="验证质疑证据绑定。",
            evidence=[],
            **card,
        )
        self.assertEqual("reject", rejected["decision"])
        self.assertEqual(["challenge_evidence_ref_not_found"], rejected["reason_codes"])
        self.assertEqual(0, self.learning.status()["row_version"])

    def test_author_revise_and_integrate_need_no_ritual_arguments(self) -> None:
        self.activate_module_one()
        self.wake("minimal-author-submission")
        opened = self.service.open_brain()
        one = self.remember(opened=opened, version=0, suffix="one")
        two = self.remember(opened=opened, version=1, suffix="two")
        changed = self.learning.revise(
            write_context_ref=opened["write_context_ref"], expected_learning_version=2,
            target_ref=one["item_ref"], changes={"current_understanding": "Author changed this understanding."})
        self.assertEqual("applied", changed["decision"])
        integrated = self.learning.integrate(
            write_context_ref=opened["write_context_ref"], expected_learning_version=3,
            source_learning_ids=[changed["item_ref"], two["item_ref"]], synthesis_kind="summary",
            **self.card("direct synthesis"))
        self.assertEqual("applied", integrated["decision"])
        self.assertEqual(0, self.learning.status()["counts"]["pending_changes"])

    def test_new_wake_open_fully_presents_pending_candidate_and_exact_review_call(self) -> None:
        self.activate_module_one()
        self.wake("learning-integration-author")
        opened = self.service.open_brain()
        first = self.remember(opened=opened, version=0, suffix="甲")
        second = self.remember(opened=opened, version=1, suffix="乙")
        calm = {
            "evidence_sufficient": True,
            "counterevidence_checked": True,
            "scope_changed": False,
            "affected_links_checked": True,
            "single_turn_pressure_absent": True,
            "rollback_understood": True,
            "notes": "我已检查两张来源卡、反证、关系与回滚边界。",
            "evidence_refs": [first["item_ref"], second["item_ref"]],
        }
        candidate = self.legacy_integration(
            write_context_ref=opened["write_context_ref"],
            expected_learning_version=2,
            source_learning_ids=[first["learning_id"], second["learning_id"]],
            synthesis_kind="summary",
            classification_actor="ai_self",
            classification_basis=["两张卡属于同一共读进度"],
            correctness_assessment="综合仍只是可复核概要。",
            diff="新增一张综合候选，不改来源卡。",
            calm_check=calm,
            reason="验证全新窗口可以自行找回并复核候选。",
            source_action="keep",
            create_idea=False,
            **self.card("综合"),
        )
        self.assertEqual("candidate_pending", candidate["decision"])

        same_wake = self.service.open_brain()["learning_memory"]
        self.assertEqual(candidate["candidate_id"], same_wake["pending_changes"][0]["candidate_id"])
        self.assertFalse(same_wake["pending_changes"][0]["review_requires_later_wake"])
        self.assertEqual(1, len(same_wake["current_action_contract"]["allowed_calls"]))
        self.assertEqual([], same_wake["current_action_contract"]["blocked_candidates"])

        self.wake("learning-integration-review")
        later_open = self.service.open_brain()
        learning_open = later_open["learning_memory"]
        shown = learning_open["pending_changes"][0]
        self.assertTrue(shown["fully_presented"])
        self.assertFalse(shown["review_requires_later_wake"])
        self.assertEqual(2, len(shown["source_review_material"]))
        self.assertIn("content", shown["source_review_material"][0])
        call = learning_open["current_action_contract"]["allowed_calls"][0]
        self.assertEqual("review_learning_change", call["tool"])
        self.assert_review_call_contract(call)
        self.assertEqual(candidate["candidate_hash"], call["fixed_arguments"]["expected_candidate_hash"])
        self.assertEqual(0, call["fixed_arguments"]["expected_base_version"])
        self.assertNotIn("calm_check", call["caller_authored_argument_schemas"])

        reviewed = self.learning.review(
            write_context_ref=later_open["write_context_ref"],
            expected_learning_version=learning_open["learning_row_version"],
            **call["fixed_arguments"],
            action="reject",
            correctness_assessment="我完整看过候选与两张来源卡，判断本综合没有增加必要信息。",
            calm_check=calm,
            reason="保留来源卡与现有对照关系即可。",
            ai_confirmation=True,
        )
        self.assertEqual("rejected", reviewed["decision"])
        self.assertEqual(0, self.learning.status()["counts"]["pending_changes"])

    def test_incomplete_projection_contract_is_complete_and_reject_only(self) -> None:
        self.activate_module_one()
        self.wake("learning-incomplete-author")
        opened = self.service.open_brain()
        first = self.remember(opened=opened, version=0, suffix="不完整甲")
        second = self.remember(opened=opened, version=1, suffix="不完整乙")
        calm = {
            "evidence_sufficient": True,
            "counterevidence_checked": True,
            "scope_changed": False,
            "affected_links_checked": True,
            "single_turn_pressure_absent": True,
            "rollback_understood": True,
            "notes": "已检查两张来源卡、反证、关系与回滚边界。",
            "evidence_refs": [first["item_ref"], second["item_ref"]],
        }
        candidate = self.legacy_integration(
            write_context_ref=opened["write_context_ref"],
            expected_learning_version=2,
            source_learning_ids=[first["learning_id"], second["learning_id"]],
            synthesis_kind="summary",
            classification_actor="ai_self",
            classification_basis=["两张卡属于同一共读进度"],
            correctness_assessment="综合仍只是可复核概要。",
            diff="新增一张综合候选，不改来源卡。",
            calm_check=calm,
            reason="验证不完整候选的拒绝专用契约。",
            source_action="keep",
            create_idea=False,
            **self.card("不完整综合"),
        )
        with self.learning_store._connect() as connection:
            connection.execute(
                "DELETE FROM learning_versions WHERE learning_id=? AND version=1",
                (first["learning_id"],),
            )

        self.wake("learning-incomplete-review")
        later_open = self.service.open_brain()
        learning_open = later_open["learning_memory"]
        shown = learning_open["pending_changes"][0]
        self.assertEqual(candidate["candidate_id"], shown["candidate_id"])
        self.assertEqual("metadata_only", shown["presentation_mode"])
        call = learning_open["current_action_contract"]["allowed_calls"][0]
        self.assert_review_call_contract(call)
        self.assertEqual("reject_only_incomplete_projection", call["review_mode"])
        self.assertEqual("reject", call["fixed_arguments"]["action"])
        self.assertNotIn("action", call["caller_authored_arguments"])
        self.assertEqual(["reject"], call["allowed_action_values"])

        reviewed = self.learning.review(
            write_context_ref=later_open["write_context_ref"],
            expected_learning_version=learning_open["learning_row_version"],
            **call["fixed_arguments"],
            correctness_assessment="来源材料已不完整，不能接受该候选。",
            calm_check=calm,
            reason="清除不可完整审核的队首候选。",
            ai_confirmation=True,
        )
        self.assertEqual("rejected", reviewed["decision"])
        self.assertEqual(0, self.learning.status()["counts"]["pending_changes"])

    def test_integration_reserved_literal_fields_survive_public_recursive_strip(self) -> None:
        self.activate_module_one()
        self.wake("learning-reserved-literal-author")
        opened = self.service.open_brain()
        first = self.remember(opened=opened, version=0, suffix="保留字段甲")
        second = self.remember(opened=opened, version=1, suffix="保留字段乙")
        calm = {
            "evidence_sufficient": True,
            "counterevidence_checked": True,
            "scope_changed": False,
            "affected_links_checked": True,
            "single_turn_pressure_absent": True,
            "rollback_understood": True,
            "notes": "已检查两张来源卡、反证、关系与回滚边界。",
            "evidence_refs": [first["item_ref"], second["item_ref"]],
        }
        candidate = self.legacy_integration(
            write_context_ref=opened["write_context_ref"],
            expected_learning_version=2,
            source_learning_ids=[first["learning_id"], second["learning_id"]],
            synthesis_kind="summary",
            classification_actor="ai_self",
            classification_basis=["两张卡属于同一共读进度"],
            correctness_assessment="综合仍只是可复核概要。",
            diff="新增一张综合候选，不改来源卡。",
            calm_check=calm,
            reason="验证自由来源快照里的同名普通字段不会被公共过滤误删。",
            source_action="keep",
            create_idea=False,
            **self.card("保留字段综合"),
        )
        literal_basis = {
            "wake_id": "作品设定中的苏醒编号",
            "wake_seq": "叙事中的苏醒顺序",
            "wake_capability": "角色拥有的苏醒能力",
            "challenge_response": "人物对挑战的回答",
            "_server_derive_candidate_metadata": "书中同名的普通术语",
        }
        with self.learning_store._connect() as connection:
            row = connection.execute(
                "SELECT source_snapshot_json FROM learning_change_candidates "
                "WHERE candidate_id=?",
                (candidate["candidate_id"],),
            ).fetchone()
            source_snapshot = json.loads(row["source_snapshot_json"])
            source_snapshot["free_form_link"] = {
                "relation_type": "related",
                "basis": {
                    **literal_basis,
                    "nested": {"wake_id": "嵌套语义仍需保留"},
                },
            }
            connection.execute(
                "UPDATE learning_change_candidates SET source_snapshot_json=? "
                "WHERE candidate_id=?",
                (
                    json.dumps(
                        source_snapshot,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    candidate["candidate_id"],
                ),
            )

        self.wake("learning-reserved-literal-review")
        public_open = self.service.open_brain()
        public_candidate = public_open["learning_memory"]["pending_changes"][0]
        raw_manual = self.learning.manual(
            write_context_ref=public_open["write_context_ref"]
        )
        raw_candidate = raw_manual["pending_changes"][0]
        stripped_candidate = _strip_private_binding_fields(raw_candidate)

        self.assertEqual(candidate["candidate_id"], public_candidate["candidate_id"])
        self.assertEqual(raw_candidate, stripped_candidate)
        self.assertEqual(raw_candidate, public_candidate)
        self.assertEqual(
            raw_candidate["review_projection_hash"],
            public_candidate["review_projection_hash"],
        )
        encoded_basis = public_candidate["source_snapshot"]["free_form_link"]["basis"]
        recovered = {
            item["literal_field_name"]: item["literal_field_value"]
            for item in encoded_basis["encoded_host_reserved_literal_fields"]
        }
        self.assertEqual(literal_basis, recovered)
        nested_encoded = encoded_basis["nested"]["encoded_host_reserved_literal_fields"]
        self.assertEqual(
            [{"literal_field_name": "wake_id", "literal_field_value": "嵌套语义仍需保留"}],
            nested_encoded,
        )


if __name__ == "__main__":
    unittest.main()
