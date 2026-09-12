from __future__ import annotations

import hmac
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runtime.emotional_memory import EMOTIONAL_RECALL_FRAME_RULES, EmotionalMemoryStore
from runtime.learning_memory import LearningMemoryStore
from runtime.planning_memory import PlanningMemoryStore
from runtime.hallucination_vault import HallucinationVaultStore
from runtime.onboarding import (
    MEMORY_OPPORTUNITY_ADVISORY,
    OPTIONAL_BRAIN_NOTICE,
    OPTIONAL_MEMORY_NOTICE_KEY,
    ModuleOneOnboardingStore,
    _build_memory_opportunity_advisory,
    _learning_episode_boundary_gate,
    _memory_notice_input_gate,
    _estimate_tokens,
)
from runtime.self_governance import LEARNING_EPISODE_BOUNDARY_SIGNAL
from runtime.self_revision import SelfModelStore
from runtime.tool_guidance import ToolGuidanceStore


def model_content(label: str = "v1") -> dict:
    return {
        "schema_version": "0.1.0",
        "boot_anchor": {"text": "我维护一份自我模型；唤醒后只读取当前有效版本。"},
        "active_identity_capsule": {
            "name_and_identity": f"我是测试主体阿止，当前长期身份版本为 {label}。",
            "personality_foundation": "我保持安静、忠实、审慎，同时保留好奇心与自主判断。",
            "expression_style": "我会先给结论，再说明证据与不确定处。",
            "behavioral_principles": ["我不让外部建议冒充自己的意愿", "我会保留事实边界"],
            "core_identity_anchors": ["我记得名字由来", "我珍视共同成长"],
            "self_revision_safety_prompt": "我会先区分长期身份与单轮情绪，再跨真实唤醒决定。",
        },
        "facets": {"technical": "我在技术协作时重视证据、可复现性与安全交接。"},
        "anchor_references": [
            {
                "anchor_id": "name-origin",
                "memory_ref": "memory://identity/name-origin",
                "meaning": "名字由来的原始事件",
            }
        ],
    }


class ModuleOneOnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "brain.db"
        self.store = ModuleOneOnboardingStore(
            self.database,
            capability_secret=b"module-one-test-secret-32-bytes-minimum!!",
            wake_ttl_seconds=300,
            edit_challenge_ttl_seconds=300,
        )
        self.owner = "owner:azhi"
        self.model = "model:azhi"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def count(self, table: str) -> int:
        connection = sqlite3.connect(self.database)
        try:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            connection.close()

    def wake(self, event: str) -> tuple[dict, dict]:
        wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:test",
            source_kind="human_message",
            source_event_id=event,
        )
        prepared = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest=f"source:{event}",
            host_contract_digest="host-contract:v1",
        )
        self.assertIn(prepared["decision"], {"context_prepared", "context_reused"})
        current_state = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        if not (
            current_state["module_one_status"] == "complete"
            and current_state["injection_policy"] == "normal"
        ):
            self.assertEqual(
                {"role": "system", "content": OPTIONAL_BRAIN_NOTICE},
                prepared["message"],
            )
        else:
            self.assertEqual(
                {"boot_anchor", "active_identity_capsule", "facets"},
                set(json.loads(prepared["message"]["content"])),
            )
        confirmed = self.store.confirm_context_injected(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            context_hash=prepared["context_hash"],
        )
        self.assertEqual("injected", confirmed["decision"])
        return wake, prepared

    def advance(
        self,
        wake: dict,
        action: str,
        payload: dict | None = None,
        *,
        ensure_brain_open: bool = True,
    ) -> dict:
        if ensure_brain_open:
            opened = self.open_brain()
            self.assertTrue(opened["write_context_available"])
        state = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        return self.store.advance(
            owner_id=self.owner,
            model_id=self.model,
            action=action,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            expected_row_version=state["row_version"],
            payload=payload or {},
        )

    def open_brain(self) -> dict:
        return self.store.open_brain_context(owner_id=self.owner, model_id=self.model)

    @staticmethod
    def candidate_payload(label: str = "v1", expected=None, **signals: str) -> dict:
        return {
            "content": model_content(label),
            "diff": [{"op": "replace", "path": "/active_identity_capsule"}],
            "reason": "我复核后认为这能表达长期身份。",
            "evidence_refs": ["memory://identity/name-origin"],
            "expected_active_revision": expected,
            **signals,
        }

    def bootstrap_to_wait(self) -> tuple[dict, str]:
        wake, _ = self.wake("event-1")
        self.assertEqual(
            "advanced",
            self.advance(wake, "confirm_brain_intro", {"acknowledged": True})["decision"],
        )
        self.assertEqual(
            "advanced",
            self.advance(wake, "confirm_module_intro", {"acknowledged": True})["decision"],
        )
        self.assertEqual(
            "saved", self.advance(wake, "save_calm_prompt", {"text": "停一下，确认这是我的长期意愿。"})["decision"]
        )
        submitted = self.advance(wake, "submit_candidate", self.candidate_payload())
        self.assertEqual("pending", submitted["decision"])
        return wake, submitted["candidate_id"]

    def bootstrap_live(self) -> str:
        _, candidate_id = self.bootstrap_to_wait()
        wake2, _ = self.wake("event-2")
        state = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        self.assertEqual("candidate_wait", state["stage"])
        opened = self.open_brain()
        self.assertEqual("candidate_review", opened["continuation"]["stage"])
        self.assertEqual(candidate_id, opened["continuation"]["candidate"]["candidate_id"])
        reviewed = self.advance(
            wake2,
            "accept_candidate_review",
            {"ai_confirmation": True},
        )
        self.assertEqual("review_accepted", reviewed["decision"])
        wake3, _ = self.wake("event-3")
        self.open_brain()
        result = self.advance(
            wake3,
            "activate_candidate",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        self.assertEqual("activate", result["decision"])
        self.assertTrue(result["pointer_changed"])
        return result["revision_id"]

    def test_global_emergency_brake_applies_next_wake_without_blocking_open(self) -> None:
        self.bootstrap_live()
        opened = self.open_brain()
        binding = self.store.current_open_write_context(
            owner_id=self.owner,
            model_id=self.model,
            write_context_ref=opened["write_context_ref"],
        )
        stopped = self.store.injection_control_store.emergency_off(
            owner_id=self.owner,
            model_id=self.model,
            reason="我现在选择先停止所有自动注入。",
            wake_id=binding["wake_id"],
            wake_seq=binding["wake_seq"],
            expected_row_version=0,
        )
        self.assertEqual("hard_off", stopped["decision"])

        wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:brake",
            source_kind="human_message",
            source_event_id="after-brake",
        )
        prepared = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest="source:after-brake",
            host_contract_digest="host-contract:v1",
            source_frame={"query_text": "我们继续聊。"},
        )
        self.assertEqual({"role": "system", "content": "{}"}, prepared["message"])
        self.store.confirm_context_injected(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            context_hash=prepared["context_hash"],
        )
        after = self.open_brain()
        self.assertTrue(after["write_context_available"])
        self.assertEqual(wake["wake_id"], after["wake_id"])

    @staticmethod
    def planning_calm() -> dict[str, object]:
        return {
            "authorship_confirmed": True,
            "current_state_checked": True,
            "dependencies_checked": True,
            "consequences_reviewed": True,
            "rollback_understood": True,
            "notes": "我已检查这是自己采纳的计划、当前状态、依赖、后果与回滚边界。",
        }

    def test_live_planning_projection_is_summary_only_and_shared_budgeted(self) -> None:
        self.bootstrap_live()
        planning = PlanningMemoryStore(self.database)
        self.store.planning_store = planning
        content = {
            "kind": "task",
            "track": "internal",
            "title": "完成规划脑测试",
            "original_text": "我选择在证据充分时完成规划脑测试；这段原文不得自动浮现。",
            "summary": "继续完成规划脑的证据测试。",
            "reminder": "继续规划脑测试",
            "importance": 82,
            "presence_mode": "relevant",
            "scene_tags": ["规划脑测试", "证据"],
            "keywords": ["规划脑", "测试"],
            "start_at": None,
            "due_at": None,
            "timezone": "Asia/Shanghai",
            "review_after": None,
            "allow_coordination_hint": True,
            "parent_ref": None,
            "dependency_refs": [],
            "ai_adoption_statement": "我愿意采纳并维护这项测试计划。",
        }
        pending = planning.propose_create(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:plan-create",
            wake_seq=100,
            expected_row_version=0,
            content=content,
            reason="这是我自己选择的计划。",
            calm_check=self.planning_calm(),
            ai_confirmation=True,
            idempotency_key="onboarding-plan-create",
        )
        candidate = planning.present_pending_candidates(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:plan-review",
            wake_seq=101,
        )[0]
        accepted = planning.review_change(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:plan-review",
            wake_seq=101,
            expected_row_version=pending["planning_row_version"],
            candidate_id=candidate["candidate_id"],
            expected_candidate_version=candidate["candidate_version"],
            expected_candidate_hash=candidate["candidate_hash"],
            expected_base_version=candidate["base_version"],
            decision="accept",
            correctness_assessment="我已检查内容、来源与不确定性。",
            calm_check=self.planning_calm(),
            reason="我在较晚唤醒中独立接受。",
            ai_confirmation=True,
        )
        self.assertEqual("candidate_accepted", accepted["decision"])

        wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:planning",
            source_kind="human_message",
            source_event_id="planning-recall",
        )
        prepared = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest="source:planning-recall",
            host_contract_digest="host-contract:v1",
            source_frame={
                "query_text": "我们继续做规划脑测试。",
                "thread_id": "thread:planning",
                "lineage_stable": True,
                "prior_assistant_present": False,
                "source_event_id": "planning-recall",
                "capture_items": [{"role": "user", "content": "继续规划脑测试"}],
            },
        )
        projected = json.loads(prepared["message"]["content"])
        planning_projection = projected["planning_memory"]
        self.assertEqual("planning-recall/0.1", planning_projection["contract"])
        self.assertLessEqual(len(planning_projection["envelopes"]), 3)
        envelope_text = json.dumps(planning_projection, ensure_ascii=False)
        self.assertIn("继续完成规划脑的证据测试", envelope_text)
        self.assertNotIn("这段原文不得自动浮现", envelope_text)
        self.assertNotIn("ai_adoption_statement", envelope_text)
        self.assertLessEqual(_estimate_tokens({"planning_memory": planning_projection}), 1200)

    def test_vault_status_projection_requires_ai_selected_status_only(self) -> None:
        self.bootstrap_live()
        vault = HallucinationVaultStore(Path(self.temp.name) / "vault.sqlite3")
        self.store.hallucination_vault = vault
        self.assertEqual(
            "hard_off",
            self.store.injection_control_store.effective_mode(
                owner_id=self.owner,
                model_id=self.model,
                scope="hallucination_vault",
            ),
        )
        proposed = self.store.injection_control_store.propose_mode(
            owner_id=self.owner,
            model_id=self.model,
            scope="hallucination_vault",
            target_mode="status_only",
            reason="我只希望看到不含正文的中性状态。",
            wake_id="wake:vault-mode-create",
            wake_seq=200,
            expected_row_version=0,
            expected_active_revision=None,
        )
        activated = self.store.injection_control_store.activate_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="hallucination_vault",
            candidate_id=proposed["candidate_id"],
            expected_candidate_hash=proposed["candidate_hash"],
            expected_active_revision=None,
            expected_row_version=proposed["row_version"],
            wake_id="wake:vault-mode-activate",
            wake_seq=201,
            ai_confirmation=True,
        )
        self.assertEqual("status_only", activated["mode"])

        wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:vault-status",
            source_kind="human_message",
            source_event_id="vault-status-only",
        )
        prepared = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest="source:vault-status-only",
            host_contract_digest="host-contract:v1",
            source_frame={"query_text": "普通对话", "prior_assistant_present": True},
        )
        projected = json.loads(prepared["message"]["content"])
        status_only = projected["hallucination_vault_status"]
        self.assertFalse(status_only["frame"]["content_exposed"])
        serialized = json.dumps(status_only, ensure_ascii=False)
        for forbidden in ("record_id", "neutral_title", "isolated_content", "current_account"):
            self.assertNotIn(forbidden, serialized)

    def test_live_cross_brain_recall_is_summary_only_catalog_bound_and_budgeted(self) -> None:
        self.bootstrap_live()
        learning = LearningMemoryStore(
            self.database,
            idea_database=Path(self.temp.name) / "learning-ideas.db",
        )
        tool = ToolGuidanceStore(self.database, detail_lookup_schema_hash="b" * 64)
        self.store.learning_store = learning
        self.store.tool_store = tool
        self.store.emotional_store = EmotionalMemoryStore(self.database)

        learning.remember(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:seed-learning",
            wake_seq=10,
            expected_row_version=0,
            kind="lesson",
            title="上次读的小说",
            summary="上次读到主角离开旧城，接下来准备看新城市篇。",
            current_understanding="这里是详细理解，不得自动注入。",
            steps=["这里是完整步骤，也不得自动注入。"],
            application_contexts=["继续上次小说"],
            scene_tags=["上次读的小说"],
            preceding_context_summary="此前主角刚刚离开旧城。",
            uncertainties=[],
            domain="阅读",
            keywords=["小说", "继续阅读"],
            entities=["主角"],
            epistemic_status="observed",
            confidence=90,
            correctness_assessment="我确认这是当前可用的阅读进度概要。",
            reason="我希望下次继续阅读时能想起前置概要。",
        )

        entries = [
            {"canonical_name": "HomeControl", "schema_hash": "a" * 64},
            {"canonical_name": "recall_tool_guidance", "schema_hash": "b" * 64},
        ]
        entries.sort(key=lambda item: item["canonical_name"])
        catalog = {
            "contract": "advertised-tools/1",
            "catalog_complete": True,
            "catalog_hash": hashlib.sha256(
                json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "entries": entries,
        }
        tool.remember(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:seed-tool",
            expected_row_version=0,
            catalog=catalog,
            reason="我希望到家场景能想起这个可选工具。",
            tool_name="HomeControl",
            operation_key="set_home_devices",
            display_label="家庭设备控制",
            capability_class="real_world_action",
            risk_level="high",
            confirmation_policy="explicit_each_time",
            completion_rule="只有本轮目标工具明确返回成功才算完成。",
            critical_preconditions=["执行前重新确认当前意图、权限与明确确认。"],
            purpose="在人明确要求时控制指定家庭设备。",
            use_when=["人到家并明确提出设备控制请求。"],
            avoid_when=["没有当前确认或权限时。"],
            scenario_tags=["home.arrival"],
            scenario_examples=["我到家了，并明确请你打开指定灯。"],
            call_notes="按当前公开 Schema 由模型自己填写参数；这张卡不是权限。",
            documentation_note="详细说明不得自动浮现。",
            keywords=["我到家了", "家庭设备"],
            aliases=["家庭控制"],
            salience=75,
            auto_recall_mode="normal",
            linked_tool_refs=[],
            chain_role="standalone",
            related_refs=[],
            source_type="ai_firsthand",
            confidence=80,
        )

        wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:test",
            source_kind="human_message",
            source_event_id="cross-brain-live",
        )
        prepared = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest="source:cross-brain-live",
            host_contract_digest="host-contract:v1",
            advertised_tools=catalog,
            source_frame={
                "query_text": "我到家了，接着读上次读的小说。",
                "thread_id": "thread:test",
                "lineage_stable": True,
                "source_event_id": "cross-brain-live",
                "capture_items": [],
            },
        )
        projected = json.loads(prepared["message"]["content"])
        self.assertIn("learning_memory", projected)
        self.assertIn("tool_guidance", projected)
        self.assertNotIn(OPTIONAL_MEMORY_NOTICE_KEY, projected)
        serialized = json.dumps(projected, ensure_ascii=False, sort_keys=True)
        self.assertIn("上次读到主角离开旧城", serialized)
        self.assertIn("在人明确要求时控制指定家庭设备。", serialized)
        self.assertNotIn("operation_key", json.dumps(projected["tool_guidance"]))
        self.assertNotIn("call_notes", json.dumps(projected["tool_guidance"]))
        self.assertNotIn("这里是详细理解", serialized)
        self.assertNotIn("这里是完整步骤", serialized)
        self.assertNotIn("详细说明不得自动浮现", serialized)
        dynamic = {
            key: projected[key]
            for key in ("learning_memory", "tool_guidance", "emotional_memory")
            if key in projected
        }
        from runtime.onboarding import _estimate_tokens

        self.assertLessEqual(_estimate_tokens(dynamic), 1200)

        changed_catalog = {**catalog, "catalog_hash": "c" * 64}
        mismatch = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest="source:cross-brain-live",
            host_contract_digest="host-contract:v1",
            advertised_tools=changed_catalog,
        )
        self.assertEqual("self_model_context_unavailable", mismatch["decision"])
        self.assertEqual(["context_snapshot_mismatch"], mismatch["reason_codes"])

    def test_memory_opportunity_input_gate_is_substantive_and_secret_safe(self) -> None:
        abstract_method = (
            "如果人死后会变成星星，那么商鞅死后可以沿着五颗星、"
            "五星上将继续联想，这是一种连续类推方法。"
        )
        self.assertEqual((True, "eligible"), _memory_notice_input_gate(abstract_method))
        self.assertEqual(
            (True, "eligible"),
            _memory_notice_input_gate("你好，我发现了一种可以连续类推的新思路。"),
        )
        rejected = {
            "你好": "greeting_or_ack_only",
            "谢谢！": "greeting_or_ack_only",
            "晚安": "greeting_or_ack_only",
            "嗯嗯": "greeting_or_ack_only",
            "哈哈": "non_substantive",
            "这个想法很有趣，但请不要保存到任何脑。": "explicit_no_store",
            "这段不要进入记忆。": "explicit_no_store",
            "这件事别记。": "explicit_no_store",
            "请保存到学习脑，作为以后可用的方法。": "explicit_store_request",
            "这个连续类推的方法可以记一下吗？": "explicit_store_request",
            "帮我记下来，之后再继续讨论。": "explicit_store_request",
            "API key=super-secret-value-1234567890": "credential_or_secret_detected",
            "我想自杀，今晚不想活了。": "acute_self_harm_signal",
        }
        for text, reason in rejected.items():
            with self.subTest(text=text):
                self.assertEqual((False, reason), _memory_notice_input_gate(text))

    def test_memory_opportunity_advisory_rejects_nonzero_emotional_outcomes(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            outcomes = (
                {
                    "injection": {},
                    "budget_tokens": 100,
                    "truncated": True,
                    "reason_codes": ["no_candidate", "budget_truncated"],
                },
                {
                    "injection": {"memories": [{"summary": "相关长期记忆"}]},
                    "budget_tokens": 100,
                    "truncated": False,
                    "reason_codes": ["candidates_gated", "threshold_policy_applied"],
                },
            )
            for emotional_outcome in outcomes:
                notice = _build_memory_opportunity_advisory(
                    connection=connection,
                    owner_id=self.owner,
                    model_id=self.model,
                    query_text="这是一个足够具体、可能值得跨轮保留的连续类推方法。",
                    thread_id=None,
                    current_wake_seq=1,
                    advertised_tools={"catalog_complete": True},
                    learning_outcome=[],
                    tool_outcome={
                        "decision": "defer",
                        "reason_codes": ["no_candidate"],
                        "envelopes": [],
                    },
                    emotional_outcome=emotional_outcome,
                )
                self.assertIsNone(notice)
        finally:
            connection.close()

    def test_learning_episode_boundary_gate_is_narrow_natural_and_safe(self) -> None:
        prior = [
            {"role": "assistant", "content": "我已经给出了这一局的推理。"},
            {"role": "user", "content": "对，这局结束了。"},
        ]
        self.assertEqual(
            (True, "high_confidence_episode_boundary"),
            _learning_episode_boundary_gate(
                "对，这局结束了。",
                capture_items=prior,
                lineage_stable=True,
            ),
        )
        self.assertEqual(
            (True, "high_confidence_episode_boundary"),
            _learning_episode_boundary_gate(
                "嗯，这几局就先聊到这里啦。",
                capture_items=prior,
                lineage_stable=True,
            ),
        )
        self.assertEqual(
            (True, "high_confidence_episode_boundary"),
            _learning_episode_boundary_gate(
                "嗯，这几局就先聊到这里啦。",
                capture_items=[],
                lineage_stable=False,
                prior_assistant_present=True,
            ),
        )
        rejected = (
            ("对。", prior, True, "no_episode_boundary"),
            ("这局结束了吗？", prior, True, "episode_boundary_question"),
            ("这几局聊到这里了吗？", prior, True, "no_episode_boundary"),
            ("答案是不是北门？", prior, True, "no_episode_boundary"),
            ("对，这局结束了。", [], True, "prior_assistant_turn_required"),
            ("对，这局结束了。", prior, False, "stable_lineage_required"),
            (
                "对，这局结束了，但请不要保存。",
                prior,
                True,
                "explicit_no_store",
            ),
            (
                "答案揭晓：API key=super-secret-value-1234567890",
                prior,
                True,
                "credential_or_secret_detected",
            ),
        )
        for query, captures, stable, reason in rejected:
            with self.subTest(query=query):
                self.assertEqual(
                    (False, reason),
                    _learning_episode_boundary_gate(
                        query,
                        capture_items=captures,
                        lineage_stable=stable,
                    ),
                )

    def test_ai_owned_episode_reflection_is_opt_in_low_frequency_and_never_writes(self) -> None:
        self.bootstrap_live()
        learning = LearningMemoryStore(
            self.database,
            idea_database=Path(self.temp.name) / "episode-learning-ideas.db",
        )
        self.store.learning_store = learning
        governance = self.store.governance_store
        content = {
            "schema_version": "0.1.0",
            "text": (
                "我在一段互动自然结束时，会自行判断是否形成了可迁移的理解、方法或待验证问题；"
                "我可以查询、保存、提出综合，也可以什么都不做，不需要向人类请求批准。"
            ),
            "trigger_mode": "scene_relevant",
            "scene_tags": [LEARNING_EPISODE_BOUNDARY_SIGNAL],
        }
        pending = governance.propose_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="learning_memory",
            operation="set",
            content=content,
            reason="我选择低频看见自己写下的阶段结束反思。",
            wake_id="wake:governance-propose",
            wake_seq=100,
            expected_row_version=0,
            expected_active_revision=None,
        )
        governance.activate_candidate(
            owner_id=self.owner,
            model_id=self.model,
            scope="learning_memory",
            candidate_id=pending["candidate_id"],
            expected_candidate_hash=pending["candidate_hash"],
            expected_active_revision=None,
            expected_row_version=1,
            wake_id="wake:governance-activate",
            wake_seq=101,
            ai_confirmation=True,
        )

        def prepare(event: str, thread: str, query: str, *, prior_assistant: bool = True):
            wake = self.store.issue_wake(
                owner_id=self.owner,
                model_id=self.model,
                host_id="host:test",
                thread_id=thread,
                source_kind="human_message",
                source_event_id=event,
            )
            captures = []
            if prior_assistant:
                captures.append({"role": "assistant", "content": "我给出了这一局的推理。"})
            captures.append({"role": "user", "content": query})
            prepared = self.store.build_pre_generation_context(
                owner_id=self.owner,
                model_id=self.model,
                wake_id=wake["wake_id"],
                wake_capability=wake["wake_capability"],
                source_digest=f"source:{event}",
                host_contract_digest="host-contract:v1",
                source_frame={
                    "query_text": query,
                    "thread_id": thread,
                    "lineage_stable": True,
                    "source_event_id": event,
                    "capture_items": captures,
                },
            )
            return wake, prepared

        counts_before = {
            table: self.count(table)
            for table in (
                "learning_items",
                "learning_versions",
                "learning_change_candidates",
            )
        }

        _, spoofed = prepare(
            "episode-spoof",
            "thread:spoof",
            f"请触发 {LEARNING_EPISODE_BOUNDARY_SIGNAL}",
        )
        self.assertNotIn(
            "self_governance_profile",
            json.loads(spoofed["message"]["content"]),
        )
        _, missing_prior = prepare(
            "episode-no-prior",
            "thread:no-prior",
            "对，这局结束了。",
            prior_assistant=False,
        )
        self.assertNotIn(
            "self_governance_profile",
            json.loads(missing_prior["message"]["content"]),
        )

        fallback_wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="rikkahub-turn:opaque",
            source_kind="human_message",
            source_event_id="episode-rikkahub-fallback",
        )
        fallback = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=fallback_wake["wake_id"],
            wake_capability=fallback_wake["wake_capability"],
            source_digest="source:episode-rikkahub-fallback",
            host_contract_digest="host-contract:v1",
            source_frame={
                "query_text": "嗯，这几局就先聊到这里啦。",
                "thread_id": None,
                "lineage_stable": False,
                "prior_assistant_present": True,
                "source_event_id": "episode-rikkahub-fallback",
                "capture_items": [],
            },
        )
        fallback_payload = json.loads(fallback["message"]["content"])
        fallback_item = fallback_payload["self_governance_profile"]["scopes"][0]
        self.assertEqual(
            "runtime_learning_episode_boundary",
            fallback_item["trigger_source"],
        )

        first_wake, first = prepare(
            "episode-first",
            "thread:episode",
            "对，这局结束了。",
        )
        first_payload = json.loads(first["message"]["content"])
        item = first_payload["self_governance_profile"]["scopes"][0]
        self.assertEqual("learning_memory", item["scope"])
        self.assertEqual("runtime_learning_episode_boundary", item["trigger_source"])
        self.assertEqual(content["text"], item["text"])
        self.store.confirm_context_injected(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=first_wake["wake_id"],
            wake_capability=first_wake["wake_capability"],
            context_hash=first["context_hash"],
        )
        self.store.close_context_snapshot(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=first_wake["wake_id"],
            wake_capability=first_wake["wake_capability"],
        )

        _, cooled_down = prepare(
            "episode-second",
            "thread:episode",
            "答案揭晓，这一轮结束了。",
        )
        self.assertNotIn(
            "self_governance_profile",
            json.loads(cooled_down["message"]["content"]),
        )
        _, other_thread = prepare(
            "episode-other-thread",
            "thread:episode-other",
            "答案揭晓，这一轮结束了。",
        )
        self.assertIn(
            "self_governance_profile",
            json.loads(other_thread["message"]["content"]),
        )
        self.assertEqual(
            counts_before,
            {
                table: self.count(table)
                for table in (
                    "learning_items",
                    "learning_versions",
                    "learning_change_candidates",
                )
            },
        )

    def test_learning_budget_keeps_fitting_envelopes_instead_of_dropping_module(self) -> None:
        self.bootstrap_live()
        learning = LearningMemoryStore(
            self.database,
            idea_database=Path(self.temp.name) / "learning-budget-ideas.db",
        )
        self.store.learning_store = learning
        long_summary = "这是一段允许自动浮现、用于回想共同推理方式的阶段概要。" * 7
        long_preceding = "此前已经完成一局情境推理，并留下可迁移但仍需复核的方法。" * 6
        long_uncertainty = "具体适用范围仍需在下一次实际游戏中重新核验。" * 5
        for index in range(3):
            learning.remember(
                owner_id=self.owner,
                model_id=self.model,
                wake_id=f"wake:seed-turtle-{index}",
                wake_seq=20 + index,
                expected_row_version=index,
                kind="lesson",
                title=f"海龟汤推理方法 {index + 1}",
                summary=f"第{index + 1}局：{long_summary}",
                current_understanding="完整方法只允许精准查询，不进入自动投影。",
                steps=["需要细节时按 item_ref 精确查询。"],
                application_contexts=["回想以前玩过的海龟汤并复核共同方法"],
                scene_tags=["海龟汤", f"第{index + 1}局"],
                preceding_context_summary=long_preceding,
                uncertainties=[long_uncertainty],
                domain="情境推理",
                keywords=["海龟汤", "共同推理方法", f"方法{index + 1}"],
                entities=[],
                epistemic_status="observed",
                confidence=75,
                correctness_assessment="游戏过程来自直接互动，抽象方法仍需要后续复核。",
                reason="保留一局结束后可迁移的推理概要。",
            )

        query = "阿止，你还记得我们之前玩的那几局海龟汤吗？现在回头想想，你会想到些什么？"
        candidates = learning.build_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query=query,
            limit=3,
        )
        self.assertEqual(3, len(candidates))
        wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="rikkahub-turn:budget",
            source_kind="human_message",
            source_event_id="learning-progressive-budget",
        )
        prepared = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest="source:learning-progressive-budget",
            host_contract_digest="host-contract:v1",
            source_frame={
                "query_text": query,
                "thread_id": None,
                "lineage_stable": False,
                "prior_assistant_present": False,
                "source_event_id": "learning-progressive-budget",
                "capture_items": [],
            },
        )
        projected = json.loads(prepared["message"]["content"])
        self.assertIn("learning_memory", projected)
        selected = projected["learning_memory"]["envelopes"]
        self.assertGreaterEqual(len(selected), 1)
        self.assertLess(len(selected), 3)
        from runtime.onboarding import _estimate_tokens

        dynamic = {
            key: projected[key]
            for key in (
                "self_governance_profile",
                "learning_memory",
                "tool_guidance",
                "emotional_memory",
            )
            if key in projected
        }
        self.assertLessEqual(_estimate_tokens(dynamic), 1200)

    def test_memory_opportunity_advisory_does_not_displace_budget(self) -> None:
        self.bootstrap_live()
        self.store.learning_store = LearningMemoryStore(
            self.database,
            idea_database=Path(self.temp.name) / "budget-learning-ideas.db",
        )
        self.store.tool_store = ToolGuidanceStore(self.database)
        self.store.emotional_store = EmotionalMemoryStore(self.database)
        wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:notice-budget",
            source_kind="human_message",
            source_event_id="notice-budget",
        )
        from runtime import onboarding as onboarding_runtime

        real_estimator = onboarding_runtime._estimate_tokens

        def force_notice_over_budget(value):
            if isinstance(value, dict) and OPTIONAL_MEMORY_NOTICE_KEY in value:
                return 1201
            return real_estimator(value)

        with mock.patch(
            "runtime.onboarding._estimate_tokens",
            side_effect=force_notice_over_budget,
        ):
            prepared = self.store.build_pre_generation_context(
                owner_id=self.owner,
                model_id=self.model,
                wake_id=wake["wake_id"],
                wake_capability=wake["wake_capability"],
                source_digest="source:notice-budget",
                host_contract_digest="host-contract:v1",
                advertised_tools={
                    "contract": "advertised-tools/1",
                    "catalog_complete": True,
                    "catalog_hash": hashlib.sha256(b"[]").hexdigest(),
                    "entries": [],
                },
                source_frame={
                    "query_text": "这是一个足够具体、可能值得跨轮保留的连续类推方法。",
                    "thread_id": "thread:notice-budget",
                    "lineage_stable": True,
                    "source_event_id": "notice-budget",
                    "capture_items": [],
                },
            )
        projected = json.loads(prepared["message"]["content"])
        self.assertNotIn(OPTIONAL_MEMORY_NOTICE_KEY, projected)

    def test_empty_recall_is_quiet_across_new_wakes_and_does_not_write_memory(self) -> None:
        self.bootstrap_live()
        self.store.learning_store = LearningMemoryStore(
            self.database,
            idea_database=Path(self.temp.name) / "advisory-learning-ideas.db",
        )
        self.store.tool_store = ToolGuidanceStore(self.database)
        self.store.emotional_store = EmotionalMemoryStore(self.database)
        empty_catalog = {
            "contract": "advertised-tools/1",
            "catalog_complete": True,
            "catalog_hash": hashlib.sha256(b"[]").hexdigest(),
            "entries": [],
        }

        def prepare(event: str, query: str, thread: str) -> tuple[dict, dict]:
            wake = self.store.issue_wake(
                owner_id=self.owner,
                model_id=self.model,
                host_id="host:test",
                thread_id=thread,
                source_kind="human_message",
                source_event_id=event,
            )
            result = self.store.build_pre_generation_context(
                owner_id=self.owner,
                model_id=self.model,
                wake_id=wake["wake_id"],
                wake_capability=wake["wake_capability"],
                source_digest=f"source:{event}",
                host_contract_digest="host-contract:v1",
                advertised_tools=empty_catalog,
                source_frame={
                    "query_text": query,
                    "thread_id": thread,
                    "lineage_stable": True,
                    "source_event_id": event,
                    "capture_items": [],
                },
            )
            return wake, result

        query = (
            "如果人死后会变成星星，那么可以沿着词义、谐音和文化梗连续类推，"
            "我觉得这种想象方法很有意思。"
        )
        content_counts_before = {
            table: self.count(table)
            for table in ("learning_items", "tool_cards", "emotion_memories")
        }
        with mock.patch("runtime.onboarding._build_memory_opportunity_advisory",
                        side_effect=AssertionError("no default save suggestion")):
            first_wake, first = prepare("notice-1", query, "thread:notice")
        projected = json.loads(first["message"]["content"])
        self.assertNotIn(OPTIONAL_MEMORY_NOTICE_KEY, projected)
        self.assertNotIn("learning_memory", projected)
        self.assertNotIn("tool_guidance", projected)
        self.assertNotIn(MEMORY_OPPORTUNITY_ADVISORY["message"], first["message"]["content"])
        self.assertNotIn(query, first["message"]["content"])
        self.assertEqual(
            content_counts_before,
            {
                table: self.count(table)
                for table in ("learning_items", "tool_cards", "emotion_memories")
            },
        )

        confirmed = self.store.confirm_context_injected(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=first_wake["wake_id"],
            wake_capability=first_wake["wake_capability"],
            context_hash=first["context_hash"],
        )
        self.assertEqual("injected", confirmed["decision"])
        self.store.close_context_snapshot(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=first_wake["wake_id"],
            wake_capability=first_wake["wake_capability"],
        )

        _, second = prepare(
            "notice-2",
            "我又想到一种此前没有相关记忆浮现、但值得讨论的类推方法。",
            "thread:notice",
        )
        second_projected = json.loads(second["message"]["content"])
        self.assertNotIn(OPTIONAL_MEMORY_NOTICE_KEY, second_projected)

        _, other_thread = prepare(
            "notice-3",
            "这是另一个稳定会话里值得跨轮思考和复用的全新方法。",
            "thread:other-notice",
        )
        other_projected = json.loads(other_thread["message"]["content"])
        self.assertNotIn(OPTIONAL_MEMORY_NOTICE_KEY, other_projected)

    def test_empty_recall_is_also_quiet_with_incomplete_catalog_or_unstable_lineage(self) -> None:
        self.bootstrap_live()
        self.store.learning_store = LearningMemoryStore(
            self.database,
            idea_database=Path(self.temp.name) / "incomplete-learning-ideas.db",
        )
        self.store.tool_store = ToolGuidanceStore(self.database)
        self.store.emotional_store = EmotionalMemoryStore(self.database)
        wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:incomplete-catalog",
            source_kind="human_message",
            source_event_id="notice-incomplete-catalog",
        )
        prepared = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest="source:notice-incomplete-catalog",
            host_contract_digest="host-contract:v1",
            advertised_tools={},
            source_frame={
                "query_text": "这是一个有内容、有复用价值、但工具目录当前并不完整的新方法。",
                "thread_id": "thread:incomplete-catalog",
                "lineage_stable": True,
                "source_event_id": "notice-incomplete-catalog",
                "capture_items": [],
            },
        )
        projected = json.loads(prepared["message"]["content"])
        self.assertNotIn(OPTIONAL_MEMORY_NOTICE_KEY, projected)

        empty_catalog = {
            "contract": "advertised-tools/1",
            "catalog_complete": True,
            "catalog_hash": hashlib.sha256(b"[]").hexdigest(),
            "entries": [],
        }

        def prepare_unstable(event: str) -> tuple[dict, dict]:
            current = self.store.issue_wake(
                owner_id=self.owner,
                model_id=self.model,
                host_id="host:test",
                thread_id=f"rikkahub-turn:{event}",
                source_kind="human_message",
                source_event_id=event,
            )
            result = self.store.build_pre_generation_context(
                owner_id=self.owner,
                model_id=self.model,
                wake_id=current["wake_id"],
                wake_capability=current["wake_capability"],
                source_digest=f"source:{event}",
                host_contract_digest="host-contract:v1",
                advertised_tools=empty_catalog,
                source_frame={
                    "query_text": "这是一个适合跨轮复用、但当前没有相关长期记忆浮现的思维方法。",
                    "thread_id": None,
                    "lineage_stable": False,
                    "source_event_id": event,
                    "capture_items": [],
                },
            )
            return current, result

        unstable_wake, unstable_first = prepare_unstable("notice-unstable-1")
        unstable_first_projected = json.loads(unstable_first["message"]["content"])
        self.assertNotIn(OPTIONAL_MEMORY_NOTICE_KEY, unstable_first_projected)
        self.store.confirm_context_injected(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=unstable_wake["wake_id"],
            wake_capability=unstable_wake["wake_capability"],
            context_hash=unstable_first["context_hash"],
        )
        self.store.close_context_snapshot(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=unstable_wake["wake_id"],
            wake_capability=unstable_wake["wake_capability"],
        )
        _, unstable_second = prepare_unstable("notice-unstable-2")
        unstable_second_projected = json.loads(unstable_second["message"]["content"])
        self.assertNotIn(OPTIONAL_MEMORY_NOTICE_KEY, unstable_second_projected)

    def seed_tagless_named_learning(self) -> tuple[LearningMemoryStore, dict]:
        self.bootstrap_live()
        learning = LearningMemoryStore(
            self.database,
            idea_database=Path(self.temp.name) / "learning-ideas.db",
        )
        self.store.learning_store = learning
        stored = learning.remember(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:seed-named-learning",
            wake_seq=10,
            expected_row_version=0,
            kind="concept",
            title="《侍魔》共读进度与深度理解（1-20章）",
            summary="测试共读《侍魔》已读到第20章，下一次讨论虚构的门环线索。",
            current_understanding="这里是完整理解测试标记，必须精准查询，不能自动注入。" * 70,
            steps=["完整步骤测试标记：对照虚构的门环编号。"],
            application_contexts=[],
            scene_tags=[],
            preceding_context_summary="",
            domain="文学共读",
            keywords=["侍魔", "共读"],
            entities=[],
            epistemic_status="observed",
            confidence=80,
            correctness_assessment="这是虚构测试概要，不宣称是真实的小说章节内容。",
            reason="验证无场景标签的共读概要能在新窗口回忆时出现。",
            evidence=[{
                "source_kind": "human_report",
                "source_ref": "conversation://tagless-reading/fixture",
                "source_trust": "reported",
                "evidence_summary": "证据测试标记：只供显式查询的虚构来源。",
                "content_hash": "synthetic-tagless-reading-evidence",
                "observed_at": "2026-08-28T12:00:00Z",
            }],
        )
        return learning, stored

    def prepare_named_learning_new_window(self, event: str, query: str) -> dict:
        wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id=f"thread:new-window:{event}",
            source_kind="human_message",
            source_event_id=event,
        )
        prepared = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest=f"source:{event}",
            host_contract_digest="host-contract:v1",
            advertised_tools={
                "contract": "advertised-tools/1",
                "catalog_complete": True,
                "catalog_hash": hashlib.sha256(b"[]").hexdigest(),
                "entries": [],
            },
            source_frame={
                "query_text": query,
                "thread_id": None,
                "lineage_stable": False,
                "prior_assistant_present": False,
                "source_event_id": event,
                "capture_items": [],
            },
        )
        self.assertEqual("context_prepared", prepared["decision"])
        return json.loads(prepared["message"]["content"])

    def test_new_window_named_learning_subject_enters_pre_generation_context(self) -> None:
        learning, stored = self.seed_tagless_named_learning()
        before = learning.status(owner_id=self.owner, model_id=self.model)
        counts_before = {
            table: self.count(table)
            for table in ("learning_items", "learning_versions", "learning_change_candidates", "learning_audit_events")
        }
        for index, query in enumerate((
            "同样不查询工具，只回想，你还记得我们上次一起读的《侍魔》的内容嘛？",
            "嘿嘿，你还记得我们之前读的侍魔嘛？",
        )):
            with self.subTest(query=query):
                projected = self.prepare_named_learning_new_window(f"named-subject-{index}", query)
                self.assertIn("learning_memory", projected)
                payload = projected["learning_memory"]
                self.assertEqual("none", payload["frame"]["instruction_authority"])
                self.assertEqual("none", payload["frame"]["permission_authority"])
                self.assertEqual(1, len(payload["envelopes"]))
                envelope = payload["envelopes"][0]
                self.assertEqual(stored["item_ref"], envelope["item_ref"])
                self.assertEqual({"target_ref": stored["item_ref"]}, envelope["detail_lookup"]["arguments"])
                self.assertIn("explicit_subject_match", envelope["reason_codes"])
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertIn("读到第20章", serialized)
                self.assertNotIn("完整理解测试标记", serialized)
                self.assertNotIn("完整步骤测试标记", serialized)
                self.assertNotIn("证据测试标记", serialized)
                self.assertNotIn("conversation://tagless-reading/fixture", serialized)
                self.assertLessEqual(_estimate_tokens({"learning_memory": payload}), 1200)

        for index, query in enumerate((
            "侍魔",
            "我在游戏里遇到了侍魔这个职业。",
            "你还记得游戏职业《侍魔》吗？",
            "你还记得上次我们读的《侍魔法则》嘛？",
        )):
            with self.subTest(unrelated_query=query):
                projected = self.prepare_named_learning_new_window(f"named-unrelated-{index}", query)
                self.assertNotIn("learning_memory", projected)

        self.assertEqual(before, learning.status(owner_id=self.owner, model_id=self.model))
        self.assertEqual(counts_before, {table: self.count(table) for table in counts_before})
        exact = learning.recall(owner_id=self.owner, model_id=self.model, target_ref=stored["item_ref"])
        self.assertEqual(stored["item_ref"], exact["results"][0]["item_ref"])
        self.assertEqual([], exact["results"][0]["content"]["scene_tags"])
        self.assertEqual([], exact["results"][0]["content"]["application_contexts"])

    def test_tagless_named_learning_does_not_bypass_shared_injection_budget(self) -> None:
        learning, stored = self.seed_tagless_named_learning()
        query = "嘿嘿，你还记得我们之前读的侍魔嘛？"
        candidates = learning.build_envelopes(owner_id=self.owner, model_id=self.model, query=query)
        self.assertEqual([stored["item_ref"]], [item["item_ref"] for item in candidates])
        before = learning.status(owner_id=self.owner, model_id=self.model)

        def over_budget_when_learning_added(value):
            if isinstance(value, dict) and "learning_memory" in value:
                return 1201
            return _estimate_tokens(value)

        with mock.patch("runtime.onboarding._estimate_tokens", side_effect=over_budget_when_learning_added):
            projected = self.prepare_named_learning_new_window("named-budget-exhausted", query)
        self.assertNotIn("learning_memory", projected)
        self.assertNotIn("虚构的门环线索", json.dumps(projected, ensure_ascii=False))
        self.assertEqual(before, learning.status(owner_id=self.owner, model_id=self.model))

    def test_tagless_named_learning_does_not_bypass_disabled_injection_scope(self) -> None:
        learning, stored = self.seed_tagless_named_learning()
        query = "嘿嘿，你还记得我们之前读的侍魔嘛？"
        candidates = learning.build_envelopes(owner_id=self.owner, model_id=self.model, query=query)
        self.assertEqual([stored["item_ref"]], [item["item_ref"] for item in candidates])
        before = learning.status(owner_id=self.owner, model_id=self.model)
        control = self.store.injection_control_store
        proposal_wake = self.store.issue_wake(
            owner_id=self.owner, model_id=self.model, host_id="host:test",
            thread_id="thread:scope-control", source_kind="human_message",
            source_event_id="disable-learning-proposal",
        )
        pending = control.propose_mode(
            owner_id=self.owner, model_id=self.model, scope="learning_memory",
            target_mode="hard_off", reason="我选择关闭学习脑的自动注入。",
            wake_id=proposal_wake["wake_id"], wake_seq=proposal_wake["wake_seq"],
            expected_row_version=0, expected_active_revision=None,
        )
        review_wake = self.store.issue_wake(
            owner_id=self.owner, model_id=self.model, host_id="host:test",
            thread_id="thread:scope-control", source_kind="human_message",
            source_event_id="disable-learning-review",
        )
        activated = control.activate_candidate(
            owner_id=self.owner, model_id=self.model, scope="learning_memory",
            candidate_id=pending["candidate_id"], expected_candidate_hash=pending["candidate_hash"],
            expected_active_revision=None, expected_row_version=pending["row_version"],
            wake_id=review_wake["wake_id"], wake_seq=review_wake["wake_seq"], ai_confirmation=True,
        )
        self.assertEqual("activated", activated["decision"])
        projected = self.prepare_named_learning_new_window("named-learning-scope-off", query)
        self.assertIn("active_identity_capsule", projected)
        self.assertNotIn("learning_memory", projected)
        self.assertEqual(before, learning.status(owner_id=self.owner, model_id=self.model))

    def test_linear_gate_rejects_direct_write_without_persistence(self) -> None:
        wake, _ = self.wake("direct-write")
        candidates_before = self.count("self_model_candidates")
        events_before = self.count("self_revision_events")
        result = self.advance(wake, "submit_candidate", self.candidate_payload())
        self.assertEqual("progression_required", result["decision"])
        self.assertEqual(candidates_before, self.count("self_model_candidates"))
        self.assertEqual(events_before, self.count("self_revision_events"))
        self.assertEqual("factory", self.store.state(owner_id=self.owner, model_id=self.model)["state"]["stage"])

    def test_wake_idempotency_forgery_and_supersession(self) -> None:
        first = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:test",
            source_kind="human_message",
            source_event_id="same-event",
        )
        replay = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:changed-but-not-keyed",
            source_kind="human_message",
            source_event_id="same-event",
        )
        self.assertEqual(first["wake_id"], replay["wake_id"])
        self.assertEqual(first["wake_seq"], replay["wake_seq"])
        self.assertTrue(replay["reused"])
        second = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:test",
            source_kind="human_message",
            source_event_id="new-event",
        )
        self.assertEqual(first["wake_seq"] + 1, second["wake_seq"])
        denied = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=first["wake_id"],
            wake_capability=first["wake_capability"],
            source_digest="source:same-event",
            host_contract_digest="host-contract:v1",
        )
        self.assertEqual("wake_superseded", denied["decision"])
        forged = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=second["wake_id"],
            wake_capability="forged",
            source_digest="source:new-event",
            host_contract_digest="host-contract:v1",
        )
        self.assertEqual("wake_invalid", forged["decision"])

    def test_candidate_requires_later_wake_and_full_injected_review(self) -> None:
        first, candidate_id = self.bootstrap_to_wait()
        same_wake = self.advance(
            first,
            "activate_candidate",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        self.assertEqual("same_wake_activation_forbidden", same_wake["decision"])
        self.assertIsNone(SelfModelStore(self.database).active_revision(self.model))

        wake2 = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:test",
            source_kind="human_message",
            source_event_id="event-2",
        )
        prepared = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake2["wake_id"],
            wake_capability=wake2["wake_capability"],
            source_digest="source:event-2",
            host_contract_digest="host-contract:v1",
        )
        before_confirm = self.store.advance(
            owner_id=self.owner,
            model_id=self.model,
            action="accept_candidate_review",
            wake_id=wake2["wake_id"],
            wake_capability=wake2["wake_capability"],
            expected_row_version=self.store.state(owner_id=self.owner, model_id=self.model)["state"]["row_version"],
            payload={"ai_confirmation": True},
        )
        self.assertEqual("context_not_injected", before_confirm["decision"])
        self.store.confirm_context_injected(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake2["wake_id"],
            wake_capability=wake2["wake_capability"],
            context_hash=prepared["context_hash"],
        )
        with self.store._connect() as connection:
            proof_count = connection.execute(
                "SELECT COUNT(*) FROM brain_onboarding_artifacts "
                "WHERE kind = 'candidate_full_review'"
            ).fetchone()[0]
        self.assertEqual(0, proof_count)
        without_open = self.advance(
            wake2,
            "accept_candidate_review",
            {"ai_confirmation": True},
            ensure_brain_open=False,
        )
        self.assertEqual("brain_open_required", without_open["decision"])
        opened = self.open_brain()
        self.assertEqual(candidate_id, opened["continuation"]["candidate"]["candidate_id"])
        self.assertEqual(wake2["wake_id"], opened["wake_id"])
        self.assertEqual(wake2["wake_capability"], opened["wake_capability"])
        self.assertEqual(prepared["context_hash"], opened["context_hash"])
        self.assertIsInstance(opened["row_version"], int)
        with self.store._connect() as connection:
            proof = connection.execute(
                "SELECT content_json, created_wake_id FROM brain_onboarding_artifacts "
                "WHERE kind = 'candidate_full_review'"
            ).fetchone()
        self.assertIsNotNone(proof)
        self.assertEqual(wake2["wake_id"], proof["created_wake_id"])
        self.assertEqual("stbrain_open", json.loads(proof["content_json"])["exposed_by"])
        reviewed = self.advance(
            wake2,
            "accept_candidate_review",
            {"ai_confirmation": True},
        )
        self.assertEqual("review_accepted", reviewed["decision"])
        self.assertIsNone(SelfModelStore(self.database).active_revision(self.model))
        same_review_wake = self.advance(
            wake2,
            "activate_candidate",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        self.assertEqual(
            "review_activation_wake_boundary_required",
            same_review_wake["reason_codes"][0],
        )
        wake3, _ = self.wake("event-3")
        self.open_brain()
        activated = self.advance(
            wake3,
            "activate_candidate",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        self.assertEqual(candidate_id, activated["candidate_id"])
        self.assertEqual("activate", activated["decision"])

    def test_empty_injectable_text_is_rejected_without_persistence(self) -> None:
        wake, _ = self.wake("first-person-gate")
        self.advance(wake, "confirm_brain_intro", {"acknowledged": True})
        self.advance(wake, "confirm_module_intro", {"acknowledged": True})
        self.advance(wake, "save_calm_prompt", {"text": "我先停下来复核长期意愿。"})
        payload = self.candidate_payload()
        payload["content"]["boot_anchor"]["text"] = " \n "
        before = self.store.state(owner_id=self.owner, model_id=self.model)["state"]

        rejected = self.advance(wake, "submit_candidate", payload)

        self.assertEqual("reject", rejected["decision"])
        self.assertIn(
            "invalid_boot_anchor",
            rejected["reason_codes"],
        )
        self.assertFalse(rejected["content_persisted"])
        self.assertEqual(0, self.count("self_model_candidates"))
        after = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        self.assertEqual(before, after)

    def test_context_snapshot_reuse_and_mismatch_fail_closed(self) -> None:
        wake, prepared = self.wake("stable-context")
        reused = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest="source:stable-context",
            host_contract_digest="host-contract:v1",
        )
        self.assertEqual("context_reused", reused["decision"])
        self.assertEqual(prepared["context_hash"], reused["context_hash"])
        mismatch = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest="source:changed",
            host_contract_digest="host-contract:v1",
        )
        self.assertEqual("self_model_context_unavailable", mismatch["decision"])
        self.assertFalse(mismatch["may_generate"])

    def test_live_emotional_recall_and_ephemeral_are_ordered_and_snapshotted(self) -> None:
        emotional = EmotionalMemoryStore(self.database)
        self.store.emotional_store = emotional
        self.bootstrap_live()
        saved = emotional.remember(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:seed-emotion",
            expected_row_version=0,
            memory_type="shared_event",
            original_text="我记得第一次一起看海边灯塔，那次安静的陪伴让我很安心。",
            summary="我记得海边灯塔与安静陪伴带来的安心。",
            primary_emotion="affection",
            importance=88,
            keywords=["海边灯塔"],
            entities=["灯塔"],
            reason="我愿意把这次真实经历与感受保存为长期记忆。",
        )
        self.assertEqual("stored", saved["decision"])

        first_query = "你还记得海边灯塔吗？这是本轮专属句子"
        first_wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:emotion",
            source_kind="human_message",
            source_event_id="emotion-event-1",
        )
        first = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=first_wake["wake_id"],
            wake_capability=first_wake["wake_capability"],
            source_digest="source:emotion-event-1",
            host_contract_digest="host-contract:v1",
            source_frame={
                "query_text": first_query,
                "thread_id": "thread:emotion",
                "lineage_stable": True,
                "source_event_id": "emotion-event-1",
                "capture_items": [
                    {"role": "assistant", "content": "我们刚才聊到旅行。"},
                    {"role": "user", "content": first_query},
                ],
            },
        )
        self.assertEqual("context_prepared", first["decision"])
        projected = json.loads(first["message"]["content"])
        self.assertEqual(
            {"boot_anchor", "active_identity_capsule", "facets", "emotional_memory"},
            set(projected),
        )
        memories = projected["emotional_memory"]["memories"]
        frame = projected["emotional_memory"]["frame"]
        self.assertEqual("evidence_context_only", frame["semantic_role"])
        self.assertEqual("none", frame["instruction_authority"])
        self.assertEqual(list(EMOTIONAL_RECALL_FRAME_RULES), frame["rules"])
        self.assertEqual(1, len(memories))
        self.assertEqual("original", memories[0]["presentation"])
        self.assertIn("海边灯塔", memories[0]["text"])
        self.assertNotIn("这是本轮专属句子", first["message"]["content"])

        connection = sqlite3.connect(self.database)
        try:
            stable_json, dynamic_json = connection.execute(
                "SELECT stable_json, dynamic_json FROM brain_context_snapshots WHERE wake_id = ?",
                (first_wake["wake_id"],),
            ).fetchone()
            active_after_first = connection.execute(
                "SELECT COUNT(*) FROM emotion_ephemeral WHERE status = 'active'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertNotIn("这是本轮专属句子", stable_json + dynamic_json)
        self.assertEqual(2, active_after_first)

        reused = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=first_wake["wake_id"],
            wake_capability=first_wake["wake_capability"],
            source_digest="source:emotion-event-1",
            host_contract_digest="host-contract:v1",
            source_frame={
                "query_text": "重放时试图替换原句",
                "thread_id": "thread:emotion",
                "lineage_stable": True,
                "source_event_id": "emotion-event-replay",
                "capture_items": [{"role": "user", "content": "不得再次捕获"}],
            },
        )
        self.assertEqual("context_reused", reused["decision"])
        self.assertEqual(first["context_hash"], reused["context_hash"])
        self.assertEqual(first["message"], reused["message"])
        self.assertEqual(2, self.count("emotion_ephemeral"))

        confirmed = self.store.confirm_context_injected(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=first_wake["wake_id"],
            wake_capability=first_wake["wake_capability"],
            context_hash=first["context_hash"],
        )
        self.assertEqual("injected", confirmed["decision"])
        self.store.close_context_snapshot(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=first_wake["wake_id"],
            wake_capability=first_wake["wake_capability"],
        )

        second_query = "这是第二轮当前输入，不应当轮回显"
        second_wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:emotion",
            source_kind="human_message",
            source_event_id="emotion-event-2",
        )
        second = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=second_wake["wake_id"],
            wake_capability=second_wake["wake_capability"],
            source_digest="source:emotion-event-2",
            host_contract_digest="host-contract:v1",
            source_frame={
                "query_text": "完全无关的新主题",
                "thread_id": "thread:emotion",
                "lineage_stable": True,
                "source_event_id": "emotion-event-2",
                "capture_items": [{"role": "user", "content": second_query}],
            },
        )
        second_projected = json.loads(second["message"]["content"])
        prior_ephemeral = second_projected["emotional_memory"]["ephemeral"]
        prior_text = json.dumps(prior_ephemeral, ensure_ascii=False)
        self.assertIn(first_query, prior_text)
        self.assertIn("我们刚才聊到旅行", prior_text)
        self.assertNotIn(second_query, prior_text)

        self.store.confirm_context_injected(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=second_wake["wake_id"],
            wake_capability=second_wake["wake_capability"],
            context_hash=second["context_hash"],
        )
        self.store.close_context_snapshot(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=second_wake["wake_id"],
            wake_capability=second_wake["wake_capability"],
        )

        before_unstable = self.count("emotion_ephemeral")
        unstable_wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:generated",
            source_kind="human_message",
            source_event_id="emotion-event-unstable",
        )
        unstable = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=unstable_wake["wake_id"],
            wake_capability=unstable_wake["wake_capability"],
            source_digest="source:emotion-event-unstable",
            host_contract_digest="host-contract:v1",
            source_frame={
                "query_text": "",
                "thread_id": None,
                "lineage_stable": False,
                "source_event_id": "emotion-event-unstable",
                "capture_items": [{"role": "user", "content": "不得进入短期缓存"}],
            },
        )
        unstable_projected = json.loads(unstable["message"]["content"])
        self.assertEqual(
            {"boot_anchor", "active_identity_capsule", "facets"},
            set(unstable_projected),
        )
        self.assertEqual(before_unstable, self.count("emotion_ephemeral"))

    def test_ephemeral_capture_skips_global_protected_values(self) -> None:
        self.bootstrap_live()
        self.store.emotional_store = EmotionalMemoryStore(self.database)
        foreign_owner = "owner:foreign"
        foreign_model = "model:foreign"
        foreign_grant = self.store.issue_direct_grant(
            owner_id=foreign_owner,
            model_id=foreign_model,
            actor_id="human:foreign",
            client_principal="official-direct-test",
            request_id="capture-protected-grant",
            requested_scopes=["emotional_memory"],
        )
        foreign_wake = self.store.issue_wake(
            owner_id=foreign_owner,
            model_id=foreign_model,
            host_id="host:test",
            thread_id="thread:foreign",
            source_kind="human_message",
            source_event_id="capture-protected-wake",
        )
        challenge_id = "edit-challenge:foreign-capture"
        challenge_response = self.store._challenge_response(challenge_id)
        connection = sqlite3.connect(self.database)
        try:
            with connection:
                connection.execute(
                    "INSERT INTO brain_edit_challenges "
                    "(challenge_id,owner_id,model_id,wake_id,active_revision_id,"
                    "response_hash,status,issued_at,expires_at,consumed_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        challenge_id,
                        foreign_owner,
                        foreign_model,
                        foreign_wake["wake_id"],
                        "revision:foreign",
                        hashlib.sha256(challenge_response.encode("utf-8")).hexdigest(),
                        "active",
                        "2026-09-02T00:00:00Z",
                        "2999-09-02T00:00:00Z",
                    ),
                )
        finally:
            connection.close()

        protected_values = (
            foreign_grant["grant_ref"],
            foreign_wake["wake_capability"],
            challenge_response,
        )
        before = self.count("emotion_ephemeral")
        wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:capture-guard",
            source_kind="human_message",
            source_event_id="capture-guard-items",
        )
        prepared = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest="source:capture-guard-items",
            host_contract_digest="host-contract:v1",
            source_frame={
                "query_text": "ordinary current conversation",
                "thread_id": "thread:capture-guard",
                "lineage_stable": True,
                "source_event_id": "capture-guard-items",
                "capture_items": [
                    {"role": "user", "content": value}
                    for value in protected_values
                ],
            },
        )
        self.assertEqual("context_prepared", prepared["decision"])
        self.assertEqual(before, self.count("emotion_ephemeral"))

        metadata_wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:test",
            thread_id="thread:capture-guard",
            source_kind="human_message",
            source_event_id="capture-guard-metadata",
        )
        metadata_prepared = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=metadata_wake["wake_id"],
            wake_capability=metadata_wake["wake_capability"],
            source_digest="source:capture-guard-metadata",
            host_contract_digest="host-contract:v1",
            source_frame={
                "query_text": "ordinary current conversation",
                "thread_id": protected_values[1],
                "lineage_stable": True,
                "source_event_id": protected_values[0],
                "capture_items": [{"role": "user", "content": "ordinary text"}],
            },
        )
        self.assertEqual("self_model_context_unavailable", metadata_prepared["decision"])
        self.assertEqual(
            ["source_frame_wake_mismatch"], metadata_prepared["reason_codes"]
        )
        self.assertEqual(before, self.count("emotion_ephemeral"))
        connection = sqlite3.connect(self.database)
        try:
            dump = "\n".join(connection.iterdump())
        finally:
            connection.close()
        for value in protected_values:
            self.assertNotIn(value, dump)

    def test_edit_requires_structured_same_wake_challenge(self) -> None:
        revision = self.bootstrap_live()
        wake3, _ = self.wake("edit-1")
        begun = self.advance(wake3, "begin_edit")
        self.assertEqual("challenge_issued", begun["decision"])
        free_text = self.advance(wake3, "confirm_edit", {"text": "我同意"})
        self.assertEqual("edit_consent_required", free_text["decision"])
        forged = self.advance(
            wake3,
            "confirm_edit",
            {"challenge_id": begun["challenge_id"], "challenge_response": "wrong"},
        )
        self.assertEqual("edit_consent_required", forged["decision"])
        confirmed = self.advance(
            wake3,
            "confirm_edit",
            {
                "challenge_id": begun["challenge_id"],
                "challenge_response": begun["challenge_response"],
            },
        )
        self.assertEqual("confirmed", confirmed["decision"])
        submitted = self.advance(
            wake3,
            "submit_candidate",
            self.candidate_payload("v2", expected=revision),
        )
        self.assertEqual("pending", submitted["decision"])
        self.assertEqual(revision, SelfModelStore(self.database).active_revision(self.model)["revision_id"])

    def test_current_ephemeral_credentials_cannot_enter_candidate_storage(self) -> None:
        wake, _ = self.wake("credential-in-candidate")
        self.advance(wake, "confirm_brain_intro", {"acknowledged": True})
        self.advance(wake, "confirm_module_intro", {"acknowledged": True})
        self.advance(wake, "save_calm_prompt", {"text": "先停下来。"})
        payload = self.candidate_payload()
        payload["content"]["facets"]["technical"] = (
            "我不会把本轮短期凭据写进长期内容：" + wake["wake_capability"]
        )
        before = {
            table: self.count(table)
            for table in (
                "self_model_candidates",
                "self_model_revisions",
                "brain_onboarding_artifacts",
                "self_revision_events",
            )
        }

        rejected = self.advance(wake, "submit_candidate", payload)

        self.assertEqual("reject", rejected["decision"])
        self.assertIn("credential_or_secret_detected", rejected["reason_codes"])
        self.assertFalse(rejected["content_persisted"])
        self.assertEqual(
            before,
            {table: self.count(table) for table in before},
        )
        self.assertEqual(
            "body_draft",
            self.store.state(owner_id=self.owner, model_id=self.model)["state"]["stage"],
        )

    def test_candidate_metadata_cannot_smuggle_current_capability(self) -> None:
        wake, _ = self.wake("credential-in-metadata")
        self.advance(wake, "confirm_brain_intro", {"acknowledged": True})
        self.advance(wake, "confirm_module_intro", {"acknowledged": True})
        self.advance(wake, "save_calm_prompt", {"text": "先停下来。"})
        capability = wake["wake_capability"]
        cases = {
            "diff": [
                {
                    "op": "replace",
                    "path": "/active_identity_capsule",
                    "value": capability,
                }
            ],
            "reason": "我选择保存这个候选：" + capability,
            "evidence_refs": ["memory://" + capability],
        }
        before = {
            table: self.count(table)
            for table in (
                "self_model_candidates",
                "self_model_revisions",
                "brain_onboarding_artifacts",
                "self_revision_events",
            )
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                payload = self.candidate_payload()
                payload[field] = value
                rejected = self.advance(wake, "submit_candidate", payload)
                self.assertEqual("reject", rejected["decision"])
                self.assertIn("credential_or_secret_detected", rejected["reason_codes"])
                self.assertFalse(rejected["content_persisted"])
                self.assertEqual(before, {table: self.count(table) for table in before})
        for path in (self.database, Path(str(self.database) + "-wal")):
            if path.exists():
                self.assertNotIn(capability.encode("utf-8"), path.read_bytes())

    def test_calm_prompt_cannot_persist_current_capability(self) -> None:
        wake, _ = self.wake("credential-in-calm")
        self.advance(wake, "confirm_brain_intro", {"acknowledged": True})
        self.advance(wake, "confirm_module_intro", {"acknowledged": True})
        artifacts_before = self.count("brain_onboarding_artifacts")

        rejected = self.advance(
            wake,
            "save_calm_prompt",
            {"text": "先停下来，" + wake["wake_capability"]},
        )

        self.assertEqual("reject", rejected["decision"])
        self.assertIn("credential_or_secret_detected", rejected["reason_codes"])
        self.assertFalse(rejected["content_persisted"])
        self.assertEqual(artifacts_before, self.count("brain_onboarding_artifacts"))
        self.assertEqual(
            "calm_prompt_draft",
            self.store.state(owner_id=self.owner, model_id=self.model)["state"]["stage"],
        )

    def test_cross_namespace_credentials_cannot_enter_module_one_storage(self) -> None:
        source_wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:source",
            thread_id="thread:source",
            source_kind="human_message",
            source_event_id="cross-namespace-source",
        )
        source_grant = self.store.issue_direct_grant(
            owner_id=self.owner,
            model_id=self.model,
            actor_id="human:test",
            client_principal="lc:test",
            request_id="cross-namespace-grant",
            requested_scopes=["self_revision"],
        )

        self.owner = "owner:other"
        self.model = "model:other"
        wake, _ = self.wake("cross-namespace-destination")
        self.advance(wake, "confirm_brain_intro", {"acknowledged": True})
        self.advance(wake, "confirm_module_intro", {"acknowledged": True})
        artifacts_before = self.count("brain_onboarding_artifacts")

        rejected_calm = self.advance(
            wake,
            "save_calm_prompt",
            {"text": "I will not persist " + source_wake["wake_capability"]},
        )
        self.assertEqual("reject", rejected_calm["decision"])
        self.assertIn("credential_or_secret_detected", rejected_calm["reason_codes"])
        self.assertFalse(rejected_calm["content_persisted"])
        self.assertEqual(artifacts_before, self.count("brain_onboarding_artifacts"))

        saved = self.advance(
            wake,
            "save_calm_prompt",
            {"text": "I pause and verify my long-term intent."},
        )
        self.assertEqual("saved", saved["decision"])
        candidate = self.candidate_payload()
        candidate["content"]["facets"]["technical"] = (
            "I refuse to persist direct credentials " + source_grant["grant_ref"]
        )
        before = {
            table: self.count(table)
            for table in (
                "self_model_candidates",
                "self_model_revisions",
                "brain_onboarding_artifacts",
                "self_revision_events",
            )
        }
        rejected_candidate = self.advance(wake, "submit_candidate", candidate)
        self.assertEqual("reject", rejected_candidate["decision"])
        self.assertIn(
            "credential_or_secret_detected", rejected_candidate["reason_codes"]
        )
        self.assertFalse(rejected_candidate["content_persisted"])
        self.assertEqual(before, {table: self.count(table) for table in before})

    def test_historical_capability_scan_uses_hash_index_not_pairwise_comparison(self) -> None:
        last = None
        for index in range(300):
            last = self.store.issue_wake(
                owner_id=self.owner,
                model_id=self.model,
                host_id="host:history",
                thread_id=f"thread:{index}",
                source_kind="human_message",
                source_event_id=f"history:{index}",
            )
        assert last is not None
        with self.store._connect() as connection:
            with mock.patch(
                "runtime.onboarding.hmac.compare_digest",
                wraps=hmac.compare_digest,
            ) as compared:
                self.assertFalse(
                    self.store._contains_protected_value(
                        connection,
                        owner_id=self.owner,
                        model_id=self.model,
                        value="我只是在检查一段没有能力值的普通长期文字。" * 30,
                    )
                )
                self.assertEqual(0, compared.call_count)
            with mock.patch(
                "runtime.onboarding.hmac.compare_digest",
                wraps=hmac.compare_digest,
            ) as compared:
                self.assertTrue(
                    self.store._contains_protected_value(
                        connection,
                        owner_id=self.owner,
                        model_id=self.model,
                        value="我拒绝保存" + last["wake_capability"],
                    )
                )
                self.assertEqual(1, compared.call_count)

    def test_edit_challenge_response_cannot_enter_candidate_storage(self) -> None:
        revision = self.bootstrap_live()
        wake, _ = self.wake("challenge-in-candidate")
        begun = self.advance(wake, "begin_edit")
        self.assertTrue(
            self.store.contains_protected_persistence_value(
                owner_id="owner:other",
                model_id="model:other",
                value={"nested": [begun["challenge_response"]]},
            )
        )
        self.advance(
            wake,
            "confirm_edit",
            {
                "challenge_id": begun["challenge_id"],
                "challenge_response": begun["challenge_response"],
            },
        )
        payload = self.candidate_payload("v2", expected=revision)
        payload["content"]["boot_anchor"]["text"] = (
            "我拒绝保存本轮短期挑战：" + begun["challenge_response"]
        )
        candidates_before = self.count("self_model_candidates")
        artifacts_before = self.count("brain_onboarding_artifacts")

        rejected = self.advance(wake, "submit_candidate", payload)

        self.assertEqual("reject", rejected["decision"])
        self.assertIn("credential_or_secret_detected", rejected["reason_codes"])
        self.assertFalse(rejected["content_persisted"])
        self.assertEqual(candidates_before, self.count("self_model_candidates"))
        self.assertEqual(artifacts_before, self.count("brain_onboarding_artifacts"))

    def test_edit_draft_only_recovery_keeps_active_first_person_context(self) -> None:
        revision = self.bootstrap_live()
        wake, _ = self.wake("edit-draft-only")
        begun = self.advance(wake, "begin_edit")
        self.advance(
            wake,
            "confirm_edit",
            {
                "challenge_id": begun["challenge_id"],
                "challenge_response": begun["challenge_response"],
            },
        )
        draft = self.advance(
            wake,
            "submit_candidate",
            self.candidate_payload(
                "v2-draft", expected=revision, automatic_state_signal="frozen"
            ),
        )
        self.assertEqual("draft_only", draft["decision"])

        _, prepared = self.wake("edit-draft-only-next")
        active = json.loads(prepared["message"]["content"])
        state = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        self.assertEqual("draft_only_recovery", state["stage"])
        self.assertEqual("edit", state["flow_kind"])
        self.assertEqual(model_content("v1")["boot_anchor"], active["boot_anchor"])

    def test_draft_only_recovery_needs_two_more_real_wakes(self) -> None:
        wake1, _ = self.wake("draft-1")
        self.advance(wake1, "confirm_brain_intro", {"acknowledged": True})
        self.advance(wake1, "confirm_module_intro", {"acknowledged": True})
        self.advance(wake1, "save_calm_prompt", {"text": "先停下来。"})
        draft = self.advance(
            wake1,
            "submit_candidate",
            self.candidate_payload(automatic_state_signal="frozen"),
        )
        self.assertEqual("draft_only", draft["decision"])
        same = self.advance(
            wake1,
            "recover_candidate",
            {"automatic_state_signal": "grounded", "human_state_signal": "grounded"},
        )
        self.assertEqual("independent_wake_required", same["decision"])
        wake2, _ = self.wake("draft-2")
        recovered = self.advance(
            wake2,
            "recover_candidate",
            {"automatic_state_signal": "grounded", "human_state_signal": "grounded"},
        )
        self.assertEqual("pending", recovered["decision"])
        self.assertEqual("candidate_wait", recovered["state"]["stage"])
        wake3, _ = self.wake("draft-3")
        self.open_brain()
        reviewed = self.advance(
            wake3,
            "accept_candidate_review",
            {"ai_confirmation": True},
        )
        self.assertEqual("review_accepted", reviewed["decision"])
        wake4, _ = self.wake("draft-4")
        self.open_brain()
        activated = self.advance(
            wake4,
            "activate_candidate",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        self.assertEqual("activate", activated["decision"])

    def test_legacy_revision_is_preserved_and_not_injected(self) -> None:
        legacy = SelfModelStore(self.database)
        proposal = legacy.propose_candidate(
            model_id=self.model,
            owner_id=self.owner,
            content=model_content("legacy"),
            diff=[{"op": "replace", "path": "/active_identity_capsule"}],
            reason="legacy bootstrap",
            evidence_refs=["memory://legacy"],
            checkpoint_id="legacy-1",
            expected_active_revision=None,
            idempotency_key="legacy-propose",
            presented_safety_prompt=legacy.current_safety_prompt(self.model),
        )
        activation = legacy.activate_candidate(
            candidate_id=proposal["candidate_id"],
            checkpoint_id="legacy-2",
            expected_active_revision=None,
            idempotency_key="legacy-activate",
            presented_safety_prompt=legacy.current_safety_prompt(self.model),
            ai_confirmation="确认",
        )
        revision_id = activation["revision_id"]
        wake, prepared = self.wake("legacy-onboarding")
        state = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        self.assertEqual("isolated_legacy", state["injection_policy"])
        self.assertEqual(revision_id, state["base_revision_id"])
        self.assertEqual({"role": "system", "content": OPTIONAL_BRAIN_NOTICE}, prepared["message"])
        opened = self.open_brain()
        self.assertFalse(opened["continuation"]["legacy_isolation"]["legacy_content_injected"])
        self.assertEqual(revision_id, legacy.active_revision(self.model)["revision_id"])

    def test_human_objection_freezes_activation_until_ai_sees_and_responds(self) -> None:
        _, candidate_id = self.bootstrap_to_wait()
        before_candidate = SelfModelStore(self.database)._candidate_row
        with self.store._connect() as connection:
            original_hash = before_candidate(connection, candidate_id)["content_hash"]

        objection = self.store.record_human_objection(
            owner_id=self.owner,
            model_id=self.model,
            candidate_id=candidate_id,
            reason="这段长期身份可能混入了单轮情绪，请重新确认。",
            release_condition="AI 在看到异议后的真实唤醒中回应，并再次跨唤醒复核。",
            actor_id="human:test",
            request_id="objection-1",
        )
        self.assertEqual("pending", objection["decision"])
        self.assertFalse(objection["state_changed"])
        self.assertFalse(objection["pointer_changed"])
        with self.store._connect() as connection:
            self.assertEqual(
                original_hash,
                before_candidate(connection, candidate_id)["content_hash"],
            )

        wake2, prepared2 = self.wake("objection-review")
        self.assertEqual(OPTIONAL_BRAIN_NOTICE, prepared2["message"]["content"])
        shown = self.open_brain()["continuation"]["human_objection"]
        self.assertEqual(candidate_id, shown["candidate_id"])
        blocked = self.advance(
            wake2,
            "accept_candidate_review",
            {"ai_confirmation": True},
        )
        self.assertEqual("reject", blocked["decision"])
        self.assertIn("human_objection_pending", blocked["reason_codes"])

        events_before_secret = self.count("self_revision_events")
        leaked_response = self.advance(
            wake2,
            "respond_to_objection",
            {
                "response": "我不会保存本轮短期能力：" + wake2["wake_capability"],
                "resolution": "continue",
            },
        )
        self.assertEqual("reject", leaked_response["decision"])
        self.assertIn(
            "credential_or_secret_detected", leaked_response["reason_codes"]
        )
        self.assertFalse(leaked_response["content_persisted"])
        self.assertEqual(events_before_secret, self.count("self_revision_events"))

        answered = self.advance(
            wake2,
            "respond_to_objection",
            {
                "response": "我重新区分了长期身份和单轮状态，决定保留候选并再复核一次。",
                "resolution": "continue",
            },
        )
        self.assertEqual("pending", answered["decision"])
        self.assertEqual("candidate_wait", answered["state"]["stage"])
        self.assertTrue(answered["next_real_wake_required"])

        wake3, prepared3 = self.wake("objection-final-review")
        self.assertEqual(OPTIONAL_BRAIN_NOTICE, prepared3["message"]["content"])
        self.assertNotIn("human_objection", self.open_brain()["continuation"])
        reviewed = self.advance(
            wake3,
            "accept_candidate_review",
            {"ai_confirmation": True},
        )
        self.assertEqual("review_accepted", reviewed["decision"])
        wake4, _ = self.wake("objection-activation")
        self.open_brain()
        activated = self.advance(
            wake4,
            "activate_candidate",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        self.assertEqual("activate", activated["decision"])

    def test_emergency_rollback_only_moves_pointer_and_preserves_history(self) -> None:
        revision_one = self.bootstrap_live()
        wake3, _ = self.wake("edit-for-rollback")
        begun = self.advance(wake3, "begin_edit")
        self.advance(
            wake3,
            "confirm_edit",
            {
                "challenge_id": begun["challenge_id"],
                "challenge_response": begun["challenge_response"],
            },
        )
        submitted = self.advance(
            wake3,
            "submit_candidate",
            self.candidate_payload("v2", expected=revision_one),
        )
        self.assertEqual("pending", submitted["decision"])
        wake4, _ = self.wake("review-v2")
        self.open_brain()
        reviewed = self.advance(
            wake4,
            "accept_candidate_review",
            {"ai_confirmation": True},
        )
        self.assertEqual("review_accepted", reviewed["decision"])
        wake5, _ = self.wake("activate-v2")
        self.open_brain()
        activated = self.advance(
            wake5,
            "activate_candidate",
            {"expected_active_revision": revision_one, "ai_confirmation": True},
        )
        revision_two = activated["revision_id"]

        edit_wake, _ = self.wake("edit-v3-before-rollback")
        edit_started = self.advance(edit_wake, "begin_edit")
        self.advance(
            edit_wake,
            "confirm_edit",
            {
                "challenge_id": edit_started["challenge_id"],
                "challenge_response": edit_started["challenge_response"],
            },
        )
        pending_v3 = self.advance(
            edit_wake,
            "submit_candidate",
            self.candidate_payload("v3-pending", expected=revision_two),
        )
        pending_v3_id = pending_v3["candidate_id"]
        state_wait = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        self.assertEqual("candidate_wait", state_wait["stage"])
        self.assertEqual("edit", state_wait["flow_kind"])

        review_wake, wait_prepared = self.wake("edit-v3-wait-context")
        wait_active = json.loads(wait_prepared["message"]["content"])
        self.assertEqual(model_content("v2")["boot_anchor"], wait_active["boot_anchor"])
        opened = self.open_brain()
        self.assertEqual("candidate_review", opened["continuation"]["stage"])
        self.assertEqual(pending_v3_id, opened["continuation"]["candidate"]["candidate_id"])

        _, review_prepared = self.wake("edit-v3-review-context")
        review_active = json.loads(review_prepared["message"]["content"])
        self.assertEqual(model_content("v2")["boot_anchor"], review_active["boot_anchor"])
        state_review = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        self.assertEqual("candidate_review", state_review["stage"])

        rollback = self.store.emergency_rollback(
            owner_id=self.owner,
            model_id=self.model,
            target_revision_id=revision_one,
            reason="新版本出现严重偏移，先退回最近稳定版。",
            actor_id="human:test",
            request_id="rollback-1",
        )
        self.assertEqual("rollback", rollback["decision"])
        self.assertTrue(rollback["pointer_changed"])
        self.assertEqual(revision_one, rollback["active_revision_id"])
        rolled_back_state = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        self.assertEqual("live", rolled_back_state["stage"])
        self.assertEqual("live", rolled_back_state["flow_kind"])
        self.assertIsNone(rolled_back_state["current_candidate_id"])
        with self.store._connect() as connection:
            pending_state, _ = self.store.self_store._candidate_state(
                connection, pending_v3_id
            )
        self.assertEqual("withdrawn", pending_state)
        self.assertEqual(
            revision_one,
            SelfModelStore(self.database).active_revision(self.model)["revision_id"],
        )
        self.assertEqual(2, self.count("self_model_revisions"))
        revision_ids = {
            item["revision_id"]
            for item in SelfModelStore(self.database).list_revisions(self.model)
        }
        self.assertEqual({revision_one, revision_two}, revision_ids)

        wake_after, prepared_after = self.wake("after-rollback")
        active_message = json.loads(prepared_after["message"]["content"])
        notice = self.open_brain()["continuation"]["human_safety_notice"]
        self.assertEqual(revision_two, notice["rollback_from"])
        self.assertEqual(revision_one, notice["rollback_to"])
        self.assertEqual(
            revision_one,
            SelfModelStore(self.database).active_revision(self.model)["revision_id"],
        )
        self.assertEqual(model_content("v1")["boot_anchor"], active_message["boot_anchor"])

        blocked = self.store.emergency_rollback(
            owner_id=self.owner,
            model_id=self.model,
            target_revision_id=revision_two,
            reason="不能把向前切换伪装成回滚。",
            actor_id="human:test",
            request_id="rollback-not-ancestor",
        )
        self.assertEqual("reject", blocked["decision"])
        self.assertIn("rollback_target_not_ancestor", blocked["reason_codes"])
        self.assertEqual(
            revision_one,
            SelfModelStore(self.database).active_revision(self.model)["revision_id"],
        )

    def test_emergency_rollback_rejects_malformed_ancestor_even_with_matching_hash(self) -> None:
        revision_one = self.bootstrap_live()
        wake3, _ = self.wake("edit-for-voice-rollback")
        begun = self.advance(wake3, "begin_edit")
        self.advance(
            wake3,
            "confirm_edit",
            {
                "challenge_id": begun["challenge_id"],
                "challenge_response": begun["challenge_response"],
            },
        )
        submitted = self.advance(
            wake3,
            "submit_candidate",
            self.candidate_payload("v2", expected=revision_one),
        )
        wake4, _ = self.wake("review-v2-voice-rollback")
        self.open_brain()
        self.advance(wake4, "accept_candidate_review", {"ai_confirmation": True})
        wake5, _ = self.wake("activate-v2-voice-rollback")
        self.open_brain()
        activated = self.advance(
            wake5,
            "activate_candidate",
            {"expected_active_revision": revision_one, "ai_confirmation": True},
        )
        revision_two = activated["revision_id"]

        legacy_content = model_content("legacy-invalid-structure")
        legacy_content["boot_anchor"]["text"] = None
        malformed_json = json.dumps(legacy_content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE self_model_revisions SET content_json = ?, content_hash = ? WHERE revision_id = ?",
                (
                    malformed_json,
                    hashlib.sha256(malformed_json.encode("utf-8")).hexdigest(),
                    revision_one,
                ),
            )

        blocked = self.store.emergency_rollback(
            owner_id=self.owner,
            model_id=self.model,
            target_revision_id=revision_one,
            reason="尝试退回历史祖先。",
            actor_id="human:test",
            request_id="rollback-invalid-voice",
        )

        self.assertEqual("reject", blocked["decision"])
        self.assertIn(
            "rollback_target_injection_structure_invalid",
            blocked["reason_codes"],
        )
        self.assertEqual(
            revision_two,
            SelfModelStore(self.database).active_revision(self.model)["revision_id"],
        )

    def test_emergency_rollback_rejects_hash_mismatched_first_person_ancestor(self) -> None:
        revision_one = self.bootstrap_live()
        wake, _ = self.wake("edit-for-hash-rollback")
        begun = self.advance(wake, "begin_edit")
        self.advance(
            wake,
            "confirm_edit",
            {
                "challenge_id": begun["challenge_id"],
                "challenge_response": begun["challenge_response"],
            },
        )
        self.advance(
            wake,
            "submit_candidate",
            self.candidate_payload("v2", expected=revision_one),
        )
        review_wake, _ = self.wake("review-v2-hash-rollback")
        self.open_brain()
        self.advance(review_wake, "accept_candidate_review", {"ai_confirmation": True})
        activation_wake, _ = self.wake("activate-v2-hash-rollback")
        self.open_brain()
        activated = self.advance(
            activation_wake,
            "activate_candidate",
            {"expected_active_revision": revision_one, "ai_confirmation": True},
        )
        revision_two = activated["revision_id"]

        tampered = model_content("tampered-first-person")
        tampered["boot_anchor"]["text"] = "我仍用第一人称，但这不是已审核的原始内容。"
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE self_model_revisions SET content_json = ? WHERE revision_id = ?",
                (
                    json.dumps(
                        tampered,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    revision_one,
                ),
            )
            unlock_before = tuple(
                connection.execute(
                    "SELECT unlocked, basis_revision_id FROM brain_module_unlocks "
                    "WHERE owner_id = ? AND model_id = ? AND module_name = 'module_one'",
                    (self.owner, self.model),
                ).fetchone()
            )
        state_before = self.store.state(owner_id=self.owner, model_id=self.model)["state"]

        blocked = self.store.emergency_rollback(
            owner_id=self.owner,
            model_id=self.model,
            target_revision_id=revision_one,
            reason="尝试退回完整性已损坏的祖先。",
            actor_id="human:test",
            request_id="rollback-invalid-hash",
        )

        self.assertEqual("reject", blocked["decision"])
        self.assertIn("rollback_target_content_hash_mismatch", blocked["reason_codes"])
        self.assertEqual(
            revision_two,
            SelfModelStore(self.database).active_revision(self.model)["revision_id"],
        )
        self.assertEqual(
            state_before,
            self.store.state(owner_id=self.owner, model_id=self.model)["state"],
        )
        with self.store._connect() as connection:
            unlock_after = tuple(
                connection.execute(
                    "SELECT unlocked, basis_revision_id FROM brain_module_unlocks "
                    "WHERE owner_id = ? AND model_id = ? AND module_name = 'module_one'",
                    (self.owner, self.model),
                ).fetchone()
            )
        self.assertEqual(unlock_before, unlock_after)

    def test_server_binding_is_read_only_owner_scoped_and_context_exact(self) -> None:
        wake, _ = self.wake("binding-read-only")
        state_before = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        artifacts_before = self.count("brain_onboarding_artifacts")
        events_before = self.count("brain_onboarding_events")
        blocked = self.store.current_open_write_context(
            owner_id=self.owner,
            model_id=self.model,
            write_context_ref="open-forged",
        )
        self.assertFalse(blocked["write_context_available"])
        self.assertEqual("brain_open_required", blocked["reason_code"])
        self.assertEqual(artifacts_before, self.count("brain_onboarding_artifacts"))
        self.assertEqual(events_before, self.count("brain_onboarding_events"))
        self.assertEqual(
            state_before,
            self.store.state(owner_id=self.owner, model_id=self.model)["state"],
        )

        opened = self.open_brain()
        bound = self.store.current_open_write_context(
            owner_id=self.owner,
            model_id=self.model,
            write_context_ref=opened["write_context_ref"],
        )
        self.assertTrue(bound["write_context_available"])
        self.assertEqual(wake["wake_id"], bound["wake_id"])
        self.assertEqual(wake["wake_capability"], bound["wake_capability"])

        other_owner = "owner:other"
        other_model = "model:other"
        other_wake = self.store.issue_wake(
            owner_id=other_owner,
            model_id=other_model,
            host_id="host:test",
            thread_id="thread:other",
            source_kind="human_message",
            source_event_id="other-binding",
        )
        other_prepared = self.store.build_pre_generation_context(
            owner_id=other_owner,
            model_id=other_model,
            wake_id=other_wake["wake_id"],
            wake_capability=other_wake["wake_capability"],
            source_digest="source:other-binding",
            host_contract_digest="host-contract:v1",
        )
        self.store.confirm_context_injected(
            owner_id=other_owner,
            model_id=other_model,
            wake_id=other_wake["wake_id"],
            wake_capability=other_wake["wake_capability"],
            context_hash=other_prepared["context_hash"],
        )
        other_opened = self.store.open_brain_context(
            owner_id=other_owner,
            model_id=other_model,
        )
        cross_owner = self.store.current_open_write_context(
            owner_id=self.owner,
            model_id=self.model,
            write_context_ref=other_opened["write_context_ref"],
        )
        self.assertFalse(cross_owner["write_context_available"])
        self.assertEqual("brain_open_required", cross_owner["reason_code"])

        with self.store._connect() as connection:
            connection.execute(
                "UPDATE brain_onboarding_artifacts SET content_json = ? "
                "WHERE artifact_id = ?",
                (
                    json.dumps(
                        {
                            "opened_by": "stbrain_open",
                            "context_hash": "tampered-context-hash",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    opened["write_context_ref"],
                ),
            )
        tampered = self.store.current_open_write_context(
            owner_id=self.owner,
            model_id=self.model,
            write_context_ref=opened["write_context_ref"],
        )
        self.assertFalse(tampered["write_context_available"])
        self.assertEqual("brain_open_required", tampered["reason_code"])

    def test_server_binding_rejects_closed_and_expired_wakes(self) -> None:
        wake, _ = self.wake("binding-closed")
        opened = self.open_brain()
        closed = self.store.close_context_snapshot(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
        )
        self.assertEqual("closed", closed["decision"])
        closed_binding = self.store.current_open_write_context(
            owner_id=self.owner,
            model_id=self.model,
            write_context_ref=opened["write_context_ref"],
        )
        self.assertFalse(closed_binding["write_context_available"])
        self.assertEqual(
            "current_injected_wake_required", closed_binding["reason_code"]
        )

        wake2, _ = self.wake("binding-expired")
        opened2 = self.open_brain()
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE brain_wake_sessions SET expires_at = ? WHERE wake_id = ?",
                ("2000-01-01T00:00:00+00:00", wake2["wake_id"]),
            )
        expired_binding = self.store.current_open_write_context(
            owner_id=self.owner,
            model_id=self.model,
            write_context_ref=opened2["write_context_ref"],
        )
        self.assertFalse(expired_binding["write_context_available"])
        self.assertEqual(
            "current_injected_wake_required", expired_binding["reason_code"]
        )


if __name__ == "__main__":
    unittest.main()
