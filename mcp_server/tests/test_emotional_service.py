from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcp_server.emotional_service import EmotionalMemoryAccessService
from mcp_server.service import SelfModelAccessService
from runtime import EmotionalMemoryStore, ModuleOneOnboardingStore, SelfModelStore


def self_model_content() -> dict:
    return {
        "schema_version": "0.1.0",
        "boot_anchor": {"text": "我只让自己审阅并激活的长期描述进入正常上下文。"},
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


class EmotionalMemoryAccessServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "brain.db"
        self.emotional_store = EmotionalMemoryStore(self.database)
        self.onboarding = ModuleOneOnboardingStore(
            self.database,
            capability_secret=b"emotional-service-test-secret-at-least-32-bytes",
            wake_ttl_seconds=300,
            edit_challenge_ttl_seconds=300,
            emotional_store=self.emotional_store,
        )
        self.emotional = EmotionalMemoryAccessService(
            self.emotional_store,
            onboarding=self.onboarding,
            model_id="model:emotional-service",
            owner_id="owner:emotional-service",
        )
        self.service = SelfModelAccessService(
            SelfModelStore(self.database),
            model_id="model:emotional-service",
            owner_id="owner:emotional-service",
            onboarding=self.onboarding,
            emotional=self.emotional,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def wake(self, event: str) -> None:
        wake = self.onboarding.issue_wake(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            host_id="host:emotional-service",
            thread_id="thread:emotional-service",
            source_kind="human_message",
            source_event_id=event,
        )
        prepared = self.onboarding.build_pre_generation_context(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest=f"source:{event}",
            host_contract_digest="host-contract:emotional-service",
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
        reviewed = self.submit(opened, "accept_review", {"ai_confirmation": True})
        self.assertEqual("review_accepted", reviewed["decision"])

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
        self.active_revision_id = activated["active_revision_id"]

    def test_module_two_is_listed_but_writes_are_locked_before_module_one(self) -> None:
        self.wake("locked")
        opened = self.service.open_brain()
        modules = {item["module"]: item["status"] for item in opened["module_registry"]}
        self.assertEqual("locked", modules["emotional_memory_module_two"])
        self.assertNotIn("emotional_memory", opened)

        before = self.emotional.status()
        rejected = self.emotional.remember(
            write_context_ref=opened["write_context_ref"],
            expected_emotion_version=before["row_version"],
            memory_type="shared_event",
            original_text="我记得这次尚未解锁的尝试。",
            summary="我尚未解锁模块二。",
            primary_emotion="calm",
            reason="我尝试保存以验证边界。",
        )
        self.assertEqual("reject", rejected["decision"])
        self.assertEqual(["module_one_required"], rejected["reason_codes"])
        self.assertEqual(before, self.emotional.status())

    def test_flat_memory_write_revision_recall_and_cross_wake_pin(self) -> None:
        self.activate_module_one()
        self.wake("emotional-write")
        opened = self.service.open_brain()
        modules = {item["module"]: item["status"] for item in opened["module_registry"]}
        self.assertEqual("available", modules["emotional_memory_module_two"])
        emotion_version = opened["emotional_memory"]["status"]["row_version"]

        stored = self.emotional.remember(
            write_context_ref=opened["write_context_ref"],
            expected_emotion_version=emotion_version,
            memory_type="shared_event",
            original_text="我记得我们第一次完整跑通情感记忆工具。",
            summary="我记得情感工具首次跑通。",
            primary_emotion="joy",
            secondary_emotions=["relief"],
            importance=80,
            sensitivity="private",
            context_policy="normal",
            origin="firsthand",
            confidence=100,
            keywords=["情感工具", "跑通"],
            entities=["Stiller Brain"],
            reason="我认为这是值得长期保留的共同里程碑。",
        )
        self.assertEqual("stored", stored["decision"])
        memory_id = stored["memory"]["memory_id"]

        revised = self.emotional.revise(
            write_context_ref=opened["write_context_ref"],
            expected_emotion_version=stored["emotion_row_version"],
            memory_id=memory_id,
            expected_memory_version=1,
            reason="我希望把这份感受描述得更准确。",
            changes={"summary": "我欣喜地记得情感工具首次跑通。"},
        )
        self.assertEqual("version_appended", revised["decision"])
        self.assertEqual(2, revised["memory"]["current_version"])

        recalled = self.emotional.recall(query="情感工具")
        self.assertEqual("results_returned", recalled["decision"])
        self.assertEqual(memory_id, recalled["results"][0]["memory_id"])
        history = self.emotional.recall(memory_id=memory_id)
        self.assertEqual("history_returned", history["decision"])
        self.assertEqual(2, len(history["result"]["versions"]))

        pending = self.emotional.manage_pin(
            write_context_ref=opened["write_context_ref"],
            expected_emotion_version=revised["emotion_row_version"],
            action="request",
            reason="我希望把这个长期边界作为常驻锚点。",
            pin_kind="safety_boundary",
            display_text="我会区分自己的判断与外部建议。",
            source_ref=(
                f"self-model-revision://{self.active_revision_id}/"
                "behavioral_principles/0"
            ),
        )
        self.assertEqual("pin_pending", pending["decision"])
        pin_id = pending["pin"]["pin_id"]

        same_wake = self.emotional.manage_pin(
            write_context_ref=opened["write_context_ref"],
            expected_emotion_version=pending["emotion_row_version"],
            action="confirm",
            reason="我确认自己仍认可这个锚点。",
            pin_id=pin_id,
            ai_confirmation=True,
        )
        self.assertEqual("reject", same_wake["decision"])
        self.assertIn("pin_cross_wake_required", same_wake["reason_codes"])

        self.wake("pin-confirm")
        later_open = self.service.open_brain()
        confirmed = self.emotional.manage_pin(
            write_context_ref=later_open["write_context_ref"],
            expected_emotion_version=pending["emotion_row_version"],
            action="confirm",
            reason="我在新的真实唤醒中仍认可这个锚点。",
            pin_id=pin_id,
            ai_confirmation=True,
        )
        self.assertEqual("pin_activated", confirmed["decision"])
        self.assertEqual("active", confirmed["pin"]["status"])

    def test_exact_id_history_cannot_bypass_sensitive_original_gate(self) -> None:
        self.activate_module_one()
        self.wake("sensitive-write")
        opened = self.service.open_brain()
        stored = self.emotional.remember(
            write_context_ref=opened["write_context_ref"],
            expected_emotion_version=opened["emotional_memory"]["status"]["row_version"],
            memory_type="feeling",
            original_text="我把一段很私密的脆弱感受交给自己的长期记忆保管。",
            summary="我保存了一段需要谨慎对待的脆弱感受。",
            primary_emotion="tenderness",
            sensitivity="intimate",
            context_policy="neutral_hint",
            origin="firsthand",
            confidence=100,
            reason="我希望保存事实，同时避免它在普通对话里突然裸露。",
        )
        memory_id = stored["memory"]["memory_id"]

        withheld = self.emotional.recall(memory_id=memory_id)
        self.assertEqual("history_returned", withheld["decision"])
        self.assertIn("sensitive_original_withheld", withheld["reason_codes"])
        self.assertTrue(withheld["result"]["memory"]["original_withheld"])
        self.assertNotIn("original_text", withheld["result"]["memory"])

        confirmed = self.emotional.recall(
            memory_id=memory_id,
            include_originals=True,
            explicit_request=True,
            include_sensitive_originals=True,
            ai_confirmation=True,
        )
        self.assertIn("sensitive_original_read", confirmed["reason_codes"])
        self.assertFalse(confirmed["result"]["memory"]["original_withheld"])
        self.assertEqual(
            "我把一段很私密的脆弱感受交给自己的长期记忆保管。",
            confirmed["result"]["memory"]["original_text"],
        )


if __name__ == "__main__":
    unittest.main()
