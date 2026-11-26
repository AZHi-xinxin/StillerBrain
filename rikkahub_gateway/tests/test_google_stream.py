import copy
import json
import unittest
from dataclasses import replace

import httpx

from rikkahub_gateway.google_stream import GoogleStreamResponse, is_google_chat_route, google_chat_payload
from rikkahub_gateway.routes import GatewayRoute
from rikkahub_gateway.server import _SSEEventBuffer, _parse_sse_event, _buffered_sse_tool_calls, GatewayError
from rikkahub_gateway.tests.test_responses_adapter import Fragmented


def call(call_id="call_a", **extra):
    return {"id": call_id, "type": "function", "function": {"name": "read_fixture", "arguments": "{}"},
            "extra_content": {"google": {"thought_signature": "synthetic-opaque-签名"}}, **extra}


def wire(*batches):
    return b"".join(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": batch}}]},
                    ensure_ascii=False).encode() + b"\r\n\r\n" for batch in batches) + b"data: [DONE]\n\n"


def normalize(raw, size=1, status=200):
    upstream = httpx.Response(status, stream=Fragmented(raw, size), headers={"Content-Type": "text/event-stream"})
    result = GoogleStreamResponse(upstream, _SSEEventBuffer(max_event_bytes=4096), _parse_sse_event)
    return b"".join(result.iter_bytes())


