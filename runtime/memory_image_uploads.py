"""Bounded ephemeral original-image transport, never memory or write authority.

References are supplied by an authenticated client after local file selection.
Their unpredictability cannot be proved by the server; owner/model isolation and
the final memory operation's existing authorization are always authoritative.
No disk, URLs, external decoding service, or automatic memory attachment.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from dataclasses import dataclass

from .memory_images import MemoryImageError, decode_image_base64

UPLOAD_CONTRACT = "memory-image-upload/1"
UPLOAD_TOOL = "stage_memory_image_upload"
UPLOAD_REF_PATTERN = r"^stimgup_[0-9a-f]{64}$"
_REF = re.compile(r"stimgup_[0-9a-f]{64}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
UPLOAD_TTL_SECONDS = 900
MAX_OWNER_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_TOTAL_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_OWNER_UPLOADS = 32
MAX_TOTAL_UPLOADS = 128


@dataclass(frozen=True)
class _Upload:
    data: bytes
    mime_type: str
    sha256: str
    width: int
    height: int
    deadline: float


def parse_upload_ref(value):
    if not isinstance(value, str) or _REF.fullmatch(value) is None:
        raise MemoryImageError("invalid_image_upload_ref")
    return value


class MemoryImageUploads:
    """One process-wide store; TTL retries do not extend residency.

    Restart discards pending bytes. A host may explicitly re-stage its original
    under the same identifier; final calls remain independently execution-bound.
    Same-scope same-ref content is immutable while present. Resolving does not
    consume authorization or claim a memory write has succeeded.
    """
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._items = {}

    @staticmethod
    def _identity(owner_id, model_id):
        if any(not isinstance(v, str) or not v or v != v.strip() for v in (owner_id, model_id)):
            raise MemoryImageError("invalid_identity")
        return owner_id, model_id

    def _prune(self, now):
        for key in [key for key, item in self._items.items() if item.deadline <= now]:
            del self._items[key]

    @staticmethod
    def _receipt(ref, item, now, replay):
        return {"contract": UPLOAD_CONTRACT, "decision": "staged", "upload_ref": ref,
            "sha256": item.sha256, "mime_type": item.mime_type, "byte_count": len(item.data),
            "width": item.width, "height": item.height,
            "expires_in_seconds": max(1, min(UPLOAD_TTL_SECONDS, math.ceil(item.deadline - now))),
            "memory_written": False, "write_authority_granted": False, "idempotent_replay": replay}

    def stage(self, *, owner_id, model_id, protocol, upload_ref, mime_type, sha256, data_base64):
        scope = self._identity(owner_id, model_id)
        ref = parse_upload_ref(upload_ref)
        if protocol != UPLOAD_CONTRACT:
            raise MemoryImageError("image_upload_protocol_unsupported")
        if not isinstance(sha256, str) or _HASH.fullmatch(sha256) is None:
            raise MemoryImageError("invalid_image_upload_hash")
        data, width, height = decode_image_base64(data_base64, mime_type)
        digest = hashlib.sha256(data).hexdigest()
        if digest != sha256:
            raise MemoryImageError("image_upload_hash_mismatch")
        with self._lock:
            now = self._clock()
            self._prune(now)
            key = (*scope, ref)
            existing = self._items.get(key)
            if existing is not None:
                if (existing.sha256, existing.mime_type, existing.data) != (digest, mime_type, data):
                    raise MemoryImageError("image_upload_reference_conflict")
                return self._receipt(ref, existing, now, True)
            owned = [item for k, item in self._items.items() if k[:2] == scope]
            if (len(owned) >= MAX_OWNER_UPLOADS or len(self._items) >= MAX_TOTAL_UPLOADS
                    or sum(len(item.data) for item in owned) + len(data) > MAX_OWNER_UPLOAD_BYTES
                    or sum(len(item.data) for item in self._items.values()) + len(data) > MAX_TOTAL_UPLOAD_BYTES):
                raise MemoryImageError("image_upload_capacity_limit")
            item = _Upload(data, mime_type, digest, width, height, now + UPLOAD_TTL_SECONDS)
            self._items[key] = item
            return self._receipt(ref, item, now, False)

    def resolve(self, *, owner_id, model_id, upload_ref, mime_type):
        scope = self._identity(owner_id, model_id)
        ref = parse_upload_ref(upload_ref)
        with self._lock:
            now = self._clock()
            self._prune(now)
            item = self._items.get((*scope, ref))
            if item is None:
                # Do not distinguish another owner's identifier from expiration.
                raise MemoryImageError("image_upload_not_available")
            if item.mime_type != mime_type:
                raise MemoryImageError("image_upload_mime_mismatch")
            if hashlib.sha256(item.data).hexdigest() != item.sha256:
                raise MemoryImageError("image_upload_integrity_failed")
            return item.data


# Share limits across services/owners in this process. No initialization IO.
default_image_uploads = MemoryImageUploads()
