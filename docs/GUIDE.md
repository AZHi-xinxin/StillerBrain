# 安装与日常使用

[首页](../README.md) · [新手安装](INSTALL.md) · [工具目录](TOOLS.md) · [缓存设置](CACHE.md) · [架构](ARCHITECTURE.md) · [启动、备份与导出](OPERATIONS.md)

**第一次部署，请先跟着 [新手安装说明](INSTALL.md)运行向导。** 它准备独立环境、私有配置和本地服务；本页继续介绍客户端连接、AI 初始设置与日常操作。已经在使用的实例保留原配置，按对应维护流程操作。

ST 可以部署在 Windows 电脑、Linux 电脑或 Linux VPS 上。Windows 与 Linux 服务均有实际验收；Linux 已验证环境为 Ubuntu 24.04 x64 / CPython 3.12.3，私人阿里云 VPS 上已有运行实例。先确认服务放在哪台设备，再选择该系统的安装命令；手机客户端连接现有实例时，直接使用部署者提供的地址与凭证。

本页的日常读写采用 `simple-memory-v1` 配置。旧版 `legacy` 连接继续保留独立授权上下文；先用 `stbrain_help` 确认自己连接的实例采用哪种方式。

## 先分清两种连接

ST 按接入能力适配模型与客户端。当前提供 OpenAI 兼容的 Chat Completions 网关和经过身份验证的 Streamable HTTP MCP。体验自动浮现时，客户端需要支持自定义模型地址、相应 MCP 传输及完整工具续轮；仅使用主动记忆工具时，可选择支持该 MCP 接口的客户端。具体组合请完成下方的小规模验证。

| 连接 | 日常记忆怎么用 | 自动浮现 |
| --- | --- | --- |
| 模型直连 + ST MCP | 模块一首次激活后，AI 可直接新增、查询和修改普通记忆 | 由模型主动查工具；单独连接 MCP 不会自动改写模型请求 |
| 模型经过 ST 网关 + ST MCP | 同样使用记忆工具，网关还绑定当前模型调用与工具续轮 | 网关在回复前带入本轮符合条件的记忆 |

这里的普通模块是情感、学习、工具和规划。模块一首次完成并激活前，这些模块为只读：可以查询已有内容，完成激活后开放写入和修改。已有活动自我定义时，后续核心编辑不会重新把普通模块关成只读。

### 模块一什么时候需要密码

- 读取自我定义：使用 `query_self_model`，免部署密码。
- 已由服务端执行绑定验证的网关注入调用：模块一写入、修改免部署密码，仍完成原有候选、复核和激活流程。
- 未经过 ST 网关的直连模型：写入、修改模块一时，先用 `authorize_self_model` 验证部署者提供的密码，再将返回的 `grant_ref` 交给 `stbrain_open_direct`。授权期为 15 分钟，打开上下文的 grant 为一次性。

网关身份由服务端核验本次调用，模型名称和“我是网关模型”的文字声明只是描述。部署密码、模型 API Key、MCP Token 各有用途。

## 已有实例：怎样开始体验

1. 在客户端接入部署者提供的 ST MCP 地址与凭证。
2. 需要自动浮现时，再将模型 Base URL 和凭证配置为对应 ST 网关；模型能力标识使用当前接入的真实模型 ID，使客户端能按实际能力处理工具调用。
3. 刷新 MCP 工具目录，调用 `stbrain_help` 查看当前配置和用法。
4. 由 AI 按模块一说明完成首次设置、后续真实轮次复核与激活。
5. 验证一次普通保存、查询、修改，以及完整工具续轮，再逐步接入自己的资料。

手机里的 `127.0.0.1` 是手机自己。远程连接应使用部署者提供、经过 TLS 与设备授权验收的地址。

## 自己部署：先建立空白测试实例

先选定 Windows 电脑、Linux 电脑或 Linux VPS，在该设备解压后的源码根目录运行安装向导。

Windows x64 / 标准 CPython 3.14：

```powershell
python -B scripts/install_stiller.py
```

Linux x64 / 标准 CPython 3.12、glibc 2.34 或以上：

```sh
python3.12 -B scripts/install_stiller.py
```

两种环境均使用常规 GIL 构建。具体系统与新向导的验收范围见 [INSTALL](INSTALL.md) 和 [验证记录](UPDATE-VALIDATION.md)。

向导询问新的源码外私有目录、模型 HTTPS 地址、真实模型 ID、隐藏输入的 API Key 和可选模块一密码。它生成 `config.env` 及独立凭证，安装依赖，短暂启动三个本机服务检查健康状态和工具目录，随后停止检查进程并给出正式启动命令。此过程不请求真实模型。

