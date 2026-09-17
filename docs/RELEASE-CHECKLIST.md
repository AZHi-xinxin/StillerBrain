# 源码包与生产验收检查表

本页对应 `0.1.0-preview.5`，采用 PolyForm Noncommercial 1.0.0。源码交付、特定实例升级和通用生产认证分别记录；本版证据见 [UPDATE-VALIDATION](UPDATE-VALIDATION.md)。

## 1. 本版源码预览已核实的内容

- [x] 正式 LICENSE、项目 NOTICE、商业申请、贡献、安全及隐私说明随源码保留。
- [x] 从 L36 来源同步 206 份源码、Schema 与测试文件，另保留公开仓库的跨 Python AST 静态测试；组件文档按本版公开用法更新。待验收的网络与内部代理差异按本次发布范围排除，不将整个部署目录原样公开。
- [x] 旧版测试保留为历史基线，不计作本版重新运行结果，见 [preview.4 验证记录](UPDATE-VALIDATION-preview.4.md) 与 [preview.3 验证记录](UPDATE-VALIDATION-preview.3.md)。
- [x] 默认 `/mcp` 仍为完整 44 项目录；显式 `/mcp?tool_profile=daily` 为日常 7 入口。当前 `simple-memory-v1` 分类工具箱可发现 45 项操作，先按分类发现、按操作读准确参数，再由统一入口执行；已知参数可直接执行，原权限继续校验。
- [x] 本版合入使用者已验收的核心记忆与 MCP 更新，网络与长上下文延迟验收单独保留；长期缓存自用反馈仍按原有定性范围描述。
- [x] 最终公开候选的源码回归、目录与调用边界检查完成，按 [UPDATE-VALIDATION](UPDATE-VALIDATION.md) 记录实际范围、通过与跳过情况；不沿用旧版测试数量作为本版结论。
- [x] 最终公开树完成凭证、私人部署标识及本地链接核对；私人记忆、运行数据库、私有配置和验证证据保留在源码包外。

依赖锁定、来源与已知漏洞查询的范围见 [UPDATE-VALIDATION](UPDATE-VALIDATION.md) 和 [第三方提示](../THIRD_PARTY_NOTICES.md)。更早预览版的来源与文件一致性记录见 [SOURCE-PROVENANCE](../SOURCE-PROVENANCE.md) 的历史段落；旧目录和测试结果以对应历史版本为准。

## 2. 每次准备上传时再核对

最终文件集合、每个文件的 SHA256 和禁止产物由以下命令检查：

```sh
python -B scripts/check_source_package.py
```

以准备上传的最终目录为检查对象。维护者复核内容后更新清单；检查通过后再打包或提交，后续改动需要重新核对。该检查验证归档完整性，人工来源、隐私及发布内容审查另行完成。

上传只选择本仓库内容。采用 Git 提交时，还需核对实际暂存区与提交历史；源码包检查器的范围是当前文件树。源码检查通过、发布授权和 GitHub 实际上传是三个分别记录的状态。

## 3. 特定实例升级与通用生产认证

某一实例的升级按其现有数据、配置与客户端验收：停服前准备、备份与恢复预演、结构迁移、旧记录核对、服务健康和真实客户端工具续轮分别留下结果。操作步骤见 [OPERATIONS](OPERATIONS.md)。本页记录源码预览依据；特定 VPS 的部署完成状态由该次部署回执确认。

更广泛的验收继续包括：全新机器及更多系统的安装、完整依赖来源与分发义务审查、部署环境和历史提交的隐私审查，以及更多客户端、长期任务和真实召回效果。

[source-release-state.json](../source-release-state.json) 记录当前源码预览验证；[release-state.json](../release-state.json) 与 `python -B scripts/check_release.py` 保留通用生产门槛。后者仍会报告尚未完成的条件，与源码完整性检查各有范围。已验收的本机功能及缓存自用反馈保留为各自的实际证据。
