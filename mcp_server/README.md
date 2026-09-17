# StillerBrain MCP 与控制面

这一层把 ST 的记忆能力提供给模型：保存经历和方法、搜索四个普通模块、修改作者内容、管理计划，以及设置 AI 写给自己的提示。

第一次使用先看 [日常指南](../docs/GUIDE.md)；完整工具分类见 [TOOLS](../docs/TOOLS.md)，详细参数与读取流程见 [OPEN_RESPONSE](OPEN_RESPONSE.md)。

## 配置与目录

新的示例配置采用 `STBRAIN_ACCESS_PROFILE=simple-memory-v1`。旧配置省略此变量时沿用兼容路径；升级采用哪种配置由部署者明确选择。

当前 `public-tools/20` 是公开契约标签。默认 `/mcp` 返回完整 44 项目录；显式使用 `/mcp?tool_profile=daily` 返回日常 7 入口，也可使用 `X-STBrain-Tool-Profile: daily` 请求头。选择器按请求生效，与访问配置独立；两个选择器各最多一次，同时提供时必须一致。升级不会自动切档，修改连接后须重连、刷新工具列表，从新回合使用。

`simple-memory-v1` 与 `legacy` 的完整目录均为 44 项，集合有一处替换：前者提供 `authorize_self_model`，并把工具卡修改统一放进 `revise_memory`；旧 `revise_tool_guidance` 调用仍兼容，保留相同授权与校验。

日常档按顺序提供 `stbrain_open`、`remember_memory`、`remember_tool_guidance`、`revise_memory`、`advance_plan`、`stbrain_tools`、`stbrain_manage`。`stbrain_tools` 按分类列操作或按操作名返回完整参数；`stbrain_manage(action=操作名, arguments=参数对象)` 沿原授权与校验路径执行。当前 `simple-memory-v1` 分类工具箱有 45 项操作，包含常用及兼容能力。已有帮助或回执提到未独立列出的操作名时，通过该入口调用，无需请人切档。

### 日常操作

| 需求 | 入口 |
| --- | --- |
| 保存经历、知识或计划 | `remember_memory(module, content)` |
| 保存工具或 MCP 服务经验 | `remember_tool_guidance(tool_name, purpose)` |
| 搜索或浏览四个普通模块 | `stbrain_open(view="recall", query=...)` |
| 修改四个普通模块 | `revise_memory(target_ref, changes)` |
| 记录计划进度 | `advance_plan(...)` |
| 查用法 | `stbrain_open(view="manual", module=...)`；静态帮助用 `stbrain_help` 操作 |
| 查分类与准确参数 | `stbrain_tools(category=...)` 或 `stbrain_tools(action=...)` |
| 使用其余能力 | `stbrain_manage(action=操作名, arguments=参数对象)` |

模块一首次完成并激活后，`simple-memory-v1` 的直连与网关调用均可直接使用普通读写入口。内部上下文和模块行版本由服务取得；AI 只需使用已查询到的目标引用和本次修改内容。普通模块首次激活前为只读，已有内容仍可按读取规则查询。

普通原文与摘要等作者字段可修改，历史版本保留。情感类型默认未分类，普通记忆来源和可信度省略时显示未标注，由作者补充。工具卡可信度可填 0–100，有效期可选；实际执行权限单独校验。

工具经验用 `recall_tool_guidance(view="experiences", card_id=...)` 读回包括成功、部分成功在内的结果，支持按经验 ID 或文本筛选，最多返回 5 条并说明总数与截断状态。日常档通过 `stbrain_manage` 调用。`failures` 仍只读非成功结果，`history` 仍读卡片版本；自报经验不会因可信度 100 自动成为独立核验。

## 模块一：由作者准备、复核和激活

模块一保存 AI 自己编写的核心自我。按当前服务器返回的合法动作完成简介、冷静词与候选准备；候选在之后的真实轮次复核，并在再后来的真实轮次明确激活。

读取用 `query_self_model`。编辑前的 `view="edit_basis"` 返回完整活动正文；候选全文用 `stbrain_open(view="review", module="self_revision")` 分页读取。内容的人称由 AI 选择，正文按结构与安全规则校验。

