"""All endpoints, credentials, DNS and HTTP here are synthetic/offline."""
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from rikkahub_gateway.management import (
    RouteStore, ManagementError, public_endpoint, managed_stream,
    handle_management_request, _private_path, CAPABILITIES, LIST, UPSERT, REQUESTS,
)
from rikkahub_gateway.routes import GatewayRoute
from rikkahub_gateway.server import GatewayApplication, GatewayError
from rikkahub_gateway.tests.test_gateway import config, FakeControl
from rikkahub_gateway.tests import test_gateway as legacy

ADMIN = "synthetic-management-" + "a" * 40
KEY = "synthetic-provider-key-" + "b" * 40
DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def payload(**changes):
    return {"schema": "orbis.st.routes-upsert/1", "requestId": "1" * 32, "expectedRevision": 0,
            "publicModel": "managed/pro", "upstreamBaseUrl": "https://provider.invalid/v1",
            "upstreamModel": "provider-pro", "apiKey": KEY, **changes}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.path_guard = patch("rikkahub_gateway.management._private_path")
        self.path_guard.start()  # Cross-platform functional tests; real guard tested separately.
        self.dns = patch("rikkahub_gateway.management.socket.getaddrinfo", return_value=DNS)
        self.dns.start()
        self.legacy = GatewayRoute("default", "https://legacy.invalid/v1", "legacy", "legacy-key")
        self.store = RouteStore(self.directory, ADMIN, (self.legacy,), forbidden=("chat-" + "c" * 40,))

    def tearDown(self):
        self.store.close()
        self.dns.stop()
        self.path_guard.stop()
        self.temporary.cleanup()

    def assertCode(self, code, action):
        with self.assertRaises(ManagementError) as caught:
            action()
        self.assertEqual(code, caught.exception.code)

    def test_empty_state_and_no_private_key_in_listing(self):
        self.assertEqual(0, self.store.listing()["revision"])
        self.assertFalse(self.store.listing()["routes"][0]["managed"])
        self.assertNotIn(self.legacy.api_key, json.dumps(self.store.listing()))
        self.assertFalse(self.store.receipt("9" * 32)["found"])

    def test_create_receipt_idempotency_and_restart(self):
        result = self.store.upsert(payload())
        self.assertEqual(result, self.store.upsert(payload()))
        self.assertEqual(result, self.store.receipt("1" * 32)["result"])
        self.assertEqual(1, self.store.listing()["revision"])
        self.store.close()
        self.store = RouteStore(self.directory, ADMIN, (self.legacy,))
        self.assertEqual(result, self.store.upsert(payload()))
        self.assertEqual(KEY, self.store.snapshot()[0].api_key)

    def test_unknown_request_only_after_healthy_state(self):
        with patch.object(self.store, "_read", side_effect=ManagementError(503, "management_unavailable")):
            self.assertCode("management_unavailable", lambda: self.store.receipt("2" * 32))

    def test_same_id_different_body_and_stale_revision_fail(self):
        self.store.upsert(payload())
        self.assertCode("request_conflict", lambda: self.store.upsert(payload(upstreamModel="other")))
        self.assertCode("revision_conflict", lambda: self.store.upsert(payload(requestId="2" * 32)))

    def test_update_retains_key_and_old_snapshot(self):
        self.store.upsert(payload())
        original = self.store.snapshot()[0]
        changed = payload(requestId="2" * 32, expectedRevision=1, upstreamModel="new-pro")
        del changed["apiKey"]
        self.store.upsert(changed)
        self.assertEqual(KEY, self.store.snapshot()[0].api_key)
        self.assertEqual("provider-pro", original.upstream_model)
        self.assertEqual("new-pro", self.store.snapshot()[0].upstream_model)

    def test_default_and_environment_aliases_are_immutable(self):
        for public in (self.legacy.public_model, self.legacy.auxiliary_model):
            self.assertCode("immutable_route", lambda: self.store.upsert(payload(publicModel=public)))

    def test_key_required_only_on_creation_and_blank_not_keep(self):
        request = payload()
        del request["apiKey"]
        self.assertCode("invalid_request", lambda: self.store.upsert(request))
        self.store.upsert(payload())
        self.assertCode("invalid_request", lambda: self.store.upsert(payload(
            requestId="2" * 32, expectedRevision=1, apiKey="")))

    def test_exact_schema_types_and_control_credentials_rejected(self):
        for changed in [dict(extra="x"), dict(expectedRevision=True), dict(requestId="UPPER"),
                        dict(schema="other"), dict(apiKey=ADMIN), dict(apiKey="chat-" + "c" * 40),
                        dict(publicModel="bad name"), dict(upstreamModel="model\n"), dict(apiKey="bad\nkey")]:
            self.assertCode("invalid_request", lambda: self.store.upsert(payload(**changed)))

    def test_no_secrets_in_any_visible_fields(self):
        for changed in [dict(publicModel=KEY), dict(upstreamModel=KEY),
                        dict(upstreamBaseUrl="https://provider.invalid/" + KEY),
                        dict(publicModel="prefix-legacy-key")]:
            self.assertCode("invalid_request", lambda: self.store.upsert(payload(**changed)))
        self.store.upsert(payload())
        for result in [self.store.listing(), self.store.receipt("1" * 32)]:
            self.assertNotIn(KEY, json.dumps(result))
            self.assertNotIn(ADMIN, json.dumps(result))

    def test_corrupt_persisted_state_fails_closed(self):
        with patch("builtins.open", return_value=io.BytesIO(b'{"schema":"bad"}')):
            self.assertCode("management_unavailable", self.store.listing)

    def test_failed_atomic_write_does_not_publish_receipt(self):
        with patch.object(self.store, "_commit", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.store.upsert(payload())
        self.assertFalse(self.store.receipt("1" * 32)["found"])
        self.assertEqual(0, self.store.listing()["revision"])

    def test_commit_completed_but_reply_lost_is_readable_without_repost(self):
        real = self.store._commit
        def lost(state):
            real(state)
            raise OSError("synthetic lost response")
        with patch.object(self.store, "_commit", side_effect=lost):
            with self.assertRaises(OSError):
                self.store.upsert(payload())
        self.assertTrue(self.store.receipt("1" * 32)["found"])
        self.assertEqual(1, self.store.listing()["revision"])

    def test_concurrent_same_revision_one_wins(self):
        results = []
        def submit(number):
            try:
                results.append(self.store.upsert(payload(requestId=str(number) * 32, publicModel="route" + str(number)))["status"])
            except ManagementError as error:
                results.append(error.code)
        threads = [threading.Thread(target=submit, args=(number,)) for number in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(["saved", "revision_conflict"], results)

    def test_route_and_journal_limits_fail_without_state_change(self):
        with patch("rikkahub_gateway.management.MAX_EXTRA_ROUTES", 1):
            self.store.upsert(payload())
            self.assertCode("route_limit_reached", lambda: self.store.upsert(payload(
                expectedRevision=1, requestId="2" * 32, publicModel="other")))
        with patch("rikkahub_gateway.management.MAX_REQUESTS", 1):
            self.assertCode("journal_limit_reached", lambda: self.store.upsert(payload(
                expectedRevision=1, requestId="2" * 32)))

    def test_process_lock_refuses_second_owner(self):
        self.assertCode("management_unavailable", lambda: RouteStore(self.directory, ADMIN, (self.legacy,)))

    def test_authorization_is_dedicated(self):
        self.assertTrue(self.store.authorize(ADMIN))
        for token in [None, "", "chat-" + "c" * 40, KEY, "管理"]:
            self.assertFalse(self.store.authorize(token))


class EndpointTests(unittest.TestCase):
    def test_no_network_for_unsafe_url_shapes(self):
        with patch("rikkahub_gateway.management.socket.getaddrinfo") as resolve:
            for value in ["http://provider.invalid/v1", "https://user:pw@host.invalid/v1", "https://a.invalid/v1?key=x",
                          "https://a.invalid/v1#x", "https://a.invalid/%2f", "https://a.invalid/../v1",
                          "file:///tmp", "https://localhost/v1", "https://host.invalid./v1"]:
                with self.subTest(value=value), self.assertRaises(ManagementError):
                    public_endpoint(value)
            resolve.assert_not_called()

    def test_private_multicast_mapped_and_mixed_dns_rejected(self):
        for address in ["127.0.0.1", "10.0.0.1", "192.168.1.1", "100.64.1.1", "169.254.169.254",
                        "0.0.0.0", "224.0.0.1", "::1", "fc00::1", "::ffff:8.8.8.8", "2002:0808:0808::1"]:
            answers = [*DNS, (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
            with self.subTest(address=address), patch("rikkahub_gateway.management.socket.getaddrinfo", return_value=answers):
                with self.assertRaises(ManagementError):
                    public_endpoint("https://provider.invalid/v1")

    def test_pins_ip_preserves_host_and_sni_and_never_follows_redirect(self):
        captured = []
        def response(request):
            captured.append(request)
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/secrets"})
        route = GatewayRoute("managed", "https://provider.invalid:8443/v1", "real", KEY, managed=True)
        with patch("rikkahub_gateway.management.socket.getaddrinfo", return_value=DNS):
            with self.assertRaises(ManagementError):
                with managed_stream(route, content=b"{}", timeout=2, transport=httpx.MockTransport(response)):
                    pass
        self.assertEqual(1, len(captured))
        self.assertEqual("93.184.216.34", captured[0].url.host)
        self.assertEqual("provider.invalid:8443", captured[0].headers["Host"])
        self.assertEqual("provider.invalid", captured[0].extensions["sni_hostname"])
        self.assertEqual("Bearer " + KEY, captured[0].headers["Authorization"])

    def test_successful_stream_uses_fresh_cookie_free_client(self):
        captured = []
        def response(request):
            captured.append(request)
            return httpx.Response(200, json={"safe": True}, headers={"Set-Cookie": "leak=other"})
        route = GatewayRoute("managed", "https://provider.invalid/v1", "real", KEY, managed=True)
        with patch("rikkahub_gateway.management.socket.getaddrinfo", return_value=DNS):
            for _ in range(2):
                with managed_stream(route, content=b"{}", timeout=2, transport=httpx.MockTransport(response)) as result:
                    self.assertEqual(200, result.status_code)
                    result.read()
        self.assertTrue(all("Cookie" not in request.headers for request in captured))


class GatewayManagementTests(StoreTests):
    def setUp(self):
        super().setUp()
        self.store.close()
        self.app = GatewayApplication(replace(config(), management_enabled=True, management_token=ADMIN,
                                             management_state_dir=str(self.directory),
                                             management_forbidden_tokens=("chat-" + "c" * 40,)), control=FakeControl())
        self.store = self.app.route_store

    def tearDown(self):
        self.app.close_short_term()
        self.app.upstream.close()
        super().tearDown()

    # Do not inherit generic tests with a differently named immutable route.
    def test_default_and_environment_aliases_are_immutable(self):
        self.assertCode("immutable_route", lambda: self.store.upsert(payload(publicModel=config().public_model)))

    def test_no_secrets_in_any_visible_fields(self):
        self.store.upsert(payload())
        self.assertNotIn(KEY, json.dumps(self.store.listing()))

    def test_empty_state_and_no_private_key_in_listing(self):
        self.assertEqual(0, self.store.listing()["revision"])

    def test_registry_refresh_and_new_turn_use_managed_route_without_changing_default(self):
        self.store.upsert(payload())
        self.app.refresh_managed_routes()
        self.assertEqual(config().public_model, self.app.default_route.public_model)
        self.assertEqual(KEY, self.app.resolve_route({"model": "managed/pro"}).api_key)
        self.assertIn("managed/pro", [item["id"] for item in self.app.models()["data"]])
        with self.assertRaises(GatewayError) as caught:
            self.app.balance("managed/pro")
        self.assertEqual("balance_unsupported", caught.exception.code)

    def test_continuation_retains_snapshot_after_management_edit(self):
        self.store.upsert(payload())
        self.app.refresh_managed_routes()
        start = {"model": "managed/pro", "messages": [{"role": "user", "content": "hello"}],
                 "tools": legacy.GatewayTests.bound_tools()}
        prepared = self.app.prepare_turn(start, {"x-st-thread-id": "one"})
        original = prepared.route
        call = legacy.GatewayTests.native_call("call-1", "synthetic")
        bindings = self.app.bind_response_tool_calls(prepared, [call])
        self.app.finish_turn(prepared, keep_for_tools=True, tool_call_ids=[call.tool_call_id], tool_call_bindings=bindings)
        self.store.upsert(payload(requestId="2" * 32, expectedRevision=1, upstreamModel="changed", apiKey="new-" + KEY))
        self.app.refresh_managed_routes()
        continuation = {**start, "messages": [*start["messages"],
            {"role": "assistant", "tool_calls": [legacy.GatewayTests.wire_call(call)]},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"exitCode":0}'}]}
        result = self.app.prepare_turn(continuation, {"x-st-thread-id": "one"})
        self.assertEqual(original, result.route)
        self.assertEqual("provider-pro", result.payload["model"])
        self.assertEqual("changed", self.app.resolve_route({"model": "managed/pro"}).upstream_model)

    def request(self, path, method="GET", auth=ADMIN, body=None, headers=None):
        encoded = json.dumps(body).encode() if body is not None else b""
        values = Message()
        if auth is not None:
            values["Authorization"] = "Bearer " + auth
        if body is not None:
            values["Content-Type"] = "application/json"
            values["Content-Length"] = str(len(encoded))
        for key, value in (headers or []):
            values[key] = value
        output = []
        handler = SimpleNamespace(path=path, headers=values, app=self.app, rfile=io.BytesIO(encoded),
            connection=SimpleNamespace(settimeout=lambda value: None), _json=lambda status, result: output.append((status, result)))
        self.assertTrue(handle_management_request(handler, method))
        self.assertTrue(handler.close_connection)
        return output[0]

    def test_http_contract_caps_list_save_lookup(self):
        self.assertTrue(self.request(CAPABILITIES, auth=None)[1]["enabled"])
        self.assertEqual("orbis.st.routes/1", self.request(LIST)[1]["schema"])
        status, result = self.request(UPSERT, "POST", body=payload())
        self.assertEqual(200, status)
        self.assertEqual(result, self.request(REQUESTS + "1" * 32)[1]["result"])

    def test_http_chat_human_upstream_tokens_not_management(self):
        for auth in [None, config().gateway_token, config().host_token, KEY]:
            self.assertEqual(401, self.request(LIST, auth=auth)[0])

    def test_http_refuses_ambiguous_redirect_origin_and_bodies(self):
        for path, headers in [(LIST + "?a=b", []), (LIST, [("Origin", "https://evil.invalid")]),
                              (LIST, [("Cookie", "x=y")]), (LIST, [("Content-Length", "1")]),
                              (LIST, [("Transfer-Encoding", "chunked")])]:
            self.assertEqual(400, self.request(path, headers=headers)[0])
        self.assertEqual(401, self.request(LIST, headers=[("Authorization", "Bearer " + ADMIN)])[0])

    def test_http_default_disabled(self):
        store = self.app.route_store
        self.app.route_store = None
        try:
            self.assertFalse(self.request(CAPABILITIES, auth=None)[1]["enabled"])
            self.assertEqual(403, self.request(LIST)[0])
        finally:
            self.app.route_store = store


class PathGuardTests(unittest.TestCase):
    def test_relative_missing_and_symlink_rejected(self):
        with self.assertRaises((ManagementError, OSError)):
            _private_path(Path("relative"), directory=True)
        with tempfile.TemporaryDirectory() as name:
            path = Path(name)
            with self.assertRaises((ManagementError, OSError)):
                _private_path(path / "missing")
            try:
                link = path / "link"
                link.symlink_to(path, target_is_directory=True)
            except OSError:
                return  # Windows without symlink privilege; no claim that it ran.
            with self.assertRaises(ManagementError):
                _private_path(link, directory=True)

    @unittest.skipIf(os.name == "nt", "POSIX permission test; Windows DACL tested separately")
    def test_posix_permissions(self):
        with tempfile.TemporaryDirectory() as name:
            _private_path(Path(name), directory=True)
            os.chmod(name, 0o755)
            with self.assertRaises(ManagementError):
                _private_path(Path(name), directory=True)

    @unittest.skipUnless(os.name == "nt", "Windows DACL and durable replacement test")
    def test_windows_private_acl_real_create_save_and_reopen(self):
        with tempfile.TemporaryDirectory() as name:
            identity = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
                capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            sid = identity.stdout.decode("ascii").strip()
            self.assertTrue(sid.startswith("S-1-5-21-"))
            result = subprocess.run(["icacls.exe", name, "/inheritance:r", "/grant:r",
                "*" + sid + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(0, result.returncode, "synthetic private directory ACL setup failed: " + result.stderr.decode(errors="replace"))
            _private_path(Path(name), directory=True)
            store = RouteStore(name, ADMIN, ())
            try:
                with patch("rikkahub_gateway.management.socket.getaddrinfo", return_value=DNS):
                    saved = store.upsert(payload())
                self.assertEqual(1, saved["revision"])
                _private_path(Path(name) / "managed-routes.json")
            finally:
                store.close()
            reopened = RouteStore(name, ADMIN, ())
            try:
                self.assertEqual(saved, reopened.receipt("1" * 32)["result"])
                self.assertNotIn(KEY, json.dumps(reopened.listing()))
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
