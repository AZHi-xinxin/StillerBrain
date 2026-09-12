"""Real advertised schema -> host boundary -> execution lease -> registered MCP.

All stores, wake capabilities and arguments are synthetic and isolated in child
processes. No HTTP server, external model or real brain is used. No product
validator, credential detector, execution ledger or authoring policy is mocked.
"""
from __future__ import annotations

import asyncio
import ast
from contextlib import closing, ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


MODES = ('emotional_memory', 'learning_memory', 'planning_memory',
         'schema', 'rejections', 'permissions', 'neutral_defaults')


class OrdinaryRevisionGatewayContractTests(unittest.TestCase):
    def run_probe(self, mode):
        allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE', 'PATH',
                   'PATHEXT', 'TEMP', 'TMP', 'OS'}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix='ordinary-schema-synthetic-') as directory:
            root = Path(directory).resolve()
            env.update({
                'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1',
                'STBRAIN_MCP_TOKEN': 'synthetic-ordinary-schema-mcp-' + 'm' * 40,
                'STBRAIN_WAKE_SECRET': 'synthetic-ordinary-schema-wake-' + 'w' * 40,
                'STBRAIN_OWNER_ID': 'synthetic-ordinary-schema-owner',
                'STBRAIN_MODEL_ID': 'synthetic-ordinary-schema-model',
                'STBRAIN_DB_PATH': str(root / 'main.db'),
                'STBRAIN_LEARNING_IDEA_DB_PATH': str(root / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(root / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1',
                'STBRAIN_EXECUTION_EPOCH': 'synthetic-ordinary-schema-epoch',
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                'PYTHONIOENCODING': 'utf-8',
            })
            completed = subprocess.run([sys.executable, '-B', '-m',
                'mcp_server.tests.test_ordinary_revision_gateway_contract', '--probe', mode],
                cwd=Path(__file__).resolve().parents[2], env=env,
                capture_output=True, text=True, encoding='utf-8', timeout=90)
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual('PASS', proof['decision'])
        self.assertEqual(0, proof['network_attempts'])
        self.assertTrue(proof['actual_host_boundary_used'])
        self.assertTrue(proof['input_unchanged'])
        self.assertFalse(proof['real_brain_used'])

    def test_emotional_body_emotions_source_and_all_author_fields_roundtrip(self):
        self.run_probe('emotional_memory')

    def test_learning_body_source_and_all_author_fields_roundtrip_without_calm_check(self):
        self.run_probe('learning_memory')

    def test_plan_body_time_and_all_author_fields_roundtrip_without_calm_check(self):
        self.run_probe('planning_memory')

    def test_registered_schema_covers_exact_whitelists_and_module_specific_limits(self):
        self.run_probe('schema')

    def test_unmarked_defaults_and_null_clear_pass_real_registered_host_contract(self):
        self.run_probe('neutral_defaults')

    def test_credentials_stale_versions_and_foreign_owner_remain_rejected(self):
        self.run_probe('rejections')

    def test_module_one_gate_signed_raw_arguments_replay_and_keyword_compatibility(self):
        self.run_probe('permissions')

    def test_current_manual_and_workflow_describe_author_body_revision_and_history(self):
        mcp_root = Path(__file__).resolve().parents[1]
        document = (mcp_root / 'OPEN_RESPONSE.md').read_text(encoding='utf-8')
        tree = ast.parse((mcp_root / 'service.py').read_text(encoding='utf-8'))
        workflow = next(ast.literal_eval(value)
                        for node in ast.walk(tree) if isinstance(node, ast.Dict)
                        for key, value in zip(node.keys, node.values)
                        if isinstance(key, ast.Constant) and key.value == 'public_workflow')
        combined = document + '\n'.join(workflow)
        for stale in ('这不是正文更新接口', '只改允许的摘要或检索元数据', '不可变原文即使走高级接口也不能覆盖'):
            self.assertNotIn(stale, combined)
        for phrase in ('original_text', 'current_understanding', '情绪', '来源', '已读'):
            self.assertIn(phrase, document)
            self.assertIn(phrase, '\n'.join(workflow))
        self.assertIn('版本历史保留', document)
        self.assertIn('保留旧版本', '\n'.join(workflow))
        self.assertIn('作者自行选择叙述人称', document)
        self.assertIn('stbrain_help(module=对应模块)', document)
        self.assertIn('首次完成并激活后', combined)

    def test_planning_voice_and_scenario_tag_descriptions_match_author_input(self):
        # Contract definitions are pure schema data; this import starts no service.
        from mcp_server.public_contract import (
            _planning_content_input_properties, _TOOL_GUIDANCE_SCENARIO_TAGS_INPUT_SCHEMA,
        )
        adoption = _planning_content_input_properties()['ai_adoption_statement']['description']
        self.assertIn('人称由作者选择', adoption)
        self.assertNotIn('必须以', adoption)
        tags = _TOOL_GUIDANCE_SCENARIO_TAGS_INPUT_SCHEMA
        for phrase in ('自然语言', '中文或英文', '回家了', '准备睡觉', 'home.arrival',
                       '1–128', '16 项', '不重复', '省略或 null', '[] 清空'):
            self.assertIn(phrase, tags['description'])
        self.assertNotIn('不要填在本字段', tags['description'])
        self.assertEqual(16, tags['maxItems'])
        self.assertTrue(tags['uniqueItems'])


async def probe(mode):
    assert mode in MODES
    root = Path(os.environ['STBRAIN_DB_PATH']).resolve().parent
    databases = {root / name for name in ('main.db', 'ideas.db', 'vault.db')}
    real_connect = sqlite3.connect
    attempts = []

    def only_synthetic(path, *args, **kwargs):
        assert Path(path).resolve() in databases, 'outside_synthetic_database'
        return real_connect(path, *args, **kwargs)

    def deny(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('network_or_service_start_forbidden')

    with ExitStack() as guard:
        for name in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'socket.socket.sendto', 'socket.socket.bind', 'socket.getaddrinfo', 'subprocess.Popen'):
            guard.enter_context(patch(name, side_effect=deny))
        guard.enter_context(patch('sqlite3.connect', side_effect=only_synthetic))
        from jsonschema import Draft202012Validator
        from mcp_server import server
        from mcp_server.daily_revision_service import ORDINARY_REVISION_FIELDS
        from mcp_server.ordinary_revision_schema import ordinary_revision_fields
        from runtime.execution_binding import ExecutionStore, canonical_hash
        from rikkahub_gateway.tool_execution import HostExecutionBoundary, NativeToolCall, ToolExecutionBoundaryError
        from tests.test_onboarding import ModuleOneOnboardingTests

        common = {'owner_id': server.OWNER_ID, 'model_id': server.MODEL_ID}
        tools = server.mcp._tool_manager.list_tools()
        assert len(tools) == 44
        schemas = {tool.name: copy.deepcopy(tool.parameters) for tool in tools}
        entries = [{'canonical_name': name, 'schema_hash': canonical_hash(schema)}
                   for name, schema in schemas.items()]
        catalog = {'contract': 'advertised-tools/1', 'catalog_complete': True,
                   'catalog_hash': canonical_hash(entries), 'entries': entries}
        boundary = HostExecutionBoundary(b'synthetic-host-schema-secret-' + b'x' * 40)
        calls = 0
        host_calls = 0
        sequence = 0

        def rows(sql, values=()):
            with closing(sqlite3.connect(root / 'main.db')) as connection:
                return connection.execute(sql, values).fetchall()

        def snapshot(*, business=False):
            output = {}
            excluded = ('INSERT INTO "brain_execution_batches"', 'INSERT INTO "brain_execution_calls"')
            for path in sorted(databases):
                with closing(sqlite3.connect(path)) as connection:
                    lines = [line for line in connection.iterdump()
                             if not (business and line.startswith(excluded))]
                output[path.name] = hashlib.sha256('\n'.join(lines).encode()).hexdigest()
            return output

        async def call(name, arguments):
            nonlocal calls
            original = copy.deepcopy(arguments)
            blocks, result = await server.mcp.call_tool(name, arguments)
            assert arguments == original, 'registered_arguments_mutated'
            assert json.loads(blocks[0].text) == result
            calls += 1
            return result

        def host_bind(name, arguments):
            nonlocal host_calls, sequence
            sequence += 1
            before = copy.deepcopy(arguments)
            result = boundary.bind_call(wake_id=wake['wake_id'], catalog=catalog, schemas=schemas,
                call=NativeToolCall('synthetic-schema-call-' + str(sequence), name,
                                    json.dumps(arguments, ensure_ascii=False)))
            assert arguments == before and result.arguments_hash == canonical_hash(arguments)
            host_calls += 1
            return result

        def issue(name, arguments):
            bound = host_bind(name, arguments)
            result = registry.issue_batch(**common, wake_id=wake['wake_id'],
                wake_capability=wake['wake_capability'], batch_id='synthetic-schema-batch-' + str(sequence),
                revision=1, calls=[{'call_id': bound.tool_call_id, 'advertised_name': name,
                    'canonical_tool': name, 'schema_hash': bound.schema_hash,
                    'catalog_hash': bound.catalog_hash, 'arguments_hash': bound.arguments_hash}])
            return result['executions'][0]['execution_ref'], bound

        async def gateway(arguments):
            ref, bound = issue('revise_memory', arguments)
            result = await call('revise_memory', {**arguments, 'execution_ref': ref})
            expected_status = 'failed' if result.get('decision') == 'reject' else 'completed'
            assert rows('SELECT status,arguments_hash FROM brain_execution_calls WHERE call_id=?',
                        (bound.tool_call_id,)) == [(expected_status, canonical_hash(arguments))]
            return result

        async def reject_gateway(arguments, reasons):
            before = snapshot(business=True)
            result = await gateway(arguments)
            assert result.get('decision') == 'reject' and result.get('state_changed') is False
            actual = result.get('reason_codes', [result.get('reason_code')])
            assert set(actual) & set(reasons), 'unexpected_rejection:' + str(actual)
            assert snapshot(business=True) == before, 'rejection_changed_business_data'
            assert 'SYNTHETIC_PRIVATE_' not in json.dumps(result, ensure_ascii=False)
            return result

        if mode == 'permissions':
            before = snapshot()
            result = await call('revise_memory', {'target_ref': 'learning://learn_' + 'a' * 32 + '@1',
                'changes': {'current_understanding': 'Synthetic unopened author revision.'}})
            assert result.get('reason_code') == 'module_one_required'
            assert snapshot() == before

        fixture = ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / 'main.db', server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()
        core = rows('SELECT * FROM self_model_revisions ORDER BY revision_id')
        originals = {}
        for module in ('emotional_memory', 'learning_memory', 'planning_memory'):
            text = '  Synthetic ' + module + ' old body\nkept exactly.  '
            result = await call('remember_memory', {'module': module, 'content': text})
            assert result.get('decision') == 'stored'
            originals[module] = (result, text)

        wake = server.onboarding.issue_wake(**common, host_id='synthetic-schema-host',
            thread_id='synthetic-schema-thread', source_kind='human_message', source_event_id='synthetic-schema-turn')
        prepared = server.onboarding.build_pre_generation_context(**common, wake_id=wake['wake_id'],
            wake_capability=wake['wake_capability'], source_digest='synthetic-source', host_contract_digest='synthetic-host',
            advertised_tools=catalog, source_frame={'query_text': 'Synthetic author changes',
                'lineage_stable': False, 'prior_assistant_present': False, 'capture_items': []})
        assert prepared['decision'] == 'context_prepared'
        server.onboarding.confirm_context_injected(**common, wake_id=wake['wake_id'],
            wake_capability=wake['wake_capability'], context_hash=prepared['context_hash'])
        registry = ExecutionStore(root / 'main.db', deployment_epoch=os.environ['STBRAIN_EXECUTION_EPOCH'],
                                  capability_secret=server.WAKE_SECRET)
        wake_count = rows('SELECT COUNT(*) FROM brain_wake_sessions')[0][0]
        body = '  Synthetic new author body\nkept exactly.  '
        changes_by_module = {
            'emotional_memory': {
                'original_text': body, 'memory_type': 'shared_event',
                'source_timestamp': '2026-09-12T00:00:00+00:00', 'summary': 'Synthetic new emotional summary.',
                'primary_emotion': 'joy', 'secondary_emotions': ['trust'], 'importance': 61,
                'sensitivity': 'internal', 'context_policy': 'normal', 'origin': 'reported', 'confidence': 55,
                'keywords': ['合成主题'], 'entities': ['synthetic entity'], 'referent_bindings': [],
                'recall_mode': 'normal', 'allow_contexts': [], 'deny_contexts': [],
                'default_decision': 'background_reference', 'explicit_request_override': 'allow_after_confirmation',
                'disclosure': 'bounded_excerpt', 'lifecycle': 'active',
            },
            'learning_memory': {
                'kind': 'fact', 'title': 'Synthetic updated title', 'summary': 'Synthetic learning summary.',
                'current_understanding': body, 'steps': ['Synthetic first step.'],
                'application_contexts': ['Synthetic application.'], 'scene_tags': ['合成场景'],
                'preceding_context_summary': 'Synthetic context.', 'uncertainties': ['Synthetic uncertainty.'],
                'domain': 'synthetic', 'keywords': ['合成学习'], 'entities': ['synthetic entity'],
                'source_basis': 'inferred', 'claim_review': {'status': 'ordinary'}, 'confidence': 47,
                'time_sensitivity': 'stable', 'valid_as_of': '', 'review_after': '', 'importance': 61,
                'sensitivity': 'internal', 'context_policy': 'normal', 'recall_mode': 'normal',
                'allow_contexts': [], 'deny_contexts': [], 'default_decision': 'background_reference',
                'explicit_request_override': 'allow_after_confirmation', 'disclosure': 'bounded_excerpt',
                'lifecycle': 'active', 'referent_bindings': [],
            },
            'planning_memory': {
                'kind': 'task', 'track': 'internal', 'title': 'Synthetic updated plan', 'original_text': body,
                'summary': 'Synthetic plan summary.', 'reminder': '合成计划提醒', 'importance': 61,
                'presence_mode': 'relevant', 'scene_tags': ['合成计划'], 'keywords': ['合成关键词'],
                'start_at': None, 'due_at': None, 'timezone': 'UTC', 'review_after': None,
                'allow_coordination_hint': False, 'parent_ref': None, 'dependency_refs': [],
                'ai_adoption_statement': 'The synthetic author chooses this plan.',
            },
        }

        if mode in changes_by_module:
            saved, old_body = originals[mode]
            changes = changes_by_module[mode]
            assert set(changes) == ORDINARY_REVISION_FIELDS[mode], 'incomplete_author_field_coverage'
            result = await gateway({'target_ref': saved['ref'], 'changes': changes})
            assert result.get('decision') == 'revised' and result.get('version') == 2, 'gateway_revision_failed'
            if mode == 'emotional_memory':
                version_rows = rows('SELECT original_snapshot_json,mutable_json FROM emotion_memory_versions '
                                    'WHERE memory_id=? ORDER BY version', (saved['id'],))
                assert json.loads(version_rows[0][0])['original_text'] == old_body
                assert json.loads(version_rows[1][0])['original_text'] == body
                stored = json.loads(version_rows[1][1])
                assert stored['primary_emotion'] == 'joy' and stored['secondary_emotions'] == ['trust']
                assert stored['origin'] == 'reported'
                read = await call('recall_emotional_memory', {'memory_id': saved['id'], 'include_originals': True})
            elif mode == 'learning_memory':
                version_rows = rows('SELECT mutable_json FROM learning_versions WHERE learning_id=? ORDER BY version',
                                    (saved['id'],))
                assert json.loads(version_rows[0][0])['current_understanding'] == old_body
                stored = json.loads(version_rows[1][0])
                assert stored['current_understanding'] == body and stored['source_basis'] == 'inferred'
                assert stored['claim_review'] == {'status': 'ordinary'}
                read = await call('recall_learning_memory', {'target_ref': result['ref'], 'include_versions': True})
            else:
                version_rows = rows('SELECT content_json FROM planning_versions WHERE plan_id=? ORDER BY version',
                                    (saved['id'],))
                assert json.loads(version_rows[0][0])['original_text'] == old_body
                stored = json.loads(version_rows[1][0])
                assert stored['original_text'] == body and stored['reminder'] == changes['reminder']
                assert stored['ai_adoption_statement'] == changes['ai_adoption_statement']
                read = await call('recall_planning_memory', {'plan_ref': result['ref'], 'include_history': True})
            assert len(version_rows) == 2
            assert json.dumps(body, ensure_ascii=False)[1:-1] in json.dumps(read, ensure_ascii=False)

        elif mode == 'neutral_defaults':
            for module in ('emotional_memory', 'learning_memory'):
                saved = originals[module][0]
                field = 'origin' if module == 'emotional_memory' else 'source_basis'
                # Initial ordinary writes above used the registered direct MCP
                # entry and omitted all source/score/category parameters.
                if module == 'emotional_memory':
                    assert rows('SELECT origin,confidence,memory_type FROM emotion_memories WHERE memory_id=?',
                                (saved['id'],)) == [('unmarked', None, 'unclassified')]
                else:
                    content = json.loads(rows('SELECT current_json FROM learning_items WHERE learning_id=?', (saved['id'],))[0][0])
                    assert content['source_basis'] == 'unmarked' and content['confidence'] is None
                authored = await gateway({'target_ref': saved['ref'], 'changes': {field: 'reported', 'confidence': 50}})
                cleared = await gateway({'target_ref': authored['ref'], 'changes': {field: 'unmarked', 'confidence': None}})
                assert cleared.get('decision') == 'revised'
                table, column = ('emotion_memory_versions', 'memory_id') if module == 'emotional_memory' else ('learning_versions', 'learning_id')
                versions = [json.loads(v[0]) for v in rows(f'SELECT mutable_json FROM {table} WHERE {column}=? ORDER BY version', (saved['id'],))]
                assert [v['confidence'] for v in versions] == [None, 50, None]
                assert versions[1][field] == 'reported' and versions[2][field] == 'unmarked'
            for name in ('remember_memory', 'remember_emotional_memory', 'remember_learning_memory',
                         'integrate_emotional_memories', 'integrate_learning_memories'):
                params = schemas[name]
                assert 'confidence' not in params.get('required', [])
                assert params['properties']['confidence'].get('default') is None
            assert 'source_basis' not in schemas['remember_learning_memory'].get('required', [])
            advanced = await call('remember_learning_memory', {
                'kind': 'fact', 'title': 'Synthetic advanced unmarked', 'summary': 'Synthetic summary.',
                'current_understanding': 'Synthetic advanced content.',
                'correctness_assessment': 'Synthetic author note.', 'reason': 'Synthetic author reason.',
            })
            assert advanced.get('decision') == 'stored'
            content = json.loads(rows('SELECT current_json FROM learning_items WHERE learning_id=?', (advanced['learning_id'],))[0][0])
            assert content['source_basis'] == 'unmarked' and content['confidence'] is None
            advanced_emotion = await call('remember_emotional_memory', {
                'memory_type': 'feeling', 'original_text': 'Synthetic advanced emotion.',
                'summary': 'Synthetic summary.', 'primary_emotion': 'joy', 'reason': 'Synthetic author reason.',
            })
            assert advanced_emotion.get('decision') == 'stored'
            assert advanced_emotion['memory']['origin'] == 'unmarked'
            assert advanced_emotion['memory']['confidence'] is None

        elif mode == 'schema':
            card = await call('remember_tool_guidance', {
                'tool_name': 'SyntheticSchemaService', 'purpose': 'Synthetic tool schema fixture.'})
            assert card['decision'] == 'stored'
            card_id = card['card']['card_id']
            originals['tool_guidance'] = ({'ref': f'tool-card://{card_id}@1'}, '')
            changes_by_module['tool_guidance'] = {
                'purpose': 'Synthetic changed use.', 'reminder': None,
                'scenario_tags': ['回家了', '到家啦'], 'confidence': 100,
                'expires_at': None, 'source_ref': None,
            }
            schema = schemas['revise_memory']
            Draft202012Validator.check_schema(schema)
            assert '$ref' not in json.dumps(schema), 'public_schema_retains_stranded_reference'
            fields = ordinary_revision_fields()
            for module, per_module in fields.items():
                assert set(per_module) == ORDINARY_REVISION_FIELDS[module]
                saved = originals[module][0]
                host_bind('revise_memory', {'target_ref': saved['ref'], 'changes': changes_by_module[module]})
            tool_changed = await gateway({'target_ref': originals['tool_guidance'][0]['ref'],
                                         'changes': changes_by_module['tool_guidance']})
            assert tool_changed.get('decision') == 'revised' and tool_changed['version'] == 2
            tool_read = await call('recall_tool_guidance', {'card_id': card_id, 'view': 'card'})
            tool_content = tool_read['results'][0]['content']
            assert tool_content['purpose'] == 'Synthetic changed use.'
            assert tool_content['claimed_confidence'] == 100 and tool_content['expires_at'] is None
            assert tool_content['scenario_tags'] == ['回家了', '到家啦']
            fields['learning_memory'].clear()
            assert set(ordinary_revision_fields()['learning_memory']) == ORDINARY_REVISION_FIELDS['learning_memory']
            assert set(schema['properties']['changes']['properties']) == set().union(*ORDINARY_REVISION_FIELDS.values())
            bad_changes = [
                ('emotional_memory', {'title': 'Wrong module.'}),
                ('learning_memory', {'original_text': 'Wrong module.'}),
                ('planning_memory', {'primary_emotion': 'joy'}),
                ('learning_memory', {'owner_id': 'SYNTHETIC_PRIVATE_OTHER'}),
                ('learning_memory', {'epistemic_status': 'observed'}),
                ('planning_memory', {'write_mode': 'ordinary_record'}),
                ('emotional_memory', {'original_hash': 'f' * 64}),
                ('learning_memory', {'current_understanding': 'x' * 2001}),
                ('emotional_memory', {'summary': 'x' * 201}),
                ('planning_memory', {'keywords': ['tag' + str(i) for i in range(17)]}),
                ('learning_memory', {'scene_tags': ['tag' + str(i) for i in range(13)]}),
                ('emotional_memory', {'secondary_emotions': ['joy', 'trust']}),
                ('learning_memory', {'keywords': 'one,two'}),
                ('planning_memory', {'keywords': None}),
                ('emotional_memory', {'importance': True}),
                ('tool_guidance', {'original_text': 'Wrong module.'}),
                ('tool_guidance', {'effective_confidence': 100}),
                ('tool_guidance', {'confidence': 101}),
            ]
            for module, changes in bad_changes:
                before = snapshot()
                try:
                    host_bind('revise_memory', {'target_ref': originals[module][0]['ref'], 'changes': changes})
                except ToolExecutionBoundaryError as exc:
                    assert str(exc) == 'tool_arguments_schema_invalid'
                else:
                    raise AssertionError('bad_author_schema_accepted')
                assert snapshot() == before
            # Different module limits are not flattened to the lowest maximum.
            host_bind('revise_memory', {'target_ref': originals['learning_memory'][0]['ref'],
                'changes': {'summary': 'x' * 240, 'keywords': ['tag' + str(i) for i in range(24)]}})
            for invalid_ref in ('self://self_' + 'a' * 32 + '@1', 'learning://learn_' + 'a' * 32 + '@0'):
                try:
                    host_bind('revise_memory', {'target_ref': invalid_ref, 'changes': {'summary': 'Synthetic.'}})
                except ToolExecutionBoundaryError as exc:
                    assert str(exc) == 'tool_arguments_schema_invalid'
                else:
                    raise AssertionError('invalid_target_schema_accepted')

        elif mode == 'rejections':
            for module, (saved, _) in originals.items():
                field = 'current_understanding' if module == 'learning_memory' else 'original_text'
                await reject_gateway({'target_ref': saved['ref'], 'changes': {
                    field: '密码轮换为 SYNTHETIC_PRIVATE_PASSWORD_903'}},
                    {'credential_or_secret_detected', 'credential_content_rejected'})
                good = await gateway({'target_ref': saved['ref'], 'changes': {field: body}})
                assert good.get('decision') == 'revised'
                await reject_gateway({'target_ref': saved['ref'], 'changes': {field: 'Synthetic stale edit.'}},
                    {'memory_version_conflict', 'learning_target_version_conflict',
                     'plan_version_conflict', 'expected_plan_version_conflict'})
            # A real synthetic foreign-owner row, created via the runtime setup
            # API, remains inaccessible to this registered service's identity.
            store = server.emotional_service.store
            foreign_owner = 'synthetic-other-owner'
            store.ensure_state(owner_id=foreign_owner, model_id=server.MODEL_ID)
            status = store.status(owner_id=foreign_owner, model_id=server.MODEL_ID)
            foreign = store.remember(owner_id=foreign_owner, model_id=server.MODEL_ID,
                wake_id='synthetic-foreign-setup', expected_row_version=status['row_version'],
                memory_type='shared_event', original_text='Synthetic other owner body.',
                summary='Synthetic other owner summary.', primary_emotion='other')
            foreign_id = foreign['memory']['memory_id']
            await reject_gateway({'target_ref': f'emotion://{foreign_id}@1',
                                  'changes': {'summary': 'SYNTHETIC_PRIVATE_FORBIDDEN_EDIT'}}, {'memory_not_found'})

        elif mode == 'permissions':
            saved = originals['learning_memory'][0]
            arguments = {'target_ref': saved['ref'], 'changes': {'current_understanding': body}}
            ref, bound = issue('revise_memory', arguments)
            before = snapshot()
            changed = await call('revise_memory', {**arguments,
                'changes': {'current_understanding': 'SYNTHETIC_PRIVATE_TAMPERED'}, 'execution_ref': ref})
            assert changed.get('reason_code') == 'execution_binding_invalid_or_finished'
            assert snapshot() == before
            accepted = await call('revise_memory', {**arguments, 'execution_ref': ref})
            assert accepted.get('decision') == 'revised'
            before = snapshot()
            replayed = await call('revise_memory', {**arguments, 'execution_ref': ref})
            assert replayed.get('reason_code') == 'execution_binding_invalid_or_finished'
            assert snapshot() == before
            assert rows('SELECT status,arguments_hash FROM brain_execution_calls WHERE call_id=?',
                        (bound.tool_call_id,)) == [('completed', canonical_hash(arguments))]
            # Canonical arrays cross the host. The existing L16 convenience is
            # receiving-MCP-only and must remain available on its direct route.
            direct = await call('revise_memory', {'target_ref': accepted['ref'],
                'changes': {'keywords': '合成甲，合成乙'}})
            assert direct.get('decision') == 'revised'
            content = json.loads(rows('SELECT current_json FROM learning_items WHERE learning_id=?',
                                      (saved['id'],))[0][0])
            assert content['keywords'] == ['合成甲', '合成乙']

        assert rows('SELECT * FROM self_model_revisions ORDER BY revision_id') == core
        assert rows('SELECT COUNT(*) FROM brain_wake_sessions')[0][0] == wake_count
        assert not attempts and host_calls > 0
        return {'decision': 'PASS', 'mode': mode, 'registered_calls': calls,
                'host_bindings': host_calls, 'actual_host_boundary_used': True,
                'input_unchanged': True, 'network_attempts': 0, 'real_brain_used': False,
                'real_model_called': False, 'http_transport_tested': False}


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        print(json.dumps(asyncio.run(probe(sys.argv[2])), ensure_ascii=False))
    else:
        unittest.main()
