"""No production paths; exercise the synthetic SQLite path allowlist itself."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.sqlite_fixture_guard import is_synthetic_sqlite_path


class SyntheticSqliteGuardTests(unittest.TestCase):
    def setUp(self):
        self.scratch = self.enterContext(tempfile.TemporaryDirectory(prefix="sqlite-guard-synthetic-"))
        self.root = Path(self.scratch).resolve()
        self.path = self.root / "existing 中文.db"
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE synthetic(value TEXT)")
        self.allowed = {self.path, self.root / "not-created.db"}

    def test_ordinary_exact_paths_still_allow_fixture_initialization(self):
        self.assertTrue(is_synthetic_sqlite_path(self.path, self.allowed))
        self.assertTrue(is_synthetic_sqlite_path(str(self.path), self.allowed))
        self.assertTrue(is_synthetic_sqlite_path(self.root / "not-created.db", self.allowed))

    def test_only_exact_existing_file_readonly_and_readwrite_uri_are_accepted(self):
        for mode in ("ro", "rw"):
            with self.subTest(mode=mode):
                uri = self.path.as_uri() + "?mode=" + mode
                self.assertTrue(is_synthetic_sqlite_path(uri, self.allowed, uri=True))
                with closing(sqlite3.connect(uri, uri=True)) as db:
                    self.assertEqual(0, db.execute("SELECT COUNT(*) FROM synthetic").fetchone()[0])

    def test_uri_requires_true_flag_and_existing_file(self):
        uri = self.path.as_uri() + "?mode=ro"
        for flag in (False, None, 1, "true"):
            self.assertFalse(is_synthetic_sqlite_path(uri, self.allowed, uri=flag))
        missing = (self.root / "not-created.db").as_uri() + "?mode=rw"
        self.assertFalse(is_synthetic_sqlite_path(missing, self.allowed, uri=True))

    def test_query_and_alias_variants_are_rejected_without_parsing(self):
        for suffix in ("", "?mode=rwc", "?mode=memory", "?mode=ro&immutable=1", "?mode=rw&cache=shared",
                       "?mode=ro#fragment", "?mode=ro&mode=rw", "?MODE=ro", "?mode=%72o"):
            self.assertFalse(is_synthetic_sqlite_path(self.path.as_uri() + suffix, self.allowed, uri=True))
        self.assertFalse(is_synthetic_sqlite_path(self.path.as_uri().replace("file:///", "file://localhost/") + "?mode=ro",
                                                 self.allowed, uri=True))

    def test_foreign_files_and_memory_are_rejected(self):
        foreign = self.root / "not-allowed.db"
        with closing(sqlite3.connect(foreign)):
            pass
        for value in (foreign, str(foreign), foreign.as_uri() + "?mode=ro", ":memory:", "file::memory:?cache=shared", b"main.db", None):
            self.assertFalse(is_synthetic_sqlite_path(value, self.allowed, uri=True))


if __name__ == "__main__":
    unittest.main()
