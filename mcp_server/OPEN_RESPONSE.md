# 日常调用与按需展开协议：public-tools/20 / brain-open/2

[日常指南](../docs/GUIDE.md) · [工具目录](../docs/TOOLS.md) · [MCP 组件](README.md)

当前公开契约为 `public-tools/20`，默认展开协议为 `brain-open/2`。本文以 `simple-memory-v1` 为主；默认完整目录提供 44 个公开工具，显式日常档为 7 入口，工具集合差异见 [TOOLS](../docs/TOOLS.md)。客户端使用当前工具 Schema 组织真实调用。

## 目录选择与操作调用

默认 `/mcp` 或 `tool_profile=full` 返回完整目录。`/mcp?tool_profile=daily` 或请求头 `X-STBrain-Tool-Profile: daily` 选择日常档；两个选择器各最多一次，同时提供时必须一致。选择按请求生效，不改变实例的访问配置，也不会在升级时自动替客户端切档。

日常档按顺序提供 `stbrain_open`、`remember_memory`、`remember_tool_guidance`、`revise_memory`、`advance_plan`、`stbrain_tools`、`stbrain_manage`。`stbrain_open` 无必填参数。`stbrain_tools()` 列分类，`stbrain_tools(category=...)` 列该类操作，`stbrain_tools(action=...)` 返回一个操作的准确参数。当前 `simple-memory-v1` 工具箱可发现 45 项操作，包含常用能力与兼容操作。

本文或 `detail_lookup` 中给出的操作名，若不在当前独立工具目录中，使用 `stbrain_manage(action=操作名, arguments=原参数对象)` 调用。已知参数时可直接执行；内层参数仍经过原 Schema、权限与执行绑定检查。直接调用七个入口时按各自 Schema 平铺业务参数，只有 `stbrain_manage` 使用内层 `arguments` 封装子操作。

更改连接后重连、刷新目录，从新回合开始；旧历史里的工具说明不会自动消失。客户端禁用或审批工具箱入口的选择继续生效。

## 1. 普通保存

```text
remember_memory(module, content)
```

仅 `module`、`content` 必填，正文长度为 1–2000 字符。

| module | 内容 | kind 省略值 |
| --- | --- | --- |
| `emotional_memory` | 经历、感受与人际语境 | `unclassified` |
| `learning_memory` | 知识、方法与当前理解 | `fact` |
| `planning_memory` | 计划、安排与承诺 | `task` |

`title`、`summary`、`keywords`、`importance` 等可按内容填写。普通记忆的 `source_basis` 省略为 `unmarked`，`confidence` 省略为 null，表示未标注；明确填写的 0–100 为作者判断。情感类型由作者选择。规划的 `parent_ref` 可选。

模块一首次激活后，简化配置的直连与网关调用均可直接保存。内部上下文和模块行版本由服务取得，普通保存直接返回存储结果。正文保留本次实际提交内容，存入后的提示附在回执里。

这些普通调用不需要先 `stbrain_open`，也无需填写内部模块行版本或另轮审核表。

工具卡使用 `remember_tool_guidance(tool_name, purpose)`。`tool_name` 可写 MCP 服务大名；`operation_key` 可省略。`reminder`、中文 `scenario_tags`、0–100 的可信度和有效期均按工具 Schema 填写。省略到期日期表示长期保留。来源字段与实际执行确认分别管理。

存储是否成功以真实工具结果为准；未知结果可先查询，避免重复写入。

## 2. 一次查询四个普通模块

```text
stbrain_open(view="recall", query="查询内容")
```

| 参数 | 用法 |
| --- | --- |
| `query` | 搜索词句；空字符串浏览目录 |
| `module` | 可选，只查其中一个普通模块 |
| `limit` | 默认 20，范围 1–50 |
| `cursor` | 继续查询时传上次返回的 `next_cursor` |

结果提供安全摘要、真实版本引用和 `detail_lookup`。保留 query、module 等查询条件继续翻页；发生数据变化或游标失效后重新开始查询。部分模块暂时不可用时，结果用 `partial/errors` 说明。

这个目录覆盖四个普通模块的可读记忆卡。核心自我、隔离匣、独立修改候选和短期缓存使用各自入口。历史版本通过对应详情工具读取。

`recall` 是独立的只读视图，无需写上下文或部署密码。它与用于核心复核的 `page`、`expected_material_hash` 分页机制分开。

