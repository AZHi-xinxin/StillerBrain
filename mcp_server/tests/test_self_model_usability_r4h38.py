"""Synthetic-only regressions for the modern, workspace-free core facade."""
from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

from mcp_server.service import _content_validation_help
from mcp_server.tests import test_public_tools_v5 as fixtures
from mcp_server.tests.test_public_tools_v5 import self_model_content


class SelfModelUsabilityTests(unittest.TestCase):
    # Reuse only fixture helpers, not an inherited set of duplicate test cases.
    setUp = fixtures.PublicFacadeV9Tests.setUp
    tearDown = fixtures.PublicFacadeV9Tests.tearDown
    wake = fixtures.PublicFacadeV9Tests.wake
    open = fixtures.PublicFacadeV9Tests.open
    submit = fixtures.PublicFacadeV9Tests.submit
    advance_to_body = fixtures.PublicFacadeV9Tests.advance_to_body

    def test_modern_deep_error_is_field_addressed_and_retry_needs_no_new_open(self):
        self.wake("synthetic-schema-correction")
        opened = self.advance_to_body()
        before = copy.deepcopy(self.service.module_one_status()["state"])
        bad = self_model_content()
        bad["boot_anchor"] = "synthetic-body-must-not-leak"
        bad["schema_version"] = "synthetic-version-must-not-leak"
        denied = self.submit(opened, "submit", {"content": bad, "reason": "Synthetic format check."})
        self.assertEqual("reject", denied["decision"])
        help_value = denied["validation_help"]
        self.assertFalse(help_value["workspace_required"])
        self.assertFalse(help_value["candidate_persisted"])
        self.assertIn("content.boot_anchor", {e["field"] for e in help_value["errors"]})
        self.assertNotIn("synthetic-body-must-not-leak", json.dumps(help_value))
        self.assertNotIn("synthetic-version-must-not-leak", json.dumps(help_value))
        self.assertEqual(before, self.service.module_one_status()["state"])
        self.assertEqual([], self.service.store.list_candidates(self.service.model_id))
        # Correct the content and submit in the same actual wake. No additional
        # open, workspace tool, ID fabrication, or hidden write retry occurs.
        with mock.patch.object(self.service, "open_brain", side_effect=AssertionError("No new open")):
            saved = self.submit(opened, "submit", {
                "content": self_model_content(), "reason": "Synthetic corrected structure.",
            })
        self.assertEqual("pending", saved["decision"])
        self.assertIsNone(self.service.store.active_revision(self.service.model_id))

    def test_length_only_revise_also_reports_limit_without_echo(self):
        self.wake("synthetic-length-error")
        opened = self.advance_to_body()
        body = self_model_content()
        body["boot_anchor"]["text"] = "我" + "合成长度验证" * 110
        denied = self.submit(opened, "submit", {"content": body, "reason": "Synthetic length check."})
        self.assertEqual("revise", denied["decision"])
        self.assertFalse(denied["content_persisted"])
        error = next(e for e in denied["validation_help"]["errors"] if e["reason_code"] == "boot_anchor_too_long")
        self.assertEqual(500, error["expected"]["maximum_characters"])
        self.assertNotIn(body["boot_anchor"]["text"], json.dumps(denied["validation_help"]))

    def test_stale_context_is_not_repaired_by_skipping_binding(self):
        self.wake("synthetic-old-context")
        old = self.advance_to_body()
        self.wake("synthetic-new-context")
        with mock.patch("mcp_server.service._content_validation_help", side_effect=AssertionError("Binding first")):
            denied = self.submit(old, "submit", {"content": {}, "reason": "Synthetic stale check."})
        self.assertFalse(denied["state_changed"])
        self.assertFalse(denied["pointer_changed"])
        self.assertNotIn("validation_help", denied)
        self.assertEqual([], self.service.store.list_candidates(self.service.model_id))

    def test_help_does_not_prescribe_a_sentence_prefix_or_new_key(self):
        guidance = self.service.content_schema()
        self.assertIn("No sentence prefix", " ".join(guidance["important_rules"]))
        self.assertIsNone(_content_validation_help({}, ["non_first_person_injection_content"]))
        help_value = _content_validation_help({}, ["invalid_content_structure"])
        self.assertNotIn("new idempotency_key", help_value["retry_hint"])
        self.assertEqual({"view": "manual", "module": "self_revision"}, help_value["schema_arguments"])


if __name__ == "__main__":
    unittest.main()
