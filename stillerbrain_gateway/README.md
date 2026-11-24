# StillerBrain 网关

StillerBrain 的模型接入服务，负责生成前记忆注入、本轮工具调用绑定和上下文接续。Orbis 及具备相应协议能力的客户端均可接入。

使用安装向导提供的命令启动完整 ST 服务。开发环境已加载私有配置时，可单独启动网关：

```sh
python -B -m stillerbrain_gateway.server
```

[配置与接口说明](../rikkahub_gateway/README.md) · [使用指南](../docs/GUIDE.md) · [运维说明](../docs/OPERATIONS.md)
