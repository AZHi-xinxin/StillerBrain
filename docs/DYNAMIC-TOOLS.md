# 工具目录在续轮更新

0.2.3 正式版行为（2026-11-11；源自 preview.1，原试用者已验收）：客户端在收到工具结果后，可以在下一次请求中增加、删除、改名或修改工具参数结构。ST 不再要求一个用户轮内的工具目录始终相同，不再因为这些变化返回 `tool_continuation_catalog_mismatch` 或 `tool_continuation_catalog_missing`。

已经发出的调用，按当时记录的调用 ID、名称与参数接收结果。新工具目录只约束接下来模型发出的调用。工具删除后，已经执行的结果仍可返回；不传 `tools`/`functions` 或提供空列表表示后续没有可用工具。客户端不应为了更新目录而改写历史中的调用名称、ID、参数或工具结果。

示例：第一批调用 `lookup({"city":"A"})` 后，客户端把工具改为 `search({"query":"..."})`。旧结果仍对应原 `lookup` 调用；模型接下来的调用只能使用当前提供的 `search`。原 ST 上下文快照和唤醒身份保持不变。

## 内部实现与升级

- Gateway 校验旧调用与回执后，通过已认证的 Host Control `POST /v1/host/context/tools` 更新当前工具目录。请求包含 `wake_id`、`wake_capability` 和与 context prepare 相同格式的 `advertised_tools`；成功响应为 `decision=tools_updated` 及当前 `catalog_hash`。
- 更新失败时尚未消费待收的调用批次，客户端可以重试同一批结果。
- `brain_context_snapshots.current_tools_json` 为新增可空字段，保存当前目录。旧记录为空时继续读取原 `advertised_tools_json`。原目录、source digest 和上下文内容/哈希不被重写。
- ST 新调用的执行凭据按当前目录签发；旧凭据保持原调用对应关系。ST 自己签发的历史调用即使已从目录移除，其内部执行字段仍不会加入模型输入。
- 这是 Gateway、Control 与 Runtime 配套更新；先一起更新代码再恢复服务。新版 Gateway 与旧版 Control 混用时，目录更新接口不可用。此文档和本地测试不是线上部署完成的证明。

## 验证与范围

新增测试覆盖：增减工具、改名、参数类型改变、空/缺省目录、多次变化后的历史合并、结果被改写、Control 更新失败后重试、数据库重开和旧库迁移、真实本地 Gateway/Control/Executor 路径，以及本地 HTTP JSON/SSE 续聊。

当前请求仍须是可解析的合法工具定义；新调用必须符合当前工具定义，历史结果必须对应实际发出的调用。线程/模型/执行权限和历史结果检查不因目录变化取消。本补丁不表示所有 HTTP 409 都已消失，也不保证上游模型接受任意不合法请求。

工具目录改变可能影响上游缓存前缀；ST 不会为维持缓存命中而继续发送已删除的工具。没有新增客户端配置开关。