精确详情按返回的 `detail_lookup` 调用。专用 `recall_emotional_memory`、`recall_learning_memory`、`recall_planning_memory`、`recall_tool_guidance` 均保留。学习的 `inventory` 适合浏览目录，`search` 适合相关检索；相关搜索中的零命中只说明本次条件下的结果。

精确读取的参数名称按对应工具填写：情感使用 `memory_id`，学习使用 `target_ref`，规划使用 `plan_ref`。直接采用真实回执中的详情参数即可。

## 3. 统一修改作者内容

```text
revise_memory(target_ref, changes, reason=可选理由)
```

只需真实 `target_ref` 与本次要改的字段。引用直接复制查询结果，版本定位已经包含其中；内部模块行版本由服务处理。真实并发冲突时重新读取，再决定本次修改。

模块一首次完成并激活后，普通作者修改使用已读引用提交原文、摘要、情绪、来源或其他公开作者字段；版本历史保留，作者自行选择叙述人称。需要对应模块说明时，完整目录调用 `stbrain_help(module=对应模块)`；日常档调用 `stbrain_manage(action="stbrain_help", arguments={"module":对应模块})`。

| 目标 | 引用前缀 | 作者字段示例 |
| --- | --- | --- |
| 情感 | `emotion://` | `original_text`、`summary`、`memory_type`、`primary_emotion`、`origin`、`confidence`、`keywords` |
| 学习 | `learning://` | `current_understanding`、`title`、`summary`、`steps`、`source_basis`、`confidence`、`keywords` |
| 规划 | `plan://` | `original_text`、`title`、`summary`、`reminder`、`kind`、`parent_ref`、`dependency_refs` |
| 工具 | `tool-card://` | `tool_name`、`purpose`、`reminder`、`scenario_tags`、`confidence`、`expires_at` |

完整可改字段和长度以 [ordinary_revision_schema.py](ordinary_revision_schema.py) 及当前公开 Schema 为准。省略的字段保留原值。工具卡的 `reminder`、`source_ref`、`expires_at` 可直接填 null 清除；旧 `clear_fields` 表达继续兼容。

工具卡退役使用 `changes.intent="retire"`，从历史恢复使用 `changes.intent="restore"` 与已查询的 `target_version`。其他作者字段的 null 含义按对应 Schema 与服务规则处理。

普通作者修改直接追加新版本，通常只交改动内容。技术 ID、所有者、哈希和派生字段由 ST 维护。原文可修订，先前版本仍可追溯。显式旧候选、隔离、恢复及核心自我修改保留各自流程。

## 4. 记录计划进度

```text
advance_plan(target_ref, expected_event_seq, event_type, note)
```

`expected_event_seq` 原样取自查询得到的 `event_seq`。`progress`、`complete`、`reopen` 使用对应真实证据；`pause`、`resume` 表达安排变化。计划状态记录与外部工具的实际执行结果分别保存。

普通计划任意层级均可独立创建，父项可选。专用 `record_planning_event` 与已有候选复核工具继续按自身参数和状态规则工作。

## 5. 提醒：内容、开关与目录快照

| 能力 | 入口与范围 |
| --- | --- |
| AI 自写轻提醒和安全阀 | `manage_self_governance_profile`：`set`、`clear`、`rollback` |
| 查看提醒 | `query_self_governance_profile` |
| 一条计划或工具卡的提醒 | 该记录的 `reminder`，日常通过 `revise_memory` 修改 |
| 人称弱提醒 | `manage_person_reference_advisory`：`set`、`disable`、`reset` |
| 模块自动注入模式 | `manage_injection_control` / `query_injection_control` |

自我治理支持 `global`、`self_revision`、`emotional_memory`、`learning_memory`、`tool_use`。`manual_only` 主动查阅；`scene_relevant` 使用作者提供的 `scene_tags`。当前治理标签按本轮人类自然话语做大小写归一后的子串匹配，适合写人类会说的表达。命中提供候选，实际呈现仍受当前模式和预算影响。

注入模式为 `enabled`、`paused`、`hard_off`、`status_only`。按工具返回的当前版本条件操作，设置参与后续新快照；已交付的聊天内容保留当时状态。

五个存入工具——`remember_memory`、`remember_emotional_memory`、`remember_learning_memory`、`remember_tool_guidance`、`remember_planning_memory`——在 `tools/list` 描述中附带当前人称提示和固定标签选词帮助。实现复制当前描述，不改变参数 Schema 和注册原描述；成功存入回执也附带提示。

