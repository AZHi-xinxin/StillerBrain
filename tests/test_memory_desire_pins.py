from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from runtime.memory_desires import MemoryDesireStore, DesireError, PIN_DDL
from runtime.memory_relations import MemoryRelationError
from tests import test_memory_desires as desire_tests
from tests.test_memory_relations import OWNER
from tests import test_memory_images as fixtures


class MemoryDesirePinTests(unittest.TestCase):
    def setUp(self):
        fixtures.MemoryImageStoreTests.setUp(self)
        self.store = MemoryDesireStore(self.database)
        self.identity = tuple(OWNER.values())

    sql = fixtures.MemoryImageStoreTests.sql
    act = desire_tests.MemoryDesireTests.act
    recall = desire_tests.MemoryDesireTests.recall

    def add_parent(self, memory_id):
        with closing(sqlite3.connect(self.database)) as db:
            columns = [r[1] for r in db.execute('PRAGMA table_info(emotion_memories)')]
            expressions = ['?' if c == 'memory_id' else c for c in columns]
            db.execute(f"INSERT INTO emotion_memories ({','.join(columns)}) "
                       f"SELECT {','.join(expressions)} FROM emotion_memories WHERE memory_id='e1'",
                       (memory_id,))
            db.commit()

    def test_pin_order_pagination_and_unpin_keep_parent_unchanged(self):
        self.add_parent('pin-other')
        before = self.sql('SELECT * FROM emotion_memories ORDER BY memory_id')
        self.act()
        self.act(memory_ref='emotion://pin-other@1')
        pin = self.act('pin', expected_revision=1)
        self.assertTrue(pin['desire']['pinned'])
        self.assertEqual(2, pin['desire']['revision'])
        page = self.act('list', limit=1)
        self.assertEqual('emotion://e1@1', page['desires'][0]['memory_ref'])
        self.assertEqual(1, page['next_offset'])
        self.assertEqual('emotion://pin-other@1', self.act('list', offset=1, limit=1)['desires'][0]['memory_ref'])
        self.assertTrue(self.act('read')['desire']['pinned'])
        with self.assertRaisesRegex(DesireError, 'revision_conflict'):
            self.act('unpin', expected_revision=1)
        self.assertFalse(self.act('pin', expected_revision=2)['state_changed'])
        self.assertFalse(self.act('unpin', expected_revision=2)['desire']['pinned'])
        self.assertEqual(before, self.sql('SELECT * FROM emotion_memories ORDER BY memory_id'))
        self.assertEqual([(2, 1), (3, 0)], self.sql(
            'SELECT revision,pinned FROM memory_desire_pin_history ORDER BY revision'))

    def test_many_pins_do_not_become_core_pins_or_bypass_recall_threshold(self):
        for i in range(7):
            self.add_parent(f'p{i}')
            self.act(memory_ref=f'emotion://p{i}@1')
            self.act('pin', memory_ref=f'emotion://p{i}@1')
        self.assertEqual(7, sum(x['pinned'] for x in self.act('list')['desires']))
        self.act(); self.act('pin')
        self.assertNotIn('desires', self.recall(score=.8))
        self.assertNotIn('desires', self.recall(score=.99, presentation='neutral_hint'))
        self.assertIn('desires', self.recall(score=.9))
        self.sql("UPDATE emotion_memories SET sensitivity='restricted' WHERE memory_id='e1'")
        self.assertNotIn('desires', self.recall(score=.99))
        self.assertNotIn('emotion://e1@1', [x['memory_ref'] for x in self.act('list')['desires']])

    def test_archive_keeps_pin_delete_clears_it_recreation_does_not_restore_it(self):
        self.act(); self.act('pin')
        self.assertTrue(self.act('archive')['desire']['pinned'])
        self.assertIn('已归档', self.recall()['desires']['attachments'][0]['text'])
        self.assertTrue(self.act('restore')['desire']['pinned'])
        self.assertFalse(self.act('delete')['desire']['pinned'])
        self.assertEqual([], self.act('list')['desires'])
        with self.assertRaisesRegex(DesireError, 'desire_not_found'): self.act('pin')
        self.assertFalse(self.act()['desire']['pinned'])

    def test_missing_foreign_or_revoked_targets_cannot_be_pinned(self):
        with self.assertRaisesRegex(DesireError, 'desire_not_found'): self.act('pin')
        with self.assertRaises(MemoryRelationError):
            self.act('pin', memory_ref='emotion://foreign@1')
        self.act()
        def deny(db): raise DesireError('synthetic_permission_revoked')
        with self.assertRaisesRegex(DesireError, 'permission_revoked'):
            self.store.manage(identity=self.identity, guard=deny, action='pin', memory_ref='emotion://e1@1')
        self.assertEqual([], self.sql('SELECT * FROM memory_desire_pins'))
        self.assertEqual(1, self.act('read')['desire']['revision'])

    def test_old_database_upgrade_preserves_notes_and_history_and_reopen(self):
        self.act(); self.act('archive')
        notes = self.sql('SELECT * FROM memory_desires')
        history = self.sql('SELECT * FROM memory_desire_history')
        for name in PIN_DDL: self.sql(f'DROP TABLE {name}')
        self.store = MemoryDesireStore(self.database)
        self.assertEqual(notes, self.sql('SELECT * FROM memory_desires'))
        self.assertEqual(history, self.sql('SELECT * FROM memory_desire_history'))
        self.assertFalse(self.act('read')['desire']['pinned'])
        self.act('pin')
        self.store = MemoryDesireStore(self.database)
        self.assertTrue(self.act('read')['desire']['pinned'])
        self.assertEqual('archived', self.act('read')['desire']['state'])

    def test_pin_write_failure_rolls_back_note_revision_and_both_histories(self):
        self.act()
        before = {table: self.sql(f'SELECT * FROM {table}') for table in (
            'memory_desires', 'memory_desire_history', 'memory_desire_pins', 'memory_desire_pin_history')}
        with patch.object(self.store, '_record_pin', side_effect=DesireError('synthetic_write_failure')):
            with self.assertRaisesRegex(DesireError, 'write_failure'):
                self.act('pin')
        for table, rows in before.items():
            self.assertEqual(rows, self.sql(f'SELECT * FROM {table}'))

    def test_partial_pin_schema_is_rejected_without_repair_or_data_loss(self):
        self.act()
        self.sql('DROP TABLE memory_desire_pin_history')
        before = self.sql('SELECT * FROM memory_desires')
        with self.assertRaisesRegex(DesireError, 'storage_unavailable'):
            MemoryDesireStore(self.database)
        self.assertEqual(before, self.sql('SELECT * FROM memory_desires'))
        self.assertEqual([], self.sql("SELECT name FROM sqlite_master WHERE name='memory_desire_pin_history'"))

