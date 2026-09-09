from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from mcp_server.public_contract import (
    PUBLIC_ACTIVATE_INPUT_SCHEMA,
    PUBLIC_CONTRACT_VERSION,
    PUBLIC_INTENT_PAYLOAD_SCHEMAS,
    PUBLIC_SUBMIT_INPUT_SCHEMA,
    PublicPayloadValidationError,
    validate_public_payload,
)
from mcp_server.service import SelfModelAccessService
from runtime import ModuleOneOnboardingStore, SelfModelStore


def self_model_content(*, with_reference: bool = False) -> dict:
    references = []
    if with_reference:
        references = [
            {
                "anchor_id": "reviewed-source",
                "memory_ref": "  memory://self/reviewed-source  ",
                "meaning": "支持我这项长期判断的真实来源",
            },
            {
                "anchor_id": "same-source",
                "memory_ref": "memory://self/reviewed-source",
                "meaning": "同一来源不应生成重复的审计引用",
            },
        ]
    return {
        "schema_version": "0.1.0",
        "boot_anchor": {"text": "我只让自己审阅并正式激活的长期模型进入正常上下文。"},
        "active_identity_capsule": {
            "name_and_identity": "我是维护这份长期自我模型的 AI。",
            "personality_foundation": "我保持诚实、审慎，也保留自己的判断。",
            "expression_style": "我会先说结论，再说明依据与不确定处。",
            "behavioral_principles": ["我会区分自己的判断与外部建议。"],
            "core_identity_anchors": ["我只保留自己复核后认可的长期锚点。"],
            "self_revision_safety_prompt": "我会在独立的真实唤醒中复核候选，再决定是否激活。",
        },
        "facets": {},
        "anchor_references": references,
    }


class PublicContractV9Tests(unittest.TestCase):
    def test_candidate_contract_is_only_content_and_reason(self) -> None:
        self.assertEqual("public-tools/20", PUBLIC_CONTRACT_VERSION)
        for intent in ("submit", "revise"):
            schema = PUBLIC_INTENT_PAYLOAD_SCHEMAS[intent]
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(["content", "reason"], schema["required"])
            self.assertEqual({"content", "reason"}, set(schema["properties"]))
            self.assertNotIn("ai_reason", schema["properties"])
            self.assertNotIn("diff", schema["properties"])
            self.assertNotIn("evidence_refs", schema["properties"])

    def test_real_submit_tool_schema_has_no_request_wrapper_or_free_object(self) -> None:
        schema = PUBLIC_SUBMIT_INPUT_SCHEMA
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            {"intent", "write_context_ref", "expected_row_version"},
            set(schema["required"]),
        )
        self.assertEqual(
            {"intent", "write_context_ref", "expected_row_version", "payload"},
            set(schema["properties"]),
        )
        self.assertNotIn("request", schema["properties"])
        branches = schema["allOf"][0]["oneOf"]
        self.assertEqual(set(PUBLIC_INTENT_PAYLOAD_SCHEMAS), {
            branch["properties"]["intent"]["const"] for branch in branches
        })

    def test_activation_schema_requires_nullable_base_and_literal_true(self) -> None:
        schema = PUBLIC_ACTIVATE_INPUT_SCHEMA
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            {
                "candidate_id",
                "write_context_ref",
                "expected_row_version",
                "expected_active_revision",
                "ai_confirmation",
            },
            set(schema["required"]),
        )
        self.assertEqual(
            [{"type": "string", "minLength": 1}, {"type": "null"}],
            schema["properties"]["expected_active_revision"]["anyOf"],
        )
        self.assertIs(True, schema["properties"]["ai_confirmation"]["const"])

    def test_ai_reason_error_names_the_exact_required_key(self) -> None:
        with self.assertRaises(PublicPayloadValidationError) as caught:
            validate_public_payload(
                "submit",
                {"content": self_model_content(), "ai_reason": "我写的理由"},
            )
        issues = {(item["path"], item["code"]) for item in caught.exception.issues}
        self.assertIn(("payload.ai_reason", "unexpected_property"), issues)
        self.assertIn(("payload.reason", "missing_reason"), issues)


