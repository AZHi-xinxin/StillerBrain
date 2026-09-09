# ST 日常使用与按需手册：public-tools/20 / brain-open/2（r4h38 本地候选）

本说明对应 public-tools/20 的 43 个工具候选目录，不是已部署或手机验收成功声明。
public-tools/19 保留给既有 r4h36 候选；本轮不改历史版本或其冻结证据。
新增 review 分段视图仍由原 `stbrain_open` 承载，已接线并通过隔离流程测试；完整验证记录见任务目录，尚未发布。
使用 ST 不需要沙箱、shell、文件路径或 grep。

## 普通新增：一次调用

正常网关路径直接调用 `remember_memory(module, content)`，仅这两个参数必填：

| module | 内容 |
|---|---|
| `emotional_memory` | 人际经历、感受与重要对话 |
| `learning_memory` | 知识、经验或当前理解 |
| `planning_memory` | 计划、任务或承诺 |

正文保留实际传入的原文，长度为 1–2000 字符；`title`、`summary` 等为可选组织信息。
默认 `source_basis=reported` 不是独立核验。普通计划直接成为活动记录，但不授权或自动执行外部操作。

不需要先 `stbrain_open`，不手填内部引用或模块版本，不提交候选，不等另轮审核。
宿主绑定准确的当前调用并取得版本；不要编造或复用 `execution_ref`。
以实际 `decision=stored`、`stored=true` 回执为准；未知或不完整回执先核查，不自动重复写入。
短说明可按需要调用 `stbrain_help()`，它不是存入前置步骤。

## 主动查询已有内容

按需要调用 `recall_emotional_memory`、`recall_learning_memory`、`recall_planning_memory`
或 `recall_tool_guidance`，无需先打开写入上下文。普通查询不等于扩大敏感内容的披露权限。

精确回读使用真实工具返回的引用：情感用 `memory_id`，学习用 `target_ref`，规划用 `plan_ref`。
学习全量目录用 `recall_learning_memory(view="inventory")`；`view="search"` 不是全量清单。
`preview_learning_recall(situation=...)` 可预览某场景的学习概要浮现。零命中不证明整个库为空，
自动浮现也不保证每次命中全部相关内容。

## 普通小改与计划进度

`revise_memory(target_ref, changes)` 使用已经读到的精确版本引用；`reason` 可选。
只修改以下字段，其他改动走对应模块的原有高级流程：

| 模块 | 普通小改字段 |
|---|---|
| 情感 | `summary`、`keywords`、`entities`、`importance` |
| 学习 | `title`、`summary`、`domain`、`keywords`、`entities`、`importance` |
| 规划 | `title`、`summary`、`keywords`、`importance` |

这不是正文更新接口：不改原始事件、学习 `current_understanding`、来源、可信状态、计划层级或权限。
不可变原文即使走高级接口也不能覆盖；更正通过补充或版本化解释保留历史。
模块行版本由宿主取得，目标版本仍以已读 `target_ref` 为准；冲突后先读回，不自动用最新版本覆盖。

计划进度用 `advance_plan(target_ref, expected_event_seq, event_type, note)`：将查询返回的
`event_seq` 原样放入 `expected_event_seq`，仍需满足原状态条件和完成等事件的真实证据要求。
不猜事件序号、不捏造证据；正常网关路径无需先 open 或手填模块行版本。
成功以相应工具的真实回执为准，不把修改计划或记录完成事件当成已执行外部任务。

## 高级操作：按需打开对应模块

专用高级新建、修改、整合、置顶及核心自我修改仍使用各自已有工具和校验。
需要说明时，调用 `stbrain_open(view="manual", module=对应模块)`；
不要为了普通新增、小改或进度快捷操作走这条路径。原 `record_planning_event` 等高级接口仍保留，
不是所有规划操作都要候选复核。

`stbrain_open()` 默认 `view="summary"`，只返回当前状态、真实根 `write_context_ref` 和模块版本，
不返回全局手册或完整候选。manual 复用同一真实唤醒的引用，仅呈现所选模块。
可选模块为 `self_revision`、`emotional_memory`、`learning_memory`、`tool_guidance`、
`planning_memory`、`self_governance_profile`、`injection_control`、`hallucination_vault`、
`shared_person_authoring`。

模块一候选全文另用 `stbrain_open(view="review", module="self_revision", page=0)` 分段读取。
`page` 默认 0；续页必须带返回材料所对应的 `expected_material_hash`，不猜哈希、不拼接不同版本的页。
review 仅支持 self_revision；其他模块不借此扩大可见范围。
只有完整材料的全部页齐备才形成完整展示 proof；summary、单页或重复同一页都不等于完整复核。
分段材料不依赖 shell 或工作区文件提取，也不意味着减少候选正文与必要复核信息。

