"""Independent synthetic review cases: no live model, browser, or memory DB."""
import io
import json
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from rikkahub_gateway.server import GatewayApplication, _GatewayHandler, _digest
from rikkahub_gateway.short_term import Handover, HandoverMessage, Source
from rikkahub_gateway.short_term_wire import FinalBodyCollector, MARKER
from rikkahub_gateway.tests import test_gateway as legacy
from rikkahub_gateway.tests import test_short_term_integration as fixtures
from runtime.onboarding import ModuleOneOnboardingStore


def sse(text='synthetic delivered final'):
    chunks = []
    for delta, finish in (({'content': text}, None), ({}, 'stop')):
        chunks.append(('data: ' + json.dumps({'id': 'review-final', 'choices': [
            {'index': 0, 'delta': delta, 'finish_reason': finish}]}) + '\n\n').encode())
    return b''.join(chunks) + b'data: [DONE]\n\n'


class BrokenWrite(io.BytesIO):
    def write(self, data):
        raise BrokenPipeError('synthetic client disconnect')


class BrokenTerminalWrite(io.BytesIO):
    def write(self, data):
        if b'[DONE]' in data:
            raise BrokenPipeError('synthetic disconnect after stop')
        return super().write(data)


class ReviewControl(fixtures.ShortTermControl):
    omit_policy = False
    legacy_identity = False

    def post(self, path, payload):
        if self.legacy_identity and path.endswith('/prepare'):
            return legacy.FakeControl.post(self, path, payload)
        result = super().post(path, payload)
        if self.omit_policy and path.endswith('/prepare'):
            result.pop('short_term_policy', None)
        return result


