"""Explicit local-file image upload/download through authenticated MCP; no server paths."""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import stat
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from runtime.memory_images import MAX_BYTES, MAX_MCP_REQUEST_BYTES, MIMES, MemoryImageError, validate_image

MAX_REQUEST_WIRE = MAX_MCP_REQUEST_BYTES
# FastMCP dict results carry BOTH structuredContent and a text block. Budget both copies.
MAX_RESPONSE_WIRE = 12 * 1024 * 1024
IMAGE_ACTIONS = frozenset({"store_memory_image", "list_memory_images", "read_memory_image", "remove_memory_image"})


class ImageClientError(ValueError):
    """Fixed diagnostics, no provider body/URL/header/credential reflection."""


def endpoint(value):
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or any(ord(c) < 33 for c in value):
            raise ValueError
        local = parsed.hostname == "localhost"
        try:
            local = local or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError
        return value
    except (TypeError, ValueError):
        raise ImageClientError("https_or_loopback_endpoint_required") from None


class ImageMCPClient:
    def __init__(self, url, token, *, transport=None):
        import httpx
        self.url = endpoint(url)
        if not isinstance(token, str) or not token or len(token) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ImageClientError("credential_required")
        self.client = httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(20, read=30), headers={"Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-03-26",
            "X-STBrain-Tool-Profile": "daily"})
        self.sequence = 0

    def close(self):
        self.client.close()

    def rpc(self, method, params=None, notification=False):
        self.sequence += 1
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = self.sequence
        if params is not None:
            body["params"] = params
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(payload) > MAX_REQUEST_WIRE:
            raise ImageClientError("request_size_limit")
        try:
            deadline = time.monotonic() + 90
            with self.client.stream("POST", self.url, content=payload, headers={"Content-Type": "application/json"}) as response:
                if response.status_code not in (200, 202):
                    raise ImageClientError("mcp_http_result_unknown")
                # This bounded CLI supports ST's stateless JSON transport, not arbitrary SSE servers.
                if not notification and response.headers.get("content-type", "").split(";")[0] != "application/json":
                    raise ImageClientError("stateless_json_mcp_required")
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if time.monotonic() > deadline:
                        raise ImageClientError("response_deadline")
                    if len(raw) + len(chunk) > MAX_RESPONSE_WIRE:
                        raise ImageClientError("response_size_limit")
                    raw.extend(chunk)
            if notification and not raw:
                return None
            result = json.loads(raw)
            if (not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or result.get("id") != self.sequence or
                    "error" in result or "result" not in result):
                raise ImageClientError("mcp_invalid_response")
            return result["result"]
        except ImageClientError:
            raise
        except Exception:
            raise ImageClientError("mcp_result_unknown_no_automatic_retry") from None

    def initialize(self):
        result = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "st-memory-images-local", "version": "1"}})
        if not isinstance(result, dict) or result.get("protocolVersion") != "2025-03-26":
            raise ImageClientError("unsupported_mcp_protocol")
        self.rpc("notifications/initialized", notification=True)
        # Discover a fixed operation through compact dispatch, even for daily7 clients.
        schema = self.call_tool("stbrain_tools", {"action": "store_memory_image"})
        if schema.get("action") != "store_memory_image":
            raise ImageClientError("memory_images_not_supported")

    def call_tool(self, name, arguments):
        if name not in {"stbrain_tools", "stbrain_manage"}:
            raise ImageClientError("image_tool_not_allowed")
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict) or result.get("isError") is True:
            raise ImageClientError("tool_result_unknown")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        blocks = result.get("content")
        if isinstance(blocks, list) and len(blocks) == 1 and blocks[0].get("type") == "text":
            try:
                value = json.loads(blocks[0]["text"])
                if isinstance(value, dict):
                    return value
            except (ValueError, KeyError, TypeError):
                pass
        raise ImageClientError("tool_result_unknown")

    def action(self, action, arguments):
        if action not in IMAGE_ACTIONS:
            raise ImageClientError("image_tool_not_allowed")
        return self.call_tool("stbrain_manage", {"action": action, "arguments": arguments})


def read_local_image(path):
    """Only the explicitly selected local ordinary file; reject symlink/reparse endpoints."""
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or
            getattr(info, "st_file_attributes", 0) & 0x400 or not 1 <= info.st_size <= MAX_BYTES):
        raise ImageClientError("local_regular_image_required")
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise ImageClientError("local_file_changed")
        data = source.read(MAX_BYTES + 1)
    if len(data) != info.st_size:
        raise ImageClientError("local_file_changed")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = MIMES["PNG"]
    elif data.startswith(b"\xff\xd8"):
        mime = MIMES["JPEG"]
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = MIMES["WEBP"]
    else:
        raise ImageClientError("image_format_unsupported")
    validate_image(data, mime)
    return data, mime


