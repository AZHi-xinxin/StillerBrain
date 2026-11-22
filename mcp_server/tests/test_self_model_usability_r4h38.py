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

    def test_custom_shape_is_accepted_on_first_submit(self):
        self.wake("synthetic-schema-correction")
        opened = self.advance_to_body()
        before = copy.deepcopy(self.service.module_one_status()["state"])
        bad = self_model_content()
        bad["boot_anchor"] = "synthetic-body-must-not-leak"
        bad["schema_version"] = "synthetic-version-must-not-leak"
        denied = self.submit(opened, "submit", {"content": bad, "reason": "Synthetic format check."})
        self.assertEqual("pending", denied["decision"])
        stored = self.service.store.list_candidates(self.service.model_id)[0]
        self.assertEqual(bad, json.loads(stored["content_json"]))
        self.assertNotIn("validation_help", denied)

    def test_long_authored_text_is_preserved(self):
        self.wake("synthetic-length-error")
        opened = self.advance_to_body()
        body = self_model_content()
        body["boot_anchor"]["text"] = "我" + "合成长度验证" * 110
        denied = self.submit(opened, "submit", {"content": body, "reason": "Synthetic length check."})
        self.assertEqual("pending", denied["decision"])
        stored = self.service.store.list_candidates(self.service.model_id)[0]
        self.assertEqual(body, json.loads(stored["content_json"]))

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
        self.assertIn("原样保存", " ".join(guidance["important_rules"]))
        self.assertIsNone(_content_validation_help({}, ["non_first_person_injection_content"]))
        help_value = _content_validation_help({}, ["invalid_content_structure"])
        self.assertIn("不限制字段", help_value["reason"])


if __name__ == "__main__":
    unittest.main()
