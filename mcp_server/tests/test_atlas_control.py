"""Host-only atlas boundary: synthetic SQLite and isolated loopback HTTP."""
from contextlib import closing
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mcp_server.control_server import ControlApplication, _ControlHandler, build_application_from_env
from runtime.atlas_device_grants import AtlasDeviceGrants, TABLE


HOST = "synthetic-host-only-token-over-thirty-two-characters"
HUMAN = "synthetic-human-only-token-over-thirty-two-characters"
PREFIX = "/v1/host/atlas/"
REGISTER = {"schema": "orbis.st.atlas-register/1", "requestId": "1" * 32,
            "deviceId": "2" * 32, "verifier": hashlib.sha256(b"synthetic-device").hexdigest()}
SNAPSHOT = {"schema": "orbis.st.atlas-snapshot/1", "verifier": REGISTER["verifier"]}
REVOKE = {"schema": "orbis.st.atlas-revoke/1", "requestId": "3" * 32, "verifier": REGISTER["verifier"]}
GRAPH = {"schema": "orbis.st.atlas/1", "generatedAt": "2026-09-01T00:00:00Z", "truncated": False, "stars": [], "edges": []}


class AtlasControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "synthetic-main.sqlite"
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE fixture (id TEXT)")
            db.commit()
        self.grants = AtlasDeviceGrants(self.database, "owner", "model", enabled=True, id_key=b"k" * 32,
                                        reader=lambda: json.dumps(GRAPH).encode())
        self.onboarding = Mock()
        self.app = self.application(self.grants)

    def application(self, grants):
        return ControlApplication(self.onboarding, owner_id="owner", model_id="model", host_token=HOST,
                                  human_token=HUMAN, human_actor_id="synthetic-human", atlas_grants=grants)

    def post(self, name, payload, *, app=None, token=HOST, headers=None, method="POST", raw=None):
        fields = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        fields.update(headers or {})
        return (app or self.app).handle(method, PREFIX + name, fields,
                                       json.dumps(payload).encode() if raw is None else raw)

    def assert_code(self, result, status, code):
        self.assertEqual((status, {"error": {"code": code}}), result)

    def test_host_register_snapshot_revoke_round_trip(self):
        status, response = self.post("register", REGISTER)
        self.assertEqual(200, status)
        self.assertEqual("active", response["status"])
        self.assertEqual((200, GRAPH), self.post("snapshot", SNAPSHOT))
        self.assertEqual((200, {"schema": "orbis.st.atlas-revoke-result/1", "status": "revoked"}), self.post("revoke", REVOKE))
        self.assert_code(self.post("snapshot", SNAPSHOT), 401, "unauthorized")
        self.assertEqual("revoked", self.post("register", REGISTER)[1]["status"])

    def test_disabled_capabilities_are_static_and_do_not_touch_store(self):
        app = self.application(None)
        with patch("sqlite3.connect", side_effect=AssertionError("no database access")):
            status, result = self.post("capabilities", {}, app=app)
        self.assertEqual(200, status)
        self.assertEqual({"schema": "orbis.st.atlas-bootstrap/1", "service": "stillerbrain",
                          "enabled": False, "scope": "atlas.metadata.read", "atlasSchema": "orbis.st.atlas/1"}, result)
        self.assert_code(self.post("register", REGISTER, app=app), 403, "atlas_disabled")

    def test_enabled_capabilities_do_not_read_or_mutate_storage(self):
        with patch.object(self.grants, "_connect", side_effect=AssertionError("no storage access")):
            self.assertTrue(self.post("capabilities", {})[1]["enabled"])

    def test_all_atlas_routes_accept_host_credential_only(self):
        for name, payload in (("capabilities", {}), ("register", REGISTER), ("snapshot", SNAPSHOT), ("revoke", REVOKE)):
            for token in (HUMAN, "orb_atlas_" + "a" * 43, "", "synthetic-gateway-token", "非授权"):
                self.assert_code(self.post(name, payload, token=token), 401, "unauthorized")

    def test_raw_tokens_and_request_owned_scope_never_forward(self):
        for name, payload in (("register", REGISTER), ("snapshot", SNAPSHOT), ("revoke", REVOKE)):
            for field in ("token", "owner_id", "model_id", "scope", "database", "url"):
                self.assert_code(self.post(name, dict(payload, **{field: "synthetic-untrusted"})), 400, "invalid_request")

    def test_host_and_human_secret_hashes_cannot_become_atlas_credentials(self):
        for token in (HOST, HUMAN):
            verifier = hashlib.sha256(token.encode()).hexdigest()
            self.assert_code(self.post("register", dict(REGISTER, verifier=verifier)), 401, "unauthorized")
            self.assert_code(self.post("snapshot", dict(SNAPSHOT, verifier=verifier)), 401, "unauthorized")

    def test_constructor_rejects_store_scope_mismatch(self):
        with self.assertRaisesRegex(ValueError, "^atlas_configuration_invalid$"):
            ControlApplication(self.onboarding, owner_id="foreign", model_id="model", host_token=HOST,
                               human_token=HUMAN, human_actor_id="human", atlas_grants=self.grants)

    def test_path_query_fragment_and_unknown_subroute_rejected(self):
        for path in ("register?owner=x", "register#fragment", "register/", "unknown"):
            self.assert_code(self.post(path, REGISTER), 400, "invalid_request")

    def test_method_media_type_and_oversize_are_fixed_errors(self):
        self.assert_code(self.post("register", REGISTER, method="GET"), 405, "method_not_allowed")
        self.assert_code(self.post("register", REGISTER, headers={"Content-Type": "text/plain"}), 415, "application_json_required")
        self.assert_code(self.post("register", {}, raw=b" " * 2049), 413, "request_too_large")

    def test_json_duplicates_invalid_utf8_arrays_and_nonfinite_are_rejected(self):
        for raw in (b'{"schema":"x","schema":"y"}', b'{"x":NaN}', b"[]", b"\xff", b""):
            self.assert_code(self.post("register", {}, raw=raw), 400, "invalid_request")

    def test_origin_cookies_transfer_encoding_and_duplicate_headers_rejected(self):
        for fields in ({"Origin": "null"}, {"Cookie": "x=y"}, {"Transfer-Encoding": "chunked"},
                       {"authorization": "Bearer " + HOST}, {"content-type": "application/json"},
                       {"Content-Length": "2", "content-length": "2"}):
            self.assert_code(self.post("capabilities", {}, headers=fields), 400, "invalid_request")

    def test_malformed_operation_schemas_rejected(self):
        for name, payload in (("capabilities", {"anything": 1}), ("snapshot", {"verifier": REGISTER["verifier"]}),
                              ("snapshot", dict(SNAPSHOT, schema="wrong")), ("revoke", {"verifier": REGISTER["verifier"]}),
                              ("revoke", dict(REVOKE, schema="wrong"))):
            self.assert_code(self.post(name, payload), 400, "invalid_request")

    def test_internal_exceptions_never_leak_paths_identities_or_credentials(self):
        with patch.object(self.grants, "register", side_effect=RuntimeError("DO_NOT_LEAK_PRIVATE_PATH_OR_TOKEN")):
            self.assert_code(self.post("register", REGISTER), 503, "atlas_unavailable")

    def test_body_deadline_cannot_be_extended_by_slow_fragments(self):
        stream = Mock()
        stream.read1.side_effect = [b"{", b" ", b"}"]
        fixture = SimpleNamespace(connection=Mock(), rfile=stream)
        with patch("mcp_server.control_server.time.monotonic", side_effect=[0, 1, 4, 5.01]):
            with self.assertRaises(TimeoutError):
                _ControlHandler._read_atlas_body(fixture, 3)
        self.assertEqual(2, stream.read1.call_count)
        self.assertEqual([4.0, 1.0], [call.args[0] for call in fixture.connection.settimeout.call_args_list])

    def test_body_deadline_applies_after_last_fragment_and_eof_is_rejected(self):
        stream = Mock()
        stream.read1.return_value = b"{}"
        fixture = SimpleNamespace(connection=Mock(), rfile=stream)
        with patch("mcp_server.control_server.time.monotonic", side_effect=[0, 1, 5.01]):
            with self.assertRaises(TimeoutError):
                _ControlHandler._read_atlas_body(fixture, 2)
        stream.read1.return_value = b""
        with self.assertRaises(OSError):
            _ControlHandler._read_atlas_body(fixture, 2)

    def test_environment_builder_assembles_fixed_main_database_and_identities(self):
        env = {"STBRAIN_DB_PATH": str(self.database), "STBRAIN_WAKE_SECRET": "synthetic-wake-secret-over-thirty-two-characters",
               "STBRAIN_OWNER_ID": "owner", "STBRAIN_MODEL_ID": "model", "STBRAIN_HOST_TOKEN": HOST,
               "STBRAIN_HUMAN_TOKEN": HUMAN, "STBRAIN_HUMAN_ACTOR_ID": "human"}
        with patch.dict("os.environ", env, clear=True), patch("mcp_server.control_server.atlas_from_env", return_value=None) as factory:
            app = build_application_from_env()
        factory.assert_called_once_with(self.database, "owner", "model")
        self.assertIsNone(app.atlas_grants)