编辑之前可直接 `query_self_model(view="edit_basis")` 读取 `result.active.content` 的完整活动五键正文，
包含原有 facets 和来源引用；不需要从注入层手工拼出一个新正文。`view="active"` 仍是所选活动层，
编辑期也可读取，但不能把它当作完整编辑基线。两种读取都不创建写资格或“已经注入”的记录。
内容由 AI 自己决定和编写，不要求每一字段以“我”、I 或 My 开头；结构错误会给具体字段说明，
同一真实唤醒内修正字段可复用现有上下文，不能用修错为理由复用上一轮引用。

以下版本映射仅用于专用高级工具；`remember_memory`、`revise_memory`、`advance_plan` 不手填模块版本：

| 模块 | 摘要中的版本值 | 高级写工具版本参数 |
|---|---|---|
| 模块一 | 根 `row_version` | `expected_row_version` |
| 情感 | `emotional_memory.row_version` | `expected_emotion_version` |
| 学习 | `learning_memory.learning_row_version` | `expected_learning_version` |
| 工具认知 | `tool_guidance.tool_row_version` | `expected_tool_row_version` |
| 规划 | `planning_memory.planning_row_version` | `expected_planning_version` |
| 自我治理 | `self_governance_profile.scope_versions` 的所选 scope | `expected_profile_version` |
| 注入控制 | `injection_control.scope_versions` 的所选 scope | `expected_control_version` |
| 隔离复核匣 | `hallucination_vault.vault_row_version` | `expected_vault_version` |
| 可选人称辅助 | `shared_person_authoring.row_version` | `expected_authoring_version` |

使用本轮实际引用与对应目标版本；同轮连续高级写入使用上一成功结果的新模块版本。
`argument_sources` 中的 JSONPath 只是取值说明，不是引用本身。新回合或引用失效后不复用旧值；
遇版本冲突保留草稿，读取当前版本后再决定，不能用新授权掩盖旧引用错误。

## 非网关 direct 写入

区别在连接与授权路径，不是官 DS 模型本身不能使用 ST。
已接通 MCP 的非网关客户端可以查询；写入须有独立人类签发、短期且一次性的真实 grant，
经 `stbrain_open_direct(grant_ref=...)` 打开有效 direct 上下文。
其授权 scope 内可调用 `remember_memory(module, content, write_context_ref=实际返回值)`；
不能用普通旧引用、MCP token 或口头声明代替授权。direct 不制造自动注入证据，
本次说明调整不改变其签发、消费、过期、重放与 scope 规则。

## 保留的呈现与审核边界

- summary 不推进模块一 `candidate_wait`，不构造完整 continuation，也不登记
  `candidate_full_review` 或 `human_objection_presented`。`review_material_presented=false`
  只表示这次摘要没有呈现，不撤销同轮此前合法完整读取产生的证明。
- self_revision 的候选全文可走 review 分段路径；其他模块只调用所选模块的展示方法。
  候选正文、哈希、版本和必要证明不能为了体积达标而裁掉，也不能把静态帮助当成已审核。
- review 全页齐备才能形成完整展示证明；证明不自动等于接受审核。核心自改仍需三个真实唤醒：
  提交候选、后来独立审核、再后来激活，不能因为读过一页就在同轮绕过。
- 服务端完整返回不等于客户端已显示或模型已读完。超大材料仍受客户端限制；必要材料
  无法完整交付时，不登记虚假的阅读完成。黑匣子手册不等于已读取敏感正文。
- 原文历史、来源与权限边界保持；核心自我修改不由普通快捷入口代写、代审或同轮激活。

## 诊断与验证范围

规划专用高级工具的固定 `binding_reason_code` 可区分引用缺失／占位、无当前唤醒、过期、
注入未就绪及本轮引用不匹配。它不泄露其他主体的记录，不能据合并原因猜测某个旧引用。
`planning_row_version_conflict` 是版本冲突，不是“没有 open”。普通入口和执行绑定拒绝
应按各自实际回执处理，不套用所有高级诊断字段。

原 public-tools/16（39 工具）的 compact-open 测试只证明当时的摘要／完整手册和合成高级候选路径，
不代表当前普通新增仍须 open 或候选。`test_daily_native_transport.py` 等隔离测试用于普通新增与
实际 FastMCP 调用／结果转换；本版本 43 工具目录及新增小改、进度入口还需对应的隔离验证。
`test_usage_consistency_r4h34.py` 保留 public-tools/18 历史 AST 锚，同时为本候选重立当前 service 锚。
已独立核对只有五个预期函数与 QueryView 声明改变；还原六处后整文件 AST 与 r4h37 一致。
新锚未扩大擦除字段或跳过任何安全检查；当前版本文档断言同步至 public-tools/20。
这只是候选源码验证，不能代替新发布包的封存、维护窗口授权和部署后验收。测试代码的存在不代表测试已通过。
这些不是手机成功回执或真实上游端到端验收。
目录升级仍应等旧工具链结束后进行，不能热换正在执行的 schema。
