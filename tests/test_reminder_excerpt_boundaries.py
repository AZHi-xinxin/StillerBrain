"""Automatic-reminder boundaries with isolated synthetic plans and tool cards."""
from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.reminder_excerpt import reminder_excerpt
from runtime.planning_memory import PlanningMemoryStore
from runtime.tool_guidance import _reminder_text
from tests import test_planning_memory as planning_fixtures
from tests import test_planning_persistent_r4h34 as planning_isolation
from tests import test_tool_guidance_simplified as tool_fixtures

OWNER, MODEL = planning_fixtures.OWNER, planning_fixtures.MODEL


class ReminderExcerptTests(unittest.TestCase):
    def test_complete_sentence_and_closing_quote(self):
        for text, expected in (
            ("先核实计划层级。" + "再核实后续内容" * 15, "先核实计划层级。"),
            ("我写下：“可以独立建目标。”" + "然后继续" * 20, "我写下：“可以独立建目标。”"),
            ("Review the plan. " + "Continue checking " * 8, "Review the plan."),
            ("(First sentence.) " + "Then continue " * 8, "(First sentence.)"),
            ("核对版本 3.14 后再讨论。" + "后续" * 40, "核对版本 3.14 后再讨论。"),
        ):
            with self.subTest(text=text[:20]):
                self.assertEqual(expected, reminder_excerpt(text, 50))

    def test_clause_is_an_explicit_excerpt_not_an_invented_full_stop(self):
        for punctuation in ("，", ",", "；", ";", "\n", "\r\n"):
            text = "我想检查计划" + punctuation + "随后核验计划层级是否需要改进" * 8
            with self.subTest(punctuation=punctuation):
                self.assertEqual("我想检查计划…", reminder_excerpt(text, 50))

    def test_long_text_without_punctuation_has_visible_ellipsis(self):
        self.assertEqual("甲" * 49 + "…", reminder_excerpt("甲" * 100, 50))
        self.assertEqual("Review the complete…", reminder_excerpt("Review the complete configuration carefully", 24))
        self.assertEqual("1,23456789…", reminder_excerpt("1,23456789012345", 11))

    def test_short_and_exact_length_remain_complete(self):
        self.assertEqual("完整计划", reminder_excerpt("  完整计划  ", 50))
        self.assertEqual("甲" * 50, reminder_excerpt("甲" * 50, 50))
        self.assertEqual("甲" * 49 + "。", reminder_excerpt("甲" * 49 + "。后续", 50))
        self.assertEqual("", reminder_excerpt("", 50))
        self.assertEqual("…", reminder_excerpt("很长的内容", 1))

    def test_combining_and_emoji_sequences_are_not_split_at_limit(self):
        for sequence in ("e\u0301", "👨‍👩‍👧‍👦", "👍🏽", "🇨🇳"):
            text = "甲" * 48 + sequence + "随后继续" * 12
            with self.subTest(sequence=sequence):
                self.assertEqual("甲" * 48 + "…", reminder_excerpt(text, 50))

    def test_tool_legacy_projection_uses_boundary_without_changing_author_value(self):
        content = {"purpose": "回家时可以查一下设备；" + "继续核对设备状态" * 30}
        before = copy.deepcopy(content)
        self.assertEqual("回家时可以查一下设备…", _reminder_text(content, 100))
        self.assertEqual(before, content)
        explicit = {**content, "reminder": "这是我自己写的提醒，保留原样"}
        self.assertEqual(explicit["reminder"], _reminder_text(explicit, 100))


