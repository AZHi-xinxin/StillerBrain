"""Actual registered MCP + signed host calls; disposable synthetic stores only."""
from __future__ import annotations

import asyncio
from contextlib import closing, ExitStack
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class GovernanceAutoCasTransportTests(unittest.TestCase):
    def run_probe(self, mode):
        allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE', 'PATH',
                   'PATHEXT', 'TEMP', 'TMP', 'OS'}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix='governance-auto-cas-synthetic-') as directory:
            root = Path(directory).resolve()
            env.update({
                'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1',
                'STBRAIN_MCP_TOKEN': 'synthetic-governance-mcp-' + 'm' * 40,
                'STBRAIN_WAKE_SECRET': 'synthetic-governance-wake-' + 'w' * 40,
                'STBRAIN_OWNER_ID': 'synthetic-governance-owner',
                'STBRAIN_MODEL_ID': 'synthetic-governance-model',
                'STBRAIN_DB_PATH': str(root / 'main.db'),
                'STBRAIN_LEARNING_IDEA_DB_PATH': str(root / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(root / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1',
                'STBRAIN_EXECUTION_EPOCH': 'synthetic-governance-epoch',
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                'PYTHONIOENCODING': 'utf-8',
            })
            completed = subprocess.run([sys.executable, '-B', '-m',
                'mcp_server.tests.test_governance_auto_cas_transport', '--probe', mode],
                cwd=Path(__file__).resolve().parents[2], env=env,
                capture_output=True, text=True, encoding='utf-8', timeout=90)
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual('PASS', proof['decision'])
        self.assertEqual(0, proof['network_attempts'])
        self.assertFalse(proof['real_brain_used'])
        if mode == 'gateway':
            self.assertGreater(proof['host_bindings'], 0)

    def test_direct_repeated_set_tags_clear_rollback_same_and_next_wake(self):
        self.run_probe('direct')

    def test_signed_gateway_repeated_set_tags_clear_rollback_and_replay(self):
        self.run_probe('gateway')

    def test_explicit_stale_cas_and_foreign_rollback_target_reject(self):
        self.run_probe('negative')

    def test_interleaved_write_cannot_overwrite_newer_content(self):
        self.run_probe('concurrent')

    def test_first_activation_still_required_and_scope_snapshot_is_single(self):
        self.run_probe('permissions')


async def probe(mode):
    root = Path(os.environ['STBRAIN_DB_PATH']).resolve().parent
    databases = {root / name for name in ('main.db', 'ideas.db', 'vault.db')}
    real_connect = sqlite3.connect
    attempts = []

    def only_synthetic(path, *args, **kwargs):
        assert Path(path).resolve() in databases, 'outside_synthetic_database'
        return real_connect(path, *args, **kwargs)

    def deny(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('network_or_service_creation_forbidden')

    with ExitStack() as guards:
        for name in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'socket.socket.sendto', 'socket.socket.bind', 'socket.getaddrinfo', 'subprocess.Popen'):
            guards.enter_context(patch(name, side_effect=deny))
        guards.enter_context(patch('sqlite3.connect', side_effect=only_synthetic))
        from jsonschema import Draft202012Validator
        from mcp_server import server
        from runtime.execution_binding import ExecutionStore, canonical_hash
        from runtime.self_governance import GOVERNANCE_DIRECT_SCOPES
        from rikkahub_gateway.tool_execution import HostExecutionBoundary, NativeToolCall
        from tests.test_onboarding import ModuleOneOnboardingTests

        common = {'owner_id': server.OWNER_ID, 'model_id': server.MODEL_ID}
        schemas = {tool.name: copy.deepcopy(tool.parameters)
                   for tool in server.mcp._tool_manager.list_tools()}
        entries = [{'canonical_name': name, 'schema_hash': canonical_hash(schema)}
                   for name, schema in schemas.items()]
        catalog = {'contract': 'advertised-tools/1', 'catalog_complete': True,
                   'catalog_hash': canonical_hash(entries), 'entries': entries}
        boundary = HostExecutionBoundary(b'synthetic-governance-host-' + b'x' * 40)
        registry = ExecutionStore(root / 'main.db',
            deployment_epoch=os.environ['STBRAIN_EXECUTION_EPOCH'], capability_secret=server.WAKE_SECRET)
        count = 0
        host_calls = 0
        wake_number = 0

        def rows(sql, args=()):
            with closing(sqlite3.connect(root / 'main.db')) as connection:
                return connection.execute(sql, args).fetchall()

        def revisions():
            return rows('SELECT revision_id,scope,operation,content_json,parent_revision_id '
                        'FROM self_governance_revisions ORDER BY scope,revision_number')

        async def direct(name, arguments):
            original = copy.deepcopy(arguments)
            blocks, result = await server.mcp.call_tool(name, arguments)
            assert arguments == original and json.loads(blocks[0].text) == result
            return result

        def prepare_wake():
            nonlocal wake_number
            wake_number += 1
            issued = server.onboarding.issue_wake(**common, host_id='synthetic-governance-host',
                thread_id='synthetic-governance-thread', source_kind='human_message',
                source_event_id='synthetic-governance-turn-' + str(wake_number))
            prepared = server.onboarding.build_pre_generation_context(**common,
                wake_id=issued['wake_id'], wake_capability=issued['wake_capability'],
                source_digest='synthetic-source-' + str(wake_number), host_contract_digest='synthetic-host',
                advertised_tools=catalog, source_frame={'query_text': 'synthetic-scene',
                    'lineage_stable': False, 'prior_assistant_present': False, 'capture_items': []})
            assert prepared['decision'] == 'context_prepared'
            server.onboarding.confirm_context_injected(**common, wake_id=issued['wake_id'],
                wake_capability=issued['wake_capability'], context_hash=prepared['context_hash'])
            return issued

        async def gateway(name, arguments, *, replay=False):
            nonlocal count, host_calls
            count += 1
            original = copy.deepcopy(arguments)
            bound = boundary.bind_call(wake_id=wake['wake_id'], catalog=catalog, schemas=schemas,
                call=NativeToolCall('synthetic-governance-call-' + str(count), name,
                                    json.dumps(arguments, ensure_ascii=False)))
            assert arguments == original and bound.arguments_hash == canonical_hash(arguments)
            issued = registry.issue_batch(**common, wake_id=wake['wake_id'],
                wake_capability=wake['wake_capability'], batch_id='synthetic-governance-batch-' + str(count),
                revision=1, calls=[{'call_id': bound.tool_call_id, 'advertised_name': name,
                    'canonical_tool': name, 'schema_hash': bound.schema_hash,
                    'catalog_hash': bound.catalog_hash, 'arguments_hash': bound.arguments_hash}])
            raw = {**arguments, 'execution_ref': issued['executions'][0]['execution_ref']}
            host_calls += 1
            result = await direct(name, raw)
            stored = rows('SELECT arguments_hash FROM brain_execution_calls WHERE call_id=?',
                          (bound.tool_call_id,))
            assert stored == [(canonical_hash(arguments),)]
            if replay:
                before = revisions()
                rejected = await direct(name, raw)
                assert rejected['state_changed'] is False and revisions() == before
            return result

        name = 'manage_self_governance_profile'
        initial = {'action': 'set', 'scope': 'tool_use', 'text': 'Synthetic first reminder.',
                   'trigger_mode': 'scene_relevant', 'scene_tags': ['synthetic-scene']}
        Draft202012Validator(schemas[name]).validate(initial)
        assert 'expected_profile_version' not in schemas[name].get('required', [])
        assert 'expected_active_revision' not in schemas[name].get('required', [])
        if mode == 'permissions':
            before = revisions()
            blocked = await direct(name, initial)
            assert blocked['reason_code'] == 'module_one_required' and revisions() == before

        fixture = ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / 'main.db', server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()
        core = rows('SELECT * FROM self_model_revisions ORDER BY revision_id')
        wake = prepare_wake()
        transport = gateway if mode == 'gateway' else direct

        if mode in {'direct', 'gateway'}:
            for scope in GOVERNANCE_DIRECT_SCOPES:
                args = {**initial, 'scope': scope}
                first = await transport(name, args)
                assert first['decision'] == 'committed', first.get('reason_codes')
                second = await transport(name, {**args, 'text': 'Synthetic edited reminder.',
                    'scene_tags': ['修改中文场景', 'synthetic-scene']})
                assert second['decision'] == 'committed', second.get('reason_codes')
                cleared = await transport(name, {'action': 'clear', 'scope': scope})
                assert cleared['decision'] == 'committed', cleared.get('reason_codes')
                restored = await transport(name, args)
                assert restored['decision'] == 'committed', restored.get('reason_codes')
                history = await transport('query_self_governance_profile',
                    {'view': 'revisions', 'scope': scope, 'include_content': True})
                assert len(history['result']) == 4
                target = history['result'][1]
                assert target['revision_id'] == second['revision_id']
                assert target['content']['scene_tags'] == ['修改中文场景', 'synthetic-scene']
                rolled = await transport(name, {'action': 'rollback', 'scope': scope,
                    'target_revision_id': target['revision_id']})
                assert rolled['decision'] == 'committed', rolled.get('reason_codes')
                assert [first['row_version'], second['row_version'], cleared['row_version'],
                        restored['row_version'], rolled['row_version']] == [1, 2, 3, 4, 5]
                queried = await transport('query_self_governance_profile',
                    {'view': 'status', 'scope': scope, 'include_content': True})
                state = queried['result']['scopes'][scope]
                assert state['active_revision_id'] == rolled['revision_id']
                assert '修改中文场景' in json.dumps(state, ensure_ascii=False)
            wake = prepare_wake()
            for scope in GOVERNANCE_DIRECT_SCOPES:
                updated = await transport(name, {**initial, 'scope': scope,
                    'text': 'Synthetic following wake reminder.'})
                assert updated['decision'] == 'committed' and updated['row_version'] == 6
            assert len(revisions()) == len(GOVERNANCE_DIRECT_SCOPES) * 6
            if mode == 'gateway':
                result = await gateway(name, {**initial, 'text': 'Synthetic replay protected reminder.'}, replay=True)
                assert result['decision'] == 'committed'
        elif mode == 'negative':
            first = await direct(name, initial)
            before = revisions()
            for stale in ({'expected_profile_version': 0}, {'expected_active_revision': None},
                          {'expected_active_revision': 'govrev_synthetic_wrong'}):
                result = await direct(name, {**initial, **stale})
                assert result['decision'] == 'rejected' and result['state_changed'] is False
                assert result.get('retryable') is True and '无需' in result['next_action']
                assert revisions() == before
            other = await direct(name, {**initial, 'scope': 'learning_memory'})
            result = await direct(name, {'action': 'rollback', 'scope': 'tool_use',
                                        'target_revision_id': other['revision_id']})
            assert result['reason_codes'] == ['rollback_target_not_found']
            assert len(revisions()) == 2
            # Legacy explicit coordinates remain accepted when they really match.
            changed = await direct(name, {**initial, 'expected_profile_version': first['row_version'],
                'expected_active_revision': first['revision_id']})
            assert changed['decision'] == 'committed'
        elif mode == 'concurrent':
            await direct(name, initial)
            original_status = server.service.governance.status
            entered = []

            def interleaved_status():
                old = original_status()
                state = old['scopes']['tool_use']
                if not entered:
                    entered.append(True)
                    server.service.governance.store.commit_revision(**common, scope='tool_use',
                        operation='set', content={'schema_version': '0.1.0',
                            'text': 'Synthetic concurrently committed content.',
                            'trigger_mode': 'manual_only', 'scene_tags': []},
                        wake_id='synthetic-interleaved-operation', wake_seq=1,
                        expected_row_version=state['row_version'],
                        expected_active_revision=state['active_revision_id'])
                return old

            with patch.object(server.service.governance, 'status', side_effect=interleaved_status):
                rejected = await direct(name, {**initial, 'text': 'Synthetic must not overwrite winner.'})
            assert rejected['decision'] == 'rejected' and rejected['state_changed'] is False
            assert rejected.get('retryable') is True
            assert len(revisions()) == 2
            assert 'Synthetic concurrently committed content.' in revisions()[-1][3]
            retry = await direct(name, {**initial, 'text': 'Synthetic fresh author resubmission.'})
            assert retry['decision'] == 'committed' and retry['row_version'] == 3
        elif mode == 'permissions':
            with patch.object(server.service.governance, 'status',
                              wraps=server.service.governance.status) as status:
                result = await direct(name, initial)
                assert result['decision'] == 'committed' and status.call_count == 1
        else:
            raise AssertionError('unknown_probe_mode')
        assert rows('SELECT * FROM self_model_revisions ORDER BY revision_id') == core
        assert rows('SELECT * FROM self_governance_candidates') == []
        return {'decision': 'PASS', 'mode': mode, 'network_attempts': len(attempts),
                'real_brain_used': False, 'host_bindings': host_calls}


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        print(json.dumps(asyncio.run(probe(sys.argv[2])), ensure_ascii=False))
    else:
        unittest.main()
