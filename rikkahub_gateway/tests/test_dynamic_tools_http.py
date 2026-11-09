"""Local JSON and SSE requests may refresh tools while returning old results."""
import copy
import unittest

from rikkahub_gateway.tests import test_gateway_interleaved_wire as fixtures


class DynamicToolsHTTPTests(unittest.TestCase):
    def roundtrip(self, stream):
        helper = fixtures.InterleavedHTTPWireTests()
        self.addCleanup(helper.doCleanups)
        helper.setUp()
        self.addCleanup(helper.tearDown)
        status, first = helper.post([helper.user], stream=stream)
        self.assertEqual(200, status, first)
        session = helper.app._current_session
        history = helper.interleaved()
        original = copy.deepcopy(history)
        helper.tools = []
        status, body = helper.post(history, stream=stream)
        self.assertEqual(200, status, body)
        self.assertIn(b"synthetic complete", body)
        self.assertEqual([], helper.upstream_payloads[1]["tools"])
        self.assertEqual(original, helper.upstream_payloads[1]["messages"][-len(original):])
        self.assertEqual(2, len(session.host_receipts))
        self.assertIsNone(helper.app._current_session)
        self.assertEqual(1, helper.control.seq)

    def test_json(self):
        self.roundtrip(False)

    def test_sse(self):
        self.roundtrip(True)
