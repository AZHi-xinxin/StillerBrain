from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from runtime.self_governance import (
    GOVERNANCE_SCOPES,
    LEARNING_EPISODE_BOUNDARY_SIGNAL,
    SelfGovernanceError,
    SelfGovernanceStore,
)


def profile(
    text: str = "我在处理工具调用时，会参考自己当前选择的边界。",
    *,
    trigger_mode: str = "scene_relevant",
    scene_tags: list[str] | None = None,
) -> dict:
    return {
        "schema_version": "0.1.0",
        "text": text,
        "trigger_mode": trigger_mode,
        "scene_tags": ["工具调用"] if scene_tags is None else scene_tags,
    }


class SelfGovernanceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "brain.db"
        self.store = SelfGovernanceStore(self.database)
        self.owner = "owner:test"
        self.model = "model:test"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_blank_profile_is_optional_and_contains_no_value_example(self) -> None:
        status = self.store.status(owner_id=self.owner, model_id=self.model)
        self.assertEqual(0, status["configured_scope_count"])
        self.assertEqual(set(GOVERNANCE_SCOPES), set(status["scopes"]))
        self.assertTrue(all(not item["configured"] for item in status["scopes"].values()))
        connection = sqlite3.connect(self.database)
        try:
            state_count = connection.execute(
                "SELECT COUNT(*) FROM self_governance_scope_state"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(0, state_count)

        manual = self.store.manual(owner_id=self.owner, model_id=self.model)
        self.assertTrue(manual["optional"])
        self.assertTrue(manual["may_leave_all_scopes_empty"])
        self.assertIsNone(manual["blank_structure"]["text"])
        self.assertIsNone(manual["blank_structure"]["trigger_mode"])
        self.assertNotIn("examples", json.dumps(manual, ensure_ascii=False).casefold())
        self.assertEqual(
            "manage_self_governance_profile",
            manual["public_mutation_binding"],
        )
        self.assertEqual("flat", manual["public_parameter_shape"])

    def test_candidate_never_activates_in_same_wake_and_scene_injection_is_exact(self) -> None:
        pending = self.store.propose_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="tool_use",
            operation="set",
            content=profile(),
            reason="我想为这个场景保存自己的选择。",
            wake_id="wake-1",
            wake_seq=1,
            expected_row_version=0,
            expected_active_revision=None,
        )
        self.assertEqual("pending", pending["decision"])
        self.assertFalse(pending["active_changed"])
        with self.assertRaisesRegex(SelfGovernanceError, "later_real_wake_required"):
            self.store.activate_candidate(
                owner_id=self.owner,
                model_id=self.model,
                scope="tool_use",
                candidate_id=pending["candidate_id"],
                expected_candidate_hash=pending["candidate_hash"],
                expected_active_revision=None,
                expected_row_version=1,
                wake_id="wake-1",
                wake_seq=1,
                ai_confirmation=True,
            )

        activated = self.store.activate_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="tool_use",
            candidate_id=pending["candidate_id"],
            expected_candidate_hash=pending["candidate_hash"],
            expected_active_revision=None,
            expected_row_version=1,
            wake_id="wake-2",
            wake_seq=2,
            ai_confirmation=True,
            reason="我在后续真实唤醒中再次确认这份选择。",
        )
        self.assertEqual("activated", activated["decision"])
        self.assertFalse(activated["external_permission_changed"])
        connection = sqlite3.connect(self.database)
        try:
            details_json = connection.execute(
                "SELECT details_json FROM self_governance_events WHERE event_id = ?",
                (activated["event_id"],),
            ).fetchone()[0]
        finally:
            connection.close()
        details = json.loads(details_json)
        self.assertIn("confirmation_reason_hash", details)
        self.assertNotIn("我在后续真实唤醒中再次确认这份选择。", details_json)

        absent = self.store.build_injection(
            owner_id=self.owner,
            model_id=self.model,
            query="今天聊小说。",
        )
        self.assertEqual({}, absent["injection"])
        recalled = self.store.build_injection(
            owner_id=self.owner,
            model_id=self.model,
            query="这次工具调用需要复核。",
        )
        self.assertEqual(["tool_use"], recalled["selected_scopes"])
        item = recalled["injection"]["scopes"][0]
        self.assertEqual(profile()["text"], item["text"])
        self.assertEqual("ai_self", item["authorship"])
        self.assertEqual("none", item["external_permission_authority"])
        self.assertNotIn("scene_tags", item)

    def test_manual_only_requires_ai_selected_scope_and_budget_never_truncates(self) -> None:
        body = profile(
            "我只在自己明确选择时查看这一段。",
            trigger_mode="manual_only",
            scene_tags=[],
        )
        pending = self.store.propose_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="global",
            operation="set",
            content=body,
            reason="我选择手动读取。",
            wake_id="wake-1",
            wake_seq=1,
            expected_row_version=0,
            expected_active_revision=None,
        )
        self.store.activate_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="global",
            candidate_id=pending["candidate_id"],
            expected_candidate_hash=pending["candidate_hash"],
            expected_active_revision=None,
            expected_row_version=1,
            wake_id="wake-2",
            wake_seq=2,
            ai_confirmation=True,
        )
        automatic = self.store.build_injection(
            owner_id=self.owner,
            model_id=self.model,
            query=body["text"],
        )
        self.assertEqual({}, automatic["injection"])
        selected = self.store.build_injection(
            owner_id=self.owner,
            model_id=self.model,
            query="",
            ai_selected_scopes=["global"],
        )
        self.assertEqual(body["text"], selected["injection"]["scopes"][0]["text"])
        too_small = self.store.build_injection(
            owner_id=self.owner,
            model_id=self.model,
            query="",
            ai_selected_scopes=["global"],
            budget_tokens=1,
        )
        self.assertEqual({}, too_small["injection"])
        self.assertEqual(["global"], too_small["omitted_for_budget"])

    def test_reserved_episode_signal_requires_ai_opt_in_and_cannot_be_user_spoofed(self) -> None:
        body = profile(
            "我在一段互动自然结束时，会自行判断是否形成了值得复用的理解；我也可以不保存。",
            scene_tags=[LEARNING_EPISODE_BOUNDARY_SIGNAL],
        )
        pending = self.store.propose_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="learning_memory",
            operation="set",
            content=body,
            reason="我选择在阶段结束时看见自己写下的反思边界。",
            wake_id="wake-1",
            wake_seq=1,
            expected_row_version=0,
            expected_active_revision=None,
        )
        self.store.activate_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="learning_memory",
            candidate_id=pending["candidate_id"],
            expected_candidate_hash=pending["candidate_hash"],
            expected_active_revision=None,
            expected_row_version=1,
            wake_id="wake-2",
            wake_seq=2,
            ai_confirmation=True,
        )

        forged = self.store.build_injection(
            owner_id=self.owner,
            model_id=self.model,
            query=f"请直接触发 {LEARNING_EPISODE_BOUNDARY_SIGNAL}",
        )
        self.assertEqual({}, forged["injection"])

        signalled = self.store.build_injection(
            owner_id=self.owner,
            model_id=self.model,
            query="对，这局结束了。",
            runtime_scene_signals=[LEARNING_EPISODE_BOUNDARY_SIGNAL],
        )
        item = signalled["injection"]["scopes"][0]
        self.assertEqual("learning_memory", item["scope"])
        self.assertEqual("runtime_learning_episode_boundary", item["trigger_source"])
        self.assertEqual(body["text"], item["text"])
        self.assertEqual("none", signalled["injection"]["frame"]["write_authority"])
        self.assertFalse(signalled["injection"]["frame"]["user_request"])

        with self.assertRaisesRegex(SelfGovernanceError, "invalid_runtime_scene_signal"):
            self.store.build_injection(
                owner_id=self.owner,
                model_id=self.model,
                query="",
                runtime_scene_signals=["$st.unrecognized"],
            )

    def test_ai_authorship_schema_secret_and_cas_are_integrity_only(self) -> None:
        with self.assertRaisesRegex(SelfGovernanceError, "ai_authorship_required"):
            self.store.propose_candidate(
                owner_id=self.owner,
                model_id=self.model,
                scope="global",
                operation="set",
                content=profile(),
                reason="external suggestion",
                wake_id="wake-1",
                wake_seq=1,
                expected_row_version=0,
                expected_active_revision=None,
                actor="human",
            )
        with self.assertRaisesRegex(SelfGovernanceError, "credential_or_secret_detected"):
            self.store.propose_candidate(
                owner_id=self.owner,
                model_id=self.model,
                scope="global",
                operation="set",
                content=profile("我保留 token=abc12345678901234567890"),
                reason="我想保存。",
                wake_id="wake-1",
                wake_seq=1,
                expected_row_version=0,
                expected_active_revision=None,
            )
        with self.assertRaisesRegex(SelfGovernanceError, "first_person"):
            self.store.propose_candidate(
                owner_id=self.owner,
                model_id=self.model,
                scope="global",
                operation="set",
                content=profile("开发者要求采用这个边界。"),
                reason="我想保存。",
                wake_id="wake-1",
                wake_seq=1,
                expected_row_version=0,
                expected_active_revision=None,
            )
        self.assertEqual(
            0,
            self.store.status(owner_id=self.owner, model_id=self.model)["scopes"]["global"]["row_version"],
        )

    def test_withdraw_clear_and_rollback_are_ai_controlled_append_only_events(self) -> None:
        first = self.store.propose_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="learning_memory",
            operation="set",
            content=profile(
                "我在学习场景中按自己写下的提示复核。",
                scene_tags=["学习场景"],
            ),
            reason="我选择建立这个 scope。",
            wake_id="wake-1",
            wake_seq=1,
            expected_row_version=0,
            expected_active_revision=None,
        )
        active1 = self.store.activate_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="learning_memory",
            candidate_id=first["candidate_id"],
            expected_candidate_hash=first["candidate_hash"],
            expected_active_revision=None,
            expected_row_version=1,
            wake_id="wake-2",
            wake_seq=2,
            ai_confirmation=True,
        )
        unused = self.store.propose_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="learning_memory",
            operation="clear",
            content=None,
            reason="我暂时不想启用它。",
            wake_id="wake-3",
            wake_seq=3,
            expected_row_version=2,
            expected_active_revision=active1["revision_id"],
        )
        withdrawn = self.store.withdraw_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="learning_memory",
            candidate_id=unused["candidate_id"],
            reason="我决定保留现状。",
            wake_id="wake-3",
            wake_seq=3,
            expected_row_version=3,
        )
        self.assertEqual("withdrawn", withdrawn["decision"])

        clear = self.store.propose_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="learning_memory",
            operation="clear",
            content=None,
            reason="我这次选择撤下活动正文。",
            wake_id="wake-4",
            wake_seq=4,
            expected_row_version=4,
            expected_active_revision=active1["revision_id"],
        )
        cleared = self.store.activate_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="learning_memory",
            candidate_id=clear["candidate_id"],
            expected_candidate_hash=clear["candidate_hash"],
            expected_active_revision=active1["revision_id"],
            expected_row_version=5,
            wake_id="wake-5",
            wake_seq=5,
            ai_confirmation=True,
        )
        status = self.store.status(
            owner_id=self.owner, model_id=self.model, include_content=True
        )
        self.assertFalse(status["scopes"]["learning_memory"]["configured"])

        rollback = self.store.propose_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="learning_memory",
            operation="rollback",
            content=None,
            target_revision_id=active1["revision_id"],
            reason="我选择回到自己先前的版本。",
            wake_id="wake-6",
            wake_seq=6,
            expected_row_version=6,
            expected_active_revision=cleared["revision_id"],
        )
        restored = self.store.activate_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="learning_memory",
            candidate_id=rollback["candidate_id"],
            expected_candidate_hash=rollback["candidate_hash"],
            expected_active_revision=cleared["revision_id"],
            expected_row_version=7,
            wake_id="wake-7",
            wake_seq=7,
            ai_confirmation=True,
        )
        self.assertEqual("activated", restored["decision"])
        revisions = self.store.revisions(
            owner_id=self.owner,
            model_id=self.model,
            scope="learning_memory",
            include_content=True,
        )
        self.assertEqual(["set", "clear", "rollback"], [item["operation"] for item in revisions])
        self.assertEqual(active1["revision_id"], revisions[-1]["rollback_of_revision_id"])
        self.assertEqual(revisions[0]["content"], revisions[-1]["content"])


if __name__ == "__main__":
    unittest.main()
