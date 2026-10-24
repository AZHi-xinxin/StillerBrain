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
        if not value:raise AssertionError(name)
        checks.append(name)
    async def call(name,args):
        blocks,result=await server.mcp.call_tool(name,args)
        check(json.loads(blocks[0].text)==result,'structured:'+name)
        return result
    rejected=await call('memory_desires',{'action':'put','memory_ref':'emotion://missing@1','text':'不可写'})
    check(rejected['decision']=='reject','activation gate')
    fixture=ModuleOneOnboardingTests()
    fixture.database,fixture.store=Path(os.environ['STBRAIN_DB_PATH']),server.onboarding
    fixture.owner,fixture.model=server.OWNER_ID,server.MODEL_ID
    fixture.bootstrap_live()
    tool=server.mcp._tool_manager.get_tool('remember_memory')
    check('desire' in tool.parameters['properties'] and 'desire' not in tool.parameters.get('required',[]),'optional schema')
    saved=await call('remember_memory',{'content':'记得广场的风铃','desire':'想再去听一次'})
    check(saved.get('stored') and saved.get('desire_attached'),'atomic create')
    ref=saved['ref']
    detail=await call('stbrain_tools',{'category':'desires'})
    check('memory_desires' in detail['actions'],'discovery')
    result=await call('stbrain_manage',{'action':'memory_desires','arguments':{'action':'read','memory_ref':ref}})
    check(result['desire']['text']=='想再去听一次','compact read')
    rejected=await call('memory_desires',{'action':'put','memory_ref':ref,'text':'不可覆盖','expected_revision':0})
    check(rejected['reason_code']=='desire_revision_conflict','CAS')
    draft=await call('memory_desires',{'action':'plan_draft','memory_ref':ref})
    check(not draft['state_changed'],'plan not falsely saved')
    plan=await call(draft['next_call']['tool'],draft['next_call']['arguments'])
    check(plan.get('stored') and plan['module']=='planning_memory','actual plan saved separately')
    archived=await call('memory_desires',{'action':'archive','memory_ref':ref})
    check(archived['desire']['state']=='archived' and archived['parent_memory_changed'] is False,'archive only note')
    await call('memory_desires',{'action':'restore','memory_ref':ref})
    await call('memory_desires',{'action':'delete','memory_ref':ref})
    check((await call('memory_desires',{'action':'list'}))['desires']==[],'delete current note')
    old=await call('remember_memory',{'content':'没有欲望也正常保存'})
    check(old.get('stored'),'old caller works')
    with closing(sqlite3.connect(fixture.database)) as db:
        check(db.execute('SELECT lifecycle FROM emotion_memories WHERE memory_id=?',(saved['id'],)).fetchone()[0]=='active','parent retained')
        before=db.execute('SELECT count(*) FROM emotion_memories').fetchone()[0]
    bad=await call('remember_memory',{'content':'不能半份写入','desire':'长'*201})
    check(bad['decision']=='reject','too long rejects before source write')
    with closing(sqlite3.connect(fixture.database)) as db:check(db.execute('SELECT count(*) FROM emotion_memories').fetchone()[0]==before,'no partial write')
    return {'decision':'PASS','count':len(checks),'checks':checks}


async def isolated_probe():
    def deny(*args, **kwargs):
        raise AssertionError("network-forbidden")
    with ExitStack() as guard:
        for name in ("socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto", "socket.create_connection", "socket.getaddrinfo"):
            guard.enter_context(patch(name, side_effect=deny))
        return await probe()


class MemoryDesireTransportTests(unittest.TestCase):
    def test_real_registered_tools_and_recall(self):
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PATHEXT", "SYSTEMDRIVE"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix="memory-desire-synthetic-") as scratch:
            env.update({"STBRAIN_ACCESS_PROFILE": "simple-memory-v1", "STBRAIN_MCP_TOKEN": "synthetic-token-000000000000000000000000",
                "STBRAIN_WAKE_SECRET": "synthetic-wake-00000000000000000000000", "STBRAIN_OWNER_ID": "synthetic-owner",
                "STBRAIN_MODEL_ID": "synthetic-model", "STBRAIN_DB_PATH": str(Path(scratch) / "main.db"),
                "STBRAIN_LEARNING_IDEA_DB_PATH": str(Path(scratch) / "ideas.db"), "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(Path(scratch) / "vault.db"),
                "STBRAIN_REQUIRE_EXECUTION_BINDING": "1", "STBRAIN_EXECUTION_EPOCH": "synthetic-emotion-epoch",
                "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
            completed = subprocess.run([sys.executable, "-B", "-m", "mcp_server.tests.test_memory_desire_transport", "--probe"],
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
