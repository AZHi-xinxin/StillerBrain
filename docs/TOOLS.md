# 完整工具目录

[返回首页](../README.md) · 43 项 MCP 工具

## 五、完整工具目录

当前公开版共有 **43 项 MCP 工具**，按用途分为以下十组。日常使用可以先掌握上一节；本节方便需要时查找。

“读取”指业务内容读取，受保护读取可能产生审计痕迹。“写入”也包括候选、版本、进度或流程状态；具体结果以回执为准。

### 1. 普通操作与帮助 · 5 项

| 工具 | 用途 | 核心输入 / 结果 |
| --- | --- | --- |
| `stbrain_health` | 查看服务健康 | 无参数；健康状态 |
| `stbrain_help` | 阅读简明操作指南 | 无参数；日常路径与模块说明 |
| `remember_memory` | 保存普通经历、知识或计划 | `module`、`content`；可选标题、来源等；写入 |
| `revise_memory` | 修订摘要与检索信息 | `target_ref`、`changes`；可选 `reason`；新版本 |
| `advance_plan` | 记录计划的推进与状态变化 | `target_ref`、`expected_event_seq`、`event_type`、`note`；按动作提供证据 |

### 2. 高级上下文与自我模型 · 5 项

| 工具 | 用途 | 核心输入 / 结果 |
| --- | --- | --- |
| `stbrain_open` | 打开高级操作上下文、模块手册或审阅材料 | `view=summary/manual/review`；模块和分页信息；产生相应上下文/审阅状态 |
| `stbrain_open_direct` | 打开人类独立授权的 Direct 写入上下文 | `grant_ref`；消费短时一次性授权 |
| `submit_self_model_candidate` | 准备、提交、修订或审阅自我模型候选 | `intent`、写入引用、当前版本、动作对应 `payload` |
| `activate_self_model_candidate` | 激活已接受的核心候选 | 候选ID、写入引用、候选基础/当前版本、AI确认 |
| `query_self_model` | 读取状态、活动自我、编辑依据或归档 | `view=status/active/search/edit_basis` 等 |

### 3. 经历、情感与关系 · 6 项

| 工具 | 用途 | 核心输入 / 结果 |
| --- | --- | --- |
| `remember_emotional_memory` | 精细记录经历及情感、来源和可见策略 | 类型、原文、摘要、主要情感、理由及版本；高级写入 |
| `recall_emotional_memory` | 找回经历与关系资料 | 查询词或记忆ID、数量、是否读取原文；读取 |
| `revise_emotional_memory` | 为旧经历追加新的理解与策略 | 记忆ID、版本、理由和变更字段；新版本 |
| `integrate_emotional_memories` | 整合同一历程的多条经历 | 2–20项来源ID及新摘要等；聚合并归档来源 |
| `manage_brain_pin` | 管理有限的长期锚点 | `request/confirm/lower/remove`；最多5项活动锚点 |
| `veto_ephemeral_memory` | 撤除指定的短暂材料 | 临时记录ID或会话ID二选一、理由等；有清除效果 |

### 4. 知识、方法与学习 · 7 项

| 工具 | 用途 | 核心输入 / 结果 |
| --- | --- | --- |
| `remember_learning_memory` | 精细保存知识卡、理解、依据和不确定处 | 类型、标题、摘要、当前理解、来源与评估等；高级写入 |
| `remember_learning_contrast_pair` | 保留两种相反说法及其对照关系 | 两项说法、对照依据、场景及不确定性；原子写入 |
| `recall_learning_memory` | 搜索、按精确引用读取或浏览目录 | `view=search/inventory`、查询或引用、分页；读取 |
| `revise_learning_memory` | 修订知识卡或提出重要变化候选 | 目标引用、版本、动作、变化分类、依据和理由 |
| `integrate_learning_memories` | 从多张卡形成总结、对照或方法 | 2–20项来源、整合类型、新理解与评估；候选 |
| `review_learning_change` | 审阅重要学习变化 | 候选ID、版本、材料hash与评估；接受或拒绝 |
| `preview_learning_recall` | 预览场景中可能浮现的学习摘要 | `situation`、`limit`；预览 |

### 5. 工具使用经验 · 5 项

