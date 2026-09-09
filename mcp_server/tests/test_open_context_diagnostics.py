"""Real onboarding gates against one isolated, synthetic-only temporary DB."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from runtime.onboarding import ModuleOneOnboardingStore


class OpenContextDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        for target in ("socket.socket", "socket.create_connection"):
            self.enterContext(
                patch(target, side_effect=AssertionError("network forbidden in synthetic test"))
            )
        temp = self.enterContext(tempfile.TemporaryDirectory(prefix="open-context-synthetic-"))
        self.database = (Path(temp) / "synthetic-brain.sqlite3").resolve()
        real_connect = sqlite3.connect

        def synthetic_connect(path: object, *args: object, **kwargs: object) -> sqlite3.Connection:
            if Path(path).resolve() != self.database:  # type: ignore[arg-type]
                raise AssertionError("only this temporary synthetic DB is permitted")
            return real_connect(path, *args, **kwargs)  # type: ignore[arg-type]

        self.enterContext(patch("sqlite3.connect", side_effect=synthetic_connect))
        self.owner = "synthetic-owner-not-for-output"
        self.model = "synthetic-model-not-for-output"
        self.sensitive_values = {self.owner, self.model, "synthetic-unknown-ref"}
        self.store = ModuleOneOnboardingStore(
            self.database,
            capability_secret=b"synthetic-diagnostic-key-not-a-real-credential!!",
            wake_ttl_seconds=300,
            direct_grant_ttl_seconds=300,
        )
        self.store.ensure_state(owner_id=self.owner, model_id=self.model)

    def wake(
        self, event: str, *, owner: str | None = None, model: str | None = None,
        prepare: bool = True, inject: bool = True,
    ) -> tuple[dict, dict | None]:
        owner, model = owner or self.owner, model or self.model
        self.sensitive_values.update((owner, model))
        wake = self.store.issue_wake(
            owner_id=owner, model_id=model, host_id="synthetic-host",
            thread_id="synthetic-thread", source_kind="human_message", source_event_id=event,
        )
        self.sensitive_values.update((wake["wake_id"], wake["wake_capability"]))
        prepared = None
        if prepare:
            prepared = self.store.build_pre_generation_context(
                owner_id=owner, model_id=model,
                wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
                source_digest="synthetic-source:" + event,
                host_contract_digest="synthetic-host-contract",
            )
            self.assertEqual("context_prepared", prepared["decision"])
            self.sensitive_values.add(prepared["context_hash"])
            if inject:
                confirmed = self.store.confirm_context_injected(
                    owner_id=owner, model_id=model,
                    wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
                    context_hash=prepared["context_hash"],
                )
                self.assertEqual("injected", confirmed["decision"])
        return wake, prepared

    def open(self, *, owner: str | None = None, model: str | None = None) -> dict:
        result = self.store.open_brain_context(
            owner_id=owner or self.owner, model_id=model or self.model, present_details=False,
        )
        if result.get("write_context_available") is True:
            self.sensitive_values.add(result["write_context_ref"])
        return result

    def binding(self, ref: str = "synthetic-unknown-ref", *, scope: str = "planning_memory") -> dict:
        return self.store.current_open_write_context(
            owner_id=self.owner, model_id=self.model,
            write_context_ref=ref, required_scope=scope,
        )

    def metadata(self) -> tuple:
        with self.store._connect() as connection:
            return tuple(
                tuple(connection.execute(query).fetchone())
                for query in (
                    "SELECT COUNT(*) FROM brain_wake_sessions",
                    "SELECT COUNT(*) FROM brain_context_snapshots",
                    "SELECT COUNT(*) FROM brain_onboarding_artifacts",
                    "SELECT COUNT(*) FROM brain_onboarding_events",
                    "SELECT SUM(row_version) FROM brain_onboarding_state",
                )
            )

    def assert_failure(
        self, result: dict, detail: str | None,
        legacy: str = "current_injected_wake_required",
    ) -> None:
        self.assertIs(False, result["write_context_available"])
        self.assertEqual(legacy, result["reason_code"])
        if detail is not None:
            self.assertEqual(detail, result["binding_reason_code"])
        self.assertLessEqual(
            set(result),
            {"continuation", "write_context_available", "reason_code", "binding_reason_code", "row_version"},
        )
        serialized = json.dumps(result)
        for value in self.sensitive_values:
            self.assertNotIn(value, serialized)

    def assert_both_fail(self, detail: str, ref: str = "synthetic-unknown-ref") -> None:
        before = self.metadata()
        self.assert_failure(self.open(), detail)
        self.assert_failure(self.binding(ref), detail)
        self.assertEqual(before, self.metadata())

    def test_no_current_wake_is_not_guessed_to_be_expired_or_closed(self) -> None:
        self.assert_both_fail("current_wake_required")

    def test_issued_and_prepared_but_unconfirmed_wakes_require_injection(self) -> None:
        self.wake("synthetic-issued-only", prepare=False)
        self.assert_both_fail("injected_context_required")
        self.wake("synthetic-prepared-only", inject=False)
        self.assert_both_fail("injected_context_required")

    def test_actual_expired_current_wake_has_expired_detail_on_both_entries(self) -> None:
        wake, _ = self.wake("synthetic-expiry")
        opened = self.open()
        self.assertTrue(self.binding(opened["write_context_ref"])["write_context_available"])
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE brain_wake_sessions SET expires_at=? WHERE wake_id=?",
                ("2000-01-01T00:00:00+00:00", wake["wake_id"]),
            )
        self.assert_both_fail("write_context_expired", opened["write_context_ref"])

    def test_closed_wake_reports_only_no_current_wake(self) -> None:
        wake, _ = self.wake("synthetic-closed")
        opened = self.open()
        closed = self.store.close_context_snapshot(
            owner_id=self.owner, model_id=self.model,
            wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
        )
        self.assertEqual("closed", closed["decision"])
        self.assert_both_fail("current_wake_required", opened["write_context_ref"])

    def test_unopened_and_wrong_ref_share_non_enumerating_failure(self) -> None:
        self.wake("synthetic-unopened")
        before = self.metadata()
        unopened = self.binding()
        self.assert_failure(unopened, "write_context_not_opened_or_mismatched", "brain_open_required")
        self.assertEqual(before, self.metadata())
        self.open()
        before = self.metadata()
        wrong = self.binding()
        self.assert_failure(wrong, "write_context_not_opened_or_mismatched", "brain_open_required")
        self.assertEqual(unopened, wrong)
        self.assertEqual(before, self.metadata())

    def test_correct_ref_succeeds_and_reopen_reuses_current_ref_without_diagnostic(self) -> None:
        wake, prepared = self.wake("synthetic-success")
        opened = self.open()
        self.assertIs(True, opened["write_context_available"])
        before = self.metadata()
        bound = self.binding(opened["write_context_ref"])
        self.assertIs(True, bound["write_context_available"])
        self.assertEqual(wake["wake_id"], bound["wake_id"])
        self.assertEqual(prepared["context_hash"], bound["context_hash"])
        self.assertEqual(opened["write_context_ref"], bound["write_context_ref"])
        self.assertNotIn("binding_reason_code", bound)
        self.assertNotIn("binding_reason_code", opened)
        self.assertEqual(before, self.metadata())
        reopened = self.open()
        self.assertEqual(opened["write_context_ref"], reopened["write_context_ref"])
        self.assertEqual(before, self.metadata())

    def test_other_owner_or_model_ref_is_indistinguishable_from_unknown_ref(self) -> None:
        self.wake("synthetic-main")
        self.open()
        expected = self.binding()
        for owner, model in (
            ("synthetic-other-owner", "synthetic-other-owner-model"),
            (self.owner, "synthetic-other-model"),
        ):
            with self.subTest(namespace="synthetic-other"):
                self.wake("synthetic-other-event", owner=owner, model=model)
                other = self.open(owner=owner, model=model)
                before = self.metadata()
                denied = self.binding(other["write_context_ref"])
                self.assert_failure(denied, "write_context_not_opened_or_mismatched", "brain_open_required")
                self.assertEqual(expected, denied)
                self.assertEqual(before, self.metadata())

    def test_superseded_ref_is_not_identified_or_rebound_to_a_new_wake(self) -> None:
        self.wake("synthetic-old")
        old = self.open()
        self.wake("synthetic-new")
        unopened = self.binding(old["write_context_ref"])
        self.assert_failure(unopened, "write_context_not_opened_or_mismatched", "brain_open_required")
        new = self.open()
        denied = self.binding(old["write_context_ref"])
        self.assert_failure(denied, "write_context_not_opened_or_mismatched", "brain_open_required")
        self.assertEqual(unopened, denied)
        self.assertTrue(self.binding(new["write_context_ref"])["write_context_available"])

    def test_malformed_wrong_source_and_wrong_hash_open_records_reject(self) -> None:
        _, prepared = self.wake("synthetic-record-integrity")
        opened = self.open()
        for content in (
            "{synthetic-invalid-json",
            "[]",
            json.dumps({"opened_by": "stbrain_open_direct", "context_hash": prepared["context_hash"]}),
            json.dumps({"opened_by": "stbrain_open", "context_hash": "synthetic-private-hash"}),
        ):
            with self.subTest(record="synthetic-invalid"):
                with self.store._connect() as connection:
                    connection.execute(
                        "UPDATE brain_onboarding_artifacts SET content_json=? WHERE artifact_id=?",
                        (content, opened["write_context_ref"]),
                    )
                self.sensitive_values.add("synthetic-private-hash")
                before = self.metadata()
                self.assert_failure(
                    self.binding(opened["write_context_ref"]),
                    "write_context_binding_mismatch", "brain_open_required",
                )
                self.assertEqual(before, self.metadata())

    def test_missing_or_mismatched_injected_hash_reports_injection_failure(self) -> None:
        wake, _ = self.wake("synthetic-injected-hash")
        opened = self.open()
        for context_hash in (None, "synthetic-private-injection-hash"):
            with self.subTest(hash_state="synthetic-invalid"):
                with self.store._connect() as connection:
                    connection.execute(
                        "UPDATE brain_wake_sessions SET context_hash=? WHERE wake_id=?",
                        (context_hash, wake["wake_id"]),
                    )
                self.assert_both_fail("injected_context_required", opened["write_context_ref"])

    def test_direct_scope_and_single_use_gate_remain_authoritative(self) -> None:
        issued = self.store.issue_direct_grant(
            owner_id=self.owner, model_id=self.model,
            actor_id="synthetic-human", client_principal="synthetic-direct-client",
            request_id="synthetic-grant", requested_scopes=["learning_memory"],
        )
        self.sensitive_values.add(issued["grant_ref"])
        arguments = {
            "owner_id": self.owner, "model_id": self.model,
            "direct_grant_ref": issued["grant_ref"],
            "direct_client_principal": "synthetic-direct-client",
            "present_details": False,
        }
        opened = self.store.open_brain_context(**arguments)
        self.sensitive_values.add(opened["write_context_ref"])
        self.assertIs(True, opened["write_context_available"])
        self.assertTrue(self.binding(opened["write_context_ref"], scope="learning_memory")["write_context_available"])
        before = self.metadata()
        denied = self.binding(opened["write_context_ref"], scope="planning_memory")
        self.assert_failure(denied, None, "direct_scope_not_authorized")
        replay = self.store.open_brain_context(**arguments)
        self.assert_failure(replay, None, "direct_grant_already_used")
        self.assertEqual(before, self.metadata())
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE brain_wake_sessions SET expires_at=? WHERE wake_id=?",
                ("2000-01-01T00:00:00+00:00", opened["wake_id"]),
            )
        self.assert_failure(
            self.binding(opened["write_context_ref"], scope="learning_memory"),
            "direct_grant_expired", "direct_grant_expired",
        )


if __name__ == "__main__":
    unittest.main()
