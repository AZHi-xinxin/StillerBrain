"""AI-directed authoring on isolated temporary state, including real MCP dispatch."""
from __future__ import annotations

import asyncio
from contextlib import closing, ExitStack
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from pydantic import ValidationError
from mcp_server.authoring_service import AuthoringRewriteAccessService
from mcp_server.public_contract import PUBLIC_REWRITE_PREVIEW_INPUT_SCHEMA, PUBLIC_REWRITE_CONFIRM_INPUT_SCHEMA
from mcp_server.tests import test_person_rewrite_simple_entry as fixtures
from runtime.authoring import AUTHOR_DECLARED_REWRITE_MODE, AuthoringError


def minimal(module="emotional_memory"):
    old = fixtures._preview_fields(module)
    return {"module": old["module"], "draft_fields": old["draft_fields"],
            "rewrite_targets": [{key: value for key, value in target.items()
                if key in {"field_path", "surface_form", "entity_ref", "target_surface_form"}}
                for target in old["rewrite_targets"]]}


class AIDirectedAuthoringTests(unittest.TestCase):
    setUp = fixtures.PersonRewriteSimpleEntryTests.setUp
    operation = fixtures.PersonRewriteSimpleEntryTests.operation
    rows = fixtures.PersonRewriteSimpleEntryTests.rows

    def snapshot(self):
        return {"status": self.authoring.status(), "previews": self.rows("authoring_rewrite_previews"),
                "receipts": self.rows("authoring_rewrite_receipts"), "memories": self.rows("emotion_memories")}

    def preview(self, fields=None):
        with self.operation("shared_person_authoring") as access:
            return self.authoring.preview(write_context_ref=access["write_context_ref"], **(fields or minimal()))

    def confirm(self, preview, **changes):
        with self.operation("shared_person_authoring") as access:
            return self.authoring.confirm(write_context_ref=access["write_context_ref"],
                **{"preview_id": preview["preview_id"], "ai_confirmation": True, **changes})

    def test_minimal_preview_confirm_memory_receipt_and_replay(self):
        preview = self.preview()
        self.assertEqual("changes_available", preview["rewrite_preview_status"])
        self.assertEqual("author_declared_literal_preview", preview["source"])
        self.assertEqual(AUTHOR_DECLARED_REWRITE_MODE, preview["validation_mode"])
        self.assertNotIn("confidence", json.dumps(json.loads(self.rows("authoring_rewrite_previews")[0]["bindings_json"])))
        confirmed = self.confirm(preview)
        self.assertEqual("confirmed", confirmed["decision"])
        self.assertEqual(preview["suggested_fields"], confirmed["final_fields"])
        self.assertEqual("exact_saved_preview_only", confirmed["confirmation_scope"])
        self.assertEqual([], self.rows("emotion_memories"))
        arguments = fixtures._memory_fields("emotional_memory", preview, confirmed["rewrite_receipt"])
        with self.operation("emotional_memory"):
            stored = self.daily.remember(**arguments)
        with self.operation("emotional_memory"):
            replay = self.daily.remember(**arguments)
        self.assertTrue(stored["stored"])
        self.assertEqual(stored["ref"], replay["ref"])
        with self.operation("emotional_memory"):
            rejected = self.daily.remember(**{**arguments, "reason": "different request"})
        self.assertEqual(["rewrite_receipt_replay_conflict"], rejected["reason_codes"])
        self.assertEqual(1, len(self.rows("emotion_memories")))

    def test_empty_participants_group_unknown_and_historical_people_are_author_declared(self):
        for mode in ("one_to_one", "group", "unknown"):
            fields = minimal()
            fields.update(conversation_mode=mode, authenticated_participant_entity_ids=[])
            fields["rewrite_targets"][0]["entity_ref"] = "Historical synthetic person"
            result = self.preview(fields)
            self.assertEqual("changes_available", result["rewrite_preview_status"])
            self.assertEqual("ai_author_declaration_not_host_authentication", result["person_identity_source"])
            saved = json.loads(self.rows("authoring_rewrite_previews")[-1]["validation_context_json"])
            self.assertEqual(AUTHOR_DECLARED_REWRITE_MODE, saved["validation_mode"])
            self.assertNotIn("authenticated_participant_entity_ids", saved)
            self.assertEqual([], saved["caller_compatibility_context"]["authenticated_participant_entity_ids"])
            self.assertEqual("confirmed", self.confirm(result)["decision"])

    def test_multiple_occurrences_need_explicit_index_and_modify_only_chosen_one(self):
        fields = minimal()
        fields["draft_fields"]["/original_text"] = "她说她回来。"
        before = self.snapshot()
        result = self.preview(fields)
        self.assertEqual("continue_original_path", result["decision"])
        self.assertEqual("occurrence_index_required", result["skipped"][0]["reason_code"])
        self.assertEqual(before, self.snapshot())
        fields["rewrite_targets"][0]["occurrence_index"] = 1
        result = self.preview(fields)
        self.assertEqual("她说测试者回来。", result["suggested_fields"]["/original_text"])

    def test_explicit_null_bool_and_negative_occurrence_indices_reject_without_writing(self):
        for invalid in (None, False, -1):
            fields = minimal()
            fields["rewrite_targets"][0]["occurrence_index"] = invalid
            self.assertFalse(Draft202012Validator(PUBLIC_REWRITE_PREVIEW_INPUT_SCHEMA).is_valid(fields))
            before = self.snapshot()
            result = self.preview(fields)
            self.assertEqual(["occurrence_index_invalid"], result["reason_codes"])
            self.assertEqual(before, self.snapshot())

    def test_original_target_indices_survive_ambiguity_missing_and_protected(self):
        fields = minimal()
        fields["draft_fields"]["/original_text"] = "她她和他。"
        target = fields["rewrite_targets"][0]
        fields["rewrite_targets"] = [target, {**target, "surface_form": "不存在", "occurrence_index": 0},
                                     {**target, "surface_form": "他"}]
        start = len("她她和".encode("utf-8"))
        fields["protected_spans"] = [{"field_path": "/original_text", "byte_start": start, "byte_end": start + 3}]
        before = self.snapshot()
        result = self.preview(fields)
        self.assertEqual("continue_original_path", result["decision"])
        self.assertEqual({0: "occurrence_index_required", 1: "source_occurrence_missing", 2: "protected_span"},
                         {item["target_index"]: item["reason_code"] for item in result["skipped"]})
        self.assertEqual(before, self.snapshot())

    def test_duplicate_and_overlapping_targets_are_skipped(self):
        for targets in (
            [{"surface_form": "她"}, {"surface_form": "她"}],
            [{"surface_form": "她"}, {"surface_form": "和她"}],
        ):
            fields = minimal()
            fields["rewrite_targets"] = [{**fields["rewrite_targets"][0], **target} for target in targets]
            before = self.snapshot()
            result = self.preview(fields)
            self.assertEqual("continue_original_path", result["decision"])
            self.assertEqual(["overlapping_patch", "overlapping_patch"], [item["reason_code"] for item in result["skipped"]])
            self.assertEqual(before, self.snapshot())

    def test_explicit_conflicting_confirmation_inputs_are_rejected_without_changes(self):
        preview = self.preview()
        wrong_final = {**preview["suggested_fields"], "/summary": "Changed after preview"}
        cases = [
            {"expected_source_draft_hash": "0" * 64}, {"expected_suggestion_hash": "0" * 64},
            {"expected_validation_context_hash": "0" * 64},
            {"final_fields": wrong_final, "final_fields_hash": fixtures._hash(wrong_final)},
            {"final_fields_hash": "0" * 64}, {"module_schema_version": "wrong-version"},
            {"module": "learning_memory_module_three"}, {"conversation_mode": "group"},
            {"authenticated_participant_entity_ids": ["different person"]}, {"ai_confirmation": False},
        ]
        for changes in cases:
            before = self.snapshot()
            result = self.confirm(preview, **changes)
            self.assertEqual("reject", result["decision"], changes)
            self.assertFalse(result["state_changed"])
            self.assertEqual(before, self.snapshot())
        self.assertEqual("confirmed", self.confirm(preview)["decision"])

    def test_explicit_old_binding_conflict_does_not_override_author_target(self):
        fields = fixtures._preview_fields("emotional_memory")
        fields["referent_bindings"][0]["entity_ref"] = "Different synthetic person"
        before = self.snapshot()
        result = self.preview(fields)
        self.assertEqual(["rewrite_legacy_binding_conflict"], result["reason_codes"])
        self.assertEqual(before, self.snapshot())

    def test_snapshot_owner_model_isolation_and_original_service_gate(self):
        preview = self.preview()
        for owner, model in (("other-owner", self.model), (self.owner, "other-model")):
            with self.assertRaisesRegex(AuthoringError, "rewrite_preview_not_found"):
                self.rewrites.confirmation_inputs(owner_id=owner, model_id=model, preview_id=preview["preview_id"])
        before = self.snapshot()
        result = self.authoring.confirm(write_context_ref="", preview_id=preview["preview_id"], ai_confirmation=True)
        self.assertEqual(["brain_open_required"], result["reason_codes"])
        self.assertEqual(before, self.snapshot())

    def test_legacy_preview_keeps_saved_context_and_rejects_changed_old_fields(self):
        fields = fixtures._preview_fields("emotional_memory")
        with self.operation("shared_person_authoring") as access:
            binding = self.host.current_open_write_context(owner_id=self.owner, model_id=self.model,
                write_context_ref=access["write_context_ref"], required_scope="shared_person_authoring")
            preview = self.rewrites.preview(owner_id=self.owner, model_id=self.model, wake_id=binding["wake_id"],
                expected_row_version=self.authoring.status()["row_version"], **fields)
        saved = json.loads(self.rows("authoring_rewrite_previews")[-1]["validation_context_json"])
        self.assertNotIn("validation_mode", saved)
        before = self.snapshot()
        rejected = self.confirm(preview, authenticated_participant_entity_ids=[])
        self.assertEqual(["validation_context_stale"], rejected["reason_codes"])
        self.assertEqual(before, self.snapshot())
        self.assertEqual("confirmed", self.confirm(preview)["decision"])

    def test_secret_source_target_final_never_persist_and_unknown_fields_reject(self):
        for change in ("source", "target"):
            fields = minimal()
            if change == "source":
                fields["draft_fields"]["/original_text"] += " token: very-secret-value"
            else:
                fields["rewrite_targets"][0]["target_surface_form"] = "token:secret-value"
            before = self.snapshot()
            result = self.preview(fields)
            self.assertEqual("reject", result["decision"])
            self.assertEqual(before, self.snapshot())
        preview = self.preview()
        before = self.snapshot()
        final = {**preview["suggested_fields"], "/summary": "token: very-secret-value"}
        self.assertEqual("reject", self.confirm(preview, final_fields=final)["decision"])
        self.assertEqual(before, self.snapshot())
        fields = minimal()
        fields["draft_fields"]["/not_a_public_field"] = "synthetic"
        self.assertEqual("reject", self.preview(fields)["decision"])