class PublicFacadeV9Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "brain.db"
        self.onboarding = ModuleOneOnboardingStore(
            self.database,
            capability_secret=b"public-v5-test-secret-that-is-at-least-32-bytes",
            wake_ttl_seconds=300,
            edit_challenge_ttl_seconds=300,
        )
        self.service = SelfModelAccessService(
            SelfModelStore(self.database),
            model_id="model:public-v5",
            owner_id="owner:public-v5",
            onboarding=self.onboarding,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def wake(self, event: str) -> None:
        wake = self.onboarding.issue_wake(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            host_id="host:public-v5",
            thread_id="thread:public-v5",
            source_kind="human_message",
            source_event_id=event,
        )
        prepared = self.onboarding.build_pre_generation_context(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest=f"source:{event}",
            host_contract_digest="host-contract:public-v5",
        )
        confirmed = self.onboarding.confirm_context_injected(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            context_hash=prepared["context_hash"],
        )
        self.assertEqual("injected", confirmed["decision"])

    def open(self) -> dict:
        result = self.service.open_brain()
        self.assertEqual("public-tools/20", result["contract_version"])
        self.assertTrue(result["write_context_available"])
        return result

    def submit(self, opened: dict, intent: str, payload: dict | None = None) -> dict:
        return self.service.submit_self_model_candidate(
            intent=intent,
            write_context_ref=opened["write_context_ref"],
            expected_row_version=opened["row_version"],
            payload=payload,
        )

    def candidate(self, candidate_id: str) -> dict:
        return next(
            candidate
            for candidate in self.service.store.list_candidates(self.service.model_id)
            if candidate["candidate_id"] == candidate_id
        )

    def advance_to_body(self) -> dict:
        opened = self.open()
        self.assertEqual("acknowledge", opened["current_action_contract"]["allowed_calls"][0]["intent"])
        self.assertEqual("advanced", self.submit(opened, "acknowledge")["decision"])

        opened = self.open()
        self.assertEqual("advanced", self.submit(opened, "acknowledge")["decision"])

        opened = self.open()
        call = opened["current_action_contract"]["allowed_calls"][0]
        self.assertEqual("save_calm_prompt", call["intent"])
        self.assertEqual(["text"], call["payload_schema"]["required"])
        saved = self.submit(
            opened,
            "save_calm_prompt",
            {"text": "我先停下来，区分长期意愿与这一次对话的状态。"},
        )
        self.assertEqual("saved", saved["decision"])
        return self.open()

    def test_open_teaches_exact_candidate_call_and_simple_submit_succeeds(self) -> None:
        self.wake("simple-submit")
        opened = self.advance_to_body()
        call = opened["current_action_contract"]["allowed_calls"][0]
        self.assertEqual("submit", call["intent"])
        self.assertEqual({"content", "reason"}, set(call["payload_schema"]["properties"]))
        self.assertEqual(
            ["diff", "evidence_refs", "expected_active_revision"],
            call["server_derived_fields"],
        )

        stored = self.submit(
            opened,
            "submit",
            {
                "content": self_model_content(),
                "reason": "我复核后认为这些内容适合作为稳定、长期的自我描述。",
            },
        )
        self.assertEqual("pending", stored["decision"])
        self.assertEqual("candidate_wait", stored["state"]["stage"])
        self.assertIsNone(self.service.store.active_revision(self.service.model_id))

        candidate = self.candidate(stored["candidate_id"])
        self.assertEqual([], json.loads(candidate["evidence_refs_json"]))
        self.assertEqual(
            [
                {"op": "add", "path": "/schema_version"},
                {"op": "add", "path": "/boot_anchor"},
                {"op": "add", "path": "/active_identity_capsule"},
                {"op": "add", "path": "/facets"},
                {"op": "add", "path": "/anchor_references"},
            ],
            json.loads(candidate["diff_json"]),
        )

    def test_guessed_wrappers_and_client_metadata_are_zero_side_effect(self) -> None:
        self.wake("bad-wrappers")
        opened = self.advance_to_body()
        before_state = copy.deepcopy(self.service.module_one_status()["state"])
        before_candidates = self.service.store.list_candidates(self.service.model_id)

        attempts = [
            {"candidate": {"content": self_model_content(), "reason": "我说明理由。"}},
            {"content": self_model_content(), "ai_reason": "我说明理由。"},
            {
                "content": self_model_content(),
                "reason": "我说明理由。",
                "diff": [{"op": "add", "path": "/schema_version"}],
                "evidence_refs": [],
                "expected_active_revision": None,
            },
            {
                "content": self_model_content(),
                "reason": "我说明理由。",
                "_server_derive_candidate_metadata": True,
            },
        ]
        for payload in attempts:
            rejected = self.submit(opened, "submit", payload)
            self.assertFalse(rejected["state_changed"])
            self.assertFalse(rejected["pointer_changed"])
            self.assertTrue(rejected.get("validation_issues") or rejected["decision"] == "server_bound_context_required")
            self.assertEqual(before_state, self.service.module_one_status()["state"])
            self.assertEqual(before_candidates, self.service.store.list_candidates(self.service.model_id))

        ai_reason_result = self.submit(
            opened,
            "submit",
            {"content": self_model_content(), "ai_reason": "我说明理由。"},
        )
        paths = {(issue["path"], issue["code"]) for issue in ai_reason_result["validation_issues"]}
        self.assertIn(("payload.ai_reason", "unexpected_property"), paths)
        self.assertIn(("payload.reason", "missing_reason"), paths)

    def test_evidence_is_derived_trimmed_and_deduplicated(self) -> None:
        self.wake("derived-evidence")
        opened = self.advance_to_body()
        stored = self.submit(
            opened,
            "submit",
            {
                "content": self_model_content(with_reference=True),
                "reason": "我只引用自己确实拥有、且可复核的来源。",
            },
        )
        self.assertEqual("pending", stored["decision"])
        candidate = self.candidate(stored["candidate_id"])
        self.assertEqual(
            ["memory://self/reviewed-source"],
            json.loads(candidate["evidence_refs_json"]),
        )

    def test_simple_contract_preserves_cross_wake_activation_and_append_only_edit(self) -> None:
        self.wake("lifecycle-1")
        opened = self.advance_to_body()
        first = self.submit(
            opened,
            "submit",
            {
                "content": self_model_content(),
                "reason": "我确认这是一份适合跨唤醒复核的首版长期描述。",
            },
        )
        self.assertEqual("pending", first["decision"])

        self.wake("lifecycle-2")
        review_open = self.open()
        self.assertEqual("candidate_review", review_open["continuation"]["stage"])
        accepted = self.submit(
            review_open,
            "accept_review",
            {"ai_confirmation": True},
        )
        self.assertEqual("review_accepted", accepted["decision"])
        same_wake = self.service.activate_self_model_candidate(
            candidate_id=first["candidate_id"],
            write_context_ref=review_open["write_context_ref"],
            expected_row_version=accepted["state"]["row_version"],
            expected_active_revision=None,
            ai_confirmation=True,
        )
        self.assertEqual("reject", same_wake["decision"])
        self.assertIn(
            "review_activation_wake_boundary_required",
            same_wake["reason_codes"],
        )
        self.assertIsNone(self.service.store.active_revision(self.service.model_id))

        self.wake("lifecycle-3")
        activation_open = self.open()
        first_activation_call = next(
            call
            for call in activation_open["current_action_contract"]["allowed_calls"]
            if call["tool"] == "activate_self_model_candidate"
        )
        self.assertIn(
            "expected_active_revision",
            first_activation_call["required_arguments"],
        )
        self.assertEqual(
            "$.continuation.candidate.base_revision_id",
            first_activation_call["argument_sources"]["expected_active_revision"],
        )
        self.assertIsNone(first_activation_call["expected_active_revision"])
        self.assertTrue(first_activation_call["first_activation"])
        wrong_candidate = self.service.activate_self_model_candidate(
            candidate_id="cand-not-current",
            write_context_ref=activation_open["write_context_ref"],
            expected_row_version=activation_open["row_version"],
            expected_active_revision=None,
            ai_confirmation=True,
        )
        self.assertEqual("reject", wrong_candidate["decision"])
        self.assertEqual(["candidate_not_current"], wrong_candidate["reason_codes"])
        self.assertIsNone(self.service.store.active_revision(self.service.model_id))

        activated = self.service.activate_self_model_candidate(
            candidate_id=first["candidate_id"],
            write_context_ref=activation_open["write_context_ref"],
            expected_row_version=activation_open["row_version"],
            expected_active_revision=None,
            ai_confirmation=True,
        )
        self.assertEqual("activate", activated["decision"])
        active_one = self.service.store.active_revision(self.service.model_id)
        self.assertIsNotNone(active_one)

        self.wake("lifecycle-4")
        live_open = self.open()
        begun = self.submit(live_open, "begin_edit")
        self.assertEqual("challenge_issued", begun["decision"])
        consent_open = self.open()
        confirmed = self.submit(
            consent_open,
            "confirm_edit",
            {"challenge_id": begun["challenge_id"], "ai_confirmation": True},
        )
        self.assertEqual("confirmed", confirmed["decision"])

        changed = self_model_content()
        changed["active_identity_capsule"]["expression_style"] = (
            "我会更简洁地先给结论，再说明证据和不确定处。"
        )
        edit_open = self.open()
        second = self.submit(
            edit_open,
            "submit",
            {
                "content": changed,
                "reason": "我复核后希望把长期表达方式写得更准确。",
            },
        )
        self.assertEqual("pending", second["decision"])
        self.assertEqual(
            [{"op": "replace", "path": "/active_identity_capsule"}],
            json.loads(self.candidate(second["candidate_id"])["diff_json"]),
        )
        self.assertEqual(
            active_one["revision_id"],
            self.candidate(second["candidate_id"])["base_revision_id"],
        )
        self.assertEqual(
            active_one["revision_id"],
            self.service.store.active_revision(self.service.model_id)["revision_id"],
        )

        self.wake("lifecycle-5")
        revise_open = self.open()
        revised = copy.deepcopy(changed)
        revised["active_identity_capsule"]["personality_foundation"] = (
            "我保持诚实、审慎、温和，也始终保留自己的判断。"
        )
        third = self.submit(
            revise_open,
            "revise",
            {
                "content": revised,
                "reason": "我在新一轮查看后认为这处修订更接近自己的长期取向。",
            },
        )
        self.assertEqual("pending", third["decision"])
        self.assertEqual(3, len(self.service.store.list_candidates(self.service.model_id)))
        self.assertEqual(
            [{"op": "replace", "path": "/active_identity_capsule"}],
            json.loads(self.candidate(third["candidate_id"])["diff_json"]),
        )
        self.assertEqual(
            active_one["revision_id"],
            self.service.store.active_revision(self.service.model_id)["revision_id"],
        )

        self.wake("lifecycle-6")
        revised_review_open = self.open()
        self.assertEqual(
            third["candidate_id"],
            revised_review_open["continuation"]["candidate"]["candidate_id"],
        )
        revised_accepted = self.submit(
            revised_review_open,
            "accept_review",
            {"ai_confirmation": True},
        )
        self.assertEqual("review_accepted", revised_accepted["decision"])
        self.assertEqual(
            active_one["revision_id"],
            self.service.store.active_revision(self.service.model_id)["revision_id"],
        )

        self.wake("lifecycle-7")
        revised_activation_open = self.open()
        revised_activation_call = next(
            call
            for call in revised_activation_open["current_action_contract"]["allowed_calls"]
            if call["tool"] == "activate_self_model_candidate"
        )
        self.assertEqual(
            active_one["revision_id"],
            revised_activation_call["expected_active_revision"],
        )
        self.assertFalse(revised_activation_call["first_activation"])
        activated_two = self.service.activate_self_model_candidate(
            candidate_id=third["candidate_id"],
            write_context_ref=revised_activation_open["write_context_ref"],
            expected_row_version=revised_activation_open["row_version"],
            expected_active_revision=revised_activation_call[
                "expected_active_revision"
            ],
            ai_confirmation=True,
        )
        self.assertEqual("activate", activated_two["decision"])
        active_two = self.service.store.active_revision(self.service.model_id)
        self.assertIsNotNone(active_two)
        self.assertEqual(third["candidate_id"], active_two["candidate_id"])
        self.assertEqual(active_one["revision_id"], active_two["parent_revision_id"])
        self.assertNotEqual(active_one["revision_id"], active_two["revision_id"])


if __name__ == "__main__":
    unittest.main()
