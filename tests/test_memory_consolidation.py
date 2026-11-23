"""Synthetic four-brain consolidation, history, recall and relation regression."""
import json
import unittest
from unittest.mock import patch
from runtime.memory_consolidation import MemoryConsolidationStore, identity
from tests import test_force_recall as fixtures


class ConsolidationTests(unittest.TestCase):
    tearDown = fixtures.ForceRecallTests.tearDown
    save = fixtures.ForceRecallTests.save
    manage = fixtures.ForceRecallTests.manage
    candidates = fixtures.ForceRecallTests.candidates

    def setUp(self):
        fixtures.ForceRecallTests.setUp(self)
        self.organizer = MemoryConsolidationStore(self.stores)
        self.access = dict(wake_id='synthetic-merge',wake_seq=100,write_context_ref='synthetic-ordinary')

    def merge(self, refs, **kw):
        return self.organizer.merge(**self.common,source_refs=refs,summary=kw.pop('summary','AI 重写的连续摘要'),access=self.access,**kw)

    def read(self, ref):
        with self.organizer.connection() as db:
            return self.organizer._read(db,self.identity,*identity(ref))

    def test_each_brain_merges_into_ordinary_editable_record_and_archives_sources(self):
        for module in self.stores:
            with self.subTest(module=module):
                a=self.save(module,keyword='第一');b=self.save(module,keyword='第二')
                result=self.merge([a,b]); item=self.read(result['ref'])
                self.assertEqual('AI 重写的连续摘要',item['summary'])
                self.assertEqual(['第一','第二'],item['keywords'])
                self.assertEqual('active',item['state'])
                self.assertNotEqual('integration',item['fields'].get('memory_type'))
                self.assertIn('PRIVATE ORIGINAL',item['content']) if module!='tool-card' else None
                self.assertIn(self.read(a)['state'],{'archived','retired'})
                store=self.stores[module];base={**self.common,'wake_id':'synthetic-edit','expected_row_version':store.status(**self.common)['row_version'],'reason':'自由修改'}
                if module=='emotion':store.revise(**base,memory_id=item['id'],expected_memory_version=1,changes={'summary':'修改后的摘要'})
                elif module=='learning':store.revise_ordinary(**base,wake_seq=101,learning_id=item['id'],expected_item_version=1,changes={'summary':'修改后的摘要'})
                elif module=='tool-card':store.revise(**base,wake_seq=101,card_id=item['id'],expected_card_version=1,purpose='修改后的摘要')
                else:store.revise_ordinary(**base,wake_seq=101,plan_id=item['id'],expected_plan_version=1,changes={'summary':'修改后的摘要'})
                self.assertEqual('修改后的摘要',self.read(result['ref'])['summary'])

    def test_all_plan_bindings_keywords_and_force_priority_survive(self):
        plans=[self.save('plan') for _ in range(3)]
        a=self.save(bound_plan_refs=plans[:2]);b=self.save(bound_plan_refs=plans[1:])
        self.manage('enable',a);self.manage('enable',b)
        result=self.merge([a,b], keywords=[f'词{i}' for i in range(200)])
        self.assertEqual(plans,result['bound_plan_refs'])
        self.assertEqual(201,len(result['keywords']))
        self.assertEqual([result['ref']],[x['ref'] for x in self.candidates()])
        group=self.organizer.timelines(**self.common,refs=[result['ref']])['groups'][0]
        self.assertEqual(3,group['bound_memory_count'])

    def test_source_timeline_remerge_expands_originals_and_retains_new_author_text(self):
        refs=[self.save(source_timestamp=f'2026-0{i}-01T12:00:00Z') for i in (3,1,2)]
        first=self.merge(refs[:2],content='作者重新串联的正文')
        second=self.merge([first['ref'],refs[2]])
        group=self.organizer.timelines(**self.common,refs=[second['ref']],include_content=True)['groups'][0]
        self.assertEqual(3,group['source_count'])
        self.assertEqual(['2026-01','2026-02','2026-03'],[x['event_at'][:7] for x in group['timeline']])
        self.assertEqual('作者重新串联的正文',self.read(first['ref'])['content'])
        self.assertIn('作者重新串联的正文',self.read(second['ref'])['content'])
        self.assertEqual('作者重新串联的正文',group['prior_summaries'][0]['content'])

    def test_empty_summary_and_failure_never_partially_archive(self):
        refs=[self.save(),self.save()]
        with self.assertRaisesRegex(ValueError,'ai_written_summary'):self.merge(refs,summary=' ')
        with patch.object(self.organizer,'_transfer',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.merge(refs)
        self.assertEqual(['active','active'],[self.read(r)['state'] for r in refs])
        with self.organizer.connection() as db:
            self.assertEqual(2,db.execute('SELECT COUNT(*) FROM emotion_memories').fetchone()[0])

    def test_large_body_summary_and_same_wake_old_low_importance_group(self):
        refs=[self.save(source_timestamp='2020-01-01T00:00:00Z',importance=1) for _ in range(4)]
        groups=self.organizer.timelines(**self.common)['groups']
        self.assertTrue(groups[0]['merge_suggested'])
        result=self.merge(refs,content='长正文。'*2000,summary='AI摘要。'*300)
        self.assertEqual(8000,len(self.read(result['ref'])['content']))

    def test_foreign_owner_not_read_or_merged(self):
        a=self.save();b=self.save()
        with self.organizer.connection() as db:db.execute('UPDATE emotion_memories SET owner_id=? WHERE memory_id=?',('other',identity(b)[1]))
        with self.assertRaisesRegex(ValueError,'memory_not_found'):self.merge([a,b])
        self.assertEqual('active',self.read(a)['state'])

    def test_backfilled_event_date_changes_timeline_but_not_storage_time(self):
        first = self.save(source_timestamp='2026-11-10T12:00:00Z')
        second = self.save(source_timestamp='2026-11-23T12:00:00Z')
        created = self.read(second)['created_at']
        store = self.stores['emotion']
        store.revise(**self.common, wake_id='backfill', expected_row_version=store.status(**self.common)['row_version'],
                     memory_id=identity(second)[1], expected_memory_version=1,
                     changes={'source_timestamp':'2026-10-10T12:00:00Z'}, reason='补写真实事件日期')
        group = self.organizer.timelines(**self.common, refs=[first, second])['groups'][0]
        self.assertEqual(['2026-10-10','2026-11-10'], [r['event_at'][:10] for r in group['timeline']])
        self.assertEqual(created, self.read(second)['created_at'])
        self.assertTrue(all(r['time_basis']=='source_timestamp' for r in group['timeline']))

    def test_plan_merge_transfers_incoming_bindings_and_completed_state(self):
        refs=[self.save('plan'),self.save('plan')];emotion=self.save(bound_plan_refs=refs)
        with self.organizer.connection() as db:
            for ref in refs:self.stores['plan']._append_event(db,**self.common,plan_id=identity(ref)[1],event_type='complete',reason='done',evidence=[],wake_id='done',wake_seq=2)
        merged=self.merge(refs)
        self.assertEqual('completed',self.read(merged['ref'])['state'])
        self.assertEqual([merged['ref']],self.read(emotion)['bound_plan_refs'])


class ConsolidationRelationsTests(unittest.TestCase):
    setUp = ConsolidationTests.setUp
    tearDown = ConsolidationTests.tearDown
    save = ConsolidationTests.save
    merge = ConsolidationTests.merge
    read = ConsolidationTests.read

    def test_generic_links_images_and_feeling_tags_follow_summary(self):
        import base64
        from runtime.memory_relations import MemoryRelationStore
        from runtime.memory_images import MemoryImageStore
        from runtime.emotion_layer import EmotionLayerStore
        from tests.test_memory_images import synthetic_image
        relation=MemoryRelationStore(self.path);images=MemoryImageStore(self.path);feelings=EmotionLayerStore(self.path)
        a=self.save();b=self.save();other=self.save('learning')
        relation.attach(**self.common,from_ref=a,to_ref=other,type='related_to')
        relation.attach(**self.common,from_ref=b,to_ref=other,type='related_to')
        feelings.manage(identity=self.identity,guard=lambda db:None,action='create',name='期待',description='合成定义')
        feelings.tag(identity=self.identity,guard=lambda db:None,memory_ref=a,emotions=['期待'])
        image=images.store(**self.common,target_ref=a,description='合成图片',mime_type='image/png',data_base64=base64.b64encode(synthetic_image()).decode(),request_key='a'*64,operation_hash='b'*64,_write_guard=lambda db:None)
        merged=self.merge([a,b]);mid=identity(merged['ref'])[1]
        with self.organizer.connection() as db:
            self.assertEqual(1,db.execute('SELECT COUNT(*) FROM memory_relation_items WHERE from_id=? OR to_id=?',(mid,mid)).fetchone()[0])
            self.assertEqual(1,db.execute('SELECT COUNT(*) FROM emotion_layer_tags WHERE memory_id=?',(mid,)).fetchone()[0])
            self.assertEqual(synthetic_image(),db.execute('SELECT original_bytes FROM memory_image_items WHERE target_id=?',(mid,)).fetchone()[0])
        group=self.organizer.timelines(**self.common,refs=[merged['ref']])['groups'][0]
        self.assertEqual(1,group['bound_memory_count'])

    def test_legacy_emotion_integrate_keeps_all_keywords_and_plans(self):
        plans=[self.save('plan'),self.save('plan')]
        a=self.save(keyword='甲',bound_plan_refs=plans[:1]);b=self.save(keyword='乙',bound_plan_refs=plans[1:])
        store=self.stores['emotion']
        result=store.integrate(**self.common,wake_id='legacy-merge',expected_row_version=store.status(**self.common)['row_version'],source_memory_ids=[identity(a)[1],identity(b)[1]],original_text='AI正文',summary='AI摘要',primary_emotion='calm',reason='自主整理',keywords=['丙'])
        with self.organizer.connection() as db:
            row=db.execute("SELECT * FROM emotion_memories WHERE lifecycle='active'").fetchone()
        self.assertEqual(['甲','乙','丙'],json.loads(row['keywords_json']))
        self.assertEqual(plans,json.loads(row['bound_plan_refs_json']))

    def test_more_than_twenty_sources_and_pagination(self):
        refs=[self.save() for _ in range(23)]
        merged=self.merge(refs)
        self.assertEqual(23,merged['source_count'])
        self.assertEqual(23,self.organizer.timelines(**self.common,refs=[merged['ref']])['groups'][0]['source_count'])

    def test_keyword_reminder_has_no_age_importance_or_independent_wake_gate(self):
        from runtime.memory_consolidation import append_consolidation_notice
        for _ in range(4):self.save(keyword='共同关键词',importance=0,source_timestamp='2020-01-01T00:00:00Z')
        with self.organizer.connection() as db:
            result=append_consolidation_notice(db,**self.common,query='提到共同关键词',dynamic={},enabled=lambda m:True)
            silent=append_consolidation_notice(db,**self.common,query='无关内容',dynamic={},enabled=lambda m:True)
        self.assertEqual(1,result['memory_consolidation_notice']['topic_count'])
        self.assertEqual({},silent)

    def test_query_semantic_hits_share_a_topic_timeline(self):
        from runtime.semantic_retrieval import Result
        a=self.save(keyword='甜食');b=self.save(keyword='糖葫芦')
        with patch('runtime.semantic_retrieval.search',return_value=Result('ready',{a:.8,b:.9})):
            groups=self.organizer.timelines(**self.common,query='吃糖的几次经历')['groups']
        self.assertEqual(1,len(groups));self.assertEqual(2,groups[0]['source_count'])

    def test_incoming_plan_dependencies_and_tool_links_follow_new_record(self):
        plans = [self.save('plan'), self.save('plan')]
        dependent = self.save('plan', dependency_refs=plans)
        emotion = self.save(bound_plan_refs=plans)
        merged = self.merge(plans)
        self.assertEqual([merged['ref']], self.read(dependent)['fields']['dependency_refs'])
        group = self.organizer.timelines(**self.common, refs=[merged['ref']])['groups'][0]
        self.assertEqual({identity(dependent), identity(emotion)}, {identity(r) for r in group['bound_memory_refs']})
        tools = [self.save('tool-card'), self.save('tool-card')]
        caller = self.save('tool-card', linked_tool_refs=tools)
        combined = self.merge(tools)
        self.assertEqual([combined['ref']], self.read(caller)['fields']['linked_tool_refs'])
        group = self.organizer.timelines(**self.common, refs=[combined['ref']])['groups'][0]
        self.assertEqual([identity(caller)], [identity(r) for r in group['bound_memory_refs']])

    def test_legacy_authenticated_execution_can_merge_without_extra_review(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from mcp_server.memory_consolidation_service import MemoryConsolidationAccessService
        refs = [self.save(), self.save()]
        onboarding = Mock()
        onboarding.authorize_other_module_write.return_value = {'decision': 'allowed'}
        service = MemoryConsolidationAccessService(self.stores, **self.common, onboarding=onboarding)
        claim = SimpleNamespace(**self.common, wake_id='legacy-synthetic')
        with patch('runtime.execution_binding.current_execution_claim', return_value=claim):
            result = service.call('merge', source_refs=refs, summary='旧入口同样自主整理')
        self.assertEqual('merged', result['decision'])
        self.assertEqual('旧入口同样自主整理', self.read(result['ref'])['summary'])
