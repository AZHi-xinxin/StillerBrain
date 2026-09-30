from __future__ import annotations

import io
import json
import unittest
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from rikkahub_gateway.server import GatewayApplication, GatewayError, _GatewayHandler, _RequestPerformance
from rikkahub_gateway.tests.test_gateway import FakeControl, config

DONE = b'data: [DONE]\n\n'


def event(delta=None, finish=None, **extras):
    choice = {"index": 0, "delta": delta or {}}
    if finish is not None:
        choice["finish_reason"] = finish
    body = {"choices": [choice], **extras}
    return b'data: ' + json.dumps(body).encode() + b'\n\n'


def usage(prompt=100, completion=3):
    return b'data: ' + json.dumps({"choices": [], "usage": {
        "prompt_tokens": prompt, "completion_tokens": completion,
        "total_tokens": prompt + completion, "prompt_cache_hit_tokens": prompt - 5,
        "prompt_cache_miss_tokens": 5,
    }}).encode() + b'\n\n'


EMPTY = event({"reasoning_content": "first thinking"}) + event({}, "stop") + usage() + DONE
ANSWER = event({"reasoning_content": "second thinking"}) + event({"content": "real answer"}, "stop") + usage(101, 5) + DONE


class TailActionStream(httpx.SyncByteStream):
    def __init__(self, raw, action):
        self.raw, self.action = raw, action
    def __iter__(self):
        yield self.raw
        self.action()


