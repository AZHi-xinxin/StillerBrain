"""Synthetic public workflows for literal-cue priority and host receipts."""
from runtime.recall_projection import short_ref
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.force_recall import ForceRecallStore, ForceRecallError, literal_position, select_forced
from runtime.onboarding import _estimate_tokens
from runtime.emotional_memory import EmotionalMemoryStore
from runtime.learning_memory import LearningMemoryStore
from runtime.planning_memory import PlanningMemoryStore
from runtime.tool_guidance import ToolGuidanceStore
from tests.test_tool_guidance import action_card, catalog


class ForceRecallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='force-recall-synthetic-')
        self.path = Path(self.temp.name) / 'brain.db'
        self.identity = ('synthetic-owner', 'synthetic-model')
        self.common = dict(zip(('owner_id', 'model_id'), self.identity))
        self.stores = {'emotion': EmotionalMemoryStore(self.path),
                       'learning': LearningMemoryStore(self.path, idea_database=Path(self.temp.name)/'ideas.db'),
                       'plan': PlanningMemoryStore(self.path),
                       'tool-card': ToolGuidanceStore(self.path, detail_lookup_schema_hash='b'*64)}
        self.force = ForceRecallStore(self.path)
        self.sequence = 0

    def tearDown(self):
        self.temp.cleanup()

    def save(self, module='emotion', keyword='ex', **overrides):
        self.sequence += 1
        store = self.stores[module]
        store.ensure_state(**self.common)
        base = {**self.common, 'wake_id': f'wake:synthetic:{self.sequence}',
                'expected_row_version': store.status(**self.common)['row_version']}
        fields = {'keywords': [keyword], **overrides}
        if module == 'emotion':
            result = store.remember(**base, memory_type='shared_event',
                original_text=f'PRIVATE ORIGINAL {self.sequence}', summary='合成摘要，不是正文', primary_emotion='calm', **fields)
            mid = result['memory']['memory_id']
        elif module == 'learning':
            result = store.remember(**base, wake_seq=self.sequence, kind='fact', title='合成知识',
                summary='有来源的合成摘要', current_understanding=f'PRIVATE ORIGINAL {self.sequence}',
                correctness_assessment='Synthetic fixture', reason='Synthetic fixture', source_basis='reported',
                claim_review={'status':'ordinary'}, confidence=40, **fields)
            mid = result['learning_id']
        elif module == 'plan':
            result = store.remember_ordinary(**base, wake_seq=self.sequence, write_context_ref='synthetic-context',
                idempotency_key=f'synthetic:{self.sequence}', content=f'PRIVATE ORIGINAL {self.sequence}',
                title='合成计划', summary='合成计划摘要', **fields)
            mid = result['plan_id']
        else:
            fields = action_card(**fields, operation_key=f'action_{self.sequence}')
            reason = fields.pop('reason')
            result = store.remember(**base, catalog=catalog(), reason=reason, **fields)
            mid = result['card']['card_id']
        return f'{module}://{mid}@1'

    def manage(self, action, target=None, **kw):
        return self.force.manage(identity=self.identity, guard=lambda db: None,
                                 action=action, target_ref=target, **kw)

    def candidates(self, query='ex', enabled=lambda scope: True):
        with self.force.connection() as db:
            return self.force.candidates(db, identity=self.identity, query=query, scope_enabled=enabled)

    def source_rows(self):
        with self.force.connection() as db:
            names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'memory_force_recall%'")]
            return {name:[tuple(r) for r in db.execute('SELECT * FROM "'+name+'"')] for name in names}

    def test_default_off_limit_shared_by_four_brains_and_no_eviction(self):
        refs = [self.save(module) for module in self.stores for _ in range(3)]
        before = self.source_rows()
        self.assertEqual([], self.candidates())
        for ref in refs[:10]: self.manage('enable', ref)
        with self.assertRaisesRegex(ForceRecallError, 'limit_disable_one_first'):
            self.manage('enable', refs[10])
        self.assertEqual(set(refs[:10]), {x['target_ref'] for x in self.manage('list')['items']})
        self.manage('disable', refs[0]); self.manage('enable', refs[10])
        self.assertEqual(10, self.manage('list')['enabled_count'])
        self.assertEqual(before, self.source_rows())

    def test_literal_boundaries_and_full_length_new_text(self):
        for text in ['ex', '我说ex那件事', 'EX', '（ｅｘ）', '还记得 ex？']:
            self.assertGreaterEqual(literal_position(text, ['ex']), 0)
        for text in ['text', 'example', 'next', '_ex', 'ex2', 'éex']:
            self.assertEqual(-1, literal_position(text, ['ex']))
        self.assertGreaterEqual(literal_position('今天到家啦', ['到家']), 0)
        ref=self.save(); self.manage('enable',ref)
        self.assertEqual([ref], [x['ref'] for x in self.candidates('很长的内容。'*1000+' ex')])

    def test_current_policy_and_module_switch_win_over_priority(self):
        refs=[self.save('emotion', recall_mode='never'),
              self.save('emotion', deny_contexts=['ex']),
              self.save('emotion', allow_contexts=['仅此场景']),
              self.save('learning', context_policy='ask_first'),
              self.save('tool-card', auto_recall_mode='never_auto', salience_reason='Synthetic author declines auto recall')]
        for ref in refs: self.manage('enable',ref)
        candidates=self.candidates()
        self.assertEqual(len(refs), len(candidates))
        self.assertTrue(all(x['entry'] is None and x['blocked'] for x in candidates))
        self.assertTrue(all(x['blocked']=='module_disabled' for x in self.candidates(enabled=lambda _:False)))

    def test_projection_preserves_disclosure_and_tool_authority(self):
        refs=[self.save('emotion', context_policy='ask_first'), self.save('learning'), self.save('tool-card')]
        for ref in refs:self.manage('enable',ref)
        result=select_forced({},self.candidates(),estimate_tokens=_estimate_tokens,budget=1200)
        text=json.dumps(result,ensure_ascii=False)
        self.assertNotIn('PRIVATE ORIGINAL',text)
        entries={x['item_ref']:x for x in result['force_recall']['items']}
        self.assertEqual('neutral_hint',entries[refs[0]]['presentation'])
        self.assertNotIn('合成摘要',entries[refs[0]]['text'])
        self.assertEqual('reported',entries[refs[1]]['source_basis'])
        self.assertEqual(40,entries[refs[1]]['confidence'])
        self.assertEqual('none',result['force_recall']['execution_authority'])

    def test_budget_keeps_last_mention_and_reports_omissions(self):
        refs=[]
        for i in range(10):
            ref=self.save(keyword=f'cue{i}', importance=99-i)
            refs.append(ref); self.manage('enable',ref)
        candidates=self.candidates(' '.join(f'cue{i}' for i in range(10)))
        result=select_forced({},candidates,estimate_tokens=_estimate_tokens,budget=350)
        self.assertLessEqual(_estimate_tokens(result),350)
        self.assertEqual(refs[-1],result['force_recall']['items'][0]['item_ref'])
        self.assertGreater(result['force_recall']['omitted_count'],0)
        self.assertEqual(10,len(result['force_recall']['items'])+result['force_recall']['omitted_count'])

    def test_CAS_empty_cues_namespace_and_nonordinary_rejections(self):
        ref=self.save(); self.manage('enable',ref,expected_revision=0)
        self.assertEqual('unchanged',self.manage('enable',ref)['decision'])
        with self.assertRaisesRegex(ForceRecallError,'revision_conflict'):self.manage('disable',ref,expected_revision=0)
        with self.assertRaisesRegex(ForceRecallError,'version_conflict'):self.manage('disable',ref.replace('@1','@2'))
        with self.assertRaisesRegex(ForceRecallError,'keywords_required'):self.manage('enable',self.save(keywords=[]))
        with self.assertRaises(ValueError):self.manage('enable','work://synthetic@1')
        with self.assertRaisesRegex(ForceRecallError,'memory_unavailable'):
            self.force.manage(identity=('other',self.identity[1]),guard=lambda _:None,action='enable',target_ref=ref)

    def test_receipts_require_host_confirmation_and_event_retry_is_idempotent(self):
        ref=self.save();self.manage('enable',ref)
        candidates=self.candidates();dynamic=select_forced({},candidates,estimate_tokens=_estimate_tokens,budget=1200)
        with self.force.connection() as db:
            for wake in ['wake:failed','wake:replacement']:
                self.force.record_prepared(db,identity=self.identity,wake_id=wake,source_event_id='event:first',candidates=candidates,dynamic=dynamic)
        self.assertEqual(0,self.manage('read',ref)['item']['hit_count'])
        with self.force.connection() as db:
            for _ in range(2):self.force.confirm(db,identity=self.identity,wake_id='wake:replacement')
            self.force.record_prepared(db,identity=self.identity,wake_id='wake:retry',source_event_id='event:first',candidates=candidates,dynamic={})
            self.force.confirm(db,identity=self.identity,wake_id='wake:retry')
        value=self.manage('read',ref)['item']
        self.assertEqual((1,1),(value['hit_count'],value['injected_count']))
        self.assertTrue(value['last_hit_at'] and value['last_injected_at'])
        with self.force.connection() as db:
            self.force.record_prepared(db,identity=self.identity,wake_id='wake:second',source_event_id='event:second',candidates=candidates,dynamic={})
            self.force.confirm(db,identity=self.identity,wake_id='wake:second')
        value=self.manage('read',ref)['item']
        self.assertEqual((2,1),(value['hit_count'],value['injected_count']))
        self.assertEqual('budget_omitted',value['last_delivery_reason'])

    def live(self):
        from tests.test_onboarding import ModuleOneOnboardingTests
        from runtime.onboarding import ModuleOneOnboardingStore
        fixture=ModuleOneOnboardingTests()
        fixture.database=self.path
        fixture.owner,fixture.model=self.identity
        fixture.store=ModuleOneOnboardingStore(self.path,capability_secret=b'synthetic-capability-32-bytes-for-test-only')
        fixture.bootstrap_live()
        fixture.store.emotional_store=self.stores['emotion']
        fixture.store.learning_store=self.stores['learning']
        fixture.store.planning_store=self.stores['plan']
        fixture.store.tool_store=self.stores['tool-card']
        return fixture.store

    def test_real_host_snapshot_confirms_full_newest_text_and_deduplicates(self):
        from rikkahub_gateway.server import GatewayApplication
        from mcp_server.control_server import _source_frame
        store=self.live()
        refs=[self.save(module) for module in self.stores]
        for ref in refs:self.manage('enable',ref)
        event='synthetic-full-message'
        wake=store.issue_wake(**self.common,host_id='host:test',thread_id='thread:test',source_kind='human_message',source_event_id=event)
        messages=[{'role':'user','content':'旧的 ex'}, {'role':'assistant','content':'ex 回声'},
                  {'role':'tool','content':'ex 工具回声'}, {'role':'user','content':'没有关键词的长文本。'*600+' ex'}]
        frame=GatewayApplication._source_frame(messages,thread_id='thread:test',lineage_stable=True,source_event_id=event)
        frame=_source_frame({'source_frame':frame})
        self.assertNotIn('ex',frame['query_text'])
        args={**self.common,'wake_id':wake['wake_id'],'wake_capability':wake['wake_capability'],
              'source_digest':'source:synthetic','host_contract_digest':'host:v1','source_frame':frame,'advertised_tools':catalog()}
        prepared=store.build_pre_generation_context(**args)
        self.assertEqual('context_prepared',prepared['decision'])
        dynamic=prepared.get('dynamic_message') or prepared['message']
        payload=json.loads(dynamic['content'])
        self.assertEqual({short_ref(x) for x in refs},{x['编号'] for x in payload['记忆']})
        self.assertNotIn('PRIVATE ORIGINAL',json.dumps(payload))
        self.assertLessEqual(_estimate_tokens(payload),1200)
        for ref in refs:self.assertEqual(0,self.manage('read',ref)['item']['hit_count'])
        store.confirm_context_injected(**self.common,wake_id=wake['wake_id'],wake_capability=wake['wake_capability'],context_hash=prepared['context_hash'])
        repeated=store.build_pre_generation_context(**args)
        self.assertEqual('context_reused',repeated['decision'])
        self.assertEqual(prepared['context_hash'],repeated['context_hash'])
        for ref in refs:self.assertEqual((1,1),tuple(self.manage('read',ref)['item'][k] for k in ['hit_count','injected_count']))
        # Older user text, assistant/tool echoes, and image-only latest input do not retrigger.
        for latest in ['今天天气不错',[{'type':'image_url','image_url':{'url':'https://example.invalid/synthetic.png'}}]]:
            f=GatewayApplication._source_frame([*messages,{'role':'user','content':latest}],thread_id='thread:test',lineage_stable=True,source_event_id='next')
            self.assertEqual([],self.candidates(f.get('keyword_query_text',f['query_text'])))
        with self.assertRaises(ValueError):_source_frame({'source_frame':{**frame,'keyword_query_text':'different'*600}})

    def test_real_AI_author_guard_and_readonly_requires_identity(self):
        from mcp_server.force_recall_service import ForceRecallAccessService
        from runtime.ordinary_access import authenticated_ordinary_operation
        store=self.live();ref=self.save()
        service=ForceRecallAccessService(self.force,onboarding=store,**self.common)
        self.assertEqual('reject',service.manage('enable',ref)['decision'])
        with authenticated_ordinary_operation(**self.common,scope='emotional_memory'):
            self.assertEqual('saved',service.manage('enable',ref)['decision'])
            self.assertTrue(service.manage('read',ref)['item']['force_recall'])
            other=ForceRecallAccessService(self.force,onboarding=store,owner_id='other',model_id=self.identity[1])
            self.assertEqual('reject',other.manage('disable',ref)['decision'])
        self.assertTrue(self.manage('read',ref)['item']['force_recall'])


if __name__=='__main__':unittest.main()
