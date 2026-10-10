from contextlib import nullcontext
from copy import copy
from pathlib import Path
from types import SimpleNamespace

from runtime.credential_guard import contains_credential_or_secret
from runtime.emotion_layer import EmotionLayerError, GUIDE, SCOPES, parse_ref, pending_emotion_tags
from runtime.execution_binding import assert_bound_execution, current_execution_claim, ExecutionBindingError
from runtime.ordinary_access import current_ordinary_access
from runtime.onboarding import OnboardingError
from runtime.daily_memory_input import DailyMemoryInputError, resolve_daily_memory_module
from .ordinary_access_policy import _module_one_allows_ordinary_writes


class EmotionLayerAccessService:
    def __init__(self, store, *, onboarding, owner_id, model_id, protected_values=()):
        if Path(store.database).resolve() != Path(onboarding.database).resolve():
            raise ValueError("emotion_database_mismatch")
        self.store, self.onboarding = store, onboarding
        self.identity = (owner_id, model_id)
        self.protected_values = tuple(protected_values)

    def _protected(self, value):
        import json
        serialized = json.dumps(value, ensure_ascii=False)
        if (contains_credential_or_secret(value) or any(secret in serialized for secret in self.protected_values)
                or self.onboarding.contains_protected_persistence_value(owner_id=self.identity[0], model_id=self.identity[1], value=value)):
            raise EmotionLayerError("credential_or_secret_detected")

    def _guard(self, scope, tool, write_context_ref=None):
        owner_id, model_id = self.identity
        claim = current_execution_claim()
        if claim is not None and ((claim.owner_id, claim.model_id) != self.identity or claim.tool_name != tool
                or Path(claim.database).resolve() != Path(self.store.database).resolve()):
            raise EmotionLayerError("emotion_identity_mismatch")
        access = current_ordinary_access(owner_id=owner_id, model_id=model_id, scope=scope)
        if access is None and claim is not None:
            opened = self.onboarding.open_brain_context(owner_id=owner_id, model_id=model_id,
                present_details=False, expected_wake_id=claim.wake_id)
            write_context_ref = opened.get("write_context_ref")
        if access is None and not write_context_ref:
            raise EmotionLayerError("emotion_authorization_required")

        def guard(connection):
            adapter = SimpleNamespace(_connect=lambda: nullcontext(connection))
            if not _module_one_allows_ordinary_writes(adapter, owner_id=owner_id, model_id=model_id):
                raise EmotionLayerError("module_one_required")
            if current_execution_claim() != claim:
                raise EmotionLayerError("emotion_identity_mismatch")
            assert_bound_execution(connection)
            if access is not None:
                if current_ordinary_access(owner_id=owner_id, model_id=model_id, scope=scope) != access:
                    raise EmotionLayerError("emotion_authorization_required")
            else:
                view = copy(self.onboarding)
                view._connect = lambda: nullcontext(connection)
                view.ensure_state = lambda **kwargs: None
                binding = view.current_open_write_context(owner_id=owner_id, model_id=model_id,
                    write_context_ref=write_context_ref, required_scope=scope,
                    expected_wake_id=claim.wake_id if claim else None)
                if binding.get("write_context_available") is not True:
                    raise EmotionLayerError("emotion_authorization_required")
                if claim is None and (binding.get("context_mode") != "human_attested_direct" or scope not in binding.get("authorized_scopes", [])):
                    raise EmotionLayerError("emotion_authorization_required")
        return guard

    @staticmethod
    def _reject(error):
        code = str(error)
        if not code.replace("_", "").isalnum() or len(code) > 80:
            code = "emotion_operation_rejected"
        return {"decision": "reject", "reason_code": code, "state_changed": False, "guide": GUIDE}

    def list(self, write_context_ref=None):
        try:
            return self.store.list(identity=self.identity, guard=self._guard("emotion_layer", "emotion_list", write_context_ref))
        except (EmotionLayerError, ExecutionBindingError, OnboardingError) as error:
            return self._reject(error)

    def manage(self, action, write_context_ref=None, **fields):
        try:
            self._protected(fields)
            return self.store.manage(identity=self.identity, guard=self._guard("emotion_layer", "emotion_manage", write_context_ref), action=action, **fields)
        except (EmotionLayerError, ExecutionBindingError, OnboardingError) as error:
            return self._reject(error)

    def tag(self, memory_ref, emotions, action="add", write_context_ref=None):
        try:
            self._protected([memory_ref, emotions])
            module, _, _ = parse_ref(memory_ref)
            return self.store.tag(identity=self.identity, guard=self._guard(SCOPES[module], "memory_emotions", write_context_ref), memory_ref=memory_ref, emotions=emotions, action=action)
        except (EmotionLayerError, ExecutionBindingError, OnboardingError) as error:
            return self._reject(error)

    def remember(self, save, fields, emotions=None):
        if emotions is None:
            with pending_emotion_tags(self.store, self.identity, None, None):
                result = save(**fields)
        else:
            try:
                self._protected(emotions)
                scope = resolve_daily_memory_module(fields)
                guard = self._guard(scope, "remember_memory", fields.get("write_context_ref"))
                with pending_emotion_tags(self.store, self.identity, emotions, guard) as pending:
                    result = save(**fields)
                # A replay returns an already-committed body, bypassing its create
                # hook. Tag its current version explicitly; never claim success
                # merely because an old body was returned.
                if result.get("stored") is True and not pending["attached"]:
                    self.store.tag(identity=self.identity, guard=guard, memory_ref=result["ref"], emotions=emotions)
            except (EmotionLayerError, ExecutionBindingError, OnboardingError, DailyMemoryInputError) as error:
                return self._reject(error)
        if result.get("stored") is True:
            try:
                scope = result["module"]
                options = self.store.list(identity=self.identity, guard=self._guard(scope, "remember_memory", fields.get("write_context_ref")))
                result["emotion_options"] = [item["name"] for item in options["emotions"] if item["state"] == "active"]
                result["emotion_guidance"] = GUIDE
                result["emotions_attached"] = emotions is not None
            except (EmotionLayerError, ExecutionBindingError, OnboardingError):
                result["emotion_guidance"] = GUIDE
        return result

    def remember_emotional(self, save, fields, emotions=None):
        """Advanced policy-bearing save uses the same atomic runtime hook."""
        try:
            guard = self._guard("emotional_memory", "remember_emotional_memory", fields.get("write_context_ref")) if emotions is not None else None
            self._protected(emotions)
            with pending_emotion_tags(self.store, self.identity, emotions, guard) as pending:
                result = save(**fields)
            if result.get("decision") == "stored":
                if emotions is not None and not pending["attached"]:
                    # A consumed rewrite receipt returns the prior body without
                    # visiting the create hook. Validate/tag that exact version
                    # under the current write authority; stale receipts reject.
                    memory = result["memory"]
                    ref = f"emotion://{memory['memory_id']}@{memory['current_version']}"
                    self.store.tag(identity=self.identity, guard=guard, memory_ref=ref, emotions=emotions)
                result["emotion_guidance"] = GUIDE
                result["emotions_attached"] = emotions is not None
                try:
                    options = self.store.list(identity=self.identity, guard=guard or self._guard("emotional_memory", "remember_emotional_memory", fields.get("write_context_ref")))
                    result["emotion_options"] = [item["name"] for item in options["emotions"] if item["state"] == "active"]
                except (EmotionLayerError, ExecutionBindingError, OnboardingError):
                    pass  # Receipt stays truthful if optional guidance cannot load.
            return result
        except (EmotionLayerError, ExecutionBindingError, OnboardingError) as error:
            return self._reject(error)
