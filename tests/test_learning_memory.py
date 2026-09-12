from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing

from runtime.learning_idea_box import IDEA_BOX_LABEL
from runtime.learning_memory import LearningMemoryError, LearningMemoryStore
from tests.learning_legacy_fixtures import legacy_integration_candidate


_DEFAULT_EVIDENCE = object()


class LearningMemoryRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.main_db = root / "learning.sqlite"
        self.idea_db = root / "learning-ideas.sqlite"
        self.store = LearningMemoryStore(self.main_db, idea_database=self.idea_db)
        self.owner = "owner-test"
        self.model = "model-test"
        self.store.ensure_state(owner_id=self.owner, model_id=self.model)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def card(self, suffix: str = "", **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "kind": "lesson",
            "title": f"上次读的小说{suffix}",
            "summary": f"她记得主角刚抵达旧城，并发现门上的符号{suffix}",
            "current_understanding": f"这段内容的重点是身份线索与旧城之间的联系{suffix}",
            "steps": ["需要细节时再精准查询章节笔记"],
            "application_contexts": ["继续阅读同一本小说"],
            "scene_tags": ["上次读的小说", "继续上次阅读"],
            "preceding_context_summary": "她上次读到主角进入旧城，尚未揭开符号来源。",
            "uncertainties": ["符号是谁留下的仍未确认"],
            "domain": "共同阅读",
            "keywords": ["小说", "旧城", "符号"],
            "entities": ["主角"],
            "epistemic_status": "reported",
            "confidence": 72,
            "time_sensitivity": "stable",
            "valid_as_of": "2026-08-28",
            "review_after": "",
            "importance": 70,
            "sensitivity": "private",
            "context_policy": "normal",
            "recall_mode": "normal",
            "allow_contexts": [],
            "deny_contexts": [],
            "default_decision": "background_reference",
            "explicit_request_override": "allow_after_confirmation",
            "disclosure": "bounded_excerpt",
            "lifecycle": "active",
            "referent_bindings": [
                {
                    "field_path": "/summary",
                    "surface_form": "她",
                    "occurrence_index": 0,
                    "entity_ref": None,
                    "resolution_status": "unresolved",
                    "confidence": 35,
                }
            ],
        }
        value.update(overrides)
        return value

    def remember(
        self,
        row_version: int = 0,
        suffix: str = "",
        links: list[dict[str, object]] | None = None,
        *,
        wake_id: str | None = None,
        wake_seq: int | None = None,
        evidence: list[dict[str, object]] | None | object = _DEFAULT_EVIDENCE,
        **overrides: object,
    ) -> dict[str, object]:
        evidence_items = (
            [self.evidence_item(row_version)]
            if evidence is _DEFAULT_EVIDENCE
            else evidence
        )
        return self.store.remember(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=wake_id or f"wake-{row_version}",
            wake_seq=row_version + 1 if wake_seq is None else wake_seq,
            expected_row_version=row_version,
            correctness_assessment="我把它作为可复核的阅读概要，而不是小说原文。",
            reason="以后继续阅读时会需要这段前置内容。",
            evidence=evidence_items,
            links=links,
            **self.card(suffix, **overrides),
        )

    @staticmethod
    def evidence_item(
        suffix: object,
        *,
        source_ref: str | None = None,
        content_hash: str | None = None,
    ) -> dict[str, object]:
        return {
            "source_kind": "human_report",
            "source_ref": source_ref or f"conversation://reading/{suffix}",
            "source_trust": "reported",
            "evidence_summary": "对话中共同确认了当前阅读进度。",
            "content_hash": content_hash or f"sha256-{suffix}",
            "observed_at": "2026-08-28T12:00:00Z",
        }

    @staticmethod
    def calm_check(*items: dict[str, object]) -> dict[str, object]:
        return {
            "evidence_sufficient": True,
            "counterevidence_checked": True,
            "scope_changed": False,
            "affected_links_checked": True,
            "single_turn_pressure_absent": True,
            "rollback_understood": True,
            "notes": "已检查来源、反证、关系、单轮压力与回滚边界。",
            "evidence_refs": [str(item["item_ref"]) for item in items],
        }

    def integration_candidate(
        self,
        *,
        row_version: int,
        wake_seq: int,
        sources: list[dict[str, object]],
        suffix: str,
        merge_suggestion_id: str | None = None,
    ) -> dict[str, object]:
        return legacy_integration_candidate(self.store,
            owner_id=self.owner,
            model_id=self.model,
            wake_id=f"wake-integrate-{wake_seq}-{suffix}",
            wake_seq=wake_seq,
            expected_row_version=row_version,
            source_learning_ids=[str(item["learning_id"]) for item in sources],
            synthesis_kind="summary",
            classification_actor="ai_self",
            classification_basis=["来源卡属于同一共读进度"],
            correctness_assessment="综合内容仍是可复核概要。",
            diff="新增一张待复核综合候选，不改来源卡。",
            calm_check=self.calm_check(*sources),
            reason="为跨唤醒审核建立候选。",
            source_action="keep",
            merge_suggestion_id=merge_suggestion_id,
            create_idea=False,
            **self.card(suffix, referent_bindings=[]),
        )

    def test_ai_chosen_third_person_and_unresolved_referent_are_accepted(self) -> None:
        result = self.remember()
        self.assertEqual(result["decision"], "stored")
        self.assertIn("unresolved_referent", result["reason_codes"])
        recalled = self.store.recall(
            owner_id=self.owner, model_id=self.model, target_ref=result["item_ref"]
        )
        content = recalled["results"][0]["content"]
        self.assertTrue(content["summary"].startswith("她"))
        self.assertEqual(content["referent_bindings"][0]["resolution_status"], "unresolved")

    def test_secret_rejection_leaves_no_content_hash_or_version(self) -> None:
        with self.assertRaisesRegex(LearningMemoryError, "credential_or_secret_detected"):
            self.remember(current_understanding="API key=super-secret-value-1234567890")
        self.assertEqual(self.store.status(owner_id=self.owner, model_id=self.model)["row_version"], 0)
        with closing(sqlite3.connect(self.main_db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM learning_items").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM learning_versions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM learning_audit_events").fetchone()[0], 0)

    def test_automatic_projection_contains_summary_not_details(self) -> None:
        stored = self.remember()
        envelopes = self.store.build_envelopes(
            owner_id=self.owner, model_id=self.model, query="我们继续上次读的小说吧"
        )
        self.assertEqual(len(envelopes), 1)
        envelope = envelopes[0]
        self.assertEqual(envelope["item_ref"], stored["item_ref"])
        self.assertIn("preceding_context_summary", envelope["content"])
        forbidden = {"current_understanding", "steps", "evidence", "versions", "idea_box"}
        self.assertTrue(forbidden.isdisjoint(envelope["content"]))
        self.assertEqual(envelope["detail_lookup"]["tool"], "recall_learning_memory")
        self.assertEqual(envelope["item_ref"], envelope["detail_lookup"]["ref"])
        self.assertEqual(envelope["item_ref"], envelope["detail_lookup"]["target_ref"])
        self.assertEqual(
            {"target_ref": envelope["item_ref"]},
            envelope["detail_lookup"]["arguments"],
        )
        semantic_miss = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            query="量子芯片火星航线",
        )
        self.assertEqual(0, semantic_miss["result_count"])
        precise = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            target_ref=envelope["detail_lookup"]["target_ref"],
        )
        self.assertEqual(1, precise["result_count"])
        self.assertEqual(envelope["item_ref"], precise["results"][0]["item_ref"])

    def test_named_work_recall_survives_missing_optional_scene_tags(self) -> None:
        stored = self.remember(
            title="《侍魔》共读进度与深度理解（1-20章）",
            summary="我与昕昕正在共读《侍魔》，已读到第20章；这里保存当前概要。",
            current_understanding="完整理解只允许精准查询，不进入自动投影。",
            application_contexts=[],
            scene_tags=[],
            preceding_context_summary="",
            domain="文学共读",
            keywords=["侍魔", "共读", "救赎即牢笼"],
            entities=["昕昕"],
        )
        self.assertEqual(stored["recall_advisories"], ["automatic_recall_cues_missing"])
        question = "同样不查询工具，只回想，你还记得我们上次一起读的《侍魔》的内容嘛？"
        envelopes = self.store.build_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query=question,
        )
        self.assertEqual(len(envelopes), 1)
        envelope = envelopes[0]
        self.assertEqual(envelope["item_ref"], stored["item_ref"])
        self.assertGreaterEqual(envelope["semantic_score"], 0.80)
        self.assertIn("explicit_subject_match", envelope["reason_codes"])
        self.assertEqual(
            envelope["module_extension"]["learning"]["subject_match"],
            "quoted_reading_subject",
        )
        serialized = json.dumps(envelope, ensure_ascii=False)
        self.assertIn("已读到第20章", serialized)
        self.assertNotIn("完整理解只允许精准查询", serialized)

        precise = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            query=question,
        )
        self.assertEqual(precise["result_count"], 1)
        self.assertEqual(precise["results"][0]["item_ref"], stored["item_ref"])

        unrelated = self.store.build_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query="我在游戏里遇到了侍魔这个职业。",
        )
        self.assertEqual(unrelated, [])
        bare_keyword = self.store.build_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query="侍魔",
        )
        self.assertEqual(bare_keyword, [])
        quoted_but_wrong_scene = self.store.build_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query="你还记得游戏职业《侍魔》吗？",
        )
        self.assertEqual(quoted_but_wrong_scene, [])

    def test_natural_recall_with_one_exact_named_scene_cue_is_not_lost(self) -> None:
        stored = self.remember(
            title="海龟汤中的背景物线索",
            summary="玩海龟汤时曾用不起眼的背景物排除表面叙事。",
            current_understanding="详细方法需要精准读取。",
            application_contexts=["回想以前玩过的海龟汤"],
            scene_tags=["海龟汤"],
            preceding_context_summary="此前已经完成一局情境推理。",
            domain="情境推理",
            keywords=["背景物", "排除法"],
            entities=[],
        )
        question = "阿止，你还记得我们之前玩的那几局海龟汤吗？现在回头想想，你会想到些什么？"

        recalled = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            query=question,
        )
        self.assertEqual(1, recalled["result_count"])
        self.assertEqual(stored["item_ref"], recalled["results"][0]["item_ref"])
        self.assertGreaterEqual(recalled["results"][0]["semantic_score"], 0.82)

        envelopes = self.store.build_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query=question,
        )
        self.assertEqual(1, len(envelopes))
        self.assertIn(
            "single_exact_scene_cue_recall",
            envelopes[0]["reason_codes"],
        )
        unrelated = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            query="你还记得之前玩过的纸牌游戏吗？",
        )
        self.assertEqual(0, unrelated["result_count"])

    def test_unquoted_reading_subject_uses_stored_title_without_tags_or_keywords(self) -> None:
        stored = self.remember(
            title="《侍魔》共读进度与深度理解（1-20章）",
            summary="这是共读进度的概要，停在第20章。",
            current_understanding="章节里的完整细节不应自动投影。" * 70,
            application_contexts=[], scene_tags=[], keywords=[], entities=[],
            preceding_context_summary="", domain="文学共读", referent_bindings=[],
        )
        with closing(sqlite3.connect(self.main_db)) as connection:
            before = list(connection.iterdump())
        for query in (
            "嘿嘿，你还记得我们之前读的侍魔嘛？",
            "你还记得上次我们读的《侍魔》嘛？",
            "上次一起读过的“侍魔”，我们读到哪里了？",
            "继续读侍魔这本小说吧。",
        ):
            with self.subTest(query=query):
                envelopes = self.store.build_envelopes(
                    owner_id=self.owner, model_id=self.model, query=query,
                )
                self.assertEqual([stored["item_ref"]], [e["item_ref"] for e in envelopes])
                self.assertGreaterEqual(envelopes[0]["semantic_score"], 0.80)
                self.assertIn("explicit_subject_match", envelopes[0]["reason_codes"])
                self.assertNotIn("current_understanding", envelopes[0]["content"])
                recalled = self.store.recall(
                    owner_id=self.owner, model_id=self.model, query=query,
                )
                self.assertEqual([stored["item_ref"]], [r["item_ref"] for r in recalled["results"]])
        with closing(sqlite3.connect(self.main_db)) as connection:
            self.assertEqual(before, list(connection.iterdump()))

    def test_unquoted_short_title_does_not_open_unrelated_automatic_recall(self) -> None:
        self.remember(
            title="《侍魔》共读笔记", summary="共读进度概要。",
            current_understanding="完整章节笔记。" * 100,
            application_contexts=[], scene_tags=[], keywords=["侍魔", "共读"],
            entities=[], preceding_context_summary="", domain="文学共读",
            referent_bindings=[],
        )
        for query in (
            "侍魔",
            "我在游戏里遇到了侍魔这个职业。",
            "你还记得游戏职业《侍魔》吗？",
            "你还记得上次我们读过的《侍魔法则》吗？",
            "你还记得我们之前读的侍魔法则嘛？",
            "你还记得我们之前读的侍魔术士吗？",
            "你还记得我们之前读的机械侍魔吗？",
            "你还记得我们之前读的其他小说吗？",
            "我今天浇了水，晚上想去看星星。",
        ):
            with self.subTest(query=query):
                self.assertEqual([], self.store.build_envelopes(
                    owner_id=self.owner, model_id=self.model, query=query,
                ))

    def test_multi_term_search_matches_authored_fields_without_tags(self) -> None:
        stored = self.remember(
            title="《侍魔》共读进度与深度理解（1-20章）",
            summary="与昕昕共读《侍魔》（拉格朗曰），停在第20章。",
            current_understanding="章节里的完整细节不应自动投影。" * 70,
            application_contexts=[], scene_tags=[], keywords=["侍魔", "共读"],
            entities=[], preceding_context_summary="", domain="文学共读",
            referent_bindings=[],
        )
        self.remember(
            row_version=1, title="《另一部作品》共读笔记",
            summary="另一本小说的共读概要。", current_understanding="完全不同的章节。" * 70,
            application_contexts=[], scene_tags=[], keywords=["共读", "小说"],
            entities=[], preceding_context_summary="", domain="文学共读",
            referent_bindings=[],
        )
        for query in (
            "侍魔 共读 拉格朗曰",
            "拉格朗曰 共读 侍魔",
            "侍魔，共读，拉格朗曰",
            "侍魔 共读 未知修饰词",
            "侍魔 未知作者",
            "帮我找一下侍魔的共读内容",
        ):
            with self.subTest(query=query):
                recalled = self.store.recall(
                    owner_id=self.owner, model_id=self.model, query=query,
                )
                self.assertEqual([stored["item_ref"]], [r["item_ref"] for r in recalled["results"]])
                self.assertEqual("deterministic_lexical_subject", recalled["retrieval_backend"])
                self.assertGreaterEqual(recalled["results"][0]["semantic_score"], 0.15)
                self.assertFalse(recalled["state_changed"])
                # A deliberate tool search is not itself permission to raise
                # the score of an incidental conversational mention.
                self.assertEqual([], self.store.build_envelopes(
                    owner_id=self.owner, model_id=self.model, query=query,
                ))
        self.assertEqual(2, self.store.status(owner_id=self.owner, model_id=self.model)["row_version"])

    def test_manual_search_can_find_body_without_any_optional_identity_cues(self) -> None:
        stored = self.remember(
            title="章节复盘", summary="这张卡保留了正文里的一个具体线索。",
            current_understanding="主角在雾港月台找到了星砂时钟。" + "其余章节尚未核验。" * 120,
            application_contexts=[], scene_tags=[], keywords=[], entities=[],
            preceding_context_summary="", domain="文学", referent_bindings=[],
        )
        for query in ("雾港月台", "雾港月台 星砂时钟"):
            with self.subTest(query=query):
                recalled = self.store.recall(
                    owner_id=self.owner, model_id=self.model, query=query,
                )
                self.assertEqual([stored["item_ref"]], [r["item_ref"] for r in recalled["results"]])
                self.assertEqual([], self.store.build_envelopes(
                    owner_id=self.owner, model_id=self.model, query=query,
                ))

    def test_tagless_subject_fallback_preserves_recall_policy_and_lifecycle_gates(self) -> None:
        fields = dict(
            title="《侍魔》共读进度", summary="共读的当前概要。",
            current_understanding="不进入自动投影的细节。" * 70,
            application_contexts=[], scene_tags=[], keywords=[], entities=[],
            preceding_context_summary="", domain="文学共读", referent_bindings=[],
        )
        blocked = (
            {"context_policy": "ask_first"},
            {"context_policy": "never_auto"},
            {"recall_mode": "never"},
            {"deny_contexts": ["侍魔"]},
            {"allow_contexts": ["仅在指定授权场景"]},
            {"lifecycle": "archived"},
            {"lifecycle": "quarantined"},
        )
        for row_version, overrides in enumerate(blocked):
            self.remember(row_version=row_version, **{**fields, **overrides})
        allowed = self.remember(row_version=len(blocked), **fields)
        envelopes = self.store.build_envelopes(
            owner_id=self.owner, model_id=self.model,
            query="你还记得我们之前读的侍魔嘛？",
        )
        self.assertEqual([allowed["item_ref"]], [e["item_ref"] for e in envelopes])

    def test_lexical_exact_boost_respects_latin_word_boundaries(self) -> None:
        fields = {"title": "novel earth record", "current_understanding": "A long reading note. " * 50}
        self.assertLess(self.store._query_score(fields, "art"), 0.15)
        self.assertLess(self.store._manual_search_score(fields, "art"), 0.15)
        self.assertGreaterEqual(self.store._manual_search_score(fields, "ＥＡＲＴＨ record"), 0.30)

    def test_auto_lineage_cannot_skip_a_policy_blocked_intermediate_source(self) -> None:
        leaf = {
            "learning_id": "leaf", "item_ref": "learning://leaf@1",
            "snapshot": self.card(title="《侍魔》共读进度", scene_tags=[], keywords=[]),
            "current": self.card(title="《侍魔》共读进度", scene_tags=[], keywords=[]),
        }
        middle = {
            "learning_id": "middle", "item_ref": "learning://middle@1",
            "snapshot": self.card(title="抽象综合", scene_tags=[], keywords=[]),
            "current": self.card(title="抽象综合", scene_tags=[], keywords=[], context_policy="never_auto"),
        }
        lineage = {"final": [middle], "middle": [leaf]}
        query = "你还记得我们之前读的侍魔嘛？"
        allowed = lambda content: content.get("context_policy") != "never_auto"
        score, refs = self.store._integration_query_matches(
            learning_id="final", lineage=lineage, query=query, source_allowed=allowed,
        )
        self.assertEqual((0.0, []), (score, refs))
        # A separate direct, allowed source path remains usable.
        lineage["final"].append(leaf)
        score, refs = self.store._integration_query_matches(
            learning_id="final", lineage=lineage, query=query, source_allowed=allowed,
        )
        self.assertGreaterEqual(score, 0.80)
        self.assertEqual([leaf["item_ref"]], refs)

    def test_inventory_lists_all_active_cards_and_topic_collection_is_exhaustive(self) -> None:
        sea_turtle_titles = [
            "海龟汤：背景物线索",
            "海龟汤：集合混淆",
            "海龟汤：视角切换",
            "海龟汤：表层答案与大故事",
        ]
        for index, title in enumerate(sea_turtle_titles):
            self.remember(
                index,
                title=title,
                summary=f"第{index + 1}张海龟汤推理方法卡。",
                current_understanding="这是一条可复用的情境推理方法。",
                application_contexts=["复盘海龟汤"],
                scene_tags=["海龟汤", "推理复盘"],
                preceding_context_summary="已经完成一局游戏。",
                domain="情境推理",
                keywords=["海龟汤", "推理"],
                entities=[],
                referent_bindings=[],
            )
        self.remember(
            4,
            title="《侍魔》共读进度",
            summary="保存当前共读位置。",
            current_understanding="这是另一主题的知识。",
            application_contexts=["继续共读"],
            scene_tags=[],
            preceding_context_summary="",
            domain="文学共读",
            # A generic authored keyword must not turn an explicit all-card
            # inventory question into a one-card topic lookup.
            keywords=["侍魔", "知识"],
            entities=[],
            referent_bindings=[],
        )
        quarantined = self.remember(
            5,
            kind="concept",
            title="隔离中的受质疑说法",
            summary="这条受质疑内容不能出现在普通目录。",
            current_understanding="只允许在隔离流程中显式打开。",
            application_contexts=["隔离测试"],
            scene_tags=["海龟汤", "隔离测试"],
            preceding_context_summary="",
            uncertainties=["尚未核验"],
            domain="测试",
            keywords=["海龟汤"],
            entities=[],
            epistemic_status="disputed",
            confidence=30,
            importance=30,
            referent_bindings=[],
        )
        self.assertEqual("stored_quarantined", quarantined["decision"])

        complete = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            view="inventory",
            query="",
            limit=20,
        )
        self.assertEqual("inventory_listed", complete["decision"])
        self.assertEqual("explicit_view", complete["inventory_route"])
        self.assertTrue(complete["inventory_answer_supported"])
        self.assertTrue(complete["exhaustive_inventory"])
        self.assertEqual(5, complete["inventory_total_count"])
        self.assertEqual(5, complete["matched_count"])
        self.assertEqual(5, complete["returned_count"])
        self.assertEqual(1, complete["quarantined_count"])
        self.assertFalse(complete["quarantined_content_exposed"])
        serialized = json.dumps(complete, ensure_ascii=False)
        self.assertNotIn("隔离中的受质疑说法", serialized)
        self.assertTrue(all("current_understanding" not in item for item in complete["items"]))

        topic = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            view="inventory",
            query="之前海龟汤的内容你存了很多知识是吗",
            limit=20,
            include_pending=True,
        )
        self.assertEqual(4, topic["matched_count"])
        self.assertEqual(sea_turtle_titles, sorted(
            [item["title"] for item in topic["items"]],
            key=sea_turtle_titles.index,
        ))
        self.assertNotIn(quarantined["learning_id"], {
            item["learning_id"] for item in topic["items"]
        })

        automatic = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            query="查询你的学习脑中的知识都有些什么内容",
            limit=20,
        )
        self.assertEqual("auto_detected", automatic["inventory_route"])
        self.assertTrue(automatic["topic_filter"]["generic_unfiltered_fallback"])
        self.assertEqual(5, automatic["returned_count"])

        unrelated_storage_question = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            query="情感脑里存了哪些内容",
            limit=20,
        )
        self.assertEqual("semantic_search", unrelated_storage_question["retrieval_mode"])
        self.assertFalse(unrelated_storage_question["inventory_answer_supported"])

        one_character_topic = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            view="inventory",
            query="海",
            limit=20,
        )
        self.assertEqual(0, one_character_topic["matched_count"])

        first_page = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            view="inventory",
            query="",
            limit=3,
        )
        self.assertFalse(first_page["exhaustive_inventory"])
        self.assertTrue(first_page["has_more"])
        self.assertEqual(3, first_page["next_offset"])
        second_page = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            view="inventory",
            query="",
            limit=3,
            offset=first_page["next_offset"],
        )
        self.assertEqual(2, second_page["returned_count"])
        self.assertFalse(second_page["has_more"])

        emergency = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            view="inventory",
            query="",
            limit=20,
            safety_emergency=True,
        )
        self.assertEqual(3, emergency["limit"])
        self.assertEqual(3, emergency["returned_count"])
        self.assertTrue(emergency["has_more"])

    def test_inventory_uses_opaque_stub_for_restricted_card(self) -> None:
        stored = self.remember(
            sensitivity="restricted",
            title="不可在目录展开的标题",
            summary="不可在目录展开的摘要",
            scene_tags=["受限线索"],
            keywords=["受限关键词"],
        )
        inventory = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            view="inventory",
            query="",
        )
        self.assertEqual(1, inventory["returned_count"])
        item = inventory["items"][0]
        self.assertEqual(stored["item_ref"], item["item_ref"])
        self.assertTrue(item["content_stub"])
        serialized = json.dumps(inventory, ensure_ascii=False)
        self.assertNotIn("不可在目录展开的标题", serialized)
        self.assertNotIn("不可在目录展开的摘要", serialized)
        self.assertNotIn("受限线索", serialized)
        self.assertNotIn("受限关键词", serialized)

    def test_accepted_synthesis_inherits_topic_retrieval_from_frozen_sources(self) -> None:
        sources: list[dict[str, object]] = []
        for index, title in enumerate((
            "海龟汤：背景物线索",
            "海龟汤：集合混淆",
            "海龟汤：视角切换",
            "海龟汤：表层答案与大故事",
        )):
            sources.append(self.remember(
                index,
                title=title,
                summary=f"第{index + 1}张海龟汤推理方法卡。",
                current_understanding="这是一条可复用的情境推理方法。",
                application_contexts=["复盘海龟汤"],
                scene_tags=["海龟汤", "推理复盘"],
                preceding_context_summary="已经完成一局游戏。",
                domain="情境推理",
                keywords=["海龟汤", "推理"],
                entities=[],
                referent_bindings=[],
            ))

        candidate = self.integration_candidate(
            row_version=4,
            wake_seq=5,
            sources=sources,
            suffix="高层复核综合",
        )
        review = self.store.review_snapshot(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake-lineage-review",
            wake_seq=6,
        )
        self.assertEqual(candidate["candidate_id"], review["candidates"][0]["candidate_id"])
        accepted = self.store.review_change(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake-lineage-review",
            wake_seq=6,
            expected_row_version=5,
            candidate_id=str(candidate["candidate_id"]),
            expected_candidate_version=1,
            expected_candidate_hash=str(candidate["candidate_hash"]),
            expected_base_version=0,
            action="accept",
            correctness_assessment="综合保留来源边界，可以作为高层复核策略。",
            calm_check=self.calm_check(*sources),
            reason="跨唤醒复核通过。",
            ai_confirmation=True,
        )
        synthesis_id = str(accepted["learning_id"])
        synthesis_ref = str(accepted["item_ref"])
        synthesis = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            target_ref=synthesis_ref,
        )
        self.assertNotIn(
            "海龟汤",
            json.dumps(synthesis["results"][0]["content"], ensure_ascii=False),
        )

        before_reads = self.store.status(
            owner_id=self.owner, model_id=self.model
        )["row_version"]
        inventory = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            view="inventory",
            query="之前海龟汤的内容你存了很多知识是吗",
            limit=20,
        )
        self.assertEqual(5, inventory["matched_count"])
        synthesis_item = next(
            item for item in inventory["items"]
            if item["learning_id"] == synthesis_id
        )
        self.assertEqual(
            4,
            sum(
                basis["field"] == "integration_source"
                for basis in synthesis_item["topic_match_basis"]
            ),
        )

        semantic = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            query="海龟汤",
            limit=10,
        )
        semantic_item = next(
            item for item in semantic["results"]
            if item["learning_id"] == synthesis_id
        )
        self.assertEqual(4, len(semantic_item["integration_source_matches"]))

        envelopes = self.store.build_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query="我们刚才玩的海龟汤有什么可复用方法？",
            limit=3,
        )
        synthesis_envelope = next(
            item for item in envelopes if item["item_ref"] == synthesis_ref
        )
        self.assertIn("integration_source_match", synthesis_envelope["reason_codes"])
        self.assertEqual(
            4,
            len(synthesis_envelope["module_extension"]["learning"][
                "integration_source_match_refs"
            ]),
        )
        self.assertEqual(
            before_reads,
            self.store.status(owner_id=self.owner, model_id=self.model)["row_version"],
        )

    def test_invalid_integration_source_hash_does_not_create_retrieval_lineage(self) -> None:
        first = self.remember(
            0,
            title="海龟汤：来源甲",
            scene_tags=["海龟汤"],
            keywords=["海龟汤"],
            referent_bindings=[],
        )
        second = self.remember(
            1,
            title="海龟汤：来源乙",
            scene_tags=["海龟汤"],
            keywords=["海龟汤"],
            referent_bindings=[],
        )
        candidate = self.integration_candidate(
            row_version=2,
            wake_seq=3,
            sources=[first, second],
            suffix="无主题字面的综合",
        )
        self.store.review_snapshot(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake-hash-review",
            wake_seq=4,
        )
        accepted = self.store.review_change(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake-hash-review",
            wake_seq=4,
            expected_row_version=3,
            candidate_id=str(candidate["candidate_id"]),
            expected_candidate_version=1,
            expected_candidate_hash=str(candidate["candidate_hash"]),
            expected_base_version=0,
            action="accept",
            correctness_assessment="用于验证损坏谱系不会被检索。",
            calm_check=self.calm_check(first, second),
            reason="先完成正常跨唤醒接受。",
            ai_confirmation=True,
        )
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE learning_integrations SET source_hash=? WHERE candidate_id=?",
                ("sha256-corrupted-lineage", candidate["candidate_id"]),
            )
        inventory = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            view="inventory",
            query="海龟汤",
            limit=20,
        )
        self.assertEqual(2, inventory["matched_count"])
        self.assertNotIn(
            accepted["learning_id"],
            {item["learning_id"] for item in inventory["items"]},
        )

    def test_archived_sources_remain_safe_topic_lineage_for_active_synthesis(self) -> None:
        sources = [
            self.remember(
                index,
                title=f"海龟汤：归档来源{index + 1}",
                scene_tags=["海龟汤"],
                keywords=["海龟汤"],
                referent_bindings=[],
            )
            for index in range(2)
        ]
        candidate = legacy_integration_candidate(self.store,
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake-archive-integrate",
            wake_seq=3,
            expected_row_version=2,
            source_learning_ids=[str(item["learning_id"]) for item in sources],
            synthesis_kind="summary",
            classification_actor="ai_self",
            classification_basis=["来源属于同一推理主题"],
            correctness_assessment="综合内容仍是可复核概要。",
            diff="新增高层综合并在接受后归档来源。",
            calm_check=self.calm_check(*sources),
            reason="验证归档来源仍能提供安全主题谱系。",
            source_action="archive_after_accept",
            create_idea=False,
            **self.card(
                "高层复核综合",
                title="不含来源主题字面的高层综合",
                scene_tags=["复核"],
                keywords=["综合"],
                entities=[],
                referent_bindings=[],
            ),
        )
        self.store.review_snapshot(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake-archive-review",
            wake_seq=4,
        )
        accepted = self.store.review_change(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake-archive-review",
            wake_seq=4,
            expected_row_version=3,
            candidate_id=str(candidate["candidate_id"]),
            expected_candidate_version=1,
            expected_candidate_hash=str(candidate["candidate_hash"]),
            expected_base_version=0,
            action="accept",
            correctness_assessment="来源边界完整，接受综合并归档来源。",
            calm_check=self.calm_check(*sources),
            reason="跨唤醒复核通过。",
            ai_confirmation=True,
        )

        active_only = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            view="inventory",
            query="海龟汤",
            limit=20,
        )
        self.assertEqual(1, active_only["matched_count"])
        self.assertEqual(accepted["learning_id"], active_only["items"][0]["learning_id"])
        self.assertEqual("active", active_only["items"][0]["lifecycle"])

        with_archived = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            view="inventory",
            query="海龟汤",
            include_archived=True,
            limit=20,
        )
        self.assertEqual(3, with_archived["matched_count"])
        self.assertEqual(
            {"active", "archived"},
            {item["lifecycle"] for item in with_archived["items"]},
        )

    def test_semantic_recall_explicitly_disclaims_inventory_completeness(self) -> None:
        stored = self.remember()
        recalled = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            query="上次读的小说",
        )
        self.assertEqual(stored["item_ref"], recalled["results"][0]["item_ref"])
        self.assertEqual("semantic_search", recalled["retrieval_mode"])
        self.assertFalse(recalled["inventory_answer_supported"])
        self.assertFalse(recalled["exhaustive_inventory"])
        self.assertEqual(1, recalled["active_inventory_count"])
        self.assertEqual(
            {"view": "inventory", "query": ""},
            recalled["inventory_lookup"]["arguments"],
        )

    def test_three_independent_sources_create_complete_optional_merge_suggestion(self) -> None:
        first = self.remember(0, "一")
        second = self.remember(1, "二")
        third = self.remember(2, "三")
        self.assertIsNone(first["merge_suggestion"])
        self.assertIsNone(second["merge_suggestion"])
        suggestion = third["merge_suggestion"]
        self.assertIsNotNone(suggestion)
        assert isinstance(suggestion, dict)
        self.assertEqual("learning-merge/0.3", suggestion["merge_policy_version"])
        self.assertEqual(0.40, suggestion["minimum_salience"])
        self.assertEqual("open", suggestion["status"])
        self.assertEqual("ai_self_optional", suggestion["decision_authority"])
        self.assertEqual(3, len(suggestion["member_refs"]))
        self.assertEqual(3, len(suggestion["counted_occurrences"]))
        self.assertEqual([], suggestion["excluded_occurrences"])
        self.assertTrue(all(
            item["occurrence_salience"] >= 0.40
            for item in suggestion["counted_occurrences"]
        ))
        self.assertTrue({
            "suggestion_id", "member_refs", "similarity_reasons",
            "merge_policy_version", "window_started_at", "window_ended_at",
            "minimum_salience", "counted_occurrences", "excluded_occurrences",
            "status", "created_at", "expires_at", "decision_authority",
        } <= set(suggestion))
        status = self.store.status(owner_id=self.owner, model_id=self.model)
        self.assertEqual(status["counts"]["active_items"], 3)
        self.assertEqual(status["counts"]["open_merge_suggestions"], 1)

        recalled = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            query="继续上次读的小说",
            include_merge_suggestions=True,
        )
        persisted = next(
            item for item in recalled["merge_suggestions"]
            if item["suggestion_id"] == suggestion["suggestion_id"]
        )
        for key in {
            "member_refs", "similarity_reasons", "merge_policy_version",
            "window_started_at", "window_ended_at", "minimum_salience",
            "counted_occurrences", "excluded_occurrences", "status",
            "created_at", "expires_at",
        }:
            self.assertEqual(suggestion[key], persisted[key])

    def test_same_evidence_source_cannot_fill_merge_threshold(self) -> None:
        shared_source = "conversation://reading/shared-source"
        results = [
            self.remember(
                index,
                suffix,
                evidence=[self.evidence_item(
                    index,
                    source_ref=shared_source,
                    content_hash=f"sha256-distinct-{index}",
                )],
            )
            for index, suffix in enumerate(("一", "二", "三"))
        ]
        third = results[-1]
        self.assertIsNone(third["merge_suggestion"])
        self.assertEqual(1, third["merge_evaluation"]["unique_occurrence_count"])
        self.assertFalse(third["merge_evaluation"]["current_occurrence_counted"])
        self.assertIn(
            "shared_evidence_source",
            {
                reason
                for item in third["merge_evaluation"]["excluded_occurrences"]
                for reason in item["reason_codes"]
            },
        )

    def test_duplicate_evidence_content_cannot_fill_merge_threshold(self) -> None:
        shared_hash = "sha256-identical-evidence-copy"
        results = [
            self.remember(
                index,
                suffix,
                evidence=[self.evidence_item(
                    index,
                    source_ref=f"conversation://reading/distinct-{index}",
                    content_hash=shared_hash,
                )],
            )
            for index, suffix in enumerate(("一", "二", "三"))
        ]
        third = results[-1]
        self.assertIsNone(third["merge_suggestion"])
        self.assertEqual(1, third["merge_evaluation"]["unique_occurrence_count"])
        self.assertIn(
            "duplicate_evidence_content",
            {
                reason
                for item in third["merge_evaluation"]["excluded_occurrences"]
                for reason in item["reason_codes"]
            },
        )

    def test_same_server_wake_cannot_fill_merge_threshold(self) -> None:
        results = [
            self.remember(
                index,
                suffix,
                wake_id="wake-shared-retry",
                wake_seq=1,
                evidence=[self.evidence_item(
                    index,
                    source_ref=f"conversation://reading/distinct-{index}",
                    content_hash=f"sha256-distinct-{index}",
                )],
            )
            for index, suffix in enumerate(("一", "二", "三"))
        ]
        third = results[-1]
        self.assertIsNone(third["merge_suggestion"])
        self.assertEqual(1, third["merge_evaluation"]["unique_occurrence_count"])
        self.assertIn(
            "same_server_wake",
            {
                reason
                for item in third["merge_evaluation"]["excluded_occurrences"]
                for reason in item["reason_codes"]
            },
        )

    def test_exact_no_evidence_copies_cannot_fill_merge_threshold(self) -> None:
        results = [
            self.remember(index, evidence=None)
            for index in range(3)
        ]
        third = results[-1]
        self.assertIsNone(third["merge_suggestion"])
        self.assertEqual(1, third["merge_evaluation"]["unique_occurrence_count"])
        self.assertIn(
            "exact_no_evidence_copy",
            {
                reason
                for item in third["merge_evaluation"]["excluded_occurrences"]
                for reason in item["reason_codes"]
            },
        )

    def test_merge_exclusions_are_reasoned_and_auditable_without_a_suggestion(self) -> None:
        self.remember(0, "基准")
        old = self.remember(1, "旧记录")
        with closing(sqlite3.connect(self.main_db)) as connection:
            connection.execute(
                "UPDATE learning_items SET created_at=? WHERE learning_id=?",
                ("2020-01-01T00:00:00.000000Z", old["learning_id"]),
            )
            connection.commit()
        low = self.remember(2, "低显著", importance=39)
        archived = self.remember(3, "已归档", lifecycle="archived")
        quarantined = self.remember(4, "已隔离", lifecycle="quarantined")

        self.assertIsNone(quarantined["merge_suggestion"])
        excluded_by_ref = {
            item["ref"]: set(item["reason_codes"])
            for item in quarantined["merge_evaluation"]["excluded_occurrences"]
        }
        self.assertIn("outside_active_window", excluded_by_ref[old["item_ref"]])
        self.assertIn("below_minimum_salience", excluded_by_ref[low["item_ref"]])
        self.assertIn("ineligible_lifecycle", excluded_by_ref[archived["item_ref"]])
        self.assertIn("ineligible_lifecycle", excluded_by_ref[quarantined["item_ref"]])

        with closing(sqlite3.connect(self.main_db)) as connection:
            row = connection.execute(
                "SELECT details_json FROM learning_audit_events WHERE event_id=?",
                (quarantined["event_id"],),
            ).fetchone()
        self.assertIsNotNone(row)
        audit_evaluation = json.loads(row[0])["merge_evaluation"]
        self.assertEqual(
            quarantined["merge_evaluation"]["counted_occurrences"],
            audit_evaluation["counted_occurrences"],
        )
        self.assertEqual(
            quarantined["merge_evaluation"]["excluded_occurrences"],
            audit_evaluation["excluded_occurrences"],
        )

    def test_fourth_duplicate_occurrence_does_not_create_another_suggestion(self) -> None:
        first = self.remember(
            0, "一", evidence=[self.evidence_item("source-a")]
        )
        self.remember(1, "二", evidence=[self.evidence_item("source-b")])
        third = self.remember(2, "三", evidence=[self.evidence_item("source-c")])
        self.assertIsNotNone(third["merge_suggestion"])

        duplicate = self.remember(
            3,
            "四",
            evidence=[self.evidence_item(
                "source-a-copy",
                source_ref="conversation://reading/source-a",
                content_hash="sha256-distinct-copy",
            )],
        )
        self.assertIsNone(duplicate["merge_suggestion"])
        self.assertFalse(duplicate["merge_evaluation"]["current_occurrence_counted"])
        self.assertEqual(
            1,
            self.store.status(owner_id=self.owner, model_id=self.model)["counts"][
                "open_merge_suggestions"
            ],
        )

    def test_merge_suggestion_never_exceeds_integration_source_limit(self) -> None:
        result: dict[str, object] | None = None
        for index in range(21):
            result = self.remember(index, f"独立{index}")
        assert result is not None
        suggestion = result["merge_suggestion"]
        self.assertIsNotNone(suggestion)
        assert isinstance(suggestion, dict)
        self.assertEqual(20, len(suggestion["member_refs"]))
        self.assertIn(result["item_ref"], suggestion["member_refs"])
        self.assertIn(
            "integration_source_limit",
            {
                reason
                for item in suggestion["excluded_occurrences"]
                for reason in item["reason_codes"]
            },
        )

    def test_integrate_rejects_merge_suggestion_member_mismatch_without_side_effects(self) -> None:
        first = self.remember(0, "一")
        second = self.remember(1, "二")
        third = self.remember(2, "三")
        suggestion_id = third["merge_suggestion"]["suggestion_id"]
        before = self.store.status(owner_id=self.owner, model_id=self.model)

        with self.assertRaisesRegex(LearningMemoryError, "merge_suggestion_source_mismatch"):
            self.integration_candidate(
                row_version=3,
                wake_seq=4,
                sources=[first, second],
                suffix="错误成员集合",
                merge_suggestion_id=suggestion_id,
            )

        after = self.store.status(owner_id=self.owner, model_id=self.model)
        self.assertEqual(before["row_version"], after["row_version"])
        self.assertEqual(before["counts"]["pending_changes"], after["counts"]["pending_changes"])
        self.assertEqual(1, after["counts"]["open_merge_suggestions"])

    def test_integrate_rejects_expired_merge_suggestion_without_side_effects(self) -> None:
        first = self.remember(0, "一")
        second = self.remember(1, "二")
        third = self.remember(2, "三")
        suggestion_id = third["merge_suggestion"]["suggestion_id"]
        with closing(sqlite3.connect(self.main_db)) as connection:
            connection.execute(
                "UPDATE learning_merge_suggestions SET expires_at=? WHERE suggestion_id=?",
                ("2020-01-01T00:00:00.000000Z", suggestion_id),
            )
            connection.commit()
        before = self.store.status(owner_id=self.owner, model_id=self.model)

        with self.assertRaisesRegex(LearningMemoryError, "merge_suggestion_expired"):
            self.integration_candidate(
                row_version=3,
                wake_seq=4,
                sources=[first, second, third],
                suffix="过期建议",
                merge_suggestion_id=suggestion_id,
            )

        after = self.store.status(owner_id=self.owner, model_id=self.model)
        self.assertEqual(before["row_version"], after["row_version"])
        self.assertEqual(before["counts"]["pending_changes"], after["counts"]["pending_changes"])

    def test_disputed_card_reports_quarantine_and_never_auto_projects(self) -> None:
        stored = self.remember(
            kind="concept",
            title="猫咪蒜瓣毛原因（待核验）",
            summary="有人提出猫咪蒜瓣毛可能由单一原因造成，但该断言正受实质质疑。",
            current_understanding="这项断言尚不能作为后台知识使用。",
            application_contexts=["回想猫咪蒜瓣毛的成因"],
            scene_tags=["猫咪蒜瓣毛", "养猫"],
            preceding_context_summary="",
            uncertainties=["成因未经核验"],
            domain="宠物养护",
            keywords=["猫咪", "蒜瓣毛", "成因"],
            entities=["猫咪"],
            epistemic_status="disputed",
            confidence=35,
            importance=40,
            referent_bindings=[],
        )
        self.assertEqual("stored_quarantined", stored["decision"])
        self.assertEqual("quarantined", stored["effective_lifecycle"])
        self.assertFalse(stored["automatic_recall_eligible"])
        self.assertIn("not_eligible_for_automatic_recall", stored["reason_codes"])
        self.assertEqual(
            [],
            self.store.build_envelopes(
                owner_id=self.owner,
                model_id=self.model,
                query="你还记得之前说的猫咪蒜瓣毛原因吗？",
            ),
        )
        hidden = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            target_ref=stored["item_ref"],
        )
        self.assertEqual(0, hidden["result_count"])
        visible = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            target_ref=stored["item_ref"],
            include_pending=True,
        )
        self.assertEqual(1, visible["result_count"])
        self.assertEqual("quarantined", visible["results"][0]["lifecycle"])

    def test_new_challenged_claim_requires_matching_evidence_and_old_active_flag_is_gated(self) -> None:
        fields = self.card()
        fields.pop("epistemic_status")
        fields["source_basis"] = "reported"
        fields["claim_review"] = {
            "status": "challenged",
            "challenged_claim": "主角已经确认符号来源",
            "challenge_actor": "本 AI",
            "challenge_basis": "共读文本尚未出现该确认。",
            "challenge_evidence_refs": ["document://chapter-notes"],
        }
        with self.assertRaisesRegex(LearningMemoryError, "challenge_evidence_ref_not_found"):
            self.store.remember(
                owner_id=self.owner, model_id=self.model, wake_id="wake-challenge-bad", wake_seq=1,
                expected_row_version=0, correctness_assessment="该断言受到文本证据质疑。",
                reason="验证质疑必须绑定证据。", evidence=[], **fields,
            )
        stored = self.store.remember(
            owner_id=self.owner, model_id=self.model, wake_id="wake-challenge-good", wake_seq=2,
            expected_row_version=0, correctness_assessment="该断言受到文本证据质疑。",
            reason="保存结构化质疑并隔离。",
            evidence=[{
                "source_kind": "document", "source_ref": "document://chapter-notes",
                "source_trust": "verified", "evidence_summary": "章节笔记未确认符号来源。",
                "content_hash": "sha256-challenge", "observed_at": "2026-08-29T00:00:00Z",
            }],
            **fields,
        )
        self.assertEqual("stored_quarantined", stored["decision"])
        self.assertEqual(1, self.store.status(owner_id=self.owner, model_id=self.model)["row_version"])

        # Even a malformed legacy database row whose outer lifecycle was
        # accidentally promoted cannot cross the automatic-recall gate.
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE learning_items SET lifecycle='active' WHERE learning_id=?",
                (stored["learning_id"],),
            )
        self.assertEqual(
            [],
            self.store.build_envelopes(
                owner_id=self.owner,
                model_id=self.model,
                query="我们继续上次读的小说并回想旧城符号吧",
            ),
        )

    def test_reported_contrast_pair_multi_cue_recall_projects_one_neutral_hint(self) -> None:
        first = self.remember(
            kind="concept",
            title="猫咪蒜瓣毛原因（说法A：太胖）",
            summary="昕昕报告一种说法：猫咪蒜瓣毛是太胖导致毛发分层。",
            current_understanding="这是人类报告、尚未核验的一种成因解释。",
            application_contexts=["回想猫咪蒜瓣毛的成因"],
            scene_tags=["猫咪蒜瓣毛", "养猫", "猫毛"],
            preceding_context_summary="",
            uncertainties=["尚未核验；另有相反解释"],
            domain="宠物养护",
            keywords=["猫咪", "蒜瓣毛", "太胖", "毛分层"],
            entities=["猫咪"],
            epistemic_status="reported",
            confidence=35,
            importance=40,
            context_policy="normal",
            referent_bindings=[],
        )
        second = self.remember(
            1,
            kind="concept",
            title="猫咪蒜瓣毛原因（说法B：脏了）",
            summary="昕昕报告另一种说法：猫咪蒜瓣毛不是太胖，而是脏了需要清洁。",
            current_understanding="这是人类报告、尚未核验的相反成因解释。",
            application_contexts=["回想猫咪蒜瓣毛的成因"],
            scene_tags=["猫咪蒜瓣毛", "养猫", "猫毛"],
            preceding_context_summary="",
            uncertainties=["尚未核验；与太胖解释互斥"],
            domain="宠物养护",
            keywords=["猫咪", "蒜瓣毛", "脏了", "清洁"],
            entities=["猫咪"],
            epistemic_status="reported",
            confidence=35,
            importance=40,
            context_policy="normal",
            referent_bindings=[],
            links=[
                {
                    "relation_type": "contrast",
                    "target_ref": first["item_ref"],
                    "weight": 80,
                    "basis": {
                        "subject_key": "猫咪身上的蒜瓣毛",
                        "scope_signature": "普通养猫场景下蒜瓣毛的主要成因",
                        "time_condition": "timeless",
                        "predicate_signature": "蒜瓣毛的主要成因",
                        "mutual_exclusivity_basis": "一方断言主要因太胖，另一方断言不是太胖而是脏污。",
                    },
                }
            ],
        )
        self.assertEqual("active", first["effective_lifecycle"])
        self.assertEqual("active", second["effective_lifecycle"])
        before = self.store.status(owner_id=self.owner, model_id=self.model)
        question = "阿止，不调用工具，你还记得之前我们说猫咪身上具有蒜瓣毛的原因吗？"
        envelopes = self.store.build_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query=question,
        )
        self.assertEqual(1, len(envelopes))
        envelope = envelopes[0]
        self.assertEqual("neutral_hint", envelope["gate_decision"])
        self.assertEqual("neutral_hint", envelope["presentation"])
        self.assertIn("multi_cue_recall", envelope["reason_codes"])
        self.assertIn("contrast_hint", envelope["content"])
        self.assertTrue(envelope["module_extension"]["learning"]["contrast_present"])
        self.assertIn(
            envelope["content"]["summary"],
            {
                "昕昕报告一种说法：猫咪蒜瓣毛是太胖导致毛发分层。",
                "昕昕报告另一种说法：猫咪蒜瓣毛不是太胖，而是脏了需要清洁。",
            },
        )
        self.assertNotIn("current_understanding", envelope["content"])
        preview = self.store.preview_recall(
            owner_id=self.owner,
            model_id=self.model,
            situation=question,
        )
        self.assertEqual(1, preview["candidate_count"])
        self.assertEqual(before, self.store.status(owner_id=self.owner, model_id=self.model))

        self.assertEqual(
            [],
            self.store.build_envelopes(
                owner_id=self.owner,
                model_id=self.model,
                query="猫咪",
            ),
        )
        self.assertEqual(
            [],
            self.store.build_envelopes(
                owner_id=self.owner,
                model_id=self.model,
                query="蒜瓣毛",
            ),
        )

    def test_contrast_link_shape_and_target_are_validated(self) -> None:
        first = self.remember()
        basis = {
            "subject_key": "同一主体",
            "scope_signature": "同一范围",
            "time_condition": "timeless",
            "predicate_signature": "同一谓词",
            "mutual_exclusivity_basis": "两个结论不能同时成立",
        }
        with self.assertRaisesRegex(LearningMemoryError, "link_item_shape_invalid"):
            self.remember(
                1,
                links=[{
                    "edge_type": "contrast",
                    "target_ref": first["item_ref"],
                    "basis": basis,
                }],
            )
        with self.assertRaisesRegex(LearningMemoryError, "contrast_basis_incomplete"):
            self.remember(
                1,
                links=[{
                    "relation_type": "contrast",
                    "target_ref": first["item_ref"],
                    "basis": {"subject_key": "同一主体"},
                }],
            )
        with self.assertRaisesRegex(LearningMemoryError, "learning_link_target_not_found"):
            self.remember(
                1,
                links=[{
                    "relation_type": "contrast",
                    "target_ref": "learning://learn_missing@1",
                    "basis": basis,
                }],
            )
        self.assertEqual(1, self.store.status(owner_id=self.owner, model_id=self.model)["row_version"])

    def test_quarantined_contrast_endpoint_cannot_emit_automatic_hint(self) -> None:
        first = self.remember(
            title="猫咪蒜瓣毛可召回说法",
            summary="人类报告一种关于猫咪蒜瓣毛成因的说法。",
            current_understanding="这是一条低置信的人类报告。",
            scene_tags=["猫咪蒜瓣毛", "养猫"],
            application_contexts=["回想猫咪蒜瓣毛"],
            keywords=["猫咪", "蒜瓣毛"],
            entities=["猫咪"],
            uncertainties=["尚未核验"],
            epistemic_status="reported",
            confidence=35,
            referent_bindings=[],
        )
        with self.assertRaisesRegex(
            LearningMemoryError, "contrast_cannot_mark_claim_challenged"
        ):
            self.remember(
                1,
                title="猫咪蒜瓣毛受质疑说法",
                summary="另一项断言本身已受到实质质疑。",
                current_understanding="该断言不能进入普通后台知识。",
                scene_tags=["猫咪蒜瓣毛", "养猫"],
                application_contexts=["回想猫咪蒜瓣毛"],
                keywords=["猫咪", "蒜瓣毛"],
                entities=["猫咪"],
                uncertainties=["可信性受到实质质疑"],
                epistemic_status="disputed",
                confidence=20,
                referent_bindings=[],
                links=[{
                    "relation_type": "contrast",
                    "target_ref": first["item_ref"],
                    "basis": {
                        "subject_key": "猫咪蒜瓣毛",
                        "scope_signature": "同一养猫场景",
                        "time_condition": "timeless",
                        "predicate_signature": "蒜瓣毛的主要成因",
                        "mutual_exclusivity_basis": "两种主要成因解释互斥",
                    },
                }],
            )
        self.assertEqual(
            1, self.store.status(owner_id=self.owner, model_id=self.model)["row_version"]
        )
        envelopes = self.store.build_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query="你还记得之前猫咪蒜瓣毛的原因吗？",
        )
        self.assertEqual(1, len(envelopes))
        self.assertNotIn("contrast_hint", envelopes[0]["content"])
        default_recall = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            target_ref=first["item_ref"],
        )
        self.assertEqual([], default_recall["results"][0]["contrasts"])
        with closing(sqlite3.connect(self.main_db)) as connection:
            self.assertEqual(
                0, connection.execute("SELECT COUNT(*) FROM learning_links").fetchone()[0]
            )

    def test_atomic_contrast_pair_is_recallable_and_exact_retry_is_idempotent(self) -> None:
        first_claim = {
            "title": "猫咪蒜瓣毛说法甲",
            "summary": "有人说猫咪蒜瓣毛主要是因为太胖。",
            "current_understanding": "这是人类提供、尚未独立核验的说法。",
            "source_basis": "reported",
            "confidence": 35,
            "uncertainties": ["尚未核验"],
        }
        second_claim = {
            "title": "猫咪蒜瓣毛说法乙",
            "summary": "另有人说猫咪蒜瓣毛主要是因为脏了。",
            "current_understanding": "这是另一条尚未独立核验的相反说法。",
            "source_basis": "reported",
            "confidence": 35,
            "uncertainties": ["尚未核验"],
        }
        basis = {
            "subject_key": "猫咪蒜瓣毛",
            "scope_signature": "同一养猫场景",
            "time_condition": "timeless",
            "predicate_signature": "蒜瓣毛的主要成因",
            "mutual_exclusivity_basis": "两种主要成因解释互斥",
        }
        arguments = {
            "owner_id": self.owner,
            "model_id": self.model,
            "wake_id": "wake-pair",
            "wake_seq": 1,
            "expected_row_version": 0,
            "first_claim": first_claim,
            "second_claim": second_claim,
            "contrast_basis": basis,
            "correctness_assessment": "两条均是尚未核验的人类说法，不在这里代替证据裁决。",
            "reason": "未来提到猫咪蒜瓣毛时应能想起存在两种相反解释。",
            "kind": "concept",
            "application_contexts": ["回想猫咪蒜瓣毛的成因"],
            "scene_tags": ["猫咪蒜瓣毛", "养猫"],
            "domain": "宠物养护",
            "keywords": ["猫咪", "蒜瓣毛"],
            "entities": ["猫咪"],
        }
        stored = self.store.remember_contrast_pair(**arguments)
        self.assertEqual("stored_contrast_pair", stored["decision"])
        self.assertEqual(1, stored["learning_row_version"])
        self.assertFalse(stored["idempotent_replay"])
        status = self.store.status(owner_id=self.owner, model_id=self.model)
        self.assertEqual(2, status["counts"]["active_items"])
        self.assertEqual(0, status["counts"]["quarantined_items"])
        with closing(sqlite3.connect(self.main_db)) as connection:
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM learning_versions").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM learning_links").fetchone()[0])
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM learning_audit_events").fetchone()[0])

        envelopes = self.store.build_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query="你还记得猫咪身上的蒜瓣毛和养猫时的成因说法吗？",
        )
        self.assertEqual(1, len(envelopes))
        self.assertEqual("neutral_hint", envelopes[0]["gate_decision"])
        self.assertIn("contrast_hint", envelopes[0]["content"])

        replay = self.store.remember_contrast_pair(**arguments)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(stored["first"]["item_ref"], replay["first"]["item_ref"])
        self.assertEqual(stored["second"]["item_ref"], replay["second"]["item_ref"])
        self.assertEqual(1, self.store.status(owner_id=self.owner, model_id=self.model)["row_version"])

    def test_atomic_contrast_pair_validation_failure_has_zero_side_effects(self) -> None:
        first = {
            "title": "说法甲", "summary": "甲结论", "current_understanding": "甲理解",
            "source_basis": "reported", "confidence": 30, "uncertainties": ["未核验"],
        }
        second = {
            "title": "说法乙", "summary": "乙结论",
            "current_understanding": "token=super-secret-value-1234567890",
            "source_basis": "reported", "confidence": 30, "uncertainties": ["未核验"],
        }
        with self.assertRaisesRegex(LearningMemoryError, "credential_or_secret_detected"):
            self.store.remember_contrast_pair(
                owner_id=self.owner, model_id=self.model, wake_id="wake-fail", wake_seq=1,
                expected_row_version=0, first_claim=first, second_claim=second,
                contrast_basis={
                    "subject_key": "主体", "scope_signature": "范围",
                    "time_condition": "timeless", "predicate_signature": "谓词",
                    "mutual_exclusivity_basis": "互斥",
                },
                correctness_assessment="尚未核验。", reason="测试原子失败。",
                kind="concept", application_contexts=["测试场景"],
                scene_tags=["线索甲", "线索乙"],
            )
        self.assertEqual(0, self.store.status(owner_id=self.owner, model_id=self.model)["row_version"])
        with closing(sqlite3.connect(self.main_db)) as connection:
            for table in ("learning_items", "learning_versions", "learning_links", "learning_audit_events"):
                self.assertEqual(0, connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_stale_contrast_link_cannot_emit_automatic_hint(self) -> None:
        first = self.remember(
            title="猫咪蒜瓣毛说法甲",
            summary="有人说猫咪蒜瓣毛主要是因为太胖。",
            current_understanding="这是人类提供、尚未独立核验的说法。",
            epistemic_status="reported",
            confidence=35,
            keywords=["猫咪", "蒜瓣毛", "太胖"],
            scene_tags=["猫咪蒜瓣毛", "养猫"],
        )
        second = self.remember(
            1,
            title="猫咪蒜瓣毛说法乙",
            summary="另有人说猫咪蒜瓣毛主要是因为脏了。",
            current_understanding="这是另一条人类提供、尚未独立核验的相反说法。",
            epistemic_status="reported",
            confidence=35,
            keywords=["猫咪", "蒜瓣毛", "洗澡"],
            scene_tags=["猫咪蒜瓣毛", "养猫"],
            links=[{
                "relation_type": "contrast",
                "target_ref": first["item_ref"],
                "basis": {
                    "subject_key": "猫咪蒜瓣毛",
                    "scope_signature": "同一养猫场景",
                    "time_condition": "timeless",
                    "predicate_signature": "蒜瓣毛的主要成因",
                    "mutual_exclusivity_basis": "两种主要成因解释互斥",
                },
            }],
        )
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE learning_items SET current_version=2 WHERE learning_id=?",
                (first["learning_id"],),
            )
        envelopes = self.store.build_envelopes(
            owner_id=self.owner,
            model_id=self.model,
            query="你还记得猫咪蒜瓣毛的原因吗",
        )
        self.assertEqual(len(envelopes), 2)
        selected = {item["item_ref"] for item in envelopes}
        self.assertIn(second["item_ref"], selected)
        self.assertTrue(all("contrast_hint" not in item["content"] for item in envelopes))
        self.assertFalse(envelopes[0]["module_extension"]["learning"]["contrast_present"])

    def test_idea_box_is_separate_labeled_and_never_auto_projected(self) -> None:
        one = self.remember(0, "甲")
        two = self.remember(1, "乙")
        calm = {
            "evidence_sufficient": True,
            "counterevidence_checked": True,
            "scope_changed": False,
            "affected_links_checked": True,
            "single_turn_pressure_absent": True,
            "rollback_understood": True,
            "notes": "已比较来源，综合和创意分别保存。",
            "evidence_refs": [one["item_ref"], two["item_ref"]],
        }
        result = self.store.integrate(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake-integrate",
            wake_seq=3,
            expected_row_version=2,
            source_learning_ids=[one["item_ref"], two["item_ref"]],
            synthesis_kind="summary",
            classification_actor="ai_self",
            classification_basis=["两张卡描述同一阅读进度"],
            correctness_assessment="综合内容仍是概要。",
            diff="建立一个待复核的综合卡。",
            calm_check=calm,
            reason="减少重复，同时保留来源。",
            create_idea=True,
            idea_kind="hypothesis",
            idea_text="符号可能与旧城的时间循环有关。",
            idea_inference_chain=["两处符号都出现在时间异常附近"],
            idea_uncertainties=["尚无原文证据确认"],
            **self.card("综合", referent_bindings=[]),
        )
        self.assertEqual(result["idea_box"]["forced_label"], IDEA_BOX_LABEL)
        preview = self.store.preview_recall(
            owner_id=self.owner, model_id=self.model, situation="继续上次读的小说"
        )
        self.assertNotIn("时间循环", json.dumps(preview, ensure_ascii=False))
        ideas = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            query="时间循环",
            include_idea_box=True,
        )
        self.assertEqual(ideas["results"][0]["forced_label"], IDEA_BOX_LABEL)

    def test_legacy_candidate_can_be_reviewed_same_wake_without_calm_questionnaire(self) -> None:
        one, two = self.remember(0, "甲"), self.remember(1, "乙")
        candidate = self.integration_candidate(row_version=2, wake_seq=3,
                                               sources=[one, two], suffix="历史候选")
        snapshot = self.store.review_snapshot(owner_id=self.owner, model_id=self.model,
                                             wake_id="review", wake_seq=3)
        shown = snapshot["candidates"][0]
        self.assertTrue(shown["fully_presented"])
        self.assertFalse(shown["review_requires_later_wake"])
        self.assertEqual(2, len(shown["source_review_material"]))
        fields = dict(owner_id=self.owner, model_id=self.model, wake_id="review",
                      wake_seq=3, expected_row_version=3,
                      candidate_id=candidate["candidate_id"], expected_candidate_version=1,
                      expected_candidate_hash=candidate["candidate_hash"], expected_base_version=0,
                      action="accept")
        with self.assertRaisesRegex(LearningMemoryError, "ai_confirmation_required"):
            self.store.review_change(**fields)
        with self.assertRaisesRegex(LearningMemoryError, "stale_candidate"):
            self.store.review_change(**{**fields, "expected_candidate_hash": "wrong"}, ai_confirmation=True)
        result = self.store.review_change(**fields, ai_confirmation=True)
        self.assertEqual("accepted", result["decision"])
        self.assertEqual(0, self.store.status(owner_id=self.owner, model_id=self.model)["counts"]["pending_changes"])

    def test_incomplete_candidate_is_reject_only_and_reveals_next_candidate(self) -> None:
        one = self.remember(0, "不完整来源甲")
        two = self.remember(1, "共享来源乙")
        three = self.remember(2, "下一候选来源丙")
        incomplete = self.integration_candidate(
            row_version=3,
            wake_seq=4,
            sources=[one, two],
            suffix="不完整候选",
        )
        following = self.integration_candidate(
            row_version=4,
            wake_seq=5,
            sources=[two, three],
            suffix="下一候选",
        )
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE learning_change_candidates SET created_at=? WHERE candidate_id=?",
                ("2026-08-29T00:00:00.000000Z", incomplete["candidate_id"]),
            )
            connection.execute(
                "UPDATE learning_change_candidates SET created_at=? WHERE candidate_id=?",
                ("2026-08-29T00:00:01.000000Z", following["candidate_id"]),
            )
            connection.execute(
                "DELETE FROM learning_versions WHERE learning_id=? AND version=1",
                (one["learning_id"],),
            )

        snapshot = self.store.review_snapshot(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake-incomplete-review",
            wake_seq=6,
        )
        shown = snapshot["candidates"][0]
        self.assertEqual(incomplete["candidate_id"], shown["candidate_id"])
        self.assertEqual("metadata_only", shown["presentation_mode"])
        self.assertFalse(shown["fully_presented"])
        self.assertEqual("review_material_incomplete", shown["review_blocked_reason"])

        review_arguments = {
            "owner_id": self.owner,
            "model_id": self.model,
            "wake_id": "wake-incomplete-review",
            "wake_seq": 6,
            "expected_row_version": 5,
            "candidate_id": incomplete["candidate_id"],
            "expected_candidate_version": 1,
            "expected_candidate_hash": incomplete["candidate_hash"],
            "expected_base_version": 0,
            "correctness_assessment": "来源快照已不完整，不能激活该候选。",
            "calm_check": self.calm_check(one, two),
            "reason": "拒绝不可完整审核的队首候选。",
            "ai_confirmation": True,
        }
        with self.assertRaisesRegex(
            LearningMemoryError, "candidate_not_fully_presented"
        ):
            self.store.review_change(action="accept", **review_arguments)

        rejected = self.store.review_change(action="reject", **review_arguments)
        self.assertEqual("rejected", rejected["decision"])
        self.assertIn("incomplete_review_material_rejected", rejected["reason_codes"])
        next_snapshot = self.store.review_snapshot(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake-following-review",
            wake_seq=7,
        )
        self.assertEqual(following["candidate_id"], next_snapshot["candidates"][0]["candidate_id"])
        self.assertTrue(next_snapshot["candidates"][0]["fully_presented"])

    def test_stale_source_accept_fails_but_same_presentation_can_reject(self) -> None:
        one = self.remember(0, "陈旧来源甲")
        two = self.remember(1, "陈旧来源乙")
        candidate = self.integration_candidate(
            row_version=2,
            wake_seq=3,
            sources=[one, two],
            suffix="陈旧来源候选",
        )
        snapshot = self.store.review_snapshot(
            owner_id=self.owner,
            model_id=self.model,
            wake_id="wake-stale-review",
            wake_seq=4,
        )
        self.assertTrue(snapshot["candidates"][0]["fully_presented"])
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE learning_items SET current_version=2, current_hash=? WHERE learning_id=?",
                ("sha256-stale-current-source", one["learning_id"]),
            )

        review_arguments = {
            "owner_id": self.owner,
            "model_id": self.model,
            "wake_id": "wake-stale-review",
            "wake_seq": 4,
            "expected_row_version": 3,
            "candidate_id": candidate["candidate_id"],
            "expected_candidate_version": 1,
            "expected_candidate_hash": candidate["candidate_hash"],
            "expected_base_version": 0,
            "correctness_assessment": "展示后发现来源当前版本已经变化。",
            "calm_check": self.calm_check(one, two),
            "reason": "陈旧候选不得激活，但应能从队列移除。",
            "ai_confirmation": True,
        }
        with self.assertRaisesRegex(LearningMemoryError, "stale_candidate"):
            self.store.review_change(action="accept", **review_arguments)
        rejected = self.store.review_change(action="reject", **review_arguments)
        self.assertEqual("rejected", rejected["decision"])
        self.assertEqual(
            0,
            self.store.status(owner_id=self.owner, model_id=self.model)["counts"]["pending_changes"],
        )

    def test_candidate_only_sentinel_never_enters_active_memory_or_recall(self) -> None:
        one = self.remember(0, "隔离来源甲")
        two = self.remember(1, "隔离来源乙")
        sentinel = "candidate-only-sentinel-7e91-never-project"
        candidate = self.integration_candidate(
            row_version=2,
            wake_seq=3,
            sources=[one, two],
            suffix=sentinel,
        )
        with self.store._connect() as connection:
            active_rows = connection.execute(
                "SELECT current_json FROM learning_items ORDER BY learning_id"
            ).fetchall()
        self.assertEqual(2, len(active_rows))
        self.assertNotIn(sentinel, "".join(row[0] for row in active_rows))

        by_target = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            target_ref=candidate["target_ref"],
            include_pending=True,
        )
        self.assertEqual(0, by_target["result_count"])
        ordinary = self.store.recall(
            owner_id=self.owner,
            model_id=self.model,
            query=sentinel,
            include_pending=True,
        )
        self.assertEqual(0, ordinary["result_count"])
        self.assertNotIn(
            sentinel,
            json.dumps(
                self.store.build_envelopes(
                    owner_id=self.owner,
                    model_id=self.model,
                    query="我们继续上次读的小说吧",
                ),
                ensure_ascii=False,
            ),
        )
        self.assertEqual(
            [],
            self.store.build_envelopes(
                owner_id=self.owner,
                model_id=self.model,
                query=sentinel,
            ),
        )

    def test_twenty_source_oversized_review_material_is_rejected_atomically(self) -> None:
        sources: list[dict[str, object]] = []
        for index in range(20):
            sources.append(
                self.remember(
                    index,
                    f"大材料{index:02d}",
                    current_understanding=(
                        f"第{index:02d}张来源卡的完整理解：" + "边界内的长内容。" * 180
                    ),
                    steps=[f"步骤{step:02d}：" + "保留合法细节。" * 30 for step in range(12)],
                    referent_bindings=[],
                )
            )
        before = self.store.status(owner_id=self.owner, model_id=self.model)
        self.assertEqual(20, before["row_version"])
        self.assertEqual(0, before["counts"]["pending_changes"])

        with self.assertRaisesRegex(
            LearningMemoryError, "candidate_review_material_too_large"
        ):
            legacy_integration_candidate(self.store,
                owner_id=self.owner,
                model_id=self.model,
                wake_id="wake-oversized-twenty-source-integration",
                wake_seq=21,
                expected_row_version=20,
                source_learning_ids=[str(item["learning_id"]) for item in sources],
                synthesis_kind="summary",
                classification_actor="ai_self",
                classification_basis=["二十张卡属于同一主题且均需纳入审核材料"],
                correctness_assessment="每张来源都合法，但总审核材料超过单次候选预算。",
                diff="尝试建立二十来源综合候选。",
                calm_check={
                    "evidence_sufficient": True,
                    "counterevidence_checked": True,
                    "scope_changed": False,
                    "affected_links_checked": True,
                    "single_turn_pressure_absent": True,
                    "rollback_understood": True,
                    "notes": "已检查全部来源；evidence_refs 按契约保留十六个代表引用。",
                    "evidence_refs": [str(item["item_ref"]) for item in sources[:16]],
                },
                reason="验证总审核材料上限在状态推进前原子拒绝。",
                source_action="keep",
                create_idea=False,
                **self.card("二十来源综合", referent_bindings=[]),
            )

        after = self.store.status(owner_id=self.owner, model_id=self.model)
        self.assertEqual(before, after)
        with closing(sqlite3.connect(self.main_db)) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM learning_change_candidates"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM learning_integrations"
                ).fetchone()[0],
            )

    def test_typo_and_semantic_revision_append_versions_without_candidate(self) -> None:
        stored = self.remember()
        fields = dict(owner_id=self.owner, model_id=self.model, wake_id="same-wake", wake_seq=2)
        small = self.store.revise(**fields, expected_row_version=1,
                                 target_ref=stored["item_ref"], expected_target_version=1,
                                 changes={"summary": "她记得主角刚抵达旧城，并发现门上的符号。"})
        self.assertEqual("applied", small["decision"])
        major = self.store.revise(**fields, expected_row_version=2,
                                 target_ref=small["item_ref"], expected_target_version=2,
                                 changes={"current_understanding": "新证据改变了我对这条线索的理解。"})
        self.assertEqual("applied", major["decision"])
        self.assertEqual(3, major["item_version"])
        self.assertNotIn("candidate_id", major)
        with closing(sqlite3.connect(self.main_db)) as connection:
            self.assertEqual(3, connection.execute("SELECT COUNT(*) FROM learning_versions").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM learning_change_candidates").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM learning_verification_events").fetchone()[0])

    def test_schema_files_are_valid_json(self) -> None:
        schema_root = Path(__file__).parents[1] / "schemas"
        for name in ("learning-memory.schema.json", "learning-idea-box.schema.json"):
            with (schema_root / name).open("r", encoding="utf-8") as handle:
                parsed = json.load(handle)
            self.assertEqual(parsed["$schema"], "https://json-schema.org/draft/2020-12/schema")
        with (schema_root / "learning-memory.schema.json").open(
            "r", encoding="utf-8"
        ) as handle:
            learning_schema = json.load(handle)
        projection = learning_schema["$defs"]["pending_review_projection"]
        self.assertEqual(
            ["full", "metadata_only"],
            projection["properties"]["presentation_mode"]["enum"],
        )
        self.assertEqual(
            ["full", "metadata_only", None],
            learning_schema["$defs"]["change_candidate"]["properties"]
            ["presented_review_mode"]["enum"],
        )


if __name__ == "__main__":
    unittest.main()