目录描述是一份读取时快照。本轮修改或关闭提醒后，最新管理结果优先；客户端刷新 MCP 目录可取得新描述。读取偏好失败时显示不可用状态，关闭时省去提示正文。标签选词短帮助当前固定，协议、权限和来源文字由程序提供。

### 可选字面称呼校对

每条草稿默认关闭，由 AI 显式 `preview_person_reference_rewrite → confirm_person_reference_rewrite → remember`，确认相同最终稿后使用一次性 `rewrite_receipt`。支持情感、学习与工具专用存入；统一 `remember_memory` 支持情感和学习，规划暂不支持。

预览只需 `module`、完整 `draft_fields` 与 `rewrite_targets`。每条目标提供 `field_path`、`surface_form`、`entity_ref`、`target_surface_form`，表示在哪里、原称呼、指谁、改成什么。单处匹配可省略 `occurrence_index`；多处匹配时显式指定从 0 开始的位置。确认只需 `preview_id` 与 `ai_confirmation: true`，返回完整 `final_fields` 和 `rewrite_receipt`。版本、哈希和预览上下文由服务读取保存的快照。

统一入口的字段映射：

| 模块 | 预览 final_fields | 统一 remember_memory |
| --- | --- | --- |
| 情感 | `/original_text`、`/summary` | `content`、`summary` |
| 学习 | `/title`、`/summary`、`/current_understanding` | `title`、`summary`、`content` |

`draft_fields` 与 `final_fields` 的键均使用带 `/` 的完整路径。学习草稿可以只包含标题、摘要与正文：统一学习入口将缺省 `/preceding_context_summary` 与精确空字符串视为等价，不要求 AI 补隐藏空字段。需要非空前置上下文时使用专用学习工具；非空、空白字符串、null 和其他字段仍严格区分。其他字段内容须与已确认预览完全相同，回执只供同一作者、本次写入使用。

人物指向由作者在目标中明确声明，另行人物认证、别名登记和重复绑定已从新流程省去；作者声明与宿主认证分别标记。明确位置的群聊或历史人物同样可预览。省略 receipt 时保持普通保存路径；要改草稿时，重新预览新的完整稿。旧调用显式提供的上下文、版本和哈希仍逐项核对。

## 6. 模块一与按需材料

`stbrain_help(module=...)` 返回静态说明。`stbrain_open` 其余视图用于当前上下文与材料：

| view | 返回内容 |
| --- | --- |
| `summary`（默认） | 当前阶段、实际引用、版本和合法动作摘要 |
| `manual` | 指定模块的说明与当前可呈现材料 |
| `review` | 模块一候选的分页全文 |

manual 支持 `self_revision`、`emotional_memory`、`learning_memory`、`tool_guidance`、`planning_memory`、`self_governance_profile`、`injection_control`、`hallucination_vault`、`shared_person_authoring`。

模块一 review 从 `page=0` 开始，续页使用返回的 `next_arguments` 和实际 `expected_material_hash`。全部页面齐备后才形成完整呈现证明；审核接受与下一真实轮次激活另有明确动作。summary 和静态帮助分别承担导航、说明用途。

编辑前可用 `query_self_model(view="edit_basis")` 取得完整活动五键正文，包括 facets 与来源引用。`view="active"` 用于读取所选活动层。作者自行决定叙述人称，按当前结构填写内容。

`submit_self_model_candidate` 的 submit/revise 载荷为 AI 自写的 `content` 与 `reason`。服务端派生差异、来源绑定与基线；当前步骤和参数来源由 `current_action_contract` 给出。

### 兼容的显式版本参数

下列映射仅用于专用高级工具中确实要求显式版本的兼容调用；普通 `remember_memory`、`revise_memory`、`advance_plan` 不手填模块行版本。简化配置已自动处理的参数保持省略，是否需要参数以当前工具 Schema 和动作回执为准。

| 模块 | 当前材料中的值 | 对应高级参数 |
| --- | --- | --- |
| 自我定义 | 根 `row_version` | `expected_row_version` |
| 情感 | `emotional_memory.row_version` | `expected_emotion_version` |
| 学习 | `learning_memory.learning_row_version` | `expected_learning_version` |
| 工具 | `tool_guidance.tool_row_version` | `expected_tool_row_version` |
| 规划 | `planning_memory.planning_row_version` | `expected_planning_version` |
| 自我治理 | 所选范围的 `scope_versions` | `expected_profile_version` |
| 注入控制 | 所选范围的 `scope_versions` | `expected_control_version` |
| 幻觉黑匣子 | `hallucination_vault.vault_row_version` | `expected_vault_version` |
| 人称校对 | `shared_person_authoring.row_version` | `expected_authoring_version` |

