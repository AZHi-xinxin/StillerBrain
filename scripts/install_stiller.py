"""Interactive, private, source-separated installation. Standard-library bootstrap."""
from __future__ import annotations

import argparse
import contextlib
import csv
import ctypes
from ctypes import wintypes
import getpass
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import socket
import subprocess
import sys
import sysconfig
import time
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid
import warnings

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "stiller-installer/1"


class InstallError(ValueError):
    """Credential-free explanation suitable for the terminal."""


def helper(name):
    spec = importlib.util.spec_from_file_location("st_install_" + name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def select_lock(system=None, machine=None, version=None, implementation=None,
                libc=None, free_threaded=None, bits=None):
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    version = sys.version_info[:2] if version is None else version
    implementation = platform.python_implementation() if implementation is None else implementation
    libc = platform.libc_ver() if libc is None else libc
    free_threaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED")) if free_threaded is None else free_threaded
    bits = (64 if sys.maxsize > 2**32 else 32) if bits is None else bits
    if implementation != "CPython" or machine.lower() not in ("amd64", "x86_64") or free_threaded or bits != 64:
        raise InstallError("向导支持 x64 标准版 CPython：Windows 3.14 或 Linux 3.12。请核对系统和 Python。")
    if system == "Windows" and tuple(version) == (3, 14):
        return "requirements-windows-py314.lock"
    if system == "Linux" and tuple(version) == (3, 12) and libc[0] == "glibc":
        match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", libc[1])
        if match and tuple(map(int, match.group(1, 2))) >= (2, 34):
            return "requirements-linux-py312.lock"
    raise InstallError("环境暂未纳入向导：Windows x64 请用 Python 3.14；Linux x64 请用 Python 3.12、glibc ≥ 2.34（已验 Ubuntu 24.04）。")


def check_text(value, label, allow_empty=False):
    if any(c in value for c in "\r\n\0"):
        raise InstallError(label + " 请填写单行内容。")
    value = value.strip()
    if (not value and not allow_empty) or len(value) > 4096:
        raise InstallError(label + " 为空或过长，请核对。")
    return value


def safe_directory(path, root=ROOT):
    path = Path(check_text(str(path), "目录")).expanduser()
    if not path.is_absolute():
        raise InstallError("请填写绝对路径，并把私有目录放在源码文件夹之外。")
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise InstallError("私有路径及其上级目录须为普通目录，不能经过链接或 junction。")
    path = path.resolve()
    root = root.resolve()
    if path == root or root in path.parents or path in root.parents:
        raise InstallError("请选择与源码分开的私有目录，不能包含源码或位于源码内。")
    return path


def protect_directory(path):
    if os.name != "nt":
        path.chmod(0o700)
        return
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    result = subprocess.run([str(system32 / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                            capture_output=True, text=True, timeout=15, check=True)
    rows = list(csv.reader(io.StringIO(result.stdout)))
    sid = rows[0][-1] if rows else ""
    if not re.fullmatch(r"S-1-5-\d+(?:-\d+)+", sid):
        raise InstallError("未能确认当前 Windows 用户，尚未保存密钥。")
    # Replace the DACL on this freshly created directory, rather than retaining
    # pre-existing explicit grants through icacls /grant. Never applied on resume.
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
    convert.restype = wintypes.BOOL
    set_security = advapi.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    set_security.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not convert("D:P(A;OICI;FA;;;" + sid + ")(A;OICI;FA;;;SY)", 1, ctypes.byref(descriptor), None):
        raise InstallError("无法建立 Windows 私有目录权限，尚未保存密钥。")
    try:
        applied = set_security(str(path), 0x00000004 | 0x80000000, descriptor)
    finally:
        kernel.LocalFree(descriptor)
    if not applied:
        raise InstallError("无法为私有目录设置访问权限，尚未保存密钥；请选择本机 NTFS 目录。")


def exclusive_write(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def make_config(private, base_url, model, api_key, ports):
    starter = helper("run_component")
    base_url = check_text(base_url, "模型地址").rstrip("/")
    model = check_text(model, "模型 ID")
    api_key = check_text(api_key, "API Key")
    identity = uuid.uuid4().hex
    config = {name: secrets.token_urlsafe(36) for name in starter.SECRET_KEYS}
    config.update({
        "STBRAIN_OWNER_ID": "ai:" + identity,
        "STBRAIN_MODEL_ID": "model:" + identity,
        "STBRAIN_HUMAN_ACTOR_ID": "human:" + uuid.uuid4().hex,
        "STBRAIN_EXECUTION_EPOCH": uuid.uuid4().hex,
        "STBRAIN_REQUIRE_EXECUTION_BINDING": "1",
        "STBRAIN_ACCESS_PROFILE": "simple-memory-v1",
        "STBRAIN_SELF_PASSWORD_HASH_FILE": "",
        "STBRAIN_DB_PATH": str(private / "self-model.db"),
        "STBRAIN_LEARNING_IDEA_DB_PATH": str(private / "learning-ideas.db"),
        "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(private / "hallucination-vault.db"),
        "STBRAIN_MCP_ISSUER_URL": "http://127.0.0.1:" + str(ports[0]),
        "STBRAIN_MCP_RESOURCE_URL": "http://127.0.0.1:" + str(ports[0]) + "/mcp",
        "STBRAIN_MCP_ALLOWED_HOSTS": "127.0.0.1:*,localhost:*",
        "STBRAIN_MCP_ALLOWED_ORIGINS": "",
        "STBRAIN_CONTROL_URL": "http://127.0.0.1:" + str(ports[1]),
        "STBRAIN_GATEWAY_HOST_ID": "stiller-local-gateway",
        "STBRAIN_GATEWAY_CONTRACT": "rikkahub-openai-gateway/2",
        "STBRAIN_GATEWAY_CONTEXT_LAYOUT": "tail-context-v2",
        "STBRAIN_UPSTREAM_BASE_URL": base_url,
        "STBRAIN_UPSTREAM_API_KEY": api_key,
        "STBRAIN_GATEWAY_MODEL": model,
        "STBRAIN_UPSTREAM_MODEL": model,
    })
    for component, port in zip(("MCP", "CONTROL", "GATEWAY"), ports):
        config["STBRAIN_" + component + "_HOST"] = "127.0.0.1"
        config["STBRAIN_" + component + "_PORT"] = str(port)
    return config


def validate_config(config, private, root=ROOT):
    starter = helper("run_component")
    try:
        starter.parse_config("".join(key + "=" + value + "\n" for key, value in config.items()))
    except ValueError:
        raise InstallError("配置须为单行原始值；模型地址、模型名称或密钥请勿添加引号。") from None
    for name in starter.DB_KEYS:
        path = safe_directory(Path(config[name]), root)
        if path.parent != private:
            raise InstallError("此向导只管理本私有目录中的三个数据库。请使用原维护流程处理外部数据库。")
    if config.get("STBRAIN_SELF_PASSWORD_HASH_FILE"):
        if safe_directory(Path(config["STBRAIN_SELF_PASSWORD_HASH_FILE"]), root).parent != private:
            raise InstallError("本向导实例的模块一密码哈希应保存在同一私有目录中。")
    for component in starter.COMPONENTS:
        try:
            starter.validate(config, component, root=root)
        except ValueError:
            raise InstallError("配置校验未通过：请核对 HTTPS 模型地址、真实模型 ID、API Key 和端口。") from None


def connection_text(config):
    return ("ST 客户端连接资料（含访问密钥，请私下保存）\n\n"
            "仅供同一台电脑使用；手机需要另行配置受保护的远程连接。\n\n"
            "一、模型网关（自动浮现）\n接口类型：OpenAI 兼容 Chat Completions\nBase URL：http://127.0.0.1:"
            + config["STBRAIN_GATEWAY_PORT"] + "/v1\n路径：/chat/completions\n模型 ID："
            + config["STBRAIN_GATEWAY_MODEL"] + "\nAPI Key：" + config["STBRAIN_GATEWAY_TOKEN"]
            + "\n\n二、MCP（读、存、改记忆，两种使用方式都需要）\n传输：Streamable HTTP\nURL：http://127.0.0.1:"
            + config["STBRAIN_MCP_PORT"] + "/mcp\n认证请求头：Authorization: Bearer " + config["STBRAIN_MCP_TOKEN"]
            + "\n\n上游模型的 API Key 已保存在 config.env。客户端网关栏填上面自动生成的网关密钥。\n"
            "连接后请让 AI 先调用 stbrain_help，完成自己的初始设置。\n")


def create_installation(private, base_url, model, api_key, ports, password="", root=ROOT):
    lock = select_lock()
    private = safe_directory(private, root)
    if private.exists() or not private.parent.is_dir():
        raise InstallError("新安装需要尚不存在的目录，且它的上级目录已经存在。已有安装请重跑向导继续。")
    config = make_config(private, base_url, model, api_key, ports)
    password = check_text(password, "模块一密码", allow_empty=True)
    if len(password) > 1024:
        raise InstallError("模块一密码最多 1024 个字符。")
    validate_config(config, private, root)
    private.mkdir(mode=0o700)
    protect_directory(private)
    if password:
        target = private / "self-password.json"
        helper("configure_self_password").create_hash(target, password, password, root=root)
        config["STBRAIN_SELF_PASSWORD_HASH_FILE"] = str(target)
    exclusive_write(private / "config.env", "".join(key + "=" + value + "\n" for key, value in config.items()))
    launch = quote_command([venv_python(private), "-B", root / "scripts/install_stiller.py", "--start", private])
    exclusive_write(private / "连接资料（请勿公开）.txt", connection_text(config)
                    + "\n今后启动命令（运行时保留终端，Ctrl+C 停止）：\n" + launch
                    + "\n\n请保留源码与私有目录的现有位置。\n")
    exclusive_write(private / "installer.json", json.dumps({"format": FORMAT, "source": str(root.resolve()),
                                                            "lock": lock}, ensure_ascii=False, indent=2) + "\n")
    return config


def read_installation(private, root=ROOT):
    private = safe_directory(private, root)
    try:
        for name in ("installer.json", "config.env", "self-password.json", "venv", "连接资料（请勿公开）.txt"):
            safe_directory(private / name, root)
        marker_file = private / "installer.json"
        config_file = private / "config.env"
        if marker_file.stat().st_size > 4096 or config_file.stat().st_size > 65536:
            raise InstallError("安装标记或配置文件过大。")
        marker = json.loads(marker_file.read_text(encoding="utf-8"))
        if (not isinstance(marker, dict) or marker.get("format") != FORMAT or marker.get("source") != str(root.resolve())
                or marker.get("lock") != select_lock()):
            raise InstallError("该目录属于其他安装或源码位置。本向导会保留它，请使用对应原安装入口。")
        config = helper("run_component").parse_config(config_file.read_text(encoding="utf-8"))
        validate_config(config, private, root)
        return config
    except InstallError:
        raise
    except (OSError, UnicodeError, ValueError, KeyError):
        raise InstallError("该目录没有完整的本向导安装标记。目录原样保留，请选一个新的目录或检查原安装。") from None


def hidden_input(prompt):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except getpass.GetPassWarning:
        raise InstallError("此窗口无法隐藏输入。请在系统终端重新运行向导，再填写密钥。") from None


def free_ports():
    chosen = []
    with contextlib.ExitStack() as stack:
        for port in range(18794, 18994):
            sock = stack.enter_context(socket.socket())
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            chosen.append(port)
            if len(chosen) == 3:
                return tuple(chosen)
    raise InstallError("未找到三个空闲本机端口。现有服务保持不变，请检查端口占用。")


def venv_python(private):
    return private / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def clean_environment():
    return {**{k: v for k, v in os.environ.items() if not k.startswith("STBRAIN_")
               and k not in ("PYTHONPATH", "PYTHONHOME")},
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}


def run_private(command, log, timeout=900):
    result = subprocess.run(command, cwd=ROOT, env=clean_environment(), stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise InstallError("此步骤未完成。原配置和密钥已保留；可重跑向导继续。详情在私有安装日志中，分享前请隐藏凭证。")


def private_log(private):
    name = private / ("install-" + uuid.uuid4().hex[:12] + ".log")
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "wb")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InstallError("本机检查遇到重定向，已停止。")


def verify_services(config, private):
    """Only localhost health and MCP catalogue; terminate this check's own processes."""
    ops = helper("stiller_ops")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    children = []
    ready = False

    def request(component, path, payload=None):
        headers = {"Accept": "application/json, text/event-stream"}
        if component == "MCP":
            headers["Authorization"] = "Bearer " + config["STBRAIN_MCP_TOKEN"]
        if payload is not None:
            headers["Content-Type"] = "application/json"
        url = "http://127.0.0.1:" + config["STBRAIN_" + component + "_PORT"] + path
        req = Request(url, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
        with opener.open(req, timeout=2) as response:
            body = response.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise InstallError("本机检查返回内容超过上限。")
            return json.loads(body) if body else None

    def check(_):
        nonlocal ready
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in children):
                raise InstallError("本次检查启动的服务提前退出。请查看私有安装日志。")
            try:
                if not request("CONTROL", "/health")["ok"] or not request("GATEWAY", "/health")["ok"]:
                    raise InstallError("服务健康检查未通过。")
                initialized = request("MCP", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "stiller-installer-check", "version": "1"}}})
                if "result" not in initialized:
                    raise InstallError("MCP 初始化未通过。")
                request("MCP", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
                catalogue = request("MCP", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                names = {tool["name"] for tool in catalogue["result"]["tools"]}
                if not {"stbrain_help", "remember_memory", "revise_memory"} <= names:
                    raise InstallError("MCP 工具目录不完整。")
                ready = True
                raise KeyboardInterrupt()
            except (URLError, TimeoutError, ConnectionError):
                time.sleep(0.2)
        raise InstallError("服务在 45 秒内未就绪。请查看私有日志；当前配置已保留。")

    with private_log(private) as log:
        def launch(command, **kwargs):
            child = subprocess.Popen(command, stdout=log, stderr=log, stdin=subprocess.DEVNULL, **kwargs)
            children.append(child)
            return child
        with contextlib.redirect_stdout(io.StringIO()):
            ops.start_stack(config, popen=launch, sleeper=check)
    if not ready:
        raise InstallError("检查被取消；本次启动的服务已经关闭。")


def quote_command(args):
    if os.name == "nt":
        return "& " + " ".join("'" + str(arg).replace("'", "''") + "'" for arg in args)
    return shlex.join(map(str, args))


def install(private):
    print("正在准备独立运行环境（首次需要联网下载依赖）……", flush=True)
    with private_log(private) as log:
        # venv can finish an interrupted creation without regenerating private configuration.
        run_private([sys.executable, "-I", "-B", "-m", "venv", str(private / "venv")], log)
        python = str(venv_python(private))
        run_private([python, "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check", "install", "--index-url", "https://pypi.org/simple",
                     "--timeout", "25", "--retries", "2",
                     "--require-hashes", "--only-binary=:all:", "--no-deps", "-r", str(ROOT / select_lock())], log)
        run_private([python, "-I", "-B", "-m", "pip", "--isolated", "check"], log)
        print("依赖已安装。正在检查三个本机服务及 MCP 工具目录……", flush=True)
        run_private([python, "-B", str(Path(__file__).resolve()), "--verify", str(private)], log, timeout=75)
    print("安装与本机服务检查通过。检查进程已停止；未调用真实模型。")
    print("连接资料：" + str(private / "连接资料（请勿公开）.txt"))
    print("今后启动 ST（运行期间保留终端，按 Ctrl+C 停止）：")
    print(quote_command([venv_python(private), "-B", Path(__file__).resolve(), "--start", private]))


def main(argv=None):
    parser = argparse.ArgumentParser(description="ST 新手安装向导：独立环境、私有配置与本机检查。")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--directory", type=Path, help="首次安装或继续同一安装的私有目录")
    group.add_argument("--start", type=Path, help="启动本向导已经安装的实例")
    group.add_argument("--verify", type=Path, help="短暂启动并检查本向导的实例，检查后停止")
    args = parser.parse_args(argv)
    try:
        select_lock()
        if helper("check_source_package").failures(ROOT):
            raise InstallError("源码完整性检查未通过。请重新下载完整发行源码，再运行向导。")
        if args.start or args.verify:
            private = safe_directory(args.start or args.verify)
            config = read_installation(private)
            if Path(sys.prefix).resolve() != (private / "venv").resolve():
                raise InstallError("请使用向导给出的完整启动命令，它会使用此实例自己的 Python。")
            if args.verify:
                verify_services(config, private)
                return 0
            print("ST 已进入启动流程。连接资料位于私有目录；按 Ctrl+C 停止本次服务。", flush=True)
            return helper("stiller_ops").start_stack(config)
        print("给 AI 准备一份新的 ST。向导安装本机服务，记忆内容由 AI 接入后自行设置。")
        default_directory = Path.home() / "StillerBrain-private"
        selected = args.directory or Path(input("私有安装目录（回车采用 " + str(default_directory) + "）：").strip() or str(default_directory))
        private = safe_directory(selected)
        if private.exists():
            config = read_installation(private)
            print("已识别此安装，将保留已有配置、身份和记忆，继续安装依赖及检查。")
            helper("stiller_ops").check_ports_stopped(config)
        else:
            base = check_text(input("模型服务的 HTTPS Base URL（通常以 /v1 结尾）："), "模型地址")
            model = check_text(input("真实模型 ID（按服务商原样填写）："), "模型 ID")
            key = check_text(hidden_input("模型 API Key（输入隐藏）："), "API Key")
            password = check_text(hidden_input("模块一授权密码（MCP 直连修改自我定义需要；仅用网关可回车跳过）："), "模块一密码", True)
            if password and password != hidden_input("再输入一次模块一密码：").strip():
                raise InstallError("两次密码不同，请重新运行向导。")
            if input("将创建私有目录、安装依赖并短暂检查本机服务。继续请输入 yes：").strip().lower() != "yes":
                print("已取消，未创建安装。")
                return 0
            config = create_installation(private, base, model, key, free_ports(), password)
        install(private)
        return 0
    except KeyboardInterrupt:
        print("\n操作已取消。已经建立的安装目录会保留，可用同一目录继续。")
        return 130
    except (InstallError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print("安装提示：" + (str(exc) if isinstance(exc, InstallError) else
                            "此步骤未完成，文件保持原样。请检查目录权限、端口或私有安装日志。"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
