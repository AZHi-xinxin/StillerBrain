"""Image attachments retain their target module's ordinary/direct/claim authorization."""
from contextlib import nullcontext
from copy import copy
import base64
import hashlib

from runtime.execution_binding import ExecutionBindingError, canonical_hash
from runtime.memory_images import CONTRACT, MAX_BASE64, MemoryImageError, parse_image_ref
from runtime.memory_image_uploads import UPLOAD_CONTRACT, default_image_uploads
from runtime.memory_relations import MemoryRelationError, SCOPES, parse_memory_ref
from runtime.onboarding import OnboardingError
from runtime.ordinary_access import current_ordinary_access
from .memory_relation_service import MemoryRelationAccessService, SAFE as RELATION_SAFE

SAFE = RELATION_SAFE | frozenset({"invalid_image_ref", "image_size_limit", "image_format_unsupported",
    "image_decoder_busy", "image_decoder_unavailable", "image_mime_mismatch", "image_pixel_limit",
    "animated_image_unsupported", "image_invalid_or_damaged", "image_base64_invalid", "image_storage_unavailable",
    "invalid_image_description", "image_owner_capacity_limit", "invalid_image_page", "image_not_available",
    "invalid_image_read_mode", "image_integrity_failed", "image_scope_not_authorized", "image_sensitive_target_unsupported",
    "invalid_image_source", "invalid_image_upload_ref", "image_upload_protocol_unsupported", "invalid_image_upload_hash",
    "image_upload_hash_mismatch", "image_upload_reference_conflict", "image_upload_capacity_limit",
    "image_upload_not_available", "image_upload_mime_mismatch", "image_upload_integrity_failed"})


