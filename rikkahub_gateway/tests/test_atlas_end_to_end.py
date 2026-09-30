"""Synthetic full HTTP chain; no model call, real credentials or private DB."""
from contextlib import closing
from dataclasses import replace
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock

import httpx

from mcp_server.control_server import ControlApplication, _ControlHandler
from runtime.atlas_device_grants import AtlasDeviceGrants
from rikkahub_gateway.server import GatewayApplication, _GatewayHandler
from rikkahub_gateway.tests.test_gateway import FakeControl, config
from rikkahub_gateway.tests import test_gateway_atlas as atlas_fixture
from rikkahub_gateway.tests.test_gateway_atlas import (
    CAPABILITIES, REGISTER, REVOKE, SNAPSHOT, TOKEN, INTENT, REVOKE_REQUEST,
)
from tests.test_atlas_metadata import create_runtime_database, insert_synthetic


class AtlasEndToEndTests(unittest.TestCase):
    call = atlas_fixture.AtlasHttpTests.call

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "synthetic-main.sqlite"
        create_runtime_database(self.database)
        with closing(sqlite3.connect(self.database)) as db:
            insert_synthetic(db, "emotion_memories", memory_id="synthetic-emotion", lifecycle="active")
            insert_synthetic(db, "learning_items", learning_id="synthetic-learning", lifecycle="active")
            insert_synthetic(db, "planning_items", plan_id="synthetic-plan", recall_lifecycle="active")
            db.commit()
        self.grants = AtlasDeviceGrants(self.database, "synthetic-owner", "synthetic-model", enabled=True, id_key=b"k" * 32)
        cfg = config()
        self.onboarding = Mock()
        control_app = ControlApplication(self.onboarding, owner_id="synthetic-owner", model_id="synthetic-model",
                                         host_token=cfg.host_token, human_token="synthetic-human-token-long-enough-32",
                                         human_actor_id="synthetic-human", atlas_grants=self.grants)
        self.onboarding.ensure_state.assert_called_once_with(owner_id="synthetic-owner", model_id="synthetic-model")
        self.onboarding.reset_mock()
        self.control_server = self.start_server(_ControlHandler, control_app)
        self.config = replace(cfg, control_url="http://127.0.0.1:" + str(self.control_server.server_address[1]))
        def no_model(request):
            raise AssertionError("model transport must not be used")
        self.chat_control = FakeControl()
        self.app = GatewayApplication(self.config, control=self.chat_control,
                                      upstream=httpx.Client(transport=httpx.MockTransport(no_model)))
        self.addCleanup(self.app.upstream.close)
        self.server = self.start_server(_GatewayHandler, self.app)

    def start_server(self, handler, app):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        server.application = app
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        def stop():
            server.shutdown(); server.server_close(); thread.join(2)
        self.addCleanup(stop)
        return server

    def tearDown(self):
        self.assertEqual([], self.chat_control.calls)
        self.assertEqual([], self.onboarding.mock_calls)
        self.assertIsNone(self.app._current_session)

    def register(self, intent=None):
        status, _, result = self.call(REGISTER, "POST", self.config.gateway_token, intent or INTENT)
        self.assertEqual(200, status)
        return result

    def test_full_register_read_retry_revoke_and_replay_chain(self):
        self.assertTrue(self.call()[2]["enabled"])
        grant = self.register()
        self.assertEqual("active", grant["status"])
        self.assertEqual(grant, self.register())
        status, headers, graph = self.call(SNAPSHOT, token=TOKEN)
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertEqual(3, len(graph["stars"]))
        self.assertEqual({"情感", "学习", "规划"}, {x["type"] for x in graph["stars"]})
        self.assertNotIn("DO_NOT_PROJECT_PRIVATE_BODY", json.dumps(graph))
        self.assertEqual(200, self.call(REVOKE, "POST", TOKEN, REVOKE_REQUEST)[0])
        self.assertEqual(200, self.call(REVOKE, "POST", TOKEN, REVOKE_REQUEST)[0])
        self.assertEqual(401, self.call(SNAPSHOT, token=TOKEN)[0])
        replay = self.register()
        self.assertEqual("revoked", replay["status"])
        self.assertEqual(grant["grantId"], replay["grantId"])
        self.assertEqual(401, self.call(SNAPSHOT, token=TOKEN)[0])

    def test_rotation_revokes_previous_but_keeps_graph_ids_stable(self):
        self.register()
        old_graph = self.call(SNAPSHOT, token=TOKEN)[2]
        token2 = "orb_atlas_" + "B" * 43
        intent = {**INTENT, "requestId": "5" * 32, "verifier": hashlib.sha256(token2.encode()).hexdigest()}
        self.register(intent)
        self.assertEqual(401, self.call(SNAPSHOT, token=TOKEN)[0])
        status, _, new_graph = self.call(SNAPSHOT, token=token2)
        self.assertEqual(200, status)
        self.assertEqual(old_graph["stars"], new_graph["stars"])

    def test_policy_disable_stops_read_and_registration_but_allows_self_revoke(self):
        self.register()
        self.grants.enabled = False
        self.assertFalse(self.call()[2]["enabled"])
        self.assertEqual(403, self.call(REGISTER, "POST", self.config.gateway_token, INTENT)[0])
        self.assertEqual(403, self.call(SNAPSHOT, token=TOKEN)[0])
        self.assertEqual(200, self.call(REVOKE, "POST", TOKEN, REVOKE_REQUEST)[0])

    def test_snapshot_reads_do_not_change_any_business_or_auth_table(self):
        self.register()
        with closing(sqlite3.connect(self.database)) as db:
            before = list(db.iterdump())
        for _ in range(3):
            self.assertEqual(200, self.call(SNAPSHOT, token=TOKEN)[0])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(before, list(db.iterdump()))

    def test_same_request_different_verifier_is_conflict_not_rotation(self):
        original = self.register()
        changed = {**INTENT, "verifier": hashlib.sha256(b"different").hexdigest()}
        self.assertEqual(409, self.call(REGISTER, "POST", self.config.gateway_token, changed)[0])
        self.assertEqual(original, self.register())
        self.assertEqual(200, self.call(SNAPSHOT, token=TOKEN)[0])

    def test_incompatible_schema_is_unavailable_not_fake_empty(self):
        self.register()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DROP TABLE learning_links")
            db.commit()
        status, _, result = self.call(SNAPSHOT, token=TOKEN)
        self.assertEqual((503, {"error": {"code": "atlas_unavailable"}}), (status, result))


if __name__ == "__main__":
    unittest.main()
