"""Real ST schemas/dispatch/receipts in a fresh synthetic DB, no networking."""
import asyncio
from contextlib import ExitStack, closing
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
    from tests.test_onboarding import ModuleOneOnboardingTests
    from migration_ui.clients import STClient
    from migration_ui.core import Chunk, canonical, receipt, write_projection

    database = Path(os.environ['STBRAIN_DB_PATH'])
    checks = 0
    def require(condition):
        nonlocal checks
        if not condition: raise AssertionError('synthetic_contract_check_failed_' + str(checks))
        checks += 1
    def rows(table):
        with closing(sqlite3.connect(database)) as connection:
            return connection.execute('SELECT * FROM ' + table).fetchall()

    advertised = [tool.model_dump() for tool in await server.mcp.list_tools()]
    class Adapter:
        expected = None
        response = None
        def request(self, url, **kw):
            request = kw['payload']
            if request['method'] == 'notifications/initialized': return b''
            if request['method'] == 'initialize': value = {'protocolVersion':'2025-03-26'}
            elif request['method'] == 'tools/list': value = {'tools':advertised}
            elif request['method'] == 'tools/call':
                require(request['params'] == self.expected)
                value = self.response
            else: raise AssertionError('unexpected_method')
            return canonical({'jsonrpc':'2.0','id':request['id'],'result':value}).encode()

    adapter = Adapter()
    client = STClient({'url':'http://127.0.0.1:8794/mcp','key':'synthetic-migration-key-000000000000000','namespace':'Synthetic only'}, adapter)
    client.preflight()
    async def actual(tool, args):
        client.validate(tool,args)
        blocks,result = await server.mcp.call_tool(tool,args)
        require(json.loads(blocks[0].text)==result)
        if tool=='remember_tool_guidance' and result.get('decision')=='stored':
            # The existing tool-card store intentionally strips edge whitespace;
            # the migration's immutable local source preserves exact bytes.
            require(result['card']['content']['call_notes']==args['call_notes'].strip())
        adapter.expected={'name':tool,'arguments':args}
        adapter.response={'content':[block.model_dump() for block in blocks], 'structuredContent':result}
        return receipt(tool, client.write(tool,args))

    require(server.SIMPLE_MEMORY_ACCESS)
    denied = await actual('remember_memory',{'module':'learning_memory','content':'Synthetic before activation.'})
    require(denied == ('REJECTED',{'code':'module_one_required'}))
    require(not rows('self_model_revisions'))
    require(not rows('brain_wake_sessions'))

    # Existing test fixture performs the real three-wake activation solely in
    # this throwaway DB. The migration engine never calls this fixture or core.
    fixture=ModuleOneOnboardingTests()
    fixture.database,fixture.store=database,server.onboarding
    fixture.owner,fixture.model=server.OWNER_ID,server.MODEL_ID
    fixture.bootstrap_live()
    before=rows('self_model_revisions')
    wakes=rows('brain_wake_sessions')
    require(len(wakes)==3)
    source='b'*64
    for index,category in enumerate(('emotional_memory','learning_memory','planning_memory','tool_guidance')):
        chunk=Chunk(str(index)*64,index,0,'  Synthetic migration source, service DemoService.\n')
        classification={'category':category,'title':'Synthetic migration','summary':'External record, not verified.','keywords':['synthetic']}
        if category=='tool_guidance': classification.update(tool_name='DemoService',purpose='Read synthetic notes.',reminder='Look up notes when useful.')
        tool,args=write_projection(chunk,source,classification)
        state,saved=await actual(tool,args)
        require(state=='STORED')
        require(bool(saved.get('ref')))
        if category=='tool_guidance':
            state,duplicate=await actual(tool,args)
            require(state=='REJECTED' and duplicate.get('code')=='tool_card_exists')
    require(rows('self_model_revisions')==before)
    require(rows('brain_wake_sessions')==wakes)
    return {'passed':True,'checks':checks,'ordinary_categories_saved':4,
            'self_rows_unchanged':True,'ordinary_operations_created_wakes':0,
            'real_http_authentication_tested':False,'private_or_production_data_accessed':False}


async def guarded_probe():
    attempts=[]
    def deny(*args,**kwargs):
        attempts.append(True)
        raise AssertionError('network_forbidden_in_real_schema_probe')
    with ExitStack() as guard:
        for name in ('socket.socket.connect','socket.socket.connect_ex','socket.socket.sendto','socket.create_connection','socket.getaddrinfo'):
            guard.enter_context(patch(name,side_effect=deny))
        result=await probe()
    if attempts: raise AssertionError('network_attempt_swallowed')
    return result


class RealContractTests(unittest.TestCase):
    def test_real_simple_profile_schema_dispatch_and_receipts_no_self_changes(self):
        allowed={'PATH','SYSTEMROOT','WINDIR','COMSPEC','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA','PATHEXT','SYSTEMDRIVE','NUMBER_OF_PROCESSORS'}
        env={k:v for k,v in os.environ.items() if k.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix='migration-synthetic-contract-') as scratch:
            env.update(STBRAIN_ACCESS_PROFILE='simple-memory-v1',
                       STBRAIN_MCP_TOKEN='synthetic-token-000000000000000000000000',
                       STBRAIN_WAKE_SECRET='synthetic-wake-00000000000000000000000',
                       STBRAIN_OWNER_ID='synthetic-owner',STBRAIN_MODEL_ID='synthetic-model',
                       STBRAIN_DB_PATH=str(Path(scratch)/'main.db'),
                       STBRAIN_LEARNING_IDEA_DB_PATH=str(Path(scratch)/'ideas.db'),
                       STBRAIN_HALLUCINATION_VAULT_DB_PATH=str(Path(scratch)/'vault.db'),
                       STBRAIN_REQUIRE_EXECUTION_BINDING='1',STBRAIN_EXECUTION_EPOCH='synthetic-migration-epoch',
                       PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
            result=subprocess.run([sys.executable,'-B','-m','migration_ui.tests.test_real_contract','--probe'],
                    cwd=Path(__file__).resolve().parents[2],env=env,capture_output=True,text=True,encoding='utf-8',timeout=60)
        self.assertEqual(0,result.returncode,result.stderr)
        proof=json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(proof['passed'])
        self.assertEqual(4,proof['ordinary_categories_saved'])
        self.assertTrue(proof['self_rows_unchanged'])
        self.assertFalse(proof['private_or_production_data_accessed'])


if __name__=='__main__':
    if sys.argv[1:]==['--probe']:print(json.dumps(asyncio.run(guarded_probe())))
    else:unittest.main()
