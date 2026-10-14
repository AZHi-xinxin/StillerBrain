"""Offline phone-bootstrap tests. No phone, download, shell or model requests."""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("test_phone_bridge", ROOT / "scripts/install_phone.py")
phone = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = phone
SPEC.loader.exec_module(phone)

RUNTIME = {"machine": "aarch64", "python": [3, 12], "implementation": "CPython",
           "libc": ["glibc", "2.39"], "bits": 64, "free_threaded": False}


@contextmanager
def no_lock(_):
    yield


class PhoneBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.prefix = self.base / "usr"
        self.prefix.mkdir()
        self.source = self.home / "source"
        self.source.mkdir()
        self.state = self.home / "phone"
        self.ctx = phone.layout(self.source, self.state, self.home, self.prefix, phone.DEFAULT_NAME)
        self.digest = "a" * 64

    def owned(self, ready=False):
        record = phone.state_record(self.ctx, self.digest, create=True)
        container = self.ctx["container"]
        (container / "rootfs").mkdir(parents=True)
        manifest = {"image_ref": phone.IMAGE, "arch": "aarch64",
                    "image_config": {"architecture": "arm64", "os": "linux"},
                    "manifest": {"config": {"digest": "sha256:" + "b" * 64}}}
        (container / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        phone.exclusive_json(container / ".stiller-phone-owner.json",
                             {"identity": record, "manifest_sha256": phone.manifest_digest(self.ctx)})
        if ready:
            phone.exclusive_json(self.state / "phone-prepared.json", {"identity": record})
        return record

    def run_main(self, argv):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return phone.main(argv)

    def test_default_and_actions_are_zero_side_effect_preview(self):
        with patch.object(phone, "host_layout") as host, patch.object(phone, "execute") as run:
            for argv in ([], ["prepare"], ["install"], ["start"], ["check"], ["--execute"]):
                self.assertEqual(0, self.run_main(argv))
            host.assert_not_called()
            run.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_layout_rejects_source_or_state_outside_private_home(self):
        for source, state in ((self.source, self.base / "outside"), (self.base, self.state)):
            with self.assertRaises(phone.PhoneError):
                phone.layout(source, state, self.home, self.prefix, phone.DEFAULT_NAME)

    def test_layout_rejects_overlap(self):
        for state in (self.source, self.source / "child", self.home):
            with self.assertRaises(phone.PhoneError):
                phone.layout(self.source, state, self.home, self.prefix, phone.DEFAULT_NAME)

    def test_names_cannot_target_arbitrary_or_legacy_distribution(self):
        for name in ("ubuntu", "../ubuntu", "stiller-../x", "stiller-x:root", "stiller-x\n", "stiller-X", "stiller-"):
            with self.assertRaises(phone.PhoneError, msg=name):
                phone.layout(self.source, self.state, self.home, self.prefix, name)

    def test_linked_path_not_resolved_away(self):
        link = self.home / "linked"
        try:
            link.symlink_to(self.base, target_is_directory=True)
        except OSError:
            self.skipTest("Host does not allow synthetic symlinks")
        with self.assertRaises(phone.PhoneError):
            phone.layout(self.source, link / "phone", self.home, self.prefix, phone.DEFAULT_NAME)

    def test_no_root_or_desktop_execution(self):
        args = SimpleNamespace(directory=None, name=phone.DEFAULT_NAME)
        with patch.object(phone.platform, "system", return_value="Windows"):
            with self.assertRaises(phone.PhoneError):
                phone.host_layout(args)
        with patch.object(phone.platform, "system", return_value="Linux"), \
             patch.object(phone.platform, "machine", return_value="aarch64"), \
             patch.object(phone.os, "getuid", return_value=0, create=True):
            with self.assertRaises(phone.PhoneError):
                phone.host_layout(args)

    def test_android_and_linux_termux_hosts_reach_checked_layout(self):
        args = SimpleNamespace(directory=None, name=phone.DEFAULT_NAME)
        # Exercise host_layout itself, including private prefix, Android build
        # marker and non-root gates. No real Android paths are created.
        for system in ("Android", "Linux"):
            for machine in ("aarch64", "arm64"):
                prefix = MagicMock()
                prefix.__str__.return_value = "/data/data/com.termux/files/usr"
                prefix.is_dir.return_value = True
                home = prefix.parent.__truediv__.return_value
                with patch.object(phone.platform, "system", return_value=system), \
                     patch.object(phone.platform, "machine", return_value=machine), \
                     patch.object(phone.os, "getuid", return_value=10234, create=True), \
                     patch.dict(os.environ, {"PREFIX": "/data/data/com.termux/files/usr"}, clear=True), \
                     patch.object(phone, "plain_path", return_value=prefix) as paths, \
                     patch.object(phone.Path, "exists", return_value=True), \
                     patch.object(phone, "layout", return_value=self.ctx) as checked:
                    self.assertIs(self.ctx, phone.host_layout(args))
                    paths.assert_called_once_with("/data/data/com.termux/files/usr")
                    checked.assert_called_once_with(phone.ROOT, home / "StillerBrain-phone", home,
                                                    prefix, phone.DEFAULT_NAME)

    def test_android_host_still_rejects_root_nested_wrong_prefix_and_no_os_marker(self):
        args = SimpleNamespace(directory=None, name=phone.DEFAULT_NAME)
        prefix = MagicMock()
        prefix.__str__.return_value = "/data/data/com.termux/files/usr"
        prefix.is_dir.return_value = True
        with patch.object(phone.platform, "system", return_value="Android"), \
             patch.object(phone.platform, "machine", return_value="aarch64"), \
             patch.object(phone, "plain_path", return_value=prefix), \
             patch.object(phone, "layout") as checked:
            with patch.object(phone.os, "getuid", return_value=0, create=True), patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(phone.PhoneError):
                    phone.host_layout(args)
            with patch.object(phone.os, "getuid", return_value=10234, create=True):
                with patch.dict(os.environ, {"container": "proot-distro"}, clear=True):
                    with self.assertRaises(phone.PhoneError):
                        phone.host_layout(args)
                with patch.dict(os.environ, {"PREFIX": "/data/data/com.termux/files/usr"}, clear=True):
                    prefix.__str__.return_value = "/sdcard/usr"
                    with self.assertRaises(phone.PhoneError):
                        phone.host_layout(args)
                    prefix.__str__.return_value = "/data/data/com.termux/files/usr"
                    with patch.object(phone.Path, "exists", return_value=False):
                        with self.assertRaises(phone.PhoneError):
                            phone.host_layout(args)
            checked.assert_not_called()

    def test_existing_state_without_provenance_is_not_adopted(self):
        self.state.mkdir()
        (self.state / "keep.txt").write_text("unchanged")
        with self.assertRaises(OSError):
            phone.state_record(self.ctx, self.digest, create=True)
        self.assertEqual("unchanged", (self.state / "keep.txt").read_text())
        self.assertFalse((self.state / "phone-owner.json").exists())

    def test_existing_container_or_legacy_never_claimed(self):
        for key in ("container", "legacy"):
            with self.subTest(key=key):
                target = self.ctx[key]
                target.mkdir(parents=True)
                with self.assertRaises(phone.PhoneError):
                    phone.state_record(self.ctx, self.digest, create=True)
                self.assertFalse(self.state.exists())
                target.rmdir()

    def test_state_resume_requires_identical_source_and_alias(self):
        original = phone.state_record(self.ctx, self.digest, create=True)
        self.assertEqual(original, phone.state_record(self.ctx, self.digest))
        with self.assertRaises(phone.PhoneError):
            phone.state_record(self.ctx, "f" * 64)
        other = {**self.ctx, "name": "stiller-other"}
        with self.assertRaises(phone.PhoneError):
            phone.state_record(other, self.digest)

    def test_state_owner_nonce_is_required(self):
        phone.state_record(self.ctx, self.digest, create=True)
        file = self.state / "phone-owner.json"
        data = json.loads(file.read_text())
        data["owner"] = ""
        file.write_text(json.dumps(data))
        with self.assertRaises(phone.PhoneError):
            phone.state_record(self.ctx, self.digest)

    def test_exclusive_marker_does_not_overwrite(self):
        target = self.home / "marker"
        phone.exclusive_json(target, {"keep": 1})
        with self.assertRaises(FileExistsError):
            phone.exclusive_json(target, {"keep": 2})
        self.assertEqual({"keep": 1}, phone.read_json(target))

    def test_json_size_type_and_symlink_are_checked(self):
        target = self.home / "marker"
        target.write_text("[]")
        with self.assertRaises(phone.PhoneError):
            phone.read_json(target)
        target.write_text("{" + " " * 1000 + "}")
        with self.assertRaises(phone.PhoneError):
            phone.read_json(target, 100)

    def test_owned_container_verifies_manifest_and_both_records(self):
        record = self.owned()
        self.assertEqual(record, phone.owned_container(self.ctx, record)["identity"])
        owner_file = self.ctx["container"] / ".stiller-phone-owner.json"
        owner = json.loads(owner_file.read_text())
        owner["identity"]["owner"] = "c" * 64
        owner_file.write_text(json.dumps(owner))
        with self.assertRaises(phone.PhoneError):
            phone.owned_container(self.ctx, record)

    def test_manifest_mutation_even_valid_oci_refuses_resume(self):
        record = self.owned()
        path = self.ctx["container"] / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["manifest"]["config"]["digest"] = "sha256:" + "d" * 64
        path.write_text(json.dumps(manifest))
        with self.assertRaises(phone.PhoneError):
            phone.owned_container(self.ctx, record)

    def test_wrong_image_architecture_and_missing_digest_refused(self):
        self.owned()
        path = self.ctx["container"] / "manifest.json"
        original = json.loads(path.read_text())
        bad = [dict(original, image_ref="ubuntu:latest"), dict(original, arch="x86_64"),
               dict(original, image_config={"architecture": "amd64", "os": "linux"}),
               dict(original, manifest={}), dict(original, image_config=[]),
               dict(original, manifest=[]), dict(original, manifest={"config": []})]
        for data in bad:
            path.write_text(json.dumps(data))
            with self.assertRaises(phone.PhoneError):
                phone.manifest_digest(self.ctx)

    def test_failed_install_never_writes_container_ownership(self):
        record = phone.state_record(self.ctx, self.digest, create=True)
        with patch.object(phone, "proot_binary", return_value="/trusted/proot-distro"), \
             patch.object(phone, "execute", side_effect=phone.PhoneError("failed")):
            with self.assertRaises(phone.PhoneError):
                phone.prepare(self.ctx, record)
        self.assertFalse((self.ctx["container"] / ".stiller-phone-owner.json").exists())
        self.assertTrue((self.state / "phone-owner.json").exists())

    def test_unmarked_container_after_interruption_never_adopted_on_retry(self):
        record = phone.state_record(self.ctx, self.digest, create=True)
        (self.ctx["container"] / "rootfs").mkdir(parents=True)
        with patch.object(phone, "execute") as run:
            with self.assertRaises(OSError):
                phone.prepare(self.ctx, record)
            run.assert_not_called()
        self.assertTrue((self.ctx["container"] / "rootfs").exists())

    def test_prepare_ready_does_not_reinstall_or_run_apt(self):
        record = self.owned(ready=True)
        with patch.object(phone, "verify_os") as os_check, patch.object(phone, "verify_runtime") as runtime, \
             patch.object(phone, "verify_storage") as storage, patch.object(phone, "execute") as run, redirect_stdout(io.StringIO()):
            phone.prepare(self.ctx, record)
            os_check.assert_called_once()
            runtime.assert_called_once()
            storage.assert_called_once()
            run.assert_not_called()

    def test_prepare_never_runs_apt_for_wrong_os(self):
        record = self.owned()
        with patch.object(phone, "verify_os", side_effect=phone.PhoneError("wrong os")), \
             patch.object(phone, "execute") as run:
            with self.assertRaises(phone.PhoneError):
                phone.prepare(self.ctx, record)
            run.assert_not_called()
        self.assertFalse((self.state / "phone-prepared.json").exists())

    def test_prepare_invokes_argv_no_reset_or_removal(self):
        record = self.owned()
        with patch.object(phone, "proot_binary", return_value="/trusted/proot-distro"), \
             patch.object(phone, "verify_os"), patch.object(phone, "verify_runtime"), \
             patch.object(phone, "verify_storage"), patch.object(phone, "execute") as run, redirect_stdout(io.StringIO()):
            phone.prepare(self.ctx, record)
        commands = [call.args[1] for call in run.call_args_list]
        self.assertEqual(2, len(commands))
        self.assertTrue(all("/usr/bin/apt-get" in cmd for cmd in commands))
        self.assertIn("python3.12-venv", commands[-1])
        for cmd in commands:
            self.assertFalse(any(word in cmd for word in ("reset", "remove", "sh", "bash", "--allow-unauthenticated")))
        self.assertTrue((self.state / "phone-prepared.json").is_file())

    def test_guest_is_isolated_and_only_expected_private_binds(self):
        with patch.object(phone, "proot_binary", return_value="/trusted/proot-distro"):
            command = phone.guest_command(self.ctx, ["/usr/bin/python3.12", "test.py"])
        self.assertEqual(2, command.count("--bind"))
        self.assertIn("--isolated", command)
        self.assertIn(str(self.source) + ":/opt/stiller-source", command)
        self.assertIn(str(self.state) + ":/opt/stiller-state", command)
        self.assertNotIn("--shared-home", command)
        self.assertNotIn("--no-kill-on-exit", command)
        self.assertNotIn("--detach", command)
        self.assertEqual(["/usr/bin/python3.12", "test.py"], command[-2:])

    def test_environment_drops_secrets_and_override_hooks(self):
        with patch.dict(os.environ, {"STBRAIN_UPSTREAM_API_KEY": "synthetic-secret", "PD_DOCKER_AUTH": "synthetic",
                                     "HTTP_PROXY": "synthetic", "PYTHONPATH": "synthetic", "PROOT_TMP_DIR": "synthetic"}):
            env = phone.clean_environment(self.ctx)
        self.assertNotIn("synthetic", json.dumps(env))
        self.assertEqual(str(self.prefix), env["TERMUX__PREFIX"])

    def test_subprocess_has_no_shell_and_no_implicit_check_or_credentials(self):
        result = SimpleNamespace(returncode=0, stdout="ok")
        with patch.object(phone.subprocess, "run", return_value=result) as run:
            self.assertEqual("ok", phone.execute(self.ctx, ["x", "a;echo no"], capture=True, timeout=10))
        self.assertEqual(["x", "a;echo no"], run.call_args.args[0])
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(10, run.call_args.kwargs["timeout"])

    def test_nonzero_command_raises_fixed_error_without_echoing_stdout(self):
        result = SimpleNamespace(returncode=1, stdout="synthetic-secret", stderr="synthetic-secret")
        with patch.object(phone.subprocess, "run", return_value=result):
            with self.assertRaises(phone.PhoneError) as failure:
                phone.execute(self.ctx, ["x"], capture=True)
        self.assertNotIn("synthetic-secret", str(failure.exception))

    def test_proot_help_stderr_is_separate_from_guest_json_stdout(self):
        result = SimpleNamespace(returncode=0, stdout="data", stderr="help")
        with patch.object(phone.subprocess, "run", return_value=result):
            self.assertEqual("data", phone.execute(self.ctx, ["x"], capture=True))
            self.assertEqual("datahelp", phone.execute(self.ctx, ["x"], capture=True, include_stderr=True))

    def test_legacy_version_is_refused_before_mutation(self):
        for version in ("4.32.0", "5.0.2", "rolling", "6.0.0"):
            with patch.object(phone, "proot_binary", return_value="/trusted/proot-distro"), \
                 patch.object(phone, "execute", return_value="PRoot-Distro version '" + version + "' by Termux") as run:
                with self.assertRaises(phone.PhoneError):
                    phone.validate_proot(self.ctx)
                self.assertEqual(1, run.call_count)

    def test_accepted_version_still_checks_required_capabilities(self):
        with patch.object(phone, "proot_binary", return_value="/trusted/proot-distro"), \
             patch.object(phone, "execute", side_effect=["PRoot-Distro version '5.10.0' by Termux", "--name --architecture", "--isolated --bind --work-dir"]) as run:
            phone.validate_proot(self.ctx)
            self.assertEqual(["/trusted/proot-distro", "--help"], run.call_args_list[0].args[1])
            self.assertTrue(run.call_args_list[0].kwargs["include_stderr"])
        with patch.object(phone, "proot_binary", return_value="/trusted/proot-distro"), \
             patch.object(phone, "execute", side_effect=["PRoot-Distro version '5.10.0' by Termux", "legacy help"]):
            with self.assertRaises(phone.PhoneError):
                phone.validate_proot(self.ctx)

    def test_runtime_accepts_only_glibc_standard_arm64_py312(self):
        with patch.object(phone, "proot_binary", return_value="/trusted/proot-distro"), \
             patch.object(phone, "execute", return_value=json.dumps(RUNTIME)):
            phone.verify_runtime(self.ctx)
        for change in ({"python": [3, 14]}, {"libc": ["bionic", ""]}, {"libc": ["glibc", "2.33"]},
                       {"machine": "x86_64"}, {"bits": 32}, {"free_threaded": True}, {"implementation": "PyPy"}):
            with patch.object(phone, "proot_binary", return_value="/trusted/proot-distro"), \
                 patch.object(phone, "execute", return_value=json.dumps({**RUNTIME, **change})):
                with self.assertRaises(phone.PhoneError):
                    phone.verify_runtime(self.ctx)

    def test_os_release_is_parsed_as_data_not_executed(self):
        with patch.object(phone, "proot_binary", return_value="/trusted/proot-distro"), \
             patch.object(phone, "execute", return_value='ID=ubuntu\nVERSION_ID="24.04"\nOTHER=$(anything)'):
            phone.verify_os(self.ctx)
        with patch.object(phone, "proot_binary", return_value="/trusted/proot-distro"), \
             patch.object(phone, "execute", return_value='ID=ubuntu\nVERSION_ID="26.04"'):
            with self.assertRaises(phone.PhoneError):
                phone.verify_os(self.ctx)

    def test_all_operations_reuse_existing_installer_and_preserve_fixed_paths(self):
        record = self.owned(ready=True)
        for action, option in (("install", "--directory"), ("start", "--start"), ("check", "--verify")):
            with patch.object(phone, "verify_os"), patch.object(phone, "verify_runtime"), \
                 patch.object(phone, "verify_storage"), \
                 patch.object(phone, "proot_binary", return_value="/trusted/proot-distro"), \
                 patch.object(phone, "execute") as run, \
                 patch.object(phone, "execute_foreground") as foreground, redirect_stdout(io.StringIO()):
                phone.operate(self.ctx, record, action)
                command = (foreground if action == "start" else run).call_args.args[1]
            self.assertEqual(["-B", "/opt/stiller-source/scripts/install_stiller.py", option, "/opt/stiller-state/private"], command[-4:])
            if action == "install":
                self.assertEqual("/usr/bin/python3.12", command[-5])
            else:
                self.assertEqual("/opt/stiller-state/private/venv/bin/python", command[-5])

    def test_storage_probe_must_report_every_capability(self):
        good = {"ok": True, "supported": True, "errors": [], "capabilities": {
            "private_permissions": True, "atomic_replace_fsync": True, "sqlite_wal": True, "process_lock": True}}
        with patch.object(phone, "proot_binary", return_value="/trusted/proot-distro"), \
             patch.object(phone, "execute", return_value=json.dumps(good)) as run:
            phone.verify_storage(self.ctx)
            self.assertEqual(["--parent", "/opt/stiller-state"], run.call_args.args[1][-2:])
        for key in good["capabilities"]:
            bad = {**good, "capabilities": {**good["capabilities"], key: False}}
            with patch.object(phone, "proot_binary", return_value="/trusted/proot-distro"), \
                 patch.object(phone, "execute", return_value=json.dumps(bad)):
                with self.assertRaises(phone.PhoneError):
                    phone.verify_storage(self.ctx)

    def test_probe_malformed_json_shapes_fail_closed_without_tracebacks(self):
        for value in ([], None, 42, {"libc": None}, {"machine": 42}, {"ok": True, "capabilities": []}):
            for check in (phone.verify_runtime, phone.verify_storage):
                with patch.object(phone, "proot_binary", return_value="/trusted/proot-distro"), \
                     patch.object(phone, "execute", return_value=json.dumps(value)):
                    with self.assertRaises(phone.PhoneError):
                        check(self.ctx)

    def test_storage_failure_stops_before_credentials_or_start(self):
        record = self.owned(ready=True)
        for action in ("install", "start", "check"):
            with patch.object(phone, "verify_os"), patch.object(phone, "verify_runtime"), \
                 patch.object(phone, "verify_storage", side_effect=phone.PhoneError("storage failed")), \
                 patch.object(phone, "execute") as run:
                with self.assertRaises(phone.PhoneError):
                    phone.operate(self.ctx, record, action)
                run.assert_not_called()

    def test_source_failure_precedes_proot_state_or_download(self):
        with patch.object(phone, "host_layout", return_value=self.ctx), \
             patch.object(phone, "source_digest", side_effect=phone.PhoneError("bad source")), \
             patch.object(phone, "validate_proot") as validate, patch.object(phone, "state_record") as state:
            self.assertEqual(1, self.run_main(["prepare", "--execute"]))
            validate.assert_not_called()
            state.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_no_space_does_not_create_state(self):
        with patch.object(phone, "host_layout", return_value=self.ctx), \
             patch.object(phone, "source_digest", return_value=self.digest), patch.object(phone, "validate_proot"), \
             patch("shutil.disk_usage", return_value=SimpleNamespace(free=phone.MIN_FREE - 1)):
            self.assertEqual(1, self.run_main(["prepare", "--execute"]))
        self.assertFalse(self.state.exists())

    def test_main_success_passes_ownership_record_into_prepare(self):
        with patch.object(phone, "host_layout", return_value=self.ctx), \
             patch.object(phone, "source_digest", return_value=self.digest), patch.object(phone, "validate_proot"), \
             patch("shutil.disk_usage", return_value=SimpleNamespace(free=phone.MIN_FREE)), \
             patch.object(phone, "state_lock", side_effect=no_lock), patch.object(phone, "prepare") as prepare:
            self.assertEqual(0, self.run_main(["prepare", "--execute"]))
        self.assertEqual(phone.FORMAT, prepare.call_args.args[1]["format"])

    def test_start_ctrl_c_waits_for_guest_without_early_kill(self):
        child = MagicMock()
        child.wait.side_effect = [KeyboardInterrupt(), 0]
        with patch.object(phone.subprocess, "Popen", return_value=child), \
             patch.object(phone.signal, "signal") as set_signal, redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                phone.execute_foreground(self.ctx, ["trusted-proot"])
        self.assertEqual([{}, {"timeout": 15}], [call.kwargs for call in child.wait.call_args_list])
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
        self.assertIn(((signal.SIGINT, signal.SIG_IGN), {}), set_signal.call_args_list)

    def test_start_stuck_guest_only_escalates_own_direct_child_after_waits(self):
        child = MagicMock()
        child.wait.side_effect = [KeyboardInterrupt(), subprocess.TimeoutExpired("synthetic", 15),
                                  subprocess.TimeoutExpired("synthetic", 5), 0]
        with patch.object(phone.subprocess, "Popen", return_value=child), \
             patch.object(phone.signal, "signal"), redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                phone.execute_foreground(self.ctx, ["trusted-proot"])
        self.assertEqual(["wait", "wait", "terminate", "wait", "kill", "wait"],
                         [call[0] for call in child.mock_calls])

    def test_start_sigterm_forwarding_and_handler_restoration(self):
        child = MagicMock()
        child.poll.return_value = None
        handlers = {}
        def install_handler(number, handler):
            handlers[number] = handler
        def wait_side_effect(*args, **kwargs):
            if not kwargs:
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            return 0
        child.wait.side_effect = wait_side_effect
        with patch.object(phone.subprocess, "Popen", return_value=child), \
             patch.object(phone.signal, "signal", side_effect=install_handler), \
             patch.object(phone.signal, "getsignal", return_value=signal.SIG_DFL), redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                phone.execute_foreground(self.ctx, ["trusted-proot"])
        child.send_signal.assert_called_once_with(signal.SIGTERM)
        child.kill.assert_not_called()
        self.assertEqual(signal.SIG_DFL, handlers[signal.SIGTERM])
        self.assertEqual(signal.SIG_DFL, handlers[signal.SIGINT])

    def test_start_normal_exit_and_nonzero_do_not_auto_restart(self):
        for code in (0, 1):
            child = MagicMock()
            child.wait.return_value = code
            with patch.object(phone.subprocess, "Popen", return_value=child) as popen, \
                 patch.object(phone.signal, "signal"):
                if code:
                    with self.assertRaises(phone.PhoneError):
                        phone.execute_foreground(self.ctx, ["trusted-proot"])
                else:
                    phone.execute_foreground(self.ctx, ["trusted-proot"])
            self.assertEqual(1, popen.call_count)
            child.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
