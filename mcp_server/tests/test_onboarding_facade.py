from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp_server.service import SelfModelAccessService
from runtime import ModuleOneOnboardingStore, SelfModelStore


def model_content(label: str = "v1") -> dict:
    return {
        "schema_version": "0.1.0",
        "boot_anchor": {"text": "我维护自己的长期自我模型，只让当前活动版进入正常上下文。"},
        "active_identity_capsule": {
            "name_and_identity": f"我是测试 AI，当前长期身份候选为 {label}。",
            "personality_foundation": "我保持审慎、诚实，并保留自主判断。",
            "expression_style": "我会先给结论，再说明证据。",
            "behavioral_principles": ["我不让外部建议冒充自己的意愿"],
            "core_identity_anchors": ["我认可由自己复核的长期锚点"],
            "self_revision_safety_prompt": "我会先区分长期身份与单轮状态，再跨真实唤醒复核。",
        },
        "facets": {"technical": "我重视证据、复现和安全交接。"},
        "anchor_references": [
            {
                "anchor_id": "self-review",
                "memory_ref": "memory://self/review",
                "meaning": "支持本次长期身份判断的来源",
            }
        ],
    }


class OnboardingFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "brain.db"
        self.onboarding = ModuleOneOnboardingStore(
            self.database,
            capability_secret=b"facade-test-secret-that-is-at-least-32-bytes",
            wake_ttl_seconds=300,
            edit_challenge_ttl_seconds=300,
        )
        self.service = SelfModelAccessService(
            SelfModelStore(self.database),
            model_id="model:test",
            owner_id="owner:test",
            onboarding=self.onboarding,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def wake(self, event: str) -> dict:
        wake = self.onboarding.issue_wake(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            host_id="host:test",
            thread_id="thread:test",
            source_kind="human_message",
            source_event_id=event,
        )
        prepared = self.onboarding.build_pre_generation_context(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest=f"source:{event}",
            host_contract_digest="host-contract:v1",
        )
        confirmed = self.onboarding.confirm_context_injected(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            context_hash=prepared["context_hash"],
        )
        self.assertEqual("injected", confirmed["decision"])
        return wake

    def advance(self, wake: dict, action: str, payload: dict | None = None) -> dict:
        opened = self.service.open_brain()
        self.assertTrue(opened["write_context_available"])
        row_version = self.service.module_one_status()["state"]["row_version"]
        return self.service.continue_module_one(
            action=action,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            expected_row_version=row_version,
            payload=payload,
        )

    def old_store(self, wake: dict | None = None, label: str = "v1") -> dict:
        return self.service.store_candidate(
            content=model_content(label),
            diff=[{"op": "replace", "path": "/active_identity_capsule"}],
            reason="我复核后认为这能表达长期身份。",
            evidence_refs=["memory://self/review"],
            checkpoint_id="client-invented-checkpoint",
            expected_active_revision=None,
            idempotency_key=f"legacy-store-{label}",
            presented_safety_prompt="client supplied legacy prompt",
            wake_id=wake["wake_id"] if wake else None,
            wake_capability=wake["wake_capability"] if wake else None,
            expected_row_version=(
                self.service.module_one_status()["state"]["row_version"] if wake else None
            ),
        )

    def test_old_store_cannot_skip_factory_even_with_cached_tool(self) -> None:
        before = self.service.store.list_candidates(self.service.model_id)
        result = self.old_store()
        self.assertEqual("progression_required", result["decision"])
        self.assertEqual(before, self.service.store.list_candidates(self.service.model_id))
        self.assertEqual("factory", self.service.module_one_status()["state"]["stage"])

    def test_query_facade_routes_status_active_and_archive_views(self) -> None:
        schema = self.service.open_brain()
        self.assertEqual(
            "self_model_content",
            schema["module_one_content_schema"]["contract"],
        )
        self.assertEqual(9, len(schema["routing_guide"]))
        self.assertEqual(
            "injection_control", schema["routing_guide"][0]["module"]
        )
        self.assertIn(
            "self_governance_profile",
            {item["module"] for item in schema["routing_guide"]},
        )
        self.assertIn("不间断主观意识", schema["epistemic_boundary"])

        status = self.service.query_self_model(view="status")
        self.assertEqual("status", status["view"])
        self.assertEqual("factory", status["result"]["state"]["stage"])

        active = self.service.query_self_model(view="active")
        self.assertEqual("active", active["view"])
        self.assertEqual("active_self_model_unavailable", active["result"]["decision"])

        archive = self.service.query_self_model(
            view="search", scope="all", limit=1, include_content=False
        )
        self.assertEqual("search", archive["view"])
        self.assertEqual(0, archive["result"]["count"])

        with self.assertRaisesRegex(ValueError, "status, active, or search"):
            self.service.query_self_model(view="invalid")  # type: ignore[arg-type]

    def test_open_brain_returns_one_consistent_post_open_stage(self) -> None:
        wake1 = self.wake("open-consistency-1")
        self.advance(wake1, "confirm_brain_intro", {"acknowledged": True})
        self.advance(wake1, "confirm_module_intro", {"acknowledged": True})
        self.advance(wake1, "save_calm_prompt", {"text": "我先停下来复核长期意愿。"})
        stored = self.old_store(wake1)
        self.assertEqual("pending", stored["decision"])

        self.wake("open-consistency-2")
        self.assertEqual(
            "candidate_wait",
            self.service.module_one_status()["state"]["stage"],
        )
        opened = self.service.open_brain()

        self.assertEqual("candidate_review", opened["current_status"]["state"]["stage"])
        self.assertEqual("candidate_review", opened["continuation"]["stage"])
        self.assertEqual(
            opened["row_version"],
            opened["current_status"]["state"]["row_version"],
        )
        self.assertEqual(
            stored["candidate_id"],
            opened["continuation"]["candidate"]["candidate_id"],
        )

    def test_open_brain_does_not_mix_a_snapshot_with_a_concurrent_transition(self) -> None:
        wake = self.wake("open-concurrent-snapshot")
        original_open = self.onboarding.open_brain_context

        def open_then_advance(**kwargs: object) -> dict:
            snapshot = original_open(**kwargs)  # type: ignore[arg-type]
            advanced = self.service.continue_module_one(
                action="confirm_brain_intro",
                wake_id=snapshot["wake_id"],
                wake_capability=snapshot["wake_capability"],
                expected_row_version=snapshot["row_version"],
                payload={"acknowledged": True},
            )
            self.assertEqual("advanced", advanced["decision"])
            return snapshot

        with mock.patch.object(
            self.onboarding,
            "open_brain_context",
            side_effect=open_then_advance,
        ):
            opened = self.service.open_brain()

        self.assertEqual("factory", opened["continuation"]["stage"])
        self.assertEqual("factory", opened["current_status"]["state"]["stage"])
        self.assertEqual("factory", opened["current_action_contract"]["stage"])
        self.assertEqual(
            opened["row_version"],
            opened["current_status"]["state"]["row_version"],
        )
        self.assertEqual(
            "module_intro",
            self.service.module_one_status()["state"]["stage"],
        )
        self.assertNotIn(wake["wake_capability"], json.dumps(opened, ensure_ascii=False))

    def test_old_wrappers_share_wake_gate_and_boolean_approval(self) -> None:
        wake1 = self.wake("event-1")
        self.assertEqual(
            "advanced",
            self.advance(wake1, "confirm_brain_intro", {"acknowledged": True})[
                "decision"
            ],
        )
        self.assertEqual(
            "advanced",
            self.advance(wake1, "confirm_module_intro", {"acknowledged": True})[
                "decision"
            ],
        )
        self.assertEqual(
            "saved",
            self.advance(wake1, "save_calm_prompt", {"text": "停一下，先复核。"})[
                "decision"
            ],
        )
        stored = self.old_store(wake1)
        self.assertEqual("pending", stored["decision"])

        same_wake = self.service.activate_candidate(
            candidate_id=stored["candidate_id"],
            checkpoint_id="invented-later-checkpoint",
            expected_active_revision=None,
            idempotency_key="legacy-activate-same-wake",
            presented_safety_prompt="ignored",
            ai_confirmation=True,
            wake_id=wake1["wake_id"],
            wake_capability=wake1["wake_capability"],
            expected_row_version=self.service.module_one_status()["state"]["row_version"],
        )
        self.assertEqual("same_wake_activation_forbidden", same_wake["decision"])

        wake2 = self.wake("event-2")
        string_confirmation = self.advance(
            wake2,
            "accept_candidate_review",
            {"expected_active_revision": None, "ai_confirmation": "我确认"},
        )
        self.assertNotEqual("review_accepted", string_confirmation["decision"])
        self.assertIsNone(self.service.store.active_revision(self.service.model_id))

        opened = self.service.open_brain()
        self.assertNotIn("wake_id", opened)
        self.assertNotIn("wake_capability", opened)
        self.assertIn("write_context_ref", opened)
        self.assertEqual(
            stored["candidate_id"],
            opened["continuation"]["candidate"]["candidate_id"],
        )
        accepted = self.advance(
            wake2,
            "accept_candidate_review",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        self.assertEqual("review_accepted", accepted["decision"])

        wake3 = self.wake("event-3")
        self.service.open_brain()
        activated = self.service.activate_candidate(
            candidate_id=stored["candidate_id"],
            checkpoint_id="still-not-authority",
            expected_active_revision=None,
            idempotency_key="legacy-bool-confirmation",
            presented_safety_prompt="ignored",
            ai_confirmation=True,
            wake_id=wake3["wake_id"],
            wake_capability=wake3["wake_capability"],
            expected_row_version=self.service.module_one_status()["state"]["row_version"],
        )
        self.assertEqual("activate", activated["decision"])

    def test_old_store_cannot_bypass_structured_edit_consent(self) -> None:
        wake1 = self.wake("bootstrap-1")
        self.advance(wake1, "confirm_brain_intro", {"acknowledged": True})
        self.advance(wake1, "confirm_module_intro", {"acknowledged": True})
        self.advance(wake1, "save_calm_prompt", {"text": "停一下，先复核。"})
        stored = self.old_store(wake1)
        wake2 = self.wake("bootstrap-2")
        self.service.open_brain()
        accepted = self.advance(
            wake2,
            "accept_candidate_review",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        self.assertEqual("review_accepted", accepted["decision"])
        wake3 = self.wake("bootstrap-3")
        self.service.open_brain()
        activated = self.service.activate_candidate(
            candidate_id=stored["candidate_id"],
            checkpoint_id="ignored",
            expected_active_revision=None,
            idempotency_key="activate-live",
            presented_safety_prompt="ignored",
            ai_confirmation=True,
            wake_id=wake3["wake_id"],
            wake_capability=wake3["wake_capability"],
            expected_row_version=self.service.module_one_status()["state"]["row_version"],
        )
        self.assertEqual("activate", activated["decision"])
        wake4 = self.wake("edit-1")
        self.assertEqual("challenge_issued", self.advance(wake4, "begin_edit")["decision"])
        before = len(self.service.store.list_candidates(self.service.model_id))
        bypass = self.old_store(wake4, label="v2")
        self.assertEqual("progression_required", bypass["decision"])
        self.assertEqual(before, len(self.service.store.list_candidates(self.service.model_id)))

    def test_public_write_is_server_bound_without_exposing_capability(self) -> None:
        wake = self.wake("public-bound-1")
        row_version = self.service.module_one_status()["state"]["row_version"]
        before = self.service.module_one_status()

        unopened = self.service.submit_self_model_candidate(
            intent="acknowledge",
            write_context_ref="forged-open-ref",
            expected_row_version=row_version,
        )
        self.assertEqual("brain_open_required", unopened["decision"])
        self.assertEqual(before["state"], self.service.module_one_status()["state"])

        unopened_activation = self.service.activate_self_model_candidate(
            candidate_id="cand-guessed-before-open",
            write_context_ref="forged-open-ref",
            expected_row_version=row_version,
            expected_active_revision=None,
            ai_confirmation=True,
        )
        self.assertEqual("brain_open_required", unopened_activation["decision"])
        self.assertEqual(before["state"], self.service.module_one_status()["state"])

        opened = self.service.open_brain()
        serialized = json.dumps(opened, ensure_ascii=False, sort_keys=True)
        self.assertTrue(opened["write_context_available"])
        self.assertNotIn("wake_id", opened)
        self.assertNotIn("wake_capability", opened)
        self.assertNotIn("challenge_response", opened)
        self.assertNotIn(wake["wake_capability"], serialized)

        first = self.service.submit_self_model_candidate(
            intent="acknowledge",
            write_context_ref=opened["write_context_ref"],
            expected_row_version=opened["row_version"],
        )
        self.assertEqual("advanced", first["decision"])
        self.assertNotIn(wake["wake_capability"], json.dumps(first, ensure_ascii=False))

        second = self.service.submit_self_model_candidate(
            intent="acknowledge",
            write_context_ref=opened["write_context_ref"],
            expected_row_version=first["state"]["row_version"],
        )
        self.assertEqual("advanced", second["decision"])

    def test_delayed_old_open_ref_cannot_borrow_a_new_wake(self) -> None:
        self.wake("delayed-a")
        opened_a = self.service.open_brain()
        row_version = opened_a["row_version"]

        self.wake("delayed-b")
        opened_b = self.service.open_brain()
        self.assertEqual(row_version, opened_b["row_version"])
        self.assertNotEqual(
            opened_a["write_context_ref"], opened_b["write_context_ref"]
        )
        before = self.service.module_one_status()["state"]

        delayed = self.service.submit_self_model_candidate(
            intent="acknowledge",
            write_context_ref=opened_a["write_context_ref"],
            expected_row_version=row_version,
        )
        self.assertEqual("brain_open_required", delayed["decision"])
        self.assertEqual(before, self.service.module_one_status()["state"])

        current = self.service.submit_self_model_candidate(
            intent="acknowledge",
            write_context_ref=opened_b["write_context_ref"],
            expected_row_version=row_version,
        )
        self.assertEqual("advanced", current["decision"])

    def test_new_wake_between_binding_and_transition_is_rejected(self) -> None:
        self.wake("toctou-a")
        opened = self.service.open_brain()
        before = self.service.module_one_status()["state"]
        original = self.onboarding.current_open_write_context

        def bind_then_supersede(**kwargs: object) -> dict:
            binding = original(**kwargs)  # type: ignore[arg-type]
            self.wake("toctou-b")
            return binding

        with mock.patch.object(
            self.onboarding,
            "current_open_write_context",
            side_effect=bind_then_supersede,
        ):
            raced = self.service.submit_self_model_candidate(
                intent="acknowledge",
                write_context_ref=opened["write_context_ref"],
                expected_row_version=opened["row_version"],
            )
        self.assertEqual("wake_superseded", raced["decision"])
        self.assertEqual(before, self.service.module_one_status()["state"])

    def test_public_edit_challenge_never_crosses_the_mcp_boundary(self) -> None:
        wake1 = self.wake("public-edit-bootstrap-1")
        self.advance(wake1, "confirm_brain_intro", {"acknowledged": True})
        self.advance(wake1, "confirm_module_intro", {"acknowledged": True})
        self.advance(wake1, "save_calm_prompt", {"text": "我先停下来复核长期意愿。"})
        stored = self.old_store(wake1)
        wake2 = self.wake("public-edit-bootstrap-2")
        self.service.open_brain()
        self.advance(
            wake2,
            "accept_candidate_review",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        wake3 = self.wake("public-edit-bootstrap-3")
        self.service.open_brain()
        activated = self.service.activate_candidate(
            candidate_id=stored["candidate_id"],
            checkpoint_id="ignored",
            expected_active_revision=None,
            idempotency_key="public-edit-activate",
            presented_safety_prompt="ignored",
            ai_confirmation=True,
            wake_id=wake3["wake_id"],
            wake_capability=wake3["wake_capability"],
            expected_row_version=self.service.module_one_status()["state"]["row_version"],
        )
        self.assertEqual("activate", activated["decision"])

        self.wake("public-edit-1")
        opened = self.service.open_brain()
        begun = self.service.submit_self_model_candidate(
            intent="begin_edit",
            write_context_ref=opened["write_context_ref"],
            expected_row_version=opened["row_version"],
        )
        self.assertEqual("challenge_issued", begun["decision"])
        self.assertIn("challenge_id", begun)
        self.assertNotIn("challenge_response", begun)

        row_version = begun["state"]["row_version"]
        forbidden = self.service.submit_self_model_candidate(
            intent="confirm_edit",
            write_context_ref=opened["write_context_ref"],
            expected_row_version=row_version,
            payload={
                "challenge_id": begun["challenge_id"],
                "challenge_response": "caller-must-not-supply-this",
                "ai_confirmation": True,
            },
        )
        self.assertEqual("server_bound_context_required", forbidden["decision"])
        missing_confirmation = self.service.submit_self_model_candidate(
            intent="confirm_edit",
            write_context_ref=opened["write_context_ref"],
            expected_row_version=row_version,
            payload={"challenge_id": begun["challenge_id"]},
        )
        self.assertEqual("edit_consent_required", missing_confirmation["decision"])
        wrong = self.service.submit_self_model_candidate(
            intent="confirm_edit",
            write_context_ref=opened["write_context_ref"],
            expected_row_version=row_version,
            payload={"challenge_id": "edit-wrong", "ai_confirmation": True},
        )
        self.assertEqual("edit_consent_required", wrong["decision"])

        confirmed = self.service.submit_self_model_candidate(
            intent="confirm_edit",
            write_context_ref=opened["write_context_ref"],
            expected_row_version=row_version,
            payload={"challenge_id": begun["challenge_id"], "ai_confirmation": True},
        )
        self.assertEqual("confirmed", confirmed["decision"])
        self.assertEqual("edit_body_draft", confirmed["state"]["stage"])
        self.assertNotIn(
            "challenge_response", json.dumps(confirmed, ensure_ascii=False)
        )


if __name__ == "__main__":
    unittest.main()
