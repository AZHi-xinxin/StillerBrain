"""Prefix reuse across changing host data without changing source messages."""
import copy
import unittest

from rikkahub_gateway.responses_adapter import ResponsesAdapter, cacheable_history_messages


def plain_input(request):
    result = copy.deepcopy(request['input'])
    for item in result:
        for field in ('content', 'output'):
            parts = item.get(field)
            if isinstance(parts, list):
                for part in parts:
                    part.pop('prompt_cache_breakpoint', None)
                if len(parts) == 1 and parts[0].get('type') == 'input_text':
                    item[field] = parts[0]['text']
    return result


class ResponsesCacheBoundaryTests(unittest.TestCase):
    def fixture(self, counter=1):
        return {'model': 'gpt-6-astra', 'messages': [
            {'role': 'system', 'content': 'fixed rules'},
            {'role': 'user', 'content': 'old human'},
            {'role': 'assistant', 'content': 'old answer'},
            {'role': 'system', 'name': 'orbis_host_data_v1', 'content': f'current data {counter}'},
            {'role': 'user', 'content': f'question {counter}'},
            {'role': 'system', 'content': f'ST dynamic memory {counter}'},
        ]}

    def test_unchanged_history_is_checkpointed_before_temporary_system(self):
        first = self.fixture()
        original = copy.deepcopy(first)
        a = ResponsesAdapter(first).request()
        second = self.fixture(2)
        second['messages'][3:3] = [first['messages'][4], {'role': 'assistant', 'content': 'answer 1'}]
        b = ResponsesAdapter(second).request()
        self.assertEqual(original, first)
        self.assertEqual(a['input'][:3], b['input'][:3])
        self.assertEqual({'mode': 'explicit'}, a['input'][1]['content'][0]['prompt_cache_breakpoint'])
        self.assertEqual('current data 1', a['input'][3]['content'])
        self.assertEqual('current data 2', b['input'][5]['content'])
        self.assertEqual('ST dynamic memory 2', b['input'][-1]['content'])
        self.assertEqual(first['messages'][0]['content'], plain_input(a)[0]['content'])

    def test_preserves_every_role_text_image_and_call_id(self):
        p = self.fixture()
        p['messages'][1:3] = [
            {'role': 'user', 'content': [{'type': 'text', 'text': 'question'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,test', 'detail': 'high'}}]},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call_example', 'type': 'function',
                'function': {'name': 'read_value', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'call_example', 'content': '{"value":7}'},
        ]
        marked = ResponsesAdapter(p).request()
        legacy = ResponsesAdapter({**p, 'model': 'other-model'}).request()
        self.assertEqual(plain_input(legacy), plain_input(marked))
        image = marked['input'][1]['content'][-1]
        self.assertEqual('input_image', image['type'])
        self.assertEqual('high', image['detail'])
        output = next(i for i in marked['input'] if i.get('type') == 'function_call_output')
        self.assertEqual('call_example', output['call_id'])
        self.assertEqual('{"value":7}', output['output'][0]['text'])

    def test_maximum_three_explicit_markers_and_old_models_unchanged(self):
        p = {'model': 'gpt-5.6-sol', 'messages': [{'role': 'user', 'content': str(i)} for i in range(70)]}
        self.assertEqual({67, 68, 69}, cacheable_history_messages(p))
        for model in ['gpt-5.4', 'deepseek-v4-pro', 'custom-gpt-6-astra']:
            with self.subTest(model=model):
                q = {**p, 'model': model}
                self.assertEqual(set(), cacheable_history_messages(q))
                self.assertEqual(p['messages'], ResponsesAdapter(q).request()['input'])

    def test_absent_user_does_not_mark_dynamic_system_only_wake(self):
        p = {'model': 'gpt-6-astra', 'messages': [{'role': 'system', 'content': 'current event'}]}
        self.assertEqual(p['messages'], ResponsesAdapter(p).request()['input'])
