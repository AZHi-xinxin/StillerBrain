import base64
from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

from runtime.memory_desires import MemoryDesireStore, DesireError, append_desire_recall, pending_desire
from runtime.memory_relations import MemoryRelationError
from tests import test_memory_images as image_fixture, test_onboarding as onboarding_fixture
from tests.test_memory_relations import OWNER
from runtime.emotional_memory import EmotionalMemoryStore
from runtime.memory_images import MemoryImageStore
from mcp_server.memory_desire_service import MemoryDesireAccessService
from runtime.ordinary_access import authenticated_ordinary_operation

class MemoryDesireTests(unittest.TestCase):
    def setUp(self):
        image_fixture.MemoryImageStoreTests.setUp(self)
        self.store=MemoryDesireStore(self.database)
        self.identity=tuple(OWNER.values())
    sql=image_fixture.MemoryImageStoreTests.sql
    def act(self, action='put', **fields):
        return self.store.manage(identity=self.identity,guard=lambda db:None,action=action,
            **{'memory_ref':'emotion://e1@1','text':'想一起看看海',**fields})
    def recall(self,score=.9,presentation='original',budget=1200):
        dynamic={'emotional_memory':{'memories':[{'memory_id':'e1','score':score,'presentation':presentation,'gate_decision':'speak'}]}}
        with closing(sqlite3.connect(self.database)) as db:
            db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
            return append_desire_recall(db,**OWNER,dynamic=dynamic,estimate_tokens=lambda x:len(json.dumps(x,ensure_ascii=False)),budget=budget)
    def test_archive_delete_restore_and_cas_leave_parent_byte_fields_unchanged(self):
        original=self.sql('SELECT * FROM emotion_memories')
        first=self.act();self.assertEqual(1,first['desire']['revision'])
        self.assertFalse(self.act()['state_changed'])
        self.act('archive',expected_revision=1)
        self.assertIn('已归档',self.recall()['desires']['attachments'][0]['text'])
        self.assertEqual(original,self.sql('SELECT * FROM emotion_memories'))
        with self.assertRaisesRegex(DesireError,'revision_conflict'):self.act(expected_revision=1)
        self.act('restore');self.act('delete')
        self.assertNotIn('desires',self.recall())
        with self.assertRaisesRegex(DesireError,'deleted_use_put'):self.act('restore')
        self.assertEqual(4,len(self.sql('SELECT * FROM memory_desire_history')))
        self.assertEqual(original,self.sql('SELECT * FROM emotion_memories'))
    def test_scope_policy_length_and_draft_never_create_a_plan(self):
        with self.assertRaises(MemoryRelationError):self.act(memory_ref='emotion://foreign@1')
        with self.assertRaisesRegex(DesireError,'emotional'):self.act(memory_ref='learning://l1@1')
        with self.assertRaisesRegex(DesireError,'text_invalid'):self.act(text='长'*201)
        self.act(text='长'*200)
        before=self.sql('SELECT * FROM planning_items')
        draft=self.act('plan_draft')
        self.assertFalse(draft['state_changed']);self.assertEqual('planning_memory',draft['next_call']['arguments']['module'])
        self.assertEqual(before,self.sql('SELECT * FROM planning_items'))
        self.sql("UPDATE emotion_memories SET sensitivity='restricted' WHERE memory_id='e1'")
        self.assertNotIn('desires',self.recall())
        self.assertEqual([],self.act('list')['desires'])
    def test_score_and_shared_budget_boundaries(self):
        self.act()
        for score in (.3,.8): self.assertNotIn('desires',self.recall(score))
        self.assertIn('desires',self.recall(.8001))
        self.assertNotIn('desires',self.recall(.99,'neutral_hint'))
        self.assertNotIn('desires',self.recall(.99,budget=1))
    def test_reminder_9_10_11_and_archive_reduces_only_desire_count(self):
        with closing(sqlite3.connect(self.database)) as db:
            columns=[r[1] for r in db.execute('PRAGMA table_info(emotion_memories)')]
            for i in range(11):
                expressions=["?" if c=='memory_id' else c for c in columns]
                db.execute(f"INSERT INTO emotion_memories ({','.join(columns)}) SELECT {','.join(expressions)} FROM emotion_memories WHERE memory_id='e1'",(f'new{i}',))
            db.commit()
        for i in range(11):
            self.act(memory_ref=f'emotion://new{i}@1')
            if i in (8,9,10):
                recalled=self.recall()
                self.assertEqual(i>=9,'reminder' in recalled.get('desires',{}))
        self.act('archive',memory_ref='emotion://new10@1');self.act('archive',memory_ref='emotion://new9@1')
        self.assertNotIn('reminder',self.recall().get('desires',{}))
        self.assertEqual('active',self.sql("SELECT lifecycle FROM emotion_memories WHERE memory_id='new10'")[0][0])

class DesireAndImageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=onboarding_fixture.ModuleOneOnboardingTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.fixture.bootstrap_live()
        self.identity=(self.fixture.owner,self.fixture.model)
        self.emotional=EmotionalMemoryStore(self.fixture.database);self.fixture.store.emotional_store=self.emotional
        self.desires=MemoryDesireStore(self.fixture.database)
        self.service=MemoryDesireAccessService(self.desires,onboarding=self.fixture.store,owner_id=self.identity[0],model_id=self.identity[1])
        self.enterContext(patch('socket.socket.connect',side_effect=AssertionError('network forbidden')))
    def save(self):
        return self.emotional.remember(owner_id=self.identity[0],model_id=self.identity[1],wake_id='synthetic-save',expected_row_version=0,
            memory_type='shared_event',original_text='我记得海边灯塔与安静陪伴。',summary='海边灯塔与安静陪伴',primary_emotion='affection',importance=88,keywords=['海边灯塔'],reason='记录合成经历')
    def test_optional_note_is_atomic_and_actual_pre_generation_recall_advertises_images(self):
        with authenticated_ordinary_operation(owner_id=self.identity[0],model_id=self.identity[1],scope='emotional_memory'):
            saved=self.service.remember(self.save,{'module':'emotional_memory'},desire='想再去海边灯塔')
        self.assertTrue(saved['desire_attached'])
        ref=f"emotion://{saved['memory']['memory_id']}@1"
        images=MemoryImageStore(self.fixture.database)
        stored=images.store(owner_id=self.identity[0],model_id=self.identity[1],target_ref=ref,description='synthetic',mime_type='image/png',
            data_base64=base64.b64encode(image_fixture.synthetic_image()).decode(),request_key='a'*64,operation_hash='b'*64,_write_guard=lambda db:None)
        wake=self.fixture.store.issue_wake(owner_id=self.identity[0],model_id=self.identity[1],host_id='host:test',thread_id='thread:desire',source_kind='human_message',source_event_id='desire-event-1')
        kwargs=dict(owner_id=self.identity[0],model_id=self.identity[1],wake_id=wake['wake_id'],wake_capability=wake['wake_capability'],source_digest='source:desire-1',host_contract_digest='host-contract:v1',
            source_frame={'query_text':'你还记得海边灯塔吗？','thread_id':'thread:desire','lineage_stable':True,'source_event_id':'desire-event-1','capture_items':[]})
        actual=self.fixture.store.build_pre_generation_context(**kwargs)
        payload=json.loads(actual['message']['content'])
        self.assertEqual(ref,payload['memory_images']['attachments'][0]['target_ref'])
        self.assertNotIn('data_uri',actual['message']['content'])
        self.assertIn('想再去海边灯塔',actual['message']['content'])
        again=self.fixture.store.build_pre_generation_context(**kwargs)
        self.assertEqual(actual['message'],again['message'])
        read=images.read(owner_id=self.identity[0],model_id=self.identity[1],target_ref=ref,image_ref=stored['image_ref'],include_data=True)
        self.assertEqual(image_fixture.synthetic_image(),base64.b64decode(read['data_uri'].split(',')[1]))
    def test_unbound_wrong_scope_and_guard_failure_do_not_write_parent_or_desire(self):
        result=self.service.remember(self.save,{'module':'emotional_memory'},desire='想走走')
        self.assertEqual('reject',result['decision'])
        with authenticated_ordinary_operation(owner_id=self.identity[0],model_id=self.identity[1],scope='learning_memory'):
            self.assertEqual('reject',self.service.remember(self.save,{'module':'emotional_memory'},desire='想走走')['decision'])
        def deny(db):raise DesireError('synthetic_guard_revoked')
        with pending_desire(self.desires,self.identity,'不能写入',deny),self.assertRaises(DesireError):self.save()
        with closing(sqlite3.connect(self.fixture.database)) as db:
            self.assertEqual(0,db.execute('SELECT count(*) FROM emotion_memories').fetchone()[0])
            self.assertEqual(0,db.execute('SELECT count(*) FROM memory_desires').fetchone()[0])
