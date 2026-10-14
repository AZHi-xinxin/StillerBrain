"""Synthetic-only storage contract tests; Windows must not fake POSIX support."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("phone_storage_tests", ROOT / "scripts/check_phone_storage.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class StorageContractTests(unittest.TestCase):
    def test_unsupported_platform_never_touches_parent(self):
        with patch.object(probe, "_posix", return_value=False), \
                patch.object(probe, "_parent", side_effect=AssertionError("must not inspect paths")):
            result = probe.run_probe("do not read me")
        self.assertEqual(["unsupported_platform"], result["errors"])
        self.assertFalse(result["ok"])
        self.assertFalse(result["supported"])
        self.assertFalse(any(result["capabilities"].values()))

    def test_relative_parent_rejected_without_creation(self):
        with patch.object(probe, "_posix", return_value=True), \
                patch.object(probe.tempfile, "mkdtemp", side_effect=AssertionError("no creation")):
            result = probe.run_probe("relative-parent")
        self.assertEqual(["parent_not_absolute"], result["errors"])

    def test_missing_parent_is_not_created(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(probe, "_posix", return_value=True):
            parent = Path(temporary) / "missing"
            result = probe.run_probe(parent)
            self.assertFalse(parent.exists())
        self.assertEqual(["parent_not_directory"], result["errors"])

    def test_parent_file_is_not_read_or_modified(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(probe, "_posix", return_value=True):
            parent = Path(temporary) / "user-config"
            parent.write_bytes(b"untouched synthetic user content")
            result = probe.run_probe(parent)
            self.assertEqual(b"untouched synthetic user content", parent.read_bytes())
        self.assertEqual(["parent_not_directory"], result["errors"])

    def test_failed_probe_retained_and_existing_sibling_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            existing = parent / "existing-config.env"
            existing.write_bytes(b"synthetic private value")
            with patch.object(probe, "_posix", return_value=True), \
                    patch.object(probe, "_permissions", side_effect=OSError("private value must not escape")), \
                    patch.object(probe, "_cleanup", side_effect=AssertionError("keep failed probe")):
                result = probe.run_probe(parent)
            self.assertEqual(b"synthetic private value", existing.read_bytes())
            self.assertEqual(1, len(list(parent.glob(probe.PREFIX + "*"))))
        self.assertEqual(["private_permissions_failed"], result["errors"])
        self.assertNotIn("private value", json.dumps(result))

    def test_success_orchestration_requires_all_four_capabilities_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(probe, "_posix", return_value=True))
            operations = [stack.enter_context(patch.object(probe, name))
                          for name in ("_permissions", "_atomic", "_sqlite", "_process_lock", "_cleanup")]
            result = probe.run_probe(Path(temporary))
            self.assertTrue(all(result["capabilities"].values()))
            self.assertTrue(result["ok"])
            self.assertEqual([], result["errors"])
            self.assertTrue(all(operation.call_count == 1 for operation in operations))

    def test_fsync_failure_is_not_downgraded(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(probe, "_posix", return_value=True))
            stack.enter_context(patch.object(probe, "_permissions"))
            stack.enter_context(patch.object(probe, "_atomic", side_effect=OSError("fsync unsupported")))
            later = stack.enter_context(patch.object(probe, "_sqlite"))
            result = probe.run_probe(Path(temporary))
            later.assert_not_called()
            self.assertTrue(result["capabilities"]["private_permissions"])
            self.assertFalse(result["capabilities"]["atomic_replace_fsync"])
        self.assertEqual(["atomic_replace_fsync_failed"], result["errors"])

    def test_lock_child_timeout_is_sanitized_and_fails(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(probe, "_posix", return_value=True))
            for name in ("_permissions", "_atomic", "_sqlite"):
                stack.enter_context(patch.object(probe, name))
            stack.enter_context(patch.object(probe, "_process_lock", side_effect=subprocess.TimeoutExpired("sensitive path", 10)))
            result = probe.run_probe(Path(temporary))
        self.assertFalse(result["ok"])
        self.assertEqual(["process_lock_child_timeout"], result["errors"])
        self.assertNotIn("sensitive", json.dumps(result))

    def test_cleanup_failure_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(probe, "_posix", return_value=True))
            for name in ("_permissions", "_atomic", "_sqlite", "_process_lock"):
                stack.enter_context(patch.object(probe, name))
            stack.enter_context(patch.object(probe, "_cleanup", side_effect=probe.ProbeError("cleanup_unexpected_entry")))
            result = probe.run_probe(Path(temporary))
        self.assertTrue(all(result["capabilities"].values()))
        self.assertFalse(result["ok"])
        self.assertEqual(["cleanup_unexpected_entry"], result["errors"])

    def test_child_command_uses_current_isolated_python_and_no_shell(self):
        with patch.object(probe.subprocess, "run", return_value=subprocess.CompletedProcess([], probe.LOCK_BUSY)) as run:
            self.assertEqual(probe.LOCK_BUSY, probe._child_result(Path("synthetic.lock"), (4, 8)))
        command = run.call_args.args[0]
        self.assertEqual([sys.executable, "-I", "-B"], command[:3])
        self.assertEqual(["--_flock-check", "synthetic.lock", "4", "8"], command[-4:])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertTrue(run.call_args.kwargs["close_fds"])
        self.assertEqual(10, run.call_args.kwargs["timeout"])

    def test_cli_help_requires_only_standard_library_and_does_not_probe(self):
        with patch.object(probe, "run_probe", side_effect=AssertionError("no probe")), \
                contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as exited:
            probe.main(["--help"])
        self.assertEqual(0, exited.exception.code)
        self.assertIn("--parent", output.getvalue())

    def test_cli_failure_is_nonzero_and_json_only(self):
        with patch.object(probe, "_posix", return_value=False), contextlib.redirect_stdout(io.StringIO()) as output:
            code = probe.main(["--parent", "unread"])
        self.assertEqual(1, code)
        result = json.loads(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual(["unsupported_platform"], result["errors"])

    def test_child_protocol_rejects_unsupported_os_without_opening_file(self):
        with patch.object(probe, "_posix", return_value=False), \
                patch.object(probe.os, "open", side_effect=AssertionError("no file access")):
            self.assertEqual(24, probe._flock_child("unread", 1, 2))

    def test_missing_platform_feature_is_a_fixed_error_category(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(probe, "_posix", return_value=True))
            for name in ("_permissions", "_atomic", "_sqlite"):
                stack.enter_context(patch.object(probe, name))
            stack.enter_context(patch.object(probe, "_process_lock", side_effect=ImportError("private module path")))
            result = probe.run_probe(Path(temporary))
        self.assertEqual(["process_lock_failed"], result["errors"])
        self.assertFalse(result["ok"])


@unittest.skipUnless(os.name == "posix", "Real POSIX permissions, flock and WAL need a POSIX runtime; Windows is unsupported")
class RealPosixStorageTests(unittest.TestCase):
    def test_complete_probe_and_exact_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            sibling = parent / "existing.sqlite3"
            sibling.write_bytes(b"not read by probe")
            result = probe.run_probe(parent)
            self.assertEqual([], result["errors"])
            self.assertTrue(result["ok"])
            self.assertTrue(all(result["capabilities"].values()))
            self.assertEqual([sibling], list(parent.iterdir()))
            self.assertEqual(b"not read by probe", sibling.read_bytes())

    def test_symlinked_parent_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            target = base / "target"
            target.mkdir()
            linked = base / "linked"
            linked.symlink_to(target, target_is_directory=True)
            result = probe.run_probe(linked)
            self.assertEqual(["parent_linked"], result["errors"])
            self.assertEqual([], list(target.iterdir()))

    def test_real_fsync_failure_keeps_failed_probe(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(probe.os, "fsync", side_effect=OSError("synthetic failure")):
            parent = Path(temporary).resolve()
            result = probe.run_probe(parent)
            self.assertFalse(result["ok"])
            self.assertEqual(["private_permissions_failed"], result["errors"])
            self.assertEqual(1, len(list(parent.glob(probe.PREFIX + "*"))))

    def test_real_cross_process_lock_is_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(probe, "_child_result", return_value=0):
                result = probe.run_probe(Path(temporary).resolve())
            self.assertEqual(["process_lock_not_exclusive"], result["errors"])
            self.assertFalse(result["ok"])

    def test_real_lock_must_release_for_another_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(probe, "_child_result", side_effect=[probe.LOCK_BUSY, probe.LOCK_BUSY]):
                result = probe.run_probe(Path(temporary).resolve())
            self.assertEqual(["process_lock_not_released"], result["errors"])
            self.assertFalse(result["ok"])

    def test_cleanup_identity_mismatch_never_deletes_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            directory = Path(tempfile.mkdtemp(prefix=probe.PREFIX, dir=parent))
            expected = directory / "permissions.txt"
            expected.write_bytes(b"synthetic known")
            with self.assertRaisesRegex(probe.ProbeError, "cleanup_scope_changed"):
                probe._cleanup(directory, parent, (-1, -1))
            self.assertTrue(expected.exists())

    def test_cleanup_refuses_unknown_entry_before_deleting_any_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            directory = Path(tempfile.mkdtemp(prefix=probe.PREFIX, dir=parent))
            expected = directory / "permissions.txt"
            expected.write_bytes(b"synthetic known")
            unknown = directory / "do-not-delete.txt"
            unknown.write_bytes(b"synthetic unknown")
            with self.assertRaisesRegex(probe.ProbeError, "cleanup_unexpected_entry"):
                probe._cleanup(directory, parent, probe._identity(directory))
            self.assertTrue(expected.exists())
            self.assertTrue(unknown.exists())

    def test_cleanup_refuses_symlink_and_never_touches_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            target = parent / "outside.txt"
            target.write_bytes(b"untouched")
            directory = Path(tempfile.mkdtemp(prefix=probe.PREFIX, dir=parent))
            (directory / "permissions.txt").symlink_to(target)
            with self.assertRaisesRegex(probe.ProbeError, "cleanup_unexpected_entry"):
                probe._cleanup(directory, parent, probe._identity(directory))
            self.assertEqual(b"untouched", target.read_bytes())


if __name__ == "__main__":
    unittest.main()
