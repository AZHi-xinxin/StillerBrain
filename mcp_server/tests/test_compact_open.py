"""Isolated compact-open safety contracts; never connect to a running brain.

All persisted state below belongs to TemporaryDirectory and is synthetic.
These tests intentionally exercise presentation proof, not just response size.
"""

from __future__ import annotations

import copy
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp_server.planning_service import PlanningMemoryAccessService
from mcp_server.service import SelfModelAccessService
from mcp_server.tests.test_onboarding_facade import model_content
from mcp_server.tests.test_planning_service import calm, plan_content
from runtime import ModuleOneOnboardingStore, SelfModelStore, SelfRevisionError
from runtime.planning_memory import PlanningMemoryStore


class CompactOpenStateTests(unittest.TestCase):
    def setUp(self) -> None:
        for target in ("socket.socket", "socket.create_connection"):
            self.enterContext(mock.patch(target, side_effect=AssertionError("offline test")))
        self.temp = tempfile.TemporaryDirectory(prefix="compact-open-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "synthetic-brain.db"
        self.onboarding = ModuleOneOnboardingStore(
            self.database,
            capability_secret=b"compact-open-isolated-test-secret-32-bytes",
            wake_ttl_seconds=300,
            edit_challenge_ttl_seconds=300,
        )
        self.service = SelfModelAccessService(
            SelfModelStore(self.database),
            model_id="model:compact-test",
            owner_id="owner:compact-test",
            onboarding=self.onboarding,
        )

    def wake(self, event: str, *, injected: bool = True) -> dict:
        wake = self.onboarding.issue_wake(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            host_id="host:compact-test",
            thread_id="thread:compact-test",
            source_kind="human_message",
            source_event_id=event,
        )
        if injected:
            prepared = self.onboarding.build_pre_generation_context(
                owner_id=self.service.owner_id,
                model_id=self.service.model_id,
                wake_id=wake["wake_id"],
                wake_capability=wake["wake_capability"],
                source_digest=f"synthetic:{event}",
                host_contract_digest="synthetic-contract:v1",
            )
            confirmed = self.onboarding.confirm_context_injected(
                owner_id=self.service.owner_id,
                model_id=self.service.model_id,
                wake_id=wake["wake_id"],
                wake_capability=wake["wake_capability"],
                context_hash=prepared["context_hash"],
            )
            self.assertEqual("injected", confirmed["decision"])
        return wake

    def runtime_open(self, *, present_details: bool) -> dict:
        return self.onboarding.open_brain_context(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            present_details=present_details,
        )

    def proof_count(self, kind: str, wake: dict) -> int:
        # Only this test's fresh temporary database is queried.
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                "SELECT count(*) FROM brain_onboarding_artifacts "
                "WHERE kind = ? AND created_wake_id = ? AND status = 'active'",
                (kind, wake["wake_id"]),
            ).fetchone()[0]

    def advance(self, wake: dict, action: str, payload: dict) -> dict:
        status = self.service.module_one_status()
        return self.onboarding.advance(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"],
            expected_row_version=status["state"]["row_version"],
            action=action,
            payload=payload,
        )

    def candidate_wait(self) -> dict:
        wake = self.wake("candidate-author")
        for action, payload in (
            ("confirm_brain_intro", {"acknowledged": True}),
            ("confirm_module_intro", {"acknowledged": True}),
            ("save_calm_prompt", {"text": "我先复核自己的长期意愿。"}),
        ):
            self.runtime_open(present_details=True)
            self.advance(wake, action, payload)
        self.runtime_open(present_details=True)
        candidate = self.advance(
            wake,
            "submit_candidate",
            {
                "content": model_content("compact-synthetic"),
                "diff": [{"op": "replace", "path": "/active_identity_capsule"}],
                "reason": "我选择这份纯隔离验例的长期描述。",
                "evidence_refs": ["memory://synthetic/compact"],
                "expected_active_revision": None,
            },
        )
        self.assertEqual("pending", candidate["decision"])
        return candidate

    def bootstrap_live(self) -> None:
        self.candidate_wait()
        review_wake = self.wake("candidate-review")
        self.runtime_open(present_details=True)
        accepted = self.advance(
            review_wake, "accept_candidate_review", {"ai_confirmation": True}
        )
        self.assertEqual("review_accepted", accepted["decision"])
        active_wake = self.wake("candidate-activation")
        self.runtime_open(present_details=True)
        activated = self.advance(
            active_wake,
            "activate_candidate",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        self.assertEqual("activate", activated["decision"])

    def test_summary_still_requires_current_confirmed_injection(self) -> None:
        unopened = self.runtime_open(present_details=False)
        self.assertFalse(unopened["write_context_available"])
        wake = self.wake("not-injected", injected=False)
        uninjected = self.runtime_open(present_details=False)
        self.assertFalse(uninjected["write_context_available"])
        self.assertNotIn("write_context_ref", uninjected)
        self.assertEqual(0, self.proof_count("brain_manual_opened", wake))

    def test_summary_neither_advances_candidate_wait_nor_builds_continuation(self) -> None:
        candidate = self.candidate_wait()
        wake = self.wake("summary-only-review")
        before = self.service.module_one_status()["state"]
        with mock.patch.object(
            self.onboarding,
            "_continuation_block",
            side_effect=AssertionError("summary must not construct details"),
        ):
            opened = self.runtime_open(present_details=False)
        self.assertTrue(opened["write_context_available"])
        self.assertIsNone(opened.get("continuation"))
        self.assertEqual(before, opened["current_status"]["state"])
        self.assertEqual("candidate_wait", opened["current_status"]["state"]["stage"])
        self.assertEqual(0, self.proof_count("candidate_full_review", wake))
        full = self.runtime_open(present_details=True)
        self.assertEqual(opened["write_context_ref"], full["write_context_ref"])
        self.assertEqual("candidate_review", full["current_status"]["state"]["stage"])
        self.assertEqual(candidate["candidate_id"], full["continuation"]["candidate"]["candidate_id"])
        self.assertEqual(1, self.proof_count("candidate_full_review", wake))

    def test_summary_does_not_create_human_objection_presentation(self) -> None:
        candidate = self.candidate_wait()
        self.onboarding.record_human_objection(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            candidate_id=candidate["candidate_id"],
            reason="Synthetic human objection for a temporary fixture only.",
            release_condition="Synthetic fixture review.",
            actor_id="human:synthetic",
            request_id="synthetic-objection-request",
        )
        wake = self.wake("summary-objection")
        self.runtime_open(present_details=False)
        self.assertEqual(0, self.proof_count("human_objection_presented", wake))
        full = self.runtime_open(present_details=True)
        self.assertIn("human_objection", full["continuation"])
        self.assertEqual(1, self.proof_count("human_objection_presented", wake))

    def test_old_full_review_cannot_be_reused_after_new_summary_wake(self) -> None:
        self.candidate_wait()
        previous = self.wake("full-review-previous")
        self.runtime_open(present_details=True)
        self.assertEqual(1, self.proof_count("candidate_full_review", previous))
        current = self.wake("summary-review-current")
        self.runtime_open(present_details=False)
        rejected = self.advance(
            current, "accept_candidate_review", {"ai_confirmation": True}
        )
        self.assertEqual("reject", rejected["decision"])
        self.assertIn("candidate_full_review_required", rejected["reason_codes"])
        self.assertEqual(0, self.proof_count("candidate_full_review", current))
        self.runtime_open(present_details=True)
        accepted = self.advance(
            current, "accept_candidate_review", {"ai_confirmation": True}
        )
        self.assertEqual("review_accepted", accepted["decision"])

    def test_summary_cannot_activate_previous_wake_accepted_candidate(self) -> None:
        self.candidate_wait()
        reviewed = self.wake("review-before-summary-activation")
        self.runtime_open(present_details=True)
        self.assertEqual(
            "review_accepted",
            self.advance(reviewed, "accept_candidate_review", {"ai_confirmation": True})["decision"],
        )
        current = self.wake("summary-activation")
        self.runtime_open(present_details=False)
        denied = self.advance(
            current,
            "activate_candidate",
            {"expected_active_revision": None, "ai_confirmation": True},
        )
        self.assertEqual("reject", denied["decision"])
        self.assertIn("candidate_full_review_required", denied["reason_codes"])
        self.assertEqual(0, self.proof_count("candidate_full_review", current))

    def test_summary_preserves_existing_same_wake_full_review_proof(self) -> None:
        self.candidate_wait()
        wake = self.wake("manual-then-summary")
        manual = self.service.open_brain(view="manual", module="self_revision")
        summary = self.service.open_brain(view="summary")
        self.assertEqual(manual["write_context_ref"], summary["write_context_ref"])
        self.assertEqual(1, self.proof_count("candidate_full_review", wake))
        accepted = self.advance(
            wake, "accept_candidate_review", {"ai_confirmation": True}
        )
        self.assertEqual("review_accepted", accepted["decision"])

    def test_same_wake_summary_and_selected_manual_reuse_one_ref(self) -> None:
        wake = self.wake("repeated-open")
        one = self.service.open_brain(view="summary")
        manual = self.service.open_brain(view="manual", module="self_revision")
        two = self.service.open_brain(view="summary")
        self.assertTrue(one["write_context_available"])
        self.assertEqual(one["write_context_ref"], manual["write_context_ref"])
        self.assertEqual(one["write_context_ref"], two["write_context_ref"])
        self.assertEqual(1, self.proof_count("brain_manual_opened", wake))
        rendered = json.dumps([one, manual, two])
        self.assertNotIn(wake["wake_capability"], rendered)
        self.assertNotIn('"wake_id"', rendered)

    def test_new_summary_wake_does_not_accept_previous_open_ref(self) -> None:
        self.wake("old-summary-ref")
        old = self.service.open_brain(view="summary")
        self.wake("new-summary-ref")
        current = self.service.open_brain(view="summary")
        self.assertNotEqual(old["write_context_ref"], current["write_context_ref"])
        binding = self.onboarding.current_open_write_context(
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
            write_context_ref=old["write_context_ref"],
            required_scope="self_revision",
        )
        self.assertIs(binding["write_context_available"], False)

    def test_locked_other_module_manual_cannot_present_self_candidate(self) -> None:
        self.candidate_wait()
        wake = self.wake("wrong-module-review")
        opened = self.service.open_brain(view="manual", module="planning_memory")
        self.assertEqual("candidate_wait", opened["current_status"]["state"]["stage"])
        self.assertEqual(0, self.proof_count("candidate_full_review", wake))
        self.assertIsNone(opened.get("continuation"))

    def test_planning_legacy_candidate_manual_exposes_exact_review_without_weakening_binding(self) -> None:
        self.bootstrap_live()
        planning = PlanningMemoryAccessService(
            PlanningMemoryStore(self.database),
            onboarding=self.onboarding,
            owner_id=self.service.owner_id,
            model_id=self.service.model_id,
        )
        self.service.planning = planning
        opened = self.service.open_brain(view="summary")
        # Public remember now stores an active plan immediately. Construct only
        # this historical pending fixture through the explicit legacy primitive.
        binding = self.onboarding.current_open_write_context(
            owner_id=self.service.owner_id, model_id=self.service.model_id,
            write_context_ref=opened["write_context_ref"], required_scope="planning_memory",
        )
        self.assertTrue(binding["write_context_available"])
        pending = planning.store.propose_create(
            owner_id=self.service.owner_id, model_id=self.service.model_id,
            wake_id=binding["wake_id"], wake_seq=binding["wake_seq"],
            expected_row_version=planning.status()["row_version"],
            content=plan_content("纯隔离规划验例"),
            reason="我选择保存这条隔离验例。",
            calm_check=calm(),
            ai_confirmation=True,
            idempotency_key="synthetic-compact-plan",
        )
        self.assertEqual("candidate_pending", pending["decision"])
        same_wake = planning.manual(write_context_ref=opened["write_context_ref"])
        candidate = same_wake["pending_changes"][0]
        self.wake("planning-summary-later")
        summary = self.service.open_brain(view="summary")

        def review(version: int, **overrides) -> dict:
            arguments = dict(
                write_context_ref=summary["write_context_ref"], expected_planning_version=version,
                candidate_id=candidate["candidate_id"], expected_candidate_version=candidate["candidate_version"],
                expected_candidate_hash=candidate["candidate_hash"], expected_base_version=candidate["base_version"],
                decision="accept", reason="我明确决定接受这条隔离验例。", ai_confirmation=True,
            )
            return planning.review(**{**arguments, **overrides})

        self.assertNotIn("pending_changes", summary["planning_memory"])
        # Summary is not full candidate content, but a presentation stamp is no
        # longer authorization. Exact hash, version, current binding and CAS are.
        for override, reason in (
            ({"write_context_ref": opened["write_context_ref"]}, "brain_open_required"),
            ({"expected_candidate_hash": "0" * 64}, "candidate_hash_mismatch"),
            ({"expected_candidate_version": candidate["candidate_version"] + 1}, "candidate_version_mismatch"),
            ({"expected_base_version": candidate["base_version"] + 1}, "candidate_base_mismatch"),
        ):
            version = planning.status()["row_version"]
            denied = review(version, **override)
            self.assertEqual("reject", denied["decision"])
            self.assertIn(reason, denied["reason_codes"])
            self.assertFalse(denied["state_changed"])
            self.assertEqual(version, planning.status()["row_version"])
        version = planning.status()["row_version"]
        stale_row = review(version - 1)
        self.assertIn("planning_row_version_conflict", stale_row["reason_codes"])
        self.assertFalse(stale_row["state_changed"])
        self.assertEqual(version, planning.status()["row_version"])
        selected = self.service.open_brain(view="manual", module="planning_memory")
        self.assertEqual(summary["write_context_ref"], selected["write_context_ref"])
        projection = selected["planning_memory"]
        self.assertTrue(projection["pending_changes"][0]["fully_presented"])
        self.assertFalse(projection["pending_changes"][0]["review_requires_later_wake"])
        self.assertEqual(candidate["candidate_hash"], projection["pending_changes"][0]["candidate_hash"])
        self.assertEqual("candidate_accepted", review(projection["planning_row_version"])["decision"])


class CompactOpenRoutingTests(unittest.TestCase):
    MODULES = {
        "emotional_memory": "emotional",
        "learning_memory": "learning",
        "tool_guidance": "tool_guidance",
        "planning_memory": "planning",
        "self_governance_profile": "governance",
        "injection_control": "injection_control",
        "hallucination_vault": "hallucination_vault",
        "shared_person_authoring": "authoring_rewrite",
    }

    def setUp(self) -> None:
        for target in ("socket.socket", "socket.create_connection"):
            self.enterContext(mock.patch(target, side_effect=AssertionError("offline test")))
        self.temp = tempfile.TemporaryDirectory(prefix="compact-route-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.status = {
            "module": "self_revision_module_one",
            "flow_version": "synthetic/1",
            "state": {"stage": "live", "row_version": 3, "base_revision_id": None},
            "allowed_actions": [],
            "next_action": "Synthetic live state.",
            "wake_boundary_required": False,
            "module_one_unlocked": True,
        }
        self.onboarding = mock.Mock()
        self.onboarding.governance_store = None
        self.onboarding.state.return_value = copy.deepcopy(self.status)
        self.onboarding.open_brain_context.side_effect = lambda **_kwargs: {
            "current_status": copy.deepcopy(self.status),
            "continuation": None,
            "write_context_available": True,
            "write_context_ref": "synthetic-current-ref",
            "row_version": 3,
            "wake_id": "synthetic-private-wake",
            "wake_capability": "synthetic-private-capability",
            "grant_ref": "synthetic-private-grant",
        }
        self.modules = {}
        for name, attr in self.MODULES.items():
            module = mock.Mock()
            module.status.return_value = {
                "status": "available", "row_version": 7,
                "counts": {"pending_changes": 1, "pending_pins": 1},
                "configured_scope_count": 1, "global_mode": "normal",
                "automatic_exposure": False, "warning_version": 0,
                "scopes": {"synthetic": {"row_version": 7, "pending_candidates": [
                    {"content": f"do-not-leak-status-body:{name}", "candidate_hash": "synthetic-hash"}
                ]}},
                "pending_changes": [{"content": f"do-not-leak-status-body:{name}"}],
            }
            module.manual.side_effect = lambda _name=name, **_kwargs: {
                "sentinel_body": f"full-selected-body:{_name}",
                "pending_changes": [{"content": f"full-selected-body:{_name}", "fully_presented": True}],
                "current_action_contract": {"allowed_calls": [{"tool": "synthetic-review"}]},
            }
            module.current_action_contract.return_value = {"pending_activations": []}
            self.modules[name] = module
        self.service = SelfModelAccessService(
            SelfModelStore(Path(self.temp.name) / "synthetic-router.db"),
            owner_id="owner:synthetic-routing", model_id="model:synthetic-routing",
            onboarding=self.onboarding,
            **{attr: self.modules[name] for name, attr in self.MODULES.items()},
        )

    def test_summary_does_not_call_any_module_manual_or_leak_pending_bodies(self) -> None:
        opened = self.service.open_brain(view="summary")
        self.assertEqual("synthetic-current-ref", opened["write_context_ref"])
        self.assertIs(self.onboarding.open_brain_context.call_args.kwargs["present_details"], False)
        for module in self.modules.values():
            module.manual.assert_not_called()
            module.current_action_contract.assert_not_called()
        rendered = json.dumps(opened)
        for forbidden in ("do-not-leak-status-body", "full-selected-body", "synthetic-private-", "$.write_context_ref"):
            self.assertNotIn(forbidden, rendered)
        self.assertNotIn("current_action_contract", opened)
        self.assertNotIn("module_one_content_schema", opened)

    def test_each_manual_returns_exact_selected_body_and_calls_no_other_module(self) -> None:
        for selected in self.MODULES:
            with self.subTest(module=selected):
                for module in self.modules.values():
                    module.reset_mock()
                self.onboarding.reset_mock()
                opened = self.service.open_brain(view="manual", module=selected)
                self.assertIs(self.onboarding.open_brain_context.call_args.kwargs["present_details"], False)
                self.assertEqual(f"full-selected-body:{selected}", opened[selected]["sentinel_body"])
                self.assertEqual("synthetic-current-ref", opened["write_context_ref"])
                for name, module in self.modules.items():
                    if name == selected:
                        module.manual.assert_called_once()
                    else:
                        module.manual.assert_not_called()
                        module.current_action_contract.assert_not_called()
                    if name != selected:
                        self.assertNotIn(f"full-selected-body:{name}", json.dumps(opened))
                self.assertNotIn("synthetic-private-", json.dumps(opened))

    def test_invalid_selector_is_rejected_before_open_or_manual(self) -> None:
        for fields in (
            {"view": "invalid"},
            {"view": "manual", "module": "invalid"},
            {"view": "summary", "module": "invalid"},
        ):
            with self.subTest(fields=fields):
                self.onboarding.reset_mock()
                with self.assertRaises((ValueError, SelfRevisionError)):
                    self.service.open_brain(**fields)
                self.onboarding.open_brain_context.assert_not_called()
                for module in self.modules.values():
                    module.manual.assert_not_called()

    def test_self_manual_never_presents_another_module(self) -> None:
        self.service.open_brain(view="manual", module="self_revision")
        for module in self.modules.values():
            module.manual.assert_not_called()
            module.current_action_contract.assert_not_called()

    def test_direct_grant_cannot_be_consumed_by_compact_view(self) -> None:
        for view in ("summary", "manual"):
            with self.subTest(view=view):
                with self.assertRaises(SelfRevisionError):
                    self.service.open_brain(
                        view=view, direct_grant_ref="synthetic-unconsumed-grant"
                    )
                self.onboarding.open_brain_context.assert_not_called()

    def test_locked_module_selection_never_calls_candidate_presentation(self) -> None:
        self.status["module_one_unlocked"] = False
        for selected in self.MODULES:
            with self.subTest(module=selected):
                self.service.open_brain(view="manual", module=selected)
        for module in self.modules.values():
            module.manual.assert_not_called()
            module.current_action_contract.assert_not_called()


if __name__ == "__main__":
    unittest.main()
