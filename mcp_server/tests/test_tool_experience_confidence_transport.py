"""Experience 0..100 through registered direct and signed gateway dispatch.

All databases and identities are synthetic. The gateway uses its actual native
boundary/reference insertion and the real host ExecutionStore; only HTTP transport
is replaced with an in-process adapter. No upstream request or live service runs.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, ExitStack
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit


class ToolExperienceConfidenceTransportTests(unittest.TestCase):
    def run_mode(self, mode):
        allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE', 'PATH',
                   'PATHEXT', 'TEMP', 'TMP', 'OS'}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix='tool-experience-transport-synthetic-') as directory:
            root = Path(directory).resolve()
            env.update({
                'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1',
                'STBRAIN_MCP_TOKEN': 'synthetic-experience-mcp-' + 'm' * 40,
                'STBRAIN_WAKE_SECRET': 'synthetic-experience-wake-' + 'w' * 40,
                'STBRAIN_OWNER_ID': 'synthetic-experience-owner',
                'STBRAIN_MODEL_ID': 'synthetic-experience-model',
                'STBRAIN_DB_PATH': str(root / 'main.db'),
                'STBRAIN_LEARNING_IDEA_DB_PATH': str(root / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(root / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1',
                'STBRAIN_EXECUTION_EPOCH': 'synthetic-experience-epoch',
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                'PYTHONIOENCODING': 'utf-8',
            })
            completed = subprocess.run([sys.executable, '-B', '-m',
                'mcp_server.tests.test_tool_experience_confidence_transport', '--probe', mode],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                text=True, encoding='utf-8', timeout=90)
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual('PASS', result['decision'])
        self.assertEqual([0, 81, 100], result['stored_confidences'])
        self.assertEqual(0, result['network_attempts'])
        self.assertFalse(result['real_brain_used'])
        self.assertFalse(result['verified'])

    def test_registered_direct_experience_range_and_readback(self):
        self.run_mode('direct')

    def test_actual_gateway_reference_registered_experience_range_and_readback(self):
        self.run_mode('gateway')


async def probe(mode):
    assert mode in {'direct', 'gateway'}
    root = Path(os.environ['STBRAIN_DB_PATH']).resolve().parent
    databases = {root / name for name in ('main.db', 'ideas.db', 'vault.db')}
    real_connect = sqlite3.connect
    network_attempts = []

    def only_synthetic(path, *args, **kwargs):
        text = str(path)
        if text.startswith('file:'):
            text = unquote(urlsplit(text).path)
            if os.name == 'nt' and re.match(r'^/[A-Za-z]:/', text):
                text = text[1:]
        assert Path(text).resolve() in databases, 'outside_synthetic_database'
        return real_connect(path, *args, **kwargs)

    def deny(*args, **kwargs):
        network_attempts.append(True)
        raise AssertionError('network_or_service_start_forbidden')

    with ExitStack() as guard:
        for name in ('socket.create_connection', 'socket.socket.connect',
                     'socket.socket.connect_ex', 'socket.socket.sendto',
                     'socket.socket.bind', 'socket.getaddrinfo', 'subprocess.Popen'):
            guard.enter_context(patch(name, side_effect=deny))
        guard.enter_context(patch('sqlite3.connect', side_effect=only_synthetic))
        from mcp_server import server
        from runtime.execution_binding import canonical_hash, current_execution_claim
        from runtime.ordinary_access import current_ordinary_access
        from rikkahub_gateway.server import GatewayApplication, GatewayConfig, PreparedTurn, TurnSession
        from rikkahub_gateway.tool_execution import NativeToolCall, ToolExecutionBoundaryError
        from tests.test_onboarding import ModuleOneOnboardingTests

        def snapshot():
            result = {}
            for path in sorted(databases):
                if path.exists():
                    with closing(sqlite3.connect(path)) as connection:
                        lines = list(connection.iterdump())
                    # Claim/finish records are the intended execution transport
                    # side effect. Everything else must be unchanged on reject.
                    lines = [line for line in lines if not line.startswith((
                        'INSERT INTO "brain_execution_calls"',
                        'INSERT INTO "brain_execution_batches"'))]
                    result[path.name] = hashlib.sha256('\n'.join(lines).encode()).hexdigest()
            return result

        async def registered(name, arguments):
            original = copy.deepcopy(arguments)
            blocks, result = await server.mcp.call_tool(name, arguments)
            assert arguments == original
            assert json.loads(blocks[0].text) == result
            assert current_execution_claim() is None
            assert current_ordinary_access(owner_id=server.OWNER_ID, model_id=server.MODEL_ID) is None
            return result

        fixture = ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / 'main.db', server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()
        saved = await registered('remember_tool_guidance', {
            'tool_name': 'SyntheticExperienceTool', 'purpose': 'Synthetic experience range check.'})
        assert saved['decision'] == 'stored', saved
        card_id = saved['card']['card_id']

        schemas = {tool.name: copy.deepcopy(tool.parameters)
                   for tool in server.mcp._tool_manager.list_tools()}
        confidence_schema = schemas['record_tool_experience']['properties']['confidence']
        assert confidence_schema['type'] == 'integer'
        assert confidence_schema['minimum'] == 0 and confidence_schema['maximum'] == 100
        assert 'write_context_ref' not in schemas['record_tool_experience'].get('required', [])
        entries = [{'canonical_name': name, 'schema_hash': canonical_hash(schema)}
                   for name, schema in schemas.items()]
        catalog = {'contract': 'advertised-tools/1', 'catalog_complete': True,
                   'catalog_hash': canonical_hash(entries), 'entries': entries}
        common = {'owner_id': server.OWNER_ID, 'model_id': server.MODEL_ID}
        counter = 0

        class SyntheticControlTransport:
            def post(self, path, payload):
                assert path == '/v1/host/tool-executions/issue'
                assert payload['deployment_epoch'] == os.environ['STBRAIN_EXECUTION_EPOCH']
                args = {key: value for key, value in payload.items() if key != 'deployment_epoch'}
                return server._execution_store.issue_batch(**common, **args)

        app = GatewayApplication(GatewayConfig(
            gateway_token='synthetic-gateway-' + 'g' * 40,
            host_token='synthetic-host-' + 'h' * 40,
            control_url='http://synthetic.invalid', upstream_base_url='http://synthetic.invalid',
            upstream_api_key='synthetic-upstream-' + 'u' * 40,
            public_model='synthetic-model', upstream_model='synthetic-upstream-model',
            require_execution_binding=True, execution_epoch=os.environ['STBRAIN_EXECUTION_EPOCH']),
            control=SyntheticControlTransport(), upstream=object())
        prepared = None
        if mode == 'gateway':
            wake = server.onboarding.issue_wake(**common, host_id='synthetic-experience-host',
                thread_id='synthetic-experience-thread', source_kind='human_message',
                source_event_id='synthetic-experience-event')
            context = server.onboarding.build_pre_generation_context(**common,
                wake_id=wake['wake_id'], wake_capability=wake['wake_capability'],
                source_digest='synthetic-source', host_contract_digest='synthetic-host',
                advertised_tools=catalog, source_frame={'query_text': 'Synthetic range check',
                    'lineage_stable': False, 'prior_assistant_present': False, 'capture_items': []})
            server.onboarding.confirm_context_injected(**common, wake_id=wake['wake_id'],
                wake_capability=wake['wake_capability'], context_hash=context['context_hash'])
            session = TurnSession(thread_id='synthetic-experience-thread',
                wake_id=wake['wake_id'], wake_capability=wake['wake_capability'],
                message={'role': 'system', 'content': 'Synthetic context'},
                context_hash=context['context_hash'], source_digest='synthetic-source',
                advertised_tools=catalog, created_at=time.monotonic(),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
            prepared = PreparedTurn(session.thread_id, {'tools': [
                {'type': 'function', 'function': {'name': name, 'parameters': schema}}
                for name, schema in schemas.items()]}, session, False)
            app._current_session = session

        async def call(arguments, *, invalid=False):
            nonlocal counter
            counter += 1
            if mode == 'direct':
                return await registered('record_tool_experience', arguments)
            raw = NativeToolCall('synthetic-experience-call-' + str(counter),
                                 'record_tool_experience', json.dumps(arguments))
            if invalid:
                # The real native boundary rejects 101 from its advertised schema.
                try:
                    app.execution_boundary.bind_call(wake_id=prepared.session.wake_id,
                        catalog=catalog, schemas=schemas, call=raw)
                except ToolExecutionBoundaryError as exc:
                    assert str(exc) == 'tool_arguments_schema_invalid'
                else:
                    raise AssertionError('gateway_schema_accepted_101')
            decorated = app.decorate_execution_calls(prepared, [raw])[0]
            if not invalid:
                bindings = app.bind_response_tool_calls(prepared, [decorated])
                assert bindings[raw.tool_call_id].tool_name == raw.tool_name
            values = json.loads(decorated.arguments_text)
            ref = values.pop('execution_ref')
            assert values == arguments and ref.startswith('stexec_')
            # Even a legitimately signed but invalid request is rejected at MCP.
            return await registered(raw.tool_name, {**values, 'execution_ref': ref})

        experience_ids = []
        for confidence in (0, 81, 100):
            result = await call({'card_id': card_id, 'outcome': 'invalid_arguments',
                'reason_code': 'synthetic_shape_mismatch',
                'attempt_summary': 'Synthetic reported attempt ' + str(confidence),
                'confidence': confidence})
            assert result['decision'] == 'recorded' and result['state_changed'], result
            assert result['execution_performed'] is False
            experience = result['experience']
            assert experience['confidence'] == confidence
            assert experience['provenance'] == 'ai_reported'
            assert experience['verified'] is False and experience['evidence_ref'] is None
            experience_ids.append(experience['experience_id'])

        before = snapshot()
        rejected = await call({'card_id': card_id, 'outcome': 'invalid_arguments',
            'reason_code': 'synthetic_shape_mismatch', 'attempt_summary': 'Synthetic rejected attempt',
            'confidence': 101}, invalid=True)
        assert rejected['reason_code'] == 'invalid_tool_arguments', rejected
        assert {'field': 'confidence', 'issue': 'less_than_equal'} in rejected['field_errors']
        assert rejected['state_changed'] is False and snapshot() == before

        before = snapshot()
        readback = await registered('recall_tool_guidance', {'card_id': card_id, 'view': 'failures'})
        assert readback['decision'] == 'precise_result' and snapshot() == before
        experiences = readback['experiences']
        assert {item['experience_id'] for item in experiences} == set(experience_ids)
        assert all(item['provenance'] == 'ai_reported' and item['verified'] is False for item in experiences)
        assert readback['execution_performed'] is False
        # Existing public failure summaries omit confidence. Read actual persisted
        # values from the allowlisted synthetic database, without changing schema.
        with closing(sqlite3.connect(root / 'main.db')) as connection:
            rows = connection.execute('SELECT experience_id,confidence,provenance,evidence_ref '
                'FROM tool_experiences WHERE card_id=? ORDER BY confidence', (card_id,)).fetchall()
        assert [row[1] for row in rows] == [0, 81, 100]
        assert {row[0] for row in rows} == set(experience_ids)
        assert all(row[2:] == ('ai_reported', None) for row in rows)
        assert not network_attempts
        return {'decision': 'PASS', 'mode': mode, 'stored_confidences': [row[1] for row in rows],
                'verified': False, 'network_attempts': len(network_attempts), 'real_brain_used': False}


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        print(json.dumps(asyncio.run(probe(sys.argv[2]))))
    else:
        unittest.main()
