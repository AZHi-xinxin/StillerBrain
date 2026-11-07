"""Reuse original DeepSeek images without persisting image bytes or altering turn lineage.

The client carries stable, account-scoped handles. Only the outbound model projection
resolves them to expiring provider file IDs. Refreshing a file never changes the
conversation/catalog checked by the gateway. No caller-selected URL is fetched.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import threading
import time
from dataclasses import dataclass

import httpx

from .balance import _official_base

PROTOCOL = 'st-image-files/1'
PREFIX = 'st-image://'
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_REQUEST_BYTES = (MAX_IMAGE_BYTES + 2) // 3 * 4 + 4096
TTL = 6 * 3600
HASH = re.compile(r'[0-9a-f]{64}\Z')
FILE_ID = re.compile(r'file-[A-Za-z0-9_-]{1,180}\Z')


class ImageFilesError(RuntimeError):
    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


@dataclass(frozen=True, repr=False)
class Entry:
    file_id: str
    expires: float
    account: str


class ImageFiles:
    def __init__(self, secret: str, *, transport=None, clock=time.time, capacity=1024):
        self._secret = secret.encode()
        self._clock, self._transport, self._capacity = clock, transport, capacity
        self._entries = {}
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(2)

    def _handle(self, route, digest):
        scope = '\0'.join((route.upstream_base_url.rstrip('/'), route.api_key, digest))
        return hmac.new(self._secret, scope.encode(), hashlib.sha256).hexdigest()

    def prepare(self, route, payload):
        if not _official_base(route.upstream_base_url):
            return {'protocol': PROTOCOL, 'supported': False}
        if not isinstance(payload, dict) or set(payload) - {'model', 'digests', 'sha256', 'data', 'mime'}:
            raise ImageFilesError(400, 'invalid_image_files_request')
        if not self._slots.acquire(blocking=False):
            raise ImageFilesError(503, 'image_files_busy')
        try:
            if 'digests' in payload:
                if set(payload) != {'model', 'digests'}:
                    raise ImageFilesError(400, 'invalid_image_files_request')
                digests = payload['digests']
                if (not isinstance(digests, list) or not 1 <= len(digests) <= 64
                        or any(not isinstance(d, str) or not HASH.fullmatch(d) for d in digests)):
                    raise ImageFilesError(400, 'invalid_image_files_request')
                refs = {}
                with self._lock:
                    for digest in set(digests):
                        handle = self._handle(route, digest)
                        entry = self._entries.get(handle)
                        if entry and entry.expires > self._clock() + 300:
                            refs[digest] = PREFIX + handle
                return {'protocol': PROTOCOL, 'supported': True, 'references': refs}
            if set(payload) != {'model', 'sha256', 'data', 'mime'}:
                raise ImageFilesError(400, 'invalid_image_files_request')
            digest, data, mime = payload['sha256'], payload['data'], payload['mime']
            if (not isinstance(digest, str) or not HASH.fullmatch(digest)
                    or not isinstance(data, str) or len(data) > MAX_REQUEST_BYTES - 4096
                    or mime not in ('image/png', 'image/jpeg', 'image/webp')):
                raise ImageFilesError(400, 'invalid_image_files_request')
            try:
                raw = base64.b64decode(data, validate=True)
            except (ValueError, UnicodeError):
                raise ImageFilesError(400, 'invalid_image_encoding') from None
            signatures = {'image/png': raw.startswith(b'\x89PNG\r\n\x1a\n'),
                          'image/jpeg': raw.startswith(b'\xff\xd8\xff'),
                          'image/webp': raw.startswith(b'RIFF') and raw[8:12] == b'WEBP'}
            if not 0 < len(raw) <= MAX_IMAGE_BYTES or not signatures[mime]:
                raise ImageFilesError(400, 'invalid_image_encoding')
            if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), digest):
                raise ImageFilesError(400, 'image_digest_mismatch')
            handle = self._handle(route, digest)
            # Serialize cache misses to avoid duplicate paid uploads. Existing turns
            # only hold this lock while resolving IDs, never while calling the model.
            with self._lock:
                entry = self._entries.get(handle)
                if not entry or entry.expires <= self._clock() + 300:
                    self._entries = {k: v for k, v in self._entries.items() if v.expires > self._clock()}
                    if handle not in self._entries and len(self._entries) >= self._capacity:
                        raise ImageFilesError(503, 'image_files_capacity')
                    file_id, expires = self._upload(route.api_key, raw, mime)
                    self._entries[handle] = Entry(file_id, expires, self._handle(route, 'account'))
            return {'protocol': PROTOCOL, 'supported': True, 'references': {digest: PREFIX + handle}}
        finally:
            self._slots.release()

    def _upload(self, key, raw, mime):
        if not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ImageFilesError(502, 'image_files_upstream_auth_failed')
        try:
            with httpx.Client(timeout=60, follow_redirects=False, trust_env=False,
                              transport=self._transport) as client:
                with client.stream('POST', 'https://api.deepseek.com/files',
                        headers={'Authorization': 'Bearer ' + key, 'Accept-Encoding': 'identity'},
                        data={'purpose': 'user_data', 'expires_after[anchor]': 'created_at',
                              'expires_after[seconds]': str(TTL)},
                        files={'file': ('image.' + mime.split('/')[-1], raw, mime)}) as response:
                    if response.status_code != 200:
                        raise ImageFilesError(502, 'image_files_upstream_failed')
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 16384:
                            raise ImageFilesError(502, 'image_files_invalid_response')
                    import json
                    value = json.loads(body)
            file_id, expiry = value.get('id'), value.get('expires_at')
            if (not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id)
                    or type(expiry) not in (int, float)
                    or not self._clock() + 300 < expiry <= self._clock() + TTL + 120):
                raise ImageFilesError(502, 'image_files_invalid_response')
            return file_id, expiry
        except ImageFilesError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise ImageFilesError(502, 'image_files_upstream_failed') from None

    def project(self, payload, route):
        """Copy only changed content; never mutate the prepared turn/snapshot."""
        messages = payload.get('messages')
        if not isinstance(messages, list):
            return payload
        result, changed = [], False
        for message in messages:
            content = message.get('content') if isinstance(message, dict) else None
            if not isinstance(content, list):
                result.append(message)
                continue
            parts = []
            for part in content:
                image = part.get('image_url') if isinstance(part, dict) and part.get('type') == 'image_url' else None
                url = image.get('url') if isinstance(image, dict) else None
                if not isinstance(url, str) or not url.startswith(PREFIX):
                    parts.append(part)
                    continue
                handle = url[len(PREFIX):]
                # The handle is an account-scoped capability. Route ownership is
                # checked via the digest-to-handle mapping retained with each entry.
                with self._lock:
                    entry = self._entries.get(handle)
                if (not _official_base(route.upstream_base_url) or not entry
                        or not hmac.compare_digest(entry.account, self._handle(route, 'account'))
                        or entry.expires <= self._clock() + 30):
                    raise ImageFilesError(409, 'image_reference_expired_prepare_again')
                parts.append({'type': 'file', 'file_id': entry.file_id})
                changed = True
            result.append({**message, 'content': parts})
        return {**payload, 'messages': result} if changed else payload
