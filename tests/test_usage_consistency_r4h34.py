"""Static usage-contract checks: no stores, MCP server import or live config.

The reviewed AST snapshots erase only explicitly selected static documentation
values. All remaining code, schemas, candidate projections and gates must stay
identical except for the explicitly reviewed r4h38 core facade delta below.
This does not claim a phone delivery or a real write has succeeded.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MCP = ROOT / "mcp_server"
STATIC_KEYS = {
    "emotional_service.py": ("tools", "write_rule"),
    "learning_service.py": ("tools", "write_rule"),
    "planning_service.py": ("tools", "write_rule", "creation_field_rules"),
    "service.py": ("public_workflow",),
    "usage_guide.py": ("daily_instruction", "result", "uncertainty", "plan_effect", "read", "advanced", "transport", "privacy"),
}
NEW_STATIC_HELP_KEYS = ("ordinary_revision", "plan_progress")
REVIEWED_PRINCIPLE_PREFIXES = {
    "learning_service.py": ("小修一次留痕；", "普通白名单字段用 revise_memory"),
    "planning_service.py": ("常驻计划最多两条；", "有效活动的 internal persistent 最多两条；"),
}
# Filled from the independently reviewed r4h33 AST with only STATIC_KEYS erased.
# Adding another erased field requires a fresh documentation/behavior review.
HISTORICAL_R4H34_NON_DOCUMENTATION_SHA256 = {
    "emotional_service.py": "8d6dfe36e0395faeb128e08b36a7efd70fced7591f2e9a19a0dc72bf1bd5cea2",
    "learning_service.py": "057e7fa7e3549357dbbb1903643032831d20a6e8cb95e411cf216506728d1a90",
    "planning_service.py": "4675af6a4536a3dd3469abbff0d228640b73755c856b81ba3b408a752815f50c",
    "service.py": "261f5d4603ef9e0ceeb549665e92d1b02ec5da48ae2e12613086c2dfe1821720",
    "usage_guide.py": "2ace29dfa0b03addfc663c0c9f3c144c8d333c9bc1f6b20d1846f15411ec74c1",
}

# Independently reviewed against frozen r4h37: exactly five changed service
# functions plus QueryView. Restoring those six AST nodes restores the complete
# baseline AST. The erasure algorithm and all other file pins are unchanged.
# Evidence: task receipt r4h38-reviewed-service-delta.json, SHA256
# 20c22b4437c7d8c9e78a7c0662c39321f4ba4791b00f72f0d7ac705d8f77585b.
REVIEWED_NON_DOCUMENTATION_SHA256 = {
    **HISTORICAL_R4H34_NON_DOCUMENTATION_SHA256,
    "service.py": "c4f0600e5d1f45948d7e0ab82e0b9d65560ee389ec5443de182f50fa1f9191b4",
}


def tree(filename):
    return ast.parse((MCP / filename).read_bytes(), filename=filename)


def dictionary_field(parsed, key):
    if key == "daily_instruction":
        matches = [node for node in ast.walk(parsed) if isinstance(node, ast.Dict)
                   and any(isinstance(k, ast.Constant) and k.value == "tool"
                           and isinstance(v, ast.Constant) and v.value == "remember_memory"
                           for k, v in zip(node.keys, node.values))]
        if len(matches) != 1:
            raise AssertionError("expected one ordinary-entry help dictionary")
        return dictionary_field(matches[0], "instruction")
    found = [(node, index) for node in ast.walk(parsed) if isinstance(node, ast.Dict)
             for index, item in enumerate(node.keys)
             if isinstance(item, ast.Constant) and item.value == key]
    if len(found) != 1:
        raise AssertionError(f"expected exactly one static field: {key}, found {len(found)}")
    node, index = found[0]
    return node, index


def literal_field(filename, key):
    node, index = dictionary_field(tree(filename), key)
    return ast.literal_eval(node.values[index])


def non_documentation_hash(filename, parsed=None):
    parsed = tree(filename) if parsed is None else parsed
    if filename == "usage_guide.py":
        for node in ast.walk(parsed):
            if isinstance(node, ast.Dict):
                keep = [(key, value) for key, value in zip(node.keys, node.values)
                        if not isinstance(key, ast.Constant) or key.value not in NEW_STATIC_HELP_KEYS]
                node.keys = [key for key, _ in keep]
                node.values = [value for _, value in keep]
    if filename in REVIEWED_PRINCIPLE_PREFIXES:
        node, index = dictionary_field(parsed, "principles")
        principles = node.values[index]
        if not isinstance(principles, ast.List):
            raise AssertionError("expected static principles list")
        changed = [index for index, value in enumerate(principles.elts)
                   if isinstance(value, ast.Constant) and isinstance(value.value, str)
                   and value.value.startswith(REVIEWED_PRINCIPLE_PREFIXES[filename])]
        if len(changed) != 1:
            raise AssertionError("expected one precisely selected static principle")
        principles.elts[changed[0]] = ast.Constant(value="REVIEWED_STATIC_PRINCIPLE")
    for key in STATIC_KEYS[filename]:
        node, index = dictionary_field(parsed, key)
        node.values[index] = ast.Constant(value="REVIEWED_STATIC_DOCUMENTATION")
    return hashlib.sha256(ast.dump(parsed, include_attributes=False).encode()).hexdigest()


def tool_functions():
    return [node for node in tree("server.py").body if isinstance(node, ast.AsyncFunctionDef)
            and any(isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
                    and isinstance(item.func.value, ast.Name) and item.func.value.id == "mcp"
                    and item.func.attr == "tool" for item in node.decorator_list)]


def guide():
    node = next(node for node in tree("usage_guide.py").body
                if isinstance(node, ast.FunctionDef) and node.name == "usage_guide")
    if len(node.body) != 1 or not isinstance(node.body[0], ast.Return):
        raise AssertionError("help must remain a single literal return, not open a context")
    return ast.literal_eval(node.body[0].value)


class UsageConsistencyTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "sqlite3.connect", "subprocess.Popen"):
            self.enterContext(patch(target, side_effect=AssertionError("external I/O forbidden")))

    def test_only_reviewed_documentation_and_core_facade_delta_changed(self):
        for filename, expected in REVIEWED_NON_DOCUMENTATION_SHA256.items():
            with self.subTest(filename=filename):
                self.assertEqual(expected, non_documentation_hash(filename))

    def test_daily_contract_still_requires_only_module_and_content(self):
        functions = tool_functions()
        self.assertEqual(43, len(functions))
        self.assertEqual(43, len({node.name for node in functions}))
        daily = next(node for node in functions if node.name == "remember_memory")
        required = len(daily.args.args) - len(daily.args.defaults)
        self.assertEqual(["module", "content"], [arg.arg for arg in daily.args.args[:required]])
        annotation = daily.args.args[0].annotation
        self.assertIsInstance(annotation, ast.Subscript)
        self.assertEqual(("emotional_memory", "learning_memory", "planning_memory"), ast.literal_eval(annotation.slice))
        self.assertNotIn("expected_row_version", [arg.arg for arg in daily.args.args])
        self.assertIn("No preliminary stbrain_open", ast.get_docstring(daily))

    def test_all_daily_manuals_recommend_the_existing_one_call_entry(self):
        for module in ("emotional_memory", "learning_memory"):
            filename = "emotional_service.py" if module == "emotional_memory" else "learning_service.py"
            instruction = literal_field(filename, "tools")["remember_memory"]
            with self.subTest(module=module):
                self.assertIn("module=" + module, instruction)
                self.assertIn("content=", instruction)
                self.assertIn("一次保存", instruction)
                self.assertIn("无需先 open、手填版本或另轮审核", instruction)
        entry = literal_field("planning_service.py", "ordinary_entry")
        self.assertEqual("remember_memory", entry["tool"])
        self.assertEqual(["module", "content"], entry["required_arguments"])
        self.assertIn("不用先开脑", entry["instruction"])

    def test_old_module_rules_are_limited_to_advanced_writes(self):
        for filename in ("emotional_service.py", "learning_service.py", "planning_service.py"):
            with self.subTest(filename=filename):
                rule = literal_field(filename, "write_rule")
                self.assertIn("高级", rule)
                self.assertIn("remember_memory", rule)
                self.assertIn("write_context_ref", rule)
                self.assertIn("row_version", rule)
        self.assertIn("不适用于普通新增", literal_field("planning_service.py", "creation_field_rules")["scope"])

    def test_advanced_tool_directories_and_query_capabilities_are_not_removed(self):
        expected = {
            "emotional_service.py": {"remember_memory", "revise_memory", "remember_emotional_memory", "recall_emotional_memory",
                "revise_emotional_memory", "integrate_emotional_memories", "manage_brain_pin", "veto_ephemeral_memory"},
            "learning_service.py": {"remember_memory", "revise_memory", "remember_learning_memory", "remember_learning_contrast_pair",
                "recall_learning_memory", "revise_learning_memory", "integrate_learning_memories",
                "review_learning_change", "preview_learning_recall"},
        }
        published = {node.name for node in tool_functions()}
        for filename, names in expected.items():
            with self.subTest(filename=filename):
                self.assertEqual(names, set(literal_field(filename, "tools")))
                self.assertTrue(names <= published)

    def test_global_workflow_does_not_turn_daily_saving_into_personality_review(self):
        workflow = literal_field("service.py", "public_workflow")
        self.assertLessEqual(len(workflow), 5)
        self.assertIn("remember_memory(module, content)", workflow[0])
        self.assertIn("无需先 open", workflow[0])
        text = " ".join(workflow)
        self.assertIn("view='manual'", text)
        self.assertIn("summary", text)
        self.assertIn("不是已读候选的证明", text)
        self.assertIn("模块一候选审核与激活仍须跨真实唤醒", text)
        self.assertNotIn("stbrain_open 会返回全局手册", text)

    def test_help_stays_static_short_and_names_only_available_tools(self):
        result = guide()
        self.assertEqual("daily-memory/1", result["contract_version"])
        self.assertIs(result["state_changed"], False)
        self.assertEqual(["module", "content"], result["daily_memory"]["required"])
        self.assertEqual(["emotional_memory", "learning_memory", "planning_memory"], result["daily_memory"]["modules"])
        serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(serialized), 1900)
        names = set(re.findall(r"\b(?:remember|recall|stbrain|preview|revise|advance)_[a-z_]+", serialized))
        self.assertTrue(names <= {node.name for node in tool_functions()})

    def test_help_does_not_fabricate_storage_verification_or_direct_authority(self):
        result = guide()
        self.assertIn("真实 stored", result["daily_memory"]["result"])
        self.assertIn("不自动重试", result["daily_memory"]["result"])
        self.assertIn("不代表已独立核实", result["daily_memory"]["uncertainty"])
        self.assertIn("人类独立授权", result["transport"])
        self.assertIn("有效 scope", result["transport"])
        self.assertIn("remember_memory", result["transport"])
        self.assertIn("不授权或自动执行外部操作", result["daily_memory"]["plan_effect"])

    def test_document_current_route_and_historical_evidence_are_distinct(self):
        document = (MCP / "OPEN_RESPONSE.md").read_text(encoding="utf-8")
        self.assertIn("public-tools/20 / brain-open/2", document.splitlines()[0])
        self.assertIn("43 个工具候选目录", document)
        self.assertIn("不需要先 `stbrain_open`", document)
        self.assertIn("原 public-tools/16（39 工具）", document)
        self.assertIn("不是手机成功回执", document)
        self.assertIn("仅用于专用高级工具", document)
        self.assertIn("stbrain_open_direct", document)
        for field in ("memory_id", "target_ref", "plan_ref"):
            self.assertIn("`" + field + "`", document)

    def test_no_new_philosophical_or_mandatory_behavior_prompt(self):
        texts = [(MCP / "OPEN_RESPONSE.md").read_text(encoding="utf-8"), json.dumps(guide(), ensure_ascii=False)]
        for filename, keys in STATIC_KEYS.items():
            for key in keys:
                texts.append(json.dumps(literal_field(filename, key), ensure_ascii=False))
        for forbidden in ("必须自由", "必须自主", "必须拒绝", "呼吸流程", "夜间消化", "dream"):
            self.assertNotIn(forbidden, " ".join(texts))

    def test_revision_help_matches_exact_runtime_whitelist_without_importing_service(self):
        service = tree("daily_revision_service.py")
        assignment = next(node for node in service.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "ORDINARY_REVISION_FIELDS"
                                  for target in node.targets))
        allowed = {key.value: ast.literal_eval(value.args[0])
                   for key, value in zip(assignment.value.keys, assignment.value.values)}
        documented = guide()["ordinary_revision"]
        self.assertEqual("revise_memory", documented["tool"])
        self.assertEqual(["target_ref", "changes"], documented["required"])
        self.assertEqual(allowed, {key: set(value) for key, value in documented["allowed_fields"].items()})
        self.assertIn("已读的精确版本", documented["instruction"])
        self.assertIn("不自动换最新版", documented["instruction"])
        for fields in allowed.values():
            self.assertFalse(fields & {"original_text", "current_understanding", "source_basis", "state", "parent_ref"})

    def test_progress_help_preserves_read_event_sequence_and_real_evidence(self):
        documented = guide()["plan_progress"]
        self.assertEqual("advance_plan", documented["tool"])
        self.assertEqual(["target_ref", "expected_event_seq", "event_type", "note"], documented["required"])
        self.assertIn("查询返回", documented["instruction"])
        self.assertIn("真实证据", documented["instruction"])
        self.assertIn("不手填模块行版本", documented["instruction"])

    def test_persistent_manual_preserves_limits_and_same_wake_snapshot(self):
        text = " ".join(literal_field("planning_service.py", "principles"))
        for expected in ("有效活动的 internal persistent 最多两条", "每个新真实唤醒", "reminder",
                         "session_start 开场项最多一条", "不超过三条", "1200 token", "同一唤醒续接不刷新快照"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
