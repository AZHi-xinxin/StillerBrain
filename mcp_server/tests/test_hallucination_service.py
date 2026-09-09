from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from mcp_server.hallucination_service import HallucinationVaultAccessService
from runtime.hallucination_vault import HallucinationVaultStore


OWNER = "owner-service"
MODEL = "model-service"
WARNING = "我将看到自己主动隔离的旧内容；我不会自动把它当成当前事实。"
SUFFIX = "我已经读完这一条，并会继续区分旧判断、当前依据和不确定之处。"


class FakeOnboarding:
    def __init__(self) -> None:
        self.allowed = True
        self.contexts = {
            "artifact-wake-1": {
                "write_context_available": True,
                "wake_id": "wake-1",
                "wake_seq": 1,
            }
        }

    def authorize_other_module_write(self, **_fields):
        return {"decision": "allowed" if self.allowed else "blocked"}

    def current_open_write_context(self, *, write_context_ref: str, **_fields):
        return self.contexts.get(
            write_context_ref,
            {"write_context_available": False},
        )

    def contains_protected_persistence_value(self, **_fields):
        return False


class HallucinationVaultAccessServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = HallucinationVaultStore(Path(self.temp.name) / "vault.db")
        self.onboarding = FakeOnboarding()
        self.service = HallucinationVaultAccessService(
            self.store,
            onboarding=self.onboarding,
            owner_id=OWNER,
            model_id=MODEL,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _record_fields() -> dict:
        return {
            "neutral_title": "一个旧断言",
            "isolated_content": "我曾把一个未经核验的说法当成事实。",
            "current_account": "我现在只确认它仍需核验。",
            "basis": "我没有足够证据支持原来的确定语气。",
            "reflection": "我选择把内容与当前判断分开。",
            "uncertainty_status": "still_uncertain",
            "reason": "我自主决定隔离这条内容。",
            "ai_confirmation": True,
        }

    def test_manual_and_status_expose_hard_off_without_injection(self) -> None:
        status = self.service.status()
        manual = self.service.manual()
        self.assertEqual(status["automatic_exposure"], "hard_off")
        self.assertFalse(status["automatic_injection"])
        self.assertFalse(manual["automatic_injection"])
        self.assertIn("零自动注入", manual["principles"][0])
        for method in ("manual", "status", "hold", "open", "transfer", "review_restore"):
            self.assertTrue(callable(getattr(self.service, method)))

    def test_write_requires_current_open_context_and_module_one(self) -> None:
        rejected = self.service.hold(
            write_context_ref="stale-ref",
            expected_vault_version=0,
            **self._record_fields(),
        )
        self.assertEqual(rejected["decision"], "reject")
        self.assertEqual(rejected["reason_codes"], ["brain_open_required"])
        self.assertEqual(self.service.open()["total"], 0)
        self.onboarding.allowed = False
        blocked = self.service.hold(
            write_context_ref="artifact-wake-1",
            expected_vault_version=0,
            **self._record_fields(),
        )
        self.assertEqual(blocked["reason_codes"], ["module_one_required"])

    def test_first_hold_setup_and_open_directory_body_separation(self) -> None:
        setup = self.service.hold(
            write_context_ref="artifact-wake-1",
            expected_vault_version=0,
            **self._record_fields(),
        )
        self.assertEqual(setup["decision"], "setup_required")
        self.assertEqual(self.service.open()["total"], 0)
        stored = self.service.hold(
            write_context_ref="artifact-wake-1",
            expected_vault_version=0,
            warning_text=WARNING,
            warning_suffix=SUFFIX,
            **self._record_fields(),
        )
        self.assertEqual(stored["decision"], "stored_quarantined")
        directory = self.service.open()
        self.assertEqual(directory["decision"], "directory")
        self.assertNotIn("content", directory["entries"][0])
        warning = self.service.open(record_id=stored["record_id"])
        self.assertEqual(warning["decision"], "warning_confirmation_required")
        opened = self.service.open(
            write_context_ref="artifact-wake-1",
            record_id=stored["record_id"],
            warning_confirmation=WARNING,
            expected_warning_version=1,
        )
        self.assertEqual(opened["decision"], "record_opened")
        self.assertTrue(opened["content_exposed"])

    def test_unknown_transfer_source_and_missing_restore_fail_closed(self) -> None:
        unsupported = self.service.transfer(
            write_context_ref="artifact-wake-1",
            intent="preview",
            source_ref="emotional://not-supported@1",
            expected_source_row_version=0,
        )
        self.assertEqual(unsupported["decision"], "reject")
        self.assertEqual(unsupported["reason_codes"], ["unsupported_source_module"])
        missing = self.service.review_restore(
            write_context_ref="artifact-wake-1",
            expected_vault_version=0,
            candidate_id="missing",
            expected_candidate_version=1,
            expected_candidate_hash="0" * 64,
            expected_base_record_version=1,
            action="reject",
            reason="我检查后拒绝。",
            ai_confirmation=True,
        )
        self.assertEqual(missing["decision"], "reject")
        self.assertEqual(missing["reason_codes"], ["restore_candidate_not_pending"])


if __name__ == "__main__":
    unittest.main()
