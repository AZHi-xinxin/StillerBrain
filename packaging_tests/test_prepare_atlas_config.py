"""Synthetic atlas configuration tests; never touch an installed instance."""
from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from packaging_tests.test_install_stiller import installer, PORTS, ROOT

atlas = installer.helper("atlas_config")
prepare = installer.helper("prepare_atlas_config")
starter = installer.helper("run_component")


def new_config(path):
    return installer.make_config(path, "https://provider.invalid/v1", "synthetic-model",
                                 "synthetic-upstream-only-" + "x" * 32, PORTS)


class AtlasConfigTests(unittest.TestCase):
    def test_new_installer_enabled_and_all_seven_credentials_protected(self):
        config = new_config(Path("C:/synthetic-private"))
        self.assertEqual("1", config[atlas.ENABLED])
        self.assertRegex(config[atlas.ID_KEY], r"^[0-9a-f]{64}$")
        hashes = json.loads(config[atlas.FORBIDDEN])
        self.assertEqual(sorted({atlas.digest(config[name]) for name in (*atlas.REQUIRED_SECRETS, atlas.ID_KEY)}), hashes)
        self.assertEqual(7, len(hashes))
        self.assertEqual(7, len(atlas.secret_values(config)))
        self.assertEqual(config, atlas.prepare(config))

    def test_new_installations_generate_independent_atlas_keys(self):
        first, second = new_config(Path("C:/a")), new_config(Path("C:/b"))
        self.assertNotEqual(first[atlas.ID_KEY], second[atlas.ID_KEY])
        for config in (first, second):
            self.assertNotIn(config[atlas.ID_KEY], [config[name] for name in atlas.REQUIRED_SECRETS])

    def test_missing_group_requires_explicit_enable(self):
        config = {key: value for key, value in new_config(Path("C:/a")).items() if key not in atlas.ATLAS_KEYS}
        with self.assertRaisesRegex(ValueError, "atlas_enable_requires_explicit_option"):
            atlas.prepare(config)
        enabled = atlas.prepare(config, enable_missing=True)
        self.assertEqual("1", enabled[atlas.ENABLED])
        self.assertEqual(config, {key: value for key, value in enabled.items() if key not in atlas.ATLAS_KEYS})

    def test_explicit_disabled_is_preserved_even_with_enable(self):
        config = new_config(Path("C:/a"))
        config[atlas.ENABLED] = "0"
        self.assertEqual(config, atlas.prepare(config, enable_missing=True))
        self.assertEqual({atlas.ENABLED: "0"}, atlas.prepare({atlas.ENABLED: "0"}, enable_missing=True))

    def test_existing_key_and_previous_hashes_retained_with_route_secret_update(self):
        config = new_config(Path("C:/a"))
        previous = json.loads(config[atlas.FORBIDDEN]) + ["f" * 64]
        config[atlas.FORBIDDEN] = json.dumps(previous)
        config["STBRAIN_UPSTREAM_ROUTE_EXTRA_API_KEY"] = "synthetic-route-" + "r" * 32
        config["STBRAIN_GATEWAY_MANAGEMENT_TOKEN"] = "synthetic-management-" + "m" * 32
        result = atlas.prepare(config)
        self.assertEqual(config[atlas.ID_KEY], result[atlas.ID_KEY])
        self.assertTrue(set(previous) < set(json.loads(result[atlas.FORBIDDEN])))
        for name in ("STBRAIN_UPSTREAM_ROUTE_EXTRA_API_KEY", "STBRAIN_GATEWAY_MANAGEMENT_TOKEN"):
            self.assertIn(atlas.digest(config[name]), json.loads(result[atlas.FORBIDDEN]))

    def test_partial_or_malformed_group_refused_not_repaired(self):
        config = new_config(Path("C:/a"))
        cases = [{key: value for key, value in config.items() if key != atlas.ID_KEY},
                 {**config, atlas.ENABLED: "yes"}, {**config, atlas.ID_KEY: "AA" * 32},
                 {**config, atlas.FORBIDDEN: "[]"}, {**config, atlas.FORBIDDEN: "[" * 1500}]
        for case in cases:
            with self.subTest(keys=list(case)), self.assertRaisesRegex(ValueError, "atlas_configuration_invalid"):
                atlas.prepare(case, enable_missing=True)

    def test_missing_key_cannot_replace_previous_schema_identity(self):
        config = {key: value for key, value in new_config(Path("C:/a")).items() if key not in atlas.ATLAS_KEYS}
        with patch.object(atlas.secrets, "token_hex", side_effect=AssertionError("no new key")), \
                self.assertRaisesRegex(ValueError, "atlas_existing_schema_requires_original_key"):
            atlas.prepare(config, enable_missing=True, prior_atlas_schema=True)

    def test_id_key_cannot_reuse_any_channel_secret(self):
        config = new_config(Path("C:/a"))
        config["STBRAIN_UPSTREAM_ROUTE_EXTRA_API_KEY"] = config[atlas.ID_KEY]
        with self.assertRaisesRegex(ValueError, "atlas_id_key_must_be_independent"):
            atlas.prepare(config)

    def test_forbidden_limit_never_truncates(self):
        config = new_config(Path("C:/a"))
        for number in range(33):
            config[f"STBRAIN_UPSTREAM_ROUTE_{number}_API_KEY"] = f"synthetic-route-{number}"
        with self.assertRaisesRegex(ValueError, "atlas_forbidden_limit_exceeded"):
            atlas.prepare(config)


class AtlasCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="atlas-candidate-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name).resolve()
        self.private = self.parent / "old-private"
        self.private.mkdir()
        self.source = self.private / "config.env"
        self.output = self.parent / "new-candidate"
        self.config = {key: value for key, value in new_config(self.private).items() if key not in atlas.ATLAS_KEYS}
        self.config["STBRAIN_CUSTOM_VALUE"] = "preserve exactly = spaces"
        modules = {"install_stiller": installer, "run_component": starter, "atlas_config": atlas}
        self.addCleanup(patch.stopall)
        patch.object(prepare, "helper", side_effect=lambda name: modules[name]).start()
        self.protect = patch.object(installer, "protect_directory").start()
        patch.object(installer.subprocess, "run", side_effect=AssertionError("no command")).start()
        patch.object(installer.subprocess, "Popen", side_effect=AssertionError("no service")).start()
        self.write_config()

    def write_config(self):
        # Fixture creation only; product code uses create-only private writes.
        self.source.write_bytes(("# synthetic comment\r\n" + "".join(key + "=" + value + "\r\n"
                                for key, value in self.config.items())).encode("utf-8"))

    def create(self, **changes):
        return prepare.create_candidate(self.source, self.output, enable=True, **changes)

    def test_candidate_create_only_no_original_or_db_write_no_secret_receipt(self):
        before = self.source.read_bytes()
        receipt = self.create()
        self.assertEqual(before, self.source.read_bytes())
        self.assertFalse(Path(self.config["STBRAIN_DB_PATH"]).exists())
        self.protect.assert_called_once_with(self.output)
        candidate = (self.output / "config.env.candidate").read_bytes()
        self.assertTrue(candidate.startswith(before))
        self.assertEqual(hashlib.sha256(candidate).hexdigest(), receipt["candidate_sha256"])
        self.assertEqual({"config.env.candidate", "candidate.json"}, {item.name for item in self.output.iterdir()})
        self.assertEqual(list(atlas.ATLAS_KEYS), receipt["changed_keys"])
        self.assertFalse(receipt["applied"] or receipt["services_started"] or receipt["databases_modified"])
        config = starter.parse_config(candidate.decode())
        for value in atlas.secret_values(config):
            self.assertNotIn(value, json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "atlas_candidate_requires_new_directory"):
            self.create()

    def test_existing_schema_refused_even_if_table_empty(self):
        database = Path(self.config["STBRAIN_DB_PATH"])
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE orbis_atlas_device_grants (id TEXT)")
            db.commit()
        before = database.read_bytes()
        with self.assertRaisesRegex(ValueError, "atlas_existing_schema_requires_original_key"):
            self.create()
        self.assertEqual(before, database.read_bytes())
        self.assertFalse(self.output.exists())
        self.assertEqual({"config.env", database.name}, {item.name for item in self.private.iterdir()})

    def test_non_atlas_schema_inspected_without_reading_memory_rows(self):
        database = Path(self.config["STBRAIN_DB_PATH"])
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE synthetic_memory (body TEXT)")
            db.execute("INSERT INTO synthetic_memory VALUES ('synthetic-never-exported')")
            db.commit()
        before = database.read_bytes()
        self.assertTrue(self.create()["schema_checked"])
        self.assertEqual(before, database.read_bytes())
        self.assertEqual({"config.env", database.name}, {item.name for item in self.private.iterdir()})
        self.assertNotIn("synthetic-never-exported", (self.output / "candidate.json").read_text())

    def test_nonempty_wal_refused_without_opening_db(self):
        Path(self.config["STBRAIN_DB_PATH"] + "-wal").write_bytes(b"synthetic")
        with patch.object(prepare.sqlite3, "connect", side_effect=AssertionError("no open")), \
                self.assertRaisesRegex(ValueError, "atlas_database_requires_quiet_snapshot"):
            self.create()
        self.assertFalse(self.output.exists())

    def test_explicit_zero_preserved_without_database_access(self):
        self.config[atlas.ENABLED] = "0"
        self.write_config()
        before = self.source.read_bytes()
        with patch.object(prepare, "schema_evidence", side_effect=AssertionError("no schema")):
            result = self.create()
        self.assertFalse(result["enabled"])
        self.assertEqual([], result["changed_keys"])
        self.assertEqual(before, (self.output / "config.env.candidate").read_bytes())

    def test_existing_key_preserved_without_schema_access(self):
        self.config = atlas.configure_new(self.config)
        self.write_config()
        with patch.object(prepare, "schema_evidence", side_effect=AssertionError("no schema")):
            result = self.create()
        self.assertTrue(result["existing_id_key_preserved"])
        candidate = starter.parse_config((self.output / "config.env.candidate").read_text())
        self.assertEqual(self.config[atlas.ID_KEY], candidate[atlas.ID_KEY])

    def test_source_change_before_write_refuses_candidate(self):
        original = prepare.read_source
        count = 0
        def changed(path):
            nonlocal count
            count += 1
            raw, state = original(path)
            return (raw + b"# changed\n", state) if count > 1 else (raw, state)
        with patch.object(prepare, "read_source", side_effect=changed), self.assertRaisesRegex(ValueError, "atlas_source_changed"):
            self.create()
        self.assertFalse(self.output.exists())

    def test_source_change_after_candidate_write_leaves_no_completion_receipt(self):
        original = prepare.read_source
        count = 0
        def changed(path):
            nonlocal count
            count += 1
            raw, state = original(path)
            return (raw + b"# changed\n", state) if count > 2 else (raw, state)
        with patch.object(prepare, "read_source", side_effect=changed), self.assertRaisesRegex(ValueError, "atlas_source_changed"):
            self.create()
        self.assertTrue((self.output / "config.env.candidate").is_file())
        self.assertFalse((self.output / "candidate.json").exists())

    def test_new_database_appearing_after_schema_check_refused(self):
        with patch.object(prepare, "schema_evidence", return_value=(False, None)), \
                patch.object(prepare, "database_state", return_value=(1, 2, 3, 4)), \
                self.assertRaisesRegex(ValueError, "atlas_database_changed"):
            self.create()
        self.assertFalse(self.output.exists())

    def test_hard_linked_secret_source_refused(self):
        os.link(self.source, self.private / "same-secret-file")
        with self.assertRaisesRegex(ValueError, "atlas_source_not_private_regular_file"):
            self.create()
        self.assertFalse(self.output.exists())

    def test_missing_enable_never_queries_schema_or_generates_key(self):
        with patch.object(prepare, "schema_evidence", side_effect=AssertionError("no schema")), \
                patch.object(atlas.secrets, "token_hex", side_effect=AssertionError("no random")), \
                self.assertRaisesRegex(ValueError, "atlas_enable_requires_explicit_option"):
            prepare.create_candidate(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_permissions_applied_before_secret_write(self):
        self.protect.side_effect = OSError("synthetic ACL failure")
        with self.assertRaises(OSError):
            self.create()
        self.assertEqual([], list(self.output.iterdir()))

    def test_cli_redacts_parse_failures_and_does_not_create_output(self):
        self.source.write_bytes(b"private-value-is-not-a-config-line\n")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = prepare.main(["--config", str(self.source), "--output", str(self.output), "--enable"])
        self.assertEqual(1, code)
        self.assertNotIn("private-value", out.getvalue() + err.getvalue())
        self.assertFalse(self.output.exists())

    def test_source_tree_or_symlink_path_refused(self):
        with self.assertRaises(ValueError):
            prepare.create_candidate(self.source, ROOT / "synthetic-private", enable=True)
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == self.private or original(path)), self.assertRaises(ValueError):
            self.create()
        self.assertFalse(self.output.exists())

    def test_oversized_source_is_refused(self):
        self.source.write_bytes(b"#" * (prepare.LIMIT + 1))
        with self.assertRaisesRegex(ValueError, "atlas_source_not_private_regular_file"):
            self.create()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
