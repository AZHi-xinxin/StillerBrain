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
    from tests.test_onboarding import ModuleOneOnboardingTests
    checks=[]
    def check(value,name):
        if not value: raise AssertionError(name)
        checks.append(name)
    async def call(name,args):
        blocks,result=await server.mcp.call_tool(name,args)
        check(json.loads(blocks[0].text)==result,'structured:'+name)
        return result
    rejected=await call('stbrain_manage',{'action':'memory_force_recall','arguments':{'action':'enable','target_ref':'emotion://missing@1'}})
    check(rejected['decision']=='reject','initial activation gate')
    fixture=ModuleOneOnboardingTests()
    fixture.database,fixture.store=Path(os.environ['STBRAIN_DB_PATH']),server.onboarding
    fixture.owner,fixture.model=server.OWNER_ID,server.MODEL_ID
    fixture.bootstrap_live()
    # Compact discovery remains single digits; this is an action behind the facade.
    from mcp import types
    with patch('mcp_server.tool_catalog_profile._request_tool_profile',return_value='daily'):
        catalog=await server.mcp._mcp_server.request_handlers[types.ListToolsRequest](types.ListToolsRequest(method='tools/list'))
    names={tool.name for tool in catalog.root.tools}
    check(len(names)<10,'small public catalog')
    check('memory_force_recall' not in names,'no extra top-level tool')
    detail=await call('stbrain_tools',{'category':'force_recall'})
    check('memory_force_recall' in detail['actions'],'discover action')
    saved=await call('remember_memory',{'content':'合成测试的普通记忆','keywords':['ex']})
    check(saved.get('stored'),'save source')
    ref=saved['ref']
    disabled=await call('stbrain_manage',{'action':'memory_force_recall','arguments':{'action':'read','target_ref':ref}})
    check(disabled['item']['force_recall'] is False,'default off')
    result=await call('stbrain_manage',{'action':'memory_force_recall','arguments':{'action':'enable','target_ref':ref,'expected_revision':0}})
    check(result['decision']=='saved' and result['item']['force_recall'],'AI can enable')
    check(result['parent_memory_changed'] is False,'only metadata changes')
    listed=await call('stbrain_manage',{'action':'memory_force_recall','arguments':{'action':'list'}})
    check(listed['enabled_count']==1 and listed['items'][0]['hit_count']==0,'real metadata receipt')
    conflict=await call('stbrain_manage',{'action':'memory_force_recall','arguments':{'action':'disable','target_ref':ref,'expected_revision':0}})
    check(conflict['reason_code']=='force_recall_revision_conflict','metadata CAS')
    disabled=await call('stbrain_manage',{'action':'memory_force_recall','arguments':{'action':'disable','target_ref':ref,'expected_revision':1}})
    check(disabled['item']['force_recall'] is False,'AI can disable')
    return {'decision':'PASS','count':len(checks),'checks':checks}


async def isolated_probe():
    def deny(*args, **kwargs):
        raise AssertionError("network-forbidden")
    with ExitStack() as guard:
        for name in ("socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto", "socket.create_connection", "socket.getaddrinfo"):
            guard.enter_context(patch(name, side_effect=deny))
        return await probe()


class ForceRecallTransportTests(unittest.TestCase):
    def test_real_registered_tools_and_recall(self):
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PATHEXT", "SYSTEMDRIVE"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix="force-recall-synthetic-") as scratch:
            env.update({"STBRAIN_ACCESS_PROFILE": "simple-memory-v1", "STBRAIN_MCP_TOKEN": "synthetic-token-000000000000000000000000",
                "STBRAIN_WAKE_SECRET": "synthetic-wake-00000000000000000000000", "STBRAIN_OWNER_ID": "synthetic-owner",
                "STBRAIN_MODEL_ID": "synthetic-model", "STBRAIN_DB_PATH": str(Path(scratch) / "main.db"),
                "STBRAIN_LEARNING_IDEA_DB_PATH": str(Path(scratch) / "ideas.db"), "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(Path(scratch) / "vault.db"),
                "STBRAIN_REQUIRE_EXECUTION_BINDING": "1", "STBRAIN_EXECUTION_EPOCH": "synthetic-emotion-epoch",
                "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
            completed = subprocess.run([sys.executable, "-B", "-m", "mcp_server.tests.test_force_recall_transport", "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"])
        print(json.dumps(proof, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(isolated_probe()), ensure_ascii=False))
    else:
        unittest.main()
