"""Optional author audit notes through actual host and registered MCP boundaries.

Each child uses three disposable SQLite databases, synthetic activated identity,
and a real execution ledger. Network and service creation are forbidden. These
tests never open an installed brain or replace an authorization validator.
"""
from __future__ import annotations

import asyncio
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


TOOLS = ('revise_learning_memory', 'integrate_learning_memories', 'revise_planning_memory')
AUDIT_FIELDS = {
    TOOLS[0]: ('classification_basis', 'correctness_assessment', 'diff', 'calm_check'),
    TOOLS[1]: ('classification_basis', 'correctness_assessment', 'diff', 'calm_check'),
    TOOLS[2]: ('calm_check', 'ai_confirmation'),
}
MODES = ('direct', 'gateway', 'schema', 'permissions', 'negative')


class LegacyOptionalAuditTransportTests(unittest.TestCase):
    def run_probe(self, mode):
        allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE', 'PATH',
                   'PATHEXT', 'TEMP', 'TMP', 'OS'}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix='optional-audit-synthetic-') as directory:
            root = Path(directory).resolve()
            env.update({
                'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1',
                'STBRAIN_MCP_TOKEN': 'synthetic-optional-audit-mcp-' + 'm' * 40,
                'STBRAIN_WAKE_SECRET': 'synthetic-optional-audit-wake-' + 'w' * 40,
                'STBRAIN_OWNER_ID': 'synthetic-optional-audit-owner',
                'STBRAIN_MODEL_ID': 'synthetic-optional-audit-model',
                'STBRAIN_DB_PATH': str(root / 'main.db'),
                'STBRAIN_LEARNING_IDEA_DB_PATH': str(root / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(root / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1',
                'STBRAIN_EXECUTION_EPOCH': 'synthetic-optional-audit-epoch',
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                'PYTHONIOENCODING': 'utf-8',
            })
            completed = subprocess.run([sys.executable, '-B', '-m',
                'mcp_server.tests.test_legacy_optional_audit_transport', '--probe', mode],
                cwd=Path(__file__).resolve().parents[2], env=env,
                capture_output=True, text=True, encoding='utf-8', timeout=90)
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual('PASS', proof['decision'])
        self.assertEqual(0, proof['network_attempts'])
        self.assertFalse(proof['real_brain_used'])
        self.assertTrue(proof['input_unchanged'])
        if mode in {'gateway', 'permissions', 'negative'}:
            self.assertGreater(proof['host_bindings'], 0)

    def test_direct_omitted_and_null_audits_append_history_without_fake_review(self):
        self.run_probe('direct')

    def test_real_host_signed_gateway_omitted_and_null_audits_append_history(self):
        self.run_probe('gateway')

    def test_public_and_runtime_models_optional_but_old_review_stays_strict(self):
        self.run_probe('schema')

    def test_first_activation_gate_covers_direct_and_signed_gateway(self):
        self.run_probe('permissions')

    def test_stale_versions_false_confirmation_audit_limits_and_replay_reject(self):
        self.run_probe('negative')


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
        from jsonschema import Draft202012Validator, ValidationError
        from mcp_server import server
        from mcp_server.public_contract import (
            _LEARNING_CALM_CHECK_INPUT_SCHEMA, _PLANNING_CALM_CHECK_INPUT_SCHEMA,
        )
        from runtime.execution_binding import ExecutionStore, canonical_hash
        from rikkahub_gateway.tool_execution import HostExecutionBoundary, NativeToolCall, ToolExecutionBoundaryError
        from tests.test_onboarding import ModuleOneOnboardingTests

        common = {'owner_id': server.OWNER_ID, 'model_id': server.MODEL_ID}
        schemas = {tool.name: copy.deepcopy(tool.parameters) for tool in server.mcp._tool_manager.list_tools()}
        entries = [{'canonical_name': name, 'schema_hash': canonical_hash(schema)}
                   for name, schema in schemas.items()]
        catalog = {'contract': 'advertised-tools/1', 'catalog_complete': True,
                   'catalog_hash': canonical_hash(entries), 'entries': entries}
        boundary = HostExecutionBoundary(b'synthetic-optional-audit-host-' + b'x' * 40)
        registry = ExecutionStore(root / 'main.db', deployment_epoch=os.environ['STBRAIN_EXECUTION_EPOCH'],
                                  capability_secret=server.WAKE_SECRET)
        count = 0
        host_calls = 0

        def rows(sql, args=()):
            with closing(sqlite3.connect(root / 'main.db')) as connection:
                return connection.execute(sql, args).fetchall()

        def snapshot():
            result = {}
            excluded = ('INSERT INTO "brain_execution_batches"', 'INSERT INTO "brain_execution_calls"')
            for path in sorted(databases):
                with closing(sqlite3.connect(path)) as connection:
                    lines = [line for line in connection.iterdump() if not line.startswith(excluded)]
                result[path.name] = hashlib.sha256('\n'.join(lines).encode()).hexdigest()
            return result

        async def direct(name, arguments):
            original = copy.deepcopy(arguments)
            blocks, result = await server.mcp.call_tool(name, arguments)
            assert arguments == original and json.loads(blocks[0].text) == result
            return result

        def prepare_wake():
            wake = server.onboarding.issue_wake(**common, host_id='synthetic-audit-host',
                thread_id='synthetic-audit-thread', source_kind='human_message',
                source_event_id='synthetic-audit-turn-' + str(count))
            prepared = server.onboarding.build_pre_generation_context(**common, wake_id=wake['wake_id'],
                wake_capability=wake['wake_capability'], source_digest='synthetic-source',
                host_contract_digest='synthetic-host', advertised_tools=catalog,
                source_frame={'query_text': 'Synthetic author changes', 'lineage_stable': False,
                              'prior_assistant_present': False, 'capture_items': []})
            assert prepared['decision'] == 'context_prepared'
            server.onboarding.confirm_context_injected(**common, wake_id=wake['wake_id'],
                wake_capability=wake['wake_capability'], context_hash=prepared['context_hash'])
            return wake

        def host_bind(name, args):
            nonlocal count, host_calls
            count += 1
            original = copy.deepcopy(args)
            bound = boundary.bind_call(wake_id=wake['wake_id'], catalog=catalog, schemas=schemas,
                call=NativeToolCall('synthetic-audit-call-' + str(count), name,
                                    json.dumps(args, ensure_ascii=False)))
            assert original == args and bound.arguments_hash == canonical_hash(args)
            host_calls += 1
            return bound

        async def gateway(name, args, *, replay=False):
            bound = host_bind(name, args)
            issued = registry.issue_batch(**common, wake_id=wake['wake_id'],
                wake_capability=wake['wake_capability'], batch_id='synthetic-audit-batch-' + str(count),
                revision=1, calls=[{'call_id': bound.tool_call_id, 'advertised_name': name,
                    'canonical_tool': name, 'schema_hash': bound.schema_hash,
                    'catalog_hash': bound.catalog_hash, 'arguments_hash': bound.arguments_hash}])
            raw = {**args, 'execution_ref': issued['executions'][0]['execution_ref']}
            result = await direct(name, raw)
            status = 'failed' if result.get('decision') == 'reject' else 'completed'
            assert rows('SELECT status,arguments_hash FROM brain_execution_calls WHERE call_id=?',
                        (bound.tool_call_id,)) == [(status, canonical_hash(args))]
            if replay:
                before = snapshot()
                again = await direct(name, raw)
                assert again.get('decision') == 'reject' and again.get('state_changed') is False
                assert snapshot() == before, 'replay_changed_business'
            return result

        placeholder = 'learning://learn_' + 'a' * 32 + '@1'
        args_by_name = {
            TOOLS[0]: {'target_ref': placeholder, 'expected_target_version': 1,
                'action': 'change', 'change_class': 'semantic_change', 'reason': 'Synthetic authored correction.',
                'current_understanding': 'Synthetic revised knowledge.'},
            TOOLS[1]: {'source_learning_ids': [placeholder, 'learning://learn_' + 'b' * 32 + '@1'],
                'synthesis_kind': 'summary', 'reason': 'Synthetic authored synthesis.', 'kind': 'fact',
                'title': 'Synthetic synthesis', 'summary': 'Synthetic summary',
                'current_understanding': 'Synthetic combined knowledge.', 'source_basis': 'reported', 'confidence': 50},
            TOOLS[2]: {'plan_id': 'plan_' + 'a' * 32, 'expected_plan_version': 1, 'intent': 'revise',
                'reason': 'Synthetic authored plan edit.', 'idempotency_key': 'synthetic-audit-plan-edit',
                'changes': {'original_text': 'Synthetic updated plan.'}},
        }
        if mode == 'permissions':
            wake = prepare_wake()
            for name in TOOLS:
                for transport in (direct, gateway):
                    before = snapshot()
                    result = await transport(name, args_by_name[name])
                    assert result.get('reason_code') == 'module_one_required'
                    assert result.get('state_changed') is False and snapshot() == before

        fixture = ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / 'main.db', server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()
        assert server.onboarding.state(**common)['module_one_unlocked'] is True
        core = rows('SELECT * FROM self_model_revisions ORDER BY revision_id')
        if mode != 'permissions':
            wake = prepare_wake()

        async def seed(module, text):
            result = await direct('remember_memory', {'module': module, 'content': text})
            assert result.get('decision') == 'stored'
            return result

        first = await seed('learning_memory', 'Synthetic original knowledge A.')
        second = await seed('learning_memory', 'Synthetic original knowledge B.')
        plan = await seed('planning_memory', 'Synthetic original plan.')
        args_by_name[TOOLS[0]].update(target_ref=first['ref'])
        args_by_name[TOOLS[1]].update(source_learning_ids=[first['ref'], second['ref']])
        args_by_name[TOOLS[2]].update(plan_id=plan['id'])
        old_learning = rows('SELECT * FROM learning_versions ORDER BY version_id')
        old_plan = rows('SELECT * FROM planning_versions ORDER BY plan_id,version')

        if mode in {'direct', 'gateway', 'permissions'}:
            transport = gateway if mode == 'gateway' else direct
            current_ref = first['ref']
            for index, nulls in enumerate((False, True), 2):
                revise_args = {**args_by_name[TOOLS[0]], 'target_ref': current_ref,
                    'expected_target_version': index - 1,
                    'current_understanding': 'Synthetic revised knowledge version ' + str(index)}
                integrate_args = {**args_by_name[TOOLS[1]], 'source_learning_ids': [current_ref, second['ref']]}
                plan_args = {**args_by_name[TOOLS[2]], 'expected_plan_version': index - 1,
                    'idempotency_key': 'synthetic-audit-plan-' + str(index),
                    'changes': {'original_text': 'Synthetic revised plan version ' + str(index)}}
                for name, args in ((TOOLS[1], integrate_args), (TOOLS[0], revise_args), (TOOLS[2], plan_args)):
                    if nulls:
                        args.update({field: None for field in AUDIT_FIELDS[name]})
                    result = await transport(name, args)
                    assert result.get('decision') in {'applied', 'revised'}, str(result)
                    assert result.get('state_changed') is True
                    if name == TOOLS[0]:
                        current_ref = result['item_ref']
                    if name == TOOLS[1]:
                        assert [source['ref'] for source in result['source_snapshot']] == args['source_learning_ids']
                    if name == TOOLS[2]:
                        assert result['candidate_created'] is False and result['review_performed'] is False
            assert rows('SELECT COUNT(*) FROM learning_change_candidates') == [(0,)]
            assert rows('SELECT COUNT(*) FROM planning_change_candidates') == [(0,)]
            assert rows('SELECT COUNT(*) FROM learning_direct_integrations') == [(2,)]
            prior_ids = {row[0] for row in old_learning}
            new_audits = [row[1:] for row in rows(
                'SELECT version_id,ai_diff,correctness_assessment FROM learning_versions')
                if row[0] not in prior_ids]
            assert new_audits == [('', '')] * 4, 'new_revision_fabricated_audit'
            assert all(row in rows('SELECT * FROM learning_versions') for row in old_learning)
            assert all(row in rows('SELECT * FROM planning_versions') for row in old_plan)
            assert rows('SELECT current_version FROM learning_items WHERE learning_id=?', (second['id'],)) == [(1,)]
            assert rows('SELECT current_version FROM learning_items WHERE learning_id=?', (first['id'],)) == [(3,)]
            assert rows('SELECT current_version FROM planning_items WHERE plan_id=?', (plan['id'],)) == [(3,)]

        elif mode == 'schema':
            for name, fields in AUDIT_FIELDS.items():
                model = server.mcp._tool_manager.get_tool(name).fn_metadata.arg_model
                for field in fields:
                    assert field not in schemas[name].get('required', [])
                    assert not model.model_fields[field].is_required()
                for nulls in (False, True):
                    args = copy.deepcopy(args_by_name[name])
                    if nulls:
                        args.update({field: None for field in fields})
                    Draft202012Validator(schemas[name]).validate(args)
                    internal = {'write_context_ref': 'synthetic-schema-context',
                        ('expected_planning_version' if name == TOOLS[2] else 'expected_learning_version'): 0}
                    model.model_validate({**args, **internal})
            for name, expected in (('review_learning_change', _LEARNING_CALM_CHECK_INPUT_SCHEMA),
                                   ('review_planning_change', _PLANNING_CALM_CHECK_INPUT_SCHEMA)):
                assert schemas[name]['properties']['calm_check'] == expected
                assert 'calm_check' in schemas[name]['required']
                assert not Draft202012Validator(expected).is_valid(None)
            remember = schemas['remember_planning_memory']
            assert 'ai_confirmation' in remember['required']
            assert remember['properties']['ai_confirmation']['const'] is True
            general = schemas['remember_memory']['properties']
            assert general['source_basis']['default'] == 'unmarked'
            assert general['confidence']['default'] is None
            assert 'observed=我亲历或观察' in general['source_basis']['description']
            assert 'reported=转述/引述' in general['source_basis']['description']
            assert 'inferred=推断' in general['source_basis']['description']
            assert 'unmarked=未标注' in general['source_basis']['description']
            assert '省略或 null 保存为未标注' in general['confidence']['description']
            assert '不代填评分' in general['confidence']['description']
            confidence_schema = Draft202012Validator(general['confidence'])
            for value in (None, 0, 50, 100):
                assert confidence_schema.is_valid(value)
            for value in (-1, 101, True, '50', 1.5):
                assert not confidence_schema.is_valid(value), 'invalid_confidence_advertised'
            source_schema = Draft202012Validator(general['source_basis'])
            assert source_schema.is_valid('unmarked')
            assert not source_schema.is_valid('guessed'), 'unknown_source_advertised'
            # The real registered argument model agrees with the directory;
            # validating omitted fields does not create a memory or fake audit.
            before_defaults = snapshot()
            model = server.mcp._tool_manager.get_tool('remember_memory').fn_metadata.arg_model
            defaults = model.model_validate({'module': 'learning_memory', 'content': 'Synthetic defaults only.'})
            assert defaults.source_basis == 'unmarked' and defaults.confidence is None
            explicit = model.model_validate({'module': 'learning_memory', 'content': 'Synthetic explicit score.',
                                             'source_basis': 'reported', 'confidence': 50})
            assert explicit.source_basis == 'reported' and explicit.confidence == 50
            assert snapshot() == before_defaults, 'schema_validation_wrote_business_data'

        elif mode == 'negative':
            async def reject(name, args, *, signed=False):
                before = snapshot()
                result = await (gateway(name, args) if signed else direct(name, args))
                assert result.get('decision') == 'reject' and result.get('state_changed') is False
                assert snapshot() == before, 'rejected_request_changed_business'
                return result

            for value in (False, 'true', 1):
                bad = {**args_by_name[TOOLS[2]], 'ai_confirmation': value}
                await reject(TOOLS[2], bad)
                before = snapshot()
                try:
                    host_bind(TOOLS[2], bad)
                except ToolExecutionBoundaryError:
                    pass
                else:
                    raise AssertionError('host_accepted_false_or_coerced_confirmation')
                assert snapshot() == before
            for field, value in (('classification_basis', 'not a list'), ('classification_basis', ['']),
                                 ('classification_basis', ['x' * 401]), ('classification_basis', ['x'] * 13),
                                 ('correctness_assessment', ''), ('correctness_assessment', 'x' * 2001),
                                 ('diff', 123), ('diff', 'x' * 2001), ('calm_check', 'not an object')):
                await reject(TOOLS[0], {**args_by_name[TOOLS[0]], field: value})
            await reject(TOOLS[0], {**args_by_name[TOOLS[0]], 'diff': 'token：SYNTHETIC_SECRET_749203'})
            # Required content, classification and idempotency are not relaxed.
            for name, field in ((TOOLS[0], 'change_class'), (TOOLS[0], 'action'),
                                (TOOLS[1], 'reason'), (TOOLS[2], 'idempotency_key')):
                bad = copy.deepcopy(args_by_name[name])
                bad.pop(field)
                await reject(name, bad)
            result = await gateway(TOOLS[0], args_by_name[TOOLS[0]], replay=True)
            assert result.get('decision') == 'applied'
            await reject(TOOLS[0], args_by_name[TOOLS[0]], signed=True)
            await reject(TOOLS[1], args_by_name[TOOLS[1]], signed=True)
            await reject(TOOLS[2], {**args_by_name[TOOLS[2]], 'expected_plan_version': 2}, signed=True)
            # Explicit real notes are retained verbatim and never turn into review proof.
            supplied = {**args_by_name[TOOLS[0]], 'target_ref': result['item_ref'], 'expected_target_version': 2,
                'current_understanding': 'Synthetic explicitly documented correction.',
                'classification_basis': ['Synthetic actual basis'], 'correctness_assessment': 'Synthetic author assessment',
                'diff': 'Synthetic author diff'}
            result = await gateway(TOOLS[0], supplied)
            assert result.get('decision') == 'applied'
            assert rows('SELECT ai_diff,correctness_assessment FROM learning_versions WHERE learning_id=? AND version=3',
                        (first['id'],)) == [('Synthetic author diff', 'Synthetic author assessment')]
            assert rows('SELECT COUNT(*) FROM learning_change_candidates') == [(0,)]
            assert rows('SELECT COUNT(*) FROM planning_change_candidates') == [(0,)]

        assert rows('SELECT * FROM self_model_revisions ORDER BY revision_id') == core
        print(json.dumps({'decision': 'PASS', 'mode': mode, 'network_attempts': len(attempts),
                          'host_bindings': host_calls, 'input_unchanged': True, 'real_brain_used': False}))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        asyncio.run(probe(sys.argv[2]))
    else:
        unittest.main()
