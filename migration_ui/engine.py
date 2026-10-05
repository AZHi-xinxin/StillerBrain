"""One sequential run; explicit consent precedes classification and writes."""
from __future__ import annotations

import threading
import uuid

from .clients import Classifier, STClient, Transport, fetch_source
from .core import (MAX_SOURCE, MigrationError, canonical, digest, has_configured_secret,
                   no_secrets, parse_source, receipt, strict_json, write_projection)


class MigrationEngine:
    def __init__(self, ledger, *, transport=None, classifier_factory=Classifier, st_factory=STClient):
        self.ledger = ledger
        self.transport = transport or Transport()
        self.classifier_factory, self.st_factory = classifier_factory, st_factory
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.running = False
        self.prepared = None
        self.report = {"phase": "idle", "rows": []}
        self.worker = None

    def prepare(self, config):
        with self.lock:
            if self.running:
                raise MigrationError("run_already_active")
            self.prepared = None
            if config.get("kind") == "url":
                raw = fetch_source(config, self.transport)
            elif config.get("kind") == "file":
                # The browser uploads bytes as base64; no arbitrary filesystem
                # path is accepted by this local web service.
                import base64
                value = config.get("base64")
                if not isinstance(value, str) or len(value) > (MAX_SOURCE * 4 // 3 + 8):
                    raise MigrationError("source_size_limit")
                try:
                    raw = base64.b64decode(value, validate=True)
                except ValueError as exc:
                    raise MigrationError("source_invalid_upload") from exc
            else:
                raise MigrationError("unsupported_source_type")
            sha, chunks = parse_source(raw, config.get("format"), config.get("text_field", "content"))
            if config.get("kind") == "url" and config.get("key"):
                # A source which echoes its own auth header must not persist
                # that credential as export data, even via JSON escaping or a
                # metadata/field-name not selected for classification.
                decoded = raw.decode("utf-8-sig")
                values = [decoded, *(c.content for c in chunks)]
                if config.get("format") == "json":
                    values.append(strict_json(decoded))
                if has_configured_secret(values, (config["key"],)):
                    raise MigrationError("configured_credential_in_source")
            # Persist only the immutable export, never source auth or URL.
            self.ledger.retain(sha, raw, config["format"])
            token = uuid.uuid4().hex
            self.prepared = (token, sha, chunks)
            self.report = {"phase": "prepared", "source_sha": sha, "total": len(chunks), "rows": [],
                           "source_bytes": len(raw), "prepared_id": token,
                           "preview": [{"record": c.record, "offset": c.offset, "characters": len(c.content), "text": c.content[:160]} for c in chunks[:5]]}
            return self.snapshot()

    def snapshot(self):
        with self.lock:
            return {**self.report, "running": self.running, "cancel_requested": self.cancelled.is_set(),
                    "rows": list(self.report.get("rows", [])), "ledger_counts": self.ledger.summary()}

    def cancel(self):
        self.cancelled.set()

    def start(self, config):
        with self.lock:
            if self.running:
                raise MigrationError("run_already_active")
            if config.get("consent") is not True:
                raise MigrationError("content_flow_consent_required")
            if not self.prepared or config.get("prepared_id") != self.prepared[0]:
                raise MigrationError("source_preview_required")
            classifier = self.classifier_factory(config.get("model", {}), self.transport)
            target = self.st_factory(config.get("target", {}), self.transport)
            # Namespace is a stable human-selected destination identity. Never
            # store/hash credentials into provenance. A different owner at the
            # same URL MUST use a different namespace (shown in consent UI).
            scope = digest(canonical({"url": target.url, "namespace": target.namespace}))
            no_secrets(target.namespace, (classifier.key, target.key))
            no_secrets(classifier.prompt, (classifier.key, target.key))
            no_secrets(classifier.model, (classifier.key, target.key))
            self.cancelled.clear()
            self.running = True
            self.report.update(phase="preflight", rows=[], error=None)
            _, sha, chunks = self.prepared
            self.worker = threading.Thread(target=self._run, args=(sha, chunks, scope, classifier, target), daemon=False)
            self.worker.start()
            return {"started": True}

    def _row(self, chunk, state, code=None, result=None):
        with self.lock:
            self.report["rows"].append({"record": chunk.record, "offset": chunk.offset,
                "chunk": chunk.id, "state": state, "code": code, "receipt": result})

    def _run(self, sha, chunks, scope, classifier, target):
        try:
            target.preflight()
            for chunk in chunks:
                if self.cancelled.is_set():
                    break
                old = self.ledger.get(scope, chunk.id)
                if old and old["state"] in {"STORED", "UNKNOWN", "SENDING"}:
                    self._row(chunk, "SKIPPED_STORED" if old["state"] == "STORED" else "UNKNOWN", old.get("code") or "interrupted_in_flight")
                    if old["state"] != "STORED":
                        break
                    continue
                with self.lock:
                    self.report["phase"] = "classifying"
                try:
                    no_secrets(chunk.content, (classifier.key, target.key))
                    result = classifier.classify(chunk)
                    no_secrets(result, (classifier.key, target.key))
                    projection = write_projection(chunk, sha, result)
                    if projection is None:
                        raise MigrationError("classification_needs_review")
                    tool, args = projection
                    target.validate(tool, args)
                except MigrationError as exc:
                    code = str(exc)
                    self.ledger.mark(scope, chunk, sha, "PENDING", code=code)
                    self._row(chunk, "PENDING", code)
                    if code in {"http_response_error", "connection_result_unknown", "response_deadline", "response_size_limit", "classifier_invalid_response", "classifier_output_limit", "classifier_tool_call_rejected"}:
                        break
                    continue
                if self.cancelled.is_set():
                    break
                # Commit before the network attempt. Any following exception
                # is unknown unless the target explicitly says no write.
                self.ledger.mark(scope, chunk, sha, "SENDING", payload_hash=digest(canonical({"tool": tool, "arguments": args})))
                with self.lock:
                    self.report["phase"] = "writing"
                try:
                    state, saved = receipt(tool, target.write(tool, args))
                    code = saved.get("code")
                except Exception:
                    state, saved, code = "UNKNOWN", {}, "target_result_unknown"
                self.ledger.mark(scope, chunk, sha, state, result=saved, code=code)
                self._row(chunk, state, code, saved or None)
                if state == "UNKNOWN" or code == "module_one_required":
                    break
            with self.lock:
                rows = self.report["rows"]
                self.report["phase"] = ("cancelled" if self.cancelled.is_set() else
                    "needs_attention" if any(r["state"] in {"UNKNOWN", "PENDING", "REJECTED"} for r in rows) else "completed")
        except MigrationError as exc:
            with self.lock:
                self.report.update(phase="stopped", error=str(exc))
        except Exception:
            with self.lock:
                self.report.update(phase="stopped", error="local_processing_error")
        finally:
            with self.lock:
                self.running = False
            # classifier/target and their credentials live only for this run;
            # no prompt, model key, target key or full configuration is logged.
