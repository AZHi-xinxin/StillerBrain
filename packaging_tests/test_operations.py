"""Operations tests use only explicit temporary synthetic databases and fake children."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("stiller_operations_tests", ROOT / "scripts" / "stiller_ops.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class FakeChild:
    def __init__(self, exit_code=None, timeout_once=False):
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False
        self.timeout_once = timeout_once

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True

    def wait(self, timeout):
        if self.timeout_once:
            self.timeout_once = False
            raise subprocess.TimeoutExpired("synthetic-child", timeout)
        self.exit_code = 0
        return 0

    def kill(self):
        self.killed = True


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stiller-synthetic-ops-")
        self.addCleanup(self.temporary.cleanup)
        self.private = Path(self.temporary.name)
        self.source = self.private / "source-data"
        self.source.mkdir()
        self.config = {key: "synthetic-only-" + str(index) * 40 for index, key in enumerate(ops.starter.SECRET_KEYS)}
        self.config.update({key: str(self.source / (role + ".db")) for role, key in ops.DATABASES.items()})
        self.config.update(STBRAIN_OWNER_ID="owner:synthetic", STBRAIN_MODEL_ID="model:synthetic",
                           STBRAIN_HUMAN_ACTOR_ID="human:synthetic", STBRAIN_EXECUTION_EPOCH="synthetic-operations-1",
                           STBRAIN_REQUIRE_EXECUTION_BINDING="1", STBRAIN_UPSTREAM_BASE_URL="https://model.test/v1",
                           STBRAIN_UPSTREAM_API_KEY="synthetic-upstream", STBRAIN_GATEWAY_MODEL="synthetic",
                           STBRAIN_UPSTREAM_MODEL="synthetic")
        sockets = []
        try:
            for name in ("MCP", "CONTROL", "GATEWAY"):
                probe = socket.socket(); sockets.append(probe)
                probe.bind(("127.0.0.1", 0))
                self.config[f"STBRAIN_{name}_HOST"] = "127.0.0.1"
                self.config[f"STBRAIN_{name}_PORT"] = str(probe.getsockname()[1])
        finally:
            for probe in sockets:
                probe.close()
        self.config["STBRAIN_CONTROL_URL"] = "http://127.0.0.1:" + self.config["STBRAIN_CONTROL_PORT"]
        for role, key in ops.DATABASES.items():
            with contextlib.closing(sqlite3.connect(self.config[key])) as connection:
                connection.execute("CREATE TABLE memory (id INTEGER PRIMARY KEY, body TEXT, data BLOB)")
                connection.execute("INSERT INTO memory VALUES (?, ?, ?)", (1, "Synthetic " + role, b"\x00\x01"))
                connection.execute("PRAGMA user_version=7")
                connection.commit()

    def make_backup(self, name="snapshot"):
        target = self.private / name
        ops.backup(self.config, target, confirm_stopped=True)
        return target

    def private_config_file(self):
        path = self.private / "synthetic.env"
        path.write_text("\n".join(key + "=" + value for key, value in self.config.items()), encoding="utf-8")
        return path

    def test_check_reuses_strict_config_and_does_not_launch(self):
        path = self.private_config_file()
        with patch.object(ops.subprocess, "Popen") as launch, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, ops.main(["check", "--config", str(path)]))
        launch.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(3, result["valid_components"])
        self.assertFalse(result["services_started"])
        self.assertFalse(result["health_checked"])
        for value in (self.config[key] for key in ops.starter.SECRET_KEYS):
            self.assertNotIn(value, output.getvalue())

    def test_disabled_binding_or_shared_credentials_still_rejected(self):
        for updates in ({"STBRAIN_REQUIRE_EXECUTION_BINDING": "0"},
                        {"STBRAIN_HOST_TOKEN": self.config["STBRAIN_MCP_TOKEN"]}):
            with self.subTest(updates=list(updates)):
                current = dict(self.config); current.update(updates)
                with self.assertRaises(ValueError):
                    ops.start_stack(current, popen=lambda *args, **kwargs: self.fail("must not launch"))

    def test_start_three_children_one_console_ctrl_c_stops_only_owned_children(self):
        children = []
        commands = []
        def create(command, **kwargs):
            commands.append((command, kwargs))
            child = FakeChild(); children.append(child); return child
        def interrupt(_):
            raise KeyboardInterrupt()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, ops.start_stack(self.config, popen=create, sleeper=interrupt, job_factory=lambda: None))
        self.assertEqual([ops.starter.COMPONENTS[name] for name in ("control", "mcp", "gateway")],
                         [command[-1] for command, _ in commands])
        self.assertTrue(all(child.terminated for child in children))
        self.assertTrue(all(settings["env"]["STBRAIN_REQUIRE_EXECUTION_BINDING"] == "1" for _, settings in commands))

    def test_partial_start_failure_cleans_up_already_created_child(self):
        child = FakeChild()
        def create(*args, **kwargs):
            if not hasattr(create, "called"):
                create.called = True; return child
            raise OSError("synthetic launch failure")
        with self.assertRaises(OSError):
            ops.start_stack(self.config, popen=create, job_factory=lambda: None)
        self.assertTrue(child.terminated)

    def test_child_exit_stops_others_and_hung_child_is_killed(self):
        children = [FakeChild(exit_code=1), FakeChild(timeout_once=True), FakeChild()]
        remaining = iter(children)
        with self.assertRaises(ops.OperationError), contextlib.redirect_stdout(io.StringIO()):
            ops.start_stack(self.config, popen=lambda *args, **kwargs: next(remaining), job_factory=lambda: None)
        self.assertTrue(children[1].killed)
        self.assertTrue(children[2].terminated)

    def test_backup_requires_explicit_offline_confirmation(self):
        target = self.private / "refused"
        with self.assertRaisesRegex(ops.OperationError, "confirm-stopped"):
            ops.backup(self.config, target, confirm_stopped=False)
        self.assertFalse(target.exists())

    def test_occupied_configured_port_refuses_backup_without_output(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0)); listener.listen()
            self.config["STBRAIN_MCP_PORT"] = str(listener.getsockname()[1])
            with self.assertRaisesRegex(ops.OperationError, "port is occupied"):
                self.make_backup()
        self.assertFalse((self.private / "snapshot").exists())

    def test_backup_is_exact_three_databases_plus_manifest_without_config_values(self):
        source_hashes = {role: ops.digest(Path(self.config[key])) for role, key in ops.DATABASES.items()}
        target = self.make_backup()
        self.assertEqual({ops.MANIFEST, *ops.FILES.values()}, {path.name for path in target.iterdir()})
        manifest = ops.verify_backup(target)
        self.assertFalse(manifest["configuration_included"])
        self.assertEqual(3, len(manifest["databases"]))
        for role, key in ops.DATABASES.items():
            self.assertEqual(source_hashes[role], ops.digest(Path(self.config[key])))
            with ops.database_connection(target / ops.FILES[role]) as connection:
                self.assertEqual("Synthetic " + role, connection.execute("SELECT body FROM memory").fetchone()[0])
        all_bytes = b"".join(path.read_bytes() for path in target.iterdir())
        for key in ops.starter.SECRET_KEYS:
            self.assertNotIn(self.config[key].encode(), all_bytes)

    def test_wal_committed_rows_are_included_and_backup_is_standalone(self):
        with contextlib.closing(sqlite3.connect(self.config["STBRAIN_DB_PATH"])) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO memory VALUES (2, 'Synthetic WAL row', NULL)")
            writer.commit()
            self.assertTrue(Path(self.config["STBRAIN_DB_PATH"] + "-wal").exists())
            target = self.make_backup()
            with ops.database_connection(target / ops.FILES["main"]) as connection:
                self.assertEqual(2, connection.execute("SELECT count(*) FROM memory").fetchone()[0])
        self.assertEqual(4, len(list(target.iterdir())))
        ops.verify_backup(target)

    def test_held_locks_block_new_writes_to_each_source(self):
        with ops.quiescent_sources(self.config, True) as paths:
            for path in paths.values():
                with contextlib.closing(sqlite3.connect(path, timeout=0)) as writer:
                    with self.assertRaises(sqlite3.OperationalError):
                        writer.execute("INSERT INTO memory VALUES (2, 'blocked synthetic write', NULL)")

    def test_active_writer_causes_failure_without_partial_publish(self):
        with contextlib.closing(sqlite3.connect(self.config["STBRAIN_DB_PATH"])) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError):
                self.make_backup()
        self.assertFalse((self.private / "snapshot").exists())

    def test_missing_database_is_not_created(self):
        missing = self.private / "missing.db"
        self.config["STBRAIN_HALLUCINATION_VAULT_DB_PATH"] = str(missing)
        with self.assertRaisesRegex(ops.OperationError, "three existing"):
            self.make_backup()
        self.assertFalse(missing.exists())

    def test_backup_refuses_existing_destination_and_source_repository(self):
        destination = self.private / "existing"; destination.mkdir()
        sentinel = destination / "keep.txt"; sentinel.write_text("synthetic keep")
        with self.assertRaisesRegex(ops.OperationError, "already exists"):
            ops.backup(self.config, destination, confirm_stopped=True)
        self.assertEqual("synthetic keep", sentinel.read_text())
        with self.assertRaisesRegex(ops.OperationError, "outside the source"):
            ops.backup(self.config, ROOT / "synthetic-refused-backup", confirm_stopped=True)
        self.assertFalse((ROOT / "synthetic-refused-backup").exists())

    def test_backup_refuses_source_database_directory(self):
        target = self.source / "refused-snapshot"
        with self.assertRaisesRegex(ops.OperationError, "outside every source database"):
            ops.backup(self.config, target, confirm_stopped=True)
        self.assertFalse(target.exists())

    def test_restore_refuses_to_mutate_backup_directory(self):
        backup = self.make_backup()
        with self.assertRaisesRegex(ops.OperationError, "outside the backup"):
            ops.restore(backup, backup / "restored")
        self.assertEqual(4, len(list(backup.iterdir())))
        ops.verify_backup(backup)

    def test_cleanup_continues_after_one_child_cannot_be_confirmed(self):
        children = [FakeChild(), FakeChild()]
        with patch.object(children[1], "wait", side_effect=OSError("synthetic failure")):
            with self.assertRaisesRegex(ops.OperationError, "could not be confirmed stopped"):
                ops.stop_children(children)
        self.assertTrue(all(child.terminated for child in children))
        self.assertEqual(0, children[0].exit_code)

    def test_containment_assignment_failure_stops_suspended_child(self):
        child = FakeChild()
        class RefusingJob:
            creationflags = 4
            closed = False
            def assign_and_resume(self, _):
                raise OSError("synthetic assignment failure")
            def close(self):
                self.closed = True
        job = RefusingJob()
        with self.assertRaises(OSError):
            ops.start_stack(self.config, popen=lambda *args, **kwargs: child, job_factory=lambda: job)
        self.assertTrue(job.closed)
        self.assertTrue(child.terminated)

    def test_occupied_port_after_cleanup_is_reported_without_killing_anything(self):
        with patch.object(ops, "check_ports_stopped", side_effect=ops.OperationError("synthetic occupied")), \
                patch.object(ops.time, "monotonic", side_effect=[0, 6]):
            with self.assertRaisesRegex(ops.OperationError, "still occupied"):
                ops.await_ports_stopped(self.config)

    def test_symbolic_link_input_is_rejected_before_resolution(self):
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(ops.OperationError, "symbolic link"):
                ops.outside_source(self.private / "synthetic-link")

    def test_changed_database_fails_hash_validation(self):
        target = self.make_backup()
        with (target / ops.FILES["ideas"]).open("ab") as stream:
            stream.write(b"synthetic corruption")
        with self.assertRaisesRegex(ops.OperationError, "hash or size"):
            ops.verify_backup(target)

    def test_extra_files_and_traversal_manifest_are_rejected(self):
        target = self.make_backup()
        extra = target / "do-not-copy.env"; extra.write_text("synthetic")
        with self.assertRaisesRegex(ops.OperationError, "exactly"):
            ops.verify_backup(target)
        extra.unlink()
        manifest = json.loads((target / ops.MANIFEST).read_text())
        manifest["databases"]["main"]["file"] = "../outside.db"
        (target / ops.MANIFEST).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ops.OperationError, "entry is invalid"):
            ops.verify_backup(target)

    def test_restore_validates_before_publish_and_never_modifies_original(self):
        backup = self.make_backup()
        original = Path(self.config["STBRAIN_DB_PATH"]); original_hash = ops.digest(original)
        destination = self.private / "restored"
        result = ops.restore(backup, destination)
        self.assertFalse(result["existing_instance_modified"])
        self.assertEqual(original_hash, ops.digest(original))
        for name in (ops.MANIFEST, *ops.FILES.values()):
            self.assertEqual(ops.digest(backup / name), ops.digest(destination / name))
        with self.assertRaisesRegex(ops.OperationError, "already exists"):
            ops.restore(backup, destination)

    def test_corrupt_restore_does_not_publish_a_directory(self):
        backup = self.make_backup(); (backup / ops.FILES["vault"]).write_bytes(b"invalid synthetic sqlite")
        destination = self.private / "restored"
        with self.assertRaises(ops.OperationError):
            ops.restore(backup, destination)
        self.assertFalse(destination.exists())

    def test_export_requires_ack_and_exact_table_and_keeps_original_types(self):
        backup = self.make_backup(); output = self.private / "selected.jsonl"
        with self.assertRaisesRegex(ops.OperationError, "ack-private-data"):
            ops.export_table(backup, "main", "memory", output, ack_private_data=False)
        with self.assertRaisesRegex(ops.OperationError, "does not exist"):
            ops.export_table(backup, "main", "memory; DROP TABLE memory", output, ack_private_data=True)
        result = ops.export_table(backup, "main", "memory", output, ack_private_data=True)
        self.assertEqual(1, result["exported_rows"])
        self.assertFalse(result["import_supported"])
        lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["id", "body", "data"], lines[0]["columns"])
        self.assertEqual([1, "Synthetic main", {"$type": "bytes", "base64": "AAE="}], lines[1]["row"])
        with self.assertRaisesRegex(ops.OperationError, "new filename"):
            ops.export_table(backup, "main", "memory", output, ack_private_data=True)
        self.assertEqual(4, len(list(backup.iterdir())))

    def test_export_refuses_output_inside_backup_or_repository(self):
        backup = self.make_backup()
        for output in (backup / "selected.jsonl", ROOT / "synthetic-refused-export.jsonl"):
            with self.assertRaises(ops.OperationError):
                ops.export_table(backup, "main", "memory", output, ack_private_data=True)
            self.assertFalse(output.exists())

    def test_output_reservation_refuses_cooperating_concurrent_publish(self):
        destination = self.private / "reserved"
        with ops.new_output_directory(destination):
            with self.assertRaisesRegex(ops.OperationError, "Another operation"):
                with ops.new_output_directory(destination):
                    self.fail("must not enter")

    def test_sqlite_failure_is_sanitized_by_cli(self):
        backup = self.make_backup()
        with patch.object(ops, "verify_backup", side_effect=sqlite3.OperationalError("synthetic-private-secret")), contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(2, ops.main(["verify", "--backup", str(backup)]))
        self.assertNotIn("synthetic-private-secret", error.getvalue())


if __name__ == "__main__":
    unittest.main()
