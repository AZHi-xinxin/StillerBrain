"""Synthetic host image transport exclusion; no database, network or execution."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from rikkahub_gateway.server import GatewayApplication, GatewayError, _advertised_tool_catalog
from rikkahub_gateway.tool_execution import (
    NativeToolCall, advertised_schemas, canonical_hash, is_host_only_image_transport,
    without_host_only_image_transport,
)
from rikkahub_gateway.tests.test_gateway import config
from rikkahub_gateway.tests.test_gateway_execution_recovery import ExecutionControl


def stage(name='stage_memory_image_upload'):
    return {'type': 'function', 'function': {'name': name, '_meta': {
        'stbrain/hostOnly': True, 'stbrain/contract': 'memory-image-upload/1'}, 'parameters': {
        'type': 'object', 'x-stbrain-host-only': True, 'x-stbrain-contract': 'memory-image-upload/1',
        'properties': {'protocol': {'type': 'string', 'enum': ['memory-image-upload/1']},
                       'data_base64': {'type': 'string'}}, 'required': ['protocol', 'data_base64']}}}


def ordinary(name):
    return {'type': 'function', 'function': {'name': name,
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}}


def store():
    """Orbis' exact advertised upload_ref-only schema, with host marker retained."""
    value = ordinary('mcp__StillerBrain__store_memory_image')
    value['function']['parameters'] = {
        'type': 'object', 'properties': {
            'target_ref': {'type': 'string'}, 'description': {'type': 'string'},
            'mime_type': {'type': 'string', 'enum': ['image/png', 'image/jpeg', 'image/webp']},
            'upload_ref': {'type': 'string', 'pattern': '^stimgup_[0-9a-f]{64}$'},
            'execution_ref': {'type': 'string', 'pattern': '^stexec_[A-Za-z0-9_-]{43}$',
                'x-stbrain-execution-tool': 'store_memory_image',
                'x-stbrain-execution-contract': 'st-execution/1'},
        }, 'required': ['target_ref', 'description', 'mime_type', 'upload_ref'],
        'additionalProperties': False,
    }
    return value


class ImageTransportProjectionTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket', 'socket.create_connection', 'sqlite3.connect'):
            self.enterContext(patch(target, side_effect=AssertionError('external access prohibited')))
        self.control = ExecutionControl()
        self.app = GatewayApplication(replace(config(), require_execution_binding=True,
            execution_epoch='synthetic-image-projection'), control=self.control, upstream=object())

    def payload(self, tools):
        return {'tools': tools, 'messages': [{'role': 'user', 'content': 'synthetic selected image'}]}

    def test_schemas_and_binding_catalog_remove_only_marked_stage(self):
        keep = [store(), ordinary('list_memory_images'), ordinary('read_memory_image'),
                ordinary('remove_memory_image'), ordinary('unrelated_tool')]
        payload = self.payload([stage(), *keep])
        self.assertEqual({tool['function']['name'] for tool in keep}, set(advertised_schemas(payload)))
        catalog = _advertised_tool_catalog(payload)
        self.assertTrue(catalog['catalog_complete'])
        self.assertEqual({tool['function']['name'] for tool in keep},
                         {entry['canonical_name'] for entry in catalog['entries']})

    def test_raw_and_mcp_names_with_schema_markers(self):
        for name in ('stage_memory_image_upload', 'mcp__StillerBrain__stage_memory_image_upload'):
            tool = stage(name)
            del tool['function']['_meta']
            self.assertTrue(is_host_only_image_transport(tool['function']))
            self.assertEqual({}, advertised_schemas(self.payload([tool])))

    def test_exact_metadata_survives_schema_extension_loss(self):
        tool = stage()
        for field in ('x-stbrain-host-only', 'x-stbrain-contract'):
            del tool['function']['parameters'][field]
        self.assertTrue(is_host_only_image_transport(tool['function']))

    def test_name_alone_or_other_names_never_filtered(self):
        for name in ('stage_memory_image_upload', 'mcp__StillerBrain__stage_memory_image_upload'):
            tool = ordinary(name)
            payload = self.payload([tool])
            self.assertIs(payload, without_host_only_image_transport(payload))
            self.assertIn(name, advertised_schemas(payload))
        for name in ('store_memory_image', 'another_stage_memory_image_upload',
                     'custom__stage_memory_image_upload', 'stage_memory_image_upload_extra'):
            self.assertFalse(is_host_only_image_transport(stage(name)['function']))

    def test_inexact_contract_or_truthy_markers_never_filtered(self):
        for flag, contract in ((True, 'memory-image-upload/2'), (1, 'memory-image-upload/1'),
                               ('true', 'memory-image-upload/1'), (False, 'memory-image-upload/1')):
            tool = stage()
            del tool['function']['_meta']
            tool['function']['parameters']['x-stbrain-host-only'] = flag
            tool['function']['parameters']['x-stbrain-contract'] = contract
            self.assertFalse(is_host_only_image_transport(tool['function']))

    def test_upstream_filter_also_applies_when_execution_binding_disabled(self):
        for enabled in (False, True):
            self.app.config = replace(self.app.config, require_execution_binding=enabled)
            payload = self.payload([stage(), ordinary('unrelated_tool')])
            original = copy.deepcopy(payload)
            result = json.loads(self.app.encode_upstream_payload(payload))
            self.assertEqual(['unrelated_tool'], [t['function']['name'] for t in result['tools']])
            self.assertEqual(original, payload)

    def test_forced_stage_choice_removed_other_choice_preserved(self):
        for name in ('stage_memory_image_upload', 'unrelated_tool'):
            payload = self.payload([stage(), ordinary('unrelated_tool')])
            choice = {'type': 'function', 'function': {'name': name}}
            payload['tool_choice'] = choice
            result = without_host_only_image_transport(payload)
            self.assertEqual(choice if name == 'unrelated_tool' else None, result.get('tool_choice'))

    def test_legacy_functions_and_forced_function_call(self):
        payload = {'functions': [stage()['function'], ordinary('unrelated_tool')['function']],
                   'function_call': {'name': 'stage_memory_image_upload'}, 'messages': []}
        result = without_host_only_image_transport(payload)
        self.assertNotIn('function_call', result)
        self.assertEqual(['unrelated_tool'], [f['name'] for f in result['functions']])
        self.assertEqual({'unrelated_tool'}, set(advertised_schemas(payload)))

    def test_prepare_excludes_stage_before_control_and_rejects_model_call(self):
        name = 'mcp__StillerBrain__stage_memory_image_upload'
        payload = self.payload([stage(name), store()])
        prepared = self.app.prepare_turn(payload, {'X-ST-Thread-ID': 'synthetic-image-thread'})
        self.assertNotIn(name, json.dumps(prepared.session.advertised_tools))
        self.assertNotIn(name, json.dumps(prepared.payload['tools']))
        call = NativeToolCall('synthetic-host-stage-call', name, '{}')
        with self.assertRaisesRegex(GatewayError, 'tool_not_advertised'):
            self.app.decorate_execution_calls(prepared, [call])
        with self.assertRaisesRegex(GatewayError, 'tool_not_advertised'):
            self.app.bind_response_tool_calls(prepared, [call])
        self.assertFalse(any(path.endswith('/issue') for path, _ in self.control.calls))

    def test_upload_ref_schema_binds_exactly_and_hides_execution_ref_only_upstream(self):
        tool = store()
        schema = tool['function']['parameters']
        name = tool['function']['name']
        prepared = self.app.prepare_turn(self.payload([stage(), tool]),
                                         {'X-ST-Thread-ID': 'synthetic-image-upload'})
        args = {'target_ref': 'emotion:synthetic', 'description': 'synthetic PNG',
                'mime_type': 'image/png', 'upload_ref': 'stimgup_' + 'a' * 64}
        call = NativeToolCall('synthetic-store-call', name, json.dumps(args))
        decorated = self.app.decorate_execution_calls(prepared, [call])
        bindings = self.app.bind_response_tool_calls(prepared, decorated)
        self.assertEqual(canonical_hash(schema), bindings[call.tool_call_id].schema_hash)
        visible = json.loads(self.app.encode_upstream_payload(prepared.payload))['tools'][0]['function']['parameters']
        self.assertNotIn('execution_ref', visible['properties'])
        self.assertNotIn('data_base64', visible['properties'])
        self.assertIn('upload_ref', visible['required'])
        bound_args = json.loads(decorated[0].arguments_text)
        bound_args.pop('execution_ref')
        self.assertEqual(args, bound_args)
        self.assertIn('execution_ref', schema['properties'])

    def test_upload_ref_projection_does_not_accept_fabricated_base64_argument(self):
        prepared = self.app.prepare_turn(self.payload([store()]),
                                         {'X-ST-Thread-ID': 'synthetic-image-base64'})
        args = {'target_ref': 'emotion:synthetic', 'description': 'synthetic PNG',
                'mime_type': 'image/png', 'data_base64': 'synthetic-not-real-bytes'}
        call = NativeToolCall('synthetic-invalid-store', store()['function']['name'], json.dumps(args))
        with self.assertRaisesRegex(GatewayError, 'tool_arguments_schema_invalid'):
            self.app.bind_response_tool_calls(prepared, [call])
        self.assertFalse(any(path.endswith('/issue') for path, _ in self.control.calls))

    def test_disabled_execution_binding_still_cannot_bind_host_stage(self):
        self.app.config = replace(self.app.config, require_execution_binding=False)
        prepared = self.app.prepare_turn(self.payload([stage()]),
                                         {'X-ST-Thread-ID': 'synthetic-no-binding-stage'})
        call = NativeToolCall('synthetic-stage-no-binding', 'stage_memory_image_upload', '{}')
        with self.assertRaisesRegex(GatewayError, 'tool_not_advertised'):
            self.app.bind_response_tool_calls(prepared, [call])
        self.assertFalse(any(path.endswith('/issue') for path, _ in self.control.calls))


if __name__ == '__main__':
    unittest.main()
