"""Synthetic Git checkout/ZIP byte-integrity tests; no network or real history."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("source_package_eol", ROOT / "scripts/check_source_package.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)
GIT = shutil.which("git")


def git(directory, *args):
    # Use only a new synthetic local index. No credentials, hooks, user config,
    # remote repository, commits, tags, or copied .git directory are involved.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    result = subprocess.run([GIT, "-c", "core.hooksPath=" + str(directory / "unused-hooks"), *args],
                            cwd=directory, env=env, capture_output=True, timeout=30)
    if result.returncode:
        raise AssertionError("Synthetic Git operation failed: " + result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def fixture(root):
    root.mkdir()
    canonical = {}
    names = CHECK.REQUIRED | {".gitignore", ".env.example", "docs/example.svg", "future-format.conf"}
    for name in sorted(names):
        canonical[name] = ("Synthetic fixture content.\n" * 15).encode("utf-8")
    canonical[".gitattributes"] = (ROOT / ".gitattributes").read_bytes().replace(b"\r\n", b"\n")
    canonical["docs/example.png"] = b"\x89PNG\r\n\x1a\n\x00SYNTHETIC-BINARY\r\n\x00"
    for name, data in canonical.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    rows = [{"path": name, "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(canonical.items())]
    canonical[CHECK.MANIFEST] = (json.dumps({"format": "stiller-source-package/1",
        "distribution": "source-available-development-preview", "files": rows}, indent=2) + "\n").encode("utf-8")
    (root / CHECK.MANIFEST).write_bytes(canonical[CHECK.MANIFEST])
    return canonical


def index_fixture(root):
    git(root, "init", "--quiet")
    git(root, "-c", "core.autocrlf=false", "add", "--all")


@unittest.skipUnless(GIT, "Git executable required for isolated source-package checkout tests")
class SourcePackageEolTests(unittest.TestCase):
    def assert_exact(self, root, canonical):
        files = {p.relative_to(root).as_posix(): p.read_bytes()
                 for p in root.rglob("*") if p.is_file()}
        self.assertEqual(canonical, files)
        self.assertEqual([], CHECK.failures(root))

    def test_git_checkout_preserves_bytes_with_all_autocrlf_modes(self):
        with tempfile.TemporaryDirectory(prefix="stiller-synthetic-eol-") as tmp:
            parent = Path(tmp)
            root = parent / "index-source"
            canonical = fixture(root)
            index_fixture(root)
            for mode in ("false", "true", "input"):
                with self.subTest(autocrlf=mode):
                    checkout = parent / ("checkout-" + mode)
                    checkout.mkdir()
                    git(root, "-c", "core.autocrlf=" + mode, "-c", "core.eol=crlf",
                        "checkout-index", "--all", "--prefix=" + checkout.as_posix() + "/")
                    self.assert_exact(checkout, canonical)

    def test_crlf_worktree_index_normalizes_text_and_preserves_binary(self):
        with tempfile.TemporaryDirectory(prefix="stiller-synthetic-eol-") as tmp:
            parent = Path(tmp)
            root = parent / "crlf-source"
            canonical = fixture(root)
            for name, data in canonical.items():
                if name != "docs/example.png":
                    (root / name).write_bytes(data.replace(b"\n", b"\r\n"))
            index_fixture(root)
            checkout = parent / "lf-checkout"
            checkout.mkdir()
            git(root, "-c", "core.autocrlf=true", "checkout-index", "--all",
                "--prefix=" + checkout.as_posix() + "/")
            self.assert_exact(checkout, canonical)

    def test_git_archive_zip_crc_and_raw_manifest_hashes(self):
        with tempfile.TemporaryDirectory(prefix="stiller-synthetic-zip-") as tmp:
            parent = Path(tmp)
            root = parent / "index-source"
            canonical = fixture(root)
            index_fixture(root)
            tree = git(root, "write-tree").decode("ascii").strip()
            archive = parent / "synthetic-source.zip"
            git(root, "archive", "--format=zip", "--output=" + str(archive), tree)
            extracted = parent / "unpacked"
            extracted.mkdir()
            with zipfile.ZipFile(archive) as zf:
                self.assertIsNone(zf.testzip())
                self.assertEqual(set(canonical), {i.filename for i in zf.infolist() if not i.is_dir()})
                for name, data in canonical.items():
                    self.assertEqual(data, zf.read(name))
                    target = extracted / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(name))
            self.assert_exact(extracted, canonical)

    def test_changed_newlines_are_still_rejected_by_strict_checker(self):
        with tempfile.TemporaryDirectory(prefix="stiller-synthetic-eol-") as tmp:
            root = Path(tmp) / "source"
            canonical = fixture(root)
            (root / "README.md").write_bytes(canonical["README.md"].replace(b"\n", b"\r\n"))
            self.assertIn("Archive content hash mismatch: README.md", CHECK.failures(root))


if __name__ == "__main__":
    unittest.main()
