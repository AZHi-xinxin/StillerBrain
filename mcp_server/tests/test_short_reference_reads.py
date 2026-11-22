"""Synthetic identity isolation and version resolution for exact short reads."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mcp_server.short_reference_reads import resolve_short_read_fields, ShortReferenceError
from runtime.recall_projection import short_ref


class ShortReferenceReadTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(tmp) / 'synthetic.db'
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE learning_items (learning_id, current_version, owner_id, model_id)')
            db.executemany('INSERT INTO learning_items VALUES (?,?,?,?)', [
                ('mine', 3, 'owner', 'model'),
                ('foreign-owner', 1, 'other', 'model'),
                ('foreign-model', 1, 'owner', 'other')])

    def resolve(self, fields):
        return resolve_short_read_fields(database=self.db, owner_id='owner',
            model_id='model', module='learning', fields=fields)

    def test_foreign_owner_and_model_are_not_targets(self):
        for identity in ('foreign-owner', 'foreign-model'):
            with self.assertRaisesRegex(ShortReferenceError, 'short_reference_not_found'):
                self.resolve({'target_ref': short_ref('learning://' + identity)})

    def test_latest_version_not_original_injection_version_and_no_write(self):
        fields = {'query': short_ref('learning://mine'), 'include_evidence': False}
        before = self.db.read_bytes()
        self.assertEqual({'query':'', 'target_ref':'learning://mine@3', 'include_evidence':False},
                         self.resolve(fields))
        self.assertEqual(before, self.db.read_bytes())
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE learning_items SET current_version=4 WHERE learning_id='mine'")
        self.assertEqual('learning://mine@4', self.resolve(fields)['target_ref'])

    def test_full_reference_and_search_are_unchanged(self):
        for fields in ({'target_ref':'learning://mine@2'}, {'query':'ordinary search'}):
            self.assertIs(fields, self.resolve(fields))

    def test_multiple_targets_rejected_instead_of_changing_request(self):
        with self.assertRaisesRegex(ShortReferenceError, 'short_reference_requires_one_target'):
            self.resolve({'target_ref':short_ref('learning://mine'), 'query':'another topic'})

    def test_missing_database_does_not_create_one(self):
        self.db.unlink()
        with self.assertRaisesRegex(ShortReferenceError, 'short_reference_lookup_unavailable'):
            self.resolve({'target_ref':short_ref('learning://mine')})
        self.assertFalse(self.db.exists())
