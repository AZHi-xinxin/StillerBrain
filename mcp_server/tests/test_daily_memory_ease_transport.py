"""Single-call memory ease, against real dispatch and isolated synthetic stores.

No real brain, credential, model, listener, phone, or deployment is used.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, ExitStack
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


async def probe():
    from mcp_server import server
    from runtime.execution_binding import ExecutionStore, canonical_hash
    from runtime.ordinary_access import tool_scope
    from tests.test_onboarding import ModuleOneOnboardingTests

    database = Path(os.environ['STBRAIN_DB_PATH'])
    common = {'owner_id': server.OWNER_ID, 'model_id': server.MODEL_ID}
    checks = []

    def check(condition, label):
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    def rows(sql, args=()):
        with closing(sqlite3.connect(database)) as conn:
            return conn.execute(sql, args).fetchall()

    def count():
        return tuple(rows('SELECT count(*) FROM ' + table)[0][0] for table in
                     ('emotion_memory_versions', 'learning_versions', 'planning_versions',
                      'work_memory_versions'))

    async def call(name, args):
        blocks, result = await server.mcp.call_tool(name, args)
        check(json.loads(blocks[0].text) == result, 'structured/text agree:' + name)
        return result

    check(server.SIMPLE_MEMORY_ACCESS, 'simple profile active')
    check(server.mcp._tool_manager.get_tool('remember_memory').parameters['required'] == ['content'],
          'one actual required public argument')
    before = count()
    for name in ('remember_memory', 'remember_work_memory'):
        rejected = await call(name, {'content': 'Synthetic not activated'})
        check(rejected.get('reason_code') == 'module_one_required', 'first activation required:' + name)
    check(count() == before, 'first activation gate writes nothing')
    fixture = ModuleOneOnboardingTests()
    fixture.database, fixture.store = database, server.onboarding
    fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
    fixture.bootstrap_live()
    initial_core = rows('SELECT * FROM self_model_revisions ORDER BY revision_id')
    initial_wakes = rows('SELECT count(*) FROM brain_wake_sessions')[0][0]

    inputs = [
        ({}, 'emotional_memory'),
        ({'module': None, 'kind': 'moment'}, 'emotional_memory'),
        ({'module': '', 'kind': 'MOMENT', 'emotion': None, 'importance': None,
          'keywords': ''}, 'emotional_memory'),
        ({'kind': 'unknown-custom-label', 'emotion': 'unknown-feeling', 'importance': '60',
          'keywords': '日常，合成'}, 'emotional_memory'),
        ({'module': 'learn', 'kind': 'fact', 'importance': 50.0,
          'keywords': '["合成", "事实"]'}, 'learning_memory'),
        ({'kind': 'fact', 'source_basis': 'unmarked'}, 'learning_memory'),
        ({'kind': 'task'}, 'planning_memory'),
        ({'module': 'plan', 'kind': 'moment'}, 'planning_memory'),
    ]
    first_ref = None
    for index, (metadata, module) in enumerate(inputs):
        content = f'  Synthetic single-call {index}\nExact whitespace stays.  '
        args = {'content': content, **metadata}
        original = json.dumps(args, sort_keys=True)
        check(tool_scope('remember_memory', args) == module, 'scope resolution:' + str(index))
        before = count()
        result = await call('remember_memory', args)
        check(result.get('decision') == 'stored' and result.get('stored') is True,
              'one invocation stored:' + str(index))
        check(result.get('module') == module and result.get('version') == 1 and result.get('ref'),
              'real resolved receipt:' + str(index))
        check(sum(count()) == sum(before) + 1, 'exactly one version:' + str(index))
        check(json.dumps(args, sort_keys=True) == original, 'caller input not mutated:' + str(index))
        table, key = {'emotional_memory': ('emotion_memory_versions', 'memory_id'),
                      'learning_memory': ('learning_versions', 'learning_id'),
                      'planning_memory': ('planning_versions', 'plan_id')}[module]
        record = rows(f'SELECT * FROM {table} WHERE {key}=?', (result['id'],))[0]
        # Schemas may store content inside JSON; decode rather than substring matching.
        def values(value):
            if isinstance(value, (list, tuple)):
                for item in value:
                    yield from values(item)
            elif isinstance(value, dict):
                for item in value.values():
                    yield from values(item)
            elif isinstance(value, str):
                yield value
                if value.startswith(('{', '[')):
                    try:
                        yield from values(json.loads(value))
                    except ValueError:
                        pass
        check(content in set(values(record)), 'original readback exact:' + str(index))
        if index == 0:
            first_ref = result['ref']

    compact = await call('stbrain_manage', {'action': 'remember_memory',
                                          'arguments': {'content': 'Synthetic compact', 'kind': 'moment'}})
    check(compact.get('decision') == 'stored', 'compact schema and guarded dispatch agree')
    scratch = await call('remember_work_memory', {'content': '  Synthetic scratch\nverbatim.  '})
    check(scratch.get('decision') == 'stored', 'scratch only content works')
    check(rows('SELECT content,tag FROM work_memory_versions ORDER BY created_at DESC LIMIT 1')[0]
          == ('  Synthetic scratch\nverbatim.  ', '随手记'), 'scratch exact body and default tag')
    check(rows('SELECT count(*) FROM brain_wake_sessions')[0][0] == initial_wakes,
          'ordinary use did not create new wakes')

    bad_inputs = [
        {'module': 'self_revision'}, {'module': 'hallucination_vault'}, {'module': 'misspelled'},
        {'owner_id': 'another-owner'}, {'importance': True}, {'importance': 101},
        {'importance': '1000'}, {'source_basis': 'made-up'}, {'confidence': True},
        {'kind': os.environ['STBRAIN_MCP_TOKEN']}, {'emotion': os.environ['STBRAIN_WAKE_SECRET']},
        {'keywords': os.environ['STBRAIN_MCP_TOKEN']}, {'title': os.environ['STBRAIN_MCP_TOKEN']},
        {'content': 'x' * 2001}, {'parent_ref': 'not-a-real-parent'}, {'due_at': 'not-a-time'},
    ]
    for index, metadata in enumerate(bad_inputs):
        before = count()
        result = await call('remember_memory', {'content': 'Synthetic rejected input', **metadata})
        check(result.get('decision') == 'reject' and result.get('state_changed') is False,
              'invalid or protected input rejected:' + str(index))
        check(count() == before, 'rejected input writes nothing:' + str(index))
        text = json.dumps(result)
        check(os.environ['STBRAIN_MCP_TOKEN'] not in text and os.environ['STBRAIN_WAKE_SECRET'] not in text,
              'safe error no credential echo:' + str(index))

    # The exact raw arguments are bound, before metadata compatibility is applied.
    all_tools = server.mcp._tool_manager.list_tools()
    entries = [{'canonical_name': tool.name, 'schema_hash': canonical_hash(tool.parameters)}
               for tool in all_tools]
    advertised = {'contract': 'advertised-tools/1', 'catalog_complete': True,
                  'catalog_hash': canonical_hash(entries), 'entries': entries}
    wake = server.onboarding.issue_wake(**common, host_id='ease-host', thread_id='ease-thread',
        source_kind='human_message', source_event_id='ease-event')
    prepared = server.onboarding.build_pre_generation_context(**common, wake_id=wake['wake_id'],
        wake_capability=wake['wake_capability'], source_digest='synthetic-source',
        host_contract_digest='synthetic-host', advertised_tools=advertised)
    check(prepared.get('decision') == 'context_prepared', 'real synthetic gateway context')
    server.onboarding.confirm_context_injected(**common, wake_id=wake['wake_id'],
        wake_capability=wake['wake_capability'], context_hash=prepared['context_hash'])
    registry = ExecutionStore(database, deployment_epoch=os.environ['STBRAIN_EXECUTION_EPOCH'],
                              capability_secret=server.WAKE_SECRET)
    raw = {'content': 'Synthetic gateway original', 'kind': 'moment', 'importance': '50'}
    issued = registry.issue_batch(**common, wake_id=wake['wake_id'], wake_capability=wake['wake_capability'],
        batch_id='ease-batch', revision=1, calls=[{'call_id': 'ease-call', 'advertised_name': 'remember_memory',
            'canonical_tool': 'remember_memory',
            'schema_hash': canonical_hash(server.mcp._tool_manager.get_tool('remember_memory').parameters),
            'catalog_hash': advertised['catalog_hash'], 'arguments_hash': canonical_hash(raw)}])
    ref = issued['executions'][0]['execution_ref']
    before = count()
    tampered = await call('remember_memory', {**raw, 'kind': 'shared_event', 'execution_ref': ref})
    check(tampered.get('decision') == 'reject' and count() == before,
          'equivalent normalized payload cannot replace signed raw arguments')
    bound = await call('remember_memory', {**raw, 'execution_ref': ref})
    check(bound.get('decision') == 'stored', 'original signed alias payload works')
    replay = await call('remember_memory', {**raw, 'execution_ref': ref})
    check(replay.get('decision') == 'reject', 'gateway replay rejected')
    check(initial_core == rows('SELECT * FROM self_model_revisions ORDER BY revision_id'),
          'all convenience changes leave self model unchanged')
    check(first_ref is not None, 'ordinary receipt exists independently from scratch')
    return {'decision': 'PASS', 'check_count': len(checks), 'checks': checks}


async def isolated_probe():
    attempts = []
    def deny(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('network_forbidden')
    with ExitStack() as stack:
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.socket.sendto',
                       'socket.create_connection', 'socket.getaddrinfo'):
            stack.enter_context(patch(target, side_effect=deny))
        result = await probe()
    if attempts:
        raise AssertionError('network attempted')
    return result


class DailyMemoryEaseTransportTests(unittest.TestCase):
    def test_single_argument_and_compatibility_through_real_dispatch(self):
        allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'USERPROFILE',
                   'LOCALAPPDATA', 'APPDATA', 'PATHEXT', 'SYSTEMDRIVE', 'NUMBER_OF_PROCESSORS'}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix='st-ease-synthetic-') as scratch:
            env.update({'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1',
                'STBRAIN_MCP_TOKEN': 'synthetic-token-ease-0000000000000000',
                'STBRAIN_WAKE_SECRET': 'synthetic-wake-ease-000000000000000',
                'STBRAIN_OWNER_ID': 'ease-owner', 'STBRAIN_MODEL_ID': 'ease-model',
                'STBRAIN_DB_PATH': str(Path(scratch) / 'main.db'),
                'STBRAIN_LEARNING_IDEA_DB_PATH': str(Path(scratch) / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(Path(scratch) / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1', 'STBRAIN_EXECUTION_EPOCH': 'ease-epoch',
                'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'})
            completed = subprocess.run([sys.executable, '-B', '-m', 'mcp_server.tests.test_daily_memory_ease_transport', '--probe'],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                text=True, encoding='utf-8', timeout=90)
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual('PASS', proof['decision'])
        print(json.dumps(proof, ensure_ascii=False))


if __name__ == '__main__':
    if sys.argv[1:] == ['--probe']:
        print(json.dumps(asyncio.run(isolated_probe()), ensure_ascii=False))
    else:
        unittest.main()
