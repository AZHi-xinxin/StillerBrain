from __future__ import annotations

import json
import random
import unittest
from unittest.mock import patch

from runtime import emotional_memory as em
from runtime.lexical_retrieval import LEXICAL_ALIAS_FAMILIES, LexicalQuery
from tests import test_emotional_memory as fixtures


def old_fuzzy(query: str, cue: str) -> float:
    """Frozen pre-preview.2 algorithm, kept only as a synthetic comparison oracle."""
    a, b = em._normalized(query), em._normalized(cue)
    if not 4 <= len(b) <= 32 or not a:
        return 0.0
    if b in a:
        return 1.0
    span = em._minimum_ordered_span(a, b)
    if span is not None:
        density = len(b) / span
        if density >= 0.60 and span <= len(b) + 6:
            return min(0.94, 0.66 + 0.28 * density)
    if 5 <= len(b) <= 12:
        for index in range(1, len(b) - 1):
            variant = b[:index] + b[index + 1:]
            span = em._minimum_ordered_span(a, variant)
            if span is not None:
                density = len(variant) / span
                if density >= 0.60 and span <= len(b) + 6:
                    return min(0.86, 0.60 + 0.28 * density)
    return 0.0


def old_score(row: dict, query: str, emotions: set[str]) -> tuple[float, bool]:
    keywords, entities = json.loads(row['keywords_json']), json.loads(row['entities_json'])
    hits = [k for k in keywords if em._hint_occurs(query, k)]
    exact = bool(hits or [e for e in entities if em._hint_occurs(query, e)])
    normalized = em._normalized(query)
    if len(normalized) >= 3 and normalized in em._normalized(row['summary']):
        return 0.9, exact
    secondary = json.loads(row['secondary_emotions_json'])
    searchable = ' '.join([row['summary'], *keywords, *entities, row['primary_emotion'], *secondary])
    semantic = em._semantic_similarity(query, searchable)
    fuzzy = max((old_fuzzy(query, cue) for cue in keywords), default=0.0)
    if not hits and fuzzy:
        semantic = max(semantic, 0.52 * fuzzy)
    emotion = 1.0 if emotions & {row['primary_emotion'], *secondary} else 0.0
    boost = max((em._keyword_specificity_boost(k) for k in hits), default=0.0)
    return min(0.99, 0.76 * semantic + 0.14 * emotion + 0.10 * (row['importance'] / 100) + boost), exact


class LexicalEquivalenceTests(unittest.TestCase):
    def test_prepared_formula_matches_frozen_score_and_exact_flag(self):
        rng = random.Random(3811)
        alphabet = '甲乙丙丁一二三四风雨秋ABCxyz１１２éßİǰ\u0307\u030c， !'
        cases = [('', '甲'), ('ＡＢ', 'ab'), ('人死后会变成星星', '人死后变星'),
                 ('人死变成星星', '人死后变星'), ('甲甲乙', '甲甲乙丙丁')]
        cases += [(''.join(rng.choices(alphabet, k=rng.randint(1, 70))),
                   ''.join(rng.choices(alphabet, k=rng.randint(1, 35)))) for _ in range(400)]
        for query, cue in cases:
            row = {'summary': cue + '合成记录', 'keywords_json': json.dumps([cue, 'ordinary cue']),
                   'entities_json': '["synthetic-name"]', 'primary_emotion': 'calm',
                   'secondary_emotions_json': '["joy"]', 'importance': rng.randrange(101)}
            emotions = em._query_emotions(query)
            with self.subTest(query_length=len(query), cue_length=len(cue)):
                expected, exact = old_score(row, query, emotions)
                actual, actual_exact = em.EmotionalMemoryStore._row_score(row, query, emotions)
                self.assertEqual(expected, actual)
                self.assertEqual(round(expected, 4), round(actual, 4))
                self.assertEqual(exact, actual_exact)
                self.assertEqual(old_fuzzy(query, cue), em._fuzzy_cue_similarity(query, cue))

    def test_impossible_cues_skip_ordered_span_without_approximation(self):
        with patch.object(em, '_minimum_ordered_span', side_effect=AssertionError('unneeded scan')):
            self.assertEqual(0, em._fuzzy_cue_similarity('a' * 4000, '甲乙丙丁'))
            self.assertEqual(0, em._fuzzy_cue_similarity('a' * 4000, 'aa甲乙a'))
            self.assertEqual(0, em._fuzzy_cue_similarity('a' * 4000, '甲aaaa'))

    def test_large_synthetic_pool_reuses_query_normalization(self):
        query = '甲乙丙丁' * 250
        row = {'summary': 'synthetic ordinary record', 'keywords_json': json.dumps(['cueword' + str(i) for i in range(24)]),
               'entities_json': '[]', 'primary_emotion': 'calm', 'secondary_emotions_json': '[]', 'importance': 50}
        original = em._normalized
        counts = {'old': 0, 'new': 0}
        phase = 'old'
        def counted(text):
            if text == query:
                counts[phase] += 1
            return original(text)
        with patch.object(em, '_normalized', side_effect=counted):
            previous = [old_score(row, query, set()) for _ in range(128)]
            phase = 'new'
            prepared = em._prepare_lexical_query(query)
            current = [em.EmotionalMemoryStore._row_score_prepared(row, prepared, set()) for _ in range(128)]
        self.assertEqual(previous, current)
        self.assertEqual(3456, counts['old'])
        self.assertEqual(2, counts['new'])


