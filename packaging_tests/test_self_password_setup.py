"""Offline installation checks with temporary synthetic password hashes only."""
import contextlib
import getpass
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings

from packaging_tests import test_starter as starter_fixtures
from packaging_tests.test_operations import ops
from mcp_server.self_password import SelfPasswordAuthority, encode_password

ROOT = Path(__file__).resolve().parents[1]
starter = starter_fixtures.starter
SPEC = importlib.util.spec_from_file_location("synthetic_self_password_setup", ROOT / "scripts/configure_self_password.py")
SETUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SETUP)


class SelfPasswordSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="stiller-synthetic-password-")
        self.addCleanup(temporary.cleanup)
        self.private = Path(temporary.name)
        self.path = self.private / "self-password.json"
        self.password = "synthetic-" + secrets.token_hex(16)

    def config(self):
        result = starter_fixtures.StarterTests().config(self.private)
        result.update(STBRAIN_ACCESS_PROFILE="simple-memory-v1",
                      STBRAIN_GATEWAY_CONTEXT_LAYOUT="tail-context-v2")
        return result

    def create(self):
        SETUP.create_hash(self.path, self.password, self.password)
        return self.path

    def test_generated_hash_matches_installed_authority_format(self):
        self.create()
        raw = self.path.read_bytes()
        record = json.loads(raw)
        self.assertNotIn(self.password.encode(), raw)
        self.assertEqual({"format", "salt_hex", "digest_hex"}, set(record))
        self.assertEqual(encode_password(self.password, salt=bytes.fromhex(record["salt_hex"])), record)
        authority = SelfPasswordAuthority(self.path)
        self.assertEqual("authorized", authority.authorize(self.password)["decision"])
        starter.validate_self_password_file(str(self.path))

    def test_random_salts_and_private_posix_mode(self):
        self.create()
        second = self.private / "second.json"
        SETUP.create_hash(second, self.password, self.password)
        self.assertNotEqual(self.path.read_bytes(), second.read_bytes())
        if os.name != "nt":
            self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))

    def test_existing_file_refused_and_preserved(self):
        self.create()
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(original, self.path.read_bytes())

    def test_invalid_or_mismatched_inputs_do_not_create_file(self):
        for first, second in (("", ""), (self.password, "different"), ("x" * 1025, "x" * 1025)):
            with self.subTest(kind=len(first)), self.assertRaises(ValueError):
                SETUP.create_hash(self.path, first, second)
            self.assertFalse(self.path.exists())

    def test_relative_source_and_missing_parent_paths_refused(self):
        cases = ((Path("relative.json"), ROOT), (self.private / "inside.json", self.private),
                 (self.private / "missing" / "hash.json", ROOT))
        for value, root in cases:
            with self.subTest(case=value.name), self.assertRaises(ValueError):
                SETUP.private_target(value, root)

    def test_link_targets_and_ancestors_refused_without_following(self):
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ValueError):
                SETUP.private_target(self.path)
            with self.assertRaisesRegex(ValueError, "Self-password hash"):
                starter.validate_self_password_file(str(self.path))

    def test_cli_hidden_input_and_no_plaintext_or_hash_in_output(self):
        output = io.StringIO()
        with patch.object(SETUP.getpass, "getpass", side_effect=[self.password, self.password]) as prompt, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(0, SETUP.main(["--output", str(self.path)]))
        self.assertEqual(2, prompt.call_count)
        self.assertNotIn(self.password, output.getvalue())
        self.assertNotIn(json.loads(self.path.read_bytes())["digest_hex"], output.getvalue())
        self.assertIn("STBRAIN_SELF_PASSWORD_HASH_FILE", output.getvalue())

    def test_cli_refuses_visible_getpass_fallback(self):
        def unhidden(_):
            warnings.warn("synthetic fallback", getpass.GetPassWarning)
            self.fail("getpass fallback must stop before visible input")
        with patch.object(SETUP.getpass, "getpass", side_effect=unhidden), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, SETUP.main(["--output", str(self.path)]))
        self.assertFalse(self.path.exists())

    def test_cli_refuses_existing_file_before_requesting_password(self):
        self.create()
        with patch.object(SETUP.getpass, "getpass") as prompt, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, SETUP.main(["--output", str(self.path)]))
        prompt.assert_not_called()

    def test_configured_hash_checked_for_all_components_without_mutation(self):
        self.create()
        config = self.config()
        config["STBRAIN_SELF_PASSWORD_HASH_FILE"] = str(self.path)
        before = dict(config)
        original = self.path.read_bytes()
        for component in starter.COMPONENTS:
            starter.validate(config, component)
        self.assertEqual(before, config)
        self.assertEqual(original, self.path.read_bytes())

    def test_optional_hash_and_legacy_profile_preserve_old_configuration(self):
        for profile in (None, "", "legacy", "simple-memory-v1"):
            with self.subTest(profile=profile):
                config = self.config()
                if profile is None:
                    config.pop("STBRAIN_ACCESS_PROFILE")
                    config.pop("STBRAIN_GATEWAY_CONTEXT_LAYOUT")
                else:
                    config["STBRAIN_ACCESS_PROFILE"] = profile
                before = dict(config)
                for component in starter.COMPONENTS:
                    starter.validate(config, component)
                self.assertEqual(before, config)
                self.assertFalse(self.path.exists())

    def test_unknown_profile_and_layout_fail_without_echoing_values(self):
        for key in ("STBRAIN_ACCESS_PROFILE", "STBRAIN_GATEWAY_CONTEXT_LAYOUT"):
            config = self.config()
            config[key] = self.password
            with self.assertRaises(ValueError) as rejected:
                starter.validate(config, "gateway")
            self.assertNotIn(self.password, str(rejected.exception))

    def test_hash_schema_size_and_path_fail_closed_without_echo(self):
        self.create()
        good = json.loads(self.path.read_bytes())
        invalid = [[], {**good, "unexpected": self.password}, {**good, "format": self.password},
                   {**good, "salt_hex": "g" * 32}, {**good, "digest_hex": "f" * 63},
                   {**good, "salt_hex": 123}, {**good, "digest_hex": None}]
        for value in invalid:
            self.path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Self-password hash") as rejected:
                starter.validate_self_password_file(str(self.path))
            self.assertNotIn(self.password, str(rejected.exception))
        for data in (self.password.encode(), b"x" * 1025, b"\xff"):
            self.path.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "Self-password hash"):
                starter.validate_self_password_file(str(self.path))
        for value, root in (("relative.json", ROOT), (str(self.private), ROOT),
                            (str(self.private / "missing.json"), ROOT), (str(self.path), self.private)):
            with self.assertRaisesRegex(ValueError, "Self-password hash"):
                starter.validate_self_password_file(value, root)

    def test_child_environment_forwards_current_settings_and_clears_inherited_values(self):
        self.create()
        config = self.config()
        config["STBRAIN_SELF_PASSWORD_HASH_FILE"] = str(self.path)
        with patch.dict(os.environ, {"STBRAIN_ACCESS_PROFILE": "host-ignored",
                                     "STBRAIN_UNKNOWN_INHERITED": "host-ignored"}):
            result = ops.child_environment(config)
        for key in ("STBRAIN_ACCESS_PROFILE", "STBRAIN_GATEWAY_CONTEXT_LAYOUT",
                    "STBRAIN_SELF_PASSWORD_HASH_FILE", "STBRAIN_REQUIRE_EXECUTION_BINDING"):
            self.assertEqual(config[key], result[key])
        self.assertNotIn("STBRAIN_UNKNOWN_INHERITED", result)

    def test_new_template_explicitly_selects_current_profile_and_layout(self):
        config = starter.parse_config((ROOT / ".env.example").read_text(encoding="utf-8"))
        self.assertEqual("simple-memory-v1", config["STBRAIN_ACCESS_PROFILE"])
        self.assertEqual("tail-context-v2", config["STBRAIN_GATEWAY_CONTEXT_LAYOUT"])
        self.assertEqual("1", config["STBRAIN_REQUIRE_EXECUTION_BINDING"])
        self.assertEqual("", config["STBRAIN_SELF_PASSWORD_HASH_FILE"])


if __name__ == "__main__":
    unittest.main()
