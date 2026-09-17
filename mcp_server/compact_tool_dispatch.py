"""A small stable toolbox; selected actions retain the original guarded path."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from runtime.compact_tool_routes import (
    ACTION_CATEGORIES, CATEGORIES, CompactRouteError, DISCOVERY_TOOL,
    MANAGE_TOOL, resolve_compact_action,
)
from runtime.execution_binding import EXECUTION_CONTRACT, EXECUTION_TOOLS

DISCOVERY_DESCRIPTION = (
    "常用入口不是全部能力；其余 ST 操作只是收进分类工具箱，没有要求人类逐个开关。"
    "想改自己的提示词/安全阀、开关浮现、改写人称、整理归纳记忆或读调用经验时，"
    "我可以自行来这里找，不必等人类提醒。"
    "按分类找 ST 功能，不必切换 MCP 档位。省略参数列分类；category 列该类操作；"
    "action 返回一个操作的完整参数说明。分类：self 自我定义、person 人称与存前提醒、"
    "diy 自写提示词/安全阀/浮现开关、emotion 情感、learning 学习、tools 工具、"
    "planning 计划、vault 黑匣子、system 状态/手册、memory 通用记忆。"
    "已知参数可直接 stbrain_manage，不强制先读说明。"
)
MANAGE_DESCRIPTION = (
    "这是收起的 ST 功能的执行入口，不是只能使用眼前几个常用工具。"
    "使用 ST 分类工具箱中的任一操作。action 填操作名，arguments 填该操作参数，"
    "例如 action='query_injection_control', arguments={} 可查看浮现开关。"
    "不知道参数时用 stbrain_tools(action=...)，不必请人切档或刷新目录。"
    "说明书、回执或 detail_lookup 中提到的 ST 操作名，若不在当前独立工具目录中，"
    "将它填入 action、原参数填入 arguments；不要直接调用未列出的旧工具名。"
    "这是完整 ST 操作入口，不执行其他 MCP 或系统命令；原有模块一密码、唤醒和权限规则不变。"
    "人类可在客户端禁用或设置审批此入口；启用它代表允许在原权限内选择其子操作。"
)


def install_compact_tool_dispatch(mcp: Any) -> None:
    manager = mcp._tool_manager
    if getattr(manager, "_compact_dispatch_installed", False):
        raise RuntimeError("compact_dispatch_already_installed")
    original = manager.call_tool  # Includes execution, password and ordinary guards.

    def available(action: str) -> bool:
        return action in ACTION_CATEGORIES and manager.get_tool(action) is not None

    async def stbrain_tools(category: str = "", action: str = "") -> dict[str, Any]:
        if category and category not in CATEGORIES:
            return {"decision": "reject", "reason_code": "compact_category_unknown",
                    "categories": list(CATEGORIES), "state_changed": False}
        if action:
            if not available(action) or (category and ACTION_CATEGORIES[action] != category):
                return {"decision": "reject", "reason_code": "compact_action_unknown", "state_changed": False}
            # list_tools carries the current owner-scoped, optional prewrite
            # reminder snapshot; get_tool alone is only the static base text.
            tool = next((item for item in manager.list_tools() if item.name == action),
                        manager.get_tool(action))
            schema = deepcopy(tool.parameters)
            schema.get("properties", {}).pop("execution_ref", None)
            # These are descriptions, not separately advertised native tools.
            return {"action": action, "category": ACTION_CATEGORIES[action],
                    "description": tool.description, "arguments_schema": schema,
                    "call_with": "stbrain_manage(action=action, arguments={...})",
                    "manual_required_before_call": False, "state_changed": False}
        if category:
            return {"category": category, "label": CATEGORIES[category][0],
                    "actions": [name for name in CATEGORIES[category][1] if available(name)],
                    "next": "已知参数直接 stbrain_manage；需要参数说明用 stbrain_tools(action=操作名)。",
                    "state_changed": False}
        return {"categories": [{"category": key, "label": label,
                                "action_count": sum(available(name) for name in names)}
                               for key, (label, names) in CATEGORIES.items()],
                "next": "按 category 找操作，或直接按 action 看说明；无需人类开关子工具。",
                "state_changed": False}

    async def stbrain_manage(action: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        # Never execute this stub: all calls must pass the guarded dispatcher.
        return {"decision": "reject", "reason_code": "compact_dispatch_required", "state_changed": False}

    count = sum(available(name) for name in ACTION_CATEGORIES)
    toolbox_notice = f"ST 分类工具箱可按需访问 {count} 项操作（包含常用能力，不是额外新增 {count} 项）。"
    mcp.add_tool(stbrain_tools, name=DISCOVERY_TOOL, description=toolbox_notice + DISCOVERY_DESCRIPTION)
    mcp.add_tool(stbrain_manage, name=MANAGE_TOOL, description=toolbox_notice + MANAGE_DESCRIPTION)
    manage_tool = manager.get_tool(MANAGE_TOOL)
    manage_tool.parameters = {
        "type": "object", "required": ["action"], "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "description": "stbrain_tools 中的操作名（不是客户端前缀名）。"},
            "arguments": {"type": "object", "description": "该操作的参数对象；无参数可省略。", "additionalProperties": True},
            "execution_ref": {"type": "string", "pattern": "^stexec_[A-Za-z0-9_-]{43}$",
                "description": "仅网关宿主填入；模型和直连调用省略。",
                "x-stbrain-execution-tool": MANAGE_TOOL,
                "x-stbrain-execution-contract": EXECUTION_CONTRACT},
        },
    }

    def rejected(code: str, convert: bool) -> Any:
        result = {"decision": "reject", "reason_code": code, "state_changed": False,
                  "next_action": "用 stbrain_tools(action=操作名) 查看参数后重试；不要把操作名当成独立工具名。"}
        return manage_tool.fn_metadata.convert_result(result) if convert else result

    async def dispatch(name: str, arguments: dict[str, Any], context: Any = None,
                       convert_result: bool = False) -> Any:
        if name != MANAGE_TOOL:
            return await original(name, arguments, context=context, convert_result=convert_result)
        try:
            envelope = dict(arguments)
            has_ref = "execution_ref" in envelope
            ref = envelope.pop("execution_ref", None)
            action, inner = resolve_compact_action(envelope)
            if not available(action):
                return rejected("compact_action_unavailable", convert_result)
            # MCP validates the outer envelope. Validate the exact inner public
            # schema too; a free-form map never widens the original operation.
            target = manager.get_tool(action)
            Draft202012Validator(target.parameters).validate(inner)
        except CompactRouteError as error:
            return rejected(str(error), convert_result)
        except (TypeError, ValueError, ValidationError):
            return rejected("compact_action_arguments_invalid", convert_result)
        if has_ref:
            if action not in EXECUTION_TOOLS:
                return rejected("compact_execution_ref_unexpected", convert_result)
            inner["execution_ref"] = ref
        return await original(action, inner, context=context, convert_result=convert_result)

    manager.call_tool = dispatch
    manager._compact_dispatch_installed = True
