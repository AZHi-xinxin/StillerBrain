from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import MagicMock

from mcp_server.authoring_service import AuthoringRewriteAccessService
from mcp_server.emotional_service import EmotionalMemoryAccessService
from mcp_server.governance_service import SelfGovernanceAccessService
from mcp_server.hallucination_service import HallucinationVaultAccessService
from mcp_server.injection_control_service import InjectionControlAccessService
from mcp_server.learning_service import LearningMemoryAccessService
from mcp_server.planning_service import PlanningMemoryAccessService
from mcp_server.tool_guidance_service import ToolGuidanceAccessService
from runtime import AUTHORING_REWRITE_MODULES


class GuardOnboarding:
    def __init__(self) -> None:
        self._protected = "stgrant_" + ("A" * 43)
        self.binding_calls: list[dict[str, Any]] = []
        self.scanned_values: list[Any] = []

    def authorize_other_module_write(self, **_fields: Any) -> dict[str, Any]:
        return {"decision": "allowed"}

    def current_open_write_context(self, **fields: Any) -> dict[str, Any]:
        self.binding_calls.append(fields)
        return {
            "write_context_available": True,
            "write_context_ref": fields["write_context_ref"],
            "wake_id": "wake:test",
            "wake_seq": 7,
        }

    def contains_protected_persistence_value(self, **fields: Any) -> bool:
        value = fields["value"]
        self.scanned_values.append(value)

        def contains(item: Any) -> bool:
            if isinstance(item, str):
                return self._protected in item
            if isinstance(item, dict):
                return any(contains(key) or contains(entry) for key, entry in item.items())
            if isinstance(item, (list, tuple, set)):
                return any(contains(entry) for entry in item)
            return False

        return contains(value)

    def nested_value(self) -> dict[str, Any]:
        return {"outer": [{"inner": f"prefix {self._protected} suffix"}]}


class ProtectedPersistenceGuardTests(unittest.TestCase):
    def make_dependencies(self) -> tuple[MagicMock, GuardOnboarding]:
        store = MagicMock()
        store.status.return_value = {}
        return store, GuardOnboarding()

    def assert_blocked(
        self,
        result: dict[str, Any],
        onboarding: GuardOnboarding,
        *,
        scope: str,
    ) -> None:
        self.assertEqual(["credential_or_secret_detected"], result["reason_codes"])
        self.assertFalse(result["state_changed"])
        self.assertFalse(
            onboarding._protected in json.dumps(result, ensure_ascii=False)
        )
        self.assertEqual(scope, onboarding.binding_calls[-1]["required_scope"])
        self.assertEqual(1, len(onboarding.scanned_values))

    def test_emotional_nested_value_is_rejected_before_store(self) -> None:
        store, onboarding = self.make_dependencies()
        service = EmotionalMemoryAccessService(
            store, onboarding=onboarding, owner_id="owner", model_id="model"
        )
        result = service.remember(
            write_context_ref="context",
            expected_emotion_version=0,
            content=onboarding.nested_value(),
        )
        self.assert_blocked(result, onboarding, scope="emotional_memory")
        store.remember.assert_not_called()

    def test_learning_nested_value_is_rejected_before_store(self) -> None:
        store, onboarding = self.make_dependencies()
        service = LearningMemoryAccessService(
            store, onboarding=onboarding, owner_id="owner", model_id="model"
        )
        result = service.remember(
            write_context_ref="context",
            expected_learning_version=0,
            content=onboarding.nested_value(),
        )
        self.assert_blocked(result, onboarding, scope="learning_memory")
        store.remember.assert_not_called()

    def test_tool_nested_value_is_rejected_before_store(self) -> None:
        store, onboarding = self.make_dependencies()
        service = ToolGuidanceAccessService(
            store, onboarding=onboarding, owner_id="owner", model_id="model"
        )
        result = service.remember(
            write_context_ref="context",
            expected_tool_row_version=0,
            content=onboarding.nested_value(),
        )
        self.assert_blocked(result, onboarding, scope="tool_guidance")
        store.remember.assert_not_called()

    def test_planning_nested_value_is_rejected_before_store(self) -> None:
        store, onboarding = self.make_dependencies()
        service = PlanningMemoryAccessService(
            store, onboarding=onboarding, owner_id="owner", model_id="model"
        )
        result = service.remember(
            write_context_ref="context",
            expected_planning_version=0,
            content=onboarding.nested_value(),
        )
        self.assert_blocked(result, onboarding, scope="planning_memory")
        store.propose_create.assert_not_called()

    def test_hallucination_nested_value_is_rejected_before_store(self) -> None:
        store, onboarding = self.make_dependencies()
        service = HallucinationVaultAccessService(
            store, onboarding=onboarding, owner_id="owner", model_id="model"
        )
        result = service.hold(
            write_context_ref="context",
            expected_vault_version=0,
            intent="record",
            content=onboarding.nested_value(),
        )
        self.assert_blocked(result, onboarding, scope="hallucination_vault")
        store.hold.assert_not_called()

    def test_governance_nested_value_is_rejected_before_store(self) -> None:
        store, onboarding = self.make_dependencies()
        service = SelfGovernanceAccessService(
            store, onboarding=onboarding, owner_id="owner", model_id="model"
        )
        result = service.manage(
            action="propose_set",
            scope="tool_use",
            write_context_ref="context",
            expected_profile_version=0,
            text="ordinary",
            trigger_mode="scene",
            scene_tags=["ordinary", onboarding._protected],
            reason="ordinary",
        )
        self.assert_blocked(result, onboarding, scope="self_governance")
        store.propose_candidate.assert_not_called()

    def test_injection_value_is_rejected_before_store(self) -> None:
        store, onboarding = self.make_dependencies()
        service = InjectionControlAccessService(
            store, onboarding=onboarding, owner_id="owner", model_id="model"
        )
        result = service.manage(
            action="emergency_off",
            scope="global",
            write_context_ref="context",
            expected_control_version=0,
            reason=f"prefix {onboarding._protected} suffix",
            ai_confirmation=True,
        )
        self.assert_blocked(result, onboarding, scope="injection_control")
        store.emergency_off.assert_not_called()

    def test_authoring_nested_value_is_rejected_before_store(self) -> None:
        store, onboarding = self.make_dependencies()
        service = AuthoringRewriteAccessService(
            store, onboarding=onboarding, owner_id="owner", model_id="model"
        )
        module = sorted(AUTHORING_REWRITE_MODULES)[0]
        result = service.preview(
            write_context_ref="context",
            expected_authoring_version=0,
            module=module,
            source=onboarding.nested_value(),
        )
        self.assert_blocked(result, onboarding, scope="shared_person_authoring")
        store.preview.assert_not_called()


if __name__ == "__main__":
    unittest.main()