版本与上下文引用取自本次实际返回值；目标引用的版本仍保留并发检查。新一轮使用新的有效上下文，发生冲突时先回读目标。

### 授权配置

在 `simple-memory-v1` 中：

- 模块一首次激活前，四个普通模块只读；激活后，官端直连 MCP 和网关注入模型均可写入、修改普通记忆。
- 读取自我定义免部署密码。已验证的网关执行绑定承担其核心写入身份校验。
- 直连写入、修改模块一时，使用 `authorize_self_model(password)` 取得短期授权，再把返回的真实 `grant_ref` 交给 `stbrain_open_direct` 打开 Direct 上下文。授权期 15 分钟，grant 单次消费。

日常档通过 `stbrain_manage` 选择这些专用操作。身份取决于服务端对本轮调用的实际绑定，不由模型名或文字声明决定；已验证网关调用无需另走密码授权。部署密码是授权凭证，不能作为记忆保存。

兼容配置保留已有授权上下文路径。需要 Direct 上下文的操作使用独立签发的 grant 和实际 `write_context_ref`；具体高级参数参考当前回执。`execution_ref` 由宿主签发，直连调用方省略。

### 成功与失败的工具经验

`recall_tool_guidance(view="experiences", card_id=...)` 读取全部结果类型，包括成功与部分成功。`query` 可为精确经验 ID 或尝试、教训、原因、结果类型中的文本片段；`limit` 最多 5 条，结果的 `total` 与 `truncated` 说明匹配总量和是否截断。原 `failures` 仍排除成功和部分成功，`history` 仍读取卡片版本。

经验保留原文、结果、卡片版本及作者可信度。`ai_reported` / `verified=false` 仍表示作者自报，100 分不自动等于独立核验。`call_notes_current` 是广告目录、参数结构、有效期、活动状态与最近失败等因素的综合诊断，不是保存状态或最后修改时间。

### 新对话第一轮的可选入口提醒

网关按真实消息结构提供 `first_user_turn`，控制面验证其布尔类型。生成前上下文只在第一轮、本轮目录确实含 `stbrain_open`、且总注入开关允许时附加 `st_start_entry`。它标记为可选、无指令权威和无授权权威，模型可以不调用而直接回应；后续轮次和未提供入口的请求不附加这条提示。仅连接 MCP 不经过该网关注入路径。

## 7. 回执和诊断

按调用的真实 `decision`、状态和版本确认结果。版本冲突、权限拒绝、普通参数校验和网关执行绑定错误是不同阶段，使用各自的错误码处理。

当前网关工具参数诊断提供经筛选的工具名、公开字段和校验类型，便于修正参数；失败批次保持未释放。详情见 [网关说明](../rikkahub_gateway/README.md)。

工具调用响应成功写出后开始独立回包计时，`STBRAIN_GATEWAY_TOOL_RESULT_WAIT_SECONDS` 默认 300 秒。完整回包取消计时，新批次重新计时；超时仅尝试安全收尾旧传输等待。仍有运行中的执行登记或控制面未确认关闭时继续保护现场，不重做工具，不伪造结果，不声称外部操作已取消。这不改变正常模型生成时限。

存入内容的凭证检查也覆盖“密码我设成了……”等中文口语写法，在持久化前拒绝真实秘密；没有真实值的流程说明、变量名与存放位置可以保留。

升级在当前工具链结束后进行，刷新客户端 Schema，再以合成内容验证普通查改和完整工具续轮。测试源码、回归结果、部署回执和手机体验分别记录，参见 [UPDATE-VALIDATION](../docs/UPDATE-VALIDATION.md)。

### 历史证据怎样使用

原 public-tools/16（39 工具）的 compact-open 记录对应当时的摘要、按需手册和高级候选流程。它作为历史依据保留；当前普通写入使用本文的简化入口，不能从旧用例推导为所有保存都要先打开上下文或提交候选。

本版新增工具、普通作者修改、统一查询、人称提示和网关反馈分别有对应的合成验证。合成测试记录不是手机成功回执，也不代替真实上游、部署后连接和实际工具续轮验收。最终发布验证数量与范围由本版验收文档记录。
