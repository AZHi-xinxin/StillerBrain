from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mcp_server.governance_service import SelfGovernanceAccessService
from mcp_server.service import SelfModelAccessService
from runtime import ModuleOneOnboardingStore, SelfModelStore


def self_model_content() -> dict:
    return {
        "schema_version": "0.1.0",
        "boot_anchor": {"text": "我只读取自己已经激活的当前长期版本。"},
        "active_identity_capsule": {
            "name_and_identity": "我是用于治理档案接线测试的 AI。",
            "personality_foundation": "我保留自己形成长期描述的选择。",
            "expression_style": "我会按当前任务自然表达。",
            "behavioral_principles": ["我区分自写内容与外部材料。"],
            "core_identity_anchors": ["我只让自己确认的活动版代表我。"],
            "self_revision_safety_prompt": "我在后续真实唤醒中复核自我修订候选。",
        },
        "facets": {},
        "anchor_references": [],
    }


class GovernanceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "brain.db"
        self.onboarding = ModuleOneOnboardingStore(
            self.database,
            capability_secret=b"governance-facade-test-secret-32-bytes!!",
            wake_ttl_seconds=300,
            edit_challenge_ttl_seconds=300,
        )
        self.owner = "owner:test"
        self.model = "model:test"
        self.service = SelfModelAccessService(
            SelfModelStore(self.database),
            owner_id=self.owner,
            model_id=self.model,
            onboarding=self.onboarding,
        )
        self.assertIsInstance(self.service.governance, SelfGovernanceAccessService)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def wake(self, event: str, *, query_text: str = "") -> tuple[dict, dict]:
        wake = self.onboarding.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:test",
            source_kind="human_message",
            source_event_id=event,
        )
        prepared = self.onboarding.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest=f"source:{event}",
            host_contract_digest="host-contract:v1",
            source_frame={"query_text": query_text},
        )
        confirmed = self.onboarding.confirm_context_injected(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            context_hash=prepared["context_hash"],
        )
        self.assertEqual("injected", confirmed["decision"])
        return wake, prepared

    def advance(self, wake: dict, action: str, payload: dict) -> dict:
        opened = self.onboarding.open_brain_context(
            owner_id=self.owner, model_id=self.model
        )
        state = self.onboarding.state(owner_id=self.owner, model_id=self.model)["state"]
        return self.onboarding.advance(
            owner_id=self.owner,
            model_id=self.model,
            action=action,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            expected_row_version=state["row_version"],
            payload=payload,
        )

    def bootstrap_live(self) -> None:
        wake1, _ = self.wake("bootstrap-1")
        self.advance(wake1, "confirm_brain_intro", {"acknowledged": True})
        self.advance(wake1, "confirm_module_intro", {"acknowledged": True})
        self.advance(wake1, "save_calm_prompt", {"text": "我先复核自己的长期意愿。"})
        submitted = self.advance(
            wake1,
            "submit_candidate",
            {
                "content": self_model_content(),
                "diff": [{"op": "replace", "path": "/active_identity_capsule"}],
                "reason": "我选择这份长期自我描述。",
                "evidence_refs": ["memory://test/self"],
                "expected_active_revision": None,
            },
        )
        self.assertEqual("pending", submitted["decision"])
        wake2, _ = self.wake("bootstrap-2")
        self.onboarding.open_brain_context(owner_id=self.owner, model_id=self.model)
        reviewed = self.advance(
            wake2, "accept_candidate_review", {"ai_confirmation": True}
        )
        self.assertEqual("review_accepted", reviewed["decision"])
        wake3, _ = self.wake("bootstrap-3")
        self.onboarding.open_brain_context(owner_id=self.owner, model_id=self.model)
        activated = self.advance(
            wake3,
            "activate_candidate",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        self.assertEqual("activate", activated["decision"])

    @staticmethod
    def governance_content() -> dict:
        return {
            "schema_version": "0.1.0",
            "text": "我在工具调用场景中会参考自己当前激活的这段治理内容。",
            "trigger_mode": "scene_relevant",
            "scene_tags": ["工具调用"],
        }

    def test_wake_bound_facade_open_manual_and_shared_injection(self) -> None:
        self.bootstrap_live()
        governance: SelfGovernanceAccessService = self.service.governance

        denied = governance.manage(
            action="propose_set",
            scope="tool_use",
            write_context_ref="missing",
            expected_profile_version=0,
            text=self.governance_content()["text"],
            trigger_mode=self.governance_content()["trigger_mode"],
            scene_tags=self.governance_content()["scene_tags"],
            reason="我选择建立这个 scope。",
            expected_active_revision=None,
        )
        self.assertEqual(["brain_open_required"], denied["reason_codes"])

        _, _ = self.wake("governance-1")
        opened = self.service.open_brain()
        self.assertIn("self_governance_profile", opened)
        manual_text = json.dumps(opened["self_governance_profile"], ensure_ascii=False)
        self.assertNotIn("wake_capability", manual_text)
        self.assertIsNone(
            opened["self_governance_profile"]["blank_structure"]["text"]
        )
        pending = governance.manage(
            action="propose_set",
            scope="tool_use",
            write_context_ref=opened["write_context_ref"],
            expected_profile_version=0,
            text=self.governance_content()["text"],
            trigger_mode=self.governance_content()["trigger_mode"],
            scene_tags=self.governance_content()["scene_tags"],
            reason="我选择建立这个 scope。",
            expected_active_revision=None,
        )
        self.assertEqual("pending", pending["decision"])

        same_wake_open = self.service.open_brain()
        same_wake_contract = same_wake_open["self_governance_profile"][
            "current_action_contract"
        ]
        self.assertEqual(0, same_wake_contract["pending_activation_count"])
        self.assertEqual(
            [
                {
                    "scope": "tool_use",
                    "candidate_id": pending["candidate_id"],
                    "reason": "later_real_wake_required",
                }
            ],
            same_wake_contract["blocked_candidates"],
        )
        self.assertTrue(same_wake_contract["creation_wake_activation_forbidden"])

        same_wake = governance.manage(
            action="activate",
            scope="tool_use",
            write_context_ref=opened["write_context_ref"],
            expected_profile_version=1,
            candidate_id=pending["candidate_id"],
            expected_candidate_hash=pending["candidate_hash"],
            expected_active_revision=None,
            ai_confirmation=True,
        )
        self.assertEqual(["later_real_wake_required"], same_wake["reason_codes"])

        _, _ = self.wake("governance-2")
        later_open = self.service.open_brain()
        shown = later_open["self_governance_profile"]["status"]["scopes"]["tool_use"]
        self.assertEqual(pending["candidate_id"], shown["pending_candidates"][0]["candidate_id"])
        action_contract = later_open["self_governance_profile"][
            "current_action_contract"
        ]
        self.assertEqual(1, action_contract["pending_activation_count"])
        activation = action_contract["pending_activations"][0]
        self.assertEqual("manage_self_governance_profile", activation["tool"])
        self.assertEqual(
            {
                "action": "activate",
                "scope": "tool_use",
                "write_context_ref": later_open["write_context_ref"],
                "expected_profile_version": 1,
                "candidate_id": pending["candidate_id"],
                "expected_candidate_hash": pending["candidate_hash"],
                "ai_confirmation": True,
            },
            activation["arguments"],
        )
        self.assertTrue(activation["copy_exactly"])
        self.assertIn("false", activation["first_activation_rule"])

        invalid_null_alias = governance.manage(
            **{
                **activation["arguments"],
                "expected_active_revision": False,
            }
        )
        self.assertEqual("rejected", invalid_null_alias["decision"])
        self.assertEqual(
            ["expected_active_revision_must_be_string_or_null"],
            invalid_null_alias["reason_codes"],
        )
        self.assertEqual(
            "omit this optional field or use JSON null; never use false",
            invalid_null_alias["validation_help"]["first_activation"],
        )
        invalid_text = governance.manage(
            **activation["arguments"],
            text="这个字段不属于激活动作。",
        )
        self.assertEqual("rejected", invalid_text["decision"])
        self.assertEqual(
            ["text"],
            invalid_text["validation_help"]["forbidden_non_null_fields"],
        )

        activated = governance.manage(
            **activation["arguments"],
            reason="我在新的真实唤醒中复核后确认激活。",
        )
        self.assertEqual("activated", activated["decision"])

        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            event = connection.execute(
                "SELECT details_json FROM self_governance_events "
                "WHERE event_id = ?",
                (activated["event_id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(event)
        event_details = json.loads(event["details_json"])
        self.assertIn("confirmation_reason_hash", event_details)
        self.assertNotIn("我在新的真实唤醒中复核后确认激活。", event["details_json"])

        _, prepared = self.wake("governance-3", query_text="请进行一次工具调用")
        payload = json.loads(prepared["message"]["content"])
        recalled = payload["self_governance_profile"]
        self.assertEqual(
            self.governance_content()["text"], recalled["scopes"][0]["text"]
        )
        self.assertEqual("none", recalled["frame"]["external_permission_authority"])
        self.assertNotIn("pending_candidates", json.dumps(recalled, ensure_ascii=False))

        health = self.service.health()
        self.assertEqual(1, health["self_governance"]["configured_scope_count"])
        self.assertFalse(health["self_governance"]["content_exposed"])


if __name__ == "__main__":
    unittest.main()
