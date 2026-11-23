"""Synthetic persistence, full-context expansion and legacy-upgrade coverage."""
import json
import sqlite3
import unittest

from runtime.emotional_memory import EmotionalMemoryStore
from runtime.emotion_plan_bindings import append_bound_plans
from runtime.onboarding import _estimate_tokens
from tests import test_force_recall as fixtures


class EmotionPlanBindingTests(unittest.TestCase):
    setUp = fixtures.ForceRecallTests.setUp
    tearDown = fixtures.ForceRecallTests.tearDown
    save = fixtures.ForceRecallTests.save

    def expand(self, dynamic):
        with self.stores['emotion']._connect() as db:
            return append_bound_plans(db, **self.common, dynamic=dynamic,
                                      planning_store=self.stores['plan'])

    def emotional(self, ref):
        return {'emotional_memory': {'memories': [{'memory_id': ref.split('://')[1].split('@')[0]}]}}

    def test_all_twenty_plans_recur_without_count_time_or_token_cap(self):
        refs = [self.save('plan') for _ in range(20)]
        emotion = self.save(bound_plan_refs=refs)
        base = {**self.emotional(emotion), 'existing_data': '已有上下文' * 1200}
        first = self.expand(base)
        self.assertGreater(_estimate_tokens(first), 1200)
        self.assertEqual(refs, [p['item_ref'] for p in first['emotion_bound_plans']['items']])
        for _ in range(5):
            self.assertEqual(first, self.expand(base))
        EmotionalMemoryStore(self.path)  # restart: bindings persist, no cooldown bookkeeping
        self.assertEqual(first, self.expand(base))
        self.assertNotIn('emotion_bound_plans', base)

    def test_actual_force_selection_and_other_emotion_channels_expand(self):
        plan = self.save('plan'); emotion = self.save(bound_plan_refs=[plan])
        for dynamic in ({'force_recall': {'items': [{'item_ref': emotion}]}},
                        {'emotion_layer': {'memories': [{'item_ref': emotion}]}},
                        {'session_continuity': {'recent_memories': [{'item_ref': emotion}]}}):
            self.assertEqual(plan, self.expand(dynamic)['emotion_bound_plans']['items'][0]['item_ref'])
        for dynamic in ({}, {'force_recall': {'items': [], 'omitted_count': 1}},
                        {'planning_memory': {'envelopes': [{'item_ref': plan}]}},
                        {'emotional_memory': {'memories': [], 'reminder': '待审数量'}}):
            self.assertEqual(dynamic, self.expand(dynamic))

    def test_same_plan_has_one_attached_body_with_both_sources(self):
        plan = self.save('plan')
        emotions = [self.save(bound_plan_refs=[plan, plan]) for _ in range(2)]
        dynamic = {'emotional_memory': {'memories': [
            self.emotional(ref)['emotional_memory']['memories'][0] for ref in emotions]}}
        items = self.expand(dynamic)['emotion_bound_plans']['items']
        self.assertEqual(1, len(items))
        self.assertEqual(set(emotions), set(items[0]['source_emotion_refs']))

    def test_missing_or_foreign_plan_never_crosses_owner_boundary(self):
        plan = self.save('plan')
        missing = 'plan://plan_' + 'f' * 32 + '@1'
        emotion = self.save(bound_plan_refs=[plan, missing])
        with self.stores['emotion']._connect() as db:
            db.execute("UPDATE planning_items SET owner_id='different-owner'")
        self.assertNotIn('emotion_bound_plans', self.expand(self.emotional(emotion)))

    def test_old_database_adds_only_empty_binding_and_preserves_history(self):
        self.save()
        with self.stores['emotion']._connect() as db:
            history = [tuple(r) for r in db.execute('SELECT * FROM emotion_memory_versions')]
            db.execute('ALTER TABLE emotion_memories DROP COLUMN bound_plan_refs_json')
            original = [tuple(r) for r in db.execute('SELECT * FROM emotion_memories')]
            columns = [r['name'] for r in db.execute('PRAGMA table_info(emotion_memories)')]
        EmotionalMemoryStore(self.path)
        with self.stores['emotion']._connect() as db:
            restored = [tuple(r) for r in db.execute('SELECT '+','.join(columns)+' FROM emotion_memories')]
            self.assertEqual(original, restored)
            self.assertEqual(history, [tuple(r) for r in db.execute('SELECT * FROM emotion_memory_versions')])
            self.assertEqual('[]', db.execute('SELECT bound_plan_refs_json FROM emotion_memories').fetchone()[0])

    def test_progress_and_paused_completed_states_do_not_suppress_link(self):
        plan = self.save('plan'); emotion = self.save(bound_plan_refs=[plan])
        plan_id = plan.split('://')[1].split('@')[0]
        store = self.stores['plan']
        for state_event in ('progress', 'pause', 'resume', 'complete'):
            # State transitions are existing functionality; seed their normal
            # persisted events and exercise the actual fold/recall readers.
            with store._connect() as db:
                store._append_event(db, **self.common, plan_id=plan_id,
                    event_type=state_event, reason='synthetic progress', evidence=[],
                    wake_id='synthetic-state', wake_seq=1)
            item = self.expand(self.emotional(emotion))['emotion_bound_plans']['items'][0]
            self.assertEqual(plan, item['item_ref'])
        self.assertEqual('completed', item['state']['state'])

    def test_binding_replacement_and_null_preserve_previous_versions_and_plan(self):
        first = self.save('plan'); second = self.save('plan')
        emotion = self.save(bound_plan_refs=[first]); memory_id = emotion.split('://')[1].split('@')[0]
        store = self.stores['emotion']
        for version, refs in ((1, [second]), (2, None)):
            store.revise(**self.common, wake_id='synthetic-edit',
                expected_row_version=store.status(**self.common)['row_version'],
                memory_id=memory_id, expected_memory_version=version,
                changes={'bound_plan_refs': refs}, reason='synthetic binding change')
            attached = self.expand(self.emotional(emotion)).get('emotion_bound_plans', {}).get('items', [])
            self.assertEqual(refs or [], [p['item_ref'] for p in attached])
        with store._connect() as db:
            history = [json.loads(r[0])['bound_plan_refs'] for r in db.execute(
                'SELECT mutable_json FROM emotion_memory_versions WHERE memory_id=? ORDER BY version', (memory_id,))]
            self.assertEqual([[first], [second], []], history)
            self.assertEqual(2, db.execute('SELECT COUNT(*) FROM planning_items').fetchone()[0])
