# 依赖清单与来源审查

## 按运行平台选择一份已验收依赖

ST 现在提供两份平台专用的完整哈希锁。安装使用对应的一份锁，包版本和下载文件的 SHA-256 一起核对。

| 平台与解释器 | 完整哈希锁 | 依赖数量与已验环境 |
| --- | --- | --- |
| Windows x86_64，常规 GIL 版 CPython 3.14 | [requirements-windows-py314.lock](../requirements-windows-py314.lock) | 30 包；基线为 CPython 3.14.6 的全新 venv |
| Linux x86_64，CPython 3.12，glibc ≥ 2.34 | [requirements-linux-py312.lock](../requirements-linux-py312.lock) | 29 包；离线安装与 L25 合成回归环境为 Ubuntu 24.04 / CPython 3.12.3 |

Linux 锁中的 `cryptography` 使用 `manylinux_2_34_x86_64` wheel，因此要求 glibc 至少 2.34。这个要求与 CPU 架构、Python 版本同样需要检查。Ubuntu 24.04 是已实际验证的发行版；满足 wheel 条件的其他发行版仍需完成自己的安装与运行检查。

macOS、ARM、32 位 Python、Alpine/musl、自由线程版 Python 和其他 Python 版本，当前没有对应的已验收锁。安装向导遇到这些环境会在创建安装目录前停止，显示已支持的系统、Python 与依赖条件。其他平台可按开发说明单独验收。

锁文件的 wheel 哈希针对相应 ABI 和平台，**不能跨平台混用，也不能去掉哈希检查后当作同样的安装结果**。缺少兼容 wheel 时，应保留错误并停止；源码构建、包升级和改用其他下载源属于另一次需要验收的选择。

## 两个平台沿用各自已验证的版本

这次加入 Linux 锁，复用的是已经完成 Linux 安装与回归的版本；Windows 锁保持原样。两份基线存在以下差异：

| 包 | Windows / Python 3.14 | Linux / Python 3.12 |
| --- | --- | --- |
| anyio | 4.15.1 | 4.14.2 |
| pydantic | 2.13.4 | 2.13.5 |
| pydantic-core | 2.46.4 | 2.46.5 |
| sse-starlette | 3.4.11 | 3.4.8 |
| pywin32 | 312 | Windows 专用，Linux 不安装 |

其余共同依赖的版本相同；含本机代码的 wheel 仍各用对应平台的文件与哈希。`requirements.txt` 只列原 Windows 审查基线的直接依赖，其中 `pydantic==2.13.4` 与 Linux 锁不同。安装时选完整平台锁即可，不要先安装 `requirements.txt` 再叠加另一份锁。

直接依赖为：

| 包 | Windows 版本 | Linux 版本 | 包声明的许可 |
| --- | --- | --- | --- |
| mcp | 1.29.0 | 1.29.0 | MIT |
| httpx | 0.28.1 | 0.28.1 | BSD-3-Clause |
| jsonschema | 4.26.0 | 4.26.0 | MIT |
| referencing | 0.37.0 | 0.37.0 | MIT |
| pydantic | 2.13.4 | 2.13.5 | MIT |

## 来源与可复核清单

- Windows：30 个包来自官方 PyPI 安装报告，精确版本、实际 wheel SHA-256、来源与许可元数据见 [Windows 清单](dependency-inventory.json)。最初在全新 venv 安装，未复用其他环境的 site-packages。
- Linux：29 个 wheel 的文件名与 SHA-256 已逐个核对官方 PyPI 对应版本，再通过强制哈希的离线安装生成安装报告。此次公开前，本地 wheel、记录的安装分发物与清单逐项一致。公开的 [Linux 清单](dependency-inventory-linux-py312.json) 仅含包名、版本、wheel 文件名、哈希、PyPI 链接与许可元数据。
- L25 在上述 Linux 依赖环境中完成独立源码回归：运行时 668 项、MCP 386 项、网关 378 项通过，另 1 项长流选测按默认开关跳过。测试使用合成材料；完整范围见 [本版验证记录](UPDATE-VALIDATION.md)。这是既有环境的源码回归记录，与安装向导本身的验收分别记录。

## 怎样验证一次干净安装

在新建、与正在运行的 ST 分开的目录中创建 venv，使用该 venv 的 Python 执行安装。依赖下载可以来自官方 PyPI，也可以事先准备 wheelhouse 后离线重装；保留安装报告与失败信息。

Windows 示例：

```powershell
python -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: --require-hashes -r requirements-windows-py314.lock
python -m pip check
```

Linux 示例：

```bash
python -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: --require-hashes -r requirements-linux-py312.lock
python -m pip check
```

上述 `python` 必须是新 venv 内、与所选锁对应的解释器。离线重装时，将索引参数替换为 `--no-index --find-links <已核验的wheel目录>`，保留 `--only-binary=:all:` 与 `--require-hashes`。

安装检查通过后，再串行运行合成测试和配置检查。测试使用新的输出目录、临时数据库、回环端口与合成凭证；已有实例的配置、记忆和环境继续保留。内存较小的电脑每次只运行一组，完整回归不是每次启动的前置步骤。

## 许可与审查范围

Python 标准库和 SQLite 也是运行基础。源码包不附带 venv、wheel、site-packages 或运行时二进制；ST 的 PolyForm Noncommercial 1.0.0 不改写第三方独立许可。完整版本与声明范围见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)，其中 pywin32 的实际许可文件与简化元数据分类需要区别对待。

Windows 基线的 30 个精确版本做过一次 OSV 已知漏洞查询，当时没有返回 advisory；UTC 时间和逐包结果见 [回执](dependency-osv-check.json)。这是该次查询的历史结果，并非对当前漏洞状态的保证；Linux 的版本集合与该回执不同，不能直接套用同一份结果。

Windows 的 30 个安装分发物、Linux 的 29 个 wheel 均定位到 LICENSE/COPYING 类文本。Windows 选取的关键包已阅读实际文本；Linux 记录了包内许可声明与文件存在情况。上述检查不是全部子组件的完整法律审查。若以后附带容器、运行时、wheel 或其他二进制，应另行核对适用许可、NOTICE 和再分发义务。

本版来源审查结果与局限见 [SOURCE-PROVENANCE.md](../SOURCE-PROVENANCE.md)。
