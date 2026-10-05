"""Synthetic image transport only: no database, filesystem upload, or network."""
import base64
import hashlib
import unittest
from unittest.mock import patch

from runtime.memory_images import MemoryImageError
from runtime.memory_image_uploads import MemoryImageUploads, UPLOAD_CONTRACT
from tests.test_memory_images import synthetic_image


class MemoryImageUploadsTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.uploads = MemoryImageUploads(clock=lambda: self.now)
        self.raw = synthetic_image()
        self.args = dict(owner_id="synthetic-owner", model_id="synthetic-model", protocol=UPLOAD_CONTRACT,
            upload_ref="stimgup_" + "a" * 64, mime_type="image/png", sha256=hashlib.sha256(self.raw).hexdigest(),
            data_base64=base64.b64encode(self.raw).decode("ascii"))

    def stage(self, **changes):
        return self.uploads.stage(**{**self.args, **changes})

    def resolve(self, **changes):
        return self.uploads.resolve(**{**{k: self.args[k] for k in
            ("owner_id", "model_id", "upload_ref", "mime_type")}, **changes})

    def test_original_bytes_receipt_and_retry_do_not_extend_lifetime(self):
        first = self.stage()
        self.assertEqual("staged", first["decision"])
        self.assertEqual(self.raw, self.resolve())
        self.assertFalse(first["memory_written"])
        self.assertFalse(first["write_authority_granted"])
        self.assertNotIn("data_base64", first)
        self.now += 45
        replay = self.stage()
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(855, replay["expires_in_seconds"])
        self.now = 1000.0
        with self.assertRaisesRegex(MemoryImageError, "^image_upload_not_available$"):
            self.resolve()
        self.assertEqual({}, self.uploads._items)

    def test_cross_owner_model_and_restart_cannot_resolve(self):
        self.stage()
        for changes in ({"owner_id": "foreign"}, {"model_id": "foreign"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(MemoryImageError, "^image_upload_not_available$"):
                self.resolve(**changes)
        self.uploads = MemoryImageUploads()
        with self.assertRaisesRegex(MemoryImageError, "^image_upload_not_available$"):
            self.resolve()

    def test_same_ref_cannot_replace_bytes_but_scopes_are_independent(self):
        self.stage()
        other = synthetic_image("JPEG")
        fields = dict(mime_type="image/jpeg", sha256=hashlib.sha256(other).hexdigest(),
                      data_base64=base64.b64encode(other).decode("ascii"))
        with self.assertRaisesRegex(MemoryImageError, "^image_upload_reference_conflict$"):
            self.stage(**fields)
        self.stage(owner_id="other", **fields)
        self.assertEqual(self.raw, self.resolve())
        self.assertEqual(other, self.resolve(owner_id="other", mime_type="image/jpeg"))

    def test_malformed_wrong_hash_protocol_or_mime_never_stage(self):
        for changes, code in (
            ({"sha256": "0" * 64}, "image_upload_hash_mismatch"),
            ({"sha256": "secret-canary"}, "invalid_image_upload_hash"),
            ({"protocol": "memory-image-upload/99"}, "image_upload_protocol_unsupported"),
            ({"upload_ref": "stimgup_" + "A" * 64}, "invalid_image_upload_ref"),
            ({"upload_ref": self.args["upload_ref"] + "\n"}, "invalid_image_upload_ref"),
            ({"mime_type": "image/jpeg"}, "image_mime_mismatch"),
            ({"data_base64": "secret-canary"}, "image_base64_invalid"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(MemoryImageError, "^" + code + "$"):
                self.stage(**changes)
            self.assertEqual({}, self.uploads._items)

    def test_declared_mime_must_match_staged_original(self):
        self.stage()
        with self.assertRaisesRegex(MemoryImageError, "^image_upload_mime_mismatch$"):
            self.resolve(mime_type="image/jpeg")
        self.assertEqual(self.raw, self.resolve())

    def test_each_capacity_limit_fails_without_evicting_unexpired_original(self):
        self.stage()
        for limit in ("MAX_OWNER_UPLOADS", "MAX_TOTAL_UPLOADS", "MAX_OWNER_UPLOAD_BYTES", "MAX_TOTAL_UPLOAD_BYTES"):
            with self.subTest(limit=limit), patch("runtime.memory_image_uploads." + limit, 1):
                with self.assertRaisesRegex(MemoryImageError, "^image_upload_capacity_limit$"):
                    self.stage(upload_ref="stimgup_" + "b" * 64)
            self.assertEqual(1, len(self.uploads._items))
            self.assertEqual(self.raw, self.resolve())


if __name__ == "__main__": unittest.main()
