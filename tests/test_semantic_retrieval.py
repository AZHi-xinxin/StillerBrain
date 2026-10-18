"""Synthetic semantic-channel tests; no real memories, API keys or network."""
from pathlib import Path
from contextlib import closing
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from runtime import semantic_retrieval as sr
from tests import test_explicit_expression_recall as fixtures
from tests import test_mixed_recall_r4h34 as mixed_fixture
from tests import test_tool_guidance as tool_fixture


class Encoder:
    def __init__(self):
        self.encoded = 0

    def passages(self, texts):
        self.encoded += len(texts)
        return ([1, 0] if "断电" in text else [0, 1] for text in texts)

    def query(self, text):
        return [1, 0] if "全黑" in text else [-1, -1]


class IndexTests(unittest.TestCase):
    def test_paraphrase_incremental_update_delete_and_no_plaintext(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "index.sqlite"
            encoder = Encoder()
            index = sr.VectorIndex(path, encoder)
            docs = [sr.Document("private-ref-a", 1, "那晚断电了"), sr.Document("b", 1, "午饭")]
            self.assertEqual({"private-ref-a": 1.0}, index.search(docs, "灯全黑了").scores)
            self.assertEqual({}, index.search(docs, "毫不相关").scores)
            self.assertEqual(2, encoder.encoded)
            docs[0] = sr.Document("private-ref-a", 2, "不是这个主题")
            self.assertEqual({}, index.search(docs, "灯全黑了").scores)
            self.assertEqual(3, encoder.encoded)
            index.search([], "灯全黑了")
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])
            raw = path.read_bytes()
            for text in ("private-ref-a", "那晚断电了", "灯全黑了"):
                self.assertNotIn(text.encode(), raw)

    def test_invalid_vector_never_claims_match(self):
        class Broken(Encoder):
            def query(self, text):
                return [float('nan'), 1]
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                sr.VectorIndex(Path(folder) / "v.db", Broken()).search([sr.Document('a', 1, '断电')], '全黑')

    def test_cached_vector_codec_rejects_corruption(self):
        import struct
        self.assertAlmostEqual(1.0, sum(v*v for v in sr._decode_vector(sr._encode_vector([1, 2, 3]))), places=6)
        for blob in (b'', b'bad', 'legacy json', struct.pack('<f', float('nan')), struct.pack('<f', 0.0)):
            with self.assertRaises(ValueError):
                sr._decode_vector(blob)

    def test_cold_start_nonblocking_and_bounded_queue(self):
        release = threading.Event()
        def factory(directory):
            release.wait(3)
            return Encoder()
        worker = sr.SemanticWorker(factory)
        with tempfile.TemporaryDirectory() as folder:
            start = time.monotonic()
            result = worker.request('scope', folder, folder, [sr.Document('a', 1, '断电')], '全黑')
            self.assertLess(time.monotonic() - start, 0.2)
            self.assertEqual('indexing_lexical_fallback', result.status)
            statuses = [worker.request('scope', folder, folder, [], str(i)).status for i in range(20)]
            self.assertIn('busy_lexical_fallback', statuses)
            release.set()
            worker.requests.join()
            result = worker.request('scope', folder, folder, [sr.Document('a', 1, '断电')], '全黑')
            self.assertEqual({'a': 1.0}, result.scores)

    def test_explicit_lookup_can_wait_for_first_result(self):
        worker = sr.SemanticWorker(lambda directory: Encoder())
        with tempfile.TemporaryDirectory() as folder:
            result = worker.request('scope', folder, folder, [sr.Document('a', 1, '断电')],
                                    '全黑', wait=1, wait_for_cold=True)
            worker.requests.join()
            self.assertEqual('ready', result.status)
            self.assertEqual({'a': 1.0}, result.scores)

    def test_explicit_wait_is_bounded_even_when_encoder_is_blocked(self):
        release = threading.Event()
        def factory(directory):
            release.wait(3)
            return Encoder()
        worker = sr.SemanticWorker(factory)
        with tempfile.TemporaryDirectory() as folder:
            try:
                with patch.object(sr, 'EXPLICIT_WAIT_SECONDS', .02):
                    start = time.monotonic()
                    result = worker.request('scope', folder, folder,
                        [sr.Document('a', 1, '断电')], '全黑', wait=99, wait_for_cold=True)
                self.assertLess(time.monotonic() - start, .5)
                self.assertEqual('indexing_lexical_fallback', result.status)
            finally:
                release.set()
                worker.requests.join()

    def test_disabled_and_namespaces_separate_owner_database_module_and_model_files(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual('disabled', sr.search(database='x', owner_id='o', model_id='m',
                module='emotion', documents=[], query='q').status)
        with tempfile.TemporaryDirectory() as folder, patch.object(sr._worker, 'request', return_value=sr.Result('ready', {})) as request:
            with patch.dict(os.environ, {'STBRAIN_SEMANTIC_MODEL_DIR': folder, 'STBRAIN_SEMANTIC_INDEX_DIR': folder}):
                args = dict(database=folder+'/db', owner_id='o', model_id='m', module='emotion', documents=[sr.Document('a', 1, 'x')], query='q')
                for override in ({}, {'owner_id':'other'}, {'model_id':'other'}, {'module':'learning'}, {'database':folder+'/db2'}):
                    sr.search(**{**args, **override})
                self.assertEqual(5, len({call.args[0] for call in request.call_args_list}))
                Path(folder, 'model.onnx').write_bytes(b'synthetic-model-signature')
                sr.search(**args)
                self.assertNotEqual(request.call_args_list[0].args[0], request.call_args.args[0])


class IntegrationTests(unittest.TestCase):
    setUp = fixtures.ThreeBrainExplicitAliasTests.setUp
    tearDown = fixtures.ThreeBrainExplicitAliasTests.tearDown
    save = fixtures.ThreeBrainExplicitAliasTests.save
    snapshot_authored = fixtures.ThreeBrainExplicitAliasTests.snapshot_authored

    def test_emotion_dense_only_is_summary_and_authored_rows_unchanged(self):
        ref = self.save('emotion', cue='纸月城晚间停电记录')
        before = self.snapshot_authored()
        with patch('runtime.emotional_memory.dense.search', return_value=sr.Result('ready', {ref:.92})):
            explicit = self.stores['emotion'].recall(**self.common, query='那晚突然全黑了，灯也亮不起来')
            automatic = self.stores['emotion'].build_injection(**self.common, query='那晚突然全黑了，灯也亮不起来')
        item = explicit['results'][0]
        self.assertTrue(item['candidate_only'])
        self.assertTrue(item['original_withheld'])
        auto = automatic['injection']['memories'][0]
        self.assertEqual('summary', auto['presentation'])
        self.assertEqual(before, self.snapshot_authored())

    def test_eligibility_is_applied_before_encoder(self):
        self.save('emotion', cue='正常摘要')
        self.save('emotion', cue='禁用摘要', recall_mode='never')
        self.save('emotion', cue='敏感摘要', sensitivity='restricted')
        with patch('runtime.emotional_memory.dense.search', return_value=sr.Result('ready', {})) as search:
            self.stores['emotion'].build_injection(**self.common, query='换一种说法')
        texts = [doc.text for doc in search.call_args.kwargs['documents']]
        self.assertEqual(['正常摘要\n'], texts)

    def test_learning_semantic_independent_of_lexical_threshold(self):
        ref = self.save('learning', cue='断电时可以使用储能灯')
        before = self.snapshot_authored()
        with patch('runtime.learning_memory.dense.search', return_value=sr.Result('ready', {ref:.88})):
            found = self.stores['learning'].build_envelopes(**self.common, query='屋里突然全黑了该怎么办')
        self.assertEqual(1, len(found))
        self.assertIn('local_semantic_candidate', found[0]['reason_codes'])
        self.assertEqual('summary', found[0]['presentation'])
        self.assertEqual(before, self.snapshot_authored())

    def test_unavailable_preserves_lexical_results(self):
        self.save('emotion', cue='合成停电记录')
        store = self.stores['emotion']
        baseline = store.build_injection(**self.common, query='合成停电记录')['injection']
        with patch('runtime.emotional_memory.dense.search', return_value=sr.Result('unavailable_lexical_fallback', {})):
            self.assertEqual(baseline, store.build_injection(**self.common, query='合成停电记录')['injection'])

    def test_empty_explicit_query_retains_backend_reason_in_result_and_audit(self):
        self.save('emotion', cue='合成停电记录')
        store = self.stores['emotion']
        with patch('runtime.emotional_memory.dense.search',
                   return_value=sr.Result('unavailable_lexical_fallback', {})):
            result = store.recall(**self.common, query='玄黓魑魅')
        self.assertEqual([], result['results'])
        self.assertIn('no_candidate', result['reason_codes'])
        code = 'semantic_backend_unavailable_lexical_fallback'
        self.assertIn(code, result['reason_codes'])
        with store._connect() as connection:
            row = connection.execute("SELECT reason_codes_json FROM emotion_audit_events "
                                     "WHERE action='recall_query' ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertIn(code, row[0])

    def test_budget_diagnostic_is_not_in_prompt(self):
        diagnostic = {}
        result = mixed_fixture.MixedRecallTests().select(learns=[mixed_fixture.learning('l', size=800, score=.5)],
            emotions=[mixed_fixture.emotion('e', size=800, score=.9)], diagnostics=diagnostic)
        self.assertEqual(1, diagnostic['candidate_counts']['learning'])
        self.assertEqual(0, diagnostic['selected_counts']['learning'])
        self.assertGreater(diagnostic['budget_rejected_attempts'], 0)
        self.assertNotIn('candidate_counts', result)

    def test_plan_semantic_candidate_and_exclusion(self):
        ref = self.save('planning', cue='断电后检查备用电源')
        store = self.stores['planning']
        def result(**kwargs):
            return sr.Result('ready', {doc.ref:.9 for doc in kwargs['documents']})
        with patch('runtime.planning_memory.dense.search', side_effect=result) as search:
            found = store.build_injection(**self.common, query='屋里突然全黑了')
            self.assertEqual(ref, found['injection']['envelopes'][0]['item_ref'].split('://')[1].split('@')[0])
            store.build_injection(**self.common, query='屋里突然全黑了', excluded_plan_ids=[ref])
            self.assertEqual([], search.call_args.kwargs['documents'])

    def test_planning_quota_is_counted_in_diagnostics(self):
        diagnostic = {}
        found = mixed_fixture.MixedRecallTests().select(
            plans=[mixed_fixture.plan(str(i), size=1) for i in range(4)],
            budget=10000, diagnostics=diagnostic)
        self.assertEqual(3, len(found['planning_memory']['envelopes']))
        self.assertEqual(1, diagnostic['module_quota_skips'])

    def test_tool_semantic_candidate_remains_advisory(self):
        fixture = tool_fixture.ToolGuidanceStoreTests()
        fixture.setUp()
        try:
            fixture.remember()
            def result(**kwargs):
                return sr.Result('ready', {doc.ref:.9 for doc in kwargs['documents']})
            with patch('runtime.tool_guidance.dense.search', side_effect=result):
                found = fixture.store.build_recall_envelopes(owner_id=fixture.owner, model_id=fixture.model,
                    query='换一种说法请求相关操作', catalog=fixture.live_catalog)
            self.assertEqual(1, len(found['envelopes']))
            self.assertFalse(found['execution_performed'])
            self.assertEqual('local_semantic_candidate', found['envelopes'][0]['retrieval_match'])
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
