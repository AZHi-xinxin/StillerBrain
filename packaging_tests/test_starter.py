import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(filename):
    spec = importlib.util.spec_from_file_location(filename, ROOT / "scripts" / (filename + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


starter = module("run_component")
release = module("check_release")


class StarterTests(unittest.TestCase):
    def config(self, private):
        value = {key: "synthetic-test-only-" + str(index) * 32 for index, key in enumerate(starter.SECRET_KEYS)}
        value.update({key: str(private / (str(index) + ".db")) for index, key in enumerate(starter.DB_KEYS)})
        value.update(STBRAIN_OWNER_ID="owner:test", STBRAIN_MODEL_ID="model:test", STBRAIN_HUMAN_ACTOR_ID="human:test", STBRAIN_EXECUTION_EPOCH="synthetic-deploy-1", STBRAIN_REQUIRE_EXECUTION_BINDING="1", STBRAIN_CONTROL_URL="http://127.0.0.1:18795", STBRAIN_UPSTREAM_BASE_URL="https://model.test/v1", STBRAIN_UPSTREAM_API_KEY="synthetic-upstream-key", STBRAIN_GATEWAY_MODEL="test-model", STBRAIN_UPSTREAM_MODEL="test-model")
        for name, port in zip(("MCP", "CONTROL", "GATEWAY"), (18794, 18795, 18796)):
            value[f"STBRAIN_{name}_HOST"] = "127.0.0.1"
            value[f"STBRAIN_{name}_PORT"] = str(port)
        return value

    def test_config_validates_without_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            for component in starter.COMPONENTS:
                starter.validate(self.config(Path(tmp)), component)

    def test_duplicate_key_rejected_without_values(self):
        with self.assertRaises(ValueError) as result:
            starter.parse_config("STBRAIN_TEST=private_value\nSTBRAIN_TEST=another_secret")
        self.assertNotIn("private_value", str(result.exception))
        self.assertNotIn("another_secret", str(result.exception))

    def test_context_layout_opt_in_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            for layout in ("legacy", "anchored-v1"):
                config = self.config(Path(tmp))
                config["STBRAIN_GATEWAY_CONTEXT_LAYOUT"] = layout
                starter.validate(config, "gateway")

    def test_unknown_context_layout_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            config["STBRAIN_GATEWAY_CONTEXT_LAYOUT"] = "anchored_typo"
            with self.assertRaisesRegex(ValueError, "Context layout"):
                starter.validate(config, "gateway")

    def test_equals_preserved(self):
        self.assertEqual("a=b", starter.parse_config("STBRAIN_TEST=a=b")["STBRAIN_TEST"])

    def test_unsafe_variants_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = [
                ("STBRAIN_REQUIRE_EXECUTION_BINDING", "0"),
                ("STBRAIN_EXECUTION_EPOCH", ""),
                ("STBRAIN_GATEWAY_HOST", "0.0.0.0"),
                ("STBRAIN_DB_PATH", str(ROOT / "data.db")),
                ("STBRAIN_DB_PATH", "relative.db"),
                ("STBRAIN_CONTROL_URL", "http://remote.test:18795"),
                ("STBRAIN_UPSTREAM_BASE_URL", "http://model.test/v1"),
                ("STBRAIN_UPSTREAM_BASE_URL", "https://user:secret@model.test/v1"),
                ("STBRAIN_UPSTREAM_BASE_URL", "https://provider.example/v1"),
            ]
            for key, value in cases:
                with self.subTest(key=key):
                    config = self.config(Path(tmp))
                    config[key] = value
                    with self.assertRaises(ValueError):
                        starter.validate(config, "gateway")

    def test_credential_reuse_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            config["STBRAIN_HOST_TOKEN"] = config["STBRAIN_MCP_TOKEN"]
            with self.assertRaises(ValueError):
                starter.validate(config, "mcp")

    def test_shared_db_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            config["STBRAIN_HALLUCINATION_VAULT_DB_PATH"] = config["STBRAIN_DB_PATH"]
            with self.assertRaises(ValueError):
                starter.validate(config, "mcp")

    def test_missing_license_and_release_state_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = release.failures(Path(tmp))
            self.assertTrue(any("LICENSE" in failure for failure in result))
            self.assertTrue(any("publication_authorized" in failure for failure in result))

    def test_database_and_env_block_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "private.db").write_bytes(b"synthetic")
            (root / ".env").write_text("synthetic")
            (root / ".env.example").write_text("synthetic")
            result = release.failures(root)
            self.assertTrue(any("private.db" in failure for failure in result))
            self.assertTrue(any(failure.endswith(".env") for failure in result))
            self.assertFalse(any(failure.endswith(".env.example") for failure in result))


if __name__ == "__main__":
    unittest.main()
