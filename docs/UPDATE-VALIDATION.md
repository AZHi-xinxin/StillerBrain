# 本次改进的验证记录

版本：`0.1.0-preview.2`，源码开发预览。验证日期：2026-09-11（北京时间）。

从公开提交 `a1abd690d9b25c5a73ec78e15e8720e4beaf4ec9` 的源码快照开始修改。此次测试在 Windows x86_64 / CPython 3.14.6 上执行，使用既有锁定依赖环境和临时合成数据库；生产实例和私人记忆库保持原状。

## 自动回归

| 范围 | 执行结果 |
| --- | --- |
| 记忆运行时 `tests` | 422 项通过 |
| MCP 服务 `mcp_server/tests` | 178 项通过 |
| 兼容网关 `rikkahub_gateway/tests` | 275 项通过 |
| 打包与运维 `packaging_tests`，启用本机服务测试 | 51 项通过 |

最后一组包括 49 项合成或模拟测试、2 项本机真实三组件测试。默认运行会跳过后两项；显式设置 `STBRAIN_RUN_LOCAL_SERVICE_SMOKE=1` 才启用。测试使用临时端口与配置，上游哨兵收到的模型连接为零。

```powershell
python -B -m unittest discover -s tests -q
python -B -m unittest discover -s mcp_server/tests -q
python -B -m unittest discover -s rikkahub_gateway/tests -q
$env:STBRAIN_RUN_LOCAL_SERVICE_SMOKE = '1'
python -B -m unittest discover -s packaging_tests -q
Remove-Item Env:STBRAIN_RUN_LOCAL_SERVICE_SMOKE
```

## 运维入口实际验证了什么

- 检查配置并在同一终端启动控制面、MCP 和网关；验证健康响应、MCP 初始化以及 43 项工具目录。
- 通过内部 `KeyboardInterrupt` 触发与 Ctrl+C 相同的清理路径；另一路主动结束本次拥有的一个子进程，验证其他子进程树退出、三端口释放。
- Windows 虚拟环境可能通过转发进程启动解释器。初测发现只结束直接子进程不足以立即释放端口，现改为挂起创建、加入本次专属 Job Object、再恢复执行，并核查整个作业清空。独立检查未发现本轮测试进程残留。
- 合成库覆盖停服检查、备份完整性、清单篡改、校验失败、新目录恢复、禁止覆盖、路径与链接边界，以及含二进制字段的按表 JSONL 导出。

完整配置、全部写入者停服、私密导出及恢复范围见[运维指南](OPERATIONS.md)。三库备份依赖明确停服条件；校验保证文件一致性，来源可信度由操作者确认。恢复输出新目录，由操作者另行切换配置。

## 检索改进实际验证了什么

- 405 组合成输入与旧实现的原始评分、精确匹配标记一致。
- 128 条合成记录、每条 24 个关键词、1000 字符查询中，查询规范化调用从 3456 次降到 2 次。该计数衡量重复工作，整体延迟需单独测量。
- 8 组指定词族替代表达中，原路径找回 0/8，显式摘要回退找回 8/8。这是机制对照集，真实记忆库整体效果仍待独立验收。
- 新词法候选只走显式检索摘要回退；原排序、自动注入、敏感披露、所有者隔离及关联传播分别回归。

详见[检索说明](RETRIEVAL.md)。记忆渐隐另见[设计讨论稿](MEMORY-DECAY-DESIGN.md)，本次只更新设计。

## 源码交付与仍待验收的范围

本次源码仅由公开基线与本轮代码、合成测试、文档组成，许可原文保持。说明中的情景是合成示例；封面为本项目生成的 SVG 矢量图。源码清单校验命令：

```powershell
python -B scripts/check_source_package.py
```

`source-release-state.json` 记录源码检查；`release-state.json` 继续单独记录生产门槛。本次尚未完成 Linux/macOS 运维实机验证、私人实例升级恢复演练、实际用户召回效果评测、移动客户端全链路验收或新一轮依赖安全审计。测试成绩属于本次源码范围。

GitHub 更新由维护者自行上传。仓库里的源文件检查不涵盖既有 Git 历史、其他分支、个人备份或实际部署配置。
