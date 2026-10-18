"""No upstream calls: delivery receipt negotiation and conservative failures."""
from dataclasses import replace
import unittest

from rikkahub_gateway.server import GatewayApplication, GatewayError
from rikkahub_gateway.short_term_wire import FinalBodyCollector
from rikkahub_gateway.tests import test_gateway as legacy
from rikkahub_gateway.tests.test_tail_context_v2_gateway import TailControl


class ContinuityControl(TailControl):
    fail_receipt = False

    def post(self, path, payload):
        if path.endswith("/delivered"):
            self.calls.append((path, payload))
            if self.fail_receipt:
                raise GatewayError(503, "synthetic_receipt_unavailable")
            return {"decision": "delivery_recorded"}
        result = super().post(path, payload)
        if path.endswith("/prepare"):
            result["continuity_delivery_receipt"] = "continuity" in payload["source_frame"]
        return result


class ContinuityDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.control = ContinuityControl()
        self.app = GatewayApplication(replace(legacy.config(), context_layout="tail-context-v2",
                                               short_term_enabled=False), control=self.control)
        self.addCleanup(self.app.upstream.close)
        self.addCleanup(self.app.close_short_term)

    def prepare(self, headers=None):
        return self.app.prepare_turn(legacy.GatewayTests.request([
            {"role": "user", "content": "synthetic"}]), headers or {})

    def collector(self, finish="stop"):
        result = FinalBodyCollector()
        result.feed({"id": "synthetic", "choices": [{"index": 0, "finish_reason": finish,
            "message": {"role": "assistant", "content": "synthetic final"}}]}, stream=False)
        return result

    def receipts(self):
        return [entry for entry in self.control.calls if entry[0].endswith("/delivered")]

    def test_route_metadata_only_from_selected_route_and_source_hash(self):
        prepared = self.prepare({"X-Session-ID": "synthetic-conversation", "X-ST-Client-ID": "orbis"})
        source = next(body["source_frame"] for path, body in self.control.calls if path.endswith("/prepare"))
        self.assertEqual(prepared.route.upstream_model, source["continuity"]["upstream_model"])
        self.assertNotIn("synthetic-conversation", source["continuity"]["source_key"])
        self.assertEqual(64, len(source["continuity"]["source_key"]))
        self.assertEqual([], self.receipts())
        self.app.finish_turn(prepared, keep_for_tools=False)
        self.assertEqual([], self.receipts())
        self.app.capture_final_body(prepared, self.collector())
        self.assertEqual(1, len(self.receipts()))

    def test_incomplete_result_and_unknown_source_do_not_ack(self):
        prepared = self.prepare({"X-Session-ID": "window"})
        self.app.capture_final_body(prepared, self.collector("length"))
        self.assertEqual([], self.receipts())
        self.app.finish_turn(prepared, keep_for_tools=False)
        unknown = self.prepare()
        self.app.capture_final_body(unknown, self.collector())
        self.assertEqual([], self.receipts())

    def test_receipt_failure_does_not_turn_delivered_answer_into_error(self):
        prepared = self.prepare({"X-Session-ID": "window"})
        self.control.fail_receipt = True
        self.app.capture_final_body(prepared, self.collector())
        self.assertEqual(1, len(self.receipts()))
        self.assertEqual(1, self.app.short_term_status()["events"]["continuity_receipt_unconfirmed"])


if __name__ == "__main__":
    unittest.main()