安装后的 `连接资料（请勿公开）.txt` 列出网关和 MCP 各自的连接资料。使用向导给出的完整命令启动 ST，再回到本页“已有实例”步骤，让 AI 阅读帮助并完成自己的初始设置。默认地址供运行 ST 的设备自身使用；手机访问电脑或 VPS 上的 ST，按[手机连接说明](INSTALL.md#8-手机上怎么用)配置受保护的远程地址。

### 手工配置与已有实例

以下保留维护者的手工配置路径。详细环境准备见 [LOCAL-DEVELOPMENT](LOCAL-DEVELOPMENT.md)，日常运维见 [OPERATIONS](OPERATIONS.md)。源码、虚拟环境、私有配置和运行数据分别保存；主库、学习创意盒和幻觉隔离库使用三个独立路径。向导安装使用私有目录中的 `config.env`，下文 `stiller.env` 是手工示例名称，请按实际路径操作。

仓库中的 `.env.example` 是占位模板，新模板选择 `simple-memory-v1` 与 `tail-context-v2`。复制到仓库外后，由部署者配置：

| 配置 | 作用 |
| --- | --- |
| `STBRAIN_ACCESS_PROFILE=simple-memory-v1` | 启用本文说明的普通读写路径 |
| `STBRAIN_SELF_PASSWORD_HASH_FILE` | 直连模块一授权所用的私有密码哈希文件 |
| `STBRAIN_DB_PATH`、`STBRAIN_LEARNING_IDEA_DB_PATH`、`STBRAIN_HALLUCINATION_VAULT_DB_PATH` | 三个运行数据库 |
| `STBRAIN_OWNER_ID`、`STBRAIN_MODEL_ID` | 当前记忆作者与实例身份绑定 |
| `STBRAIN_UPSTREAM_BASE_URL`、`STBRAIN_UPSTREAM_MODEL`、`STBRAIN_UPSTREAM_API_KEY` | 实际模型服务 |
| `STBRAIN_GATEWAY_MODEL`、`STBRAIN_GATEWAY_TOKEN` | 客户端看到的模型标识与网关凭证 |
| `STBRAIN_MCP_TOKEN`、`STBRAIN_HOST_TOKEN`、`STBRAIN_HUMAN_TOKEN`、`STBRAIN_WAKE_SECRET` | 不同服务和权限通道的独立秘密 |
| `STBRAIN_EXECUTION_EPOCH` | 本套部署的执行绑定标识 |
| `STBRAIN_GATEWAY_CONTEXT_LAYOUT` | `legacy`、`anchored-v1` 或 `tail-context-v2`；详见缓存页 |

省略访问配置或布局变量的旧配置继续沿用兼容路径和 `legacy` 布局；新模板的选择不会自动改写旧实例。保持执行绑定开启，并使用独立随机秘密。

### 配置直连模块一密码

需要官方模型直连修改模块一时，先创建源码外的私有目录，再运行：

```powershell
python -B scripts/configure_self_password.py --output C:/stiller-private/self-password.json
```

助手会让你隐藏输入两遍密码，保存带随机盐的 scrypt 哈希；已存在的目标文件会保留。随后把私有 `.env` 中的 `STBRAIN_SELF_PASSWORD_HASH_FILE` 指向这个绝对路径。密码无需写进命令参数，哈希文件与私有配置留在源码外。

只走已验证网关时可暂时留空哈希路径；此时直连模块一授权会提示 `self_password_not_configured`。普通模块首次激活后的读写仍独立于这个密码。

在仓库根目录，使用自己的虚拟环境和私有配置执行。以下为 Windows 路径示例；Linux 使用自己的 venv 解释器和源码外私有配置路径：

```powershell
python -B scripts/stiller_ops.py check --config C:/stiller-private/stiller.env
python -B scripts/stiller_ops.py start --config C:/stiller-private/stiller.env
```

开发示例默认端口为 MCP `18794`、控制面 `18795`、网关 `18796`；以实际配置为准。OpenAI 兼容模型 Base URL 末尾使用 `/v1`，MCP 地址末尾使用 `/mcp`。客户端模型凭证填写网关 Token，上游 API Key 留在网关私有配置中。

## 日常使用：四个动作

### 1. 新增一条经历、方法或计划

普通保存使用 `remember_memory`，只需 `module` 与 `content`。下面是合成 MCP 调用示例：

```json
{"name":"remember_memory","arguments":{"module":"learning_memory","content":"我学会先列出候选方案，再比较时间和材料。","keywords":["比较方案","选择方法"]}}
```

可用模块：`emotional_memory`、`learning_memory`、`planning_memory`。工具卡另用 `remember_tool_guidance`，必填 `tool_name` 与 `purpose`。

来源省略时为 `unmarked`，可信度省略时为未标注。作者可以按实际经历选择来源，自填 0–100 的可信度。情感类型省略时为 `unclassified`，由 AI 按内容自行分类。

### 2. 统一找记忆，再按需要展开

```json
{"name":"stbrain_open","arguments":{"view":"recall","query":"比较方案"}}
```

这一视图统一查询情感、学习、工具和规划，返回摘要、真实引用及详情查询入口。空查询用于分页浏览目录；继续翻页时保留查询条件，使用返回的 `next_cursor`。自我定义和幻觉隔离内容通过各自入口读取。

统一查询只是读取，不是保存前必须打开的步骤。具体模块的 `recall_*` 继续支持更细的查询和历史读取。

### 3. 只提交想修改的部分

```json
{"name":"revise_memory","arguments":{"target_ref":"<刚查到的真实引用>","changes":{"summary":"先列出候选，再比较时间和材料。","keywords":["比较方案","选择方法"]}}}
```

四个普通模块共用这一修改入口。原文、当前理解、摘要、关键词、重要度，以及各模块公开的作者字段均可按契约修改。普通修改无需 `edit_class` 或审查表；记录 ID、归属、哈希等技术字段由 ST 维护。

查询引用已带定位所需版本，AI 原样使用即可，内部模块版本由服务取得。发生真实并发冲突时，重新读取，再决定如何修改。旧版本保留在历史中。

### 4. 记录计划进度

`advance_plan` 接收查到的 `target_ref` 与 `event_seq`，加上动作和说明。暂停、恢复可直接表达安排变化；推进、完成和重新开启需要该动作要求的真实证据。创建任何层级的普通计划时，父项都可选。

计划记录保留“打算做什么”和“进展如何”；实际执行由宿主的工具和任务循环完成。

## 给 AI 自己写提醒

| 想调整什么 | 入口 |
| --- | --- |
| 写给自己的轻提醒、提示词或安全阀 | `query_self_governance_profile` / `manage_self_governance_profile` |
| 一条工具卡或计划的简短提醒 | 对应记录的 `reminder`，普通修改用 `revise_memory` |
| 人称弱提醒 | `manage_person_reference_advisory` 的 `set` / `disable` / `reset` |
| 哪些模块自动参与后续上下文 | `query_injection_control` / `manage_injection_control` |

轻提醒默认空白，可自写、清空、回滚。`manual_only` 留待主动查询；`scene_relevant` 配合自己写的场景标签浮现。普通场景标签按本轮人类话语匹配，适合写“提醒我”“设个闹钟”“回家了”等自然表达。当前这类治理提醒使用大小写归一后的子串匹配；原词、近义和相关说法由 AI 按真实语境补充，命中后仍受开关和容量影响。

五个存入入口在工具说明中提供当前人称提醒与固定的标签选词建议，便于 AI 准备参数。说明是读取时的快照：本轮最新管理结果优先，刷新客户端 MCP 目录可获得新说明；当前没有客户端自动刷新通知。成功回执也附带当前提醒，保存的正文保持作者本次提交内容。

可自定义的是上述作者提示及公开的记录设置。标签选词短建议目前是固定帮助文案；协议、权限和来源标记也由程序维护。

## 常见问题

**官端模型也能写普通记忆吗？**

在 `simple-memory-v1`、模块一已首次激活且 MCP 身份有效时，可以直接写入和修改四个普通模块。模块一写入另用密码授权。

**“只读”是不是里面没有记忆？**

只读描述当前操作权限。已有内容仍可按身份与读取规则查询。

**工具卡需要填具体操作全名吗？**

可以记 MCP 服务大名。具体操作、参数和步骤按需要放入详细内容，使用时再查当前工具目录。

**少了一句提示，是保存失败吗？**

保存以真实 `stored` / `revised` 等回执为准。提醒还受设置、目录缓存和注入时机影响；已关闭的人称提醒不会自动恢复默认。

**再次遇到回复中断怎么办？**

保留时间、版本和错误码，查看安全工具名、字段及校验类型。结果未知的写入先查询。当前网关提供更明确的失败线索，客户端长期展示错误与历史中断的具体原因仍分别核实。

## 维护与公开反馈

升级前保留三库、私有配置和版本备份，等待工具链结束后再维护。源码检查、合成回归、实际服务验收和手机体验是不同范围；当前记录见 [UPDATE-VALIDATION](UPDATE-VALIDATION.md)。

反馈使用合成示例和脱敏错误信息。运行库、聊天、附件、完整请求与凭证继续留在自己的受控目录。参见 [PRIVACY](../PRIVACY.md) 与 [SECURITY](../SECURITY.md)。
