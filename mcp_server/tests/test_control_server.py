from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mcp_server.control_server import ControlApplication, build_application_from_env
from runtime import (
    HallucinationVaultStore,
    InjectionControlStore,
    ModuleOneOnboardingStore,
    PlanningMemoryStore,
)


class ControlServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "brain.db"
        self.onboarding = ModuleOneOnboardingStore(
            self.database,
            capability_secret=b"control-wake-secret-that-is-at-least-32-bytes",
            wake_ttl_seconds=300,
            edit_challenge_ttl_seconds=300,
        )
        self.host_token = "host-token-that-is-definitely-longer-than-32"
        self.human_token = "human-token-that-is-definitely-longer-than-32"
        self.app = ControlApplication(
            self.onboarding,
            owner_id="owner:test",
            model_id="model:test",
            host_token=self.host_token,
            human_token=self.human_token,
            human_actor_id="human:owner",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def body(payload: dict) -> bytes:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def post(self, path: str, payload: dict, token: str) -> tuple[int, dict]:
        return self.app.handle(
            "POST",
            path,
            {
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
            self.body(payload),
        )

    def issue_and_confirm(self, event: str = "event-1") -> dict:
        status, wake = self.post(
            "/v1/host/wakes",
            {
                "host_id": "host:test",
                "thread_id": "thread:test",
                "source_kind": "human_message",
                "source_event_id": event,
            },
            self.host_token,
        )
        self.assertEqual(200, status)
        status, prepared = self.post(
            "/v1/host/context/prepare",
            {
                "wake_id": wake["wake_id"],
                "wake_capability": wake["wake_capability"],
                "source_digest": f"source:{event}",
                "host_contract_digest": "host-contract:v1",
            },
            self.host_token,
        )
        self.assertEqual(200, status)
        status, confirmed = self.post(
            "/v1/host/context/confirm",
            {
                "wake_id": wake["wake_id"],
                "wake_capability": wake["wake_capability"],
                "context_hash": prepared["context_hash"],
            },
            self.host_token,
        )
        self.assertEqual(200, status)
        self.assertEqual("injected", confirmed["decision"])
        return wake

    def test_tokens_are_role_separated_and_body_is_strict_utf8_json(self) -> None:
        status, _ = self.app.handle("POST", "/v1/host/wakes", {}, b"{}")
        self.assertEqual(415, status)
        status, _ = self.post("/v1/host/wakes", {}, self.human_token)
        self.assertEqual(401, status)
        status, _ = self.post(
            "/v1/human/objections", {}, self.host_token
        )
        self.assertEqual(401, status)
        status, result = self.app.handle(
            "POST",
            "/v1/host/wakes",
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.host_token}",
            },
            b"\xff",
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_utf8_json", result["error"])

    def test_human_can_issue_only_a_server_bound_direct_grant(self) -> None:
        issued = {
            "grant_ref": "opaque-one-use-reference",
            "expires_at": "2099-01-01T00:00:00Z",
            "authorized_scopes": ["learning_memory"],
            "status": "pending",
            "grant_id": "must-not-cross-the-control-boundary",
        }
        issue = Mock(return_value=issued)
        self.onboarding.issue_direct_grant = issue  # type: ignore[attr-defined]
        request = {
            "request_id": "123e4567-e89b-42d3-a456-426614174000",
            "requested_scopes": ["learning_memory"],
        }

        denied_status, _ = self.post(
            "/v1/human/direct-grants", request, self.host_token
        )
        self.assertEqual(401, denied_status)
        self.assertFalse(issue.called)

        status, result = self.post(
            "/v1/human/direct-grants", request, self.human_token
        )
        self.assertEqual(200, status)
        self.assertEqual(
            {
                "grant_ref": "opaque-one-use-reference",
                "expires_at": "2099-01-01T00:00:00Z",
                "scopes": ["learning_memory"],
                "status": "pending",
            },
            result,
        )
        issue.assert_called_once_with(
            owner_id="owner:test",
            model_id="model:test",
            actor_id="human:owner",
            client_principal="official-deepseek-direct",
            request_id=request["request_id"],
            requested_scopes=["learning_memory"],
        )

    def test_direct_grant_body_cannot_choose_identity_authority_or_ttl(self) -> None:
        issue = Mock()
        self.onboarding.issue_direct_grant = issue  # type: ignore[attr-defined]
        base = {
            "request_id": "123e4567-e89b-42d3-a456-426614174000",
            "requested_scopes": ["learning_memory"],
        }
        for field, value in {
            "owner_id": "owner:foreign",
            "model_id": "model:foreign",
            "actor_id": "human:foreign",
            "client_principal": "untrusted-client",
            "ttl_seconds": 999999,
        }.items():
            with self.subTest(field=field):
                status, result = self.post(
                    "/v1/human/direct-grants",
                    {**base, field: value},
                    self.human_token,
                )
                self.assertEqual(400, status)
                self.assertEqual("invalid_request", result["error"])
        self.assertFalse(issue.called)

    def test_host_can_issue_prepare_confirm_and_close_without_ai_mutation(self) -> None:
        wake = self.issue_and_confirm()
        status, closed = self.post(
            "/v1/host/context/close",
            {
                "wake_id": wake["wake_id"],
                "wake_capability": wake["wake_capability"],
            },
            self.host_token,
        )
        self.assertEqual(200, status)
        self.assertEqual("closed", closed["decision"])
        status, health = self.app.handle("GET", "/health")
        self.assertEqual(200, status)
        self.assertTrue(health["ok"])

    def test_facet_selection_preserves_absent_empty_and_explicit_names(self) -> None:
        prepare = Mock(return_value={"decision": "context_prepared"})
        self.onboarding.build_pre_generation_context = prepare
        base = {"wake_id": "synthetic-wake", "wake_capability": "synthetic-capability",
                "source_digest": "synthetic-source", "host_contract_digest": "synthetic-host"}
        for extra, expected in (({}, None), ({"facet_names": []}, []),
                                ({"facet_names": ["own_name_7"]}, ["own_name_7"])):
            with self.subTest(extra=extra):
                prepare.reset_mock()
                status, _ = self.post("/v1/host/context/prepare", {**base, **extra}, self.host_token)
                self.assertEqual(status, 200)
                self.assertEqual(prepare.call_args.kwargs["facet_names"], expected)
        for value in (None, "own_name_7", [1], {"name": "own_name_7"}):
            with self.subTest(invalid=value):
                prepare.reset_mock()
                status, _ = self.post("/v1/host/context/prepare",
                                      {**base, "facet_names": value}, self.host_token)
                self.assertEqual(status, 400)
                prepare.assert_not_called()

    def test_environment_factory_wires_every_automatic_injection_store(self) -> None:
        database = Path(self.temp.name) / "factory-main.db"
        learning_database = Path(self.temp.name) / "factory-learning.db"
        vault_database = Path(self.temp.name) / "factory-vault.db"
        environment = {
            "STBRAIN_DB_PATH": str(database),
            "STBRAIN_LEARNING_IDEA_DB_PATH": str(learning_database),
            "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(vault_database),
            "STBRAIN_WAKE_SECRET": "factory-wake-secret-that-is-at-least-32-bytes",
            "STBRAIN_OWNER_ID": "owner:factory",
            "STBRAIN_MODEL_ID": "model:factory",
            "STBRAIN_HOST_TOKEN": "factory-host-token-that-is-at-least-32-bytes",
            "STBRAIN_HUMAN_TOKEN": "factory-human-token-that-is-at-least-32-bytes",
            "STBRAIN_HUMAN_ACTOR_ID": "human:factory",
        }
        with patch.dict(os.environ, environment, clear=False):
            application = build_application_from_env()

        onboarding = application.onboarding
        self.assertIsInstance(onboarding.planning_store, PlanningMemoryStore)
        self.assertIsInstance(
            onboarding.injection_control_store, InjectionControlStore
        )
        self.assertIsInstance(
            onboarding.hallucination_vault, HallucinationVaultStore
        )
        self.assertEqual(str(vault_database), onboarding.hallucination_vault.database)

    def test_source_frame_is_bounded_strict_and_not_copied_into_snapshot(self) -> None:
        def issue(event: str) -> dict:
            status, wake = self.post(
                "/v1/host/wakes",
                {
                    "host_id": "host:test",
                    "thread_id": "thread:test",
                    "source_kind": "human_message",
                    "source_event_id": event,
                },
                self.host_token,
            )
            self.assertEqual(200, status)
            return wake

        def prepare(event: str, frame: object) -> tuple[int, dict]:
            wake = issue(event)
            return self.post(
                "/v1/host/context/prepare",
                {
                    "wake_id": wake["wake_id"],
                    "wake_capability": wake["wake_capability"],
                    "source_digest": f"source:{event}",
                    "host_contract_digest": "host-contract:v1",
                    "source_frame": frame,
                },
                self.host_token,
            )

        raw_query = "这是只用于瞬时召回的本轮原句-unique-source-frame"
        status, result = prepare(
            "frame-valid",
            {
                "query_text": raw_query,
                "thread_id": "thread:test",
                "lineage_stable": True,
                "prior_assistant_present": True,
                "source_event_id": "frame-valid",
                "capture_items": [
                    {"role": "assistant", "content": "上一句"},
                    {"role": "user", "content": raw_query},
                ],
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("context_prepared", result["decision"])
        connection = sqlite3.connect(self.database)
        try:
            stable_json, dynamic_json = connection.execute(
                "SELECT stable_json, dynamic_json FROM brain_context_snapshots "
                "ORDER BY prepared_at DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual("{}", stable_json)
        self.assertEqual("{}", dynamic_json)
        self.assertNotIn(raw_query, stable_json + dynamic_json)

        invalid_frames = [
            "not-an-object",
            {"query_text": "ok", "unknown": True},
            {"query_text": "x" * 4001},
            {
                "query_text": "ok",
                "lineage_stable": False,
                "prior_assistant_present": "yes",
                "capture_items": [],
            },
            {
                "query_text": "ok",
                "lineage_stable": True,
                "thread_id": None,
                "source_event_id": "event",
                "capture_items": [],
            },
            {
                "query_text": "ok",
                "lineage_stable": False,
                "capture_items": [
                    {"role": "user", "content": str(index)} for index in range(5)
                ],
            },
            {
                "query_text": "ok",
                "lineage_stable": False,
                "capture_items": [{"role": "system", "content": "not allowed"}],
            },
            {
                "query_text": "ok",
                "lineage_stable": False,
                "capture_items": [{"role": "user", "content": "x" * 1201}],
            },
        ]
        for index, frame in enumerate(invalid_frames):
            with self.subTest(index=index):
                invalid_status, invalid = prepare(f"frame-invalid-{index}", frame)
                self.assertEqual(400, invalid_status)
                self.assertEqual("invalid_request", invalid["error"])

    def test_human_objection_route_freezes_but_cannot_edit_candidate(self) -> None:
        wake = self.issue_and_confirm()
        common = {
            "owner_id": "owner:test",
            "model_id": "model:test",
            "wake_id": wake["wake_id"],
            "wake_capability": wake["wake_capability"],
        }

        def advance(action: str, payload: dict) -> dict:
            opened = self.onboarding.open_brain_context(
                owner_id="owner:test", model_id="model:test"
            )
            self.assertTrue(opened["write_context_available"])
            row_version = self.onboarding.state(
                owner_id="owner:test", model_id="model:test"
            )["state"]["row_version"]
            return self.onboarding.advance(
                **common,
                action=action,
                expected_row_version=row_version,
                payload=payload,
            )

        advance("confirm_brain_intro", {"acknowledged": True})
        advance("confirm_module_intro", {"acknowledged": True})
        advance("save_calm_prompt", {"text": "停一下，先复核。"})
        submitted = advance(
            "submit_candidate",
            {
                "content": {
                    "schema_version": "0.1.0",
                    "boot_anchor": {"text": "我维护自己的长期自我模型。"},
                    "active_identity_capsule": {
                        "name_and_identity": "我是测试 AI。",
                        "personality_foundation": "我保持审慎并保留自主判断。",
                        "expression_style": "我会先给结论。",
                        "behavioral_principles": ["我不让他人代写意愿"],
                        "core_identity_anchors": ["我认可自己的复核"],
                        "self_revision_safety_prompt": "我会跨真实唤醒后再批准。",
                    },
                    "facets": {},
                    "anchor_references": [],
                },
                "diff": [{"op": "add", "path": "/active_identity_capsule"}],
                "reason": "这是我自己的长期判断。",
                "evidence_refs": ["memory://self/review"],
                "expected_active_revision": None,
            },
        )
        self.assertEqual("pending", submitted["decision"])
        status, result = self.post(
            "/v1/human/objections",
            {
                "candidate_id": submitted["candidate_id"],
                "reason": "发现潜在安全问题，请 AI 下轮复核。",
                "release_condition": "AI 读取异议并自行回应。",
                "request_id": "human-objection-1",
                "content": "这个字段不得被人类入口采用",
            },
            self.human_token,
        )
        self.assertEqual(200, status)
        self.assertEqual("pending", result["decision"])
        self.assertIn("human_objection_pending", result["reason_codes"])
        self.assertNotIn("content", result)


if __name__ == "__main__":
    unittest.main()
