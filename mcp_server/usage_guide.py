"""Static, credential-free usage help; reading help never opens a write context."""
from __future__ import annotations
from typing import Any

from .public_contract import BRAIN_MANUAL_MODULES


def stored_memory_wording_advisory() -> dict[str, Any]:
    """A short optional post-save tip; no generated tags or additional writes."""
    return {
        "optional": True,
        "stage": "after_store",
        "message": (
            "我可以按记忆内容选原词、近义表达、语义相关话题和情感语境作为检索线索；"
            "场景标签优先考虑人类自然会说的话。比如 Tasker／自动化／不会用了／修好了。"
            "是否补充或调整，由我决定。"
        ),
        "fields": "keywords 是内容线索；场景标签使用对应工具已公布的 scene_tags 或 scenario_tags。",
        "read_tool": "stbrain_help",
        "memory_content_changed": False,
    }


def _recall_wording_guide() -> dict[str, Any]:
    """Authoring suggestions, not generated tags or a recall guarantee."""
    return {
        "principle": "选词可考虑原词＋近义表达＋语义相关话题＋情感语境关联，由 AI 按真实内容主动填写和增改。",
        "wording": [
            "奶奶家 / 奶奶 / 家里的近况 / 家人牵挂",
            "小王 / 同事 / 上班 / 零食；情感语境：生气或委屈",
            "下雨 / 雨天 / 带伞；情感语境：想念",
            "Tasker / 自动化 / 配置 / 不会用了 / 修好了",
        ],
        "fields": "keywords 是内容检索线索；场景线索使用对应工具实际公布的 scene_tags 或 scenario_tags，其中治理轻提醒普通标签匹配本轮人类自然话语。remember_memory 用 keywords，场景字段在支持它的专用工具填写。情感分类另按当前工具支持的 emotion 等字段填写；上述中文情绪表达也可写进正文或摘要，不代表所有模块都支持自由中文情绪枚举。",
        "effect": "这些是选词映射，不是固定回复或系统自动扩写；当前实现不保证自动补全同义词或联想关系。命中只是候选，实际还取决于已有记录、各模块模式、预算和授权。",
    }


