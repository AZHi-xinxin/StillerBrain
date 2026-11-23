"""Authenticated ordinary-memory organization, with no extra author workflow."""
from runtime.memory_consolidation import MemoryConsolidationStore
from runtime.ordinary_access import current_ordinary_access

GUIDE = {
    'discover': 'stbrain_manage(action="memory_timelines", arguments={"query":"主题","include_content":true})；query可省略查看各主题，refs可精确查看已归档来源。',
    'merge': 'stbrain_manage(action="merge_memories", arguments={"source_refs":["引用1","引用2"],"summary":"你重新写的摘要"})。四普通脑各自支持整理，跨脑记录保留关联。',
    'authoring': '摘要由AI重写，没有人称、模板、审核表、隔轮或来源独立次数要求。可选title、content、fields修改其他作者字段；content省略时按来源时间串联原文。',
    'automatic': '关键词去重合并，计划绑定和其他关系转到新记忆；来源归档不自动浮现，精确原文可查；已整理记录再合并时展开原始时间线。',
    'ordinary': '结果就是普通记忆；使用revise_memory自由修改，浮现规则与同脑普通记忆一致。写入正文上限100万字、摘要6.4万字，关键词不限制数量和长度；浮现预算保持原设置。',
    'timeline': '按记录日期排列，time_basis说明是情感来源时间source_timestamp、计划开始时间start_at、知识截至时间valid_as_of，还是存入时间created_at_fallback；不把它们混称为事情发生时间。分组为候选，AI决定哪些相关或重复，四条以上提醒整理，不强制合并。',
    'backfill_dates': '最近存入列表始终按created_at倒序，补录的旧事会出现在最近存入；只改正文日期不影响排序。情感source_timestamp可由AI在remember_emotional_memory填写，或用revise_memory的changes.source_timestamp修改；省略时原机制默认记录当时。补录需显式填原事发生时间，修改该字段不会改created_at。其他脑按time_basis解释日期，未知时保留未知，不编造具体时刻。',
}

class MemoryConsolidationAccessService:
    def __init__(self, stores, *, owner_id, model_id, onboarding=None, protected_values=()):
        self.store = MemoryConsolidationStore(stores)
        self.owner_id, self.model_id = owner_id, model_id
        self.onboarding = onboarding
        self.protected_values = tuple(x for x in protected_values if x)

    def call(self, action, **arguments):
        from runtime.credential_guard import contains_credential_or_secret
        from runtime.memory_consolidation import encode
        if contains_credential_or_secret(arguments) or any(x in encode(arguments) for x in self.protected_values):
            return {'decision':'reject','reason_code':'credential_or_secret_detected','state_changed':False}
        if action == 'merge':
            access = current_ordinary_access(owner_id=self.owner_id,model_id=self.model_id,scope='memory_consolidation')
            if access is None:
                from runtime.execution_binding import current_execution_claim
                from runtime.ordinary_access import authenticated_ordinary_operation
                from runtime.memory_consolidation import identity, MODULES
                claim = current_execution_claim()
                if claim is not None and (claim.owner_id,claim.model_id) == (self.owner_id,self.model_id):
                    modules = {identity(ref)[0] for ref in arguments.get('source_refs',[])}
                    for module in modules:
                        names={'emotion':'emotional_memory_module_two','learning':'learning_memory_module_three','plan':'planning_memory_module_five','tool-card':'tool_guidance_module'}
                        allowed=self.onboarding.authorize_other_module_write(owner_id=self.owner_id,model_id=self.model_id,module_name=names[module])
                        if allowed.get('decision') != 'allowed':
                            return {'decision':'reject','reason_code':'module_one_required','state_changed':False}
                    with authenticated_ordinary_operation(owner_id=self.owner_id,model_id=self.model_id,scope='memory_consolidation',claim=claim):
                        return self.call(action,**arguments)
                return {'decision':'reject','reason_code':'authenticated_ordinary_operation_required','state_changed':False}
            arguments['access'] = access
        try:
            result = getattr(self.store, action)(owner_id=self.owner_id,model_id=self.model_id,**arguments)
        except ValueError as exc:
            # Runtime errors use fixed codes, not caller content.
            return {'decision':'reject','reason_code':str(exc),'state_changed':False}
        return result
