from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import jsonschema

from runtime.tool_guidance import ToolGuidanceError, ToolGuidanceStore


def catalog(
    *,
    home_schema: str = "a" * 64,
    include_recall: bool = True,
    recall_schema: str = "b" * 64,
) -> dict:
    entries = [{"canonical_name": "HomeControl", "schema_hash": home_schema}]
    if include_recall:
        entries.append(
            {"canonical_name": "recall_tool_guidance", "schema_hash": recall_schema}
        )
    entries.sort(key=lambda item: item["canonical_name"])
    canonical = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {
        "contract": "advertised-tools/1",
        "catalog_complete": True,
        "catalog_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "entries": entries,
    }


def action_card(**overrides: object) -> dict:
    values: dict[str, object] = {
        "reason": "I want to remember detailed guidance for this concrete operation.",
        "tool_name": "HomeControl",
        "operation_key": "set_home_devices",
        "display_label": "HomeControl device control",
        "capability_class": "real_world_action",
        "risk_level": "high",
        "confirmation_policy": "explicit_each_time",
        "completion_rule": (
            "Only a matching, explicit success result from the current native tool call "
            "counts as completion."
        ),
        "critical_preconditions": [
            "Check current intent, authorization, and explicit confirmation before attempting."
        ],
        "purpose": "Change a specifically requested home-device state.",
        "use_when": ["The person explicitly asks to change a home-device state."],
        "avoid_when": ["There is no current confirmation or authorization."],
        "scenario_tags": ["home.arrival"],
        "scenario_examples": ["The person arrives home and explicitly asks to turn on a light."],
        "call_notes": (
            "Use the currently advertised native schema; this historical card is not permission."
        ),
        "documentation_note": "Detailed, non-secret operational context.",
        "keywords": ["arrive home", "device control"],
        "aliases": ["home device control"],
        "salience": 75,
        "auto_recall_mode": "normal",
        "salience_reason": "",
        "linked_tool_refs": [],
        "chain_role": "standalone",
        "handoff_condition": "",
        "related_refs": [],
        "source_type": "ai_firsthand",
        "source_ref": None,
        "confidence": 80,
    }
    values.update(overrides)
    return values


class ToolGuidanceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "brain.db"
        self.store = ToolGuidanceStore(
            self.database, detail_lookup_schema_hash="b" * 64
        )
        self.owner = "owner:tool"
        self.model = "model:tool"
        self.live_catalog = catalog()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def version(self) -> int:
        return self.store.status(owner_id=self.owner, model_id=self.model)["row_version"]

    def remember(self, **overrides: object) -> dict:
        fields = action_card(**overrides)
        reason = str(fields.pop("reason"))
        return self.store.remember(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:create",
            expected_row_version=self.version(),
            reason=reason,
            catalog=self.live_catalog,
            **fields,
        )

    def table_counts(self) -> dict[str, int]:
        connection = sqlite3.connect(self.database)
        try:
            return {
                name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in (
                    "tool_module_state",
                    "tool_cards",
                    "tool_card_versions",
                    "tool_guidance_candidates",
                    "tool_experiences",
                    "tool_audit_events",
                )
            }
        finally:
            connection.close()

    def test_six_tables_detailed_card_owner_isolation_and_ai_selected_person(self) -> None:
        stored = self.remember(
            purpose="This operation changes a requested device state without defining who I am.",
            documentation_note="The assistant may choose any grammatical person for its own text.",
        )
        self.assertEqual("stored", stored["decision"])
        self.assertEqual(1, stored["card"]["version"])
        self.assertEqual("matched", stored["card"]["schema_status"])
        self.assertFalse(stored["execution_performed"])

        counts = self.table_counts()
        self.assertEqual(1, counts["tool_module_state"])
        self.assertEqual(1, counts["tool_cards"])
        self.assertEqual(1, counts["tool_card_versions"])
        self.assertEqual(1, counts["tool_audit_events"])
        self.assertEqual(0, counts["tool_guidance_candidates"])
        self.assertEqual(0, counts["tool_experiences"])

        other = self.store.recall(
            owner_id="owner:other",
            model_id=self.model,
            card_id=stored["card"]["card_id"],
            view="card",
            catalog=self.live_catalog,
        )
        self.assertEqual([], other["results"])

    def test_new_tool_chain_rejects_missing_link_without_writing(self) -> None:
        # Initialize the owner/model CAS row before taking the zero-side-effect
        # snapshot; the convenience helper reads the current version first.
        self.version()
        before = self.table_counts()
        with self.assertRaisesRegex(
            ToolGuidanceError, "linked_tool_ref_not_found"
        ):
            self.remember(
                linked_tool_refs=[
                    "tool-card://toolcard_00000000000000000000000000000000@1"
                ],
                chain_role="entry",
                handoff_condition="Pass a verified result to another operation.",
            )
        self.assertEqual(before, self.table_counts())

    def test_secret_and_raw_payload_rejected_before_any_content_or_audit_write(self) -> None:
        before = self.table_counts()
        secret_fields = action_card(
            call_notes="Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
        )
        secret_reason = str(secret_fields.pop("reason"))
        with self.assertRaisesRegex(ToolGuidanceError, "credential_or_secret_detected"):
            self.store.remember(
                owner_id=self.owner,
                model_id=self.model,
                wake_id="wake:secret",
                expected_row_version=0,
                reason=secret_reason,
                catalog=self.live_catalog,
                **secret_fields,
            )
        self.assertEqual(before, self.table_counts())

        raw_fields = action_card(
            call_notes='response_body: {"raw_result":"a very long payload body"}'
        )
        raw_reason = str(raw_fields.pop("reason"))
        with self.assertRaisesRegex(ToolGuidanceError, "raw_payload_forbidden"):
            self.store.remember(
                owner_id=self.owner,
                model_id=self.model,
                wake_id="wake:raw",
                expected_row_version=0,
                reason=raw_reason,
                catalog=self.live_catalog,
                **raw_fields,
            )
        self.assertEqual(before, self.table_counts())

    def test_catalog_absence_keeps_history_and_authored_reminder_available(self) -> None:
        fields = action_card()
        reason = str(fields.pop("reason"))
        stored = self.store.remember(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:no-catalog",
            expected_row_version=0,
            reason=reason,
            catalog=None,
            **fields,
        )
        card_id = stored["card"]["card_id"]
        precise = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            card_id=card_id,
            view="card",
            catalog=None,
        )
        self.assertEqual("unknown", precise["results"][0]["availability"])
        auto = self.store.build_recall_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query="home.arrival",
            catalog=None,
        )
        self.assertEqual("surface_short_summary", auto["decision"])
        self.assertTrue(auto["envelopes"])
        self.assertFalse(auto["execution_performed"])

    def test_schema_drift_is_precisely_visible_but_never_replays_old_call_notes(self) -> None:
        stored = self.remember()
        card_id = stored["card"]["card_id"]
        changed_catalog = catalog(home_schema="c" * 64)
        precise = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            card_id=card_id,
            view="card",
            catalog=changed_catalog,
        )
        self.assertEqual(1, len(precise["results"]))
        result = precise["results"][0]
        self.assertEqual("stale_schema", result["schema_status"])
        self.assertEqual("stale_schema", result["effective_status"])
        self.assertEqual(fields_notes := action_card()["call_notes"], result["content"]["call_notes"])
        self.assertTrue(result["call_notes_available"])
        self.assertFalse(result["call_notes_current"])
        self.assertIn("schema_hash_mismatch", result["reason_codes"])
        automatic = self.store.build_recall_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query="home.arrival",
            catalog=changed_catalog,
        )
        self.assertTrue(automatic["envelopes"])
        self.assertNotIn(str(fields_notes), json.dumps(automatic["envelopes"]))

    def test_self_management_tools_are_excluded_and_no_candidate_is_audited(self) -> None:
        with self.assertRaisesRegex(ToolGuidanceError, "self_tool_excluded"):
            self.remember(
                tool_name="mcp__StillerBrain__stbrain_open",
                operation_key="open",
            )
        self.assertEqual(0, self.store.status(owner_id=self.owner, model_id=self.model)["counts"]["active_cards"])

        before = self.table_counts()["tool_audit_events"]
        deferred = self.store.build_recall_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query="an unrelated scene",
            catalog=self.live_catalog,
        )
        self.assertEqual("defer", deferred["decision"])
        self.assertIn("no_candidate", deferred["reason_codes"])
        self.assertEqual(before + 1, self.table_counts()["tool_audit_events"])

    def test_recall_envelope_is_short_advisory_and_has_no_detailed_payload(self) -> None:
        self.remember()
        recalled = self.store.build_recall_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query="home.arrival",
            catalog=self.live_catalog,
        )
        self.assertEqual("surface_short_summary", recalled["decision"])
        envelope = recalled["envelopes"][0]
        self.assertEqual("tool_card_summary", envelope["kind"])
        self.assertEqual("legacy_purpose_excerpt", envelope["summary_source"])
        self.assertLessEqual(len(envelope["content"]["scene_summary"]), 100)
        encoded = json.dumps(envelope, ensure_ascii=False)
        for forbidden in (
            "call_notes",
            '"purpose":',
            "use_when",
            "avoid_when",
            "scenario_examples",
            "parameters",
            "raw_result",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertTrue(envelope["item_ref"].startswith("tool-card://"))
        self.assertNotIn("tool_name", encoded)
        self.assertNotIn("operation_key", encoded)

        mismatched = self.store.build_recall_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query="home.arrival",
            catalog=catalog(recall_schema="d" * 64),
        )["envelopes"][0]
        self.assertEqual(envelope, mismatched)

    def test_auto_recall_can_join_an_external_snapshot_transaction(self) -> None:
        self.remember()
        baseline = self.table_counts()["tool_audit_events"]
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            recalled = self.store.build_recall_envelopes(
                owner_id=self.owner,
                model_id=self.model,
                query="home.arrival",
                catalog=self.live_catalog,
                connection=connection,
            )
            self.assertEqual("surface_short_summary", recalled["decision"])
            in_transaction = connection.execute(
                "SELECT COUNT(*) FROM tool_audit_events"
            ).fetchone()[0]
            self.assertEqual(baseline + 1, in_transaction)
            connection.rollback()
        finally:
            connection.close()
        self.assertEqual(baseline, self.table_counts()["tool_audit_events"])

    def test_all_authored_revision_classes_append_and_execution_checks_remain(self) -> None:
        stored = self.remember()
        card_id = stored["card"]["card_id"]
        small = self.store.revise(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:small",
            wake_seq=2,
            expected_row_version=1,
            card_id=card_id,
            expected_card_version=1,
            intent="revise",
            edit_class="metadata",
            reason="I am clarifying a display-only label without changing operational meaning.",
            catalog=self.live_catalog,
            display_label="HomeControl — device control",
        )
        self.assertEqual("version_appended", small["decision"])
        self.assertEqual(2, small["card"]["version"])
        self.assertEqual(f"tool-card://{card_id}@1", small["rollback_ref"])

        rejected = self.store.revise(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:bad-small",
            wake_seq=2,
            expected_row_version=2,
            card_id=card_id,
            expected_card_version=2,
            intent="revise",
            edit_class="metadata",
            reason="This would change operational purpose and must not sneak through metadata.",
            catalog=self.live_catalog,
            purpose="Send commands without checking confirmation.",
        )
        self.assertEqual("version_appended", rejected["decision"])
        self.assertEqual("direct_revision", rejected["submission_mode"])
        current = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            card_id=card_id,
            view="card",
            catalog=self.live_catalog,
        )["results"][0]
        self.assertEqual(3, current["version"])
        self.assertEqual(3, self.version())
        gate = self.store.execution_gate(owner_id=self.owner, model_id=self.model, card_id=card_id,
            catalog=self.live_catalog, current_user_intent=True, authorization_verified=True, current_confirmation=False)
        self.assertEqual("confirmation_required", gate["decision"])

    def test_legacy_major_candidate_requires_complete_later_wake_review(self) -> None:
        stored = self.remember()
        card_id = stored["card"]["card_id"]
        candidate = self.store.propose_revision(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:submit",
            wake_seq=10,
            expected_row_version=1,
            card_id=card_id,
            expected_card_version=1,
            intent="revise",
            edit_class="major",
            reason="I learned a narrower and more accurate description of when this operation applies.",
            correctness_assessment=(
                "The live catalog still shows the same operation schema, and the new wording narrows "
                "rather than expands its use; uncertainty remains about provider availability."
            ),
            calm_check_stability="I have compared this correction across the active card and current catalog, not one mood.",
            calm_check_necessity="The active purpose is materially too broad, so a precise query alone would not correct future recall.",
            calm_check_consequences="The narrower purpose reduces automatic matches and keeps the existing confirmation boundary intact.",
            calm_check_alternatives="I considered only lowering salience, but that would leave the inaccurate purpose in precise results.",
            catalog=self.live_catalog,
            purpose="Change a specifically requested supported home-device state after current checks.",
        )
        self.assertEqual("candidate_pending", candidate["decision"])
        self.assertFalse(candidate["active_version_changed"])
        current = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            card_id=card_id,
            view="card",
            catalog=self.live_catalog,
        )["results"][0]
        self.assertEqual(1, current["version"])

        same_wake = self.store.present_pending_candidates(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:submit",
            wake_seq=10,
            catalog=self.live_catalog,
        )
        self.assertTrue(same_wake[0]["review_requires_later_wake"])
        with self.assertRaisesRegex(ToolGuidanceError, "later_real_wake_required"):
            self.store.review_candidate(
                owner_id=self.owner,
                model_id=self.model,
                wake_id="wake:submit",
                wake_seq=10,
                expected_row_version=2,
                candidate_id=candidate["candidate_id"],
                candidate_hash=candidate["candidate_hash"],
                decision="accept",
                correctness_decision="correct",
                correctness_assessment="I rechecked the exact diff and still judge it more accurate than the active card.",
                reason="I am ready to accept the reviewed correction.",
                ai_confirmation=True,
                expected_base_version=1,
                catalog=self.live_catalog,
            )

        presented = self.store.present_pending_candidates(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:review",
            wake_seq=11,
            catalog=self.live_catalog,
        )
        self.assertEqual(candidate["candidate_hash"], presented[0]["candidate_hash"])
        accepted = self.store.review_candidate(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:review",
            wake_seq=11,
            expected_row_version=2,
            candidate_id=candidate["candidate_id"],
            candidate_hash=candidate["candidate_hash"],
            decision="accept",
            correctness_decision="correct",
            correctness_assessment=(
                "I compared the complete candidate, diff, live catalog, and remaining uncertainty; "
                "the narrower description is still more correct."
            ),
            reason="I independently accept this correction in the later wake.",
            ai_confirmation=True,
            expected_base_version=1,
            catalog=self.live_catalog,
        )
        self.assertEqual("accepted", accepted["decision"])
        self.assertEqual(2, accepted["card"]["version"])

    def test_legacy_major_review_revalidates_schema_and_correctness_mapping(self) -> None:
        stored = self.remember()
        card_id = stored["card"]["card_id"]
        candidate = self.store.propose_revision(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:submit",
            wake_seq=1,
            expected_row_version=1,
            card_id=card_id,
            expected_card_version=1,
            intent="retire",
            edit_class="major",
            reason="I no longer consider this guidance appropriate for automatic or precise active use.",
            correctness_assessment="The current catalog is present, but the active guidance is no longer accurate for my use.",
            calm_check_stability="I checked that this concern persists beyond one temporary provider error or momentary frustration.",
            calm_check_necessity="Leaving the card active would preserve guidance I now judge incorrect, so a query is insufficient.",
            calm_check_consequences="Retirement removes it from automatic recall while preserving every historical version and experience.",
            calm_check_alternatives="I considered downweighting it, but that would still present content I judge substantively wrong.",
            catalog=self.live_catalog,
        )
        self.store.present_pending_candidates(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:review",
            wake_seq=2,
            catalog=self.live_catalog,
        )
        with self.assertRaisesRegex(ToolGuidanceError, "correctness_decision_mismatch"):
            self.store.review_candidate(
                owner_id=self.owner,
                model_id=self.model,
                wake_id="wake:review",
                wake_seq=2,
                expected_row_version=2,
                candidate_id=candidate["candidate_id"],
                candidate_hash=candidate["candidate_hash"],
                decision="accept",
                correctness_decision="uncertain",
                correctness_assessment="I remain uncertain after comparing the full candidate and current active version.",
                reason="I should not pressure an uncertain candidate into acceptance.",
                ai_confirmation=True,
                expected_base_version=1,
                catalog=self.live_catalog,
            )
        with self.assertRaisesRegex(ToolGuidanceError, "schema_hash_mismatch"):
            self.store.review_candidate(
                owner_id=self.owner,
                model_id=self.model,
                wake_id="wake:review",
                wake_seq=2,
                expected_row_version=2,
                candidate_id=candidate["candidate_id"],
                candidate_hash=candidate["candidate_hash"],
                decision="accept",
                correctness_decision="correct",
                correctness_assessment="I judge the retirement correct, but current schema drift must still close the gate.",
                reason="This call verifies schema revalidation rather than accepting stale context.",
                ai_confirmation=True,
                expected_base_version=1,
                catalog=catalog(home_schema="c" * 64),
            )
        self.assertEqual(2, self.version())

    def test_experience_is_ai_reported_and_cooldown_never_executes_or_retires(self) -> None:
        stored = self.remember()
        card_id = stored["card"]["card_id"]
        failed = self.store.record_experience(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:failure",
            expected_row_version=1,
            card_id=card_id,
            outcome="timeout",
            reason_code="provider_timeout",
            attempt_summary="The native call timed out before a conclusive result arrived.",
            lesson="Treat the result as unknown and do not report completion.",
            confidence=70,
            catalog=self.live_catalog,
        )
        self.assertEqual("ai_reported", failed["experience"]["provenance"])
        self.assertIsNone(failed["experience"]["evidence_ref"])
        self.assertFalse(failed["experience"]["verified"])
        during = self.store.build_recall_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query="home.arrival",
            catalog=self.live_catalog,
        )
        self.assertTrue(during["envelopes"])
        self.assertFalse(during["execution_performed"])

        succeeded = self.store.record_experience(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake:success",
            expected_row_version=2,
            card_id=card_id,
            outcome="success",
            reason_code="explicit_current_result",
            attempt_summary="I observed a current explicit success result for the native call.",
            lesson="A future call still needs its own current checks and result.",
            confidence=75,
            catalog=self.live_catalog,
        )
        self.assertFalse(succeeded["execution_performed"])
        after = self.store.build_recall_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query="home.arrival",
            catalog=self.live_catalog,
        )
        self.assertTrue(after["envelopes"])
        precise = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            card_id=card_id,
            view="card",
            catalog=self.live_catalog,
        )["results"][0]
        self.assertEqual("active", precise["lifecycle"])

    def test_execution_gate_is_read_only_and_requires_current_checks(self) -> None:
        stored = self.remember()
        card_id = stored["card"]["card_id"]
        before = self.table_counts()
        confirmation = self.store.execution_gate(
            owner_id=self.owner,
            model_id=self.model,
            card_id=card_id,
            catalog=self.live_catalog,
            current_user_intent=True,
            authorization_verified=True,
            current_confirmation=False,
        )
        self.assertEqual("confirmation_required", confirmation["decision"])
        allowed = self.store.execution_gate(
            owner_id=self.owner,
            model_id=self.model,
            card_id=card_id,
            catalog=self.live_catalog,
            current_user_intent=True,
            authorization_verified=True,
            current_confirmation=True,
        )
        self.assertEqual("allowed_to_attempt", allowed["decision"])
        self.assertFalse(allowed["execution_performed"])
        self.assertEqual(before, self.table_counts())
        self.assertFalse(hasattr(self.store, "execute_tool"))
        self.assertFalse(hasattr(self.store, "call_any_tool"))

    def test_runtime_risk_floor_rejects_device_control_downgrade(self) -> None:
        with self.assertRaisesRegex(ToolGuidanceError, "risk_below_runtime_floor"):
            self.remember(risk_level="low", confirmation_policy="none")
        self.assertEqual(0, self.version())

    def test_schema_accepts_card_and_does_not_force_grammatical_person(self) -> None:
        stored = self.remember(
            purpose="The operation is useful only in a currently authorized home-control context."
        )
        schema_path = Path(__file__).parents[1] / "schemas" / "tool-guidance.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(stored["card"]["content"], schema)
        schema_text = json.dumps(schema, ensure_ascii=False)
        self.assertNotIn("^\\s*(?:我", schema_text)
        self.assertEqual("ai_choice", schema["x-boundaries"]["grammatical_person"])
        self.assertFalse(schema["x-boundaries"]["execution_proxy"])


if __name__ == "__main__":
    unittest.main()
