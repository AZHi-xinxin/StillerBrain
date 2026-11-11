# 可选 OpenAI Responses 上游

客户端继续使用 ST 的 `/v1/chat/completions` 入口。ST 在上游路由中显式选择 Responses 协议，不绕过自动记忆，不要求把客户端改成直连 OpenAI。

在部署者的私有环境配置中，给 `STBRAIN_GATEWAY_ROUTES_JSON` 追加路由，保留已有数组内容。例如：

```json
[
  {"public_model":"my-gpt-astra","upstream_base_url":"https://api.openai.com/v1","upstream_model":"gpt-6-astra","api_key_env":"STBRAIN_UPSTREAM_OPENAI_KEY","api_mode":"responses"},
  {"public_model":"my-gpt-sol","upstream_base_url":"https://api.openai.com/v1","upstream_model":"gpt-5.6-sol","api_key_env":"STBRAIN_UPSTREAM_OPENAI_KEY","api_mode":"responses"}
]
```

在自己的私有配置中填写 `STBRAIN_UPSTREAM_OPENAI_KEY`，不要提交到 Git 或复制到聊天。接口权限和费用由自己的 OpenAI API 账号承担。此示例只定义模型路由，没有替换原默认模型、MCP、记忆身份或客户端设置。

`api_mode` 省略时仍为 `chat_completions`。该字段用于部署者静态路由；当前人类路由管理接口不会自动创建 Responses 路由。既有受保护 supervisor 若严格验证路由字段，需要同步允许这两个可选字段后按维护流程加载。

自动推理在适配器中使用 medium；Astra 不接受 none，客户端不要为它选择关闭推理。支持的其他等级以模型接口为准。温度、top_p 只在 none 时透传，避免推理模型拒绝不兼容参数。工具名映射回原名，工具调用结果按原 call_id 继续；加密推理只存在所属活动轮的内存，`store=false`，不写进私人记忆或日志。

服务器需要能够访问官方 API。若部署者已自行配置本机 HTTP 代理，可为该 Responses 路由显式加 `proxy_url`，只接受带端口、无凭证的 HTTP 回环地址。未配置时沿用原出口，不读取系统代理变量；不要填远端共享代理、客户端代理地址或密钥。公开源码不提供网络出口，也不依赖维护者电脑。

合成回归覆盖请求转换、文本和图片输入、流式结束、函数调用往返、推理续接及用量；真实官方两款模型已做小规模往返验证。长上下文、所有模型和长时缓存命中率不作保证。
