"""L36: synthetic-only colloquial credential assignment regressions.

No live credentials, stores, network or running services are used. Integration
cases invoke the existing daily facade and real stores behind a synthetic host.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import importlib
import json
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import mcp_server.tests.test_daily_memory_service as daily_fixture
from runtime.credential_guard import contains_credential_or_secret as detects


_VALUE = "SYNTHETIC_L36_ONLY"


class ColloquialCredentialTests(unittest.TestCase):
    def test_reported_short_verb_and_interposed_subject_forms(self):
        for index, template in enumerate((
            "密码我设成了 {value}", "密码 我设成 {value}", "密码设成{value}",
            "密码设成了{value}", "密码我设置成了 {value}",
            "密码 我已经设为 {value}", "站点密码我们刚刚改成了：{value}",
            "数据库口令我手动修改为{value}", "密钥她重新设置为 {value}",
            "令牌他们已经重置成了 {value}", "口令您刚换成了 {value}",
            "API key我刚刚改成了 {value}", "token我重新设为{value}",
            "密码我设成了\"{value}\"", "密码我设成了　{value}",
            "密码我设成了：\n{value}",
        )):
            with self.subTest(case=index):
                self.assertTrue(detects(template.format(value=_VALUE)))

    def test_values_remain_protected_inside_nested_fields_and_json(self):
        text = "密码我设成了 " + _VALUE
        for index, data in enumerate((
            {"original_text": text}, {"summary": text}, {"keywords": [text]},
            {"nested": [{"description": text}]}, json.dumps({"content": text}),
            "配置如下：" + json.dumps({"content": text}),
            "密码我设成了 " + json.dumps({"value": _VALUE}),
        )):
            with self.subTest(case=index):
                self.assertTrue(detects(data))

    def test_original_direct_and_token_format_guards_remain(self):
        for index, text in enumerate((
            "密码：" + _VALUE, "password: " + _VALUE,
            "ghp_" + "s" * 36, "Bearer " + "s" * 24,
            "密码已轮换为" + _VALUE, "token updated to " + _VALUE,
        )):
            with self.subTest(case=index):
                self.assertTrue(detects(text))

    def test_discussion_without_a_value_is_not_an_assignment(self):
        for index, text in enumerate((
            "普通文本，今天去散步。", "api key 的轮换流程", "API 名称：天气查询",
            "API key 我知道怎么配置了。", "密码我没有设置，也不会把值存进记忆。",
            "密码我没设成任何固定值。", "密码我不设成固定值。",
            "如果要设置密码，先选择安全的存放位置。", "假设要重置口令，应该怎么操作？",
            "密码我设成了", "密码 我设成", "密码设成", "密码我设成了：",
            "密码我设成了。下一步：检查网络。",
            "密码我设成了，具体值不记录。", "密码我修改过了，不记明文。",
            "密码学：研究安全通信。", "本轮 token 预算：2000",
            "token 我刚更新了使用统计。", "密码我知道了。今天安排：学习。",
        )):
            with self.subTest(case=index):
                self.assertFalse(detects(text))

    def test_no_cross_sentence_or_cross_field_assignment(self):
        for index, value in enumerate((
            ["密码我设成", _VALUE], {"heading": "密码", "body": "我设成了 " + _VALUE},
            "密码。后续我设成了 " + _VALUE,
            "密码\n我设成了 " + _VALUE,
            "密码我\n设成了 " + _VALUE,
        )):
            with self.subTest(case=index):
                self.assertFalse(detects(value))

    def test_explicit_references_stay_storable_but_not_trailing_values(self):
        for index, reference in enumerate((
            "${DEMO_PASSWORD}", "环境变量 DEMO_PASSWORD", "/etc/example/private.env",
            r"路径 C:\Example\private.env",
        )):
            with self.subTest(case=index):
                self.assertFalse(detects("密码我设成了 " + reference))
                self.assertTrue(detects("密码我设成了 " + reference + "，实际值是 " + _VALUE))

    def test_all_shared_component_gates_receive_the_fix(self):
        names = (
            "authoring", "emotional_memory", "hallucination_vault", "learning_idea_box",
            "learning_memory", "planning_memory", "self_governance",
        )
        for name in names:
            with self.subTest(component=name):
                gate = importlib.import_module("runtime." + name)._contains_secret
                self.assertTrue(gate("密码我设成了 " + _VALUE))
                self.assertFalse(gate("api key 的轮换流程"))
        from runtime import tool_guidance
        self.assertEqual("credential_or_secret_detected", tool_guidance._forbidden_content("密码我设成了 " + _VALUE))

    def test_long_whitespace_does_not_make_new_grammar_combinatorial(self):
        started = time.perf_counter()
        self.assertFalse(detects("密码" + " " * 10_000 + "readme"))
        self.assertFalse(detects("密码我" + " " * 10_000 + "readme"))
        self.assertTrue(detects("密码我" + " " * 10_000 + "设成了 " + _VALUE))
        self.assertLess(time.perf_counter() - started, 1.0)


class ColloquialPersistenceTests(unittest.TestCase):
    # Reuse the existing real-store/synthetic-host fixture, not its test methods.
    rows = daily_fixture.DailyMemoryServiceTests.rows
    counts = daily_fixture.DailyMemoryServiceTests.counts

    def setUp(self):
        # Release source is intentionally mounted/read-only on Linux.  Keep all
        # synthetic databases in the platform temp area, never beside source.
        self.scratch = self.enterContext(tempfile.TemporaryDirectory(
            prefix="synthetic-secret-l36-",
        ))
        self.enterContext(patch.object(tempfile, "tempdir", self.scratch))
        daily_fixture.DailyMemoryServiceTests.setUp(self)

    def test_daily_store_rejects_before_any_memory_or_version_is_written(self):
        before = self.counts()
        for index, module in enumerate(self.daily.services):
            for variant, text in enumerate((
                "密码我设成了 " + _VALUE, "密码 我设成 " + _VALUE, "密码设成" + _VALUE,
            )):
                with self.subTest(module=module, variant=variant):
                    self.bound.return_value = replace(self.claim, call_id=f"reject-{index}-{variant}")
                    result = self.daily.remember(module, text)
                    self.assertEqual("reject", result["decision"])
                    expected_reason = (
                        "credential_content_rejected"
                        if module == "planning_memory"
                        else "credential_or_secret_detected"
                    )
                    self.assertEqual([expected_reason], result["reason_codes"])
                    self.assertNotIn(_VALUE, json.dumps(result))
                    self.assertEqual(before, self.counts())
        for table in ("learning_versions", "planning_versions", "planning_events", "planning_change_candidates"):
            self.assertEqual([], self.rows(table))
        # Also check every SQLite textual field, including audit/rejection rows.
        with closing(sqlite3.connect(self.database)) as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn(_VALUE, dump)

    def test_normal_credential_procedure_can_still_be_stored(self):
        for index, module in enumerate(self.daily.services):
            with self.subTest(module=module):
                self.bound.return_value = replace(self.claim, call_id=f"normal-{index}")
                result = self.daily.remember(module, "api key 的轮换流程：只记录存放路径，不记明文。")
                self.assertEqual("stored", result["decision"])
        self.assertEqual((1, 1, 1), self.counts())


if __name__ == "__main__":
    unittest.main()
