"""Scene-tag wording reflects actual selection; no private stores or services."""
from __future__ import annotations

import ast
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp_server.usage_guide import module_usage_guide, simple_usage_guide, usage_guide
from runtime.onboarding import OPTIONAL_BRAIN_NOTICE
from runtime.self_governance import LEARNING_EPISODE_BOUNDARY_SIGNAL, SelfGovernanceStore


SPOKEN_PHRASES = ("设个闹钟", "提醒我", "回家了", "还记得")


class SceneTagAuthoringHelpTests(unittest.TestCase):
    def test_static_help_distinguishes_spoken_tags_runtime_signals_and_budget(self):
        with patch("sqlite3.connect", side_effect=AssertionError("static_help_opened_database")):
            for simple in (False, True):
                help_text = module_usage_guide("self_governance_profile", simple=simple)["scene_matching"]
                for phrase in (*SPOKEN_PHRASES, "人类", "自行增改", "完整短语", "casefold",
                               "预算", "开关", "下一次", "系统事件标记", "不会自动推断同义词"):
                    self.assertIn(phrase, help_text)
                self.assertIn("一次未浮现不等于标签写错", help_text)
            for builder in (simple_usage_guide, usage_guide):
                door = builder()["self_reminders"]["scene_tags"]
                for phrase in (*SPOKEN_PHRASES, "人类", "预算", "系统事件标记"):
                    self.assertIn(phrase, door)

    def test_four_wording_maps_explain_separate_fields_without_auto_expansion(self):
        for builder in (simple_usage_guide, usage_guide):
            guide = builder()["wording_guide"]
            self.assertEqual(4, len(guide["wording"]))
            for phrase in ("原词", "近义表达", "语义相关话题", "情感语境关联"):
                self.assertIn(phrase, guide["principle"])
            for phrase in ("奶奶家", "小王", "下雨", "Tasker", "想念", "零食"):
                self.assertIn(phrase, json.dumps(guide, ensure_ascii=False))
            for field in ("keywords", "scene_tags", "emotion", "当前工具", "正文或摘要"):
                self.assertIn(field, guide["fields"])
            self.assertIn("不是固定回复或系统自动扩写", guide["effect"])
            self.assertIn("命中只是候选", guide["effect"])
            for module in ("emotional_memory", "learning_memory", "tool_guidance", "planning_memory"):
                module_guide = module_usage_guide(module, simple=True)["wording_guide"]
                self.assertEqual(guide["wording"], module_guide["wording"])
        # These authoring suggestions do not turn general keywords into user-only tags.
        self.assertIn("内容检索线索", guide["fields"])

    def test_legacy_help_remains_a_bounded_single_literal_return(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "usage_guide.py").read_text(encoding="utf-8"))
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "usage_guide")
        self.assertEqual(1, len(fn.body))
        self.assertIsInstance(fn.body[0], ast.Return)
        literal = ast.literal_eval(fn.body[0].value)
        self.assertEqual(usage_guide(), literal)
        self.assertLessEqual(len(json.dumps(literal, ensure_ascii=False, separators=(",", ":"))), 3500)
        self.assertEqual(simple_usage_guide()["wording_guide"]["wording"], literal["wording_guide"]["wording"])

    def test_initial_and_first_notice_expose_spoken_tag_direction(self):
        for phrase in (*SPOKEN_PHRASES, "本轮人类话语", "预算", "系统事件标记"):
            self.assertIn(phrase, OPTIONAL_BRAIN_NOTICE)
        tree = ast.parse((Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8"))
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "mcp" for target in node.targets))
        instructions = next(keyword.value for keyword in assignment.value.keywords if keyword.arg == "instructions")
        for branch in (instructions.body, instructions.orelse):
            text = "".join(node.value for node in ast.walk(branch)
                          if isinstance(node, ast.Constant) and isinstance(node.value, str))
            for phrase in (*SPOKEN_PHRASES, "本轮人类话语", "预算", "系统事件标记"):
                self.assertIn(phrase, text)

    def test_actual_registered_schema_and_docs_explain_tags_in_both_profiles(self):
        allowed_env = {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATH",
                       "PATHEXT", "TEMP", "TMP", "OS"}
        for profile in ("simple-memory-v1", "legacy"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory(prefix="scene-help-catalog-synthetic-") as folder:
                root = Path(folder).resolve()
                env = {key: value for key, value in os.environ.items() if key.upper() in allowed_env}
                env.update({"STBRAIN_ACCESS_PROFILE": profile,
                    "STBRAIN_MCP_TOKEN": "synthetic-scene-catalog-token-" + "m" * 40,
                    "STBRAIN_WAKE_SECRET": "synthetic-scene-catalog-wake-" + "w" * 40,
                    "STBRAIN_OWNER_ID": "synthetic-scene-owner", "STBRAIN_MODEL_ID": "synthetic-scene-model",
                    "STBRAIN_DB_PATH": str(root / "main.db"),
                    "STBRAIN_LEARNING_IDEA_DB_PATH": str(root / "ideas.db"),
                    "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(root / "vault.db"),
                    "STBRAIN_REQUIRE_EXECUTION_BINDING": "0", "PYTHONNOUSERSITE": "1",
                    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
                result = subprocess.run([sys.executable, "-B", "-m",
                    "mcp_server.tests.test_scene_tag_authoring_help", "--catalog-probe"],
                    cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                    text=True, encoding="utf-8", timeout=45)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual({"decision": "PASS", "network_calls": 0, "real_brain_used": False},
                                 json.loads(result.stdout.strip().splitlines()[-1]))


class SceneTagExistingBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="scene-phrases-synthetic-")
        self.addCleanup(self.folder.cleanup)
        self.store = SelfGovernanceStore(Path(self.folder.name) / "brain.db")
        self.identity = {"owner_id": "synthetic-scene-owner", "model_id": "synthetic-scene-model"}

    def save(self, tags, *, mode="scene_relevant", scope="tool_use"):
        state = self.store.status(**self.identity)["scopes"][scope]
        return self.store.commit_revision(**self.identity, scope=scope, operation="set",
            content={"schema_version": "0.1.0", "text": "合成作者自行撰写的提醒正文。",
                     "trigger_mode": mode, "scene_tags": tags},
            wake_id="synthetic-scene-wake", wake_seq=1, expected_row_version=state["row_version"],
            expected_active_revision=state["active_revision_id"])

    def test_latest_user_text_excludes_assistant_internal_action_description(self):
        from rikkahub_gateway.server import GatewayApplication
        self.save(["动手之前", "想用工具"])
        frame = GatewayApplication._source_frame([
            {"role": "user", "content": "动手之前，先想一想。"},
            {"role": "assistant", "content": "我现在想用工具。"},
            {"role": "user", "content": "请帮我设个闹钟。"}],
            thread_id="synthetic-thread", lineage_stable=True, source_event_id="synthetic-event")
        self.assertEqual("请帮我设个闹钟。", frame["query_text"])
        self.assertEqual([], self.store.build_injection(**self.identity, query=frame["query_text"])["selected_scopes"])
        self.save(list(SPOKEN_PHRASES))
        self.assertEqual(["tool_use"], self.store.build_injection(**self.identity, query=frame["query_text"])["selected_scopes"])

    def test_literal_phrases_casefold_and_no_automatic_synonyms(self):
        self.save(["回家了", "Tasker"])
        for query in ("我终于回家了。", "TASKER 该怎么设置？"):
            self.assertEqual(["tool_use"], self.store.build_injection(**self.identity, query=query)["selected_scopes"])
        for query in ("我到家啦。", "我回 家了。", "自动化工具怎么设置？"):
            self.assertEqual([], self.store.build_injection(**self.identity, query=query)["selected_scopes"])
        self.save(["回家了", "到家啦", "自动化"])
        self.assertEqual(["tool_use"], self.store.build_injection(**self.identity, query="我到家啦。")["selected_scopes"])
        # The guidance is a naming suggestion, not a new validator/word whitelist.
        self.save(["动手之前"])
        self.assertEqual(["tool_use"], self.store.build_injection(**self.identity, query="我动手之前先问你。")["selected_scopes"])

    def test_matching_words_can_still_be_omitted_by_mode_or_budget(self):
        self.save(["提醒我"], mode="manual_only")
        self.assertEqual([], self.store.build_injection(**self.identity, query="提醒我一下。")["selected_scopes"])
        self.save(["提醒我"])
        limited = self.store.build_injection(**self.identity, query="提醒我一下。", budget_tokens=0)
        self.assertEqual([], limited["selected_scopes"])
        self.assertEqual(["tool_use"], limited["omitted_for_budget"])
        self.assertEqual(["tool_use"], self.store.build_injection(**self.identity, query="提醒我一下。")["selected_scopes"])

    def test_reserved_system_marker_is_the_only_exception_described(self):
        self.save([LEARNING_EPISODE_BOUNDARY_SIGNAL], scope="learning_memory")
        typed = self.store.build_injection(**self.identity, query=LEARNING_EPISODE_BOUNDARY_SIGNAL)
        self.assertEqual([], typed["selected_scopes"])
        event = self.store.build_injection(**self.identity, query="普通聊天",
            runtime_scene_signals=[LEARNING_EPISODE_BOUNDARY_SIGNAL])
        self.assertEqual(["learning_memory"], event["selected_scopes"])
        self.save(["提醒我"], scope="learning_memory")
        self.assertEqual(["learning_memory"], self.store.build_injection(**self.identity, query="请提醒我。")["selected_scopes"])
        manual = self.store.manual(**self.identity)
        for phrase in (*SPOKEN_PHRASES, "人类", "完整", "预算", "系统事件标记"):
            self.assertIn(phrase, manual["scene_tag_authoring"])
        self.assertIsNone(manual["blank_structure"]["text"])


def catalog_probe():
    root = Path(os.environ["STBRAIN_DB_PATH"]).resolve().parent
    databases = {root / name for name in ("main.db", "ideas.db", "vault.db")}
    real_connect = sqlite3.connect
    def connect(path, *args, **kwargs):
        assert Path(path).resolve() in databases, "non_synthetic_database"
        return real_connect(path, *args, **kwargs)
    def deny(*args, **kwargs):
        raise AssertionError("network_or_service_creation_forbidden")
    with ExitStack() as guards:
        guards.enter_context(patch("sqlite3.connect", side_effect=connect))
        for name in ("socket.create_connection", "socket.socket.connect", "socket.socket.bind",
                     "socket.getaddrinfo", "subprocess.Popen"):
            guards.enter_context(patch(name, side_effect=deny))
        from mcp_server import server
        tool = server.mcp._tool_manager.get_tool("manage_self_governance_profile")
        schema = json.dumps(tool.parameters["properties"]["scene_tags"], ensure_ascii=False)
        for phrase in (*SPOKEN_PHRASES, "人类", "casefold", "预算", "系统事件标记"):
            assert phrase in schema and phrase in tool.description, "scene_guidance_missing"
        remember = server.mcp._tool_manager.get_tool("remember_memory")
        for phrase in ("奶奶家", "小王", "下雨", "Tasker", "keywords", "scene_tags", "情感分类", "命中只是候选"):
            assert phrase in remember.description, "wording_map_missing"
        return {"decision": "PASS", "network_calls": 0, "real_brain_used": False}


if __name__ == "__main__":
    if sys.argv[1:] == ["--catalog-probe"]:
        print(json.dumps(catalog_probe()))
    else:
        unittest.main()
