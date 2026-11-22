"""Real author lifecycle plus pure hard/soft/summary rules, synthetic data only."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import jsonschema
from runtime.facet_recall import DEFAULT_POLICY, select_facets, recall_facets, valid_policy
from runtime.recall_projection import estimate_dynamic, project_dynamic
from runtime.self_revision import active_injection_structure_violations
from tests import test_onboarding as fixtures
from mcp_server.service import SelfModelAccessService


def facet(text='Authored original.', summary='My short note.', keywords=(), aliases=(), related=()):
    return dict(text=text, summary=summary, keywords=list(keywords), aliases=list(aliases), related_facets=list(related))


class MatchingTests(unittest.TestCase):
    def select(self, values, query='', **kw):
        return select_facets(values, query, estimate=estimate_dynamic, **kw)

    def test_all_five_hard_hits_and_linked_facets_survive_small_budget(self):
        values = {f'side{i}': facet(text='全文不可截断。' * 50, keywords=['trigger']) for i in range(5)}
        values['side0']['related_facets'] = ['linked']
        values['linked'] = facet(text='关联全文', related=['side0'])
        values['quiet'] = facet(summary='我自己写的未命中摘要。')
        result = self.select(values, 'trigger', policy={**DEFAULT_POLICY, 'budget_tokens': 256})
        self.assertEqual(set(values)-{'quiet'}, set(result['facets']))
        self.assertEqual({'quiet': '我自己写的未命中摘要。'}, result['summaries'])
        self.assertTrue(result['diagnostics']['mandatory_exceeds_budget'])
        self.assertEqual(values['side0']['text'], result['facets']['side0'])
        self.assertNotIn('diagnostics', str(project_dynamic({'self_facets': result})))

    def test_key_keyword_alias_and_body_are_independent_adjustable_hard_channels(self):
        values = {'named': facet(), 'keywords': facet(keywords=['信号']),
                  'aliases': facet(aliases=['另一个称呼']), 'body': facet(text='开始研究星轨摄影。')}
        result = self.select(values, 'named 信号 另一个称呼。现在研究星轨摄影。')
        self.assertEqual(set(values), set(result['facets']))
        off = {**DEFAULT_POLICY, **{k:False for k in ('match_key','match_keywords','match_aliases','match_body','semantic_enabled')}}
        result = self.select(values, 'named 信号 另一个称呼 星轨摄影', policy=off)
        self.assertEqual({}, result['facets'])
        self.assertEqual(set(values), set(result['summaries']))

    def test_soft_match_expands_only_when_budget_fits_and_leaves_every_other_summary(self):
        values = {'small': facet(text='我自己写的软命中全文。'), 'large': facet(text='长篇全文' * 450),
                  'unmatched': facet(summary='这个摘要也要保留。')}
        result = self.select(values, 'unrelated', policy={**DEFAULT_POLICY,'budget_tokens':256},
            semantic_scores={'large':.95,'small':.8}, semantic_status='ready')
        self.assertEqual({'small'}, set(result['facets']))
        self.assertEqual({'large','unmatched'}, set(result['summaries']))

    def test_legacy_without_summary_keeps_exact_original_never_auto_summarizes(self):
        values = {'legacy': '这是我自己写的旧原文。', 'new': facet(summary='AI自写摘要。')}
        result = self.select(values)
        self.assertEqual(values['legacy'], result['facets']['legacy'])
        self.assertEqual({'new':'AI自写摘要。'}, result['summaries'])

    def test_custom_separator_is_verbatim_and_can_be_disabled(self):
        values = {'direct':facet(), 'other':facet()}
        for phrase in ('我自己定的过渡句。',''):
            result = self.select(values,'direct',policy={**DEFAULT_POLICY,'summary_separator':phrase})
            wire = project_dynamic({'self_facets':result})['人格侧面']
            self.assertEqual(phrase, wire.get('提示',''))
            self.assertNotIn(DEFAULT_POLICY['summary_separator'], str(wire))
            self.assertEqual({'direct'},set(wire['原文']))
            self.assertEqual({'other'},set(wire['摘要']))

    def test_semantic_pending_failure_or_disabled_never_blocks_hard_and_summary(self):
        values = {'hard':facet(), 'other':facet()}
        for value in (None, Exception('synthetic service unavailable')):
            with patch('runtime.facet_recall.dense.search', return_value=value, side_effect=value if isinstance(value,Exception) else None) as search:
                if value is None:
                    from runtime.semantic_retrieval import Result
                    search.return_value=Result('indexing_lexical_fallback',{})
                result=recall_facets(values,'hard',policy=DEFAULT_POLICY,database='/tmp/synthetic-unused',owner_id='o',model_id='m',estimate=estimate_dynamic)
                self.assertFalse(search.call_args.kwargs['explicit_wait'])
                self.assertEqual({'hard'},set(result['facets']))
                self.assertEqual({'other'},set(result['summaries']))
        with patch('runtime.facet_recall.dense.search',side_effect=AssertionError('must not call')):
            recall_facets(values,'hard',policy={**DEFAULT_POLICY,'semantic_enabled':False},database='/tmp/unused',owner_id='o',model_id='m',estimate=estimate_dynamic)


class AuthorLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ModuleOneOnboardingTests(methodName='runTest')
        self.f.setUp(); self.addCleanup(self.f.tearDown)
        self.content = fixtures.model_content('synthetic-rich')
        self.content.update(schema_version='0.2.0', facet_recall={**DEFAULT_POLICY,'semantic_enabled':False,'summary_separator':'我的摘要过渡句。'},
            facets={'work':facet(text='我在工作时认真核对。',keywords=['一起工作']),
                    'play':facet(text='我在玩耍时自由发挥。',summary='我也有爱玩的另一面。')})
        original=self.f.candidate_payload
        def authored(*args, **kwargs):
            result=original(*args, **kwargs);result['content']=deepcopy(self.content);return result
        self.f.candidate_payload=authored
        self.f.bootstrap_live()

    def prepare(self, event, query='一起工作'):
        wake=self.f.store.issue_wake(owner_id=self.f.owner,model_id=self.f.model,host_id='host:synthetic',
            thread_id='thread:synthetic',source_kind='human_message',source_event_id=event)
        args=dict(owner_id=self.f.owner,model_id=self.f.model,wake_id=wake['wake_id'],wake_capability=wake['wake_capability'],
                  source_digest=event,host_contract_digest='synthetic-host',source_frame={'query_text':query})
        return wake,args,self.f.store.build_pre_generation_context(**args)

    def test_runtime_schema_and_all_authored_fields_agree_without_rewriting(self):
        schema=json.loads((Path(__file__).parents[1]/'schemas/self-model.schema.json').read_text())
        jsonschema.validate(self.content,schema)
        self.assertEqual([], active_injection_structure_violations(self.content))
        self.assertEqual([],self.f.store.self_store._content_findings(self.content)[0])
        for invalid in ({**self.content,'facet_recall':{**self.content['facet_recall'],'budget_tokens':True}},
                        {**self.content,'facets':{'broken':facet(related=['missing'])}}):
            self.assertEqual([], self.f.store.self_store._content_findings(invalid)[0])
            jsonschema.validate(invalid, schema)

    def test_current_wire_is_all_facets_and_exact_read_returns_settings_receipt(self):
        wake,args,result=self.prepare('facet-current')
        wire=json.loads(result['message']['content'])['人格侧面']
        self.assertEqual({'work':self.content['facets']['work']['text']},wire['原文'])
        self.assertEqual({'play':self.content['facets']['play']['summary']},wire['摘要'])
        self.assertEqual('我的摘要过渡句。',wire['提示'])
        for forbidden in ('hard_matches','semantic_status','budget_tokens','related_facets'):
            self.assertNotIn(forbidden,result['message']['content'])
        self.f.store.confirm_context_injected(owner_id=self.f.owner,model_id=self.f.model,
            wake_id=wake['wake_id'],wake_capability=wake['wake_capability'],context_hash=result['context_hash'])
        service=SelfModelAccessService(self.f.store.self_store,owner_id=self.f.owner,model_id=self.f.model,onboarding=self.f.store)
        read=service.query_self_model(view='active',facet_names=['work'])['result']
        self.assertEqual(self.content['facets']['work'],read['facets']['work'])
        self.assertEqual(self.content['facet_recall'],read['facet_recall_settings'])
        self.assertIn('work',read['facet_recall_receipt']['hard_matches'])
        self.assertEqual([],read['facet_recall_receipt']['summary_only'])
        again=self.f.store.build_pre_generation_context(**args)
        self.assertEqual(result['message'],again['message'])

    def test_facets_do_not_compete_for_ordinary_memory_cap(self):
        from runtime.emotional_memory import EmotionalMemoryStore
        self.f.store.emotional_store=EmotionalMemoryStore(self.f.database)
        self.f.store.emotional_store.remember(owner_id=self.f.owner,model_id=self.f.model,wake_id='synthetic-save',expected_row_version=0,
            memory_type='shared_event',original_text='我记得一起工作时核对了一个例子。',summary='一起工作的例子。',
            primary_emotion='affection',importance=80,keywords=['一起工作'],reason='合成测试记录')
        _,_,one=self.prepare('facet-with-memory')
        with patch('runtime.onboarding.recall_facets',return_value={'facets':{'big':'全' * 12000},'summaries':{}}):
            _,_,two=self.prepare('large-facet-with-memory')
        self.assertEqual(json.loads(one['message']['content'])['记忆'],json.loads(two['message']['content'])['记忆'])
        self.assertEqual(12000,len(json.loads(two['message']['content'])['人格侧面']['原文']['big']))