async def native_probe():
    # First execute the existing full-parameter compatibility chain under its
    # guarded real MCP dispatcher; then the minimal chain on the same temp DB.
    await fixtures.native_probe()
    root = Path(os.environ["STBRAIN_DB_PATH"]).parent.resolve()
    real_connect = sqlite3.connect
    def connect(path, *args, **kwargs):
        assert Path(path).resolve() in {root / x for x in ("main.db", "ideas.db", "vault.db")}
        return real_connect(path, *args, **kwargs)
    with ExitStack() as stack:
        for target in ("socket.create_connection", "socket.socket.connect", "subprocess.Popen"):
            stack.enter_context(patch(target, side_effect=AssertionError("offline synthetic")))
        stack.enter_context(patch("sqlite3.connect", side_effect=connect))
        from mcp_server import server
        async def call(name, arguments):
            blocks, result = await server.mcp.call_tool(name, arguments)
            assert json.loads(blocks[0].text) == result
            return result
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        assert len(tools) == 44
        preview_schema = tools["preview_person_reference_rewrite"].inputSchema
        confirm_schema = tools["confirm_person_reference_rewrite"].inputSchema
        assert set(preview_schema["required"]) == {"module", "draft_fields", "rewrite_targets"}
        assert set(confirm_schema["required"]) == {"preview_id", "ai_confirmation"}
        required_target = preview_schema["properties"]["rewrite_targets"]["items"]["required"]
        assert set(required_target) == {"field_path", "surface_form", "entity_ref", "target_surface_form"}
        for module in fixtures.MODULES:
            args = minimal(module)
            Draft202012Validator(preview_schema).validate(args)
            preview = await call("preview_person_reference_rewrite", args)
            assert preview["decision"] == "preview_only", preview
            confirm_args = {"preview_id": preview["preview_id"], "ai_confirmation": True}
            Draft202012Validator(confirm_schema).validate(confirm_args)
            confirmed = await call("confirm_person_reference_rewrite", confirm_args)
            assert confirmed["decision"] == "confirmed", confirmed
            assert confirmed["final_fields"] == preview["suggested_fields"]
            stored = await call("remember_memory", fixtures._memory_fields(module, preview, confirmed["rewrite_receipt"]))
            assert stored["stored"] is True, stored
        for name, args, field in (
            ("preview_person_reference_rewrite", minimal(), "module_schema_version"),
            ("confirm_person_reference_rewrite", confirm_args, "final_fields"),
        ):
            tool = server.mcp._tool_manager.get_tool(name)
            supplied = {**args, field: None}
            assert not Draft202012Validator(tool.parameters).is_valid(supplied)
            try:
                tool.fn_metadata.arg_model.model_validate(supplied)
            except ValidationError:
                pass
            else:
                raise AssertionError("explicit null must match published non-null optional schema")
            rejected = await call(name, supplied)
            assert rejected["decision"] == "reject", rejected
        with closing(sqlite3.connect(root / "main.db")) as db:
            assert db.execute("SELECT COUNT(*) FROM authoring_rewrite_receipts WHERE status='consumed'").fetchone()[0] == 4
        return {"decision": "PASS", "minimal_native_chain": True, "legacy_native_chain": True,
                "null_contract_consistent": True, "real_model_calls": 0}


