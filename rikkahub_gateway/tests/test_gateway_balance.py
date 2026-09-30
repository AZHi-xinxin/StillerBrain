from __future__ import annotations

import asyncio
import copy
import http.client
import json
import threading
import time
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import httpx

from rikkahub_gateway.balance import (
    BALANCE_URL, MAX_BALANCE_BYTES, BalanceError, read_balance,
)
from rikkahub_gateway.server import GatewayApplication, GatewayError, _GatewayHandler
from rikkahub_gateway.tests.test_gateway import FakeControl, config


KEY = "synthetic-official-upstream-key-only"
VALID = {"is_available": True, "balance_infos": [{
    "currency": "CNY", "total_balance": "110.00",
    "granted_balance": "10.00", "topped_up_balance": "100.00",
}]}


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


def response(value=VALID, *, status=200, headers=None, raw=None, stream=None):
    return httpx.Response(status, headers={"Content-Type": "application/json", **(headers or {})},
                          stream=stream or Chunks([raw if raw is not None else json.dumps(value).encode()]))


class BalanceTransportTests(unittest.TestCase):
    def read(self, reply, *, base="https://api.deepseek.com/v1", key=KEY, protected=()):
        self.requests = []
        def handle(request):
            self.requests.append(request)
            return reply
        return read_balance(base, key, protected_values=protected, transport=httpx.MockTransport(handle))

    def reject(self, reply, code="balance_invalid_response", status=502):
        with self.assertRaises(BalanceError) as raised:
            self.read(reply)
        self.assertEqual((status, code), (raised.exception.status, raised.exception.code))

    def test_success_uses_fixed_url_get_and_only_server_credential(self):
        self.assertEqual(VALID, self.read(response()))
        request, = self.requests
        self.assertEqual("GET", request.method)
        self.assertEqual(BALANCE_URL, str(request.url))
        self.assertEqual("Bearer " + KEY, request.headers["Authorization"])
        self.assertEqual(b"", request.content)
        self.assertEqual("identity", request.headers["Accept-Encoding"])
        self.assertNotIn("Cookie", request.headers)

    def test_official_base_variants_use_same_fixed_endpoint(self):
        for base in ("https://api.deepseek.com", "https://api.deepseek.com/", "https://api.deepseek.com/v1/", "https://api.deepseek.com:443/v1"):
            with self.subTest(base=base):
                self.assertEqual(VALID, self.read(response(), base=base))
                self.assertEqual(BALANCE_URL, str(self.requests[0].url))

    def test_nonofficial_or_ambiguous_base_never_reaches_transport(self):
        for base in ("http://api.deepseek.com/v1", "https://evil.invalid/v1", "https://api.deepseek.com.evil.invalid", "https://api.deepseek.com@evil.invalid", "https://user@api.deepseek.com", "https://api.deepseek.com:444", "https://api.deepseek.com/v1?url=x", "https://api.deepseek.com/#x", "https://api.deepseek.com/../v1", "https://api.deepseek.com./v1", "https://api.deepseek.com/v1\n", "https://api.deepseek.com/anthropic"):
            with self.subTest(base=base), self.assertRaises(BalanceError) as raised:
                self.read(response(), base=base)
            self.assertEqual("balance_official_upstream_required", raised.exception.code)
            self.assertEqual([], self.requests)

    def test_invalid_server_key_never_reaches_transport(self):
        for key in ("", "key\r\nX-Evil: yes", " key", "nonascii-密钥"):
            with self.subTest(key=key), self.assertRaises(BalanceError):
                self.read(response(), key=key)
            self.assertEqual([], self.requests)

    def test_redirect_never_forwards_credential_or_follows(self):
        self.reject(response(status=302, headers={"Location": "https://evil.invalid/steal"}), "balance_upstream_redirect_rejected")
        self.assertEqual(1, len(self.requests))

    def test_provider_auth_and_rate_errors_are_fixed_safe_codes(self):
        for status, expected_status, code in ((401,502,"balance_upstream_auth_failed"),(403,502,"balance_upstream_auth_failed"),(429,503,"balance_upstream_rate_limited"),(500,502,"balance_upstream_unavailable"),(204,502,"balance_upstream_unavailable")):
            with self.subTest(status=status):
                self.reject(response(status=status, raw=KEY.encode()), code, expected_status)

    def test_transport_exception_text_is_not_exposed(self):
        def fail(request):
            raise httpx.ConnectError("private: " + KEY, request=request)
        with self.assertRaises(BalanceError) as raised:
            read_balance("https://api.deepseek.com", KEY, transport=httpx.MockTransport(fail))
        self.assertEqual("balance_upstream_unavailable", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_header_wait_is_cancelled_by_total_deadline(self):
        cancelled = []
        async def wait(request):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)
        start=time.monotonic()
        with patch("rikkahub_gateway.balance.BALANCE_TIMEOUT_SECONDS", 0.03), self.assertRaises(BalanceError) as raised:
            read_balance("https://api.deepseek.com", KEY, transport=httpx.MockTransport(wait))
        self.assertEqual("balance_upstream_timeout", raised.exception.code)
        self.assertTrue(cancelled)
        self.assertLess(time.monotonic()-start, 1)

    def test_stream_read_is_cancelled_and_closed_by_total_deadline(self):
        stream=Chunks([b"{"], delay=10)
        with patch("rikkahub_gateway.balance.BALANCE_TIMEOUT_SECONDS", 0.03), self.assertRaises(BalanceError) as raised:
            self.read(response(stream=stream))
        self.assertEqual("balance_upstream_timeout", raised.exception.code)
        self.assertTrue(stream.closed)

    def test_declared_oversized_body_is_rejected_before_read(self):
        stream=Chunks([b"{}"], delay=10)
        self.reject(response(headers={"Content-Length": str(MAX_BALANCE_BYTES+1)},stream=stream), "balance_response_too_large")
        self.assertTrue(stream.closed)

    def test_chunked_oversized_body_is_rejected(self):
        stream=Chunks([b" "*MAX_BALANCE_BYTES, b"x"])
        self.reject(response(stream=stream), "balance_response_too_large")
        self.assertTrue(stream.closed)

    def test_content_type_and_compression_are_rejected(self):
        for headers in ({"Content-Type":"text/html"},{"Content-Encoding":"gzip"},{"Content-Encoding":"br"}):
            with self.subTest(headers=headers):self.reject(response(headers=headers))

    def test_invalid_json_and_duplicate_keys_are_rejected(self):
        for raw in (b"not-json",b"\xff",b'[]',b'{"is_available":true,"is_available":false,"balance_infos":[]}',b'['*1200+b']'*1200):
            with self.subTest(raw=raw[:20]):self.reject(response(raw=raw))

    def test_unknown_success_shape_is_not_zero(self):
        for value in ({},{"balance":0},{"data":{"total_usage":123}}, {"is_available":True,"balance_infos":[]}):
            with self.subTest(value=value):self.reject(response(value))

    def test_amounts_must_be_bounded_decimal_strings(self):
        for amount in (1, 0, None, True, "NaN", "Infinity", "1e3", " 1", "1 ", "0x10", "9"*19, KEY):
            value=copy.deepcopy(VALID);value["balance_infos"][0]["total_balance"]=amount
            with self.subTest(amount=amount):self.reject(response(value))

    def test_currency_and_available_types_are_strict(self):
        for currency in ("EUR", "usd", None, 1):
            value=copy.deepcopy(VALID);value["balance_infos"][0]["currency"]=currency
            with self.subTest(currency=currency):self.reject(response(value))
        value=copy.deepcopy(VALID);value["is_available"]=1
        self.reject(response(value))

    def test_zero_and_negative_balance_preserve_provider_truth(self):
        value=copy.deepcopy(VALID);value["is_available"]=False
        value["balance_infos"][0].update(total_balance="-0.10",granted_balance="0",topped_up_balance="0.00")
        self.assertEqual(value,self.read(response(value)))

    def test_two_distinct_currencies_are_not_summed(self):
        value=copy.deepcopy(VALID)
        second=copy.deepcopy(value["balance_infos"][0]);second["currency"]="USD"
        value["balance_infos"].append(second)
        self.assertEqual(value,self.read(response(value)))
        second["currency"]="CNY"
        self.reject(response(value))

    def test_unknown_provider_fields_are_not_exposed(self):
        value=copy.deepcopy(VALID);value["debug"]=KEY
        value["balance_infos"][0]["secret"]=KEY
        self.assertEqual(VALID,self.read(response(value)))

    def test_numeric_credential_cannot_be_echoed_as_amount(self):
        value=copy.deepcopy(VALID);value["balance_infos"][0]["total_balance"]="1234567890123456"
        with self.assertRaises(BalanceError):
            self.read(response(value), protected=("1234567890123456",))

    def test_proxy_environment_is_ignored(self):
        with patch.dict("os.environ", {"HTTPS_PROXY":"http://untrusted.invalid:9999","ALL_PROXY":"http://untrusted.invalid:9999"}):
            self.assertEqual(VALID,self.read(response()))


