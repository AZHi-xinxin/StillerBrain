"""Host staging grants no authority and preserves the existing final write gates."""
import base64
from contextlib import closing
import hashlib
import sqlite3
import unittest
from unittest.mock import patch

from mcp_server.memory_image_service import MemoryImageAccessService
from runtime.memory_images import MemoryImageStore
from runtime.memory_image_uploads import MemoryImageUploads, UPLOAD_CONTRACT
from runtime.ordinary_access import authenticated_ordinary_operation
from tests.test_atlas_metadata import create_runtime_database
from tests.test_memory_relations import seed_endpoints, OWNER
from tests.test_memory_images import synthetic_image
from tests import test_onboarding as onboarding_fixture


class MemoryImageUploadServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = onboarding_fixture.ModuleOneOnboardingTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        self.fixture.owner, self.fixture.model = OWNER.values()
        self.database, self.onboarding = self.fixture.database, self.fixture.store
        create_runtime_database(self.database); seed_endpoints(self.database)
        self.now = 0.0
        self.uploads = MemoryImageUploads(clock=lambda: self.now)
        self.service = MemoryImageAccessService(MemoryImageStore(self.database), onboarding=self.onboarding,
            **OWNER, uploads=self.uploads)
        self.raw = synthetic_image()
        self.ref = "stimgup_" + "a" * 64
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))

    def stage(self, **changes):
        return self.service.stage_upload(**{"protocol": UPLOAD_CONTRACT, "upload_ref": self.ref,
            "mime_type": "image/png", "sha256": hashlib.sha256(self.raw).hexdigest(),
            "data_base64": base64.b64encode(self.raw).decode("ascii"), **changes})

    def put(self, **changes):
        return self.service.put(**{"target_ref": "emotion://e1@1", "description": "Synthetic host original",
            "mime_type": "image/png", "upload_ref": self.ref, **changes})

    def count(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute("SELECT count(*) FROM memory_image_items").fetchone()[0]

    def ordinary(self):
        return authenticated_ordinary_operation(**OWNER, scope="emotional_memory")

    def test_staging_before_activation_does_not_grant_or_write(self):
        before = self.database.read_bytes()
        receipt = self.stage()
        self.assertEqual("staged", receipt["decision"])
        self.assertFalse(receipt["memory_written"])
        self.assertFalse(receipt["write_authority_granted"])
        self.assertEqual(before, self.database.read_bytes())
        with self.ordinary(): self.assertEqual("module_one_required", self.put()["reason_code"])
        self.assertEqual(0, self.count())

    def test_active_module_still_requires_real_context(self):
        self.fixture.bootstrap_live(); self.stage()
        self.assertEqual("execution_binding_required", self.put()["reason_code"])
        self.assertEqual(0, self.count())
        with self.ordinary():
            stored = self.put()
            self.assertEqual("stored", stored["decision"])
            read = self.service.read("emotion://e1@1", stored["image_ref"], True)
        self.assertEqual(self.raw, base64.b64decode(read["data_uri"].split(",", 1)[1]))

    def test_xor_source_mime_and_expiration_rejected_without_writes(self):
        self.fixture.bootstrap_live(); self.stage()
        cases = [({"upload_ref": None}, "invalid_image_source"),
                 ({"data_base64": base64.b64encode(self.raw).decode()}, "invalid_image_source"),
                 ({"mime_type": "image/jpeg"}, "image_upload_mime_mismatch"),
                 ({"upload_ref": "stimgup_" + "b" * 64}, "image_upload_not_available")]
        with self.ordinary():
            for changes, code in cases:
                with self.subTest(code=code): self.assertEqual(code, self.put(**changes)["reason_code"])
            self.now = 900
            self.assertEqual("image_upload_not_available", self.put()["reason_code"])
        self.assertEqual(0, self.count())

    def test_legacy_bytes_and_staged_retry_share_idempotency_without_reviving_removed(self):
        self.fixture.bootstrap_live(); self.stage()
        with self.ordinary():
            first = self.put()
            retry = self.put()
            old_client = self.put(upload_ref=None, data_base64=base64.b64encode(self.raw).decode())
            self.assertEqual(first["image_ref"], retry["image_ref"])
            self.assertEqual(first["image_ref"], old_client["image_ref"])
            self.assertFalse(retry["state_changed"])
            self.assertEqual("removed", self.service.remove("emotion://e1@1", first["image_ref"])["decision"])
            self.assertEqual("removed", self.put()["decision"])
        self.assertEqual(1, self.count())

    def test_staging_fixed_failure_does_not_echo_original(self):
        result = self.service.stage_upload(UPLOAD_CONTRACT, self.ref, "image/png", "0" * 64, "secret-canary")
        self.assertEqual("reject", result["decision"])
        self.assertNotIn("secret-canary", str(result))
        self.assertFalse(result["state_changed"])


if __name__ == "__main__": unittest.main()