class NativeAIDirectedTests(unittest.TestCase):
    def test_minimal_registered_mcp_schema_dispatch_and_receipt_chain(self):
        kept = {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATH", "PATHEXT", "TEMP", "TMP", "OS"}
        env = {key: value for key, value in os.environ.items() if key.upper() in kept}
        with tempfile.TemporaryDirectory(prefix="author-directed-native-") as temporary:
            root = Path(temporary)
            env.update(STBRAIN_MCP_TOKEN="synthetic-token-00000000000000000000000",
                STBRAIN_WAKE_SECRET="synthetic-wake-0000000000000000000000",
                STBRAIN_OWNER_ID="synthetic-owner", STBRAIN_MODEL_ID="synthetic-model",
                STBRAIN_DB_PATH=str(root / "main.db"), STBRAIN_LEARNING_IDEA_DB_PATH=str(root / "ideas.db"),
                STBRAIN_HALLUCINATION_VAULT_DB_PATH=str(root / "vault.db"),
                STBRAIN_REQUIRE_EXECUTION_BINDING="1", STBRAIN_EXECUTION_EPOCH="synthetic-authoring-epoch",
                STBRAIN_ACCESS_PROFILE="simple-memory-v1", PYTHONIOENCODING="utf-8",
                PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, "-B", "-m", __name__, "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True,
                encoding="utf-8", timeout=90,
                creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("PASS", json.loads(result.stdout.strip().splitlines()[-1])["decision"])


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(native_probe())))
    else:
        unittest.main()
