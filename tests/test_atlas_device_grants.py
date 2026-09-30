"""Synthetic main-DB delegation tests; never use deployment data or tokens."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from runtime.atlas_device_grants import (
    AtlasDeviceGrants, AtlasGrantError, INDEX, SCOPE, TABLE, TABLE_SQL,
    capabilities, from_env, registration, revocation, strict_object, token_verifier,
)


KEY = b"k" * 32
EMPTY_GRAPH = b'{"schema":"orbis.st.atlas/1","generatedAt":"2026-01-01T00:00:00Z","truncated":false,"stars":[],"edges":[]}'


def request(number=1, device=1):
    return {"schema": "orbis.st.atlas-register/1", "requestId": f"{number:032x}",
            "deviceId": f"{device:032x}", "verifier": hashlib.sha256(f"synthetic-{number}".encode()).hexdigest()}


def revoke_request(number=1):
    return {"schema": "orbis.st.atlas-revoke/1", "requestId": f"{number:032x}"}


class AtlasDeviceGrantsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "synthetic-main.sqlite"
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE business_sentinel (id INTEGER PRIMARY KEY, body TEXT)")
            db.execute("INSERT INTO business_sentinel VALUES (1,'synthetic private text')")
            db.commit()

    def store(self, **overrides):
        values = dict(enabled=True, id_key=KEY, reader=lambda: EMPTY_GRAPH)
        values.update(overrides)
        return AtlasDeviceGrants(self.database, "owner-fixture", "model-fixture", **values)

    def error(self, status, code, callback, *args):
        with self.assertRaises(AtlasGrantError) as caught:
            callback(*args)
        self.assertEqual((status, code), (caught.exception.status, caught.exception.code))
        self.assertEqual(code, str(caught.exception))

    def rows(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute(f"SELECT * FROM {TABLE} ORDER BY grant_id").fetchall()

    def test_absent_configuration_does_not_access_database(self):
        absent = Path(self.temp.name) / "never-created.sqlite"
        self.assertIsNone(from_env(absent, "owner", "model", {}))
        self.assertFalse(absent.exists())

    def test_disabled_configuration_does_not_create_or_inspect_schema(self):
        store = self.store(enabled=False)
        self.assertFalse(capabilities(store.enabled)["enabled"])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual([], db.execute("SELECT name FROM sqlite_master WHERE name=?", (TABLE,)).fetchall())
        self.error(403, "atlas_disabled", store.register, request())
        self.error(403, "atlas_disabled", store.snapshot, request()["verifier"])

    def test_initialization_changes_only_new_operational_schema(self):
        with closing(sqlite3.connect(self.database)) as db:
            before = list(db.iterdump())
        self.store()
        with closing(sqlite3.connect(self.database)) as db:
            after = list(db.iterdump())
        self.assertEqual(before, [line for line in after if TABLE not in line and INDEX not in line])

    def test_registration_is_idempotent_and_contains_no_verifier(self):
        store = self.store()
        first = store.register(request())
        self.assertEqual(first, store.register(request()))
        self.assertEqual({"schema", "requestId", "grantId", "status", "scope", "expiresAt"}, set(first))
        self.assertEqual(SCOPE, first["scope"])
        self.assertIsNone(first["expiresAt"])
        self.assertEqual(1, len(self.rows()))
        self.assertNotIn(request()["verifier"], json.dumps(first))

    def test_same_request_cannot_change_device_or_verifier(self):
        store = self.store()
        store.register(request())
        for field in ("deviceId", "verifier"):
            changed = dict(request(), **{field: "f" * len(request()[field])})
            self.error(409, "request_conflict", store.register, changed)
        self.assertEqual(1, len(self.rows()))

    def test_verifier_cannot_be_registered_again_under_another_request(self):
        store = self.store()
        store.register(request())
        changed = dict(request(2), verifier=request()["verifier"])
        self.error(409, "request_conflict", store.register, changed)

    def test_device_rotation_revokes_old_and_replay_never_revives(self):
        store = self.store()
        first = store.register(request())
        second = store.register(request(2))
        self.assertNotEqual(first["grantId"], second["grantId"])
        self.assertEqual("revoked", store.register(request())["status"])
        self.error(401, "unauthorized", store.snapshot, request()["verifier"])
        self.assertEqual([], store.snapshot(request(2)["verifier"])["stars"])

    def test_self_revoke_is_idempotent_and_disabled_policy_keeps_revocation(self):
        store = self.store()
        store.register(request())
        disabled = self.store(enabled=False)
        expected = {"schema": "orbis.st.atlas-revoke-result/1", "status": "revoked"}
        self.assertEqual(expected, disabled.revoke(request()["verifier"], revoke_request()))
        before = self.rows()
        self.assertEqual(expected, disabled.revoke(request()["verifier"], revoke_request(2)))
        self.assertEqual(before, self.rows())
        self.assertEqual("revoked", store.register(request())["status"])

    def test_revoke_cannot_target_another_grant(self):
        store = self.store()
        store.register(request())
        store.register(request(2, 2))
        self.error(400, "invalid_request", store.revoke, request()["verifier"], dict(revoke_request(), grantId="f" * 32))
        store.revoke(request()["verifier"], revoke_request())
        self.assertEqual([], store.snapshot(request(2)["verifier"])["stars"])

    def test_unknown_or_forbidden_verifier_never_reads_or_revokes(self):
        forbidden = request()["verifier"]
        store = self.store(forbidden=frozenset({forbidden}))
        for value in (forbidden, "g" * 64, "", None, 12):
            self.error(401, "unauthorized", store.snapshot, value)
            self.error(401, "unauthorized", store.revoke, value, revoke_request())
        self.error(401, "unauthorized", store.register, request())
        self.error(401, "unauthorized", store.snapshot, request(2)["verifier"])

    def test_forbidden_policy_blocks_preexisting_grant(self):
        self.store().register(request())
        store = self.store(forbidden=frozenset({request()["verifier"]}))
        self.error(401, "unauthorized", store.snapshot, request()["verifier"])

    def test_scope_is_server_owned_and_cross_scope_request_collides(self):
        self.store().register(request())
        other = AtlasDeviceGrants(self.database, "other-owner", "model-fixture", enabled=True, id_key=KEY)
        self.error(401, "unauthorized", other.snapshot, request()["verifier"])
        self.error(401, "unauthorized", other.revoke, request()["verifier"], revoke_request())
        self.error(409, "request_conflict", other.register, request())

    def test_registration_rejects_extra_fields_and_invalid_types(self):
        store = self.store()
        for field in ("owner_id", "model_id", "scope", "token", "expiresAt", "name", "url"):
            self.error(400, "invalid_request", store.register, dict(request(), **{field: "untrusted"}))
        for field in ("requestId", "deviceId", "verifier"):
            for value in (None, 1, True, [], "A" * len(request()[field]), request()[field] + "\n"):
                self.error(400, "invalid_request", registration, dict(request(), **{field: value}))
        self.error(400, "invalid_request", revocation, dict(revoke_request(), requestId=True))
        self.assertEqual([], self.rows())

    def test_strict_json_rejects_duplicates_nonutf8_depth_and_nonobjects(self):
        for raw in (b'{"a":1,"a":2}', b'{"x":{"y":1,"y":2}}', b"\xff", b"[]", b"null",
                    b'{"v":NaN}', b'{"v":Infinity}', b'{"v":' + b"[" * 1100 + b"]" * 1100 + b"}",
                    b"{}" + b" " * 2048, b""):
            self.error(400, "invalid_request", strict_object, raw)

    def test_token_shape_is_distinct_from_high_privilege_tokens(self):
        token = "orb_atlas_" + "a" * 43
        self.assertEqual(hashlib.sha256(token.encode()).hexdigest(), token_verifier(token))
        for bad in ("high-authority-token", "a" * 64, token + "=", token + "\n", None):
            self.error(401, "unauthorized", token_verifier, bad)

    def test_eight_device_limit_and_rotation_does_not_consume_ninth_slot(self):
        store = self.store()
        for number in range(1, 9):
            store.register(request(number, number))
        self.error(429, "grant_limit_reached", store.register, request(9, 9))
        self.assertEqual("active", store.register(request(10, 1))["status"])
        store.revoke(request(2)["verifier"], revoke_request())
        self.assertEqual("active", store.register(request(9, 9))["status"])

    def test_tombstone_limit_keeps_last_active_and_allows_idempotent_retry(self):
        store = self.store()
        for number in range(1, 513):
            store.register(request(number))
        self.error(429, "grant_limit_reached", store.register, request(513))
        self.assertEqual("active", store.register(request(512))["status"])
        self.assertEqual("revoked", store.register(request(1))["status"])
        self.assertEqual(512, len(self.rows()))

    def test_concurrent_duplicate_requests_create_exactly_one_grant(self):
        first, second = self.store(), self.store()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda store: store.register(request()), (first, second)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(1, len(self.rows()))

    def test_read_gate_fails_fast_and_does_not_serialize_chat(self):
        entered, release = threading.Event(), threading.Event()
        def reader():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("synthetic reader timeout")
            return EMPTY_GRAPH
        store = self.store(reader=reader)
        store.register(request())
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(store.snapshot, request()["verifier"])
            try:
                self.assertTrue(entered.wait(2))
                self.error(503, "atlas_unavailable", store.snapshot, request()["verifier"])
            finally:
                release.set()
            self.assertEqual([], pending.result()["stars"])

    def test_revoke_from_another_store_during_read_rejects_snapshot(self):
        second = self.store()
        def reader():
            second.revoke(request()["verifier"], revoke_request())
            return EMPTY_GRAPH
        first = self.store(reader=reader)
        first.register(request())
        self.error(401, "unauthorized", first.snapshot, request()["verifier"])

    def test_snapshot_does_not_write_authorization_or_business_rows(self):
        store = self.store()
        store.register(request())
        with closing(sqlite3.connect(self.database)) as db:
            before = list(db.iterdump())
        store.snapshot(request()["verifier"])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_invalid_reader_output_is_sanitized_as_unavailable(self):
        for raw in (b"invalid private content", b"{}" + b" " * (1024 * 1024)):
            store = self.store(reader=lambda raw=raw: raw)
            store.register(request())
            self.error(503, "atlas_unavailable", store.snapshot, request()["verifier"])

    def test_backup_contains_authorizations_and_restores_revocations(self):
        store = self.store()
        store.register(request())
        store.revoke(request()["verifier"], revoke_request())
        backup = Path(self.temp.name) / "full-backup.sqlite"
        with closing(sqlite3.connect(self.database)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
            self.assertEqual(1, target.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE status='revoked'").fetchone()[0])
            self.assertEqual("synthetic private text", target.execute("SELECT body FROM business_sentinel").fetchone()[0])

    def test_partial_index_cannot_be_silently_recreated(self):
        self.store()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute(f"DROP INDEX {INDEX}")
            db.commit()
        self.error(503, "atlas_unavailable", self.store)

    def test_table_lookalike_without_constraints_rejected_without_changes(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute(TABLE_SQL.replace("TEXT NOT NULL PRIMARY KEY", "TEXT PRIMARY KEY"))
            db.commit()
            before = list(db.iterdump())
        self.error(503, "atlas_unavailable", self.store)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_trigger_or_extra_index_rejected_before_read_or_write(self):
        store = self.store()
        store.register(request())
        with closing(sqlite3.connect(self.database)) as db:
            db.execute(f"CREATE TRIGGER atlas_bad AFTER INSERT ON {TABLE} BEGIN UPDATE business_sentinel SET body='tampered'; END")
            db.commit()
        self.error(503, "atlas_unavailable", store.snapshot, request()["verifier"])
        self.error(503, "atlas_unavailable", store.register, request(2))
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DROP TRIGGER atlas_bad")
            db.execute(f"CREATE INDEX atlas_extra ON {TABLE}(device_id)")
            db.commit()
        self.error(503, "atlas_unavailable", self.store)

    def test_missing_database_not_created_by_initialization(self):
        target = Path(self.temp.name) / "missing.sqlite"
        self.error(503, "atlas_unavailable", lambda: AtlasDeviceGrants(target, "owner", "model", enabled=True, id_key=KEY))
        self.assertFalse(target.exists())

    def test_configuration_pair_and_forbidden_list_fail_closed(self):
        key = KEY.hex()
        valid = {"STBRAIN_ATLAS_BOOTSTRAP_ENABLED": "0", "STBRAIN_ATLAS_ID_KEY": key,
                 "STBRAIN_ATLAS_FORBIDDEN_VERIFIERS_JSON": json.dumps([hashlib.sha256(key.encode()).hexdigest()])}
        self.assertFalse(from_env(self.database, "owner", "model", valid).enabled)
        invalid = [dict(valid, STBRAIN_ATLAS_BOOTSTRAP_ENABLED="yes"),
                   dict(valid, STBRAIN_ATLAS_ID_KEY=key.upper()),
                   dict(valid, STBRAIN_ATLAS_FORBIDDEN_VERIFIERS_JSON='[]'),
                   dict(valid, STBRAIN_ATLAS_FORBIDDEN_VERIFIERS_JSON=json.dumps(["0" * 64])),
                   {name: value for name, value in valid.items() if name != "STBRAIN_ATLAS_ID_KEY"}]
        # KEY is hexadecimal digits only; use letters to exercise lower-case validation.
        invalid[1]["STBRAIN_ATLAS_ID_KEY"] = "AA" * 32
        for env in invalid:
            with self.assertRaisesRegex(ValueError, "^atlas_configuration_invalid$"):
                from_env(self.database, "owner", "model", env)


if __name__ == "__main__":
    unittest.main()
