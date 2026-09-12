"""Synthetic-only optional advice tests: no server, model call or private DB."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mcp_server.authoring_service import AuthoringRewriteAccessService
from runtime.authoring import AuthoringRewriteStore
from runtime.execution_binding import ExecutionBindingError
from runtime.onboarding import ModuleOneOnboardingStore
from runtime.ordinary_access import authenticated_ordinary_operation
from runtime.person_reference_advisory import (
    PERSON_REFERENCE_ADVISORY_DEFAULT, PersonReferenceAdvisoryError,
    PersonReferenceAdvisoryStore, initialize_person_reference_advisory_schema,
)
from tests import test_onboarding as onboarding_fixture
from tests import test_execution_binding as execution_fixture


class PersonReferenceAdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix="person-advisory-synthetic-"))
        self.database = Path(self.temp) / "synthetic.sqlite3"
        for target in ("socket.socket", "socket.create_connection", "subprocess.Popen"):
            self.enterContext(patch(target, side_effect=AssertionError("offline synthetic test")))
        real_connect = sqlite3.connect

        def synthetic_connect(path, *args, **kwargs):
            if not Path(path).resolve().is_relative_to(Path(self.temp).resolve()):
                raise AssertionError("only isolated synthetic databases allowed")
            return real_connect(path, *args, **kwargs)

        self.enterContext(patch("sqlite3.connect", side_effect=synthetic_connect))
        self.store = PersonReferenceAdvisoryStore(self.database)
        self.identity = {"owner_id": "synthetic-owner", "model_id": "synthetic-model"}

    def operation(self, scope="shared_person_authoring", **identity):
        return authenticated_ordinary_operation(**{**self.identity, **identity}, scope=scope)

    def change(self, action, text=None):
        with self.operation():
            return self.store.manage(**self.identity, action=action, text=text)

    def rows(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute("SELECT * FROM person_reference_advisory_history ORDER BY change_id").fetchall()

    def test_default_read_is_optional_read_only_and_independent_of_rewrite(self):
        first = self.store.read(**self.identity)
        self.assertEqual(first, self.store.read(**self.identity))
        self.assertEqual([], self.rows())
        self.assertTrue(first["enabled"])
        self.assertTrue(first["optional"])
        self.assertEqual(PERSON_REFERENCE_ADVISORY_DEFAULT, first["message"])
        self.assertEqual("host_advisory", first["source"])
        self.assertEqual("manage_person_reference_advisory", first["manage_tool"])
        self.assertFalse(first["rewrite_assist_default"])
        self.assertEqual(0, first["history"]["change_count"])

    def test_set_disable_reset_persist_and_hide_old_prose_without_versions(self):
        authored = "写故事时我自选第三人称；整理共同经历时我愿意写清角色名字。"
        set_result = self.change("set", authored)
        self.assertEqual("saved", set_result["decision"])
        self.assertEqual(authored, set_result["authoring_advisory"]["message"])
        self.assertEqual("ai_authored", set_result["authoring_advisory"]["source"])
        disabled = self.change("disable")
        self.assertFalse(disabled["authoring_advisory"]["enabled"])
        self.assertNotIn("message", disabled["authoring_advisory"])
        self.assertNotIn(authored, json.dumps(disabled, ensure_ascii=False))
        self.assertNotIn(PERSON_REFERENCE_ADVISORY_DEFAULT, json.dumps(disabled, ensure_ascii=False))
        self.store = PersonReferenceAdvisoryStore(self.database)
        self.assertFalse(self.store.read(**self.identity)["enabled"])
        reset = self.change("reset")
        self.assertEqual(PERSON_REFERENCE_ADVISORY_DEFAULT, reset["authoring_advisory"]["message"])
        self.assertNotIn(authored, json.dumps(reset, ensure_ascii=False))
        self.assertEqual(["set", "disable", "reset"], [row[3] for row in self.rows()])
        self.assertEqual(3, reset["authoring_advisory"]["history"]["change_count"])

    def test_preference_is_owner_and_model_scoped(self):
        self.change("set", "只有这个主体自己的提示。")
        for identity in ({"owner_id": "other-owner"}, {"model_id": "other-model"}):
            other = self.store.read(**{**self.identity, **identity})
            self.assertEqual(PERSON_REFERENCE_ADVISORY_DEFAULT, other["message"])
            self.assertEqual(0, other["history"]["change_count"])
            with self.operation(**identity):
                with self.assertRaises(PersonReferenceAdvisoryError):
                    self.store.manage(**self.identity, action="disable")
        self.assertEqual(1, len(self.rows()))

    def test_unauthenticated_wrong_scope_wrong_ref_and_forged_binding_cannot_write(self):
        with self.assertRaises(PersonReferenceAdvisoryError):
            self.store.manage(**self.identity, action="disable")
        with self.operation("learning_memory"):
            with self.assertRaises(PersonReferenceAdvisoryError):
                self.store.manage(**self.identity, action="disable")
        with self.operation():
            with self.assertRaisesRegex(PersonReferenceAdvisoryError, "binding_mismatch"):
                self.store.manage(**self.identity, action="disable", write_context_ref="invented")
        with self.assertRaises(PersonReferenceAdvisoryError):
            self.store.manage(**self.identity, action="disable", write_context_ref="invented",
                              onboarding={"write_context_available": True})
        self.assertEqual([], self.rows())

    def test_text_validation_preserves_free_narrative_person_and_reports_limit(self):
        for text, reason in ((None, "required"), ("  ", "required"), ("文" * 2001, "max_2000"),
                             ("password=synthetic_value", "credential_or_secret")):
            with self.subTest(reason=reason), self.operation():
                with self.assertRaisesRegex(PersonReferenceAdvisoryError, reason):
                    self.store.manage(**self.identity, action="set", text=text)
        with self.operation():
            for action in ("disable", "reset"):
                with self.assertRaisesRegex(PersonReferenceAdvisoryError, "text_only_allowed"):
                    self.store.manage(**self.identity, action=action, text="正文")
            with self.assertRaisesRegex(PersonReferenceAdvisoryError, "invalid_person_reference"):
                self.store.manage(**self.identity, action="other")
        exact = "她" * 2000
        result = self.change("set", exact)
        self.assertEqual(exact, result["authoring_advisory"]["message"])
        self.assertEqual(1, len(self.rows()))

    def test_history_is_append_only_even_for_direct_sql(self):
        self.change("set", "我自己写的提示。")
        before = self.rows()
        with closing(sqlite3.connect(self.database)) as connection:
            for statement in ("UPDATE person_reference_advisory_history SET advisory_text='changed'",
                              "DELETE FROM person_reference_advisory_history"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append_only"):
                    connection.execute(statement)
        self.assertEqual(before, self.rows())

    def test_schema_helper_has_no_implicit_commit_and_preserves_old_tables(self):
        other = Path(self.temp) / "schema-only.sqlite3"
        with closing(sqlite3.connect(other)) as connection:
            connection.execute("CREATE TABLE existing_data (value TEXT)")
            connection.execute("INSERT INTO existing_data(rowid,value) VALUES (73,'keep')")
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            initialize_person_reference_advisory_schema(connection)
            self.assertTrue(connection.in_transaction)
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM person_reference_advisory_history").fetchone()[0])
            connection.rollback()
            self.assertEqual([], connection.execute("SELECT name FROM sqlite_master WHERE name LIKE '%person_reference_advisory%'").fetchall())
            self.assertEqual([(73, "keep")], connection.execute("SELECT rowid,value FROM existing_data").fetchall())

    def test_execution_check_at_transaction_end_rolls_back_insert(self):
        real_assert = __import__("runtime.person_reference_advisory", fromlist=["assert_bound_execution"]).assert_bound_execution

        def reject_after_insert(connection):
            real_assert(connection)
            if connection.execute("SELECT COUNT(*) FROM person_reference_advisory_history").fetchone()[0]:
                raise ExecutionBindingError("execution_claim_not_current")

        with self.operation(), patch("runtime.person_reference_advisory.assert_bound_execution", side_effect=reject_after_insert):
            with self.assertRaisesRegex(ExecutionBindingError, "execution_claim_not_current"):
                self.store.manage(**self.identity, action="set", text="这条必须回滚。")
        self.assertEqual([], self.rows())

    def test_service_manual_current_text_disable_and_rewrite_state_independence(self):
        onboarding = ModuleOneOnboardingStore(self.database, capability_secret=b"synthetic-advisory-secret-0000000000000")
        rewrite = AuthoringRewriteStore(self.database, receipt_secret=b"synthetic-rewrite-secret-0000000000000")
        service = AuthoringRewriteAccessService(rewrite, onboarding=onboarding, **self.identity)
        before = service.status()
        self.assertEqual(PERSON_REFERENCE_ADVISORY_DEFAULT, service.manual()["authoring_advisory"]["message"])
        with self.operation():
            set_result = service.manage_advisory(action="set", text="我可以自己保留第三人称。")
        self.assertEqual("saved", set_result["decision"])
        self.assertEqual("我可以自己保留第三人称。", service.manual()["authoring_advisory"]["message"])
        with self.operation():
            disabled = service.manage_advisory(action="disable")
        self.assertEqual("saved", disabled["decision"])
        self.assertNotIn("message", service.manual()["authoring_advisory"])
        self.assertEqual(before, service.status())
        with self.operation():
            rejected = service.manage_advisory(action="set", text="密码=synthetic_value")
        self.assertEqual(["credential_or_secret_detected"], rejected["reason_codes"])
        self.assertEqual(2, len(self.rows()))


class PersonReferenceAdvisoryLegacyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = onboarding_fixture.ModuleOneOnboardingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("offline synthetic test")))
        self.onboarding = self.fixture.store
        self.identity = {"owner_id": self.fixture.owner, "model_id": self.fixture.model}
        self.store = PersonReferenceAdvisoryStore(self.fixture.database)

    def test_real_open_context_can_write_and_superseded_context_cannot(self):
        self.fixture.bootstrap_live()
        opened = self.fixture.open_brain()
        arguments = {**self.identity, "write_context_ref": opened["write_context_ref"], "onboarding": self.onboarding}
        result = self.store.manage(**arguments, action="set", text="我会用自己选择的叙事方式。")
        self.assertEqual("saved", result["decision"])
        self.fixture.wake("synthetic-new-wake")
        with self.assertRaisesRegex(PersonReferenceAdvisoryError, "brain_open_required"):
            self.store.manage(**arguments, action="disable")
        self.assertEqual(1, self.store.read(**self.identity)["history"]["change_count"])

    def test_unactivated_and_issued_capability_are_rejected(self):
        wake, _ = self.fixture.wake("synthetic-first-wake")
        opened = self.fixture.open_brain()
        with self.assertRaisesRegex(PersonReferenceAdvisoryError, "module_one_required"):
            self.store.manage(**self.identity, action="disable", onboarding=self.onboarding,
                              write_context_ref=opened["write_context_ref"])
        with authenticated_ordinary_operation(**self.identity, scope="shared_person_authoring"):
            with self.assertRaisesRegex(PersonReferenceAdvisoryError, "credential_or_secret_detected"):
                self.store.manage(**self.identity, action="set", text="提到 " + wake["wake_capability"],
                                  onboarding=self.onboarding)
        self.assertEqual(0, self.store.read(**self.identity)["history"]["change_count"])

    def test_real_direct_grant_scope_and_revocation_are_enforced(self):
        self.fixture.bootstrap_live()

        def open_direct(request_id, scopes):
            issued = self.onboarding.issue_direct_grant(
                **self.identity, actor_id="synthetic-human", client_principal="synthetic-direct-client",
                request_id=request_id, requested_scopes=scopes)
            return self.onboarding.open_brain_context(
                **self.identity, direct_grant_ref=issued["grant_ref"],
                direct_client_principal="synthetic-direct-client")

        opened = open_direct("synthetic-authoring-direct", ["shared_person_authoring"])
        arguments = {**self.identity, "write_context_ref": opened["write_context_ref"],
                     "onboarding": self.onboarding}
        result = self.store.manage(**arguments, action="set", text="我自主选用清楚的人物指代。")
        self.assertEqual("saved", result["decision"])

        original_binding = self.store._legacy_binding

        def revoke_after_validation(**fields):
            binding = original_binding(**fields)
            with closing(sqlite3.connect(self.fixture.database)) as connection:
                connection.execute("UPDATE brain_direct_grants SET status='closed' WHERE opened_wake_id=?",
                                   (binding["wake_id"],))
                connection.commit()
            return binding

        with patch.object(self.store, "_legacy_binding", side_effect=revoke_after_validation):
            with self.assertRaisesRegex(PersonReferenceAdvisoryError, "brain_open_required"):
                self.store.manage(**arguments, action="disable")
        self.assertTrue(self.store.read(**self.identity)["enabled"])
        wrong_scope = open_direct("synthetic-learning-only-direct", ["learning_memory"])
        with self.assertRaisesRegex(PersonReferenceAdvisoryError, "brain_open_required"):
            self.store.manage(**self.identity, action="disable", onboarding=self.onboarding,
                              write_context_ref=wrong_scope["write_context_ref"])
        self.assertEqual(1, self.store.read(**self.identity)["history"]["change_count"])


class PersonReferenceAdvisoryExecutionTests(unittest.TestCase):
    def test_actual_completed_execution_and_wrong_identity_are_rejected(self):
        fixture = execution_fixture.ExecutionBindingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store = PersonReferenceAdvisoryStore(fixture.database)
        claim = fixture.claim(fixture.issue()[0])
        with fixture.store.bind(claim):
            with self.assertRaisesRegex(ExecutionBindingError, "execution_owner_mismatch"):
                store.read(owner_id="other-owner", model_id=fixture.common["model_id"])
            with authenticated_ordinary_operation(**fixture.common, scope="shared_person_authoring", claim=claim):
                self.assertEqual("saved", store.manage(**fixture.common, action="disable")["decision"])
        fixture.store.finish(claim)
        with fixture.store.bind(claim):
            with authenticated_ordinary_operation(**fixture.common, scope="shared_person_authoring", claim=claim):
                with self.assertRaisesRegex(ExecutionBindingError, "execution_claim_not_current"):
                    store.manage(**fixture.common, action="reset")
        self.assertEqual(1, store.read(**fixture.common)["history"]["change_count"])


if __name__ == "__main__":
    unittest.main()
