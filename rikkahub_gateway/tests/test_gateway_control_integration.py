from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mcp_server.control_server import ControlApplication
from rikkahub_gateway.server import GatewayApplication, GatewayConfig
from runtime import ModuleOneOnboardingStore
from runtime.onboarding import OPTIONAL_BRAIN_NOTICE


class DirectControlClient:
    """Exercise the real control router without opening a network listener."""

    def __init__(self, application: ControlApplication, token: str) -> None:
        self.application = application
        self.token = token

    def post(self, path: str, payload: dict) -> dict:
        status, result = self.application.handle(
            "POST",
            path,
            {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if status != 200:
            raise AssertionError(f"control request failed: {status} {result}")
        return result


class GatewayControlIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "gateway-integration.db"
        self.onboarding = ModuleOneOnboardingStore(
            self.database,
            capability_secret=b"integration-wake-secret-that-is-at-least-32-bytes",
            wake_ttl_seconds=300,
            edit_challenge_ttl_seconds=300,
        )
        self.host_token = "integration-host-token-that-is-longer-than-32"
        self.control_app = ControlApplication(
            self.onboarding,
            owner_id="owner:integration",
            model_id="model:integration",
            host_token=self.host_token,
            human_token="integration-human-token-that-is-longer-than-32",
            human_actor_id="human:integration",
        )
        self.config = GatewayConfig(
            gateway_token="integration-gateway-token-longer-than-32",
            control_url="http://unused.test",
            host_token=self.host_token,
            upstream_base_url="http://upstream.test/v1",
            upstream_api_key="integration-upstream-key",
            public_model="stiller-rikka",
            upstream_model="real-model",
        )
        self.gateway = GatewayApplication(
            self.config,
            control=DirectControlClient(self.control_app, self.host_token),
        )
        self.headers = {
            "Authorization": f"Bearer {self.config.gateway_token}",
            "X-ST-Thread-ID": "thread:integration",
        }

    def tearDown(self) -> None:
        self.gateway.upstream.close()
        self.temp.cleanup()

    def rows(self, sql: str) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    @staticmethod
    def request(messages: list[dict]) -> dict:
        return {"model": "stiller-rikka", "messages": messages}

    def test_real_control_database_preserves_one_wake_across_tool_loop(self) -> None:
        first = self.gateway.prepare_turn(
            self.request([{"role": "user", "content": "开始模块一"}]),
            self.headers,
        )

        injected = first.payload["messages"][0]
        self.assertEqual({"role": "system", "content": OPTIONAL_BRAIN_NOTICE}, injected)
        self.assertEqual(injected, first.session.message)

        wakes = self.rows(
            "SELECT wake_id, wake_seq, status, injected_at FROM brain_wake_sessions"
        )
        snapshots = self.rows(
            "SELECT wake_id, status, context_hash FROM brain_context_snapshots"
        )
        self.assertEqual(1, len(wakes))
        self.assertEqual(1, wakes[0]["wake_seq"])
        self.assertEqual("current", wakes[0]["status"])
        self.assertIsNotNone(wakes[0]["injected_at"])
        self.assertEqual(1, len(snapshots))
        self.assertEqual("injected", snapshots[0]["status"])
        self.assertEqual(first.session.context_hash, snapshots[0]["context_hash"])

        self.gateway.finish_turn(
            first,
            keep_for_tools=True,
            tool_call_ids=["call-1"],
        )

        continuation = self.gateway.prepare_turn(
            self.request(
                [
                    {"role": "user", "content": "开始模块一"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "stbrain_health",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-1",
                        "content": '{"ok":true}',
                    },
                ]
            ),
            self.headers,
        )
        self.assertTrue(continuation.continuation)
        self.assertEqual(first.session.wake_id, continuation.session.wake_id)
        self.assertEqual(1, len(self.rows("SELECT wake_id FROM brain_wake_sessions")))

        self.gateway.finish_turn(continuation, keep_for_tools=False)
        wakes = self.rows("SELECT status FROM brain_wake_sessions")
        snapshots = self.rows("SELECT status FROM brain_context_snapshots")
        self.assertEqual("closed", wakes[0]["status"])
        self.assertEqual("closed", snapshots[0]["status"])

        next_turn = self.gateway.prepare_turn(
            self.request(
                [
                    {"role": "user", "content": "开始模块一"},
                    {"role": "assistant", "content": "本轮结束"},
                    {"role": "user", "content": "下一次真实消息"},
                ]
            ),
            self.headers,
        )
        self.assertNotEqual(first.session.wake_id, next_turn.session.wake_id)
        wake_rows = self.rows(
            "SELECT wake_seq, status FROM brain_wake_sessions ORDER BY wake_seq"
        )
        self.assertEqual([1, 2], [row["wake_seq"] for row in wake_rows])
        self.assertEqual(["closed", "current"], [row["status"] for row in wake_rows])
        self.gateway.finish_turn(next_turn, keep_for_tools=False)


if __name__ == "__main__":
    unittest.main()
