"""Synthetic explicit-search aliases; no new author fields or automatic cues."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime import emotional_memory as em
from runtime import learning_memory as lm
from runtime import planning_memory as pm
from runtime.lexical_retrieval import (
    LEXICAL_ALIAS_FAMILIES, explicit_alias_match, prepare_explicit_alias_query,
)


class ExplicitExpressionHelperTests(unittest.TestCase):
    def test_existing_eight_families_work_both_directions(self):
        self.assertEqual(8, len(LEXICAL_ALIAS_FAMILIES))
        for first, second in LEXICAL_ALIAS_FAMILIES:
            for query, field in ((first, second), (second, first)):
                with self.subTest(query=query, field=field):
                    result = explicit_alias_match(query, [field])
                    self.assertEqual('lexical_alias_candidate', result['match_kind'])
                    self.assertTrue(result['candidate_only'])
                    self.assertEqual(0.28, result['score'])

    def test_complete_home_expressions_match_without_keyword_authoring(self):
        for query in ('刚到家', '刚进家门', '我已经回到家', '回家了'):
            with self.subTest(query=query):
                result = explicit_alias_match(query, ['合成摘要：到家了，整理随身物品。'])
                self.assertIsNotNone(result)
                self.assertEqual('按常见词句找到的相关候选，保留原记录的时间与语境。', result['interpretation'])
                self.assertNotIn('整理随身物品', json.dumps(result, ensure_ascii=False))

    def test_few_common_preparing_and_leaving_expressions(self):
        self.assertIsNotNone(explicit_alias_match('准备睡了', ['准备上床睡觉']))
        self.assertIsNotNone(explicit_alias_match('已经出门', ['刚出门']))
        self.assertIsNone(explicit_alias_match('睡醒了', ['起床了']))

    def test_clear_negation_future_past_and_conditional_do_not_gain_home_hint(self):
        for query in ('明天才回家', '我明天才到家了', '我还没到家', '我没有回家了',
                      '昨天刚到家', '下周回家了再说', '如果刚到家', '可能到家了',
                      '计划回家了再开灯', '回家了以后再联系'):
            with self.subTest(query=query):
                self.assertIsNone(explicit_alias_match(query, ['回家了', '刚进家门']))

    def test_candidate_side_context_is_also_checked(self):
        for saved in ('明天才回家', '明天到家了再说', '没有回家了', '昨天刚进家门',
                      '如果回家了', '等到回家了以后'):
            with self.subTest(saved=saved):
                self.assertIsNone(explicit_alias_match('刚到家', [saved]))

    def test_clause_local_guard_does_not_rewrite_or_truncate_text(self):
        fields = ['明天要出门，刚进家门，今天很累。']
        before = list(fields)
        result = explicit_alias_match('回家了', fields)
        self.assertIsNotNone(result)
        self.assertEqual(before, fields)
        self.assertIsNone(explicit_alias_match('到家', ['回家']))
        self.assertIsNone(explicit_alias_match('陌生词', fields))

    def test_prepared_query_reuse_is_identical_and_has_no_saved_text(self):
        query = '想找合照和刚到家的记录'
        fields = ['合影', '回家了']
        prepared = prepare_explicit_alias_query(query)
        self.assertEqual(explicit_alias_match(query, fields), explicit_alias_match(prepared, fields))
        self.assertEqual(2, explicit_alias_match(prepared, fields)['matched_family_count'])
        self.assertEqual(0.32, explicit_alias_match(prepared, fields)['score'])


class ThreeBrainExplicitAliasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='explicit-alias-synthetic-')
        self.root = Path(self.temp.name)
        self.common = {'owner_id': 'synthetic-alias-owner', 'model_id': 'synthetic-alias-model'}
        self.stores = {
            'emotion': em.EmotionalMemoryStore(self.root / 'emotion.sqlite'),
            'learning': lm.LearningMemoryStore(self.root / 'learning.sqlite',
                                              idea_database=self.root / 'ideas.sqlite'),
            'planning': pm.PlanningMemoryStore(self.root / 'planning.sqlite'),
        }
        self.sequence = 0
        for store in self.stores.values():
            store.ensure_state(**self.common)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, brain, cue='遛弯', *, keywords=None, **overrides):
        self.sequence += 1
        store = self.stores[brain]
        base = {**self.common, 'wake_id': 'synthetic-alias-wake-' + str(self.sequence),
                'expected_row_version': store.status(**self.common)['row_version']}
        if brain == 'emotion':
            return store.remember(**base, memory_type='shared_event',
                original_text='Synthetic private original, retained verbatim ' + str(self.sequence),
                summary=cue, primary_emotion='calm', keywords=keywords or [], **overrides)['memory']['memory_id']
        if brain == 'learning':
            return store.remember(**base, wake_seq=self.sequence, kind='fact', title='静谧片刻',
                summary=cue, current_understanding='Synthetic authored knowledge ' + str(self.sequence),
                correctness_assessment='Synthetic author note for fixture creation.',
                reason='Synthetic fixture only.', source_basis='reported', claim_review={'status': 'ordinary'}, confidence=50,
                keywords=keywords or [], **overrides)['learning_id']
        return store.remember_ordinary(**base, wake_seq=self.sequence, write_context_ref='synthetic-fixture-context',
            idempotency_key='synthetic-alias-plan-' + str(self.sequence),
            content='Synthetic authored plan ' + str(self.sequence), title='静谧片刻', summary=cue,
            keywords=keywords or [], **overrides)['plan_id']

    def search(self, brain, query='散步', **options):
        output = self.stores[brain].recall(**self.common, query=query, **options)
        return output['plans' if brain == 'planning' else 'results']

    @staticmethod
    def result_id(brain, item):
        return item[{'emotion': 'memory_id', 'learning': 'learning_id', 'planning': 'plan_id'}[brain]]

    def snapshot_authored(self):
        result = {}
        for brain, store in self.stores.items():
            table = {'emotion': 'emotion_memories', 'learning': 'learning_items', 'planning': 'planning_items'}[brain]
            with store._connect() as connection:
                result[brain] = [tuple(row) for row in connection.execute('SELECT * FROM ' + table)]
                # The emotion runtime has its own version table naming. Include
                # all extant version tables without reading outside this fixture.
                names = [row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%versions'")]
                result[brain + '_versions'] = {
                    name: [tuple(row) for row in connection.execute('SELECT * FROM "' + name + '"')]
                    for name in names
                }
        return result

    def test_all_three_manual_paths_find_summary_alias_without_new_keywords(self):
        ids = {brain: self.save(brain) for brain in self.stores}
        before = self.snapshot_authored()
        for brain in self.stores:
            with self.subTest(brain=brain):
                items = self.search(brain)
                self.assertEqual([ids[brain]], [self.result_id(brain, item) for item in items])
                self.assertTrue(items[0]['candidate_only'])
                self.assertEqual('lexical_alias_candidate', items[0]['retrieval_match'])
                self.assertEqual(0.28, items[0]['candidate_score'])
        self.assertEqual(before, self.snapshot_authored())

    def test_complete_home_expression_recovers_a_long_explicit_search(self):
        query = '刚进家门，找找关于灯光和音乐的过往记忆'
        for brain in self.stores:
            with self.subTest(brain=brain):
                memory_id = self.save(brain, '回家了')
                items = self.search(brain, query)
                self.assertIn(memory_id, [self.result_id(brain, item) for item in items])

    def test_future_literal_related_results_never_get_added_arrived_hint(self):
        for brain in self.stores:
            self.save(brain, '回家了')
            for query in ('明天才回家', '我还没到家', '如果回家了'):
                with self.subTest(brain=brain, query=query):
                    items = self.search(brain, query)
                    self.assertTrue(all(not item.get('candidate_only') for item in items))

    def test_normal_hits_keep_precedence_limit_and_exact_paths_remain_exact(self):
        for brain in self.stores:
            with self.subTest(brain=brain):
                fallback = self.save(brain)
                normal = self.save(brain, '散步', keywords=['散步'])
                items = self.search(brain, limit=1)
                self.assertEqual(normal, self.result_id(brain, items[0]))
                self.assertNotIn('candidate_only', items[0])
                items = self.search(brain, limit=2)
                self.assertEqual([normal, fallback], [self.result_id(brain, item) for item in items])
                if brain == 'emotion':
                    self.assertTrue(items[0]['exact_match'])
                    self.assertNotIn('original_text', items[1])
                elif brain == 'learning':
                    result = self.stores[brain].recall(**self.common, target_ref=f'learning://{fallback}@1')
                    self.assertNotIn('candidate_only', result['results'][0])
                    self.assertEqual('exact_ref', result['retrieval_mode'])
                else:
                    result = self.stores[brain].recall(**self.common, plan_ref=f'plan://{fallback}@1')
                    self.assertNotIn('candidate_only', result['plans'][0])

    def test_owner_model_and_quarantine_filters_still_apply(self):
        for brain, store in self.stores.items():
            memory_id = self.save(brain)
            for other in ({**self.common, 'owner_id': 'synthetic-foreign-owner'},
                          {**self.common, 'model_id': 'synthetic-foreign-model'}):
                result = store.recall(**other, query='散步')
                self.assertEqual([], result['plans' if brain == 'planning' else 'results'])
            table, key, lifecycle = {
                'emotion': ('emotion_memories', 'memory_id', 'lifecycle'),
                'learning': ('learning_items', 'learning_id', 'lifecycle'),
                'planning': ('planning_items', 'plan_id', 'recall_lifecycle'),
            }[brain]
            with store._connect() as connection:
                connection.execute(f'UPDATE {table} SET {lifecycle}=? WHERE {key}=?', ('quarantined', memory_id))
            self.assertEqual([], self.search(brain))

    def test_emotion_private_disclosure_gates_stay_summary_only_or_excluded(self):
        ordinary = self.save('emotion')
        for fields in ({'sensitivity': 'intimate'}, {'recall_mode': 'never'},
                       {'default_decision': 'ask_first'}, {'deny_contexts': ['散步']},
                       {'allow_contexts': ['无关场景']}):
            self.save('emotion', **fields)
        results = self.search('emotion', include_originals=True, explicit_request=True,
                              include_sensitive_originals=True, ai_confirmation=True)
        self.assertEqual([ordinary], [item['memory_id'] for item in results])
        self.assertNotIn('Synthetic private original', json.dumps(results))
        self.assertTrue(results[0]['original_withheld'])

    def test_automatic_paths_do_not_call_alias_expansion(self):
        for brain in self.stores:
            self.save(brain)
        with patch.object(em, 'prepare_explicit_alias_query', side_effect=AssertionError('automatic alias')):
            self.stores['emotion'].build_injection(**self.common, query='散步')
        with patch.object(lm, 'prepare_explicit_alias_query', side_effect=AssertionError('automatic alias')):
            self.stores['learning'].build_envelopes(**self.common, query='散步')
        with patch.object(pm, 'prepare_explicit_alias_query', side_effect=AssertionError('automatic alias')):
            self.stores['planning'].build_injection(**self.common, query='散步')


if __name__ == '__main__':
    unittest.main()
