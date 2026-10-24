"""Synthetic pictures and existing authorization; no model calls or remote services."""
from contextlib import closing
import json
import sqlite3
import unittest

from runtime.memory_image_recall import image_attachment_hint, append_image_hints
from tests import test_memory_images as fixtures
from tests.test_memory_relations import OWNER


class MemoryImageRecallTests(unittest.TestCase):
    setUp = fixtures.MemoryImageStoreTests.setUp
    put = fixtures.MemoryImageStoreTests.put
    sql = fixtures.MemoryImageStoreTests.sql

    def hint(self, **changes):
        with closing(sqlite3.connect(self.database)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            return image_attachment_hint(db, **{**OWNER, 'target_ref': 'emotion://e1@1', **changes})

    def test_marker_has_no_bytes_description_or_write_and_only_when_attached(self):
        self.assertIsNone(self.hint())
        self.put(description='PRIVATE_PIXEL_DESCRIPTION')
        hint = self.hint()
        self.assertEqual(1, hint['count'])
        self.assertFalse(hint['image_bytes_included'])
        self.assertEqual('list_memory_images', hint['lookup']['tool'])
        self.assertNotIn('PRIVATE', json.dumps(hint))
        self.assertIsNone(self.hint(owner_id='foreign'))

    def test_archived_restricted_never_and_removed_images_do_not_leak_presence(self):
        self.put()
        for column, value in [('sensitivity', 'intimate'), ('sensitivity', 'restricted'),
                              ('explicit_request_override', 'never'), ('lifecycle', 'archived')]:
            self.sql("UPDATE emotion_memories SET sensitivity='private',explicit_request_override='allow_after_confirmation',lifecycle='active'")
            self.sql('UPDATE emotion_memories SET '+column+'=? WHERE memory_id=?', (value, 'e1'))
            self.assertIsNone(self.hint(), (column, value))
        self.sql("UPDATE emotion_memories SET lifecycle='active'")
        self.sql('UPDATE memory_image_items SET active=0')
        self.assertIsNone(self.hint())

    def test_only_selected_memory_gets_hint_under_residual_budget(self):
        self.put()
        dynamic={'emotional_memory': {'memories': [{'memory_id': 'e1', 'summary': 'synthetic'}]}}
        estimate=lambda x:len(json.dumps(x, ensure_ascii=False))
        with closing(sqlite3.connect(self.database)) as db:
            db.row_factory=sqlite3.Row; db.execute('PRAGMA query_only=ON')
            changed=append_image_hints(db, **OWNER, dynamic=dynamic, estimate_tokens=estimate, budget=2000)
            self.assertEqual(1, changed['memory_images']['attachments'][0]['count'])
            self.assertNotIn('memory_images', dynamic)
            self.assertIs(dynamic, append_image_hints(db, **OWNER, dynamic=dynamic, estimate_tokens=estimate, budget=estimate(dynamic)))
            unrelated={'emotional_memory': {'memories': [{'memory_id':'e2', 'summary':'emotion://e1@1'}]}}
            self.assertIs(unrelated, append_image_hints(db, **OWNER, dynamic=unrelated, estimate_tokens=estimate, budget=2000))
            self.assertEqual(0, db.total_changes)

    def test_older_or_damaged_image_schema_cannot_break_text_recall(self):
        with closing(sqlite3.connect(':memory:')) as db:
            self.assertIsNone(image_attachment_hint(db, **OWNER, target_ref='emotion://e1@1'))
            db.execute('CREATE TABLE memory_image_items (broken TEXT)')
            self.assertIsNone(image_attachment_hint(db, **OWNER, target_ref='emotion://e1@1'))
