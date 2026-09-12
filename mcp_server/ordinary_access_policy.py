"""Opt-in simple-memory dispatcher; no weakening of self/vault execution."""
from runtime.execution_binding import current_execution_claim
from runtime.ordinary_access import authenticated_ordinary_operation, ordinary_write_scope, tool_scope
from pydantic import ValidationError
from mcp.server.fastmcp.exceptions import ToolError
from difflib import get_close_matches
from .keyword_arguments import KeywordCredentialError, normalize_keyword_arguments

VERSION_FIELDS = {
    'emotional_memory': ('expected_emotion_version', 'expected_emotion_row_version'),
    'learning_memory': ('expected_learning_version', 'expected_learning_row_version'),
    'tool_guidance': ('expected_tool_version', 'expected_tool_row_version'),
    'planning_memory': ('expected_planning_version', 'expected_planning_row_version'),
    'self_governance': ('expected_profile_version',),
    'injection_control': ('expected_control_version',),
    'shared_person_authoring': ('expected_authoring_version',),
}

def _module_one_allows_ordinary_writes(onboarding, *, owner_id, model_id):
    """Read the actual activation/unlock records without initializing state.

    A UI flag, candidate, imported legacy pointer, or caller-provided property
    cannot establish this prerequisite. Subsequent self-revision deliberation
    may keep using the already activated revision; no current stage is assumed.
    """
    try:
        with onboarding._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM brain_module_unlocks u "
                "JOIN self_models m ON m.owner_id=u.owner_id AND m.model_id=u.model_id "
                "JOIN brain_onboarding_state s ON s.owner_id=u.owner_id AND s.model_id=u.model_id "
                "JOIN self_model_revisions r ON r.model_id=m.model_id "
                "AND r.revision_id=m.active_revision_id "
                "WHERE u.owner_id=? AND u.model_id=? AND u.module_name='module_one' "
                "AND u.unlocked=1 AND u.basis_revision_id=r.revision_id "
                "AND s.base_revision_id=r.revision_id AND s.module_one_status='complete' "
                "AND s.injection_policy='normal' "
                "AND (s.stage='live' OR (s.flow_kind='edit' AND s.stage IN "
                "('edit_consent','edit_body_draft','candidate_wait','candidate_review'))) "
                "AND r.author='ai' AND r.activation_checkpoint_id IS NOT NULL",
                (owner_id, model_id),
            ).fetchone() is not None
    except Exception:
        # Registry/activation evidence must be available; exception text may
        # contain private implementation details and never crosses this gate.
        return False

