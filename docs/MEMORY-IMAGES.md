# ST 记忆图片附件

ST 可以把**真实图片原字节、作者填写的文字描述与已有记忆 ref**一起保存在本人的数据库。当前支持情感 `emotion://`、学习 `learning://` 和规划 `plan://`；先有记忆，再附图。它是 ST 的持久附件，不只是聊天里的临时图片，也不是自动识图服务。图片不会被自动放进每日浮现、常驻上下文、星图或其他记忆正文。

## 最简单的使用方式：在配套客户端发图

支持图片桥的 Orbis 中，可把图片与“把这张原图附进 ST 记忆”一起发给助手；助手选择或新建目标记忆，再附上原图。之后明确要求取回，客户端显示实际返回的图片，而不只是“保存成功”的文字。

这条链路已由使用者在 **Orbis `2.6.4-orbis-dev.61` 测试包**验收。ST 与前端都要支持下述桥接协议；不能据此认定公开 Orbis `2.6.4` 已含图片桥。仅更新 ST 不会自动赋予旧前端传递聊天原件和显示回图的能力。ST 存图本身不绑定 Orbis：其他宿主可以适配，或直接使用下文 CLI。

## 本机最短存取流程

使用装有仓库依赖的 Python，在源码根目录运行；安装依赖时使用对应平台的带 hash 锁文件。凭证放在本机环境变量 `STBRAIN_MCP_TOKEN` 中，或者在提示时隐藏输入，**不要把密钥值放进命令行**。公开地址须 HTTPS，本机回环地址可 HTTP。CLI 不启动服务器，不读取 ST 配置或数据库，也不跟随重定向、环境代理或自动重试。

1. 先通过 `remember_memory(content=...)` 存一条记忆或查询已有记忆，取得真实 ref。正文是普通存入的唯一必填项；已首次激活的 `simple-memory-v1` 无需每次先 `open`，`legacy` 仍需真实授权上下文。
2. 选一个本机图片文件；如下命令会核对真实文件、显示大小/hash/目标并要求输入 `yes`，然后才联系指定 ST：

```text
python -B scripts/memory_images.py put --endpoint https://YOUR-ST/mcp --target-ref emotion://YOUR-ID@1 --file "D:\Pictures\chosen.png"
```

描述可按提示填写；由人类/AI提供的描述不是服务自动看图得出的结论。成功回执提供 `image_ref`。不确定的同内容重试有服务端幂等保护；已移除的旧上传不会复活。

3. 查这条记忆的图片描述及编号（不取原图）：

```text
python -B scripts/memory_images.py list --endpoint https://YOUR-ST/mcp --target-ref emotion://YOUR-ID@1
```

4. 明确取回一张原图到**全新**本机文件。不会覆盖任何现有文件；下载后验证 SHA-256、真实图像格式，排他创建、落盘并从同一文件句柄读回核对 hash：

```text
python -B scripts/memory_images.py get --endpoint https://YOUR-ST/mcp --target-ref emotion://YOUR-ID@1 --image-ref image://YOUR-IMAGE-ID@1 --file "D:\Pictures\restored-new.png"
```

上传前与导出后的 hash 应相同。遇到中断、未知结果或磁盘错误不会自动重试/删除文件；保留现有文件，核对结果后再操作。

5. 需要从列表移除时使用 `remove` 加同样的 `--target-ref`、`--image-ref`。这是**软删除**：隐藏附件，不删除原记忆，数据库和旧备份仍留原图，不等于安全擦除。

## MCP 与授权

不需要自建模型网关，认证的直连 MCP 可沿下述权限使用。本版模型日常目录为 8 个入口（新增常驻 `remember_work_memory`），完整模型目录为 54 项（含下列 4 个图片操作）。传输层 `tools/list` 另列一个标为 host-only 的图片运输能力，不能把它加给模型。图片操作在日常档收于 `stbrain_tools(category="images")` / `stbrain_manage(action=..., arguments={...})`：

| action | 内容 |
| --- | --- |
| `store_memory_image` | `target_ref`、`description`、`mime_type`，以及 `upload_ref` 或真实 `data_base64` 恰一项；可选 `request_id` |
| `list_memory_images` | `target_ref`，可分页，只有描述和元数据 |
| `read_memory_image` | `target_ref`、`image_ref`；默认仅元数据，明确 `include_data=true` 才返回原图 data URI |
| `remove_memory_image` | 同一 `target_ref`、`image_ref`，软删除 |

图片不会变成公开 URL；只有认证、同 owner/model、仍可访问的目标记忆才允许读取。simple-memory-v1 的新增/移除须模块一完成并激活；legacy 保留目标原模块的真实授权上下文，网关调用保留执行绑定与迟到/重放拒绝。咨询室的受限执行档未因此自动增加图片权限。

