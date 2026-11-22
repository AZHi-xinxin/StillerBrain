"""Use the existing authenticated AI author guard; no human write endpoint."""
from runtime.emotion_layer import EmotionLayerError
from runtime.execution_binding import ExecutionBindingError
from runtime.onboarding import OnboardingError
from runtime.force_recall import GUIDE, SCOPES, _parse
from .emotion_layer_service import EmotionLayerAccessService


class ForceRecallAccessService(EmotionLayerAccessService):
    def manage(self, action="list", target_ref=None, expected_revision=None, write_context_ref=None):
        if action == "guide":
            return {"guide": GUIDE, "state_changed": False}
        try:
            self._protected(target_ref)
            scope = SCOPES[_parse(target_ref)[0]] if target_ref is not None else "emotional_memory"
            result = self.store.manage(identity=self.identity,
                guard=self._guard(scope, "memory_force_recall", write_context_ref),
                action=action, target_ref=target_ref, expected_revision=expected_revision)
            self._protected(result)
            return result
        except (EmotionLayerError, ExecutionBindingError, OnboardingError) as error:
            code = str(error)
            if not code.replace("_", "").isalnum() or len(code) > 80:
                code = "force_recall_rejected"
            return {"decision": "reject", "reason_code": code, "state_changed": False, "guide": GUIDE}
