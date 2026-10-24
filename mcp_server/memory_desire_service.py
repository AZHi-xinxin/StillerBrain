"""Desires inherit emotional-memory authorization and transaction guards."""
from runtime.emotion_layer import EmotionLayerError
from runtime.memory_desires import DesireError, GUIDE, pending_desire, validate_desire
from runtime.memory_relations import MemoryRelationError
from runtime.execution_binding import ExecutionBindingError
from runtime.onboarding import OnboardingError
from runtime.daily_memory_input import resolve_daily_memory_module, DailyMemoryInputError
from runtime.ordinary_access import current_ordinary_access
from .emotion_layer_service import EmotionLayerAccessService

class MemoryDesireAccessService(EmotionLayerAccessService):
    def _guard(self, scope, tool, write_context_ref=None):
        access=current_ordinary_access(owner_id=self.identity[0],model_id=self.identity[1],scope=scope)
        if access is not None and write_context_ref is not None and write_context_ref!=access['write_context_ref']:
            raise DesireError('write_context_binding_mismatch')
        return super()._guard(scope,tool,write_context_ref)

    @staticmethod
    def _reject(error):
        code=str(error)
        if not code.replace('_','').isalnum() or len(code)>80: code='desire_operation_rejected'
        return {'decision':'reject','reason_code':code,'state_changed':False,'guide':GUIDE}

    def manage(self, action, memory_ref=None, text=None, expected_revision=None, offset=0, limit=20, write_context_ref=None):
        if action=='guide': return {'guide':GUIDE,'state_changed':False}
        try:
            self._protected([memory_ref,text])
            guard=self._guard('emotional_memory','memory_desires',write_context_ref)
            result=self.store.manage(identity=self.identity,guard=guard,action=action,memory_ref=memory_ref,text=text,
                expected_revision=expected_revision,offset=offset,limit=limit)
            self._protected(result)
            return result
        except (EmotionLayerError,MemoryRelationError,ExecutionBindingError,OnboardingError) as error:
            return self._reject(error)

    def remember(self, save, fields, desire=None, tool='remember_memory'):
        """The optional note commits in the same SQL transaction as its parent."""
        if desire is None or desire=='': return save()
        try:
            text=validate_desire(desire); self._protected(text)
            if tool=='remember_memory' and resolve_daily_memory_module(fields)!='emotional_memory':
                raise DesireError('desire_requires_emotional_memory')
            guard=self._guard('emotional_memory',tool,fields.get('write_context_ref'))
            with pending_desire(self.store,self.identity,text,guard) as status: result=save()
            if result.get('stored') or result.get('decision')=='stored':
                result['desire_attached']=status['attached']
                result['desire_guidance']=GUIDE
                if not status['attached']: result['desire_notice']='本次返回既有记忆回执，没有重写欲望；可用 memory_desires read 核对或 put 补充。'
            return result
        except (EmotionLayerError,MemoryRelationError,ExecutionBindingError,OnboardingError,DailyMemoryInputError) as error:
            return self._reject(error)
