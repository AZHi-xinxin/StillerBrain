"""Only temporary synthetic SQLite stores; no service, model or network calls."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest

import jsonschema

from runtime.tool_guidance import ToolGuidanceError, ToolGuidanceStore
from tests.test_tool_guidance import action_card, catalog


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def snapshot(path):
    with closing(sqlite3.connect(path)) as connection:
        schema = connection.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'
        ).fetchall()
        tables = [row[1] for row in schema if row[0] == 'table']
        rows = {name: connection.execute(
            f'SELECT rowid,* FROM {quote(name)} ORDER BY rowid'
        ).fetchall() for name in tables}
        return schema, rows


class FailAtConnection(sqlite3.Connection):
    fail_prefix = None

    def execute(self, sql, parameters=()):
        if self.fail_prefix and sql.startswith(self.fail_prefix):
            self.fail_prefix = None
            raise sqlite3.OperationalError('synthetic_migration_failure')
        return super().execute(sql, parameters)


class ToolExperienceConfidenceMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tool-experience-migration-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'synthetic.db'
        self.store = ToolGuidanceStore(self.path)
        self.owner, self.model = 'synthetic-owner', 'synthetic-model'
        created = self.store.remember(
            owner_id=self.owner, model_id=self.model, wake_id='synthetic-create',
            expected_row_version=0, catalog=catalog(), **action_card(),
        )
        self.card = created['card']['card_id']

    def record(self, confidence, **overrides):
        arguments = dict(
            owner_id=self.owner, model_id=self.model, wake_id='synthetic-experience',
            expected_row_version=self.store.status(owner_id=self.owner, model_id=self.model)['row_version'],
            card_id=self.card, outcome='unknown', reason_code='synthetic_result',
            attempt_summary='A synthetic attempt with no external execution.',
            lesson='Consult the current native result.', confidence=confidence,
        )
        arguments.update(overrides)
        return self.store.record_experience(**arguments)

    def legacy_fixture(self, *, dependents=False):
        self.record(0)
        self.record(70)
        self.record(80)
        # Build the previous real CHECK in an isolated fixture, without calling
        # or mocking the migration under test. Prove it rejects 81 below.
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('BEGIN IMMEDIATE')
            ddl = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='tool_experiences'"
            ).fetchone()[0]
            self.assertEqual(1, ddl.count('CHECK(confidence BETWEEN 0 AND 100)'))
            ddl = ddl.replace('CHECK(confidence BETWEEN 0 AND 100)', 'CHECK(confidence BETWEEN 0 AND 80)')
            ddl = ddl.replace('CREATE TABLE tool_experiences', 'CREATE TABLE legacy_fixture_experiences', 1)
            connection.execute(ddl)
            connection.execute('INSERT INTO legacy_fixture_experiences SELECT * FROM tool_experiences')
            connection.execute('DROP TABLE tool_experiences')
            connection.execute('ALTER TABLE legacy_fixture_experiences RENAME TO tool_experiences')
            connection.execute('CREATE INDEX idx_tool_experiences_card '
                               'ON tool_experiences(owner_id, model_id, card_id, occurred_at)')
            connection.execute('UPDATE tool_experiences SET rowid=rowid+40')
            if dependents:
                connection.executescript('''
                    CREATE TABLE experience_reference_fixture (
                        id TEXT PRIMARY KEY,
                        experience_id TEXT REFERENCES tool_experiences(experience_id) ON DELETE CASCADE
                    );
                    CREATE TABLE experience_trigger_fixture (id INTEGER PRIMARY KEY, marker TEXT);
                    CREATE INDEX fixture_experience_confidence ON tool_experiences(confidence DESC);
                    CREATE TRIGGER fixture_experience_added AFTER INSERT ON tool_experiences
                    BEGIN
                        INSERT INTO experience_trigger_fixture(marker) VALUES('new_experience');
                    END;
                    CREATE VIEW fixture_experience_view AS
                        SELECT experience_id, confidence FROM tool_experiences;
                ''')
                experience_id = connection.execute('SELECT experience_id FROM tool_experiences LIMIT 1').fetchone()[0]
                connection.execute('INSERT INTO experience_reference_fixture VALUES (?,?)', ('synthetic-ref', experience_id))
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute('UPDATE tool_experiences SET confidence=81')
            connection.rollback()

    def test_new_schema_and_runtime_accept_zero_through_one_hundred_without_verification(self):
        schema = json.loads((Path(__file__).parents[1] / 'schemas/tool-guidance.schema.json').read_text(encoding='utf-8'))
        for confidence in (0, 80, 81, 100):
            result = self.record(confidence)
            experience = result['experience']
            self.assertEqual(confidence, experience['confidence'])
            self.assertEqual('ai_reported', experience['provenance'])
            self.assertIsNone(experience['evidence_ref'])
            self.assertFalse(experience['verified'])
            self.assertFalse(result['execution_performed'])
            # Test this change's scalar schema contract. Existing response-only
            # catalog hashes are not described by the old full experience schema.
            jsonschema.Draft202012Validator(
                schema['$defs']['experience']['properties']['confidence']
            ).validate(experience['confidence'])
        for confidence in (-1, 101, True, False, '100', 80.5, None, [], {}):
            before = snapshot(self.path)
            with self.assertRaisesRegex(ToolGuidanceError, '^invalid_confidence$'):
                self.record(confidence)
            self.assertEqual(before, snapshot(self.path))
        for forbidden in ({'provenance': 'verified'}, {'verified': True}, {'evidence_ref': 'synthetic-receipt'}):
            before = snapshot(self.path)
            with self.assertRaises(TypeError):
                self.record(100, **forbidden)
            self.assertEqual(before, snapshot(self.path))

    def test_migration_preserves_all_rows_rowids_audit_indexes_triggers_views_and_foreign_keys(self):
        self.legacy_fixture(dependents=True)
        before_schema, before_rows = snapshot(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('PRAGMA foreign_keys=ON')
            before_fk = connection.execute('PRAGMA foreign_key_list(tool_experiences)').fetchall()
            ToolGuidanceStore._migrate_experience_confidence(connection)
            self.assertEqual(1, connection.execute('PRAGMA foreign_keys').fetchone()[0])
            self.assertEqual(0, connection.execute('PRAGMA legacy_alter_table').fetchone()[0])
            self.assertEqual([], connection.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(before_fk, connection.execute('PRAGMA foreign_key_list(tool_experiences)').fetchall())
            self.assertEqual(3, connection.execute('SELECT COUNT(*) FROM fixture_experience_view').fetchone()[0])
            self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM experience_trigger_fixture').fetchone()[0])
        after_schema, after_rows = snapshot(self.path)
        self.assertEqual(before_rows, after_rows)
        without_experience = lambda rows: [row for row in rows if not (row[0] == 'table' and row[1] == 'tool_experiences')]
        self.assertEqual(without_experience(before_schema), without_experience(after_schema))
        old_ddl = next(row[3] for row in before_schema if row[0] == 'table' and row[1] == 'tool_experiences')
        new_ddl = next(row[3] for row in after_schema if row[0] == 'table' and row[1] == 'tool_experiences')
        self.assertEqual(old_ddl.replace('CHECK(confidence BETWEEN 0 AND 80)', 'CHECK(confidence BETWEEN 0 AND 100)'), new_ddl)
        # The genuine constructor is now idempotent; a new 100 is still merely
        # an AI report and fires the preserved INSERT trigger exactly once.
        self.store = ToolGuidanceStore(self.path)
        self.assertEqual((after_schema, after_rows), snapshot(self.path))
        self.assertEqual('ai_reported', self.record(100)['experience']['provenance'])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(1, connection.execute('SELECT COUNT(*) FROM experience_trigger_fixture').fetchone()[0])
            self.assertEqual(1, connection.execute('SELECT COUNT(*) FROM experience_reference_fixture').fetchone()[0])

    def test_constructor_migrates_legacy_once_and_repeated_starts_leave_data_unchanged(self):
        self.legacy_fixture()
        _, before_rows = snapshot(self.path)
        self.store = ToolGuidanceStore(self.path)
        after = snapshot(self.path)
        self.assertEqual(before_rows, after[1])
        self.store = ToolGuidanceStore(self.path)
        self.assertEqual(after, snapshot(self.path))
        self.assertEqual(100, self.record(100)['experience']['confidence'])

    def test_two_connections_that_both_observe_legacy_recheck_under_write_lock(self):
        self.legacy_fixture(dependents=True)
        before_rows = snapshot(self.path)[1]
        barrier = threading.Barrier(2, timeout=10)
        ddl_counts = []

        class SynchronizedConnection(sqlite3.Connection):
            first_schema_read = True

            def execute(connection, sql, parameters=()):
                cursor = super().execute(sql, parameters)
                if connection.first_schema_read and sql.startswith('SELECT sql FROM sqlite_master'):
                    connection.first_schema_read = False
                    row = cursor.fetchone()
                    cursor.close()
                    self.assertIn('BETWEEN 0 AND 80', row[0])
                    barrier.wait()
                    return SimpleNamespace(fetchone=lambda: row)
                if sql.startswith('CREATE TABLE "tool_experiences_confidence_100_migration"'):
                    ddl_counts.append(1)
                return cursor

        def worker():
            with closing(sqlite3.connect(self.path, timeout=10, factory=SynchronizedConnection)) as connection:
                connection.execute('PRAGMA foreign_keys=ON')
                ToolGuidanceStore._migrate_experience_confidence(connection)
                return (connection.execute('PRAGMA foreign_keys').fetchone()[0],
                        connection.execute('PRAGMA foreign_key_check').fetchall())

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual([(1, []), (1, [])], results)
        self.assertEqual(1, len(ddl_counts))
        self.assertEqual(before_rows, snapshot(self.path)[1])
        self.assertEqual(100, self.record(100)['experience']['confidence'])

    def test_failures_before_and_after_drop_rollback_entire_migration_and_restore_connection_flags(self):
        self.legacy_fixture(dependents=True)
        before = snapshot(self.path)
        phases = (
            'CREATE TABLE "tool_experiences_confidence_100_migration"',
            'INSERT INTO tool_experiences_confidence_100_migration',
            'DROP TABLE tool_experiences',
            'ALTER TABLE tool_experiences_confidence_100_migration',
            'CREATE INDEX fixture_experience_confidence',
            'CREATE TRIGGER fixture_experience_added',
            'PRAGMA foreign_key_check',
        )
        for phase in phases:
            with self.subTest(phase=phase), closing(sqlite3.connect(self.path, factory=FailAtConnection)) as connection:
                connection.execute('PRAGMA foreign_keys=ON')
                connection.fail_prefix = phase
                with self.assertRaisesRegex(sqlite3.OperationalError, '^synthetic_migration_failure$'):
                    ToolGuidanceStore._migrate_experience_confidence(connection)
                self.assertFalse(connection.in_transaction)
                self.assertEqual(1, connection.execute('PRAGMA foreign_keys').fetchone()[0])
                self.assertEqual(0, connection.execute('PRAGMA legacy_alter_table').fetchone()[0])
                self.assertEqual(before, snapshot(self.path))
        ToolGuidanceStore(self.path)
        self.assertEqual(before[1], snapshot(self.path)[1])

    def test_foreign_key_failure_rolls_back_and_keeps_original_invalid_rows_for_repair(self):
        self.legacy_fixture()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE tool_experiences SET card_id='synthetic-missing-card'")
            connection.commit()
        before = snapshot(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            with self.assertRaisesRegex(ToolGuidanceError, '^experience_confidence_migration_foreign_key_failure$'):
                ToolGuidanceStore._migrate_experience_confidence(connection)
        self.assertEqual(before, snapshot(self.path))

    def test_expanded_schema_still_rejects_forged_provenance_receipt_and_out_of_range_sql(self):
        self.legacy_fixture()
        ToolGuidanceStore(self.path)
        for assignment in ("provenance='verified'", "evidence_ref='synthetic-receipt'", 'confidence=101', 'confidence=-1'):
            before = snapshot(self.path)
            with closing(sqlite3.connect(self.path)) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute('UPDATE tool_experiences SET ' + assignment)
                connection.rollback()
            self.assertEqual(before, snapshot(self.path))


if __name__ == '__main__':
    unittest.main()
