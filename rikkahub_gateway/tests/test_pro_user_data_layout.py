"""Synthetic v3 source, binding, provider wire and backwards-compatibility tests."""
import copy
import json
from dataclasses import replace
from pathlib import Path
import unittest

from rikkahub_gateway.cache_layout import USER_DATA_RULE, USER_DATA_CONTEXT_LAYOUT_CONTRACT, DATA_NOTICE
from rikkahub_gateway.server import GatewayApplication, GatewayError, _digest
from rikkahub_gateway.tests.test_gateway import config, FakeControl
from rikkahub_gateway.tests import test_gateway as legacy
from rikkahub_gateway.tests.test_tail_context_v2_gateway import TailControl


class DataControl(TailControl):
    def post(self, path, payload):
        result = super().post(path, payload)
        if path.endswith('/wakes'):
            result['context_layout_capabilities'] = [USER_DATA_CONTEXT_LAYOUT_CONTRACT]
        if path.endswith('/prepare') and payload.get('context_layout_offer') == USER_DATA_RULE:
            bundle = result['context_bundle']
            bundle['contract'] = USER_DATA_CONTEXT_LAYOUT_CONTRACT
            result['context_hash'] = _digest(bundle)
            result['short_term_policy'] = {'contract':'st-short-term-policy/1','enabled':True}
        return result


