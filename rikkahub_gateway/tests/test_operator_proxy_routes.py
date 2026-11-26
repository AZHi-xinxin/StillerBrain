"""Operator-specified proxies work for both upstream protocols, without global proxy leakage."""
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx
from rikkahub_gateway.routes import GatewayRoute, validate_route
from rikkahub_gateway.server import GatewayApplication
from rikkahub_gateway.tests.test_gateway import FakeControl, config
from rikkahub_gateway.tests.test_responses_adapter import events, response


class OperatorProxyTests(unittest.TestCase):
    def test_chat_proxy_preserves_request_signatures_and_upstream(self):
        route = GatewayRoute('gemini-4-argon', 'https://generativelanguage.googleapis.com/v1beta/openai',
                             'gemini-4-argon', 'synthetic-google-key', proxy_url='http://127.0.0.1:18912')
        validate_route(route)
        payload = {'model': route.upstream_model, 'stream': True, 'messages': [
            {'role': 'assistant', 'tool_calls': [{'id': 'call_fixture', 'type': 'function',
             'function': {'name': 'read_fixture', 'arguments': '{}'},
             'extra_content': {'google': {'thought_signature': 'synthetic-signature'}}}]},
            {'role': 'tool', 'tool_call_id': 'call_fixture', 'content': 'synthetic-result'}]}
        raw = json.dumps(payload).encode()
        seen = []
        wire = b'data: {"choices":[{"delta":{"content":"ready"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

        def handle(request):
            seen.append(request)
            return httpx.Response(200, content=wire, headers={'Content-Type': 'text/event-stream'})

        direct = httpx.Client(transport=httpx.MockTransport(lambda request: self.fail('wrong direct route')))
        app = GatewayApplication(replace(config(), extra_routes=(route,)), control=FakeControl(), upstream=direct)
        real_client = httpx.Client
        options = []
        def client(**kwargs):
            options.append(kwargs)
            return real_client(transport=httpx.MockTransport(handle), trust_env=False)
        try:
            with patch('rikkahub_gateway.server.httpx.Client', side_effect=client):
                with app.upstream_stream(route, content=raw, timeout=19) as result:
                    self.assertEqual(wire, b''.join(result.iter_bytes()))
            self.assertEqual('http://127.0.0.1:18912', options[0]['proxy'])
            self.assertEqual(19, options[0]['timeout'])
            self.assertFalse(options[0]['trust_env'])
            self.assertFalse(options[0]['follow_redirects'])
            self.assertNotEqual(False, options[0].get('verify', True))
            self.assertEqual(route.completion_url, str(seen[0].url))
            self.assertEqual('Bearer synthetic-google-key', seen[0].headers['Authorization'])
            self.assertEqual(raw, seen[0].content)
        finally:
            app.close_short_term(); direct.close()

    def test_responses_proxy_keeps_response_adapter(self):
        route = GatewayRoute('gpt-test', 'https://api.openai.com/v1', 'gpt-test', 'synthetic-openai-key',
                             api_mode='responses', proxy_url='http://127.0.0.1:18908')
        seen = []
        def handle(request):
            seen.append(request)
            return httpx.Response(200, content=events(response(text='ready')),
                                  headers={'Content-Type': 'text/event-stream'})
        direct = httpx.Client(transport=httpx.MockTransport(lambda r: self.fail('wrong direct route')))
        app = GatewayApplication(replace(config(), extra_routes=(route,)), control=FakeControl(), upstream=direct)
        real_client = httpx.Client
        try:
            with patch('rikkahub_gateway.server.httpx.Client', side_effect=lambda **kw:
                       real_client(transport=httpx.MockTransport(handle), trust_env=False)):
                with app.upstream_stream(route, content=json.dumps({'model':'gpt-test', 'stream':True,
                     'messages':[{'role':'user','content':'synthetic'}]}).encode()) as result:
                    body = b''.join(result.iter_bytes())
            chunks = [json.loads(line[6:]) for line in body.splitlines() if line.startswith(b'data: {')]
            self.assertEqual('ready', ''.join(c.get('delta', {}).get('content', '')
                for chunk in chunks for c in chunk.get('choices', [])))
            self.assertIn(b'[DONE]', body)
            self.assertEqual('https://api.openai.com/v1/responses', str(seen[0].url))
            self.assertIn('input', json.loads(seen[0].content))
        finally:
            app.close_short_term(); direct.close()

    def test_route_without_proxy_uses_existing_transport(self):
        route = GatewayRoute('fixture', 'https://example.com/v1', 'fixture', 'synthetic-key')
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'fixture')))
        app = GatewayApplication(replace(config(), extra_routes=(route,)), control=FakeControl(), upstream=client)
        try:
            with patch('rikkahub_gateway.server.httpx.Client', side_effect=AssertionError('unexpected new transport')):
                with app.upstream_stream(route, content=b'{}') as result:
                    self.assertEqual(b'fixture', b''.join(result.iter_bytes()))
        finally:
            app.close_short_term(); client.close()


if __name__ == '__main__': unittest.main()
