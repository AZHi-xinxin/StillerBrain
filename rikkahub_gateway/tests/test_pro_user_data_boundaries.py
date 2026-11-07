"""V3 runs existing execution/recovery fences plus real Control/SQLite HTTP."""
import json
import copy
import sqlite3
from contextlib import closing
from dataclasses import replace
from unittest.mock import patch

from rikkahub_gateway.cache_layout import USER_DATA_RULE, USER_DATA_CONTEXT_LAYOUT_CONTRACT
from rikkahub_gateway.server import GatewayError
from rikkahub_gateway.tests import test_gateway as legacy
from rikkahub_gateway.tests import test_gateway_execution_recovery as execution
from rikkahub_gateway.tests import test_gateway_execution_projection as projection
from rikkahub_gateway.tests import test_cache_layout as layout
from rikkahub_gateway.tests.test_pro_user_data_layout import DataControl


class DataExecutionControl(DataControl, execution.ExecutionControl):
    pass


class DataExecutionRecoveryTests(execution.GatewayExecutionRecoveryTests):
    def setUp(self):
        cfg=replace(legacy.config(),context_layout='tail-context-v2',upstream_model='deepseek-v4-pro')
        with patch.object(execution,'ExecutionControl',DataExecutionControl),patch.object(execution,'config',lambda:cfg):
            super().setUp()
        self.assertEqual(USER_DATA_CONTEXT_LAYOUT_CONTRACT,self.prepared.session.context_bundle['contract'])

    def test_recovered_chat_with_old_partial_history_accepts_new_human_not_old_continuation(self):
        # Preserve every original recovery/authority assertion. Only the v3
        # carrier representation differs from the inherited legacy assertion.
        partial=self.later_batch_partial();original=copy.deepcopy(partial)
        with self.assertRaises(GatewayError):self.app.prepare_turn(partial,self.headers)
        self.assertTrue(self.control.closed.wait(timeout=1.0))
        human={'role':'user','content':'synthetic next human turn in the same chat'}
        body={'tools':self.tools,'messages':[*partial['messages'],human]}
        with self.app._lock:self.assertIsNone(self.app._current_session)
        new=self.app.prepare_turn(body,self.headers)
        self.assertFalse(new.continuation)
        self.assertNotEqual(self.prepared.session.wake_id,new.session.wake_id)
        self.assertEqual(original,partial)
        self.assertEqual(partial['messages'],new.payload['messages'][2:-1])
        self.assertEqual({'type':'text','text':human['content']},new.payload['messages'][-1]['content'][0])
        with self.assertRaisesRegex(GatewayError,'tool_continuation_context_lost'):
            self.app.prepare_turn(partial,self.headers)
        self.assertIs(new.session,self.app._current_session)
        self.app.finish_turn(new,keep_for_tools=False)


class DataExecutionProjectionHTTPTests(projection.ExecutionProjectionHTTPTests):
    def setUp(self):
        cfg=replace(legacy.config(),context_layout='tail-context-v2',upstream_model='deepseek-v4-pro')
        with patch.object(execution,'ExecutionControl',DataExecutionControl),patch.object(legacy,'config',lambda:cfg):
            super().setUp()

    def upstream(self,request):
        self.assertEqual(USER_DATA_CONTEXT_LAYOUT_CONTRACT,self.app._current_session.context_bundle['contract'])
        return super().upstream(request)


class DataRealControlWireTests(layout.CacheLayoutRealControlWireTests):
    def setUp(self):
        cfg=replace(layout.config(),upstream_model='deepseek-v4-pro')
        with patch.object(layout,'config',lambda:cfg):super().setUp()
        self.app.config=replace(self.app.config,context_layout='tail-context-v2')

    def test_three_human_wakes_and_tool_continuation_use_real_authenticated_bundle(self):
        messages=[{'role':'system','content':'Synthetic frontend preferences.'},
                  {'role':'user','content':'Earlier human text.'},
                  {'role':'assistant','content':'Earlier answer.'},
                  {'role':'system','name':'orbis_host_data_v1','content':'Synthetic body counter: 2'},
                  {'role':'user','content':'你还记得我们之前读的侍魔嘛？'}]
        first=self.post(messages);session=self.app._current_session
        self.assertEqual(USER_DATA_RULE,session.context_bundle['layout'])
        dynamic=json.loads(session.context_bundle['dynamic_message']['content'])
        self.assertIn('learning_memory',dynamic)
        parts=self.upstream_payloads[0]['messages'][-1]['content']
        self.assertEqual('Synthetic body counter: 2',json.loads(parts[-2]['text'].split('\n',1)[1])['content'])
        self.assertEqual(dynamic,json.loads(json.loads(parts[-1]['text'].split('\n',1)[1])['content']))
        messages.extend([first['choices'][0]['message'],{'role':'tool','tool_call_id':self.call['id'],'content':'{"synthetic":true}'}])
        answer=self.post(messages)
        self.assertEqual(self.upstream_payloads[0]['messages'],self.upstream_payloads[1]['messages'][:-2])
        self.assertEqual(1,len(session.host_receipts))
        self.assertIsNone(self.app._current_session)
        for _ in (2,3):
            # Original source history has no injected data parts.
            messages.extend([answer['choices'][0]['message'],{'role':'user','content':'你还记得我们之前读的侍魔嘛？'}])
            answer=self.post(messages)
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            rows=connection.execute("SELECT status,context_layout_json FROM brain_context_snapshots WHERE context_layout_json != '{}' ORDER BY rowid").fetchall()
        self.assertEqual(3,len(rows));self.assertEqual(['closed']*3,[row[0] for row in rows])
        self.assertEqual([USER_DATA_RULE]*3,[json.loads(row[1])['layout'] for row in rows])
        for payload in self.upstream_payloads:
            for secret in ('wake_capability','context_layout_offer','context_bundle',self.token):
                self.assertNotIn(secret,json.dumps(payload))
