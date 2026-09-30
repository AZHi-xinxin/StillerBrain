"""Projection exercised against the current runtime-created synthetic schema."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from runtime.atlas_metadata import (
    AtlasMetadataReader, AtlasScope, AtlasUnavailable, MAX_STARS, READ_COLUMNS,
    _authorize, _opaque,
)
from runtime.emotional_memory import EmotionalMemoryStore
from runtime.learning_memory import LearningMemoryStore
from runtime.planning_memory import PlanningMemoryStore


STAMP = "2026-09-01T00:00:00Z"


def create_runtime_database(database):
    EmotionalMemoryStore(database)
    LearningMemoryStore(database, idea_database=database.with_name("synthetic-ideas.sqlite"))
    PlanningMemoryStore(database)


def insert_synthetic(db, table, **overrides):
    # Real module DDL above is authoritative. Fill unprojected required fields
    # with a distinctive private sentinel; metadata access may never read it.
    values = {}
    for _, name, kind, required, default, primary in db.execute(f"PRAGMA table_info({table})"):
        if required or primary:
            values[name] = 1 if kind == "INTEGER" else "DO_NOT_PROJECT_PRIVATE_BODY"
    values.update(owner_id="synthetic-owner", model_id="synthetic-model", created_at=STAMP)
    values.update(overrides)
    columns = ",".join(values)
    db.execute(f"INSERT INTO {table}({columns}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))


class AtlasMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "synthetic-main.sqlite"
        create_runtime_database(self.database)
        self.scope = AtlasScope(self.database, "synthetic-owner", "synthetic-model", b"k" * 32)
        self.reader = AtlasMetadataReader(self.scope)

    def read(self):
        return json.loads(self.reader.read())

    def seed(self, table, **values):
        with closing(sqlite3.connect(self.database)) as db:
            insert_synthetic(db, table, **values)
            db.commit()

    def emotion(self, identity="e1", **values):
        self.seed("emotion_memories", memory_id=identity, lifecycle="active", **values)

    def learning(self, identity="l1", **values):
        self.seed("learning_items", learning_id=identity, lifecycle="active", **values)

    def planning(self, identity="p1", **values):
        self.seed("planning_items", plan_id=identity, recall_lifecycle="active", **values)

    def test_current_runtime_schema_empty_is_valid(self):
        graph = self.read()
        self.assertEqual({"schema", "generatedAt", "truncated", "stars", "edges"}, set(graph))
        self.assertEqual("orbis.st.atlas/1", graph["schema"])
        self.assertEqual([], graph["stars"])
        self.assertEqual([], graph["edges"])

    def test_three_categories_metadata_only_and_no_database_changes(self):
        self.emotion()
        self.learning()
        self.planning()
        with closing(sqlite3.connect(self.database)) as db:
            before = list(db.iterdump())
        graph = self.read()
        self.assertEqual({"情感", "学习", "规划"}, {star["type"] for star in graph["stars"]})
        for star in graph["stars"]:
            self.assertEqual({"id", "type", "storedAt"}, set(star))
            self.assertRegex(star["id"], r"^[0-9a-f]{64}$")
        encoded = json.dumps(graph)
        for private in ("DO_NOT_PROJECT_PRIVATE_BODY", "synthetic-owner", "synthetic-model", "e1", "l1", "p1"):
            self.assertNotIn('"' + private + '"', encoded)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_authorizer_never_reads_content_or_operational_credentials(self):
        self.emotion()
        reads = []
        def recording(action, first, second, database, source):
            if action == sqlite3.SQLITE_READ:
                reads.append((first, second))
            return _authorize(action, first, second, database, source)
        with patch("runtime.atlas_metadata._authorize", recording):
            self.read()
        self.assertTrue(reads)
        for table, column in reads:
            self.assertIn(column, READ_COLUMNS[table])
        for table, column in (("emotion_memories", "original_text"), ("learning_items", "current_json"),
                              ("planning_versions", "content_json"), ("orbis_atlas_device_grants", "verifier")):
            self.assertEqual(sqlite3.SQLITE_DENY, _authorize(sqlite3.SQLITE_READ, table, column, "main", None))
        self.assertEqual(sqlite3.SQLITE_DENY, _authorize(sqlite3.SQLITE_UPDATE, "emotion_memories", "created_at", "main", None))

    def test_other_owners_models_and_nonactive_records_are_not_visible(self):
        self.emotion("own")
        self.emotion("other-owner", owner_id="other")
        self.emotion("other-model", model_id="other")
        self.seed("emotion_memories", memory_id="archived", lifecycle="archived")
        self.seed("learning_items", learning_id="quarantined", lifecycle="quarantined")
        self.seed("planning_items", plan_id="quarantined", recall_lifecycle="quarantined")
        self.assertEqual([_opaque(self.scope, "emotion", "own")], [star["id"] for star in self.read()["stars"]])

    def test_ids_are_stable_but_separated_by_module_scope_and_key(self):
        self.emotion("same")
        self.learning("same")
        ids = {star["id"] for star in self.read()["stars"]}
        self.assertEqual(ids, {star["id"] for star in self.read()["stars"]})
        self.assertEqual(2, len(ids))
        for scope in (AtlasScope(self.database, "other", "synthetic-model", b"k" * 32),
                      AtlasScope(self.database, "synthetic-owner", "other", b"k" * 32),
                      AtlasScope(self.database, "synthetic-owner", "synthetic-model", b"z" * 32)):
            self.assertNotIn(_opaque(scope, "emotion", "same"), ids)

    def test_edges_require_owned_active_endpoints(self):
        self.emotion("a")
        self.emotion("b")
        self.emotion("foreign", owner_id="other")
        for identity, first, second in (("valid", "a", "b"), ("self", "a", "a"), ("foreign", "a", "foreign")):
            self.seed("emotion_edges", edge_id=identity, from_memory_id=first, to_memory_id=second, lifecycle="active")
        self.assertEqual(1, len(self.read()["edges"]))

    def test_learning_links_bind_both_current_versions(self):
        self.learning("a", current_version=2)
        self.learning("b", current_version=3)
        for identity, first, second in (("valid", "a@2", "b@3"), ("old-source", "a@1", "b@3"), ("old-target", "a@2", "b@2")):
            self.seed("learning_links", link_id=identity, from_ref="learning://" + first, to_ref="learning://" + second)
        self.assertEqual(1, len(self.read()["edges"]))
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE learning_items SET current_version=4 WHERE learning_id='b'")
            db.commit()
        self.assertEqual([], self.read()["edges"])

    def test_planning_edges_bind_current_source_version_and_active(self):
        self.planning("a", current_version=2)
        self.planning("b")
        for identity, version, active in (("valid", 2, 1), ("old", 1, 1), ("inactive", 2, 0)):
            self.seed("planning_edges", edge_id=identity, source_plan_id="a", target_plan_id="b", source_version=version,
                      edge_type="dependency", active=active)
        self.assertEqual(1, len(self.read()["edges"]))

    def test_global_latest_limit_and_timezone_ordering(self):
        with closing(sqlite3.connect(self.database)) as db:
            for number in range(MAX_STARS + 1):
                insert_synthetic(db, "learning_items", learning_id=f"record-{number}", lifecycle="active")
            insert_synthetic(db, "planning_items", plan_id="newest", recall_lifecycle="active", created_at="2026-09-02T00:30:00+08:00")
            db.commit()
        graph = self.read()
        self.assertEqual(MAX_STARS, len(graph["stars"]))
        self.assertTrue(graph["truncated"])
        self.assertEqual(_opaque(self.scope, "planning", "newest"), graph["stars"][0]["id"])
        self.assertEqual("2026-09-01T16:30:00.000000Z", graph["stars"][0]["storedAt"])

    def test_wal_committed_records_are_visible(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            insert_synthetic(writer, "learning_items", learning_id="in-wal", lifecycle="active")
            writer.commit()
            self.assertEqual(1, len(self.read()["stars"]))

    def test_missing_table_or_view_is_not_mistaken_for_empty(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DROP TABLE learning_links")
            db.commit()
        with self.assertRaisesRegex(AtlasUnavailable, "^atlas_unavailable$"):
            self.read()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE VIEW learning_links AS SELECT * FROM learning_items")
            db.commit()
        with self.assertRaises(AtlasUnavailable):
            self.read()

    def test_invalid_timestamp_fails_without_exposing_body_or_path(self):
        self.emotion(created_at="unparseable-private-sentinel")
        with self.assertRaisesRegex(AtlasUnavailable, "^atlas_unavailable$"):
            self.read()

    def test_missing_database_is_not_created(self):
        missing = Path(self.temp.name) / "absent.sqlite"
        with self.assertRaisesRegex(AtlasUnavailable, "^atlas_unavailable$"):
            AtlasMetadataReader(AtlasScope(missing, "owner", "model", b"k" * 32)).read()
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
