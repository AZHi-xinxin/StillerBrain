# 本地开发与全新安装验证

此指南用于**全新的开发实例**。本候选已在全新虚拟环境完成合成回归和三服务启动验证，不代表所有机器或真实客户端通过；不要对照个人生产环境的端口和脚本照抄安装。

## 环境

当前验证平台为 Windows x86_64、CPython 3.14.6 与 SQLite。其他版本、Linux/macOS 和不同客户端必须分别验收。30 个完整依赖的 Windows / CPython 3.14 wheel 哈希锁已生成，见 [依赖说明](DEPENDENCIES.md)。

```sh
python -B scripts/check_source_package.py
python -m venv ../stiller-dev-venv
```

保持仓库为当前目录。Windows 激活 `../stiller-dev-venv/Scripts/Activate.ps1`，POSIX 使用 `source ../stiller-dev-venv/bin/activate`。
先设置环境变量 `PYTHONDONTWRITEBYTECODE=1`（PowerShell：`$env:PYTHONDONTWRITEBYTECODE='1'`；POSIX：`export PYTHONDONTWRITEBYTECODE=1`），再运行：

```sh
python -m pip --isolated install --index-url https://pypi.org/simple --require-hashes -r requirements-windows-py314.lock
python -m pip check
python -B -m unittest discover -s tests -q
python -B -m unittest discover -s mcp_server/tests -q
python -B -m unittest discover -s rikkahub_gateway/tests -q
python -B -m unittest discover -s packaging_tests -q
```

依赖下载需要网络；默认测试不应使用真实模型 Key 或个人数据库。若安装所需版本不可取得，应明确失败并重新锁定/验收，不静默替换版本。

上述环境变量保证测试子进程也不生成字节码。虚拟环境及测试产物保留在源码分发树外；锁文件只适用于上述平台。

## 私有配置

将 `.env.example` 复制到仓库以外的私有目录。每个 `REPLACE_` 项必须替换；按当前系统填写三个**绝对且位于仓库外**的全新数据库路径。私有目录限制为运行账号可读写；Windows 需检查 NTFS ACL，不能把文件扩展名当安全保护。

生成至少 32 字符的独立随机 Token 和 wake secret，不要复用上游 Key。三个服务共用一个私有配置文件及同一个新部署 UUID（`STBRAIN_EXECUTION_EPOCH`），且 `STBRAIN_REQUIRE_EXECUTION_BINDING=1`。不要只给 MCP 设置 epoch 而让控制面/网关使用不匹配的模式。

`STBRAIN_GATEWAY_CONTEXT_LAYOUT` 保持默认 `legacy`；`anchored-v1` 仅在宿主确认同一次生成内历史冻结后启用。特别注意电量/时间占位符的续轮变化，见 [缓存说明](CACHE.md)。

示例端口为 `18794/18795/18796`，只用于开发。若端口被占用，先确认所属服务，再选择另一组空闲端口并同步 URL；不要关闭未知进程来抢端口。

私有配置格式是 UTF-8 `STBRAIN_NAME=value`，不支持变量插值、引号包装、重复键或多行值；秘密中的等号可保留。不要在命令行传凭证值。

## 启动

在三个独立终端运行，以真实私有文件路径替换占位路径：

```sh
python -B scripts/run_component.py --config /absolute/private/stiller.env --component control
python -B scripts/run_component.py --config /absolute/private/stiller.env --component mcp
python -B scripts/run_component.py --config /absolute/private/stiller.env --component gateway
```

这些命令不安装开机启动、不修改防火墙、不配置 DNS，不生成或导入人格，也不自动连接旧数据。验证配置而不启动进程：

```sh
python -B scripts/run_component.py --config /absolute/private/stiller.env --component gateway --check
```

新客户端模型入口为本机网关 `/v1`，MCP 入口是 MCP 服务 `/mcp`；两者使用不同 Token。手机不能用它自己的 `127.0.0.1` 访问电脑服务。远程访问必须另行设计受控隧道、TLS 和设备授权；此候选没有一键公网发布功能。

## 首次体验和停止

先用新的测试窗口验证普通聊天、静态 `stbrain_help` 和同一轮原生工具续轮。AI 可以选择不使用 ST。真正使用时由 AI 自主完成其核心首次流程；不要把测试 fixture 灌入真实身份。

停止前等待当前工具链结束；先停止入口，再停止服务。中断时核查写入回执，不自动重试未知结果；不要直接清空数据库以解除占用。此开发启动器没有生产 supervisor、升级管理器或自动崩溃恢复承诺。
