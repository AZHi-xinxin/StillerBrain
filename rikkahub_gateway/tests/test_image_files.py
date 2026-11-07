import base64
import copy
import hashlib
import json
import unittest
from dataclasses import replace
import httpx

from rikkahub_gateway.image_files import ImageFiles, ImageFilesError, TTL, PREFIX
from rikkahub_gateway.routes import GatewayRoute


class ImageFilesTests(unittest.TestCase):
    def setUp(self):
        self.now = 2000000000
        self.sent = []
        self.route = GatewayRoute('visible', 'https://api.deepseek.com/v1', 'deepseek-v4-flash', 'synthetic-only-key')
        def handle(request):
            self.sent.append(request)
            return httpx.Response(200, json={'id': 'file-api-test'+str(len(self.sent)), 'expires_at': self.now+TTL})
        self.cache = ImageFiles('synthetic-server-secret', transport=httpx.MockTransport(handle), clock=lambda: self.now)
        self.raw = b'\x89PNG\r\n\x1a\n' + b'synthetic bytes, not private imagery'
        self.digest = hashlib.sha256(self.raw).hexdigest()
        self.upload = {'model':'visible','sha256':self.digest,'data':base64.b64encode(self.raw).decode(),'mime':'image/png'}
        self.probe = {'model':'visible','digests':[self.digest]}

    def prepare(self):
        return self.cache.prepare(self.route, self.upload)['references'][self.digest]

    def payload(self, ref):
        return {'model':'deepseek-v4-flash','messages':[{'role':'user','content':[
            {'type':'text','text':'What is in this image?'}, {'type':'image_url','image_url':{'url':ref}}]}]}

    def test_upload_once_original_bytes_ttl_and_small_file_projection(self):
        self.assertEqual({},self.cache.prepare(self.route,self.probe)['references'])
        ref=self.prepare()
        self.assertTrue(ref.startswith(PREFIX))
        self.assertEqual(ref,self.prepare())
        self.assertEqual(ref,self.cache.prepare(self.route,self.probe)['references'][self.digest])
        self.assertEqual(1,len(self.sent))
        req=self.sent[0]
        self.assertEqual('https://api.deepseek.com/files',str(req.url))
        self.assertEqual('Bearer synthetic-only-key',req.headers['Authorization'])
        self.assertIn(self.raw,req.content)
        self.assertIn(b'expires_after[seconds]',req.content)
        self.assertIn(str(TTL).encode(),req.content)
        payload=self.payload(ref);before=copy.deepcopy(payload)
        projected=self.cache.project(payload,self.route)
        self.assertEqual(before,payload)
        self.assertEqual({'type':'file','file_id':'file-api-test1'},projected['messages'][0]['content'][1])

    def test_expiry_refresh_keeps_handle_and_turn_input_identical(self):
        ref=self.prepare();payload=self.payload(ref)
        self.now += TTL-120
        self.assertEqual({},self.cache.prepare(self.route,self.probe)['references'])
        self.assertEqual(ref,self.prepare())
        self.assertEqual(2,len(self.sent))
        self.assertEqual('file-api-test2',self.cache.project(payload,self.route)['messages'][0]['content'][1]['file_id'])
        self.assertEqual(ref,payload['messages'][0]['content'][1]['image_url']['url'])

    def test_account_route_rotation_cannot_resolve_old_handle(self):
        ref=self.prepare()
        for other in (replace(self.route,api_key='other-key'),replace(self.route,upstream_base_url='https://evil.invalid')):
            with self.assertRaises(ImageFilesError):self.cache.project(self.payload(ref),other)
        same_account=replace(self.route,public_model='another-alias',upstream_model='deepseek-v4-pro')
        self.assertEqual('file-api-test1',self.cache.project(self.payload(ref),same_account)['messages'][0]['content'][1]['file_id'])

    def test_unknown_expired_and_restarted_references_fail_without_model_request(self):
        ref=self.prepare();self.now += TTL
        with self.assertRaises(ImageFilesError):self.cache.project(self.payload(ref),self.route)
        with self.assertRaises(ImageFilesError):self.cache.project(self.payload(PREFIX+'f'*64),self.route)

    def test_nonofficial_no_upload_and_bad_inputs_cannot_poison_cache(self):
        self.assertFalse(self.cache.prepare(replace(self.route,upstream_base_url='https://elsewhere.invalid'),self.upload)['supported'])
        for changes in ({'sha256':'a'*64},{'data':'not base64'},{'mime':'text/html'},{'url':'https://elsewhere.invalid'}, {'digests':['bad']}):
            with self.assertRaises(ImageFilesError):self.cache.prepare(self.route,{**self.upload,**changes})
        self.assertEqual([],self.sent)

    def test_redirect_errors_and_unbounded_provider_ids_are_not_forwarded(self):
        for response in (httpx.Response(302,headers={'Location':'https://elsewhere.invalid'}),
                         httpx.Response(500,text='private credential'),
                         httpx.Response(200,json={'id':'bad','expires_at':self.now+TTL}),
                         httpx.Response(200,json={'id':'file-api-test','expires_at':self.now+TTL*2}),
                         httpx.Response(200,text='x'*17000)):
            cache=ImageFiles('s',transport=httpx.MockTransport(lambda request:response),clock=lambda:self.now)
            with self.assertRaises(ImageFilesError) as raised:cache.prepare(self.route,self.upload)
            self.assertNotIn('credential',str(raised.exception))
            self.assertEqual({},cache._entries)

    def test_capacity_does_not_evict_a_live_conversation_reference(self):
        self.cache._capacity=1;ref=self.prepare()
        raw=self.raw+b'2'
        with self.assertRaises(ImageFilesError):
            self.cache.prepare(self.route,{**self.upload,'sha256':hashlib.sha256(raw).hexdigest(),'data':base64.b64encode(raw).decode()})
        self.assertEqual('file-api-test1',self.cache.project(self.payload(ref),self.route)['messages'][0]['content'][1]['file_id'])

    def test_normal_content_remote_images_and_tools_are_unchanged(self):
        payload={'messages':[{'role':'tool','content':'a tool result'}, {'role':'user','content':[{'type':'image_url','image_url':{'url':'https://existing.invalid/image.png'}}]}], 'tools':[{'name':'already-known'}]}
        self.assertIs(payload,self.cache.project(payload,self.route))

if __name__=='__main__':unittest.main()