class AtlasControlHttpTests(AtlasControlTests):
    # Network cases share only setup/helpers; inherited pure-router cases are
    # intentionally excluded by discovery below, avoiding duplicate counts.
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ControlHandler)
        self.server.daemon_threads = True
        self.server.application = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def wire(self, extra=(), *, length="2", body=b"{}", name="capabilities", auth=True):
        lines = [f"POST {PREFIX}{name} HTTP/1.1", "Host: 127.0.0.1", "Connection: close", "Content-Type: application/json"]
        if auth:
            lines.append("Authorization: Bearer " + HOST)
        if length is not None:
            lines.append("Content-Length: " + length)
        lines.extend(extra)
        with socket.create_connection(self.server.server_address, timeout=3) as client:
            client.sendall("\r\n".join(lines).encode() + b"\r\n\r\n" + body)
            client.shutdown(socket.SHUT_WR)
            output = b""
            while True:
                chunk = client.recv(8192)
                if not chunk:
                    break
                output += chunk
        head, raw = output.split(b"\r\n\r\n", 1)
        return int(head.split(b" ", 2)[1]), head.lower(), json.loads(raw)

    def test_wire_duplicate_authorization_cannot_be_collapsed(self):
        status, _, payload = self.wire(["Authorization: Bearer " + HUMAN])
        self.assert_code((status, payload), 400, "invalid_request")

    def test_wire_duplicate_content_length_and_transfer_encoding_rejected(self):
        for headers in (["Content-Length: 2"], ["Transfer-Encoding: chunked"], ["Content-Type: application/json"]):
            status, _, payload = self.wire(headers)
            self.assert_code((status, payload), 400, "invalid_request")

    def test_wire_short_declared_body_does_not_execute(self):
        with patch.object(self.grants, "register", side_effect=AssertionError("must not register")) as register:
            raw = json.dumps(REGISTER).encode()
            status, _, payload = self.wire(body=raw, length=str(len(raw) + 1), name="register")
        self.assert_code((status, payload), 400, "invalid_request")
        register.assert_not_called()

    def test_wire_oversize_and_noncanonical_framing_rejected(self):
        for length in (None, "02", "-1", "2,2"):
            status, _, payload = self.wire(length=length)
            self.assert_code((status, payload), 400, "invalid_request")
        status, _, payload = self.wire(length="2049")
        self.assert_code((status, payload), 413, "request_too_large")

    def test_wire_responses_are_no_store_nosniff_and_no_secrets(self):
        status, headers, payload = self.wire()
        self.assertEqual(200, status)
        self.assertIn(b"cache-control: no-store", headers)
        self.assertIn(b"x-content-type-options: nosniff", headers)
        self.assertIn(b"connection: close", headers)
        self.assertTrue(payload["enabled"])
        self.assertNotIn(HOST, json.dumps(payload))


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(AtlasControlTests))
    for name in sorted(AtlasControlHttpTests.__dict__):
        if name.startswith("test_wire_"):
            suite.addTest(AtlasControlHttpTests(name))
    return suite


if __name__ == "__main__":
    unittest.main()
