# 工具目录与常用调用

[首页](../README.md) · [日常指南](GUIDE.md) · [MCP 详细说明](../mcp_server/OPEN_RESPONSE.md)

本工作副本提供 **日常 8 入口**与**完整模型目录 57 项**两种呈现，包含 4 个图片操作和 3 个情绪标签操作。完整传输 `tools/list` 另含 1 项 host-only 图片运输能力，共 58 项；宿主须过滤运输项，不发给模型。`/mcp?tool_profile=daily` 显式选择日常档；默认 `/mcp` 或 `tool_profile=full` 保留完整目录。`simple-memory-v1` 和 `legacy` 是独立的访问配置：它们的完整目录数量相同，集合不同；前者提供 `authorize_self_model`，并收起专用 `revise_tool_guidance`，后者的旧调用仍保留校验与兼容。`public-tools/21` 是协议标签，不是工具数量。

下面以 `simple-memory-v1` 为主。模块一首次完成并激活后，普通读写无需每次先 `open`，由服务补入内部上下文和模块行版本；AI 使用查询返回的真实目标引用。`legacy` 仍需真实授权上下文。黑匣子、模块一以及显式旧候选操作保留各自的授权和复核路径。

## 日常八入口与分类工具箱

| 入口 | 用途 |
| --- | --- |
| `stbrain_open` | 首选读取入口，无必填参数；`recall` 搜索，`manual` 按模块展开说明 |
| `remember_memory` | 只填 `content` 即可保存；可选分类指明经历、知识或计划 |
| `remember_work_memory` | 只填 `content` 暂存碎片或工作资料；默认标签“随手记”，仅显式读取 |
| `remember_tool_guidance` | 保存工具或 MCP 服务用途 |
| `revise_memory` | 修改四个普通模块的作者字段 |
| `advance_plan` | 记录计划进展 |
| `stbrain_tools` | 无参数列分类，`category` 列操作，`action` 读准确参数 |
| `stbrain_manage` | 用操作名和对应参数执行选定的 ST 功能 |

当前分类工具箱包含常用能力、工作记忆、记忆关联、图片附件及兼容的 `revise_tool_guidance`；不是在完整目录之外再增加一套权限。分类包括 `system`、`memory`、`relations`、`images`、`self`、`person`、`work`、`emotion`、`learning`、`tools`、`planning`、`diy`、`vault`。以当前分类回执为准。

例如，读取浮现开关可按需要查参数，再执行：

```json
{"name":"stbrain_tools","arguments":{"action":"query_injection_control"}}
```

```json
{"name":"stbrain_manage","arguments":{"action":"query_injection_control","arguments":{}}}
```

已知参数时可直接执行，不要求每次先查目录。工具箱说明随当前 MCP 目录提供，具体操作参数按需加载；详细校验、模块激活和授权规则保持原样。

下文表格、帮助和 `detail_lookup` 返回的操作名，如果不在当前独立工具目录中，填入 `stbrain_manage.action`，其原参数放入内层 `arguments`。例如日常档使用 `stbrain_manage(action="stbrain_help", arguments={"module":"learning_memory"})` 读学习说明。直接调用八个入口时，各业务参数仍平铺填写，不额外套一层业务 `arguments`。

客户端需重连、刷新目录后从新回合使用日常档；旧会话已发送的说明不会自动消失。目录选择不改变权限，客户端仍可禁用或要求审批工具箱入口。

## 常用操作

| 需求 | 调用 |
| --- | --- |
| 按模块了解用法 | `stbrain_open(view="manual", module=...)`；静态帮助用 `stbrain_help` 操作 |
| 新增一条普通记忆 | `remember_memory(content=...)`；`module`、分类和其他元数据可省略 |
| 暂存碎片或工作资料 | `remember_work_memory(content=...)`；标签可省略，仅显式读取 |
| 新增工具卡 | `remember_tool_guidance(tool_name, purpose)` |
| 跨普通模块搜索或浏览 | `stbrain_open(view="recall", query=...)`；空 query 为目录 |
| 修改四个普通模块 | `revise_memory(target_ref, changes)` |
| 查询自写提醒 | `query_self_governance_profile(view="status", include_content=true)` |
| 自写或清空提醒 | `manage_self_governance_profile(action="set"/"clear", scope=...)` |

