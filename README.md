# Stiller Brain（ST）

昕昕 & 阿止

为 AI 提供可选择使用、可追溯、可修订的长期记忆基础设施。ST 保存 AI 自己写下或明确采纳的内容，不替 AI 编写人格，也不把检索结果自动当作事实。

**`0.1.0-preview.1` · 源码开发预览 · source-available，非 OSI 开源。**

个人非商业使用免费，商业使用需另行授权；准确范围以正式 [PolyForm Noncommercial 1.0.0 许可证](LICENSE) 为准，包括其中明确允许的机构用途。许可已适用于此发布包，不是待定草案。中文说明不增加或收窄标准条款，详情见 [商业授权说明](COMMERCIAL-LICENSE.md)。这是一份可供分发、学习和本地测试的源码预览，不是生产稳定版认证。

## 能做什么

- 自我模型：AI 提交候选、在后来的真实轮次复核、再在后续轮次激活；保留差异、来源、历史和回退路径。
- 日常记忆：普通学习、情感、规划记忆通过 `remember_memory(module, content)` 一次保存；核心自我修改的多轮复核不套在日常新增上。
- 选择性召回：按问题或精确引用读取；自动注入有预算和披露边界，不把全部记忆塞进每轮对话。
- 工具经验与计划：保存使用经验、规划及证据；记忆本身不授予现实世界操作权限。
- 幻觉隔离：隔离记录使用独立数据库，不静默注入普通上下文。
- 宿主适配：MCP 提供工具，独立控制面与 OpenAI 兼容网关绑定真实轮次、上下文快照和工具调用。

ST 不需要项目作者的域名、账号、记忆数据库或特定手机。此源码包不包含 Android 前端、个人自我模型或生活记忆。其他客户端需要验证历史与工具结果的传输行为，不承诺任意前端即插即用。

## 先在新的测试环境使用

1. 阅读 [设计边界](docs/ARCHITECTURE.md)、[安全说明](SECURITY.md) 和 [数据与隐私](PRIVACY.md)。
2. 按 [本地开发指南](docs/LOCAL-DEVELOPMENT.md) 建立全新环境、数据库和独立凭证。已验收的依赖锁针对 Windows x86_64 / CPython 3.14；其他平台需要单独验收。
3. 运行完整测试，再连接新建的测试聊天窗口。不要首次安装就指向现有个人数据库，也不要把数据库、密钥或 `.env` 放进仓库。
4. AI 需要帮助时读取 `stbrain_help`；实际工具名、参数和合法动作以当前 MCP 工具目录及回执为准。

## 测试与两种验收边界

先在原始仓库根目录运行完整性检查，再按本地开发指南在仓库外建立虚拟环境。
运行测试前设置 `PYTHONDONTWRITEBYTECODE=1`，随后运行：

```sh
python -B scripts/check_source_package.py
python -B -m unittest discover -s tests -v
python -B -m unittest discover -s mcp_server/tests -v
python -B -m unittest discover -s rikkahub_gateway/tests -v
python -B -m unittest discover -s packaging_tests -v
```

第一项只需 Python 标准库，检查本次**源码包**的文件集合及记录哈希；后四项验证代码与打包测试。结果不代表真实客户端已经验收、已经部署生产或已经降低账单。原有严格生产门禁 `python -B scripts/check_release.py` 另行保留；未完成的生产和用户验收条件不能因为源码包可分发就改为通过。状态参见 [源码发布状态](source-release-state.json)、[生产状态](release-state.json) 和 [发布检查表](docs/RELEASE-CHECKLIST.md)。

本次完整回归通过 **881 项测试**：runtime 406、MCP 178、gateway 275、打包 22。使用已从官方 PyPI 全新安装并哈希锁定的 Windows x86_64 / CPython 3.14.6 venv；三个服务在新临时合成环境中启动，权限隔离及 43 项工具目录检查通过，付费模型请求为 0。30 个依赖有版本、来源及对应平台的哈希锁，详见 [依赖记录](docs/DEPENDENCIES.md)。

## 缓存优化的真实范围

缓存布局默认 `legacy`；`anchored-v1` 是需要宿主兼容验证后主动开启的优化选项，不会自动改变用户生产配置。它的目标是减少 ST 注入布局造成的请求前缀变化，不改变记忆内容和哲学层规则。

Rikka 等客户端若在工具续轮刷新电量、时间等动态占位符，会破坏严格历史绑定；应修正宿主序列化，不能关闭保护来让测试通过。新增数据库列需要升级与回退演练。详见 [缓存说明](docs/CACHE.md)。

**缓存命中由模型供应商决定；本版没有真实聊天节费验证，不保证命中率、零费用或任何降幅。** 对话长度、工具结果、历史变动、模型和供应商缓存行为都影响费用。不要把合成测试的通过误读成账单改善。

## 限制、反馈与权利

这是单实例、明确身份边界的开发预览，不是经过独立渗透测试的多租户托管服务。历史截断、重排、错误工具名和不兼容的续轮行为可能使调用失败；中断恢复不能自动重放结果未知的写入。

贡献前请读 [CONTRIBUTING.md](CONTRIBUTING.md)。Issue 只提交版本、脱敏错误类别和合成复现；凭证、个人记忆、完整请求或合同请勿公开。漏洞按 [SECURITY.md](SECURITY.md) 处理。

第三方依赖和客户端保留各自权利，本项目许可不替它们重新授权。见 [第三方提示](THIRD_PARTY_NOTICES.md)、[源码来源说明](SOURCE-PROVENANCE.md) 和 [授权决策](docs/LICENSING-DECISION.md)。限制商业用途不符合 [OSI 开源定义第 6 条](https://opensource.org/osd)，因此本项目明确使用 source-available（源码可见）这一称呼。
