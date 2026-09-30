"""Authenticated explicit work-memory access, sharing ordinary learning scope.

No self-module, automatic recall hook, consultation fields or hidden identity
selection is introduced. The existing dispatcher still owns transport and
execution claims; this facade independently checks their identity and gates.
"""
from __future__ import annotations

from pathlib import Path
from contextlib import nullcontext
import re
from types import SimpleNamespace

from runtime.execution_binding import ExecutionBindingError, ExecutionClaim, canonical_hash, current_execution_claim
from runtime.learning_memory import LEARNING_MODULE
from runtime.onboarding import OnboardingError
from runtime.ordinary_access import current_ordinary_access
from runtime.work_memory import ATLAS_TOKEN, WorkMemoryError, WorkMemoryStore, parse_ref, validate_content_tag, validate_revision_fields
from .ordinary_access_policy import _module_one_allows_ordinary_writes


REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
CONTRACT = "work-memory/1"
SAFE_CODES = frozenset({
    "invalid_content", "invalid_tag", "invalid_query", "invalid_work_query", "invalid_work_ref", "invalid_identity",
    "invalid_request_key", "invalid_request_id", "empty_work_revision", "work_memory_not_found", "work_version_conflict",
    "request_conflict", "work_memory_unavailable", "credential_or_secret_detected", "module_one_required",
    "execution_owner_mismatch", "execution_wake_mismatch", "execution_claim_not_current", "execution_registry_unavailable",
    "execution_binding_required", "write_context_binding_mismatch", "explicit_context_conflicts_with_bound_execution",
    "request_id_conflicts_with_bound_execution", "brain_open_required", "invalid_direct_context", "direct_scope_not_authorized",
    "work_memory_authorization_unavailable", "invalid_work_lifecycle",
})