class StoredPlanningReminderTests(unittest.TestCase):
    setUp = planning_isolation.PlanningPersistentCandidateTests.setUp
    key = planning_fixtures.PlanningMemoryStoreTests.key

    def remember(self, content, **fields):
        return self.store.remember_ordinary(
            owner_id=OWNER, model_id=MODEL, write_context_ref="synthetic-write-ref",
            wake_id="synthetic-wake", wake_seq=1,
            expected_row_version=self.store.status(owner_id=OWNER, model_id=MODEL)["row_version"],
            content=content, idempotency_key=self.key("ordinary"), **fields)

    def read(self, result):
        return self.store.recall(owner_id=OWNER, model_id=MODEL,
            plan_ref=result["plan_ref"], include_history=True)["plans"][0]["content"]

    def snapshot(self):
        with self.store._connect() as connection:
            return tuple(connection.iterdump())

    def test_real_ordinary_path_stores_complete_reminder_and_verbatim_original(self):
        original = "  我想核验计划的独立创建。\n" + "再核验计划层级是否符合自己的用法" * 6
        saved = self.remember(original, kind="goal")
        data = self.read(saved)
        self.assertEqual("我想核验计划的独立创建。", data["reminder"])
        self.assertEqual(original, data["original_text"])
        self.assertIsNone(data["parent_ref"])
        self.assertIn("reminder", saved["display_excerpt_fields"])

    def test_explicit_summary_and_reminder_remain_author_controlled(self):
        text = "完整原文" * 30
        summary = "摘要里的完整第一句。" + "后续摘要内容" * 12
        saved = self.remember(text, summary=summary)
        self.assertEqual("摘要里的完整第一句。", self.read(saved)["reminder"])
        self.assertEqual(summary, self.read(saved)["summary"])
        explicit = "我自写的提醒，仍有后续补充"
        authored = self.remember(text, reminder=explicit)
        self.assertEqual(explicit, self.read(authored)["reminder"])
        self.assertNotIn("reminder", authored["display_excerpt_fields"])
        empty = self.remember(text, reminder="")
        self.assertEqual("", self.read(empty)["reminder"])

    def test_reopening_old_plan_does_not_regenerate_or_write_history(self):
        old = self.remember("旧正文" * 60, reminder="旧的节选到这里就不")
        before = self.snapshot()
        reopened = PlanningMemoryStore(self.database)
        content = reopened.recall(owner_id=OWNER, model_id=MODEL,
            plan_ref=old["plan_ref"], include_history=True)["plans"][0]["content"]
        self.assertEqual("旧的节选到这里就不", content["reminder"])
        self.assertEqual(before, self.snapshot())


class LegacyToolReminderProjectionTests(unittest.TestCase):
    row = tool_fixtures.SimplifiedToolGuidanceTests.row
    create = tool_fixtures.SimplifiedToolGuidanceTests.create
    recall = tool_fixtures.SimplifiedToolGuidanceTests.recall
    details = tool_fixtures.SimplifiedToolGuidanceTests.details

    def setUp(self):
        tool_fixtures.SimplifiedToolGuidanceTests.setUp(self)
        for target in ("socket.socket", "socket.create_connection"):
            self.enterContext(patch(target, side_effect=AssertionError("offline test")))
        connect = sqlite3.connect

        def isolated_connect(path, *args, **kwargs):
            if Path(path).resolve() != self.path.resolve():
                raise AssertionError("only this synthetic database may be opened")
            return connect(path, *args, **kwargs)

        self.enterContext(patch("sqlite3.connect", side_effect=isolated_connect))

    def test_real_legacy_envelope_ends_cleanly_without_rewriting_card(self):
        purpose = "回家时看看设备状态，" + "再决定是否需要进一步检查" * 15
        card = self.create(purpose=purpose, reminder=None)["card"]
        before = self.details(card["card_id"])
        envelope = self.recall()["envelopes"][0]
        self.assertEqual("回家时看看设备状态…", envelope["content"]["scene_summary"])
        self.assertEqual("legacy_purpose_excerpt", envelope["summary_source"])
        after = self.details(card["card_id"])
        self.assertEqual(before["content_hash"], after["content_hash"])
        self.assertEqual(before["content"], after["content"])


if __name__ == "__main__":
    unittest.main()
