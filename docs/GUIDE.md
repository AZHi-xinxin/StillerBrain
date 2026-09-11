# 安装、使用与架构详解

[返回首页](../README.md) · [统一启动与备份导出](OPERATIONS.md) · [完整工具目录](TOOLS.md)

本页保留前一预览版已校对的底层接入与使用指南。逐组件命令继续可用；本次新增的简化运维入口见 OPERATIONS，检索变化见 RETRIEVAL。历史验证数字见文中对应范围，本次结果见 UPDATE-VALIDATION。

## 三、安装与接入

### MCP、网关与 Direct 分别做什么

只连接 MCP 时，持有效 MCP 凭证可以读取 `stbrain_help`、服务健康和 `query_self_model(view="status")` 等信息；具体记忆读取仍遵守模块开通、身份和披露条件。MCP 凭证本身不代表当前写入授权，也不会自动把记忆注入模型请求。当前提供的自动接入路径由兼容网关、控制面和 MCP 服务配合完成。

源码还提供 Direct 路线：独立人类控制通道签发短时、一次性且限定范围的授权，AI 用 `stbrain_open_direct` 消费授权并取得对应的写入上下文，再按工具规则写入。Direct 需要单独完成宿主适配与验收；它保留首次采纳、模块就绪及各自复核条件，由 AI 完成采纳，并与自动注入证据分开。

### 先准备一个空白测试实例

当前发布记录的验证基线是 **Windows x86_64 / CPython 3.14.6 / SQLite**。完整依赖锁对应 Windows / CPython 3.14；其他平台需要各自的依赖与兼容性验证。

准备这些东西：

- 一份源码，以及位于源码目录外的虚拟环境和私有配置目录。
- 三个全新的数据库路径，用来保存主记忆、学习创意盒和隔离记录。
- 一个支持原生工具调用的上游模型服务及自己的 API Key。
- 一个能够连接 MCP Streamable HTTP、使用 OpenAI 兼容模型接口的宿主；工具续轮兼容性也需要验证。

