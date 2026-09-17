"""Unified read through actual registration and host binding; synthetic DBs only."""
from __future__ import annotations

import asyncio
from contextlib import closing, ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit


MODES = ('directory', 'search', 'cursor', 'privacy', 'binding', 'multiple_databases')


class UnifiedRecallTransportTests(unittest.TestCase):
    def probe(self, mode):
        allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE', 'PATH',
                   'PATHEXT', 'TEMP', 'TMP', 'OS'}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix='unified-recall-synthetic-') as directory:
            root = Path(directory).resolve()
            env.update({'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1',
                'STBRAIN_MCP_TOKEN': 'synthetic-unified-mcp-' + 'm' * 40,
                'STBRAIN_WAKE_SECRET': 'synthetic-unified-wake-' + 'w' * 40,
                'STBRAIN_OWNER_ID': 'synthetic-unified-owner',
                'STBRAIN_MODEL_ID': 'synthetic-unified-model',
                'STBRAIN_DB_PATH': str(root / 'main.db'),
                'STBRAIN_LEARNING_IDEA_DB_PATH': str(root / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(root / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1',
                'STBRAIN_EXECUTION_EPOCH': 'synthetic-unified-epoch',
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONIOENCODING': 'utf-8'})
            result = subprocess.run([sys.executable, '-B', '-m',
                'mcp_server.tests.test_unified_recall_transport', '--probe', mode],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                text=True, encoding='utf-8', timeout=100)
        self.assertEqual(0, result.returncode, result.stderr)
        proof = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual('PASS', proof['decision'])
        self.assertEqual(0, proof['network_attempts'])
        self.assertFalse(proof['real_brain_used'])

    def test_complete_four_module_directory_beyond_old_reader_caps(self):
        self.probe('directory')

    def test_search_grouping_summary_and_exact_detail_lookup(self):
        self.probe('search')

    def test_cursor_changes_identity_query_tampering_and_old_age(self):
        self.probe('cursor')

    def test_sensitive_isolated_foreign_and_legacy_credentials_are_not_disclosed(self):
        self.probe('privacy')

    def test_actual_host_lease_registered_read_and_unchanged_write_gates(self):
        self.probe('binding')

    def test_independent_database_snapshots_and_honest_partial_errors(self):
        self.probe('multiple_databases')


async def probe(mode):
    assert mode in MODES
    root = Path(os.environ['STBRAIN_DB_PATH']).resolve().parent
    databases = {root / name for name in ('main.db', 'ideas.db', 'vault.db', 'separate.db')}
    real_connect = sqlite3.connect
    attempts = []

    def only_synthetic(path, *args, **kwargs):
        text = str(path)
        if text.startswith('file:'):
            text = unquote(urlsplit(text).path)
            if os.name == 'nt' and re.match(r'^/[A-Za-z]:/', text):
                text = text[1:]
        assert Path(text).resolve() in databases, 'outside_synthetic_database'
        return real_connect(path, *args, **kwargs)

    def deny(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('network_or_service_start_forbidden')

    with ExitStack() as guard:
        for name in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'socket.socket.sendto', 'socket.socket.bind', 'socket.getaddrinfo', 'subprocess.Popen'):
            guard.enter_context(patch(name, side_effect=deny))
        guard.enter_context(patch('sqlite3.connect', side_effect=only_synthetic))
        from mcp_server import server
        from mcp_server.unified_recall_service import MODULES, UnifiedRecallAccessService, _decode
        from runtime.execution_binding import ExecutionStore, canonical_hash
        from rikkahub_gateway.tool_execution import HostExecutionBoundary, NativeToolCall
        from tests.test_onboarding import ModuleOneOnboardingTests

        def snapshot(*, business=False):
            result = {}
            for path in sorted(databases):
                if not path.exists():
                    continue
                with closing(sqlite3.connect(path)) as connection:
                    dump = list(connection.iterdump())
                if business:
                    dump = [line for line in dump if not line.startswith(
                        ('INSERT INTO "brain_execution_calls"', 'INSERT INTO "brain_execution_batches"'))]
                result[path.name] = hashlib.sha256('\n'.join(dump).encode()).hexdigest()
            return result

        def sql(query, args=()):
            with closing(sqlite3.connect(root / 'main.db')) as connection:
                result = connection.execute(query, args).fetchall()
                connection.commit()
                return result

        calls = 0
        async def call(name, args):
            nonlocal calls
            original = copy.deepcopy(args)
            blocks, result = await server.mcp.call_tool(name, args)
            assert args == original and json.loads(blocks[0].text) == result
            calls += 1
            return result

        async def recall(**args):
            before = snapshot()
            result = await call('stbrain_open', {'view': 'recall', **args})
            assert snapshot() == before, 'unified_read_changed_database'
            assert result['state_changed'] is False
            return result

        assert len(server.mcp._tool_manager.list_tools()) == 46  # Includes compact facades.
        empty = await recall()
        assert empty['items'] == [] and empty['exhaustive'] and not empty['partial'], empty
        # Reading before first activation does not grant ordinary writes or open core review.
        before = snapshot()
        rejected = await call('remember_memory', {'module': 'learning_memory', 'content': 'Synthetic blocked write.'})
        assert rejected['reason_code'] == 'module_one_required' and snapshot() == before
        for view in ('summary', 'manual', 'review'):
            denied = await call('stbrain_open', {'view': view})
            assert denied['reason_code'] == 'execution_binding_required'

        fixture = ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / 'main.db', server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()
        saved = {name: [] for name in MODULES}
        for name in MODULES:
            count = (53 if name == 'learning_memory' else 7 if name == 'tool_guidance' else 2) if mode == 'directory' else (53 if mode == 'multiple_databases' and name == 'learning_memory' else 2)
            for index in range(count):
                summary = f'Synthetic orchard {name} note {index}.'
                if name == 'tool_guidance':
                    result = await call('remember_tool_guidance', {
                        'tool_name': 'SyntheticTool' + str(index), 'purpose': summary,
                        'reminder': 'Synthetic orchard reminder ' + str(index), 'keywords': ['orchard']})
                    assert result.get('decision') == 'stored', result
                    saved[name].append(result['card'])
                else:
                    result = await call('remember_memory', {'module': name,
                        'content': 'BODY_ONLY_PRIVATE_' + name + '_' + str(index),
                        'summary': summary, 'title': 'Synthetic orchard ' + str(index), 'keywords': ['orchard']})
                    assert result.get('decision') == 'stored', result
                    saved[name].append(result)

        if mode in {'directory', 'search'}:
            if mode == 'directory':
                # Existing readable pending cards are not pending change proposals.
                pending = saved['learning_memory'][-1]['id']
                sql("UPDATE learning_items SET lifecycle='pending_review' WHERE learning_id=?", (pending,))
            args = {'query': 'orchard'} if mode == 'search' else {}
            found, cursor = [], None
            while True:
                page = await recall(**args, limit=5, **({'cursor': cursor} if cursor else {}))
                assert not page['partial'] and page['snapshot_scope'] == 'shared_database', page
                assert 'BODY_ONLY_PRIVATE_' not in json.dumps(page)
                found.extend(page['items'])
                cursor = page['next_cursor']
                if not cursor:
                    break
            assert len(found) == sum(map(len, saved.values()))
            assert len({item['ref'] for item in found}) == len(found)
            assert [item['module'] for item in found] == sorted(
                [item['module'] for item in found], key=MODULES.index)
            for module in MODULES:
                one = await recall(module=module, limit=50, **args)
                assert all(item['module'] == module for item in one['items'])
            if mode == 'directory':
                entry = next(item for item in found if pending in item['ref'])
                assert entry['lifecycle'] == 'pending_review' and entry['detail_lookup']['arguments']['include_pending'] is True
                lookup = entry['detail_lookup']
                before = snapshot()
                detail = await call(lookup['tool'], lookup['arguments'])
                assert detail['result_count'] == 1 and detail['results'][0]['lifecycle'] == 'pending_review', detail
                assert snapshot() == before, 'pending_card_read_changed_state'
            if mode == 'search':
                for item in found:
                    lookup = item['detail_lookup']
                    detail = await call(lookup['tool'], lookup['arguments'])
                    assert detail.get('decision') not in {'reject', 'no_candidate'}, detail
                absent = await recall(query='玄黓魑魅')
                assert absent['items'] == [] and absent['exhaustive']

        elif mode == 'cursor':
            first = await recall(limit=1)
            cursor = first['next_cursor']
            assert cursor and set(_decode(cursor)) == {'v', 'binding', 'snapshot', 'offset'}
            assert (await recall(cursor=cursor, limit=3))['offset'] == 1
            for args in ({'query': 'orchard'}, {'module': 'learning_memory'}):
                assert (await recall(cursor=cursor, **args))['reason_code'] == 'cursor_query_or_identity_mismatch'
            assert (await recall(cursor=cursor[:-2] + '!!'))['reason_code'] == 'cursor_invalid'
            # No clock/age field: timestamp advancement does not expire a directory cursor.
            with patch('time.time', return_value=99999999999):
                assert (await recall(cursor=cursor))['offset'] == 1
            own = server.service
            foreign_services = {}
            for key, component in {'emotional_memory': own.emotional, 'learning_memory': own.learning,
                                   'planning_memory': own.planning, 'tool_guidance': own.tool_guidance}.items():
                copied = copy.copy(component)
                copied.owner_id = 'synthetic-other-owner'
                foreign_services[key] = copied
            foreign = UnifiedRecallAccessService(services=foreign_services, owner_id='synthetic-other-owner',
                model_id=server.MODEL_ID, onboarding=server.onboarding, simple=True)
            assert foreign.recall(cursor=cursor)['reason_code'] == 'cursor_query_or_identity_mismatch'
            target = saved['learning_memory'][0]
            changed = await call('revise_memory', {'target_ref': target['ref'], 'changes': {'summary': 'Synthetic new summary.'}})
            assert changed['decision'] == 'revised'
            assert (await recall(cursor=cursor))['reason_code'] == 'cursor_stale'
            assert (await recall())['matched_count'] == 8

        elif mode == 'privacy':
            emotion = saved['emotional_memory'][0]['id']
            learned = saved['learning_memory'][0]['id']
            restricted = await call('revise_memory', {'target_ref': saved['emotional_memory'][0]['ref'],
                'changes': {'sensitivity': 'restricted', 'original_text': 'SENSITIVE_CURRENT_BODY_943',
                            'summary': 'RESTRICTED_PRIVATE_SUMMARY'}})
            assert restricted['decision'] == 'revised'
            sql("UPDATE learning_items SET current_json=json_set(current_json,'$.sensitivity','restricted','$.summary','RESTRICTED_PRIVATE_SUMMARY') WHERE learning_id=?", (learned,))
            quarantined = saved['learning_memory'][1]['id']
            sql("UPDATE learning_items SET lifecycle='quarantined' WHERE learning_id=?", (quarantined,))
            safe = await recall()
            text = json.dumps(safe)
            assert 'RESTRICTED_PRIVATE_SUMMARY' not in text and quarantined not in text
            assert len([item for item in safe['items'] if item['content_stub']]) == 2
            sensitive_entry = next(item for item in safe['items'] if emotion in item['ref'])
            detail = await call(sensitive_entry['detail_lookup']['tool'], sensitive_entry['detail_lookup']['arguments'])
            assert 'SENSITIVE_CURRENT_BODY_943' not in json.dumps(detail), 'detail_bypassed_sensitive_original_gate'
            # The existing learning reader exposes authored content; its separate
            # evidence confirmations are not the emotion original-text gate.
            learning_entry = next(item for item in safe['items'] if learned in item['ref'])
            learning_detail = await call(learning_entry['detail_lookup']['tool'], learning_entry['detail_lookup']['arguments'])
            assert learning_detail['result_count'] == 1 and 'RESTRICTED_PRIVATE_SUMMARY' in json.dumps(learning_detail)
            server.emotional_store.capture_ephemeral(owner_id=server.OWNER_ID, model_id=server.MODEL_ID,
                thread_id='synthetic-retention-thread', source_event_id='synthetic-retention-event',
                items=[{'role': 'user', 'content': 'EPHEMERAL_PRIVATE_ORCHARD_743'}])
            sql("UPDATE emotion_ephemeral SET expires_at='2000-01-01T00:00:00Z' WHERE thread_id='synthetic-retention-thread'")
            for arguments in ({}, {'query': 'EPHEMERAL_PRIVATE_ORCHARD_743'}):
                response = await recall(**arguments)
                assert 'EPHEMERAL_PRIVATE_ORCHARD_743' not in json.dumps(response)
            assert sql("SELECT status,content FROM emotion_ephemeral WHERE thread_id='synthetic-retention-thread'") == [('active', 'EPHEMERAL_PRIVATE_ORCHARD_743')]
            tool = saved['tool_guidance'][0]
            expired = await call('revise_tool_guidance', {'card_id': tool['card_id'], 'expected_card_version': 1,
                                                        'expires_at': '2000-01-01T00:00:00Z'})
            assert expired['card']['effective_status'] == 'expired'
            expired_entry = next(item for item in (await recall(module='tool_guidance'))['items'] if tool['card_id'] in item['ref'])
            expired_detail = await call(expired_entry['detail_lookup']['tool'], expired_entry['detail_lookup']['arguments'])
            assert expired_detail['results'][0]['effective_status'] == 'expired'
            # A foreign owner is excluded even when content matches the query.
            foreign = saved['planning_memory'][1]['id']
            sql('UPDATE planning_items SET owner_id=? WHERE plan_id=?', ('synthetic-other-owner', foreign))
            assert foreign not in json.dumps(await recall(query='orchard'))
            old = saved['emotional_memory'][1]['id']
            sql("UPDATE emotion_memories SET summary='token: SYNTHETIC_PRIVATE_SECRET_949' WHERE memory_id=?", (old,))
            guarded = await recall()
            assert guarded['partial'] and guarded['next_cursor'] is None
            assert 'SYNTHETIC_PRIVATE_SECRET' not in json.dumps(guarded)
            assert (await recall(query='密码：SYNTHETIC_PRIVATE_SECRET_482'))['reason_code'] == 'credential_or_secret_detected'
            for module in ('self_revision', 'hallucination_vault'):
                assert (await recall(module=module))['decision'] == 'reject'

        elif mode == 'multiple_databases':
            with closing(sqlite3.connect(root / 'main.db')) as source, closing(sqlite3.connect(root / 'separate.db')) as dest:
                source.backup(dest)
            component = copy.copy(server.service.learning)
            component.store = copy.copy(component.store)
            component.store.database = str(root / 'separate.db')
            server.service.learning = component
            page = await recall(limit=1)
            assert page['snapshot_scope'] == 'per_database' and not page['partial'], page
            assert (await recall(cursor=page['next_cursor']))['offset'] == 1
            tool_component = server.service.tool_guidance
            server.service.tool_guidance = None
            partial = await recall(limit=1)
            assert partial['partial'] and partial['next_cursor']
            assert partial['errors'] == [{'module': 'tool_guidance', 'reason_code': 'module_unavailable'}]
            assert (await recall(cursor=page['next_cursor']))['reason_code'] == 'cursor_stale'
            found, next_cursor = list(partial['items']), partial['next_cursor']
            while next_cursor:
                continuation = await recall(limit=17, cursor=next_cursor)
                assert continuation['partial'] and continuation['errors'] == partial['errors']
                found.extend(continuation['items'])
                next_cursor = continuation['next_cursor']
            assert len(found) == 57 and len({item['ref'] for item in found}) == 57
            server.service.tool_guidance = tool_component
            assert (await recall(cursor=partial['next_cursor']))['reason_code'] == 'cursor_stale'

        elif mode == 'binding':
            for ref in (None, '', 'stexec_' + 'a' * 43):
                result = await recall(execution_ref=ref)
                assert result['reason_code'] == 'execution_binding_invalid_or_finished'
            for bad in ({'query': {'secret': 'SYNTHETIC_PRIVATE_VALUE'}}, {'limit': True}, {'owner_id': 'SYNTHETIC_PRIVATE_OWNER'}):
                result = await recall(**bad)
                assert result['reason_code'] == 'invalid_recall_arguments'
                assert 'SYNTHETIC_PRIVATE_' not in json.dumps(result)
            for args in ({'page': 1}, {'expected_material_hash': 'a' * 64}):
                assert (await recall(**args))['reason_code'] == 'recall_uses_cursor_not_review_page'
            tools = server.mcp._tool_manager.list_tools()
            schemas = {tool.name: copy.deepcopy(tool.parameters) for tool in tools}
            entries = [{'canonical_name': name, 'schema_hash': canonical_hash(schema)} for name, schema in schemas.items()]
            catalog = {'contract': 'advertised-tools/1', 'catalog_complete': True,
                       'catalog_hash': canonical_hash(entries), 'entries': entries}
            common = {'owner_id': server.OWNER_ID, 'model_id': server.MODEL_ID}
            wake = server.onboarding.issue_wake(**common, host_id='synthetic-unified-host', thread_id='synthetic-unified-thread',
                source_kind='human_message', source_event_id='synthetic-unified-event')
            prepared = server.onboarding.build_pre_generation_context(**common, wake_id=wake['wake_id'],
                wake_capability=wake['wake_capability'], source_digest='synthetic-source', host_contract_digest='synthetic-host',
                advertised_tools=catalog, source_frame={'query_text': 'Synthetic query', 'lineage_stable': False,
                    'prior_assistant_present': False, 'capture_items': []})
            server.onboarding.confirm_context_injected(**common, wake_id=wake['wake_id'],
                wake_capability=wake['wake_capability'], context_hash=prepared['context_hash'])
            registry = server._execution_store
            arguments = {'view': 'recall', 'query': 'orchard', 'limit': 2}
            bound = HostExecutionBoundary(b'synthetic-unified-host-' + b'x' * 40).bind_call(
                wake_id=wake['wake_id'], catalog=catalog, schemas=schemas,
                call=NativeToolCall('synthetic-unified-call', 'stbrain_open', json.dumps(arguments)))
            issued = registry.issue_batch(**common, wake_id=wake['wake_id'], wake_capability=wake['wake_capability'],
                batch_id='synthetic-unified-batch', revision=1, calls=[{'call_id': bound.tool_call_id,
                    'advertised_name': 'stbrain_open', 'canonical_tool': 'stbrain_open',
                    'schema_hash': bound.schema_hash, 'catalog_hash': bound.catalog_hash, 'arguments_hash': bound.arguments_hash}])
            ref = issued['executions'][0]['execution_ref']
            before = snapshot(business=True)
            result = await call('stbrain_open', {**arguments, 'execution_ref': ref})
            assert result['decision'] == 'recalled' and result['returned_count'] == 2, result
            assert snapshot(business=True) == before
            assert (await call('stbrain_open', {**arguments, 'execution_ref': ref}))['reason_code'] == 'execution_binding_invalid_or_finished'
        assert not attempts
        return {'decision': 'PASS', 'mode': mode, 'registered_calls': calls,
                'network_attempts': len(attempts), 'real_brain_used': False}


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        print(json.dumps(asyncio.run(probe(sys.argv[2])), ensure_ascii=False))
    else:
        unittest.main()
