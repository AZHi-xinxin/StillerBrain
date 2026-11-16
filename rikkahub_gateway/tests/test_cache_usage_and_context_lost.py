"""Provider counter compatibility and recovery of a settled tool history."""
import unittest

from rikkahub_gateway.server import GatewayApplication, GatewayError, _RequestPerformance
from rikkahub_gateway.tests import test_gateway as f


class CacheUsageAndContextLostTests(unittest.TestCase):
    def test_gpt_nested_cache_reads_are_observable_without_invented_misses(self):
        telemetry = _RequestPerformance()
        telemetry.note_model('gpt-6-astra')
        telemetry.note_usage({'usage': {'prompt_tokens': 151705, 'completion_tokens': 448,
            'prompt_tokens_details': {'cached_tokens': 76904, 'cache_write_tokens': 74801}}})
        self.assertEqual('gpt-6-astra', telemetry.observed_model)
        self.assertEqual(76904, telemetry.usage_snapshot['prompt_cache_hit_tokens'])
        self.assertNotIn('prompt_cache_miss_tokens', telemetry.usage_snapshot)
        telemetry.note_usage({'usage': {'prompt_tokens': 100, 'prompt_tokens_details': {'cached_tokens': 0}}})
        self.assertEqual(0, telemetry.prompt_cache_hit_tokens)
        self.assertEqual({'prompt_tokens': 100, 'prompt_cache_hit_tokens': 0}, telemetry.usage_snapshot)

    def test_nested_invalid_value_is_not_logged_or_subtracted(self):
        for invalid in [True, 'synthetic-private-text', -1, 10_000_001]:
            with self.subTest(value=invalid):
                telemetry = _RequestPerformance()
                telemetry.note_usage({'usage': {'prompt_tokens_details': {'cached_tokens': invalid}}})
                self.assertEqual({}, telemetry.usage_snapshot)
                self.assertEqual(['prompt_cache_hit_tokens'], telemetry.usage_snapshot_invalid_fields)

    def test_completed_tool_result_retained_when_human_starts_fresh_turn(self):
        control = f.FakeControl()
        app = GatewayApplication(f.config(), control=control)
        self.addCleanup(app.upstream.close)
        call = f.GatewayTests.native_call('call_finished', 'synthetic no execution')
        history = [{'role': 'user', 'content': 'check fictional status'},
            {'role': 'assistant', 'content': None, 'tool_calls': [f.GatewayTests.wire_call(call)]},
            {'role': 'tool', 'tool_call_id': call.tool_call_id, 'content': '{"status":"done"}'}]
        payload = f.GatewayTests.request(history, tools=f.GatewayTests.bound_tools())
        resumed = app.prepare_turn(payload, {'X-ST-Thread-ID': 'synthetic-recovery'})
        self.assertTrue(resumed.continuation)
        self.assertEqual(history, resumed.payload['messages'][-len(history):])
        self.assertEqual([], resumed.session.host_receipts)
        app.finish_turn(resumed, keep_for_tools=False)
        self.assertEqual(1, control.seq)
        new_history = history + [{'role': 'user', 'content': 'Continue using the existing result.'}]
        fresh = app.prepare_turn({**payload, 'messages': new_history}, {'X-ST-Thread-ID': 'synthetic-recovery'})
        self.assertFalse(fresh.continuation)
        self.assertEqual(new_history, fresh.payload['messages'][-len(new_history):])
        self.assertEqual(2, control.seq)
        self.assertFalse(any('/tool-executions/' in path for path, _ in control.calls))
