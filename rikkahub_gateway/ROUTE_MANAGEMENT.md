# 人类专用路由管理 v1

本功能默认关闭，只管理额外网关模型路由。它不改变 ST 身份、记忆、环境配置中的默认/静态路由或其他服务，也不是 AI 工具。

## 部署与凭证

- `STBRAIN_GATEWAY_MANAGEMENT_ENABLED=1` 显式开启。
- `STBRAIN_GATEWAY_MANAGEMENT_TOKEN` 使用独立随机秘密，至少 32 个可打印 ASCII 字符；不能复用聊天、MCP、人类控制、唤醒或上游凭证。此版不交换聊天凭证来授予管理权限。
- `STBRAIN_GATEWAY_MANAGEMENT_STATE_DIR` 指向已经存在的绝对私有目录：POSIX 仅所有者可访问；Windows 仅当前账号、SYSTEM 和 Administrators。禁止符号链接和重解析点。里面的独立 JSON/锁文件不属于三个记忆数据库，须另行备份，不能提交到仓库。
- 使用受认证、加密的入口，例如 HTTPS 或既有受控私有网络；不要开放公共或不受控局域网明文入口。不需要时在配置与反向代理处关闭。

管理凭证由人类保管。前端必须实现安全本地保存与明确确认，不能把密钥暴露给 AI、日志、错误详情或反馈附件。服务端不保证任意第三方前端都已实现这些保护。

## 同源接口

接口位于所选 ST 网关，不接受查询字符串、重定向、Cookie 或浏览器 Origin。错误只包含 `{"error":{"code":"..."}}`，不回显密钥。

| 请求 | 结果与约束 |
| --- | --- |
| `GET /v1/st/routes/capabilities` | 不需凭证；`schema=orbis.st.routes-capabilities/1`，返回 `enabled`、`authorization=dedicated-human-bearer`、`maxRoutes=16` |
| `GET /v1/st/routes` | 独立管理 Bearer；`schema=orbis.st.routes/1`、全局 `revision`、`routes`；每项为 `publicModel`、`upstreamBaseUrl`、`upstreamModel`、`managed`、`keyConfigured`，不返回 API Key |
| `POST /v1/st/routes/upsert` | 同一管理权限；不超过 16 KiB JSON，载荷见下文；只验证/保存配置，不发模型请求，也不验证模型可用性 |
| `GET /v1/st/routes/requests/<requestId>` | 同一管理权限，只读查询回执；`schema=orbis.st.routes-request/1`，含 `requestId`、`found` 与 `result` |

管理目录会显示上游地址，普通模型目录只显示公开模型 ID。只有 `managed=true` 的条目可改，既有环境路由不可修改。

提交格式如下；这些是占位值，不是可直接使用的凭证：

```json
{
  "schema": "orbis.st.routes-upsert/1",
  "requestId": "0123456789abcdef0123456789abcdef",
  "expectedRevision": 0,
  "publicModel": "example-chat",
  "upstreamBaseUrl": "https://provider.example/v1",
  "upstreamModel": "example-upstream",
  "apiKey": "REPLACE_WITH_SEPARATE_PROVIDER_KEY"
}
```

`requestId` 为 32 位小写十六进制。新条目必需 `apiKey`；更新已管理条目时省略该字段保留原密钥，显式空值拒绝。`publicModel` 是稳定条目标识。上游必须是公开 HTTPS 基础地址，不含用户信息、查询、片段或路径穿越。提交前由人类核对并确认。成功回执为 `schema=orbis.st.routes-upsert-result/1`，包含 `requestId`、`revision`、`publicModel`、`status=saved`。

全局版本不符返回 `409 revision_conflict`。相同请求 ID、不同载荷返回 `409 request_conflict`；相同 ID 与载荷返回原回执，即使后来又编辑过。POST 结果未知时先查上述只读回执，不自动重发密钥或换 ID；只有在持久状态健康且成功加锁读取后才返回 `found=false`。幂等日志有容量上限，满时拒绝继续写，不自动删旧记录。

其他错误码包括 `invalid_request`、`unsafe_upstream`、`unauthorized`、`management_disabled`、`immutable_route`、`route_limit_reached`、`journal_limit_reached`、`management_unavailable`、`method_not_allowed`。

## 运行与升级边界

未设置管理变量的旧部署保持原路由。已管理的新增/更新路由无需重启；正在生成及其工具续轮仍使用原来的不可变路由快照，后续新轮次才采用更新值。不重放旧消息或工具；客户端切换所选模型仍是另一项明确操作。

已管理 HTTPS 地址的 DNS 结果必须全部可公开路由。每次请求固定到已检查 IP，同时保留原 TLS SNI、证书校验和 Host；禁止重定向、环境代理与跨供应商 Cookie。私网、链路本地、回环或公私混合解析均拒绝。既有部署者静态路由的注册策略独立保留；管理接口不能借此修改它们。当前网关全部上游 HTTP 客户端也不继承环境代理/环境 CA，见 [网关说明](README.md)。

回退到不识别管理状态的旧代码会令这些额外路由不可用，但应原样保留私有状态；默认和环境路由仍按旧版本能力工作。不要为回退路由管理而恢复旧记忆数据库。回退前核对原配置与客户端模型选择，按受控维护流程操作。
