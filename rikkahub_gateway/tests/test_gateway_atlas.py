from __future__ import annotations

import asyncio
import copy
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from rikkahub_gateway.atlas import (
    AtlasGateway, CAPABILITIES, REGISTER, REVOKE, SNAPSHOT, project_response,
)
from rikkahub_gateway.server import GatewayApplication, _GatewayHandler
from rikkahub_gateway.tests.test_gateway import FakeControl, config
from runtime.atlas_device_grants import AtlasGrantError, capabilities


TOKEN = "orb_atlas_" + "A" * 43
VERIFIER = hashlib.sha256(TOKEN.encode()).hexdigest()
INTENT = {"schema": "orbis.st.atlas-register/1", "requestId": "1" * 32,
          "deviceId": "2" * 32, "verifier": VERIFIER}
REGISTERED = {"schema": "orbis.st.atlas-register-result/1", "requestId": "1" * 32,
              "grantId": "3" * 32, "status": "active", "scope": "atlas.metadata.read", "expiresAt": None}
REVOKED = {"schema": "orbis.st.atlas-revoke-result/1", "status": "revoked"}
REVOKE_REQUEST = {"schema": "orbis.st.atlas-revoke/1", "requestId": "4" * 32}
GRAPH = {"schema": "orbis.st.atlas/1", "generatedAt": "2026-09-26T00:00:00Z", "truncated": False,
         "stars": [{"id": "a" * 64, "type": "学习", "storedAt": "2026-09-25T00:00:00Z"},
                   {"id": "b" * 64, "type": "情感", "storedAt": "2026-09-25T00:00:00Z"}],
         "edges": [{"a": "a" * 64, "b": "b" * 64}]}


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay, self.closed = chunks, delay, False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


def response(value, status=200, headers=None, raw=None, stream=None):
    return httpx.Response(status, headers={"Content-Type": "application/json", **(headers or {})},
                          stream=stream or Chunks([json.dumps(value).encode() if raw is None else raw]))