class ExplicitAliasRecallTests(unittest.TestCase):
    # Reuse only the synthetic fixture helpers, not the base class's tests.
    setUp = fixtures.EmotionalMemoryTests.setUp
    tearDown = fixtures.EmotionalMemoryTests.tearDown
    version = fixtures.EmotionalMemoryTests.version
    remember = fixtures.EmotionalMemoryTests.remember
    _seed_active_self_model = fixtures.EmotionalMemoryTests._seed_active_self_model

    def add(self, keyword='合影', **overrides):
        fields = dict(summary='我整理过一段普通的测试记录，保留了过程和结果。', keywords=[keyword],
                      entities=['合成人物'], primary_emotion='calm',
                      original_text='原文专用合成细节，不应通过候选摘要返回。' + keyword)
        fields.update(overrides)
        return self.remember('合成检索事件', **fields)['memory']

    def search(self, query='合照', **kwargs):
        return self.store.recall(owner_id=self.owner, model_id=self.model, query=query, **kwargs)

    def test_eight_documented_alias_families_add_summary_candidates(self):
        recovered = 0
        for first, second in LEXICAL_ALIAS_FAMILIES:
            memory = self.add(second)
            with self.store._connect() as connection:
                before = self.store._scored_rows(connection, owner_id=self.owner, model_id=self.model,
                                                 query=first, include_archived=False)
            self.assertNotIn(memory['memory_id'], [row['memory_id'] for row, _, _, _ in before])
            result = next(item for item in self.search(first)['results'] if item['memory_id'] == memory['memory_id'])
            self.assertTrue(result['candidate_only'])
            self.assertFalse(result['exact_match'])
            self.assertLess(result['score'], 0.2)
            self.assertGreaterEqual(result['candidate_score'], 0.28)
            self.assertNotIn('original_text', result)
            self.assertNotIn('original_hash', result)
            self.assertTrue(result['original_withheld'])
            recovered += 1
        self.assertEqual(8, recovered)

    def test_new_candidate_is_summary_even_with_all_original_flags(self):
        memory = self.add()
        result = self.search(explicit_request=True, include_sensitive_originals=True, ai_confirmation=True)
        self.assertNotIn(memory['original_text'], json.dumps(result, ensure_ascii=False))
        self.assertIn('lexical_alias_candidates_summary_only', result['reason_codes'])
        exact = self.search('合影')['results'][0]
        self.assertTrue(exact['exact_match'])
        self.assertNotIn('candidate_only', exact)
        self.assertEqual(memory['original_text'], exact['original_text'])

    def test_raw_referent_bindings_stay_withheld_in_alias_candidate(self):
        self.add(original_text='合成人物说她记得那次活动。', referent_bindings=[{
            'field_path': '/original_text', 'surface_form': '她', 'occurrence_index': 0,
            'entity_ref': 'person:synthetic', 'resolution_status': 'resolved', 'confidence': 100,
        }])
        item = self.search()['results'][0]
        self.assertEqual([], item['referent_bindings'])
        self.assertTrue(item['referent_bindings_withheld'])

    def test_alias_does_not_return_a_full_original_mislabeled_as_summary(self):
        self.add(original_text='这是一段测试。', summary='说明：这是一段测试。')
        self.assertEqual([], self.search()['results'])

    def test_alias_candidate_does_not_create_association_recall(self):
        source = self.add()
        target = self.add('不相干测试')
        self.store.revise(owner_id=self.owner, model_id=self.model, wake_id='synthetic-edge',
            expected_row_version=self.version(), memory_id=source['memory_id'], expected_memory_version=1,
            changes={'importance': 51}, reason='确认这条合成测试关联。',
            associations=[{'target_memory_id': target['memory_id'], 'edge_type': 'continuation', 'weight': 100}])
        self.assertEqual([source['memory_id']], [item['memory_id'] for item in self.search()['results']])
        self.assertEqual({}, self.store.build_injection(owner_id=self.owner, model_id=self.model, query='合照')['injection'])

    def test_alias_does_not_change_automatic_injection_or_seed_graph(self):
        memory = self.add()
        for query in ('合照', '想找一张合照'):
            baseline = self.store.build_injection(owner_id=self.owner, model_id=self.model, query=query)
            with patch.object(em, 'alias_query_families', side_effect=AssertionError('automatic expansion')):
                again = self.store.build_injection(owner_id=self.owner, model_id=self.model, query=query)
            self.assertEqual(baseline['injection'], again['injection'])
        self.assertEqual({}, self.store.build_injection(owner_id=self.owner, model_id=self.model, query='合照')['injection'])
        self.assertEqual(memory['memory_id'], self.search()['results'][0]['memory_id'])

    def test_old_results_keep_precedence_and_limit(self):
        fallback = self.add()
        old = self.add('合照', summary='我记得一次合照的经过。')
        result = self.search(limit=1)['results']
        self.assertEqual([old['memory_id']], [item['memory_id'] for item in result])
        self.assertNotIn('candidate_only', result[0])
        self.assertEqual(fallback['memory_id'], self.search(limit=2)['results'][1]['memory_id'])

    def test_owner_model_and_quarantine_boundaries(self):
        memory = self.add()
        for owner, model in (('owner:other', self.model), (self.owner, 'model:other')):
            self.assertEqual([], self.store.recall(owner_id=owner, model_id=model, query='合照')['results'])
        with self.store._connect() as connection:
            connection.execute("UPDATE emotion_memories SET lifecycle='quarantined' WHERE memory_id=?", (memory['memory_id'],))
        self.assertEqual([], self.search(include_archived=True)['results'])

    def test_archived_is_only_included_when_requested(self):
        memory = self.add()
        with self.store._connect() as connection:
            connection.execute("UPDATE emotion_memories SET lifecycle='archived' WHERE memory_id=?", (memory['memory_id'],))
        self.assertEqual([], self.search()['results'])
        self.assertEqual(memory['memory_id'], self.search(include_archived=True)['results'][0]['memory_id'])

    def test_ambiguous_entity_and_unrelated_words_do_not_become_aliases(self):
        self.add('其他测试', entities=['合影'])
        self.assertEqual([], self.search()['results'])
        self.add()
        for query in ('照料', '合同', '散热', '登录名之外的橙子种植'):
            self.assertEqual([], self.search(query)['results'])
        login = self.add('登录')
        self.assertNotIn(login['memory_id'], [item['memory_id'] for item in self.search('登陆火星')['results']])

    def test_sensitive_and_context_gates_are_not_expanded(self):
        policies = ({'sensitivity': 'intimate'}, {'sensitivity': 'restricted'},
                    {'context_policy': 'ask_first'}, {'context_policy': 'neutral_hint'},
                    {'context_policy': 'never_auto'}, {'recall_mode': 'never'},
                    {'recall_mode': 'summary_only'}, {'default_decision': 'defer'}, {'default_decision': 'ask_first'},
                    {'deny_contexts': ['合照']}, {'allow_contexts': ['特定复盘']})
        for policy in policies:
            memory = self.add(**policy)
            results = self.search(explicit_request=True, include_sensitive_originals=True, ai_confirmation=True)['results']
            self.assertNotIn(memory['memory_id'], [item['memory_id'] for item in results])

    def test_query_features_prepared_once_in_database_search(self):
        for _ in range(3):
            self.add()
        original = LexicalQuery.prepare
        with patch.object(LexicalQuery, 'prepare', wraps=original) as prepared:
            self.search()
        self.assertEqual(1, prepared.call_count)

    def test_fallback_audit_never_logs_query_text(self):
        self.add()
        query = '合照' + ' synthetic-query-marker '
        self.search(query)
        with self.store._connect() as connection:
            rows = connection.execute("SELECT * FROM emotion_audit_events").fetchall()
        self.assertNotIn(query, str([tuple(row) for row in rows]))


if __name__ == '__main__':
    unittest.main()