class WorkMemoryAccessService:
    def __init__(self, store: WorkMemoryStore, *, onboarding, owner_id: str, model_id: str, protected_values=()):
        if (not isinstance(owner_id, str) or not owner_id.strip() or not isinstance(model_id, str) or not model_id.strip() or
                Path(store.database).resolve() != Path(onboarding.database).resolve()):
            raise ValueError("work_memory_configuration_invalid")
        self.store, self.onboarding = store, onboarding
        self.owner_id, self.model_id = owner_id.strip(), model_id.strip()
        if (not isinstance(protected_values, (list, tuple, set, frozenset)) or
                any(not isinstance(value, str) or not value for value in protected_values)):
            raise ValueError("work_memory_configuration_invalid")
        # Only credentials already held by this process; never import other
        # roles' keys merely to extend this exact-value protection list.
        self._protected_values = tuple(frozenset(protected_values))

    @property
    def identity(self):
        return {"owner_id": self.owner_id, "model_id": self.model_id}

    @staticmethod
    def _reject(code):
        code = code if code in SAFE_CODES else "work_memory_operation_rejected"
        return {"module": "work_memory", "contract_version": CONTRACT, "decision": "reject",
                "reason_code": code, "reason_codes": [code], "stored": False,
                "state_changed": False, "automatic_recall_eligible": False}

    def _result(self, result):
        result = {"module": "work_memory", "contract_version": CONTRACT, **result}
        self._protected(result)
        return result

    def _claim(self, tool):
        claim = current_execution_claim()
        if claim is not None and (
            not isinstance(claim, ExecutionClaim) or (claim.owner_id, claim.model_id) != (self.owner_id, self.model_id) or
            claim.tool_name != tool or Path(claim.database).resolve() != Path(self.store.database).resolve()
        ):
            raise WorkMemoryError("execution_owner_mismatch")
        return claim

    def _can_write(self):
        # An ordinary ContextVar alone must not override the real activation
        # records; this duplicates the existing dispatcher prerequisite.
        if not _module_one_allows_ordinary_writes(self.onboarding, **self.identity):
            raise WorkMemoryError("module_one_required")
        permission = self.onboarding.authorize_other_module_write(**self.identity, module_name=LEARNING_MODULE)
        if permission.get("decision") != "allowed":
            raise WorkMemoryError("module_one_required")

    def _transaction_gate(self, connection):
        # Reuse the exact existing activation rule on the already locked
        # transaction, eliminating an activation-change TOCTOU without another
        # connection or inventing a work-memory permission model.
        adapter = SimpleNamespace(_connect=lambda: nullcontext(connection))
        if not _module_one_allows_ordinary_writes(adapter, **self.identity):
            raise WorkMemoryError("module_one_required")

    def _binding(self, tool, write_context_ref):
        claim = self._claim(tool)
        self._can_write()
        ordinary = current_ordinary_access(**self.identity, scope="learning_memory")
        if ordinary is not None:
            if write_context_ref is not None and write_context_ref != ordinary["write_context_ref"]:
                raise WorkMemoryError("write_context_binding_mismatch")
            ref = ordinary["write_context_ref"]
        elif claim is not None:
            if write_context_ref is not None:
                raise WorkMemoryError("explicit_context_conflicts_with_bound_execution")
            opened = self.onboarding.open_brain_context(**self.identity, present_details=False, expected_wake_id=claim.wake_id)
            if opened.get("write_context_available") is not True:
                raise WorkMemoryError("brain_open_required")
            ref = opened.get("write_context_ref")
        else:
            ref = write_context_ref
        if not isinstance(ref, str) or not ref or ref != ref.strip() or ref.startswith("$"):
            raise WorkMemoryError("execution_binding_required")
        binding = self.onboarding.current_open_write_context(
            **self.identity, write_context_ref=ref, required_scope="learning_memory",
            expected_wake_id=claim.wake_id if claim is not None else None,
        )
        if binding.get("write_context_available") is not True:
            raise WorkMemoryError("write_context_binding_mismatch")
        allowed_modes = {"human_attested_direct", "ordinary_authenticated"} if claim is None else {"gateway_injected", "ordinary_authenticated"}
        if binding.get("context_mode") not in allowed_modes:
            raise WorkMemoryError("invalid_direct_context")
        if claim is not None and binding.get("wake_id") != claim.wake_id:
            raise WorkMemoryError("execution_wake_mismatch")
        if claim is None and (not isinstance(binding.get("authorized_scopes"), list) or "learning_memory" not in binding["authorized_scopes"]):
            raise WorkMemoryError("direct_scope_not_authorized")
        if not isinstance(binding.get("wake_id"), str) or type(binding.get("wake_seq")) is not int:
            raise WorkMemoryError("brain_open_required")
        return claim

    @staticmethod
    def _request_key(claim, request_id):
        if claim is not None:
            if request_id is not None:
                raise WorkMemoryError("request_id_conflicts_with_bound_execution")
            # Stable call identity only, never raw execution_ref/claim_id or
            # wake capability. Names/IDs are represented by a domain hash.
            return canonical_hash(["work-memory-request/1", "gateway", claim.owner_id, claim.model_id,
                                   claim.wake_id, claim.batch_id, claim.call_id, claim.tool_name, claim.deployment_epoch])
        if request_id is None:
            return None
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise WorkMemoryError("invalid_request_id")
        return canonical_hash(["work-memory-request/1", "direct", request_id])

    def _configured_protected(self, value):
        def contains(item, depth=0, active=None):
            if depth > 64:
                return True
            if isinstance(item, str):
                return bool(ATLAS_TOKEN.search(item)) or any(secret in item for secret in self._protected_values)
            if isinstance(item, (dict, list, tuple, set, frozenset)):
                active = set() if active is None else active
                identity = id(item)
                if identity in active:
                    return True
                active.add(identity)
                try:
                    children = (*item.keys(), *item.values()) if isinstance(item, dict) else item
                    return any(contains(child, depth + 1, active) for child in children)
                finally:
                    active.remove(identity)
            return False
        if contains(value):
            raise WorkMemoryError("credential_or_secret_detected")

    def _protected(self, value):
        self._configured_protected(value)
        if self.onboarding.contains_protected_persistence_value(**self.identity, value=value):
            raise WorkMemoryError("credential_or_secret_detected")

    def remember(self, content: str, tag: str, write_context_ref=None, request_id=None):
        try:
            self._configured_protected({"content": content, "tag": tag, "request_id": request_id})
            validate_content_tag(content, tag)
            if request_id is not None and (not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id)):
                raise WorkMemoryError("invalid_request_id")
            claim = self._binding("remember_work_memory", write_context_ref)
            key = self._request_key(claim, request_id)
            self._protected({"content": content, "tag": tag})
            self._can_write()
            return self._result(self.store.remember(**self.identity, content=content, tag=tag, request_key=key,
                                                    _write_guard=self._transaction_gate))
        except (WorkMemoryError, ExecutionBindingError) as exc:
            return self._reject(str(exc))
        except OnboardingError:
            return self._reject("work_memory_authorization_unavailable")

    def revise(self, target_ref: str, content=None, tag=None, write_context_ref=None, lifecycle=None):
        try:
            self._configured_protected({"target_ref": target_ref, "content": content, "tag": tag, "lifecycle": lifecycle})
            parse_ref(target_ref)
            validate_revision_fields(content, tag, lifecycle)
            claim = self._binding("revise_work_memory", write_context_ref)
            self._protected({"target_ref": target_ref, "content": content, "tag": tag})
            if content is None or tag is None:
                # Internal preflight needs the original even during retirement;
                # the bound write authorization and final version CAS still apply.
                previous = self.store.recall(**self.identity, target_ref=target_ref, include_retired=True)["results"][0]
                self._protected({"content": previous["content"] if content is None else content,
                                 "tag": previous["tag"] if tag is None else tag})
            self._can_write()
            return self._result(self.store.revise(**self.identity, target_ref=target_ref, content=content, tag=tag, lifecycle=lifecycle,
                                                  request_key=self._request_key(claim, None), _write_guard=self._transaction_gate))
        except (WorkMemoryError, ExecutionBindingError) as exc:
            return self._reject(str(exc))
        except OnboardingError:
            return self._reject("work_memory_authorization_unavailable")

    def recall(self, query="", target_ref="", limit=5, offset=0, include_retired=False):
        try:
            self._configured_protected({"query": query, "target_ref": target_ref, "limit": limit, "offset": offset})
            claim = self._claim("recall_work_memory")
            ordinary = current_ordinary_access(**self.identity, scope="learning_memory")
            # Authenticated ordinary reads remain available before activation.
            # The legacy path keeps its existing learning permission gate.
            if ordinary is None:
                permission = self.onboarding.authorize_other_module_write(**self.identity, module_name=LEARNING_MODULE)
                if permission.get("decision") != "allowed":
                    raise WorkMemoryError("module_one_required")
                if claim is not None and claim.tool_name != "recall_work_memory":
                    raise WorkMemoryError("execution_owner_mismatch")
            result = self.store.recall(**self.identity, query=query, target_ref=target_ref, limit=limit, offset=offset,
                                       include_retired=include_retired)
            self._protected(result)
            return self._result(result)
        except (WorkMemoryError, ExecutionBindingError) as exc:
            return self._reject(str(exc))
        except OnboardingError:
            return self._reject("work_memory_authorization_unavailable")
