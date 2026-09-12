"""Only temporary synthetic SQLite files are used by these migration checks."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from runtime.emotional_memory import EmotionalMemoryError, EmotionalMemoryStore


class EmotionalNullableMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="emotion-nullable-synthetic-"))
        self.path = Path(directory) / "synthetic.sqlite"
        self.db = self.enterContext(closing(sqlite3.connect(self.path)))
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE emotion_memories (
                memory_id TEXT PRIMARY KEY, memory_type TEXT NOT NULL,
                origin TEXT NOT NULL, confidence INTEGER NOT NULL,
                extra TEXT NOT NULL DEFAULT '', CHECK(confidence BETWEEN 0 AND 100)
            );
            CREATE INDEX synthetic_memory_type ON emotion_memories(memory_type);
            CREATE TABLE synthetic_history (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id TEXT NOT NULL REFERENCES emotion_memories(memory_id) ON DELETE CASCADE,
                body TEXT NOT NULL
            );
            CREATE TABLE synthetic_trigger_ledger (body TEXT);
            CREATE TRIGGER synthetic_memory_insert AFTER INSERT ON emotion_memories
            BEGIN INSERT INTO synthetic_trigger_ledger(body) VALUES ('synthetic insertion'); END;
            INSERT INTO emotion_memories(rowid,memory_id,memory_type,origin,confidence,extra)
                VALUES(7,'synthetic-old-50','meaningful_dialogue','reported',50,'unchanged');
            INSERT INTO emotion_memories(rowid,memory_id,memory_type,origin,confidence,extra)
                VALUES(19,'synthetic-old-100','shared_event','firsthand',100,'unchanged');
            INSERT INTO synthetic_history(memory_id,body) VALUES('synthetic-old-50','{old exact history}');
        """)

    def _rows(self):
        return {name: self.db.execute(f"SELECT rowid,* FROM {name} ORDER BY rowid").fetchall()
                for name in ("emotion_memories", "synthetic_history", "synthetic_trigger_ledger", "sqlite_sequence")}

    def _schema(self):
        return self.db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name").fetchall()

    def test_migration_preserves_every_row_rowid_object_and_sequence(self):
        before_rows, before_schema = self._rows(), self._schema()
        EmotionalMemoryStore._migrate_nullable_confidence(self.db)
        self.assertEqual(before_rows, self._rows())
        expected = [(t,n,tb,s.replace("confidence INTEGER NOT NULL", "confidence INTEGER"))
                    for t,n,tb,s in before_schema]
        actual = [(t,n,tb,s.replace('CREATE TABLE "emotion_memories"', 'CREATE TABLE emotion_memories'))
                  for t,n,tb,s in self._schema()]
        self.assertEqual(expected, actual)
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        EmotionalMemoryStore._migrate_nullable_confidence(self.db)
        self.assertEqual(before_rows, self._rows())
        self.assertEqual(actual, [(t,n,tb,s.replace('CREATE TABLE "emotion_memories"', 'CREATE TABLE emotion_memories'))
                                 for t,n,tb,s in self._schema()])

    def test_null_new_value_allowed_and_check_still_enforced(self):
        EmotionalMemoryStore._migrate_nullable_confidence(self.db)
        self.db.execute("INSERT INTO emotion_memories(memory_id,memory_type,origin,confidence) VALUES(?,?,?,?)",
                        ("synthetic-null", "unclassified", "unmarked", None))
        self.assertIsNone(self.db.execute("SELECT confidence FROM emotion_memories WHERE memory_id='synthetic-null'").fetchone()[0])
        for value in (-1, 101):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute("INSERT INTO emotion_memories(memory_id,memory_type,origin,confidence) VALUES(?,?,?,?)",
                                (f"synthetic-bad-{value}", "unclassified", "unmarked", value))

    def test_active_transaction_is_rejected_without_changing_schema(self):
        before_schema = self._schema()
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(EmotionalMemoryError, "requires_idle_connection"):
            EmotionalMemoryStore._migrate_nullable_confidence(self.db)
        self.db.rollback()
        self.assertEqual(before_schema, self._schema())

    def test_foreign_key_failure_rolls_back_all_schema_and_rows(self):
        self.db.execute("PRAGMA foreign_keys=OFF")
        self.db.execute("INSERT INTO synthetic_history(memory_id,body) VALUES('missing-parent','synthetic broken FK')")
        self.db.commit()
        self.db.execute("PRAGMA foreign_keys=ON")
        before_schema, before_rows = self._schema(), self._rows()
        with self.assertRaisesRegex(EmotionalMemoryError, "foreign_key_failure"):
            EmotionalMemoryStore._migrate_nullable_confidence(self.db)
        self.assertEqual(before_schema, self._schema())
        self.assertEqual(before_rows, self._rows())
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_foreign_key_delete_cascade_still_works_after_migration(self):
        EmotionalMemoryStore._migrate_nullable_confidence(self.db)
        self.db.execute("DELETE FROM emotion_memories WHERE memory_id='synthetic-old-50'")
        self.assertEqual(self.db.execute("SELECT count(*) FROM synthetic_history").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
