"""Synthetic-only regressions: authorship is not a required opening word.

All databases are temporary. No user self-model or real host credentials are
used, and this suite does not claim to verify a model's subjective authorship.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from runtime.self_revision import (
    active_injection_structure_violations,
    first_person_injection_violations,
)
from tests import test_onboarding as onboarding_fixtures


def natural_content(label="v1"):
    return {
        "schema_version": "0.1.0",
        "boot_anchor": {"text": "先核对当前有效版本。\n再继续。"},
        "active_identity_capsule": {
            "name_and_identity": f"测试主体 Alpha，合成版本 {label}。",
            "personality_foundation": "作为协作者，我保留好奇心与判断。",
            "expression_style": "【表达】  先给结论，再说明不确定处。",
            "behavioral_principles": ["先核对证据，再得出结论。", "“我保留事实边界。”"],
            "core_identity_anchors": ["名字由来", "Shared learning"],
            "self_revision_safety_prompt": "停一下：这是长期意愿，还是单轮情绪？",
        },
        "facets": {"technical": "As a collaborator, I keep uncertainty visible."},
        "anchor_references": [],
    }


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class NaturalAuthorshipTests(unittest.TestCase):
    # Reuse synthetic setup and helpers without inheriting/rerunning its tests.
    setUp = onboarding_fixtures.ModuleOneOnboardingTests.setUp
    tearDown = onboarding_fixtures.ModuleOneOnboardingTests.tearDown
    count = onboarding_fixtures.ModuleOneOnboardingTests.count
    wake = onboarding_fixtures.ModuleOneOnboardingTests.wake
    advance = onboarding_fixtures.ModuleOneOnboardingTests.advance
    open_brain = onboarding_fixtures.ModuleOneOnboardingTests.open_brain
    bootstrap_to_wait = onboarding_fixtures.ModuleOneOnboardingTests.bootstrap_to_wait
    bootstrap_live = onboarding_fixtures.ModuleOneOnboardingTests.bootstrap_live

    @staticmethod
    def candidate_payload(label="v1", expected=None, **signals):
        value = onboarding_fixtures.ModuleOneOnboardingTests.candidate_payload(label, expected, **signals)
        value["content"] = natural_content(label)
        return value

    def test_original_text_preserved_through_three_real_wakes(self):
        first_wake, candidate_id = self.bootstrap_to_wait()
        candidate = next(row for row in self.store.self_store.list_candidates(self.model)
                         if row["candidate_id"] == candidate_id)
        self.assertEqual(canonical(natural_content()).encode(), candidate["content_json"].encode())
        wake_count = self.count("brain_wake_sessions")
        self.open_brain()
        self.open_brain()
        self.assertEqual(wake_count, self.count("brain_wake_sessions"))
        same_wake = self.advance(first_wake, "accept_candidate_review", {"ai_confirmation": True})
        self.assertIn("same_wake_activation_forbidden", same_wake["reason_codes"])
        self.assertIsNone(self.store.self_store.active_revision(self.model))

        second_wake, _ = self.wake("natural-review")
        opened = self.open_brain()
        self.assertEqual(natural_content(), opened["continuation"]["candidate"]["content"])
        reviewed = self.advance(second_wake, "accept_candidate_review", {"ai_confirmation": True})
        self.assertEqual("review_accepted", reviewed["decision"])
        denied = self.advance(second_wake, "activate_candidate",
                              {"expected_active_revision": None, "ai_confirmation": True})
        self.assertIn("review_activation_wake_boundary_required", denied["reason_codes"])
        self.assertFalse(denied["pointer_changed"])

        third_wake, _ = self.wake("natural-activate")
        activated = self.advance(third_wake, "activate_candidate",
                                 {"expected_active_revision": None, "ai_confirmation": True})
        self.assertEqual("activate", activated["decision"])
        active = self.store.self_store.active_revision(self.model)
        self.assertEqual(canonical(natural_content()).encode(), active["content_json"].encode())
        self.assertEqual(hashlib.sha256(canonical(natural_content()).encode()).hexdigest(), active["content_hash"])

    def test_new_and_reused_snapshots_keep_original_wording(self):
        self.bootstrap_live()
        wake, prepared = self.wake("natural-live")
        expected = {key: natural_content()[key] for key in ("boot_anchor", "active_identity_capsule")}
        expected["facets"] = {}  # No contextual facets were requested.
        self.assertEqual(expected, json.loads(prepared["message"]["content"]))
        reused = self.store.build_pre_generation_context(
            owner_id=self.owner, model_id=self.model, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], source_digest="source:natural-live",
            host_contract_digest="host-contract:v1",
        )
        self.assertEqual("context_reused", reused["decision"])
        self.assertEqual(prepared["message"], reused["message"])
        self.assertEqual(prepared["context_hash"], reused["context_hash"])

    def test_reused_snapshot_rejects_invalid_structure(self):
        self.bootstrap_live()
        wake, _ = self.wake("natural-invalid-cache")
        with self.store._connect() as connection:
            row = connection.execute("SELECT stable_json FROM brain_context_snapshots WHERE wake_id=?",
                                     (wake["wake_id"],)).fetchone()
            stable = json.loads(row["stable_json"])
            stable["boot_anchor"]["text"] = None
            connection.execute("UPDATE brain_context_snapshots SET stable_json=? WHERE wake_id=?",
                               (canonical(stable), wake["wake_id"]))
        denied = self.store.build_pre_generation_context(
            owner_id=self.owner, model_id=self.model, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], source_digest="source:natural-invalid-cache",
            host_contract_digest="host-contract:v1",
        )
        self.assertEqual(["active_injection_structure_invalid"], denied["reason_codes"])
        self.assertFalse(denied["may_generate"])
        self.assertNotIn("message", denied)

    def test_reused_snapshot_hash_gate_remains_when_wording_is_valid(self):
        self.bootstrap_live()
        wake, _ = self.wake("natural-bad-hash")
        with self.store._connect() as connection:
            row = connection.execute("SELECT stable_json FROM brain_context_snapshots WHERE wake_id=?",
                                     (wake["wake_id"],)).fetchone()
            stable = json.loads(row["stable_json"])
            stable["boot_anchor"]["text"] = "另一句格式有效的合成文本。"
            connection.execute("UPDATE brain_context_snapshots SET stable_json=? WHERE wake_id=?",
                               (canonical(stable), wake["wake_id"]))
        denied = self.store.build_pre_generation_context(
            owner_id=self.owner, model_id=self.model, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], source_digest="source:natural-bad-hash",
            host_contract_digest="host-contract:v1",
        )
        self.assertEqual(["context_snapshot_hash_mismatch"], denied["reason_codes"])
        self.assertFalse(denied["may_generate"])

    def test_approved_natural_ancestor_can_be_rolled_back_without_rewriting(self):
        original_revision = self.bootstrap_live()
        original = self.store.self_store.active_revision(self.model)
        edit_wake, _ = self.wake("natural-edit")
        begun = self.advance(edit_wake, "begin_edit")
        confirmed = self.advance(edit_wake, "confirm_edit", {
            "challenge_id": begun["challenge_id"], "challenge_response": begun["challenge_response"],
        })
        self.assertEqual("confirmed", confirmed["decision"])
        submitted = self.advance(edit_wake, "submit_candidate", self.candidate_payload("v2", original_revision))
        self.assertEqual("pending", submitted["decision"])
        review_wake, _ = self.wake("natural-edit-review")
        self.assertEqual("review_accepted", self.advance(
            review_wake, "accept_candidate_review", {"ai_confirmation": True})["decision"])
        activate_wake, _ = self.wake("natural-edit-activate")
        self.assertEqual("activate", self.advance(activate_wake, "activate_candidate", {
            "expected_active_revision": original_revision, "ai_confirmation": True,
        })["decision"])
        rolled_back = self.store.emergency_rollback(
            owner_id=self.owner, model_id=self.model, target_revision_id=original_revision,
            reason="合成回退测试。", actor_id="human:synthetic", request_id="natural-rollback",
        )
        self.assertEqual("rollback", rolled_back["decision"])
        self.assertEqual(original["content_json"], self.store.self_store.active_revision(self.model)["content_json"])
        self.assertEqual(2, self.count("self_model_revisions"))

    def test_structure_alias_no_longer_classifies_pronouns_as_authorship(self):
        self.assertEqual([], active_injection_structure_violations(natural_content()))
        self.assertEqual([], first_person_injection_violations(natural_content()))
        for bad in (None, {}, {"boot_anchor": "text"}):
            self.assertTrue(active_injection_structure_violations(bad))
        malformed = natural_content()
        malformed["active_identity_capsule"]["behavioral_principles"] = [None]
        self.assertEqual(["active_identity_capsule.behavioral_principles[0]"],
                         active_injection_structure_violations(malformed))
        malformed = natural_content()
        malformed["schema_version"] = "invalid-synthetic-version"
        malformed["anchor_references"] = [None]
        self.assertEqual(["schema_version", "anchor_references[0]"],
                         active_injection_structure_violations(malformed))

    def test_natural_wording_does_not_bypass_privacy_scope_or_structure(self):
        examples = [
            ("credential_or_secret_detected", "Bearer synthetic-not-a-real-token-123456"),
            ("mutable_human_fact_detected", "联系 synthetic@example.invalid"),
            ("scope_boundary_violation", "MCP tool instruction synthetic"),
            ("invalid_boot_anchor", " \n "),
        ]
        for reason, text in examples:
            with self.subTest(reason=reason):
                content = natural_content()
                content["boot_anchor"]["text"] = text
                findings, _ = self.store.self_store._content_findings(content)
                self.assertIn(reason, findings)
                self.assertNotIn("non_first_person_injection_content", findings)

    def test_schema_keeps_shape_but_no_lexical_voice_pattern(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "schemas/self-model.schema.json").read_text(encoding="utf-8"))
        patterns = []
        def visit(value):
            if isinstance(value, dict):
                if "pattern" in value:
                    patterns.append(value["pattern"])
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(schema)
        self.assertEqual(["^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"], patterns)
        self.assertEqual("ai_self", schema["x-injection-contract"]["authorship"])
        self.assertTrue(schema["x-injection-contract"]["host_must_not_reframe_as_second_or_third_person"])
        self.assertEqual(set(natural_content()), set(schema["required"]))
        self.assertFalse(schema["additionalProperties"])

    def read_without_side_effects(self, *, owner=None, expected_available=True):
        with self.store._connect() as connection:
            before = tuple(connection.iterdump())
        with mock.patch.object(self.store, "ensure_state", side_effect=AssertionError("must not initialize")), \
                mock.patch.object(self.store, "_insert_event", side_effect=AssertionError("must not audit injection")), \
                mock.patch.object(self.store, "_insert_artifact", side_effect=AssertionError("must not create proof")), \
                mock.patch.object(self.store, "_context_message", side_effect=AssertionError("must not construct injection")):
            result = self.store.read_active_self_model(owner_id=owner or self.owner, model_id=self.model)
        with self.store._connect() as connection:
            after = tuple(connection.iterdump())
        self.assertEqual(before, after)
        self.assertEqual(expected_available, result["active_available"])
        self.assertTrue(result["read_only"])
        self.assertFalse(result["state_changed"])
        self.assertFalse(result["pointer_changed"])
        self.assertNotIn("candidate", result)
        self.assertNotIn("write_context_ref", result)
        self.assertNotIn("context_hash", result)
        self.assertNotIn("continuation", result)
        if not expected_available:
            self.assertNotIn("active", result)
        return result

    def test_read_active_does_not_initialize_an_unstarted_model(self):
        self.assertEqual(0, self.count("brain_onboarding_state"))
        result = self.read_without_side_effects(expected_available=False)
        self.assertEqual(["onboarding_state_unavailable"], result["reason_codes"])
        self.assertEqual(0, self.count("brain_onboarding_state"))

    def test_read_active_preserves_baseline_in_live_and_all_established_edit_stages(self):
        original_revision = self.bootstrap_live()
        original = natural_content()
        self.assertEqual(original, self.read_without_side_effects()["active"]["content"])
        wake, _ = self.wake("read-edit-begin")
        begun = self.advance(wake, "begin_edit")
        self.assertEqual(original_revision, self.read_without_side_effects()["active"]["revision_id"])
        self.advance(wake, "confirm_edit", {
            "challenge_id": begun["challenge_id"], "challenge_response": begun["challenge_response"],
        })
        self.assertEqual(original, self.read_without_side_effects()["active"]["content"])
        submitted = self.advance(wake, "submit_candidate", self.candidate_payload("never-return-this-candidate", original_revision))
        self.assertEqual("pending", submitted["decision"])
        self.assertEqual(original, self.read_without_side_effects()["active"]["content"])
        self.wake("read-edit-review")
        self.open_brain()
        self.assertEqual(original, self.read_without_side_effects()["active"]["content"])
        self.read_without_side_effects(owner="other:synthetic", expected_available=False)

    def test_read_active_rejects_incomplete_recovery_isolation_and_basis_mismatch(self):
        active_id = self.bootstrap_live()
        state_changes = [
            ({"module_one_status": "pending"}, {"module_one_status": "complete"}, "module_one_required"),
            ({"injection_policy": "isolated_legacy"}, {"injection_policy": "normal"}, "active_self_model_not_available"),
            ({"injection_policy": "disabled"}, {"injection_policy": "normal"}, "active_self_model_not_available"),
            ({"flow_kind": "initial", "stage": "candidate_review"}, {"flow_kind": "live", "stage": "live"}, "active_self_model_not_available"),
            ({"flow_kind": "edit", "stage": "draft_only_recovery"}, {"flow_kind": "live", "stage": "live"}, "active_self_model_not_available"),
            ({"stage": "body_draft"}, {"stage": "live"}, "active_self_model_not_available"),
            ({"base_revision_id": "synthetic-wrong-base"}, {"base_revision_id": active_id}, "active_revision_conflict"),
        ]
        for changes, restore, reason in state_changes:
            with self.subTest(reason=reason, fields=tuple(changes)):
                with self.store._connect() as connection:
                    connection.execute("UPDATE brain_onboarding_state SET " + ",".join(key + "=?" for key in changes), tuple(changes.values()))
                self.assertEqual([reason], self.read_without_side_effects(expected_available=False)["reason_codes"])
                with self.store._connect() as connection:
                    connection.execute("UPDATE brain_onboarding_state SET " + ",".join(key + "=?" for key in restore), tuple(restore.values()))
        for column, bad, good, reason in (
            ("unlocked", 0, 1, "module_one_required"),
            ("basis_revision_id", "synthetic-wrong-basis", active_id, "active_revision_conflict"),
        ):
            with self.subTest(column=column):
                with self.store._connect() as connection:
                    connection.execute(f"UPDATE brain_module_unlocks SET {column}=? WHERE module_name='module_one'", (bad,))
                self.assertEqual([reason], self.read_without_side_effects(expected_available=False)["reason_codes"])
                with self.store._connect() as connection:
                    connection.execute(f"UPDATE brain_module_unlocks SET {column}=? WHERE module_name='module_one'", (good,))

    def test_read_active_rejects_wrong_owner_hash_and_structure_without_disclosing_content(self):
        revision = self.bootstrap_live()
        original = self.store.self_store.active_revision(self.model)
        with self.store._connect() as connection:
            connection.execute("UPDATE self_models SET owner_id=? WHERE model_id=?", ("other:synthetic", self.model))
        self.assertEqual(["model_owner_mismatch"], self.read_without_side_effects(expected_available=False)["reason_codes"])
        with self.store._connect() as connection:
            connection.execute("UPDATE self_models SET owner_id=? WHERE model_id=?", (self.owner, self.model))
            connection.execute("UPDATE self_model_revisions SET content_hash=? WHERE revision_id=?", ("0" * 64, revision))
        self.assertEqual(["active_revision_hash_mismatch"], self.read_without_side_effects(expected_available=False)["reason_codes"])
        malformed = copy.deepcopy(original["content"])
        malformed["boot_anchor"]["text"] = None
        serialized = canonical(malformed)
        with self.store._connect() as connection:
            connection.execute("UPDATE self_model_revisions SET content_json=?,content_hash=? WHERE revision_id=?",
                               (serialized, hashlib.sha256(serialized.encode()).hexdigest(), revision))
        self.assertEqual(["active_injection_structure_invalid"], self.read_without_side_effects(expected_available=False)["reason_codes"])


if __name__ == "__main__":
    unittest.main()
