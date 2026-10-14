# 0.2.0-preview.6 · 手机本地部署入口（实验性）

本次新增 Android ARM64 手机本地部署入口。ST 服务运行在手机的 Termux／独立 Ubuntu 24.04 中，同一手机的 Orbis 通过 `127.0.0.1` 接入；不要求另购电脑或 VPS。**这不是原生 Android App，也不是把模型下载到手机。真实手机全流程及后台可靠性尚待验收。**

## 新增与修复

- Linux ARM64／CPython 3.12 专用完整哈希锁，沿用原 Linux 的精确依赖版本；不修改既有 Windows 与 Linux x64 锁。
- 手机专用 `prepare → install → check → start` 引导，默认只预览，明确加 `--execute` 才执行。
- 使用独立 Ubuntu 别名和私有目录，验证源码、运行环境与安装归属；不覆盖、不接管未知旧安装，不自动迁移记忆。
- 输入凭证或启动前，检查私有权限、原子写入同步、SQLite WAL 与跨进程文件锁。检查失败就停止，不通过放松权限或删除哈希检查绕过。
- 接受新版 Termux Python 报告的 `Android` 宿主名称；ST 本体仍只装进符合要求的 Ubuntu/glibc 环境。
- Linux 启动器收到可处理的终止／挂断信号时，正常收尾自己启动的服务；手机外层在 Ctrl+C 后留出收尾时间，不按端口扫描或终止其他应用。
- 补充同设备连接说明、手机备份风险、失败恢复边界与验收清单。

## 使用前请注意

- 入口面向官方 Termux、Android ARM64、proot-distro 5.10.x／具备对应命令的 5.x；iOS、32 位 Android、没有 Android 运行环境的纯鸿蒙不在此路线内。
- 不把原生 Termux/Bionic 当成 manylinux，也不支持把私有数据放在共享下载目录或 `/sdcard`。
- 至少预留 3 GiB 存储；下载环境和依赖需要网络。模型 API 的收费仍由所配置的服务商决定。
- 手机锁屏、节电、强停或系统回收可能结束服务。SIGKILL 无法保证正常收尾，不承诺长期保活或定时消息必达。
- 新入口用于独立新实例。已有手机部署或更换源码版本必须先备份并单独安排升级，不能直接删除归属标记或覆盖数据。
- 本次不更改数据库结构、模型路由、记忆语义或生产实例；已有情绪层、星图和图片存取能力保留。

[手机本地部署](docs/PHONE-LOCAL.md) · [常规安装](docs/INSTALL.md) · [本版验证与限制](docs/UPDATE-VALIDATION.md) · [依赖说明](docs/DEPENDENCIES.md)
