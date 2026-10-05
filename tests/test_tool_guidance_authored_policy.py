"""Synthetic tool cards are advice; saving them never grants tool authority."""
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from runtime.tool_guidance import ToolGuidanceError, ToolGuidanceStore
from tests.test_tool_guidance import action_card, catalog


class AuthoredToolPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-tool-policy-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic.sqlite3"
        self.store = ToolGuidanceStore(self.path)
        self.owner, self.model = "synthetic-owner", "synthetic-model"
        self.store.ensure_state(owner_id=self.owner, model_id=self.model)

    def row(self):
        return self.store.status(owner_id=self.owner, model_id=self.model)["row_version"]

    def save(self, **fields):
        values = {"tool_name": "HomeControl", "purpose": "说明当前工具的使用场景。"}
        values.update(fields)
        return self.store.remember(owner_id=self.owner, model_id=self.model,
            wake_id="synthetic-wake", expected_row_version=self.row(),
            reason="Synthetic tool advice", catalog=catalog(), **values)

    def gate(self, card, **fields):
        values = {"catalog": catalog(), "current_user_intent": True,
                  "authorization_verified": True, "current_confirmation": False}
        values.update(fields)
        return self.store.execution_gate(owner_id=self.owner, model_id=self.model,
            card_id=card["card_id"], **values)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return list(connection.iterdump())

    def test_all_valid_authored_risk_confirmation_combinations_are_preserved(self):
        for capability in ("information_query", "real_world_action"):
            for risk in ("low", "medium", "high", "critical"):
                for confirmation in ("none", "contextual", "explicit_each_time"):
                    with self.subTest(capability=capability, risk=risk, confirmation=confirmation):
                        saved = self.save(operation_key=f"{capability}_{risk}_{confirmation}",
                            capability_class=capability, risk_level=risk,
                            confirmation_policy=confirmation)
                        content = saved["card"]["content"]
                        self.assertEqual((capability, risk, confirmation), tuple(content[k]
                            for k in ("capability_class", "risk_level", "confirmation_policy")))
                        self.assertFalse(saved["execution_performed"])

    def test_keywords_never_upgrade_or_reject_authored_metadata_or_rewrite_text(self):
        for index, word in enumerate(("支付", "删除", "上传", "发布", "控制设备", "pay", "purchase", "delete", "send", "publish", "upload", "account")):
            with self.subTest(word=word):
                purpose = f"  保留原文：{word}工具的说明，不是执行请求。  "
                saved = self.save(tool_name=f"{word}Service", operation_key=f"advice_{index}",
                    purpose=purpose, risk_level="low", confirmation_policy="none")["card"]
                # Existing _text strips outer whitespace; no new transformation.
                self.assertEqual(purpose.strip(), saved["content"]["purpose"])
                self.assertEqual("low", saved["content"]["risk_level"])
                self.assertEqual("none", saved["content"]["confirmation_policy"])

    def test_new_defaults_add_no_card_confirmation_but_never_grant_authority(self):
        card = self.save()["card"]
        self.assertEqual(("medium", "none"), (card["content"]["risk_level"], card["content"]["confirmation_policy"]))
        for change in ({"current_user_intent": False}, {"authorization_verified": False},
                       {"catalog": None}, {"catalog": catalog(home_schema="c" * 64)}):
            with self.subTest(change=change):
                result = self.gate(card, **change)
                self.assertEqual("denied", result["decision"])
                self.assertFalse(result["execution_performed"])
        allowed = self.gate(card)
        self.assertEqual("allowed_to_attempt", allowed["decision"])
        self.assertFalse(allowed["execution_performed"])

    def test_author_explicit_confirmation_remains_enforced_even_for_low_risk(self):
        card = self.save(risk_level="low", confirmation_policy="explicit_each_time")["card"]
        before = self.snapshot()
        self.assertEqual("confirmation_required", self.gate(card)["decision"])
        permitted = self.gate(card, current_confirmation=True)
        self.assertEqual("allowed_to_attempt", permitted["decision"])
        self.assertFalse(permitted["execution_performed"])
        self.assertEqual(before, self.snapshot())

    def test_preexisting_explicit_policy_is_not_migrated_by_read_or_other_edit(self):
        fields = action_card()
        fields.pop("reason")
        card = self.save(**fields)["card"]
        self.store = ToolGuidanceStore(self.path)
        read = self.store.recall(owner_id=self.owner, model_id=self.model,
            card_id=card["card_id"], view="card")["results"][0]
        self.assertEqual(card["content_hash"], read["content_hash"])
        revised = self.store.revise(owner_id=self.owner, model_id=self.model,
            wake_id="synthetic-next", wake_seq=1, expected_row_version=self.row(),
            card_id=card["card_id"], expected_card_version=1, reason="Synthetic text-only edit",
            purpose="只修订工具的场景说明。", catalog=catalog())["card"]
        self.assertEqual(("high", "explicit_each_time"), (revised["content"]["risk_level"], revised["content"]["confirmation_policy"]))
        self.assertEqual("confirmation_required", self.gate(revised)["decision"])
        old = self.store.recall(owner_id=self.owner, model_id=self.model,
            card_id=card["card_id"], view="history")["versions"][1]
        self.assertEqual(card["content_hash"], old["content_hash"])

    def test_invalid_enums_and_secret_content_still_reject_without_any_write(self):
        for fields, code in (({"risk_level": "safe"}, "invalid_risk_level"),
                ({"confirmation_policy": "automatic"}, "invalid_confirmation_policy"),
                ({"capability_class": "execute_anything"}, "invalid_capability_class"),
                ({"purpose": "Authorization: Bearer synthetic-private-token-value"}, "credential_or_secret_detected")):
            with self.subTest(code=code):
                before = self.snapshot()
                with self.assertRaisesRegex(ToolGuidanceError, code):
                    self.save(**fields)
                self.assertEqual(before, self.snapshot())

    def test_stale_version_cannot_change_authored_policy(self):
        card = self.save()["card"]
        before = self.snapshot()
        with self.assertRaisesRegex(ToolGuidanceError, "tool_card_version_conflict"):
            self.store.revise(owner_id=self.owner, model_id=self.model,
                wake_id="synthetic-next", wake_seq=1, expected_row_version=self.row(),
                card_id=card["card_id"], expected_card_version=2, reason="Synthetic stale edit",
                risk_level="low", confirmation_policy="none", catalog=catalog())
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
