"""Explicit relation operations under ordinary/legacy execution authorization."""
from contextlib import nullcontext
from copy import copy
from pathlib import Path
from types import SimpleNamespace
import json
import re

from runtime.credential_guard import contains_credential_or_secret
from runtime.execution_binding import ExecutionBindingError, ExecutionClaim, canonical_hash, current_execution_claim
from runtime.memory_relations import CONTRACT, MemoryRelationError, SCOPES, parse_memory_ref, parse_edge_ref, validate_relation
from runtime.onboarding import OnboardingError
from runtime.ordinary_access import current_ordinary_access
from runtime.work_memory import ATLAS_TOKEN
from .ordinary_access_policy import _module_one_allows_ordinary_writes

SAFE = frozenset({"invalid_memory_ref", "invalid_edge_ref", "invalid_relation_type", "invalid_relation_label",
    "fixed_relation_label_not_allowed", "self_relation_forbidden", "credential_or_secret_detected",
    "relation_storage_unavailable", "invalid_identity", "invalid_request_key", "invalid_request_id", "request_conflict",
    "relation_endpoint_unavailable", "memory_version_conflict", "relation_not_found", "edge_version_conflict",
    "reattach_requires_new_request", "invalid_relation_page", "relation_scope_not_authorized", "module_one_required",
    "execution_owner_mismatch", "execution_wake_mismatch", "execution_claim_not_current", "execution_registry_unavailable",
    "execution_binding_required", "write_context_binding_mismatch", "explicit_context_conflicts_with_bound_execution",
    "request_id_conflicts_with_bound_execution", "brain_open_required", "relation_authorization_unavailable"})


