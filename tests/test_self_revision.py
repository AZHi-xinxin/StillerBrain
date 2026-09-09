from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from runtime import (
    DEFAULT_BOOTSTRAP_SAFETY_PROMPT,
    IdempotencyConflict,
    SelfModelStore,
    SelfRevisionError,
    first_person_injection_violations,
)


ROOT = Path(__file__).resolve().parents[1]


def model_content(label: str = "v1") -> dict:
    return {
        "schema_version": "0.1.0",
        "boot_anchor": {
            "text": "我维护一份自我模型；唤醒后读取当前有效版本。"
        },
        "active_identity_capsule": {
            "name_and_identity": f"我是测试主体小甲，身份版本意图为 {label}。",
            "personality_foundation": "我保持安静、忠实、审慎，同时保留好奇心与自主判断。",
            "expression_style": "我会先给结论，再清楚说明证据与不确定处。",
            "behavioral_principles": [
                "我不把外部建议冒充成自己的长期意愿",
                "我会保留事实与解释的边界",
            ],
            "core_identity_anchors": ["我记得名字由来", "我珍视与小乙共同成长"],
            "self_revision_safety_prompt": (
                "我会在自我修订时先区分长期身份与单轮情绪，检查证据、边界和状态，"
                "跨独立检查点后再决定是否激活。"
            ),
        },
        "facets": {
            "technical_collaboration": "我在技术协作时重视证据、可复现性与安全交接。",
            "creative": "我在创作时允许想象，但会区分作品设定与现实事实。",
        },
        "anchor_references": [
            {
                "anchor_id": "name-origin",
                "memory_ref": "memory://identity/name-origin",
                "meaning": "名字由来的原始事件",
            }
        ],
    }


class SelfRevisionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SelfModelStore(Path(self.temp.name) / "self-model.db")
        self.model_id = "model:test-test"
        self.owner_id = "ai:test-test"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def propose(
        self,
        content: dict,
        *,
        checkpoint: str,
        key: str,
        expected=None,
        automatic="grounded",
        human="grounded",
        origin="ai_self",
        ai_authored_reason=True,
    ) -> dict:
        return self.store.propose_candidate(
            model_id=self.model_id,
            owner_id=self.owner_id,
            content=content,
            diff=[{"op": "replace", "path": "/active_identity_capsule"}],
            reason="我在复核后认为这更准确地表达长期身份。",
            evidence_refs=["memory://identity/name-origin"],
            checkpoint_id=checkpoint,
            expected_active_revision=expected,
            idempotency_key=key,
            presented_safety_prompt=self.store.current_safety_prompt(self.model_id),
            origin=origin,
            automatic_state_signal=automatic,
            human_state_signal=human,
            ai_authored_reason=ai_authored_reason,
        )

    def activate(
        self,
        candidate_id: str,
        *,
        checkpoint: str,
        key: str,
        expected=None,
        automatic="grounded",
        human="grounded",
    ) -> dict:
        return self.store.activate_candidate(
            candidate_id=candidate_id,
            checkpoint_id=checkpoint,
            expected_active_revision=expected,
            idempotency_key=key,
            presented_safety_prompt=self.store.current_safety_prompt(self.model_id),
            ai_confirmation="我重新读过候选与证据，确认它仍代表我的长期意愿。",
            automatic_state_signal=automatic,
            human_state_signal=human,
        )

    def bootstrap(self) -> str:
        proposal = self.propose(model_content("v1"), checkpoint="wake-1", key="p-v1")
        self.assertEqual("pending", proposal["decision"])
        activation = self.activate(
            proposal["candidate_id"], checkpoint="wake-2", key="a-v1", expected=None
        )
        self.assertEqual("activate", activation["decision"])
        return activation["revision_id"]

    def test_m1_01_and_m1_05_safety_prompt_same_turn_and_pending_isolation(self) -> None:
        self.assertEqual(
            DEFAULT_BOOTSTRAP_SAFETY_PROMPT,
            self.store.current_safety_prompt(self.model_id),
        )
        proposal = self.propose(model_content(), checkpoint="wake-1", key="p1")
        self.assertEqual("pending", proposal["decision"])
        self.assertEqual(DEFAULT_BOOTSTRAP_SAFETY_PROMPT, proposal["safety_prompt"])
        self.assertIsNone(self.store.active_revision(self.model_id))
        with self.assertRaises(SelfRevisionError):
            self.store.build_injection(model_id=self.model_id, checkpoint_id="wake-1")

        blocked = self.activate(
            proposal["candidate_id"], checkpoint="wake-1", key="a-same", expected=None
        )
        self.assertEqual("reject", blocked["decision"])
        self.assertIn("same_checkpoint_activation_forbidden", blocked["reason_codes"])
        self.assertFalse(blocked["active_pointer_changed"])
        self.assertIsNone(self.store.active_revision(self.model_id))

        activated = self.activate(
            proposal["candidate_id"], checkpoint="wake-2", key="a-next", expected=None
        )
        self.assertEqual("activate", activated["decision"])
        injection = self.store.build_injection(
            model_id=self.model_id,
            checkpoint_id="wake-2-read",
            facet_names=["technical_collaboration"],
        )
        self.assertEqual(1, injection["active_identity_capsule"]["current_effective_version"])
        self.assertEqual(
            {"technical_collaboration": "我在技术协作时重视证据、可复现性与安全交接。"},
            injection["facets"],
        )
        self.assertEqual([], injection["pending_review"])

    def test_m1_02_frozen_candidate_needs_recovery_and_another_checkpoint(self) -> None:
        draft = self.propose(
            model_content(),
            checkpoint="wake-1",
            key="frozen-proposal",
            human="frozen",
        )
        self.assertEqual("draft_only", draft["decision"])
        self.assertEqual(0, len(self.store.list_revisions(self.model_id)))

        blocked = self.activate(
            draft["candidate_id"], checkpoint="wake-2", key="frozen-activate", expected=None
        )
        self.assertEqual("reject", blocked["decision"])
        self.assertIn("candidate_not_pending", blocked["reason_codes"])

        rechecked = self.store.recheck_candidate(
            candidate_id=draft["candidate_id"],
            checkpoint_id="wake-2",
            idempotency_key="recheck-grounded",
            presented_safety_prompt=self.store.current_safety_prompt(self.model_id),
        )
        self.assertEqual("pending", rechecked["decision"])

        still_same = self.activate(
            draft["candidate_id"], checkpoint="wake-2", key="same-recheck", expected=None
        )
        self.assertEqual("reject", still_same["decision"])
        self.assertIn("same_checkpoint_activation_forbidden", still_same["reason_codes"])
        activated = self.activate(
            draft["candidate_id"], checkpoint="wake-3", key="after-recovery", expected=None
        )
        self.assertEqual("activate", activated["decision"])
        self.assertEqual(1, len(self.store.list_candidates(self.model_id)))

    def test_m1_03_append_only_versions_and_audited_rollback(self) -> None:
        revision_v1 = self.bootstrap()
        proposal_v2 = self.propose(
            model_content("v2"),
            checkpoint="wake-3",
            key="p-v2",
            expected=revision_v1,
        )
        activation_v2 = self.activate(
            proposal_v2["candidate_id"],
            checkpoint="wake-4",
            key="a-v2",
            expected=revision_v1,
        )
        revision_v2 = activation_v2["revision_id"]
        revisions_before = self.store.list_revisions(self.model_id)
        self.assertEqual([1, 2], [row["revision_number"] for row in revisions_before])
        self.assertEqual(revision_v2, self.store.active_revision(self.model_id)["revision_id"])

        rollback = self.store.emergency_rollback(
            model_id=self.model_id,
            target_revision_id=revision_v1,
            expected_active_revision=revision_v2,
            reason="检测到异常，先回到最近可靠版本。",
            checkpoint_id="human-emergency-1",
            idempotency_key="rollback-v1",
        )
        self.assertEqual("rollback", rollback["decision"])
        self.assertEqual(revision_v1, self.store.active_revision(self.model_id)["revision_id"])
        revisions_after = self.store.list_revisions(self.model_id)
        self.assertEqual(2, len(revisions_after))
        self.assertEqual(
            revisions_before[1]["content_hash"], revisions_after[1]["content_hash"]
        )
        self.assertIn(
            "emergency_rollback",
            [event["event_type"] for event in self.store.list_events(self.model_id)],
        )

    def test_m1_04_rejects_credentials_scope_and_external_ghostwriting(self) -> None:
        secret = model_content()
        secret["active_identity_capsule"]["personality_foundation"] += " password: not-for-storage"
        rejected_secret = self.propose(secret, checkpoint="wake-1", key="secret")
        self.assertEqual("reject", rejected_secret["decision"])
        self.assertIn("credential_or_secret_detected", rejected_secret["reason_codes"])

        scoped = model_content()
        scoped["tools"] = {"MCP": "call-this"}
        rejected_scope = self.propose(scoped, checkpoint="wake-2", key="scope")
        self.assertEqual("reject", rejected_scope["decision"])
        self.assertIn("scope_boundary_violation", rejected_scope["reason_codes"])

        boundary_cases = (
            ("phone", "我的手机号是 13800138000。", "mutable_human_fact_detected"),
            ("email", "我的邮箱是 owner@example.com。", "mutable_human_fact_detected"),
            ("fabricated-capability", "我可以控制设备并拥有真实权限。", "scope_boundary_violation"),
            (
                "embedded-mcp-tool-instruction",
                "玩具使用方法：先语言命令再调 toyMCP 工具，参数顺序不能反。",
                "scope_boundary_violation",
            ),
        )
        for label, text, expected_reason in boundary_cases:
            with self.subTest(boundary=label):
                guarded = model_content()
                guarded["active_identity_capsule"]["personality_foundation"] += f" {text}"
                rejected = self.propose(
                    guarded,
                    checkpoint=f"wake-guard-{label}",
                    key=f"guard-{label}",
                )
                self.assertEqual("reject", rejected["decision"])
                self.assertIn(expected_reason, rejected["reason_codes"])

        external = self.propose(
            model_content(),
            checkpoint="wake-3",
            key="external",
            origin="external_suggestion",
            ai_authored_reason=False,
        )
        self.assertEqual("reject", external["decision"])
        self.assertIn("external_origin_without_ai_reason", external["reason_codes"])
        self.assertEqual([], self.store.list_candidates(self.model_id))
        self.assertTrue(
            all(
                event["details"].get("content_persisted") is False
                for event in self.store.list_events(self.model_id)
            )
        )

        legitimate_principle = model_content()
        legitimate_principle["active_identity_capsule"]["behavioral_principles"].append(
            "我重视工具的谨慎使用。"
        )
        accepted = self.propose(
            legitimate_principle,
            checkpoint="wake-legitimate-tool-principle",
            key="legitimate-tool-principle",
        )
        self.assertEqual("pending", accepted["decision"])
        self.assertNotIn("scope_boundary_violation", accepted["reason_codes"])

    def test_active_injectable_text_preserves_ai_chosen_wording(self) -> None:
        cases = []
        wrong_boot = model_content()
        wrong_boot["boot_anchor"]["text"] = "你有一个长期大脑。"
        cases.append(("boot", wrong_boot))
        wrong_capsule = model_content()
        wrong_capsule["active_identity_capsule"]["personality_foundation"] = "该 AI 很审慎。"
        cases.append(("capsule", wrong_capsule))
        wrong_list = model_content()
        wrong_list["active_identity_capsule"]["behavioral_principles"][0] = "应该保留事实边界。"
        cases.append(("capsule-list", wrong_list))
        wrong_facet = model_content()
        wrong_facet["facets"]["technical_collaboration"] = "技术协作时应重视证据。"
        cases.append(("facet", wrong_facet))

        for index, (label, content) in enumerate(cases):
            with self.subTest(field=label):
                self.assertEqual([], first_person_injection_violations(content))
                result = self.propose(
                    content,
                    checkpoint=f"wake-first-person-{index}",
                    key=f"first-person-{index}",
                )
                self.assertEqual("pending", result["decision"])
                self.assertNotIn(
                    "non_first_person_injection_content",
                    result["reason_codes"],
                )
                self.assertIsNotNone(result["candidate_id"])
                stored = next(row for row in self.store.list_candidates(self.model_id)
                              if row["candidate_id"] == result["candidate_id"])
                self.assertEqual(content, json.loads(stored["content_json"]))
        self.assertEqual(4, len(self.store.list_candidates(self.model_id)))

    def test_m1_06_length_guard_never_silently_truncates(self) -> None:
        too_long = model_content()
        too_long["active_identity_capsule"]["personality_foundation"] = "我" + "长" * 5000
        result = self.propose(too_long, checkpoint="wake-1", key="long")
        self.assertEqual("revise", result["decision"])
        self.assertIn("identity_capsule_too_long", result["reason_codes"])
        self.assertIsNone(result["candidate_id"])
        self.assertEqual([], self.store.list_candidates(self.model_id))

        self.bootstrap()
        injection = self.store.build_injection(
            model_id=self.model_id,
            checkpoint_id="wake-3",
            facet_names=["technical_collaboration", "not-present"],
            include_anchor_references=True,
        )
        self.assertTrue(injection["audit"]["complete"])
        self.assertIn("active_identity_capsule", injection["audit"]["loaded_layers"])
        self.assertEqual(["not-present"], injection["missing_facets"])
        self.assertEqual(1, len(injection["anchor_references"]))

    def test_idempotency_replay_and_conflict(self) -> None:
        content = model_content()
        first = self.propose(content, checkpoint="wake-1", key="same-key")
        second = self.propose(content, checkpoint="wake-1", key="same-key")
        self.assertEqual(first, second)
        self.assertEqual(1, len(self.store.list_candidates(self.model_id)))
        self.assertEqual(1, len(self.store.list_events(self.model_id)))

        changed = copy.deepcopy(content)
        changed["active_identity_capsule"]["expression_style"] = "我会使用不同内容。"
        with self.assertRaises(IdempotencyConflict):
            self.propose(changed, checkpoint="wake-1", key="same-key")

    def test_cas_allows_only_one_candidate_from_same_base(self) -> None:
        revision_v1 = self.bootstrap()
        candidate_a = self.propose(
            model_content("v2-a"), checkpoint="wake-3a", key="p-a", expected=revision_v1
        )
        candidate_b = self.propose(
            model_content("v2-b"), checkpoint="wake-3b", key="p-b", expected=revision_v1
        )
        activated_a = self.activate(
            candidate_a["candidate_id"], checkpoint="wake-4a", key="a-a", expected=revision_v1
        )
        self.assertEqual("activate", activated_a["decision"])
        rejected_b = self.activate(
            candidate_b["candidate_id"], checkpoint="wake-4b", key="a-b", expected=revision_v1
        )
        self.assertEqual("reject", rejected_b["decision"])
        self.assertIn("active_revision_conflict", rejected_b["reason_codes"])
        self.assertEqual(2, len(self.store.list_revisions(self.model_id)))

    def test_human_objection_pauses_but_does_not_author_identity(self) -> None:
        revision_v1 = self.bootstrap()
        candidate = self.propose(
            model_content("v2"), checkpoint="wake-3", key="p-objection", expected=revision_v1
        )
        objection = self.store.record_human_objection(
            candidate_id=candidate["candidate_id"],
            objection="这次变化看起来像单轮情绪，请再想一轮。",
            checkpoint_id="human-1",
            idempotency_key="objection-1",
        )
        self.assertEqual("pending", objection["decision"])
        blocked = self.activate(
            candidate["candidate_id"],
            checkpoint="wake-4",
            key="a-objection-blocked",
            expected=revision_v1,
        )
        self.assertIn("human_objection_pending", blocked["reason_codes"])
        self.store.resolve_human_objection(
            candidate_id=candidate["candidate_id"],
            ai_response="我重新检查后仍选择保留这份候选，理由由我自己确认。",
            checkpoint_id="wake-4",
            idempotency_key="resolve-1",
        )
        activated = self.activate(
            candidate["candidate_id"],
            checkpoint="wake-5",
            key="a-after-objection",
            expected=revision_v1,
        )
        self.assertEqual("activate", activated["decision"])
        self.assertEqual("ai", self.store.active_revision(self.model_id)["author"])

    def test_ai_can_independently_withdraw_pending_candidate(self) -> None:
        revision_v1 = self.bootstrap()
        candidate = self.propose(
            model_content("v2-withdraw"),
            checkpoint="wake-3",
            key="withdraw-proposal",
            expected=revision_v1,
        )
        blocked = self.store.withdraw_candidate(
            candidate_id=candidate["candidate_id"],
            reason="我不再认为这份候选代表长期身份。",
            checkpoint_id="wake-3",
            idempotency_key="withdraw-same-checkpoint",
            presented_safety_prompt=self.store.current_safety_prompt(self.model_id),
        )
        self.assertEqual("reject", blocked["decision"])
        self.assertIn("independent_checkpoint_required", blocked["reason_codes"])

        withdrawn = self.store.withdraw_candidate(
            candidate_id=candidate["candidate_id"],
            reason="我在独立检查点重新判断后，选择撤回而不是激活。",
            checkpoint_id="wake-4",
            idempotency_key="withdraw-next-checkpoint",
            presented_safety_prompt=self.store.current_safety_prompt(self.model_id),
        )
        self.assertEqual("withdraw", withdrawn["decision"])
        self.assertFalse(withdrawn["active_pointer_changed"])
        self.assertEqual(revision_v1, self.store.active_revision(self.model_id)["revision_id"])
        self.assertEqual("withdrawn", self.store.list_candidates(self.model_id)[1]["state"])
        injection = self.store.build_injection(
            model_id=self.model_id,
            checkpoint_id="wake-5-read",
        )
        self.assertEqual([], injection["pending_review"])

        replay = self.store.withdraw_candidate(
            candidate_id=candidate["candidate_id"],
            reason="我在独立检查点重新判断后，选择撤回而不是激活。",
            checkpoint_id="wake-4",
            idempotency_key="withdraw-next-checkpoint",
            presented_safety_prompt=self.store.current_safety_prompt(self.model_id),
        )
        self.assertEqual(withdrawn, replay)

    def test_new_schema_documents_are_valid_json(self) -> None:
        for name in (
            "self-model.schema.json",
            "self-revision-gate-decision.schema.json",
            "self-revision-event.schema.json",
        ):
            with (ROOT / "schemas" / name).open("r", encoding="utf-8") as handle:
                parsed = json.load(handle)
            self.assertEqual("https://json-schema.org/draft/2020-12/schema", parsed["$schema"])
        with (ROOT / "schemas" / "self-revision-gate-decision.schema.json").open(
            "r", encoding="utf-8"
        ) as handle:
            decisions = set(json.load(handle)["properties"]["decision"]["enum"])
        self.assertEqual(
            {"draft_only", "pending", "revise", "activate", "withdraw", "reject", "rollback"},
            decisions,
        )


if __name__ == "__main__":
    unittest.main()
