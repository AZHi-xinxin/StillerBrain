"""Real SQLite links and exact model projection, using synthetic memories only."""
import copy
import json
import unittest

from runtime.associated_recall import append_associated_recall
from runtime.emotion_plan_bindings import append_bound_plans
from runtime.memory_relations import MemoryRelationStore
from runtime.recall_projection import project_dynamic, short_ref, relation_lines
from runtime.onboarding import ModuleOneOnboardingStore, _sha256
from tests import test_force_recall as fixtures


class AssociatedRecallTests(unittest.TestCase):
    setUp = fixtures.ForceRecallTests.setUp
    tearDown = fixtures.ForceRecallTests.tearDown
    save = fixtures.ForceRecallTests.save

    def link(self, left, right, **options):
        return MemoryRelationStore(self.path).attach(
            **self.common, from_ref=left, to_ref=right, **{'type': 'same_event', **options})

    def expand(self, roots, enabled=lambda _: True):
        dynamic = {'force_recall': {'items': [{'item_ref': ref, 'text': '原始命中摘要'} for ref in roots]}}
        with self.stores['emotion']._connect() as db:
            result = append_associated_recall(db, **self.common, dynamic=dynamic, enabled=enabled, query='ex')
            result = append_bound_plans(db, **self.common, dynamic=result, planning_store=self.stores['plan'])
        return result

    def test_relation_then_bound_plan_is_one_line_and_no_repeated_plan(self):
        plan = self.save('plan')
        first, second = self.save(), self.save(bound_plan_refs=[plan])
        self.link(first, second)
        result = self.expand([first, plan])
        wire = project_dynamic(result)
        self.assertEqual([short_ref(first)+'—同一事件→'+short_ref(second)+'—绑定→'+short_ref(plan)], wire['关联'])
        self.assertEqual({short_ref(first), short_ref(second)}, {m['编号'] for m in wire['记忆']})
        self.assertEqual(1, len(wire['绑定计划']))
        self.assertEqual('进行中', wire['绑定计划'][0]['状态'])
        self.assertEqual('PRIVATE ORIGINAL 1', wire['绑定计划'][0]['计划']['original_text'])
        self.assertEqual({'记忆', '绑定计划', '关联'}, set(wire))
        text = json.dumps(wire, ensure_ascii=False)
        for unwanted in ('notice', 'instruction_authority', 'source_emotion_refs', 'emotion://', 'plan://', '你应该'):
            self.assertNotIn(unwanted, text)
        # Recalling the same root again is independent of count/time/progress.
        self.assertEqual(result, self.expand([first, plan]))

    def test_one_hop_only_cycles_deduplicated_and_current_summary_not_truncated(self):
        first, second, third = self.save(), self.save(), self.save()
        self.link(first, second); self.link(second, third)
        store = self.stores['emotion']
        summary = '第二条的完整自写摘要。' * 100
        store.revise(**self.common, wake_id='synthetic-revision',
            expected_row_version=store.status(**self.common)['row_version'],
            memory_id=second.split('://')[1].split('@')[0], expected_memory_version=1,
            changes={'summary': summary}, reason='synthetic revision')
        result = self.expand([first])
        self.assertEqual(1, len(result['associated_recall']['items']))
        item = result['associated_recall']['items'][0]
        self.assertTrue(item['item_ref'].endswith('@2'))
        self.assertEqual(summary, item['text'])
        self.assertEqual(summary, project_dynamic(result)['记忆'][1]['摘要'])
        self.assertNotIn(short_ref(third), str(project_dynamic(result)))
        both = project_dynamic(self.expand([first, second]))
        self.assertEqual(3, len(both['记忆']))

    def test_reverse_custom_labels_and_shared_target_have_one_body(self):
        first, second, third = self.save(), self.save(), self.save()
        self.link(first, third, type='custom', label='一起吃过', reverse_label='想起')
        self.link(second, third)
        result = self.expand([first, second])
        self.assertEqual(1, len(result['associated_recall']['items']))
        reverse = project_dynamic(self.expand([third]))
        self.assertTrue(any('—想起→' in line for line in reverse['关联']))

    def test_modules_and_existing_author_disclosure_still_apply(self):
        first = self.save()
        hidden = self.save(context_policy='never_auto')
        ask = self.save(context_policy='ask_first')
        plan = self.save('plan')
        for target in (hidden, ask, plan):
            self.link(first, target)
        result = self.expand([first], enabled=lambda scope: scope != 'planning_memory')
        items = result['associated_recall']['items']
        self.assertEqual([ask], [x['item_ref'] for x in items])
        self.assertEqual('neutral_hint', items[0]['presentation'])
        self.assertNotIn('合成摘要', items[0]['text'])
        with self.stores['emotion']._connect() as db:
            neutral = {'force_recall': {'items': [{'item_ref': first, 'presentation': 'neutral_hint'}]}}
            self.assertEqual(neutral, append_associated_recall(db, **self.common, dynamic=neutral,
                             enabled=lambda _: True, query='ex'))

    def test_deleted_relation_and_foreign_or_archived_targets_stop_expanding(self):
        first, second = self.save(), self.save()
        receipt = self.link(first, second)
        MemoryRelationStore(self.path).detach(**self.common, edge_ref=receipt['edge_ref'])
        self.assertEqual([], self.expand([first])['associated_recall']['items'])
        self.link(first, second, request_key='f'*64)
        with self.stores['emotion']._connect() as db:
            db.execute("UPDATE emotion_memories SET owner_id='another-owner' WHERE memory_id=?", (second.split('://')[1].split('@')[0],))
        self.assertEqual([], self.expand([first])['associated_recall']['items'])

    def test_all_four_brains_and_completed_plan_status(self):
        first = self.save()
        targets = [self.save(module) for module in ('learning', 'plan', 'tool-card')]
        for target in targets:
            self.link(first, target)
        plan_id = targets[1].split('://')[1].split('@')[0]
        store = self.stores['plan']
        with store._connect() as db:
            store._append_event(db, **self.common, plan_id=plan_id, event_type='complete',
                reason='synthetic completion', evidence=[], wake_id='synthetic-state', wake_seq=1)
        result = self.expand([first])
        items = result['associated_recall']['items']
        self.assertEqual(set(targets), {x['item_ref'] for x in items})
        self.assertEqual('completed', next(x for x in items if x['item_ref'] == targets[1])['state'])
        self.assertEqual('完成', next(x for x in project_dynamic(result)['记忆'] if x['编号'] == short_ref(targets[1]))['状态'])

    def test_snapshot_without_new_layout_keeps_prior_bound_plan_wire_hash(self):
        plan = self.save('plan'); emotion = self.save(bound_plan_refs=[plan])
        result = self.expand([emotion]); result.pop('associated_recall')
        original = project_dynamic(result)
        self.assertIn('情感绑定的计划', original)
        self.assertIn('引用', original['情感绑定的计划'][0])
        first = ModuleOneOnboardingStore._context_message(stable={}, dynamic=result, compact=2)
        persisted = json.loads(json.dumps(result))
        self.assertEqual(_sha256(first), _sha256(ModuleOneOnboardingStore._context_message(
            stable={}, dynamic=persisted, compact=2)))

    def test_path_rendering_branches_cycles_and_duplicate_edges(self):
        def path(a, b):
            return {'from_ref': 'emotion://'+a, 'to_ref': 'emotion://'+b, 'type': 'same_event'}
        edges = [path('a','b'), path('b','c'), path('b','d'), path('d','a')]
        lines = relation_lines(edges + copy.deepcopy(edges))
        self.assertEqual(4, sum(line.count('—同一事件→') for line in lines))
        self.assertLessEqual(len(lines), 4)
