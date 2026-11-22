"""AI-authored facet summaries and retrieval policy; never rewrite identity."""
from __future__ import annotations

from collections.abc import Mapping
import re

from . import semantic_retrieval as dense
from .facet_selection import _fold, _occurs, _current_clauses, _grams, _ENGLISH_WORD, _ENGLISH_STOP, _SCENES

DEFAULT_POLICY = {
    'budget_tokens': 6000,
    'match_key': True, 'match_body': True, 'match_keywords': True, 'match_aliases': True,
    'semantic_enabled': True, 'semantic_threshold': 0.60,
    'summary_separator': '我想起了这些内容，还有一些有些模糊，只有摘要',
}
# Names and authored JSON are unrestricted; retrieval settings are interpreted locally.
from .self_content import text, facet_map
import math
REFERENCE = re.compile(r'\[\[facet:(.*?)\]\]', re.S)
FACET_METADATA = {'summary', 'keywords', 'aliases', 'related_facets'}


def facet_body(value):
    if not isinstance(value, Mapping):
        return text(value)
    body = {k: v for k, v in value.items() if k not in FACET_METADATA}
    if set(body) == {'text'}:
        return text(body['text'])
    return text(body if body else value)


def facet_summary(value):
    return text(value['summary']) if isinstance(value, Mapping) and 'summary' in value else None


def terms(value):
    if value is None:
        return []
    return [text(x) for x in value] if isinstance(value, list) else [text(value)]


def valid_policy(value):
    # Settings never gate authorship. effective_policy interprets supported values.
    return True


def valid_facet(value, *, rich=True):
    return True


def references(value):
    explicit = terms(value.get('related_facets')) if isinstance(value, Mapping) else []
    return set(explicit) | set(REFERENCE.findall(facet_body(value)))


def valid_references(facets):
    return True  # Forward references are allowed; expansion skips missing targets.


def effective_policy(value=None):
    policy = dict(DEFAULT_POLICY)
    if not isinstance(value, Mapping):
        return policy
    for key, default in policy.items():
        raw = value.get(key, default)
        if isinstance(default, bool):
            if isinstance(raw, bool): policy[key] = raw
            elif str(raw).lower() in ('true', 'false'): policy[key] = str(raw).lower() == 'true'
        elif key in ('budget_tokens', 'semantic_threshold'):
            try:
                number = float(raw)
                if not isinstance(raw, bool) and math.isfinite(number):
                    policy[key] = max(0, int(number)) if key == 'budget_tokens' else number
            except (TypeError, ValueError, OverflowError):
                pass
        elif key == 'summary_separator':
            policy[key] = '' if raw is None else text(raw)
    return policy


def hard_matches(facets, query, policy):
    literal = _fold(query)
    body_query = ' '.join(_current_clauses(query))
    words = set(_ENGLISH_WORD.findall(body_query)) - _ENGLISH_STOP
    grams = _grams(body_query)
    matches = {}
    for name, value in facets.items():
        reasons = []
        if policy['match_key'] and _occurs(literal, _fold(name)):
            reasons.append('key')
        if isinstance(value, Mapping):
            for field in ('keywords', 'aliases'):
                if policy['match_' + field] and any(_occurs(literal, _fold(x)) for x in terms(value.get(field))):
                    reasons.append(field)
        if policy['match_body'] and body_query:
            body = _fold(REFERENCE.sub('', facet_body(value)))
            # Body matching is conservative current-context lexical overlap.
            # Explicit keywords/aliases above remain literal, under AI control.
            if (words & set(_ENGLISH_WORD.findall(body)) or grams & _grams(body)
                or any(any(_occurs(body_query, x) and _occurs(body, x) for x in family) for family in _SCENES)):
                reasons.append('body')
        if reasons:
            matches[name] = reasons
    return matches


def select_facets(facets, query, *, policy=None, semantic_scores=None, semantic_status='disabled', estimate):
    """All facets survive. Budget restricts soft expansions, never hard hits."""
    policy = effective_policy(policy)
    facets = facet_map(facets)
    hard = hard_matches(facets, query, policy)
    missing = {name for name, value in facets.items() if facet_summary(value) is None}
    def closure(names):
        found = set(names)
        pending = list(names)
        while pending:
            for name in references(facets[pending.pop()]):
                if name in facets and name not in found:
                    found.add(name); pending.append(name)
        return found
    full = closure(set(hard) | missing)
    soft = []
    def payload(selected):
        return {'facets': {k: facet_body(v) for k, v in facets.items() if k in selected},
                'summaries': {k: facet_summary(v) for k, v in facets.items() if k not in selected},
                'summary_separator': policy['summary_separator']}
    mandatory_cost = estimate({'self_facets': payload(full)})
    if policy['semantic_enabled']:
        for name, score in sorted((semantic_scores or {}).items(), key=lambda x: (-x[1], x[0])):
            if name not in facets or name in full or score < policy['semantic_threshold']:
                continue
            trial = full | closure({name})
            if estimate({'self_facets': payload(trial)}) <= policy['budget_tokens']:
                full = trial; soft.append(name)
    result = payload(full)
    result['diagnostics'] = {'hard_matches': hard, 'soft_matches': soft,
        'reference_expansions': sorted(full - set(hard) - missing - set(soft)),
        'legacy_without_summary': sorted(missing), 'summary_only': sorted(set(facets) - full),
        'semantic_status': semantic_status, 'policy': policy,
        'mandatory_exceeds_budget': mandatory_cost > policy['budget_tokens'],
        'estimated_tokens': estimate({'self_facets': result})}
    return result


def recall_facets(facets, query, *, policy, database, owner_id, model_id, estimate):
    policy = effective_policy(policy)
    facets = facet_map(facets)
    semantic = dense.Result('disabled', {})
    if policy['semantic_enabled'] and query.strip() and any(isinstance(v, Mapping) for v in facets.values()):
        # Same existing local worker: no external model; cold start never waits,
        # hot search has a 150 ms ceiling and timeout falls back to hard+summary.
        docs = [dense.Document(k, 1, k + ' ' + ((facet_summary(v) or '') + ' ') + facet_body(v))
                for k, v in facets.items()]
        try:
            semantic = dense.search(database=str(database), owner_id=owner_id, model_id=model_id,
                module='self_facets', documents=docs, query=query, explicit_wait=False)
        except Exception:
            semantic = dense.Result('unavailable_lexical_fallback', {})
    return select_facets(facets, query, policy=policy, semantic_scores=semantic.scores,
                         semantic_status=semantic.status, estimate=estimate)


RULES = (
    '每条facet都出现：key、正文词句、keywords、aliases硬命中显示全文，不限3条；'
    '未命中显示你自写的summary，旧字符串没有摘要时暂保全文，不替你生成。'
    '正文按当前语境词句匹配（过滤否定、引用、假设），key/keywords/aliases按最新人类正文的字面词匹配。'
    'related_facets或正文[[facet:名称]]可联动全文；环状引用去重。'
    '本地语义只做软命中：冷启动不等，热检索最多150ms，未及时返回就用硬命中与摘要。'
    'budget_tokens是facets独立预算，不挤普通记忆；必带全文及所有摘要优先，即使超过预算也不裁剪，预算限制额外软命中展开。'
    '字段、名称（含中文）、字数、条数和版本不限制写入；可用字符串或自定JSON，未知字段原样保留。'
    'summary、keywords、aliases、related_facets均可省略；无摘要就保全文，未建引用不阻止保存。'
    '上述开关、预算、语义阈值、摘要分隔句均可通过你自己的三轮自我模型修订流程修改；只填需要改的设置。诊断只在精确查询时返回。'
)
