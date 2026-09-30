"""Human-only, durable route registry. Never logs input or sends model probes."""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import threading
from urllib.parse import urlsplit

import httpx

from .routes import GatewayRoute, MAX_EXTRA_ROUTES, validate_route, _unique_pairs

CAPABILITIES = "/v1/st/routes/capabilities"
LIST = "/v1/st/routes"
UPSERT = "/v1/st/routes/upsert"
REQUESTS = "/v1/st/routes/requests/"
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
MAX_BODY = 16384
MAX_STATE = 4 * 1024 * 1024
MAX_REQUESTS = 4096


class ManagementError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def _fail(code="management_unavailable", status=503):
    raise ManagementError(status, code)


def _private_path(path, *, directory=False):
    """Reject links, junctions, wrong owner/mode and non-private Windows DACLs."""
    path = Path(path)
    if not path.is_absolute():
        _fail()
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _fail()
    info = path.stat()
    if directory != stat.S_ISDIR(info.st_mode) or (not directory and not stat.S_ISREG(info.st_mode)):
        _fail()
    if os.name != "nt":
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            _fail()
    else:
        # Only the path is passed to the subprocess. No credential/body is ever passed.
        command = "$ErrorActionPreference='Stop'; $p=$env:ST_ROUTE_ACL_CHECK; $a=Get-Acl -LiteralPath $p; $me=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; $ok=@($me,'S-1-5-18','S-1-5-32-544'); if($a.Access.Count -eq 0 -or $a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -notin $ok){exit 4}; $accessOk=$ok+@('S-1-3-4'); foreach($r in $a.Access){ if($r.AccessControlType -eq 'Allow' -and $r.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -notin $accessOk){exit 4} }; exit 0"
        env = {**os.environ, "ST_ROUTE_ACL_CHECK": str(path)}
        # PowerShell 7's inherited module path can break Windows PowerShell 5
        # autoloading. Use the OS-owned module directory, never the caller's cwd.
        env["PSModulePath"] = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) /
                                   "System32" / "WindowsPowerShell" / "v1.0" / "Modules")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            _fail()


def public_endpoint(value, *, resolve=True):
    """HTTPS public endpoints only; DNS is rechecked and pinned at every request."""
    try:
        validate_route(GatewayRoute("check", value, "check", "validation-only-key"))
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.endswith("."):
            _fail("unsafe_upstream", 400)
        host = parsed.hostname.encode("idna").decode("ascii")
        if host != parsed.hostname.lower() or "." not in host and ":" not in host:
            _fail("unsafe_upstream", 400)
        if not resolve:
            return parsed, ()
        answers = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        addresses = tuple(dict.fromkeys(answer[4][0] for answer in answers))
        if not addresses or len(addresses) > 32:
            _fail("unsafe_upstream", 400)
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
                _fail("unsafe_upstream", 400)
            if isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo):
                _fail("unsafe_upstream", 400)
            if ip.version == 6 and (ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48")):
                _fail("unsafe_upstream", 400)
        return parsed, addresses
    except ManagementError:
        raise
    except (ValueError, TypeError, OSError, UnicodeError):
        _fail("unsafe_upstream", 400)


@contextlib.contextmanager
def managed_stream(route, *, content, timeout, transport=None):
    """No DNS TOCTOU: use checked IP for TCP, original hostname for TLS and Host."""
    try:
        parsed, addresses = public_endpoint(route.upstream_base_url)
        host = parsed.hostname
        original = httpx.URL(route.completion_url)
        pinned = original.copy_with(host=addresses[0])
        headers = {"Host": original.netloc.decode("ascii"),
                   "Authorization": "Bearer " + route.api_key,
                   "Content-Type": "application/json; charset=utf-8",
                   "Accept": "application/json, text/event-stream"}
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False,
                          transport=transport, limits=httpx.Limits(max_connections=1,
                                                                  max_keepalive_connections=0)) as client:
            with client.stream("POST", pinned, headers=headers, content=content,
                               extensions={"sni_hostname": host}) as response:
                if 300 <= response.status_code < 400:
                    _fail("unsafe_upstream", 502)
                yield response
    except ManagementError:
        # Do not expose DNS, endpoint or key-containing HTTP exception text.
        raise


