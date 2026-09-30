"""Synthetic-only consultation restrictions; never contacts a model or live DB."""
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.execution_binding import ExecutionStore, ExecutionBindingError, canonical_hash
from runtime.execution_profiles import (
    ACTIVE_PROFILE, ARCHIVING_PROFILE, DEFAULT_PROFILE, ExecutionProfileError,
    profile_allows_tool, wake_profile,
)
from runtime.onboarding import ModuleOneOnboardingStore, OnboardingError


class ExecutionProfileTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("socket.socket", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("network forbidden")))
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="st-profile-synthetic-"))
        self.db = Path(directory) / "synthetic.sqlite3"
        original_connect = sqlite3.connect
        def connect(path, *args, **kwargs):
            if Path(path).resolve() != self.db.resolve():
                raise AssertionError("synthetic database only")
            return original_connect(path, *args, **kwargs)
        self.enterContext(patch("sqlite3.connect", side_effect=connect))
        self.common = {"owner_id": "synthetic-owner", "model_id": "synthetic-model"}
        self.secret = "synthetic-secret-not-production-00000000000"
        self.host = ModuleOneOnboardingStore(self.db, capability_secret=self.secret)
        self.store = ExecutionStore(self.db, deployment_epoch="synthetic-epoch", capability_secret=self.secret)
        self.event = dict(self.common, host_id="synthetic-host", thread_id="synthetic-thread",
                          source_kind="human_message", source_event_id="synthetic-event")
        self.schema = canonical_hash({"type": "object"})

    def prepare(self, profile, tool):
        self.wake = self.host.issue_wake(**self.event, execution_profile=profile)
        entries = [{"canonical_name": tool, "schema_hash": self.schema}]
        self.catalog_hash = canonical_hash(entries)
        result = self.host.build_pre_generation_context(
            **self.common, wake_id=self.wake["wake_id"], wake_capability=self.wake["wake_capability"],
            source_digest="synthetic-source", host_contract_digest="synthetic-host-contract",
            advertised_tools={"contract": "advertised-tools/1", "catalog_complete": True,
                              "catalog_hash": self.catalog_hash, "entries": entries})
        self.host.confirm_context_injected(**self.common, wake_id=self.wake["wake_id"],
            wake_capability=self.wake["wake_capability"], context_hash=result["context_hash"])

    def issue(self, tool, arguments):
        return self.store.issue_batch(**self.common, wake_id=self.wake["wake_id"],
            wake_capability=self.wake["wake_capability"], batch_id="synthetic-batch", revision=1,
            calls=[{"call_id": "synthetic-call", "advertised_name": tool, "canonical_tool": tool,
                    "schema_hash": self.schema, "catalog_hash": self.catalog_hash,
                    "arguments_hash": canonical_hash(arguments)}])["executions"][0]["execution_ref"]

    def test_default_preserves_old_wake_response_and_writes(self):
        self.prepare(DEFAULT_PROFILE, "remember_memory")
        self.assertNotIn("execution_profile", self.wake)
        reference = self.issue("remember_memory", {})
        claim = self.store.claim(**self.common, execution_ref=reference, tool_name="remember_memory", arguments={})
        self.store.finish(claim)

    def test_active_refuses_long_term_write_before_lease(self):
        self.prepare(ACTIVE_PROFILE, "remember_memory")
        with self.assertRaisesRegex(ExecutionBindingError, "execution_profile_denied"):
            self.issue("remember_memory", {})
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM brain_execution_calls").fetchone()[0])

    def test_active_refuses_work_write_too(self):
        self.prepare(ACTIVE_PROFILE, "remember_work_memory")
        with self.assertRaisesRegex(ExecutionBindingError, "execution_profile_denied"):
            self.issue("remember_work_memory", {})

    def test_archive_work_write_keeps_normal_claim(self):
        self.prepare(ARCHIVING_PROFILE, "remember_work_memory")
        reference = self.issue("remember_work_memory", {"content": "synthetic", "tag": "consultation"})
        claim = self.store.claim(**self.common, execution_ref=reference, tool_name="remember_work_memory",
                                 arguments={"content": "synthetic", "tag": "consultation"})
        self.store.finish(claim)

    def test_archive_cannot_modify_self_model(self):
        self.prepare(ARCHIVING_PROFILE, "activate_self_model_candidate")
        with self.assertRaisesRegex(ExecutionBindingError, "execution_profile_denied"):
            self.issue("activate_self_model_candidate", {})

    def test_read_open_view_allowed(self):
        self.prepare(ACTIVE_PROFILE, "stbrain_open")
        reference = self.issue("stbrain_open", {"view": "recall"})
        claim = self.store.claim(**self.common, execution_ref=reference, tool_name="stbrain_open", arguments={"view": "recall"})
        self.store.finish(claim)

    def test_default_open_view_not_allowed_at_claim(self):
        self.prepare(ACTIVE_PROFILE, "stbrain_open")
        reference = self.issue("stbrain_open", {})
        with self.assertRaisesRegex(ExecutionBindingError, "execution_profile_denied"):
            self.store.claim(**self.common, execution_ref=reference, tool_name="stbrain_open", arguments={})
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual("issued", connection.execute("SELECT status FROM brain_execution_calls").fetchone()[0])

    def test_same_event_cannot_upgrade_or_drop_profile(self):
        first = self.host.issue_wake(**self.event, execution_profile=ACTIVE_PROFILE)
        same = self.host.issue_wake(**self.event, execution_profile=ACTIVE_PROFILE)
        self.assertEqual(first["wake_id"], same["wake_id"])
        self.assertTrue(same["reused"])
        for profile in (ARCHIVING_PROFILE, DEFAULT_PROFILE):
            with self.assertRaisesRegex(OnboardingError, "execution_profile_conflict"):
                self.host.issue_wake(**self.event, execution_profile=profile)

    def test_default_event_cannot_be_relabelled(self):
        self.host.issue_wake(**self.event)
        with self.assertRaisesRegex(OnboardingError, "execution_profile_conflict"):
            self.host.issue_wake(**self.event, execution_profile=ACTIVE_PROFILE)

    def test_restart_preserves_profile(self):
        original = self.host.issue_wake(**self.event, execution_profile=ACTIVE_PROFILE)
        reopened = ModuleOneOnboardingStore(self.db, capability_secret=self.secret)
        self.assertEqual(ACTIVE_PROFILE, reopened.issue_wake(**self.event, execution_profile=ACTIVE_PROFILE)["execution_profile"])
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(ACTIVE_PROFILE, wake_profile(connection, original["wake_id"]))

    def test_invalid_profile_creates_no_wake(self):
        for invalid in (None, "", "full", " default", {}, 0):
            with self.assertRaises(ExecutionProfileError):
                self.host.issue_wake(**self.event, execution_profile=invalid)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM brain_wake_sessions").fetchone()[0])

    def test_unknown_and_compact_names_fail_closed(self):
        for name in ("stbrain_manage", "remember_memory", "new_future_tool", "read_anything", "open_hallucination_vault"):
            self.assertFalse(profile_allows_tool(ACTIVE_PROFILE, name, {}))
        self.assertTrue(profile_allows_tool(ACTIVE_PROFILE, "recall_work_memory", {}))


if __name__ == "__main__":
    unittest.main()