class GatewayBalanceWireTests(unittest.TestCase):
    def setUp(self):
        self.requests=[]
        def handle(request):
            self.requests.append(request)
            return response()
        self.control=FakeControl()
        self.config=replace(config(),upstream_base_url="https://api.deepseek.com/v1",upstream_api_key=KEY,human_token="synthetic-human-"+"h"*32)
        self.model_requests=[]
        def model(request):
            self.model_requests.append(request)
            raise AssertionError("balance_must_not_call_model")
        self.app=GatewayApplication(self.config,control=self.control,upstream=httpx.Client(transport=httpx.MockTransport(model)),balance_transport=httpx.MockTransport(handle))
        self.session=object()
        self.app._current_session=self.session
        self.server=ThreadingHTTPServer(("127.0.0.1",0),_GatewayHandler)
        self.server.application=self.app
        self.thread=threading.Thread(target=lambda:self.server.serve_forever(poll_interval=0.01),daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
        self.app.upstream.close()
        if self.app.human_control is not None:self.app.human_control.client.close()
        self.assertEqual([],self.control.calls)
        self.assertEqual([],self.model_requests)
        self.assertIs(self.session,self.app._current_session)

    def get(self,path="/v1/user/balance",*,token=None,extra=(),body=b"",method="GET"):
        client=http.client.HTTPConnection("127.0.0.1",self.server.server_address[1],timeout=3)
        client.putrequest(method,path)
        if token is not False:client.putheader("Authorization","Bearer "+(self.config.gateway_token if token is None else token))
        for key,value in extra:client.putheader(key,value)
        client.endheaders(body)
        reply=client.getresponse()
        result=reply.status,dict(reply.getheaders()),json.loads(reply.read())
        client.close()
        return result

    def test_both_canonical_paths_are_authenticated_readonly_no_store(self):
        for path in ("/user/balance","/v1/user/balance"):
            status,headers,payload=self.get(path)
            self.assertEqual(200,status);self.assertEqual(VALID,payload)
            self.assertEqual("no-store",headers["Cache-Control"])
            self.assertEqual("Bearer "+KEY,self.requests[-1].headers["Authorization"])
            self.assertNotIn(self.config.gateway_token,str(self.requests[-1].headers))

    def test_missing_bad_human_host_and_upstream_auth_are_rejected(self):
        for token in (False,"wrong","invalid-é",self.config.host_token,self.config.human_token,KEY):
            with self.subTest(token=token):
                status,_,payload=self.get(token=token)
                self.assertEqual(401,status);self.assertEqual("unauthorized",payload["error"]["code"])
        self.assertEqual([],self.requests)

    def test_duplicate_auth_is_rejected(self):
        status,_,_=self.get(extra=(("Authorization","Bearer "+self.config.gateway_token),))
        self.assertEqual(401,status);self.assertEqual([],self.requests)

    def test_query_and_unread_body_are_rejected(self):
        for path,extra,body in (("/v1/user/balance?url=https://evil.invalid",(),b""),("/v1/user/balance",(("Content-Length","1"),),b"x"),("/v1/user/balance",(("Transfer-Encoding","chunked"),),b"0\r\n\r\n")):
            with self.subTest(path=path,extra=extra):self.assertEqual(400,self.get(path,extra=extra,body=body)[0])
        self.assertEqual([],self.requests)

    def test_legacy_credits_remains_404_without_false_usage_alias(self):
        for path in ("/credits","/v1/credits"):
            self.assertEqual(404,self.get(path)[0])
        self.assertEqual([],self.requests)

    def test_post_does_not_invoke_balance(self):
        self.assertEqual(404,self.get(method="POST")[0]);self.assertEqual([],self.requests)

    def test_busy_fails_without_queuing_or_upstream_request(self):
        self.app._balance_slots.acquire();self.app._balance_slots.acquire()
        try:
            status,_,payload=self.get()
            self.assertEqual(503,status);self.assertEqual("balance_busy",payload["error"]["code"])
            self.assertEqual([],self.requests)
        finally:self.app._balance_slots.release();self.app._balance_slots.release()

    def test_safe_upstream_error_does_not_close_or_replace_wake(self):
        with patch("rikkahub_gateway.server.read_balance",side_effect=BalanceError(504,"balance_upstream_timeout")):
            status,_,payload=self.get()
        self.assertEqual(504,status);self.assertEqual("balance_upstream_timeout",payload["error"]["code"])
        self.assertEqual([],self.requests)


if __name__ == "__main__":
    unittest.main()
