"""One authenticated ordinary-memory operation, independent of self revision.

Only the MCP dispatcher enters this context, after transport authentication and,
for gateway calls, execution-claim validation. It grants no self-model/vault or
external tool authority and never creates/replaces an injected wake.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import itertools
import time
import uuid

ORDINARY_SCOPES = frozenset({
    'emotional_memory', 'learning_memory', 'tool_guidance', 'planning_memory',
    'self_governance', 'injection_control', 'shared_person_authoring',
})
ORDINARY_TOOLS = {
    **dict.fromkeys(('remember_emotional_memory', 'revise_emotional_memory',
                    'integrate_emotional_memories', 'manage_brain_pin',
                    'veto_ephemeral_memory', 'recall_emotional_memory'), 'emotional_memory'),
    **dict.fromkeys(('remember_learning_memory', 'remember_learning_contrast_pair',
                    'revise_learning_memory', 'integrate_learning_memories',
                    'review_learning_change', 'recall_learning_memory',
                    'preview_learning_recall'), 'learning_memory'),
    **dict.fromkeys(('remember_tool_guidance', 'revise_tool_guidance',
                    'review_tool_guidance_candidate', 'record_tool_experience',
                    'recall_tool_guidance'), 'tool_guidance'),
    **dict.fromkeys(('remember_planning_memory', 'record_planning_event',
                    'revise_planning_memory', 'review_planning_change',
                    'advance_plan', 'recall_planning_memory'), 'planning_memory'),
    'manage_self_governance_profile': 'self_governance',
    'query_self_governance_profile': 'self_governance',
    'manage_injection_control': 'injection_control',
    'query_injection_control': 'injection_control',
    **dict.fromkeys(('preview_person_reference_rewrite', 'confirm_person_reference_rewrite',
                    'manage_person_reference_advisory'),
                   'shared_person_authoring'),
}
# This allowlist describes operations that do not author ordinary-memory
# content or receipts. Rewrite previews persist authoring receipts, so they
# deliberately remain writes. New ordinary tools default to write protection.
ORDINARY_READ_TOOLS = frozenset({
    'recall_emotional_memory', 'recall_learning_memory', 'preview_learning_recall',
    'recall_tool_guidance', 'recall_planning_memory',
    'query_self_governance_profile', 'query_injection_control',
})
MODULE_SCOPES = {
    'emotional_memory_module_two': 'emotional_memory',
    'learning_memory_module_three': 'learning_memory',
    'tool_guidance_module_four': 'tool_guidance',
    'tool_guidance_module': 'tool_guidance',
    'planning_memory_module_five': 'planning_memory',
    'self_governance': 'self_governance',
    'self_governance_profile': 'self_governance',
    'injection_control': 'injection_control',
    'shared_person_authoring': 'shared_person_authoring',
}
_CURRENT = ContextVar('st_authenticated_ordinary_operation', default=None)
_SEQUENCE = itertools.count(time.time_ns() // 1000000)

def tool_scope(name, arguments):
    if name == 'manage_injection_control' and arguments.get('scope') == 'hallucination_vault':
        return None
    if name in {'manage_self_governance_profile', 'manage_injection_control'}:
        action = arguments.get('action', '')
        if action in {'propose_set', 'propose_clear', 'propose_mode', 'propose_rollback', 'activate', 'withdraw'}:
            return None
    if name == 'remember_memory':
        value = arguments.get('module')
        return value if value in {'emotional_memory', 'learning_memory', 'planning_memory'} else None
    if name == 'revise_memory':
        ref = arguments.get('target_ref')
        if isinstance(ref, str):
            return {'emotion': 'emotional_memory', 'learning': 'learning_memory',
                    'plan': 'planning_memory', 'tool-card': 'tool_guidance'}.get(ref.split('://', 1)[0])
        return None
    return ORDINARY_TOOLS.get(name)

def ordinary_write_scope(name, arguments):
    """Classify ordinary authorship even when it retains legacy execution rules.

    Governance/injection advanced actions bypass the convenience context, not
    the initial module-one prerequisite. The vault keeps its separate policy.
    """
    if name in ORDINARY_READ_TOOLS:
        return None
    if name == 'manage_injection_control' and arguments.get('scope') == 'hallucination_vault':
        return None
    if name in {'manage_self_governance_profile', 'manage_injection_control'}:
        return ORDINARY_TOOLS[name]
    return tool_scope(name, arguments)

def current_ordinary_access(*, owner_id, model_id, scope=None):
    value = _CURRENT.get()
    if (value is None or value['owner_id'] != owner_id or value['model_id'] != model_id
            or (scope is not None and scope not in value['authorized_scopes'])):
        return None
    return {**value, 'authorized_scopes': list(value['authorized_scopes'])}

@contextmanager
def authenticated_ordinary_operation(*, owner_id, model_id, scope, claim=None):
    if scope not in ORDINARY_SCOPES or not owner_id or not model_id:
        raise ValueError('ordinary_scope_invalid')
    if claim is not None and (claim.owner_id, claim.model_id) != (owner_id, model_id):
        raise ValueError('ordinary_identity_mismatch')
    operation_id = 'ordinaryop_' + uuid.uuid4().hex
    value = {
        'owner_id': owner_id, 'model_id': model_id,
        'write_context_available': True,
        'write_context_ref': 'ordinaryctx_' + uuid.uuid4().hex,
        'context_mode': 'ordinary_authenticated',
        'authorized_scopes': [scope],
        'wake_id': claim.wake_id if claim is not None else operation_id,
        'wake_seq': next(_SEQUENCE), 'operation_id': operation_id,
        'operation_source': 'gateway_execution' if claim is not None else 'authenticated_mcp',
        'injected_wake_created': False,
    }
    token = _CURRENT.set(value)
    try:
        yield current_ordinary_access(owner_id=owner_id, model_id=model_id)
    finally:
        _CURRENT.reset(token)
