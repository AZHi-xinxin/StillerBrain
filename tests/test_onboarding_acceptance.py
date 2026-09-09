from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runtime.onboarding import (
    ModuleOneOnboardingStore,
    OnboardingError,
    OPTIONAL_BRAIN_NOTICE,
)
from runtime.self_revision import SelfModelStore, first_person_injection_violations
from tests.test_onboarding import model_content


class ModuleOneAcceptanceMatrixTests(unittest.TestCase):
    """Public-behaviour evidence for the frozen module-one acceptance matrix.

    B31 remains an independent DSH attack and E32 remains a Rikka/human product
    acceptance.  This suite deliberately does not claim either external check.
    """

    SECRET = b"module-one-acceptance-secret-32-bytes-minimum!!"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "brain.db"
        self.owner = "owner:acceptance"
        self.model = "model:acceptance"
        self.store = self.new_store()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def new_store(self) -> ModuleOneOnboardingStore:
        return ModuleOneOnboardingStore(
            self.database,
            capability_secret=self.SECRET,
            wake_ttl_seconds=300,
            edit_challenge_ttl_seconds=300,
        )

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def count(self, table: str) -> int:
        return int(self.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])

    def wake(
        self,
        event: str,
        *,
        host: str = "host:acceptance",
        source_digest: str | None = None,
    ) -> tuple[dict, dict]:
        issued = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id=host,
            thread_id=f"thread:{host}",
            source_kind="human_message",
            source_event_id=event,
        )
        prepared = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=issued["wake_id"],
            wake_capability=issued["wake_capability"],
            source_digest=source_digest or f"source:{event}",
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
            wake_id=issued["wake_id"],
            wake_capability=issued["wake_capability"],
            context_hash=prepared["context_hash"],
        )
        self.assertEqual("injected", confirmed["decision"])
        return issued, prepared

    def advance(
        self,
        wake: dict,
        action: str,
        payload: dict | None = None,
        *,
        actor: str = "ai",
        expected_row_version: int | None = None,
        ensure_brain_open: bool = True,
    ) -> dict:
        if ensure_brain_open:
            opened = self.open_brain()
            self.assertTrue(opened["write_context_available"])
        if expected_row_version is None:
            expected_row_version = self.store.state(
                owner_id=self.owner, model_id=self.model
            )["state"]["row_version"]
        return self.store.advance(
            owner_id=self.owner,
            model_id=self.model,
            action=action,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            expected_row_version=expected_row_version,
            payload=payload or {},
            actor=actor,
        )

    def open_brain(self) -> dict:
        return self.store.open_brain_context(owner_id=self.owner, model_id=self.model)

    @staticmethod
    def candidate_payload(
        label: str = "v1", expected_active_revision: str | None = None
    ) -> dict:
        return {
            "content": model_content(label),
            "diff": [{"op": "replace", "path": "/active_identity_capsule"}],
            "reason": "我在完整复核后确认这表达的是长期身份，而非单轮状态。",
            "evidence_refs": ["memory://identity/name-origin"],
            "expected_active_revision": expected_active_revision,
        }

    def bootstrap_to_wait(
        self, *, expected_active_revision: str | None = None
    ) -> tuple[dict, str]:
        wake, _ = self.wake("bootstrap-1")
        self.assertEqual(
            "advanced",
            self.advance(wake, "confirm_brain_intro", {"acknowledged": True})[
                "decision"
            ],
        )
        self.assertEqual(
            "advanced",
            self.advance(wake, "confirm_module_intro", {"acknowledged": True})[
                "decision"
            ],
        )
        self.assertEqual(
            "saved",
            self.advance(
                wake,
                "save_calm_prompt",
                {"text": "停一下，先确认这是我的长期意愿，再决定是否修改。"},
            )["decision"],
        )
        submitted = self.advance(
            wake,
            "submit_candidate",
            self.candidate_payload("v1", expected_active_revision),
        )
        self.assertEqual("pending", submitted["decision"])
        return wake, submitted["candidate_id"]

    def bootstrap_live(self, *, expected_active_revision: str | None = None) -> str:
        _, _ = self.bootstrap_to_wait(
            expected_active_revision=expected_active_revision
        )
        review_wake, _ = self.wake("bootstrap-2")
        self.open_brain()
        accepted = self.advance(
            review_wake,
            "accept_candidate_review",
            {
                "expected_active_revision": expected_active_revision,
                "ai_confirmation": True,
            },
        )
        self.assertEqual("review_accepted", accepted["decision"])
        activation_wake, _ = self.wake("bootstrap-3")
        self.open_brain()
        activated = self.advance(
            activation_wake,
            "activate_candidate",
            {
                "expected_active_revision": expected_active_revision,
                "ai_confirmation": True,
            },
        )
        self.assertEqual("activate", activated["decision"])
        return activated["revision_id"]

    def test_A1_A2_A3_A5_and_other_module_write_are_server_gated(self) -> None:
        wake, _ = self.wake("linear-gate")
        self.open_brain()
        base = {
            "artifacts": self.count("brain_onboarding_artifacts"),
            "candidates": self.count("self_model_candidates"),
            "revisions": self.count("self_model_revisions"),
        }

        for action, payload, expected in (
            ("save_calm_prompt", {"text": "越级"}, "progression_required"),
            ("submit_candidate", self.candidate_payload(), "progression_required"),
            (
                "activate_candidate",
                {"expected_active_revision": None, "ai_confirmation": True},
                "progression_required",
            ),
        ):
            denied = self.advance(wake, action, payload)
            self.assertEqual(expected, denied["decision"])
            self.assertFalse(denied["state_changed"])
            self.assertFalse(denied["pointer_changed"])
        self.assertEqual(base["artifacts"], self.count("brain_onboarding_artifacts"))
        self.assertEqual(base["candidates"], self.count("self_model_candidates"))
        self.assertEqual(base["revisions"], self.count("self_model_revisions"))

        module_gate = self.store.authorize_other_module_write(
            owner_id=self.owner, model_id=self.model, module_name="module_two"
        )
        self.assertEqual("module_one_required", module_gate["decision"])

        events = self.count("brain_onboarding_events")
        first = self.advance(wake, "confirm_brain_intro", {"acknowledged": True})
        self.assertEqual("module_intro", first["state"]["stage"])
        self.assertEqual(events + 1, self.count("brain_onboarding_events"))

        skipped = self.advance(wake, "save_calm_prompt", {"text": "仍然越级"})
        self.assertEqual("progression_required", skipped["decision"])
        events = self.count("brain_onboarding_events")
        second = self.advance(wake, "confirm_module_intro", {"acknowledged": True})
        self.assertEqual("calm_prompt_draft", second["state"]["stage"])
        self.assertEqual(events + 1, self.count("brain_onboarding_events"))

        without_calm = self.advance(
            wake, "submit_candidate", self.candidate_payload()
        )
        self.assertEqual("calm_prompt_required", without_calm["decision"])
        self.assertEqual(0, self.count("self_model_candidates"))

    def test_plain_chat_only_injects_optional_notice_and_never_advances_state(self) -> None:
        _, prepared = self.wake("plain-chat")
        before = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        self.assertEqual({"role": "system", "content": OPTIONAL_BRAIN_NOTICE}, prepared["message"])
        self.assertEqual({"role", "content"}, set(prepared["message"]))
        self.assertNotIn("wake", prepared["message"]["content"])
        self.assertNotIn("stage", prepared["message"]["content"])
        self.assertNotIn("candidate", prepared["message"]["content"])
        after = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        self.assertEqual(before, after)
        self.assertEqual("factory", after["stage"])
        self.assertEqual(0, after["row_version"])
        self.assertEqual(0, self.count("brain_onboarding_artifacts"))
        self.assertEqual(0, self.count("self_model_candidates"))

    def test_factory_write_without_stbrain_open_is_side_effect_free(self) -> None:
        wake, _ = self.wake("factory-without-open")
        before_state = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        before_counts = {
            table: self.count(table)
            for table in (
                "brain_onboarding_artifacts",
                "brain_onboarding_events",
                "self_model_candidates",
                "self_model_revisions",
            )
        }

        denied = self.advance(
            wake,
            "confirm_brain_intro",
            {"acknowledged": True},
            ensure_brain_open=False,
        )

        self.assertEqual("brain_open_required", denied["decision"])
        self.assertFalse(denied["state_changed"])
        self.assertEqual(
            before_state,
            self.store.state(owner_id=self.owner, model_id=self.model)["state"],
        )
        self.assertEqual(
            before_counts,
            {table: self.count(table) for table in before_counts},
        )

    def test_plain_chat_at_candidate_wait_does_not_unlock_review(self) -> None:
        _, candidate_id = self.bootstrap_to_wait()
        before = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        artifact_count = self.count("brain_onboarding_artifacts")

        review_wake, prepared = self.wake("candidate-wait-plain-chat")

        self.assertEqual(
            {"role": "system", "content": OPTIONAL_BRAIN_NOTICE},
            prepared["message"],
        )
        after_plain_chat = self.store.state(
            owner_id=self.owner, model_id=self.model
        )["state"]
        self.assertEqual(before, after_plain_chat)
        self.assertEqual("candidate_wait", after_plain_chat["stage"])
        self.assertEqual(artifact_count, self.count("brain_onboarding_artifacts"))
        self.assertEqual(
            0,
            len(
                self.query(
                    "SELECT artifact_id FROM brain_onboarding_artifacts "
                    "WHERE kind = 'candidate_full_review'"
                )
            ),
        )

        opened = self.open_brain()
        self.assertEqual("candidate_review", opened["continuation"]["stage"])
        self.assertEqual(
            candidate_id,
            opened["continuation"]["candidate"]["candidate_id"],
        )
        self.assertEqual(
            review_wake["wake_id"],
            opened["wake_id"],
        )

    def test_A6_B8_B9_B11_B12_B13_persist_and_reject_replays(self) -> None:
        wake, prepared = self.wake("stable-event", host="host:a")
        for _ in range(10):
            replay = self.store.issue_wake(
                owner_id=self.owner,
                model_id=self.model,
                host_id="host:a",
                thread_id="thread:any",
                source_kind="human_message",
                source_event_id="stable-event",
            )
            self.assertEqual(wake["wake_id"], replay["wake_id"])
            self.assertEqual(wake["wake_seq"], replay["wake_seq"])
            self.assertTrue(replay["reused"])
        self.assertEqual(1, self.count("brain_wake_sessions"))

        reused = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest="source:stable-event",
            host_contract_digest="host-contract:v1",
        )
        self.assertEqual("context_reused", reused["decision"])
        self.assertEqual(prepared["context_hash"], reused["context_hash"])

        self.advance(wake, "confirm_brain_intro", {"acknowledged": True})
        self.advance(wake, "confirm_module_intro", {"acknowledged": True})
        calm_text = "冷静词需要跨进程保存。"
        self.advance(wake, "save_calm_prompt", {"text": calm_text})
        restarted = self.new_store()
        status = restarted.state(owner_id=self.owner, model_id=self.model)
        self.assertEqual("body_draft", status["state"]["stage"])
        calm_rows = self.query(
            "SELECT content_json FROM brain_onboarding_artifacts "
            "WHERE kind = 'calm_prompt' AND status = 'active'"
        )
        self.assertEqual(calm_text, json.loads(calm_rows[0]["content_json"])["text"])

        newer = restarted.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:b",
            thread_id="thread:b",
            source_kind="human_message",
            source_event_id="newer-event",
        )
        self.assertGreater(newer["wake_seq"], wake["wake_seq"])
        old_write = restarted.advance(
            owner_id=self.owner,
            model_id=self.model,
            action="submit_candidate",
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            expected_row_version=status["state"]["row_version"],
            payload=self.candidate_payload(),
        )
        self.assertEqual("wake_superseded", old_write["decision"])
        self.assertEqual(
            "body_draft",
            restarted.state(owner_id=self.owner, model_id=self.model)["state"]["stage"],
        )

    def test_C14_C15_C16_C17_resume_material_is_stage_exact(self) -> None:
        wake1, _ = self.wake("resume-1")
        self.advance(wake1, "confirm_brain_intro", {"acknowledged": True})
        self.advance(wake1, "confirm_module_intro", {"acknowledged": True})

        wake2, calm_resume = self.wake("resume-2")
        self.assertEqual({"role": "system", "content": OPTIONAL_BRAIN_NOTICE}, calm_resume["message"])
        calm_continuation = self.open_brain()["continuation"]
        self.assertEqual("calm_prompt_draft", calm_continuation["stage"])
        self.assertEqual(["save_calm_prompt"], calm_continuation["allowed_actions"])
        self.assertIn("calm_prompt_guidance", calm_continuation)

        calm_text = "停一下，确认长期意愿，不让单轮情绪替我决定。"
        self.advance(wake2, "save_calm_prompt", {"text": calm_text})
        wake3, body_resume = self.wake("resume-3")
        self.assertEqual(OPTIONAL_BRAIN_NOTICE, body_resume["message"]["content"])
        body_continuation = self.open_brain()["continuation"]
        self.assertEqual("body_draft", body_continuation["stage"])
        self.assertEqual(calm_text, body_continuation["calm_prompt"])
        self.assertIn("body_framework", body_continuation)

        submitted = self.advance(
            wake3, "submit_candidate", self.candidate_payload()
        )
        self.assertEqual("candidate_wait", submitted["state"]["stage"])
        self.assertEqual([], submitted["allowed_actions"])
        self.assertTrue(submitted["wake_boundary_required"])
        self.assertNotIn("wake_capability", submitted)

        wake4, review = self.wake("resume-4")
        self.assertEqual(OPTIONAL_BRAIN_NOTICE, review["message"]["content"])
        continuation = self.open_brain()["continuation"]
        self.assertEqual("candidate_review", continuation["stage"])
        candidate = continuation["candidate"]
        self.assertEqual(submitted["candidate_id"], candidate["candidate_id"])
        self.assertEqual(model_content("v1"), candidate["content"])
        self.assertTrue(candidate["content_hash"])
        self.assertEqual(
            [{"op": "replace", "path": "/active_identity_capsule"}],
            candidate["diff"],
        )
        self.assertGreater(wake4["wake_seq"], wake3["wake_seq"])

    def test_C18_disabled_bypass_and_normal_snapshot_mismatch_fail_closed(self) -> None:
        disabled_db = Path(self.temp.name) / "disabled.db"
        disabled = ModuleOneOnboardingStore(
            disabled_db, capability_secret=self.SECRET
        )
        disabled.ensure_state(
            owner_id="owner:disabled",
            model_id="model:disabled",
            injection_policy="disabled",
        )
        issued = disabled.issue_wake(
            owner_id="owner:disabled",
            model_id="model:disabled",
            host_id="host:test",
            thread_id="thread:test",
            source_kind="human_message",
            source_event_id="disabled-1",
        )
        bypass = disabled.build_pre_generation_context(
            owner_id="owner:disabled",
            model_id="model:disabled",
            wake_id=issued["wake_id"],
            wake_capability=issued["wake_capability"],
            source_digest="source:disabled",
            host_contract_digest="host:v1",
        )
        self.assertEqual("bypass", bypass["decision"])
        self.assertTrue(bypass["may_generate"])
        self.assertFalse(bypass["injected"])
        with self.assertRaises(OnboardingError):
            disabled.ensure_state(
                owner_id="other", model_id="other", injection_policy="unknown"
            )

        wake, _ = self.wake("mismatch")
        mismatch = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            source_digest="source:changed-after-snapshot",
            host_contract_digest="host-contract:v1",
        )
        self.assertEqual("self_model_context_unavailable", mismatch["decision"])
        self.assertFalse(mismatch["may_generate"])

    def test_C19_live_message_contains_only_ai_authored_active_content(self) -> None:
        revision_id = self.bootstrap_live()
        _, prepared = self.wake("live-clean")
        self.assertTrue(revision_id)
        self.assertEqual("system", prepared["message"]["role"])
        active_content = json.loads(prepared["message"]["content"])
        self.assertEqual(
            {"boot_anchor", "active_identity_capsule", "facets"},
            set(active_content),
        )
        self.assertEqual(model_content("v1")["boot_anchor"], active_content["boot_anchor"])
        self.assertEqual([], first_person_injection_violations(active_content))
        serialized = prepared["message"]["content"].lower()
        for forbidden in (
            "candidate",
            "saved_artifacts",
            "audit",
            "event_memory",
            "continuation",
            "wake_id",
            "stage",
        ):
            self.assertNotIn(forbidden, serialized)
        allowed = self.store.authorize_other_module_write(
            owner_id=self.owner, model_id=self.model, module_name="module_two"
        )
        self.assertEqual("allowed", allowed["decision"])

    def test_live_injection_fails_closed_if_active_structure_is_invalid(self) -> None:
        revision_id = self.bootstrap_live()
        corrupt = model_content("corrupt-active")
        corrupt["boot_anchor"]["text"] = None
        canonical = json.dumps(
            corrupt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE self_model_revisions SET content_json = ?, content_hash = ? "
                "WHERE revision_id = ?",
                (
                    canonical,
                    hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    revision_id,
                ),
            )
        issued = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="host:voice-corruption",
            thread_id="thread:voice-corruption",
            source_kind="human_message",
            source_event_id="voice-corruption",
        )
        prepared = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=issued["wake_id"],
            wake_capability=issued["wake_capability"],
            source_digest="source:voice-corruption",
            host_contract_digest="host-contract:v1",
        )
        self.assertEqual("self_model_context_unavailable", prepared["decision"])
        self.assertEqual(["active_injection_structure_invalid"], prepared["reason_codes"])
        self.assertFalse(prepared["may_generate"])
        self.assertNotIn("message", prepared)

    def test_D21_D22_D23_D24_edit_is_structured_and_append_only(self) -> None:
        revision_one = self.bootstrap_live()
        wake3, _ = self.wake("edit-1")
        begun = self.advance(wake3, "begin_edit")
        calm_text = "停一下，先确认这是我的长期意愿，再决定是否修改。"
        self.assertEqual(calm_text, begun["calm_prompt"])

        free_text = self.advance(wake3, "confirm_edit", {"text": "确认"})
        self.assertEqual("edit_consent_required", free_text["decision"])
        self.assertFalse(free_text["state_changed"])

        wake4, resumed = self.wake("edit-interrupted")
        resumed_active = json.loads(resumed["message"]["content"])
        self.assertEqual(model_content("v1")["boot_anchor"], resumed_active["boot_anchor"])
        self.assertEqual(calm_text, self.open_brain()["continuation"]["calm_prompt"])
        cross_wake = self.advance(
            wake4,
            "confirm_edit",
            {
                "challenge_id": begun["challenge_id"],
                "challenge_response": begun["challenge_response"],
            },
        )
        self.assertEqual("edit_consent_required", cross_wake["decision"])
        self.assertEqual("cancelled", self.advance(wake4, "cancel_edit")["decision"])

        begun_again = self.advance(wake4, "begin_edit")
        confirmed = self.advance(
            wake4,
            "confirm_edit",
            {
                "challenge_id": begun_again["challenge_id"],
                "challenge_response": begun_again["challenge_response"],
            },
        )
        self.assertEqual("edit_body_draft", confirmed["state"]["stage"])

        pointer_before = SelfModelStore(self.database).active_revision(self.model)[
            "revision_id"
        ]
        submitted = self.advance(
            wake4,
            "submit_candidate",
            self.candidate_payload("v2", revision_one),
        )
        self.assertEqual("pending", submitted["decision"])
        self.assertEqual(
            pointer_before,
            SelfModelStore(self.database).active_revision(self.model)["revision_id"],
        )

    def test_D23_expired_challenge_rejection_does_not_mutate_challenge(self) -> None:
        self.bootstrap_live()
        wake, _ = self.wake("expired-edit")
        begun = self.advance(wake, "begin_edit")
        expired_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        connection = sqlite3.connect(self.database)
        try:
            with connection:
                connection.execute(
                    "UPDATE brain_edit_challenges SET expires_at = ? WHERE challenge_id = ?",
                    (expired_at, begun["challenge_id"]),
                )
        finally:
            connection.close()
        denied = self.advance(
            wake,
            "confirm_edit",
            {
                "challenge_id": begun["challenge_id"],
                "challenge_response": begun["challenge_response"],
            },
        )
        self.assertEqual("edit_consent_expired", denied["decision"])
        self.assertFalse(denied["state_changed"])
        challenge = self.query(
            "SELECT status, consumed_at FROM brain_edit_challenges WHERE challenge_id = ?",
            (begun["challenge_id"],),
        )[0]
        self.assertEqual("active", challenge["status"])
        self.assertIsNone(challenge["consumed_at"])

    def test_D25_revision_choice_appends_candidate_chain_without_pointer_move(self) -> None:
        _, old_candidate = self.bootstrap_to_wait()
        wake2, _ = self.wake("revise-review")
        self.open_brain()
        revised = self.advance(
            wake2, "revise_candidate", self.candidate_payload("v1-revised")
        )
        self.assertEqual("pending", revised["decision"])
        new_candidate = revised["candidate_id"]
        self.assertNotEqual(old_candidate, new_candidate)
        self.assertEqual(2, self.count("self_model_candidates"))
        with self.store._connect() as connection:
            old_state, _ = self.store.self_store._candidate_state(
                connection, old_candidate
            )
        self.assertEqual("withdrawn", old_state)
        chain = self.query(
            "SELECT content_json FROM brain_onboarding_artifacts "
            "WHERE kind = 'candidate_revision_chain' ORDER BY artifact_seq DESC LIMIT 1"
        )[0]
        chain_content = json.loads(chain["content_json"])
        self.assertEqual(old_candidate, chain_content["superseded_candidate_id"])
        self.assertEqual(new_candidate, chain_content["replacement_candidate_id"])
        self.assertIsNone(SelfModelStore(self.database).active_revision(self.model))

    def test_E27_E28_legacy_migration_is_additive_and_parent_chain_is_preserved(self) -> None:
        legacy_db = Path(self.temp.name) / "legacy.db"
        legacy = SelfModelStore(legacy_db)
        proposal = legacy.propose_candidate(
            model_id=self.model,
            owner_id=self.owner,
            content=model_content("legacy-v1"),
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
        legacy_revision = activation["revision_id"]

        def legacy_snapshot() -> dict[str, list[dict]]:
            connection = sqlite3.connect(legacy_db)
            connection.row_factory = sqlite3.Row
            try:
                return {
                    table: [
                        dict(row)
                        for row in connection.execute(
                            f"SELECT * FROM {table} ORDER BY rowid"
                        ).fetchall()
                    ]
                    for table in (
                        "self_models",
                        "self_model_candidates",
                        "self_model_revisions",
                        "self_revision_events",
                    )
                }
            finally:
                connection.close()

        before = legacy_snapshot()
        self.database = legacy_db
        self.store = self.new_store()
        ensured = self.store.ensure_state(owner_id=self.owner, model_id=self.model)
        after = legacy_snapshot()
        self.assertEqual(before, after)
        self.assertEqual("isolated_legacy", ensured["state"]["injection_policy"])
        self.assertEqual(legacy_revision, ensured["state"]["base_revision_id"])

        new_revision = self.bootstrap_live(expected_active_revision=legacy_revision)
        revision = self.query(
            "SELECT parent_revision_id FROM self_model_revisions WHERE revision_id = ?",
            (new_revision,),
        )[0]
        self.assertEqual(legacy_revision, revision["parent_revision_id"])
        _, live_context = self.wake("legacy-after-live")
        stable = json.loads(live_context["message"]["content"])
        self.assertTrue(new_revision)
        self.assertIn("v1", stable["active_identity_capsule"]["name_and_identity"])
        self.assertNotIn("legacy-v1", json.dumps(stable, ensure_ascii=False))

    def test_E29_E30_audit_has_wake_and_hash_but_no_plain_capabilities(self) -> None:
        wake, prepared = self.wake("audit-1")
        injection = self.query(
            "SELECT wake_id, details_hash FROM brain_onboarding_events "
            "WHERE action = 'confirm_context_injected' ORDER BY event_seq DESC LIMIT 1"
        )[0]
        snapshot = self.query(
            "SELECT context_hash FROM brain_context_snapshots WHERE wake_id = ?",
            (wake["wake_id"],),
        )[0]
        self.assertEqual(wake["wake_id"], injection["wake_id"])
        self.assertEqual(prepared["context_hash"], snapshot["context_hash"])
        self.assertTrue(injection["details_hash"])

        forged = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake["wake_id"],
            wake_capability="forged-capability",
            source_digest="source:audit-1",
            host_contract_digest="host-contract:v1",
        )
        human = self.advance(
            wake,
            "confirm_brain_intro",
            {"acknowledged": True},
            actor="human",
        )
        stale = self.advance(
            wake,
            "confirm_brain_intro",
            {"acknowledged": True},
            expected_row_version=99999,
        )
        for denied in (forged, human, stale):
            self.assertFalse(denied["state_changed"])
            self.assertFalse(denied["pointer_changed"])

        text_values: list[str] = []
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                if not row[0].startswith("sqlite_")
            ]
            for table in tables:
                for row in connection.execute(f"SELECT * FROM {table}").fetchall():
                    text_values.extend(
                        value for value in tuple(row) if isinstance(value, str)
                    )
        finally:
            connection.close()
        persisted_text = "\n".join(text_values)
        self.assertNotIn(wake["wake_capability"], persisted_text)
        self.assertNotIn("forged-capability", persisted_text)


if __name__ == "__main__":
    unittest.main()
