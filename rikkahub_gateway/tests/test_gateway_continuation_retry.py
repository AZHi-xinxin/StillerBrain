"""Network retries retain settled tools, and replay only complete validated answers."""
import copy
import io
import unittest
from unittest.mock import patch
import httpx
from rikkahub_gateway.server import GatewayError
from rikkahub_gateway.tests import test_gateway_interruption_recovery as fixture

class DisconnectedWriter:
    def write(self, data): raise BrokenPipeError('synthetic disconnect')
    def flush(self): pass

class ContinuationRetryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.AnchoredInterruptionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.app=self.f.app
        _,calls=self.f.arm()
        self.payload=self.f.payload([*self.f.history,*self.f.units(calls)])
        self.turn=self.app.prepare_turn(self.payload,self.f.headers)
        self.initial_calls=copy.deepcopy(self.f.control.calls)

    def test_completed_stream_replayed_without_upstream_or_tools(self):
        requests=self.f.mock_responses([self.f.upstream_body(stream=True)],stream=True)
        self.f.handler(DisconnectedWriter())._proxy_stream(self.turn)
        self.assertTrue(self.app._continuation_retry.ready)
        self.assertEqual(2,len(self.turn.session.host_receipts))
        output=io.BytesIO()
        retried=self.app.prepare_turn(self.payload,self.f.headers)
        self.f.handler(output)._proxy_stream(retried)
        self.assertIn(b'synthetic response',output.getvalue())
        self.assertEqual(1,len(requests))
        self.assertIsNone(self.app._current_session)
        # Even a lost completion acknowledgement replays the same answer.
        again=self.app.prepare_turn(self.payload,self.f.headers)
        other=io.BytesIO();self.f.handler(other)._proxy_stream(again)
        self.assertEqual(output.getvalue(),other.getvalue())
        self.assertEqual(1,len(requests))
        self.assertEqual(sum(p.endswith('/issue') for p,_ in self.initial_calls),sum(p.endswith('/issue') for p,_ in self.f.control.calls))
        self.assertEqual(2,len(again.session.host_receipts))

    def test_completed_json_replayed_without_extra_generation(self):
        requests=self.f.mock_responses([self.f.upstream_body(stream=False)],stream=False)
        self.f.handler(DisconnectedWriter())._proxy_json(self.turn)
        retried=self.app.prepare_turn(self.payload,self.f.headers)
        output=io.BytesIO();self.f.handler(output)._proxy_json(retried)
        self.assertIn(b'synthetic response',output.getvalue())
        self.assertEqual(1,len(requests))

    def test_disconnect_before_completion_regenerates_only_answer(self):
        class Interrupted(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"unfinished"},"finish_reason":null}]}\n\n'
                raise AssertionError('downstream disconnect must stop consumption')
        client=httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,stream=Interrupted())),trust_env=False)
        self.addCleanup(client.close);self.app.upstream=client
        self.f.handler(DisconnectedWriter())._proxy_stream(self.turn)
        self.assertIsNone(self.app._continuation_retry.response)
        requests=self.f.mock_responses([self.f.upstream_body(stream=True)],stream=True)
        retried=self.app.prepare_turn(self.payload,self.f.headers)
        self.assertEqual(self.turn.payload,retried.payload)
        out=io.BytesIO();self.f.handler(out)._proxy_stream(retried)
        self.assertNotIn(b'unfinished',out.getvalue())
        self.assertEqual(1,len(requests))
        self.assertEqual(2,len(retried.session.host_receipts))
        self.assertEqual(sum(p.endswith('/issue') for p,_ in self.initial_calls),sum(p.endswith('/issue') for p,_ in self.f.control.calls))


    def test_new_human_invalidates_retry_and_late_writer(self):
        self.app.park_continuation_retry(self.turn)
        newer=self.app.prepare_turn(self.f.payload([*self.payload['messages'],{'role':'user','content':'new task'}]),self.f.headers)
        self.assertIsNone(self.app._continuation_retry)
        with self.assertRaises(GatewayError):self.app.prepare_turn(self.payload,self.f.headers)
        self.assertFalse(self.app.park_continuation_retry(self.turn))
        self.assertIs(self.app._current_session,newer.session)

    def test_explicit_stop_invalidates_ready_retry(self):
        self.turn.session.client_request_id = 'a' * 32
        self.app.park_continuation_retry(self.turn)
        payload={'model':self.turn.route.public_model,'request_id':'a'*32,
                 'binding':self.app._turn_control_binding(self.turn.session)}
        headers={**self.f.headers,'Authorization':'Bearer '+self.app.config.gateway_token}
        status=self.app.control_turn({k:v for k,v in payload.items() if k!='binding'},headers)
        self.assertTrue(status['can_stop'])
        result=self.app.control_turn(payload,headers,stop=True)
        self.assertEqual('retired',result['state'])
        self.assertIsNone(self.app._continuation_retry)
        with self.assertRaises(GatewayError):self.app.prepare_turn(self.payload,self.f.headers)

    def test_upstream_timeout_preserves_completed_tools(self):
        client=httpx.Client(transport=httpx.MockTransport(
            lambda _: (_ for _ in ()).throw(httpx.ReadTimeout('synthetic timeout'))),trust_env=False)
        self.addCleanup(client.close);self.app.upstream=client
        with self.assertRaises(GatewayError):self.f.handler(io.BytesIO())._proxy_json(self.turn)
        self.assertTrue(self.app._continuation_retry.ready)
        self.f.mock_responses([self.f.upstream_body(stream=False)],stream=False)
        retried=self.app.prepare_turn(self.payload,self.f.headers)
        output=io.BytesIO();self.f.handler(output)._proxy_json(retried)
        self.assertIn(b'synthetic response',output.getvalue())
        self.assertEqual(2,len(retried.session.host_receipts))

    def test_failed_host_finish_releases_lane_but_keeps_retry(self):
        self.turn.session.client_request_id='a'*32
        self.app.park_continuation_retry(self.turn)
        headers={**self.f.headers,'Authorization':'Bearer '+self.app.config.gateway_token}
        payload={'model':self.turn.route.public_model,'request_id':'a'*32}
        status=self.app.control_turn(payload,headers)
        self.assertEqual('waiting_tool_results',status['state'])
        self.assertTrue(status['can_stop'])
        result=self.app.control_turn({**payload,'binding':status['binding'],'terminal_reason':'failed'},headers,finish=True)
        self.assertEqual('retired',result['state'])
        self.assertIsNone(self.app._current_session)
        retried=self.app.prepare_turn(self.payload,self.f.headers)
        self.assertIs(self.app._current_session,retried.session)
        self.f.mock_responses([self.f.upstream_body(stream=False)],stream=False)
        output=io.BytesIO();self.f.handler(output)._proxy_json(retried)
        self.assertIn(b'synthetic response',output.getvalue())
        self.assertEqual(2,len(retried.session.host_receipts))


