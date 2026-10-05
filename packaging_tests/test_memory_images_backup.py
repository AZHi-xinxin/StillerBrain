"""Real operations backup/restore, synthetic image bytes and new temporary roots only."""
import base64
from contextlib import closing
from pathlib import Path
import sqlite3
import unittest

from packaging_tests import test_operations as operation_fixture
from runtime.memory_images import MemoryImageStore, MemoryImageError
from tests.test_atlas_metadata import create_runtime_database
from tests.test_memory_relations import seed_endpoints, OWNER
from tests.test_memory_images import maximum_png, synthetic_image


class MemoryImageBackupTests(unittest.TestCase):
    def test_full_backup_restore_keeps_original_blob_attachment_and_removed_tombstone(self):
        fixture = operation_fixture.OperationsTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        ops = operation_fixture.ops
        fixture.config.update(STBRAIN_OWNER_ID=OWNER["owner_id"], STBRAIN_MODEL_ID=OWNER["model_id"])
        source = Path(fixture.config["STBRAIN_DB_PATH"])
        create_runtime_database(source); seed_endpoints(source)
        store = MemoryImageStore(source)
        original = maximum_png()
        def put(backend, data, key):
            return backend.store(**OWNER, target_ref="emotion://e1@1", description="Synthetic original retained",
                mime_type="image/png", data_base64=base64.b64encode(data).decode(), request_key=key * 64,
                operation_hash=key * 64, _write_guard=lambda db: None)
        active = put(store, original, "a")
        removed = put(store, synthetic_image(), "b")
        store.remove(**OWNER, target_ref="emotion://e1@1", image_ref=removed["image_ref"], _write_guard=lambda db: None)
        before = ops.digest(source)
        backup = fixture.make_backup("images-full-backup")
        restored = fixture.private / "images-new-restored"
        result = ops.restore(backup, restored)
        self.assertFalse(result["existing_instance_modified"])
        self.assertEqual(before, ops.digest(source))
        restored_database = restored / ops.FILES["main"]
        reopened = MemoryImageStore(restored_database)
        received = reopened.read(**OWNER, target_ref="emotion://e1@1", image_ref=active["image_ref"], include_data=True)
        self.assertEqual(original, base64.b64decode(received["data_uri"].split(",")[1]))
        self.assertEqual(active["sha256"], received["sha256"])
        self.assertEqual("emotion://e1@1", received["target_ref"])
        self.assertEqual([active["image_ref"]], [item["image_ref"] for item in reopened.list(**OWNER, target_ref="emotion://e1@1")["images"]])
        with self.assertRaisesRegex(MemoryImageError, "image_not_available"):
            reopened.read(**OWNER, target_ref="emotion://e1@1", image_ref=removed["image_ref"], include_data=True)
        self.assertEqual("removed", put(reopened, synthetic_image(), "b")["decision"])
        with closing(sqlite3.connect(restored_database)) as db:
            self.assertEqual(2, db.execute("SELECT count(*) FROM memory_image_items").fetchone()[0])
            self.assertEqual("ok", db.execute("PRAGMA integrity_check").fetchone()[0])
        with self.assertRaisesRegex(ops.OperationError, "already exists"):
            ops.restore(backup, restored)
        self.assertEqual(before, ops.digest(source))


if __name__ == "__main__": unittest.main()
