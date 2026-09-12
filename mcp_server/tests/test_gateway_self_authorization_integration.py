"""Registered gateway/direct self-authorization, synthetic and offline only.

The real server registration, execution ledger/HMAC, password authority, public
facade and runtime transitions run together. Only sockets and access outside
three temporary databases are blocked. No HTTP server/model is started. Test
host acknowledgements are synthetic; they make no claim about a real model.
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


PASSWORD = 'synthetic-self-authorization-passphrase'
SUBMIT = 'submit_self_model_candidate'
ACTIVATE = 'activate_self_model_candidate'


def require(condition, code):
    if not condition:
        raise AssertionError(code)


class GatewaySelfAuthorizationIntegrationTests(unittest.TestCase):
    def probe(self, mode):
        allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP',
                   'SYSTEMDRIVE', 'PATHEXT', 'USERPROFILE', 'LOCALAPPDATA'}
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix='gateway-self-synthetic-') as scratch:
            root = Path(scratch).resolve()
            env.update({
                'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1',
                'STBRAIN_MCP_TOKEN': 'synthetic-self-auth-' + 'm' * 40,
                'STBRAIN_WAKE_SECRET': 'synthetic-self-wake-' + 'w' * 40,
                'STBRAIN_OWNER_ID': 'synthetic-self-owner',
                'STBRAIN_MODEL_ID': 'synthetic-self-model',
                'STBRAIN_DB_PATH': str(root / 'main.db'),
                'STBRAIN_LEARNING_IDEA_DB_PATH': str(root / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(root / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1',
                'STBRAIN_EXECUTION_EPOCH': 'synthetic-self-epoch',
                'STBRAIN_SELF_PASSWORD_HASH_FILE': str(root / 'password.json'),
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                'PYTHONIOENCODING': 'utf-8',
            })
            result = subprocess.run([sys.executable, '-B', '-m',
                'mcp_server.tests.test_gateway_self_authorization_integration', '--probe', mode],
                cwd=Path(__file__).resolve().parents[2], env=env,
                capture_output=True, text=True, encoding='utf-8', timeout=90)
        self.assertEqual(0, result.returncode, result.stderr)
        proof = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual('PASS', proof['decision'])
        self.assertEqual(mode, proof['mode'])
        self.assertEqual(0, proof['network_attempts'])
        self.assertFalse(proof['real_model_requested'])
        return proof

    def test_registered_gateway_acknowledgements_and_candidate_submit_need_no_password(self):
        self.probe('gateway-submit')

    def test_gateway_keeps_independent_wakes_full_review_and_explicit_activation(self):
        self.probe('gateway-full-chain')

    def test_direct_self_write_requires_password_even_with_a_valid_nonpassword_grant(self):
        self.probe('direct-no-password')

    def test_authorized_direct_grant_preserves_actual_self_write_path(self):
        self.probe('direct-authorized')

    def test_status_and_active_reads_require_no_password(self):
        self.probe('plain-reads')

    def test_missing_null_bad_cross_tool_and_changed_arguments_never_grant_exemption(self):
        self.probe('invalid-executions')

    def test_password_authorization_cannot_replace_missing_or_bad_gateway_binding(self):
        self.probe('invalid-executions-after-password')

    def test_successful_gateway_execution_cannot_replay_into_the_next_stage(self):
        self.probe('replay')

    def test_gateway_password_exemption_retains_credential_content_rejection(self):
        self.probe('credential-content')


async def run_probe(mode):
    root = Path(os.environ['STBRAIN_DB_PATH']).resolve().parent
    databases = {root / name for name in ('main.db', 'ideas.db', 'vault.db')}
    real_connect = sqlite3.connect
    network_attempts = []

    def synthetic_connect(path, *args, **kwargs):
        require(isinstance(path, (str, Path)) and Path(path).resolve() in databases,
                'non_synthetic_database_rejected')
        return real_connect(path, *args, **kwargs)

    def no_network(*args, **kwargs):
        network_attempts.append(True)
        raise AssertionError('network_or_child_service_forbidden')

    with ExitStack() as guards:
        for target in ('socket.create_connection', 'socket.socket.connect',
                       'socket.socket.connect_ex', 'socket.socket.sendto',
                       'socket.socket.bind', 'socket.getaddrinfo', 'subprocess.Popen'):
            guards.enter_context(patch(target, side_effect=no_network))
        guards.enter_context(patch('sqlite3.connect', side_effect=synthetic_connect))
        from mcp_server import server
        from mcp_server.self_password import encode_password
        from runtime.execution_binding import ExecutionStore, canonical_hash, current_execution_claim
        from runtime.ordinary_access import current_ordinary_access
        from tests.test_direct_grants import self_model_content

        common = dict(owner_id=server.OWNER_ID, model_id=server.MODEL_ID)
        registry = ExecutionStore(root / 'main.db', deployment_epoch='synthetic-self-epoch',
                                  capability_secret=server.WAKE_SECRET)
        call_number = 0
        wake_number = 0
        signed_calls = 0
        wake = None
        catalog = None

        def db_rows(table):
            require(table in {'brain_onboarding_state', 'brain_onboarding_artifacts',
                'brain_onboarding_events', 'self_models', 'self_model_candidates',
                'self_model_revisions', 'self_revision_events', 'brain_wake_sessions',
                'brain_execution_calls', 'brain_direct_grants'}, 'unexpected_table')
            with closing(sqlite3.connect(root / 'main.db')) as db:
                return db.execute('SELECT * FROM ' + table).fetchall()

        def self_snapshot():
            # Executions may legitimately be claimed/finished even when a
            # business gate rejects. Assert all actual self-definition state,
            # versions, candidates, artifacts and audit records stay unchanged.
            tables = ('brain_onboarding_state', 'brain_onboarding_artifacts',
                'brain_onboarding_events', 'self_models', 'self_model_candidates',
                'self_model_revisions', 'self_revision_events')
            return canonical_hash({table: sorted(repr(row) for row in db_rows(table)) for table in tables})

        def version():
            return server.onboarding.state(**common)['state']['row_version']

        def stage():
            return server.onboarding.state(**common)['state']['stage']

        async def call(name, args):
            blocks, result = await server.mcp.call_tool(name, args)
            require(json.loads(blocks[0].text) == result, 'registered_result_mismatch')
            require(current_execution_claim() is None, 'execution_claim_leaked')
            require(current_ordinary_access(**common) is None, 'ordinary_context_leaked')
            require(PASSWORD not in json.dumps(result), 'password_returned')
            return result

        def new_gateway_wake():
            nonlocal wake_number, wake, catalog
            wake_number += 1
            entries = [{'canonical_name': tool.name, 'schema_hash': canonical_hash(tool.parameters)}
                       for tool in server.mcp._tool_manager.list_tools()]
            catalog = {'contract': 'advertised-tools/1', 'catalog_complete': True,
                       'catalog_hash': canonical_hash(entries), 'entries': entries}
            wake = server.onboarding.issue_wake(**common, host_id='synthetic-gateway',
                thread_id='synthetic-self-thread', source_kind='human_message',
                source_event_id='synthetic-self-event-' + str(wake_number))
            prepared = server.onboarding.build_pre_generation_context(**common,
                wake_id=wake['wake_id'], wake_capability=wake['wake_capability'],
                source_digest='synthetic-source-' + str(wake_number),
                host_contract_digest='synthetic-gateway-contract', advertised_tools=catalog)
            require(prepared.get('decision') == 'context_prepared', 'gateway_context_not_prepared')
            confirmed = server.onboarding.confirm_context_injected(**common,
                wake_id=wake['wake_id'], wake_capability=wake['wake_capability'],
                context_hash=prepared['context_hash'])
            require(confirmed.get('decision') == 'injected', 'synthetic_host_ack_failed')

        def issue(name, args):
            nonlocal call_number
            call_number += 1
            batch = dict(**common, wake_id=wake['wake_id'], wake_capability=wake['wake_capability'],
                         batch_id='synthetic-self-batch-' + str(call_number), revision=1)
            tool = server.mcp._tool_manager.get_tool(name)
            ref = registry.issue_batch(**batch, calls=[{
                'call_id': 'synthetic-self-call-' + str(call_number),
                'canonical_tool': name, 'advertised_name': name,
                'schema_hash': canonical_hash(tool.parameters), 'catalog_hash': catalog['catalog_hash'],
                'arguments_hash': canonical_hash(args),
            }])['executions'][0]['execution_ref']
            return ref, batch

        async def signed(name, args):
            nonlocal signed_calls
            ref, batch = issue(name, args)
            result = await call(name, {**args, 'execution_ref': ref})
            signed_calls += 1
            counts = registry.batch_status(**batch)['counts']
            require(counts.get('running', 0) == 0 and counts.get('issued', 0) == 0,
                    'execution_not_finished')
            return result

        async def gateway_open():
            opened = await signed('stbrain_open', {})
            require(opened.get('write_context_available') is True, 'gateway_context_not_opened')
            return opened

        def submit_args(opened, intent, payload=None):
            return {'intent': intent, 'write_context_ref': opened['write_context_ref'],
                    'expected_row_version': version(), 'payload': payload or {}}

        async def to_body_draft(opened, submit_call):
            for intent, payload in (('acknowledge', {}), ('acknowledge', {}),
                ('save_calm_prompt', {'text': '合成 AI 选择先核对长期意愿。'})):
                result = await submit_call(SUBMIT, submit_args(opened, intent, payload))
                require(result.get('decision') in {'advanced', 'saved'}, 'onboarding_step_denied')
            require(stage() == 'body_draft', 'body_draft_not_reached')

        async def submit_candidate(opened, submit_call):
            result = await submit_call(SUBMIT, submit_args(opened, 'submit', {
                'content': self_model_content('synthetic-gateway-auth'),
                'reason': '合成 AI 自主提交用于授权回归的定义。'}))
            require(result.get('decision') == 'pending', 'candidate_not_pending')
            require(not db_rows('self_model_revisions'), 'submission_changed_active_definition')
            return result

        def activation_args(opened, candidate):
            return {'candidate_id': candidate['candidate_id'],
                'write_context_ref': opened['write_context_ref'], 'expected_row_version': version(),
                'expected_active_revision': None, 'ai_confirmation': True}

        async def authorize():
            # Only an explicitly synthetic temporary verifier is written.
            (root / 'password.json').write_text(
                json.dumps(encode_password(PASSWORD, salt=b'G' * 16)), encoding='utf-8')
            grant = await call('authorize_self_model', {'password': PASSWORD})
            require(grant.get('decision') == 'authorized', 'direct_password_denied')
            require(grant.get('authorization_basis') == 'deployment_password_possession',
                    'wrong_password_authorization_basis')
            return grant

        server.onboarding.ensure_state(**common)
        require(not server.self_password_authority.allowed(), 'unexpected_password_authorization')
        require(not db_rows('self_model_candidates') and not db_rows('self_model_revisions'),
                'fixture_not_empty')
        require(await server.StaticBearerVerifier().verify_token('wrong') is None, 'bad_mcp_bearer_accepted')
        checks = 0

        if mode == 'plain-reads':
            before = self_snapshot()
            for view in ('status', 'active', 'search'):
                result = await call('query_self_model', {'view': view})
                require(result.get('view') == view and result.get('decision') != 'reject',
                        'read_required_password')
                checks += 1
            require(before == self_snapshot(), 'read_changed_self_state')
        elif mode in {'direct-no-password', 'direct-authorized'}:
            if mode == 'direct-no-password':
                # A legacy server-authorized direct grant alone is deliberately
                # insufficient for this profile's additional password policy.
                grant = server.onboarding.issue_direct_grant(**common,
                    actor_id='synthetic-human', client_principal=server.DIRECT_CLIENT_PRINCIPAL,
                    request_id='synthetic-without-password', requested_scopes=['self_revision'])
            else:
                wrong = await call('authorize_self_model', {'password': 'wrong-synthetic-value'})
                require(wrong.get('decision') == 'reject', 'wrong_password_authorized')
                grant = await authorize()
            opened = await call('stbrain_open_direct', {'grant_ref': grant['grant_ref']})
            require(opened.get('write_context_available') is True, 'direct_grant_not_opened')
            if mode == 'direct-no-password':
                before = self_snapshot()
                for name, args in ((SUBMIT, submit_args(opened, 'acknowledge')),
                    (ACTIVATE, {'candidate_id': 'cand_' + '1' * 32,
                        'write_context_ref': opened['write_context_ref'], 'expected_row_version': version(),
                        'expected_active_revision': None, 'ai_confirmation': True})):
                    denied = await call(name, args)
                    require(denied.get('reason_code') == 'self_password_required',
                            'direct_nonpassword_grant_bypassed_password')
                    require(before == self_snapshot(), 'unauthorized_direct_write_changed_state')
                    checks += 1
            else:
                await to_body_draft(opened, call)
                await submit_candidate(opened, call)
                require(server.self_password_authority.allowed(), 'direct_authorization_lost')
                require(len(db_rows('brain_direct_grants')) == 1, 'unexpected_direct_grants')
                checks += 4
        else:
            if mode == 'invalid-executions-after-password':
                await authorize()
            new_gateway_wake()
            opened = await gateway_open()
            args = submit_args(opened, 'acknowledge')
            if mode.startswith('invalid-executions'):
                ref, batch = issue(SUBMIT, args)
                attempts = [args, {**args, 'execution_ref': None}, {**args, 'execution_ref': ''},
                    {**args, 'execution_ref': 'bad'}, {**args, 'execution_ref': 7},
                    {**args, 'execution_ref': 'stexec_' + 'A' * 43},
                    {**args, 'execution_ref': ref, 'expected_row_version': version() + 1}]
                for arguments in attempts:
                    before = self_snapshot()
                    denied = await call(SUBMIT, arguments)
                    require(denied.get('decision') == 'reject', 'bad_execution_exempted_password')
                    require(denied.get('reason_code') in
                            {'execution_binding_required', 'execution_binding_invalid_or_finished'},
                            'bad_execution_reached_inner_authority')
                    require(before == self_snapshot(), 'bad_execution_changed_self_state')
                    checks += 1
                before = self_snapshot()
                crossed = await call(ACTIVATE, {**activation_args(opened, {'candidate_id': 'cand_' + '1' * 32}),
                                              'execution_ref': ref})
                require(crossed.get('reason_code') == 'execution_binding_invalid_or_finished',
                        'cross_tool_lease_accepted')
                require(before == self_snapshot(), 'cross_tool_changed_self_state')
                require(registry.batch_status(**batch)['counts']['issued'] == 1,
                        'invalid_attempts_consumed_correct_lease')
                valid = await call(SUBMIT, {**args, 'execution_ref': ref})
                require(valid.get('decision') == 'advanced', 'valid_lease_unusable_after_bad_attempts')
                checks += 2
            elif mode == 'replay':
                ref, batch = issue(SUBMIT, args)
                first = await call(SUBMIT, {**args, 'execution_ref': ref})
                require(first.get('decision') == 'advanced', 'valid_ack_denied')
                before = self_snapshot()
                replay = await call(SUBMIT, {**args, 'execution_ref': ref})
                require(replay.get('reason_code') == 'execution_binding_invalid_or_finished',
                        'completed_lease_replayed')
                require(before == self_snapshot(), 'replay_advanced_next_stage')
                require(stage() == 'module_intro', 'replay_changed_stage')
                require(registry.batch_status(**batch)['counts']['completed'] == 1,
                        'completed_lease_count_wrong')
                checks += 2
            elif mode in {'gateway-submit', 'gateway-full-chain', 'credential-content'}:
                await to_body_draft(opened, signed)
                checks += 3
                if mode == 'credential-content':
                    for sample in ('站点密码轮换：DEMO_OLD → DEMO_NEW', 'token：DEMO_SYNTHETIC_VALUE'):
                        content = self_model_content('synthetic-credential-negative')
                        content['facets']['technical'] = sample
                        before = self_snapshot()
                        denied = await signed(SUBMIT, submit_args(opened, 'submit',
                            {'content': content, 'reason': '合成检测保留边界'}))
                        require('credential_or_secret_detected' in denied.get('reason_codes', []),
                                'gateway_authorization_bypassed_credential_guard')
                        require(before == self_snapshot(), 'credential_candidate_changed_self_state')
                        require('DEMO_' not in json.dumps(denied), 'credential_value_echoed')
                        checks += 1
                candidate = await submit_candidate(opened, signed)
                checks += 1
                require(not server.self_password_authority.allowed(), 'gateway_set_password_authority')
                require(not (root / 'password.json').exists(), 'gateway_required_password_file')
                require(not db_rows('brain_direct_grants'), 'gateway_created_direct_grant')
                if mode == 'gateway-full-chain':
                    before = self_snapshot()
                    same = await signed(ACTIVATE, activation_args(opened, candidate))
                    require(same.get('decision') != 'activate', 'same_proposal_wake_activated')
                    require(before == self_snapshot(), 'same_wake_attempt_changed_self_state')
                    new_gateway_wake()
                    page_args = {'view': 'review', 'module': 'self_revision', 'page': 0}
                    for _ in range(20):
                        reviewed = await signed('stbrain_open', page_args)
                        if reviewed.get('review_material_presented') is True:
                            break
                        page_args = reviewed.get('next_arguments')
                        require(isinstance(page_args, dict), 'review_pages_unavailable')
                    else:
                        raise AssertionError('review_page_limit_exceeded')
                    accepted = await signed(SUBMIT, submit_args(reviewed, 'accept_review',
                        {'ai_confirmation': True}))
                    require(accepted.get('decision') == 'review_accepted', 'later_wake_review_denied')
                    before = self_snapshot()
                    same = await signed(ACTIVATE, activation_args(reviewed, candidate))
                    require(same.get('decision') != 'activate', 'same_review_wake_activated')
                    require(before == self_snapshot(), 'same_review_wake_changed_self_state')
                    new_gateway_wake()
                    final_opened = await signed('stbrain_open', {'view': 'manual', 'module': 'self_revision'})
                    activated = await signed(ACTIVATE, activation_args(final_opened, candidate))
                    require(activated.get('decision') == 'activate' and activated.get('pointer_changed') is True,
                            'later_wake_activation_denied')
                    require(len(db_rows('self_model_revisions')) == 1, 'wrong_active_revision_count')
                    require(len(db_rows('brain_wake_sessions')) == 3, 'real_wake_boundaries_not_preserved')
                    checks += 4
            else:
                raise AssertionError('unknown_probe_mode')

        for database in databases:
            with closing(sqlite3.connect(database)) as db:
                require(PASSWORD not in '\n'.join(db.iterdump()), 'password_persisted_in_business_db')
        require(not network_attempts, 'network_attempted')
        require(not any(row for row in db_rows('brain_execution_calls') if 'running' in row),
                'running_execution_leaked')
        return {'decision': 'PASS', 'mode': mode, 'checks': checks,
                'signed_dispatches': signed_calls, 'network_attempts': len(network_attempts),
                'real_model_requested': False, 'synthetic_databases': 3}


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        print(json.dumps(asyncio.run(run_probe(sys.argv[2]))))
    else:
        unittest.main()
