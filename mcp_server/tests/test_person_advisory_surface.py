"""Effective optional advice through actual MCP; isolated synthetic stores only."""
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
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcp_server.person_reference_surface import AUTHORING_RESULT_TOOLS, install_person_reference_advisory_surface
from mcp_server.usage_guide import stored_memory_wording_advisory
from runtime.person_reference_advisory import PERSON_REFERENCE_ADVISORY_DEFAULT


class AdvisoryResultSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, result, *, advice=None, error=None, convert=False, name='remember_memory'):
        async def original(*args, **kwargs):
            self.assertFalse(kwargs['convert_result'])
            return result
        def read():
            if error:
                raise error
            return advice
        tool = SimpleNamespace(fn_metadata=SimpleNamespace(convert_result=lambda value: ('converted', value)))
        manager = SimpleNamespace(call_tool=original, get_tool=lambda name: tool, list_tools=lambda: [])
        install_person_reference_advisory_surface(SimpleNamespace(_tool_manager=manager),
                                                  SimpleNamespace(advisory_status=read))
        return await manager.call_tool(name, {}, convert_result=convert)

    async def test_enabled_and_disabled_preserve_original_receipts(self):
        original = {'decision': 'stored', 'ref': 'synthetic-ref', 'stored': True}
        for advice in ({'enabled': True, 'message': 'synthetic custom'}, {'enabled': False}):
            result = await self.check(original, advice=advice, convert=True)
            self.assertEqual(('converted', {**original, 'person_reference_advisory': advice,
                                           'wording_advisory': stored_memory_wording_advisory()}), result)
            self.assertNotIn('person_reference_advisory', original)
            self.assertNotIn('wording_advisory', original)

    async def test_all_save_entries_get_short_optional_wording_without_mutation(self):
        for name in ('remember_memory', 'remember_emotional_memory', 'remember_learning_memory',
                     'remember_tool_guidance', 'remember_planning_memory'):
            for decision in ('stored', 'stored_quarantined'):
                with self.subTest(name=name, decision=decision):
                    original = {'decision': decision, 'stored': True, 'ref': 'synthetic-ref',
                                'summary': 'An author chose this wording.', 'keywords': ['literal']}
                    result = await self.check(original, advice={'enabled': False}, name=name)
                    hint = result['wording_advisory']
                    self.assertTrue(hint['optional'])
                    self.assertEqual('after_store', hint['stage'])
                    self.assertFalse(hint['memory_content_changed'])
                    self.assertLess(len(hint['message']), 180)
                    for word in ('原词', '近义表达', '语义相关', '情感语境', '人类自然', '由我决定'):
                        self.assertIn(word, hint['message'])
                    self.assertEqual(original, {key: result[key] for key in original})
                    self.assertNotIn('wording_advisory', original)
                    self.assertNotIn('message', result['person_reference_advisory'])

    async def test_wording_does_not_appear_for_failed_saves_or_revision_reads(self):
        original = {'decision': 'stored', 'stored': False}
        self.assertEqual(original, await self.check(original, error=AssertionError('must not read')))
        for name in ('revise_memory', 'stbrain_help', 'recall_emotional_memory'):
            original = {'decision': 'revised' if name == 'revise_memory' else 'read'}
            self.assertEqual(original, await self.check(original, name=name,
                                                       error=AssertionError('must not read')))

    def test_wording_projection_is_fresh_per_result(self):
        first = stored_memory_wording_advisory()
        first['message'] = 'mutated caller copy'
        self.assertNotEqual(first, stored_memory_wording_advisory())

    async def test_optional_read_failure_does_not_hide_success(self):
        for error in (sqlite3.OperationalError('synthetic locked'), RuntimeError('synthetic expired lease')):
            result = await self.check({'decision': 'stored', 'ref': 'synthetic-ref'}, error=error)
            self.assertEqual('stored', result['decision'])
            self.assertFalse(result['person_reference_advisory']['available'])

    async def test_legacy_advisory_and_cached_receipt_metadata_follow_current_setting(self):
        original = {'decision': 'stored', 'authoring_advisory': {'message': 'old host advice'}}
        result = await self.check(original, advice={'enabled': False})
        self.assertEqual({'enabled': False}, result['authoring_advisory'])
        self.assertEqual('old host advice', original['authoring_advisory']['message'])

    async def test_denial_and_unrelated_tools_are_untouched(self):
        denied = {'decision': 'reject', 'stored': False}
        self.assertEqual(denied, await self.check(denied, error=AssertionError('must not read')))
        for decision in ('rejected', 'candidate_pending', 'not_confirmed_stored'):
            value = {'decision': decision}
            self.assertEqual(value, await self.check(value, error=AssertionError('must not read')))
        self.assertEqual({'decision': 'read'}, await self.check({'decision': 'read'},
                          error=AssertionError('must not read'), name='recall_learning_memory'))


class AdvisoryCatalogSurfaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from mcp.server.fastmcp import FastMCP
        self.mcp = FastMCP('synthetic-advisory-catalog')
        self.save_count = self.read_count = 0
        self.read_error = None
        self.advice = {'enabled': True, 'message': PERSON_REFERENCE_ADVISORY_DEFAULT}

        async def save(content: str):
            self.save_count += 1
            return {'decision': 'stored', 'stored': True, 'content': content}

        for name in sorted(AUTHORING_RESULT_TOOLS | {'stbrain_help'}):
            self.mcp.add_tool(save, name=name, description='Synthetic base description: ' + name)
        manager = self.mcp._tool_manager
        self.baseline = {tool.name: (tool.description, json.dumps(tool.parameters, sort_keys=True))
                         for tool in manager.list_tools()}

        def read():
            self.read_count += 1
            if self.read_error is not None:
                raise self.read_error
            return self.advice

        install_person_reference_advisory_surface(self.mcp, SimpleNamespace(advisory_status=read))

    async def test_actual_tools_list_shows_both_tips_before_any_save(self):
        listed = await self.mcp.list_tools()
        self.assertEqual(1, self.read_count)
        self.assertEqual(0, self.save_count)
        self.assertEqual(set(self.baseline), {tool.name for tool in listed})
        for tool in listed:
            self.assertEqual(self.baseline[tool.name][1], json.dumps(tool.inputSchema, sort_keys=True))
            self.assertEqual(self.baseline[tool.name][0], self.mcp._tool_manager.get_tool(tool.name).description)
            if tool.name in AUTHORING_RESULT_TOOLS:
                self.assertTrue(tool.description.startswith('存入前·可选提醒'))
                for text in (PERSON_REFERENCE_ADVISORY_DEFAULT, stored_memory_wording_advisory()['message'],
                             'manage_person_reference_advisory', '由 AI 自选', '快照', '本轮管理结果优先',
                             '刷新 MCP 工具目录', '无需为提醒增加预览或确认调用'):
                    self.assertIn(text, tool.description)
            else:
                self.assertEqual(self.baseline[tool.name][0], tool.description)

    async def test_current_set_disable_reset_leave_old_snapshots_and_schemas_unchanged(self):
        initial = await self.mcp.list_tools()
        initial_descriptions = [tool.description for tool in initial]
        second = await self.mcp.list_tools()
        self.assertEqual(initial_descriptions, [tool.description for tool in second])
        custom = '我写合成故事时，自行决定人称。\n这一句保持我原来的写法。'
        self.advice = {'enabled': True, 'message': custom, 'source': 'ai_authored'}
        authored = await self.mcp.list_tools()
        authored_descriptions = [tool.description for tool in authored]
        self.advice = {'enabled': False, 'message': 'OLD_BODY_MUST_NOT_LEAK'}
        disabled = await self.mcp.list_tools()
        self.advice = {'enabled': True, 'message': PERSON_REFERENCE_ADVISORY_DEFAULT}
        reset = await self.mcp.list_tools()
        self.assertEqual(5, self.read_count)
        self.assertEqual(0, self.save_count)
        self.assertEqual(initial_descriptions, [tool.description for tool in initial])
        self.assertEqual(authored_descriptions, [tool.description for tool in authored])
        self.assertEqual(initial_descriptions, [tool.description for tool in reset])
        for snapshot in (initial, second, authored, disabled, reset):
            for tool in snapshot:
                self.assertEqual(self.baseline[tool.name][1], json.dumps(tool.inputSchema, sort_keys=True))
                if tool.name in AUTHORING_RESULT_TOOLS:
                    self.assertEqual(1, tool.description.count('存入前·可选提醒'))
        for tool in authored:
            if tool.name in AUTHORING_RESULT_TOOLS:
                self.assertIn(custom, tool.description)
                self.assertNotIn(PERSON_REFERENCE_ADVISORY_DEFAULT, tool.description)
        for tool in disabled:
            if tool.name in AUTHORING_RESULT_TOOLS:
                self.assertIn('人称提醒已关闭', tool.description)
                self.assertIn(stored_memory_wording_advisory()['message'], tool.description)
                for body in (PERSON_REFERENCE_ADVISORY_DEFAULT, custom, 'OLD_BODY_MUST_NOT_LEAK'):
                    self.assertNotIn(body, tool.description)

    async def test_preference_read_failure_keeps_directory_and_successful_save_available(self):
        self.read_error = RuntimeError('PRIVATE_OPTIONAL_READ_FAILURE')
        listed = await self.mcp.list_tools()
        self.assertEqual(set(self.baseline), {tool.name for tool in listed})
        for tool in listed:
            if tool.name in AUTHORING_RESULT_TOOLS:
                self.assertIn('人称提醒当前不可读取', tool.description)
                self.assertIn(stored_memory_wording_advisory()['message'], tool.description)
                self.assertNotIn(PERSON_REFERENCE_ADVISORY_DEFAULT, tool.description)
                self.assertNotIn('PRIVATE_OPTIONAL_READ_FAILURE', tool.description)
        result = await self.mcp._tool_manager.call_tool('remember_memory', {'content': 'Synthetic unmodified content.'})
        self.assertEqual(1, self.save_count)
        self.assertEqual('stored', result['decision'])
        self.assertEqual('Synthetic unmodified content.', result['content'])
        self.assertFalse(result['person_reference_advisory']['available'])


