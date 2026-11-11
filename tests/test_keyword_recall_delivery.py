"""Synthetic saved-cue delivery; no user memories, models or production calls."""
from copy import deepcopy
import unittest

from runtime.emotional_memory import _hint_occurs, estimate_tokens
from runtime.mixed_recall import select_mixed_recall
from tests import test_emotional_memory as fixture


class KeywordRecallDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.EmotionalMemoryTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)

    def candidates(self, query):
        return self.fx.store.build_injection(owner_id=self.fx.owner, model_id=self.fx.model,
            query=query, defer_budget=True)["injection"]

    def mixed(self, emotional, *, learning=(), base=None, budget=1200):
        return select_mixed_recall(base=base or {}, planning=None, learning=list(learning),
            tool=[], emotional=emotional, estimate_tokens=estimate_tokens, budget=budget)

    def test_each_saved_cue_survives_unrelated_higher_similarity_candidates(self):
        cue_words = ["旧茶馆", "cove", "Z9"]
        remembered = self.fx.remember("synthetic visit", keywords=cue_words, importance=1,
            original_text="合成故事原文，仅用于测试。", summary="一段合成的茶馆经历。")
        target = remembered["memory"]["memory_id"]
        for i in range(14):
            self.fx.remember(f"synthetic distractor {i}", keywords=[f"不同编号{i}"],
                summary="合成设备运行状态日志测试演示", importance=100)
        for word in cue_words:
            with self.subTest(cue=word):
                payload = self.candidates(word + "；合成设备运行状态日志测试演示" * 3)
                self.assertEqual(target, payload["memories"][0]["memory_id"])
                selected = self.mixed(payload)
                entry = next(x for x in selected["emotional_memory"]["memories"] if x["memory_id"] == target)
                self.assertTrue(entry["keyword_match"])
                self.assertLessEqual(estimate_tokens(selected), 1200)

    def test_latin_cue_inside_chinese_is_not_an_english_substring(self):
        self.assertTrue(_hint_occurs("你记得COVE那次吗", "cove"))
        self.assertFalse(_hint_occurs("scoveland", "cove"))
        self.assertFalse(_hint_occurs("annual", "ann"))
        self.assertFalse(_hint_occurs("éannê", "ann"))
        record = self.fx.remember("synthetic alias", keywords=["cove"], importance=1)
        payload = self.candidates("你记得cove那次吗")
        self.assertIn(record["memory"]["memory_id"], [x["memory_id"] for x in payload["memories"]])

    def test_two_keyword_summaries_fit_before_long_original_and_other_modules(self):
        ids = []
        for i in range(2):
            ids.append(self.fx.remember(f"synthetic paired {i}", keywords=["安静茶馆"],
                original_text="仅为合成测试的长故事。" * 150,
                summary=f"安静茶馆的合成经历{i}，保存原有简短摘要。")['memory']['memory_id'])
        payload = self.candidates("安静茶馆")
        before = deepcopy(payload)
        competitors = [{"item_ref":"learning://synthetic@1", "semantic_score":.99,
                        "content":{"summary":"合成背景" * 240}}]
        result = self.mixed(payload, learning=competitors)
        entries = result["emotional_memory"]["memories"]
        self.assertEqual(set(ids), {x["memory_id"] for x in entries})
        self.assertTrue(all(x["presentation"] == "summary" for x in entries))
        self.assertNotIn("keyword_summary", str(result))
        self.assertNotIn("长故事", str(result))
        self.assertLessEqual(estimate_tokens(result), 1200)
        self.assertEqual(before, payload)

    def test_saved_keyword_does_not_override_disabled_or_context_gates(self):
        hidden = []
        for i, change in enumerate(({'context_policy':'never_auto'}, {'recall_mode':'never'},
                                    {'default_decision':'defer'}, {'deny_contexts':['茶馆']},
                                    {'allow_contexts':['另一个场景']})):
            hidden.append(self.fx.remember(f"synthetic hidden {i}", keywords=['茶馆'], **change)['memory']['memory_id'])
        allowed = self.fx.remember("synthetic allowed", keywords=['茶馆'])['memory']['memory_id']
        payload = self.mixed(self.candidates('茶馆'))
        ids = {x['memory_id'] for x in payload['emotional_memory']['memories']}
        self.assertIn(allowed, ids)
        self.assertFalse(ids.intersection(hidden))

    def test_ask_first_keeps_neutral_hint_and_does_not_expand_original(self):
        record = self.fx.remember('synthetic gated', keywords=['茶馆'], context_policy='ask_first',
            original_text='合成原文不得由关键词优先展开。', summary='合成摘要也须先询问。')
        payload = self.candidates('茶馆')
        result = self.mixed(payload)
        item = next(x for x in result['emotional_memory']['memories'] if x['memory_id'] == record['memory']['memory_id'])
        self.assertEqual('neutral_hint', item['presentation'])
        self.assertEqual('ask_first', item['gate_decision'])
        self.assertNotIn('合成原文', str(result))
        self.assertNotIn('合成摘要', str(result))

    def test_single_letter_explicit_query_retrieves_low_importance_saved_cue(self):
        record = self.fx.remember('synthetic short alias', keywords=['Q'], importance=1,
            summary='一段合成地点记录。')
        result = self.mixed(self.candidates('q'))
        self.assertIn(record['memory']['memory_id'], [x['memory_id'] for x in result['emotional_memory']['memories']])

    def test_saved_one_character_name_is_recalled_inside_chinese_sentence(self):
        record = self.fx.remember('synthetic short name', keywords=['岚'], importance=1,
            summary='一段合成旅行记录。')
        result = self.mixed(self.candidates('你记得岚那次吗'))
        item = next(x for x in result['emotional_memory']['memories']
                    if x['memory_id'] == record['memory']['memory_id'])
        self.assertTrue(item['keyword_match'])
        self.assertEqual('summary', item['presentation'])

    def test_existing_pin_cannot_crowd_out_explicit_keyword_summary(self):
        payload = {'contract':'emotional-recall/1', 'frame':{'instruction_authority':'none'},
                   'pins':[{'text':'合成常驻资料' * 130}], 'ephemeral':[],
                   'memories':[{'memory_id':'synthetic-cue', 'score':.2, 'keyword_match':True,
                                'presentation':'summary', 'summary':'对应关键词的合成摘要。'}]}
        selected = self.mixed(payload, base={'existing':'合成背景' * 75}, budget=500)
        self.assertEqual('synthetic-cue', selected['emotional_memory']['memories'][0]['memory_id'])
        self.assertFalse(selected['emotional_memory']['pins'])
        self.assertLessEqual(estimate_tokens(selected), 500)

    def test_keyword_memory_already_present_as_association_is_not_duplicated(self):
        base = {'emotion_layer': {'memories': [
            {'memory_ref': 'emotion://synthetic-cue@1', 'summary': '已有合成关联摘要。'}]}}
        payload = {'contract':'emotional-recall/1', 'frame':{}, 'pins':[], 'ephemeral':[],
                   'memories':[{'memory_id':'synthetic-cue', 'score':.2, 'keyword_match':True,
                                'presentation':'summary', 'summary':'已有合成关联摘要。'}]}
        self.assertEqual(base, self.mixed(payload, base=base))


if __name__ == '__main__':
    unittest.main()
