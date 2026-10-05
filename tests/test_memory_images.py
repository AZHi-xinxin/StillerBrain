"""Only generated pixels and temporary synthetic module databases."""
import base64
from contextlib import closing
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zlib

from PIL import Image
from runtime.memory_images import (MemoryImageStore, MemoryImageError, MAX_BYTES, MAX_BASE64,
                                   decode_image_base64, validate_image)
from runtime.memory_relations import MemoryRelationError
from tests.test_atlas_metadata import create_runtime_database
from tests.test_memory_relations import seed_endpoints, OWNER


def synthetic_image(format="PNG"):
    output = BytesIO()
    Image.new("RGB", (3, 2), (20, 50, 80)).save(output, format=format)
    return output.getvalue()


def maximum_png():
    raw = synthetic_image()
    # A genuine ancillary text chunk, no trailing executable/polyglot fixture.
    data = b"fixture\0" + b"x" * (MAX_BYTES - len(raw) - 12 - 8)
    chunk = len(data).to_bytes(4, "big") + b"tEXt" + data + zlib.crc32(b"tEXt" + data).to_bytes(4, "big")
    return raw[:-12] + chunk + raw[-12:]


class MemoryImageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "main.sqlite"
        create_runtime_database(self.database); seed_endpoints(self.database)
        self.store = MemoryImageStore(self.database)
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))

    def put(self, **changes):
        values = {**OWNER, "target_ref": "emotion://e1@1", "description": "Synthetic original image",
                  "mime_type": "image/png", "data_base64": base64.b64encode(synthetic_image()).decode(),
                  "request_key": "a" * 64, "operation_hash": "b" * 64, "_write_guard": lambda db: None}
        return self.store.store(**{**values, **changes})

    def read(self, image_ref, **changes):
        return self.store.read(**{**OWNER, "target_ref": "emotion://e1@1", "image_ref": image_ref, **changes})

    def sql(self, query, args=()):
        with closing(sqlite3.connect(self.database)) as db:
            result = db.execute(query, args).fetchall(); db.commit(); return result

    def test_real_original_bytes_roundtrip_and_metadata_has_no_blob(self):
        saved = self.put()
        self.assertTrue(saved["state_changed"])
        self.assertFalse(saved["automatic_recall_eligible"])
        self.assertNotIn("data_uri", saved)
        listed = self.store.list(**OWNER, target_ref="emotion://e1@1")
        self.assertEqual([saved["image_ref"]], [item["image_ref"] for item in listed["images"]])
        self.assertNotIn("data_uri", json.dumps(listed))
        self.assertNotIn("data_uri", self.read(saved["image_ref"]))
        retrieved = self.read(saved["image_ref"], include_data=True)
        data = base64.b64decode(retrieved["data_uri"].split(",", 1)[1])
        self.assertEqual(synthetic_image(), data)
        self.assertEqual(hashlib.sha256(data).hexdigest(), retrieved["sha256"])
        self.assertEqual((3, 2), (retrieved["width"], retrieved["height"]))

    def test_remove_keeps_backup_bytes_but_old_request_never_resurrects(self):
        saved = self.put()
        removed = self.store.remove(**OWNER, target_ref="emotion://e1@1", image_ref=saved["image_ref"], _write_guard=lambda db: None)
        self.assertTrue(removed["original_retained_for_backup"])
        replay = self.put()
        self.assertEqual("removed", replay["decision"]); self.assertFalse(replay["active"])
        self.assertEqual([], self.store.list(**OWNER, target_ref="emotion://e1@1")["images"])
        with self.assertRaisesRegex(MemoryImageError, "image_not_available"): self.read(saved["image_ref"], include_data=True)
        self.assertEqual(synthetic_image(), self.sql("SELECT original_bytes FROM memory_image_items")[0][0])

    def test_cross_owner_model_and_wrong_memory_cannot_read_or_remove(self):
        saved = self.put()
        for changes in ({"owner_id": "other"}, {"model_id": "other"}, {"target_ref": "emotion://e2@1"}):
            with self.assertRaises(MemoryRelationError): self.read(saved["image_ref"], include_data=True, **changes)
        with self.assertRaises(MemoryRelationError):
            self.store.remove(**OWNER, target_ref="learning://l1@1", image_ref=saved["image_ref"], _write_guard=lambda db: None)

    def test_hidden_or_future_memory_and_damaged_blob_fail_closed(self):
        with self.assertRaisesRegex(MemoryRelationError, "memory_version_conflict"): self.put(target_ref="emotion://e1@2")
        saved = self.put()
        self.sql("UPDATE emotion_memories SET lifecycle='archived' WHERE memory_id='e1'")
        with self.assertRaisesRegex(MemoryRelationError, "relation_endpoint_unavailable"): self.read(saved["image_ref"])
        self.sql("UPDATE emotion_memories SET lifecycle='active' WHERE memory_id='e1'")
        self.sql("UPDATE memory_image_items SET sha256=?", ("0" * 64,))
        with self.assertRaisesRegex(MemoryImageError, "image_integrity_failed"): self.read(saved["image_ref"], include_data=True)

    def test_request_conflict_and_transaction_guard_do_not_write(self):
        self.put()
        with self.assertRaisesRegex(MemoryImageError, "request_conflict"): self.put(operation_hash="c" * 64)
        def denied(db): raise MemoryImageError("image_scope_not_authorized")
        with self.assertRaisesRegex(MemoryImageError, "image_scope_not_authorized"): self.put(request_key="d" * 64, _write_guard=denied)
        self.assertEqual(1, self.sql("SELECT count(*) FROM memory_image_items")[0][0])

    def test_sensitive_original_gate_applies_to_all_image_operations_after_reclassification(self):
        for target, changes in (("emotion://e1@1", ("sensitivity", "intimate")),
                                ("emotion://e1@1", ("sensitivity", "restricted")),
                                ("emotion://e1@1", ("explicit_request_override", "never")),
                                ("learning://l1@1", ("sensitivity", "restricted")),
                                ("learning://l1@1", ("explicit_request_override", "never"))):
            with self.subTest(target=target, field=changes):
                self.sql("UPDATE emotion_memories SET sensitivity='private',explicit_request_override='allow_after_confirmation'")
                self.sql("UPDATE learning_items SET current_json=json_set(current_json,'$.sensitivity','private','$.explicit_request_override','allow_after_confirmation')")
                key = hashlib.sha256(str((target, changes)).encode()).hexdigest()
                saved = self.put(target_ref=target, request_key=key)
                if target.startswith("emotion"):
                    self.sql("UPDATE emotion_memories SET " + changes[0] + "=? WHERE memory_id='e1'", (changes[1],))
                else:
                    self.sql("UPDATE learning_items SET current_json=json_set(current_json,?,?) WHERE learning_id='l1'",
                             ("$." + changes[0], changes[1]))
                operations = (
                    lambda: self.put(target_ref=target, request_key=key),
                    lambda: self.store.list(**OWNER, target_ref=target),
                    lambda: self.store.read(**OWNER, target_ref=target, image_ref=saved["image_ref"]),
                    lambda: self.store.read(**OWNER, target_ref=target, image_ref=saved["image_ref"], include_data=True),
                    lambda: self.store.remove(**OWNER, target_ref=target, image_ref=saved["image_ref"], _write_guard=lambda db: None))
                for operation in operations:
                    with self.assertRaisesRegex(MemoryImageError, "^image_sensitive_target_unsupported$"): operation()

    def test_store_does_not_modify_original_memory_tables(self):
        with closing(sqlite3.connect(self.database)) as db:
            before = [line for line in db.iterdump() if "memory_image_" not in line]
        self.put()
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(before, [line for line in db.iterdump() if "memory_image_" not in line])

    def test_png_jpeg_webp_decode_and_exact_four_mib_original_roundtrip(self):
        for format, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
            self.assertEqual((3, 2), validate_image(synthetic_image(format), mime))
        data = maximum_png(); self.assertEqual(MAX_BYTES, len(data))
        encoded = base64.b64encode(data).decode(); self.assertEqual(MAX_BASE64, len(encoded))
        stored = self.put(data_base64=encoded)
        self.assertEqual(data, base64.b64decode(self.read(stored["image_ref"], include_data=True)["data_uri"].split(",")[1]))

    def test_bad_format_svg_truncation_mime_base64_and_oversize_rejected(self):
        for data, mime in ((b"<svg></svg>", "image/png"), (synthetic_image()[:30], "image/png"),
                           (synthetic_image(), "image/jpeg"), (b"x" * (MAX_BYTES + 1), "image/png")):
            with self.assertRaises(MemoryImageError): validate_image(data, mime)
        for value in ("https://example.invalid/image.png", "file:///private/image", "AAAA\n", "%%%"):
            with self.assertRaises(MemoryImageError): decode_image_base64(value, "image/png")

    def test_animation_and_pixel_budget_rejected_before_full_decode(self):
        frames = BytesIO()
        Image.new("RGB", (2, 2), "red").save(frames, format="PNG", save_all=True,
            append_images=[Image.new("RGB", (2, 2), "blue")], duration=50)
        with self.assertRaisesRegex(MemoryImageError, "animated_image_unsupported"): validate_image(frames.getvalue(), "image/png")
        with patch("runtime.memory_images.MAX_PIXELS", 5):
            with self.assertRaisesRegex(MemoryImageError, "image_pixel_limit"): validate_image(synthetic_image(), "image/png")

    def test_quota_schema_trigger_and_pagination_limits(self):
        with patch("runtime.memory_images.MAX_OWNER_BYTES", 1):
            with self.assertRaisesRegex(MemoryImageError, "image_owner_capacity_limit"): self.put()
        for limit in (True, 0, 21):
            with self.assertRaisesRegex(MemoryImageError, "invalid_image_page"): self.store.list(**OWNER, target_ref="emotion://e1@1", limit=limit)
        self.sql("CREATE TRIGGER image_test AFTER INSERT ON memory_image_items BEGIN SELECT 1; END")
        with self.assertRaisesRegex(MemoryImageError, "image_storage_unavailable"): self.put()


if __name__ == "__main__": unittest.main()
