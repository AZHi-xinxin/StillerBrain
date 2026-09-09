"""Offline, synthetic-store regression for grounded natural-description search.

D3_RETRIEVABLE is the field-allowlisted technical card supplied by the 2026-09-04
read-only audit.  It is a test fixture, not an import or write to a live brain.
All other content is synthetic; every store below lives in a TemporaryDirectory.
"""

from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

from runtime.learning_memory import (
    _natural_description_evidence,
    _natural_positive_occurrence,
    _natural_quantities,
    _natural_quantity_matches,
)
from tests import test_learning_memory as _existing_fixture


D3_QUERY = "手机连接家里电脑时曾经只有约30KB/s，后来换成IPv6直连后速度提高了两百多倍；当时判断真正的瓶颈在哪一层？"
D3_RETRIEVABLE = {
    "title": "慢链路根因：DERP 中继兜底，不是模型问题",
    "summary": "手机↔家电脑 Tailscale 慢的根因是 DERP 东京中继（约30KB/s）而非 ST/工具/模型；IPv6 直连后 273 倍提速。排障先查链路。",
    "current_understanding": "手机与家电脑之间走 Tailscale 时，若 NAT 打洞失败，流量会 fallback 到官方公共中继 DERP（亚太节点=东京），带宽仅约 30KB/s，大 prompt 请求体上传极慢。修法：让两端协商直连（如 IPv6 直连），或用公网 IP 的国内 VPS 做中转（手机→VPS 直连不出国）。挂梯子与否与此无关，东京路是 Tailscale 自动兜底选路。注意：慢的时候先查链路（直连/中继），别先怀疑注入层、工具 schema 或模型本身。",
    "application_contexts": [],
    "scene_tags": ["排障_卡顿", "网络_直连", "VPS迁移"],
    "keywords": ["DERP", "中继", "Tailscale", "直连", "慢", "提速"],
    "entities": [],
    "domain": "技术",
}