class RouteStore:
    """Single gateway process owns the store; threads serialize mutations/reads."""
    def __init__(self, directory, token, immutable, forbidden=()):
        self.directory = Path(directory)
        self.path = self.directory / "managed-routes.json"
        self.lock_path = self.directory / "managed-routes.lock"
        self._lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)
        self._token = token
        self._immutable = tuple(immutable)
        self._forbidden = tuple(v for v in (token, *forbidden) if v)
        self._retained_keys = set()
        self._lock_fd = None
        if (not isinstance(token, str) or not 32 <= len(token) <= 4096
                or any(not 33 <= ord(char) <= 126 for char in token)
                or any(hmac.compare_digest(token, value) for value in forbidden if value)):
            _fail()
        try:
            _private_path(self.directory, directory=True)
            if self.lock_path.exists():
                _private_path(self.lock_path)
            self._lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            _private_path(self.lock_path)
            if os.name == "nt":
                import msvcrt
                if os.fstat(self._lock_fd).st_size == 0:
                    os.write(self._lock_fd, b"0")
                    os.fsync(self._lock_fd)
                os.lseek(self._lock_fd, 0, 0)
                msvcrt.locking(self._lock_fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not self.path.exists():
                self._commit({"schema": "st-managed-routes-private/1", "revision": 0,
                              "digestKey": secrets.token_hex(32), "routes": [], "requests": {}})
            self._read()
        except Exception:
            self.close()
            _fail()

    def close(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def authorize(self, token):
        return isinstance(token, str) and token.isascii() and hmac.compare_digest(token, self._token)

    def _read(self):
        try:
            _private_path(self.directory, directory=True)
            _private_path(self.path)
            with open(self.path, "rb") as stream:
                raw = stream.read(MAX_STATE + 1)
            if len(raw) > MAX_STATE:
                _fail()
            state = json.loads(raw, object_pairs_hook=_unique_pairs)
            if (set(state) != {"schema", "revision", "digestKey", "routes", "requests"}
                    or state["schema"] != "st-managed-routes-private/1"
                    or type(state["revision"]) is not int or state["revision"] < 0
                    or not re.fullmatch(r"[0-9a-f]{64}", state["digestKey"])
                    or not isinstance(state["routes"], list) or len(state["routes"]) > MAX_EXTRA_ROUTES
                    or not isinstance(state["requests"], dict) or len(state["requests"]) > MAX_REQUESTS):
                _fail()
            aliases = {item.public_model for item in self._immutable}
            for row in state["routes"]:
                if set(row) != {"publicModel", "upstreamBaseUrl", "upstreamModel", "apiKey"}:
                    _fail()
                route = self._route(row)
                validate_route(route)
                public_endpoint(route.upstream_base_url, resolve=False)
                if any(hmac.compare_digest(route.api_key, secret) for secret in self._forbidden):
                    _fail()
                if route.public_model in aliases or any(route.public_model == item.auxiliary_model for item in self._immutable):
                    _fail()
                aliases.add(route.public_model)
                self._retained_keys.add(route.api_key)
            protected = {*self._forbidden, *self._retained_keys, *(row.api_key for row in self._immutable)}
            visible = [value for row in (*self._immutable, *(self._route(row) for row in state["routes"]))
                       for value in (row.public_model, row.upstream_base_url, row.upstream_model)]
            if any(secret in value for secret in protected for value in visible):
                _fail()
            for request_id, entry in state["requests"].items():
                if (not HEX32.fullmatch(request_id) or set(entry) != {"digest", "result"}
                        or not re.fullmatch(r"[0-9a-f]{64}", entry["digest"])
                        or not isinstance(entry["result"], dict)
                        or set(entry["result"]) != {"schema", "requestId", "revision", "publicModel", "status"}
                        or entry["result"]["schema"] != "orbis.st.routes-upsert-result/1"
                        or entry["result"]["requestId"] != request_id
                        or type(entry["result"]["revision"]) is not int
                        or not 1 <= entry["result"]["revision"] <= state["revision"]
                        or entry["result"]["publicModel"] not in aliases
                        or entry["result"]["status"] != "saved"):
                    _fail()
            if state["revision"] != len(state["requests"]):
                _fail()
            self._snapshot = tuple(self._route(row) for row in state["routes"])
            return state
        except ManagementError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RecursionError, AttributeError):
            _fail()

    @staticmethod
    def _route(row):
        return GatewayRoute(row["publicModel"], row["upstreamBaseUrl"], row["upstreamModel"], row["apiKey"], managed=True)

    def _commit(self, state):
        _private_path(self.directory, directory=True)
        if self.path.exists():
            _private_path(self.path)
        raw = json.dumps(state, ensure_ascii=True, separators=(",", ":")).encode()
        if len(raw) > MAX_STATE:
            _fail("journal_limit_reached", 429)
        temporary = self.directory / (".routes-" + secrets.token_hex(16) + ".tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            _private_path(temporary)
            if os.name == "nt":
                move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
                move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
                move.restype = ctypes.c_int
                if not move(str(temporary), str(self.path), 0x1 | 0x8):
                    _fail()
            else:
                os.replace(temporary, self.path)
                descriptor = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary.exists():
                temporary.unlink()

    def snapshot(self):
        with self._lock:
            # Process-exclusive store; disk health is checked on each management
            # operation. Chat requests never spawn ACL checks or read secret files.
            return self._snapshot

    def protected(self):
        with self._lock:
            return frozenset((*self._forbidden, *self._retained_keys))

    def listing(self):
        with self._lock:
            state = self._read()
            routes = (*self._immutable, *(self._route(row) for row in state["routes"]))
            return {"schema": "orbis.st.routes/1", "revision": state["revision"], "routes": [
                {"publicModel": route.public_model, "upstreamBaseUrl": route.upstream_base_url,
                 "upstreamModel": route.upstream_model, "managed": route.managed, "keyConfigured": True}
                for route in routes]}

    def receipt(self, request_id):
        if not HEX32.fullmatch(request_id):
            _fail("invalid_request", 400)
        with self._lock:
            entry = self._read()["requests"].get(request_id)
            return {"schema": "orbis.st.routes-request/1", "requestId": request_id,
                    "found": entry is not None, "result": entry["result"] if entry else None}

    def upsert(self, payload):
        keys = {"schema", "requestId", "expectedRevision", "publicModel", "upstreamBaseUrl", "upstreamModel"}
        if (not isinstance(payload, dict) or set(payload) not in (keys, keys | {"apiKey"})
                or payload.get("schema") != "orbis.st.routes-upsert/1"
                or not isinstance(payload.get("requestId"), str) or not HEX32.fullmatch(payload["requestId"])
                or type(payload.get("expectedRevision")) is not int or payload["expectedRevision"] < 0):
            _fail("invalid_request", 400)
        with self._lock:
            state = self._read()
            digest = hmac.new(bytes.fromhex(state["digestKey"]),
                              json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
            old = state["requests"].get(payload["requestId"])
            if old:
                if not hmac.compare_digest(digest, old["digest"]):
                    _fail("request_conflict", 409)
                return old["result"]
            if payload["expectedRevision"] != state["revision"]:
                _fail("revision_conflict", 409)
            if len(state["requests"]) >= MAX_REQUESTS:
                _fail("journal_limit_reached", 429)
            public = payload["publicModel"]
            if any(public in (row.public_model, row.auxiliary_model) for row in self._immutable):
                _fail("immutable_route", 409)
            previous = next((row for row in state["routes"] if row["publicModel"] == public), None)
            key = payload.get("apiKey", previous["apiKey"] if previous else None)
            row = {"publicModel": public, "upstreamBaseUrl": payload["upstreamBaseUrl"],
                   "upstreamModel": payload["upstreamModel"], "apiKey": key}
            try:
                route = self._route(row)
                validate_route(route)
            except (ValueError, TypeError, KeyError):
                _fail("invalid_request", 400)
            if any(hmac.compare_digest(route.api_key, secret) for secret in self._forbidden):
                _fail("invalid_request", 400)
            protected = {*self._forbidden, *self._retained_keys, route.api_key,
                         *(item.api_key for item in self._immutable)}
            labels = [value for item in (*self._immutable, *(self._route(r) for r in state["routes"]), route)
                      for value in (item.public_model, item.upstream_base_url, item.upstream_model)]
            if any(secret in label for secret in protected for label in labels):
                _fail("invalid_request", 400)
            public_endpoint(route.upstream_base_url)
            if previous is None and len(state["routes"]) >= MAX_EXTRA_ROUTES:
                _fail("route_limit_reached", 429)
            if previous is not None:
                state["routes"][state["routes"].index(previous)] = row
            else:
                state["routes"].append(row)
            state["revision"] += 1
            result = {"schema": "orbis.st.routes-upsert-result/1", "requestId": payload["requestId"],
                      "revision": state["revision"], "publicModel": public, "status": "saved"}
            state["requests"][payload["requestId"]] = {"digest": digest, "result": result}
            self._commit(state)
            self._retained_keys.add(route.api_key)
            self._snapshot = tuple(self._route(row) for row in state["routes"])
            return result


def handle_management_request(handler, method):
    path = urlsplit(handler.path).path
    if path not in (CAPABILITIES, LIST, UPSERT) and not path.startswith(REQUESTS):
        return False
    handler.close_connection = True
    slot = None
    try:
        if (handler.path != path or any(handler.headers.get_all(name, []) for name in
                ("Origin", "Cookie", "Transfer-Encoding", "Content-Encoding"))):
            _fail("invalid_request", 400)
        if method == "GET" and handler.headers.get_all("Content-Length", []) not in ([], ["0"]):
            _fail("invalid_request", 400)
        app = handler.app
        store = app.route_store
        if path == CAPABILITIES:
            if method != "GET":
                _fail("method_not_allowed", 405)
            handler._json(200, {"schema": "orbis.st.routes-capabilities/1", "enabled": store is not None,
                               "authorization": "dedicated-human-bearer", "maxRoutes": MAX_EXTRA_ROUTES})
            return True
        if store is None:
            _fail("management_disabled", 403)
        auth = handler.headers.get_all("Authorization", [])
        if (len(auth) != 1 or not auth[0].startswith("Bearer ") or len(auth[0]) > 4103
                or not store.authorize(auth[0][7:])):
            _fail("unauthorized", 401)
        if not store.slots.acquire(blocking=False):
            _fail("management_rate_limited", 429)
        slot = store.slots
        if path == LIST and method == "GET":
            result = store.listing()
        elif path.startswith(REQUESTS) and method == "GET":
            result = store.receipt(path[len(REQUESTS):])
            app.refresh_managed_routes()
        elif path == UPSERT and method == "POST":
            lengths = handler.headers.get_all("Content-Length", [])
            content_types = handler.headers.get_all("Content-Type", [])
            if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,6}", lengths[0])
                    or not 1 <= int(lengths[0]) <= MAX_BODY or len(content_types) != 1
                    or content_types[0].split(";", 1)[0].strip().lower() != "application/json"):
                _fail("invalid_request", 400)
            handler.connection.settimeout(6.0)
            raw = handler.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                _fail("invalid_request", 400)
            try:
                payload = json.loads(raw, object_pairs_hook=_unique_pairs)
            except (ValueError, UnicodeError, RecursionError):
                _fail("invalid_request", 400)
            result = store.upsert(payload)
            app.refresh_managed_routes()
        else:
            _fail("method_not_allowed", 405)
        handler._json(200, result)
    except ManagementError as error:
        handler._json(error.status, {"error": {"code": error.code}})
    except Exception:
        # Management failures are intentionally never passed into chat performance logs.
        handler._json(503, {"error": {"code": "management_unavailable"}})
    finally:
        if slot is not None:
            slot.release()
    return True
