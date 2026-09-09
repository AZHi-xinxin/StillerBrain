"""Synthetic-only coverage for additive, non-sensitive write binding diagnostics."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mcp_server.planning_service import PlanningMemoryAccessService
from mcp_server.tests.test_planning_service import calm, plan_content
from runtime.planning_memory import PlanningMemoryError, PlanningMemoryStore


class FakeOnboarding:
    def __init__(self) -> None:
        self.allowed = True
        self.protected = False
        self.lookups: list[dict[str, object]] = []
        self.binding: dict[str, object] = {
            "write_context_available": True,
            "wake_id": "synthetic-wake",
            "wake_seq": 3,
        }

    def authorize_other_module_write(self, **_fields: object) -> dict[str, object]:
        return {"decision": "allowed" if self.allowed else "reject"}

    def current_open_write_context(self, **fields: object) -> dict[str, object]:
        self.lookups.append(fields)
        return self.binding

    def contains_protected_persistence_value(self, **_fields: object) -> bool:
        return self.protected


class FakeStore:
    def __init__(self) -> None:
        self.writes: list[dict[str, object]] = []
        self.error: str | None = None

    def ensure_state(self, **_fields: object) -> None:
        pass

    def status(self, **_fields: object) -> dict[str, object]:
        return {"row_version": 7, "counts": {"pending_changes": 0}}

    def _write(self, **fields: object) -> dict[str, object]:
        self.writes.append(fields)
        if self.error is not None:
            raise PlanningMemoryError(self.error)
        return {
            "decision": "candidate_pending",
            "state_changed": True,
            "active_plan_changed": False,
            "planning_row_version": 8,
        }

    propose_create = _write
    propose_revision = _write
    record_event = _write
    review_change = _write


class OfflineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        for target in ("socket.socket", "socket.create_connection"):
            self.enterContext(
                patch(target, side_effect=AssertionError("network forbidden in synthetic test"))
            )


class PlanningBindingDiagnosticsTests(OfflineTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(
            patch("sqlite3.connect", side_effect=AssertionError("DB forbidden in fake test"))
        )
        self.store = FakeStore()
        self.onboarding = FakeOnboarding()
        self.service = PlanningMemoryAccessService(
            self.store,  # type: ignore[arg-type]
            onboarding=self.onboarding,  # type: ignore[arg-type]
            owner_id="synthetic-owner",
            model_id="synthetic-model",
        )

    def remember(self, ref: object = "synthetic-open-ref") -> dict[str, object]:
        return self.service.remember(
            write_context_ref=ref,  # type: ignore[arg-type]
            expected_planning_version=7,
            content={"synthetic": True},
        )

    def assert_binding_rejection(
        self,
        result: dict[str, object],
        detail: str,
        legacy_reason: str = "brain_open_required",
    ) -> None:
        self.assertEqual("reject", result["decision"])
        self.assertEqual([legacy_reason], result["reason_codes"])
        self.assertIs(False, result["state_changed"])
        self.assertIs(False, result["active_plan_changed"])
        self.assertEqual(detail, result["binding_reason_code"])
        self.assertEqual([], self.store.writes)
        self.assertEqual(7, self.service.status()["row_version"])

    def test_missing_and_empty_ref_are_distinct_from_unopened_ref(self) -> None:
        for ref in (None, "", " \t\n", False, 7, [], {}):
            with self.subTest(ref_type=type(ref).__name__):
                self.assert_binding_rejection(self.remember(ref), "write_context_ref_required")
        self.assertEqual([], self.onboarding.lookups)
        self.onboarding.binding = {
            "write_context_available": False,
            "reason_code": "brain_open_required",
        }
        self.assert_binding_rejection(self.remember(), "brain_open_required")

    def test_fixed_runtime_failure_codes_are_preserved_without_authority(self) -> None:
        for reason in (
            "brain_open_required",
            "current_injected_wake_required",
            "direct_grant_expired",
            "direct_grant_required",
            "direct_grant_invalid",
            "direct_scope_not_authorized",
        ):
            with self.subTest(reason=reason):
                self.onboarding.binding = {
                    "write_context_available": False,
                    "reason_code": reason,
                }
                self.assert_binding_rejection(self.remember(), reason)
                self.assertEqual(
                    {
                        "owner_id": "synthetic-owner",
                        "model_id": "synthetic-model",
                        "write_context_ref": "synthetic-open-ref",
                        "required_scope": "planning_memory",
                    },
                    self.onboarding.lookups[-1],
                )

    def test_literal_jsonpath_is_rejected_without_resolving_a_ref(self) -> None:
        for ref in ("$.write_context_ref", "  $.write_context_ref\n"):
            self.assert_binding_rejection(self.remember(ref), "write_context_ref_placeholder")
        self.assertEqual([], self.onboarding.lookups)
        self.onboarding.allowed = False
        self.assert_binding_rejection(
            self.remember("$.write_context_ref"),
            "write_context_ref_placeholder",
            "module_one_required",
        )
        self.assertEqual([], self.onboarding.lookups)

    def test_supplemental_fixed_reason_takes_precedence_over_legacy_reason(self) -> None:
        for reason in (
            "current_wake_required",
            "write_context_expired",
            "injected_context_required",
            "write_context_not_opened_or_mismatched",
            "write_context_binding_mismatch",
        ):
            with self.subTest(reason=reason):
                self.onboarding.binding = {
                    "write_context_available": False,
                    "reason_code": "brain_open_required",
                    "binding_reason_code": reason,
                }
                self.assert_binding_rejection(self.remember(), reason)

    def test_unknown_supplemental_reason_is_redacted_not_copied_or_authorized(self) -> None:
        sentinel = "synthetic-sensitive-supplemental-value"
        for reason in (sentinel, {"private": sentinel}, [sentinel], None):
            with self.subTest(reason_type=type(reason).__name__):
                self.onboarding.binding = {
                    "write_context_available": False,
                    "reason_code": "brain_open_required",
                    "binding_reason_code": reason,
                }
                result = self.remember()
                self.assert_binding_rejection(result, "write_context_binding_unavailable")
                self.assertNotIn(sentinel, json.dumps(result))

    def test_unknown_reason_and_binding_payload_are_never_reflected(self) -> None:
        sentinel = "synthetic-sensitive-value-never-return"
        for reason in (sentinel, {"private": sentinel}, [sentinel], None, False, 9):
            with self.subTest(reason_type=type(reason).__name__):
                self.onboarding.binding = {
                    "write_context_available": False,
                    "reason_code": reason,
                    "wake_id": sentinel,
                    "wake_capability": sentinel,
                    "snapshot": {"private": sentinel},
                    "context_hash": sentinel,
                    "write_context_ref": sentinel,
                }
                before = deepcopy(self.onboarding.binding)
                result = self.remember(sentinel)
                self.assert_binding_rejection(result, "write_context_binding_unavailable")
                self.assertNotIn(sentinel, json.dumps(result))
                self.assertEqual(before, self.onboarding.binding)
                self.assertEqual(
                    {
                        "module", "contract_version", "decision", "reason_codes",
                        "status", "state_changed", "active_plan_changed", "binding_reason_code",
                    },
                    set(result),
                )

    def test_module_lock_does_not_lookup_or_write_context(self) -> None:
        self.onboarding.allowed = False
        self.assert_binding_rejection(
            self.remember(), "module_one_required", "module_one_required"
        )
        self.assertEqual([], self.onboarding.lookups)

    def test_truthy_non_boolean_availability_does_not_authorize(self) -> None:
        for available in (False, None, 0, 1, "true", [], {}):
            with self.subTest(available_type=type(available).__name__):
                self.onboarding.binding = {
                    "write_context_available": available,
                    "reason_code": "brain_open_required",
                }
                self.assert_binding_rejection(self.remember(), "brain_open_required")

    def test_all_four_mutators_use_the_same_rejection_gate(self) -> None:
        self.onboarding.binding = {
            "write_context_available": False,
            "reason_code": "direct_scope_not_authorized",
        }
        for name in ("remember", "revise", "record_event", "review"):
            with self.subTest(mutator=name):
                result = getattr(self.service, name)(
                    write_context_ref="synthetic-open-ref", expected_planning_version=7
                )
                self.assert_binding_rejection(result, "direct_scope_not_authorized")

    def test_success_has_no_diagnostic_or_binding_payload(self) -> None:
        result = self.remember("  synthetic-open-ref  ")
        self.assertEqual("candidate_pending", result["decision"])
        self.assertNotIn("binding_reason_code", result)
        self.assertNotIn("synthetic-open-ref", json.dumps(result))
        self.assertNotIn("synthetic-wake", json.dumps(result))
        self.assertEqual(1, len(self.store.writes))
        self.assertEqual(
            {
                "owner_id": "synthetic-owner", "model_id": "synthetic-model",
                "wake_id": "synthetic-wake", "wake_seq": 3,
                "expected_row_version": 7, "content": {"synthetic": True},
            },
            self.store.writes[0],
        )

    def test_cas_and_governance_errors_keep_original_codes_without_binding_detail(self) -> None:
        for reason in ("planning_row_version_conflict", "later_real_wake_required"):
            with self.subTest(reason=reason):
                self.store.error = reason
                result = self.remember()
                self.assertEqual("reject", result["decision"])
                self.assertEqual([reason], result["reason_codes"])
                self.assertIs(False, result["state_changed"])
                self.assertNotIn("binding_reason_code", result)

    def test_protected_value_check_is_still_before_write_callback(self) -> None:
        self.onboarding.protected = True
        result = self.remember()
        self.assertEqual(["credential_or_secret_detected"], result["reason_codes"])
        self.assertNotIn("binding_reason_code", result)
        self.assertEqual([], self.store.writes)

    def test_manual_rejection_contract_is_unchanged(self) -> None:
        self.onboarding.binding = {
            "write_context_available": False,
            "reason_code": "current_injected_wake_required",
        }
        result = self.service.manual(write_context_ref="synthetic-open-ref")
        self.assertEqual(["brain_open_required"], result["reason_codes"])
        self.assertNotIn("binding_reason_code", result)
        self.assertEqual([], result["pending_changes"])
        self.assertEqual([], self.store.writes)


class PlanningBindingSyntheticCASTests(OfflineTestCase):
    def test_actual_planning_cas_stays_distinct_and_rejected_write_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory(prefix="planning-binding-synthetic-") as temp:
            database = (Path(temp) / "synthetic-planning.sqlite3").resolve()
            real_connect = sqlite3.connect

            def synthetic_connect(path: object, *args: object, **kwargs: object) -> sqlite3.Connection:
                if Path(path).resolve() != database:  # type: ignore[arg-type]
                    raise AssertionError("only this temporary synthetic DB is permitted")
                return real_connect(path, *args, **kwargs)  # type: ignore[arg-type]

            with patch("sqlite3.connect", side_effect=synthetic_connect):
                store = PlanningMemoryStore(database)
                onboarding = FakeOnboarding()
                service = PlanningMemoryAccessService(
                    store,
                    onboarding=onboarding,  # type: ignore[arg-type]
                    owner_id="synthetic-owner",
                    model_id="synthetic-model",
                )
                before = service.status()
                fields = {
                    "write_context_ref": "synthetic-open-ref",
                    "expected_planning_version": before["row_version"],
                    "content": plan_content("合成诊断计划"),
                    "reason": "仅合成测试。",
                    "calm_check": calm(),
                    "ai_confirmation": True,
                    "idempotency_key": "synthetic-first",
                }
                accepted = service.remember(**fields)
                self.assertEqual("candidate_pending", accepted["decision"])
                self.assertIs(False, accepted["active_plan_changed"])
                after = service.status()
                self.assertEqual(before["row_version"] + 1, after["row_version"])
                self.assertEqual(1, after["counts"]["pending_changes"])
                fields["idempotency_key"] = "synthetic-stale-version"
                rejected = service.remember(**fields)
                self.assertEqual(["planning_row_version_conflict"], rejected["reason_codes"])
                self.assertIs(False, rejected["state_changed"])
                self.assertIs(False, rejected["active_plan_changed"])
                self.assertNotIn("binding_reason_code", rejected)
                self.assertEqual(after, service.status())


if __name__ == "__main__":
    unittest.main()