class LearningNaturalDescriptionRecallTests(unittest.TestCase):
    # Reuse the established temporary-store authoring fixture without inheriting
    # its test methods (and thus without accidentally running them twice).
    setUp = _existing_fixture.LearningMemoryRuntimeTests.setUp
    tearDown = _existing_fixture.LearningMemoryRuntimeTests.tearDown
    card = _existing_fixture.LearningMemoryRuntimeTests.card
    remember = _existing_fixture.LearningMemoryRuntimeTests.remember
    evidence_item = staticmethod(_existing_fixture.LearningMemoryRuntimeTests.evidence_item)

    @staticmethod
    def fields(**overrides: object) -> dict[str, object]:
        return {
            **D3_RETRIEVABLE,
            "referent_bindings": [],
            "preceding_context_summary": "",
            **overrides,
        }

    def recall(self, query: str, **options: object) -> dict[str, object]:
        return self.store.recall(owner_id=self.owner, model_id=self.model, query=query, **options)

    def database_snapshot(self) -> tuple[list[str], list[str]]:
        with closing(sqlite3.connect(self.main_db)) as main, closing(sqlite3.connect(self.idea_db)) as ideas:
            return list(main.iterdump()), list(ideas.iterdump())

    def test_original_d3_reproduces_lexical_miss_then_matches_active_version_one(self) -> None:
        self.assertAlmostEqual(0.05172413793103448, self.store._query_score(D3_RETRIEVABLE, D3_QUERY))
        stored = self.remember(**self.fields())
        before = self.database_snapshot()
        recalled = self.recall(D3_QUERY)
        self.assertEqual([stored["item_ref"]], [item["item_ref"] for item in recalled["results"]])
        self.assertTrue(stored["item_ref"].endswith("@1"))
        result = recalled["results"][0]
        self.assertGreaterEqual(result["semantic_score"], 0.15)
        self.assertLess(result["semantic_score"], 0.50)
        self.assertEqual("grounded_natural_description", result["retrieval_evidence"]["reason"])
        self.assertEqual(["ipv6"], result["retrieval_evidence"]["identifiers"])
        self.assertIn("两百多倍", result["retrieval_evidence"]["quantities"])
        self.assertIn("手机", result["retrieval_evidence"]["literal_cues"])
        self.assertIn("电脑", result["retrieval_evidence"]["literal_cues"])
        self.assertFalse(recalled["state_changed"])
        self.assertEqual(before, self.database_snapshot())

    def test_natural_paraphrases_need_no_title_marker_or_optional_tags(self) -> None:
        stored = self.remember(**self.fields(
            title="传输实测记录", scene_tags=[], keywords=[], entities=[], application_contexts=[],
        ))
        for query in (
            D3_QUERY,
            "手机往电脑传数据只有30KB/s；切换IPv6后快了二百多倍，这一变化说明什么？",
            "那次手机和电脑走IPv6，传输从30KB/s提高了273倍，排查结论是什么？",
            "手机连接电脑的３０ＫＢ／ｓ慢速问题，后来使用ｉｐｖ６直连，提速二百七十多倍，怎么解释？",
        ):
            with self.subTest(query=query):
                self.assertEqual([stored["item_ref"]], [item["item_ref"] for item in self.recall(query)["results"]])

    def test_same_mechanism_generalizes_to_database_and_lab_descriptions(self) -> None:
        cases = (
            (
                {"title": "并发实验记录", "summary": "数据库启用 SQLite 的 WAL 模式后，并发写入等待由 5 秒降至 2 秒。", "current_understanding": "使用提交前后的计时验证等待开销。"},
                "数据库用SQLite切换WAL以后，并发写入等待由5秒变成2秒，这说明什么？",
            ),
            (
                {"title": "光学实验记录", "summary": "相机曝光设为 20 毫秒，LED 照明时图像噪声改善 40 倍。", "current_understanding": "对照组使用相同的光圈。"},
                "相机那次用LED照明，曝光20ms后图像噪声好了四十倍，实验观察到了什么？",
            ),
        )
        for row_version, (fields, query) in enumerate(cases):
            with self.subTest(query=query):
                stored = self.remember(row_version=row_version, **self.fields(
                    **fields, scene_tags=[], keywords=[], entities=[], application_contexts=[], domain="实验",
                ))
                evidence = _natural_description_evidence(fields, query)
                self.assertIsNotNone(evidence)
                self.assertEqual(stored["item_ref"], self.recall(query)["results"][0]["item_ref"])

    def test_wrong_protocol_number_unit_or_direction_cannot_gain_the_fallback(self) -> None:
        self.remember(**self.fields())
        queries = (
            D3_QUERY.replace("IPv6", "IPv4"),
            D3_QUERY.replace("IPv6", "IPv60"),
            D3_QUERY.replace("30KB/s", "300KB/s"),
            D3_QUERY.replace("30KB/s", "30kb/s"),
            D3_QUERY.replace("两百多倍", "两千多倍"),
            D3_QUERY.replace("换成IPv6直连后速度提高", "没有换成IPv6直连，速度没有提高"),
            D3_QUERY.replace("30KB/s", "30KiB/s"),
            D3_QUERY.replace("30KB/s", "-30KB/s"),
            D3_QUERY.replace("30KB/s", "−30KB/s"),
            D3_QUERY.replace("30KB/s", "1,030KB/s"),
            D3_QUERY.replace("速度提高", "速度降低"),
        )
        for query in queries:
            with self.subTest(query=query):
                self.assertIsNone(_natural_description_evidence(D3_RETRIEVABLE, query))
                self.assertEqual(0, self.recall(query)["result_count"])

    def test_structured_units_and_chinese_ranges_are_not_fuzzy_number_matching(self) -> None:
        def match(query: str, authored: str) -> bool:
            return _natural_quantity_matches(_natural_quantities(query)[0], _natural_quantities(authored)[0])

        for query, authored in (("两百多倍", "273倍"), ("二百七十多倍", "273倍"), ("30KB/s", "0.03MB/s"), ("20ms", "20毫秒"), ("二〇二六倍", "2026倍"), ("1,030KB/s", "1030KB/s")):
            with self.subTest(query=query, authored=authored):
                self.assertTrue(match(query, authored))
        for query, authored in (("两百多倍", "300倍"), ("二百七十多倍", "280倍"), ("273倍", "两百多倍"), ("二百七十多倍", "两百多倍"), ("30KB/s", "30kb/s"), ("30KB/s", "30KiB/s"), ("20ms", "20秒"), ("40倍", "40%"), ("9007199254740992倍", "9007199254740993倍")):
            with self.subTest(query=query, authored=authored):
                self.assertFalse(match(query, authored))

    def test_unicode_and_untrusted_numeric_input_is_safe_and_preserves_evidence(self) -> None:
        for digits in ("٣٠", "३०", "３０"):
            with self.subTest(digits=digits):
                self.assertIsNotNone(_natural_description_evidence(D3_RETRIEVABLE, D3_QUERY.replace("30", digits)))
        for amount in ("9" * 400, "百百", "一百二", "1,0030", "两两", "一万万"):
            with self.subTest(amount=amount):
                self.assertEqual([], _natural_quantities(amount + "倍"))
                self.assertIsNone(_natural_description_evidence(D3_RETRIEVABLE, D3_QUERY.replace("两百多倍", amount + "倍")))

    def test_negated_contrast_marker_keeps_a_positive_and_b_negative(self) -> None:
        for sentence in ("IPv6而非IPv4", "IPv6，并非IPv4", "IPv6，非IPv4", "不是IPv4而是IPv6"):
            with self.subTest(sentence=sentence):
                positive, negative = sentence.index("IPv6"), sentence.index("IPv4")
                self.assertTrue(_natural_positive_occurrence(sentence, positive, positive + 4))
                self.assertFalse(_natural_positive_occurrence(sentence, negative, negative + 4))

    def test_same_dimension_measurement_order_is_not_a_bag_of_numbers(self) -> None:
        fields = {"summary": "数据库启用 SQLite 的 WAL 模式后，并发写入等待由 5 秒降至 2 秒。"}
        correct = "数据库用SQLite切换WAL以后，并发写入等待由5秒变成2秒，这说明什么？"
        reversed_change = "数据库用SQLite切换WAL以后，并发写入等待由2秒变成5秒，这说明什么？"
        self.assertIsNotNone(_natural_description_evidence(fields, correct))
        self.assertIsNone(_natural_description_evidence(fields, reversed_change))
        # Existing literal search may still retrieve this card for comparison;
        # a query contradiction is not an instruction to hide authored history.
        self.assertGreaterEqual(self.store._manual_search_score(fields, reversed_change), 0.15)

    def test_repeated_approximation_is_one_anchor_but_exact_facts_still_need_support(self) -> None:
        fields = {"summary": "手机与电脑传输提速273倍。"}
        self.assertIsNone(_natural_description_evidence(fields, "手机和电脑的传输提升273倍，也就是两百多倍？"))
        conflicting = D3_QUERY.replace("两百多倍", "两百多倍也就是250倍")
        self.assertIsNone(_natural_description_evidence(D3_RETRIEVABLE, conflicting))

    def test_generic_topical_words_or_one_anchor_are_insufficient(self) -> None:
        for query in (
            "手机连接家里电脑后速度很慢，当时判断真正的瓶颈在哪一层？",
            "手机连接家里电脑后来换成IPv6直连，当时判断真正的瓶颈在哪一层？",
            "曾经只有约30KB/s，后来换成IPv6之后提高了两百多倍；当时是什么情况？",
            "手机手机手机30KB/s IPv6两百多倍？",
        ):
            with self.subTest(query=query):
                self.assertIsNone(_natural_description_evidence(D3_RETRIEVABLE, query))

    def test_wrong_nearby_cards_do_not_displace_the_card_with_matching_facts(self) -> None:
        wrong_fields = (
            self.fields(summary=D3_RETRIEVABLE["summary"].replace("IPv6", "IPv4"), current_understanding="只记录了另一种协议的测试。"),
            self.fields(summary=D3_RETRIEVABLE["summary"].replace("30KB/s", "300KB/s"), current_understanding="另一次测试的速度不同。"),
            self.fields(summary=D3_RETRIEVABLE["summary"].replace("273 倍", "27 倍"), current_understanding="另一次测试的倍率不同。"),
            self.fields(title="相机连接实验", summary="相机和投影仪使用 IPv6 传输，30KB/s 提高了 273 倍。", current_understanding="测试的是光学设备。", scene_tags=[], keywords=[]),
        )
        wrong_refs = [self.remember(row_version=index, **fields)["item_ref"] for index, fields in enumerate(wrong_fields)]
        correct = self.remember(row_version=len(wrong_fields), **self.fields())
        recalled = self.recall(D3_QUERY)
        self.assertEqual([correct["item_ref"]], [item["item_ref"] for item in recalled["results"]])
        self.assertTrue(set(wrong_refs).isdisjoint(item["item_ref"] for item in recalled["results"]))

    def test_quote_negation_and_cross_passage_anchor_laundering_do_not_gain_boost(self) -> None:
        misleading = (
            "教材引用的例句：“手机与电脑使用IPv6直连，30KB/s后来提高了273倍。”这只是引用。",
            "手机与电脑并没有使用IPv6直连，30KB/s也未提高273倍。",
            "手机与电脑使用IPv6直连后，30KB/s未提高273倍。",
            "手机与电脑使用IPv4而非IPv6直连，30KB/s提高273倍。",
            "假设手机与电脑使用IPv6直连，30KB/s后来提高了273倍。",
            "手机与电脑连接速度为30KB/s。另一实验使用IPv6，改善了273倍。",
            "手机与电脑连接速度为30KB/s；另一实验使用IPv6，改善了273倍。",
            "手机与电脑连接速度为30KB/s，另一实验使用IPv6，改善了273倍。",
            "并非真实观测，手机与电脑使用IPv6直连，30KB/s后来提高了273倍。",
            "手机与电脑使用IPv6直连，30KB/s后来提高了273倍吗？",
        )
        for understanding in misleading:
            with self.subTest(understanding=understanding):
                fields = {"title": "文字分析练习", "summary": "区分陈述、引用与假设。", "current_understanding": understanding}
                self.assertIsNone(_natural_description_evidence(fields, D3_QUERY))
        # Finding a literal quotation remains a legitimate explicit search;
        # this patch only withholds the new positive-evidence ranking boost.
        quoted = {"current_understanding": misleading[0]}
        self.assertGreaterEqual(self.store._manual_search_score(quoted, "30KB/s"), 0.15)

    def test_additional_path_has_explicit_query_field_and_numeric_work_bounds(self) -> None:
        with patch("runtime.learning_memory._natural_quantities", side_effect=AssertionError("must not scan oversized query")):
            self.assertIsNone(_natural_description_evidence(D3_RETRIEVABLE, D3_QUERY + "很长" * 300))
        long_field = {"current_understanding": D3_RETRIEVABLE["summary"] + "正文" * 3000}
        with patch("runtime.learning_memory._natural_text_cues", side_effect=AssertionError("must not scan oversized field")):
            self.assertIsNone(_natural_description_evidence(long_field, D3_QUERY))
        self.assertEqual([], _natural_quantities("9" * 1000 + "KB/s"))

    def test_existing_successful_literal_search_skips_the_additional_scan(self) -> None:
        with patch("runtime.learning_memory._natural_description_evidence", side_effect=AssertionError("unneeded scan")):
            self.assertGreaterEqual(self.store._manual_search_score(D3_RETRIEVABLE, "30KB/s"), 0.15)

    def test_fallback_reads_only_the_existing_field_allowlist(self) -> None:
        hidden = {"title": "无关记录", "summary": "无关概要", "current_understanding": "无关正文"}
        for field in ("steps", "uncertainties", "evidence", "pending_candidate", "preceding_context_summary", "private_notes"):
            with self.subTest(field=field):
                self.assertIsNone(_natural_description_evidence({**hidden, field: D3_RETRIEVABLE["summary"]}, D3_QUERY))
        tags_only = {**hidden, "keywords": ["IPv6", "30KB/s", "273倍", "手机", "电脑"]}
        self.assertIsNone(_natural_description_evidence(tags_only, D3_QUERY))

    def test_manual_fallback_does_not_expand_automatic_recall_or_policy_gates(self) -> None:
        blocked = (
            {}, {"context_policy": "ask_first"}, {"context_policy": "never_auto"},
            {"recall_mode": "never"}, {"deny_contexts": ["手机"]},
            {"allow_contexts": ["只允许另一个指定场景"]},
        )
        for row_version, overrides in enumerate(blocked):
            self.remember(row_version=row_version, **self.fields(**overrides))
        before = self.database_snapshot()
        self.assertGreater(self.recall(D3_QUERY)["result_count"], 0)
        self.assertEqual([], self.store.build_envelopes(owner_id=self.owner, model_id=self.model, query=D3_QUERY))
        self.assertEqual(before, self.database_snapshot())

    def test_owner_model_lifecycle_and_readonly_regressions(self) -> None:
        active = self.remember(**self.fields())
        excluded_refs = []
        for row_version, lifecycle in enumerate(("archived", "superseded", "pending_review", "quarantined"), start=1):
            excluded_refs.append(self.remember(row_version=row_version, **self.fields(lifecycle=lifecycle))["item_ref"])
        original_owner, original_model = self.owner, self.model
        for owner, model in (("other-owner", original_model), (original_owner, "other-model")):
            self.owner, self.model = owner, model
            self.store.ensure_state(owner_id=owner, model_id=model)
            excluded_refs.append(self.remember(**self.fields(current_understanding="SCOPE_SENTINEL_NOT_FOR_ORIGINAL_OWNER"))["item_ref"])
        self.owner, self.model = original_owner, original_model
        before = self.database_snapshot()
        for query in (D3_QUERY, "手机往电脑传数据只有30KB/s；IPv6直连后快了两百多倍，原因是什么？"):
            recalled = self.recall(query, include_versions=True, include_evidence=True)
            self.assertEqual([active["item_ref"]], [item["item_ref"] for item in recalled["results"]])
            self.assertFalse(recalled["state_changed"])
            self.assertNotIn("SCOPE_SENTINEL", json.dumps(recalled, ensure_ascii=False))
        for ref in excluded_refs:
            self.assertEqual(0, self.store.recall(owner_id=self.owner, model_id=self.model, target_ref=ref)["result_count"])
        self.assertEqual(before, self.database_snapshot())


if __name__ == "__main__":
    unittest.main()
