"""Offline entry-help checks: AST only, never import server or open a store."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


def source_tree(relative: str) -> ast.Module:
    return ast.parse((ROOT / relative).read_bytes(), filename=relative)


def literal_help() -> dict:
    tree = source_tree("mcp_server/usage_guide.py")
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "usage_guide")
    if len(function.body) != 1 or not isinstance(function.body[0], ast.Return):
        raise AssertionError("help must remain a literal return without side effects")
    return ast.literal_eval(function.body[0].value)


def public_names() -> set[str]:
    tree = source_tree("mcp_server/public_contract.py")
    assignment = next(node for node in tree.body
                      if isinstance(node, ast.AnnAssign)
                      and isinstance(node.target, ast.Name)
                      and node.target.id == "PUBLIC_TOOL_NAMES")
    return set(ast.literal_eval(assignment.value))


class StEntryHelpTests(unittest.TestCase):
    def test_help_remains_short_static_and_only_names_published_tools(self):
        help_ = literal_help()
        self.assertIs(help_["state_changed"], False)
        self.assertEqual("daily-memory/1", help_["contract_version"])
        serialized = json.dumps(help_, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(serialized), 3500)
        mentioned = set(re.findall(
            r"\b(?:remember|recall|stbrain|query|preview|revise|advance)_[a-z_]+", serialized))
        mentioned -= {field for fields in help_['ordinary_revision']['allowed_fields'].values() for field in fields}
        self.assertTrue(mentioned <= public_names(), mentioned - public_names())
        self.assertEqual(44, len(public_names()))
        self.assertFalse({"breath", "stbrain_breath"} & public_names())
        self.assertNotIn("stbrain_breath", serialized)

    def test_exact_read_routes_and_module_distinctions(self):
        read = literal_help()["read"]
        for text in (
            "StillerBrain（ST）", "stbrain_open(view='recall', query=查询词)",
            "空query", "四普通脑", "module", "next_cursor", "detail_lookup",
            "各recall_*", "学习inventory", "query_self_model",
            "身份和披露边界", "零命中不等于库为空", "partial/errors",
        ):
            self.assertIn(text, read)

    def test_no_workspace_dependency_or_new_author_confirmation(self):
        help_ = literal_help()
        self.assertIn("使用 ST 无需 Shell 或 workspace", help_["transport"])
        self.assertIn("无需从文件提取工具结果", help_["transport"])
        self.assertIn("人类独立授权", help_["transport"])
        serialized = json.dumps(help_, ensure_ascii=False)
        for forbidden in ("必须每轮", "必须先运行", "必须以我开头", "必须以“我”开头"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(["module", "content"], help_["daily_memory"]["required"])
        self.assertIn("不用先 open", help_["daily_memory"]["instruction"])

    def test_recall_view_and_core_three_wakes_remain_separate(self):
        advanced = literal_help()["advanced"]
        for text in ("summary/manual/review", "view='recall'", "只读查询入口",
                     "view='manual'", "提交、后来复核、再后来激活", "真实唤醒流程",
                     "普通记忆专用修订和整合直接保存", "全部页齐才形成展示证明"):
            self.assertIn(text, advanced)

    def test_documented_query_parameters_exist_without_server_import(self):
        tree = source_tree("mcp_server/server.py")
        functions = {node.name: node for node in tree.body
                     if isinstance(node, ast.AsyncFunctionDef)}
        expected = {
            "recall_emotional_memory": {"query", "memory_id"},
            "recall_learning_memory": {"query", "target_ref", "view", "offset"},
            "recall_planning_memory": {"query", "plan_ref"},
            "recall_tool_guidance": {"query", "tool_name", "card_id"},
            "query_self_model": {"view"},
            "stbrain_open": {"view", "module", "query", "limit", "cursor"},
        }
        for name, required in expected.items():
            with self.subTest(tool=name):
                self.assertTrue(required <= {arg.arg for arg in functions[name].args.args})
        self.assertNotIn("view", {arg.arg for arg in functions["recall_planning_memory"].args.args})


if __name__ == "__main__":
    unittest.main(verbosity=2)
