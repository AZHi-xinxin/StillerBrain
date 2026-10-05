"""All content/keys/endpoints are synthetic; no external services are used."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from migration_ui.core import (Chunk, MAX_SOURCE, MigrationError, canonical, digest,
    endpoint, no_secrets, parse_source, receipt, strict_json, write_projection)
from migration_ui.clients import Classifier, NoRedirect, STClient, Transport, fetch_source
from migration_ui.engine import MigrationEngine
from migration_ui.server import make_server
from migration_ui.storage import PrivateLedger


def stored():
    return {"decision": "stored", "stored": True, "count": 1, "state_changed": True,
            "id": "learn_" + "a" * 32, "ref": "learning://learn_" + "a" * 32 + "@1", "version": 1}


def source_config(text="Synthetic memory."):
    return {"kind": "file", "format": "markdown", "base64": base64.b64encode(text.encode()).decode()}


class CoreTests(unittest.TestCase):
    def test_source_is_lossless_and_chunks_stable(self):
        text = "  header\n" + ("Long synthetic line.\n" * 240) + "tail  "
        sha, chunks = parse_source(text.encode(), "markdown")
        self.assertEqual(text, "".join(c.content for c in chunks))
        self.assertTrue(all(len(c.content) <= 2000 for c in chunks))
        self.assertEqual((sha, chunks), parse_source(text.encode(), "markdown"))
        self.assertEqual(list(range(len(chunks))), [i for i, _ in enumerate(chunks)])

    def test_mapping_accepts_only_explicit_records(self):
        _, chunks = parse_source(canonical({"records": [{"正文": "one", "other": "not scanned"}, {"正文": "two"}]}).encode(), "json", "正文")
        self.assertEqual(["one", "two"], [c.content for c in chunks])
        for bad in [b'{"private_config":"x"}', b'{"records":[{"other":"x"}]}', b'{"memories":[],"records":[]}']:
            with self.assertRaises(MigrationError):
                parse_source(bad, "json")

    def test_same_length_different_field_is_not_same_receipt_identity(self):
        raw=b'[{"content":"one","text":"two"}]'
        sha,a=parse_source(raw,'json','content')
        other_sha,b=parse_source(raw,'json','text')
        self.assertEqual(sha,other_sha)
        self.assertNotEqual(a[0].id,b[0].id)

    def test_duplicate_json_nan_depth_and_bytes_rejected(self):
        for bad in ['{"a":1,"a":2}', '[NaN]']:
            with self.assertRaises(MigrationError): strict_json(bad)
        for raw, fmt in [(b'\xff', 'markdown'), (b'x' * (MAX_SOURCE + 1), 'markdown'), (("[" * 40 + '"x"' + "]" * 40).encode(), 'json')]:
            with self.assertRaises(MigrationError): parse_source(raw, fmt)

    def test_urls_reject_credentials_redirect_schemes_and_remote_http(self):
        for value in ['file:///tmp/x', 'http://remote.example/a', 'https://u:p@host.example/', 'https://host.example/#x', 'https://host.example/?token=secretvalue', 'https://host.example/\nx']:
            with self.assertRaises(MigrationError): endpoint(value)
        self.assertEqual('http://127.0.0.1:1234/mcp', endpoint('http://127.0.0.1:1234/mcp'))
        self.assertEqual('https://host.example/export?page=1', endpoint('https://host.example/export?page=1', source=True))

    def test_private_http_requires_per_endpoint_explicit_consent(self):
        for url in ['http://192.168.2.9/mcp','http://10.3.2.1/export','http://172.16.0.9/model','http://100.124.1.2/mcp','http://[fd00::23]/mcp']:
            with self.assertRaises(MigrationError): endpoint(url)
            self.assertEqual(url,endpoint(url,trusted_private_http=True))
        for url in ['http://169.254.169.254/latest','http://8.8.8.8/mcp','http://example.invalid/mcp','http://172.32.0.1/mcp','http://100.128.0.1/mcp','http://[fe80::1]/mcp']:
            with self.assertRaises(MigrationError): endpoint(url,trusted_private_http=True)
        with self.assertRaises(MigrationError): endpoint('https://source.invalid/export?api_key=opaque',source=True)

    def test_writer_uses_original_not_model_content(self):
        original = '  Synthetic immutable source.\n'
        chunk = Chunk('a' * 64, 2, 123, original)
        tool, args = write_projection(chunk, 'b' * 64, {'category':'learning_memory', 'summary':'Model summary'})
        self.assertEqual('remember_memory', tool)
        self.assertEqual(original, args['content'])
        self.assertEqual('reported', args['source_basis'])
        self.assertIn('record=2;offset=123', args['reason'])
        self.assertNotIn('write_context_ref', args)
        with self.assertRaises(MigrationError):
            write_projection(chunk, 'b'*64, {'category':'learning_memory', 'content':'model replacement'})

    def test_self_and_free_tools_cannot_expand_write_scope(self):
        chunk = Chunk('a'*64,0,0,'Synthetic')
        for value in [{'category':'self_model'}, {'category':'learning_memory','tool':'activate'}, {'category':'learning_memory','write_context_ref':'forged'}]:
            with self.assertRaises(MigrationError): write_projection(chunk, 'b'*64, value)
        self.assertIsNone(write_projection(chunk,'b'*64,{'category':'unclassified'}))

    def test_tool_card_is_inert_source_bound_and_not_truncated(self):
        chunk = Chunk('a'*64,0,0,'中文服务 is for synthetic notes.')
        tool, args = write_projection(chunk,'b'*64,{'category':'tool_guidance','tool_name':'中文服务','purpose':'Read notes.'})
        self.assertEqual('remember_tool_guidance',tool)
        self.assertEqual(chunk.content,args['call_notes'])
        self.assertEqual('none',args['confirmation_policy'])
        self.assertEqual('medium',args['risk_level'])
        self.assertEqual('external_document',args['source_type'])
        with self.assertRaises(MigrationError): write_projection(chunk,'b'*64,{'category':'tool_guidance','tool_name':'invented','purpose':'x'})
        with self.assertRaises(MigrationError): write_projection(Chunk('a'*64,0,0,'Tool '+ 'x'*800),'b'*64,{'category':'tool_guidance','tool_name':'Tool','purpose':'x'})

    def test_credentials_are_rejected_not_rewritten(self):
        with self.assertRaises(MigrationError): no_secrets('Bearer ' + 's'*32)
        with self.assertRaises(MigrationError): no_secrets('content sensitiveExactValue end', ('sensitiveExactValue',))
        no_secrets('The key is stored in env STBRAIN_MCP_TOKEN.')

    def test_receipts_are_strict_and_tool_specific(self):
        self.assertEqual('STORED',receipt('remember_memory',stored())[0])
        for value in [None, {}, {'decision':'stored'}, {**stored(),'count':True}, {**stored(),'ref':'invented'}, {**stored(),'state_changed':False}]:
            self.assertEqual('UNKNOWN',receipt('remember_memory',value)[0])
        card={'decision':'stored','state_changed':True,'execution_performed':False,'card':{'card_id':'toolcard_'+'b'*32,'version':1}}
        self.assertEqual('STORED',receipt('remember_tool_guidance',card)[0])
        self.assertEqual('UNKNOWN',receipt('remember_memory',card)[0])
        self.assertEqual('REJECTED',receipt('remember_tool_guidance',{'decision':'reject','state_changed':False,'reason_codes':['tool_card_exists']})[0])


class FakeClassifier:
    calls = []
    result = {'category':'learning_memory'}
    def __init__(self,config,transport):
        self.url='https://model.invalid/v1/chat/completions'; self.key='synthetic-model-key'; self.prompt='';self.model='synthetic'
    def classify(self,chunk):
        self.calls.append(chunk)
        return self.result.copy()


class FakeST:
    calls=[]
    outcome=None
    before_write=None
    def __init__(self,config,transport):
        self.url='https://st.invalid/mcp';self.namespace='Synthetic target';self.key='synthetic-st-key'
    def preflight(self): pass
    def validate(self,tool,args):
        if tool not in {'remember_memory','remember_tool_guidance'}: raise AssertionError('bad tool')
    def write(self,tool,args):
        self.calls.append((tool,args))
        if self.before_write: self.before_write()
        if isinstance(self.outcome,Exception): raise self.outcome
        return self.outcome or stored()


class LedgerEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/'migration-ui'
        self.ledger=PrivateLedger(self.root)
        FakeClassifier.calls=[];FakeClassifier.result={'category':'learning_memory'}
        FakeST.calls=[];FakeST.outcome=None;FakeST.before_write=None
        self.engine=MigrationEngine(self.ledger,classifier_factory=FakeClassifier,st_factory=FakeST)

    def tearDown(self):
        if self.engine.worker: self.engine.worker.join(5)
        self.ledger.close();self.temp.cleanup()

    def run_job(self):
        self.engine.start({'prepared_id':self.engine.prepared[0],'consent':True,'model':{},'target':{}})
        self.engine.worker.join(5)
        self.assertFalse(self.engine.running)

    def test_consent_first_no_network(self):
        self.engine.prepare(source_config())
        with self.assertRaises(MigrationError): self.engine.start({'prepared_id':self.engine.prepared[0]})
        self.assertEqual([],FakeClassifier.calls);self.assertEqual([],FakeST.calls)

    def test_stored_skips_and_raw_is_retained(self):
        source=source_config('  Synthetic original.\n')
        prepared=self.engine.prepare(source)
        self.run_job();self.run_job()
        self.assertEqual(1,len(FakeST.calls))
        self.assertEqual('  Synthetic original.\n',FakeST.calls[0][1]['content'])
        self.assertEqual('SKIPPED_STORED',self.engine.snapshot()['rows'][0]['state'])
        self.assertEqual(base64.b64decode(source['base64']),self.ledger.db.execute('SELECT raw FROM sources WHERE sha=?',(prepared['source_sha'],)).fetchone()[0])
        self.assertNotIn(b'synthetic-st-key',self.ledger.path.read_bytes())
        self.assertNotIn(b'synthetic-model-key',self.ledger.path.read_bytes())

    def test_unknown_stops_and_never_replays(self):
        self.engine.prepare(source_config('x'*2100))
        FakeST.outcome=TimeoutError()
        self.run_job();self.run_job()
        self.assertEqual(1,len(FakeST.calls))
        self.assertEqual('UNKNOWN',self.engine.snapshot()['rows'][0]['state'])

    def test_failed_final_receipt_commit_cannot_replay_in_same_process(self):
        self.engine.prepare(source_config())
        real_mark=self.ledger.mark
        def fail_commit(scope,chunk,source,state,**kwargs):
            if state=='STORED': raise OSError('Synthetic full disk')
            return real_mark(scope,chunk,source,state,**kwargs)
        with patch.object(self.ledger,'mark',side_effect=fail_commit): self.run_job()
        self.assertEqual({'SENDING':1},self.ledger.summary())
        self.assertEqual(1,len(FakeST.calls))
        self.run_job()
        self.assertEqual(1,len(FakeST.calls))
        self.assertEqual('UNKNOWN',self.engine.snapshot()['rows'][0]['state'])

    def test_source_escaped_auth_echo_never_reaches_ledger_or_classifier(self):
        secret='opaque/source/key'
        for document in [{'records':[{'content':secret}]}, {'records':[{'content':'safe','metadata':secret}]}, {'records':[{'content':'safe',secret:'not secret'}]}]:
            raw=canonical(document).replace('/',r'\/').encode()
            self.assertNotIn(secret.encode(),raw)
            with patch.object(self.engine.transport,'request',return_value=raw):
                with self.assertRaises(MigrationError): self.engine.prepare({'kind':'url','url':'https://source.invalid/export','read_consent':True,'format':'json','key':secret})
        self.assertEqual(0,self.ledger.db.execute('SELECT count(*) FROM sources').fetchone()[0])
        self.assertEqual([],FakeClassifier.calls)
        self.assertEqual([],FakeST.calls)

    def test_interrupted_sending_becomes_unknown_on_restart(self):
        self.engine.prepare(source_config())
        chunk=self.engine.prepared[2][0]
        self.ledger.mark('scope',chunk,self.engine.prepared[1],'SENDING',payload_hash='hash')
        self.ledger.close();self.ledger=PrivateLedger(self.root);self.engine.ledger=self.ledger
        self.assertEqual('UNKNOWN',self.ledger.get('scope',chunk.id)['state'])
        with self.assertRaises(MigrationError): self.ledger.mark('scope',chunk,self.engine.prepared[1],'SENDING')

    def test_cancel_keeps_inflight_receipt_and_stops_next(self):
        self.engine.prepare(source_config('x'*2100))
        FakeST.before_write=lambda _: self.engine.cancel()
        self.run_job()
        self.assertEqual(1,len(FakeST.calls))
        self.assertEqual('STORED',self.engine.snapshot()['rows'][0]['state'])
        self.assertEqual('cancelled',self.engine.snapshot()['phase'])

    def test_bad_classification_retains_pending_no_write(self):
        self.engine.prepare(source_config())
        FakeClassifier.result={'category':'self_model'}
        self.run_job()
        self.assertEqual([],FakeST.calls)
        self.assertEqual('PENDING',self.engine.snapshot()['rows'][0]['state'])

    def test_module_one_rejection_stops_remaining(self):
        self.engine.prepare(source_config('x'*2100))
        FakeST.outcome={'decision':'reject','state_changed':False,'reason_codes':['module_one_required']}
        self.run_job()
        self.assertEqual(1,len(FakeST.calls))
        self.assertEqual('REJECTED',self.engine.snapshot()['rows'][0]['state'])

    def test_duplicate_process_and_unknown_directory_rejected(self):
        with self.assertRaises(MigrationError): PrivateLedger(self.root)
        wrong=Path(self.temp.name)/'other'
        with self.assertRaises(MigrationError): PrivateLedger(wrong)


class RPCTransport:
    def __init__(self,schema=None):self.requests=[];self.schema=schema or {'type':'object'}
    def request(self,url,**kw):
        value=kw['payload'];self.requests.append(value)
        if value['method']=='notifications/initialized': return b''
        if value['method']=='initialize': result={'protocolVersion':'2025-03-26'}
        elif value['method']=='tools/list': result={'tools':[{'name':name,'inputSchema':self.schema} for name in ['remember_memory','remember_tool_guidance']]}
        else: result={'structuredContent':stored(),'content':[]}
        return canonical({'jsonrpc':'2.0','id':value['id'],'result':result}).encode()


class ClientTests(unittest.TestCase):
    def client(self,transport): return STClient({'url':'https://st.invalid/mcp','key':'synthetic-value','namespace':'test'},transport)
    def test_stateless_mcp_handshake_and_fixed_tool(self):
        tr=RPCTransport();client=self.client(tr);client.preflight()
        self.assertEqual(['initialize','notifications/initialized','tools/list'],[r['method'] for r in tr.requests])
        self.assertEqual(stored(),client.write('remember_memory',{'module':'learning_memory','content':'Synthetic'}))
        with self.assertRaises(MigrationError):client.write('stbrain_manage',{})
        with self.assertRaises(MigrationError):client.write('remember_memory',{'module':'self_model'})

    def test_schema_cannot_make_network_requests(self):
        for field in ('$ref','$dynamicRef','$recursiveRef','$id'):
            with self.assertRaises(MigrationError):self.client(RPCTransport({'type':'object','properties':{'x':{field:'https://unapproved.invalid/schema'}}})).preflight()

    def test_legacy_required_context_is_not_fabricated(self):
        with self.assertRaises(MigrationError):self.client(RPCTransport({'type':'object','required':['write_context_ref']})).preflight()

    def test_source_read_consent_before_fetch(self):
        with self.assertRaises(MigrationError):fetch_source({'url':'https://source.invalid'},RPCTransport())

    def test_classifier_never_sends_credentials_or_tools(self):
        class Stub:
            request_body=None
            def request(self,url,**kw):
                self.request_body=kw['payload']
                return canonical({'choices':[{'message':{'content':'{"category":"learning_memory"}'}}]}).encode()
        tr=Stub();client=Classifier({'url':'https://model.invalid/chat/completions','key':'synthetic-value','model':'synthetic','prompt':'Classify conservatively.'},tr)
        self.assertEqual({'category':'learning_memory'},client.classify(Chunk('a'*64,0,0,'synthetic')))
        self.assertNotIn('tools',tr.request_body)
        self.assertNotIn('synthetic-value',canonical(tr.request_body))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        class Engine:
            def snapshot(self):return {'phase':'idle','running':False,'rows':[]}
            def prepare(self,data):return {'prepared':True}
        self.server=make_server(Engine())
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_address[1])
        self.html=urlopen(self.base).read().decode()
        self.token=self.html.split('name="migration-csrf" content="')[1].split('"')[0]
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join(2)
    def request(self,path,**headers):
        return urlopen(Request(self.base+path,headers=headers))
    def test_api_requires_csrf_and_rejects_host_origin_cross_site(self):
        for path,headers in [('/api/status',{}),('/api/status',{'X-Migration-CSRF':self.token,'Origin':'https://attacker.invalid'}),('/',{'Host':'attacker.invalid'}),('/',{'Sec-Fetch-Site':'cross-site'})]:
            with self.assertRaises(HTTPError) as error:self.request(path,**headers)
            self.assertEqual(403,error.exception.code)
            error.exception.close()
        self.assertEqual('idle',json.load(self.request('/api/status',**{'X-Migration-CSRF':self.token}))['phase'])
    def test_security_headers_and_no_external_assets(self):
        response=self.request('/')
        self.assertEqual('no-store',response.headers['Cache-Control'])
        self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
        self.assertNotIn('https://cdn',self.html)
        self.assertNotIn('localStorage', (Path(__file__).parents[1]/'static'/'app.js').read_text(encoding='utf-8'))
    def test_transport_does_not_follow_redirect(self):
        class Handler(BaseHTTPRequestHandler):
            calls=0
            def log_message(self,*args):pass
            def do_GET(self):
                Handler.calls+=1;self.send_response(302);self.send_header('Location','http://127.0.0.1:1/never');self.end_headers()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with self.assertRaises(MigrationError):Transport().request('http://127.0.0.1:'+str(server.server_address[1])+'/',method='GET')
            self.assertEqual(1,Handler.calls)
        finally:server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