class RegisteredPersonAdvisoryTests(unittest.TestCase):
    def test_real_registered_preferences_and_current_help(self):
        allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE', 'PATH', 'PATHEXT', 'TEMP', 'TMP', 'OS'}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix='synthetic-person-advisory-') as folder:
            root = Path(folder).resolve()
            env.update({'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1',
                'STBRAIN_MCP_TOKEN': 'synthetic-person-mcp-' + 'm' * 40,
                'STBRAIN_WAKE_SECRET': 'synthetic-person-wake-' + 'w' * 40,
                'STBRAIN_OWNER_ID': 'synthetic-person-owner', 'STBRAIN_MODEL_ID': 'synthetic-person-model',
                'STBRAIN_DB_PATH': str(root / 'main.db'),
                'STBRAIN_LEARNING_IDEA_DB_PATH': str(root / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(root / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1', 'STBRAIN_EXECUTION_EPOCH': 'synthetic-advisory-epoch',
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONIOENCODING': 'utf-8'})
            result = subprocess.run([sys.executable, '-B', '-m',
                'mcp_server.tests.test_person_advisory_surface', '--probe'],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                text=True, encoding='utf-8', timeout=90)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({'passed': True, 'real_memory_used': False, 'network_attempts': 0},
                         json.loads(result.stdout.strip().splitlines()[-1]))


async def probe():
    root = Path(os.environ['STBRAIN_DB_PATH']).resolve().parent
    dbs = {root / name for name in ('main.db', 'ideas.db', 'vault.db')}
    original_connect = sqlite3.connect
    def connect(path, *args, **kwargs):
        assert Path(path).resolve() in dbs, 'outside_synthetic_store'
        return original_connect(path, *args, **kwargs)
    def deny(*args, **kwargs):
        raise AssertionError('network_forbidden')
    with ExitStack() as guards:
        guards.enter_context(patch('sqlite3.connect', side_effect=connect))
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.socket.bind', 'socket.socket.sendto', 'socket.getaddrinfo', 'subprocess.Popen'):
            guards.enter_context(patch(target, side_effect=deny))
        from mcp_server import server
        from runtime.execution_binding import canonical_hash
        from tests.test_onboarding import ModuleOneOnboardingTests
        identity = {'owner_id': server.OWNER_ID, 'model_id': server.MODEL_ID}
        async def call(name, args):
            blocks, result = await server.mcp.call_tool(name, args)
            assert json.loads(blocks[0].text) == result
            return result
        def history_count():
            with closing(sqlite3.connect(root / 'main.db')) as connection:
                return connection.execute('SELECT COUNT(*) FROM person_reference_advisory_history').fetchone()[0]
        def row_counts():
            result = {}
            for database in sorted(dbs):
                with closing(sqlite3.connect(database)) as connection:
                    for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                        quoted = '"' + table.replace('"', '""') + '"'
                        result[(database.name, table)] = connection.execute('SELECT COUNT(*) FROM ' + quoted).fetchone()[0]
            return result
        async def catalog_snapshot(message=None, *, disabled=False):
            before = row_counts()
            listed = await server.mcp.list_tools()
            assert row_counts() == before, 'tools/list wrote synthetic store rows'
            assert len(listed) == 46, 'registered tool count changed'
            for tool in listed:
                assert tool.inputSchema == server.mcp._tool_manager.get_tool(tool.name).parameters
                if tool.name in AUTHORING_RESULT_TOOLS:
                    assert tool.description.startswith('存入前·可选提醒')
                    assert tool.description.count('存入前·可选提醒') == 1
                    assert stored_memory_wording_advisory()['message'] in tool.description
                    assert '本轮管理结果优先' in tool.description
                    if disabled:
                        assert '人称提醒已关闭' in tool.description
                        assert PERSON_REFERENCE_ADVISORY_DEFAULT not in tool.description
                        assert '我写合成记录时，会明确人物名字。' not in tool.description
                    else:
                        assert message in tool.description
                else:
                    assert not tool.description.startswith('存入前·可选提醒')
            return listed
        name = 'manage_person_reference_advisory'
        schema = server.mcp._tool_manager.get_tool(name).parameters
        assert schema.get('required') == ['action']
        assert 'execution_ref' in schema['properties']
        assert 'rewrite_receipt' in server.mcp._tool_manager.get_tool('remember_memory').parameters['properties']
        assert history_count() == 0
        original_catalog = await catalog_snapshot(PERSON_REFERENCE_ADVISORY_DEFAULT)
        original_descriptions = [tool.description for tool in original_catalog]
        initial = (await call('stbrain_help', {}))['person_reference_advisory']
        assert initial['enabled'] is True and initial['optional'] is True
        assert initial['manage_tool'] == name
        for word in ('第一人称', 'user 名', '我和她', '可能'):
            assert word in initial['message']
        assert history_count() == 0, 'default read persisted a preference'
        denied = await call(name, {'action': 'disable'})
        assert denied['reason_code'] == 'module_one_required' and history_count() == 0
        fixture = ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / 'main.db', server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()
        for args in ({'action': 'disable', 'execution_ref': 'invented'},
                     {'action': 'disable', 'execution_ref': None}):
            rejected = await call(name, args)
            assert rejected['decision'] == 'reject' and history_count() == 0
        changed = await call(name, {'action': 'set', 'text': '我写合成记录时，会明确人物名字。'})
        assert changed['decision'] != 'reject' and history_count() == 1
        current = (await call('stbrain_help', {}))['person_reference_advisory']
        assert current['enabled'] and current['message'] == '我写合成记录时，会明确人物名字。'
        authored_catalog = await catalog_snapshot(current['message'])
        authored_descriptions = [tool.description for tool in authored_catalog]
        assert original_descriptions == [tool.description for tool in original_catalog]
        stored = await call('remember_memory', {'module': 'emotional_memory', 'content': '我和她做了合成实验。'})
        assert stored['decision'] == 'stored' and stored['person_reference_advisory']['message'] == current['message']
        assert stored['wording_advisory'] == stored_memory_wording_advisory()
        assert history_count() == 1, 'memory write changed preference'
        with closing(sqlite3.connect(root / 'main.db')) as connection:
            assert connection.execute('SELECT original_text FROM emotion_memories WHERE memory_id=?',
                                      (stored['id'],)).fetchone()[0] == '我和她做了合成实验。'
        await call(name, {'action': 'disable'})
        assert history_count() == 2
        await catalog_snapshot(disabled=True)
        assert authored_descriptions == [tool.description for tool in authored_catalog]
        for module in (None, 'emotional_memory', 'learning_memory', 'tool_guidance',
                       'planning_memory', 'shared_person_authoring'):
            advice = (await call('stbrain_help', {} if module is None else {'module': module}))['person_reference_advisory']
            assert advice['enabled'] is False and 'message' not in advice
            assert '我写合成记录时' not in json.dumps(advice, ensure_ascii=False)
        assert 'message' not in server.authoring_rewrite_service.manual()['authoring_advisory']
        stored = await call('remember_memory', {'module': 'learning_memory', 'content': '合成角色继续使用第三人称叙事。'})
        assert stored['decision'] == 'stored' and not stored['person_reference_advisory']['enabled']
        assert 'message' not in stored['person_reference_advisory']
        assert stored['wording_advisory']['optional']
        dedicated = await call('remember_learning_memory', {
            'kind': 'fact', 'title': '独立合成记录', 'summary': '专用入口验证。',
            'current_understanding': '第三人称由作者自由选择。',
            'correctness_assessment': '作者自行记录。', 'reason': '验证已关闭提醒的专用入口。'})
        assert dedicated['decision'] == 'stored'
        assert dedicated['wording_advisory'] == stored_memory_wording_advisory()
        for key in ('person_reference_advisory', 'authoring_advisory'):
            assert dedicated[key]['enabled'] is False and 'message' not in dedicated[key]
        # A real host-issued lease exercises the same preference mutation for gateway calls.
        tools = server.mcp._tool_manager.list_tools()
        entries = [{'canonical_name': t.name, 'schema_hash': canonical_hash(t.parameters)} for t in tools]
        advertised = {'contract': 'advertised-tools/1', 'catalog_complete': True,
                      'catalog_hash': canonical_hash(entries), 'entries': entries}
        wake = server.onboarding.issue_wake(**identity, host_id='synthetic-host', thread_id='synthetic-thread',
                                           source_kind='human_message', source_event_id='synthetic-advisory-event')
        prepared = server.onboarding.build_pre_generation_context(**identity, wake_id=wake['wake_id'],
            wake_capability=wake['wake_capability'], source_digest='synthetic-source', host_contract_digest='synthetic-host',
            advertised_tools=advertised, source_frame={'query_text': '合成话题', 'lineage_stable': False,
                                                      'prior_assistant_present': False, 'capture_items': []})
        server.onboarding.confirm_context_injected(**identity, wake_id=wake['wake_id'],
            wake_capability=wake['wake_capability'], context_hash=prepared['context_hash'])
        args = {'action': 'reset'}
        issued = server._execution_store.issue_batch(**identity, wake_id=wake['wake_id'],
            wake_capability=wake['wake_capability'], batch_id='synthetic-person-batch', revision=1,
            calls=[{'call_id': 'synthetic-person-call', 'advertised_name': name, 'canonical_tool': name,
                    'schema_hash': canonical_hash(schema), 'catalog_hash': advertised['catalog_hash'],
                    'arguments_hash': canonical_hash(args)}])
        args['execution_ref'] = issued['executions'][0]['execution_ref']
        reset = await call(name, args)
        assert reset['decision'] != 'reject' and history_count() == 3
        assert (await call('stbrain_help', {}))['person_reference_advisory']['message'] == initial['message']
        await catalog_snapshot(initial['message'])
        replay = await call(name, args)
        assert replay['decision'] == 'reject' and history_count() == 3
        return {'passed': True, 'real_memory_used': False, 'network_attempts': 0}


if __name__ == '__main__':
    if '--probe' in sys.argv:
        print(json.dumps(asyncio.run(probe())))
    else:
        unittest.main()
