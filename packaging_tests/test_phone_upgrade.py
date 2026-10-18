"""Offline upgrade fault tests, not a Termux or real-device acceptance claim."""
from contextlib import nullcontext, closing, contextmanager
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from packaging_tests import test_install_phone as bridge_tests
phone, ROOT = bridge_tests.phone, bridge_tests.ROOT


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    obj = importlib.util.module_from_spec(spec)
    sys.modules[name] = obj
    spec.loader.exec_module(obj)
    return obj


upgrade = load("phone_upgrade")
guest = load("phone_update_guest")


class PhoneUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = bridge_tests.PhoneBridgeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.old = f.owned(ready=True)
        self.source = f.home / "new-source"
        self.source.mkdir()
        self.ctx = {**f.ctx, "source": self.source}
        self.digests = patch.object(phone, "source_digest", side_effect=lambda ctx: "c" * 64 if ctx["source"] == self.source else f.digest)
        self.digests.start(); self.addCleanup(self.digests.stop)
        for name in ("verify_os", "verify_runtime", "verify_storage", "execute"):
            item = patch.object(phone, name); item.start(); self.addCleanup(item.stop)
        item = patch.object(phone, "guest_command", side_effect=lambda ctx, command: command)
        item.start(); self.addCleanup(item.stop)
    def run_upgrade(self):
        upgrade.transition(phone, self.ctx, "c" * 64, "upgrade", self.fixture.source)
    def test_success_switches_all_markers_with_same_owner_and_retains_previous_source(self):
        with patch.object(upgrade, "guest") as command:
            self.run_upgrade()
            self.assertEqual(["stage", "verify-unchanged"], [c.args[2] for c in command.call_args_list])
        record = phone.state_record(self.ctx, "c" * 64)
        self.assertEqual(self.old["owner"], record["owner"])
        phone.owned_container(self.ctx, record)
        self.assertEqual({"identity": record}, phone.read_json(self.ctx["state"] / "phone-prepared.json"))
        self.assertTrue(self.fixture.source.is_dir())
    def test_failed_candidate_keeps_pending_journal_and_can_explicitly_rollback(self):
        with patch.object(upgrade, "guest", side_effect=phone.PhoneError("failed test")):
            with self.assertRaises(phone.PhoneError): self.run_upgrade()
        journal = phone.read_json(self.ctx["state"] / "phone-upgrade.json")
        self.assertEqual("preparing", journal["phase"])
        self.assertEqual(self.old, phone.read_json(self.ctx["state"] / "phone-owner.json"))
        with patch.object(upgrade, "guest"):
            upgrade.transition(phone, self.ctx, "c" * 64, "rollback")
        self.assertEqual(self.old, phone.state_record(self.fixture.ctx, self.fixture.digest))
    def test_interruption_between_owner_markers_is_recoverable_without_guessing(self):
        real_atomic = upgrade.atomic
        def interrupted(p, path, value):
            if path.name == "phone-prepared.json": raise OSError("simulated power failure")
            return real_atomic(p, path, value)
        with patch.object(upgrade, "guest"), patch.object(upgrade, "atomic", side_effect=interrupted):
            with self.assertRaises(OSError): self.run_upgrade()
        with patch.object(upgrade, "guest"):
            upgrade.transition(phone, self.ctx, "c" * 64, "rollback")
        self.assertEqual(self.old, phone.state_record(self.fixture.ctx, self.fixture.digest))
        phone.owned_container(self.fixture.ctx, self.old)
    def test_tampered_marker_refused_and_in_place_source_refused(self):
        with self.assertRaises(phone.PhoneError):
            upgrade.transition(phone, self.ctx, "c" * 64, "upgrade", self.source)
        with patch.object(upgrade, "guest"): self.run_upgrade()
        path = self.ctx["state"] / "phone-owner.json"
        value = phone.read_json(path); value["owner"] = "d" * 64
        upgrade.atomic(phone, path, value)
        with patch.object(upgrade, "guest") as command:
            with self.assertRaises(phone.PhoneError): upgrade.transition(phone, self.ctx, "c" * 64, "rollback")
            command.assert_not_called()

    def test_normal_start_rechecks_pending_upgrade_after_acquiring_lock(self):
        @contextmanager
        def interrupted_update(ctx):
            upgrade.atomic(phone, ctx["state"] / "phone-upgrade.json", {"phase": "preparing"})
            yield
        with patch.object(phone, "host_layout", return_value=self.fixture.ctx), \
             patch.object(phone, "validate_proot"), patch.object(phone, "state_lock", side_effect=interrupted_update), \
             patch.object(phone, "operate") as operate:
            self.assertEqual(1, phone.main(["start", "--execute"]))
            operate.assert_not_called()


class GuestUpgradeTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve()
        self.private = self.state / "private"; self.private.mkdir()
        (self.private / "config.env").write_text("TEST_ONLY_KEY=unchanged\n")
        (self.private / "self-password.json").write_text('{"test":"hash"}')
        (self.private / "venv").mkdir(); (self.private / "venv" / "keep").write_text("old environment")
        self.dbs = {}
        for name in ("main", "ideas", "vault"):
            path = self.private / (name + ".db")
            with closing(sqlite3.connect(path)) as db:
                db.execute("create table notes(value text)"); db.execute("insert into notes values('before')"); db.commit()
            self.dbs[name] = path
        self.files = {k: v.name for k, v in self.dbs.items()}
        def check(path):
            with closing(sqlite3.connect(path)) as db: self.assertEqual([("ok",)], db.execute("pragma integrity_check").fetchall())
        def backup(config, output, **kwargs):
            output.mkdir()
            for role, path in self.dbs.items():
                with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(output / self.files[role])) as dest: source.backup(dest)
        self.ops = SimpleNamespace(check_ports_stopped=lambda _: None, backup=backup, source_databases=lambda _: self.dbs,
                                   FILES=self.files, check_database=check, quiescent_sources=lambda *_: nullcontext(), verify_backup=lambda _: None)
        modules = {"install_stiller": SimpleNamespace(read_installation=lambda _: {}), "stiller_ops": self.ops}
        p = patch.object(guest, "module", side_effect=modules.__getitem__); p.start(); self.addCleanup(p.stop)
        self.operation = "upgrade-" + "a" * 24
    def test_new_candidate_preserves_config_databases_and_original_runtime_and_rollback_keeps_later_data(self):
        guest.stage(self.state, self.operation)
        folder = self.state / "phone-upgrades" / self.operation
        self.assertEqual("old environment", (folder / "original/venv/keep").read_text())
        self.assertFalse((self.private / "venv").exists())
        guest.verify_unchanged(self.state, self.operation)
        guest.stage(self.state, self.operation)  # Resume is idempotent.
        with closing(sqlite3.connect(self.private / "main.db")) as db: db.execute("insert into notes values('after')"); db.commit()
        guest.rollback(self.state, self.operation)
        with closing(sqlite3.connect(self.private / "main.db")) as db: self.assertEqual([("before",)], db.execute("select value from notes").fetchall())
        with closing(sqlite3.connect(folder / "after-upgrade/main.db")) as db: self.assertEqual(2, db.execute("select count(*) from notes").fetchone()[0])
        guest.rollback(self.state, self.operation)
        self.assertEqual("old environment", (self.private / "venv/keep").read_text())
    def test_changed_config_is_not_silently_accepted(self):
        guest.stage(self.state, self.operation)
        (self.private / "config.env").write_text("TEST_ONLY_KEY=changed\n")
        with self.assertRaises(ValueError): guest.verify_unchanged(self.state, self.operation)
    def test_interrupted_private_swap_restores_original_without_deleting_candidate(self):
        original = guest.atomic
        def interrupt(path, value):
            if value.get("phase") == "original_saved": raise OSError("simulated interruption")
            return original(path, value)
        with patch.object(guest, "atomic", side_effect=interrupt):
            with self.assertRaises(OSError): guest.stage(self.state, self.operation)
        guest.rollback(self.state, self.operation)
        self.assertEqual("old environment", (self.private / "venv/keep").read_text())
        self.assertTrue((self.state / "phone-upgrades" / self.operation / "candidate").is_dir())
    def test_foreign_operation_and_link_paths_refused(self):
        for value in ("../private", "upgrade-no", ""):
            with self.assertRaises(ValueError): guest.paths(self.state, value)

    def test_directory_sync_failure_after_move_is_recoverable(self):
        original = guest.durable_rename
        def interrupted(source, target):
            original(source, target)
            if target.name == "original":
                raise OSError("simulated fsync failure after rename")
        with patch.object(guest, "durable_rename", side_effect=interrupted):
            with self.assertRaises(OSError): guest.stage(self.state, self.operation)
        guest.rollback(self.state, self.operation)
        self.assertEqual("old environment", (self.private / "venv/keep").read_text())
