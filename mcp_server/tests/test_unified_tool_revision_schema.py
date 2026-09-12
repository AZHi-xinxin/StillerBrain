"""Offline schemas for optional tool-card changes through revise_memory."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from mcp_server.daily_revision_service import ORDINARY_REVISION_FIELDS, parse_revision_target
from mcp_server.ordinary_revision_schema import install_ordinary_revision_schema, ordinary_revision_fields
from runtime.tool_guidance import ToolGuidanceError, _SCENE_TAG, _strings


class UnifiedToolRevisionSchemaTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.create_connection', 'socket.socket', 'subprocess.Popen'):
            self.enterContext(patch(target, side_effect=AssertionError('offline schema test')))
        self.parameters = {
            'type': 'object', 'required': ['target_ref', 'changes'], 'additionalProperties': False,
            'properties': {'target_ref': {'type': 'string', 'minLength': 1, 'maxLength': 100},
                           'changes': {'type': 'object'},
                           'reason': {'type': ['string', 'null']},
                           'write_context_ref': {'type': ['string', 'null']}},
        }
        install_ordinary_revision_schema(self.parameters)
        self.validator = Draft202012Validator(self.parameters)
        self.target = 'tool-card://toolcard_' + 'a' * 32 + '@3'

    def accepts(self, changes, target=None, **extra):
        return self.validator.is_valid({'target_ref': target or self.target, 'changes': changes, **extra})

    def test_schema_is_standalone_and_all_four_field_sets_match_runtime(self):
        Draft202012Validator.check_schema(self.parameters)
        self.assertNotIn('$ref', json.dumps(self.parameters))
        fields = ordinary_revision_fields()
        self.assertEqual({'emotional_memory', 'learning_memory', 'planning_memory', 'tool_guidance'}, set(fields))
        for module, properties in fields.items():
            self.assertEqual(ORDINARY_REVISION_FIELDS[module], set(properties), module)
        self.assertEqual(set().union(*ORDINARY_REVISION_FIELDS.values()),
                         set(self.parameters['properties']['changes']['properties']))

    def test_only_observed_target_and_changed_fields_are_required(self):
        self.assertEqual(['target_ref', 'changes'], self.parameters['required'])
        for changes in ({'purpose': '用于整理共享文件。'}, {'reminder': '回家后我可以查看设备。'},
                        {'scenario_tags': ['回家了', '准备睡觉']}, {'confidence': 100},
                        {'tool_name': '家庭 MCP'}, {'operation_key': 'general'}):
            with self.subTest(changes=changes):
                self.assertTrue(self.accepts(changes))
        for branch in self.parameters['allOf']:
            self.assertNotIn('required', branch['then']['properties']['changes'])
        self.assertFalse(self.accepts({}))
        self.assertFalse(self.accepts({'purpose': 'x'}, expected_card_version=3))
        self.assertFalse(self.accepts({'purpose': 'x'}, expected_tool_row_version=1))

    def test_public_aliases_do_not_expose_stored_derived_or_identity_fields(self):
        fields = ordinary_revision_fields()['tool_guidance']
        self.assertIn('tool_name', fields)
        self.assertIn('confidence', fields)
        for field in ('canonical_tool_name', 'claimed_confidence', 'effective_confidence',
                      'observed_schema_hash', 'valid_from', 'lifecycle', 'owner_id', 'model_id',
                      'card_id', 'version', 'content_hash', 'execution_ref'):
            with self.subTest(field=field):
                self.assertNotIn(field, fields)
                self.assertFalse(self.accepts({field: 'synthetic'}))

    def test_clear_scalar_null_and_legacy_clear_fields_remain_available(self):
        for field in ('reminder', 'source_ref', 'expires_at'):
            self.assertTrue(self.accepts({field: None}))
            self.assertTrue(self.accepts({'clear_fields': [field]}))
            self.assertTrue(self.accepts({field: None, 'clear_fields': [field]}))
        self.assertTrue(self.accepts({'clear_fields': None, 'purpose': '保留到期设置。'}))
        self.assertTrue(self.accepts({'clear_fields': ['expires_at', 'expires_at']}))
        self.assertFalse(self.accepts({'clear_fields': ['owner_id']}))
        self.assertFalse(self.accepts({'clear_fields': 'expires_at'}))
        for field in ('scenario_tags', 'confidence', 'purpose', 'edit_class', 'target_version'):
            self.assertTrue(self.accepts({field: None, 'call_notes': '兼容空参数，保留旧值。'}))

    def test_legacy_optional_operations_do_not_require_review_forms(self):
        for changes in (
            {'intent': 'retire'}, {'intent': 'restore', 'target_version': 1},
            {'edit_class': 'typo', 'field_name': 'purpose', 'before_text': '原词', 'after_text': '新词'},
            {'edit_class': 'source_addition', 'source_ref': 'document://synthetic-source'},
            {'edit_class': 'salience_downweight', 'salience': 20},
            {'purpose': '新版用途', 'correctness_assessment': None},
        ):
            with self.subTest(changes=changes):
                self.assertTrue(self.accepts(changes))
        for changes in ({'intent': 'execute'}, {'edit_class': 'automatic_review'},
                        {'target_version': 0}, {'target_version': True}):
            self.assertFalse(self.accepts(changes))

    def test_tool_limits_and_other_modules_keep_their_own_branches(self):
        self.assertTrue(self.accepts({'reminder': '字' * 100, 'confidence': 0}))
        for changes in ({'reminder': '字' * 101}, {'confidence': 101}, {'confidence': -1},
                        {'confidence': True}, {'keywords': ['k' + str(i) for i in range(17)]},
                        {'scenario_tags': ['回家\n了']}, {'scenario_tags': [' ']}):
            with self.subTest(changes=changes):
                self.assertFalse(self.accepts(changes))
        learning = 'learning://learn_' + 'b' * 32 + '@1'
        emotion = 'emotion://emmem_' + 'c' * 32 + '@1'
        planning = 'plan://plan_' + 'd' * 32 + '@1'
        self.assertTrue(self.accepts({'keywords': ['k' + str(i) for i in range(24)]}, learning))
        self.assertTrue(self.accepts({'original_text': '修改原文。'}, emotion))
        self.assertTrue(self.accepts({'parent_ref': None}, planning))
        self.assertFalse(self.accepts({'purpose': '工具字段'}, learning))
        self.assertFalse(self.accepts({'intent': 'retire'}, emotion))
        self.assertFalse(self.accepts({'summary': '别的模块字段'}))

    def test_existing_runtime_trims_outer_whitespace_but_rejects_internal_controls(self):
        # Existing tool-card normalization strips each item before fullmatch.
        # A trailing newline therefore normalizes to the same natural phrase;
        # this records inherited behavior, not a production/schema relaxation.
        self.assertEqual(['回家了'], _strings('scenario_tags', [' \n回家了\n '],
                         16, item_chars=128, pattern=_SCENE_TAG))
        self.assertTrue(self.accepts({'scenario_tags': ['回家了\n']}))
        with self.assertRaisesRegex(ToolGuidanceError, 'invalid_scenario_tags_item'):
            _strings('scenario_tags', ['回家\n了'], 16, item_chars=128, pattern=_SCENE_TAG)

    def test_target_prefixes_and_versions_match_runtime_parser(self):
        self.assertEqual(('tool_guidance', 'toolcard_' + 'a' * 32, 3), parse_revision_target(self.target))
        for target in ('tool-card://toolcard_' + 'a' * 32,
                       'tool-card://toolcard_' + 'a' * 32 + '@latest',
                       'tool-card://learn_' + 'a' * 32 + '@1',
                       'learning://toolcard_' + 'a' * 32 + '@1',
                       'tool-card://toolcard_' + 'a' * 32 + '@0'):
            with self.subTest(target=target):
                self.assertFalse(self.accepts({'purpose': 'x'}, target))
                with self.assertRaises(ValueError):
                    parse_revision_target(target)

    def test_schema_copies_are_independent(self):
        first = ordinary_revision_fields()
        first['tool_guidance']['confidence'].clear()
        self.assertTrue(Draft202012Validator(ordinary_revision_fields()['tool_guidance']['confidence']).is_valid(100))
        self.assertFalse(Draft202012Validator(ordinary_revision_fields()['tool_guidance']['confidence']).is_valid(101))


if __name__ == '__main__':
    unittest.main()