def module_usage_guide(module: str, *, simple: bool) -> dict[str, Any]:
    """Pure documentation only: no store, status lookup, open or review receipt.

    The selected profile is supplied by the server, never by the caller. Actual
    availability comes from health/tool receipts, not a static documentation page.
    """
    if module not in BRAIN_MANUAL_MODULES:
        raise ValueError("unknown_manual_module")
    modules = {
        "self_revision": {
            "title": "模块一 · 自我定义",
            "purpose": "由 AI 编写、复核和决定是否激活自己的自我定义。",
            "facets": {
                "names": "名称由作者自定：1–80位英文字母或数字开头，可含._-；正文可以写中文。最多16项，每项最多2500字符，完整自我正文合计最多16000字符。",
                "automatic": "默认网关在模块一生效且自我注入开启后，按当前用户文字与侧面的名称、原文做本地词句匹配，相关侧面原样进入动态上下文；无需增加scene等字段。",
                "budget": "自动选择使用现有动态预算的余量，最多3项；没有相关线索或完整侧面放不下时本轮省略，已保存内容保持。它是轻量词句匹配，不是通用语义理解。",
                "host_override": "自制宿主在context/prepare省略facet_names使用自动选择；显式[]本轮不选侧面，显式名称列表按原key精确选择。",
                "read": "query_self_model(view='edit_basis')可读活动版完整正文；自动注入只包含当轮选中的侧面。",
            },
            "read": [
                {"tool": "query_self_model", "arguments": {"view": "status"}, "use": "查看当前阶段。"},
                {"tool": "query_self_model", "arguments": {"view": "active"}, "use": "读取活动自我定义。"},
                {"tool": "query_self_model", "arguments": {"view": "edit_basis"}, "use": "读取活动版完整编辑依据。"},
            ],
            "write_tools": ["submit_self_model_candidate", "activate_self_model_candidate"],
            "workflow": [
                "是否修改由 AI 决定；需要修改时按相应连接的授权入口获取真实上下文。",
                "按返回的当前阶段、版本和 action contract 提交候选。",
                "在独立的后续上下文中读取完整待审材料，明确复核，再按后续阶段激活。",
            ],
            "review": "本页是工具说明。候选正文、分页及完整呈现证明由相应授权的 open/review 流程提供；本页阅读不计入复核。",
        },
        "emotional_memory": {
            "title": "情感与经历记忆",
            "purpose": "保存经历原文、情感理解和情境，并保留修订历史。",
            "read": [{"tool": "recall_emotional_memory", "use": "query 搜索；使用返回的 memory_id 查询原文和版本。"}],
            "write_tools": ["remember_memory", "revise_memory", "remember_emotional_memory",
                            "revise_emotional_memory", "integrate_emotional_memories", "manage_brain_pin", "veto_ephemeral_memory"],
            "example": {"tool": "remember_memory", "arguments": {"module": "emotional_memory", "content": "一次值得记住的交流。", "keywords": ["交流", "感受"]}},
            "workflow": ["普通新增可用 remember_memory。来源与可信度可省略，分别保存为 unmarked 和 null，表示未标注；填写的来源和0–100可信度保留作者选择。", "普通修订使用 revise_memory，附刚查询到的 target_ref 和 changes；原文用 changes.original_text，类型用 changes.memory_type，来源时间用 changes.source_timestamp，修改后保留旧版本。", "revise_emotional_memory 接收其工具顶层公布的摘要、情绪、来源等字段；原文修改走上述 revise_memory。revise_memory 的 changes.confidence=null 表示清除评分，省略字段保留原值。", "整合、固定条目或否决临时记忆时，使用对应工具 schema 和真实目标。"],
        },
        "learning_memory": {
            "title": "学习与方法记忆",
            "purpose": "保存知识、方法、当前理解与不确定性，随新证据修订。",
            "read": [{"tool": "recall_learning_memory", "use": "query 搜索；view='inventory' 浏览目录，再按回执提供的引用读原文。"},
                     {"tool": "preview_learning_recall", "use": "预览检索结果。"}],
            "write_tools": ["remember_memory", "revise_memory", "remember_learning_memory", "remember_learning_contrast_pair",
                            "revise_learning_memory", "integrate_learning_memories", "review_learning_change"],
            "example": {"tool": "remember_memory", "arguments": {"module": "learning_memory", "content": "这里填写已学到的方法及依据。", "keywords": ["方法", "学习"]}},
            "workflow": ["普通新增可用 remember_memory。来源与可信度可省略，分别保存为 unmarked 和 null，表示未标注；填写的来源和0–100可信度保留作者选择。", "使用 revise_memory 修改 current_understanding 等作者字段，旧版本保留；changes.confidence=null 表示清除评分，省略字段保留原值。", "旧候选的接受或拒绝仍使用已读取的准确版本、hash 和明确决定。"],
        },
        "tool_guidance": {
            "title": "工具提醒与经验",
            "purpose": "保存工具用途、适用场景、步骤和使用经验。",
            "read_notes": [
                "call_notes_available 表示原文可手动读取；call_notes_current 是当前目录/参数结构、有效期、活动状态及最近失败的综合诊断，不是保存成功或最新版本标记。false 不删除原文，也不禁止读回。",
                "直接入口参数平铺；经 stbrain_manage 调用时才包在 arguments 内。读取经验不执行工具，也不会把全文自动注入。",
            ],
            "read": [{"tool": "recall_tool_guidance", "use": "空 query 或 view='directory' 查目录；card_id + view='card' 读原文，view='experiences' 读全部结果的经验（包括成功），view='failures' 仅读非成功项。经验可用 query 填编号或正文片段缩小范围，total/truncated 提示未列完。"}],
            "write_tools": ["remember_tool_guidance", "revise_memory", "record_tool_experience", "review_tool_guidance_candidate"],
            "example": {"tool": "remember_tool_guidance", "arguments": {"tool_name": "示例工具", "purpose": "整理资料时查询相关记录。"}},
            "workflow": ["tool_name 可写工具或 MCP 服务名，purpose 写用途，reminder 可写最多100字的一句场景提醒。", "四个普通脑统一用 revise_memory 修改：target_ref 使用查到的 tool-card://toolcard_…@版本，changes 只填要改的 reminder、purpose、scenario_tags、confidence 等。", "reminder/source_ref/expires_at 填 null 可清除，省略保留；clear_fields 兼容旧写法。普通修改无需 edit_class 或审查表。退役用 changes.intent='retire'；恢复用 changes.intent='restore' 和 changes.target_version，均放在 changes 内。", "退役示例：revise_memory(target_ref=已读引用, changes={'intent':'retire'})；恢复示例：revise_memory(target_ref=已读当前引用, changes={'intent':'restore','target_version':已读历史版本号})。工具卡用 intent 管理退役与恢复，不使用 changes.lifecycle。", "详细步骤按需读取；旧候选仍按查询回执处理。保存提醒是记忆操作，实际执行工具使用当前可用目录和授权。"],
        },
        "planning_memory": {
            "title": "计划与进度",
            "purpose": "保存 AI 采纳的计划，并记录有依据的推进事件。",
            "read": [{"tool": "recall_planning_memory", "use": "query 搜索；按返回的 plan_ref 读取计划、版本和 event_seq。"}],
            "write_tools": ["remember_memory", "revise_memory", "advance_plan", "remember_planning_memory",
                            "record_planning_event", "revise_planning_memory", "review_planning_change"],
            "example": {"tool": "remember_memory", "arguments": {"module": "planning_memory", "content": "下一次检查实验结果。", "keywords": ["实验"]}},
            "workflow": ["普通新增可用 remember_memory。", "正文修订使用 revise_memory 和刚读到的版本引用。", "进度用 advance_plan：target_ref、expected_event_seq、event_type、note；progress/complete/reopen 附实际 evidence。", "record_planning_event 使用自己的 schema；保存计划与执行外部操作分别授权。"],
        },
        "self_governance_profile": {
            "title": "给自己写轻提醒、提示词与自定义安全阀",
            "purpose": "AI 可以在这里自行编写、修改、清空或回滚给自己的话，并选择何时浮现；正文默认空白，内容和是否使用都由 AI 决定。self_governance_profile 是这个入口的工具名称。",
            "read": [{"tool": "query_self_governance_profile", "arguments": {"view": "status", "include_content": True}, "use": "查看各范围当前正文和版本；可加 scope 只看一处。"},
                     {"tool": "query_self_governance_profile", "arguments": {"view": "manual"}, "use": "读取空白结构、设置方式和浮现机制。"}],
            "write_tools": ["manage_self_governance_profile"],
            "workflow": ["先查看要修改的 scope；set 写入或替换自己的 text，clear 清空当前内容，rollback 回到自己选定的历史版本。旧版本保留。", "manual_only 表示自己主动查阅；scene_relevant 配合自己写的 scene_tags，在匹配的本轮场景按容量浮现原文。中文标签可用。", "global 是跨模块范围，并非每轮常驻；它仍遵循自己选择的 trigger_mode。规划场景的通用提醒可放 global 并选择计划相关的场景标签，单条计划的短提醒另用该计划 reminder。", "模块一的冷静词和自我修订安全提示也由 AI 自写，使用模块一独立流程；这里的普通轻提醒不替代该流程。"],
            "scopes": {"global": "跨模块", "self_revision": "自我修订", "emotional_memory": "情感记忆", "learning_memory": "学习记忆", "tool_use": "工具使用"},
            "authorship": "机制说明只介绍能力，不提供应当照抄的提醒正文。AI 可留空、修改、撤回；自写内容不改变外部工具或账户权限。",
            "scene_matching": "治理轻提醒的普通 scene_tags 用来匹配本轮人类说的话：优先写人类聊天中自然会说的词句，如“设个闹钟”“提醒我”“回家了”“还记得”，AI 可按对方实际表达自行增改，并非固定清单。只写“动手之前”“想用工具”等 AI 内部动作描述时，人的话里若没有相同词句就不会命中。当前按标签完整短语是否包含在宿主提供的本轮场景 query 中匹配（casefold，不区分英文大小写），不会自动推断同义词；本项目网关取最新一条人类消息的文本。匹配后仍要满足 scene_relevant 模式、自动注入开关及预算，修改由下一次生成前的新快照使用；一次未浮现不等于标签写错。专用系统事件标记 $st.learning_episode_boundary 需要真实运行时事件，人工消息写出该标记不算事件；此例外不影响普通中文场景词。",
        },
        "injection_control": {
            "title": "注入控制",
            "purpose": "管理记忆提供给模型时的范围和配置。",
            "read": [{"tool": "query_injection_control", "use": "按工具 schema 查询控制状态、scope 和版本。"}],
            "write_tools": ["manage_injection_control"],
            "workflow": ["先查询相关 scope，再按 schema 提交明确的 action。", "涉及关闭、恢复等高级控制时遵循该 action 的授权回执；静态帮助不产生控制权限。"],
        },
        "hallucination_vault": {
            "title": "幻觉黑匣子",
            "purpose": "隔离待辨别记录，并保留独立的转移、核查与恢复流程。",
            "read": [{"tool": "open_hallucination_vault", "use": "按当前 schema 的读取选项查询，实际可读范围由独立隔离策略决定。"}],
            "write_tools": ["hold_hallucination_record", "transfer_hallucination_record", "review_hallucination_restore"],
            "workflow": ["使用独立 vault 权限和精确记录引用。", "转移或恢复按工具回执提供的候选、版本、hash 与确认流程进行。"],
        },
        "shared_person_authoring": {
            "title": "人称与指代修订",
            "purpose": "查看当前人称弱提醒，自行修改/关闭/恢复；也可预览并明确确认本次草稿的指代调整。",
            "read": [{"tool": "stbrain_help", "arguments": {"module": "shared_person_authoring"}, "use": "读取当前生效的人称弱提醒；关闭时不返回提醒正文。"}],
            "write_tools": ["manage_person_reference_advisory", "preview_person_reference_rewrite", "confirm_person_reference_rewrite"],
            "workflow": ["manage_person_reference_advisory：set + text 写自己的提醒并开启，disable 关闭正文显示，reset 恢复默认建议；每次修改保留历史，普通激活后的 simple-memory-v1 无需填版本。", "人称由作者选择；弱提醒开关与本条草稿改写彼此独立，关闭提醒不改变既有记忆。", "按 schema 提供当前待写草稿 draft_fields、draft_version、referent_bindings 和明确的 rewrite_targets。", "preview 会保存预览回执，属于写操作；首次激活前的只读模式会阻止它。", "读取预览后，由 AI 按实际结果明确确认；草稿版本、预览 hash 和作者确认由真实回执关联。"],
            "rewrite_scope": "现有机制为预览→确认→带一次性 rewrite_receipt 存入，是作者指定的字面指代替换，不是自动转换整段叙事人称；每次草稿默认不启用。适用于情感、学习、工具，规划暂未接入。",
            "rewrite_inputs": "人物/别名引用与范围由作者或接入方提供一致的声明；当前没有可查询的宿主人物认证或别名注册表。请使用确知的人物对应关系，不能把自填编号当作身份验证。未确定人物时可保留原文，普通记忆不要求填写这些字段。",
            "field_format": "draft_fields、final_fields 的键使用带 / 的完整字段路径；referent_bindings、rewrite_targets 内的 field_path 使用同一路径。情感片段示例：{\"/original_text\":\"我和她完成了练习。\",\"/summary\":\"共同练习。\"}。示例仅展示格式，人物对应和其他参数按实际草稿填写。final_fields 使用本次预览 suggested_fields 的带 / 字段名与确认文本，hash 按完整对象计算。",
            "unified_writer": "情感和学习的确认回执可交给 remember_memory；情感 /original_text 对应 content，学习 /current_understanding 对应 content，/summary、/title 对应 summary、title。final_fields 必须与实际存入的文本完全对应。学习在该入口的 /preceding_context_summary 固定为空字符串，其他取值请走专用工具。工具卡用 remember_tool_guidance，按适用字段使用 /purpose、/call_notes 等路径。",
        },
    }
    entry = modules[module]
    if module in {"emotional_memory", "learning_memory", "tool_guidance", "planning_memory"}:
        entry["wording_guide"] = _recall_wording_guide()
        entry["person_reference_help"] = "存入前，工具说明提供当前人称提醒与标签选词建议，AI 组织参数时即可参考；采用与否由 AI 自选。工具目录是快照，本轮最新管理结果优先，客户端刷新 MCP 目录可更新说明。stbrain_help 与成功存入回执也返回当前人称提醒；AI 可用 manage_person_reference_advisory 修改、关闭或恢复。普通存入仍一次调用，不自动改写正文。"
    if module in {"emotional_memory", "learning_memory", "planning_memory", "tool_guidance"}:
        entry["read"].insert(0, {
            "tool": "stbrain_open", "arguments": {"view": "recall", "module": module},
            "use": "完整分页目录；可加query搜索。按next_cursor续页，detail_lookup读取原文。省略module跨四普通脑查询。",
        })
    if module == "tool_guidance":
        entry["confidence"] = "工具卡与 record_tool_experience 的 confidence 均由作者填写0–100整数；经验仍为ai_reported、verified=false，实际执行权限独立核对。"
    if not simple and module == "tool_guidance":
        # Legacy dedicated writes need actual context/version receipts. Never
        # manufacture those values merely to make a static example executable.
        entry.pop("example")
        entry["input_fields"] = "tool_name、purpose 写用途；write_context_ref 和 expected_tool_row_version 使用授权上下文的真实回执。"
        entry["write_tools"].append("revise_tool_guidance")
    if module == "tool_guidance":
        entry["compatibility"] = "revise_tool_guidance 内部保留旧调用兼容；simple-memory-v1 常用目录收起该重复入口，新操作统一使用 revise_memory。网关只接受本轮实际工具目录中的名称；旧历史中的工具名不代表本轮可用。"
    if module in {"emotional_memory", "learning_memory", "planning_memory", "tool_guidance"}:
        from .daily_revision_service import ORDINARY_REVISION_FIELDS
        entry["revise_author_fields"] = sorted(ORDINARY_REVISION_FIELDS[module])
        entry["revision_rule"] = "target_ref 使用已读的带版本引用，changes 只填决定修改的作者字段；冲突时重新读取后判断。"
    if module == "self_revision":
        authorization = (
            "simple-memory-v1：服务端验证的网关注入调用免部署密码，通过 stbrain_open 获取本轮上下文；"
            "官端及其他直连修改先 authorize_self_model(password)，用返回的一次性 grant_ref 调 stbrain_open_direct。"
            if simple else
            "legacy：网关使用验证过的执行绑定与真实注入；直连使用服务端独立授权的一次性 grant 调 stbrain_open_direct。"
        )
    elif module == "hallucination_vault":
        authorization = "本模块保留独立的隔离和授权流程，普通模块的便捷写入权限不替代 vault 权限。"
    elif simple and module == "self_governance_profile":
        authorization = "首次模块一完成并激活前只读，激活后 set/clear 直接修改自己的提醒；内部绑定、机械版本和当前生效记录由 ST 配对处理，无需手填数字。rollback 选择历史查询返回的目标引用。高级 action 保留各自授权。"
    elif simple:
        authorization = "首次模块一完成并激活前只读，激活后开放普通作者写改。普通支持操作省略 write_context_ref 和机械模块行版本，作者目标版本与明确确认仍须提供；治理/注入的高级 action 保留各自授权。"
    else:
        authorization = "legacy 的写入使用有效网关注入绑定或相应 scope 的授权 direct 上下文；具体内部字段以工具 schema 与真实回执为准。"
    return {
        "contract_version": "static-module-help/1",
        "access_profile": "simple-memory-v1" if simple else "legacy",
        "module": module, "manual_scope": "static_instructions", "state_changed": False,
        "write_context_created": False, "review_evidence_created": False,
        "authorization": authorization,
        "status_source": "实际状态查询 stbrain_health；self_revision 也可用 query_self_model(view='status')。本页不查询或声明当前已激活状态。",
        "transport": "官端直连省略宿主保留的 execution_ref；网关由宿主填入有效本轮引用。实际工具名称和字段以最新 MCP 工具目录为准。",
        **entry,
    }