def install_ordinary_access_policy(mcp, *, onboarding, owner_id, model_id, services):
    manager = mcp._tool_manager
    original = manager.call_tool
    if getattr(manager, '_ordinary_policy_installed', False):
        raise RuntimeError('ordinary_policy_already_installed')
    manager._ordinary_policy_installed = True

    async def ordinary(name, arguments, context=None, convert_result=False):
        tool = manager.get_tool(name)
        if tool is None:
            matches = get_close_matches(name, list(manager._tools), n=3, cutoff=0.4)
            raise ToolError('工具名不在当前 ST 目录中。请刷新 MCP 工具列表，按当前名称调用。可能相关：' + ', '.join(matches))
        if (ordinary_write_scope(name, arguments) is not None
                and not _module_one_allows_ordinary_writes(onboarding, owner_id=owner_id, model_id=model_id)):
            result = {
                'decision': 'reject', 'reason_code': 'module_one_required',
                'reason_codes': ['module_one_required'], 'state_changed': False,
                'message': '模块一完成设置并激活前，其他普通模块保持只读。',
                'next_action': '请先完成模块一的设置、独立复核与激活；激活后普通模块恢复新增和修改。',
            }
            return tool.fn_metadata.convert_result(result) if convert_result else result
        scope = tool_scope(name, arguments)
        if scope is None:
            return await original(name, arguments, context=context, convert_result=convert_result)
        args = dict(arguments)
        claim = current_execution_claim()
        with authenticated_ordinary_operation(owner_id=owner_id, model_id=model_id,
                                               scope=scope, claim=claim) as access:
            fields = tool.fn_metadata.arg_model.model_fields
            # The outer guard has already checked the original signed payload.
            # Compatibility changes only our copy, after the activation gate.
            try:
                args, keyword_errors = normalize_keyword_arguments(name, args, fields)
            except KeywordCredentialError:
                result = {
                    'decision': 'reject', 'reason_code': 'credential_or_secret_detected',
                    'reason_codes': ['credential_or_secret_detected'], 'state_changed': False,
                    'message': '关键词中检测到凭据内容，本次未写入。',
                    'next_action': '关键词请使用主题短语；凭据仅保留存放位置或变量名。',
                }
                return tool.fn_metadata.convert_result(result) if convert_result else result
            # Keep the signed gateway payload unchanged until the outer guard
            # has claimed it. Only then fill internal bookkeeping parameters.
            if 'write_context_ref' in fields and name not in {'remember_memory','revise_memory','advance_plan'}:
                args['write_context_ref'] = access['write_context_ref']
            component = services.get(scope)
            if component is not None:
                # One coherent scope snapshot supplies both halves of a CAS.
                # The store still compares this snapshot in its write transaction;
                # a real intervening writer must be rejected, never overwritten.
                state = None
                def current_state():
                    nonlocal state
                    if state is None:
                        state = component.status()
                        if scope in {'self_governance', 'injection_control'}:
                            state = state.get('scopes', {}).get(args.get('scope'), {})
                    return state
                for field in VERSION_FIELDS.get(scope, ()):
                    if field in fields and field not in args:
                        snapshot = current_state()
                        if 'row_version' in snapshot:
                            args[field] = snapshot['row_version']
                if (scope == 'self_governance'
                        and args.get('action') in {'set', 'clear', 'rollback'}
                        and 'expected_active_revision' not in args
                        and 'expected_active_revision' in fields):
                    # None supplied explicitly remains a deliberate "no active
                    # revision" assertion for old clients. Omission means that
                    # the host owns this bookkeeping, not that the scope is empty.
                    snapshot = current_state()
                    if 'active_revision_id' in snapshot:
                        args['expected_active_revision'] = snapshot['active_revision_id']
            field_errors = list(keyword_errors)
            try:
                tool.fn_metadata.arg_model.model_validate(args)
            except ValidationError as exc:
                for error in exc.errors(include_input=False, include_url=False)[:8]:
                    safe_error = {
                        'field': error['loc'][0] if error['loc'] and error['loc'][0] in fields else 'unrecognized_field',
                        'issue': error['type'],
                    }
                    if safe_error not in keyword_errors:
                        field_errors.append(safe_error)
            if field_errors:
                result = {
                    'decision': 'reject', 'reason_code': 'invalid_tool_arguments',
                    'state_changed': False,
                    'message': '请按这个工具当前公布的字段填写；不同工具的参数分别对应自己的用途。',
                    'allowed_fields': sorted(fields),
                    'required_fields': tool.parameters.get('required', []),
                    # No values, arbitrary key names, or model-produced error prose.
                    'field_errors': field_errors[:8],
                }
                if any(error in ({'field': 'keywords', 'issue': 'list_type'},
                                 {'field': 'changes', 'issue': 'keywords_format'})
                       for error in result['field_errors']):
                    # Canonical type and fixed guidance; never echo bad input.
                    keyword_schema = tool.parameters.get('properties', {}).get('keywords', {})
                    nullable = any(branch.get('type') == 'null'
                                   for branch in keyword_schema.get('anyOf', []))
                    may_omit = bool(keyword_schema) and (
                        'keywords' not in tool.parameters.get('required', []))
                    result['expected_type'] = 'array<string> | null' if nullable else 'array<string>'
                    result['example'] = ['标签一', '标签二']
                    result['next_action'] = (
                        'keywords 请填写 JSON 字符串数组，例如 ["标签一", "标签二"]；'
                        '直连 MCP 接收端也兼容单个短语、逗号/顿号/分号/换行分隔文本或合法 JSON 数组字符串。'
                        '网关调用请使用真实数组。'
                        '每项须为非空文字；含分隔标点的完整关键词请放在真实数组内。'
                        '需要清空时可填写 []。'
                        + ('也可省略 keywords。' if may_omit else '')
                        + ('本工具也允许填写 null。' if nullable else '')
                    )
                if name in {'advance_plan', 'record_planning_event'}:
                    result['next_action'] = ('advance_plan 使用 target_ref/expected_event_seq/event_type/note；'
                                             'record_planning_event 使用自己的 plan_id 等字段。请重新读取计划和当前 schema。')
                return tool.fn_metadata.convert_result(result) if convert_result else result
            return await original(name, args, context=context, convert_result=convert_result)
    manager.call_tool = ordinary

    for name in manager._tools:
        tool = manager.get_tool(name)
        # Generic tools already publish optional binding; dedicated tools gain
        # the same convenience without changing the low-level legacy API.
        example = {'module': 'emotional_memory', 'target_ref': 'emotion://x@1'}
        scope = tool_scope(name, example)
        if scope is None:
            continue
        internal = {'write_context_ref', *VERSION_FIELDS.get(scope, ())}
        tool.parameters['required'] = [key for key in tool.parameters.get('required', [])
                                       if key not in internal]
        for key in internal & tool.parameters.get('properties', {}).keys():
            tool.parameters['properties'][key]['description'] = '可省略；服务按当前已认证操作补入内部绑定。'
        if name == 'manage_self_governance_profile':
            for key in ('expected_profile_version', 'expected_active_revision'):
                if key not in tool.parameters.get('properties', {}):
                    continue
                tool.parameters['properties'][key]['description'] = (
                    '宿主管理字段：普通 set/clear/rollback 省略，服务自动读取同一范围的最新状态并防止并发覆盖。'
                    '旧 propose/activate 等候选动作按 current_action_contract 保留原绑定。'
                    '显式填写时将严格检查；不要猜版本或用 null 表示“自动”。'
                )
        if 'keywords' in tool.parameters.get('properties', {}):
            tool.parameters['properties']['keywords']['description'] = (
                '优先填写字符串数组，例如 ["学习", "经验"]。'
                '直连 MCP 接收端兼容单个短语、逗号/顿号/分号/换行分隔文本及合法 JSON 数组字符串；'
                '网关调用请使用真实数组。'
                '数组元素内部的标点保留，数量和长度按本工具原有限额。'
            )
