"""Model-visible contract; all text and identities here are synthetic."""
import copy
import json
import unittest

from runtime.recall_projection import project_dynamic, short_ref, continuity_notice, estimate_dynamic
from runtime.onboarding import _estimate_tokens, _sha256
from tests import test_context_layout as fixture


class ProjectionTests(unittest.TestCase):
    def test_six_summaries_keep_content_and_drop_diagnostic_boilerplate(self):
        memories = [{'memory_id': 'synthetic-' + str(i), 'score': .67, 'exact_match': True,
                     'keyword_match': True, 'association_depth': 0, 'presentation': 'original',
                     'primary_emotion': 'affection', 'origin': 'firsthand', 'confidence': None,
                     'gate_decision': 'background_reference', 'text': 'PRIVATE ORIGINAL',
                     'auto_summary': '我记得这件事。' + str(i)} for i in range(6)]
        data = {'emotional_memory': {'frame': {'rules': ['HOST BOILERPLATE']},
                                    'pins': [], 'ephemeral': [], 'memories': memories},
                'session_continuity': {'source_transition': 'same', 'model_transition': 'same',
                                       'recent_memories': []}}
        before = copy.deepcopy(data)
        result = project_dynamic(data)
        self.assertEqual({'记忆'}, set(result))
        self.assertEqual(6, len(result['记忆']))
        for i, row in enumerate(result['记忆']):
            self.assertEqual({'编号', '摘要'}, set(row))
            self.assertEqual('我记得这件事。' + str(i), row['摘要'])
            self.assertRegex(row['编号'], r'^#E[0-9a-f]{12}$')
        text = json.dumps(result)
        for forbidden in ('PRIVATE ORIGINAL', 'score', 'HOST BOILERPLATE', 'ephemeral', 'memory_id'):
            self.assertNotIn(forbidden, text)
        self.assertEqual(data, before)
        self.assertLess(len(text), len(json.dumps(data)))
        self.assertEqual(_estimate_tokens(result), estimate_dynamic(data))

    def test_ask_first_never_releases_summary_or_original(self):
        value = {'emotional_memory': {'memories': [{'memory_id': 'sensitive',
                 'presentation': 'neutral_hint', 'gate_decision': 'ask_first',
                 'text': '有件事，展开前先确认。', 'auto_summary': 'PRIVATE SUMMARY',
                 'original_text': 'PRIVATE ORIGINAL'}]}}
        result = project_dynamic(value)
        self.assertNotIn('PRIVATE', str(result))
        self.assertEqual('有件事，展开前先确认。', result['记忆'][0]['摘要'])
        self.assertNotIn('来源', result['记忆'][0])

    def test_every_provenance_stays_in_diagnostics_and_explicit_receipts(self):
        for origin in ('firsthand', 'observed', 'reported', 'inferred', 'unmarked'):
            data = {'force_recall': {'items': [{'item_ref': 'emotion://x@1',
                'summary': '原作者摘要', 'origin': origin, 'origin_label': '来源标签',
                'candidate_only': True, 'confidence': 20, 'excerpt': True}], 'omitted_count': 2}}
            before = copy.deepcopy(data)
            result = project_dynamic(data)
            self.assertEqual({'编号', '摘要'}, set(result['记忆'][0]))
            self.assertEqual('原作者摘要', result['记忆'][0]['摘要'])
            self.assertEqual(before, data)
            self.assertIn('2', result['必浮现省略'])
            self.assertIn('来源', project_dynamic(data, include_sources=True)['记忆'][0])

    def test_empty_and_same_window_have_no_frames(self):
        self.assertEqual({}, project_dynamic({'emotional_memory': {'pins': [], 'ephemeral': [], 'memories': []},
            'session_continuity': {'source_transition': 'same', 'model_transition': 'same'}}))
        self.assertEqual('', continuity_notice({'source_transition': 'unknown', 'model_transition': 'unknown'}))

    def test_window_model_and_combined_changes_are_concise(self):
        data = {'source_transition': 'changed', 'model_transition': 'same',
                'previous_source_key': '12345678aaaa', 'source_key': '87654321bbbb',
                'previous_route': {'upstream_model': 'old'}, 'route': {'upstream_model': 'new'}}
        self.assertEqual('本轮已换窗，对话窗口从窗口12345678变为窗口87654321。模型未改变。', continuity_notice(data))
        data.update(source_transition='same', model_transition='changed')
        self.assertEqual('本轮对话窗口未改变。当前对话模型身份改变，模型已从old变为new。', continuity_notice(data))
        data['source_transition'] = 'changed'
        self.assertIn('从old变为new', continuity_notice(data))

    def test_short_reference_is_record_stable_and_module_specific(self):
        self.assertEqual(short_ref('emotion://x@1'), short_ref('emotion://x@5'))
        self.assertNotEqual(short_ref('emotion://x@1'), short_ref('plan://x@1'))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.base = fixture.ContextLayoutTests(methodName='runTest')
        self.base.setUp(); self.addCleanup(self.base.doCleanups)
        self.base.fixture.seed_tagless_named_learning()

    def test_internal_receipt_and_compact_wire_are_separate_and_restart_frozen(self):
        b = self.base
        wake = b.issue()
        result = b.prepare(wake, source_frame={'query_text': '你还记得我们之前读的侍魔嘛？'})
        wire = result['context_bundle']['dynamic_message']['content']
        self.assertIn('摘要', wire)
        self.assertNotIn('semantic_score', wire)
        self.assertNotIn('item_ref', wire)
        self.assertNotIn('来源', wire)
        with b.connect() as db:
            diagnostic = db.execute('SELECT dynamic_json FROM brain_context_snapshots WHERE wake_id=?', (wake['wake_id'],)).fetchone()[0]
        self.assertIn('semantic_score', diagnostic)
        b.confirm(wake, result['context_hash'])
        b.store = fixture.ModuleOneOnboardingStore(b.database, capability_secret=b.store.capability_secret)
        reused = b.prepare(wake)
        self.assertEqual(result['context_bundle'], reused['context_bundle'])
        self.assertEqual(result['context_hash'], reused['context_hash'])

    def test_committed_v1_projection_keeps_its_hash_after_source_removal(self):
        b = self.base
        wake = b.issue()
        b.prepare(wake, context_layout=None, source_frame={'query_text': '你还记得我们之前读的侍魔嘛？'})
        with b.connect() as db:
            row = db.execute('SELECT stable_json,dynamic_json FROM brain_context_snapshots WHERE wake_id=?', (wake['wake_id'],)).fetchone()
            legacy = b.store._context_message(stable=json.loads(row[0]), dynamic=json.loads(row[1]), compact=True)
            old_hash = _sha256(legacy)
            db.execute('DELETE FROM brain_context_summary_only WHERE wake_id=?', (wake['wake_id'],))
            db.execute('UPDATE brain_context_snapshots SET context_hash=? WHERE wake_id=?', (old_hash, wake['wake_id']))
            db.execute('UPDATE brain_wake_sessions SET context_hash=? WHERE wake_id=?', (old_hash, wake['wake_id']))
        b.store = fixture.ModuleOneOnboardingStore(b.database, capability_secret=b.store.capability_secret)
        reused = b.prepare(wake, context_layout=None)
        self.assertEqual('context_reused', reused['decision'])
        self.assertEqual(old_hash, reused['context_hash'])
        self.assertEqual(legacy, reused['message'])

    def test_pre_upgrade_single_snapshot_keeps_its_exact_hash(self):
        b = self.base
        wake = b.issue()
        result = b.prepare(wake, context_layout=None, source_frame={'query_text': '你还记得我们之前读的侍魔嘛？'})
        with b.connect() as db:
            row = db.execute('SELECT stable_json,dynamic_json FROM brain_context_snapshots WHERE wake_id=?', (wake['wake_id'],)).fetchone()
            legacy = b.store._context_message(stable=json.loads(row[0]), dynamic=json.loads(row[1]))
            old_hash = _sha256(legacy)
            db.execute('DELETE FROM brain_context_projection_versions WHERE wake_id=?', (wake['wake_id'],))
            db.execute('UPDATE brain_context_snapshots SET context_hash=? WHERE wake_id=?', (old_hash, wake['wake_id']))
            db.execute('UPDATE brain_wake_sessions SET context_hash=? WHERE wake_id=?', (old_hash, wake['wake_id']))
        b.store = fixture.ModuleOneOnboardingStore(b.database, capability_secret=b.store.capability_secret)
        reused = b.prepare(wake, context_layout=None)
        self.assertEqual('context_reused', reused['decision'])
        self.assertEqual(old_hash, reused['context_hash'])
        self.assertEqual(legacy, reused['message'])
        self.assertIn('semantic_score', reused['message']['content'])
