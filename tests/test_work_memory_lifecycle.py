"""Reversible retirement: isolated temporary databases, never live memories."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from runtime.work_memory import (WorkMemoryStore, WorkMemoryError, ITEM_SQL,
                                 VERSION_SQL, INDEX_SQL, LIFECYCLE_SQL)

SCOPE = {"owner_id": "synthetic-owner", "model_id": "synthetic-model"}


class WorkLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="synthetic-work-lifecycle-")
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / "main.sqlite"
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE sentinel(body TEXT)")
            db.execute("INSERT INTO sentinel VALUES('untouched')")
            db.commit()
        self.store = WorkMemoryStore(self.database)

    def create(self, tag="test"):
        return self.store.remember(**SCOPE, content="  exact original\r\n原文🙂  ", tag=tag)

    def revise(self, ref, **changes):
        return self.store.revise(**SCOPE, target_ref=ref, **changes)

    def recall(self, **query):
        return self.store.recall(**SCOPE, **query)

    def dump(self):
        with closing(sqlite3.connect(self.database)) as db:
            return list(db.iterdump())

    def test_create_retire_hidden_explicit_restore_visible(self):
        first = self.create()
        retired = self.revise(first["target_ref"], lifecycle="retired")
        self.assertEqual((2, "retired"), (retired["version"], retired["lifecycle"]))
        self.assertEqual(0, self.recall()["count"])
        found = self.recall(include_retired=True)["results"][0]
        self.assertEqual(retired["target_ref"], found["current_target_ref"])
        restored = self.revise(found["current_target_ref"], lifecycle="active")
        current = self.recall()["results"][0]
        self.assertEqual((restored["target_ref"], "active", 3),
                         (current["target_ref"], current["lifecycle"], current["version"]))
        self.assertEqual("  exact original\r\n原文🙂  ", current["content"])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(3, db.execute("SELECT count(*) FROM work_memory_versions").fetchone()[0])
            self.assertEqual([("active",), ("retired",), ("active",)], db.execute(
                "SELECT lifecycle FROM work_memory_lifecycle_versions ORDER BY version").fetchall())
            self.assertEqual([], db.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual("ok", db.execute("PRAGMA integrity_check").fetchone()[0])

    def test_old_reference_does_not_bypass_current_retirement(self):
        first = self.create()
        retired = self.revise(first["target_ref"], lifecycle="retired")
        for ref in (first["target_ref"], retired["target_ref"]):
            with self.assertRaisesRegex(WorkMemoryError, "^work_memory_not_found$"):
                self.recall(target_ref=ref)
        old = self.recall(target_ref=first["target_ref"], include_retired=True)["results"][0]
        self.assertEqual(("active", "retired", retired["target_ref"]),
                         (old["lifecycle"], old["current_lifecycle"], old["current_target_ref"]))
        with self.assertRaisesRegex(WorkMemoryError, "^work_version_conflict$"):
            self.revise(first["target_ref"], lifecycle="active")

    def test_edit_retired_keeps_retirement_and_can_restore_with_explicit_edit(self):
        retired = self.revise(self.create()["target_ref"], lifecycle="retired")
        edited = self.revise(retired["target_ref"], tag="changed")
        self.assertEqual("retired", edited["lifecycle"])
        self.assertEqual(0, self.recall(query="changed")["count"])
        restored = self.revise(edited["target_ref"], content="new exact body", lifecycle="active")
        self.assertEqual("new exact body", self.recall(target_ref=restored["target_ref"])["results"][0]["content"])

    def test_strict_lifecycle_and_boolean_query_no_changes(self):
        ref = self.create()["target_ref"]
        before = self.dump()
        for value in (False, True, 0, 1, [], {}, "Retired", " retired", "deleted", ""):
            with self.assertRaisesRegex(WorkMemoryError, "^invalid_work_lifecycle$"):
                self.revise(ref, lifecycle=value)
        for value in (None, 0, 1, "true", "false", [], {}):
            with self.assertRaisesRegex(WorkMemoryError, "^invalid_work_query$"):
                self.recall(include_retired=value)
        self.assertEqual(before, self.dump())

    def test_single_exact_ref_only_no_bulk_or_other_brain(self):
        before = self.dump()
        for ref in (None, [], {}, "*", "work://*", "work://" + "a" * 32,
                    "emotion://emmem_" + "a" * 32 + "@1"):
            with self.assertRaisesRegex(WorkMemoryError, "^invalid_work_ref$"):
                self.revise(ref, lifecycle="retired")
        self.assertEqual(before, self.dump())

    def test_owner_and_model_isolation_include_retired_and_restore(self):
        ref = self.revise(self.create()["target_ref"], lifecycle="retired")["target_ref"]
        before = self.dump()
        for scope in (dict(SCOPE, owner_id="other"), dict(SCOPE, model_id="other")):
            self.assertEqual(0, self.store.recall(**scope, include_retired=True)["count"])
            for operation in (lambda: self.store.recall(**scope, target_ref=ref, include_retired=True),
                              lambda: self.store.revise(**scope, target_ref=ref, lifecycle="active")):
                with self.assertRaisesRegex(WorkMemoryError, "^work_memory_not_found$"):
                    operation()
        self.assertEqual(before, self.dump())

    def test_filter_precedes_pagination(self):
        refs = [self.create()["target_ref"] for _ in range(8)]
        for ref in refs[:3]:
            self.revise(ref, lifecycle="retired")
        active = self.recall()
        self.assertEqual((5, False, None), (active["count"], active["more"], active["next_offset"]))
        included = self.recall(include_retired=True)
        tail = self.recall(include_retired=True, offset=included["next_offset"])
        self.assertEqual((5, 3), (included["count"], tail["count"]))

    def test_lifecycle_is_part_of_idempotency_hash(self):
        ref = self.create()["target_ref"]
        key = "b" * 64
        retired = self.revise(ref, lifecycle="retired", request_key=key)
        self.revise(retired["target_ref"], lifecycle="active")
        replay = self.revise(ref, lifecycle="retired", request_key=key)
        self.assertEqual((retired["target_ref"], "retired", False),
                         (replay["target_ref"], replay["lifecycle"], replay["state_changed"]))
        with self.assertRaisesRegex(WorkMemoryError, "^request_conflict$"):
            self.revise(ref, lifecycle="active", request_key=key)
        self.assertEqual("active", self.recall()["results"][0]["lifecycle"])

    def test_atomic_guard_and_racing_edit(self):
        ref = self.create()["target_ref"]
        before = self.dump()
        def deny(_):
            raise WorkMemoryError("module_one_required")
        with self.assertRaisesRegex(WorkMemoryError, "^module_one_required$"):
            self.revise(ref, lifecycle="retired", _write_guard=deny)
        self.assertEqual(before, self.dump())
        other = WorkMemoryStore(self.database)
        def change(store):
            try:
                return store.revise(**SCOPE, target_ref=ref, lifecycle="retired")["version"]
            except WorkMemoryError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual([2, "work_version_conflict"], list(pool.map(change, (self.store, other))))

    def test_constructor_adds_one_table_preserves_legacy_rows_and_replay(self):
        legacy = self.database.parent / "legacy.sqlite"
        key, work_id = "c" * 64, "d" * 32
        operation_hash = hashlib.sha256(json.dumps(["remember", "legacy exact", "legacy"],
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with closing(sqlite3.connect(legacy)) as db:
            for ddl in (ITEM_SQL, VERSION_SQL, INDEX_SQL):
                db.execute(ddl)
            db.execute("INSERT INTO work_memory_items VALUES(?,?,?,1,'old','old')", (*SCOPE.values(), work_id))
            db.execute("INSERT INTO work_memory_versions VALUES(?,?,?,1,'legacy exact','legacy','old',?,?)",
                       (*SCOPE.values(), work_id, key, operation_hash))
            db.commit()
            old = list(db.iterdump())
        migrated = WorkMemoryStore(legacy)
        with closing(sqlite3.connect(legacy)) as db:
            self.assertEqual(old, [line for line in db.iterdump() if 'work_memory_lifecycle_versions' not in line])
            self.assertEqual(0, db.execute("SELECT count(*) FROM work_memory_lifecycle_versions").fetchone()[0])
        replay = migrated.remember(**SCOPE, content="legacy exact", tag="legacy", request_key=key)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual("active", replay["lifecycle"])
        retired = migrated.revise(**SCOPE, target_ref=replay["target_ref"], lifecycle="retired")
        fresh = migrated.remember(**SCOPE, content="new", tag="new")
        self.assertEqual([fresh["target_ref"]], [r["target_ref"] for r in migrated.recall(**SCOPE)["results"]])
        self.assertEqual(2, migrated.recall(**SCOPE, include_retired=True)["count"])
        self.assertEqual("retired", retired["lifecycle"])
        self.assertEqual(2, WorkMemoryStore(legacy).recall(**SCOPE, include_retired=True)["count"])

    def test_malformed_additive_schema_or_trigger_fails_closed(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TRIGGER bad AFTER INSERT ON work_memory_lifecycle_versions BEGIN UPDATE sentinel SET body='changed'; END")
            db.commit()
        before = self.dump()
        for operation in (lambda: WorkMemoryStore(self.database), self.create):
            with self.assertRaisesRegex(WorkMemoryError, "^work_memory_unavailable$"):
                operation()
        self.assertEqual(before, self.dump())

    def test_backup_contains_lifecycle_and_read_only_queries_do_not_change_data(self):
        self.revise(self.create()["target_ref"], lifecycle="retired")
        before = self.dump()
        self.recall(include_retired=True)
        self.recall()
        self.assertEqual(before, self.dump())
        backup = self.database.parent / "backup.sqlite"
        with closing(sqlite3.connect(self.database)) as src, closing(sqlite3.connect(backup)) as dst:
            src.backup(dst)
        self.assertEqual(0, WorkMemoryStore(backup).recall(**SCOPE)["count"])
        self.assertEqual(1, WorkMemoryStore(backup).recall(**SCOPE, include_retired=True)["count"])


if __name__ == "__main__":
    unittest.main()
