"""Unified semantic search regression; synthetic stores and no model/network."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcp_server.unified_recall_service import UnifiedRecallAccessService
from runtime import semantic_retrieval as dense
from tests import test_explicit_expression_recall as fixtures


class UnifiedSemanticRecallTests(unittest.TestCase):
    setUp = fixtures.ThreeBrainExplicitAliasTests.setUp
    tearDown = fixtures.ThreeBrainExplicitAliasTests.tearDown
    save = fixtures.ThreeBrainExplicitAliasTests.save
    snapshot_authored = fixtures.ThreeBrainExplicitAliasTests.snapshot_authored

    def service(self):
        return UnifiedRecallAccessService(
            services={'emotional_memory': SimpleNamespace(
                store=self.stores['emotion'], **self.common)},
            **self.common, onboarding=None, simple=True)

    def test_unified_entry_returns_semantic_only_summary_candidate(self):
        target = self.save('emotion', cue='合成断电记录')
        before = self.snapshot_authored()
        with patch('runtime.emotional_memory.dense.search',
                   return_value=dense.Result('ready', {target: .81})) as search:
            found = self.service().recall(query='玄黓魑魅', module='emotional_memory')
        self.assertEqual(1, found['matched_count'])
        item = found['items'][0]
        self.assertIn(target, item['ref'])
        self.assertTrue(item['candidate_only'])
        self.assertEqual('local_semantic_candidate', item['retrieval_match'])
        self.assertEqual(.81, item['cosine_similarity'])
        self.assertEqual('none', item['permission_authority'])
        self.assertTrue(search.call_args.kwargs['explicit_wait'])
        self.assertEqual({'emotional_memory': 'ready'}, found['semantic_status'])
        self.assertTrue(found['retrieval_complete'])
        self.assertEqual(before, self.snapshot_authored())

    def test_empty_fallback_is_not_claimed_complete_and_does_not_change_records(self):
        self.save('emotion', cue='合成断电记录')
        before = self.snapshot_authored()
        for status in ('indexing_lexical_fallback', 'busy_lexical_fallback', 'unavailable_lexical_fallback'):
            with self.subTest(status=status), patch('runtime.emotional_memory.dense.search',
                    return_value=dense.Result(status, {})):
                found = self.service().recall(query='玄黓魑魅', module='emotional_memory')
                self.assertEqual([], found['items'])
                self.assertEqual({'emotional_memory': status}, found['semantic_status'])
                self.assertFalse(found['retrieval_complete'])
                self.assertFalse(found['exhaustive'])
        self.assertEqual(before, self.snapshot_authored())

    def test_directory_does_not_embed_and_disabled_keeps_old_search(self):
        self.save('emotion', cue='合成断电记录')
        with patch('runtime.emotional_memory.dense.search',
                   return_value=dense.Result('disabled', {})) as search:
            directory = self.service().recall(module='emotional_memory')
            search.assert_not_called()
            self.assertEqual({}, directory['semantic_status'])
            found = self.service().recall(query='合成断电记录', module='emotional_memory')
            self.assertEqual(1, found['matched_count'])
            self.assertTrue(found['retrieval_complete'])
            self.assertNotIn('candidate_only', found['items'][0])

    def test_encoder_receives_only_current_eligible_summaries(self):
        target = self.save('emotion', cue='普通合成摘要')
        self.save('emotion', cue='不浮现合成摘要', recall_mode='never')
        self.save('emotion', cue='敏感合成摘要', sensitivity='restricted')
        with patch('runtime.emotional_memory.dense.search',
                   return_value=dense.Result('ready', {target: .81})) as search:
            self.service().recall(query='玄黓魑魅', module='emotional_memory')
        self.assertEqual([target], [doc.ref for doc in search.call_args.kwargs['documents']])
        self.assertEqual(['普通合成摘要\n'], [doc.text for doc in search.call_args.kwargs['documents']])

    def test_semantic_completion_invalidates_old_page_cursor(self):
        for _ in range(3):
            self.save('emotion', cue='合成断电记录')
        service = self.service()
        with patch('runtime.emotional_memory.dense.search',
                   return_value=dense.Result('indexing_lexical_fallback', {})):
            first = service.recall(query='合成断电记录', module='emotional_memory', limit=1)
            self.assertTrue(first['next_cursor'])
            second = service.recall(query='合成断电记录', module='emotional_memory',
                                    limit=1, cursor=first['next_cursor'])
            self.assertEqual(1, second['offset'])
        with patch('runtime.emotional_memory.dense.search', return_value=dense.Result('ready', {})):
            changed = service.recall(query='合成断电记录', module='emotional_memory',
                                     limit=1, cursor=first['next_cursor'])
        self.assertEqual('cursor_stale', changed['reason_code'])


if __name__ == '__main__':
    unittest.main()
