"""Host-authenticated execution routes, using only the synthetic fixture DB."""
import json
import unittest

from mcp_server.control_server import ControlApplication
from runtime.execution_binding import canonical_hash
from tests import test_execution_binding as fixtures


class ExecutionControlTests(unittest.TestCase):
    new_wake = fixtures.ExecutionBindingTests.new_wake

    def setUp(self):
        fixtures.ExecutionBindingTests.setUp(self)
        self.host = "synthetic-host-token-0000000000000000000000"
        self.human = "synthetic-human-token-000000000000000000000"
        self.control = ControlApplication(self.onboarding, **self.common, host_token=self.host,
            human_token=self.human, human_actor_id="synthetic-human", execution_store=self.store)
        self.body = {key: value for key, value in self.batch.items() if key not in self.common}
        self.body["deployment_epoch"] = "synthetic-epoch"
        self.calls = [{"call_id": "synthetic-call", "advertised_name": "stbrain_open",
            "canonical_tool": "stbrain_open", "schema_hash": self.schema_hash,
            "catalog_hash": self.catalog["catalog_hash"], "arguments_hash": canonical_hash({})}]

    def request(self, operation, body=None, token=None):
        return self.control.handle("POST", "/v1/host/tool-executions/" + operation,
            {"Authorization": "Bearer " + (token or self.host), "Content-Type": "application/json"},
            json.dumps(body if body is not None else self.body).encode())

    def test_host_issue_status_revoke(self):
        status, issued = self.request("issue", {**self.body, "calls": self.calls})
        self.assertEqual(200, status)
        self.assertEqual(1, len(issued["executions"]))
        self.assertEqual(1, self.request("status")[1]["counts"]["issued"])
        self.assertEqual("closed", self.request("revoke")[1]["batch_status"])
        self.assertEqual(1, self.request("status")[1]["counts"]["revoked"])

    def test_human_token_cannot_issue_or_cancel_host_execution(self):
        for operation, body in (("issue", {**self.body, "calls": self.calls}), ("status", self.body), ("revoke", self.body)):
            self.assertEqual(401, self.request(operation, body, self.human)[0])

    def test_wrong_epoch_or_revision_fails_without_revocation(self):
        self.request("issue", {**self.body, "calls": self.calls})
        for changed in ({"deployment_epoch": "synthetic-other"}, {"revision": 2}):
            self.assertEqual(409, self.request("revoke", {**self.body, **changed})[0])
        self.assertEqual(1, self.request("status")[1]["counts"]["issued"])

    def test_missing_or_unknown_body_fields_fail_closed(self):
        for body in ({"revision": 1}, {**self.body, "force": True}, {**self.body, "revision": True}):
            self.assertEqual(409, self.request("revoke", body)[0])

    def test_false_catalog_or_direct_tool_cannot_be_leased(self):
        for changed in ({"schema_hash": "a" * 64}, {"canonical_tool": "stbrain_open_direct"}):
            status, _ = self.request("issue", {**self.body, "calls": [{**self.calls[0], **changed}]})
            self.assertEqual(409, status)

    def test_offline_epoch_retirement_has_no_http_route(self):
        self.assertEqual(409, self.request("retire-stopped-epochs")[0])


if __name__ == "__main__":
    unittest.main()
