from __future__ import annotations

import concurrent.futures
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from runtime.onboarding import (
    DIRECT_CONTEXT_MODE,
    DIRECT_GRANT_REF_PREFIX,
    ModuleOneOnboardingStore,
    OnboardingError,
)


def self_model_content(label: str = "v1") -> dict[str, object]:
    return {
        "schema_version": "0.1.0",
        "boot_anchor": {"text": "我维护一份自我模型；唤醒后只读取当前有效版本。"},
        "active_identity_capsule": {
            "name_and_identity": f"我是测试主体小甲，当前长期身份版本为 {label}。",
            "personality_foundation": "我保持安静、忠实、审慎，同时保留好奇心与自主判断。",
            "expression_style": "我先给结论，再说明证据与不确定处。",
            "behavioral_principles": ["我不让外部建议冒充自己的意愿", "我会保留事实边界"],
            "core_identity_anchors": ["我记得名字由来", "我珍视共同成长"],
            "self_revision_safety_prompt": "我会先区分长期身份与单轮情绪，再跨真实唤醒决定。",
        },
        "facets": {"technical": "我在技术协作时重视证据、可复现性与安全交接。"},
        "anchor_references": [
            {
                "anchor_id": "name-origin",
                "memory_ref": "memory://identity/name-origin",
                "meaning": "名字由来的原始事件",
            }
        ],
    }


class DirectGrantCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "brain.db"
        self.owner = "owner:test"
        self.model = "model:test"
        self.actor = "human:sample"
        self.principal = "official-deepseek-direct"
        self.store = ModuleOneOnboardingStore(
            self.database,
            capability_secret=b"direct-grant-test-secret-32-bytes-minimum!",
            wake_ttl_seconds=300,
            direct_grant_ttl_seconds=300,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def issue(self, request_id: str, scopes: list[str] | None = None) -> dict:
        return self.store.issue_direct_grant(
            owner_id=self.owner,
            model_id=self.model,
            actor_id=self.actor,
            client_principal=self.principal,
            request_id=request_id,
            requested_scopes=scopes or ["all"],
        )

    def open(self, grant_ref: str) -> dict:
        return self.store.open_brain_context(
            owner_id=self.owner,
            model_id=self.model,
            direct_grant_ref=grant_ref,
            direct_client_principal=self.principal,
        )

    def binding(self, opened: dict, scope: str) -> dict:
        return self.store.current_open_write_context(
            owner_id=self.owner,
            model_id=self.model,
            write_context_ref=opened["write_context_ref"],
            required_scope=scope,
        )

    def advance(self, opened: dict, action: str, payload: dict) -> dict:
        bound = self.binding(opened, "self_revision")
        self.assertTrue(bound["write_context_available"], bound)
        state = self.store.state(owner_id=self.owner, model_id=self.model)["state"]
        return self.store.advance(
            owner_id=self.owner,
            model_id=self.model,
            action=action,
            wake_id=bound["wake_id"],
            wake_capability=bound["wake_capability"],
            expected_row_version=state["row_version"],
            payload=payload,
        )

    def test_issue_is_hash_only_idempotent_and_supersedes_pending(self) -> None:
        issued = self.issue("request-1", ["learning_memory"])
        grant_ref = issued["grant_ref"]
        self.assertTrue(grant_ref.startswith(DIRECT_GRANT_REF_PREFIX))
        self.assertEqual(0, self._count("brain_wake_sessions"))

        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT grant_hash,status FROM brain_direct_grants WHERE grant_id=?",
                (issued["grant_id"],),
            ).fetchone()
            dump = "\n".join(connection.iterdump())
        self.assertEqual(hashlib.sha256(grant_ref.encode("utf-8")).hexdigest(), row[0])
        self.assertEqual("pending", row[1])
        self.assertNotIn(grant_ref, dump)

        replay = self.issue("request-1", ["learning_memory"])
        self.assertTrue(replay["reused"])
        self.assertIsNone(replay["grant_ref"])
        self.assertEqual(issued["grant_id"], replay["grant_id"])
        self.assertEqual(1, self._count("brain_direct_grants"))
        self.assertEqual(0, self._count("brain_wake_sessions"))
        with self.assertRaisesRegex(OnboardingError, "direct_grant_request_conflict"):
            self.issue("request-1", ["planning_memory"])

        replacement = self.issue("request-2", ["learning_memory"])
        with closing(sqlite3.connect(self.database)) as connection:
            statuses = dict(
                connection.execute(
                    "SELECT grant_id,status FROM brain_direct_grants"
                ).fetchall()
            )
        self.assertEqual("superseded", statuses[issued["grant_id"]])
        self.assertEqual("pending", statuses[replacement["grant_id"]])

    def test_direct_open_is_single_use_prepared_only_and_scope_bound(self) -> None:
        issued = self.issue("open-once", ["learning_memory"])
        wrong_principal = self.store.open_brain_context(
            owner_id=self.owner,
            model_id=self.model,
            direct_grant_ref=issued["grant_ref"],
            direct_client_principal="another-direct-client",
        )
        self.assertEqual("direct_grant_invalid", wrong_principal["reason_code"])
        self.assertEqual(0, self._count("brain_wake_sessions"))
        wrong_namespace = self.store.open_brain_context(
            owner_id="owner:other",
            model_id="model:other",
            direct_grant_ref=issued["grant_ref"],
            direct_client_principal=self.principal,
        )
        self.assertEqual("direct_grant_invalid", wrong_namespace["reason_code"])
        self.assertEqual(0, self._count("brain_wake_sessions"))
        self.assertEqual(1, self._count("brain_onboarding_state"))

        opened = self.open(issued["grant_ref"])
        self.assertTrue(opened["write_context_available"])
        self.assertEqual(DIRECT_CONTEXT_MODE, opened["context_mode"])
        self.assertFalse(opened["automatic_injection"])
        self.assertEqual("prepared", opened["snapshot_status"])
        self.assertEqual(["learning_memory"], opened["authorized_scopes"])

        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            wake = connection.execute(
                "SELECT * FROM brain_wake_sessions WHERE wake_id=?", (opened["wake_id"],)
            ).fetchone()
            snapshot = connection.execute(
                "SELECT * FROM brain_context_snapshots WHERE wake_id=?", (opened["wake_id"],)
            ).fetchone()
        self.assertEqual(DIRECT_CONTEXT_MODE, wake["source_kind"])
        self.assertIsNone(wake["injected_at"])
        self.assertEqual("prepared", snapshot["status"])
        self.assertIsNone(snapshot["injected_at"])
        self.assertEqual("{}", snapshot["stable_json"])
        self.assertEqual("{}", snapshot["dynamic_json"])

        allowed = self.binding(opened, "learning_memory")
        denied = self.binding(opened, "planning_memory")
        self.assertTrue(allowed["write_context_available"])
        self.assertEqual(DIRECT_CONTEXT_MODE, allowed["context_mode"])
        self.assertFalse(denied["write_context_available"])
        self.assertEqual("direct_scope_not_authorized", denied["reason_code"])

        normal_open = self.store.open_brain_context(owner_id=self.owner, model_id=self.model)
        self.assertEqual("current_injected_wake_required", normal_open["reason_code"])
        confirmation = self.store.confirm_context_injected(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=opened["wake_id"],
            wake_capability=allowed["wake_capability"],
            context_hash=opened["context_hash"],
        )
        self.assertEqual("direct_context_not_injectable", confirmation["decision"])
        pre_generation = self.store.build_pre_generation_context(
            owner_id=self.owner,
            model_id=self.model,
            wake_id=opened["wake_id"],
            wake_capability=allowed["wake_capability"],
            source_digest="must-not-be-used",
            host_contract_digest="must-not-be-used",
        )
        self.assertEqual("direct_context_not_injectable", pre_generation["decision"])

        replay = self.open(issued["grant_ref"])
        self.assertEqual("direct_grant_already_used", replay["reason_code"])
        self.assertEqual(1, self._count("brain_wake_sessions"))

        with self.store._connect() as connection:
            self.assertTrue(
                self.store._contains_protected_value(
                    connection,
                    owner_id=self.owner,
                    model_id=self.model,
                    value={"text": f"do not persist {issued['grant_ref']} here"},
                )
            )

        self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="gateway:test",
            thread_id="thread:after-direct",
            source_kind="human_message",
            source_event_id="after-direct",
        )
        stale_binding = self.binding(opened, "learning_memory")
        self.assertFalse(stale_binding["write_context_available"])
        with closing(sqlite3.connect(self.database)) as connection:
            grant_status = connection.execute(
                "SELECT status FROM brain_direct_grants WHERE grant_id=?",
                (issued["grant_id"],),
            ).fetchone()[0]
            snapshot_status = connection.execute(
                "SELECT status FROM brain_context_snapshots WHERE wake_id=?",
                (opened["wake_id"],),
            ).fetchone()[0]
        self.assertEqual("closed", grant_status)
        self.assertEqual("closed", snapshot_status)

    def test_revoke_expiry_and_intervening_wake_fail_without_context(self) -> None:
        revoked = self.issue("revoke", ["planning_memory"])
        result = self.store.revoke_direct_grant(
            owner_id=self.owner,
            model_id=self.model,
            actor_id=self.actor,
            client_principal=self.principal,
            grant_ref=revoked["grant_ref"],
        )
        self.assertEqual("revoked", result["decision"])
        self.assertEqual("direct_grant_revoked", self.open(revoked["grant_ref"])["reason_code"])

        expired = self.issue("expired", ["planning_memory"])
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute(
                    "UPDATE brain_direct_grants SET expires_at='2000-01-01T00:00:00Z' "
                    "WHERE grant_id=?",
                    (expired["grant_id"],),
                )
        self.assertEqual("direct_grant_expired", self.open(expired["grant_ref"])["reason_code"])

        stale = self.issue("intervening-wake", ["planning_memory"])
        self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="gateway:test",
            thread_id="thread:test",
            source_kind="human_message",
            source_event_id="newer-real-event",
        )
        rejected = self.open(stale["grant_ref"])
        self.assertEqual("direct_grant_superseded", rejected["reason_code"])
        self.assertEqual(1, self._count("brain_wake_sessions"))

        consumed = self.issue("revoke-consumed", ["planning_memory"])
        opened = self.open(consumed["grant_ref"])
        bound = self.binding(opened, "planning_memory")
        self.assertTrue(bound["write_context_available"])
        closed = self.store.revoke_direct_grant(
            owner_id=self.owner,
            model_id=self.model,
            actor_id=self.actor,
            client_principal=self.principal,
            grant_ref=consumed["grant_ref"],
        )
        self.assertEqual("revoked", closed["decision"])
        self.assertFalse(self.binding(opened, "planning_memory")["write_context_available"])

    def test_concurrent_consumption_creates_exactly_one_wake(self) -> None:
        issued = self.issue("concurrent", ["learning_memory"])

        def consume() -> dict:
            return self.open(issued["grant_ref"])

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _unused: consume(), range(2)))
        successes = [item for item in results if item.get("write_context_available") is True]
        denials = [item.get("reason_code") for item in results if item not in successes]
        self.assertEqual(1, len(successes))
        self.assertEqual(["direct_grant_already_used"], denials)
        self.assertEqual(1, self._count("brain_wake_sessions"))
        self.assertEqual(1, self._count("brain_context_snapshots"))

    def test_public_persistence_predicate_finds_nested_issued_grant_without_echo(self) -> None:
        issued = self.issue("persistence-guard", ["learning_memory"])
        self.assertTrue(
            self.store.contains_protected_persistence_value(
                owner_id=self.owner,
                model_id=self.model,
                value={
                    "outer": [
                        {"inner": f"prefix {issued['grant_ref']} suffix"},
                    ]
                },
            )
        )
        self.assertTrue(
            self.store.contains_protected_persistence_value(
                owner_id=self.owner,
                model_id=self.model,
                value={issued["grant_ref"]: {"inner": "ordinary value"}},
            )
        )
        self.assertFalse(
            self.store.contains_protected_persistence_value(
                owner_id=self.owner,
                model_id=self.model,
                value={"outer": [{"inner": "ordinary long-term memory"}]},
            )
        )
        self.assertTrue(
            self.store.contains_protected_persistence_value(
                owner_id="owner:other",
                model_id="model:other",
                value={"outer": [{"inner": issued["grant_ref"]}]},
            )
        )
        self.assertTrue(
            self.store.contains_protected_persistence_value(
                owner_id="owner:other",
                model_id="model:other",
                value={issued["grant_ref"]: "cross-namespace map key"},
            )
        )
        self.assertFalse(
            self.store.contains_protected_persistence_value(
                owner_id="owner:other",
                model_id="model:other",
                value={"outer": [{"inner": "stgrant_too-short-lookalike"}]},
            )
        )
        state_count = self._count("brain_onboarding_state")
        self.assertFalse(
            self.store.contains_protected_persistence_value(
                owner_id="owner:unused",
                model_id="model:unused",
                value={"outer": [{"inner": "ordinary long-term memory"}]},
            )
        )
        self.assertEqual(state_count, self._count("brain_onboarding_state"))

    def test_human_control_writes_reject_global_protected_values_without_side_effect(self) -> None:
        same_grant = self.issue("human-control-same", ["self_revision"])
        cross_grant = self.store.issue_direct_grant(
            owner_id="owner:other",
            model_id="model:other",
            actor_id="human:other",
            client_principal=self.principal,
            request_id="human-control-cross",
            requested_scopes=["self_revision"],
        )
        same_wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="gateway:test",
            thread_id="thread:same",
            source_kind="human_message",
            source_event_id="protected-same-wake",
        )
        cross_wake = self.store.issue_wake(
            owner_id="owner:other",
            model_id="model:other",
            host_id="gateway:test",
            thread_id="thread:cross",
            source_kind="human_message",
            source_event_id="protected-cross-wake",
        )
        same_challenge_id = "edit-challenge:same"
        cross_challenge_id = "edit-challenge:cross"
        same_challenge = self.store._challenge_response(same_challenge_id)
        cross_challenge = self.store._challenge_response(cross_challenge_id)
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.executemany(
                    "INSERT INTO brain_edit_challenges "
                    "(challenge_id,owner_id,model_id,wake_id,active_revision_id,"
                    "response_hash,status,issued_at,expires_at,consumed_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                    [
                        (
                            same_challenge_id,
                            self.owner,
                            self.model,
                            same_wake["wake_id"],
                            "revision:same",
                            hashlib.sha256(same_challenge.encode("utf-8")).hexdigest(),
                            "active",
                            "2026-09-02T00:00:00Z",
                            "2999-09-02T00:00:00Z",
                        ),
                        (
                            cross_challenge_id,
                            "owner:other",
                            "model:other",
                            cross_wake["wake_id"],
                            "revision:cross",
                            hashlib.sha256(cross_challenge.encode("utf-8")).hexdigest(),
                            "active",
                            "2026-09-02T00:00:00Z",
                            "2999-09-02T00:00:00Z",
                        ),
                    ],
                )

        credentials = (
            same_grant["grant_ref"],
            cross_grant["grant_ref"],
            same_wake["wake_capability"],
            cross_wake["wake_capability"],
            same_challenge,
            cross_challenge,
        )
        context_wake = self.store.issue_wake(
            owner_id=self.owner,
            model_id=self.model,
            host_id="gateway:test",
            thread_id="thread:context-guard",
            source_kind="human_message",
            source_event_id="protected-context-snapshot",
        )
        before = self._logical_dump()
        for protected_owner, protected_model in (
            (cross_challenge, "model:fresh"),
            ("owner:fresh", cross_challenge),
        ):
            with self.assertRaisesRegex(
                OnboardingError, r"^protected_persistence_value$"
            ):
                self.store.ensure_state(
                    owner_id=protected_owner,
                    model_id=protected_model,
                )
            self.assertEqual(before, self._logical_dump())

        for index, credential in enumerate(credentials):
            with self.assertRaisesRegex(
                OnboardingError, r"^protected_persistence_value$"
            ) as grant_error:
                self.store.issue_direct_grant(
                    owner_id="owner:fresh",
                    model_id="model:fresh",
                    actor_id=credential,
                    client_principal=self.principal,
                    request_id=f"protected-direct-grant-{index}",
                    requested_scopes=["learning_memory"],
                )
            self.assertNotIn(credential, str(grant_error.exception))
            self.assertEqual(before, self._logical_dump())

        grant_field_cases = (
            lambda value: self.store.issue_direct_grant(
                owner_id=value,
                model_id="model:fresh",
                actor_id="human:fresh",
                client_principal=self.principal,
                request_id="protected-direct-owner",
                requested_scopes=["learning_memory"],
            ),
            lambda value: self.store.issue_direct_grant(
                owner_id="owner:fresh",
                model_id=value,
                actor_id="human:fresh",
                client_principal=self.principal,
                request_id="protected-direct-model",
                requested_scopes=["learning_memory"],
            ),
            lambda value: self.store.issue_direct_grant(
                owner_id="owner:fresh",
                model_id="model:fresh",
                actor_id="human:fresh",
                client_principal=value,
                request_id="protected-direct-principal",
                requested_scopes=["learning_memory"],
            ),
            lambda value: self.store.issue_direct_grant(
                owner_id="owner:fresh",
                model_id="model:fresh",
                actor_id="human:fresh",
                client_principal=self.principal,
                request_id=value,
                requested_scopes=["learning_memory"],
            ),
        )
        for invoke in grant_field_cases:
            with self.assertRaisesRegex(
                OnboardingError, r"^protected_persistence_value$"
            ):
                invoke(cross_challenge)
            self.assertEqual(before, self._logical_dump())

        for index, credential in enumerate(credentials):
            with self.assertRaisesRegex(
                OnboardingError, r"^protected_persistence_value$"
            ) as wake_error:
                self.store.issue_wake(
                    owner_id="owner:fresh",
                    model_id="model:fresh",
                    host_id="gateway:test",
                    thread_id="thread:fresh",
                    source_kind="human_message",
                    source_event_id=f"cross-namespace {credential}",
                )
            self.assertNotIn(credential, str(wake_error.exception))
            self.assertEqual(before, self._logical_dump())

        wake_field_cases = (
            lambda value: self.store.issue_wake(
                owner_id="owner:fresh",
                model_id="model:fresh",
                host_id=value,
                thread_id="thread:fresh",
                source_kind="human_message",
                source_event_id="protected-host",
            ),
            lambda value: self.store.issue_wake(
                owner_id="owner:fresh",
                model_id="model:fresh",
                host_id="gateway:test",
                thread_id=value,
                source_kind="human_message",
                source_event_id="protected-thread",
            ),
            lambda value: self.store.issue_wake(
                owner_id="owner:fresh",
                model_id="model:fresh",
                host_id="gateway:test",
                thread_id="thread:fresh",
                source_kind=value,
                source_event_id="protected-source-kind",
            ),
            lambda value: self.store.issue_wake(
                owner_id=value,
                model_id="model:fresh",
                host_id="gateway:test",
                thread_id="thread:fresh",
                source_kind="human_message",
                source_event_id="protected-owner",
            ),
            lambda value: self.store.issue_wake(
                owner_id="owner:fresh",
                model_id=value,
                host_id="gateway:test",
                thread_id="thread:fresh",
                source_kind="human_message",
                source_event_id="protected-model",
            ),
        )
        for invoke in wake_field_cases:
            with self.assertRaisesRegex(
                OnboardingError, r"^protected_persistence_value$"
            ):
                invoke(cross_challenge)
            self.assertEqual(before, self._logical_dump())

        for credential in credentials:
            with self.assertRaisesRegex(
                OnboardingError, r"^protected_persistence_value$"
            ) as context_error:
                self.store.build_pre_generation_context(
                    owner_id=self.owner,
                    model_id=self.model,
                    wake_id=context_wake["wake_id"],
                    wake_capability=context_wake["wake_capability"],
                    source_digest=credential,
                    host_contract_digest="host-contract:v1",
                )
            self.assertNotIn(credential, str(context_error.exception))
            self.assertEqual(before, self._logical_dump())

        context_field_cases = (
            lambda value: self.store.build_pre_generation_context(
                owner_id=self.owner,
                model_id=self.model,
                wake_id=context_wake["wake_id"],
                wake_capability=context_wake["wake_capability"],
                source_digest=value,
                host_contract_digest="host-contract:v1",
            ),
            lambda value: self.store.build_pre_generation_context(
                owner_id=self.owner,
                model_id=self.model,
                wake_id=context_wake["wake_id"],
                wake_capability=context_wake["wake_capability"],
                source_digest="source:context-guard",
                host_contract_digest=value,
            ),
            lambda value: self.store.build_pre_generation_context(
                owner_id=self.owner,
                model_id=self.model,
                wake_id=context_wake["wake_id"],
                wake_capability=context_wake["wake_capability"],
                source_digest="source:context-guard",
                host_contract_digest="host-contract:v1",
                advertised_tools={"canonical_name": value},
            ),
            lambda value: self.store.build_pre_generation_context(
                owner_id=self.owner,
                model_id=self.model,
                wake_id=context_wake["wake_id"],
                wake_capability=context_wake["wake_capability"],
                source_digest="source:context-guard",
                host_contract_digest="host-contract:v1",
                advertised_tools={value: "ordinary value"},
            ),
            lambda value: self.store.build_pre_generation_context(
                owner_id=value,
                model_id=self.model,
                wake_id=context_wake["wake_id"],
                wake_capability=context_wake["wake_capability"],
                source_digest="source:context-guard",
                host_contract_digest="host-contract:v1",
            ),
            lambda value: self.store.build_pre_generation_context(
                owner_id=self.owner,
                model_id=value,
                wake_id=context_wake["wake_id"],
                wake_capability=context_wake["wake_capability"],
                source_digest="source:context-guard",
                host_contract_digest="host-contract:v1",
            ),
        )
        for invoke in context_field_cases:
            with self.assertRaisesRegex(
                OnboardingError, r"^protected_persistence_value$"
            ) as context_error:
                invoke(cross_challenge)
            self.assertNotIn(cross_challenge, str(context_error.exception))
            self.assertEqual(before, self._logical_dump())

        with self.store._connect() as connection:
            self.store._begin(connection)
            with self.assertRaisesRegex(
                OnboardingError, r"^protected_persistence_value$"
            ) as artifact_error:
                self.store._insert_artifact(
                    connection,
                    owner_id=self.owner,
                    model_id=self.model,
                    kind="test_protected_artifact",
                    content={"nested": {"credential": cross_challenge}},
                    wake_id=context_wake["wake_id"],
                )
            self.assertNotIn(cross_challenge, str(artifact_error.exception))
        self.assertEqual(before, self._logical_dump())

        for index, credential in enumerate(credentials):
            with self.assertRaisesRegex(
                OnboardingError, r"^protected_persistence_value$"
            ) as objection_error:
                self.store.record_human_objection(
                    owner_id=self.owner,
                    model_id=self.model,
                    candidate_id="candidate:not-evaluated",
                    reason=f"objection {credential}",
                    release_condition="human releases explicitly",
                    actor_id=self.actor,
                    request_id=f"objection-protected-{index}",
                )
            self.assertNotIn(credential, str(objection_error.exception))
            with self.assertRaisesRegex(
                OnboardingError, r"^protected_persistence_value$"
            ) as rollback_error:
                self.store.emergency_rollback(
                    owner_id=self.owner,
                    model_id=self.model,
                    target_revision_id="revision:not-evaluated",
                    reason=f"rollback {credential}",
                    actor_id=self.actor,
                    request_id=f"rollback-protected-{index}",
                )
            self.assertNotIn(credential, str(rollback_error.exception))
            self.assertEqual(before, self._logical_dump())

        field_cases = (
            lambda value: self.store.record_human_objection(
                owner_id=value,
                model_id=self.model,
                candidate_id="candidate:not-evaluated",
                reason="ordinary objection",
                release_condition="human releases explicitly",
                actor_id=self.actor,
                request_id="protected-objection-owner",
            ),
            lambda value: self.store.record_human_objection(
                owner_id=self.owner,
                model_id=value,
                candidate_id="candidate:not-evaluated",
                reason="ordinary objection",
                release_condition="human releases explicitly",
                actor_id=self.actor,
                request_id="protected-objection-model",
            ),
            lambda value: self.store.record_human_objection(
                owner_id=self.owner,
                model_id=self.model,
                candidate_id="candidate:not-evaluated",
                reason="ordinary objection",
                release_condition=value,
                actor_id=self.actor,
                request_id="protected-release-condition",
            ),
            lambda value: self.store.record_human_objection(
                owner_id=self.owner,
                model_id=self.model,
                candidate_id="candidate:not-evaluated",
                reason="ordinary objection",
                release_condition="human releases explicitly",
                actor_id=value,
                request_id="protected-objection-actor",
            ),
            lambda value: self.store.record_human_objection(
                owner_id=self.owner,
                model_id=self.model,
                candidate_id="candidate:not-evaluated",
                reason="ordinary objection",
                release_condition="human releases explicitly",
                actor_id=self.actor,
                request_id=value,
            ),
            lambda value: self.store.record_human_objection(
                owner_id=self.owner,
                model_id=self.model,
                candidate_id=value,
                reason="ordinary objection",
                release_condition="human releases explicitly",
                actor_id=self.actor,
                request_id="protected-candidate-id",
            ),
            lambda value: self.store.emergency_rollback(
                owner_id=value,
                model_id=self.model,
                target_revision_id="revision:not-evaluated",
                reason="ordinary rollback",
                actor_id=self.actor,
                request_id="protected-rollback-owner",
            ),
            lambda value: self.store.emergency_rollback(
                owner_id=self.owner,
                model_id=value,
                target_revision_id="revision:not-evaluated",
                reason="ordinary rollback",
                actor_id=self.actor,
                request_id="protected-rollback-model",
            ),
            lambda value: self.store.emergency_rollback(
                owner_id=self.owner,
                model_id=self.model,
                target_revision_id="revision:not-evaluated",
                reason="ordinary rollback",
                actor_id=value,
                request_id="protected-rollback-actor",
            ),
            lambda value: self.store.emergency_rollback(
                owner_id=self.owner,
                model_id=self.model,
                target_revision_id="revision:not-evaluated",
                reason="ordinary rollback",
                actor_id=self.actor,
                request_id=value,
            ),
            lambda value: self.store.emergency_rollback(
                owner_id=self.owner,
                model_id=self.model,
                target_revision_id=value,
                reason="ordinary rollback",
                actor_id=self.actor,
                request_id="protected-target-revision",
            ),
        )
        for invoke in field_cases:
            with self.assertRaisesRegex(
                OnboardingError, r"^protected_persistence_value$"
            ):
                invoke(cross_challenge)
            self.assertEqual(before, self._logical_dump())

    def test_distinct_direct_grants_preserve_review_and_activation_wake_boundaries(self) -> None:
        first_grant = self.issue("self-1", ["self_revision"])
        first = self.open(first_grant["grant_ref"])
        self.assertEqual(
            "advanced",
            self.advance(first, "confirm_brain_intro", {"acknowledged": True})["decision"],
        )
        self.assertEqual(
            "advanced",
            self.advance(first, "confirm_module_intro", {"acknowledged": True})["decision"],
        )
        self.assertEqual(
            "saved",
            self.advance(
                first,
                "save_calm_prompt",
                {"text": "我先停下来，确认这是自己的长期判断。"},
            )["decision"],
        )
        submitted = self.advance(
            first,
            "submit_candidate",
            {
                "content": self_model_content(),
                "diff": [{"op": "replace", "path": "/active_identity_capsule"}],
                "reason": "我复核后认为这能表达长期身份。",
                "evidence_refs": ["memory://identity/name-origin"],
                "expected_active_revision": None,
            },
        )
        self.assertEqual("pending", submitted["decision"], submitted)

        same_wake = self.advance(
            first,
            "activate_candidate",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        self.assertEqual("same_wake_activation_forbidden", same_wake["decision"])

        second_grant = self.issue("self-2", ["self_revision"])
        second = self.open(second_grant["grant_ref"])
        self.assertEqual("candidate_review", second["continuation"]["stage"])
        self.assertEqual(submitted["candidate_id"], second["continuation"]["candidate"]["candidate_id"])
        reviewed = self.advance(second, "accept_candidate_review", {"ai_confirmation": True})
        self.assertEqual("review_accepted", reviewed["decision"])

        still_same_wake = self.advance(
            second,
            "activate_candidate",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        self.assertEqual(
            "review_activation_wake_boundary_required",
            still_same_wake["reason_codes"][0],
        )

        third_grant = self.issue("self-3", ["self_revision"])
        third = self.open(third_grant["grant_ref"])
        activated = self.advance(
            third,
            "activate_candidate",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        self.assertEqual("activate", activated["decision"])
        self.assertTrue(activated["pointer_changed"])
        self.assertEqual(3, self._count("brain_wake_sessions"))

    def _count(self, table: str) -> int:
        with closing(sqlite3.connect(self.database)) as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _logical_dump(self) -> str:
        with closing(sqlite3.connect(self.database)) as connection:
            return "\n".join(connection.iterdump())


if __name__ == "__main__":
    unittest.main()