class MemoryRelationAccessService:
    def __init__(self, store, *, onboarding, owner_id, model_id, protected_values=()):
        if (Path(store.database).resolve() != Path(onboarding.database).resolve() or
                any(not isinstance(t, str) or not t or t != t.strip() for t in (owner_id, model_id)) or
                any(not isinstance(t, str) or not t for t in protected_values)):
            raise ValueError("relation_configuration_invalid")
        self.store, self.onboarding = store, onboarding
        self.owner_id, self.model_id = owner_id, model_id
        self._secrets = tuple(protected_values)

    @property
    def identity(self):
        return {"owner_id": self.owner_id, "model_id": self.model_id}

    @staticmethod
    def _reject(code):
        code = code if code in SAFE else "relation_operation_rejected"
        return {"contract_version": CONTRACT, "decision": "reject", "reason_code": code,
                "reason_codes": [code], "state_changed": False, "automatic_recall_eligible": False}

    def _protected(self, value):
        def raw_secret(item):
            if isinstance(item, str):
                return bool(ATLAS_TOKEN.search(item)) or any(secret in item for secret in self._secrets)
            if isinstance(item, dict):
                return any(raw_secret(key) or raw_secret(child) for key, child in item.items())
            if isinstance(item, (list, tuple)):
                return any(raw_secret(child) for child in item)
            return False
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
            if (len(encoded) > 1024 * 1024 or contains_credential_or_secret(value) or raw_secret(value) or
                    self.onboarding.contains_protected_persistence_value(**self.identity, value=value)):
                raise MemoryRelationError("credential_or_secret_detected")
        except (TypeError, ValueError, RecursionError, UnicodeError):
            raise MemoryRelationError("credential_or_secret_detected") from None

    def _claim(self, tool):
        claim = current_execution_claim()
        if claim is not None and (not isinstance(claim, ExecutionClaim) or
                (claim.owner_id, claim.model_id) != (self.owner_id, self.model_id) or claim.tool_name != tool or
                Path(claim.database).resolve() != Path(self.store.database).resolve()):
            raise MemoryRelationError("execution_owner_mismatch")
        return claim

    def _gate(self, connection=None):
        adapter = self.onboarding if connection is None else SimpleNamespace(_connect=lambda: nullcontext(connection))
        if not _module_one_allows_ordinary_writes(adapter, **self.identity):
            raise MemoryRelationError("module_one_required")

    def _binding(self, tool, endpoint_scopes, write_context_ref):
        claim = self._claim(tool); self._gate()
        ordinary = current_ordinary_access(**self.identity, scope="memory_relations")
        if ordinary is not None:
            if write_context_ref is not None and write_context_ref != ordinary["write_context_ref"]:
                raise MemoryRelationError("write_context_binding_mismatch")
            if claim is not None and ordinary["wake_id"] != claim.wake_id:
                raise MemoryRelationError("execution_wake_mismatch")
            def guard(db):
                self._gate(db)
                current = current_ordinary_access(**self.identity, scope="memory_relations")
                if current != ordinary or self._claim(tool) != claim:
                    raise MemoryRelationError("write_context_binding_mismatch")
            return claim, guard
        if claim is not None:
            if write_context_ref is not None:
                raise MemoryRelationError("explicit_context_conflicts_with_bound_execution")
            opened = self.onboarding.open_brain_context(**self.identity, present_details=False, expected_wake_id=claim.wake_id)
            write_context_ref = opened.get("write_context_ref") if opened.get("write_context_available") else None
        if not isinstance(write_context_ref, str) or not write_context_ref or write_context_ref.startswith("$"):
            raise MemoryRelationError("execution_binding_required")
        self._legacy_scopes(self.onboarding, claim, endpoint_scopes, write_context_ref)

        def guard(db):
            self._gate(db)
            if self._claim(tool) != claim:
                raise MemoryRelationError("execution_owner_mismatch")
            # Reuse the authoritative legacy gate in the SAME locked transaction.
            # No shared object is patched, no second connection is opened, and no
            # state is initialized here: activation already proves the state exists.
            view = copy(self.onboarding)
            view._connect = lambda: nullcontext(db)
            def existing_state(**identity):
                if view._state_row(db, identity["owner_id"], identity["model_id"]) is None:
                    raise MemoryRelationError("relation_authorization_unavailable")
            view.ensure_state = existing_state
            self._legacy_scopes(view, claim, endpoint_scopes, write_context_ref)
        return claim, guard

    def _legacy_scopes(self, onboarding, claim, endpoint_scopes, write_context_ref):
        # A legacy limited grant must cover both endpoints and the new operation;
        # never treat emotional/learning permission as permission for the other.
        for scope in {*endpoint_scopes, "memory_relations"}:
            bound = onboarding.current_open_write_context(**self.identity, write_context_ref=write_context_ref,
                required_scope=scope, expected_wake_id=claim.wake_id if claim else None)
            if bound.get("write_context_available") is not True:
                raise MemoryRelationError("relation_scope_not_authorized")
            if claim is None and (bound.get("context_mode") != "human_attested_direct" or scope not in bound.get("authorized_scopes", [])):
                raise MemoryRelationError("relation_scope_not_authorized")
            if claim is not None and (bound.get("context_mode") != "gateway_injected" or bound.get("wake_id") != claim.wake_id):
                raise MemoryRelationError("execution_wake_mismatch")

    @staticmethod
    def _key(claim, request_id):
        if claim is not None:
            if request_id is not None:
                raise MemoryRelationError("request_id_conflicts_with_bound_execution")
            return canonical_hash([CONTRACT, "gateway", claim.owner_id, claim.model_id, claim.wake_id,
                                   claim.batch_id, claim.call_id, claim.tool_name, claim.deployment_epoch])
        if request_id is None:
            return None
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise MemoryRelationError("invalid_request_id")
        return canonical_hash([CONTRACT, "direct", request_id])

    def attach(self, from_ref, to_ref, type, label=None, reverse_label=None, request_id=None, write_context_ref=None):
        try:
            self._protected([from_ref, to_ref, type, label, reverse_label, request_id])
            first, second, *_ = validate_relation(from_ref, to_ref, type, label, reverse_label)
            claim, guard = self._binding("attach_memory_relation", {SCOPES[first[0]], SCOPES[second[0]]}, write_context_ref)
            result = self.store.attach(**self.identity, from_ref=from_ref, to_ref=to_ref, type=type, label=label,
                reverse_label=reverse_label, request_key=self._key(claim, request_id), _write_guard=guard)
            self._protected(result)
            return {"contract_version": CONTRACT, **result}
        except (MemoryRelationError, ExecutionBindingError) as exc:
            return self._reject(str(exc))
        except OnboardingError:
            return self._reject("relation_authorization_unavailable")

    def detach(self, edge_ref, request_id=None, write_context_ref=None):
        try:
            self._protected([edge_ref, request_id]); parse_edge_ref(edge_ref)
            self._claim("detach_memory_relation")
            scopes = self.store.relation_scopes(**self.identity, edge_ref=edge_ref)
            claim, guard = self._binding("detach_memory_relation", scopes, write_context_ref)
            result = self.store.detach(**self.identity, edge_ref=edge_ref, request_key=self._key(claim, request_id), _write_guard=guard)
            self._protected(result)
            return {"contract_version": CONTRACT, **result}
        except (MemoryRelationError, ExecutionBindingError) as exc:
            return self._reject(str(exc))
        except OnboardingError:
            return self._reject("relation_authorization_unavailable")

    def read(self, target_ref, limit=30, offset=0):
        try:
            self._protected([target_ref, limit, offset]); parse_memory_ref(target_ref)
            claim = self._claim("read_memory_relations")
            ordinary = current_ordinary_access(**self.identity, scope="memory_relations")
            if ordinary is None:
                permission = self.onboarding.authorize_other_module_write(**self.identity, module_name="memory_relations")
                if permission.get("decision") != "allowed":
                    raise MemoryRelationError("module_one_required")
            # This facade has a server-fixed owner/model, never client identity.
            # Relations read no bodies and do not inherit a write-grant capability.
            result = self.store.read(**self.identity, target_ref=target_ref, limit=limit, offset=offset)
            self._protected(result)
            return {"contract_version": CONTRACT, **result}
        except (MemoryRelationError, ExecutionBindingError) as exc:
            return self._reject(str(exc))
        except OnboardingError:
            return self._reject("relation_authorization_unavailable")