| 工具 | 用途 | 核心输入 / 结果 |
| --- | --- | --- |
| `remember_tool_guidance` | 保存工具用途、使用条件、风险和成功标准 | 工具名、操作标识、完成规则、场景等；指导卡 |
| `recall_tool_guidance` | 查询工具建议、卡片、历史或失败经验 | 查询词、工具名或卡片ID；`suggestions/card/history/failures` |
| `revise_tool_guidance` | 修正、退役或恢复工具指导卡 | 卡片ID、版本、动作和变化分类；修订或候选 |
| `review_tool_guidance_candidate` | 复核重大工具卡变化 | 候选和材料hash、决定与评估；审阅状态 |
| `record_tool_experience` | 记录一次工具尝试的结果与教训 | 卡片ID、结果、原因分类、尝试摘要等；经验记录 |

### 6. AI 自我治理边界 · 2 项

| 工具 | 用途 | 核心输入 / 结果 |
| --- | --- | --- |
| `manage_self_governance_profile` | 提议、确认、清除或回退自身行为边界 | 动作、范围、版本、第一人称文本等；候选或状态变化 |
| `query_self_governance_profile` | 阅读机制、边界状态与历史 | `view=status/manual/revisions`、范围等；读取 |

### 7. 自动上下文注入控制 · 2 项

| 工具 | 用途 | 核心输入 / 结果 |
| --- | --- | --- |
| `manage_injection_control` | 调整全局或模块的自动注入方式 | 动作、范围、版本、目标模式等；设置或候选 |
| `query_injection_control` | 阅读注入状态、手册与历史 | `view=status/manual/history`；读取 |

注入控制管理“自动送入后续上下文”的内容。已保存的数据和主动读取是另外的功能；更宽松的控制变化按相应候选流程处理。

### 8. 目标、计划与承诺 · 5 项

| 工具 | 用途 | 核心输入 / 结果 |
| --- | --- | --- |
| `remember_planning_memory` | 创建经过明确采纳的高级计划候选 | 类型、轨道、计划内容、场景、采纳说明、冷静检查等 |
| `recall_planning_memory` | 查找计划、进度与历史 | 查询词或精确计划引用、历史选项等；读取 |
| `record_planning_event` | 使用高级接口记录有依据的计划事件 | 计划ID、版本、事件类型、证据等；事件账本 |
| `revise_planning_memory` | 提议改变、放弃、归档、恢复或回退计划 | 计划ID、版本、动作、理由、冷静检查等；候选 |
| `review_planning_change` | 在后续真实轮次处理计划候选 | 候选与版本、材料hash、决定和评估；审阅 |

### 9. 幻觉与不确定内容隔离 · 4 项

| 工具 | 用途 | 核心输入 / 结果 |
| --- | --- | --- |
| `hold_hallucination_record` | 保存选定隔离内容或更新自写警示 | `intent=record/update_warning`、内容/警示及理由等 |
| `open_hallucination_vault` | 列目录、查看警示或明确读取一条记录 | 记录ID、分页；正文读取需当前警示确认 |
| `transfer_hallucination_record` | 预览并确认转入隔离，或提出恢复候选 | `preview/commit/propose_restore`、来源与版本、hash等 |
| `review_hallucination_restore` | 审阅恢复候选 | 候选与版本、材料hash、激活/拒绝/撤回动作等 |

### 10. 共享人物指代校对 · 2 项

| 工具 | 用途 | 核心输入 / 结果 |
| --- | --- | --- |
| `preview_person_reference_rewrite` | 校对草稿中的已绑定人物称呼 | 草稿、人物绑定、保护片段及版本等；生成字面替换预览 |
| `confirm_person_reference_rewrite` | 确认已核对的同一份替换预览 | 预览ID、原文/建议/最终字段hash与确认等；单次写入回执 |

人物指代确认得到回执后，最终记忆仍通过相应模块的写入工具保存。

高级写入通常需要 `write_context_ref` 和 `expected_*` 版本：从真实的当前操作上下文和查询回执取得即可。普通网关写入的当前轮次由宿主绑定；Direct 写入需要独立人类授权。`public-tools/20` 是协议版本标签，工具总数以目录为准。

源码依据：[工具声明](https://github.com/AZHi-xinxin/StillerBrain/blob/main/mcp_server/server.py)、[公开输入契约](https://github.com/AZHi-xinxin/StillerBrain/blob/main/mcp_server/public_contract.py)、[简明使用指南](https://github.com/AZHi-xinxin/StillerBrain/blob/main/mcp_server/usage_guide.py)。