首版不提供敏感原文的额外确认流程：目标是 intimate/restricted、敏感级别未知，或情感/学习原文策略为 never 时，四个图片操作统一拒绝（包括描述/列表），不通过 include_data 绕过原记忆权限。已有附图的原记忆后来改为受限，也立即受此限制；原件仍保留在数据库备份中。

模型不得编造 base64，也不能给服务一个 URL 或任意服务器文件路径让它自行读取。普通客户端可能不把 JSON data URI 显示为图片；此版本保证的是显式受权原图读写和 CLI 导出，**不保证每个客户端/模型都能看见或理解它**。不支持原生 MCP `ImageContent` 的客户端仍可下载原件。

## Orbis 聊天图片到 ST（需两端配套更新）

新宿主协议 `memory-image-upload/1` 把“给模型一个图片引用”和“传输真实文件”分开，不让模型生成大段 base64。仅更新 ST 或仅更新 Orbis 不等于这条链路已可用；旧客户端仍可使用上面的真实 `data_base64` / CLI 路径。

1. 在 Orbis 选择聊天图片后，助手通过本地只读图片列表取得 `stimgup_` 加 64 位小写十六进制的随机引用及 MIME/hash。此时标为 `prepared_not_uploaded`：**选图或列图不会自动上传，更不会自动存记忆**。
2. 助手明确选择目标记忆，调用 `store_memory_image`（或 compact 对应 action），填写原引用。真正调用获准后，Orbis 才以 `stage_memory_image_upload` 上传该图片原件，核对运输回执，再原样发送已获准的正式存图参数。不得把引用临时替换成 base64 绕过参数哈希绑定。
3. ST 暂存区只在当前进程的有界内存中，按 owner/model 隔离。引用有效期 15 分钟；同引用同原件重试不延长有效期。到期不可用，进程重启也会丢失暂存；重新列图/选择后由宿主重新准备，不自动重试正式写入。引用不是记忆编号或权限凭证。
4. 暂存回执 `decision=staged` 只说明运输已核对，不表示记忆已存。只有正式操作通过现有模块权限、目标权限和执行绑定并返回存入结果，才取得持久 `image_ref`。内存限额为每 owner/model 32 MiB/32 项、单进程总计 64 MiB/128 项；超限明确拒绝。
5. 读图仍须显式 `read_memory_image(include_data=true)`。ST 保持原 JSON 元数据/data URI 合同，配套 Orbis 只对已核对的图片合同做窄范围原生图片转换；不把任意 JSON/文本自动解析为图片。模型是否能看图取决于实际模型与连接配置，不能保证所有模型都支持视觉。

原件仍限 4 MiB，所有原图隐私、EXIF 保留和敏感目标限制不变。host-only 标记用于目录投影，不是额外写入授权；暂存工具既不出现在 compact 动作目录，也不应出现在模型目录。服务端不会接受任意本机路径、URL、owner/model 覆盖参数。

## 大小、隐私与备份

- PNG、JPEG、WebP，仅单帧；最大 **4 MiB 原图、2000 万像素、任一边 16384 像素**。完整解码核验，损坏、SVG、动画、格式/MIME不一致或超限明确拒绝，不截断、不自动压缩。
- 每个 owner/model 最多 2000 张、累计原图 512 MiB；软删除保留的原图仍占用此保护限额。列表最多每页20项。图片不加入自动 recall。
- CLI 列表若返回 next_offset，用 `--offset` 传该值继续；业务拒绝返回非零退出码。旧上传已软删除时会明确提示 image_removed_not_reattached，不把“未复活”误报为上传成功。
- 原图**逐字节保留**，包括 EXIF、定位或其他元数据。不要上传密钥截图；文字描述的凭据检查不等于能检测图片/EXIF里的秘密。本版不做 OCR、去定位、抹除元数据或自动识图。
- SQLite 新增独立附件和幂等回执表，原有正文不改写。完整数据库备份会包含图片；旧版“只导出文字记忆”的工具不会自动包含它。恢复与迁移前需确认备份保留新增表。
- 4 MiB 转 base64 最大 5,592,408 字符。CLI 的请求预算6 MiB、回执预算12 MiB（包含 FastMCP 同时返回 text 与 structuredContent 的两份原图）。服务器图像字段有明确上限；超过客户端/反向代理自身预算时应显示失败，不能据此声称已存入。未替第三方 MCP 客户端、反向代理或识图模型放宽大小限额。
- 源码回归只用生成的合成像素、临时数据库和隔离 MCP；配套手机链路的用户验收另行记录，不推广为所有客户端、模型或输入图片均通过。

## 本地回归

```text
python -B -m unittest tests.test_memory_images tests.test_memory_image_cli mcp_server.tests.test_memory_image_service mcp_server.tests.test_memory_image_transport packaging_tests.test_memory_images_backup -v
```

最终源码包的清单与公开树回归见[本版验证](UPDATE-VALIDATION.md)。客户端适配、模型视觉能力、ST 附件保存与供应商缓存是不同能力，应分别验证。
