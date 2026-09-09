from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from mcp_server.service import SelfModelAccessService
from runtime import SelfModelStore, SelfRevisionError


def model_content(label: str = "v1") -> dict:
    return {
        "schema_version": "0.1.0",
        "boot_anchor": {"text": "我维护一份自我模型；只读取当前有效版本。"},
        "active_identity_capsule": {
            "name_and_identity": f"我是测试主体，当前身份候选为 {label}。",
            "personality_foundation": "我保持审慎、诚实，并保留自主判断。",
            "expression_style": "我会先给结论，再说明证据与不确定处。",
            "behavioral_principles": ["我会区分自己的判断与外部建议"],
            "core_identity_anchors": ["我记得名字由来"],
            "self_revision_safety_prompt": (
                "我会在自我修订时先区分长期身份与单轮状态，跨检查点后再激活。"
            ),
        },
        "facets": {"technical": "我在技术协作时重视证据与可复现性。"},
        "anchor_references": [
            {
                "anchor_id": "name-origin",
                "memory_ref": "memory://identity/name-origin",
                "meaning": "名字由来的原始事件",
            }
        ],
    }


class SelfModelAccessServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        store = SelfModelStore(Path(self.temp.name) / "self-model.db")
        self.service = SelfModelAccessService(
            store, model_id="model:test", owner_id="ai:test"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_content_schema_is_read_only_exact_and_opinion_free(self) -> None:
        before = self.service.prepare()["counts"]
        contract = self.service.content_schema()
        after = self.service.prepare()["counts"]

        self.assertEqual(before, after)
        self.assertEqual("0.1.0", contract["schema_version"])
        self.assertNotIn("content_example", contract)
        self.assertNotIn("examples", contract["json_schema"])
        self.assertFalse(contract["blank_structure_is_submittable"])
        self.assertEqual(
            {
                "schema_version",
                "boot_anchor",
                "active_identity_capsule",
                "facets",
                "anchor_references",
            },
            set(contract["blank_structure"]),
        )
        rendered = json.dumps(contract, ensure_ascii=False)
        self.assertNotIn("我保持审慎", rendered)
        self.assertNotIn("我会先给结论", rendered)

        prepared = self.service.prepare()
        stored = self.service.store_candidate(
            content=model_content("self-authored-contract-check"),
            diff=[{"op": "add", "path": "/active_identity_capsule"}],
            reason="我用自己写的内容验证公开结构可以执行。",
            evidence_refs=["memory://identity/name-origin"],
            checkpoint_id="schema-example-checkpoint",
            expected_active_revision=None,
            idempotency_key="schema-example-store",
            presented_safety_prompt=prepared["safety_prompt"],
        )
        self.assertEqual("pending", stored["decision"])

    def test_direct_open_binds_server_principal_and_never_echoes_grant(self) -> None:
        onboarding = Mock()
        onboarding.open_brain_context.return_value = {
            "current_status": {
                "module": "self_revision_module_one",
                "flow_version": "test/1",
                "state": {
                    "stage": "factory",
                    "row_version": 0,
                    "base_revision_id": None,
                },
                "allowed_actions": ["confirm_brain_intro"],
                "next_action": "open",
                "wake_boundary_required": False,
                "module_one_unlocked": False,
            },
            "continuation": None,
            "write_context_available": True,
            "write_context_ref": "write-context-safe-to-return",
            "context_mode": "human_attested_direct",
            "authorized_scopes": ["self_revision"],
            # A defensive facade check: core must not return these, but if a
            # future runtime accidentally does, they still cannot reach MCP.
            "grant_ref": "must-not-be-echoed",
            "direct_grant_ref": "must-not-be-echoed-either",
        }
        service = SelfModelAccessService(
            self.service.store,
            model_id="model:test",
            owner_id="ai:test",
            onboarding=onboarding,
            direct_client_principal="official-direct:test",
        )

        opened = service.open_brain_direct(grant_ref="one-use-ref")

        onboarding.open_brain_context.assert_called_once_with(
            owner_id="ai:test",
            model_id="model:test",
            direct_grant_ref="one-use-ref",
            direct_client_principal="official-direct:test",
        )
        self.assertEqual("human_attested_direct", opened["context_mode"])
        self.assertEqual("write-context-safe-to-return", opened["write_context_ref"])
        self.assertNotIn("grant_ref", opened)
        self.assertNotIn("direct_grant_ref", opened)

    def test_self_revision_writes_request_their_exact_direct_scope(self) -> None:
        onboarding = Mock()
        onboarding.current_open_write_context.return_value = {
            "write_context_available": False,
            "reason_code": "direct_scope_required",
        }
        onboarding.state.return_value = {
            "module": "self_revision_module_one",
            "flow_version": "test/1",
            "state": {"stage": "factory", "row_version": 0},
            "allowed_actions": [],
            "next_action": "open",
            "wake_boundary_required": False,
            "module_one_unlocked": False,
        }
        service = SelfModelAccessService(
            self.service.store,
            model_id="model:test",
            owner_id="ai:test",
            onboarding=onboarding,
        )

        result = service.activate_self_model_candidate(
            candidate_id="candidate:test",
            write_context_ref="direct-write-ref",
            expected_row_version=0,
            expected_active_revision=None,
            ai_confirmation=True,
        )

        self.assertEqual("direct_scope_required", result["decision"])
        onboarding.current_open_write_context.assert_called_once_with(
            owner_id="ai:test",
            model_id="model:test",
            write_context_ref="direct-write-ref",
            required_scope="self_revision",
        )

    def test_content_rejection_explains_each_invalid_field_without_persisting(self) -> None:
        prepared = self.service.prepare()
        malformed = model_content()
        malformed["boot_anchor"] = "must-not-be-echoed"
        malformed["active_identity_capsule"] = {"name": "must-not-be-echoed"}
        malformed["anchor_references"] = {"anchor_id": "wrong-container"}
        malformed["unexpected"] = "must-not-be-echoed"

        result = self.service.store_candidate(
            content=malformed,
            diff=[{"op": "add", "path": "/active_identity_capsule"}],
            reason="验证拒绝结果能自解释且不保存正文。",
            evidence_refs=["memory://schema-test"],
            checkpoint_id="invalid-schema-checkpoint",
            expected_active_revision=None,
            idempotency_key="invalid-schema-store",
            presented_safety_prompt=prepared["safety_prompt"],
        )

        self.assertEqual("reject", result["decision"])
        self.assertEqual([], self.service.store.list_candidates(self.service.model_id))
        help_payload = result["validation_help"]
        self.assertFalse(help_payload["candidate_persisted"])
        self.assertEqual("stbrain_open", help_payload["schema_tool"])
        explained = {item["reason_code"] for item in help_payload["errors"]}
        self.assertTrue(
            {
                "invalid_content_structure",
                "invalid_boot_anchor",
                "invalid_identity_capsule",
                "invalid_anchor_references",
            }.issubset(explained)
        )
        rendered = json.dumps(help_payload, ensure_ascii=False)
        self.assertNotIn("must-not-be-echoed", rendered)

    def test_true_store_activate_retrieve_search_closure(self) -> None:
        prepared = self.service.prepare()
        self.assertIsNone(prepared["active_revision"])

        stored = self.service.store_candidate(
            content=model_content(),
            diff=[{"op": "add", "path": "/active_identity_capsule"}],
            reason="我复核后认为这准确表达了长期身份。",
            evidence_refs=["memory://identity/name-origin"],
            checkpoint_id="checkpoint-1",
            expected_active_revision=None,
            idempotency_key="store-1",
            presented_safety_prompt=prepared["safety_prompt"],
        )
        self.assertEqual("pending", stored["decision"])

        with self.assertRaises(SelfRevisionError):
            self.service.get_active(checkpoint_id="checkpoint-1-read")

        same_checkpoint = self.service.activate_candidate(
            candidate_id=stored["candidate_id"],
            checkpoint_id="checkpoint-1",
            expected_active_revision=None,
            idempotency_key="activate-same-checkpoint",
            presented_safety_prompt=prepared["safety_prompt"],
            ai_confirmation="我确认这代表长期意愿。",
        )
        self.assertEqual("reject", same_checkpoint["decision"])

        activated = self.service.activate_candidate(
            candidate_id=stored["candidate_id"],
            checkpoint_id="checkpoint-2",
            expected_active_revision=None,
            idempotency_key="activate-2",
            presented_safety_prompt=prepared["safety_prompt"],
            ai_confirmation="我在独立检查点重新复核，仍确认这代表我的长期意愿。",
        )
        self.assertEqual("activate", activated["decision"])

        injection = self.service.get_active(
            checkpoint_id="checkpoint-2-read", facet_names=["technical"]
        )
        self.assertEqual(1, injection["active_identity_capsule"]["current_effective_version"])
        self.assertEqual(
            "我在技术协作时重视证据与可复现性。", injection["facets"]["technical"]
        )

        found = self.service.search(query="长期身份", include_content=True)
        self.assertGreaterEqual(found["count"], 1)
        self.assertTrue(any(item["kind"] == "active_revision" for item in found["results"]))

    def test_owner_scope_rejects_foreign_candidate(self) -> None:
        with self.assertRaisesRegex(SelfRevisionError, "not found for this self-model"):
            self.service.activate_candidate(
                candidate_id="cand_foreign",
                checkpoint_id="checkpoint-2",
                expected_active_revision=None,
                idempotency_key="activate-foreign",
                presented_safety_prompt=self.service.prepare()["safety_prompt"],
                ai_confirmation="确认",
            )

    def test_search_hides_candidate_content_by_default(self) -> None:
        prepared = self.service.prepare()
        self.service.store_candidate(
            content=model_content("hidden-candidate"),
            diff=[{"op": "add", "path": "/active_identity_capsule"}],
            reason="保存候选用于审核。",
            evidence_refs=["memory://identity/name-origin"],
            checkpoint_id="checkpoint-1",
            expected_active_revision=None,
            idempotency_key="store-hidden",
            presented_safety_prompt=prepared["safety_prompt"],
        )
        result = self.service.search(scope="candidates")
        self.assertEqual(1, result["count"])
        item = result["results"][0]["item"]
        self.assertNotIn("content", item)
        self.assertNotIn("reason", item)
        self.assertNotIn("diff", item)
        self.assertNotIn("evidence_refs", item)
        hidden_match = self.service.search(
            scope="candidates", query="保存候选用于审核"
        )
        self.assertEqual(0, hidden_match["count"])
        revealed = self.service.search(scope="candidates", include_content=True)
        revealed_item = revealed["results"][0]["item"]
        self.assertIn("content", revealed_item)
        self.assertIn("reason", revealed_item)
        self.assertIn("diff", revealed_item)
        self.assertIn("evidence_refs", revealed_item)


if __name__ == "__main__":
    unittest.main()
