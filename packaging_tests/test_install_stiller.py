"""Offline installer contract tests: synthetic directories, no service/dependency calls."""
from __future__ import annotations

import contextlib
import getpass
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings

from packaging_tests.test_starter import starter


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("stiller_installer_contract_tests", ROOT / "scripts/install_stiller.py")
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)

WINDOWS_LOCK = "requirements-windows-py314.lock"
LINUX_LOCK = "requirements-linux-py312.lock"
SYNTHETIC_KEY = "synthetic-upstream-only-do-not-use-0001"
SYNTHETIC_MODEL = "synthetic-provider/model-v2"
PORTS = (18794, 18795, 18796)


class InstallerPlatformTests(unittest.TestCase):
    def windows(self, **changes):
        values = dict(system="Windows", machine="AMD64", version=(3, 14),
                      implementation="CPython", libc=("", ""), free_threaded=False, bits=64)
        values.update(changes)
        return installer.select_lock(**values)

    def linux(self, **changes):
        values = dict(system="Linux", machine="x86_64", version=(3, 12),
                      implementation="CPython", libc=("glibc", "2.34"), free_threaded=False, bits=64)
        values.update(changes)
        return installer.select_lock(**values)

    def test_windows_supported_architecture_aliases(self):
        for machine in ("AMD64", "x86_64"):
            with self.subTest(machine=machine):
                self.assertEqual(WINDOWS_LOCK, self.windows(machine=machine))

    def test_linux_supported_architecture_aliases(self):
        for machine in ("AMD64", "x86_64"):
            with self.subTest(machine=machine):
                self.assertEqual(LINUX_LOCK, self.linux(machine=machine))

    def test_linux_glibc_minimum_and_newer(self):
        for version in ("2.34", "2.35", "2.39", "2.100", "3.0"):
            with self.subTest(version=version):
                self.assertEqual(LINUX_LOCK, self.linux(libc=("glibc", version)))

    def test_unsupported_platforms_fail_before_install(self):
        cases = [dict(system="Darwin"), dict(machine="arm64"), dict(machine="aarch64"),
                 dict(version=(3, 13)), dict(version=(3, 12)),
                 dict(implementation="PyPy"), dict(free_threaded=True)]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(installer.InstallError):
                self.windows(**changes)

    def test_linux_unsupported_python_and_libc(self):
        cases = [dict(version=(3, 14)), dict(version=(3, 11)), dict(machine="arm64"),
                 dict(libc=("musl", "1.2.5")), dict(libc=("glibc", "2.33")),
                 dict(libc=("glibc", "2.9")), dict(libc=("", "")),
                 dict(libc=("glibc", "invalid")), dict(free_threaded=True)]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(installer.InstallError):
                self.linux(**changes)

    def test_32_bit_interpreter_rejected_despite_x64_machine(self):
        for choose in (self.windows, self.linux):
            with self.subTest(platform=choose.__name__), self.assertRaises(installer.InstallError):
                choose(bits=32)


class InstallerInputTests(unittest.TestCase):
    def test_text_is_trimmed(self):
        self.assertEqual("model-v2", installer.check_text("  model-v2  ", "model"))

    def test_text_empty_is_explicit(self):
        self.assertEqual("", installer.check_text("  ", "optional", allow_empty=True))
        with self.assertRaises(installer.InstallError):
            installer.check_text("  ", "required")

    def test_text_rejects_env_line_injection_and_null(self):
        for value in ("synthetic\nSTBRAIN_MCP_HOST=0.0.0.0", "synthetic\rsecret",
                      "synthetic\x00secret", "\nsynthetic", "synthetic\r\n"):
            with self.subTest(kind=repr(value[-2:])), self.assertRaises(installer.InstallError) as caught:
                installer.check_text(value, "API key")
            self.assertNotIn(value, str(caught.exception))

    def test_hidden_input_uses_getpass_without_printing_value(self):
        output = io.StringIO()
        with patch.object(installer.getpass, "getpass", return_value=SYNTHETIC_KEY) as ask, \
                patch("builtins.input", side_effect=AssertionError("visible input forbidden")), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = installer.hidden_input("API key: ")
        ask.assert_called_once()
        self.assertEqual(SYNTHETIC_KEY, result)
        self.assertNotIn(SYNTHETIC_KEY, output.getvalue())

    def test_hidden_input_aborts_if_terminal_would_echo(self):
        def unavailable(*args, **kwargs):
            warnings.warn("synthetic no echo control", getpass.GetPassWarning)
            self.fail("warning must interrupt before visible fallback")
        with patch.object(installer.getpass, "getpass", side_effect=unavailable), \
                patch("builtins.input", side_effect=AssertionError("visible input forbidden")), \
                self.assertRaises(installer.InstallError):
            installer.hidden_input("API key: ")


class InstallerPrivateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="stiller-synthetic-install-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.private = self.parent / "new-private"
        self.addCleanup(patch.stopall)
        # This suite never changes a temporary directory's inherited Windows ACL,
        # never invokes pip, and never starts any service process.
        self.protect = patch.object(installer, "protect_directory").start()
        patch.object(installer.subprocess, "run", side_effect=AssertionError("no external command in unit test")).start()
        patch.object(installer.subprocess, "Popen", side_effect=AssertionError("no service in unit test")).start()
        patch.object(installer, "select_lock", return_value=WINDOWS_LOCK).start()

    def create(self, **changes):
        values = dict(private=self.private, base_url="https://model.test/v1",
                      model=SYNTHETIC_MODEL, api_key=SYNTHETIC_KEY,
                      ports=PORTS, password="", root=ROOT)
        values.update(changes)
        return installer.create_installation(**values)

    def make(self, **changes):
        values = dict(private=self.private, base_url="https://model.test/v1",
                      model=SYNTHETIC_MODEL, api_key=SYNTHETIC_KEY, ports=PORTS)
        values.update(changes)
        return installer.make_config(**values)

    def snapshot(self):
        return {str(path.relative_to(self.private)): path.read_bytes()
                for path in self.private.rglob("*") if path.is_file()}

    def test_safe_directory_accepts_new_absolute_private_path(self):
        self.assertEqual(self.private, installer.safe_directory(self.private, root=ROOT))
        self.assertFalse(self.private.exists())

    def test_safe_directory_rejects_relative_and_source_paths(self):
        for path in (Path("relative-private"), ROOT, ROOT / "synthetic-private"):
            with self.subTest(path=str(path)), self.assertRaises(installer.InstallError):
                installer.safe_directory(path, root=ROOT)

    def test_safe_directory_rejects_linked_ancestor_before_resolve(self):
        real_check = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == self.parent or real_check(path)):
            with self.assertRaises(installer.InstallError):
                installer.safe_directory(self.private, root=ROOT)

    def test_safe_directory_rejects_junction_ancestor(self):
        with patch.object(Path, "is_junction", lambda path: path == self.parent, create=True):
            with self.assertRaises(installer.InstallError):
                installer.safe_directory(self.private, root=ROOT)

    def test_safe_directory_rejects_linked_leaf(self):
        real_check = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == self.private or real_check(path)):
            with self.assertRaises(installer.InstallError):
                installer.safe_directory(self.private, root=ROOT)

    def test_config_is_valid_for_every_existing_component(self):
        config = self.make()
        for component in starter.COMPONENTS:
            with self.subTest(component=component):
                starter.validate(config, component, root=ROOT)
        self.assertFalse(self.private.exists())

    def test_config_keeps_actual_model_and_upstream(self):
        config = self.make()
        self.assertEqual(SYNTHETIC_MODEL, config["STBRAIN_GATEWAY_MODEL"])
        self.assertEqual(SYNTHETIC_MODEL, config["STBRAIN_UPSTREAM_MODEL"])
        self.assertEqual("https://model.test/v1", config["STBRAIN_UPSTREAM_BASE_URL"])
        self.assertEqual(SYNTHETIC_KEY, config["STBRAIN_UPSTREAM_API_KEY"])

    def test_config_generates_five_distinct_local_credentials(self):
        first, second = self.make(), self.make()
        values = [first[key] for key in starter.SECRET_KEYS]
        self.assertEqual(5, len(values))
        self.assertEqual(5, len(set(values)))
        self.assertTrue(all(len(value) >= 32 for value in values))
        self.assertNotIn(SYNTHETIC_KEY, values)
        self.assertTrue(all(first[key] != second[key] for key in starter.SECRET_KEYS))

    def test_config_defaults_simple_tail_layout_and_loopback(self):
        config = self.make()
        self.assertEqual("simple-memory-v1", config["STBRAIN_ACCESS_PROFILE"])
        self.assertEqual("tail-context-v2", config["STBRAIN_GATEWAY_CONTEXT_LAYOUT"])
        self.assertEqual("1", config["STBRAIN_REQUIRE_EXECUTION_BINDING"])
        for name, port in zip(("MCP", "CONTROL", "GATEWAY"), PORTS):
            self.assertEqual("127.0.0.1", config[f"STBRAIN_{name}_HOST"])
            self.assertEqual(str(port), config[f"STBRAIN_{name}_PORT"])
        self.assertEqual("http://127.0.0.1:18795", config["STBRAIN_CONTROL_URL"])

    def test_config_has_three_separate_private_database_paths(self):
        config = self.make()
        paths = [Path(config[key]) for key in starter.DB_KEYS]
        self.assertEqual(3, len(set(paths)))
        for path in paths:
            self.assertTrue(path.is_absolute())
            self.assertIn(self.private, path.parents)
            self.assertNotIn(ROOT, path.parents)

    def test_create_new_private_files_and_marker(self):
        config = self.create()
        self.assertTrue(self.private.is_dir())
        self.assertTrue(self.protect.called)
        parsed = starter.parse_config((self.private / "config.env").read_text(encoding="utf-8-sig"))
        self.assertEqual(config, parsed)
        marker = json.loads((self.private / "installer.json").read_text(encoding="utf-8"))
        self.assertEqual("stiller-installer/1", marker["format"])
        self.assertEqual(str(ROOT.resolve()), marker["source"])
        self.assertEqual(WINDOWS_LOCK, marker["lock"])
        for key in (*starter.SECRET_KEYS, "STBRAIN_UPSTREAM_API_KEY"):
            self.assertNotIn(config[key], json.dumps(marker))
        for component in starter.COMPONENTS:
            starter.validate(config, component, root=ROOT)

    def test_create_does_not_print_credentials(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            config = self.create()
        for key in (*starter.SECRET_KEYS, "STBRAIN_UPSTREAM_API_KEY"):
            self.assertNotIn(config[key], output.getvalue())

    def test_read_resume_is_readonly_without_new_secrets(self):
        config = self.create()
        before = self.snapshot()
        with patch.object(installer, "make_config", side_effect=AssertionError("resume must not remake config")):
            first = installer.read_installation(self.private, root=ROOT)
            second = installer.read_installation(self.private, root=ROOT)
        self.assertEqual(config, first)
        self.assertEqual(first, second)
        self.assertEqual(before, self.snapshot())

    def test_missing_venv_does_not_prevent_readonly_resume(self):
        config = self.create()
        self.assertFalse((self.private / "venv").exists())
        self.assertEqual(config, installer.read_installation(self.private, root=ROOT))

    def test_existing_unknown_directory_is_not_adopted_or_overwritten(self):
        self.private.mkdir()
        sentinel = self.private / "keep.txt"
        sentinel.write_bytes(b"synthetic existing content")
        with self.assertRaises(installer.InstallError):
            self.create()
        with self.assertRaises(installer.InstallError):
            installer.read_installation(self.private, root=ROOT)
        self.assertEqual({"keep.txt": b"synthetic existing content"}, self.snapshot())

    def test_existing_empty_directory_is_not_claimed(self):
        self.private.mkdir()
        with self.assertRaises(installer.InstallError):
            self.create()
        self.assertEqual([], list(self.private.iterdir()))

    def test_existing_known_installation_is_not_recreated(self):
        self.create()
        before = self.snapshot()
        with self.assertRaises(installer.InstallError):
            self.create(api_key="synthetic-other-secret")
        self.assertEqual(before, self.snapshot())

    def test_resume_rejects_changed_marker_source_format_or_lock(self):
        self.create()
        marker_path = self.private / "installer.json"
        original = json.loads(marker_path.read_text(encoding="utf-8"))
        for key, value in (("format", "foreign-installer/1"), ("source", str(self.parent)),
                           ("lock", "foreign.lock")):
            with self.subTest(key=key):
                marker_path.write_text(json.dumps({**original, key: value}), encoding="utf-8")
                before = self.snapshot()
                with self.assertRaises(installer.InstallError):
                    installer.read_installation(self.private, root=ROOT)
                self.assertEqual(before, self.snapshot())
        marker_path.write_text(json.dumps(original), encoding="utf-8")

    def test_resume_rejects_non_object_marker_without_traceback_type(self):
        self.create()
        marker_path = self.private / "installer.json"
        for value in ([], None, "synthetic-invalid-marker"):
            with self.subTest(shape=type(value).__name__):
                marker_path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(installer.InstallError):
                    installer.read_installation(self.private, root=ROOT)

    def test_resume_rejects_linked_marker_config_or_venv(self):
        self.create()
        (self.private / "venv").mkdir()
        real_check = Path.is_symlink
        for filename in ("installer.json", "config.env", "venv"):
            target = self.private / filename
            with self.subTest(filename=filename), \
                    patch.object(Path, "is_symlink", lambda path: path == target or real_check(path)), \
                    self.assertRaises(installer.InstallError):
                installer.read_installation(self.private, root=ROOT)

    def test_resume_rejects_venv_junction(self):
        self.create()
        (self.private / "venv").mkdir()
        with patch.object(Path, "is_junction", lambda path: path == self.private / "venv", create=True), \
                self.assertRaises(installer.InstallError):
            installer.read_installation(self.private, root=ROOT)

    def test_resume_rejects_duplicate_config_key_without_echo(self):
        self.create()
        config_path = self.private / "config.env"
        with config_path.open("a", encoding="utf-8") as stream:
            stream.write("\nSTBRAIN_UPSTREAM_API_KEY=synthetic-duplicate-secret\n")
        with self.assertRaises(installer.InstallError) as caught:
            installer.read_installation(self.private, root=ROOT)
        self.assertNotIn("synthetic-duplicate-secret", str(caught.exception))

    def test_password_hash_contains_no_plaintext_and_validates(self):
        password = "synthetic-module-one-password"
        config = self.create(password=password)
        target = Path(config["STBRAIN_SELF_PASSWORD_HASH_FILE"])
        self.assertIn(self.private, target.parents)
        self.assertNotIn(password.encode(), target.read_bytes())
        starter.validate_self_password_file(str(target), root=ROOT)
        self.assertNotIn(password, (self.private / "config.env").read_text(encoding="utf-8"))

    def test_dependency_failure_resume_keeps_configuration_and_credentials(self):
        config = self.create()
        before = self.snapshot()
        calls = []
        def first_run(command, log, timeout=900):
            calls.append(command)
            if len(calls) == 2:
                raise installer.InstallError("synthetic dependency installation failed")
        with patch.object(installer, "private_log", side_effect=lambda _: contextlib.nullcontext(io.BytesIO())), \
                patch.object(installer, "run_private", side_effect=first_run), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(installer.InstallError):
            installer.install(self.private)
        self.assertEqual(config, installer.read_installation(self.private, root=ROOT))
        self.assertEqual(before, self.snapshot())
        with patch.object(installer, "private_log", side_effect=lambda _: contextlib.nullcontext(io.BytesIO())), \
                patch.object(installer, "run_private") as run, contextlib.redirect_stdout(io.StringIO()):
            installer.install(self.private)
        self.assertEqual(4, run.call_count)
        pip_command = run.call_args_list[1].args[0]
        for option in ("--isolated", "--require-hashes", "--only-binary=:all:", "--no-deps"):
            self.assertIn(option, pip_command)
        self.assertIn("https://pypi.org/simple", pip_command)
        self.assertIn("--verify", run.call_args_list[-1].args[0])
        self.assertEqual(before, self.snapshot())

    def test_health_checks_only_loopback_health_and_mcp_catalogue(self):
        config = self.make()
        requests = []
        response_limits = []
        children = []
        class Response:
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, size):
                response_limits.append(size)
                return json.dumps(self.payload).encode()
        def open_request(request, timeout):
            self.assertLessEqual(timeout, 2)
            payload = json.loads(request.data) if request.data else None
            requests.append((request.full_url, payload, dict(request.header_items())))
            if request.full_url in ("http://127.0.0.1:18795/health", "http://127.0.0.1:18796/health"):
                self.assertIsNone(payload)
                self.assertNotIn("Authorization", dict(request.header_items()))
                return Response({"ok": True})
            self.assertEqual("http://127.0.0.1:18794/mcp", request.full_url)
            self.assertEqual("Bearer " + config["STBRAIN_MCP_TOKEN"], request.get_header("Authorization"))
            self.assertIn(payload["method"], ("initialize", "notifications/initialized", "tools/list"))
            if payload["method"] == "tools/list":
                return Response({"result": {"tools": [{"name": name} for name in
                    ("stbrain_help", "remember_memory", "revise_memory")]}})
            return Response({"result": {}})
        def popen(*args, **kwargs):
            child = SimpleNamespace(poll=lambda: None, stopped=False)
            children.append(child)
            return child
        def stack(current, popen, sleeper):
            self.assertEqual(config, current)
            for role in ("control", "mcp", "gateway"):
                popen(["synthetic-python", role])
            try:
                sleeper(.25)
            except KeyboardInterrupt:
                return 0
            finally:
                for child in children:
                    child.stopped = True
        with patch.object(installer, "helper", return_value=SimpleNamespace(start_stack=stack)), \
                patch.object(installer, "build_opener", return_value=SimpleNamespace(open=open_request)) as build, \
                patch.object(installer, "private_log", side_effect=lambda _: contextlib.nullcontext(io.BytesIO())), \
                patch.object(installer.subprocess, "Popen", side_effect=popen):
            installer.verify_services(config, self.private)
        self.assertEqual(5, len(requests))
        self.assertTrue(all(limit == 2 * 1024 * 1024 + 1 for limit in response_limits))
        self.assertTrue(all(child.stopped for child in children))
        handlers = build.call_args.args
        self.assertTrue(any(isinstance(handler, installer.NoRedirect) for handler in handlers))
        self.assertTrue(any(isinstance(handler, installer.ProxyHandler) and handler.proxies == {} for handler in handlers))

    def test_health_occupied_port_does_not_launch_or_stop_any_process(self):
        config = self.make()
        ops = installer.helper("stiller_ops")
        with patch.object(installer, "helper", return_value=ops), \
                patch.object(ops, "check_ports_stopped", side_effect=ops.OperationError("synthetic occupied port")), \
                patch.object(ops, "stop_children") as stop, \
                patch.object(installer, "private_log", side_effect=lambda _: contextlib.nullcontext(io.BytesIO())), \
                patch.object(installer.subprocess, "Popen") as launch, \
                self.assertRaises(ops.OperationError):
            installer.verify_services(config, self.private)
        launch.assert_not_called()
        stop.assert_not_called()

    def test_health_redirect_rejected_without_echoing_target(self):
        target = "https://synthetic.invalid/?key=synthetic-do-not-echo"
        with self.assertRaises(installer.InstallError) as caught:
            installer.NoRedirect().redirect_request(None, None, 302, "synthetic", {}, target)
        self.assertNotIn(target, str(caught.exception))

    def test_clean_child_environment_drops_inherited_brain_and_python_overrides(self):
        with patch.dict(os.environ, {"STBRAIN_UPSTREAM_API_KEY": "synthetic-inherited-key",
                                     "STBRAIN_DB_PATH": "synthetic-inherited-path",
                                     "PYTHONPATH": "synthetic-foreign-imports",
                                     "PYTHONHOME": "synthetic-foreign-python"}):
            result = installer.clean_environment()
        self.assertFalse(any(key.startswith("STBRAIN_") for key in result))
        self.assertNotIn("PYTHONPATH", result)
        self.assertNotIn("PYTHONHOME", result)
        self.assertEqual("1", result["PYTHONNOUSERSITE"])

    def source_check_passes(self):
        real_helper = installer.helper
        def load(name):
            if name == "check_source_package":
                return SimpleNamespace(failures=lambda root: [])
            return real_helper(name)
        return patch.object(installer, "helper", side_effect=load)

    def test_main_new_install_questionnaire_uses_hidden_key_and_real_creation(self):
        output = io.StringIO()
        password = "synthetic-questionnaire-password"
        with self.source_check_passes(), \
                patch("builtins.input", side_effect=[str(self.private), "https://model.test/v1", SYNTHETIC_MODEL, "yes"]) as visible, \
                patch.object(installer.getpass, "getpass", side_effect=[SYNTHETIC_KEY, password, password]) as hidden, \
                patch.object(installer, "free_ports", return_value=PORTS), \
                patch.object(installer, "install") as install, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = installer.main([])
        self.assertEqual(0, result)
        self.assertEqual(4, visible.call_count)
        self.assertEqual(3, hidden.call_count)
        install.assert_called_once_with(self.private)
        config = installer.read_installation(self.private, root=ROOT)
        self.assertEqual(SYNTHETIC_KEY, config["STBRAIN_UPSTREAM_API_KEY"])
        self.assertEqual(SYNTHETIC_MODEL, config["STBRAIN_GATEWAY_MODEL"])
        self.assertTrue(Path(config["STBRAIN_SELF_PASSWORD_HASH_FILE"]).is_file())
        for secret in (SYNTHETIC_KEY, password, *(config[key] for key in starter.SECRET_KEYS)):
            self.assertNotIn(secret, output.getvalue())

    def test_main_user_cancellation_creates_no_directory(self):
        output = io.StringIO()
        with self.source_check_passes(), \
                patch("builtins.input", side_effect=["https://model.test/v1", SYNTHETIC_MODEL, "no"]), \
                patch.object(installer.getpass, "getpass", side_effect=[SYNTHETIC_KEY, ""]), \
                patch.object(installer, "free_ports") as choose_ports, \
                patch.object(installer, "install") as install, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = installer.main(["--directory", str(self.private)])
        self.assertEqual(0, result)
        self.assertFalse(self.private.exists())
        self.assertFalse(self.protect.called)
        choose_ports.assert_not_called()
        install.assert_not_called()
        self.assertNotIn(SYNTHETIC_KEY, output.getvalue())

    def test_main_unknown_resume_directory_never_requests_or_prints_key(self):
        self.private.mkdir()
        (self.private / "keep.txt").write_text(SYNTHETIC_KEY, encoding="utf-8")
        before = self.snapshot()
        output = io.StringIO()
        with self.source_check_passes(), patch("builtins.input") as visible, \
                patch.object(installer.getpass, "getpass") as hidden, \
                patch.object(installer, "install") as install, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = installer.main(["--directory", str(self.private)])
        self.assertEqual(1, result)
        visible.assert_not_called()
        hidden.assert_not_called()
        install.assert_not_called()
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(SYNTHETIC_KEY, output.getvalue())

    def test_main_unsupported_platform_refuses_before_prompt_or_creation(self):
        output = io.StringIO()
        with patch.object(installer, "select_lock", side_effect=installer.InstallError("synthetic unsupported platform")), \
                patch.object(installer, "helper") as load, \
                patch("builtins.input") as visible, patch.object(installer.getpass, "getpass") as hidden, \
                patch.object(installer, "install") as install, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = installer.main(["--directory", str(self.private)])
        self.assertEqual(1, result)
        load.assert_not_called()
        visible.assert_not_called()
        hidden.assert_not_called()
        install.assert_not_called()
        self.assertFalse(self.private.exists())

    def test_main_verify_rejects_foreign_python_before_starting(self):
        self.create()
        before = self.snapshot()
        with self.source_check_passes(), patch.object(sys, "prefix", str(self.parent / "foreign-venv")), \
                patch.object(installer, "verify_services") as verify, \
                patch.object(installer, "install") as install, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = installer.main(["--verify", str(self.private)])
        self.assertEqual(1, result)
        verify.assert_not_called()
        install.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_main_verify_accepts_only_own_private_venv(self):
        config = self.create()
        (self.private / "venv").mkdir()
        before = self.snapshot()
        with self.source_check_passes(), patch.object(sys, "prefix", str(self.private / "venv")), \
                patch.object(installer, "verify_services") as verify, \
                patch.object(installer, "install") as install, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = installer.main(["--verify", str(self.private)])
        self.assertEqual(0, result)
        verify.assert_called_once_with(config, self.private)
        install.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_connection_document_persists_correct_private_start_command(self):
        self.create()
        documents = [path for path in self.private.iterdir() if path.suffix == ".txt"]
        self.assertEqual(1, len(documents))
        text = documents[0].read_text(encoding="utf-8")
        expected = installer.quote_command([installer.venv_python(self.private), "-B",
            ROOT / "scripts/install_stiller.py", "--start", self.private])
        self.assertIn(expected, text)
        self.assertNotIn(SYNTHETIC_KEY, text)

    @unittest.skipIf(os.name == "nt", "POSIX modes; Windows ACL helper is mocked")
    def test_private_generated_files_have_owner_only_mode(self):
        self.create(password="synthetic-password")
        for path in self.private.rglob("*"):
            if path.is_file():
                with self.subTest(filename=path.name):
                    self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
