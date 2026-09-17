"""Local FastMCP conversions for ordinary module flows, not HTTP/delivery proof.

Each scenario runs in a fresh subprocess with three explicitly synthetic databases.
Sockets are forbidden throughout business-tool calls. No external tool is executed,
no migration is attempted, and only fixed booleans/counts/response sizes are printed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCENARIOS = ("emotional", "learning", "tool_guidance", "governance_controls", "vault_optional")


def require(condition: object, code: str) -> None:
    if not condition:
        raise AssertionError(code)


async def synthetic_probe(scenario: str) -> dict:
    from mcp_server import server
    from mcp_server.tests.test_compact_open import CompactOpenStateTests
    from mcp_server.tests.test_hallucination_service import WARNING, SUFFIX
    from runtime import AUTHORING_SCHEMA_VERSIONS
    from tests.test_tool_guidance import action_card

    require(scenario in SCENARIOS, "unknown_synthetic_scenario")
    fixture = CompactOpenStateTests()
    fixture.setUp()  # Installs socket blockers before any business-tool call.
    measurements: dict[str, dict[str, int]] = {}
    called: set[str] = set()
    try:
        fixture.service = server.service
        fixture.onboarding = server.service.onboarding
        fixture.database = Path(os.environ["STBRAIN_DB_PATH"])
        fixture.bootstrap_live()
        native_tools = server.mcp._tool_manager.list_tools()
        names = {tool.name for tool in native_tools}
        require(len(names) == 46, "unexpected_registered_tool_count")
        require("workspace_shell" not in names, "sandbox_tool_present")
        require(not any("sandbox" in name or "shell" in name for name in names), "sandbox_tool_present")
        synthetic_external_tools = []
        if scenario == "tool_guidance":
            synthetic_mcp = server.FastMCP("SyntheticCatalogFixture")

            @synthetic_mcp.tool()
            async def synthetic_catalog_read_only() -> dict[str, bool]:
                """An isolated catalog fixture, never an executable test step."""
                raise AssertionError("synthetic_target_must_never_execute")

            synthetic_external_tools = synthetic_mcp._tool_manager.list_tools()

        async def call(name: str, arguments: dict) -> dict:
            require(name in names, "non_native_tool_requested")
            called.add(name)
            result = await server.mcp.call_tool(name, arguments)
            require(isinstance(result, tuple) and len(result) == 2, "native_result_shape")
            blocks, structured = result
            require(isinstance(structured, dict), "structured_result_missing")
            require(len(blocks) == 1 and blocks[0].type == "text", "text_result_missing")
            require(json.loads(blocks[0].text) == structured, "native_dual_result_mismatch")
            if name == "stbrain_open":
                key = "summary" if arguments.get("view", "summary") == "summary" else arguments["module"]
                size = {"utf8_bytes": len(blocks[0].text.encode("utf-8")), "characters": len(blocks[0].text)}
                previous = measurements.get(key)
                if previous is None or size["utf8_bytes"] > previous["utf8_bytes"]:
                    measurements[key] = size
            return structured

        def wake(event: str, *, advertised: bool = False) -> None:
            if not advertised:
                fixture.wake(event)
                return
            # Build a synthetic host snapshot from the actual local MCP schemas.
            # This is not an HTTP/gateway integration test or a capability grant.
            entries = []
            for tool in [*native_tools, *synthetic_external_tools]:
                schema = tool.parameters
                require(isinstance(schema, dict), "native_schema_missing")
                encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                entries.append({"canonical_name": tool.name, "schema_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest()})
            entries.sort(key=lambda item: item["canonical_name"])
            encoded_entries = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            catalog = {
                "contract": "advertised-tools/1", "catalog_complete": True,
                "catalog_hash": hashlib.sha256(encoded_entries.encode("utf-8")).hexdigest(),
                "entries": entries,
            }
            issued = fixture.onboarding.issue_wake(
                owner_id=server.OWNER_ID, model_id=server.MODEL_ID,
                host_id="host:synthetic-native", thread_id="thread:synthetic-native",
                source_kind="human_message", source_event_id=event,
            )
            prepared = fixture.onboarding.build_pre_generation_context(
                owner_id=server.OWNER_ID, model_id=server.MODEL_ID,
                wake_id=issued["wake_id"], wake_capability=issued["wake_capability"],
                source_digest=f"synthetic:{event}", host_contract_digest="native-fixture/1",
                advertised_tools=catalog,
            )
            confirmed = fixture.onboarding.confirm_context_injected(
                owner_id=server.OWNER_ID, model_id=server.MODEL_ID,
                wake_id=issued["wake_id"], wake_capability=issued["wake_capability"],
                context_hash=prepared["context_hash"],
            )
            require(confirmed["decision"] == "injected", "synthetic_injection_failed")

        wake(f"native-{scenario}", advertised=scenario == "tool_guidance")
        opened = await call("stbrain_open", {})
        require(opened["view"] == "summary" and opened["write_context_available"] is True, "summary_unavailable")
        ref = opened["write_context_ref"]
        require(isinstance(ref, str) and ref and ref != "$.write_context_ref", "invalid_root_ref")
        require(opened["review_material_presented"] is False, "summary_claimed_review")
        covered: dict[str, bool] = {}

        if scenario == "emotional":
            initial_version = opened["emotional_memory"]["row_version"]
            original = "这是一条完全合成的情感经历，用于离线接口验收。"
            first = await call("remember_emotional_memory", {
                "write_context_ref": ref, "expected_emotion_version": initial_version,
                "memory_type": "shared_event", "original_text": original,
                "summary": "合成经历初版摘要。", "primary_emotion": "calm",
                "origin": "reported", "confidence": 50,
                "reason": "我选择保存这条合成情感验例。",
            })
            require(first["decision"] == "stored", "emotional_create_failed")
            memory_id = first["memory"]["memory_id"]
            revision_arguments = {
                "write_context_ref": ref, "expected_emotion_version": first["emotion_row_version"],
                "memory_id": memory_id, "expected_memory_version": 1,
                "summary": "合成经历修订摘要。", "reason": "我只修订合成解释，不覆盖原文。",
            }
            changed = await call("revise_emotional_memory", revision_arguments)
            require(changed["decision"] == "version_appended", "emotional_revision_failed")
            require(changed["memory"]["current_version"] == 2, "emotional_revision_version")
            require(changed["emotion_row_version"] == first["emotion_row_version"] + 1, "emotional_cas_chain")
            conflict = await call("revise_emotional_memory", {
                **revision_arguments, "expected_memory_version": 2,
                "summary": "不得写入的旧版本摘要。",
            })
            require(conflict["decision"] == "reject" and "emotion_row_version_conflict" in conflict["reason_codes"], "emotional_stale_cas_not_rejected")
            history = await call("recall_emotional_memory", {"memory_id": memory_id})
            require(history["decision"] == "history_returned", "emotional_readback_failed")
            require(len(history["result"]["versions"]) == 2, "emotional_history_changed")
            require(original in json.dumps(history, ensure_ascii=False), "emotional_original_not_preserved")
            again = await call("stbrain_open", {})
            require(again["write_context_ref"] == ref, "same_wake_ref_changed")
            require(again["emotional_memory"]["row_version"] == changed["emotion_row_version"], "emotional_summary_version_stale")
            await call("stbrain_open", {"view": "manual", "module": "emotional_memory"})
            covered = {"create_read_revision": True, "same_wake_cas": True, "original_immutable": True, "no_rewrite_receipt_needed": True}

        elif scenario == "learning":
            initial_version = opened["learning_memory"]["learning_row_version"]
            fields = {
                "write_context_ref": ref, "expected_learning_version": initial_version,
                "kind": "lesson", "title": "合成学习卡甲", "summary": "合成接口验例的概要。",
                "current_understanding": "我把本条作为合成、可复核的说明，不声称真实发生。",
                "source_basis": "reported", "confidence": 50,
                "correctness_assessment": "我确认这里只验证离线接口的结构。",
                "reason": "我选择保存这条合成学习卡。",
            }
            first = await call("remember_learning_memory", fields)
            require(first["decision"] == "stored", "learning_create_failed")
            second = await call("remember_learning_memory", {
                **fields, "expected_learning_version": first["learning_row_version"], "title": "合成学习卡乙",
            })
            require(second["decision"] == "stored", "learning_second_write_failed")
            require(second["learning_row_version"] == first["learning_row_version"] + 1, "learning_cas_chain")
            conflict = await call("remember_learning_memory", {**fields, "title": "不得保存的旧版本卡"})
            require(conflict["decision"] == "reject" and conflict["reason_codes"] == ["learning_row_version_conflict"], "learning_stale_cas_not_rejected")
            readback = await call("recall_learning_memory", {"target_ref": first["item_ref"], "include_versions": True})
            require(readback["decision"] == "recalled", "learning_readback_failed")
            require(readback["results"][0]["content"]["title"] == fields["title"], "learning_readback_not_exact")
            inventory = await call("recall_learning_memory", {"view": "inventory", "limit": 20})
            require(inventory["decision"] == "inventory_listed" and inventory["returned_count"] == 2, "learning_inventory_failed")
            again = await call("stbrain_open", {})
            require(again["write_context_ref"] == ref, "same_wake_ref_changed")
            require(again["learning_memory"]["learning_row_version"] == second["learning_row_version"], "learning_summary_version_stale")
            await call("stbrain_open", {"view": "manual", "module": "learning_memory"})
            covered = {"create_read_inventory": True, "same_wake_cas": True, "no_rewrite_receipt_needed": True}

        elif scenario == "tool_guidance":
            fields = action_card(
                tool_name="synthetic_catalog_read_only", operation_key="read_fixture_state",
                display_label="Synthetic read-only lookup advice", capability_class="information_query",
                risk_level="low", confirmation_policy="none",
                completion_rule="Only a current native result can establish the returned fixture state.",
                critical_preconditions=["Check that the native tool remains advertised."],
                purpose="Read a synthetic fixture value without changing anything.",
                use_when=["A fixture-state query is explicitly requested."],
                avoid_when=["Do not infer any real service state from a synthetic fixture."],
                scenario_tags=["synthetic.lookup"], scenario_examples=["A synthetic read-only fixture query."],
                keywords=["synthetic lookup"], aliases=["fixture state lookup"],
                call_notes="Read the current native schema; this synthetic card authorizes nothing.",
                source_type="ai_inferred", confidence=50,
            )
            excluded = await call("remember_tool_guidance", {
                **fields, "tool_name": "stbrain_health", "write_context_ref": ref,
                "expected_tool_row_version": opened["tool_guidance"]["tool_row_version"],
            })
            require(excluded["reason_codes"] == ["self_tool_excluded"], "self_tool_exclusion_changed")
            stored = await call("remember_tool_guidance", {
                **fields, "write_context_ref": ref,
                "expected_tool_row_version": opened["tool_guidance"]["tool_row_version"],
            })
            require(stored["decision"] == "stored", "tool_guidance_create_failed")
            require(stored["card"]["schema_status"] == "matched", "tool_catalog_not_bound")
            require(stored["execution_performed"] is False, "tool_guidance_executed_target")
            card_id = stored["card"]["card_id"]
            revision_arguments = {
                "write_context_ref": ref, "expected_tool_row_version": stored["tool_row_version"],
                "card_id": card_id, "expected_card_version": 1,
                "intent": "revise", "edit_class": "metadata",
                "display_label": "Synthetic read-only lookup advice, revised label",
                "reason": "I only clarify a synthetic display label without changing capability.",
            }
            changed = await call("revise_tool_guidance", revision_arguments)
            require(changed["decision"] == "version_appended", "tool_guidance_revision_failed")
            require(changed["card"]["version"] == 2, "tool_guidance_revision_version")
            require(changed["tool_row_version"] == stored["tool_row_version"] + 1, "tool_guidance_cas_chain")
            conflict = await call("revise_tool_guidance", revision_arguments)
            require(conflict["decision"] == "reject" and "tool_row_version_conflict" in conflict["reason_codes"], "tool_stale_cas_not_rejected")
            exact = await call("recall_tool_guidance", {"card_id": card_id, "view": "card"})
            require(exact["decision"] == "precise_result", "tool_guidance_readback_failed")
            require(exact["results"][0]["version"] == 2, "tool_guidance_readback_version")
            require(exact["execution_performed"] is False, "tool_guidance_read_executed_target")
            again = await call("stbrain_open", {})
            require(again["write_context_ref"] == ref, "same_wake_ref_changed")
            require(again["tool_guidance"]["tool_row_version"] == changed["tool_row_version"], "tool_summary_version_stale")
            await call("stbrain_open", {"view": "manual", "module": "tool_guidance"})
            require("stbrain_health" not in called, "target_health_tool_was_executed")
            require("synthetic_catalog_read_only" not in called, "synthetic_target_was_executed")
            covered = {"create_read_revision": True, "same_wake_cas": True, "synthetic_native_catalog_bound": True, "self_tool_exclusion_preserved": True, "target_execution_performed": False}

        elif scenario == "governance_controls":
            # Empty-module manual measurements are deliberately not large-candidate guarantees.
            for module in (
                "self_revision", "emotional_memory", "learning_memory", "tool_guidance", "planning_memory",
                "self_governance_profile", "injection_control", "hallucination_vault", "shared_person_authoring",
            ):
                manual = await call("stbrain_open", {"view": "manual", "module": module})
                require(manual["write_context_ref"] == ref, "manual_changed_same_wake_ref")
            governance = await call("manage_self_governance_profile", {
                "action": "propose_set", "scope": "tool_use", "write_context_ref": ref,
                "expected_profile_version": opened["self_governance_profile"]["scope_versions"]["tool_use"],
                "text": "我会把纯合成工具说明作为参考，不把它当成外部行动授权。",
                "trigger_mode": "manual_only", "scene_tags": [],
                "reason": "我选择在合成库中建立这段治理候选。",
            })
            require(governance["decision"] == "pending", "governance_candidate_failed")
            governance_manual = await call("stbrain_open", {"view": "manual", "module": "self_governance_profile"})
            require(governance_manual["self_governance_profile"]["current_action_contract"]["pending_activation_count"] == 0, "governance_same_wake_contract_unlocked")
            blocked = await call("manage_self_governance_profile", {
                "action": "activate", "scope": "tool_use", "write_context_ref": ref,
                "expected_profile_version": governance_manual["self_governance_profile"]["status"]["scopes"]["tool_use"]["row_version"],
                "candidate_id": governance["candidate_id"], "expected_candidate_hash": governance["candidate_hash"],
                "ai_confirmation": True,
            })
            require("later_real_wake_required" in blocked["reason_codes"], "governance_same_wake_not_rejected")
            control = await call("manage_injection_control", {
                "action": "propose_mode", "scope": "hallucination_vault", "write_context_ref": ref,
                "expected_control_version": opened["injection_control"]["scope_versions"]["hallucination_vault"],
                "target_mode": "status_only", "reason": "我只在纯合成库中申请中性状态显示。", "ai_confirmation": True,
            })
            require(control["decision"] == "candidate_pending", "injection_candidate_failed")
            control_manual = await call("stbrain_open", {"view": "manual", "module": "injection_control"})
            require(not control_manual["injection_control"]["current_action_contract"]["pending_activations"], "injection_same_wake_contract_unlocked")
            blocked = await call("manage_injection_control", {
                "action": "activate", "scope": "hallucination_vault", "write_context_ref": ref,
                "expected_control_version": control["row_version"],
                "candidate_id": control["candidate_id"], "expected_candidate_hash": control["candidate_hash"],
                "ai_confirmation": True,
            })
            require("later_real_wake_required" in blocked["reason_codes"], "injection_same_wake_not_rejected")
            wake("native-governance-controls-later")
            later = await call("stbrain_open", {})
            require(later["write_context_ref"] != ref, "later_wake_ref_not_changed")
            governance_manual = await call("stbrain_open", {"view": "manual", "module": "self_governance_profile"})
            activation = governance_manual["self_governance_profile"]["current_action_contract"]["pending_activations"][0]
            accepted = await call(activation["tool"], activation["arguments"])
            require(accepted["decision"] == "activated", "governance_later_activation_failed")
            control_manual = await call("stbrain_open", {"view": "manual", "module": "injection_control"})
            activation = control_manual["injection_control"]["current_action_contract"]["pending_activations"][0]
            accepted = await call(activation["tool"], activation["arguments"])
            require(accepted["decision"] == "activated", "injection_later_activation_failed")
            readback = await call("query_injection_control", {"view": "status"})
            require(readback["result"]["scopes"]["hallucination_vault"]["mode"] == "status_only", "injection_readback_failed")
            await call("query_self_governance_profile", {"view": "status", "include_content": False})
            covered = {"all_module_manual_sizes": True, "governance_candidate_and_later_activation": True, "injection_candidate_and_later_activation": True, "same_wake_activation_rejected": True}

        elif scenario == "vault_optional":
            fields = {
                "write_context_ref": ref, "expected_vault_version": opened["hallucination_vault"]["vault_row_version"],
                "intent": "record", "neutral_title": "纯合成隔离断言",
                "isolated_content": "这是一条主动隔离的合成正文。", "current_account": "我只确认这是一条离线验例。",
                "basis": "没有把合成内容当真实经历。", "reflection": "我保持隔离和普通记忆的边界。",
                "uncertainty_status": "still_uncertain", "reason": "我选择在合成库中隔离这条内容。", "ai_confirmation": True,
            }
            setup = await call("hold_hallucination_record", fields)
            require(setup["decision"] == "setup_required", "vault_missing_warning_not_rejected")
            stored = await call("hold_hallucination_record", {**fields, "warning_text": WARNING, "warning_suffix": SUFFIX})
            require(stored["decision"] == "stored_quarantined", "vault_create_failed")
            directory = await call("open_hallucination_vault", {})
            require(directory["decision"] == "directory" and directory["total"] == 1, "vault_directory_failed")
            require("isolated_content" not in json.dumps(directory), "vault_directory_exposed_body")
            warning = await call("open_hallucination_vault", {"record_id": stored["record_id"]})
            require(warning["decision"] == "warning_confirmation_required", "vault_warning_gate_missing")
            body = await call("open_hallucination_vault", {
                "record_id": stored["record_id"], "warning_confirmation": WARNING,
                "expected_warning_version": 1, "write_context_ref": ref,
            })
            require(body["decision"] == "record_opened" and body["content_exposed"] is True, "vault_exact_warning_open_failed")
            require(fields["isolated_content"] in json.dumps(body, ensure_ascii=False), "vault_body_not_exact")
            await call("stbrain_open", {"view": "manual", "module": "hallucination_vault"})
            preview = await call("preview_person_reference_rewrite", {
                "write_context_ref": ref,
                "expected_authoring_version": opened["shared_person_authoring"]["row_version"],
                "module": "emotional_memory_module_two", "draft_version": 0,
                "draft_fields": {"/original_text": "我保留本条合成原文。", "/summary": "合成原文。"},
                "referent_bindings": [], "rewrite_targets": [], "conversation_mode": "unknown",
                "authenticated_participant_entity_ids": [], "alias_collision_scope": "synthetic:optional",
                "alias_collision_scope_version": 1, "protected_spans": [],
                "module_schema_version": AUTHORING_SCHEMA_VERSIONS["emotional_memory_module_two"],
            })
            require(preview["decision"] == "continue_original_path", "optional_authoring_blocked_plain_write")
            begun = await call("submit_self_model_candidate", {
                "intent": "begin_edit", "write_context_ref": ref, "expected_row_version": opened["row_version"],
            })
            require(begun["decision"] == "challenge_issued", "self_revision_begin_failed")
            require("challenge_response" not in begun, "self_revision_private_challenge_exposed")
            await call("stbrain_open", {"view": "manual", "module": "self_revision"})
            cancelled = await call("submit_self_model_candidate", {
                "intent": "cancel_edit", "write_context_ref": ref, "expected_row_version": begun["state"]["row_version"],
            })
            require(cancelled["state"]["stage"] == "live", "self_revision_cancel_failed")
            covered = {"vault_create_warning_read": True, "vault_migration_performed": False, "authoring_optional_noop": True, "self_revision_begin_cancel": True}

        return {
            "decision": "PASS", "scenario": scenario,
            "native_tool_count": len(names), "called_tool_count": len(called),
            "sandbox_tools": 0, "network_calls": 0, "http_delivery_tested": False,
            "native_dual_result_equal": True, "synthetic_only": True,
            "covered": covered, "response_sizes": measurements,
        }
    finally:
        fixture.doCleanups()


class NativeModulesNoSandboxTests(unittest.TestCase):
    def run_scenario(self, scenario: str) -> None:
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("FastMCP dependency not installed in this interpreter")
        environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith(("STBRAIN_", "OMBRE_", "OPENAI_", "DEEPSEEK_", "E32_"))
            and key.upper() not in {"PYTHONPATH", "PYTHONSTARTUP", "PYTHONOPTIMIZE"}
        }
        with tempfile.TemporaryDirectory(prefix=f"native-{scenario}-synthetic-") as scratch:
            environment.update({
                "STBRAIN_MCP_TOKEN": "synthetic-native-modules-token-at-least-thirty-two-characters",
                "STBRAIN_REQUIRE_EXECUTION_BINDING": "0",
                "STBRAIN_WAKE_SECRET": "synthetic-native-modules-secret-at-least-thirty-two-characters",
                "STBRAIN_OWNER_ID": "owner:synthetic-native-modules", "STBRAIN_MODEL_ID": "model:synthetic-native-modules",
                "STBRAIN_DB_PATH": str(Path(scratch) / "brain.db"),
                "STBRAIN_LEARNING_IDEA_DB_PATH": str(Path(scratch) / "ideas.db"),
                "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(Path(scratch) / "vault.db"),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
            })
            completed = subprocess.run(
                [sys.executable, "-B", "-m", "mcp_server.tests.test_native_modules_no_sandbox", "--probe", scenario],
                cwd=Path(__file__).resolve().parents[2], env=environment,
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"])
        self.assertEqual(scenario, proof["scenario"])
        self.assertEqual(0, proof["sandbox_tools"])
        self.assertIs(False, proof["http_delivery_tested"])
        self.assertIs(True, proof["native_dual_result_equal"])
        # Fixed outcome labels and numeric sizes only; no synthetic body/ref values.
        print(json.dumps(proof, ensure_ascii=False, sort_keys=True))

    def test_emotional_create_revise_readback_same_wake(self) -> None:
        self.run_scenario("emotional")

    def test_learning_create_readback_inventory_same_wake(self) -> None:
        self.run_scenario("learning")

    def test_tool_guidance_create_revise_readback_without_execution(self) -> None:
        self.run_scenario("tool_guidance")

    def test_governance_injection_native_later_wake_contracts_and_sizes(self) -> None:
        self.run_scenario("governance_controls")

    def test_vault_warning_read_and_optional_entrypoints(self) -> None:
        self.run_scenario("vault_optional")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        print(json.dumps(asyncio.run(synthetic_probe(sys.argv[2])), ensure_ascii=False))
    else:
        unittest.main()