def export_new_file(result, destination):
    """Validate bounded bytes/hash, create exclusively, flush, then read back our own handle."""
    if result.get("decision") != "read" or result.get("mime_type") not in MIMES.values():
        raise ImageClientError("image_read_failed")
    uri = result.get("data_uri")
    prefix = "data:" + result["mime_type"] + ";base64,"
    if not isinstance(uri, str) or not uri.startswith(prefix) or len(uri) > MAX_REQUEST_WIRE:
        raise ImageClientError("invalid_image_receipt")
    try:
        data = base64.b64decode(uri[len(prefix):], validate=True)
    except (ValueError, UnicodeError):
        raise ImageClientError("invalid_image_receipt") from None
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != result.get("byte_count") or digest != result.get("sha256"):
        raise ImageClientError("image_hash_mismatch")
    validate_image(data, result["mime_type"])
    path = Path(destination)
    # x+b cannot overwrite a file or symlink. No automatic deletion on an I/O failure.
    with path.open("x+b") as output:
        output.write(data); output.flush(); os.fsync(output.fileno())
        output.seek(0)
        if hashlib.sha256(output.read(MAX_BYTES + 1)).hexdigest() != digest:
            raise ImageClientError("local_readback_failed")
    return digest


def main(argv=None):
    parser = argparse.ArgumentParser(description="ST 图片本机存取（候选）：显式上传/下载，绝不覆盖既有文件。")
    parser.add_argument("operation", choices=("put", "get", "list", "remove"))
    parser.add_argument("--endpoint", required=True, help="认证 MCP 完整 HTTPS URL；本机可用 HTTP")
    parser.add_argument("--target-ref", required=True, help="已有 emotion://、learning:// 或 plan:// 记忆 ref")
    parser.add_argument("--file", help="put 的本机图片文件，或 get 的全新输出文件")
    parser.add_argument("--image-ref", help="get/remove 使用 list 回执中的 image_ref")
    parser.add_argument("--description", help="put 的作者描述；省略时交互填写，不进行自动识图")
    parser.add_argument("--offset", type=int, default=0, help="list 的分页起点；使用上一页 next_offset")
    parser.add_argument("--token-env", default="STBRAIN_MCP_TOKEN", help="只传凭证环境变量名，不在命令行传值")
    args = parser.parse_args(argv)
    client = None
    try:
        target = endpoint(args.endpoint)
        from runtime.memory_relations import parse_memory_ref
        parse_memory_ref(args.target_ref)
        arguments = {"target_ref": args.target_ref}
        if args.operation == "list":
            if not 0 <= args.offset <= 2000:
                raise ImageClientError("invalid_image_page")
            arguments["offset"] = args.offset
        if args.operation == "put":
            if not args.file:
                raise ImageClientError("local_file_required")
            data, mime = read_local_image(args.file)
            description = args.description if args.description is not None else input("图片描述（不会自动识图）：")
            if not description.strip() or len(description) > 2000:
                raise ImageClientError("invalid_image_description")
            arguments.update(description=description, mime_type=mime, data_base64=base64.b64encode(data).decode("ascii"))
            print(f"将上传原图 {len(data)} bytes；SHA-256 {hashlib.sha256(data).hexdigest()}")
            print("原图 EXIF/定位信息会保留；不要存密钥截图。图片不是自动识图结果。")
        elif args.operation in {"get", "remove"}:
            from runtime.memory_images import parse_image_ref
            parse_image_ref(args.image_ref)
            arguments["image_ref"] = args.image_ref
            if args.operation == "get":
                if not args.file:
                    raise ImageClientError("new_output_file_required")
                if Path(args.file).exists() or Path(args.file).is_symlink():
                    raise ImageClientError("output_already_exists")
                arguments["include_data"] = True
            else:
                print("仅从列表移除；原图仍保留在数据库/备份，不是安全擦除。")
        print(f"目标 MCP：{target}\n记忆：{args.target_ref}；操作：{args.operation}")
        if input("确认本次操作？输入 yes：").strip() != "yes":
            print("已取消，未联系 ST。")
            return 0
        token = os.environ.get(args.token_env) or getpass.getpass("ST MCP 凭证（不回显）：")
        client = ImageMCPClient(target, token)
        client.initialize()
        operation = {"put": "store_memory_image", "get": "read_memory_image", "list": "list_memory_images", "remove": "remove_memory_image"}[args.operation]
        result = client.action(operation, arguments)
        if args.operation == "put" and result.get("decision") == "removed":
            raise ImageClientError("image_removed_not_reattached")
        expected = {"put": {"stored"}, "get": {"read"}, "list": {"listed"}, "remove": {"removed"}}
        if result.get("decision") not in expected[args.operation]:
            # Do not reflect untrusted server text/credentials in error receipts.
            from mcp_server.memory_image_service import SAFE
            code = result.get("reason_code")
            raise ImageClientError(code if isinstance(code, str) and code in SAFE else "image_action_not_confirmed")
        if args.operation == "get":
            print("已导出到新文件，读回 SHA-256：" + export_new_file(result, args.file))
        else:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ImageClientError, MemoryImageError) as error:
        print("未确认完成：" + str(error) + "。不会自动重试或覆盖文件。", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("未确认完成：本机文件或参数不可用；不会自动重试或覆盖文件。", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