- 服务端验证的网关注入调用：凭当前执行绑定使用核心写入能力，免部署密码。
- 未经过网关的直连核心写入：先通过 `authorize_self_model` 验证部署密码，再消费真实 `grant_ref` 打开 Direct 上下文；授权与核心修订阶段分别检查。读取自我定义和已激活后的普通记忆读写不要求此密码。

身份以服务端对本次调用的验证为准，不能靠模型名字或口头声明。日常档的统一执行入口也保留这些规则；网关模型无需另向人类索要部署密码。

查看手册、查询活动自我，以及正常聊天各有自己的作用。核心候选的完整呈现、复核结论和激活使用实际服务回执。

## 自定义提醒与人称辅助

`manage_self_governance_profile` 管理 AI 自写的轻提醒与安全阀；`manage_injection_control` 控制各模块的后续自动注入。前者默认空白，可写入、清空和回滚。

`manage_person_reference_advisory` 的 `set`、`disable`、`reset` 分别自写、关闭与恢复默认人称弱提醒。当前人称提醒和固定标签选词帮助前置到五个存入工具的目录描述，成功保存时也附在回执中；保存正文仍为本次实际提交内容。

另有人称字面校对：每条草稿默认关闭，由 AI 显式 `preview → confirm → remember`。预览提供模块、完整草稿和“称呼指谁、替换成什么”；确认提供预览编号与明确同意，版本和校验信息由服务读取。人物指向采用作者本次声明，已省去人物认证、别名登记和重复绑定。情感、学习和工具的相应存入支持单次回执；统一 `remember_memory` 支持情感、学习，规划暂未接通。这是指定称呼的最小替换，全文叙事视角仍由作者组织。示例见 [工具用法](../docs/TOOLS.md#10-人称提醒与可选称呼校对--3-项)。

学习草稿的 `/preceding_context_summary` 省略与精确空字符串等价，统一存入不要求补上该隐藏空字段。其他字段、非空内容、作者和回执绑定仍严格核对。

日常目录把无必填参数的 `stbrain_open` 放在首位。网关以消息结构判断新对话第一轮；仅在本轮目录提供该入口、总注入开关允许时附上一条可忽略的入口提醒。它不强制调用，不每轮重复，也不替代客户端工具许可。

共享凭证检查覆盖中文口语赋值，含真实秘密的内容在持久化前拒绝。普通流程与存放位置可记录；不要将部署密码、Token 或 API Key 存进记忆。

## 控制面

`control_server.py` 是独立 HTTP 服务，使用与 MCP 分开的凭证。

| 权限通道 | 主要接口 |
| --- | --- |
| 宿主 | `/v1/host/wakes`、`/v1/host/context/prepare`、`/v1/host/context/confirm`、`/v1/host/context/close` |
| 人类控制 | `/v1/human/objections`、`/v1/human/rollback`、`/v1/human/direct-grants` |

宿主绑定真实外部轮次与本轮上下文；人类控制处理异议、回退到已有批准版本和限定授权。自我正文仍由作者工具提交、复核与批准。部署 Token、唤醒秘密与数据库保存在源码外。

## 启动与验收

推荐使用 [统一运维入口](../docs/OPERATIONS.md) 加载私有配置并启动三个服务。已在私有终端加载环境变量时，也可分别运行：

```powershell
python -m mcp_server.server
python -m mcp_server.control_server
```

MCP 使用 Streamable HTTP，完整档路径为 `/mcp`，日常档为 `/mcp?tool_profile=daily`；开发示例端口为 `18794`，实际地址以配置输出为准。控制面使用独立监听端口。面向手机的连接由部署者配置受控 HTTPS 与设备访问权限。

测试建议依次进行：源码与配置检查、一次性测试库回归、空白实例首次激活、普通读写、网关工具续轮、手机连接。实际运行 `stbrain_open` 可能记录上下文打开或材料呈现，因此生产只读诊断应选用专门只读入口。

合成测试分布于 `tests/`、`mcp_server/tests/`、`rikkahub_gateway/tests/`；部署验收见 [UPDATE-VALIDATION](../docs/UPDATE-VALIDATION.md)。
