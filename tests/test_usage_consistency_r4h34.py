"""Static usage-contract checks: no stores, MCP server import or live config.

The core facade retains reviewed AST pins. Ordinary-memory behavior is now
intentionally different in the local simple profile and is exercised by the
full revision and native transport suites rather than the old documentation-only pin.
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
    # Local L13/L14 review: only __init__, health, open_brain, compact/selected
    # projections and four display/manual helpers differ from the sealed prior
    # service. Restoring those five AST methods and removing four new helpers
    # reproduces the full baseline AST exactly. The 21 untouched methods,
    # including every self-write/activation facade, are pinned independently below.
    # L19 independently reviewed: one new recall_memory read facade and three
    # updated static strings. Removing/restoring exactly these nodes reproduces
    # the sealed L18 whole-file AST; see service-ast-delta-verification.json.
    # The 21 protected self-edit methods and the erasure algorithm stay exact.
    # L20: only the facets visibility help literal differs from sealed L19.
    # Restoring that one literal reproduces the entire L19 AST; the erasure
    # algorithm remains unchanged; content_schema changes only this help literal.
    "service.py": "8ec2ed634c1c12e55f10f9acdade1c0e60c38c4917c8201ebea430306df0c82a",
}
PROTECTED_SELF_METHODS = (
    '_action_for_public_intent', '_bound_public_write', '_candidate_for_this_model',
    '_current_candidate_matches', '_onboarding_status', '_progression_required',
    '_public_gate_denial', '_public_payload_denial', 'activate_candidate',
    'activate_self_model_candidate', 'content_schema', 'continue_module_one',
    'get_active', 'module_one_status', 'open_brain_direct', 'prepare', 'query_self_model',
    'recheck_candidate', 'search', 'store_candidate', 'submit_self_model_candidate',
)
# L20 whole-file restore proof: content_schema changes only facets visibility
# help. All other protected methods remain byte-for-byte AST equivalent to L19.
PROTECTED_SELF_METHODS_SHA256 = '45149f76209b006c997b9c3b00a28db642b28e13a9c9e068906e8c4b2e4b28e5'


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
    parsed = tree(filename)
    if filename == "usage_guide.py":
        parsed = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == "usage_guide")
    node, index = dictionary_field(parsed, key)
    return ast.literal_eval(node.values[index])


def stable_ast_snapshot(parsed):
    """Version-independent single-line representation for these reviewed pins.

    The stored hashes use the compact 3.14 representation, but ast.dump's empty
    list default differs on 3.12. Define that policy here instead of selecting a
    dump mode by interpreter version. Keep declared field order, list order,
    node kinds and exact literal values; source-location attributes are absent.
    Class-level None defaults identify optional AST fields on both versions.
    Constant.value=None and None entries inside lists remain actual values.
    This is a deliberately narrow serializer, not an AST semantic validator.
    """
    missing = object()

    def render(value):
        if isinstance(value, ast.AST):
            node_type = type(value)
            if getattr(ast, node_type.__name__, None) is not node_type:
                raise TypeError("unrecognized AST node type")
            fields = []
            for name in node_type._fields:
                item = getattr(value, name, missing)
                if item is missing:
                    raise TypeError("missing declared AST field: " + name)
                if item is None:
                    if getattr(node_type, name, missing) is None:
                        continue
                    if not (node_type is ast.Constant and name == "value"):
                        raise TypeError("None in non-optional AST field: " + name)
                if node_type is ast.Constant and name == "value":
                    if type(item) not in (str, bytes, int, float, complex, bool, type(None), type(Ellipsis)):
                        raise TypeError("unsupported AST constant value")
                elif type(item) is list and not item:
                    continue
                fields.append(name + "=" + render(item))
            return node_type.__name__ + "(" + ", ".join(fields) + ")"
        if type(value) is list:
            return "[" + ", ".join(render(item) for item in value) + "]"
        if type(value) in (str, bytes, int, float, complex, bool, type(None), type(Ellipsis)):
            return repr(value)
        raise TypeError("unsupported AST snapshot value")

    if not isinstance(parsed, ast.AST):
        raise TypeError("expected an AST root")
    return render(parsed)


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
    return hashlib.sha256(stable_ast_snapshot(parsed).encode("utf-8")).hexdigest()


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
        self.assertEqual(REVIEWED_NON_DOCUMENTATION_SHA256['service.py'], non_documentation_hash('service.py'))
        # The planning service now deliberately uses the direct writer; preserve
        # this substantive expectation instead of merely updating a file hash.
        called = {node.func.attr for node in ast.walk(tree('planning_service.py'))
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertTrue({'remember_direct', 'revise_direct'} <= called)
        self.assertNotIn('propose_create', called)

    def test_self_write_review_activation_and_binding_methods_remain_exact_baseline_ast(self):
        cls = next(n for n in tree('service.py').body
                   if isinstance(n, ast.ClassDef) and n.name == 'SelfModelAccessService')
        methods = {n.name: stable_ast_snapshot(n) for n in cls.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in PROTECTED_SELF_METHODS}
        self.assertEqual(set(PROTECTED_SELF_METHODS), set(methods))
        self.assertEqual(PROTECTED_SELF_METHODS_SHA256,
                         hashlib.sha256(json.dumps(methods, sort_keys=True).encode()).hexdigest())

    def test_snapshot_does_not_depend_on_ast_dump(self):
        with patch.object(ast, "dump", side_effect=AssertionError("ast.dump must not be used")):
            # Only this facade remains pinned in L25. Ordinary modules have real
            # reviewed behavior changes, so do not restore their historical pins.
            self.assertEqual(REVIEWED_NON_DOCUMENTATION_SHA256["service.py"],
                             non_documentation_hash("service.py"))
            self.test_self_write_review_activation_and_binding_methods_remain_exact_baseline_ast()

    def test_snapshot_has_complete_nonempty_fields_and_preserves_order(self):
        parsed = ast.parse("def guarded(user, allowed=False):\n    return user if allowed else None")
        self.assertEqual(
            "Module(body=[FunctionDef(name='guarded', args=arguments(args=[arg(arg='user'), "
            "arg(arg='allowed')], defaults=[Constant(value=False)]), body=[Return(value=IfExp("
            "test=Name(id='allowed', ctx=Load()), body=Name(id='user', ctx=Load()), "
            "orelse=Constant(value=None)))])])", stable_ast_snapshot(parsed),
        )
        self.assertEqual(
            "Dict(keys=[None, Constant(value='x')], values=[Name(id='source', ctx=Load()), "
            "Constant(value=0)])",
            stable_ast_snapshot(ast.parse("{**source, 'x': 0}", mode="eval").body),
        )

    def test_snapshot_keeps_falsy_and_non_string_constants_distinct(self):
        literals = [None, False, 0, "", b"", 0j, Ellipsis, True, 1, "1", b"1", 1.0, 1j]
        representations = [stable_ast_snapshot(ast.Constant(value=value)) for value in literals]
        self.assertEqual(len(literals), len(set(representations)))
        for value, representation in zip(literals, representations):
            with self.subTest(value=repr(value)):
                self.assertEqual("Constant(value=" + repr(value) + ")", representation)

    def test_snapshot_empty_fields_optional_defaults_and_locations_are_neutral(self):
        parsed = ast.parse("def f():\n    pass")
        expected = "Module(body=[FunctionDef(name='f', args=arguments(), body=[Pass()])])"
        self.assertEqual(expected, stable_ast_snapshot(parsed))
        function = parsed.body[0]
        function.returns = None
        function.type_comment = None
        function.type_params = []
        parsed.type_ignores = []
        ast.increment_lineno(parsed, 100)
        self.assertEqual(expected, stable_ast_snapshot(parsed))
        parameter = ast.TypeVar(name="T", bound=None)
        original_fields = ast.TypeVar._fields
        older_fields = tuple(name for name in original_fields if name != "default_value")
        with patch.object(ast.TypeVar, "_fields", older_fields):
            older = stable_ast_snapshot(parameter)
        with patch.object(ast.TypeVar, "_fields", older_fields + ("default_value",)):
            with patch.object(ast.TypeVar, "default_value", None, create=True):
                parameter.default_value = None
                self.assertEqual(older, stable_ast_snapshot(parameter))
                parameter.default_value = ast.Name(id="int", ctx=ast.Load())
                self.assertNotEqual(older, stable_ast_snapshot(parameter))
                self.assertIn("default_value=Name(id='int', ctx=Load())", stable_ast_snapshot(parameter))

    def test_snapshot_rejects_unknown_values_and_missing_required_fields(self):
        class UnknownNode(ast.AST):
            _fields = ()
        for value in ({}, set(), (), object(), []):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(TypeError):
                    stable_ast_snapshot(ast.Constant(value=value))
        with self.assertRaises(TypeError):
            stable_ast_snapshot(UnknownNode())
        missing_name = ast.Name(id="user", ctx=ast.Load())
        del missing_name.id
        with self.assertRaises(TypeError):
            stable_ast_snapshot(missing_name)
        with self.assertRaises(TypeError):
            stable_ast_snapshot(ast.Expr(value=None))
        with self.assertRaises(TypeError):
            stable_ast_snapshot([ast.Pass()])

    def test_business_constants_guards_parameters_order_and_permissions_change_hash(self):
        variants = {
            "constant": ("def gate(user):\n    return 7", "def gate(user):\n    return 8"),
            "guard": ("def gate(user):\n    return user if allowed else None",
                      "def gate(user):\n    return user if bypass else None"),
            "parameter": ("def gate(user):\n    return user", "def gate(user, bypass=False):\n    return user"),
            "list_order": ("actions = ['read', 'write']", "actions = ['write', 'read']"),
            "permission_value": ("policy = {'requires_permission': True}", "policy = {'requires_permission': False}"),
            "permission_field": ("policy = {'requires_permission': True}", "policy = {'bypass_permission': True}"),
        }
        for case, (before, after) in variants.items():
            with self.subTest(case=case):
                original, changed = tree("service.py"), tree("service.py")
                original.body.extend(ast.parse(before).body)
                changed.body.extend(ast.parse(after).body)
                self.assertNotEqual(non_documentation_hash("service.py", original),
                                    non_documentation_hash("service.py", changed))

    def test_reviewed_documentation_edits_remain_hash_neutral(self):
        # Preserve the currently active facade pin and its exact existing erasure
        # allowlist; do not reintroduce old ordinary-module documentation pins.
        for key in STATIC_KEYS["service.py"]:
            with self.subTest(key=key):
                parsed = tree("service.py")
                node, index = dictionary_field(parsed, key)
                node.values[index] = ast.Constant(value="Independently reviewed wording update")
                self.assertEqual(REVIEWED_NON_DOCUMENTATION_SHA256["service.py"],
                                 non_documentation_hash("service.py", parsed))

    def test_daily_contract_still_requires_only_module_and_content(self):
        functions = tool_functions()
        self.assertEqual(44, len(functions))
        self.assertEqual(44, len({node.name for node in functions}))
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
                self.assertTrue("write_context_ref" in rule or "上下文" in rule)
                self.assertTrue("普通" in rule)
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
        self.assertLessEqual(len(serialized), 3500)
        names = set(re.findall(r"\b(?:remember|recall|stbrain|preview|revise|advance)_[a-z_]+", serialized))
        names -= {field for fields in result['ordinary_revision']['allowed_fields'].values() for field in fields}
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
        self.assertIn("44 个公开工具", document)
        self.assertIn('stbrain_open(view="recall", query="查询内容")', document)
        self.assertIn("官端直连 MCP 和网关注入模型均可写入、修改普通记忆", document)
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

    def test_revision_help_matches_exact_whitelists_and_scoped_tool_manual(self):
        service = tree("daily_revision_service.py")
        assignment = next(node for node in service.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "ORDINARY_REVISION_FIELDS"
                                  for target in node.targets))
        allowed = {key.value: ast.literal_eval(value.args[0])
                   for key, value in zip(assignment.value.keys, assignment.value.values)}
        documented = guide()["ordinary_revision"]
        self.assertEqual("revise_memory", documented["tool"])
        self.assertEqual(["target_ref", "changes"], documented["required"])
        listed = {key: set(value) for key, value in documented["allowed_fields"].items()}
        # Keep the overview short: tool-card fields live in its existing
        # selected module manual, with the same exact runtime field check.
        from mcp_server.usage_guide import module_usage_guide
        self.assertIn("stbrain_help(module='tool_guidance')", documented['tool_guidance_help'])
        self.assertNotIn('tool_guidance', listed)
        for simple in (False, True):
            page = module_usage_guide('tool_guidance', simple=simple)
            complete = {**listed, 'tool_guidance': set(page['revise_author_fields'])}
            self.assertEqual(allowed, complete)
        self.assertIn("已读的精确版本", documented["instruction"])
        self.assertIn("不自动换最新版", documented["instruction"])
        self.assertIn('original_text', allowed['emotional_memory'])
        self.assertIn('current_understanding', allowed['learning_memory'])
        self.assertIn('original_text', allowed['planning_memory'])
        for fields in allowed.values():
            self.assertFalse(fields & {"owner_id", "model_id", "version", "hash", "state"})

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
