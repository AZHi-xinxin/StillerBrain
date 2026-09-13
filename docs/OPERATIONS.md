# 本机启动、备份与导出

`scripts/stiller_ops.py` 把三个服务的启动，以及私人数据的离线维护集中到一个入口。它面向单机、自用的 ST 实例；已有的独立凭证、严格执行绑定和三个组件分工都继续生效。

**第一次安装，从 [新手向导](INSTALL.md)开始。** 安装检查通过后，复制向导给出的完整启动命令：它使用私有 `venv` 中的 Python，通过 `scripts/install_stiller.py --start` 启动该实例。Windows 命令粘贴到 PowerShell；运行期间保留终端，按 Ctrl+C 停止本次服务。请保留创建该安装时的源码文件夹及其位置。

下文保留手工运维命令，路径均为示例。向导用户请把 `--config` 后的路径替换为自己的 `<私有安装目录>/config.env`，并使用该目录 `venv` 中 Python 的完整路径，或先激活这个环境；在 ST 源码根目录运行。手工部署的环境准备与配置见[完整指南](GUIDE.md)。

向导把三个数据库放在私有安装目录中。备份另选一个并列目录，例如安装在 `C:/ST-private`，备份放在 `C:/ST-backups/snapshot-001`；保持备份位于源数据库所在目录之外。

## 1. 文件分开放