有部署经验的人可以按下面的步骤操作。第一次接触后端部署时，可以把本节和[完整开发指南](https://github.com/AZHi-xinxin/StillerBrain/blob/main/docs/LOCAL-DEVELOPMENT.md)交给协助你部署的人；部署完成后，日常使用主要在聊天前端进行。

### 1. 检查源码与安装依赖

以下命令以仓库根目录为当前目录。先检查原样发布包，再创建仓库外的虚拟环境：

```powershell
python -B scripts/check_source_package.py
python -m venv ../stiller-dev-venv
../stiller-dev-venv/Scripts/Activate.ps1
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m pip --isolated install --index-url https://pypi.org/simple --require-hashes -r requirements-windows-py314.lock
python -m pip check
```

源码包检查对照发布时的文件清单与哈希。修改 README 或源码后，下一次分发需要同步清单并重新验证。虚拟环境、运行库和日志都保留在源码分发目录之外。

### 2. 填写私有配置

把仓库中的 `.env.example` 复制到仓库外的私有目录，例如自己新建的 `C:/stiller-private/stiller.env`。逐项替换示例占位内容；三个数据库使用三个不同的绝对路径。

| 配置项 | 作用 |
| --- | --- |
| `STBRAIN_DB_PATH` | 主数据库：自我、经历、知识、工具经验、计划及相关版本和审计 |
| `STBRAIN_LEARNING_IDEA_DB_PATH` | 学习创意盒的附属数据库 |
| `STBRAIN_HALLUCINATION_VAULT_DB_PATH` | 幻觉隔离库 |
| `STBRAIN_UPSTREAM_BASE_URL` | 实际模型服务的 HTTPS 地址 |
| `STBRAIN_UPSTREAM_MODEL` | 上游模型的真实标识 |
| `STBRAIN_UPSTREAM_API_KEY` | 上游模型服务提供的 API Key |
| `STBRAIN_GATEWAY_MODEL` | 网关向聊天客户端提供的模型标识 |
| `STBRAIN_GATEWAY_TOKEN` | 聊天客户端访问网关时使用的凭证 |
| `STBRAIN_MCP_TOKEN` | MCP 工具入口的凭证 |
| `STBRAIN_HOST_TOKEN` | 可信宿主使用控制面的凭证 |
| `STBRAIN_HUMAN_TOKEN` | 独立人类控制通道的凭证 |
| `STBRAIN_WAKE_SECRET` | 内部轮次与执行绑定所用的签名秘密 |
| `STBRAIN_EXECUTION_EPOCH` | 这一套部署共用的新部署标识 |
| `STBRAIN_OWNER_ID` | 记忆作者的身份标识 |
| `STBRAIN_MODEL_ID` | 这一实例使用的 AI 身份绑定标识，与上游模型名称分开 |
| `STBRAIN_HUMAN_ACTOR_ID` | 独立人类控制通道的参与者标识 |

五个本地秘密，即 Gateway、MCP、Host、Human Token 和 Wake Secret，分别使用至少 32 字符的独立随机值；上游 API Key 单独保存。三个服务共用同一份一致的配置，并保持 `STBRAIN_REQUIRE_EXECUTION_BINDING=1`。

配置格式是 UTF-8 的 `STBRAIN_NAME=value`，值直接填写。该启动器使用简单配置格式；引号包装、变量插值、重复键和多行值不在支持范围内。私有配置和备份按同等级保护，Windows 请检查目录的 NTFS 访问权限。

### 3. 运行合成测试

```powershell
python -B -m unittest discover -s tests -q
python -B -m unittest discover -s mcp_server/tests -q
python -B -m unittest discover -s rikkahub_gateway/tests -q
python -B -m unittest discover -s packaging_tests -q
```

这些测试使用合成数据。执行它们时保持真实记忆库和真实模型请求在测试范围之外。

### 4. 启动三个组件

在三个独立终端中启用同一虚拟环境，并保持仓库根目录为当前目录。把下面的配置路径换成自己的实际路径：

```powershell
# 终端一：控制面
python -B scripts/run_component.py --config C:/stiller-private/stiller.env --component control
```

```powershell
# 终端二：MCP 服务
python -B scripts/run_component.py --config C:/stiller-private/stiller.env --component mcp
```

```powershell
# 终端三：兼容网关
python -B scripts/run_component.py --config C:/stiller-private/stiller.env --component gateway
```

启动前可在每条命令末尾加 `--check`，先核对配置。此开发启动器只监听 `127.0.0.1`，开机启动、守护运行和远程接入另行部署。

### 5. 在同机聊天客户端中填写

以下是 `.env.example` 使用的开发端口。调整端口后，同步修改相关地址。

| 客户端里的位置 | 同机测试填写内容 |
| --- | --- |
| 模型供应商类型 | OpenAI 兼容接口 |
| 模型 Base URL | `http://127.0.0.1:18796/v1` |
| 模型 API Key 输入栏 | 私有配置中的 `STBRAIN_GATEWAY_TOKEN` 值 |
| 模型名称 | 私有配置中的 `STBRAIN_GATEWAY_MODEL` 值 |
| MCP 传输类型 | Streamable HTTP |
| MCP 地址 | `http://127.0.0.1:18794/mcp` |
| MCP 鉴权 | `Authorization: Bearer <你自己的 STBRAIN_MCP_TOKEN>`，或客户端等效的 Token 输入框 |

上游模型 Key 由网关服务保存；客户端模型栏填写的是网关 Token。控制面使用端口 `18795`，由可信网关/宿主和独立人类控制通道连接。

**手机的 `127.0.0.1` 指手机自己。** 从手机访问电脑服务，需要单独设置并验收受控隧道、TLS 和设备授权。当前示例是同机开发配置，不能直接作为手机或公网地址。

本版 MCP 入口提供 Streamable HTTP。上面的 Python 命令负责启动 HTTP 服务，并非 stdio 客户端的启动配置。

### 6. 第一次体验

新建一个测试聊天窗口，先验证普通聊天，再请 AI 调用 `stbrain_help` 了解工具。接着验证一次真实工具调用及其续轮，确认前端能完整保留调用与结果。

AI 可以根据需要使用 ST。首次部署时，由 AI 自己阅读说明、形成内容并明确采纳，完成核心首次流程，使记忆模块就绪。按 `stbrain_help` 和模块手册核对状态；如果收到 `module_one_required` 等回执，先完成对应的开通或恢复流程。已有活动基础上的正常核心编辑可以继续日常记忆。部署和验证完成后，再安排旧资料迁移。

## 四、日常怎么用

### 普通使用者：用自然语言交流即可

你可以这样表达需求，AI 会结合当前上下文和工具规则决定如何处理：

| 想做的事 | 可以怎么说 | 常用工具 |
| --- | --- | --- |
| 留下一段经历 | “今天这件事，你愿意的话可以记下来。” | `remember_memory` |
| 找回以前的事 | “我们之前聊过那次散步吗？” | `recall_emotional_memory` |
| 找回方法 | “上次比较方案的方法是什么？” | `recall_learning_memory` |
| 继续一项计划 | “看看整理笔记那件事进行到哪一步了。” | `recall_planning_memory`、`advance_plan` |
| 整理检索信息 | “这条记录的摘要能再清楚一点吗？” | `revise_memory` |
| 查看工具经验 | “做这件事前，看看之前有没有相关工具的经验。” | `recall_tool_guidance` |

日常新增支持三个模块：`emotional_memory`（经历与情感）、`learning_memory`（知识与方法）、`planning_memory`（计划）。在模块已就绪、当前网关会话绑定有效且满足写入条件时，普通新增一次调用即可，工具的必填参数为 `module` 和 `content`。基础初始化与授权条件仍按所连接实例的回执处理。

### 一起使用时，可以把这些选择留给他

- **把经历讲清楚。** 提供发生了什么、有哪些资料和哪些地方还不确定，让他基于实际内容决定是否记录。
- **一起讨论怎么记。** 可以交流这件事为什么重要，让他形成自己的正文、摘要和标签。
- **给重要改变留出时间。** 先前的判断可以重新讨论，核心修改沿真实的后续轮次阅读、复核和确认。
- **根据需要调整参与方式。** 由 AI 在工具支持的流程内维护记忆和自动注入设置；用量、权限和版本条件继续由程序检查。

### 想让记忆库暂时安静下来时

AI 可以通过注入控制工具申请调整自动参与后续上下文的模块。全局紧急关闭从下一次真实唤醒生效；恢复或放宽限制按对应流程确认。已经发送给模型的本轮内容仍在该轮上下文中。

人类也可以在前端选用**不经过 ST 网关的供应商配置**，由那条独立路径进行模型对话。自动注入、已保存的数据和主动 MCP 读取分别管理；主动调用读取工具时，相应内容仍会返回宿主。

### 怎么判断真的保存了

查看工具真实回执。`stored` 表示已存入；候选、等待复核或拒绝则有各自状态。AI 的一句“我记住了”适合作为交流表达，保存结果以实际工具回执为依据。

回执缺失或请求中断时，先查询当前状态，再决定是否重试；修改使用查询返回的精确引用和版本。这样可以减少重复记录，也能保留正确的变化顺序。

### 给 AI 与开发者的调用示例

下面是 MCP `tools/call` 的 `params` 对象，`name` 表示工具名，`arguments` 表示参数。示例内容全部为虚构演示；JSON 粘入普通聊天框后仍是聊天文字，实际调用由宿主执行。

读取使用帮助：

```json
{"name":"stbrain_help","arguments":{}}
```

保存一条转述的经历：

```json
{
  "name": "remember_memory",
  "arguments": {
    "module": "emotional_memory",
    "content": "对方告诉我，今天完成了一次周末散步，想把这个小小的好消息记下来。",
    "title": "一次周末散步",
    "emotion": "joy",
    "source_basis": "reported",
    "keywords": ["散步", "周末"]
  }
}
```

保存一条方法或经验：

```json
{
  "name": "remember_memory",
  "arguments": {
    "module": "learning_memory",
    "content": "本次合成演示的方法是：先列出三个候选方案，再比较各自需要的时间和材料。",
    "title": "先列候选再比较",
    "kind": "strategy",
    "source_basis": "reported",
    "confidence": 50
  }
}
```

登记一个普通计划：

```json
{
  "name": "remember_memory",
  "arguments": {
    "module": "planning_memory",
    "content": "下次整理练习笔记时，检查示例是否有清楚的步骤。",
    "title": "检查练习笔记",
    "kind": "task",
    "track": "internal"
  }
}
```

查找经历摘要、浏览学习目录、查询计划与工具经验：

```json
{"name":"recall_emotional_memory","arguments":{"query":"周末散步","limit":5,"include_originals":false}}
```

```json
{"name":"recall_learning_memory","arguments":{"view":"inventory","query":"","limit":20,"offset":0}}
```

```json
{"name":"recall_planning_memory","arguments":{"query":"练习笔记","limit":10,"include_history":true}}
```

```json
{"name":"recall_tool_guidance","arguments":{"query":"查询天气","view":"suggestions","limit":3}}
```

学习记忆的 `inventory` 用于按页浏览目录；查某个话题时使用相关性搜索。搜索没有命中只表示这次查询没有返回匹配项。完整目录需要按回执分页读取。

修订摘要的示例：

```json
{
  "name": "revise_memory",
  "arguments": {
    "target_ref": "learning://learn_00000000000000000000000000000001@1",
    "changes": {
      "summary": "先列候选方案，再比较时间和材料。",
      "keywords": ["比较", "方案", "时间"]
    },
    "reason": "让摘要更清楚，便于下次按主题查找。"
  }
}
```

暂停计划的示例：

```json
{
  "name": "advance_plan",
  "arguments": {
    "target_ref": "plan://plan_00000000000000000000000000000001@1",
    "expected_event_seq": 0,
    "event_type": "pause",
    "note": "合成演示：安排改变，等待确定新的整理时间。"
  }
}
```

最后两个示例中的引用和序号是合成占位值。实际修改前，将查询返回的学习 `item_ref` 或计划 `plan_ref` 原样填入 `target_ref`；计划的 `expected_event_seq` 使用同次查询返回的 `event_seq`。推进、完成或重新开启计划需要实际证据；暂停和恢复的参数要求按当前工具契约处理。

普通修订主要用于标题、摘要和检索信息。涉及原意、可信状态、权限或核心自我的重要变化，使用对应的高级流程。完整合法参数始终以所连接版本的工具目录为准。

## 六、架构与关键流程

### 三个服务怎样配合

```text
聊天前端 ── 模型请求 ──→ 兼容网关 ──→ 你配置的模型服务
   │                       │
   └── MCP 工具调用 ──→ MCP 服务
                           │
                       ST runtime
                           │
               主库 / 学习创意盒 / 隔离库

可信网关或宿主 ──→ 控制面：真实轮次、上下文快照、工具执行绑定
独立人类权限   ──→ 控制面：异议、有限回退、Direct 授权
```

“宿主”是负责接收真实消息、触发模型和传递工具结果的客户端或适配层。网关负责把 ST 的上下文机制接到模型请求中；MCP 负责提供模型可以调用的记忆工具；控制面负责可信的轮次与执行凭据。

| 层 | 主要职责 |
| --- | --- |
| Runtime | 记忆规则、版本、候选状态、存储、召回与审计 |
| MCP 服务 | 工具目录、参数校验、上下文绑定、操作回执 |
| 控制面 | 真实轮次签发、上下文快照准备与确认、权限分工 |
| 兼容网关 | 模型请求适配、上下文注入、原生工具续轮校验 |
| 宿主 / 前端 | 用户交互、模型选择、工具结果传输、现实操作授权 |

### 日常新增：简短的保存路径

```text
AI 形成一条要保存的内容
        ↓
remember_memory(module, content)
        ↓
校验当前绑定与模块条件 → 返回 stored 和记录引用
        ↓
以后按问题或精确引用召回
```

标题、摘要和关键词方便组织，正文按传入内容保存。来源默认 `reported`，表示转述；可信度表达当前评估，后续可以根据新依据修订。

### 核心自我：给变化留下复核空间

```text
真实轮次 A：提交候选
        ↓
后来的真实轮次 B：完整阅读材料，独立复核
        ↓
再后来的真实轮次 C：校验当前版本与状态，激活候选
```

同一轮里的多次工具调用、自动续轮和重试共享这一轮的身份。反复调用 `stbrain_open` 也沿用宿主签发的真实轮次。

候选材料分页时，按返回参数读取到全部材料已呈现，保持材料hash与版本一致。日常新增、核心自我和各模块高级变更分别遵守自己的流程。

### 为什么使用版本和来源

引用和版本帮助工具准确定位“刚才读到的那一份内容”。出现并发修改时，重新读取当前状态，再决定如何调整，可以避免用旧理解覆盖新记录。

原文、后来的解释、当前判断与修改历史分别保留。重要操作会检查真实轮次、已读材料和允许的动作，让“已经保存”“已经审阅”“已经激活”有可核查的区别。

### 源码目录

```text
StillerBrain/
├─ runtime/             记忆规则、状态与存储
├─ mcp_server/          工具服务、公开契约、控制面及相关服务
├─ rikkahub_gateway/    兼容模型网关与工具续轮适配
├─ schemas/             数据结构约定
├─ tests/               运行时测试
├─ packaging_tests/     打包与分发检查
├─ scripts/             开发启动、检查等辅助脚本
├─ docs/                架构、开发、缓存、依赖与发布文档
├─ .env.example         私有配置的占位模板
├─ LICENSE              正式许可证
└─ SOURCE-PACKAGE-MANIFEST.json  发布文件与哈希清单
```

普通学习记忆在主库中；学习创意盒使用附属库。幻觉隔离库独立保存警示与隔离内容，按自己的规则读取和恢复。

## 七、验证与维护

### 已公开的验证记录

`0.1.0-preview.1` 的发布记录包含 **881 项合成回归**：runtime 406、MCP 178、gateway 275、打包 22；还记录了新环境的三服务启动、权限隔离和 43 项工具目录检查，付费模型请求为 0。

这是这一发布基线的验收记录。新机器、新客户端、改动后的源码，以及真实使用体验，需要各自的验证。依据见[源码发布状态](https://github.com/AZHi-xinxin/StillerBrain/blob/main/source-release-state.json)与[依赖记录](https://github.com/AZHi-xinxin/StillerBrain/blob/main/docs/DEPENDENCIES.md)。

### 三种检查分别看什么

| 检查 | 说明 |
| --- | --- |
| `scripts/check_source_package.py` | 原样源码包的文件集合与记录哈希 |
| 四组 `unittest` | 运行时、MCP、网关与打包规则的合成回归 |
| `scripts/check_release.py` | 保留更严格的生产就绪门禁 |

源码分发验收与生产就绪是不同范围。生产状态文件保留尚未完成的条件；源码预览发布并不自动满足这些条件。见[发布检查表](https://github.com/AZHi-xinxin/StillerBrain/blob/main/docs/RELEASE-CHECKLIST.md)。

### 升级和旧资料迁移

先保留一致的数据库、配置和版本备份，在独立测试实例中完成升级与恢复演练，再安排正式迁移。SQLite 附属文件、学习创意盒和隔离库都属于需要考虑的备份范围。

旧聊天、日记或文档可以作为迁移素材。真正进入 ST 时，由 AI 阅读、理解并决定如何采纳；先小批量试验，再检查读取和保存回执。数据库升级、源码版本回退和内容修订是三件不同的事情，分别按经过验证的方案处理。

可以按旧资料所在的位置安排：

- **以前主要在聊天窗口里交流：** 使用原服务可用的导出功能保留记录，把选定资料交给接入 ST 的 AI 阅读，再由他完成初始采纳与各模块记录。
- **以前已经使用别的记忆库：** 保留旧库备份与可用导出，让他比较后决定迁移重要主线、部分内容，还是安排更完整的分批迁移。
- **前端已经写有提示词：** 把这些内容作为资料与他一起阅读，供他理解、讨论和决定哪些部分适合采纳进自我模型。
- **资料数量很多：** 其他 agent 可以协助整理格式和批次；正式写入使用经过验证、符合身份与授权规则的工具或导入路径，保留来源、版本和真实回执。

迁移节奏由资料的可导出程度、工具支持与核对结果决定。先搬一小部分并确认读写顺畅，再扩大范围，会更容易保留这些记录的来处。

停止服务前，等待当前工具链结束。对中断中结果未知的写入，先查真实状态，再决定后续动作。

## 八、缓存、费用与使用边界

### 缓存优化在做什么

供应商缓存通常与相同输入前缀的复用有关。ST 的优化目标，是让自身注入布局更稳定，减少重复输入中的无谓变化。

当前公开版提供两种布局：

| 布局 | 使用方式 |
| --- | --- |
| `legacy` | 默认布局，作为保守起点 |
| `anchored-v1` | 宿主通过兼容验证后显式启用；把稳定帧和本轮动态帧按约定分开安排 |

一次生成内，宿主应冻结已经发送的历史、工具目录以及时间、电量等动态占位符展开。工具续轮沿用同一上下文快照，这也是严格历史绑定的重要部分。[缓存说明](https://github.com/AZHi-xinxin/StillerBrain/blob/main/docs/CACHE.md)记录了具体条件。

单纯在历史末尾换一个话题，与重写前面的输入内容，是两种不同变化。实际费用还受模型、输入输出长度、工具结果和供应商缓存行为影响。

衡量效果时，比较供应商提供的输入、缓存命中输入、未命中输入和输出用量。汇总命中率使用“总缓存命中输入 ÷ 总输入”；缺失字段标记未知。公开预览版尚无真实聊天节费结论，具体改善以自己的实测为准。

### 常见问题

**换窗口后能找回记忆吗？**  
在同一身份绑定、同一 ST 实例和有效接入条件下，模型可以按需查询已有记录。自动注入受预算与披露规则约束，更完整的材料可通过工具读取。

“找回已经保存的长期记录”和“直接接上上一窗口最后一句”是两层能力。当前短期材料按同一身份与会话标识关联；宿主维持同一会话连续性时，它们可以帮助衔接近期交流。新窗口使用新的会话标识时，上一窗口的短期内容需要另行处理，不能据此承诺自动接续。

**换模型后会怎样？**  
数据库承载已保存内容，模型能力决定它如何理解和使用这些内容。模型身份、宿主绑定与工具协议要按部署方案配置和验证。

**所有内容都会送给模型吗？**  
ST 选择与当前语境相关、符合披露条件的内容，并控制自动注入预算。主动工具读取也会把相应结果返回宿主；数据去向需要连同前端和模型服务一起考虑。

**可以离线使用吗？**  
存储可以部署在本机；模型调用是否离线取决于所选模型和宿主配置。使用远程模型时，被选中的上下文会发送给该服务。

**计划保存后会自动替我干活吗？**  
ST 负责计划、进度与证据的持续记录。真正执行由宿主的任务循环、工具和权限系统完成，长期自主运行需要这些部分共同支持。

**兼容所有前端吗？**  
接入重点是原生工具调用、真实轮次绑定和完整工具结果传输。每种前端需要实际验证历史截断、重排、续轮和中断恢复等行为。当前开发网关按单实例设计，多实例共享与水平扩容需要另行设计。

**“幻觉隔离”能保证记录绝对正确吗？**  
隔离库提供单独保存、警示和审阅恢复的机制。内容判断由 AI 作出，真实性仍需要来源、证据和持续核查。

## 九、数据、权限与隐私

记忆正文、版本与修订信息保存到部署者指定的数据库。项目发布的是源码，使用者自行管理实例；使用 ST 无需把私人记忆交给项目维护者。

MCP 读取结果会返回宿主，网关选中的记忆和对话会进入所配置模型的请求。部署前，请逐层确认前端、代理、网关、模型服务与日志设施的数据处理方式。

Host Token、Human Token 和内部签名秘密保留在相应可信通道。AI 能读写的记忆，与外部账号、文件、手机操作的授权分别管理。工具经验卡记录方法与风险，实际操作仍遵守宿主权限。

软件许可针对代码；个人记忆、附件、聊天和身份资料保留自己的权利与处理边界。公开反馈时，使用合成复现和脱敏信息，私有配置、数据库、凭证和完整请求留在受控位置。

更多说明见[数据与隐私](https://github.com/AZHi-xinxin/StillerBrain/blob/main/PRIVACY.md)与[安全说明](https://github.com/AZHi-xinxin/StillerBrain/blob/main/SECURITY.md)。

## 十、参与、授权与后续方向

### 参与改进

欢迎在许可范围内学习、修改、测试与提交改进建议。普通问题可以通过 [Issues](https://github.com/AZHi-xinxin/StillerBrain/issues) 反馈；附上版本、客户端类型、错误类别与最小合成复现步骤，会更方便定位。

贡献代码前请阅读[贡献指南](https://github.com/AZHi-xinxin/StillerBrain/blob/main/CONTRIBUTING.md)。安全问题按 SECURITY 中的流程处理。第三方依赖、独立客户端和外部贡献继续保留各自权利。

### 非商业许可与商业授权

正式许可为未经修改的 **PolyForm Noncommercial 1.0.0**。个人非商业使用、修改及许可允许的分发可免费进行，并遵守许可证的通知等条件。

原许可也明确允许慈善组织、教育机构、公共研究组织、公共安全或卫生组织、环境保护组织、政府机构的使用，并有相应资金来源条款。这些允许范围完整保留。

把 ST 或修改版用于超出上述许可范围的商业 AI 产品、SaaS、商业 API 或其他商业场景时，请先申请单独书面授权。商业授权联系昕昕：在仓库 Issue 提交“商业授权申请”，简述版本和用途，再由维护者确认私密沟通渠道。实际授权以单独书面协议为准。

本节是阅读指引，准确条款以 [LICENSE](https://github.com/AZHi-xinxin/StillerBrain/blob/main/LICENSE) 和[商业授权说明](https://github.com/AZHi-xinxin/StillerBrain/blob/main/COMMERCIAL-LICENSE.md)为准。由于许可限制商业用途，项目采用 source-available 分类；[OSI 开源定义](https://opensource.org/osd)要求允许包括商业在内的使用领域。

### 后续方向

以下是可继续讨论和验证的方向，具体进度随版本更新：

- 更友好的安装与配置体验。
- 更多客户端和模型组合的兼容性验收。
- 更易理解的使用说明、状态回执与问题定位方式。
- 稳定上下文、真实用量观察及有证据的缓存优化。
- 与独立前端协作，提供按需查看、可追溯理解与有边界的操作体验。

## 文档索引

| 文档 | 适合什么时候看 |
| --- | --- |
| [ARCHITECTURE](https://github.com/AZHi-xinxin/StillerBrain/blob/main/docs/ARCHITECTURE.md) | 理解结构和设计边界 |
| [LOCAL-DEVELOPMENT](https://github.com/AZHi-xinxin/StillerBrain/blob/main/docs/LOCAL-DEVELOPMENT.md) | 建立全新开发实例 |
| [CACHE](https://github.com/AZHi-xinxin/StillerBrain/blob/main/docs/CACHE.md) | 检查上下文布局与宿主兼容条件 |
| [DEPENDENCIES](https://github.com/AZHi-xinxin/StillerBrain/blob/main/docs/DEPENDENCIES.md) | 查看依赖与验证平台 |
| [RELEASE-CHECKLIST](https://github.com/AZHi-xinxin/StillerBrain/blob/main/docs/RELEASE-CHECKLIST.md) | 了解源码分发与生产门禁 |
| [PRIVACY](https://github.com/AZHi-xinxin/StillerBrain/blob/main/PRIVACY.md) | 确认数据去向与备份边界 |
| [SECURITY](https://github.com/AZHi-xinxin/StillerBrain/blob/main/SECURITY.md) | 安全部署与漏洞反馈 |
| [CONTRIBUTING](https://github.com/AZHi-xinxin/StillerBrain/blob/main/CONTRIBUTING.md) | 参与代码和文档贡献 |
| [COMMERCIAL-LICENSE](https://github.com/AZHi-xinxin/StillerBrain/blob/main/COMMERCIAL-LICENSE.md) | 了解商业授权申请 |
| [SOURCE-PROVENANCE](https://github.com/AZHi-xinxin/StillerBrain/blob/main/SOURCE-PROVENANCE.md) | 查看源码来源说明 |
| [THIRD_PARTY_NOTICES](https://github.com/AZHi-xinxin/StillerBrain/blob/main/THIRD_PARTY_NOTICES.md) | 查看第三方权利提示 |

---

ST 的工作，是让留下的内容有来处，让后来的理解有依据，让重要的改变有过程。
