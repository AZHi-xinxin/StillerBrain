"""Small explicit-search alternatives; all cards and stores are synthetic."""
import json
import sqlite3
import unittest
from unittest.mock import patch

from runtime.tool_guidance import _scene_score
from tests import test_tool_guidance as fixtures


class ToolExplicitAliasRecallTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ToolGuidanceStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def create(self, tag, **changes):
        return self.fixture.remember(
            tool_name='Synthetic' + str(self.fixture.version()),
            operation_key='synthetic_photo', purpose='Arrange synthetic photographs.',
            use_when=[], scenario_examples=[], aliases=[], scenario_tags=[tag],
            keywords=[], **changes,
        )['card']

    def recall(self, query, **options):
        arguments = dict(owner_id=self.fixture.owner, model_id=self.fixture.model,
                         query=query, view='directory', catalog=self.fixture.live_catalog)
        arguments.update(options)
        return self.fixture.store.recall(**arguments)

    def rows(self):
        connection = sqlite3.connect(self.fixture.database)
        try:
            return list(connection.iterdump())
        finally:
            connection.close()

    def test_directory_weak_alias_is_explained_and_does_not_displace_normal_hit(self):
        alias = self.create('合影')
        normal = self.create('合照')
        self.assertLess(_scene_score('合照', alias['content']), .5)
        before = self.rows()
        response = self.recall('合照')
        self.assertEqual([normal['card_id'], alias['card_id']], [item['card_id'] for item in response['results']])
        first, second = response['results']
        self.assertNotIn('retrieval_match', first)
        self.assertEqual('lexical_alias_candidate', second['retrieval_match'])
        self.assertTrue(second['candidate_only'])
        self.assertLess(second['candidate_score'], .5)
        self.assertNotIn('confidence', second['retrieval_evidence'])
        self.assertNotIn('content', second)
        self.assertEqual('按常见词句找到的相关候选，保留原记录的时间与语境。',
                         second['retrieval_evidence']['interpretation'])
        self.assertEqual([normal['card_id']], [item['card_id'] for item in self.recall('合照', limit=1)['results']])
        self.assertEqual(before, self.rows())

    def test_expression_candidate_is_read_only_and_unrelated_search_has_none(self):
        card = self.create('回家了')
        self.assertLess(_scene_score('已经回到家', card['content']), .5)
        before = self.rows()
        results = self.recall('已经回到家')['results']
        self.assertEqual([card['card_id']], [item['card_id'] for item in results])
        self.assertTrue(results[0]['candidate_only'])
        self.assertEqual([], self.recall('量子概率云')['results'])
        self.assertEqual(before, self.rows())

    def test_exact_reference_and_automatic_recall_never_use_alias_helper(self):
        card = self.create('合影')
        before = self.rows()
        with (patch('runtime.tool_guidance.explicit_alias_match', side_effect=AssertionError('alias_forbidden')),
              patch('runtime.tool_guidance.prepare_explicit_alias_query', side_effect=AssertionError('alias_forbidden'))):
            precise = self.recall('合照', view='card', card_id=card['card_id'])
            self.assertEqual(card['card_id'], precise['results'][0]['card_id'])
            self.assertNotIn('retrieval_match', precise['results'][0])
            self.assertEqual(before, self.rows())
            automatic = self.fixture.store.build_recall_envelopes(
                owner_id=self.fixture.owner, model_id=self.fixture.model,
                query='合照', catalog=self.fixture.live_catalog,
            )
            self.assertNotIn('lexical_alias_candidate', json.dumps(automatic))
        # Automatic recall has its pre-existing audit event. It must not change
        # saved cards, versions, experiences or the module row version.
        business = lambda rows: [row for row in rows if 'tool_audit_events' not in row]
        self.assertEqual(business(before), business(self.rows()))

    def test_owner_scope_and_manual_only_rules_are_not_overridden(self):
        card = self.create('合影', auto_recall_mode='never_auto',
                           salience_reason='Keep this synthetic advice for manual lookup.')
        self.assertEqual([], self.recall('合照', owner_id='synthetic-other')['results'])
        self.assertEqual([], self.recall('合照', view='suggestions')['results'])
        # Directory is an explicit manual lookup and was already allowed to
        # include never_auto cards; its result is advice, not execution authority.
        self.assertEqual(card['card_id'], self.recall('合照')['results'][0]['card_id'])


if __name__ == '__main__':
    unittest.main()
