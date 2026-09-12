"""Public ordinary author-field contracts, derived from the module schemas.

The outer schema presents a union for property-oriented clients. Exact target
branches retain each module's field set and constraints. Runtime authorization,
target CAS, credential checks and stored-content validation remain authoritative.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


_SCHEMAS = Path(__file__).resolve().parents[1] / 'schemas'
_TARGETS = {
    'emotional_memory': ('emotion', 'emmem'),
    'learning_memory': ('learning', 'learn'),
    'planning_memory': ('plan', 'plan'),
    'tool_guidance': ('tool-card', 'toolcard'),
}


def _load(name: str) -> dict[str, Any]:
    try:
        return json.loads((_SCHEMAS / name).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RuntimeError('ordinary author schema is unavailable') from exc


def _inline(value: Any, definitions: dict[str, Any], active: frozenset[str] = frozenset()) -> Any:
    """Keep every property self-contained, without remote or recursive refs."""
    if isinstance(value, list):
        return [_inline(item, definitions, active) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    if '$ref' in value:
        reference = value['$ref']
        if (not isinstance(reference, str) or not reference.startswith('#/$defs/')
                or reference in active or reference[8:] not in definitions):
            raise RuntimeError('ordinary author schema has an unresolved reference')
        resolved = _inline(definitions[reference[8:]], definitions, active | {reference})
        siblings = {key: item for key, item in value.items() if key != '$ref'}
        return ({'allOf': [resolved, _inline(siblings, definitions, active)]}
                if siblings else resolved)
    return {key: _inline(item, definitions, active) for key, item in value.items()}


def ordinary_revision_fields() -> dict[str, dict[str, Any]]:
    """Fresh schema copies for four ordinary-author whitelists."""
    emotion = _load('emotional-memory.schema.json')
    learning = _load('learning-memory.schema.json')
    planning = _load('planning-memory.schema.json')
    tool = _load('tool-guidance.schema.json')
    emotion_fields = emotion['$defs']['authoredChanges']['properties']
    learning_fields = {
        key: value for key, value in learning['$defs']['learning_card']['properties'].items()
        if key not in {'epistemic_status', 'provenance_badge'}
    }
    planning_fields = planning['$defs']['legacy_plan_content']['properties']
    tool_fields = {
        {'canonical_tool_name': 'tool_name', 'claimed_confidence': 'confidence'}.get(key, key): value
        for key, value in tool['$defs']['cardContent']['properties'].items()
        if key not in {'effective_confidence', 'observed_schema_hash', 'valid_from', 'lifecycle'}
    }
    result = {
        'emotional_memory': _inline(emotion_fields, emotion['$defs']),
        'learning_memory': _inline(learning_fields, learning['$defs']),
        'planning_memory': _inline(planning_fields, planning['$defs']),
        'tool_guidance': _inline(tool_fields, tool['$defs']),
    }
    result['planning_memory']['ai_adoption_statement']['description'] = (
        'AI 自己撰写的采纳声明；人称由作者选择，最多 500 字符。'
        '省略保留当前内容，提供此字段不会授予外部执行权限。'
    )
    tools = result['tool_guidance']
    tools['reminder'] = {
        'anyOf': [tools['reminder'], {'type': 'null'}],
        'description': '作者的一句话提醒；省略保留原值，null 清除自写提醒并恢复既有用途节选规则。',
    }
    tools['source_ref']['description'] = '省略保留原值，null 明确清除；来源类型沿用工具卡原规则。'
    tools['expires_at']['description'] = '省略保留原值，null 明确清除到期时间；日期由作者选择。'
    tools['confidence']['description'] = '作者自填 0–100；数值不授予实际执行权限。'
    tools.update({
        'intent': {'enum': ['revise', 'retire', 'restore'],
                   'description': '可省略，默认修订作者内容；retire 退役，restore 恢复已读历史版本。'},
        'target_version': {'type': 'integer', 'minimum': 1,
                           'description': '仅 restore 使用的已读历史版本；普通内容修改省略。'},
        'edit_class': {'enum': ['typo', 'metadata', 'source_addition', 'salience_downweight', 'major'],
                       'description': '旧操作兼容选项；普通内容修改省略，直接提交要改的字段。'},
        'field_name': {'type': 'string', 'minLength': 1,
                       'description': '仅旧 typo 操作指定的作者文本字段。'},
        'before_text': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
        'after_text': {'type': 'string', 'maxLength': 1000},
        'clear_fields': {'type': 'array',
                         'items': {'enum': ['reminder', 'source_ref', 'expires_at']},
                         'description': '旧清除方式兼容；也可直接将对应 changes 字段写成 null。'},
    })
    for key in ('correctness_assessment', 'calm_check_stability', 'calm_check_necessity',
                'calm_check_consequences', 'calm_check_alternatives'):
        tools[key] = {'type': 'string', 'description': '旧调用兼容项；普通作者修订省略，无需填写审核表。'}
    for key, schema in list(tools.items()):
        if key not in {'reminder', 'source_ref', 'expires_at'}:
            tools[key] = {'anyOf': [schema, {'type': 'null'}],
                          'description': schema.get('description', '') + ' 省略或 null 保留现有值。'}
    return result


def install_ordinary_revision_schema(parameters: dict[str, Any]) -> None:
    """Replace only revise_memory's authored changes and target discriminator."""
    modules = ordinary_revision_fields()
    union: dict[str, Any] = {}
    for name in sorted(set().union(*(set(fields) for fields in modules.values()))):
        variants = []
        owners = []
        for module, fields in modules.items():
            if name not in fields:
                continue
            owners.append(module)
            variant = copy.deepcopy(fields[name])
            if variant not in variants:
                variants.append(variant)
        schema = variants[0] if len(variants) == 1 else {'anyOf': variants}
        schema['description'] = (
            '适用模块：' + '、'.join(owners) + '。按 target_ref 对应模块使用本字段；'
            '只填写本次要改的内容，省略保留原值。'
            + schema.get('description', '')
        )
        union[name] = schema
    properties = parameters['properties']
    properties['changes'] = {
        'type': 'object', 'minProperties': 1, 'additionalProperties': False,
        'properties': union,
        'description': (
            '普通作者字段：emotion:// 使用 original_text、primary_emotion、origin 等；'
            'learning:// 使用 current_understanding、source_basis、claim_review 等；'
            'plan:// 使用 original_text、提醒、时间和层级等；'
            'tool-card:// 使用 purpose、reminder、scenario_tags、confidence 等。按 target_ref 分支约束。'
            '正文修订保留旧版本。keywords 等列表使用真实 JSON 数组；'
            '直连 MCP 的 keywords 字符串兼容仍由接收端处理，网关使用数组。'
            '编号、owner/model、哈希、版本、审计及派生字段由系统维护。'
        ),
    }
    properties['target_ref'] = {
        **properties['target_ref'],
        'pattern': r'^(?:emotion://emmem_|learning://learn_|plan://plan_|tool-card://toolcard_)[0-9a-f]{32}@[1-9][0-9]{0,14}$',
        'description': '已经读到的精确版本引用；前缀决定 changes 的字段与限额。版本冲突时先读回再决定，不自动覆盖。',
    }
    branches = []
    for module, fields in modules.items():
        scheme, prefix = _TARGETS[module]
        branches.append({
            'if': {'properties': {'target_ref': {'pattern': '^' + scheme + '://'}},
                   'required': ['target_ref']},
            'then': {'properties': {
                'changes': {'type': 'object', 'minProperties': 1,
                            'additionalProperties': False, 'properties': copy.deepcopy(fields)},
            }},
        })
    parameters['allOf'] = [*parameters.get('allOf', []), *branches]
