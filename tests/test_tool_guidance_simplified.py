"""Synthetic-only regression for authored scene reminders and direct revisions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import jsonschema

from runtime.tool_guidance import ToolGuidanceError, ToolGuidanceStore, _scene_score
from tests.test_tool_guidance import action_card, catalog


class SimplifiedToolGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic.db"
        self.store = ToolGuidanceStore(self.path)
        self.owner, self.model = "synthetic-owner", "synthetic-model"
        self.store.ensure_state(owner_id=self.owner, model_id=self.model)

    def row(self):
        return self.store.status(owner_id=self.owner, model_id=self.model)["row_version"]

    def create(self, **changes):
        fields = dict(tool_name="HomeMCP", purpose="到家后，可以查看家中设备的状态。",
                      reminder="到家时，可以先看看家里设备的状态。", keywords=["到家", "回家"])
        fields.update(changes)
        return self.store.remember(owner_id=self.owner, model_id=self.model, wake_id="same-wake",
            expected_row_version=self.row(), reason="Synthetic authored reminder", catalog=None, **fields)

    def revise(self, card, **changes):
        return self.store.revise(owner_id=self.owner, model_id=self.model, wake_id="same-wake", wake_seq=1,
            expected_row_version=self.row(), card_id=card["card_id"], expected_card_version=card["version"],
            reason="Synthetic exact author revision", catalog=None, **changes)

    def recall(self, query="我刚到家，想看看情况", **kwargs):
        return self.store.build_recall_envelopes(owner_id=self.owner, model_id=self.model,
            query=query, catalog=kwargs.pop("catalog", None), **kwargs)

    def counts(self):
        with closing(sqlite3.connect(self.path)) as c:
            return {name: c.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in
                ("tool_cards", "tool_card_versions", "tool_guidance_candidates", "tool_audit_events")}

    def details(self, card_id, **kwargs):
        return self.store.recall(owner_id=self.owner, model_id=self.model,
            card_id=card_id, view="card", **kwargs)["results"][0]

    def seed_pending(self, card):
        # Compatibility fixture, not an exposed ordinary MCP route.
        return self.store.propose_revision(owner_id=self.owner, model_id=self.model,
            wake_id="legacy-wake", wake_seq=10, expected_row_version=self.row(),
            card_id=card["card_id"], expected_card_version=card["version"], intent="revise", edit_class="major",
            purpose="这份旧候选建议在回家后查看设备。", reason="Synthetic legacy pending proposal",
            correctness_assessment="I compared this complete synthetic proposal carefully.",
            calm_check_stability="This synthetic meaning remains stable over time.",
            calm_check_necessity="This synthetic revision describes the narrower purpose.",
            calm_check_consequences="The change affects advice and preserves execution checks.",
            calm_check_alternatives="Keeping the previous version is still an available alternative.",
            catalog=None)

    def test_light_service_card_and_exact_author_reminder_without_catalog(self):
        saved = self.create(tool_name="家庭设备服务")
        content = saved["card"]["content"]
        self.assertIsNone(content["observed_schema_hash"])
        self.assertEqual("general", content["operation_key"])
        self.assertEqual([], content["scenario_tags"])
        self.assertEqual("", content["call_notes"])
        self.assertEqual(("real_world_action", "high", "explicit_each_time"),
            tuple(content[k] for k in ("capability_class", "risk_level", "confirmation_policy")))
        envelope = self.recall()["envelopes"][0]
        self.assertEqual(content["reminder"], envelope["content"]["scene_summary"])
        self.assertEqual("authored_reminder", envelope["summary_source"])
        self.assertLess(envelope["token_cost"], 150)
        encoded = json.dumps(envelope, ensure_ascii=False)
        for forbidden in ("家庭设备服务", "canonical_tool_name", "operation_key", "call_notes", "schema_hash", "parameters"):
            self.assertNotIn(forbidden, encoded)

    def test_legacy_omission_preserves_content_shape_hash_and_manual_notes(self):
        fields = action_card()
        fields.pop("reason")
        saved = self.store.remember(owner_id=self.owner, model_id=self.model, wake_id="legacy",
            expected_row_version=self.row(), reason="Synthetic old card", catalog=catalog(), **fields)
        card = saved["card"]
        before = self.details(card["card_id"])
        self.assertNotIn("reminder", before["content"])
        for advertised in (None, catalog(home_schema="c" * 64), {"catalog_complete": True, "entries": []}):
            envelope = self.recall(query="home.arrival", catalog=advertised)["envelopes"][0]
            self.assertEqual(fields["purpose"], envelope["content"]["scene_summary"])
            self.assertEqual("legacy_purpose_excerpt", envelope["summary_source"])
        after = self.details(card["card_id"], catalog=catalog(home_schema="c" * 64))
        self.assertEqual(before["content_hash"], after["content_hash"])
        self.assertEqual(before["content"], after["content"])
        self.assertEqual(fields["call_notes"], after["content"]["call_notes"])
        self.assertFalse(after["call_notes_current"])
        self.assertTrue(after["call_notes_available"])

    def test_confidence_expiry_and_ai_reported_failure_are_not_reminder_gates(self):
        old = datetime.now(timezone.utc) - timedelta(days=60)
        with patch("runtime.tool_guidance._now_dt", return_value=old):
            card = self.create(confidence=0, expires_at=(old + timedelta(days=30)).isoformat())["card"]
        self.store.record_experience(owner_id=self.owner, model_id=self.model, wake_id="failed",
            expected_row_version=self.row(), card_id=card["card_id"], outcome="timeout", reason_code="timeout",
            attempt_summary="Synthetic timeout, result unknown.", lesson="Check any later actual result.",
            confidence=40, catalog=None)
        self.assertEqual("expired", self.details(card["card_id"])["effective_status"])
        self.assertTrue(self.recall()["envelopes"])
        gate = self.store.execution_gate(owner_id=self.owner, model_id=self.model, card_id=card["card_id"],
            catalog=None, current_user_intent=True, authorization_verified=True, current_confirmation=True)
        self.assertNotEqual("allowed_to_attempt", gate["decision"])

    def test_any_authored_field_direct_revision_preserves_history(self):
        card = self.create()["card"]
        changed = self.revise(card, edit_class="metadata", tool_name="HomeMCP-v2", operation_key="service_advice",
            purpose="在回家场景下查看设备。", reminder="回家时可以查看设备状态。", keywords=["回家"],
            aliases=["设备"], scenario_examples=["刚回家"], chain_role="entry", handoff_condition="先查看状态。",
            confidence=30, salience=99, call_notes="Only the current live tool documentation supplies parameters.")
        self.assertEqual("direct_revision", changed["submission_mode"])
        self.assertEqual(2, changed["card"]["version"])
        self.assertEqual(0, self.counts()["tool_guidance_candidates"])
        self.assertEqual(2, self.counts()["tool_card_versions"])
        history = self.store.recall(owner_id=self.owner, model_id=self.model,
            card_id=card["card_id"], view="history")["versions"]
        self.assertEqual(card["content_hash"], history[1]["content_hash"])
        self.assertIsNone(changed["card"]["content"]["observed_schema_hash"])

    def test_direct_edit_does_not_refresh_observation_or_expiry(self):
        fields = action_card()
        fields.pop("reason")
        saved = self.store.remember(owner_id=self.owner, model_id=self.model, wake_id="legacy",
            expected_row_version=self.row(), reason="Synthetic", catalog=catalog(), **fields)["card"]
        edited = self.revise(saved, purpose="Changed authored wording.")["card"]
        self.assertEqual(saved["content"]["expires_at"], edited["content"]["expires_at"])
        self.assertEqual(saved["content"]["observed_schema_hash"], edited["content"]["observed_schema_hash"])

    def test_conflict_cross_owner_and_internal_field_edits_rejected(self):
        card = self.create()["card"]
        for field, value in {"observed_schema_hash": "a" * 64, "owner_id_other": "other",
                             "effective_confidence": 100, "lifecycle": "retired"}.items():
            before = self.counts()
            with self.assertRaises(ToolGuidanceError):
                self.revise(card, **{field: value})
            self.assertEqual(before, self.counts())
        self.revise(card, reminder="新的提醒。")
        with self.assertRaisesRegex(ToolGuidanceError, "tool_card_version_conflict"):
            self.revise(card, reminder="冲突版本。")
        self.assertEqual([], self.store.recall(owner_id="other", model_id=self.model, view="directory")["results"])

    def test_retire_and_restore_keep_every_version_and_author_switch(self):
        card = self.create()["card"]
        retired = self.revise(card, intent="retire")["card"]
        self.assertEqual([], self.recall()["envelopes"])
        self.assertEqual("retired", self.details(card["card_id"])["lifecycle"])
        restored = self.revise(retired, intent="restore", target_version=1)["card"]
        self.assertEqual(3, restored["version"])
        self.assertTrue(self.recall()["envelopes"])
        off = self.revise(restored, auto_recall_mode="never_auto", salience_reason="我选择手动查询。")
        self.assertEqual([], self.recall()["envelopes"])
        self.assertEqual(4, self.counts()["tool_card_versions"])
        self.assertEqual(1, self.counts()["tool_cards"])
        self.assertEqual("never_auto", self.details(card["card_id"])["content"]["auto_recall_mode"])

    def test_withdraw_old_pending_offline_after_current_base_changes(self):
        card = self.create()["card"]
        pending = self.seed_pending(card)
        self.revise(card, reminder="已经直接修订的新提醒。")
        row_before = self.row()
        result = self.store.review_candidate(owner_id=self.owner, model_id=self.model, wake_id="same-wake",
            wake_seq=1, expected_row_version=row_before, candidate_id=pending["candidate_id"],
            candidate_hash=pending["candidate_hash"], expected_base_version=1, decision="withdraw",
            reason="我选择退出旧候选。", catalog=None)
        self.assertEqual("withdraw", result["decision"])
        self.assertEqual(row_before + 1, result["tool_row_version"])
        self.assertFalse(result["active_version_changed"])
        self.assertEqual(1, self.counts()["tool_guidance_candidates"])
        self.assertEqual(2, self.counts()["tool_card_versions"])
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual("withdrawn", c.execute("SELECT status FROM tool_guidance_candidates").fetchone()[0])

    def test_withdraw_rejects_hash_base_and_cas_without_mutation(self):
        card = self.create()["card"]
        pending = self.seed_pending(card)
        args = dict(owner_id=self.owner, model_id=self.model, wake_id="withdraw", expected_row_version=self.row(),
            candidate_id=pending["candidate_id"], candidate_hash=pending["candidate_hash"],
            expected_base_version=1, reason="Synthetic withdrawal")
        for delta in ({"candidate_hash": "f" * 64}, {"expected_base_version": 2},
                      {"expected_row_version": 0}, {"owner_id": "other"}):
            before = self.counts()
            with self.assertRaises(ToolGuidanceError):
                self.store.withdraw_candidate(**(args | delta))
            self.assertEqual(before, self.counts())
        self.store.withdraw_candidate(**args)

    def test_expired_legacy_candidate_can_be_withdrawn_without_presentation(self):
        card = self.create()["card"]
        pending = self.seed_pending(card)
        future = datetime.now(timezone.utc) + timedelta(days=365)
        with patch("runtime.tool_guidance._now_dt", return_value=future):
            result = self.store.withdraw_candidate(owner_id=self.owner, model_id=self.model, wake_id="withdraw",
                expected_row_version=self.row(), candidate_id=pending["candidate_id"],
                candidate_hash=pending["candidate_hash"], expected_base_version=1, reason="退出过期候选。")
        self.assertEqual("withdraw", result["decision"])

    def test_new_chain_is_advice_and_requires_real_owned_refs(self):
        first = self.create()["card"]
        ref = f"tool-card://{first['card_id']}@1"
        second = self.create(tool_name="SecondService", linked_tool_refs=[ref], chain_role="entry",
                             handoff_condition="先查询细节。")
        self.assertEqual([ref], second["card"]["content"]["linked_tool_refs"])
        with self.assertRaisesRegex(ToolGuidanceError, "linked_tool_ref_not_found"):
            self.create(tool_name="MissingService", linked_tool_refs=["tool-card://toolcard_" + "0" * 32 + "@1"])

    def test_original_execution_boundary_still_requires_current_authorization(self):
        fields = action_card()
        fields.pop("reason")
        card = self.store.remember(owner_id=self.owner, model_id=self.model, wake_id="synthetic",
            expected_row_version=self.row(), reason="Synthetic", catalog=catalog(), **fields)["card"]
        self.assertTrue(self.recall(query="home.arrival", catalog=None)["envelopes"])
        for advertised, intent, auth, confirm in [(None, True, True, True),
                (catalog(home_schema="c" * 64), True, True, True), (catalog(), False, True, True),
                (catalog(), True, False, True), (catalog(), True, True, False)]:
            result = self.store.execution_gate(owner_id=self.owner, model_id=self.model, card_id=card["card_id"],
                catalog=advertised, current_user_intent=intent, authorization_verified=auth, current_confirmation=confirm)
            self.assertNotEqual("allowed_to_attempt", result["decision"])
            self.assertFalse(result["execution_performed"])
        before = self.counts()
        with self.assertRaisesRegex(ToolGuidanceError, "risk_below_runtime_floor"):
            self.revise(card, risk_level="low", confirmation_policy="none")
        self.assertEqual(before, self.counts())

    def test_secret_rejected_before_version_or_audit_write(self):
        card = self.create()["card"]
        before = self.counts()
        with self.assertRaisesRegex(ToolGuidanceError, "credential_or_secret_detected"):
            self.revise(card, reminder="Authorization: Bearer synthetic-secret-credential-value")
        self.assertEqual(before, self.counts())

    def test_directory_original_detail_and_missing_lookup_guidance(self):
        card = self.create(call_notes="tool: detailed_operation; optional parameter notes")["card"]
        directory = self.store.recall(owner_id=self.owner, model_id=self.model, view="directory")
        self.assertEqual(card["card_id"], directory["results"][0]["card_id"])
        self.assertNotIn("call_notes", json.dumps(directory))
        self.assertEqual(card["content"]["call_notes"], self.details(directory["results"][0]["ref"])["content"]["call_notes"])
        missing = self.store.recall(owner_id=self.owner, model_id=self.model, card_id="missing", view="card")
        self.assertEqual("directory", missing["lookup_guidance"]["view"])

    def test_local_phrases_and_irrelevant_query_and_limit(self):
        self.create()
        self.assertTrue(self.recall(query="我回家了，帮我想想接下来做什么")["envelopes"])
        self.assertFalse(self.recall(query="证明一个三角函数恒等式")["envelopes"])
        for index in range(3):
            self.create(tool_name=f"Service{index}")
        self.assertEqual(2, len(self.recall()["envelopes"]))
        self.assertEqual(1, len(self.recall(limit=1)["envelopes"]))
        with self.assertRaises(ToolGuidanceError):
            self.recall(limit=0)

    def test_card_and_minimal_envelope_validate_without_required_reminder(self):
        schema = json.loads((Path(__file__).parents[1] / "schemas/tool-guidance.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        saved = self.create(tool_name="家庭设备服务")
        jsonschema.validate(saved["card"]["content"], schema)
        old = self.create(tool_name="Legacy", reminder=None)
        self.assertNotIn("reminder", old["card"]["content"])
        jsonschema.validate(old["card"]["content"], schema)
        for envelope in self.recall()["envelopes"]:
            jsonschema.validate(envelope, {"$ref": "#/$defs/recallEnvelope", "$defs": schema["$defs"]})

    def test_versioned_ref_reads_the_exact_original_after_revision(self):
        card = self.create()["card"]
        ref = f"tool-card://{card['card_id']}@1"
        self.revise(card, reminder="第二版本的提醒。")
        self.assertEqual(2, self.details(card["card_id"])["version"])
        old = self.details(ref)
        self.assertEqual(1, old["version"])
        self.assertEqual(card["content_hash"], old["content_hash"])
        self.assertEqual(card["content"], old["content"])

    def test_latin_keyword_does_not_match_inside_an_unrelated_word(self):
        card = self.create(tool_name="FanService", purpose="fan", keywords=["fan"],
                           reminder="Check the fan.", aliases=[])["card"]
        self.assertLess(_scene_score("infant", card["content"]), 0.5)
        self.assertGreaterEqual(_scene_score("please check the fan", card["content"]), 0.95)

    def test_mixed_budget_and_permission_frame_stay_in_force(self):
        from runtime.mixed_recall import select_mixed_recall
        from runtime.tool_guidance import estimate_tokens
        self.create()
        self.create(tool_name="OtherService")
        envelopes = self.recall()["envelopes"]
        result = select_mixed_recall(base={}, planning=None, learning=[], tool=envelopes,
                                    emotional=None, estimate_tokens=estimate_tokens, budget=1200)
        payload = result["tool_guidance"]
        self.assertEqual("none", payload["frame"]["permission_authority"])
        self.assertEqual("none", payload["frame"]["execution_authority"])
        self.assertLessEqual(sum(item["token_cost"] for item in payload["envelopes"]), 240)
        self.assertNotIn("HomeMCP", json.dumps(payload))

    def test_direct_risk_reclassification_still_cannot_authorize_a_call(self):
        fields = action_card()
        fields.pop("reason")
        card = self.store.remember(owner_id=self.owner, model_id=self.model, wake_id="synthetic",
            expected_row_version=self.row(), reason="Synthetic", catalog=catalog(), **fields)["card"]
        changed = self.revise(card, capability_class="information_query", risk_level="low",
                              confirmation_policy="none")["card"]
        result = self.store.execution_gate(owner_id=self.owner, model_id=self.model, card_id=changed["card_id"],
            catalog=catalog(), current_user_intent=True, authorization_verified=False, current_confirmation=True)
        self.assertNotEqual("allowed_to_attempt", result["decision"])
        self.assertFalse(result["execution_performed"])


if __name__ == "__main__":
    unittest.main()
