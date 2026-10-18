# 在安卓手机本机运行 ST（实验性）

已有手机安装需要更新？请看[手机保留数据升级与回退](PHONE-UPGRADE.md)。新入口从独立新源码执行 `upgrade`，先备份再切换；不要在已绑定的旧源码目录直接拉取并重跑安装。

[首页](../README.md) · [常规安装](INSTALL.md) · [连接指南](GUIDE.md) · [备份与导出](OPERATIONS.md)

这条路线把 ST 服务放在手机里运行，再由**同一部手机**上的 Orbis 连接它。**不必购买 VPS，也不必一直开着电脑。** 模型仍通过你配置的上游 API 调用；这不是把大语言模型本身下载到手机，也不是免费模型额度。

本页提供的是实验性安装入口，不是“所有安卓手机已经验证可用”的承诺。ARM64 依赖锁、脚本测试、Linux 运行测试与真实手机验收是不同层次；**不能把其中一项通过当作 Termux／PRoot／Orbis 的全流程真机验收。** 实际验证范围以[验证记录](UPDATE-VALIDATION.md)为准，手机待验项见页末。

## 1. 先确认是否适合这条路线

需要：

- 能正常运行官方 **Termux** 的 **Android ARM64／aarch64** 手机。
- 官方 Termux 应用（包名 `com.termux`）；不要混装不同来源签名的 Termux 与插件。安装来源见 [Termux 官方说明](https://github.com/termux/termux-app#installation)。未经核实的第三方改版不在此入口支持范围内。
- Termux 中的 Python 和 **proot-distro 5.10.x**。入口也接受更新的 5.x，但仍会检查所需命令参数；低于 5.10 或 6.x 暂不自动继续。
- 至少 **3 GiB 可用存储**，以及下载 Ubuntu、系统软件包和 Python 依赖所需的网络。3 GiB 是最低检查线，不保证安装及以后增长的记忆都够用。
- 可用的模型 HTTPS API 地址、真实模型 ID 与 API Key；支持范围见[安装说明](INSTALL.md#2-准备三项模型资料)。

本路线是：**Termux 外层 → 专用 Ubuntu 24.04 ARM64 → 标准 CPython 3.12／glibc → ST**。Termux 自带的 Python 只负责引导，不在 Android Bionic 环境直接安装 ST 的 Linux wheel。

**不支持 iOS、32 位 Android，或没有 Android 应用运行环境的纯鸿蒙。** 某些系统自带兼容环境也不等于已验证支持，需要单独核实。入口不要求 root，不更改 SELinux，不需要 Docker 或 systemd。PRoot 只是用户态兼容方式，**不是安全隔离边界**。[PRoot 官方限制](https://github.com/termux/proot-distro#limitations)

已有电脑／VPS 版 ST 时，手机仍可直接连接原服务；没有必要为了使用 Orbis 而重新建立一份手机记忆库。本页用于**新建独立实例**，不自动迁移已有记忆。

## 2. 准备 Termux 与完整源码

以下命令都在 **Termux 外层终端**中执行，不是在已经进入 Ubuntu 的终端里执行。

先安装本次需要的工具，不需要执行全局软件升级：

```sh
pkg install python proot-distro git
```

查看架构、Python 和 proot-distro 帮助：

```sh
uname -m
python --version
proot-distro --help
```

`uname -m` 应为 `aarch64`。外层 Python 不要求正好 3.12，真正运行 ST 的 Python 3.12 由后面的 Ubuntu 准备步骤安装。

**proot-distro 5.10 的版本在 `--help` 最后的页脚中**，例如 `PRoot-Distro version '5.10.0' ...`；不要使用该版本没有提供的 `--version` 参数。若版本太旧，先按 [proot-distro 官方说明](https://github.com/termux/proot-distro#installation)核实更新方式；不要为了越过提示删除检查，也不要套用旧版的发行版安装命令。

把完整源码放进 Termux 的私有 home。下面示例目录应当尚不存在；已有同名目录时，先核对内容，不要覆盖：

```sh
cd "$HOME"
git clone --depth 1 https://github.com/AZHi-xinxin/StillerBrain.git StillerBrain-phone-source
cd "$HOME/StillerBrain-phone-source"
```

确认这个源码版本中已有 `scripts/install_phone.py` 和 `requirements-linux-arm64-py312.lock`。若没有，说明拿到的是没有本入口的旧版源码，不要自行拼凑文件或删除完整性检查。

**不要把源码、运行环境或私有数据放在 `/sdcard`、下载目录、`~/storage` 快捷方式、网盘同步目录中。** 这些位置可能不具备需要的权限、文件锁、原子写入或数据库持久化能力。引导也拒绝符号链接路径。

## 3. 先预览，再明确执行四个步骤

在源码目录运行以下命令，只显示说明，**不下载、不安装、不启动服务**；这个预览也不代表已经通过设备检查：

```sh
python -B scripts/install_phone.py --help
python -B scripts/install_phone.py prepare
```

默认使用：

| 内容 | 默认位置或名称 |
| --- | --- |
| 源码 | 本示例的 `$HOME/StillerBrain-phone-source` |
| 专用状态目录 | `$HOME/StillerBrain-phone`，由引导创建 |
| 私有 ST 实例 | `$HOME/StillerBrain-phone/private` |
| Ubuntu 专用别名 | `stiller-phone-ubuntu2404` |

不要事先手动创建专用状态目录或借用其他 Ubuntu 的目录。引导会记录自己的目录、源码完整性摘要与发行版归属；不认识的已有目录不会自动接管。

### 第一步：prepare，准备专用 Ubuntu

确认后显式添加 `--execute`：

```sh
python -B scripts/install_phone.py prepare --execute
```

这一步下载 `ubuntu:24.04` 的 ARM64 环境，在其中安装 Python 3.12、venv 与证书；不全局更新 Termux 软件，不重置其他发行版，不输入模型密钥。

随后检查客体系统与 Python ABI，并用一个新建的合成测试目录检查：私有权限、原子替换及文件／目录同步、SQLite WAL 读写与重开完整性、不同进程之间的排他文件锁。任何一项失败就停止，不能靠忽略权限、锁或同步错误继续安装。

成功探针只清理自己创建的已知文件；失败探针通常保留在专用状态目录的 `st-phone-storage-probe-*` 子目录中，仅含测试数据。**它不会读取现有的记忆库或模型配置。**

### 第二步：install，填写模型资料并安装 ST

```sh
python -B scripts/install_phone.py install --execute
```

按向导填写 HTTPS Base URL、真实模型 ID、API Key，以及可选的模块一授权密码。密钥和密码使用隐藏输入；输入时看不到字符是正常的。检查说明后输入 `yes` 才继续。

依赖使用专门的 ARM64 锁文件、固定版本和哈希验证安装，不沿用 x64 wheel，不从源码临时编译替代包。不要删除 `--require-hashes`、修改锁文件或关闭源码检查来绕过下载错误。

安装后会短暂启动本实例的三个服务并检查本机健康状态与 MCP 工具目录，再停止检查进程。**这一步不请求真实模型，也不能证明模型 Key 或 Orbis 的工具续轮已经可用。**

### 第三步：check，单独复核本机服务

```sh
python -B scripts/install_phone.py check --execute
```

`check` 会重新做环境和存储检查，并短暂启动、检查、停止本实例的服务，不调用真实模型。首次安装已经做过本机检查；这条命令便于之后独立复核。

请在这份 ST **未运行时**执行。若已有 `start` 在其他终端运行，先回原终端正常停止；不要并行启动第二套同实例服务。

### 第四步：start，保持前台运行

```sh
python -B scripts/install_phone.py start --execute
```

保留这个 Termux 会话，再切到 Orbis 使用。要停止时，回到运行终端按 **Ctrl+C**，等待收尾完成、命令提示符返回；不要反复强停，也不要直接清除 Termux 数据。

底层安装向导可能显示 `/opt/...` 开头的客体命令。**那些路径属于 Ubuntu 内部，不要直接粘贴到 Termux 外层。** 手机日常启动仍使用上面的 `install_phone.py start --execute`。

以上每步都必须显式带 `--execute` 才执行。若需要第二个完全独立的新实例，可用 `--directory` 和 `--name` 指定新的私有目录及 `stiller-` 开头的别名；四个步骤都要使用相同参数。初次安装建议先使用默认值。

## 4. 同一手机上的 Orbis 怎样连接

在本机私下查看：

```text
$HOME/StillerBrain-phone/private/连接资料（请勿公开）.txt
```

它包含真正分配到的地址、端口及两组独立凭证。**不要截图或把整份文件发到群里。** 端口由安装器选择，不能假定每个人都一样。

- **模型网关**：OpenAI 兼容 Chat Completions；Base URL 是文件中的 `http://127.0.0.1:实际网关端口/v1`，路径 `/chat/completions`，填写文件中的模型 ID 与**网关 Key**。
- **MCP**：Streamable HTTP；URL 是文件中的 `http://127.0.0.1:实际MCP端口/mcp`，认证使用文件中给出的 MCP Token。
- 想同时使用回复前的记忆浮现与主动读写工具，需要按[连接指南](GUIDE.md)配置网关和 MCP。只连 MCP 不等于模型请求已经通过 ST 网关。

这里的 `127.0.0.1` 指**运行 ST 的这部手机自身**，因此同一手机上的 Orbis 应使用它。电脑或另一部手机的 `127.0.0.1` 不是这部手机；不要为了让别的设备连接而把监听改成 `0.0.0.0`。跨设备访问需要另行设计受保护的连接。

上游服务商 API Key 保存在实例的 `config.env` 中；**Orbis 的 ST 网关栏填的是生成的网关 Key，不是上游 Key。** 首次接入后可让 AI 调用 `stbrain_help` 完成初始设置。实际发送模型请求可能产生服务商费用。

## 5. 后台、备份与更新边界

**前台启动不等于手机会永远保活。** Android 锁屏、省电、系统内存回收、用户强停 Termux 都可能中断服务；不同厂商的行为需要实际测试。若系统允许，可自行了解该机型的后台与电池设置，但本入口不修改这些权限，不承诺 `nohup`、关终端、重启手机后仍然运行，也不能据此保证定时消息一定送达。

先备份，再清理或升级：

- 正常停止写入并确认本实例已经停止后，按[备份说明](OPERATIONS.md#3-停服后备份)制作并验证离线备份。该文档是通用运维入口，手机需要在对应 Ubuntu 环境内使用实例自己的 Python 和实际路径；不确定时先保留现场求助。
- 记忆备份与连接配置不是同一件事；还应单独、私密保存必要配置和安装归属资料，记录对应的源码版本。含密钥和私人记忆的材料只能放到自己控制的加密备份位置，不能上传公开仓库。
- **卸载 Termux、清除应用数据、删除专用目录或重置发行版，可能丢失本地安装和记忆。** 只有源码压缩包、聊天截图或留在同一 Termux 内的备份，不足以应对应用数据被清除。

这个入口**不会自动接管其他手机部署、已有 Ubuntu 或缺少归属标记的安装**。同一引导建立的旧版本现可按[显式升级流程](PHONE-UPGRADE.md)更新；保留旧源码，使用独立新源码目录，先备份与检查再切换。不要直接覆盖旧源码，也不要删除标记文件绕过归属检查。

同一源码和本入口已确认归属的安装，可以重跑相同步骤继续；如果发行版下载中断，留下了没有成功归属记录的目录，入口会保留并拒绝自动接管，而不是默默重置它。保留报错供核查，不要先删目录或重新生成密钥。

## 6. 遇到问题先看这里

| 提示或现象 | 先做什么 |
| --- | --- |
| 没有执行、只看见说明 | 确认你确实希望执行该步骤，再加 `--execute`。 |
| 未识别 Termux／架构／PRoot 版本 | 在 Termux 外层查看 `uname -m` 和 `proot-distro --help`；不要在已有 Ubuntu 中嵌套运行。 |
| 源码完整性不通过 | 保留原安装，重新取得完整、对应版本源码；不要关闭哈希检查。 |
| 私有权限、同步、SQLite 或文件锁失败 | 确认目录位于 Termux 私有 home；保留固定错误类别，别改用共享存储或忽略检查。 |
| 目录、别名或归属记录不一致 | 不要删除原目录；确认是不是已有实例、旧源码或中断的准备操作。 |
| 实例已有操作／端口占用 | 回原运行终端正常停止并等待；不要按端口随意杀其他应用。 |
| 本机检查通过，但 Orbis 连不上 | 确认 `start` 仍在运行，并按私有连接资料逐项核对地址、端口、两种凭证与 MCP 传输方式。 |
| 切出 Termux 或锁屏后断开 | 先核对服务是否被系统停止；本机检查通过不代表后台持续运行已验收。 |

求助时可提供手机系统与架构、工具版本、失败步骤、固定错误类别及已脱敏截图。不要公开 `config.env`、连接资料、数据库、完整私有日志或任何 Token／API Key。

## 7. 手机验收清单（不是已经全部通过）

- [ ] 对应手机上完成官方 Termux、Ubuntu 24.04 ARM64、Python 3.12 的首次准备。
- [ ] 四项存储能力真实通过，失败时保留现场且不越过检查。
- [ ] 锁定 ARM64 依赖安装完成，本机三服务与 MCP 工具检查通过。
- [ ] 同手机 Orbis 能连网关和 MCP，使用合成内容完成一次写入、读取及工具续轮。
- [ ] 正常停止、再次启动后，原身份、配置与测试记忆保留。
- [ ] Ctrl+C 收尾后只停止本次启动的服务，没有残留占用，也没有终止其他实例。
- [ ] 弱网、下载中断、磁盘不足或重复执行时，不覆盖旧目录、记忆或密钥。
- [ ] 实测切应用、锁屏、节电、重启后的表现，记录机型与限制，不推广为所有手机都能常驻。
- [ ] 完成一次离线备份验证与独立恢复演练，再考虑迁移正式记忆或清理旧环境。

在这些项目未完成前，建议先使用新的测试实例，不把它作为唯一的正式记忆存放位置。
