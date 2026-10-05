"""Owner-scoped original image attachments. Never part of automatic memory recall."""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import re
import threading
import uuid
import warnings

from .execution_binding import assert_bound_execution
from .memory_relations import MemoryRelationError, MemoryRelationStore, _ddl, _now, parse_memory_ref

CONTRACT = "memory-images/1"
MAX_BYTES = 4 * 1024 * 1024
MAX_BASE64 = ((MAX_BYTES + 2) // 3) * 4
MAX_MCP_REQUEST_BYTES = 6 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_OWNER_BYTES = 512 * 1024 * 1024
MAX_OWNER_ITEMS = 2000
MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
IMAGE_REF = re.compile(r"image://([0-9a-f]{32})@1\Z")
_DECODERS = threading.BoundedSemaphore(2)

ITEM_SQL = """CREATE TABLE IF NOT EXISTS memory_image_items (
 owner_id TEXT NOT NULL, model_id TEXT NOT NULL, image_id TEXT NOT NULL,
 target_ref TEXT NOT NULL, target_module TEXT NOT NULL, target_id TEXT NOT NULL,
 description TEXT NOT NULL, mime_type TEXT NOT NULL, sha256 TEXT NOT NULL,
 byte_count INTEGER NOT NULL CHECK(byte_count BETWEEN 1 AND 4194304),
 width INTEGER NOT NULL, height INTEGER NOT NULL, original_bytes BLOB NOT NULL,
 active INTEGER NOT NULL CHECK(active IN (0,1)), created_at TEXT NOT NULL,
 PRIMARY KEY(owner_id,model_id,image_id), CHECK(length(original_bytes)=byte_count)
)"""
REQUEST_SQL = """CREATE TABLE IF NOT EXISTS memory_image_requests (
 owner_id TEXT NOT NULL, model_id TEXT NOT NULL, request_key TEXT NOT NULL,
 operation_hash TEXT NOT NULL, image_id TEXT NOT NULL,
 PRIMARY KEY(owner_id,model_id,request_key),
 FOREIGN KEY(owner_id,model_id,image_id) REFERENCES memory_image_items(owner_id,model_id,image_id)
)"""
INDEX_SQL = """CREATE INDEX IF NOT EXISTS memory_image_target_order
 ON memory_image_items(owner_id,model_id,target_module,target_id,active,created_at,image_id)"""
DDL = {"memory_image_items": ("table", ITEM_SQL), "memory_image_requests": ("table", REQUEST_SQL),
       "memory_image_target_order": ("index", INDEX_SQL)}
META = "image_id,target_ref,description,mime_type,sha256,byte_count,width,height,active,created_at"


class MemoryImageError(MemoryRelationError):
    """Only fixed value-free diagnostic codes."""


def parse_image_ref(value):
    match = IMAGE_REF.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise MemoryImageError("invalid_image_ref")
    return match[1]


def validate_image(data: bytes, mime_type: str):
    """Decode only bounded in-memory raster bytes; preserve rather than re-encode originals."""
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_BYTES:
        raise MemoryImageError("image_size_limit")
    if not isinstance(mime_type, str) or mime_type not in MIMES.values():
        raise MemoryImageError("image_format_unsupported")
    if not _DECODERS.acquire(blocking=False):
        raise MemoryImageError("image_decoder_busy")
    try:
        try:
            from PIL import Image
        except ImportError:
            raise MemoryImageError("image_decoder_unavailable") from None
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=tuple(MIMES)) as image:
                if MIMES.get(image.format) != mime_type:
                    raise MemoryImageError("image_mime_mismatch")
                width, height = image.size
                if not (1 <= width <= 16384 and 1 <= height <= 16384 and width * height <= MAX_PIXELS):
                    raise MemoryImageError("image_pixel_limit")
                if getattr(image, "n_frames", 1) != 1:
                    raise MemoryImageError("animated_image_unsupported")
                image.verify()
            # verify alone is not a complete JPEG decode. Do a bounded actual decode too.
            with Image.open(BytesIO(data), formats=tuple(MIMES)) as image:
                image.load()
        return width, height
    except MemoryImageError:
        raise
    except Exception:
        raise MemoryImageError("image_invalid_or_damaged") from None
    finally:
        _DECODERS.release()


