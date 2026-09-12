"""Show optional advice before authoring and in receipts, without rewriting memory."""
from __future__ import annotations

from typing import Any, Mapping

from .usage_guide import stored_memory_wording_advisory


AUTHORING_RESULT_TOOLS = frozenset({
    "remember_memory", "remember_emotional_memory", "remember_learning_memory",
    "remember_tool_guidance", "remember_planning_memory",
})


def _before_store_description(advisory: Any) -> str:
    """One directory snapshot; missing preferences never resurrect a default."""
    lines = ["存入前·可选提醒"]
    if isinstance(advisory, Mapping) and advisory.get("enabled") is False:
        lines.append("人称提醒已关闭。")
    elif (isinstance(advisory, Mapping) and advisory.get("enabled") is True
          and isinstance(advisory.get("message"), str) and advisory["message"].strip()):
        lines.append("人称提醒：" + advisory["message"])
    else:
        lines.append("人称提醒当前不可读取；可通过 stbrain_help 查看。")
    lines.extend([
        "选词建议：" + stored_memory_wording_advisory()["message"],
        "叙事人称和是否采用建议由 AI 自选；manage_person_reference_advisory 可修改、关闭或恢复人称提醒。",
        "可直接组织参数并存入，无需为提醒增加预览或确认调用。",
        "这里是工具目录的偏好快照；本轮管理结果优先，客户端刷新 MCP 工具目录后可看到新偏好。",
    ])
    return "\n".join(lines)


def install_person_reference_advisory_surface(mcp: Any, service: Any) -> None:
    """Run inside the normal authorization wrappers; never replace a write receipt."""
    manager = mcp._tool_manager
    original = manager.call_tool
    original_list = manager.list_tools

    def list_tools():
        listed = original_list()
        try:
            # One owner/model-scoped, read-only projection per directory. Do not
            # cache it process-wide or include changing history/timestamp fields.
            advisory = service.advisory_status()
        except Exception:
            advisory = None
        prefix = _before_store_description(advisory)
        return [
            tool.model_copy(update={"description": prefix + "\n\n" + tool.description})
            if tool.name in AUTHORING_RESULT_TOOLS else tool
            for tool in listed
        ]

    async def call_tool(name, arguments, context=None, convert_result=False):
        result = await original(name, arguments, context=context, convert_result=False)
        if (name in AUTHORING_RESULT_TOOLS and isinstance(result, dict)
                and result.get("decision") in {"stored", "stored_quarantined"}
                and result.get("stored") is not False):
            try:
                advisory = service.advisory_status()
            except Exception:
                # A successful memory write must not look like a failed write
                # just because its optional follow-up advice could not be read.
                advisory = {"available": False, "optional": True,
                            "read_tool": "stbrain_help"}
            result = {**result, "person_reference_advisory": advisory,
                      "wording_advisory": stored_memory_wording_advisory()}
            if "authoring_advisory" in result:
                # Legacy learning results (including old idempotent receipts)
                # may carry a baked-in host default. Project today's preference
                # in its existing metadata slot, never edit the saved receipt.
                result["authoring_advisory"] = advisory
        tool = manager.get_tool(name)
        return tool.fn_metadata.convert_result(result) if convert_result else result

    manager.call_tool = call_tool
    # FastMCP's already registered tools/list handler reads this manager on
    # every request. Copy descriptions only: keep original tools, exact schemas,
    # execution metadata, tool counts and every write/authorization path intact.
    manager.list_tools = list_tools