手工配置可以采用这样的私人目录结构（向导安装的实际文件位置见 [INSTALL](INSTALL.md#5-看见安装完成)）：

```text
C:/stiller-private/
  stiller.env              私有配置，只由本机维护
  data/                    三个正在使用的数据库
  backups/                 完整备份，每次使用新子目录
  restores/                恢复演练，每次使用新子目录
  exports/                 按需导出的私人内容
```

先创建这些父目录，备份和恢复命令会创建最后一级的新目录。三个数据库路径分别来自：

| 配置项 | 备份中的文件 | 保存的范围 |
| --- | --- | --- |
| `STBRAIN_DB_PATH` | `main.sqlite3` | 主库内的记忆、模块记录及相关运行状态 |
| `STBRAIN_LEARNING_IDEA_DB_PATH` | `ideas.sqlite3` | 灵感盒数据库 |
| `STBRAIN_HALLUCINATION_VAULT_DB_PATH` | `vault.sqlite3` | 幻觉保险箱数据库 |

程序要求私人配置、数据库和输出均位于源码目录之外；备份还必须位于三个源数据库所在目录之外。恢复目录和导出文件也要放在备份目录之外。这样源码包和私人资料各自独立。

**备份与导出包含私人数据，保存在自己控制的磁盘上。** 它们不应放进 GitHub、公开交付文件夹或自动同步的公共目录。脚本尽力设置文件权限；Windows 的 `chmod` 不等同于设置最小 NTFS ACL，请自行检查父目录的 Windows 安全权限。需要防止磁盘丢失后被读取时，使用自己的磁盘加密或加密备份方案。脚本本身生成的是未加密文件。

## 2. 一个终端检查并启动

向导用户日常启动可以直接使用安装结束时输出的完整命令。下面是独立运维入口的配置检查与启动方式；把环境和 `--config` 路径换成自己的安装。

检查私有配置：

```powershell
python -B scripts/stiller_ops.py check --config C:/stiller-private/stiller.env
```

成功时会输出一行 JSON，其中 `valid_components` 为 `3`。`database_files_present` 表示三个配置路径上的文件是否都存在；首次使用时可以为 `false`。这一步检查配置格式、独立凭证、绑定开关、监听地址和组件关系，**不会启动服务，也不等于健康检查通过**。

接着启动：

```powershell
python -B scripts/stiller_ops.py start --config C:/stiller-private/stiller.env
```

同一个终端会启动并监督控制面、MCP 服务和兼容网关。启动时会先确认三个配置端口空闲；三个组件仍只绑定 `127.0.0.1`。终端保持打开，服务日志会显示在这里。脚本不额外保存日志文件；分享终端日志前仍应检查其中是否有私人信息。

按 **Ctrl+C** 结束本次启动的三个子进程。一个子进程提前退出时，其余由本命令启动的子进程也会停止，并给出失败提示。它不会查找、终止其他实例的进程。

Windows 上，服务先以挂起状态创建，加入本次专属的 Job Object 后才开始执行；退出时连同其解释器子进程一起回收，并检查进程树结束与监听端口释放。其他平台使用直接子进程管理，仍需在自己的系统上单独验收。

这是一组前台服务，适合本机开发与个人使用。它不安装开机服务、不设置远程访问，也不改变原有身份授权流程。出现“三个进程已启动”的提示后，还需要按[完整指南](GUIDE.md)检查各组件是否就绪并完成客户端连接。

`check`、`start` 和 `backup` 共用原有配置校验，因此都需要一份完整有效的实例配置，包括现有的上游字段。检查与备份不会请求模型；统一启动本身也不会发起聊天生成。

## 3. 停服后备份

先停止该实例的三个服务，以及会写入这些数据库的脚本、管理工具、后台任务和其他 ST 实例。等待已经开始的工作完成，再进行备份。使用统一启动入口时，先在它的终端按 Ctrl+C，确认结束。

```powershell
python -B scripts/stiller_ops.py backup --config C:/stiller-private/stiller.env --output C:/stiller-private/backups/snapshot-001 --confirm-stopped
```

`--confirm-stopped` 表示你确认已经停止**所有写入者**，不是自动停服开关。程序会额外检查三个配置端口没有服务监听，并在三个源数据库上持有 SQLite 写入预留锁，随后使用 SQLite backup API 复制已提交的数据，包括 WAL 中已提交的内容。

### 跨库一致性的前提

三个库分别取得锁、分别复制。**SQLite backup API 的单库快照不是三个数据库的跨库原子事务。** 停止所有写入者、等待它们的已有操作结束，是得到同一停服状态下三库备份的必要前提；端口检查和预留锁是额外保护，无法证明未配置的其他写入者已经退出。如果业务操作在备份开始前已经只完成一半，备份工具也不能替它完成或修复。

缺少任一数据库、端口仍被占用、数据库仍有写事务或目标已存在时，命令会拒绝发布备份。该版本要求三个库都已经创建；尚未初始化完整的实例请先完成正常初始化流程。

成功的备份文件夹恰好包含：

```text
snapshot-001/
  backup-manifest.json
  main.sqlite3
  ideas.sqlite3
  vault.sqlite3
```

清单记录格式版本、时间、各库大小、SHA-256 和 SQLite 基本校验信息。脚本不把私有 `.env`、上游密钥或本地凭证配置复制进备份，也不在清单里写原始数据库路径。**数据库内容会完整保留**；如果曾把敏感文字写进记忆，它也会在备份里。私有配置需要另外通过自己的安全方式保存。

先在目标同级临时目录生成文件，全部检查通过后才以新目录名发布。目标目录默认拒绝覆盖。操作中断后，可能留下一个 `.目标名.stiller-publish-lock` 预约文件；先核实对应操作已经结束，再人工处理，或改用新的目标名称。操作期间请保持输入、输出及其父目录由本次任务独占，避免其他程序同时搬动或替换路径。

## 4. 检查备份

```powershell
python -B scripts/stiller_ops.py verify --backup C:/stiller-private/backups/snapshot-001
```

检查固定文件集合、清单格式、文件大小、SHA-256、SQLite 完整性以及元数据。成功时 `verified_databases` 为 `3`。不要向完整备份文件夹再添加说明书、配置文件或导出文件；额外文件会使本工具拒绝校验。

**校验说明文件与清单彼此一致，不证明备份来自可信的人，也不是数字签名。** 请保管好原备份，只恢复自己信任的来源。数据库结构完整也不等同于每条业务数据都符合当前版本的业务规则。

## 5. 恢复到新目录

```powershell
python -B scripts/stiller_ops.py restore --backup C:/stiller-private/backups/snapshot-001 --output C:/stiller-private/restores/rehearsal-001
```

程序先校验备份，复制到新的临时目录，再校验复制结果，最后发布 `rehearsal-001`。已经存在的目标会被拒绝。原备份、正在使用的数据库和私有配置保持原状；此命令不会切换当前实例。

恢复后的三个数据库使用上面的固定文件名。接下来由维护者在**另外一份私有配置**中填写这三个新路径，用兼容的 ST 版本核验副本。恢复演练应保持与原实例隔离；在核对身份、部署 epoch、端口和授权方案之前，先不要把副本接入原客户端或与原实例同时运行。

这是一份完整数据库快照，包含当时的运行状态，不只是挑选出来的记忆文本。正式切换需要另外安排停服、配置备份和回退方案；本命令提供安全的新目录落盘步骤，不自动执行生产切换或跨版本数据迁移。

## 6. 按需导出一个表

导出只读取已经校验过的离线备份，让你明确选一个数据库和其中一个表。以情感记忆表为例：

```powershell
python -B scripts/stiller_ops.py export --backup C:/stiller-private/backups/snapshot-001 --database main --table emotion_memories --output C:/stiller-private/exports/emotion-memories-001.jsonl --ack-private-data
```

`--ack-private-data` 表示理解导出可能包含私人原文。输出文件名必须是新的；命令完成前再次校验源备份，使用不覆盖已有文件的方式发布结果。当前实现要求目标磁盘支持硬链接；不支持时会安全失败，可换到本机 NTFS 或其他支持硬链接的文件系统。

可按需求选择源码中实际使用的表，例如：

| `--database` | `--table` | 内容方向 |
| --- | --- | --- |
| `main` | `emotion_memories` | 情感记忆记录 |
| `main` | `learning_items` | 学习模块条目 |
| `main` | `planning_items` | 规划模块条目 |
| `main` | `tool_cards` | 工具卡片 |
| `main` | `self_model_revisions` | 自我模型修订记录 |
| `ideas` | `learning_idea_box` | 灵感盒条目 |
| `vault` | `vault_records` | 保险箱记录 |

实际可导出的表取决于该实例已初始化的模块和版本；不存在的表会返回错误。每次导出选定表的**全部行和全部列**，暂不提供逐条选择、筛选条件或脱敏功能。关联、证据与历史版本可能放在其他表里；单表导出不代表某个模块的完整档案。需要完整恢复时保留三库备份。

JSONL 首行是格式、表名、列名与源数据库哈希；后续每行的 `row` 按列名顺序保存一条记录。SQLite 二进制值使用带类型标记的 Base64 对象。它适合本地审阅或开发专用转换器，**不是承诺可直接导入任意产品的通用记忆格式**；本版本没有 JSONL 回灌命令。

## 7. 返回结果与常见问题

成功的非启动命令输出简短 JSON，退出码为 `0`；失败输出不包含任意 SQL、数据库正文或凭证值的固定提示，退出码为 `2`。`start` 的 Ctrl+C 正常退出码为 `0`。

| 提示方向 | 处理方法 |
| --- | --- |
| 配置校验失败 | 按[本地开发指南](LOCAL-DEVELOPMENT.md)检查完整私有配置，保持独立凭证及严格绑定。 |
| 配置端口被占用 | 查明对应实例并正常停止；不要随意终止其他应用。 |
| 数据库有活动写事务 | 等待并停止所有写入者，再重试备份。 |
| 缺少数据库 | 确认配置选中的是目标实例，且三个模块数据库已经正常创建。 |
| 目标已存在 | 使用新名称，保留原备份或原恢复副本。 |
| 哈希或完整性不匹配 | 保留问题文件供本地检查，改用另一份已验证备份。 |
| 子进程未能确认停止 | 检查本命令启动的本地服务进程；确认全部结束后再备份。 |

这些命令是本机运维工具，不增加 MCP 工具数量，也不代替 AI 的模块初始化、复核或当前唤醒授权。

## 8. 维护者如何复跑测试

常规合成数据库与模拟进程测试：

```powershell
python -B -m unittest packaging_tests.test_operations -v
```

另外提供可选的真实本机三组件启动测试，需要已安装完整依赖。测试会创建临时合成数据库、分配本机临时端口，只检查健康、工具目录与清理行为；上游地址指向专用的本机哨兵，不进行模型生成。默认的测试发现会跳过这两项，明确启用后才启动服务：

```powershell
$env:STBRAIN_RUN_LOCAL_SERVICE_SMOKE='1'
python -B -m unittest packaging_tests.test_operations_local_smoke -v
Remove-Item Env:STBRAIN_RUN_LOCAL_SERVICE_SMOKE
```

真实启动测试通过程序内部的 `KeyboardInterrupt` 走 Ctrl+C 同一清理分支；不会向桌面或其他进程发送全局键盘事件。当前实测平台为 Windows x86_64 / CPython 3.14.6。操作脚本及 Windows 辅助类不增加第三方依赖。
