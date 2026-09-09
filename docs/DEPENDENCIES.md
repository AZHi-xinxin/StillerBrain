# 依赖清单与来源审查

本版基线在 Windows x86_64 / CPython 3.14.6 的全新 venv 中从官方 PyPI 安装依赖，未复用现有环境的 site-packages。30 个依赖的精确版本、实际 wheel SHA-256、来源域名和声明许可见 [清单](dependency-inventory.json)。下面仅列直接依赖：

| 包 | 版本 | 包声明的许可 |
| --- | --- | --- |
| mcp | 1.29.0 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| jsonschema | 4.26.0 | MIT |
| referencing | 0.37.0 | MIT |
| pydantic | 2.13.4 | MIT |

`requirements-windows-py314.lock` 为该平台/解释器组合的完整哈希锁；`requirements.txt` 只固定直接依赖。Linux、macOS、其他 Python 版本必须生成对应锁并重新验收，不能去掉哈希而声称同样复现。

Python 标准库和 SQLite 也是运行基础。源码包不附带 venv、wheel、site-packages 或运行时二进制；ST 的 PolyForm Noncommercial 1.0.0 不改写第三方独立许可。完整版本和注意事项见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)，其中 pywin32 的实际许可文件与简化元数据分类需要区别对待。

基线已对全部 30 个精确版本进行过一次 OSV 已知漏洞查询，当时没有返回 advisory；UTC 时间和逐包结果见 [回执](dependency-osv-check.json)。这不是当前永远没有漏洞的证明，也不是来源或法律审计。查询只发送公开包名与版本，不发送代码、配置、凭证或个人信息。

所有 30 个安装分发物中均定位到许可文本文件，选取的关键包已阅读实际文本；没有把“存在许可文件”冒称为全部子组件的完整法律审查。若以后附带容器、运行时、wheel 或其他二进制，必须重新核对完整许可、NOTICE 和再分发义务，不能把个人环境直接复制进仓库。

本版的来源审查结果与局限见 [SOURCE-PROVENANCE.md](../SOURCE-PROVENANCE.md)。原始设计中的 clean-room 用语不是独立认证，也不替代对具体借用代码的调查。
