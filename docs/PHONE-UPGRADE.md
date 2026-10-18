# 手机本地 ST：保留数据升级与回退

适用：用 `install_phone.py` 建立的 Termux → 专用 Ubuntu 24.04 ARM64 实例，仍保留完整旧源码、`phone-owner.json` 和原专用目录。其他手动部署、改过安装归属、丢失旧源码的实例，不套用本流程。**不卸载 Termux、不清数据、不重建发行版、不重新生成密钥。**

这是显式维护操作，会有停机时间，不在聊天中自动运行。脚本回归测试与真实 Termux 手机验收不同；本次新增升级/回退流程尚需真机验收，不宣称所有机型已通过。

## 1. 停止原服务，保留旧源码

在原来运行 ST 的 Termux 终端按一次 `Ctrl+C`，等待正常退出。若手动启动过其他写同一数据库的进程，也要先停止；脚本不会替你杀别的进程。以下命令在 **Termux 外层** 执行，不是在 Ubuntu 里面。

不要在旧源码目录直接 `git pull` 或切换版本。安装标记绑定旧源码内容；保留原目录是校验与回退的依据。另取一个尚不存在的新目录：

```sh
cd "$HOME"
git clone --depth 1 https://github.com/AZHi-xinxin/StillerBrain.git StillerBrain-phone-update
cd "$HOME/StillerBrain-phone-update"
python -B scripts/install_phone.py upgrade --from-source "$HOME/StillerBrain-phone-source" --directory "$HOME/StillerBrain-phone"
```

最后一条不带 `--execute`，只显示说明，不改数据。若旧目录名不同，改成自己的**原绝对路径**。使用过自定义发行版别名的，每一步加相同的 `--name stiller-你的别名`。已有 `StillerBrain-phone-update` 时用另一个全新目录名，不能覆盖它。

## 2. 执行升级（备份先于切换）

```sh
python -B scripts/install_phone.py upgrade --from-source "$HOME/StillerBrain-phone-source" --directory "$HOME/StillerBrain-phone" --execute
```

入口会按顺序：

1. 校验新旧源码、旧安装归属、同一 Ubuntu 实例及操作锁，检查服务端口已停止。
2. 用 SQLite 备份接口制作主库、想法库、保险库三份离线快照，检查完整性和哈希。
3. 复制已有私有配置与数据准备候选副本；原实例连同原运行环境完整保留。
4. 为候选安装锁定依赖，短暂启动三个本机服务检查，随后停止；**不请求真实模型**。
5. 确认 `config.env` 与密码授权文件未变化，成功后才更新源码归属标记。

备份位于终端打印的 `~/StillerBrain-phone/phone-upgrades/upgrade-…/`：`databases/` 是带校验清单的三库备份，`original/` 是切换前的完整私有实例，包括原运行环境和配置。目录含网关 Key、MCP 令牌及私人记忆，**请勿公开、发群或放到共享存储**。它不是跨设备灾备；卸载 Termux 仍会删除 Termux 私有空间。

至少需要 3 GiB 额外可用空间；数据较大时脚本会要求更多。失败会保留现场，不自动删除目录腾空间。不要用“删掉备份/所有权标记”绕过失败。

## 3. 检查与启动

升级成功后都从**新源码目录**执行：

```sh
python -B scripts/install_phone.py check --directory "$HOME/StillerBrain-phone" --execute
python -B scripts/install_phone.py start --directory "$HOME/StillerBrain-phone" --execute
```

第二条是前台服务，保持终端，`Ctrl+C` 停止。原网关 Key、MCP 令牌、端口、模型配置、星图授权数据和三库记忆均沿用，因此没有主动要求 Orbis 重新连接或重新授权。升级检查不等于真实上游模型响应测试，请再在 Orbis 发一条测试消息。

从 `preview.7` 这类同格式手机安装升级，不要求先单独安装 `0.2.0`；仍必须通过脚本实际的归属、数据库和服务检查，不能手动改标记宣称兼容。Orbis 是否同时更新按客户端发布说明决定；它不是数据库升级步骤。

## 4. 失败与回退

升级未完成时不要用旧源码强行启动候选数据。可从同一新源码重复同一条 `upgrade`（已激活的候选可继续依赖安装）；准备阶段被中断时先执行回退。回退前同样正常停服务：

```sh
cd "$HOME/StillerBrain-phone-update"
python -B scripts/install_phone.py rollback --directory "$HOME/StillerBrain-phone" --execute
```

然后回原源码检查、启动：

```sh
cd "$HOME/StillerBrain-phone-source"
python -B scripts/install_phone.py check --directory "$HOME/StillerBrain-phone" --execute
python -B scripts/install_phone.py start --directory "$HOME/StillerBrain-phone" --execute
```

回退恢复的是**升级前**实例。升级后新增的数据不会合并进旧库，也不会删除：它们保留在同次维护目录的 `after-upgrade/`。如已在新版积累重要记录，请先考虑是否确实要回退，保留两份副本，不手动混合数据库。当前入口只自动处理最近一次升级；更早的维护目录一直保留，不能拿别次目录替换本次记录。

## 验证范围

离线测试覆盖旧归属拒绝原地换源码、新旧归属切换、提交标记中断后回退、配置不变、三库快照、原环境保留、重复执行、升级后新增记录留在副本中。无付费模型调用、无自动密钥轮换。Android 厂商省电、PRoot 下载及网络、整机断电仍须实际设备验证。
