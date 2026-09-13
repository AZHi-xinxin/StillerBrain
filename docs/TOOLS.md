# 工具目录与常用调用

[首页](../README.md) · [日常指南](GUIDE.md) · [MCP 详细说明](../mcp_server/OPEN_RESPONSE.md)

L30 当前目录为 **44 项**。`simple-memory-v1` 和 `legacy` 的数量相同，集合不同：普通目录提供 `authorize_self_model`，并收起专用的 `revise_tool_guidance`；后者的旧调用仍保留校验与兼容。旧文档里的 40 / 43 等数量属于当时的目录或统计范围，使用时以当前 `tools/list` 为准。`public-tools/20` 是协议标签，不是工具数量。

下面以 `simple-memory-v1` 为主。模块一首次激活后，普通读写由服务补入内部上下文和模块行版本；AI 使用查询返回的真实目标引用。黑匣子、模块一以及显式旧候选操作保留各自的授权和复核路径。

## 先掌握这些

| 需求 | 调用 |
| --- | --- |
| 按模块了解用法 | `stbrain_help(module=...)`；省略模块看总览 |
| 新增经历、知识、计划 | `remember_memory(module, content)` |
| 新增工具卡 | `remember_tool_guidance(tool_name, purpose)` |
| 跨普通模块搜索或浏览 | `stbrain_open(view="recall", query=...)`；空 query 为目录 |
| 修改四个普通模块 | `revise_memory(target_ref, changes)` |
| 查询自写提醒 | `query_self_governance_profile(view="status", include_content=true)` |
| 自写或清空提醒 | `manage_self_governance_profile(action="set"/"clear", scope=...)` |

正文示例是调用意图说明。真实参数结构以连接实例的工具 Schema 为准；把 JSON 放进普通聊天框仍是聊天文字。

## 完整目录

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
| `recall_tool_guidance` | 目录、建议、卡片原文、历史和失败经验 |
| `review_tool_guidance_candidate` | 审阅已存在的工具卡候选 |
| `record_tool_experience` | 保存实际尝试的作者记录和经验 |

工具卡修改统一使用 `revise_memory`。`tool_name` 可写 MCP 服务大名，`operation_key` 可省略；`purpose` 写用途，`reminder` 可写至多 100 字符的短提醒。中文场景标签可用，具体调用细节按需保存和读取。

可信度由 AI 在 0–100 内填写，来源单独记录。有效期可选：创建时省略或 null 表示不设置到期；后续统一修改时 `expires_at:null` 清除日期。工具经验的作者判断与真实执行器确认分开表示。

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

源码依据：[server.py](../mcp_server/server.py)、[公开参数契约](../mcp_server/public_contract.py)、[普通修改 Schema](../mcp_server/ordinary_revision_schema.py)、[使用帮助](../mcp_server/usage_guide.py)。
