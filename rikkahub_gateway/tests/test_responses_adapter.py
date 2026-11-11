"""Synthetic Responses wire/turn tests; no real credentials or business DB."""
import copy
import io
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

import httpx

from rikkahub_gateway.responses_adapter import ResponsesAdapter, ResponsesTurnState, ResponsesProtocolError, encode
from rikkahub_gateway.routes import GatewayRoute, routes_from_env, validate_route
from rikkahub_gateway.server import GatewayApplication, _GatewayHandler
from rikkahub_gateway.tests import test_gateway as f


def response(text="完成", *, calls=False, status="completed"):
    output = [{"type": "reasoning", "id": "rs_test", "encrypted_content": "opaque-synthetic-only",
               "summary": [{"type": "summary_text", "text": "核对"}]}]
    if text:
        output.append({"type": "message", "id": "msg_test", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": text, "annotations": []}]})
    if calls:
        output.append({"type": "function_call", "id": "fc_test", "call_id": "call_test", "status": "completed",
                       "name": "workspace_shell", "arguments": '{"command":"synthetic-not-run"}'})
    return {"id": "resp_test", "status": status, "output": output, "created_at": 1,
            "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30,
                      "input_tokens_details": {"cached_tokens": 5}, "output_tokens_details": {"reasoning_tokens": 7}}}


def events(body, *, terminal=True):
    rows = [{"type": "response.created", "response": {"id": body["id"]}},
            {"type": "response.reasoning_summary_text.delta", "delta": "核对"}]
    text = "".join(c["text"] for i in body["output"] if i["type"] == "message" for c in i["content"])
    rows.extend({"type": "response.output_text.delta", "delta": c} for c in text)
    if terminal:
        rows.append({"type": "response." + body["status"], "response": body})
    return b"".join(b"event: " + row["type"].encode() + b"\r\ndata: " + encode(row) + b"\r\n\r\n" for row in rows)


class Fragmented(httpx.SyncByteStream):
    def __init__(self, wire, size):
        self.wire, self.size = wire, size

    def __iter__(self):
        for offset in range(0, len(self.wire), self.size):
            yield self.wire[offset:offset + self.size]


class ResponsesTests(unittest.TestCase):
    def payload(self, **extra):
        return {"model": "gpt-6-astra", "messages": [{"role": "user", "content": "测试"}], **extra}

    def test_routes_explicit_protocol_and_legacy_default(self):
        env = {"STBRAIN_UPSTREAM_ROUTE_OPENAI_API_KEY": "synthetic-key",
               "STBRAIN_GATEWAY_ROUTES_JSON": json.dumps([{"public_model": "st-astra", "upstream_model": "gpt-6-astra",
                 "upstream_base_url": "https://api.openai.com/v1", "api_key_env": "STBRAIN_UPSTREAM_ROUTE_OPENAI_API_KEY",
                 "api_mode": "responses"}])}
        route = routes_from_env(env)[0]
        self.assertEqual("https://api.openai.com/v1/responses", route.completion_url)
        self.assertNotIn("synthetic-key", repr(route))
        legacy = replace(route, api_mode="chat_completions")
        self.assertTrue(legacy.completion_url.endswith("/chat/completions"))
        env["STBRAIN_GATEWAY_ROUTES_JSON"] = env["STBRAIN_GATEWAY_ROUTES_JSON"].replace('"responses"', '"typo"')
        with self.assertRaises(ValueError):
            routes_from_env(env)

    def test_request_preserves_context_images_and_optional_schema(self):
        messages = [{"role": "system", "content": "固定记忆"}, {"role": "system", "content": "本轮身体资料"},
                    {"role": "user", "content": [{"type": "text", "text": "原用户文字"},
                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,synthetic", "detail": "high"}}]}]
        p = self.payload(messages=messages, tools=f.GatewayTests.bound_tools(), temperature=.7, max_tokens=2048)
        before = copy.deepcopy(p)
        sent = ResponsesAdapter(p).request()
        self.assertEqual(before, p)
        self.assertFalse(sent["store"])
        self.assertEqual("medium", sent["reasoning"]["effort"])
        self.assertEqual(2048, sent["max_output_tokens"])
        self.assertNotIn("temperature", sent)
        self.assertEqual(["固定记忆", "本轮身体资料"], [i["content"] for i in sent["input"][:2]])
        self.assertEqual("input_image", sent["input"][-1]["content"][-1]["type"])
        self.assertFalse(sent["tools"][0]["strict"])

    def test_tool_name_mapping_and_tool_choice_round_trip(self):
        name = "mcp.long." + "x" * 90
        tools = f.GatewayTests.bound_tools(); tools[0]["function"]["name"] = name
        adapter = ResponsesAdapter(self.payload(tools=tools, tool_choice={"type": "function", "function": {"name": name}}))
        request = adapter.request(); alias = request["tools"][0]["name"]
        self.assertLessEqual(len(alias), 64)
        self.assertEqual(alias, request["tool_choice"]["name"])
        body = response(calls=True); body["output"][-1]["name"] = alias
        self.assertEqual(name, adapter.completion(body)["choices"][0]["message"]["tool_calls"][0]["function"]["name"])

    def test_encrypted_reasoning_retained_only_for_matching_own_turn(self):
        state = ResponsesTurnState()
        first = ResponsesAdapter(self.payload(), state)
        completion = first.completion(response(calls=True))
        assistant = completion["choices"][0]["message"]
        self.assertNotIn("opaque-synthetic", json.dumps(completion))
        history = [*self.payload()["messages"], assistant,
                   {"role": "tool", "tool_call_id": "call_test", "content": '{"ok":true}'}]
        p = self.payload(messages=history)
        sent = ResponsesAdapter(p, state).request()
        self.assertIn("opaque-synthetic-only", json.dumps(sent))
        self.assertEqual(1, sum(i.get("type") == "function_call" for i in sent["input"]))
        self.assertEqual("function_call_output", sent["input"][-1]["type"])
        self.assertNotIn("opaque-synthetic-only", json.dumps(ResponsesAdapter(p).request()))

    def test_stale_tool_arguments_do_not_replay_reasoning(self):
        state = ResponsesTurnState(); a = ResponsesAdapter(self.payload(), state)
        assistant = a.completion(response(calls=True))["choices"][0]["message"]
        assistant["tool_calls"][0]["function"]["arguments"] = '{"command":"changed"}'
        sent = ResponsesAdapter(self.payload(messages=[assistant]), state).request()
        self.assertNotIn("opaque-synthetic-only", json.dumps(sent))

    def test_json_usage_and_finish_reasons(self):
        for calls, status, finish in [(False, "completed", "stop"), (True, "completed", "tool_calls"), (False, "incomplete", "length")]:
            body = ResponsesAdapter(self.payload()).completion(response(calls=calls, status=status))
            self.assertEqual(finish, body["choices"][0]["finish_reason"])
            self.assertEqual(5, body["usage"]["prompt_tokens_details"]["cached_tokens"])
            self.assertEqual(7, body["usage"]["completion_tokens_details"]["reasoning_tokens"])
        with self.assertRaises(ResponsesProtocolError):
            ResponsesAdapter(self.payload()).completion(response(calls=True, status="incomplete"))

    def test_fragmented_sse_keeps_text_summary_calls_and_usage(self):
        for size in (1, 7, 4096):
            adapter = ResponsesAdapter(self.payload(stream=True)); adapter.request()
            raw = httpx.Response(200, stream=Fragmented(events(response(calls=True)), size))
            wire = b"".join(adapter.wrap(raw).iter_bytes()).decode()
            self.assertEqual(1, wire.count('[DONE]'))
            self.assertEqual(1, wire.count('"call_test"'))
            self.assertIn('"finish_reason":"tool_calls"', wire)
            self.assertIn('"reasoning_content":"核对"', wire)
            self.assertNotIn('opaque-synthetic', wire)

    def test_no_tool_execution_marker_on_interrupted_stream(self):
        raw = httpx.Response(200, stream=Fragmented(events(response(calls=True), terminal=False), 11))
        iterator = ResponsesAdapter(self.payload(stream=True)).wrap(raw).iter_bytes()
        received = []
        with self.assertRaises(ResponsesProtocolError):
            for part in iterator:
                received.append(part)
        self.assertNotIn(b'tool_calls', b''.join(received))
        self.assertNotIn(b'[DONE]', b''.join(received))

    def test_provider_errors_remain_errors(self):
        wire = b'data: {"type":"response.failed","response":{"error":{"code":"rate_limit_exceeded","message":"try later"}}}\n\n'
        raw = httpx.Response(200, stream=Fragmented(wire, 8))
        result = b''.join(ResponsesAdapter(self.payload(stream=True)).wrap(raw).iter_bytes())
        self.assertIn(b'rate_limit_exceeded', result)
        self.assertNotIn(b'finish_reason', result)

    def test_proxy_is_operator_owned_loopback_only(self):
        base = GatewayRoute('st-astra', 'https://api.openai.com/v1', 'gpt-6-astra', 'synthetic-key', api_mode='responses')
        validate_route(replace(base, proxy_url='http://127.0.0.1:18908'))
        for url in ('http://external.invalid:18908', 'http://user:password@127.0.0.1:18908', 'http://127.0.0.1:18908/path'):
            with self.assertRaises(ValueError):
                validate_route(replace(base, proxy_url=url))

    def test_disconnect_close_fence_reaches_original_upstream(self):
        raw = httpx.Response(200, stream=Fragmented(events(response()), 13))
        wrapped = ResponsesAdapter(self.payload(stream=True)).wrap(raw)
        before_close = []
        original = wrapped.close
        def fence():
            before_close.append(not raw.is_closed)
            original()
        wrapped.close = fence
        list(wrapped.iter_bytes())
        wrapped.close()
        self.assertTrue(before_close[0])
        self.assertTrue(raw.is_closed)

    def test_malformed_provider_sse_becomes_protocol_error(self):
        raw = httpx.Response(200, stream=Fragmented(b'data: {broken}\n\n', 2))
        with self.assertRaises(ResponsesProtocolError):
            list(ResponsesAdapter(self.payload(stream=True)).wrap(raw).iter_bytes())

    def test_gateway_memory_tools_and_continuation_in_json_and_stream(self):
        for stream in (False, True):
            sent = []
            def upstream(req):
                self.assertEqual('/v1/responses', req.url.path)
                payload = json.loads(req.content); sent.append(payload)
                body = response(text="" if len(sent) == 1 else "完成", calls=len(sent) == 1)
                return (httpx.Response(200, headers={'content-type':'text/event-stream'}, content=events(body))
                        if stream else httpx.Response(200, json=body))
            route = GatewayRoute('st-astra', 'https://api.openai.com/v1', 'gpt-6-astra', 'synthetic-key', api_mode='responses')
            control = f.FakeControl()
            client = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
            app = GatewayApplication(replace(f.config(), extra_routes=(route,)), control=control, upstream=client)
            def post(messages):
                p = {'model':route.public_model, 'messages':messages, 'tools':f.GatewayTests.bound_tools(), 'stream':stream}
                h = object.__new__(_GatewayHandler); h.server = SimpleNamespace(application=app)
                raw = encode(p)
                h.headers = {'Authorization':'Bearer '+app.config.gateway_token,'Content-Length':str(len(raw)),
                             'Content-Type':'application/json',
                             'X-ST-Thread-ID':'synthetic-responses'}
                h.path='/v1/chat/completions'; h.rfile=io.BytesIO(raw); h.wfile=io.BytesIO()
                statuses=[]; h.send_response=statuses.append; h.send_header=lambda *a:None; h.end_headers=lambda:None
                h.do_POST(); self.assertEqual([200],statuses)
                return h.wfile.getvalue()
            try:
                user={'role':'user','content':'合成查询'}
                first=post([user]); self.assertIn(b'call_test',first)
                self.assertIn(f.OPTIONAL_BRAIN_NOTICE,json.dumps(sent[0],ensure_ascii=False))
                self.assertNotIn('opaque-synthetic',first.decode())
                assistant={'role':'assistant','content':'','tool_calls':[f.GatewayTests.wire_call(f.GatewayTests.native_call('call_test','synthetic-not-run'))]}
                final=post([user,assistant,{'role':'tool','tool_call_id':'call_test','content':'synthetic result'}])
                if stream:
                    parts = [json.loads(line[6:]) for line in final.decode().splitlines()
                             if line.startswith('data: {')]
                    self.assertEqual('完成', ''.join(c.get('delta', {}).get('content', '')
                                     for part in parts for c in part.get('choices', [])))
                else:
                    self.assertIn('完成', final.decode())
                self.assertIn('opaque-synthetic-only',json.dumps(sent[1]))
                self.assertEqual(1,control.seq)
            finally:
                app.close_short_term(); client.close()


if __name__ == '__main__':
    unittest.main()
