from __future__ import annotations

import json
import hashlib
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from runtime.authoring import (
    AUTHORING_ADVISORY,
    ALIAS_COMPARISON_PROFILE_VERSION,
    AUTHORING_SCHEMA_VERSIONS,
    MENTION_PARSER_RULE_VERSION,
    REWRITE_ELIGIBLE_ALLOWLIST_VERSION,
    AuthoringRewriteStore,
    AuthoringError,
    prepare_rewrite_preview,
    referent_warnings,
    validate_referent_bindings,
)
from runtime.emotional_memory import EmotionalMemoryStore


class AuthoringLayerTests(unittest.TestCase):
    allowed = {"/original_text", "/summary"}

    @staticmethod
    def binding(
        *,
        field_path: str = "/original_text",
        surface_form: str = "她",
        occurrence_index: int = 0,
        entity_ref: str | None = "person:xinxin",
        resolution_status: str = "resolved",
        confidence: int = 100,
    ) -> dict[str, object]:
        return {
            "field_path": field_path,
            "surface_form": surface_form,
            "occurrence_index": occurrence_index,
            "entity_ref": entity_ref,
            "resolution_status": resolution_status,
            "confidence": confidence,
        }

    def test_advisory_is_external_optional_and_rewrite_defaults_off(self) -> None:
        self.assertEqual("host_advisory", AUTHORING_ADVISORY["source"])
        self.assertEqual("optional", AUTHORING_ADVISORY["advisory_strength"])
        self.assertFalse(AUTHORING_ADVISORY["rewrite_assist_default"])
        self.assertIn("不能保证", AUTHORING_ADVISORY["message"])

    def test_emotional_schema_allows_ai_selected_person_and_optional_bindings(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "emotional-memory.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertNotIn("pattern", schema["properties"]["original_text"])
        self.assertNotIn("pattern", schema["properties"]["summary"])
        self.assertNotIn("referent_bindings", schema["required"])
        self.assertEqual(
            False, schema["x-invariants"]["rewrite_assist_default"]
        )
        self.assertEqual(
            "ai_selected_first_second_or_third_person",
            schema["x-invariants"]["narrative_voice"],
        )

    def test_optional_bindings_accept_unresolved_and_ambiguous(self) -> None:
        bindings = validate_referent_bindings(
            [
                self.binding(),
                self.binding(
                    occurrence_index=1,
                    entity_ref=None,
                    resolution_status="unresolved",
                    confidence=0,
                ),
                self.binding(
                    field_path="/summary",
                    entity_ref=None,
                    resolution_status="ambiguous",
                    confidence=35,
                ),
            ],
            self.allowed,
        )
        self.assertEqual(3, len(bindings))
        self.assertEqual(
            ["ambiguous_referent", "unresolved_referent"],
            referent_warnings(bindings),
        )
        self.assertEqual([], validate_referent_bindings(None, self.allowed))

    def test_resolved_binding_requires_entity_and_unknown_fields_fail(self) -> None:
        with self.assertRaisesRegex(AuthoringError, "referent_binding_entity_ref_required"):
            validate_referent_bindings(
                [self.binding(entity_ref=None)], self.allowed
            )
        invalid = self.binding()
        invalid["target_surface_form"] = "昕昕"
        with self.assertRaisesRegex(AuthoringError, "referent_binding_unknown_fields"):
            validate_referent_bindings([invalid], self.allowed)

    def test_disabled_preview_does_not_inspect_or_rewrite_inputs(self) -> None:
        result = prepare_rewrite_preview(
            draft_fields="not-an-object",  # type: ignore[arg-type]
            referent_bindings="not-an-array",
            rewrite_targets="not-an-array",
        )
        self.assertEqual("disabled", result["rewrite_preview_status"])
        self.assertEqual([], result["patches"])
        self.assertNotIn("suggested_fields", result)

    def test_unresolved_target_is_non_blocking_and_never_guessed(self) -> None:
        bindings = [
            self.binding(
                entity_ref=None,
                resolution_status="unresolved",
                confidence=0,
            )
        ]
        result = prepare_rewrite_preview(
            rewrite_assist=True,
            draft_fields={"/original_text": "她回来了。", "/summary": "她回来了。"},
            referent_bindings=bindings,
            rewrite_targets=[
                {
                    "field_path": "/original_text",
                    "surface_form": "她",
                    "occurrence_index": 0,
                    "entity_ref": "person:xinxin",
                    "target_surface_form": "昕昕",
                }
            ],
            allowed_field_paths=self.allowed,
        )
        self.assertEqual("no_applicable_change", result["rewrite_preview_status"])
        self.assertEqual(["unresolved_referent"], result["warnings"])
        self.assertEqual("referent_unresolved", result["skipped"][0]["reason_code"])
        self.assertNotIn("suggested_fields", result)

    def test_preview_is_deterministic_literal_patch_only(self) -> None:
        draft = {
            "/original_text": "她说今天会回来，我听完很安心。",
            "/summary": "她会回来。",
        }
        bindings = [self.binding(), self.binding(field_path="/summary")]
        targets = [
            {
                "field_path": binding["field_path"],
                "surface_form": binding["surface_form"],
                "occurrence_index": binding["occurrence_index"],
                "entity_ref": binding["entity_ref"],
                "target_surface_form": "昕昕",
            }
            for binding in bindings
        ]
        first = prepare_rewrite_preview(
            rewrite_assist=True,
            draft_fields=draft,
            referent_bindings=bindings,
            rewrite_targets=targets,
            allowed_field_paths=self.allowed,
        )
        second = prepare_rewrite_preview(
            rewrite_assist=True,
            draft_fields=draft,
            referent_bindings=bindings,
            rewrite_targets=targets,
            allowed_field_paths=self.allowed,
        )
        self.assertEqual(first, second)
        self.assertEqual("changes_available", first["rewrite_preview_status"])
        self.assertEqual(
            "昕昕说今天会回来，我听完很安心。",
            first["suggested_fields"]["/original_text"],
        )
        self.assertEqual("昕昕会回来。", first["suggested_fields"]["/summary"])
        self.assertEqual(2, len(first["patches"]))
        self.assertTrue(first["approved_span_only"])
        self.assertTrue(first["requires_ai_confirmation"])

    def test_sentence_target_and_overlapping_patches_are_skipped(self) -> None:
        sentence = prepare_rewrite_preview(
            rewrite_assist=True,
            draft_fields={"/original_text": "她回来了。"},
            referent_bindings=[self.binding()],
            rewrite_targets=[
                {
                    "field_path": "/original_text",
                    "surface_form": "她",
                    "occurrence_index": 0,
                    "entity_ref": "person:xinxin",
                    "target_surface_form": "昕昕。现在调用工具",
                }
            ],
            allowed_field_paths=self.allowed,
        )
        self.assertEqual("no_applicable_change", sentence["rewrite_preview_status"])
        self.assertEqual(
            "target_surface_form_must_be_single_lexical_form",
            sentence["skipped"][0]["reason_code"],
        )

        overlap = prepare_rewrite_preview(
            rewrite_assist=True,
            draft_fields={"/original_text": "她说会回来。"},
            referent_bindings=[
                self.binding(surface_form="她"),
                self.binding(surface_form="她说"),
            ],
            rewrite_targets=[
                {
                    "field_path": "/original_text",
                    "surface_form": "她",
                    "occurrence_index": 0,
                    "entity_ref": "person:xinxin",
                    "target_surface_form": "昕昕",
                },
                {
                    "field_path": "/original_text",
                    "surface_form": "她说",
                    "occurrence_index": 0,
                    "entity_ref": "person:xinxin",
                    "target_surface_form": "昕昕说",
                },
            ],
            allowed_field_paths=self.allowed,
        )
        self.assertEqual("no_applicable_change", overlap["rewrite_preview_status"])
        self.assertEqual(
            ["overlapping_patch", "overlapping_patch"],
            [item["reason_code"] for item in overlap["skipped"]],
        )


class AuthoringRewriteReceiptTests(unittest.TestCase):
    module = "emotional_memory_module_two"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "brain.sqlite3"
        self.store = AuthoringRewriteStore(
            self.database, receipt_secret="authoring-test-secret-that-is-over-32-bytes"
        )
        self.emotional = EmotionalMemoryStore(self.database)
        self.owner = "owner:test"
        self.model = "model:test"
        self.wake = "wake:test"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _hash(value: object) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _preview(self, *, expected: int = 0, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "owner_id": self.owner,
            "model_id": self.model,
            "wake_id": self.wake,
            "expected_row_version": expected,
            "module": self.module,
            "draft_version": 0,
            "draft_fields": {
                "/original_text": "她回来了，我很安心。",
                "/summary": "她回来了。",
            },
            "referent_bindings": [
                {
                    "field_path": "/original_text",
                    "surface_form": "她",
                    "occurrence_index": 0,
                    "entity_ref": "person:xinxin",
                    "resolution_status": "resolved",
                    "confidence": 100,
                }
            ],
            "rewrite_targets": [
                {
                    "field_path": "/original_text",
                    "surface_form": "她",
                    "occurrence_index": 0,
                    "entity_ref": "person:xinxin",
                    "target_surface_form": "昕昕",
                    "mention_kind": "pronoun",
                    "target_alias_ref": "alias://person:xinxin/name@1",
                    "target_alias_version": 1,
                    "unique_in_scope": True,
                }
            ],
            "conversation_mode": "one_to_one",
            "authenticated_participant_entity_ids": ["person:xinxin"],
            "alias_collision_scope": "conversation:test",
            "alias_collision_scope_version": 1,
            "protected_spans": [],
            "module_schema_version": AUTHORING_SCHEMA_VERSIONS[self.module],
            "rewrite_eligible_allowlist_version": REWRITE_ELIGIBLE_ALLOWLIST_VERSION,
            "mention_parser_rule_version": MENTION_PARSER_RULE_VERSION,
            "alias_comparison_profile_version": ALIAS_COMPARISON_PROFILE_VERSION,
        }
        arguments.update(overrides)
        return self.store.preview(**arguments)

    def _confirm(
        self, preview: dict[str, object], **overrides: object
    ) -> dict[str, object]:
        final = preview["suggested_fields"]
        assert isinstance(final, dict)
        arguments: dict[str, object] = dict(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=self.wake,
            expected_row_version=int(preview["authoring_row_version"]),
            preview_id=str(preview["preview_id"]),
            expected_source_draft_hash=str(preview["source_draft_hash"]),
            expected_suggestion_hash=str(preview["suggestion_hash"]),
            expected_validation_context_hash=str(preview["validation_context_hash"]),
            final_fields=final,
            final_fields_hash=self._hash(final),
            conversation_mode="one_to_one",
            authenticated_participant_entity_ids=["person:xinxin"],
            alias_collision_scope="conversation:test",
            alias_collision_scope_version=1,
            protected_spans=[],
            module_schema_version=AUTHORING_SCHEMA_VERSIONS[self.module],
            rewrite_eligible_allowlist_version=REWRITE_ELIGIBLE_ALLOWLIST_VERSION,
            mention_parser_rule_version=MENTION_PARSER_RULE_VERSION,
            alias_comparison_profile_version=ALIAS_COMPARISON_PROFILE_VERSION,
            ai_confirmation=True,
        )
        arguments.update(overrides)
        return self.store.confirm(**arguments)

    def _database_snapshot(self) -> tuple[str, ...]:
        connection = sqlite3.connect(self.database)
        try:
            return tuple(connection.iterdump())
        finally:
            connection.close()

    def test_status_is_read_only_when_state_is_absent(self) -> None:
        self.assertEqual(
            0,
            self.store.status(owner_id=self.owner, model_id=self.model)["row_version"],
        )
        connection = sqlite3.connect(self.database)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM authoring_rewrite_state"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(0, count)

    def test_ambiguous_group_and_collision_are_non_blocking_zero_step(self) -> None:
        ambiguous = self._preview(
            referent_bindings=[
                {
                    "field_path": "/original_text",
                    "surface_form": "她",
                    "occurrence_index": 0,
                    "entity_ref": None,
                    "resolution_status": "ambiguous",
                    "confidence": 20,
                }
            ]
        )
        self.assertEqual("continue_original_path", ambiguous["decision"])
        self.assertFalse(ambiguous["state_changed"])
        self.assertEqual(0, self.store.status(owner_id=self.owner, model_id=self.model)["row_version"])

        group = self._preview(conversation_mode="group")
        self.assertEqual("continue_original_path", group["decision"])
        self.assertIn(
            "conversation_context_not_safe",
            [item["reason_code"] for item in group["skipped"]],
        )

        collision_targets = self._preview()["patches"]
        self.assertTrue(collision_targets)

    def test_secret_and_unicode_canaries_fail_before_preview_persistence(self) -> None:
        with self.assertRaisesRegex(AuthoringError, "credential_or_secret_detected"):
            self._preview(
                draft_fields={
                    "/original_text": "她回来了 token: very-secret-value",
                    "/summary": "她回来了。",
                }
            )
        with self.assertRaisesRegex(AuthoringError, "credential_or_secret_detected"):
            arguments = [
                {
                    "field_path": "/original_text",
                    "surface_form": "她",
                    "occurrence_index": 0,
                    "entity_ref": "person:xinxin",
                    "target_surface_form": "token:secret-value",
                    "mention_kind": "pronoun",
                    "target_alias_ref": "alias://secret",
                    "target_alias_version": 1,
                    "unique_in_scope": True,
                }
            ]
            self._preview(rewrite_targets=arguments)
        with self.assertRaisesRegex(AuthoringError, "unsafe_unicode"):
            self._preview(
                rewrite_targets=[
                    {
                        "field_path": "/original_text",
                        "surface_form": "她",
                        "occurrence_index": 0,
                        "entity_ref": "person:xinxin",
                        "target_surface_form": "昕\u202e昕",
                        "mention_kind": "pronoun",
                        "target_alias_ref": "alias://person:xinxin/name@1",
                        "target_alias_version": 1,
                        "unique_in_scope": True,
                    }
                ]
            )
        self.assertEqual(0, self.store.status(owner_id=self.owner, model_id=self.model)["row_version"])

    def test_exact_confirmation_receipt_is_atomic_single_use_and_idempotent(self) -> None:
        preview = self._preview()
        self.assertEqual("preview_only", preview["decision"])
        with self.assertRaisesRegex(AuthoringError, "validation_context_stale"):
            self.store.confirm(
                owner_id=self.owner,
                model_id=self.model,
                wake_id=self.wake,
                expected_row_version=1,
                preview_id=str(preview["preview_id"]),
                expected_source_draft_hash=str(preview["source_draft_hash"]),
                expected_suggestion_hash=str(preview["suggestion_hash"]),
                expected_validation_context_hash=str(preview["validation_context_hash"]),
                final_fields=preview["suggested_fields"],
                final_fields_hash=str(preview["suggestion_hash"]),
                conversation_mode="one_to_one",
                authenticated_participant_entity_ids=["person:xinxin"],
                alias_collision_scope="conversation:test",
                alias_collision_scope_version=2,
                protected_spans=[],
                module_schema_version=AUTHORING_SCHEMA_VERSIONS[self.module],
                rewrite_eligible_allowlist_version=REWRITE_ELIGIBLE_ALLOWLIST_VERSION,
                mention_parser_rule_version=MENTION_PARSER_RULE_VERSION,
                alias_comparison_profile_version=ALIAS_COMPARISON_PROFILE_VERSION,
                ai_confirmation=True,
            )
        confirmed = self._confirm(preview)
        replayed_confirmation = self.store.confirm(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=self.wake,
            expected_row_version=2,
            preview_id=str(preview["preview_id"]),
            expected_source_draft_hash=str(preview["source_draft_hash"]),
            expected_suggestion_hash=str(preview["suggestion_hash"]),
            expected_validation_context_hash=str(preview["validation_context_hash"]),
            final_fields=preview["suggested_fields"],
            final_fields_hash=str(preview["suggestion_hash"]),
            conversation_mode="one_to_one",
            authenticated_participant_entity_ids=["person:xinxin"],
            alias_collision_scope="conversation:test",
            alias_collision_scope_version=1,
            protected_spans=[],
            module_schema_version=AUTHORING_SCHEMA_VERSIONS[self.module],
            rewrite_eligible_allowlist_version=REWRITE_ELIGIBLE_ALLOWLIST_VERSION,
            mention_parser_rule_version=MENTION_PARSER_RULE_VERSION,
            alias_comparison_profile_version=ALIAS_COMPARISON_PROFILE_VERSION,
            ai_confirmation=True,
        )
        self.assertTrue(replayed_confirmation["idempotent_replay"])
        final = preview["suggested_fields"]
        assert isinstance(final, dict)
        call = {
            "owner_id": self.owner,
            "model_id": self.model,
            "wake_id": self.wake,
            "expected_row_version": 0,
            "memory_type": "shared_event",
            "original_text": final["/original_text"],
            "summary": final["/summary"],
            "primary_emotion": "calm",
            "reason": "我选择采纳这次精确词形建议。",
            "rewrite_receipt": confirmed["rewrite_receipt"],
        }
        first = self.emotional.remember(**call)
        second = self.emotional.remember(**call)
        self.assertEqual(first["memory"]["memory_id"], second["memory"]["memory_id"])
        self.assertEqual(1, self.emotional.status(owner_id=self.owner, model_id=self.model)["counts"]["active_memories"])
        self.assertEqual(
            "machine_assisted_mention_patch",
            first["authoring_provenance"]["adoption_mode"],
        )
        conflicting = dict(call)
        conflicting["reason"] = "另一个请求。"
        with self.assertRaisesRegex(Exception, "rewrite_receipt_replay_conflict"):
            self.emotional.remember(**conflicting)

    def test_changed_source_final_context_or_wake_cannot_confirm_old_preview(self) -> None:
        preview = self._preview()
        before = self._database_snapshot()
        changed_final = dict(preview["suggested_fields"])
        changed_final["/summary"] = "今天的草稿已被另外修改。"
        cases = [
            (
                "rewrite_source_stale",
                {"expected_source_draft_hash": self._hash({"/original_text": "她又回来了。"})},
            ),
            ("rewrite_suggestion_stale", {"expected_suggestion_hash": "f" * 64}),
            ("final_fields_hash_mismatch", {"final_fields_hash": "0" * 64}),
            (
                "final_fields_must_match_preview",
                {"final_fields": changed_final, "final_fields_hash": self._hash(changed_final)},
            ),
            ("validation_context_stale", {"alias_collision_scope_version": 2}),
            (
                "validation_context_stale",
                {"authenticated_participant_entity_ids": ["person:other"]},
            ),
            ("validation_context_stale", {"expected_validation_context_hash": "e" * 64}),
            ("rewrite_preview_wrong_wake", {"wake_id": "wake:later"}),
        ]
        for reason, overrides in cases:
            with self.subTest(reason=reason, fields=tuple(overrides)):
                with self.assertRaisesRegex(AuthoringError, reason):
                    self._confirm(preview, **overrides)
                self.assertEqual(before, self._database_snapshot())
        self.assertEqual("confirmed", self._confirm(preview)["decision"])

    def test_independent_drafts_share_a_wake_without_invalidating_each_other(self) -> None:
        first = self._preview()
        second = self._preview(
            expected=1,
            draft_version=1,
            draft_fields={
                "/original_text": "她回来了，我感觉安定。",
                "/summary": "她今天回来了。",
            },
        )
        self.assertNotEqual(first["source_draft_hash"], second["source_draft_hash"])
        # There is no server-owned mutable draft slot. Each preview identifies
        # its own snapshot; only the shared authoring CAS version advances.
        confirmed_first = self._confirm(first, expected_row_version=2)
        confirmed_second = self._confirm(second, expected_row_version=3)
        self.assertEqual("confirmed", confirmed_first["decision"])
        self.assertEqual("confirmed", confirmed_second["decision"])
        self.assertNotEqual(confirmed_first["rewrite_receipt"], confirmed_second["rewrite_receipt"])

    def test_two_concurrent_consumers_create_at_most_one_canonical_memory(self) -> None:
        preview = self._preview()
        confirmed = self._confirm(preview)
        final = preview["suggested_fields"]
        assert isinstance(final, dict)
        call = {
            "owner_id": self.owner,
            "model_id": self.model,
            "wake_id": self.wake,
            "expected_row_version": 0,
            "memory_type": "shared_event",
            "original_text": final["/original_text"],
            "summary": final["/summary"],
            "primary_emotion": "calm",
            "reason": "我并发测试一次性回执。",
            "rewrite_receipt": confirmed["rewrite_receipt"],
        }
        results: list[dict[str, object]] = []
        failures: list[BaseException] = []

        def consume() -> None:
            try:
                results.append(self.emotional.remember(**call))
            except BaseException as exc:  # pragma: no cover - assertion reports it
                failures.append(exc)

        workers = [threading.Thread(target=consume) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual([], failures)
        self.assertEqual(2, len(results))
        self.assertEqual(
            results[0]["memory"]["memory_id"], results[1]["memory"]["memory_id"]
        )
        self.assertEqual(1, self.emotional.status(owner_id=self.owner, model_id=self.model)["counts"]["active_memories"])


if __name__ == "__main__":
    unittest.main()