class ShortTermReviewTests(unittest.TestCase):
    def setUp(self):
        self.control = ReviewControl()
        self.app = GatewayApplication(replace(legacy.config(), context_layout='tail-context-v2',
                                             short_term_enabled=True), control=self.control)
        self.addCleanup(lambda: self.app.upstream.close())
        self.addCleanup(self.app.close_short_term)

    def prepare(self, source='A', **kwargs):
        headers = {'X-Session-ID': source} if source is not None else {}
        return self.app.prepare_turn(legacy.GatewayTests.request(
            [{'role': 'user', 'content': 'synthetic user'}], **kwargs), headers)

    def collector(self, text='synthetic final', mid='review-body'):
        collector = FinalBodyCollector()
        collector.feed(fixtures.body(text, mid=mid), stream=False)
        return collector

    def complete(self, prepared, text='synthetic final', mid='review-body'):
        self.app.finish_turn(prepared, keep_for_tools=False)
        self.app.capture_final_body(prepared, self.collector(text, mid))

    def handover(self, prepared):
        for message in prepared.payload['messages']:
            content = message.get('content')
            if isinstance(content, str) and content.startswith(MARKER):
                return json.loads(content[len(MARKER):])
        return None

    def handler(self, raw, *, status=200, output=None):
        self.app.upstream.close()
        self.app.upstream = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, content=raw)))
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=self.app)
        handler.wfile = output if output is not None else io.BytesIO()
        handler.send_response = lambda *args: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        return handler

    def test_3xx_sse_never_enters_short_term(self):
        for status in (301, 302, 304, 307):
            with self.subTest(status=status):
                source = self.prepare('A-' + str(status), stream=True)
                self.handler(sse(), status=status)._proxy_stream(source)
                target = self.prepare('B-' + str(status))
                self.assertIsNone(self.handover(target))
                self.app.finish_turn(target, keep_for_tools=False)

    def test_json_broken_pipe_does_not_capture(self):
        source = self.prepare('A')
        handler = self.handler(json.dumps(fixtures.body('undelivered')).encode(), output=BrokenWrite())
        handler._proxy_json(source)
        self.assertIsNone(self.handover(self.prepare('B')))
        self.assertIsNone(getattr(handler, '_commit_deadline', None))

    def test_stream_disconnect_after_stop_still_does_not_capture(self):
        source = self.prepare('A', stream=True)
        output = BrokenTerminalWrite()
        handler = self.handler(sse('partial delivered body'), output=output)
        handler._proxy_stream(source)
        self.assertIn(b'partial delivered body', output.getvalue())
        self.assertIsNone(self.handover(self.prepare('B')))
        self.assertIsNone(getattr(handler, '_commit_deadline', None))

    def test_final_write_and_capture_are_atomic_against_new_window(self):
        source = self.prepare('A')
        handler = self.handler(b'')
        write_reached = threading.Event()
        next_attempted = threading.Event()
        next_finished = threading.Event()
        result = {}

        def next_window():
            if not write_reached.wait(2):
                result['error'] = 'write never reached'
                return
            next_attempted.set()
            try:
                result['prepared'] = self.prepare('B')
            except Exception as error:
                result['error'] = error
            finally:
                next_finished.set()

        thread = threading.Thread(target=next_window, daemon=True)
        thread.start()
        try:
            def write_final():
                write_reached.set()
                self.assertTrue(next_attempted.wait(2))
                self.assertFalse(next_finished.wait(0.03))

            handler._commit_final_delivery(source, self.collector('latest A body'),
                                           write_final, False, (), {})
        finally:
            write_reached.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertNotIn('error', result)
        self.assertEqual('latest A body', self.handover(result['prepared'])['items'][0]['content'])

    def test_commit_writes_share_one_deadline(self):
        source = self.prepare('A')
        handler = self.handler(b'')
        now = [100.0]
        written = []

        def write_final():
            with handler._client_write_boundary(source):
                written.append('first')
            now[0] = 111.0
            with handler._client_write_boundary(source):
                written.append('must not write')

        with patch('rikkahub_gateway.server.time.monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(TimeoutError):
                handler._commit_final_delivery(source, self.collector('undelivered'),
                                               write_final, False, (), {})
        self.assertEqual(['first'], written)
        self.assertIsNone(self.handover(self.prepare('B')))
        self.assertIsNone(getattr(handler, '_commit_deadline', None))

    def test_missing_authenticated_policy_is_fail_closed_and_clears_old_body(self):
        self.complete(self.prepare('A'), 'old body')
        self.control.omit_policy = True
        unknown_policy = self.prepare('B')
        self.assertIsNone(self.handover(unknown_policy))
        self.assertNotIn('ST_SOURCE_V1', json.dumps(unknown_policy.payload))
        self.complete(unknown_policy, 'must not be cached', 'missing-policy')
        self.control.omit_policy = False
        self.assertIsNone(self.handover(self.prepare('C')))

    def test_disabled_authenticated_policy_is_fail_closed_and_clears_old_body(self):
        self.complete(self.prepare('A'), 'old body')
        self.control.allowed = False
        off = self.prepare('B')
        self.assertIsNone(self.handover(off))
        self.assertNotIn('ST_SOURCE_V1', json.dumps(off.payload))
        self.complete(off, 'must not be cached', 'disabled-policy')
        self.control.allowed = True
        self.assertIsNone(self.handover(self.prepare('C')))

    def test_unknown_source_cuts_previous_known_lineage(self):
        self.complete(self.prepare('A'), 'old A body')
        unknown = self.prepare(None)
        self.assertIsNone(self.handover(unknown))
        self.assertIsNone(unknown.session.short_term_ticket)
        self.complete(unknown, 'unknown body', 'unknown-message')
        self.assertIsNone(self.handover(self.prepare('B')))

    def test_missing_control_identity_cuts_previous_known_lineage(self):
        self.complete(self.prepare('A'), 'old A body')
        self.control.legacy_identity = True
        unknown = self.prepare('B')
        self.assertIsNone(unknown.session.context_bundle)
        self.assertIsNone(unknown.session.short_term_ticket)
        self.assertIsNone(self.handover(unknown))
        self.complete(unknown, 'unknown identity body', 'unknown-identity-message')
        self.control.legacy_identity = False
        self.assertIsNone(self.handover(self.prepare('C')))

    def test_handover_filters_individual_expiry_and_retired_wakes(self):
        current = self.prepare('B')
        now = time.monotonic()
        handover = Handover(Source('fixture', 'A'), (
            HandoverMessage('older', 'already expired', 1800.0),
            HandoverMessage('newer', 'still fresh', 1.0)))
        self.app._short_term_frames[current.session.wake_id] = (now, now + 1799.0, handover)
        self.app._short_term_frames['retired-wake'] = (now, now + 1799.0, handover)
        self.app.maintain_short_term()
        self.assertEqual({current.session.wake_id}, set(self.app._short_term_frames))
        retained = self.app._short_term_frames[current.session.wake_id][2]
        self.assertEqual(['still fresh'], [item.text for item in retained.messages])

    def test_handover_body_is_not_added_to_control_hash_or_prepare_payload(self):
        self.complete(self.prepare('A'), 'UNIQUE_SYNTHETIC_FINAL_BODY')
        target = self.prepare('B')
        self.assertIsNotNone(self.handover(target))
        self.assertEqual(_digest(target.session.context_bundle), target.session.context_hash)
        self.assertNotIn('UNIQUE_SYNTHETIC_FINAL_BODY', json.dumps(target.session.context_bundle))
        self.assertNotIn('UNIQUE_SYNTHETIC_FINAL_BODY', json.dumps(self.control.calls))

    def test_control_policy_requires_both_global_and_emotional_enabled(self):
        for modes, expected in (({'global': 'enabled', 'emotional_memory': 'enabled'}, True),
                                ({'global': 'disabled', 'emotional_memory': 'enabled'}, False),
                                ({'global': 'enabled', 'emotional_memory': 'disabled'}, False),
                                ({'global': 'ask', 'emotional_memory': 'enabled'}, False)):
            with self.subTest(modes=modes):
                store = SimpleNamespace(effective_mode=lambda **kwargs: modes[kwargs['scope']])
                service = SimpleNamespace(injection_control_store=store)
                value = ModuleOneOnboardingStore._short_term_policy(service, object(), 'owner', 'model')
                self.assertEqual({'contract': 'st-short-term-policy/1', 'enabled': expected}, value)


if __name__ == '__main__':
    unittest.main()
