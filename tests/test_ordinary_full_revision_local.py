"""Local candidate contract: synthetic stores, no network or production data."""
from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_daily_revision_r4h34 as fixtures
from tests.test_planning_memory import content as planning_content
from runtime.emotional_memory import EmotionalMemoryStore, EmotionalMemoryError
from runtime.ordinary_access import authenticated_ordinary_operation
from mcp_server.daily_revision_service import parse_revision_target, ORDINARY_REVISION_FIELDS


class FullOrdinaryRevisionTests(unittest.TestCase):
    setUp = fixtures.OrdinaryRevisionTests.setUp
    rows = fixtures.OrdinaryRevisionTests.rows
    snapshot = fixtures.OrdinaryRevisionTests.snapshot
    revise = fixtures.OrdinaryRevisionTests.revise

    def test_original_changes_version_all_three_modules(self):
        new = '  A revised author body\nwith original spacing.  '
        for module, field, table, column in (
            ('emotional_memory', 'original_text', 'emotion_memory_versions', 'original_snapshot_json'),
            ('learning_memory', 'current_understanding', 'learning_versions', 'mutable_json'),
            ('planning_memory', 'original_text', 'planning_versions', 'content_json'),
        ):
            with self.subTest(module=module):
                old = self.rows(table)[0]
                result = self.revise(module, {field: new})
                self.assertTrue(result['revised'], result)
                self.assertEqual(old, self.rows(table)[0])
                versions = self.rows(table)
                self.assertEqual(self.original, json.loads(versions[0][column])[field])
                self.assertEqual(new, json.loads(versions[-1][column])[field])
                self.assertEqual(2, len(versions))
        self.assertEqual([], self.rows('learning_change_candidates'))
        self.assertEqual([], self.rows('planning_change_candidates'))

    def test_emotional_authored_fields_and_source_are_direct(self):
        change = {'summary': 'Updated summary', 'primary_emotion': 'joy',
                  'secondary_emotions': ['calm'], 'importance': 83, 'origin': 'inferred',
                  'confidence': 63, 'keywords': ['synthetic'], 'entities': ['author'],
                  'memory_type': 'shared_event', 'source_timestamp': '2026-09-11T01:02:03Z',
                  'recall_mode': 'never', 'allow_contexts': [], 'deny_contexts': ['public']}
        result = self.revise('emotional_memory', change)
        self.assertTrue(result['revised'], result)
        row = self.rows('emotion_memories')[0]
        for key in ('primary_emotion','importance','origin','confidence','source_timestamp','recall_mode'):
            self.assertEqual(change[key], row[key])

    def test_learning_author_body_source_and_tags_are_direct(self):
        result = self.revise('learning_memory', {
            'current_understanding': '  Synthetic updated knowledge.  ', 'source_basis': 'inferred',
            'steps': ['Observe', 'Compare'], 'application_contexts': ['Synthetic work'],
            'scene_tags': ['practice'], 'uncertainties': ['Needs observation'], 'confidence': 43,
            'domain': 'Design', 'importance': 71, 'recall_mode': 'never',
        })
        self.assertTrue(result['revised'], result)
        body = json.loads(self.rows('learning_items')[0]['current_json'])
        self.assertEqual('inferred', body['source_basis'])
        self.assertEqual('inferred', body['epistemic_status'])
        self.assertEqual([], self.rows('learning_verification_events'))
        self.assertEqual([], self.rows('learning_change_candidates'))

    def test_system_coordinates_rejected_before_any_write(self):
        before = self.snapshot()
        for module in self.refs:
            for key in ('owner_id','model_id','current_version','original_hash','created_at'):
                result = self.revise(module, {key: 'fake'})
                self.assertFalse(result['revised'], result)
        self.assertEqual(before, self.snapshot())

    def test_stale_author_body_is_never_retried(self):
        for module, field in (('emotional_memory','original_text'),('learning_memory','current_understanding'),
                              ('planning_memory','original_text')):
            self.assertTrue(self.revise(module, {field: 'New version'})['revised'])
            before = self.snapshot()
            self.assertFalse(self.revise(module, {field: 'Stale overwrite'})['revised'])
            self.assertEqual(before, self.snapshot())

    def test_emotion_current_and_historical_originals_are_separate(self):
        self.assertTrue(self.revise('emotional_memory', {'original_text': 'New original'})['revised'])
        item_id = parse_revision_target(self.refs['emotional_memory'])[1]
        result = self.emotional.store.recall_history(owner_id=fixtures.OWNER, model_id=fixtures.MODEL,
                                                    memory_id=item_id)['result']
        self.assertEqual('New original', result['memory']['original_text'])
        self.assertEqual(self.original, result['versions'][0]['original_snapshot']['original_text'])
        self.assertEqual('New original', result['versions'][1]['original_snapshot']['original_text'])
        self.assertNotIn('original_text', result['versions'][1]['mutable'])

    def test_old_sensitive_original_stays_withheld_after_current_policy_changes(self):
        result = self.revise('emotional_memory', {'original_text': 'Sensitive old body',
                              'sensitivity': 'restricted', 'context_policy': 'neutral_hint',
                              'explicit_request_override': 'never'})
        self.assertTrue(result['revised'], result)
        self.refs['emotional_memory'] = result['ref']
        result = self.revise('emotional_memory', {'original_text': 'Public new body',
                              'sensitivity': 'private', 'context_policy': 'normal',
                              'explicit_request_override': 'allow_after_confirmation'})
        self.assertTrue(result['revised'], result)
        item_id = parse_revision_target(result['ref'])[1]
        for confirmed in (False, True):
            history = self.emotional.store.recall_history(owner_id=fixtures.OWNER, model_id=fixtures.MODEL,
                memory_id=item_id, explicit_request=confirmed, include_sensitive_originals=confirmed,
                ai_confirmation=confirmed)['result']
            self.assertNotIn('Sensitive old body', json.dumps(history))
            self.assertEqual('Public new body', history['memory']['original_text'])

    def test_no_original_read_leaks_any_historical_body(self):
        self.assertTrue(self.revise('emotional_memory', {'original_text': 'Second body'})['revised'])
        item_id = parse_revision_target(self.refs['emotional_memory'])[1]
        result = self.emotional.store.recall_history(owner_id=fixtures.OWNER, model_id=fixtures.MODEL,
                  memory_id=item_id, include_originals=False)['result']
        for version in result['versions']:
            self.assertNotIn('original_text', version['original_snapshot'])
        self.assertNotIn('Second body', json.dumps(result))

    def test_legacy_snapshot_upgrade_is_additive_idempotent_and_keeps_old_hash(self):
        old = self.rows('emotion_memory_versions')[0]
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute('ALTER TABLE emotion_memory_versions DROP COLUMN original_snapshot_json')
            connection.execute('ALTER TABLE emotion_memory_versions DROP COLUMN original_snapshot_hash')
            connection.commit()
        EmotionalMemoryStore(self.database)
        upgraded = self.rows('emotion_memory_versions')[0]
        self.assertEqual(old['mutable_json'], upgraded['mutable_json'])
        self.assertEqual(old['mutable_hash'], upgraded['mutable_hash'])
        self.assertEqual(self.original, json.loads(upgraded['original_snapshot_json'])['original_text'])
        self.assertTrue(self.revise('emotional_memory', {'original_text': 'Current changed'})['revised'])
        before = self.rows('emotion_memory_versions')
        EmotionalMemoryStore(self.database)
        self.assertEqual(before, self.rows('emotion_memory_versions'))

    def test_snapshot_tampering_is_not_returned_as_history(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE emotion_memory_versions SET original_snapshot_json='{}'")
            connection.commit()
        item_id = parse_revision_target(self.refs['emotional_memory'])[1]
        with self.assertRaisesRegex(EmotionalMemoryError, 'original_snapshot_integrity_mismatch'):
            self.emotional.store.recall_history(owner_id=fixtures.OWNER, model_id=fixtures.MODEL,
                                               memory_id=item_id)

    def test_missing_original_history_cannot_be_repaired_by_overwriting_current(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute('DELETE FROM emotion_memory_versions')
            connection.commit()
        before = self.snapshot()
        result = self.revise('emotional_memory', {'original_text': 'Would erase missing source'})
        self.assertEqual(['original_snapshot_integrity_mismatch'], result['reason_codes'])
        self.assertEqual(before, self.snapshot())

    def test_invalid_source_timestamp_rejects_without_appending_version(self):
        before = self.snapshot()
        self.assertFalse(self.revise('emotional_memory', {'source_timestamp': 'not-a-time'})['revised'])
        self.assertEqual(before, self.snapshot())

    def test_authenticated_ordinary_facade_needs_no_injected_wake(self):
        self.bound.return_value = None
        with authenticated_ordinary_operation(owner_id=fixtures.OWNER, model_id=fixtures.MODEL,
                                             scope='emotional_memory') as access:
            self.host.ref, self.host.wake_id, self.host.wake_seq = access['write_context_ref'], access['wake_id'], access['wake_seq']
            self.host.mode, self.host.scopes = access['context_mode'], {'emotional_memory'}
            result = self.revise('emotional_memory', {'original_text': 'Authenticated revised original'})
            self.assertTrue(result['revised'], result)
            self.assertEqual([], self.host.open_calls)
            self.assertTrue(self.rows('emotion_memory_versions')[-1]['wake_id'].startswith('ordinaryop_'))
            self.assertFalse(self.revise('learning_memory')['revised'])
        self.assertFalse(self.revise('emotional_memory')['revised'])

    def test_authenticated_context_does_not_replace_explicit_mismatched_ref(self):
        self.bound.return_value = None
        with authenticated_ordinary_operation(owner_id=fixtures.OWNER, model_id=fixtures.MODEL,
                                             scope='emotional_memory'):
            before = self.snapshot()
            result = self.revise('emotional_memory', write_context_ref='foreign-ref')
            self.assertEqual(['write_context_binding_mismatch'], result['reason_codes'])
            self.assertEqual(before, self.snapshot())

    def test_planning_service_direct_creation_and_third_person_edit(self):
        content = planning_content('Synthetic second plan')
        content['ai_adoption_statement'] = 'The author adopts this plan.'
        result = self.planning.remember(write_context_ref=self.host.ref,
            expected_planning_version=self.planning.status()['row_version'], content=content,
            reason='Author creates plan', idempotency_key='synthetic-new-plan')
        self.assertEqual('stored', result['decision'], result)
        revised = self.planning.revise(write_context_ref=self.host.ref,
            expected_planning_version=self.planning.status()['row_version'], plan_id=result['plan_id'],
            expected_plan_version=1, intent='revise', changes={'ai_adoption_statement':'It remains the author choice.',
            'original_text':'  Updated third-person plan.  '}, reason='Update', idempotency_key='synthetic-revision')
        self.assertEqual('revised', revised['decision'], revised)
        self.assertEqual([], self.rows('planning_change_candidates'))

    def test_ordinary_plan_can_add_author_statement_without_changing_write_mode(self):
        result = self.revise('planning_memory', {'ai_adoption_statement': 'The author adopts this plan.'})
        self.assertTrue(result['revised'], result)
        content = json.loads(self.rows('planning_versions')[-1]['content_json'])
        self.assertEqual('ordinary_record', content['write_mode'])
        self.assertEqual('The author adopts this plan.', content['ai_adoption_statement'])
        self.assertNotIn('ai_adoption_statement', json.loads(self.rows('planning_versions')[0]['content_json']))

    def test_secret_and_invalid_values_leave_original_history_unchanged(self):
        before = self.snapshot()
        for module, changes in (
            ('emotional_memory', {'importance':101}), ('emotional_memory', {'primary_emotion':'made-up'}),
            ('learning_memory', {'confidence':True}), ('planning_memory', {'parent_ref':'bad'}),
            ('emotional_memory', {'original_text':'api_key=sk-' + 'x'*40}),
        ):
            self.assertFalse(self.revise(module, changes)['revised'])
        self.assertEqual(before, self.snapshot())

    def test_actor_wording_does_not_change_owner_identity(self):
        result = self.revise('emotional_memory', {'original_text':'The author remembers this event.'})
        self.assertTrue(result['revised'], result)
        row = self.rows('emotion_memories')[0]
        self.assertEqual((fixtures.OWNER,fixtures.MODEL), (row['owner_id'],row['model_id']))

    def test_derived_learning_fields_are_explicitly_excluded(self):
        self.assertNotIn('epistemic_status', ORDINARY_REVISION_FIELDS['learning_memory'])
        self.assertNotIn('provenance_badge', ORDINARY_REVISION_FIELDS['learning_memory'])
        self.assertIn('source_basis', ORDINARY_REVISION_FIELDS['learning_memory'])


if __name__ == '__main__':
    unittest.main()
