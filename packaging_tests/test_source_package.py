import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("source_package", ROOT / "scripts/check_source_package.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class SourcePackageTests(unittest.TestCase):
    def make(self, root):
        for name in CHECK.REQUIRED:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Synthetic fixture content. " * 15, encoding="utf-8")
        self.manifest(root)

    def manifest(self, root):
        rows = [{"path": p.relative_to(root).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in sorted(root.rglob("*")) if p.is_file() and p.name != CHECK.MANIFEST]
        (root / CHECK.MANIFEST).write_text(json.dumps({"format": "stiller-source-package/1",
            "distribution": "source-available-development-preview", "files": rows}), encoding="utf-8")

    def test_valid_read_only_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make(root)
            before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
            self.assertEqual([], CHECK.failures(root))
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()})

    def test_modified_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make(root)
            (root / "README.md").write_text("changed", encoding="utf-8")
            self.assertTrue(any("hash mismatch" in e for e in CHECK.failures(root)))

    def test_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make(root)
            (root / "NOTICE").unlink()
            self.assertTrue(any("file missing" in e for e in CHECK.failures(root)))

    def test_added_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make(root)
            (root / "unexpected.txt").write_text("synthetic", encoding="utf-8")
            self.assertTrue(any("file-set mismatch" in e for e in CHECK.failures(root)))

    def test_forbidden_artifact_even_if_manifest_lists_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make(root)
            for name in (".env", "private.db", "backup.zip", "state.sqlite-wal"):
                (root / name).write_text("synthetic", encoding="utf-8")
            self.manifest(root)
            errors = CHECK.failures(root)
            self.assertEqual(4, sum("Forbidden artifact" in e for e in errors))

    def test_env_example_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make(root)
            (root / ".env.example").write_text("REPLACE_WITH_YOUR_VALUE", encoding="utf-8")
            self.manifest(root)
            self.assertEqual([], CHECK.failures(root))

    def test_nested_private_directory_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make(root)
            (root / "private").mkdir()
            self.assertTrue(any("Forbidden directory" in e for e in CHECK.failures(root)))

    def test_forbidden_directory_names_are_case_insensitive(self):
        for name in ("Private", "SECRETS", "Logs", ".VENV", "Evidence", "Private-Verification"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make(root)
                directory = root / "nested" / name
                directory.mkdir(parents=True)
                (directory / "fixture.txt").write_text("synthetic", encoding="utf-8")
                self.manifest(root)
                self.assertIn("Forbidden directory: nested/" + name, CHECK.failures(root))

    def test_git_case_variants_are_not_silently_ignored(self):
        for name in (".GIT", "nested/.Git"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make(root)
                (root / name).mkdir(parents=True)
                self.assertIn("Forbidden directory: " + name, CHECK.failures(root))

    def test_unsafe_or_duplicate_manifest_rows_fail(self):
        for name in ("../outside", "/outside", "C:/outside", "a\\b", "README.md"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make(root)
                path = root / CHECK.MANIFEST
                data = json.loads(path.read_text(encoding="utf-8"))
                data["files"].append({"path": name, "sha256": "0" * 64})
                path.write_text(json.dumps(data), encoding="utf-8")
                self.assertIn("Source manifest missing or invalid", CHECK.failures(root))

    def test_truncated_manifest_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make(root)
            (root / CHECK.MANIFEST).write_text("{", encoding="utf-8")
            self.assertIn("Source manifest missing or invalid", CHECK.failures(root))

    def test_does_not_claim_production_readiness(self):
        self.assertFalse(hasattr(CHECK, "production_ready"))
        original = (ROOT / "scripts/check_release.py").read_text(encoding="utf-8")
        self.assertIn('"user_acceptance_complete"', original)
        self.assertIn('"publication_authorized"', original)


if __name__ == "__main__":
    unittest.main()
