"""Bounded synthetic children only; catchable signals, never SIGKILL guarantees."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from packaging_tests import test_operations as fixtures

ops = fixtures.ops
ROOT = Path(__file__).resolve().parents[1]


class FakeSignals:
    SIGTERM = 15
    SIGHUP = 1

    def __init__(self):
        self.originals = {self.SIGTERM: lambda *_: None, self.SIGHUP: lambda *_: None}
        self.handlers = dict(self.originals)
        self.calls = []

    def getsignal(self, number):
        return self.handlers[number]

    def signal(self, number, handler):
        previous = self.handlers[number]
        self.handlers[number] = handler
        self.calls.append((number, handler))
        return previous

    def send(self, number):
        self.handlers[number](number, None)


class PosixSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OperationsTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = self.fixture.config
        self.fake = FakeSignals()
        self.enterContext(patch.object(ops, "signal", self.fake))
        # Do not mutate the process-wide os.name or pathlib's platform behavior.
        self.enterContext(patch.object(ops, "os", SimpleNamespace(name="posix", environ=os.environ)))

    def assert_restored(self):
        self.assertEqual(self.fake.originals, self.fake.handlers)

    def test_handlers_defer_repeated_signals_and_restore(self):
        with ops._posix_stop_requests() as requested:
            self.assertFalse(requested())
            self.fake.send(self.fake.SIGTERM)
            self.fake.send(self.fake.SIGHUP)
            self.fake.send(self.fake.SIGTERM)
            self.assertTrue(requested())
        self.assert_restored()

    def test_windows_signal_policy_is_untouched(self):
        with patch.object(ops.os, "name", "nt"), ops._posix_stop_requests() as requested:
            self.assertFalse(requested())
        self.assertFalse(self.fake.calls)

    def test_non_main_thread_signal_policy_is_untouched(self):
        results = []
        def worker():
            with ops._posix_stop_requests() as requested:
                results.append(requested())
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual([False], results)
        self.assertFalse(self.fake.calls)

    def test_unrestorable_non_python_handler_is_not_replaced(self):
        self.fake.handlers[self.fake.SIGHUP] = None
        with ops._posix_stop_requests() as requested:
            self.fake.send(self.fake.SIGTERM)
            self.assertTrue(requested())
        self.assertIsNone(self.fake.handlers[self.fake.SIGHUP])
        self.assertFalse(any(number == self.fake.SIGHUP for number, _ in self.fake.calls))
        self.assertIs(self.fake.originals[self.fake.SIGTERM], self.fake.handlers[self.fake.SIGTERM])

    def test_available_posix_signal_is_used_when_sighup_is_absent(self):
        supported = SimpleNamespace(SIGTERM=self.fake.SIGTERM,
                                    getsignal=self.fake.getsignal, signal=self.fake.signal)
        with patch.object(ops, "signal", supported), ops._posix_stop_requests() as requested:
            self.fake.send(self.fake.SIGTERM)
            self.assertTrue(requested())
        self.assert_restored()

    def test_partial_handler_install_failure_restores_installed_handler(self):
        original = self.fake.signal
        def fail_second(number, handler):
            if number == self.fake.SIGHUP:
                raise OSError("synthetic unavailable signal")
            return original(number, handler)
        with patch.object(self.fake, "signal", side_effect=fail_second):
            with self.assertRaises(OSError), ops._posix_stop_requests():
                self.fail("handler setup must fail before creating children")
        self.assert_restored()

    def test_signal_during_creation_registers_child_before_cleanup(self):
        children = []
        def launch(*_, **__):
            child = fixtures.FakeChild()
            children.append(child)
            # Signal arrives before Popen's return value is appended by caller.
            self.fake.send(self.fake.SIGHUP)
            self.fake.send(self.fake.SIGTERM)
            return child
        self.assertEqual(0, ops.start_stack(self.config, popen=launch, job_factory=lambda: None))
        self.assertEqual(1, len(children))
        self.assertTrue(children[0].terminated)
        self.assertIsNotNone(children[0].poll())
        self.assert_restored()

    def test_repeated_signal_during_cleanup_does_not_interrupt_owned_children(self):
        children = [fixtures.FakeChild() for _ in range(3)]
        remaining = iter(children)
        for child in children:
            original_wait = child.wait
            def wait(timeout, original=original_wait):
                self.fake.send(self.fake.SIGTERM)
                self.fake.send(self.fake.SIGHUP)
                return original(timeout)
            child.wait = wait
        def stop(_):
            self.fake.send(self.fake.SIGTERM)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, ops.start_stack(self.config, popen=lambda *_, **__: next(remaining),
                                               sleeper=stop, job_factory=lambda: None))
        self.assertTrue(all(child.terminated and child.poll() is not None for child in children))
        self.assert_restored()

    def test_startup_failure_with_signal_cleans_partial_group_and_restores(self):
        child = fixtures.FakeChild()
        def launch(*_, **__):
            if not getattr(launch, "called", False):
                launch.called = True
                return child
            self.fake.send(self.fake.SIGHUP)
            raise OSError("synthetic launch failure")
        with self.assertRaisesRegex(ops.OperationError, "mcp service could not be launched"):
            ops.start_stack(self.config, popen=launch, job_factory=lambda: None)
        self.assertTrue(child.terminated)
        self.assert_restored()

    def test_cleanup_failure_still_restores_handlers(self):
        child = fixtures.FakeChild()
        def launch(*_, **__):
            self.fake.send(self.fake.SIGTERM)
            return child
        with patch.object(child, "wait", side_effect=OSError("synthetic wait failure")):
            with self.assertRaisesRegex(ops.OperationError, "could not be confirmed stopped"):
                ops.start_stack(self.config, popen=launch, job_factory=lambda: None)
        self.assertTrue(child.terminated)
        self.assert_restored()

    def test_preflight_port_failure_does_not_install_handlers_or_launch(self):
        with patch.object(ops, "check_ports_stopped", side_effect=ops.OperationError("synthetic occupied")):
            with self.assertRaises(ops.OperationError):
                ops.start_stack(self.config, popen=lambda *_, **__: self.fail("must not launch"))
        self.assertFalse(self.fake.calls)


@unittest.skipUnless(os.name == "posix" and hasattr(signal, "SIGHUP"),
                     "Real SIGTERM/SIGHUP process test requires a POSIX host")
class PosixSyntheticProcessTests(unittest.TestCase):
    def run_signal_harness(self, signal_name, fail_launch=False):
        fixture = fixtures.OperationsTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config_file = fixture.private / "synthetic-config.json"
        config_file.write_text(json.dumps(fixture.config), encoding="utf-8")
        harness = r'''
import importlib.util, json, os, signal, subprocess, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("signal_ops", Path(sys.argv[1]) / "scripts/stiller_ops.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)
config = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
chosen = getattr(signal, sys.argv[3])
fail_launch = sys.argv[4] == "failure"
observed = []
def original_handler(number, frame):
    observed.append(number)
signal.signal(signal.SIGTERM, original_handler)
signal.signal(signal.SIGHUP, original_handler)
children = []
unrelated = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(15)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def launch(command, **kwargs):
    if fail_launch and children:
        os.kill(os.getpid(), chosen)
        raise OSError("synthetic partial launch failure")
    child = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(15)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    children.append(child)
    return child
def request_stop(_):
    os.kill(os.getpid(), chosen)
    os.kill(os.getpid(), chosen)
try:
    try:
        result = ops.start_stack(config, popen=launch, sleeper=request_stop, job_factory=lambda: None)
        assert not fail_launch and result == 0
    except ops.OperationError:
        assert fail_launch
    assert len(children) == (1 if fail_launch else 3)
    assert all(child.poll() is not None for child in children)
    assert unrelated.poll() is None, "unrelated synthetic process was stopped"
    assert not observed, "original handler was called before cleanup"
    assert signal.getsignal(signal.SIGTERM) is original_handler
    assert signal.getsignal(signal.SIGHUP) is original_handler
    os.kill(os.getpid(), chosen)
    assert observed == [chosen], "original handler was not restored"
    print("SYNTHETIC_SIGNAL_CLEANUP_OK", flush=True)
finally:
    for child in children + [unrelated]:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", harness, str(ROOT), str(config_file),
                                 signal_name, "failure" if fail_launch else "normal"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=fixture.private, env={"PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1"}, timeout=20)
        self.assertEqual(0, result.returncode, "Bounded synthetic POSIX signal cleanup failed")
        self.assertIn(b"SYNTHETIC_SIGNAL_CLEANUP_OK", result.stdout)

    def test_real_sigterm_stops_only_owned_children_and_restores(self):
        self.run_signal_harness("SIGTERM")

    def test_real_sighup_stops_only_owned_children_and_restores(self):
        self.run_signal_harness("SIGHUP")

    def test_real_sigterm_partial_launch_failure_restores(self):
        self.run_signal_harness("SIGTERM", fail_launch=True)

    def test_real_sighup_partial_launch_failure_restores(self):
        self.run_signal_harness("SIGHUP", fail_launch=True)


if __name__ == "__main__":
    unittest.main()
