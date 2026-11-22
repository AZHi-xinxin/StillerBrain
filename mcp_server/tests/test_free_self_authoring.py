"""Authored content survives the real submit/review/activate/inject lifecycle."""
import json
import unittest
from copy import deepcopy
from mcp_server.tests import test_public_tools_v5 as fixtures
from runtime.onboarding import ModuleOneOnboardingStore
from runtime.facet_recall import select_facets
from runtime.recall_projection import estimate_dynamic
from mcp_server.public_contract import PUBLIC_SUBMIT_INPUT_SCHEMA
import jsonschema


class FreeSelfAuthoringTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PublicFacadeV9Tests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.wake('author-1')
        self.f.advance_to_body()

    def submit(self, content):
        args = {'intent': 'submit', 'payload': {'content': content}}
        jsonschema.validate(args, PUBLIC_SUBMIT_INPUT_SCHEMA)
        result = self.f.service.submit_self_model_candidate(**args)
        self.assertEqual('pending', result['decision'], result.get('reason_codes'))
        self.assertEqual(content, json.loads(self.f.candidate(result['candidate_id'])['content_json']))
        return result

    def finish(self, content):
        first = self.submit(content)
        same = self.f.service.submit_self_model_candidate(intent='accept_review', payload={'ai_confirmation': True})
        self.assertNotEqual('review_accepted', same['decision'])
        self.f.wake('author-2')
        accepted = self.f.service.submit_self_model_candidate(intent='accept_review', payload={'ai_confirmation': True})
        self.assertEqual('review_accepted', accepted['decision'], accepted.get('reason_codes'))
        activate = dict(candidate_id=first['candidate_id'], expected_active_revision=None, ai_confirmation=True)
        same = self.f.service.activate_self_model_candidate(**activate)
        self.assertIn('review_activation_wake_boundary_required', same['reason_codes'])
        self.f.wake('author-3')
        result = self.f.service.activate_self_model_candidate(**activate)
        self.assertEqual('activate', result['decision'], result.get('reason_codes'))
        self.assertEqual(content, json.loads(self.f.service.store.active_revision(self.f.service.model_id)['content_json']))
        self.f.wake('author-4')
        result = self.f.service.query_self_model(view='edit_basis')
        self.assertIn(json.dumps(content, ensure_ascii=False, sort_keys=True), json.dumps(result, ensure_ascii=False, sort_keys=True))
        return result

    def test_unstructured_text_is_saved_reviewed_and_injected(self):
        self.finish('我自己写的整段文本。password: DEMO_SYNTHETIC；站点密码轮换：DEMO_OLD → DEMO_NEW；token：DEMO_SYNTHETIC_VALUE。')

    def test_array_content_is_saved_reviewed_and_injected(self):
        self.finish(['自写内容', {'任意字段': ['中文', 3, None]}])

    def test_custom_fields_chinese_names_large_facets_and_partial_settings(self):
        content = {
            '自定章节': {'正文': '没有固定字段的自我描述'},
            'boot_anchor': '直接写提醒',
            'active_identity_capsule': {'喜欢的表达': ['长句', '短句']},
            'facets': {f'中文 名称🌙{i}': {'text': '正文' * 1400, 'summary': '自写摘要' * 45,
                         'keywords': '触发', '任意附记': {'喜欢': True}} for i in range(20)},
            'facet_recall': {'semantic_enabled': False},
        }
        content['facets']['关联侧面'] = {'text': '[[facet:以后再建]]', 'related_facets': '以后再建'}
        before = deepcopy(content)
        self.finish(content)
        result = select_facets(content['facets'], '触发', policy=content['facet_recall'], estimate=estimate_dynamic)
        self.assertEqual(set(content['facets']), set(result['facets']))
        self.assertIn('任意附记', result['facets']['中文 名称🌙0'])
        self.assertEqual(before, content)

    def test_missing_binding_does_not_create_a_wake(self):
        with self.f.onboarding._connect() as connection:
            before = connection.execute('SELECT count(*) FROM brain_wake_sessions').fetchone()[0]
        self.submit({'我的定义': '自己写'})
        with self.f.onboarding._connect() as connection:
            after = connection.execute('SELECT count(*) FROM brain_wake_sessions').fetchone()[0]
        self.assertEqual(before, after)
