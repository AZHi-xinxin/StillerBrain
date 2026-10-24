"""Small discovery-only contracts for the six ordinary daily entry points.

The registered tools, their validators and every authorization wrapper remain
unchanged.  In particular, ``changes`` is an author-supplied map here, not a
second copy of all four modules' storage schemas.  The normal backend still
validates its exact module fields; an exact on-demand contract remains available
through ``stbrain_tools``.  Ordinary calls never require reading that contract.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from mcp import types


COMPACT_ORDINARY_TOOL_NAMES = frozenset({
    "remember_memory", "remember_work_memory", "remember_tool_guidance", "revise_memory",
    "advance_plan", "stbrain_open",
})

_DESCRIPTIONS = {
    "remember_memory": (
        "日常存入：只填 content 即可，module 默认 auto，无需先打开、查目录或交审核表。"
        "正文原样保存；kind/emotion/importance/keywords 可省略、留空或 null，moment 等分类可兼容。"
        "来源/可信度未填为未标注，不猜正文或编标签；回执会给出实际模块与归一化提示。"
        "模块一首次激活前只读，激活后普通直连和网关均省略 write_context_ref；旧模式保留原授权。"
        "rewrite_receipt 可选，仅接受已确认且字段一致的情感/学习回执，不自动改写。"
        "desire可选，情感记忆上200字内的想做之事；memory_desires可增改归档，父记忆保留。"
        "emotions可选，填写自定义情绪名字/id；emotion_list与emotion_manage管理，存入回执给可用名称。"
        "仅真实 stored 回执表示保存成功，计划不授予外部操作权限。"
    ),
    "remember_work_memory": (
        "随手记：只填 content，原文保存在 ST 独立工作记录，tag 默认“随手记”。"
        "不自动混入长期回忆、网关注入或普通检索；整理/读取时按需用 "
        "stbrain_manage(action='recall_work_memory' 或 'revise_work_memory', arguments={...})。"
        "无需先查目录或打开；模块一首次激活前只读，激活后省略 write_context_ref，旧模式保留原授权。"
        "只有 stored 回执表示存入成功；不要写密钥。"
    ),
    "remember_tool_guidance": (
        "记录工具或 MCP 服务用途；tool_name 可用 MCP 级名字，purpose 是用途。"
        "reminder 为可选的一句话提醒（最多100字符），详细操作写 call_notes；场景可用中文。"
        "可信度由 AI 自填0–100；expires_at 可省略，无固定到期。"
        "风险与确认策略可省略或自主填写，不按写操作/关键词拦截存卡；"
        "省略时 medium/none 只是默认元数据，不增加卡片逐次确认要求；"
        "这是使用经验和提醒，不授予真实工具权限。"
        "模块一首次激活前只读，激活后省略 write_context_ref、expected_tool_row_version；旧模式沿用原授权。"
    ),
    "revise_memory": (
        "直接修改四个普通脑：target_ref 填已读到的完整引用，changes 只填要改的作者字段。"
        "无需审核表或预先打开；正文可改，旧版本保留，版本冲突须重读后决定。"
        "情感/规划正文用 original_text，学习用 current_understanding，工具卡用 purpose/call_notes。"
        "工具卡退役 changes={\"intent\":\"retire\"}；恢复用 intent=restore、target_version。"
        "工具卡 reminder/source_ref/expires_at 为 null 表示清除；省略保留。"
        "字段需对应目标模块，编号、owner、哈希及审计由系统维护。"
        "激活后的普通直连/网关省略 write_context_ref；旧模式沿用原授权。"
        "仅不清楚字段时可用 stbrain_tools(action='revise_memory') 查详细格式，不是必经步骤。"
    ),
    "advance_plan": (
        "追加一次计划进度，不执行计划。target_ref、expected_event_seq 使用已读计划的引用和 event_seq。"
        "progress/complete/reopen 需真实 evidence；pause/resume 可不填。"
        "证据包含 source_kind、source_ref、evidence_summary、provenance；不能编造。"
        "冲突先重读，不自动覆盖。激活后的普通直连/网关省略 write_context_ref；旧模式保留原授权。"
    ),
    "stbrain_open": (
        "ST 的首选读取入口，无必填参数；新对话需要接续共同经历、身份关系或旧事时，先从这里打开或查询。"
        "view=recall 统一查询四个普通脑；query 留空分页浏览，module 留空跨脑，"
        "保持 query/module 用 next_cursor 翻页。返回摘要、精确引用及原文/历史读取线索。"
        "此只读查询无需唤醒或密码，不含模块一、待审核或隔离内容。"
        "读原文或历史时，detail_lookup.tool 填入 stbrain_manage 的 action，"
        "detail_lookup.arguments 原样填入 arguments；不要把未列出的旧操作名直接当工具调用。"
        "默认 summary 打开本轮状态；manual+module 读说明；review 从 page=0 开始，"
        "按 next_arguments 读至 fully_presented=true，后页使用返回的 expected_material_hash。"
        "模块一沿用真实唤醒、完整审阅和激活规则；普通存入不必先打开。"
        "不常用功能可 stbrain_tools 按分类查找，再用 stbrain_manage 执行，无需切换客户端工具档位。"
    ),
}

_FIELD_DESCRIPTIONS = {
    "remember_memory": {
        "content": "唯一必填：1–2000字原文，原样保存，不自动截断或改写。",
        "module": "默认 auto（也可省略、null或空）：按明确字段线索选模块；可指定 emotional_memory / learning_memory / planning_memory。",
        "source_basis": "observed=亲历/观察；reported=转述；inferred=推断；unmarked=未标注。",
        "confidence": "AI 自填0–100；省略或 null 为未标注。",
        "keywords": "可省略、null或空；可给短语数组、JSON数组字符串或分隔短语，不自动编造标签。",
        "kind": "可省略、null或空；moment/event兼容shared_event；其他非标准分类回到模块默认并提示，不拒绝正文。",
        "emotion": "可省略、null或空，默认other；无法识别的感受保守归other并提示，不猜测情绪。",
        "desire": "可选欲望备注，情感记忆上最多200字；省略不影响存入，用memory_desires管理。",
        "emotions": "可选的自定义情绪名称/id数组；与正文同事务保存，省略不打标签。",
        "importance": "可省略、null或空，默认50；可填0–100整数或等值数字字符串/整数值小数，不接受布尔。",
        "rewrite_receipt": "已确认的一次性改写回执；仅情感/学习，字段须与确认结果一致。",
    },
    "remember_work_memory": {
        "content": "唯一必填：1–32768字符原文且UTF-8不超过128KiB；独立保存，不自动进入长期回忆。",
        "tag": "可选检索标签；省略用“随手记”，显式标签需1–160字符。",
    },
    "remember_tool_guidance": {
        "scenario_tags": "自然场景词句，如 [\"回家了\",\"到家了\"]；每项1–128字符，最多16项。",
        "tool_name": "工具或 MCP 服务的名称，可绑定服务级大名字。",
        "confidence": "由 AI 自填0–100；不代表独立核验或真实执行权限。",
        "expires_at": "可选带时区 ISO 时间；省略/null 无固定到期。",
        "source_type": "按真实来源选择，不因来源类别限制自填可信度。",
    },
    "revise_memory": {
        "target_ref": "已读到的完整版本引用，例如 emotion://emmem_<32位十六进制>@1；不要编造编号。",
    },
    "advance_plan": {
        "expected_event_seq": "已读计划的 event_seq；冲突时重读，不填写猜测的序号。",
    },
    "stbrain_open": {
        "cursor": "recall 分页使用返回的 next_cursor，保持原 query/module。",
        "page": "仅模块一 review 分页使用；recall 用 cursor。",
    },
}

_SCHEMA_MAP_KEYS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
_SCHEMA_KEYS = frozenset({
    "items", "contains", "additionalProperties", "unevaluatedProperties", "propertyNames",
    "not", "if", "then", "else", "additionalItems", "unevaluatedItems", "contentSchema",
})
_SCHEMA_LIST_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_PRESENTATION_KEYS = frozenset({"title", "description", "$comment", "examples"})
_ADVISORY_START = "存入前·可选提醒\n"
_ADVISORY_END = "这里是工具目录的偏好快照；本轮管理结果优先，客户端刷新 MCP 工具目录后可看到新偏好。"
_REVISION_PATTERNS = frozenset({"^emotion://", "^learning://", "^plan://", "^tool-card://"})


def _compact_schema(schema: Any) -> Any:
    """Remove prose annotations, never property names, defaults or constraints."""
    if not isinstance(schema, dict):
        return deepcopy(schema)
    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _PRESENTATION_KEYS:
            continue
        if key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
            result[key] = {name: _compact_schema(item) for name, item in value.items()}
        elif key in _SCHEMA_KEYS:
            result[key] = _compact_schema(value)
        elif key in _SCHEMA_LIST_KEYS and isinstance(value, list):
            result[key] = [_compact_schema(item) for item in value]
        else:
            # Literal default/const/enum objects and x-* metadata are not schemas.
            result[key] = deepcopy(value)
    return result


def _ordinary_revision_branch(branch: Any) -> bool:
    """Recognize only the four duplicated target-specific author-map branches."""
    if not isinstance(branch, dict) or set(branch) != {"if", "then"}:
        return False
    condition, consequence = branch["if"], branch["then"]
    if not isinstance(condition, dict) or not isinstance(consequence, dict):
        return False
    if set(condition) != {"properties", "required"} or condition["required"] != ["target_ref"]:
        return False
    properties = condition["properties"]
    if not isinstance(properties, dict) or set(properties) != {"target_ref"}:
        return False
    target = properties["target_ref"]
    if not isinstance(target, dict) or set(target) != {"pattern"} or target["pattern"] not in _REVISION_PATTERNS:
        return False
    return (set(consequence) == {"properties"}
            and isinstance(consequence["properties"], dict)
            and set(consequence["properties"]) == {"changes"})


def _description(tool: types.Tool) -> str:
    original = tool.description or ""
    short = _DESCRIPTIONS[tool.name]
    if tool.name not in {"remember_memory", "remember_tool_guidance"} or not original.startswith(_ADVISORY_START):
        return short
    # Authored text can contain blank lines, the sentinel itself or any prose.
    # The surface appends the actual closing sentinel last; never split at the
    # first blank line or arbitrarily truncate the author's reminder.
    end = original.rfind(_ADVISORY_END + "\n\n")
    if end < 0:
        return original  # Unknown surface revision: preserve rather than lose advice.
    prefix = original[:end + len(_ADVISORY_END)]
    return prefix + "\n\n" + short


def project_compact_tool(tool: types.Tool) -> types.Tool:
    """Return a detached compact discovery Tool; never edit runtime registration."""
    if tool.name not in COMPACT_ORDINARY_TOOL_NAMES:
        return tool
    original = tool.inputSchema
    schema = _compact_schema(original)
    properties = schema.get("properties", {})
    for name, description in _FIELD_DESCRIPTIONS.get(tool.name, {}).items():
        if name in properties:
            properties[name]["description"] = description
    if "execution_ref" in properties:
        # Keep this reserved host-binding contract byte-for-byte, including
        # x-stbrain-execution-tool/contract, for gateway canonicalization.
        properties["execution_ref"] = deepcopy(original["properties"]["execution_ref"])
    if tool.name == "revise_memory" and "changes" in properties:
        properties["changes"] = {
            "type": "object", "minProperties": 1, "additionalProperties": True,
            "description": (
                "只填要改的作者字段，省略保留；如 {\"summary\":\"新摘要\",\"keywords\":[\"学习\",\"经验\"]}。"
                "情感/规划正文 original_text；学习 current_understanding；工具 purpose/reminder/call_notes。"
                "字段和范围由目标模块校验；系统编号、哈希、审计不可自填。"
            ),
        }
        branches = [branch for branch in original.get("allOf", []) if not _ordinary_revision_branch(branch)]
        if branches:
            schema["allOf"] = [_compact_schema(branch) for branch in branches]
        else:
            schema.pop("allOf", None)
    return tool.model_copy(deep=True, update={"inputSchema": schema, "description": _description(tool)})
