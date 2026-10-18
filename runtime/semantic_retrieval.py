"""Optional, local-only summary retrieval. Derived vectors never grant disclosure.

The caller supplies only eligible current projections. A single daemon worker
owns the model and SQLite indexes; cold starts and failures fall back to lexical
retrieval without waiting for model initialization. No network or query logging.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import sqlite3
import sys
import threading
import time
from typing import Callable, Iterable, Sequence

MODEL = "BAAI/bge-small-zh-v1.5"
MODEL_REVISION = "summary-embedding/2-f32le"
MIN_COSINE = 0.50  # Development recall threshold, not a factual confidence.
MAX_DOCUMENTS = 4096
MAX_TEXT = 1600
EXPLICIT_WAIT_SECONDS = 2.0


@dataclass(frozen=True)
class Document:
    ref: str
    version: int
    text: str

    @property
    def digest(self) -> str:
        return _hash([self.version, self.text[:MAX_TEXT]])


@dataclass(frozen=True)
class Result:
    status: str
    scores: dict[str, float]


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _unit(vector: Iterable[float]) -> list[float]:
    values = [float(v) for v in vector]
    if not values or not all(math.isfinite(v) for v in values):
        raise ValueError("invalid_embedding")
    norm = math.sqrt(sum(v * v for v in values))
    if not norm:
        raise ValueError("empty_embedding")
    return [v / norm for v in values]


def candidate_priority(cosine: float) -> float:
    """Ranking hint, NOT probability. Exact lexical hits still take precedence."""
    return round(0.5 + 0.3 * max(0.0, min(1.0, cosine)), 4)


def _encode_vector(vector: Iterable[float]) -> bytes:
    values = array('f', _unit(vector))
    if sys.byteorder != 'little':
        values.byteswap()
    return values.tobytes()


def _decode_vector(blob: bytes) -> array:
    if not isinstance(blob, bytes) or not blob or len(blob) % 4:
        raise ValueError('invalid_cached_embedding')
    values = array('f')
    values.frombytes(blob)
    if sys.byteorder != 'little':
        values.byteswap()
    norm_squared = sum(v * v for v in values)
    if not math.isfinite(norm_squared) or abs(norm_squared - 1.0) > 0.001:
        raise ValueError('invalid_cached_embedding')
    return values


class LocalEncoder:
    def __init__(self, directory: str):
        from fastembed import TextEmbedding
        self.model = TextEmbedding(
            model_name=MODEL, specific_model_path=directory,
            local_files_only=True, threads=2, providers=["CPUExecutionProvider"],
        )

    def passages(self, texts: Sequence[str]):
        return self.model.passage_embed(list(texts), batch_size=32)

    def query(self, text: str):
        return next(iter(self.model.query_embed(text)))


class VectorIndex:
    """Disposable index; stores vectors + hashed refs/content, never source text."""
    def __init__(self, path: Path, encoder: object):
        self.path, self.encoder = path, encoder

    def search(self, documents: Sequence[Document], query: str, threshold: float = MIN_COSINE) -> Result:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=0.25)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS vectors (ref TEXT PRIMARY KEY, digest TEXT, vector BLOB)")
            saved = {row[0]: (row[1], row[2]) for row in connection.execute("SELECT ref,digest,vector FROM vectors")}
            keys = {_hash(doc.ref) for doc in documents}
            # Reconcile against this request's eligible snapshot, not stale hits.
            connection.executemany("DELETE FROM vectors WHERE ref=?", [(key,) for key in saved.keys() - keys])
            changed = [doc for doc in documents if saved.get(_hash(doc.ref), (None,))[0] != doc.digest]
            if changed:
                encoded = list(self.encoder.passages([doc.text[:MAX_TEXT] for doc in changed]))
                if len(encoded) != len(changed):
                    raise ValueError("embedding_count_mismatch")
                for doc, vector in zip(changed, encoded):
                    key, serialized = _hash(doc.ref), _encode_vector(vector)
                    connection.execute("INSERT OR REPLACE INTO vectors VALUES (?,?,?)", (key, doc.digest, serialized))
                    saved[key] = (doc.digest, serialized)
            if not documents or not query.strip():
                return Result("ready", {})
            needle = _unit(self.encoder.query(query[:4000]))
            ranked = []
            for doc in documents:
                digest, serialized = saved[_hash(doc.ref)]
                if digest != doc.digest:
                    continue
                vector = _decode_vector(serialized)
                if len(vector) != len(needle):
                    raise ValueError("embedding_dimension_mismatch")
                cosine = sum(left * right for left, right in zip(needle, vector))
                if cosine >= threshold:
                    ranked.append((doc.ref, round(min(1.0, cosine), 6)))
            ranked.sort(key=lambda item: (-item[1], item[0]))
            return Result("ready", dict(ranked[:5]))


class SemanticWorker:
    def __init__(self, factory: Callable[[str], object] = LocalEncoder):
        self.factory = factory
        self.requests: queue.Queue = queue.Queue(maxsize=8)
        self.lock = threading.Lock()
        self.pending: dict[str, threading.Event] = {}
        self.completed: OrderedDict[str, tuple[float, Result]] = OrderedDict()
        self.started = False
        self.warm = False
        self.encoder = None
        self.encoder_directory = None

    def request(self, namespace: str, directory: str, index_dir: str,
                documents: Sequence[Document], query: str, wait: float = 0.15,
                encoder_signature: str = "", *, wait_for_cold: bool = False) -> Result:
        if len(documents) > MAX_DOCUMENTS:
            return Result("corpus_limit_lexical_fallback", {})
        docs = tuple(sorted(documents, key=lambda doc: doc.ref))
        if len({doc.ref for doc in docs}) != len(docs):
            return Result("invalid_projection_lexical_fallback", {})
        key = _hash([namespace, query, [(d.ref, d.digest) for d in docs]])
        with self.lock:
            cached = self.completed.get(key)
            if cached and (cached[1].status == "ready" or time.monotonic() - cached[0] < 30):
                self.completed.move_to_end(key)
                return cached[1]
            event = self.pending.get(key)
            if event is None:
                event = threading.Event()
                try:
                    self.requests.put_nowait((key, event, namespace, directory, index_dir, docs, query, encoder_signature))
                except queue.Full:
                    return Result("busy_lexical_fallback", {})
                self.pending[key] = event
            if not self.started:
                self.started = True
                threading.Thread(target=self._run, name="st-local-semantic", daemon=True).start()
            warm = self.warm
        if warm or wait_for_cold:
            # Explicit lookup may await the first result; automatic injection
            # retains its nonblocking cold start and short hot deadline.
            ceiling = EXPLICIT_WAIT_SECONDS if wait_for_cold else 0.15
            event.wait(max(0.0, min(ceiling, wait)))
        with self.lock:
            return self.completed.get(key, (0, Result("indexing_lexical_fallback", {})))[1]

    def _run(self):
        while True:
            key, event, namespace, directory, index_dir, docs, query, signature = self.requests.get()
            try:
                if self.encoder_directory != (directory, signature):
                    self.encoder = self.factory(directory)
                    self.encoder_directory = (directory, signature)
                result = VectorIndex(Path(index_dir) / (namespace + ".sqlite3"), self.encoder).search(docs, query)
            except Exception:
                # Error text can contain local paths or source fragments. Do not
                # log or return it, and do not poison later lexical requests.
                result = Result("unavailable_lexical_fallback", {})
            with self.lock:
                self.warm = result.status == "ready"
                self.completed[key] = (time.monotonic(), result)
                self.pending.pop(key, None)
                while len(self.completed) > 16:
                    self.completed.popitem(last=False)
                event.set()
            self.requests.task_done()


_worker = SemanticWorker()


def search(*, database: str, owner_id: str, model_id: str, module: str,
           documents: Sequence[Document], query: str, explicit_wait: bool = False) -> Result:
    directory = os.environ.get("STBRAIN_SEMANTIC_MODEL_DIR", "")
    index_dir = os.environ.get("STBRAIN_SEMANTIC_INDEX_DIR", "")
    if not directory or not index_dir:
        return Result("disabled", {})
    if not query.strip() or not documents:
        return Result("empty", {})
    try:
        model_path, index_path = Path(directory), Path(index_dir)
        if not model_path.is_absolute() or not index_path.is_absolute() or not model_path.is_dir():
            return Result("invalid_configuration_lexical_fallback", {})
        # A replacement model/tokenizer invalidates vectors and warm results.
        files = sorted((str(p.relative_to(model_path)), p.stat().st_size, p.stat().st_mtime_ns)
                       for p in model_path.rglob("*") if p.is_file())
        namespace = _hash([str(Path(database).resolve()), owner_id, model_id, module,
                           MODEL, MODEL_REVISION, str(model_path.resolve()), files, str(index_path.resolve())])
        return _worker.request(namespace, str(model_path.resolve()), str(index_path.resolve()), documents, query[:4000],
                               encoder_signature=_hash(files), wait_for_cold=explicit_wait,
                               wait=EXPLICIT_WAIT_SECONDS if explicit_wait else 0.15)
    except (OSError, ValueError):
        return Result("invalid_configuration_lexical_fallback", {})
