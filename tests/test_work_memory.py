"""Work storage uses temporary SQLite only, with no recall/injection wiring."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from runtime.execution_binding import ExecutionBindingError, ExecutionClaim, _BOUND_CLAIM
from runtime.work_memory import WorkMemoryStore, WorkMemoryError, MAX_RESULT_BYTES, MAX_OFFSET, ITEM_SQL


SCOPE = {"owner_id": "synthetic-owner", "model_id": "synthetic-model"}


class WorkMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "synthetic-main.sqlite"
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE business_sentinel(id TEXT PRIMARY KEY,body TEXT)")
            db.execute("INSERT INTO business_sentinel VALUES('original','unchanged')")
            db.commit()
        self.store = WorkMemoryStore(self.database)

    def remember(self, content="original work body", tag="alpha", **kwargs):
        return self.store.remember(**SCOPE, content=content, tag=tag, **kwargs)

    def read(self, **kwargs):
        return self.store.recall(**SCOPE, **kwargs)

    def revise(self, ref, **kwargs):
        return self.store.revise(**SCOPE, target_ref=ref, **kwargs)

    def error(self, code, callback, *args, **kwargs):
        with self.assertRaisesRegex(WorkMemoryError, "^" + code + "$"):
            callback(*args, **kwargs)

    def dump(self):
        with closing(sqlite3.connect(self.database)) as db:
            return list(db.iterdump())

    def test_exact_original_including_spaces_newlines_and_unicode(self):
        text, tag = "  原文\r\n第二行🙂 \t ", " 标签完整名字 "
        result = self.remember(text, tag)
        self.assertTrue(result["stored"])
        self.assertTrue(result["state_changed"])
        self.assertFalse(result["automatic_recall_eligible"])
        self.assertRegex(result["target_ref"], r"^work://[0-9a-f]{32}@1$")
        record = self.read(target_ref=result["target_ref"])["results"][0]
        self.assertEqual((text, tag), (record["content"], record["tag"]))

    def test_tag_only_literal_query_and_no_body_search(self):
        self.remember("body-only searchable phrase", "project alpha_%")
        self.remember("other", "ordinary tag")
        self.assertEqual(1, self.read(query="_%")["count"])
        self.assertEqual(0, self.read(query="body-only")["count"])
        self.assertEqual(0, self.read(query="ALPHA")["count"])
        self.assertEqual(0, self.read(query="' OR 1=1 --")["count"])

    def test_empty_query_is_explicit_current_inventory(self):
        one = self.remember("one")
        self.revise(one["target_ref"], content="two")
        result = self.read()
        self.assertEqual(1, result["count"])
        self.assertEqual("two", result["results"][0]["content"])

    def test_revision_preserves_old_version_only_exact_reference_reads_it(self):
        old = self.remember("first", "old tag")
        new = self.revise(old["target_ref"], content="second", tag="new tag")
        self.assertEqual(2, new["version"])
        self.assertEqual("first", self.read(target_ref=old["target_ref"])["results"][0]["content"])
        self.assertEqual("second", self.read(target_ref=new["target_ref"])["results"][0]["content"])
        self.assertEqual(0, self.read(query="old tag")["count"])
        self.assertEqual(1, self.read(query="new tag")["count"])

    def test_body_and_tag_can_be_edited_independently(self):
        old = self.remember("first", "first tag")
        middle = self.revise(old["target_ref"], tag="other tag")
        latest = self.revise(middle["target_ref"], content="latest")
        value = self.read(target_ref=latest["target_ref"])["results"][0]
        self.assertEqual(("latest", "other tag"), (value["content"], value["tag"]))

    def test_stale_revision_never_updates_current_content(self):
        original = self.remember()
        self.revise(original["target_ref"], content="new")
        before = self.dump()
        self.error("work_version_conflict", self.revise, original["target_ref"], tag="stale")
        self.assertEqual(before, self.dump())

    def test_concurrent_edits_have_one_winner_and_no_history_loss(self):
        original = self.remember()
        second = WorkMemoryStore(self.database)
        def change(store):
            try:
                return store.revise(**SCOPE, target_ref=original["target_ref"], content="updated")["version"]
            except WorkMemoryError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, (self.store, second)))
        self.assertCountEqual([2, "work_version_conflict"], results)
        self.assertEqual("original work body", self.read(target_ref=original["target_ref"])["results"][0]["content"])

    def test_owner_and_model_isolation_for_search_read_and_revision(self):
        own = self.remember()
        for scope in (dict(SCOPE, owner_id="foreign"), dict(SCOPE, model_id="foreign")):
            self.assertEqual(0, self.store.recall(**scope)["count"])
            self.error("work_memory_not_found", self.store.recall, **scope, target_ref=own["target_ref"])
            self.error("work_memory_not_found", self.store.revise, **scope, target_ref=own["target_ref"], content="changed")

    def test_not_found_and_cross_identity_have_same_error(self):
        self.error("work_memory_not_found", self.read, target_ref="work://" + "0" * 32 + "@1")

    def test_precise_reference_rejects_unversioned_noncanonical_and_other_modules(self):
        for ref in ("work://" + "a" * 32, "work://" + "a" * 32 + "@01", "work://" + "A" * 32 + "@1",
                    "work://" + "a" * 32 + "@2147483648", "learning://a@1", "work://../file@1", None):
            self.error("invalid_work_ref", self.revise, ref, content="x")

    def test_pagination_limit_offset_and_exact_ref_ambiguity(self):
        for index in range(7):
            self.remember(f"text {index}")
        first = self.read(limit=5)
        second = self.read(offset=first["next_offset"])
        self.assertEqual((5, True, 2, False), (first["count"], first["more"], second["count"], second["more"]))
        self.assertFalse({v["target_ref"] for v in first["results"]} & {v["target_ref"] for v in second["results"]})
        for args in ({"limit": 0}, {"limit": 6}, {"limit": True}, {"offset": -1}, {"offset": MAX_OFFSET + 1},
                     {"offset": True}, {"target_ref": first["results"][0]["target_ref"], "query": "alpha"}):
            self.error("invalid_work_query", self.read, **args)

    def test_limits_reject_instead_of_truncating(self):
        for value in ("", "  ", None, 123, "a" * 32769, "bad\ud800"):
            self.error("invalid_content", self.remember, value)
        for value in ("", "  ", None, 1, ["tag"], "a" * 161, "bad\udfff"):
            self.error("invalid_tag", self.remember, tag=value)
        result = self.remember("🙂" * 32768, "标" * 160)
        self.assertEqual("🙂" * 32768, self.read(target_ref=result["target_ref"])["results"][0]["content"])

    def test_serialized_output_budget_keeps_whole_records_and_resumes(self):
        content = "a" + "\x01" * 32767
        for index in range(5):
            self.remember(content, f"record {index}")
        first = self.read()
        self.assertTrue(first["more"])
        self.assertLessEqual(len(json.dumps(first, ensure_ascii=False).encode()), MAX_RESULT_BYTES)
        self.assertTrue(all(item["content"] == content for item in first["results"]))
        second = self.read(offset=first["next_offset"])
        self.assertEqual(5, first["count"] + second["count"])

    def test_atlas_token_syntax_blocks_body_tag_partial_edits_and_old_reads(self):
        token = "orb_atlas_" + "A" * 43
        for content, tag in ((token, "tag"), ("body", token)):
            self.error("credential_or_secret_detected", self.remember, content, tag)
        saved = self.remember()
        for field in ("content", "tag"):
            self.error("credential_or_secret_detected", self.revise, saved["target_ref"], **{field: token})
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE work_memory_versions SET tag=?", (token,))
            db.commit()
        self.error("credential_or_secret_detected", self.read, target_ref=saved["target_ref"])
        self.error("credential_or_secret_detected", self.revise, saved["target_ref"], content="changed")

    def test_static_credential_detection_blocks_save_edit_and_read(self):
        for content, tag in (("API_KEY=synthetic-secret-value", "safe"), ("safe", "password=synthetic-secret")):
            self.error("credential_or_secret_detected", self.remember, content, tag)
        saved = self.remember()
        self.error("credential_or_secret_detected", self.revise, saved["target_ref"], content="Authorization: Bearer synthetic-secret-value")
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE work_memory_versions SET content='API_KEY=synthetic-secret-value'")
            db.commit()
        self.error("credential_or_secret_detected", self.read, target_ref=saved["target_ref"])

    def test_empty_revision_does_not_append_version(self):
        result = self.remember()
        before = self.dump()
        self.error("empty_work_revision", self.revise, result["target_ref"])
        self.assertEqual(before, self.dump())

    def test_create_idempotency_and_conflicting_retry(self):
        key = hashlib.sha256(b"synthetic-call").hexdigest()
        first = self.remember(request_key=key)
        second = self.remember(request_key=key)
        self.assertEqual(first["target_ref"], second["target_ref"])
        self.assertTrue(second["stored"])
        self.assertFalse(second["state_changed"])
        self.assertTrue(second["idempotent_replay"])
        self.error("request_conflict", self.remember, "changed", request_key=key)
        self.assertEqual(1, self.read()["count"])

    def test_revision_idempotency_returns_original_result_not_latest(self):
        original = self.remember()
        key = hashlib.sha256(b"synthetic-revision").hexdigest()
        first = self.revise(original["target_ref"], content="second", request_key=key)
        self.revise(first["target_ref"], content="third")
        replay = self.revise(original["target_ref"], content="second", request_key=key)
        self.assertEqual(first["target_ref"], replay["target_ref"])
        self.assertFalse(replay["state_changed"])

    def test_idempotency_scope_is_separate_for_other_identity(self):
        key = "a" * 64
        first = self.remember(request_key=key)
        second = self.store.remember(owner_id="other", model_id=SCOPE["model_id"], content="other", tag="other", request_key=key)
        self.assertNotEqual(first["target_ref"], second["target_ref"])

    def test_concurrent_same_request_creates_one_record(self):
        second = WorkMemoryStore(self.database)
        def create(store):
            return store.remember(**SCOPE, content="same", tag="same", request_key="b" * 64)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, (self.store, second)))
        self.assertEqual(results[0]["target_ref"], results[1]["target_ref"])
        self.assertEqual(1, sum(item["state_changed"] for item in results))

    def test_invalid_request_key_cannot_store_raw_execution_secret(self):
        self.error("invalid_request_key", self.remember, request_key="stexec_synthetic-secret")

    def test_transaction_guard_blocks_create_and_edit_atomically(self):
        original = self.remember()
        before = self.dump()
        def deny(_):
            raise WorkMemoryError("module_one_required")
        self.error("module_one_required", self.remember, _write_guard=deny)
        self.error("module_one_required", self.revise, original["target_ref"], tag="other", _write_guard=deny)
        self.assertEqual(before, self.dump())

    def test_runtime_rechecks_execution_scope_and_registry(self):
        for owner, expected in (("foreign", "execution_owner_mismatch"), (SCOPE["owner_id"], "execution_registry_unavailable")):
            claim = ExecutionClaim("synthetic-ref", owner, SCOPE["model_id"], "wake", "batch", "call", "remember_work_memory", "epoch", "claim", str(self.database))
            token = _BOUND_CLAIM.set(claim)
            try:
                with self.assertRaisesRegex(ExecutionBindingError, expected):
                    self.remember()
            finally:
                _BOUND_CLAIM.reset(token)
        self.assertEqual(0, self.read()["count"])

    def test_persistence_and_full_backup_include_history_without_fourth_database(self):
        first = self.remember()
        new = self.revise(first["target_ref"], tag="new")
        other = WorkMemoryStore(self.database)
        self.assertEqual("new", other.recall(**SCOPE, target_ref=new["target_ref"])["results"][0]["tag"])
        backup = Path(self.temp.name) / "backup.sqlite"
        with closing(sqlite3.connect(self.database)) as source, closing(sqlite3.connect(backup)) as dest:
            source.backup(dest)
            self.assertEqual(2, dest.execute("SELECT COUNT(*) FROM work_memory_versions").fetchone()[0])
            self.assertEqual("unchanged", dest.execute("SELECT body FROM business_sentinel").fetchone()[0])
        self.assertEqual({"synthetic-main.sqlite", "backup.sqlite"}, {path.name for path in Path(self.temp.name).iterdir()})

    def test_query_is_readonly_and_does_not_touch_business_tables(self):
        self.remember()
        before = self.dump()
        self.read()
        self.read(query="alpha")
        self.assertEqual(before, self.dump())

    def test_no_ordinary_table_registration(self):
        self.remember("distinct work original", "distinct work tag")
        with closing(sqlite3.connect(self.database)) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual({"business_sentinel", "work_memory_items", "work_memory_versions", "work_memory_lifecycle_versions"}, names)

    def test_partial_schema_and_triggers_fail_without_migration(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TRIGGER malicious AFTER INSERT ON work_memory_versions BEGIN UPDATE business_sentinel SET body='changed'; END")
            db.commit()
        self.error("work_memory_unavailable", self.remember)
        self.error("work_memory_unavailable", WorkMemoryStore, self.database)

    def test_missing_main_database_is_not_created(self):
        missing = Path(self.temp.name) / "missing.sqlite"
        self.error("work_memory_unavailable", WorkMemoryStore, missing)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
