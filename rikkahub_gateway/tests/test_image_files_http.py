import base64
import hashlib
import http.client
import json
import threading
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import httpx

from rikkahub_gateway.image_files import ImageFiles, TTL
from rikkahub_gateway.server import GatewayApplication, GatewayError, _GatewayHandler
from rikkahub_gateway.tests.test_gateway import config, FakeControl
from rikkahub_gateway.tests import test_gateway as fixtures


class ImageFilesHttpTests(unittest.TestCase):
    def setUp(self):
        self.clock = 2000000000
        self.uploads = []
        self.config = replace(config(), upstream_base_url='https://api.deepseek.com/v1')
        self.control = FakeControl()
        self.app = GatewayApplication(self.config, control=self.control)
        def upload(request):
            self.uploads.append(request)
            return httpx.Response(200, json={'id':'file-api-'+str(len(self.uploads)), 'expires_at':self.clock+TTL})
        self.app.image_files = ImageFiles(self.config.gateway_token, clock=lambda:self.clock, transport=httpx.MockTransport(upload))
        self.server = ThreadingHTTPServer(('127.0.0.1',0), _GatewayHandler)
        self.server.daemon_threads = True
        self.server.application = self.app
        self.worker = threading.Thread(target=lambda:self.server.serve_forever(poll_interval=.01), daemon=True)
        self.worker.start()
        raw = b'\x89PNG\r\n\x1a\nsynthetic-image'
        self.digest = hashlib.sha256(raw).hexdigest()
        self.payload = {'model':'stiller-rikka','sha256':self.digest,'data':base64.b64encode(raw).decode(),'mime':'image/png'}
        self.auth = [('Authorization','Bearer '+self.config.gateway_token)]

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join(timeout=2)

    def post(self, body, headers=None, path='/v1/image-files'):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        conn = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=2)
        try:
            conn.putrequest('POST',path)
            for k,v in (self.auth if headers is None else headers):conn.putheader(k,v)
            conn.putheader('Content-Type','application/json');conn.putheader('Content-Length',str(len(raw)))
            conn.endheaders(raw)
            response = conn.getresponse()
            return response.status,json.loads(response.read())
        finally:conn.close()

    def test_authenticated_prepare_has_no_turn_or_model_side_effect(self):
        status, result = self.post(self.payload)
        self.assertEqual(200,status)
        self.assertTrue(result['references'][self.digest].startswith('st-image://'))
        status,again = self.post({'model':'stiller-rikka','digests':[self.digest]})
        self.assertEqual(200,status); self.assertEqual(result,again)
        self.assertEqual(1,len(self.uploads));self.assertEqual([],self.control.calls)
        self.assertIsNone(self.app._current_session)

    def test_auth_framing_duplicates_unknown_models_never_upload(self):
        for headers,path,body in [([], '/v1/image-files',self.payload),
                (self.auth*2,'/v1/image-files',self.payload),
                (self.auth+[('Transfer-Encoding','chunked')],'/v1/image-files',self.payload),
                (self.auth+[('Content-Length','1')],'/v1/image-files',self.payload),
                (self.auth,'/v1/image-files?unexpected=1',self.payload),
                (self.auth,'/v1/image-files',b'{"model":"stiller-rikka","model":"another"}'),
                (self.auth,'/v1/image-files',{**self.payload,'model':'unknown-model'})]:
            with self.subTest(path=path,headers=len(headers)):
                self.assertIn(self.post(body,headers,path)[0],(400,401,404))
        self.assertEqual([],self.uploads);self.assertEqual([],self.control.calls)

    def test_file_renewal_and_first_use_tool_preserve_real_continuation_checks(self):
        _,prepared = self.post(self.payload)
        ref = prepared['references'][self.digest]
        user = {'role':'user','content':[{'type':'text','text':'synthetic tool test'},
            {'type':'image_url','image_url':{'url':ref}}]}
        tools = fixtures.GatewayTests.bound_tools()
        headers = {'Authorization':'Bearer '+self.config.gateway_token,'X-ST-Thread-ID':'synthetic-image-chain'}
        first = self.app.prepare_turn(fixtures.GatewayTests.request([user],tools=tools),headers)
        before = json.loads(self.app.encode_upstream_payload(first.payload,route=first.route))
        self.assertIn('file-api-1',json.dumps(before));self.assertNotIn('st-image://',json.dumps(before))
        call = fixtures.GatewayTests.native_call('new-call','read only synthetic data')
        bindings = self.app.bind_response_tool_calls(first,[call])
        with patch.object(self.app,'_schedule_tool_result_wait'):
            self.app.finish_turn(first,keep_for_tools=True,tool_call_ids=[call.tool_call_id],tool_call_bindings=bindings)
        self.clock += TTL-120
        self.assertEqual(ref,self.post(self.payload)[1]['references'][self.digest])
        history = [user,{'role':'assistant','tool_calls':[fixtures.GatewayTests.wire_call(call)]},
            {'role':'tool','tool_call_id':call.tool_call_id,'content':'{"ok":true}'}]
        continued = self.app.prepare_turn(fixtures.GatewayTests.request(history,tools=tools),headers)
        after = json.loads(self.app.encode_upstream_payload(continued.payload,route=continued.route))
        self.assertIn('file-api-2',json.dumps(after));self.assertNotIn('st-image://',json.dumps(after))
        self.assertEqual(ref,user['content'][1]['image_url']['url'])
        self.assertEqual(2,len(self.uploads))
        self.app.finish_turn(continued,keep_for_tools=False)


if __name__ == '__main__': unittest.main()
