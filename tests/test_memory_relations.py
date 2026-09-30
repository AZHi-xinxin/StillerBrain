"""Synthetic actual module tables; no real memories, network or model calls."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from runtime.atlas_metadata import AtlasMetadataReader, AtlasScope, AtlasUnavailable, _authorize
from runtime.memory_relations import MemoryRelationStore, MemoryRelationError, parse_memory_ref, validate_relation
from tests.test_atlas_metadata import create_runtime_database, insert_synthetic, STAMP

OWNER = {"owner_id": "synthetic-owner", "model_id": "synthetic-model"}


def seed_endpoints(database):
    with closing(sqlite3.connect(database)) as db:
        for identity in ("e1", "e2"):
            insert_synthetic(db, "emotion_memories", memory_id=identity, lifecycle="active", sensitivity="private", memory_type="event",
                             original_text="PRIVATE_EMOTION_BODY", summary="PRIVATE_EMOTION_SUMMARY")
        for identity in ("l1", "l2"):
            insert_synthetic(db, "learning_items", learning_id=identity, lifecycle="active", kind="insight",
                current_json=json.dumps({"title": "Synthetic learning title", "summary": "PRIVATE_LEARNING_SUMMARY",
                                         "current_understanding": "PRIVATE_LEARNING_BODY", "sensitivity": "private"}))
        insert_synthetic(db, "planning_items", plan_id="p1", kind="plan", recall_lifecycle="active")
        db.execute("INSERT INTO planning_versions VALUES(?,1,NULL,?,?,?,?,?)", ("p1",
            json.dumps({"title": "Synthetic plan title", "original_text": "PRIVATE_PLAN_BODY"}), "hash", "synthetic", "wake", STAMP))
        db.commit()


class MemoryRelationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "main.sqlite"
        create_runtime_database(self.database); seed_endpoints(self.database)
        self.store = MemoryRelationStore(self.database)
        self.scope = AtlasScope(self.database, **OWNER, id_key=b"S" * 32)
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))

    def attach(self, **changes):
        return self.store.attach(**OWNER, **{"from_ref": "emotion://e1@1", "to_ref": "learning://l1@1", "type": "related_to", **changes})

    def read(self, target="emotion://e1@1", **changes):
        return self.store.read(**OWNER, target_ref=target, **changes)

    def sql(self, query, parameters=()):
        with closing(sqlite3.connect(self.database)) as db:
            result = db.execute(query, parameters).fetchall(); db.commit(); return result

    def atlas(self):
        return json.loads(AtlasMetadataReader(self.scope).read())

    def test_cross_module_one_hop_titles_only_no_body_or_summary(self):
        result = self.attach()
        self.assertTrue(result["state_changed"])
        hop = self.read()["relations"][0]
        self.assertEqual("Synthetic learning title", hop["target_title"])
        self.assertEqual(result["edge_ref"], hop["edge_ref"])
        reverse = self.read("learning://l1@1")["relations"][0]
        self.assertIsNone(reverse["target_title"])
        self.assertEqual("event", reverse["target_type"])
        for text in ("PRIVATE_", "summary", "original_text", "current_understanding"):
            self.assertNotIn(text, json.dumps([hop, reverse]))

    def test_every_fixed_type_reverse_is_automatic_and_duplicate_pair_is_one_edge(self):
        reverse = {"same_event": "same_event", "continuation_of": "continues", "continues": "continuation_of",
                   "caused_by": "causes", "causes": "caused_by", "related_to": "related_to"}
        for kind, inverse in reverse.items():
            first = self.attach(type=kind)
            pair = self.attach(from_ref="learning://l1@1", to_ref="emotion://e1@1", type=inverse)
            self.assertEqual(first["edge_ref"], pair["edge_ref"])
            self.assertFalse(pair["state_changed"])
            found = [r for r in self.read()["relations"] if r["edge_ref"] == first["edge_ref"]][0]
            self.assertEqual(kind, found["type"])

    def test_custom_labels_are_literal_optional_reverse_is_not_invented(self):
        self.attach(type="custom", label="author label")
        self.assertEqual("author label", self.read()["relations"][0]["label"])
        self.assertIsNone(self.read("learning://l1@1")["relations"][0]["label"])
        second = self.attach(type="custom", label="one", reverse_label="two")
        found = [r for r in self.read("learning://l1@1")["relations"] if r["edge_ref"] == second["edge_ref"]][0]
        self.assertEqual("two", found["label"])

    def test_custom_missing_label_fixed_label_and_self_edges_rejected(self):
        for fields in ({"type": "custom"}, {"label": "invented"}, {"to_ref": "emotion://e1@1"},
                       {"type": "custom", "label": "x\nunsafe"}, {"type": "unknown"}):
            with self.assertRaises(MemoryRelationError): self.attach(**fields)
        self.assertEqual([], self.read()["relations"])

    def test_work_and_non_memory_refs_are_not_supported_or_created(self):
        for ref in ("work://a@1", "self://a@1", "https://other/a", "emotion://../x@1", "emotion://e1@0"):
            with self.assertRaises(MemoryRelationError): parse_memory_ref(ref)

    def test_owner_model_isolation_and_missing_inactive_targets(self):
        self.attach()
        for identity in ({"owner_id": "other", "model_id": OWNER["model_id"]}, {"owner_id": OWNER["owner_id"], "model_id": "other"}):
            with self.assertRaisesRegex(MemoryRelationError, "relation_endpoint_unavailable"):
                self.store.read(**identity, target_ref="emotion://e1@1")
        self.sql("UPDATE learning_items SET lifecycle='quarantined' WHERE learning_id='l1'")
        self.assertEqual([], self.read()["relations"])
        self.assertEqual([], self.atlas()["edges"])
        with self.assertRaisesRegex(MemoryRelationError, "relation_endpoint_unavailable"): self.attach()

    def test_foreign_edge_reference_cannot_detach(self):
        relation = self.attach()
        with self.assertRaisesRegex(MemoryRelationError, "relation_not_found"):
            self.store.detach(owner_id="other", model_id=OWNER["model_id"], edge_ref=relation["edge_ref"])
        self.assertEqual(1, len(self.read()["relations"]))

    def test_stale_creation_ref_rejected_but_body_revision_does_not_break_edge(self):
        relation = self.attach()
        self.sql("UPDATE learning_items SET current_version=2 WHERE learning_id='l1'")
        self.assertEqual("learning://l1@2", self.read()["relations"][0]["target_ref"])
        self.assertEqual(relation["edge_ref"], self.read()["relations"][0]["edge_ref"])
        self.assertEqual(1, len(self.atlas()["edges"]))
        with self.assertRaisesRegex(MemoryRelationError, "memory_version_conflict"): self.attach()
        self.assertFalse(self.attach(to_ref="learning://l1@2")["state_changed"])

    def test_detach_appends_auditable_version_without_changing_any_original_table(self):
        def original():
            with closing(sqlite3.connect(self.database)) as db:
                return [line for line in db.iterdump() if "memory_relation_" not in line]
        before = original()
        relation = self.attach()
        detached = self.store.detach(**OWNER, edge_ref=relation["edge_ref"])
        self.assertFalse(detached["active"]); self.assertTrue(detached["edge_ref"].endswith("@2"))
        self.assertEqual([], self.read()["relations"])
        self.assertEqual([(1, 1, "attach"), (2, 0, "detach")], self.sql("SELECT version,active,operation FROM memory_relation_versions ORDER BY version"))
        self.assertEqual(before, original())

    def test_same_request_different_args_conflicts_and_old_retry_never_resurrects(self):
        first = self.attach(request_key="1" * 64)
        self.assertFalse(self.attach(request_key="1" * 64)["state_changed"])
        with self.assertRaisesRegex(MemoryRelationError, "request_conflict"): self.attach(request_key="1" * 64, type="same_event")
        self.store.detach(**OWNER, edge_ref=first["edge_ref"])
        self.assertFalse(self.attach(request_key="1" * 64)["active"])
        with self.assertRaisesRegex(MemoryRelationError, "reattach_requires_new_request"): self.attach()
        new = self.attach(request_key="2" * 64)
        self.assertTrue(new["active"]); self.assertTrue(new["edge_ref"].endswith("@3"))
        with self.assertRaisesRegex(MemoryRelationError, "edge_version_conflict"):
            self.store.detach(**OWNER, edge_ref=first["edge_ref"])

    def test_detach_retry_idempotent_and_stale_reference_cannot_disable_reattachment(self):
        first = self.attach()
        detached = self.store.detach(**OWNER, edge_ref=first["edge_ref"], request_key="3" * 64)
        self.assertEqual(detached["edge_ref"], self.store.detach(**OWNER, edge_ref=first["edge_ref"])["edge_ref"])
        active = self.attach(request_key="4" * 64)
        replay = self.store.detach(**OWNER, edge_ref=first["edge_ref"], request_key="3" * 64)
        self.assertEqual(active["edge_ref"], replay["edge_ref"]); self.assertTrue(replay["active"])

    def test_sensitive_target_title_and_authored_labels_are_stubbed(self):
        self.attach(type="custom", label="PRIVATE_RELATION_LABEL")
        self.sql("UPDATE learning_items SET current_json=? WHERE learning_id='l1'",
                 (json.dumps({"title": "PRIVATE_TARGET_TITLE", "sensitivity": "intimate"}),))
        result = self.read()["relations"][0]
        self.assertEqual("受限记忆", result["target_title"]); self.assertIsNone(result["label"])
        self.assertTrue(result["restricted_stub"]); self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_explicit_scope_subset_never_crosses_to_unauthorized_target(self):
        self.attach()
        self.assertEqual([], self.read(allowed_scopes={"emotional_memory"})["relations"])
        with self.assertRaisesRegex(MemoryRelationError, "relation_scope_not_authorized"):
            self.read(allowed_scopes={"learning_memory"})

    def test_atlas_edges_are_metadata_only_and_read_no_labels_titles_or_bodies(self):
        self.attach(type="custom", label="PRIVATE_RELATION_LABEL")
        self.attach(from_ref="learning://l1@1", to_ref="plan://p1@1")
        reads = []
        def record(action, table, column, database, source):
            if action == sqlite3.SQLITE_READ: reads.append((table, column))
            return _authorize(action, table, column, database, source)
        with patch("runtime.atlas_metadata._authorize", record): graph = self.atlas()
        self.assertEqual(2, len(graph["edges"]))
        self.assertTrue(all(set(edge) == {"a", "b"} for edge in graph["edges"]))
        encoded = json.dumps(graph)
        for private in ("PRIVATE_", "Synthetic", "relation://", "label", "title", "summary"):
            self.assertNotIn(private, encoded)
        for forbidden in (("memory_relation_items", "label"), ("memory_relation_items", "reverse_label"),
                          ("learning_items", "current_json"), ("emotion_memories", "summary")):
            self.assertNotIn(forbidden, reads)
            self.assertEqual(sqlite3.SQLITE_DENY, _authorize(sqlite3.SQLITE_READ, *forbidden, "main", None))

    def test_old_database_without_new_tables_still_projects_and_partial_schema_fails_closed(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DROP TABLE memory_relation_requests"); db.execute("DROP TABLE memory_relation_versions")
            db.execute("DROP TABLE memory_relation_items"); db.commit()
        self.assertEqual([], self.atlas()["edges"])
        self.sql("CREATE TABLE memory_relation_items(owner_id TEXT)")
        with self.assertRaises(AtlasUnavailable): self.atlas()
        with self.assertRaises(MemoryRelationError): MemoryRelationStore(self.database)

    def test_missing_main_database_is_not_created(self):
        path = Path(self.temp.name) / "missing.sqlite"
        with self.assertRaises(MemoryRelationError): MemoryRelationStore(path)
        self.assertFalse(path.exists())

    def test_pair_snapshot_and_pagination_are_bounded(self):
        self.attach(); self.attach(to_ref="plan://p1@1")
        first = self.read(limit=1)
        self.assertEqual(1, len(first["relations"])); self.assertEqual(1, first["next_offset"])
        second = self.read(limit=1, offset=1)
        self.assertEqual(1, len(second["relations"])); self.assertIsNone(second["next_offset"])
        for value in (0, 51, True, "2"):
            with self.assertRaises(MemoryRelationError): self.read(limit=value)

    def test_plain_credentials_cannot_be_custom_labels(self):
        for value in ("Bearer synthetic-long-secret-value", "orb_atlas_" + "A" * 43, "password: synthetic-secret"):
            with self.assertRaisesRegex(MemoryRelationError, "credential_or_secret_detected"):
                self.attach(type="custom", label=value)

    def test_transaction_gate_failure_leaves_no_relation_or_receipt(self):
        def deny(db): raise MemoryRelationError("module_one_required")
        with self.assertRaisesRegex(MemoryRelationError, "module_one_required"):
            self.attach(request_key="5" * 64, _write_guard=deny)
        self.assertEqual([(0,)], self.sql("SELECT COUNT(*) FROM memory_relation_items"))
        self.assertEqual([(0,)], self.sql("SELECT COUNT(*) FROM memory_relation_requests"))