class EmptyRecoveryTests(unittest.TestCase):
    def invoke(self, responses, *, continuation=False, custom_config=None, protected=False,
               tail_action=None, writer=None, with_performance=False):
        calls = []
        holder = {}
        def upstream(request):
            calls.append(request)
            item = responses[len(calls)-1]
            if isinstance(item, Exception):
                raise item
            if isinstance(item, httpx.SyncByteStream):
                return httpx.Response(200, stream=item)
            if isinstance(item, tuple):
                return httpx.Response(item[0], content=item[1])
            if len(calls) == 1 and tail_action:
                return httpx.Response(200, stream=TailActionStream(item, lambda: tail_action(holder['app'])))
            return httpx.Response(200, content=item)
        client = httpx.Client(transport=httpx.MockTransport(upstream))
        self.addCleanup(client.close)
        control = FakeControl()
        app = GatewayApplication(custom_config or config(), control=control, upstream=client)
        holder['app'] = app
        payload = {"model": config().public_model, "stream": True,
                   "messages": [{"role": "user", "content": "capability-1" if protected else "synthetic input"}],
                   "tools": [{"type": "function", "function": {"name": "host_lookup", "parameters": {"type": "object", "properties": {}}}}]}
        prepared = app.prepare_turn(payload, {})
        if continuation:
            app.finish_turn(prepared, keep_for_tools=True, tool_call_ids=['prior-call'])
            payload['messages'].extend([
                {"role": "assistant", "tool_calls": [{"id": "prior-call", "type": "function", "function": {"name": "host_lookup", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "prior-call", "content": "already executed receipt"},
            ])
            prepared = app.prepare_turn(payload, {})
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=app)
        handler.wfile = writer or io.BytesIO()
        headers = []
        handler.send_response = headers.append
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.close_connection = False
        if with_performance:
            handler._request_performance = _RequestPerformance()
        code = None
        with patch('rikkahub_gateway.server._PERFORMANCE_LOGGER.info') as logger:
            try:
                handler._proxy_stream(prepared)
            except GatewayError as e:
                code = e.code
        raw = handler.wfile.getvalue()
        data = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b'data: {')]
        errors = [x['error']['code'] for x in data if 'error' in x]
        code = code or (errors[-1] if errors else None)
        return SimpleNamespace(calls=calls, raw=raw, data=data, code=code, app=app,
                               headers=headers, control=control, handler=handler,
                               logs=[json.loads(x.args[0]) for x in logger.call_args_list])

    def test_empty_then_answer_same_request_once_headers_done_and_reasoning_preserved(self):
        r = self.invoke([EMPTY, ANSWER], continuation=True)
        self.assertIsNone(r.code)
        self.assertEqual(2, len(r.calls))
        self.assertEqual(r.calls[0].content, r.calls[1].content)
        self.assertEqual(1, sum(path == '/v1/host/wakes' for path, _ in r.control.calls))
        self.assertEqual([200], r.headers)
        self.assertEqual(1, r.raw.count(DONE))
        self.assertIn(b'first thinking', r.raw)
        self.assertIn(b'second thinking', r.raw)
        self.assertIn(b'real answer', r.raw)
        self.assertIsNone(r.app._current_session)
        final_usage = next(x['usage'] for x in r.data if 'usage' in x)
        self.assertEqual({'prompt_tokens': 101, 'completion_tokens': 5, 'total_tokens': 106,
                          'prompt_cache_hit_tokens': 96, 'prompt_cache_miss_tokens': 5}, final_usage)
        metadata=next(x['st_gateway_retry'] for x in r.data if 'st_gateway_retry' in x)
        self.assertEqual({'prompt_tokens': 201, 'completion_tokens': 8, 'total_tokens': 209,
                          'prompt_cache_hit_tokens': 191, 'prompt_cache_miss_tokens': 10}, metadata['usage_total'])
        self.assertEqual(2, r.logs[-1]['attempt_count'])

    def test_double_empty_stops_after_two_model_requests(self):
        r = self.invoke([EMPTY, EMPTY, ANSWER])
        self.assertEqual('upstream_empty_completion', r.code)
        self.assertEqual(2, len(r.calls))
        self.assertEqual(1, r.raw.count(DONE))
        self.assertIsNone(r.app._current_session)

    def test_complete_answer_unchanged_no_retry(self):
        r = self.invoke([ANSWER])
        self.assertIsNone(r.code)
        self.assertEqual(1, len(r.calls))
        self.assertNotIn(b'st_gateway_retry', r.raw)

    def test_no_retry_for_unknown_choice_output(self):
        r = self.invoke([event({'reasoning_content':'think', 'future_output':'unknown'}, 'stop') + DONE])
        self.assertEqual('upstream_empty_completion', r.code)
        self.assertEqual(1, len(r.calls))

    def test_no_retry_for_partial_content_or_refusal_or_audio(self):
        for value in [{'content':'partial'}, {'refusal':'no'}, {'audio':{'id':'clip'}}]:
            with self.subTest(value=value):
                r = self.invoke([event({'reasoning_content':'think', **value})])
                self.assertEqual('upstream_incomplete_stream', r.code)
                self.assertEqual(1, len(r.calls))

    def test_no_retry_for_partial_tool_call(self):
        r = self.invoke([event({'reasoning_content':'think', 'tool_calls':[{'index':0, 'function':{'arguments':'{'}}]})])
        self.assertEqual('upstream_incomplete_stream', r.code)
        self.assertEqual(1, len(r.calls))

    def test_no_retry_for_length_unknown_finish_and_no_done(self):
        for finish, done in [('length',DONE), ('content_filter',DONE), ('future',DONE), ('stop',b'')]:
            with self.subTest(finish=finish, done=bool(done)):
                r = self.invoke([event({'reasoning_content':'think'},finish)+done])
                self.assertEqual(1,len(r.calls))
                self.assertIsNotNone(r.code)

    def test_no_retry_after_explicit_provider_error(self):
        r = self.invoke([event({'reasoning_content':'think'})+b'data: {"error":{"code":"provider_error"}}\n\n'+DONE])
        self.assertEqual(1,len(r.calls))
        self.assertEqual('provider_error',r.code)

    def test_no_retry_after_invalid_trailing_output(self):
        r=self.invoke([EMPTY+event({'content':'after done'})])
        self.assertEqual('upstream_invalid_stream',r.code)
        self.assertEqual(1,len(r.calls))

    def test_no_retry_after_network_timeout(self):
        r=self.invoke([httpx.ReadTimeout('synthetic')])
        self.assertEqual('upstream_unavailable',r.code)
        self.assertEqual(1,len(r.calls))

    def test_second_attempt_http_error_is_single_sse_not_second_http_response(self):
        r=self.invoke([EMPTY,(503,b'{"error":{"message":"do not leak this"}}')])
        self.assertEqual('upstream_error',r.code)
        self.assertEqual(2,len(r.calls))
        self.assertEqual([200],r.headers)
        self.assertNotIn(b'do not leak this',r.raw)

    def test_second_timeout_no_third_request(self):
        r=self.invoke([EMPTY,httpx.ReadTimeout('synthetic')])
        self.assertEqual('upstream_unavailable',r.code)
        self.assertEqual(2,len(r.calls))

    def test_session_replaced_before_retry_never_calls_model_again(self):
        r=self.invoke([EMPTY,ANSWER],tail_action=lambda app:setattr(app,'_current_session',None))
        self.assertEqual('tool_continuation_context_lost',r.code)
        self.assertEqual(1,len(r.calls))

    def test_generation_advanced_before_retry_never_calls_model_again(self):
        def advance(app): app._current_session.request_generation += 1
        r=self.invoke([EMPTY,ANSWER],tail_action=advance)
        self.assertEqual(1,len(r.calls))

    def test_deadline_shared_no_fresh_timeout_budget(self):
        now=[10.0]
        with patch('rikkahub_gateway.server.time.monotonic',side_effect=lambda:now[0]):
            r=self.invoke([EMPTY,ANSWER],custom_config=replace(config(),timeout_seconds=5),tail_action=lambda app:now.__setitem__(0,16.0))
        self.assertEqual('upstream_unavailable',r.code)
        self.assertEqual(1,len(r.calls))

    def test_combined_stream_byte_limit_not_reset(self):
        first=event({'reasoning_content':'t'*500})+event({},'stop')+DONE
        second=event({'content':'a'*500},'stop')+DONE
        r=self.invoke([first,second],custom_config=replace(config(),max_stream_bytes=1024))
        self.assertEqual('upstream_stream_limit_reached',r.code)
        self.assertEqual(2,len(r.calls))

    def test_client_disconnected_before_retry_never_second_request(self):
        class Writer(io.BytesIO):
            def write(self, data):
                if data.startswith(b': st-gateway'): raise BrokenPipeError()
                return super().write(data)
        r=self.invoke([EMPTY,ANSWER],writer=Writer())
        self.assertEqual(1,len(r.calls))
        self.assertIsNone(r.app._current_session)

    def test_missing_first_usage_keeps_actual_context_but_total_unknown(self):
        first=event({'reasoning_content':'think'})+event({},'stop')+DONE
        r=self.invoke([first,ANSWER],with_performance=True)
        self.assertIsNone(r.code)
        self.assertEqual(101,next(x['usage']['prompt_tokens'] for x in r.data if 'usage' in x))
        self.assertFalse(r.logs[-1]['usage_complete'])
        self.assertIsNone(r.logs[-1]['usage_total'])
        self.assertEqual(101,r.handler._request_performance.prompt_tokens)

    def test_standard_performance_is_last_attempt_billing_log_is_aggregate(self):
        r=self.invoke([EMPTY,ANSWER],with_performance=True)
        self.assertEqual(101,r.handler._request_performance.prompt_tokens)
        self.assertEqual(5,r.handler._request_performance.completion_tokens)
        self.assertEqual(201,r.logs[-1]['usage_total']['prompt_tokens'])

    def test_protected_history_retry_preserves_all_or_nothing_visibility(self):
        r=self.invoke([EMPTY,ANSWER],protected=True)
        self.assertIsNone(r.code)
        self.assertEqual(2,len(r.calls))
        self.assertNotIn(b'first thinking',r.raw)
        self.assertIn(b'real answer',r.raw)

    def test_second_tool_batch_binds_once_not_reexecuted(self):
        second=event({'tool_calls':[{'index':0,'id':'new-call','type':'function','function':{'name':'host_lookup','arguments':'{}'}}]},'tool_calls')+DONE
        r=self.invoke([EMPTY,second],continuation=True)
        self.assertIsNone(r.code)
        self.assertEqual(2,len(r.calls))
        self.assertEqual({'new-call'},r.app._current_session.expected_tool_call_ids)
        self.assertEqual(1,r.raw.count(b'new-call'))

    def test_unknown_top_level_output_and_nontext_reasoning_never_retry(self):
        for raw in [event({'reasoning_content':'think'},'stop',future_output='unknown')+DONE,
                    event({'reasoning_content':['unknown']},'stop')+DONE]:
            r=self.invoke([raw])
            self.assertEqual(1,len(r.calls))

    def test_multiple_choices_and_3xx_never_retry(self):
        two=event({'reasoning_content':'think'},'stop')+b'data: {"choices":[{"index":1,"delta":{"reasoning_content":"other"},"finish_reason":"stop"}]}\n\n'+DONE
        self.assertEqual(1,len(self.invoke([two]).calls))
        self.assertEqual(1,len(self.invoke([(302,EMPTY)]).calls))

    def test_actual_blocking_second_read_is_closed_by_shared_deadline(self):
        class BlockingStream(httpx.SyncByteStream):
            def __init__(self): self.closed=threading.Event()
            def __iter__(self):
                self.closed.wait(1.5)
                if False: yield b''
            def close(self): self.closed.set()
        stream=BlockingStream()
        started=time.perf_counter()
        r=self.invoke([EMPTY,stream],custom_config=replace(config(),timeout_seconds=0.03))
        self.assertEqual(2,len(r.calls))
        self.assertTrue(stream.closed.is_set())
        self.assertLess(time.perf_counter()-started,1.0)
        self.assertEqual('upstream_unavailable',r.code)

    def test_second_timeout_is_remaining_not_fresh_budget(self):
        now=[10.0]
        with patch('rikkahub_gateway.server.time.monotonic',side_effect=lambda:now[0]):
            r=self.invoke([EMPTY,ANSWER],custom_config=replace(config(),timeout_seconds=5),tail_action=lambda app:now.__setitem__(0,14.0))
        self.assertEqual(5,r.calls[0].extensions['timeout']['read'])
        self.assertEqual(1,r.calls[1].extensions['timeout']['read'])

    def test_after_failed_tool_continuation_new_user_can_start_without_replaying_tool(self):
        r=self.invoke([EMPTY,EMPTY],continuation=True)
        wire=json.loads(r.calls[-1].content)
        # Remove only the gateway injection; the client's exact historical tool
        # declaration and receipt survive before a genuinely new user event.
        client_history=wire['messages'][1:]
        client_history.append({'role':'assistant','reasoning_content':'failed thought','content':''})
        client_history.append({'role':'user','content':'new independent event'})
        prepared=r.app.prepare_turn({'model':config().public_model,'stream':True,'messages':client_history,'tools':wire['tools']},{})
        self.assertFalse(prepared.continuation)
        self.assertEqual(2,r.control.seq)
        self.assertEqual(1,sum(m.get('role')=='tool' for m in prepared.payload['messages']))

    def test_corrupt_first_usage_never_fabricates_aggregate(self):
        corrupt=event({'reasoning_content':'think'})+event({},'stop',usage={'prompt_tokens':True,'completion_tokens':3,'total_tokens':103})+DONE
        r=self.invoke([corrupt,ANSWER])
        self.assertFalse(r.logs[-1]['usage_complete'])
        self.assertEqual(101,next(x['usage']['prompt_tokens'] for x in r.data if 'usage' in x))

    def test_unknown_terminal_choice_without_delta_never_retry(self):
        raw=event({'reasoning_content':'think'})+b'data: {"choices":[{"index":0,"finish_reason":"stop","future_output":"unknown"}]}\n\n'+DONE
        self.assertEqual(1,len(self.invoke([raw]).calls))

    def test_malformed_choice_or_delta_never_retry(self):
        for payload in ({'choices':[None]}, {'choices':{}}, {'choices':[{'index':0,'delta':'unknown'}]}):
            raw=event({'reasoning_content':'think'})+b'data: '+json.dumps(payload).encode()+b'\n\n'+event({},'stop')+DONE
            self.assertEqual(1,len(self.invoke([raw]).calls))

    def test_duplicate_done_in_first_attempt_rejected_without_retry(self):
        r=self.invoke([EMPTY+DONE,ANSWER])
        self.assertEqual('upstream_invalid_stream',r.code)
        self.assertEqual(1,len(r.calls))
        self.assertEqual(1,r.raw.count(DONE))

    def test_duplicate_done_in_second_attempt_returns_only_single_error_terminal(self):
        r=self.invoke([EMPTY,ANSWER+DONE])
        self.assertEqual('upstream_invalid_stream',r.code)
        self.assertEqual(2,len(r.calls))
        self.assertEqual(1,r.raw.count(DONE))
        self.assertEqual(1,len([row for row in r.data if 'error' in row]))
        self.assertNotIn(b'real answer',r.raw)

    def test_protected_short_prefix_is_tracked_across_attempt_and_response_id(self):
        first=event({'reasoning_content':'capab'},id='attempt-one')+event({},'stop')+DONE
        second=event({'reasoning_content':'ility-1'},id='attempt-two')+event({'content':'synthetic answer'},'stop')+DONE
        r=self.invoke([first,second])
        self.assertEqual('upstream_protected_value',r.code)
        self.assertEqual(2,len(r.calls))
        self.assertIn(b'capab',r.raw)
        self.assertNotIn(b'ility-1',r.raw)
        self.assertEqual(1,r.raw.count(DONE))
        self.assertFalse(r.logs[-1]['recovered'])

    def test_protected_prefix_cross_attempt_channel_and_path_is_blocked(self):
        first=event({'reasoning_content':'capab'})+event({},'stop')+DONE
        for continuation in (
            event({'content':'ility-1'},'stop'),
            b'data: {"choices":[{"index":0,"message":{"content":"ility-1"},"finish_reason":"stop"}]}\n\n',
            event({'tool_calls':[{'index':0,'id':'call-new','type':'function','function':{'name':'host_lookup','arguments':'ility-1'}}]},'tool_calls'),
        ):
            with self.subTest(continuation=continuation):
                r=self.invoke([first,continuation+DONE])
                self.assertEqual('upstream_protected_value',r.code)
                self.assertNotIn(b'ility-1',r.raw)
                self.assertEqual(1,r.raw.count(DONE))

    def test_second_provider_error_is_not_reported_as_recovered(self):
        second=b'data: {"error":{"code":"provider_error"}}\n\n'+DONE
        r=self.invoke([EMPTY,second])
        self.assertEqual('provider_error',r.code)
        self.assertEqual(2,len(r.calls))
        self.assertFalse(r.logs[-1]['recovered'])
        self.assertEqual(1,r.raw.count(DONE))

    def test_usage_null_does_not_delay_first_or_second_attempt_progress(self):
        for retry in (False,True):
            with self.subTest(retry=retry):
                writer=io.BytesIO()
                checks=[]
                class ObservedStream(httpx.SyncByteStream):
                    def __iter__(self):
                        yield event({'content':'synthetic progress'},usage=None)
                        checks.append(writer.getvalue())
                        yield event({},'stop')+usage()+DONE
                r=self.invoke(([EMPTY] if retry else [])+[ObservedStream()],writer=writer)
                self.assertIsNone(r.code)
                self.assertEqual(1,len(checks))
                self.assertIn(b'synthetic progress',checks[0])
                self.assertNotIn(b'st_gateway_retry',checks[0])

if __name__ == '__main__': unittest.main()
