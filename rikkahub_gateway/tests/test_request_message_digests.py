"""Byte-equivalent request-local optimization; synthetic data and fake Control."""
from __future__ import annotations

import copy
import json
import random
import unittest
from dataclasses import replace
from unittest.mock import patch

from rikkahub_gateway import server
from rikkahub_gateway.server import GatewayApplication, _RequestMessageDigests, _digest
from rikkahub_gateway.tests.test_cache_layout import BundleControl
from rikkahub_gateway.tests.test_gateway import FakeControl, config
from rikkahub_gateway.tests import test_gateway_split_tool_adversarial as split_fixtures
from rikkahub_gateway.tests.test_tail_context_v2_gateway import TailControl


class RequestMessageDigestsTests(unittest.TestCase):
    def assert_exact(self, messages, tools):
        original = copy.deepcopy(messages)
        original_tools = copy.deepcopy(tools)
        result = _RequestMessageDigests.build(messages, tools)
        self.assertEqual(tuple(_digest(messages[:i]) for i in range(len(messages)+1)), result.prefixes)
        self.assertEqual(_digest(messages), result.history)
        self.assertEqual(_digest({'messages': messages, 'advertised_tools': tools}), result.source)
        self.assertEqual(original, messages)
        self.assertEqual(original_tools, tools)
        # The snapshot retains only hashes; not message references, JSON or media.
        self.assertEqual({'prefixes', 'source'}, set(vars(result)))
        self.assertTrue(all(len(value) == 64 for value in (*result.prefixes, result.source)))
        return result

    def test_empty_unicode_escapes_nested_values_and_number_format(self):
        self.assert_exact([], {})
        self.assert_exact([
            {'role':'system', 'content':'引号" 换行\n 制表\t NUL\0 😀 café e\u0301 / \\'},
            {'content':None, 'role':'assistant', 'tool_calls':[{'function':{'arguments':'{"z": 1,"a":2}', 'name':'fixture'},'id':'synthetic'}]},
            {'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,QUJDRA==','detail':'auto'}},
                                     {'text':'hello','type':'text'}], 'extra':{'z':[True,False,None,-0.0,1.25,1e-20,10**30],'a':{}}},
        ], {'entries':[{'name':'工具','parameters':{'z':True,'a':[]}}],'catalog_hash':'synthetic'})

    def test_unsupported_non_json_value_still_raises(self):
        with self.assertRaises(TypeError):
            _RequestMessageDigests.build([{'content':object()}], {})

    def test_deterministic_mixed_histories_match_whole_json_hashes(self):
        rng = random.Random(20261018)
        values = [None,True,False,0,1,-0.0,1e20,'汉字😀','"quoted"\n',{},['synthetic',None]]
        for trial in range(80):
            messages = [{'role':rng.choice(['system','user','assistant','tool']),
                         'content':copy.deepcopy(rng.choice(values)),
                         'extra':{str(i):copy.deepcopy(rng.choice(values)) for i in range(rng.randrange(4))}}
                        for _ in range(rng.randrange(18))]
            with self.subTest(trial=trial):
                self.assert_exact(messages, {'entries':[{'name':f'tool_{i}'} for i in range(rng.randrange(5))]})

    def test_edits_reordering_branches_and_catalog_changes_never_reuse_old_hashes(self):
        messages = [{'role':'user','content':'same question'}, {'role':'assistant','content':'answer'}]
        first = self.assert_exact(messages, {'entries':[]})
        messages[0]['content'] = 'edited question'
        edited = self.assert_exact(messages, {'entries':[]})
        self.assertNotEqual(first.history, edited.history)
        reversed_result = self.assert_exact(list(reversed(messages)), {'entries':[]})
        self.assertNotEqual(edited.history, reversed_result.history)
        changed_tools = self.assert_exact(messages, {'entries':[{'name':'new_tool'}]})
        self.assertEqual(edited.history, changed_tools.history)
        self.assertNotEqual(edited.source, changed_tools.source)

    def test_large_media_is_encoded_once_and_not_retained_in_result(self):
        messages = [{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,'+('Q' * 300_000)}}]} for _ in range(3)]
        with patch.object(server, '_canonical', wraps=server._canonical) as canonical:
            result = _RequestMessageDigests.build(messages, {'entries':[]})
        self.assertEqual(4, canonical.call_count)  # one tool catalog, three messages
        self.assertEqual(_digest(messages), result.history)
        self.assertLess(len(repr(result)), 1000)


class RequestDigestIntegrationTests(unittest.TestCase):
    def app(self, mode='legacy'):
        control = {'legacy': FakeControl, 'anchored-v1': BundleControl,
                   'tail-context-v2': TailControl}[mode]()
        app = GatewayApplication(replace(config(), context_layout=mode),
                                 control=control)
        self.addCleanup(app.upstream.close)
        return app

    @staticmethod
    def request(messages):
        return {'model':config().public_model,'messages':messages,'stream':True,
                'tools':[{'type':'function','function':{'name':'fixture','parameters':{'type':'object','properties':{}}}}]}

    def test_payload_and_source_are_exact_for_all_layouts(self):
        messages = [{'role':'system','content':'synthetic prompt'},
                    {'role':'user','content':[{'type':'text','text':'hello'},
                        {'type':'image_url','image_url':{'url':'data:image/png;base64,QUJD'}}]}]
        payload = self.request(messages)
        untouched = copy.deepcopy(payload)
        for mode in ('legacy','anchored-v1','tail-context-v2'):
            with self.subTest(mode=mode):
                app = self.app(mode)
                prepared = app.prepare_turn(payload, {'X-ST-Thread-ID':'synthetic-thread'})
                expected_source = _digest({'messages':messages,'advertised_tools':server._advertised_tool_catalog(payload)})
                self.assertEqual(expected_source, prepared.session.source_digest)
                self.assertEqual(_digest(messages), prepared.session.history_anchor_digest)
                if mode != 'legacy':
                    self.assertIsNotNone(prepared.session.context_bundle)
                self.assertEqual(untouched, payload)
                self.assertEqual(app._apply_context(prepared.session, messages), prepared.payload['messages'])
                self.assertEqual(payload['tools'], prepared.payload['tools'])
                self.assertEqual(payload['stream'], prepared.payload['stream'])

    def test_append_edit_and_other_thread_comparison_keeps_old_semantics(self):
        for mode in ('legacy','anchored-v1','tail-context-v2'):
            with self.subTest(mode=mode):
                app = self.app(mode)
                messages = [{'role':'user','content':'fixture'}]
                first = app.prepare_turn(self.request(messages), {'X-ST-Thread-ID':'a'})
                app.finish_turn(first, keep_for_tools=False)
                messages.extend([{'role':'assistant','content':'reply'}, {'role':'user','content':'next'}])
                second = app.prepare_turn(self.request(messages), {'X-ST-Thread-ID':'a'})
                self.assertTrue(second.cache_comparison['prior_input_prefix_preserved'])
                app.finish_turn(second, keep_for_tools=False)
                messages[0]['content'] = 'edited'
                third = app.prepare_turn(self.request(messages), {'X-ST-Thread-ID':'a'})
                self.assertFalse(third.cache_comparison['prior_input_prefix_preserved'])
                app.finish_turn(third, keep_for_tools=False)
                fourth = app.prepare_turn(self.request(messages), {'X-ST-Thread-ID':'b'})
                self.assertTrue(all(v is None for v in fourth.cache_comparison.values()))

    def test_preparation_does_not_reencode_full_large_history_for_comparison(self):
        app = self.app()
        messages = [{'role':'user','content':[{'type':'text','text':'synthetic'},
                    {'type':'image_url','image_url':{'url':'data:image/png;base64,' + 'A'*100_000}}]}]
        first = app.prepare_turn(self.request(messages), {'X-ST-Thread-ID':'a'})
        app.finish_turn(first, keep_for_tools=False)
        messages.extend([{'role':'assistant','content':'reply'}, {'role':'user','content':'next'}])
        with patch.object(server,'_canonical',wraps=server._canonical) as canonical:
            second = app.prepare_turn(self.request(messages), {'X-ST-Thread-ID':'a'})
        media_calls = [call for call in canonical.call_args_list if 'data:image/' in json.dumps(call.args[0])]
        self.assertEqual(1,len(media_calls))
        self.assertEqual(messages[0],media_calls[0].args[0])
        self.assertTrue(second.cache_comparison['prior_input_prefix_preserved'])

    def test_split_continuation_keeps_raw_stop_anchor_and_normalized_cache_digest(self):
        fixture = split_fixtures.SplitToolDeclarationAdversarialTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        tools, _, _, _, _, messages = fixture.prepare_split_wait()
        normalized = fixture.app._normalize_split_tool_declarations(copy.deepcopy(messages))
        self.assertNotEqual(len(messages), len(normalized))
        resumed = fixture.app.prepare_turn(fixture.request(messages, tools=tools), fixture.headers)
        self.assertEqual(len(messages), resumed.session.history_anchor_count)
        self.assertEqual(_digest(messages), resumed.session.history_anchor_digest)
        self.assertNotEqual(_digest(normalized), resumed.session.history_anchor_digest)
        self.assertEqual(_digest(normalized), fixture.app._last_cache_comparison['history'])


if __name__ == '__main__':
    unittest.main()