# Real loopback HTTP test: both sockets disconnect, the same tool-result request
# retries, and a lost final acknowledgement replays without another upstream call.
from dataclasses import replace
from http.client import HTTPConnection
import json
import socket
import time
from rikkahub_gateway.server import GatewayApplication
from rikkahub_gateway.tests import test_gateway_client_disconnect as sockets

class _CountingUpstream(sockets._Upstream):
    def do_POST(self):
        self.server.request_count += 1
        super().do_POST()

class ContinuationSocketTests(unittest.TestCase):
    setUp = sockets.GatewayClientDisconnectTests.setUp
    tearDown = sockets.GatewayClientDisconnectTests.tearDown

    def request(self,payload):
        client=HTTPConnection('127.0.0.1',self.gateway.server_port,timeout=4)
        self.clients.append(client)
        client.request('POST','/v1/chat/completions',json.dumps(payload),self.headers)
        response=client.getresponse()
        self.assertEqual(200,response.status)
        return response

    def test_real_socket_disconnect_and_lost_final_ack(self):
        old=self.app
        self.app=GatewayApplication(replace(old.config,require_execution_binding=True,
            execution_epoch='synthetic-retry'),control=fixture.InterruptionControl())
        self.gateway.application=self.app
        old.upstream.close();old.close_short_term()
        self.upstream.RequestHandlerClass=_CountingUpstream
        self.upstream.request_count=0
        payload={'model':self.app.config.public_model,'stream':True,
                 'messages':[{'role':'user','content':'synthetic request'}],
                 'tools':[fixture.schema('safe_read',{})]}
        self.upstream.block=False;self.upstream.tool=True
        first=self.request(payload).read()
        events=[json.loads(line[6:]) for line in first.splitlines() if line.startswith(b'data: {')]
        calls=[call for event in events for choice in event.get('choices',[]) for call in choice.get('delta',{}).get('tool_calls',[])]
        self.assertEqual(1,len(calls));calls[0].pop('index',None)
        payload['messages'] += [{'role':'assistant','content':None,'tool_calls':calls},
                                {'role':'tool','tool_call_id':calls[0]['id'],'content':'already performed once'}]
        self.upstream.tool=False;self.upstream.block=True;self.upstream.release.clear()
        response=self.request(payload)
        self.assertIn(b'prefix',response.fp.readline())
        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        response.close();self.upstream.release.set()
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            retry=self.app._continuation_retry
            if retry is not None and retry.ready:break
            time.sleep(0.02)
        self.assertTrue(retry.ready)
        self.assertEqual(1,len(retry.prepared.session.host_receipts))
        self.upstream.block=False
        answer=self.request(payload).read()
        self.assertIn(b'done',answer)
        count=self.upstream.request_count
        self.assertEqual(answer,self.request(payload).read())
        self.assertEqual(count,self.upstream.request_count)
        self.assertEqual(1,len(self.app._continuation_retry.prepared.session.host_receipts))
