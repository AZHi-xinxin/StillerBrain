"""Synthetic-only lease, exact-wake and cancellation fencing tests."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from runtime.execution_binding import ExecutionBindingError, ExecutionStore, canonical_hash
from runtime.onboarding import ModuleOneOnboardingStore


class ExecutionBindingTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection"):
            self.enterContext(patch(target, side_effect=AssertionError("network forbidden")))
        temp = self.enterContext(tempfile.TemporaryDirectory(prefix="execution-binding-synthetic-"))
        self.database = (Path(temp) / "synthetic.sqlite3").resolve()
        connect = sqlite3.connect
        def safe_connect(path, *args, **kwargs):
            if Path(path).resolve() != self.database:
                raise AssertionError("only synthetic DB allowed")
            return connect(path, *args, **kwargs)
        self.enterContext(patch("sqlite3.connect", side_effect=safe_connect))
        self.common = {"owner_id": "synthetic-owner", "model_id": "synthetic-model"}
        self.secret = b"synthetic-execution-secret-0000000000000000"
        self.onboarding = ModuleOneOnboardingStore(self.database, capability_secret=self.secret)
        self.store = ExecutionStore(self.database, deployment_epoch="synthetic-epoch", capability_secret=self.secret)
        self.schema_hash = canonical_hash({"type": "object"})
        entries = [{"canonical_name": "stbrain_open", "schema_hash": self.schema_hash}]
        self.catalog = {"contract": "advertised-tools/1", "catalog_complete": True,
                        "catalog_hash": canonical_hash(entries), "entries": entries}
        self.wake = self.new_wake("synthetic-A")
        self.batch = {**self.common, "wake_id": self.wake["wake_id"],
                      "wake_capability": self.wake["wake_capability"], "batch_id": "synthetic-batch", "revision": 1}

    def new_wake(self, event):
        wake = self.onboarding.issue_wake(**self.common, host_id="synthetic-host", thread_id="synthetic-thread",
                                         source_kind="human_message", source_event_id=event)
        prepared = self.onboarding.build_pre_generation_context(
            **self.common, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
            source_digest="synthetic-source", host_contract_digest="synthetic-contract", advertised_tools=self.catalog)
        self.onboarding.confirm_context_injected(**self.common, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], context_hash=prepared["context_hash"])
        return wake

    def issue(self, count=1):
        result = self.store.issue_batch(**self.batch, calls=[
            {"call_id": f"synthetic-call-{index}", "advertised_name": "stbrain_open",
             "canonical_tool": "stbrain_open", "schema_hash": self.schema_hash,
             "catalog_hash": self.catalog["catalog_hash"], "arguments_hash": canonical_hash({})}
            for index in range(count)])
        return [item["execution_ref"] for item in result["executions"]]

    def claim(self, ref):
        return self.store.claim(**self.common, execution_ref=ref, tool_name="stbrain_open", arguments={})

    def close(self):
        return self.onboarding.close_context_snapshot(**self.common, wake_id=self.wake["wake_id"],
                                                       wake_capability=self.wake["wake_capability"])

    def test_issue_claim_exact_open_and_finish(self):
        ref = self.issue()[0]
        claim = self.claim(ref)
        with self.store.bind(claim):
            opened = self.onboarding.open_brain_context(**self.common, present_details=False,
                                                       expected_wake_id=claim.wake_id)
            self.assertTrue(opened["write_context_available"])
            bound = self.onboarding.current_open_write_context(**self.common, write_context_ref=opened["write_context_ref"])
            self.assertEqual(self.wake["wake_id"], bound["wake_id"])
        self.store.finish(claim)
        self.assertEqual(1, self.store.batch_status(**self.batch)["counts"]["completed"])

    def test_running_blocks_close_and_replacement(self):
        claim = self.claim(self.issue()[0])
        for action in (self.close, lambda: self.new_wake("synthetic-B")):
            with self.assertRaisesRegex(ExecutionBindingError, "execution_calls_running"):
                action()
        self.store.finish(claim)
        self.assertEqual("closed", self.close()["decision"])

    def test_revoke_waits_for_running_and_revokes_unclaimed(self):
        refs = self.issue(2)
        claim = self.claim(refs[0])
        status = self.store.revoke_batch(**self.batch)
        self.assertEqual("cancelling", status["batch_status"])
        self.assertEqual(1, status["counts"]["running"])
        self.assertEqual(1, status["counts"]["revoked"])
        with self.assertRaisesRegex(ExecutionBindingError, "execution_not_available"):
            self.claim(refs[1])
        with self.store.bind(claim):
            self.assertTrue(self.onboarding.open_brain_context(**self.common, present_details=False)["write_context_available"])
        self.store.finish(claim)
        self.assertEqual("closed", self.store.revoke_batch(**self.batch)["batch_status"])
        self.close()
        self.new_wake("synthetic-B-after-drain")
        with self.assertRaises(ExecutionBindingError):
            self.claim(refs[1])

    def test_cancelled_A_cannot_open_B(self):
        ref = self.issue()[0]
        self.store.revoke_batch(**self.batch)
        self.close()
        self.new_wake("synthetic-B")
        with self.assertRaises(ExecutionBindingError):
            self.claim(ref)
        result = self.onboarding.open_brain_context(**self.common, present_details=False,
                                                    expected_wake_id=self.wake["wake_id"])
        self.assertFalse(result["write_context_available"])

    def test_expired_long_running_claim_drains_and_recovery_can_close(self):
        refs = self.issue(2)
        claim = self.claim(refs[0])
        # Move the wake expiry back over 31 minutes without sleeping or touching
        # clocks/configuration outside this single synthetic database.
        expired = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        with self.store._connect() as connection:
            connection.execute("UPDATE brain_wake_sessions SET expires_at=? WHERE wake_id=?",
                               (expired, self.wake["wake_id"]))
        self.assertEqual(1, self.store.batch_status(**self.batch)["counts"]["running"])
        with self.assertRaisesRegex(ExecutionBindingError, "execution_wake_expired"):
            self.claim(refs[1])
        with self.assertRaisesRegex(ExecutionBindingError, "execution_wake_expired"):
            self.issue(2)
        for action in (self.close, lambda: self.new_wake("synthetic-B-before-expired-drain")):
            with self.assertRaisesRegex(ExecutionBindingError, "execution_calls_running"):
                action()
        status = self.store.revoke_batch(**self.batch)
        self.assertEqual("cancelling", status["batch_status"])
        self.assertEqual(1, status["counts"]["running"])
        self.assertEqual(1, status["counts"]["revoked"])
        self.store.finish(claim)
        self.assertEqual(0, self.store.batch_status(**self.batch)["counts"]["running"])
        self.assertEqual("closed", self.store.revoke_batch(**self.batch)["batch_status"])
        self.assertEqual("closed", self.close()["decision"])
        # Close remains idempotent, while neither old execution can claim the
        # new wake or receive a new lease after expiry.
        self.assertEqual("closed", self.close()["decision"])
        self.new_wake("synthetic-B-after-expired-drain")
        for ref in refs:
            with self.assertRaises(ExecutionBindingError):
                self.claim(ref)

    def test_expired_cleanup_keeps_exact_identity_capability_and_revision(self):
        self.issue()
        with self.store._connect() as connection:
            connection.execute("UPDATE brain_wake_sessions SET expires_at=? WHERE wake_id=?",
                ((datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(), self.wake["wake_id"]))
        for change in ({"owner_id": "synthetic-other"}, {"model_id": "synthetic-other"},
                       {"wake_capability": "synthetic-wrong"}, {"revision": 2},
                       {"batch_id": "synthetic-other"}):
            for action in (self.store.batch_status, self.store.revoke_batch):
                with self.assertRaises(ExecutionBindingError):
                    action(**{**self.batch, **change})
        self.assertEqual(1, self.store.batch_status(**self.batch)["counts"]["issued"])
        wrong_close = self.onboarding.close_context_snapshot(**self.common,
            wake_id=self.wake["wake_id"], wake_capability="synthetic-wrong")
        self.assertNotEqual("closed", wrong_close["decision"])

    def test_mismatched_arguments_name_owner_and_epoch_are_rejected(self):
        ref = self.issue()[0]
        for changed in ({"arguments": {"view": "manual"}}, {"tool_name": "query_self_model"},
                        {"owner_id": "synthetic-other"}):
            with self.assertRaisesRegex(ExecutionBindingError, "execution_binding_mismatch"):
                self.store.claim(**{**self.common, "execution_ref": ref, "tool_name": "stbrain_open", "arguments": {}, **changed})
        other = ExecutionStore(self.database, deployment_epoch="synthetic-next-epoch", capability_secret=self.secret)
        with self.assertRaisesRegex(ExecutionBindingError, "execution_binding_mismatch"):
            other.claim(**self.common, execution_ref=ref, tool_name="stbrain_open", arguments={})

    def test_no_replay_after_success_or_failure(self):
        ref = self.issue()[0]
        claim = self.claim(ref)
        with self.assertRaises(ExecutionBindingError):
            self.claim(ref)
        self.store.finish(claim, failed=True)
        self.store.finish(claim)
        with self.assertRaises(ExecutionBindingError):
            self.claim(ref)
        self.assertEqual(1, self.store.batch_status(**self.batch)["counts"]["failed"])

    def test_issue_is_idempotent_but_changed_batch_rejected(self):
        self.assertEqual(self.issue(), self.issue())
        with self.assertRaisesRegex(ExecutionBindingError, "execution_batch_conflict"):
            self.issue(2)

    def test_wrong_cancel_revision_does_not_revoke(self):
        self.issue()
        with self.assertRaises(ExecutionBindingError):
            self.store.revoke_batch(**{**self.batch, "revision": 2})
        self.assertEqual(1, self.store.batch_status(**self.batch)["counts"]["issued"])

    def test_missing_or_embedded_ref_in_business_args_rejected(self):
        for ref in (None, "", "stexec_fake"):
            with self.assertRaises(ExecutionBindingError):
                self.claim(ref)
        ref = self.issue()[0]
        with self.assertRaisesRegex(ExecutionBindingError, "execution_arguments_invalid"):
            self.store.claim(**self.common, execution_ref=ref, tool_name="stbrain_open", arguments={"execution_ref": ref})

    def test_claim_context_does_not_leak_after_exit(self):
        claim = self.claim(self.issue()[0])
        with self.store.bind(claim):
            with self.assertRaises(ExecutionBindingError):
                self.onboarding.open_brain_context(**self.common, expected_wake_id="synthetic-wrong")
        self.store.finish(claim)
        self.assertTrue(self.onboarding.open_brain_context(**self.common, present_details=False)["write_context_available"])

    def test_epoch_constructor_does_not_implicitly_retire_running(self):
        refs = self.issue(2)
        claim = self.claim(refs[0])
        new = ExecutionStore(self.database, deployment_epoch="synthetic-next-epoch", capability_secret=self.secret)
        with self.assertRaisesRegex(ExecutionBindingError, "execution_calls_running"):
            self.new_wake("synthetic-B-still-blocked")
        result = new.retire_stopped_epochs(epochs=["synthetic-epoch"], stop_receipt_sha256="a" * 64)
        self.assertEqual({"revoked": 1, "orphaned": 1, "closed_batches": 1}, result["counts"])
        for ref in refs:
            with self.assertRaises(ExecutionBindingError):
                self.claim(ref)
        with self.store.bind(claim):
            with self.assertRaises(ExecutionBindingError):
                self.onboarding.open_brain_context(**self.common, present_details=False)
        self.store.finish(claim)
        self.assertEqual(1, self.store.batch_status(**self.batch)["counts"]["orphaned"])

    def test_epoch_retirement_rejects_current_epoch_and_bad_receipt(self):
        self.issue()
        for args in ({"epochs": ["synthetic-epoch"], "stop_receipt_sha256": "a" * 64},
                     {"epochs": ["synthetic-old"], "stop_receipt_sha256": "bad"}):
            with self.assertRaises(ExecutionBindingError):
                self.store.retire_stopped_epochs(**args)
        self.assertEqual(1, self.store.batch_status(**self.batch)["counts"]["issued"])

    def test_execution_reference_cannot_be_persisted_even_for_other_namespace(self):
        ref = self.issue()[0]
        for value in (ref, {"nested": ["text " + ref + " text"]}, {ref: "key"}):
            self.assertTrue(self.onboarding.contains_protected_persistence_value(
                owner_id="synthetic-other", model_id="synthetic-other", value=value))
        self.assertFalse(self.onboarding.contains_protected_persistence_value(
            **self.common, value="Documentation names the stexec_ prefix without a token."))


if __name__ == "__main__":
    unittest.main()