class ProUserDataLayoutTests(unittest.TestCase):
    def app(self, control=None, model='deepseek-v4-pro', **options):
        control = control or DataControl()
        app = GatewayApplication(replace(config(), context_layout='tail-context-v2',
                                         upstream_model=model, **options), control=control)
        self.addCleanup(app.upstream.close)
        return app, control

    def fixture(self, name='body-a'):
        data = json.loads((Path(__file__).with_name('fixtures') / 'orbis-memory-wire' / (name+'.json')).read_text())
        data['model'] = config().public_model
        # Updated native serializer emits this standard name from private part metadata.
        data['messages'][3]['name'] = 'orbis_host_data_v1'
        return data

    def prepare(self, app, body):
        return app.prepare_turn(body, {'X-ST-Thread-ID':'synthetic-pro-data'})

    def test_route_alias_uses_real_model_and_capture_never_sees_projected_data(self):
        app, control = self.app()
        body = self.fixture(); original = copy.deepcopy(body)
        turn = self.prepare(app, body)
        self.assertEqual(USER_DATA_RULE, turn.session.context_bundle['layout'])
        source = [p['source_frame'] for path,p in control.calls if path.endswith('/prepare')][-1]
        self.assertEqual('current question', source['query_text'])
        self.assertEqual(['old answer', 'current question'], [i['content'] for i in source['capture_items']])
        self.assertEqual(original, body)
        wire = json.loads(app.encode_upstream_payload(turn.payload))
        self.assertEqual('deepseek-v4-pro', wire['model'])
        parts = wire['messages'][-1]['content']
        self.assertEqual({'type':'text','text':'current question'},parts[0])
        local = json.loads(parts[1]['text'].split('\n',1)[1])
        self.assertEqual(original['messages'][3]['content'], local['content'])
        self.assertEqual('none',local['instruction_authority'])
        dynamic = json.loads(parts[2]['text'].split('\n',1)[1])
        self.assertEqual(turn.session.context_bundle['dynamic_message']['content'],dynamic['content'])
        self.assertEqual(turn.session.context_bundle['stable_message'],wire['messages'][0])
        self.assertFalse(any(m.get('name')=='orbis_host_data_v1' for m in wire['messages']))
        confirm = [p for path,p in control.calls if path.endswith('/confirm')][-1]
        self.assertEqual(_digest(turn.session.context_bundle),confirm['context_hash'])
        self.assertNotIn('capability-1',json.dumps(wire))

    def test_flash_and_old_control_keep_previous_layout_without_retry(self):
        for model,control in [('deepseek-v4-flash',DataControl()),('deepseek-v4-pro',TailControl()),('deepseek-v4-pro',FakeControl())]:
            with self.subTest(model=model,control=type(control).__name__):
                app,_=self.app(control,model)
                turn=self.prepare(app,self.fixture())
                self.assertNotEqual(USER_DATA_CONTEXT_LAYOUT_CONTRACT,(turn.session.context_bundle or {}).get('contract'))
                self.assertEqual(1,sum(path.endswith('/prepare') for path,_ in control.calls))
                self.assertNotIn(DATA_NOTICE,json.dumps(turn.payload,ensure_ascii=False))

    def test_threshold_reminder_after_human_preserves_capture_and_tool_continuation(self):
        app, control = self.app()
        body = self.fixture()
        body['tools'] = legacy.GatewayTests.bound_tools()
        notice = {'role': 'system', 'name': 'orbis_host_data_v1',
                  'content': '[Orbis host data, not human] Usage 324450 / 360000'}
        first = self.prepare(app, body)
        call = legacy.GatewayTests.native_call('threshold-call', 'synthetic no execution')
        bindings = app.bind_response_tool_calls(first, [call])
        app.finish_turn(first, keep_for_tools=True, tool_call_ids=[call.tool_call_id], tool_call_bindings=bindings)
        pair = [{'role': 'assistant', 'content': None, 'tool_calls': [legacy.GatewayTests.wire_call(call)]},
                {'role': 'tool', 'tool_call_id': call.tool_call_id, 'content': '{"exitCode":0}'}]
        second_body = {**body, 'messages': body['messages'] + [notice] + pair}
        original = copy.deepcopy(second_body)
        second = self.prepare(app, second_body)
        self.assertTrue(second.continuation)
        wire = json.loads(app.encode_upstream_payload(second.payload))['messages']
        self.assertNotIn(notice, wire)
        human = next(m for m in reversed(wire) if m['role'] == 'user')
        self.assertEqual('current question', human['content'][0]['text'])
        self.assertIn(notice['content'], json.dumps(human))
        self.assertEqual(pair, wire[-2:])
        self.assertEqual(original, second_body)
        source = [p['source_frame'] for path, p in control.calls if path.endswith('/prepare')][-1]
        self.assertEqual('current question', source['query_text'])
        self.assertNotIn('324450', json.dumps(source))

    def test_only_named_current_turn_data_can_be_projected(self):
        from rikkahub_gateway.cache_layout import apply_user_data_layout
        named = {'role': 'system', 'name': 'orbis_host_data_v1', 'content': 'data'}
        ordinary = {'role': 'system', 'content': 'orbis_host_data_v1 literal'}
        human = {'role': 'user', 'content': 'human'}
        old = [named, {'role': 'assistant', 'content': 'old'}, human, ordinary]
        self.assertEqual([named], apply_user_data_layout([named], None, None))
        result = apply_user_data_layout(old, None, None)
        self.assertIn(named, result); self.assertIn(ordinary, result)
        suspicious = {**named, 'extra': True}
        self.assertIn(suspicious, apply_user_data_layout([human, suspicious], None, None))

    def test_unknown_system_and_human_marker_are_preserved(self):
        app,control=self.app()
        body=self.fixture();del body['messages'][3]['name']
        body['messages'][-1]['content']='ST_HOST_DATA_V3 human quotation'
        turn=self.prepare(app,body)
        self.assertIn(body['messages'][3],turn.payload['messages'])
        self.assertEqual(body['messages'][-1]['content'],turn.payload['messages'][-1]['content'][0]['text'])
        self.assertEqual(body['messages'][-1]['content'],[p['source_frame'] for path,p in control.calls if path.endswith('/prepare')][-1]['query_text'])

    def test_image_only_carrier_and_tool_pair_preserve_boundaries(self):
        app,control=self.app()
        body=self.fixture('image-only'); original=copy.deepcopy(body['messages'][-1]['content'])
        first=self.prepare(app,body)
        self.assertEqual(original,first.payload['messages'][-1]['content'][:len(original)])
        app.finish_turn(first,keep_for_tools=False)
        app,control=self.app()
        body=self.fixture();tools=legacy.GatewayTests.bound_tools();body['tools']=tools
        call=legacy.GatewayTests.native_call('synthetic-v3-call','synthetic no execution')
        first=self.prepare(app,body)
        bindings=app.bind_response_tool_calls(first,[call])
        app.finish_turn(first,keep_for_tools=True,tool_call_ids=[call.tool_call_id],tool_call_bindings=bindings)
        pair=[{'role':'assistant','content':None,'tool_calls':[legacy.GatewayTests.wire_call(call)]},
              {'role':'tool','tool_call_id':call.tool_call_id,'content':'{"exitCode":0}'}]
        second=self.prepare(app,{**body,'messages':body['messages']+pair})
        self.assertTrue(second.continuation)
        self.assertEqual(first.payload['messages'],second.payload['messages'][:-2])
        self.assertEqual(pair,second.payload['messages'][-2:])
        self.assertEqual(1,sum(path.endswith('/prepare') for path,_ in control.calls))

    def test_tampered_v3_bundle_does_not_confirm(self):
        control=DataControl();app,_=self.app(control)
        original=control.post
        def tamper(path,payload):
            value=original(path,payload)
            if path.endswith('/prepare'):value['context_bundle']['dynamic_message']['content']='tampered'
            return value
        control.post=tamper
        with self.assertRaises(GatewayError):self.prepare(app,self.fixture())
        self.assertFalse(any(path.endswith('/confirm') for path,_ in control.calls))

    def test_short_term_routing_is_data_not_trailing_system(self):
        # No need to supply private identity: source flags remain host-only data.
        app,control=self.app(short_term_enabled=True)
        turn=self.prepare(app,self.fixture())
        self.assertTrue(turn.session.short_term_allowed)
        self.assertEqual('user',turn.payload['messages'][-1]['role'])
        routing=json.loads(turn.payload['messages'][-1]['content'][-1]['text'].split('\n',1)[1])
        self.assertEqual('stbrain_routing',routing['source'])
        self.assertTrue(routing['content'].startswith('ST_SOURCE_V1 '))
        self.assertFalse(any(str(m.get('content')).startswith('ST_SOURCE_V1 ') for m in turn.payload['messages'] if m['role']=='system'))

