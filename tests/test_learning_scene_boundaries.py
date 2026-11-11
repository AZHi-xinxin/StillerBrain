"""Synthetic automatic recall: scene cues must not match inside English words."""
import unittest
from runtime.learning_memory import _scene_tag_matches
from tests import test_learning_memory as fixtures

class LearningSceneBoundaryTests(unittest.TestCase):
    setUp = fixtures.LearningMemoryRuntimeTests.setUp
    tearDown = fixtures.LearningMemoryRuntimeTests.tearDown
    card = fixtures.LearningMemoryRuntimeTests.card
    remember = fixtures.LearningMemoryRuntimeTests.remember
    evidence_item = staticmethod(fixtures.LearningMemoryRuntimeTests.evidence_item)

    def test_short_label_does_not_pin_unrelated_card_at_point_82(self):
        self.remember(scene_tags=["ST"], importance=85)
        for query in ["Just had lunch", "first question", "STBrain", "s t", "今天吃饭了"]:
            with self.subTest(query=query):
                self.assertEqual([], self.store.build_envelopes(owner_id=self.owner, model_id=self.model, query=query))
        for query in ["检查ST配置", "repair st", "(ST)"]:
            envelope, = self.store.build_envelopes(owner_id=self.owner, model_id=self.model, query=query)
            self.assertIn("scene_match", envelope["reason_codes"])
            self.assertEqual(["ST"], envelope["module_extension"]["learning"]["scene_match"])
            self.assertGreaterEqual(envelope["semantic_score"], .82)

    def test_integration_ancestor_cannot_reintroduce_substring_boost(self):
        card = self.card(scene_tags=["ST"])
        lineage = {"integrated": [{"learning_id": "source", "item_ref": "learning://source@1",
                                   "snapshot": card, "current": card}]}
        for manual in [False, True]:
            score, refs = self.store._integration_query_matches(
                learning_id="integrated", lineage=lineage, query="Just had lunch", manual_search=manual)
            self.assertLess(score, .82)
            self.assertEqual([], refs)
            score, refs = self.store._integration_query_matches(
                learning_id="integrated", lineage=lineage, query="检查ST配置", manual_search=manual)
            self.assertGreaterEqual(score, .82)
            self.assertEqual(["learning://source@1"], refs)

    def test_identifiers_punctuation_whitespace_and_chinese_cues(self):
        for tag, query, matches in [
            ("AI", "chair", False), ("ST", "first", False), ("ST", "ST_2", False),
            ("ST", "1st", False), ("ST", "修ST吧", True), ("ST", "st!", True),
            ("ST", "s t", False), ("go home", "go\nhome", True),
            ("go home", "gohome", False), ("C++", "C++参考", True),
            ("继续阅读", "今天继续阅读吧", True), ("", "anything", False),
        ]:
            with self.subTest(tag=tag, query=query):
                self.assertEqual(matches, _scene_tag_matches(tag, query))