def simple_usage_guide() -> dict[str, Any]:
    """Test profile help matches its actual access policy, not the legacy gate."""
    return {
        "contract_version": "simple-memory/1", "access_profile": "simple-memory-v1",
        "state_changed": False,
        "module_help": {"tool": "stbrain_help", "parameter": "module", "modules": list(BRAIN_MANUAL_MODULES),
                        "instruction": "传 module 单独读取该模块的静态使用说明；查询实际状态用 stbrain_health。"},
        "ordinary_access": "模块一首次完成设置并激活前，其他普通模块只读，可查询已有内容。激活后官端 MCP 与网关均可直接存、查、改普通记忆；服务负责内部绑定。",
        "remember": {
            "tool": "remember_memory", "required": ["module", "content"],
            "modules": ["emotional_memory", "learning_memory", "planning_memory"],
            "optional": "标题、摘要、关键词、重要度等由 AI 按需填写；来源与可信度省略时分别保存 unmarked 和 null，显示未标注。情感 kind 省略时为 unclassified（未分类），分类由 AI 自选。",
            "keywords_example": ["学习", "方法"],
            "before_store": "先看存入工具说明中的人称与标签提醒，再组织参数；AI 自选，普通存入仍一次调用。人称自写/关闭以最新管理结果为准，客户端缓存工具说明时刷新 MCP 目录。",
            "keywords_format": "keywords 优先填 JSON 字符串数组，如 [\"学习\",\"方法\"]；直连 MCP 接收端兼容单个短语、逗号/顿号/分号/换行分隔文本及合法 JSON 数组字符串。网关调用请使用真实数组。含分隔标点的完整关键词放在真实数组内，数组元素按原样处理。",
        },
        "wording_guide": _recall_wording_guide(),
        "read": {
            "emotional_memory": "recall_emotional_memory：query 搜索，memory_id 查版本与原文。",
            "learning_memory": "recall_learning_memory：query 搜索，view='inventory' 查目录，再按查询结果中的引用读原文。",
            "planning_memory": "recall_planning_memory：query 搜索或按 plan_ref 读取计划及事件。",
            "tool_guidance": "recall_tool_guidance：directory 查目录；card_id + card/history/experiences 分别查原文、版本、全部结果的经验；failures 只查非成功项。",
            "principle": "搜索结果先给可选条目，再按需要读原文；零命中可以换关键词或查目录。",
        },
        "revise": {
            "tool": "revise_memory", "required": ["target_ref", "changes"],
            "instruction": "四个普通脑统一修改：使用刚读到的版本引用，只填要改的作者字段。工具卡用 tool-card:// 引用；reminder、source_ref、expires_at 填 null 清除。工具卡退役用 changes.intent='retire'；恢复用 changes.intent='restore' 和 changes.target_version，均放在 changes 内。原文、摘要、重要度、关键词及各模块作者字段均可修改，旧版本保留。",
            "conflict": "版本冲突时重新读取；当前内容由 AI 判断后再提交。",
            "technical_fields": "ID、owner、版本、hash 和由来源推导的状态由 ST 维护。",
        },
        "tool_reminder": {
            "tool": "remember_tool_guidance", "required": ["tool_name", "purpose"],
            "reminder": "可填写最多100字的一句场景提醒，tool_name 可以是 MCP 服务名称。",
            "details": "具体操作名、使用步骤和经验放在可选详细字段中，按需查询。实际执行使用当前工具目录及权限。",
        },
        "self_reminders": {
            "title": "给自己写轻提醒与自定义安全阀",
            "purpose": "AI 可以自行编写、修改和撤回给自己的提示词；默认空白，是否填写与正文内容由 AI 决定。",
            "help": {"tool": "stbrain_help", "arguments": {"module": "self_governance_profile"}},
            "read_entry": {"tool": "query_self_governance_profile", "arguments": {"view": "status", "include_content": True}},
            "write": "manage_self_governance_profile：set 写入或替换，clear 清空，rollback 回到历史版本。普通模式在模块一首次激活后直接保存。",
            "appearance": "manual_only 主动查阅；scene_relevant 按自己写的中文或英文 scene_tags 匹配浮现。global 也遵循此选择，并非每轮常驻。",
            "scene_tags": "仅就治理轻提醒而言，普通标签优先写人类自然会说的词句，如“设个闹钟”“提醒我”“回家了”“还记得”，按实际表达自行增改。它匹配本轮人类话语，不读取 AI 的内部动作；完整词句命中后仍受模式、开关和预算影响。系统事件标记另按真实事件触发，详见模块帮助。",
            "planning": "规划场景的通用提示可用 global + scene_relevant；单条工具卡或计划另有自己的 reminder。",
            "module_one": "模块一冷静词与自我修订提示同样由 AI 自写，使用模块一独立流程；本入口不改动该流程或外部权限。",
        },
        "plan_progress": {
            "tool": "advance_plan", "required": ["target_ref", "expected_event_seq", "event_type", "note"],
            "instruction": "expected_event_seq 取自刚查询的 event_seq；progress/complete/reopen 附真实 evidence。record_planning_event 是另一高级接口，应使用它自己 schema 中的字段。",
        },
        "self_revision": {
            "authorization": "已通过服务端执行绑定验证的网关注入调用，写入或修改模块一免部署密码。官方 DS 或其他非网关注入的直连模型，先用 authorize_self_model(password) 取得15分钟修改授权，再用返回的一次性 grant_ref 调用 stbrain_open_direct。",
            "identity": "网关身份以服务端验证的本次执行绑定为准；模型名称及 local-test-model 等内部命名空间仅用于标识。",
            "choice": "部署者自行决定是否为直连模型提供密码授权。阅读自我定义使用 query_self_model，免密码。",
            "workflow": "网关和直连均使用现有候选、审核与激活工具，按说明读取材料；提交、独立审核和激活分别保留真实唤醒边界。",
        },
        "module_status": "read_only 表示可读取已有内容，新增和修改须待模块一首次完成并激活；available 表示已开放普通读写。已有活动自我定义时，新候选的待审核状态不重复关闭普通记忆。stbrain_help(module=...) 提供分模块静态说明；stbrain_open 的编辑上下文和待审材料仍使用相应连接绑定。实际保存和修改以工具回执为准。",
        "transport": "普通操作省略 write_context_ref；官端直连省略宿主保留的 execution_ref，gateway 的 execution_ref 由宿主提供。显式无效的网关绑定会被拒绝。",
        "results": "以 stored/revised 等真实回执为准；失败回执按 reason 和当前工具 schema 调整参数。",
        "vault": "幻觉黑匣子继续使用独立隔离与恢复流程。",
    }