class AtlasTransportTests(unittest.TestCase):
    def invoke(self, result, route=CAPABILITIES, payload=None, protected=()):
        self.requests = []
        def handle(request):
            self.requests.append(request)
            return result
        self.gateway = AtlasGateway("http://control.test", "synthetic-host-secret", protected_values=protected,
                                    transport=httpx.MockTransport(handle))
        return self.gateway.request(route, payload or {})

    def test_discovery_enabled_and_disabled_exact_shapes(self):
        for enabled in (True, False):
            self.assertEqual(capabilities(enabled), self.invoke(response(capabilities(enabled))))
        request = self.requests[0]
        self.assertEqual("http://control.test/v1/host/atlas/capabilities", str(request.url))
        self.assertEqual("POST", request.method)
        self.assertEqual("Bearer synthetic-host-secret", request.headers["Authorization"])
        self.assertEqual("identity", request.headers["Accept-Encoding"])
        self.assertNotIn("Cookie", request.headers)
        self.assertEqual({}, json.loads(request.content))

    def test_registration_preserves_intent_only_verifier(self):
        self.assertEqual(REGISTERED, self.invoke(response(REGISTERED), REGISTER, INTENT))
        self.assertEqual(INTENT, json.loads(self.requests[0].content))
        self.assertNotIn(TOKEN, self.requests[0].content.decode())

    def test_revoked_registration_is_not_reported_active(self):
        value = {**REGISTERED, "status": "revoked"}
        self.assertEqual(value, self.invoke(response(value), REGISTER, INTENT))

    def test_snapshot_and_revoke_shapes(self):
        self.assertEqual(GRAPH, self.invoke(response(GRAPH), SNAPSHOT, {"verifier": VERIFIER}))
        self.assertEqual(REVOKED, self.invoke(response(REVOKED), REVOKE, {**REVOKE_REQUEST, "verifier": VERIFIER}))

    def test_internal_invalid_verifier_rejected_before_transport(self):
        for route in (REGISTER, SNAPSHOT, REVOKE):
            for payload in ({}, {"verifier": None}, {"verifier": []}, {"verifier": "not-a-hash"}):
                with self.assertRaises(AtlasGrantError) as raised:
                    self.invoke(response({}), route, payload)
                self.assertEqual((400, "invalid_request"), (raised.exception.status, raised.exception.code))
                self.assertEqual([], self.requests)

    def test_redirect_never_followed(self):
        with self.assertRaises(AtlasGrantError):
            self.invoke(response({}, 302, {"Location": "https://steal.invalid"}))
        self.assertEqual(1, len(self.requests))

    def test_fixed_error_passthrough_but_no_arbitrary_details(self):
        for status, code in ((403, "atlas_disabled"), (401, "unauthorized"),
                             (409, "request_conflict"), (429, "grant_limit_reached")):
            with self.assertRaises(AtlasGrantError) as raised:
                self.invoke(response({"error": {"code": code}}, status))
            self.assertEqual((status, code), (raised.exception.status, raised.exception.code))
        with self.assertRaises(AtlasGrantError) as raised:
            self.invoke(response({"error": {"code": "unauthorized", "detail": "secret"}}, 401))
        self.assertEqual("atlas_unavailable", raised.exception.code)

    def test_duplicate_json_unknown_fields_and_wrong_types_fail(self):
        values = [{}, {**capabilities(True), "enabled": 1}, {**capabilities(True), "body": "private"}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(AtlasGrantError):
                self.invoke(response(value))
        for raw in (b'{"x":1,"x":2}', b'[]', b'{"x":NaN}', b'[' * 1200, b'\xff'):
            with self.subTest(raw=raw[:20]), self.assertRaises(AtlasGrantError):
                self.invoke(response({}, raw=raw))

    def test_content_encoding_and_non_json_rejected(self):
        for headers in ({"Content-Encoding": "gzip"}, {"Content-Type": "text/html"},
                        {"Content-Length": "5000"}, {"Content-Length": "-1"}, {"Content-Length": "2,2"}):
            with self.subTest(headers=headers), self.assertRaises(AtlasGrantError):
                self.invoke(response(capabilities(True), headers=headers))

    def test_chunked_size_bound_and_stream_closed(self):
        stream = Chunks([b" " * 4096, b"x"])
        with self.assertRaises(AtlasGrantError):
            self.invoke(response({}, stream=stream))
        self.assertTrue(stream.closed)

    def test_total_deadline_cancels_slow_stream(self):
        stream = Chunks([b"{"], delay=10)
        start = time.monotonic()
        with patch("rikkahub_gateway.atlas.TIMEOUT_SECONDS", 0.03), self.assertRaises(AtlasGrantError):
            self.invoke(response({}, stream=stream))
        self.assertLess(time.monotonic() - start, 1)
        self.assertTrue(stream.closed)

    def test_protected_verifier_rejected_before_transport(self):
        intent = {**INTENT, "verifier": hashlib.sha256(b"synthetic-gateway-secret").hexdigest()}
        with self.assertRaises(AtlasGrantError):
            self.invoke(response(REGISTERED), REGISTER, intent, ("synthetic-gateway-secret",))
        self.assertEqual([], self.requests)

    def test_atlas_shaped_high_privilege_secrets_cannot_read_or_revoke(self):
        for route in (REGISTER, SNAPSHOT, REVOKE):
            with self.subTest(route=route), self.assertRaises(AtlasGrantError) as raised:
                self.invoke(response(REGISTERED), route, {**INTENT, "verifier": VERIFIER}, (TOKEN,))
            self.assertEqual((401, "unauthorized"), (raised.exception.status, raised.exception.code))
            self.assertEqual([], self.requests)

    def test_registration_identity_response_mismatch_and_extra_fields_rejected(self):
        for changes in ({"requestId": "f" * 32}, {"grantId": "x"}, {"expiresAt": "tomorrow"},
                        {"scope": "all"}, {"owner": "private"}, {"status": "pending"}):
            with self.subTest(changes=changes), self.assertRaises(AtlasGrantError):
                self.invoke(response({**REGISTERED, **changes}), REGISTER, INTENT)

    def test_graph_rejects_body_fields_invalid_ids_edges_and_types(self):
        values = []
        for key in ("body", "summary", "title", "owner_id"):
            value = copy.deepcopy(GRAPH); value["stars"][0][key] = "private"; values.append(value)
        for key, val in (("id", "raw-id"), ("type", "private"), ("storedAt", "now")):
            value = copy.deepcopy(GRAPH); value["stars"][0][key] = val; values.append(value)
        for edge in ({"a": "a" * 64, "b": "c" * 64}, {"a": "a" * 64, "b": "a" * 64},
                     {"a": "a" * 64, "b": "b" * 64, "body": "private"}):
            value = copy.deepcopy(GRAPH); value["edges"] = [edge]; values.append(value)
        value = copy.deepcopy(GRAPH); value["stars"].append(value["stars"][0]); values.append(value)
        value = copy.deepcopy(GRAPH); value["edges"] *= 2; values.append(value)
        values.append({**GRAPH, "truncated": 1})
        values.append({**GRAPH, "generatedAt": "secret"})
        for value in values:
            with self.subTest(value=value), self.assertRaises(AtlasGrantError):
                project_response(SNAPSHOT, value, {})


class AtlasHttpTests(unittest.TestCase):
    def setUp(self):
        self.config = config()
        self.requests = []
        self.control = FakeControl()
        self.model_requests = []
        def upstream(request):
            self.model_requests.append(request)
            raise AssertionError("model requests forbidden")
        def control(request):
            self.requests.append(request)
            values = {"/v1/host/atlas/capabilities": capabilities(True), "/v1/host/atlas/register": REGISTERED,
                      "/v1/host/atlas/snapshot": GRAPH, "/v1/host/atlas/revoke": REVOKED}
            return response(values[request.url.path])
        self.app = GatewayApplication(self.config, control=self.control,
                                      upstream=httpx.Client(transport=httpx.MockTransport(upstream)),
                                      atlas_transport=httpx.MockTransport(control))
        self.session = self.app._current_session
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        self.server.application = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.app.upstream.close()
        self.assertEqual([], self.control.calls)
        self.assertEqual([], self.model_requests)
        self.assertIs(self.session, self.app._current_session)

    def call(self, path=CAPABILITIES, method="GET", token=None, value=None, extra=(), raw=None):
        body = json.dumps(value).encode() if raw is None and value is not None else (raw or b"")
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        client.putrequest(method, path)
        if token is not None:
            client.putheader("Authorization", "Bearer " + token)
        if method == "POST":
            client.putheader("Content-Type", "application/json")
            client.putheader("Content-Length", str(len(body)))
        for key, val in extra:
            client.putheader(key, val)
        client.endheaders(body)
        reply = client.getresponse()
        result = reply.status, dict(reply.getheaders()), json.loads(reply.read())
        client.close()
        return result

    def test_discovery_does_not_require_key_and_no_store(self):
        status, headers, value = self.call()
        self.assertEqual(200, status); self.assertEqual(capabilities(True), value)
        self.assertEqual("no-store", headers["Cache-Control"])

    def test_registration_uses_only_gateway_key_and_no_raw_secret_forwarding(self):
        status, _, value = self.call(REGISTER, "POST", self.config.gateway_token, INTENT)
        self.assertEqual((200, REGISTERED), (status, value))
        request, = self.requests
        self.assertEqual(INTENT, json.loads(request.content))
        self.assertNotIn(self.config.gateway_token, str(request.headers))

    def test_snapshot_and_revoke_use_device_token_not_gateway_key(self):
        self.assertEqual(200, self.call(SNAPSHOT, token=TOKEN)[0])
        self.assertEqual({"schema": "orbis.st.atlas-snapshot/1", "verifier": VERIFIER},
                         json.loads(self.requests[-1].content))
        self.assertEqual(200, self.call(REVOKE, "POST", TOKEN, REVOKE_REQUEST)[0])
        self.assertEqual({**REVOKE_REQUEST, "verifier": VERIFIER}, json.loads(self.requests[-1].content))
        self.assertNotIn(TOKEN, str(self.requests[-1].headers))

    def test_wrong_roles_never_reach_control(self):
        for token in (None, "wrong", TOKEN, self.config.host_token, self.config.upstream_api_key):
            with self.subTest(token=token):
                self.assertEqual(401, self.call(REGISTER, "POST", token, INTENT)[0])
        for token in (None, self.config.gateway_token, self.config.host_token, self.config.upstream_api_key):
            with self.subTest(token=token):
                self.assertEqual(401, self.call(SNAPSHOT, token=token)[0])
        self.assertEqual([], self.requests)

    def test_duplicate_auth_framing_origin_and_queries_rejected(self):
        for extra in ((('Authorization', 'Bearer '+TOKEN),), (("Origin", "https://evil.invalid"),), (("Cookie", "session=synthetic"),),
                      (("Content-Length", "1"),), (("Transfer-Encoding", "chunked"),),
                      (("Content-Encoding", "gzip"),)):
            with self.subTest(extra=extra):
                self.assertIn(self.call(SNAPSHOT, token=TOKEN, extra=extra)[0], (400, 401))
        for suffix in ("?", "?x=1", "#fragment"):
            self.assertEqual(400, self.call(CAPABILITIES + suffix)[0])
        self.assertEqual([], self.requests)

    def test_post_duplicate_length_and_duplicate_json_rejected(self):
        self.assertEqual(400, self.call(REGISTER, "POST", self.config.gateway_token, INTENT,
                                      extra=(("Content-Length", "1"),))[0])
        self.assertEqual(400, self.call(REGISTER, "POST", self.config.gateway_token,
                                      raw=b'{"schema":"x","schema":"y"}')[0])
        self.assertEqual([], self.requests)

    def test_unknown_fields_and_oversized_requests_rejected(self):
        self.assertEqual(400, self.call(REGISTER, "POST", self.config.gateway_token,
                                      {**INTENT, "ownerId": "other"})[0])
        self.assertEqual(413, self.call(REGISTER, "POST", self.config.gateway_token, raw=b" " * 2049)[0])
        self.assertEqual([], self.requests)

    def test_wrong_methods_do_not_forward(self):
        self.assertEqual(405, self.call(REGISTER)[0])
        self.assertEqual(405, self.call(CAPABILITIES, "POST", value={})[0])
        self.assertEqual([], self.requests)

    def test_busy_not_queued_and_unrelated_legacy_route_unchanged(self):
        self.app.atlas._slots.acquire(); self.app.atlas._slots.acquire()
        try:
            self.assertEqual(503, self.call()[0])
        finally:
            self.app.atlas._slots.release(); self.app.atlas._slots.release()
        self.assertEqual(200, self.call("/v1/models", token=self.config.gateway_token)[0])
        self.assertEqual([], self.requests)


if __name__ == "__main__":
    unittest.main()
