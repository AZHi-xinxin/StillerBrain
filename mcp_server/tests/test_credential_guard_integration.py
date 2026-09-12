"""Real credential gates and MCP facades, using only synthetic temporary DBs.

No detector or authorization implementation is mocked. Registered MCP calls
are in-process dispatch (not an HTTP authentication/transport test). A separate
process supplies synthetic credentials before importing the server; sockets,
child services and connections to any other database are forbidden. Historical
fixtures are inserted only into these temporary DBs to represent records made
before this guard existed. They do not rewrite any user's historical record.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, ExitStack
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


MODULES = ('emotional_memory', 'learning_memory', 'planning_memory')
SAMPLES = {
    'chinese_rotation': '站点密码轮换：DEMO_OLD_921 → DEMO_NEW_921',
    'fullwidth_token': 'token：DEMO_FULLWIDTH_921',
}
SAFE_TEXT = '  该 AI 本轮 token 预算：3000，继续做课程整理。  '
NEW_SAFE_TEXT = '  该 AI 本轮 token 用量：2100，保留自己的原文。  '


class CredentialGuardIntegrationTests(unittest.TestCase):
    def probe(self, mode):
        allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP',
                   'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'PATHEXT',
                   'SYSTEMDRIVE', 'NUMBER_OF_PROCESSORS'}
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix='credential-guard-synthetic-') as scratch:
            root = Path(scratch).resolve()
            env.update({
                'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1',
                'STBRAIN_MCP_TOKEN': 'synthetic-guard-auth-' + 'm' * 40,
                'STBRAIN_WAKE_SECRET': 'synthetic-guard-wake-' + 'w' * 40,
                'STBRAIN_OWNER_ID': 'synthetic-guard-owner',
                'STBRAIN_MODEL_ID': 'synthetic-guard-model',
                'STBRAIN_DB_PATH': str(root / 'main.db'),
                'STBRAIN_LEARNING_IDEA_DB_PATH': str(root / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(root / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1',
                'STBRAIN_EXECUTION_EPOCH': 'synthetic-credential-epoch',
                'STBRAIN_SELF_PASSWORD_HASH_FILE': str(root / 'absent-verifier.json'),
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8',
            })
            result = subprocess.run(
                [sys.executable, '-B', '-m',
                 'mcp_server.tests.test_credential_guard_integration', '--probe', mode],
                cwd=Path(__file__).resolve().parents[2], env=env,
                capture_output=True, text=True, encoding='utf-8', timeout=60,
            )
        # The child prints only fixed assertions/labels, never payloads/config.
        self.assertEqual(0, result.returncode, result.stderr)
        proof = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual('PASS', proof['decision'])
        self.assertEqual(mode, proof['mode'])
        self.assertEqual(0, proof['network_attempts'])
        self.assertTrue(proof['core_self_definition_unchanged'])
        return proof

    def test_registered_daily_writes_reject_rotation_and_fullwidth_in_all_three_modules(self):
        self.assertEqual(6, self.probe('daily-rejection')['checks'])

    def test_registered_optional_fields_cannot_hide_credential_values(self):
        self.assertEqual(12, self.probe('metadata-rejection')['checks'])

    def test_registered_revisions_reject_without_new_version_or_audit_payload(self):
        self.assertEqual(6, self.probe('revision-rejection')['checks'])

    def test_normal_token_usage_preserves_exact_author_text_versions_and_permissions(self):
        self.assertEqual(3, self.probe('ordinary-positive')['checks'])

    def test_explicit_legacy_candidate_submission_cannot_bypass_real_detector(self):
        self.assertEqual(2, self.probe('legacy-submission')['checks'])

    def test_hash_valid_historical_candidate_cannot_be_accepted_after_fix(self):
        self.assertEqual(2, self.probe('legacy-acceptance')['checks'])

    def test_historical_records_fail_final_context_guard_without_snapshot_or_rewrite(self):
        self.assertEqual(2, self.probe('historical-context')['checks'])

    def test_normal_token_usage_can_prepare_generation_context_without_model_request(self):
        self.assertEqual(1, self.probe('context-positive')['checks'])

    def test_hash_valid_old_prepared_and_injected_snapshots_cannot_be_reused(self):
        self.assertEqual(4, self.probe('historical-snapshot-reuse')['checks'])

    def test_hash_valid_old_snapshots_cannot_confirm_or_reconfirm_injection(self):
        self.assertEqual(4, self.probe('historical-snapshot-confirm')['checks'])


def require(condition, code):
    if not condition:
        raise AssertionError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


async def run_probe(mode):
    root = Path(os.environ['STBRAIN_DB_PATH']).resolve().parent
    databases = {root / name for name in ('main.db', 'ideas.db', 'vault.db')}
    real_connect = sqlite3.connect
    attempts = []

    def synthetic_connect(path, *args, **kwargs):
        if not isinstance(path, (str, Path)) or Path(path).resolve() not in databases:
            raise AssertionError('non_synthetic_database_rejected')
        return real_connect(path, *args, **kwargs)

    def no_network(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('network_or_service_forbidden')

    with ExitStack() as guards:
        for name in ('socket.create_connection', 'socket.socket.connect',
                     'socket.socket.connect_ex', 'socket.socket.sendto',
                     'socket.socket.bind', 'socket.getaddrinfo', 'subprocess.Popen'):
            guards.enter_context(patch(name, side_effect=no_network))
        guards.enter_context(patch('sqlite3.connect', side_effect=synthetic_connect))
        from mcp_server import server
        from mcp_server.tests.test_planning_service import calm, plan_content
        from runtime.credential_guard import contains_credential_or_secret
        from runtime.execution_binding import canonical_hash
        from runtime.onboarding import OnboardingError
        from runtime.ordinary_access import current_ordinary_access
        from runtime.planning_memory import PlanningMemoryError
        from tests.test_onboarding import ModuleOneOnboardingTests

        common = dict(owner_id=server.OWNER_ID, model_id=server.MODEL_ID)
        # Initialize legitimate module metadata before no-change comparisons.
        for service in (server.emotional_service, server.learning_service, server.planning_service):
            service.status()

        def rows(table):
            require(table in {'self_model_revisions', 'brain_wake_sessions',
                             'brain_context_snapshots'}, 'unexpected_table')
            with closing(sqlite3.connect(root / 'main.db')) as db:
                return db.execute('SELECT * FROM ' + table).fetchall()

        def snapshot():
            result = {}
            for path in sorted(databases):
                with closing(sqlite3.connect(path)) as db:
                    tables = sorted(row[0] for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"))
                    result[path.name] = {
                        name: sorted(repr(row) for row in db.execute(
                            'SELECT * FROM "' + name.replace('"', '""') + '"'))
                        for name in tables
                    }
            return digest(result)

        def all_text_values():
            def collect(value):
                if isinstance(value, str):
                    yield value
                    if value.startswith(('{', '[')):
                        try:
                            nested = json.loads(value)
                        except (ValueError, TypeError):
                            return
                        yield from collect(nested)
                elif isinstance(value, dict):
                    for child in value.values():
                        yield from collect(child)
                elif isinstance(value, (tuple, list)):
                    for child in value:
                        yield from collect(child)
            with closing(sqlite3.connect(root / 'main.db')) as db:
                for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                    for row in db.execute('SELECT * FROM "' + table.replace('"', '""') + '"'):
                        yield from collect(row)

        async def call(name, arguments):
            blocks, result = await server.mcp.call_tool(name, arguments)
            require(json.loads(blocks[0].text) == result, 'registered_result_mismatch')
            require(current_ordinary_access(**common) is None, 'ordinary_authority_leaked')
            return result

        async def reject_unchanged(name, arguments):
            before = snapshot()
            result = await call(name, arguments)
            require(result.get('decision') == 'reject', 'credential_write_not_rejected')
            require(result.get('state_changed') is False, 'rejection_claimed_state_change')
            require(any('credential' in code for code in result.get('reason_codes', [])),
                    'rejection_not_credential_gate')
            require(before == snapshot(), 'rejected_write_changed_database')
            serialized = canonical(result)
            require('DEMO_' not in serialized, 'rejected_value_echoed')

        async def seed_plan():
            result = await call('remember_memory', {'module': 'planning_memory',
                'content': SAFE_TEXT, 'title': '合成课堂安排', 'summary': '合成课堂安排',
                'keywords': ['合成课堂安排'], 'importance': 90})
            require(result.get('decision') == 'stored', 'safe_seed_rejected')
            return result

        def prepare(label):
            wake = server.onboarding.issue_wake(**common, host_id='synthetic-host',
                thread_id='synthetic-thread-' + label, source_kind='human_message',
                source_event_id='synthetic-event-' + label)
            entries = [{'canonical_name': tool.name, 'schema_hash': canonical_hash(tool.parameters)}
                       for tool in server.mcp._tool_manager.list_tools()]
            catalog = {'contract': 'advertised-tools/1', 'catalog_complete': True,
                       'catalog_hash': canonical_hash(entries), 'entries': entries}
            arguments = dict(**common, wake_id=wake['wake_id'],
                wake_capability=wake['wake_capability'], source_digest='synthetic-digest-' + label,
                host_contract_digest='synthetic-contract', advertised_tools=catalog,
                source_frame={'query_text': '合成课堂安排', 'lineage_stable': False,
                              'capture_items': []})
            return arguments

        require(not rows('self_model_revisions') and not rows('brain_wake_sessions'), 'fixture_not_empty')
        # Credential-specific positive/negative paths need a genuinely activated
        # synthetic module one. First prove the new unactivated write gate, then
        # traverse the existing real three-wake runtime fixture (no SQL unlock).
        for module in MODULES:
            before = snapshot()
            blocked = await call('remember_memory', {'module': module, 'content': SAFE_TEXT})
            require(blocked.get('reason_code') == 'module_one_required', 'unactivated_module_write_allowed')
            require(before == snapshot(), 'unactivated_rejection_changed_database')
        fixture = ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / 'main.db', server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()
        require(server.onboarding.state(**common)['module_one_unlocked'] is True,
                'synthetic_activation_not_complete')
        original_core = rows('self_model_revisions')
        original_wakes = rows('brain_wake_sessions')
        original_snapshots = rows('brain_context_snapshots')
        for value in SAMPLES.values():
            require(contains_credential_or_secret(value), 'real_detector_missed_sample')
        require(not contains_credential_or_secret(SAFE_TEXT), 'normal_usage_false_positive')
        require(await server.StaticBearerVerifier().verify_token('wrong') is None,
                'wrong_bearer_accepted')
        require(await server.StaticBearerVerifier().verify_token(os.environ['STBRAIN_MCP_TOKEN']) is not None,
                'synthetic_bearer_rejected')
        checks = 0

        if mode == 'daily-rejection':
            for module in MODULES:
                for value in SAMPLES.values():
                    await reject_unchanged('remember_memory', {'module': module, 'content': value})
                    checks += 1
        elif mode == 'metadata-rejection':
            for module in MODULES:
                for field in ('summary', 'title', 'reason', 'keywords'):
                    value = SAMPLES['fullwidth_token']
                    args = {'module': module, 'content': SAFE_TEXT,
                            field: [value] if field == 'keywords' else value}
                    await reject_unchanged('remember_memory', args)
                    checks += 1
        elif mode in {'revision-rejection', 'ordinary-positive'}:
            for module in MODULES:
                saved = await call('remember_memory', {'module': module, 'content': SAFE_TEXT})
                require(saved.get('decision') == 'stored', 'normal_usage_write_rejected')
                field = 'current_understanding' if module == 'learning_memory' else 'original_text'
                if mode == 'revision-rejection':
                    for value in SAMPLES.values():
                        await reject_unchanged('revise_memory', {'target_ref': saved['ref'],
                            'changes': {field: value}})
                        checks += 1
                else:
                    require(SAFE_TEXT in set(all_text_values()), 'original_author_text_modified')
                    revised = await call('revise_memory', {'target_ref': saved['ref'],
                        'changes': {field: NEW_SAFE_TEXT}})
                    require(revised.get('decision') == 'revised' and revised.get('version') == 2,
                            'ordinary_revision_not_preserved')
                    values = set(all_text_values())
                    require(SAFE_TEXT in values and NEW_SAFE_TEXT in values,
                            'exact_author_versions_not_preserved')
                    before = snapshot()
                    stale = await call('revise_memory', {'target_ref': saved['ref'],
                        'changes': {'summary': '旧引用不能覆盖新版'}})
                    require(stale.get('decision') == 'reject', 'stale_author_version_accepted')
                    require(before == snapshot(), 'stale_revision_changed_database')
                    checks += 1
            require(rows('brain_wake_sessions') == original_wakes, 'ordinary_call_created_fake_real_wake')
        elif mode == 'legacy-submission':
            for label, value in SAMPLES.items():
                before = snapshot()
                try:
                    server.planning_store.propose_create(**common,
                        wake_id='synthetic-legacy-1', wake_seq=1,
                        expected_row_version=server.planning_service.status()['row_version'],
                        content=plan_content(value), reason='合成旧候选测试', calm_check=calm(),
                        ai_confirmation=True, idempotency_key='synthetic-' + label)
                except PlanningMemoryError as error:
                    require(str(error) == 'credential_content_rejected', 'wrong_legacy_rejection')
                else:
                    raise AssertionError('legacy_submission_bypassed_detector')
                require(before == snapshot(), 'legacy_rejection_changed_database')
                checks += 1
        elif mode == 'legacy-acceptance':
            for label, value in SAMPLES.items():
                pending = server.planning_store.propose_create(**common,
                    wake_id='synthetic-legacy-1', wake_seq=1,
                    expected_row_version=server.planning_service.status()['row_version'],
                    content=plan_content('合成历史候选'), reason='合成旧候选夹具', calm_check=calm(),
                    ai_confirmation=True, idempotency_key='synthetic-' + label)
                # Synthesize an old, internally hash-consistent candidate. Do
                # not disable the new detector to create it through today's API.
                with closing(sqlite3.connect(root / 'main.db')) as db:
                    db.row_factory = sqlite3.Row
                    row = dict(db.execute('SELECT * FROM planning_change_candidates WHERE candidate_id=?',
                        (pending['candidate_id'],)).fetchone())
                    proposed = json.loads(row['proposed_content_json'])
                    proposed['summary'] = value
                    diff = json.loads(row['diff_json'])
                    if isinstance(diff.get('summary'), dict):
                        diff['summary']['after'] = value
                    material = {k: row[k] for k in ('candidate_id', 'candidate_version', 'plan_id',
                        'intent', 'base_version', 'reason', 'submitted_wake_id', 'submitted_wake_seq')}
                    material.update(proposed_content=proposed, canonical_diff=diff,
                                    calm_check=json.loads(row['calm_check_json']))
                    candidate_hash = digest(material)
                    db.execute('UPDATE planning_change_candidates SET proposed_content_json=?, '
                        'proposed_content_hash=?, diff_json=?, candidate_hash=? WHERE candidate_id=?',
                        (canonical(proposed), digest(proposed), canonical(diff), candidate_hash,
                         pending['candidate_id']))
                    db.commit()
                await reject_unchanged('review_planning_change', {
                    'candidate_id': pending['candidate_id'], 'expected_candidate_version': 1,
                    'expected_candidate_hash': candidate_hash, 'expected_base_version': 0,
                    'decision': 'accept', 'correctness_assessment': '合成作者确认',
                    'calm_check': calm(), 'reason': '合成旧候选采纳测试', 'ai_confirmation': True})
                checks += 1
        elif mode in {'historical-snapshot-reuse', 'historical-snapshot-confirm'}:
            await seed_plan()
            for label, value in SAMPLES.items():
                for status in ('prepared', 'injected'):
                    arguments = prepare(label + '-' + status)
                    prepared = server.onboarding.build_pre_generation_context(**arguments)
                    require(prepared.get('decision') == 'context_prepared', 'old_snapshot_seed_failed')
                    confirmation = {k: arguments[k] for k in
                                    ('owner_id', 'model_id', 'wake_id', 'wake_capability')}
                    if status == 'injected':
                        # Synthetic host acknowledgement only; no model is called.
                        safe_confirmed = server.onboarding.confirm_context_injected(
                            **confirmation, context_hash=prepared['context_hash'])
                        require(safe_confirmed.get('decision') == 'injected', 'safe_confirmation_failed')

                    def replace_summary(node):
                        if isinstance(node, dict):
                            return {k: replace_summary(child) for k, child in node.items()}
                        if isinstance(node, list):
                            return [replace_summary(child) for child in node]
                        if isinstance(node, str):
                            return node.replace('合成课堂安排', value)
                        return node

                    # Retain the real snapshot shape and all status metadata;
                    # replace a selected synthetic summary as an old-version
                    # fixture and recompute every affected content hash.
                    with closing(sqlite3.connect(root / 'main.db')) as db:
                        db.row_factory = sqlite3.Row
                        row = db.execute('SELECT * FROM brain_context_snapshots WHERE wake_id=?',
                            (arguments['wake_id'],)).fetchone()
                        require(row['status'] == status, 'old_snapshot_status_mismatch')
                        stable = json.loads(row['stable_json'])
                        dynamic = replace_summary(json.loads(row['dynamic_json']))
                        require(value in canonical(dynamic), 'old_snapshot_summary_not_replaced')
                        require(json.loads(row['context_layout_json']) == {}, 'unexpected_fixture_layout')
                        message = server.onboarding._context_message(stable=stable, dynamic=dynamic)
                        old_hash = digest(message)
                        db.execute('UPDATE brain_context_snapshots SET dynamic_json=?,dynamic_hash=?, '
                            'context_hash=? WHERE wake_id=?',
                            (canonical(dynamic), digest(dynamic), old_hash, arguments['wake_id']))
                        db.execute('UPDATE brain_wake_sessions SET context_hash=? WHERE wake_id=?',
                            (old_hash, arguments['wake_id']))
                        db.commit()
                    before = snapshot()
                    if mode == 'historical-snapshot-reuse':
                        denied = server.onboarding.build_pre_generation_context(**arguments)
                        require(denied.get('decision') == 'self_model_context_unavailable',
                                'old_credential_snapshot_reused')
                        require(denied.get('may_generate') is False, 'old_snapshot_allowed_generation')
                    else:
                        denied = server.onboarding.confirm_context_injected(
                            **confirmation, context_hash=old_hash)
                        require(denied.get('decision') == 'protected_persistence_value',
                                'old_credential_injection_confirmed')
                    require(denied.get('reason_codes') == ['protected_persistence_value'],
                            'old_snapshot_rejected_for_wrong_reason')
                    require('message' not in denied and 'context_bundle' not in denied,
                            'old_snapshot_content_returned')
                    require('DEMO_' not in canonical(denied), 'old_snapshot_value_echoed')
                    require(before == snapshot(), 'denial_rewrote_old_snapshot_or_metadata')
                    checks += 1
        elif mode in {'historical-context', 'context-positive'}:
            saved = await seed_plan()
            values = SAMPLES.items() if mode == 'historical-context' else [('normal', SAFE_TEXT)]
            for label, value in values:
                if mode == 'historical-context':
                    with closing(sqlite3.connect(root / 'main.db')) as db:
                        raw = db.execute('SELECT content_json FROM planning_versions WHERE plan_id=? AND version=1',
                            (saved['id'],)).fetchone()[0]
                        content = json.loads(raw)
                        content.update(summary=value, reminder=value, presence_mode='persistent')
                        db.execute('UPDATE planning_versions SET content_json=?,content_hash=? '
                            'WHERE plan_id=? AND version=1',
                            (canonical(content), digest(content), saved['id']))
                        db.commit()
                    projection = server.planning_store.build_injection(**common, query='合成课堂安排')
                    require(value in canonical(projection), 'historical_fixture_not_selected')
                arguments = prepare(label)
                before = snapshot()
                if mode == 'historical-context':
                    try:
                        server.onboarding.build_pre_generation_context(**arguments)
                    except OnboardingError as error:
                        require(str(error) == 'protected_persistence_value', 'wrong_final_context_rejection')
                    else:
                        raise AssertionError('historical_credential_reached_generation_context')
                    require(before == snapshot(), 'failed_context_persisted_state_or_rewrote_history')
                    require(rows('brain_context_snapshots') == original_snapshots,
                            'rejected_context_snapshot_persisted')
                else:
                    prepared = server.onboarding.build_pre_generation_context(**arguments)
                    require(prepared.get('decision') == 'context_prepared', 'normal_context_not_prepared')
                    require(prepared.get('may_generate') is False, 'host_confirmation_gate_bypassed')
                    require(not contains_credential_or_secret(prepared.get('message')), 'prepared_message_unsafe')
                    require(len(rows('brain_context_snapshots')) == len(original_snapshots) + 1,
                            'normal_snapshot_missing')
                checks += 1
        else:
            raise AssertionError('unknown_probe_mode')
        require(rows('self_model_revisions') == original_core, 'core_self_definition_changed')
        require(not attempts, 'network_attempted')
        return {'decision': 'PASS', 'mode': mode, 'checks': checks,
                'network_attempts': len(attempts), 'core_self_definition_unchanged': True,
                'synthetic_database_count': 3, 'real_model_requested': False}


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        print(json.dumps(asyncio.run(run_probe(sys.argv[2])), ensure_ascii=True))
    else:
        unittest.main()