`remember_memory` 的 `content` 是唯一必填项。只有正文时保存为情感脑的未分类记录；自动模块选择只依据明确元数据，不猜正文语义。情感分类 `moment`/`event` 兼容为 `shared_event`；不认识的分类或情绪用默认值并返回固定警告，不丢正文。未知模块、来源与可信度等字段仍严格校验；已确认的人称校对回执要求精确模块和原样最终字段。细节见 [普通保存](../mcp_server/OPEN_RESPONSE.md#1-普通保存)。

正文示例是调用意图说明。真实参数结构以连接实例的工具 Schema 为准；把 JSON 放进普通聊天框仍是聊天文字。

## 完整目录

以下列出完整模型目录的 57 个独立入口，不含 host-only 运输项。使用日常档时，其余操作通过上面的分类工具箱调用。

### 情绪标签 · 3 项

| 工具 | 用途 |
| --- | --- |
| `emotion_list` | 看自己定义的名称、定义、设置及无正文计数 |
| `emotion_manage` | 新建、改名、修改定义、归档、恢复、删除或设置连带数量和冷却 |
| `memory_emotions` | 给当前版本的普通记忆添加、移除或替换标签；不改变原文或权限 |

日常档从 `stbrain_tools(category="feelings")` 发现，细节见 [情绪层](EMOTION-LAYER.md)。

### 工作记忆与关系 · 6 项

| 工具 | 用途 |
| --- | --- |
| `remember_work_memory` | 显式保存碎片或工作正文；只有 `content` 必填，标签省略为“随手记” |
| `recall_work_memory` | 按查询或真实引用读取工作记录，支持分页与退役记录选择 |
| `revise_work_memory` | 在读过的版本上修订正文、标签或 active/retired 生命周期 |
| `attach_memory_relation` | 以真实情感、学习或规划引用建立关系 |
| `read_memory_relations` | 在授权范围内读取一跳关系 |
| `detach_memory_relation` | 解除一条已读关系，保留关系版本历史 |

工作记忆使用 `work://…@版本` 引用，不走普通 `revise_memory`，也不自动参与召回或图谱展示；退役不是物理删除。它与换窗的有界短期缓存、供应商 token 缓存分别管理。模块激活、身份、学习作用域和执行授权仍需满足。

关系目前支持情感、学习和规划，不涵盖工作记忆、自我定义或黑匣子。它记录显式联系，不自动改写端点正文、不让未授权内容进入结果；关系名称不能证明现实因果。具体参数通过 `stbrain_tools(action="attach_memory_relation")` 等当前目录取得。

### 本版新增：记忆图片附件 · 4 项

| 工具 | 用途 |
| --- | --- |
| `store_memory_image` | 给已有且可访问的情感、学习或规划记忆附上真实图片原字节与描述 |
| `list_memory_images` | 分页列出同一记忆的图片描述和编号，不取原图 |
| `read_memory_image` | 默认读取元数据；明确 `include_data=true` 才返回原图 |
| `remove_memory_image` | 软删除同一记忆的图片附件，不删除原记忆或擦除备份 |

日常档从 `stbrain_tools(category="images")` / `stbrain_manage` 使用。图片不自动进入召回、星图或模型请求；不是自动识图服务，敏感目标另有拒绝边界。大小、权限和隐私限制见 [图片附件](MEMORY-IMAGES.md)。

### 1. 普通操作与帮助 · 5 项

| 工具 | 用途 |
| --- | --- |
| `stbrain_health` | 检查服务健康、模块状态和无正文计数 |
| `stbrain_help` | 总览及指定模块的静态使用帮助 |
| `remember_memory` | 简洁保存情感、学习或规划内容 |
| `revise_memory` | 统一修改四个普通模块的作者字段，保留历史 |
| `advance_plan` | 记录推进、完成、暂停、恢复或重新开启 |

### 2. 上下文与模块一 · 6 项

| 工具 | 用途 |
| --- | --- |
| `stbrain_open` | `recall` 统一读取；`summary` 查看当前上下文；`manual` 按模块读材料；`review` 分页呈现模块一候选 |
| `stbrain_open_direct` | 消费真实短期 grant，打开相应 Direct 上下文 |
| `authorize_self_model` | 普通配置中的直连模块一密码授权；查询和普通写入分别处理 |
| `submit_self_model_candidate` | 按当前阶段准备、提交、修订或复核自我定义候选 |
| `activate_self_model_candidate` | 在满足真实后续轮次等条件后激活已接受候选 |
| `query_self_model` | 状态、活动自我、完整编辑依据和归档读取 |

### 3. 情感记忆 · 6 项

| 工具 | 用途 |
| --- | --- |
| `remember_emotional_memory` | 用详细字段保存经历、摘要、来源和情感 |
| `recall_emotional_memory` | 搜索，或按记忆 ID 读取原文与历史 |
| `revise_emotional_memory` | 专用情感修订；日常优先使用统一 `revise_memory` |
| `integrate_emotional_memories` | 由作者整合相关经历并保留来源关系 |
| `manage_brain_pin` | 管理有限数量的跨轮锚点 |
| `veto_ephemeral_memory` | 按工具范围撤除短暂材料 |

### 4. 学习记忆 · 7 项

| 工具 | 用途 |
| --- | --- |
| `remember_learning_memory` | 保存知识、当前理解、来源与场景 |
| `remember_learning_contrast_pair` | 成对保存不同或相反观点及对照关系 |
| `recall_learning_memory` | 搜索、精确读取；`inventory` 分页浏览目录 |
| `revise_learning_memory` | 专用学习修订；日常作者字段可用统一入口 |
| `integrate_learning_memories` | 由多张卡形成归纳、对照或方法 |
| `review_learning_change` | 按当前回执审阅已有学习候选 |
| `preview_learning_recall` | 预览指定场景的学习概要 |

### 5. 工具记忆 · 4 项

| 工具 | 用途 |
| --- | --- |
| `remember_tool_guidance` | 记一个工具或 MCP 服务的用途和经验 |
| `recall_tool_guidance` | 目录、建议、卡片原文、版本历史、失败或全部结果的经验 |
| `review_tool_guidance_candidate` | 审阅已存在的工具卡候选 |
| `record_tool_experience` | 保存实际尝试的作者记录和经验 |

工具卡修改统一使用 `revise_memory`。`tool_name` 可写 MCP 服务大名，`operation_key` 可省略；`purpose` 写用途，`reminder` 可写至多 100 字符的短提醒。中文场景标签可用，具体调用细节按需保存和读取。

可信度由 AI 在 0–100 内填写，来源单独记录。有效期可选：创建时省略或 null 表示不设置到期；后续统一修改时 `expires_at:null` 清除日期。工具经验的作者判断与真实执行器确认分开表示。

保存工具卡只是记录用法，不执行工具。`risk_level` 与 `confirmation_policy` 可省略或独立填写，不再因为写操作、设备/发送等关键词或高风险标签拒绝存卡。新卡默认 `medium` / `none`，仅为未另行设置的元数据、不给卡片额外增加逐次确认要求，不是安全评级或执行授权。已有卡片不自动改写；作者明确选择的 `explicit_each_time` 保留。实际调用仍由调用端与目标工具按当前授权处理。

读回成功、部分成功及其他结果，使用 `recall_tool_guidance(view="experiences", card_id=真实卡编号)`。`query` 可填精确经验 ID，或尝试、教训、原因和结果类型中的文本；`limit` 最多 5 条，查看 `total` 与 `truncated` 判断是否还有匹配记录。日常档示例：

```json
{"name":"stbrain_manage","arguments":{"action":"recall_tool_guidance","arguments":{"view":"experiences","card_id":"<已有卡编号>","limit":5}}}
```

原 `failures` 仍只返回非成功结果，`history` 读取卡片版本。经验中的 `ai_reported` / `verified=false` 表示作者自报，可信度 100 也不自动变成独立核验。读回按任务需要使用，不要求每次保存后都多查一遍。

`call_notes_current` 是工具广告、参数结构匹配、有效期、活动状态及最近失败等条件的综合诊断，不代表“是否保存”或“最近是否修改”。保存与修改结果以实际回执和版本为准。

### 6. 自写轻提醒与安全阀 · 2 项

| 工具 | 用途 |
| --- | --- |
| `manage_self_governance_profile` | `set` 自写/替换，`clear` 清空，`rollback` 回到已存历史；显式旧候选动作沿旧路径 |
| `query_self_governance_profile` | 读取机制、当前内容、状态和版本 |

支持 `global`、`self_revision`、`emotional_memory`、`learning_memory`、`tool_use` 范围。`manual_only` 主动查阅，`scene_relevant` 结合作者的 `scene_tags` 浮现；`global` 也遵循触发模式。规划场景可用 global 提醒，单条计划还有自己的 reminder。

### 7. 自动注入控制 · 2 项

| 工具 | 用途 |
| --- | --- |
| `manage_injection_control` | 设置、回滚模块模式或执行全局紧急关闭 |
| `query_injection_control` | 查询模式、手册和历史 |

模式为 `enabled`、`paused`、`hard_off`、`status_only`。使用查询所得的作用范围与当前版本条件；黑匣子保持独立授权流程。设置作用于之后的新快照，已发送的上下文保持当时内容。记录本身、主动查询与自动浮现分别管理。

### 8. 规划记忆 · 5 项

| 工具 | 用途 |
| --- | --- |
| `remember_planning_memory` | 详细字段保存计划；简洁新增可用 remember_memory |
| `recall_planning_memory` | 读取计划、进度、事件和历史 |
| `record_planning_event` | 专用计划事件接口，按本工具的字段与证据规则使用 |
| `revise_planning_memory` | 专用计划修订；日常修改优先使用统一入口 |
| `review_planning_change` | 处理已有规划候选 |

普通计划的父项可选。`advance_plan` 的 `expected_event_seq` 来自查到的 `event_seq`；`progress`、`complete`、`reopen` 需要真实 evidence，`pause`、`resume` 按说明提交安排变化。

### 9. 幻觉黑匣子 · 4 项

| 工具 | 用途 |
| --- | --- |
| `hold_hallucination_record` | 保存隔离内容或更新 AI 自写警示 |
| `open_hallucination_vault` | 查看目录、警示，按独立确认流程读取正文 |
| `transfer_hallucination_record` | 预览和确认转入隔离，或提出恢复候选 |
| `review_hallucination_restore` | 按独立复核流程接受、拒绝或撤回恢复候选 |

黑匣子正文不进入普通自动记忆检索；相关操作使用自身材料与回执。

已存入的黑匣子原记录不可修改、覆盖或删除。补充认识应使用 `intent=record`
另存一条，保留原记录；这不是对原记录的原位追加或改写功能。
`intent=update_warning` 仅更新开库前的 `warning_text` 和读完后的 `warning_suffix`，
不能携带记录正文来修改原文。

两段警示词都必须由 AI 自写、以第一人称开头（如“我”或英文 `I` / `My`）。
人称校验失败会用 `invalid_fields` 指明哪一段不符合开头格式；隔离原文不接受这项人称检查，
也不会因为正文提到其他人而被该检查拦截。缺少结束警示、尚未初始化、尝试改写原文、
没有本轮黑匣子写入上下文分别返回对应原因，不要靠反复改正文人称重试。

### 10. 人称提醒与可选称呼校对 · 3 项

| 工具 | 用途 |
| --- | --- |
| `manage_person_reference_advisory` | `set` 自写人称提醒，`disable` 关闭，`reset` 恢复默认 |
| `preview_person_reference_rewrite` | 作者指明草稿里的称呼指谁、换成什么，查看字面替换预览 |
| `confirm_person_reference_rewrite` | 确认相同最终稿，取得单次写入 receipt |

称呼校对每条草稿默认关闭。情感、学习、工具的对应存入入口支持回执；统一 `remember_memory` 已接通情感和学习，规划暂未接入。它校对作者指定的字面称呼，叙事视角与措辞由作者自己组织。

**最短流程：指明替换 → 预览 → 确认 → 按原入口存入。** 版本和校验信息由 ST 从保存的预览取得；人物指向采用作者的明确声明，另交人物认证、别名登记或重复绑定已从新流程中省去。

下面是一段合成的情感草稿，两个字段均保留在预览中：

```json
{
  "module": "emotional_memory",
  "draft_fields": {
    "/original_text": "我和她完成了练习。",
    "/summary": "共同练习。"
  },
  "rewrite_targets": [{
    "field_path": "/original_text",
    "surface_form": "她",
    "entity_ref": "一起练习的人",
    "target_surface_form": "小林"
  }]
}
```

把这组参数交给 `preview_person_reference_rewrite`。`entity_ref` 是作者给人物的名称或标记；只有一处匹配时可省略位置，多处时加 `occurrence_index` 指明第几处（从 0 开始）。`draft_fields` 的键使用带 `/` 的完整字段路径；学习正文对应 `/current_understanding`，工具卡字段按当前工具说明提供。

看过预览后，把它返回的 `preview_id` 与 `ai_confirmation: true` 交给 `confirm_person_reference_rewrite`。确认结果带有完整 `final_fields` 和 `rewrite_receipt`，随后把原样最终稿及回执交给对应存入入口。确认完成表示接受这份草稿；实际保存以存入工具的成功回执为准。

学习草稿可以只提供 `/title`、`/summary`、`/current_understanding`。统一存入时，省略 `/preceding_context_summary` 与精确空字符串等价，无需为通过校验补上隐藏空字段；非空、空白字符串、null 及其他字段仍按原规则区分。确认后改了正文或其他内容，应重新预览确认。

草稿在预览后有新改动时，重新预览新稿。旧调用可以继续显式提供版本、哈希和上下文字段，ST 会核对它们与原预览的一致性。原有模块激活、调用者权限、保护范围和回执检查继续生效。

## 普通修改：字段和引用

`revise_memory` 的必填参数为 `target_ref` 与 `changes`。以下是常用作者字段示意，完整集合由当前工具 Schema 给出：

| 模块 | 引用形态 | 常用 changes |
| --- | --- | --- |
| 情感 | `emotion://…@版本` | original_text、summary、primary_emotion、origin、confidence、keywords、importance |
| 学习 | `learning://…@版本` | title、summary、current_understanding、source_basis、confidence、keywords、importance |
| 规划 | `plan://…@版本` | title、original_text、summary、reminder、parent_ref、keywords、importance |
| 工具 | `tool-card://toolcard_…@版本` | purpose、reminder、scenario_tags、confidence、expires_at 等 |

引用应直接复制查询回执。工具卡的 `reminder`、`source_ref`、`expires_at` 显式 null 清除，省略保留；旧 `clear_fields` 写法兼容。普通修改直接形成可追溯的新版本，技术 ID、所有者、哈希和派生状态由系统维护。

## 每次存入前的短提醒

五个存入入口的目录描述会展示当前人称提醒及固定标签选词建议；成功回执也会附带。人称提醒可自写与关闭，标签选词建议目前是固定帮助文案。目录为读取时的快照，修改偏好后以最新管理结果为准，刷新 MCP 目录取得新版本。

日常档只常驻其中的统一存入和工具卡入口；其他存入操作的当前说明可用 `stbrain_tools(action=...)` 按需读取。网关新对话第一轮的 ST 入口提醒是另一项可选提示，只在本轮提供 `stbrain_open` 且总注入开关允许时出现，不构成保存前的必经步骤。

源码依据：[server.py](../mcp_server/server.py)、[公开参数契约](../mcp_server/public_contract.py)、[普通修改 Schema](../mcp_server/ordinary_revision_schema.py)、[使用帮助](../mcp_server/usage_guide.py)。
