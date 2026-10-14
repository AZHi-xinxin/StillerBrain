"""Opt-in experimental Termux -> Ubuntu 24.04 ARM64 installation bridge.

No root, shell pipelines, shared storage, migration, reset or automatic cleanup.
The host Python only orchestrates; ST runs in the guest's glibc CPython 3.12.
CLI/storage contract: https://github.com/termux/proot-distro (v5.10).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import signal
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "stiller-phone-bridge/1"
IMAGE = "ubuntu:24.04"
DEFAULT_NAME = "stiller-phone-ubuntu2404"
GUEST_SOURCE = "/opt/stiller-source"
GUEST_STATE = "/opt/stiller-state"
MIN_FREE = 3 * 1024**3
RUNTIME_PROBE = (
    "import json,platform,sys,sysconfig; print(json.dumps({"
    "'machine':platform.machine(),'python':list(sys.version_info[:2]),"
    "'implementation':platform.python_implementation(),'libc':list(platform.libc_ver()),"
    "'bits':64 if sys.maxsize>2**32 else 32,"
    "'free_threaded':bool(sysconfig.get_config_var('Py_GIL_DISABLED'))}))"
)


class PhoneError(ValueError):
    """Fixed, credential-free user-facing explanation."""


def plain_path(path):
    """Reject links before resolving: do not allow a home/storage symlink escape."""
    path = Path(path).expanduser()
    if (not path.is_absolute() or any(ord(c) < 32 or ord(c) == 127 for c in str(path))
            or ":" in str(path)[len(path.anchor):]
            or ".." in path.parts):
        raise PhoneError("请使用不含冒号或上级跳转的绝对私有路径。")
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise PhoneError("路径不能经过符号链接或共享存储快捷方式。")
    return path.resolve()


def beneath(path, base):
    return path != base and base in path.parents


def layout(source, state, home, prefix, name):
    if not re.fullmatch(r"stiller-[a-z0-9][a-z0-9_-]{0,47}", name):
        raise PhoneError("发行版别名须以 stiller- 开头，只含小写字母、数字、下划线和短横线。")
    source, state, home, prefix = map(plain_path, (source, state, home, prefix))
    if not beneath(source, home) or not beneath(state, home):
        raise PhoneError("源码和专用目录都必须在 Termux 私有 home 中，不能放 /sdcard、下载目录或共享存储。")
    if source == state or source in state.parents or state in source.parents:
        raise PhoneError("专用目录必须与源码分开，不能互相包含。")
    if not source.is_dir() or not state.parent.is_dir():
        raise PhoneError("源码与专用目录的上级目录必须已经存在。")
    runtime = plain_path(prefix / "var/lib/proot-distro")
    container = plain_path(runtime / "containers" / name)
    legacy = plain_path(runtime / "installed-rootfs" / name)
    return {"source": source, "state": state, "home": home, "prefix": prefix,
            "runtime": runtime, "container": container, "legacy": legacy, "name": name}


def host_layout(args):
    # CPython 3.13+ Android builds may report "Android" (not "Linux").
    # This is the host-only orchestrator gate; ST's guest still requires glibc.
    if platform.system() not in ("Linux", "Android") or platform.machine().lower() not in ("aarch64", "arm64"):
        raise PhoneError("执行入口只支持 Android ARM64 上的 Termux；本机 Python 只用于引导。")
    if not hasattr(os, "getuid") or os.getuid() == 0:
        raise PhoneError("请在普通 Termux 会话执行，不要使用 root 或嵌套发行版。")
    if os.environ.get("container") or os.environ.get("PROOT_TMP_DIR"):
        raise PhoneError("请先退出已有 Linux/PRoot 会话，在 Termux 外层执行。")
    prefix = plain_path(os.environ.get("PREFIX", ""))
    if not re.fullmatch(r"/data/(?:data|user/\d+)/com\.termux/files/usr", str(prefix)):
        raise PhoneError("未识别官方 Termux 私有目录；此入口不会在未知容器或共享存储中安装。")
    if not Path("/system/build.prop").exists() or not prefix.is_dir():
        raise PhoneError("未确认 Android/Termux 环境，未执行安装。")
    home = prefix.parent / "home"
    return layout(ROOT, args.directory or home / "StillerBrain-phone", home, prefix, args.name)


def clean_environment(ctx):
    # Never pass provider keys, proxy credentials or PRoot override hooks to downloads.
    env = {key: os.environ[key] for key in ("TERM", "COLORTERM", "LANG", "LC_ALL",
            "TERMUX_VERSION", "TERMUX_APP__APP_VERSION_NAME") if key in os.environ}
    env.update({"HOME": str(ctx["home"]), "PREFIX": str(ctx["prefix"]),
                "TERMUX__HOME": str(ctx["home"]), "TERMUX__PREFIX": str(ctx["prefix"]),
                "TERMUX_APP__PACKAGE_NAME": "com.termux",
                "PATH": str(ctx["prefix"] / "bin") + ":/system/bin",
                "TMPDIR": str(ctx["prefix"] / "tmp"), "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"})
    return env


def execute(ctx, command, *, capture=False, timeout=None, include_stderr=False):
    result = subprocess.run(list(map(str, command)), cwd=ctx["source"],
                            env=clean_environment(ctx), shell=False, check=False,
                            capture_output=capture, text=True, timeout=timeout)
    if result.returncode:
        raise PhoneError("当前步骤未完成，已有目录保留。不会重置发行版、替换密钥或删除记忆。")
    if capture:
        output = result.stdout + (getattr(result, "stderr", "") if include_stderr else "")
        if len(output) > 128 * 1024:
            raise PhoneError("检查返回内容异常，已停止。")
        return output
    return ""


def execute_foreground(ctx, command):
    """Let the guest supervisor finish after Ctrl+C; never kill other sessions.

    subprocess.run kills its direct child immediately on KeyboardInterrupt.
    That can take PRoot away before ST closes SQLite. This wrapper instead
    gives the existing foreground process group time to handle terminal SIGINT.
    PRoot's default kill-on-exit remains enabled as the last-resort boundary.
    """
    child = subprocess.Popen(list(map(str, command)), cwd=ctx["source"],
                             env=clean_environment(ctx), shell=False)
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)

    def term_requested(_signum, _frame):
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, term_requested)
    try:
        try:
            result = child.wait()
        except KeyboardInterrupt:
            # Ignore repeated shutdown keystrokes only during the bounded wait.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print("正在等待本次 ST 服务正常收尾，请稍候……", flush=True)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.terminate()  # Only this wrapper's own PRoot child.
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            raise
        if result:
            raise PhoneError("本次前台 ST 进程已退出；请查看私有日志，不会自动重启、重置或重新请求模型。")
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def proot_binary(ctx):
    path = ctx["prefix"] / "bin/proot-distro"
    proot = ctx["prefix"] / "bin/proot"
    if any(not item.is_file() or not os.access(item, os.X_OK) or not beneath(item.resolve(), ctx["prefix"])
           for item in (path, proot)):
        raise PhoneError("请先在 Termux 手动运行 pkg install python proot-distro，再运行 proot-distro --help 查看末尾版本；不会自动升级现有软件。")
    return str(path)


def validate_proot(ctx):
    binary = proot_binary(ctx)
    # v5.10 has no --version switch. Official help is written to stderr and
    # its footer is "PRoot-Distro version '5.10.0' by Termux ...".
    version = execute(ctx, [binary, "--help"], capture=True, timeout=15, include_stderr=True)
    match = re.search(r"PRoot-Distro\s+version\s+'(\d+)\.(\d+)\.(\d+)'", version)
    if not match or not ((5, 10, 0) <= tuple(map(int, match.groups())) < (6, 0, 0)):
        raise PhoneError("此入口需要已核对 CLI 的 proot-distro 5.10.x 或更新的 5.x。请运行 proot-distro --help 查看末尾版本；旧版仅提供说明，不会自动升级或套用旧版命令。请参考官方 Termux 文档更新后重试。")
    for operation, flags in (("install", ("--name", "--architecture")),
                             ("login", ("--isolated", "--bind", "--work-dir"))):
        help_text = execute(ctx, [binary, operation, "--help"], capture=True, timeout=15, include_stderr=True)
        if not all(flag in help_text for flag in flags):
            raise PhoneError("当前 proot-distro 缺少已核对的命令参数，未修改任何发行版。")


def source_digest(ctx):
    source = plain_path(ctx["source"])
    spec = importlib.util.spec_from_file_location("phone_source_check", source / "scripts/check_source_package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.failures(source):
        raise PhoneError("源码完整性检查未通过；请使用完整发行源码。尚未下载或安装手机运行环境。")
    return hashlib.sha256((source / "SOURCE-PACKAGE-MANIFEST.json").read_bytes()).hexdigest()


def read_json(path, limit=65536):
    path = plain_path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise PhoneError("安装标记不是普通文件或内容过大，拒绝接管。")
        value = json.loads(stream.read(limit + 1))
    if not isinstance(value, dict):
        raise PhoneError("安装标记不完整，拒绝接管。")
    return value


def exclusive_json(path, value):
    plain_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def identity(ctx, digest):
    return {"format": FORMAT, "name": ctx["name"], "source": str(ctx["source"]),
            "state": str(ctx["state"]), "container": str(ctx["container"]),
            "source_manifest_sha256": digest, "image": IMAGE, "arch": "aarch64"}


def state_record(ctx, digest, *, create=False):
    expected = identity(ctx, digest)
    plain_path(ctx["state"])
    if not ctx["state"].exists():
        if not create:
            raise PhoneError("请先显式执行 prepare；未找到本引导创建的专用目录。")
        if ctx["container"].exists() or ctx["legacy"].exists():
            raise PhoneError("此发行版别名已存在，不会接管或覆盖；请选择新的 stiller- 别名和专用目录。")
        ctx["state"].mkdir(mode=0o700)
        exclusive_json(ctx["state"] / "phone-owner.json", {**expected, "owner": secrets.token_hex(32)})
    record = read_json(ctx["state"] / "phone-owner.json")
    if (any(record.get(key) != value for key, value in expected.items())
            or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("owner", "")))):
        raise PhoneError("专用目录不属于本源码/别名或源码已更换。请保留原目录并检查备份，不会自动迁移或覆盖。")
    if os.name != "nt" and ctx["state"].stat().st_mode & 0o077:
        raise PhoneError("专用目录权限过宽；请在确认该目录归属后自行改为仅当前用户可访问。")
    return record


@contextmanager
def state_lock(ctx):
    import fcntl
    path = plain_path(ctx["state"] / "phone-operation.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PhoneError("操作锁不是普通文件，已停止。")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PhoneError("此实例已有安装、检查或前台运行任务；请先在原终端正常停止。") from None
        yield
    finally:
        os.close(fd)


def manifest_digest(ctx):
    plain_path(ctx["container"] / "rootfs")
    if not (ctx["container"] / "rootfs").is_dir():
        raise PhoneError("发行版目录不完整，不会重建或覆盖。")
    path = ctx["container"] / "manifest.json"
    manifest = read_json(path, limit=1024 * 1024)
    if manifest.get("image_ref") != IMAGE or manifest.get("arch") != "aarch64":
        raise PhoneError("发行版来源不是本次 Ubuntu 24.04 ARM64，拒绝接管。")
    config = manifest.get("image_config", {})
    metadata = manifest.get("manifest", {})
    descriptor = metadata.get("config", {}) if isinstance(metadata, dict) else {}
    digest = descriptor.get("digest", "") if isinstance(descriptor, dict) else ""
    if (not isinstance(config, dict) or config.get("architecture") != "arm64" or config.get("os") != "linux"
            or not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)):
        raise PhoneError("发行版缺少有效的 ARM64 OCI 来源记录，已停止。")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def owned_container(ctx, record):
    if ctx["legacy"].exists():
        raise PhoneError("发现同名旧版发行版；不会自动迁移或接管。")
    owner = read_json(ctx["container"] / ".stiller-phone-owner.json")
    if (owner.get("identity") != record or owner.get("manifest_sha256") != manifest_digest(ctx)):
        raise PhoneError("发行版所有权或来源记录不一致，不会继续执行。")
    return owner


def guest_command(ctx, command):
    # PRoot's isolated mode is not a security sandbox; it avoids unnecessary binds.
    return [proot_binary(ctx), "login", "--isolated",
            "--bind", str(ctx["source"]) + ":" + GUEST_SOURCE,
            "--bind", str(ctx["state"]) + ":" + GUEST_STATE,
            "--work-dir", GUEST_SOURCE, ctx["name"], "--", "/usr/bin/env", "-i",
            "HOME=/root", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C.UTF-8",
            "DEBIAN_FRONTEND=noninteractive", "PYTHONDONTWRITEBYTECODE=1", *command]


def verify_os(ctx):
    content = execute(ctx, guest_command(ctx, ["/usr/bin/cat", "/etc/os-release"]), capture=True, timeout=30)
    values = {}
    for line in content.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in ("ID", "VERSION_ID"):
            values[key] = value.strip('"')
    if values != {"ID": "ubuntu", "VERSION_ID": "24.04"}:
        raise PhoneError("客体系统不是 Ubuntu 24.04，未运行包安装。")


def verify_runtime(ctx):
    raw = execute(ctx, guest_command(ctx, ["/usr/bin/python3.12", "-I", "-B", "-c", RUNTIME_PROBE]),
                  capture=True, timeout=30)
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise PhoneError("客体 Python 检查返回结构异常，未调用 ST 安装器。")
    libc = result.get("libc", [])
    if (result.get("machine") not in ("aarch64", "arm64") or result.get("python") != [3, 12]
            or result.get("implementation") != "CPython" or result.get("bits") != 64
            or result.get("free_threaded") is not False or not isinstance(libc, list) or len(libc) != 2 or libc[0] != "glibc"
            or not isinstance(libc[1], str) or not re.fullmatch(r"\d+\.\d+", libc[1])
            or tuple(map(int, libc[1].split("."))) < (2, 34)):
        raise PhoneError("客体不是要求的 ARM64/glibc/标准 CPython 3.12，未调用 ST 安装器。")


def verify_storage(ctx):
    raw = execute(ctx, guest_command(ctx, ["/usr/bin/python3.12", "-I", "-B",
                  GUEST_SOURCE + "/scripts/check_phone_storage.py", "--parent", GUEST_STATE]),
                  capture=True, timeout=60)
    result = json.loads(raw)
    expected = ("private_permissions", "atomic_replace_fsync", "sqlite_wal", "process_lock")
    if (not isinstance(result, dict) or result.get("ok") is not True or result.get("supported") is not True or result.get("errors") != []
            or not isinstance(result.get("capabilities"), dict)
            or not all(result.get("capabilities", {}).get(key) is True for key in expected)):
        raise PhoneError("手机客体的私有权限/持久化/数据库/跨进程锁检查未通过，未进入凭证输入或启动服务。")


def prepare(ctx, record):
    if not ctx["container"].exists():
        if ctx["legacy"].exists():
            raise PhoneError("同名旧版发行版已存在，拒绝自动迁移。")
        execute(ctx, [proot_binary(ctx), "install", IMAGE, "--name", ctx["name"], "--architecture", "aarch64"])
        # A failed/unknown install is NEVER adopted on retry. Only a confirmed
        # success may write ownership; an existing unmarked rootfs is preserved.
        digest = manifest_digest(ctx)
        exclusive_json(ctx["container"] / ".stiller-phone-owner.json",
                       {"identity": record, "manifest_sha256": digest})
    owned_container(ctx, record)
    verify_os(ctx)
    ready = ctx["state"] / "phone-prepared.json"
    if ready.exists():
        if read_json(ready) != {"identity": record}:
            raise PhoneError("准备状态不一致，未修改现有安装。")
        verify_runtime(ctx)
        verify_storage(ctx)
        print("已准备，无需重装发行版或 Python。")
        return
    execute(ctx, guest_command(ctx, ["/usr/bin/apt-get", "-o", "Acquire::Retries=2",
                                   "-o", "APT::Update::Error-Mode=any", "update"]))
    execute(ctx, guest_command(ctx, ["/usr/bin/apt-get", "install", "--yes", "--no-install-recommends",
                                   "python3.12", "python3.12-venv", "ca-certificates"]))
    verify_runtime(ctx)
    verify_storage(ctx)
    exclusive_json(ready, {"identity": record})
    print("Ubuntu/Python 已准备。下一步显式运行 install；尚未配置或调用真实模型。")


def operate(ctx, record, action):
    owned_container(ctx, record)
    if read_json(ctx["state"] / "phone-prepared.json") != {"identity": record}:
        raise PhoneError("请先完成 prepare，未启动 ST。")
    verify_os(ctx)
    verify_runtime(ctx)
    verify_storage(ctx)
    private = GUEST_STATE + "/private"
    python = "/usr/bin/python3.12" if action == "install" else private + "/venv/bin/python"
    option = {"install": "--directory", "start": "--start", "check": "--verify"}[action]
    command = [python, "-B", GUEST_SOURCE + "/scripts/install_stiller.py", option, private]
    print("凭证仅在原安装向导中隐藏输入；连接资料只保存在专用目录/private。")
    print("仅监听 127.0.0.1，供同一手机上的 Orbis 连接；不是局域网公开服务。")
    if action == "start":
        print("运行期间保留此 Termux 终端，Ctrl+C 正常停止。Android 锁屏/省电可能结束进程，不能保证后台常驻。")
    if action == "start":
        execute_foreground(ctx, guest_command(ctx, command))
    else:
        execute(ctx, guest_command(ctx, command))


def explain(action):
    print("实验性手机本地路线：Termux ARM64 → 独立 Ubuntu 24.04 → glibc CPython 3.12 → ST。")
    print("不使用 Termux 原生 Python 安装 ST 依赖；不 root、不改 SELinux、不迁移已有发行版。")
    print("prepare 下载 Ubuntu 与系统 Python；install 下载锁定依赖并隐藏输入模型配置。")
    print("start 前台运行；check 短暂启动本实例的本机服务检查，结束后停止，不请求真实模型。")
    print("请预留至少 3 GiB 可用存储（不是保证足够；后续记忆另占空间），联网下载会产生流量。")
    print("源码和专用目录仅限 Termux 私有 home。保留备份；卸载 Termux/清除其数据会丢失本地安装与记忆。")
    print("PRoot 不是安全隔离边界；后台可靠性、厂商省电策略仍需对应手机验证。")
    if action:
        print("当前为预览，未执行 " + action + "。确认后在命令末尾加 --execute。")
    else:
        print("用法：python -B scripts/install_phone.py {prepare,install,start,check} [--directory 私有专用目录] [--name stiller-别名] --execute")


def main(argv=None):
    parser = argparse.ArgumentParser(description="实验性手机 ST 引导；默认仅说明，不安装任何内容。")
    parser.add_argument("action", nargs="?", choices=("prepare", "install", "start", "check"))
    parser.add_argument("--execute", action="store_true", help="明确执行当前步骤；不传时仅预览")
    parser.add_argument("--directory", type=Path, help="Termux 私有 home 下的专用目录；与源码分开")
    parser.add_argument("--name", default=DEFAULT_NAME, help="专用发行版别名，须以 stiller- 开头")
    args = parser.parse_args(argv)
    explain(args.action if not args.execute else None)
    if not args.action or not args.execute:
        return 0
    try:
        ctx = host_layout(args)
        digest = source_digest(ctx)  # Before any download, apt, ownership creation or login.
        validate_proot(ctx)
        if args.action == "prepare":
            import shutil
            if shutil.disk_usage(ctx["home"]).free < MIN_FREE:
                raise PhoneError("可用存储不足 3 GiB，未创建新安装；请先自行腾出空间。")
        record = state_record(ctx, digest, create=args.action == "prepare")
        with state_lock(ctx):
            if args.action == "prepare":
                prepare(ctx, record)
            else:
                operate(ctx, record, args.action)
        return 0
    except KeyboardInterrupt:
        print("\n已中断；保留已创建的目录，不自动删除或重置。")
        return 130
    except (PhoneError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        error = sys.exc_info()[1]
        print("手机引导提示：" + (str(error) if isinstance(error, PhoneError) else
              "未能完成或读取确认记录，已有目录原样保留。请核对上一步输出；不要删除记忆或重置发行版。"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