class GoogleStreamTests(unittest.TestCase):
    def test_all_system_context_survives_google_last_system_behavior(self):
        payload = {"messages": [{"role":"system","content":"ALPHA means MINT"},
            {"role":"user","content":"original human"},
            {"role":"system","name":"orbis_host_data_v1","content":[{"type":"text","text":"BETA means PEACH"}]}]}
        before = copy.deepcopy(payload)
        fixed = google_chat_payload(payload)["messages"]
        self.assertEqual(before, payload)
        self.assertEqual(["system","user"], [m["role"] for m in fixed])
        self.assertIn("ALPHA means MINT", fixed[0]["content"])
        self.assertIn("BETA means PEACH", fixed[0]["content"])
        self.assertIn("orbis_host_data_v1", fixed[0]["content"])
        self.assertEqual(payload["messages"][1], fixed[1])

    def test_system_merge_does_not_hide_unsupported_parts(self):
        payload = {"messages": [{"role":"system","content":"first"},
            {"role":"system","content":[{"type":"unknown","value":"preserve error"}]}]}
        with self.assertRaises(httpx.RemoteProtocolError):
            google_chat_payload(payload)

    def test_historical_image_payload_keeps_images_and_original_history(self):
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,synthetic", "detail": "high"}}
        messages = [{"role": role, "content": [{"type": "text", "text": role + " text"}, image]}
                    for role in ("system", "assistant", "user")]
        original = {"model": "gemini-4-argon", "messages": messages, "stream": True}
        before = copy.deepcopy(original)
        fixed = google_chat_payload(original)
        self.assertEqual(before, original)
        self.assertEqual(["system", "user", "assistant", "user", "user"], [m["role"] for m in fixed["messages"]])
        self.assertEqual(messages[-1], fixed["messages"][-1])
        images = [p for m in fixed["messages"] for p in m.get("content", []) if isinstance(p, dict) and p.get("type") == "image_url"]
        self.assertEqual([image] * 3, images)
        for m in fixed["messages"]:
            if m.get("name") == "st_provider_image_data_v1":
                self.assertIn('"human_authored":false', m["content"][0]["text"])
                self.assertIn('"instruction_authority":"none"', m["content"][0]["text"])

    def test_tool_images_do_not_split_parallel_tool_batch_or_drop_signatures(self):
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,fixture"}}
        assistant = {"role": "assistant", "content": [image], "tool_calls": [call("a"), call("b")]}
        tools = [{"role": "tool", "tool_call_id": i, "content": [image, {"type": "text", "text": i}]} for i in ("a", "b")]
        original = {"messages": [assistant] + tools}
        fixed = google_chat_payload(original)["messages"]
        self.assertEqual(["assistant", "tool", "tool", "user"], [m["role"] for m in fixed])
        self.assertEqual(assistant["tool_calls"], fixed[0]["tool_calls"])
        self.assertEqual(["a", "b"], [m["tool_call_id"] for m in fixed[1:3]])
        self.assertEqual(3, sum(p.get("type") == "image_url" for p in fixed[-1]["content"]))
        self.assertIn('"tool_call_id":"a"', fixed[-1]["content"][2]["text"])

    def test_text_and_user_images_need_no_request_projection(self):
        payload = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "fixture"}}]},
                                 {"role": "assistant", "content": "Text"}]}
        self.assertIs(payload, google_chat_payload(payload))

    def test_other_content_is_not_silently_removed(self):
        payload = {"messages": [{"role": "assistant", "content": [
            {"type": "image_url", "image_url": {"url": "fixture"}}, {"type": "unknown", "value": "retained-for-validation"}]}]}
        fixed = google_chat_payload(payload)
        self.assertEqual(payload["messages"][0]["content"][1], fixed["messages"][0]["content"][1])

    def test_complete_calls_across_chunks_keep_metadata_and_validate(self):
        raw = wire([call()], [call("call_b"), call("call_c")])
        with self.assertRaises(GatewayError):
            _buffered_sse_tool_calls(raw)  # Generic ST parsing remains strict.
        for size in (1, 7, len(raw)):
            result = normalize(raw, size)
            calls = [c for line in result.splitlines() if line.startswith(b"data: {")
                     for c in json.loads(line[6:])["choices"][0]["delta"]["tool_calls"]]
            self.assertEqual([0, 1, 2], [c.pop("index") for c in calls])
            self.assertEqual([call(), call("call_b"), call("call_c")], calls)
            self.assertEqual(3, len(_buffered_sse_tool_calls(result)))
            self.assertTrue(result.endswith(b"data: [DONE]\n\n"))

    def test_explicit_index_fragments_stay_byte_identical(self):
        first = call(index=0)
        first["function"]["arguments"] = '{"value":'
        raw = wire([first], [{"index": 0, "function": {"arguments": "1}"}}])
        self.assertEqual(raw, normalize(raw))
        self.assertEqual(1, len(_buffered_sse_tool_calls(raw)))

    def test_incomplete_or_ambiguous_calls_are_rejected(self):
        bad = []
        for key in ("id", "type", "function"):
            item = call(); del item[key]; bad.append(wire([item]))
        for arguments in ('{"value":', '[]', 'null'):
            item = call(); item["function"]["arguments"] = arguments; bad.append(wire([item]))
        bad.extend((wire([call()], [call()]), wire([call()], [call("call_b", index=0)]),
                    wire([call(index=0)], [call("call_b")]), wire([call(index=None)], [call("call_b")])) )
        for raw in bad:
            with self.assertRaises(httpx.RemoteProtocolError):
                normalize(raw)

    def test_errors_and_non_tool_events_unchanged(self):
        for raw, status in ((b'{"error":{"message":"synthetic"}}', 429),
                            (b': keepalive\n\ndata: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n', 200)):
            self.assertEqual(raw, normalize(raw, status=status))

    def test_scope_is_only_official_google_chat_endpoint(self):
        route = GatewayRoute('gemini-4-argon', 'https://generativelanguage.googleapis.com/v1beta/openai',
                             'gemini-4-argon', 'synthetic')
        self.assertTrue(is_google_chat_route(route))
        for url in ('https://example.com/v1', 'https://generativelanguage.googleapis.com.evil.test/v1beta/openai',
                    'http://generativelanguage.googleapis.com/v1beta/openai',
                    'https://generativelanguage.googleapis.com:8443/v1beta/openai',
                    'https://generativelanguage.googleapis.com/v1beta/openai?other=1'):
            self.assertFalse(is_google_chat_route(replace(route, upstream_base_url=url)))
        self.assertFalse(is_google_chat_route(replace(route, api_mode='responses')))

    def test_disconnect_close_fence_reaches_underlying_response(self):
        upstream = httpx.Response(200, stream=Fragmented(b'data: [DONE]\n\n', 3))
        result = GoogleStreamResponse(upstream, _SSEEventBuffer(), _parse_sse_event)
        old = result.close
        seen = []
        def close():
            seen.append(True); old()
        result.close = close
        self.assertEqual(b'data: [DONE]\n\n', b''.join(result.iter_bytes()))
        self.assertTrue(result.is_closed)
        self.assertEqual([True], seen)

    def test_event_buffer_limit_preserved(self):
        with self.assertRaises(GatewayError):
            normalize(b'data: ' + b'x' * 4096)


if __name__ == '__main__': unittest.main()