def decode_image_base64(value, mime_type):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_BASE64:
        raise MemoryImageError("image_size_limit")
    try:
        data = base64.b64decode(value, validate=True)
        if base64.b64encode(data).decode("ascii") != value:
            raise ValueError
    except (ValueError, UnicodeError):
        raise MemoryImageError("image_base64_invalid") from None
    width, height = validate_image(data, mime_type)
    return data, width, height


class MemoryImageStore(MemoryRelationStore):
    """Uses the existing bounded SQL connection and original endpoint identity checks."""
    def __init__(self, database):
        from pathlib import Path
        self.database = str(Path(database).resolve())
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            present = db.execute("SELECT name FROM sqlite_master WHERE name IN (?,?,?)", tuple(DDL)).fetchall()
            if not present:
                for _, sql in DDL.values():
                    db.execute(sql)
            self._schema(db)
            db.commit()

    @staticmethod
    def _schema(db):
        for name, (kind, sql) in DDL.items():
            row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if row is None or row[0] != kind or not isinstance(row[1], str) or _ddl(row[1]) != _ddl(sql):
                raise MemoryImageError("image_storage_unavailable")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?,?)", tuple(DDL)[:2]).fetchone():
            raise MemoryImageError("image_storage_unavailable")

    @staticmethod
    def _endpoint(db, scope, endpoint, *, current=False):
        result = MemoryRelationStore._endpoint(db, scope, endpoint, current=current)
        # Relation projection may legitimately return a restricted stub; image
        # descriptions and original bytes are content, not safe relation metadata.
        # This first version has no sensitive-original confirmation protocol.
        blocked = result["restricted_stub"]
        if endpoint[0] == "emotion":
            row = db.execute("SELECT explicit_request_override FROM emotion_memories WHERE owner_id=? AND model_id=? AND memory_id=?",
                             (*scope, endpoint[1])).fetchone()
            blocked = blocked or row[0] == "never"
        elif endpoint[0] == "learning":
            row = db.execute("SELECT json_extract(current_json,'$.explicit_request_override') FROM learning_items WHERE owner_id=? AND model_id=? AND learning_id=?",
                             (*scope, endpoint[1])).fetchone()
            blocked = blocked or row[0] == "never"
        if blocked:
            raise MemoryImageError("image_sensitive_target_unsupported")
        return result

    @staticmethod
    def _metadata(row):
        return {"image_ref": f"image://{row['image_id']}@1", "target_ref": row["target_ref"],
                **{key: row[key] for key in ("description", "mime_type", "sha256", "byte_count", "width", "height", "created_at")},
                "active": bool(row["active"]), "automatic_recall_eligible": False,
                "description_basis": "caller_supplied_not_visual_verification", "original_bytes_preserved": True}

    def store(self, *, owner_id, model_id, target_ref, description, mime_type, data_base64,
              request_key, operation_hash, _write_guard):
        self._identity(owner_id, model_id)
        endpoint = parse_memory_ref(target_ref)
        if not isinstance(description, str) or not description.strip() or len(description) > 2000 or "\x00" in description:
            raise MemoryImageError("invalid_image_description")
        self._key(request_key); self._key(operation_hash)
        data, width, height = decode_image_base64(data_base64, mime_type)
        digest = hashlib.sha256(data).hexdigest()
        scope = owner_id, model_id
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE"); self._schema(db); assert_bound_execution(db); _write_guard(db)
            self._endpoint(db, scope, endpoint, current=True)
            replay = db.execute("SELECT operation_hash,image_id FROM memory_image_requests WHERE owner_id=? AND model_id=? AND request_key=?",
                                (*scope, request_key)).fetchone()
            if replay is not None:
                if replay["operation_hash"] != operation_hash:
                    raise MemoryImageError("request_conflict")
                row = db.execute(f"SELECT {META} FROM memory_image_items WHERE owner_id=? AND model_id=? AND image_id=?",
                                 (*scope, replay["image_id"])).fetchone()
                # The tombstone is authoritative: replay cannot reattach removed bytes.
                return {"decision": "stored" if row["active"] else "removed", "state_changed": False,
                        "idempotent_replay": True, **self._metadata(row)}
            count, used = db.execute("SELECT count(*),coalesce(sum(byte_count),0) FROM memory_image_items WHERE owner_id=? AND model_id=?", scope).fetchone()
            if count >= MAX_OWNER_ITEMS or used + len(data) > MAX_OWNER_BYTES:
                raise MemoryImageError("image_owner_capacity_limit")
            image_id = uuid.uuid4().hex
            db.execute("INSERT INTO memory_image_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                       (*scope, image_id, target_ref, *endpoint[:2], description, mime_type, digest, len(data), width, height, data, _now()))
            db.execute("INSERT INTO memory_image_requests VALUES(?,?,?,?,?)", (*scope, request_key, operation_hash, image_id))
            row = db.execute(f"SELECT {META} FROM memory_image_items WHERE owner_id=? AND model_id=? AND image_id=?", (*scope, image_id)).fetchone()
            db.commit()
            return {"decision": "stored", "state_changed": True, "idempotent_replay": False, **self._metadata(row)}

    def list(self, *, owner_id, model_id, target_ref, limit=20, offset=0):
        self._identity(owner_id, model_id); endpoint = parse_memory_ref(target_ref)
        if type(limit) is not int or not 1 <= limit <= 20 or type(offset) is not int or not 0 <= offset <= MAX_OWNER_ITEMS:
            raise MemoryImageError("invalid_image_page")
        with self._connection() as db:
            db.execute("BEGIN"); self._schema(db); assert_bound_execution(db)
            self._endpoint(db, (owner_id, model_id), endpoint)
            rows = db.execute(f"SELECT {META} FROM memory_image_items WHERE owner_id=? AND model_id=? AND target_module=? AND target_id=? AND active=1 ORDER BY created_at,image_id LIMIT ? OFFSET ?",
                              (owner_id, model_id, *endpoint[:2], limit + 1, offset)).fetchall()
            return {"decision": "listed", "state_changed": False, "images": [self._metadata(row) for row in rows[:limit]],
                    "next_offset": offset + limit if len(rows) > limit else None, "automatic_recall_eligible": False}

    def _attached(self, db, scope, endpoint, image_ref, *, include_data=False):
        identity = parse_image_ref(image_ref)
        fields = META + (",original_bytes" if include_data else "")
        row = db.execute(f"SELECT {fields} FROM memory_image_items WHERE owner_id=? AND model_id=? AND image_id=? AND target_module=? AND target_id=? AND active=1",
                         (*scope, identity, *endpoint[:2])).fetchone()
        if row is None:
            raise MemoryImageError("image_not_available")
        return row

    def read(self, *, owner_id, model_id, target_ref, image_ref, include_data=False):
        self._identity(owner_id, model_id); endpoint = parse_memory_ref(target_ref)
        if type(include_data) is not bool:
            raise MemoryImageError("invalid_image_read_mode")
        with self._connection() as db:
            db.execute("BEGIN"); self._schema(db); assert_bound_execution(db)
            self._endpoint(db, (owner_id, model_id), endpoint)
            row = self._attached(db, (owner_id, model_id), endpoint, image_ref, include_data=include_data)
            result = {"decision": "read", "state_changed": False, **self._metadata(row)}
            if include_data:
                data = row["original_bytes"]
                if not isinstance(data, bytes) or len(data) != row["byte_count"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                    raise MemoryImageError("image_integrity_failed")
                result["data_uri"] = "data:" + row["mime_type"] + ";base64," + base64.b64encode(data).decode("ascii")
            return result

    def remove(self, *, owner_id, model_id, target_ref, image_ref, _write_guard):
        self._identity(owner_id, model_id); endpoint = parse_memory_ref(target_ref)
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE"); self._schema(db); assert_bound_execution(db); _write_guard(db)
            self._endpoint(db, (owner_id, model_id), endpoint)
            identity = parse_image_ref(image_ref)
            row = db.execute(f"SELECT {META} FROM memory_image_items WHERE owner_id=? AND model_id=? AND image_id=? AND target_module=? AND target_id=?",
                             (owner_id, model_id, identity, *endpoint[:2])).fetchone()
            if row is None:
                raise MemoryImageError("image_not_available")
            changed = bool(row["active"])
            db.execute("UPDATE memory_image_items SET active=0 WHERE owner_id=? AND model_id=? AND image_id=?", (owner_id, model_id, identity))
            db.commit()
            return {"decision": "removed", "state_changed": changed, "image_ref": image_ref, "active": False,
                    "original_retained_for_backup": True, "automatic_recall_eligible": False}
