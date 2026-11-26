"""Provider adaptation must not change ST recall/capture or client history."""
import copy
import json
import unittest
from dataclasses import replace

import httpx

from rikkahub_gateway.server import GatewayApplication
from rikkahub_gateway.tests.test_gateway import FakeControl, config


class GoogleGatewaySourceBoundaryTests(unittest.TestCase):
    def test_image_projection_runs_after_source_binding_for_stream_and_json(self):
        for stream in (False, True):
            for official in (False, True):
                with self.subTest(stream=stream, official=official):
                    control = FakeControl()
                    sent = []

                    def respond(request):
                        sent.append(json.loads(request.content))
                        return httpx.Response(200, content=b'data: [DONE]\n\n' if stream else b'{}')

                    client = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)
                    base = ('https://generativelanguage.googleapis.com/v1beta/openai' if official
                            else 'https://other-provider.invalid/v1')
                    app = GatewayApplication(replace(config(), upstream_base_url=base,
                        upstream_model='gemini-4-argon'), control=control, upstream=client)
                    payload = {'model': app.config.public_model, 'stream': stream, 'messages': [
                        {'role': 'system', 'content': 'synthetic client system'},
                        {'role': 'user', 'content': 'synthetic previous question'},
                        {'role': 'assistant', 'content': [
                            {'type': 'text', 'text': 'synthetic previous answer'},
                            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,fixture'}}]},
                        {'role': 'user', 'content': 'synthetic current question'}]}
                    original = copy.deepcopy(payload)
                    try:
                        turn = app.prepare_turn(payload, {'X-ST-Thread-ID': 'synthetic-google-source'})
                        source = next(p['source_frame'] for path, p in control.calls if path.endswith('/prepare'))
                        self.assertEqual('synthetic current question', source['query_text'])
                        self.assertEqual(['synthetic previous answer', 'synthetic current question'],
                                         [item['content'] for item in source['capture_items']])
                        self.assertEqual('/v1/host/context/confirm', control.calls[-1][0])
                        before_wire = copy.deepcopy(turn.payload)
                        before_control = copy.deepcopy(control.calls)
                        with app.upstream_stream(turn.route, content=json.dumps(turn.payload).encode()) as response:
                            b''.join(response.iter_bytes())
                        self.assertEqual(original, payload)
                        self.assertEqual(before_wire, turn.payload)
                        self.assertEqual(before_control, control.calls)
                        self.assertNotIn('st_provider_image_data_v1', json.dumps(source))
                        outgoing = sent[0]['messages']
                        carriers = [m for m in outgoing if m.get('name') == 'st_provider_image_data_v1']
                        if official:
                            self.assertEqual(1, len(carriers))
                            self.assertIn('"human_authored":false', carriers[0]['content'][0]['text'])
                            self.assertEqual(1, sum(m['role'] == 'system' for m in outgoing))
                            self.assertIn('synthetic client system', outgoing[0]['content'])
                            self.assertEqual(original['messages'][-1], outgoing[-1])
                        else:
                            self.assertEqual([], carriers)
                            self.assertEqual(before_wire['messages'], outgoing)
                        app.finish_turn(turn, keep_for_tools=False)
                    finally:
                        app.close_short_term()
                        client.close()


if __name__ == '__main__':
    unittest.main()