def usage_guide() -> dict[str, Any]:
    return {
        "contract_version": "daily-memory/1", "state_changed": False,
        "module_help": {"tool": "stbrain_help", "parameter": "module", "modules": [
                            "self_revision", "emotional_memory", "learning_memory", "tool_guidance",
                            "planning_memory", "self_governance_profile", "injection_control",
                            "hallucination_vault", "shared_person_authoring"],
                        },
        "daily_memory": {
            "tool": "remember_memory", "required": ["module", "content"],
            "modules": ["emotional_memory", "learning_memory", "planning_memory"],
            "instruction": "普通新增仅需 module 和真实 content，一次调用；不用先 open、手填版本或另轮审核。",
            "result": "真实 stored=保存；回执不完整先核查，不自动重试。",
            "uncertainty": "来源/可信度默认unmarked/null（未标注），不代表已独立核实；情感kind默认unclassified（未分类）。",
            "plan_effect": "计划active，不授权或自动执行外部操作。",
        },
        "wording_guide": {
            "principle": "原词＋近义表达＋语义相关话题＋情感语境关联。",
            "wording": [
                "奶奶家 / 奶奶 / 家里的近况 / 家人牵挂",
                "小王 / 同事 / 上班 / 零食；情感语境：生气或委屈",
                "下雨 / 雨天 / 带伞；情感语境：想念",
                "Tasker / 自动化 / 配置 / 不会用了 / 修好了",
            ],
            "fields": "keywords内容检索线索；scene_tags/scenario_tags场景；emotion按当前工具schema，中文可入正文或摘要。",
            "effect": "不是固定回复或系统自动扩写；命中只是候选，受已有记录、模式、预算和授权约束。",
        },
        "read": "StillerBrain（ST）：stbrain_open(view='recall', query=查询词)查四普通脑；空query目录，module选脑，next_cursor续页，detail_lookup详情。各recall_*、学习inventory、query_self_model保留。沿用身份和披露边界；隔离/候选另读。零命中不等于库为空；partial/errors=未完整搜索。",
        "ordinary_revision": {
            "tool": "revise_memory", "required": ["target_ref", "changes"],
            "instruction": "四脑统一用已读的精确版本引用改作者字段，保留历史；模块行版本由宿主处理，冲突读回，不自动换最新版。",
            "tool_guidance_help": "字段见stbrain_help(module='tool_guidance')。",
            "allowed_fields": {
                "emotional_memory": ["original_text", "memory_type", "source_timestamp", "summary", "primary_emotion",
                    "secondary_emotions", "importance", "sensitivity", "context_policy", "origin", "confidence",
                    "keywords", "entities", "referent_bindings", "recall_mode", "allow_contexts", "deny_contexts",
                    "default_decision", "explicit_request_override", "disclosure", "lifecycle"],
                "learning_memory": ["kind", "title", "summary", "current_understanding", "steps", "application_contexts",
                    "scene_tags", "preceding_context_summary", "uncertainties", "domain", "keywords", "entities",
                    "source_basis", "claim_review", "confidence", "time_sensitivity", "valid_as_of", "review_after",
                    "importance", "sensitivity", "context_policy", "recall_mode", "allow_contexts", "deny_contexts",
                    "default_decision", "explicit_request_override", "disclosure", "lifecycle", "referent_bindings"],
                "planning_memory": ["kind", "track", "title", "original_text", "summary", "reminder", "importance", "presence_mode",
                    "scene_tags", "keywords", "start_at", "due_at", "timezone", "review_after", "allow_coordination_hint",
                    "parent_ref", "dependency_refs", "ai_adoption_statement"],
            },
        },
        "plan_progress": {
            "tool": "advance_plan", "required": ["target_ref", "expected_event_seq", "event_type", "note"],
            "instruction": "使用查询返回的目标版本与event_seq；完成需真实证据，不手填模块行版本。",
        },
        "self_reminders": {
            "title": "给自己写轻提醒与自定义安全阀",
            "purpose": "AI自写、默认空白。",
            "help": {"tool": "stbrain_help", "arguments": {"module": "self_governance_profile"}},
            "read_entry": {"tool": "query_self_governance_profile", "arguments": {"view": "status", "include_content": True}},
            "write": "manage_self_governance_profile：set/clear/rollback保留历史、沿用授权；模块一另走独立流程。",
            "appearance": "manual_only主动查阅；scene_relevant匹配scene_tags；global并非每轮常驻。",
            "scene_tags": "治理标签用人类自然词句：设个闹钟/提醒我/回家了/还记得，AI自行增改。匹配仍受模式/开关/预算影响；系统事件标记须真实事件。",
        },
        "advanced": "summary/manual/review供编辑/复核；view='recall'只读查询入口。view='manual'+module说明；view='review'分页page=0，续页expected_material_hash；全部页齐才形成展示证明。核心提交、后来复核、再后来激活的真实唤醒流程；普通记忆专用修订和整合直接保存；旧候选按已读hash处理。",
        "transport": "使用 ST 无需 Shell 或 workspace，无需从文件提取工具结果。execution_ref由网关提供；直连需人类独立授权direct上下文，有效 scope 内用remember_memory。",
        "privacy": "不存令牌/密码；授权不回显；帮助不含私人记忆。",
    }