class MemoryImageAccessService(MemoryRelationAccessService):
    def __init__(self, *args, uploads=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.uploads = default_image_uploads if uploads is None else uploads

    def stage_upload(self, protocol, upload_ref, mime_type, sha256, data_base64):
        """Authenticated transport only; no ordinary/direct context or DB write."""
        try:
            return self.uploads.stage(**self.identity, protocol=protocol, upload_ref=upload_ref,
                mime_type=mime_type, sha256=sha256, data_base64=data_base64)
        except MemoryRelationError as error:
            code = str(error) if str(error) in SAFE else "image_operation_rejected"
            return {"contract": UPLOAD_CONTRACT, "decision": "reject", "reason_code": code,
                    "state_changed": False, "memory_written": False, "write_authority_granted": False}

    @staticmethod
    def _reject(code):
        code = code if code in SAFE else "image_operation_rejected"
        return {"contract_version": CONTRACT, "decision": "reject", "reason_code": code,
                "state_changed": False, "automatic_recall_eligible": False}

    def _binding(self, tool, scope, write_context_ref):
        claim = self._claim(tool); self._gate()
        ordinary = current_ordinary_access(**self.identity, scope=scope)
        if ordinary is not None:
            if write_context_ref is not None and write_context_ref != ordinary["write_context_ref"]:
                raise MemoryImageError("write_context_binding_mismatch")
            if claim is not None and ordinary["wake_id"] != claim.wake_id:
                raise MemoryImageError("execution_wake_mismatch")
            def guard(db):
                self._gate(db)
                if current_ordinary_access(**self.identity, scope=scope) != ordinary or self._claim(tool) != claim:
                    raise MemoryImageError("write_context_binding_mismatch")
            return claim, guard
        if claim is not None:
            if write_context_ref is not None:
                raise MemoryImageError("explicit_context_conflicts_with_bound_execution")
            opened = self.onboarding.open_brain_context(**self.identity, present_details=False, expected_wake_id=claim.wake_id)
            write_context_ref = opened.get("write_context_ref") if opened.get("write_context_available") else None
        if not isinstance(write_context_ref, str) or not write_context_ref or write_context_ref.startswith("$"):
            raise MemoryImageError("execution_binding_required")

        def verify(onboarding):
            bound = onboarding.current_open_write_context(**self.identity, write_context_ref=write_context_ref,
                required_scope=scope, expected_wake_id=claim.wake_id if claim else None)
            if bound.get("write_context_available") is not True:
                raise MemoryImageError("image_scope_not_authorized")
            if claim is None and (bound.get("context_mode") != "human_attested_direct" or scope not in bound.get("authorized_scopes", [])):
                raise MemoryImageError("image_scope_not_authorized")
            if claim is not None and (bound.get("context_mode") != "gateway_injected" or bound.get("wake_id") != claim.wake_id):
                raise MemoryImageError("execution_wake_mismatch")
        verify(self.onboarding)

        def guard(db):
            self._gate(db)
            if self._claim(tool) != claim:
                raise MemoryImageError("execution_owner_mismatch")
            view = copy(self.onboarding)
            view._connect = lambda: nullcontext(db)
            def existing_state(**identity):
                if view._state_row(db, identity["owner_id"], identity["model_id"]) is None:
                    raise MemoryImageError("image_scope_not_authorized")
            view.ensure_state = existing_state
            verify(view)
        return claim, guard

    def _read_scope(self, tool, target_ref):
        scope = SCOPES[parse_memory_ref(target_ref)[0]]
        self._claim(tool)
        if current_ordinary_access(**self.identity, scope=scope) is None:
            module = {"emotional_memory": "emotional_memory_module_two", "learning_memory": "learning_memory_module_three",
                      "planning_memory": "planning_memory_module_five"}[scope]
            if self.onboarding.authorize_other_module_write(**self.identity, module_name=module).get("decision") != "allowed":
                raise MemoryImageError("module_one_required")

    def _key(self, claim, request_id, operation):
        if claim is not None:
            if request_id is not None:
                raise MemoryImageError("request_id_conflicts_with_bound_execution")
            return canonical_hash([CONTRACT, "gateway", claim.owner_id, claim.model_id, claim.wake_id,
                                   claim.batch_id, claim.call_id, claim.tool_name, claim.deployment_epoch])
        if request_id is None:
            return canonical_hash([CONTRACT, "content-retry", operation])
        # Use the existing strict direct-request validation, but a separate namespace.
        return canonical_hash([CONTRACT, super()._key(None, request_id)])

    def put(self, target_ref, description, mime_type, data_base64=None, request_id=None, write_context_ref=None,
            upload_ref=None):
        try:
            self._protected([target_ref, description, mime_type, request_id])
            scope = SCOPES[parse_memory_ref(target_ref)[0]]
            claim, guard = self._binding("store_memory_image", scope, write_context_ref)
            if (data_base64 is None) == (upload_ref is None):
                raise MemoryImageError("invalid_image_source")
            if upload_ref is not None:
                # The outer execution guard already hashed the ORIGINAL tool
                # arguments. Resolve privately only after its claim and this
                # module's original authorization; never rewrite the MCP args.
                original = self.uploads.resolve(**self.identity, upload_ref=upload_ref, mime_type=mime_type)
                data_base64 = base64.b64encode(original).decode("ascii")
            if not isinstance(data_base64, str) or not 1 <= len(data_base64) <= MAX_BASE64:
                raise MemoryImageError("image_size_limit")
            try:
                digest = hashlib.sha256(data_base64.encode("ascii")).hexdigest()
            except UnicodeError:
                raise MemoryImageError("image_base64_invalid") from None
            operation = canonical_hash([CONTRACT, target_ref, description, mime_type, digest])
            result = self.store.store(**self.identity, target_ref=target_ref, description=description,
                mime_type=mime_type, data_base64=data_base64, request_key=self._key(claim, request_id, operation),
                operation_hash=operation, _write_guard=guard)
            self._protected(result)
            return {"contract_version": CONTRACT, **result}
        except (MemoryRelationError, ExecutionBindingError) as error:
            return self._reject(str(error))
        except OnboardingError:
            return self._reject("image_scope_not_authorized")

    def list(self, target_ref, limit=20, offset=0):
        try:
            self._protected([target_ref, limit, offset]); self._read_scope("list_memory_images", target_ref)
            result = self.store.list(**self.identity, target_ref=target_ref, limit=limit, offset=offset)
            self._protected(result)
            return {"contract_version": CONTRACT, **result}
        except (MemoryRelationError, ExecutionBindingError) as error:
            return self._reject(str(error))
        except OnboardingError:
            return self._reject("image_scope_not_authorized")

    def read(self, target_ref, image_ref, include_data=False):
        try:
            self._protected([target_ref, image_ref]); parse_image_ref(image_ref)
            self._read_scope("read_memory_image", target_ref)
            result = self.store.read(**self.identity, target_ref=target_ref, image_ref=image_ref, include_data=include_data)
            # No OCR/credential detection claim for pixels, EXIF or the original byte stream.
            self._protected({k: v for k, v in result.items() if k != "data_uri"})
            return {"contract_version": CONTRACT, **result}
        except (MemoryRelationError, ExecutionBindingError) as error:
            return self._reject(str(error))
        except OnboardingError:
            return self._reject("image_scope_not_authorized")

    def remove(self, target_ref, image_ref, write_context_ref=None):
        try:
            self._protected([target_ref, image_ref]); parse_image_ref(image_ref)
            _, guard = self._binding("remove_memory_image", SCOPES[parse_memory_ref(target_ref)[0]], write_context_ref)
            result = self.store.remove(**self.identity, target_ref=target_ref, image_ref=image_ref, _write_guard=guard)
            return {"contract_version": CONTRACT, **result}
        except (MemoryRelationError, ExecutionBindingError) as error:
            return self._reject(str(error))
        except OnboardingError:
            return self._reject("image_scope_not_authorized")
