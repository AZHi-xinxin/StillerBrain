import base64
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from mcp_server.memory_image_service import MemoryImageAccessService
from runtime.memory_images import MemoryImageStore
from runtime.ordinary_access import authenticated_ordinary_operation
from tests.test_atlas_metadata import create_runtime_database
from tests.test_memory_relations import seed_endpoints, OWNER
from tests.test_memory_images import synthetic_image
from tests import test_onboarding as onboarding_fixture


class MemoryImageServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = onboarding_fixture.ModuleOneOnboardingTests(); self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        self.fixture.owner, self.fixture.model = OWNER.values()
        self.database, self.onboarding = self.fixture.database, self.fixture.store
        create_runtime_database(self.database); seed_endpoints(self.database)
        self.store = MemoryImageStore(self.database)
        self.service = MemoryImageAccessService(self.store, onboarding=self.onboarding, **OWNER,
            protected_values=("synthetic-protected-long-key-value-12345",))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))

    def ordinary(self, scope="emotional_memory", **identity):
        return authenticated_ordinary_operation(**{**OWNER, **identity}, scope=scope)

    def put(self, **changes):
        return self.service.put(**{"target_ref": "emotion://e1@1", "description": "Synthetic picture",
            "mime_type": "image/png", "data_base64": base64.b64encode(synthetic_image()).decode(), **changes})

    def test_activation_required_and_scoped_read_before_activation(self):
        with self.ordinary():
            self.assertEqual("module_one_required", self.put()["reason_code"])
            self.assertEqual([], self.service.list("emotion://e1@1")["images"])

    def test_wrong_owner_scope_and_unbound_write_rejected(self):
        self.fixture.bootstrap_live()
        self.assertEqual("execution_binding_required", self.put()["reason_code"])
        for changes in ({"scope": "learning_memory"}, {"owner_id": "foreign"}, {"model_id": "foreign"}):
            with self.ordinary(**changes): self.assertEqual("execution_binding_required", self.put()["reason_code"])
        with self.ordinary():
            self.assertEqual("write_context_binding_mismatch", self.put(write_context_ref="wrong")["reason_code"])

    def test_metadata_and_bytes_are_explicit_and_idempotent_across_calls(self):
        self.fixture.bootstrap_live()
        with self.ordinary(): saved = self.put()
        with self.ordinary():
            retry = self.put()
            self.assertEqual(saved["image_ref"], retry["image_ref"]); self.assertFalse(retry["state_changed"])
            self.assertNotIn("data_uri", self.service.read("emotion://e1@1", saved["image_ref"]))
            self.assertIn("data_uri", self.service.read("emotion://e1@1", saved["image_ref"], True))
            self.assertEqual("removed", self.service.remove("emotion://e1@1", saved["image_ref"])["decision"])
            self.assertEqual("removed", self.put()["decision"])

    def test_secret_description_rejected_without_echo_or_image_ocr_claim(self):
        self.fixture.bootstrap_live()
        with self.ordinary():
            result = self.put(description="synthetic-protected-long-key-value-12345")
            self.assertEqual("credential_or_secret_detected", result["reason_code"])
            self.assertNotIn("synthetic-protected", str(result))

    def test_legacy_direct_requires_target_original_module(self):
        self.fixture.bootstrap_live()
        def opened(scopes, suffix):
            grant = self.onboarding.issue_direct_grant(**OWNER, actor_id="synthetic-human", client_principal="synthetic-client",
                request_id="synthetic-image-" + suffix, requested_scopes=scopes)
            return self.onboarding.open_brain_context(**OWNER, direct_grant_ref=grant["grant_ref"], direct_client_principal="synthetic-client")
        wrong = opened(["learning_memory"], "wrong")
        self.assertEqual("image_scope_not_authorized", self.put(write_context_ref=wrong["write_context_ref"])["reason_code"])
        right = opened(["emotional_memory"], "right")
        self.assertEqual("stored", self.put(write_context_ref=right["write_context_ref"])["decision"])

    def test_activation_revoked_between_binding_and_transaction_does_not_store(self):
        self.fixture.bootstrap_live()
        original = self.store.store
        def race(**kwargs):
            with closing(sqlite3.connect(self.database)) as db:
                db.execute("UPDATE brain_module_unlocks SET unlocked=0"); db.commit()
            return original(**kwargs)
        with self.ordinary(), patch.object(self.store, "store", side_effect=race):
            self.assertEqual("module_one_required", self.put()["reason_code"])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM memory_image_items").fetchone()[0])


if __name__ == "__main__": unittest.main()
