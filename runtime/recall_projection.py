"""Small model-facing views of already-gated recall; diagnostic data stays intact.

This is a presentation function, never a reader, writer or disclosure decision.
Short references identify records (not immutable versions); explicit lookup
returns the current version and normal detail/disclosure controls still apply.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

PREFIXES = {'emotion': 'E', 'learning': 'L', 'plan': 'P', 'tool-card': 'T'}
SHORT_REF = re.compile(r'#[ELPT][0-9a-f]{12}\Z')


def short_ref(ref: str) -> str:
    module, separator, identity = ref.partition('://')
    if not separator or module not in PREFIXES or not identity:
        raise ValueError('invalid_recall_reference')
    identity = identity.rsplit('@', 1)[0]
    return '#' + PREFIXES[module] + hashlib.sha256((module + '://' + identity).encode()).hexdigest()[:12]


def _source(item: Mapping) -> str:
    content = item.get('content', {})
    basis = item.get('origin') or item.get('source_basis') or content.get('source_basis') or item.get('provenance_class')
    if not basis:
        label = item.get('origin_label', '')
        basis = next((value for prefix, value in (('据转述', 'reported'), ('这是我的推断', 'inferred'),
                       ('我的亲历记录', 'firsthand'), ('来源未标注', 'unmarked')) if label.startswith(prefix)), '')
    source = {'firsthand': '亲历', 'observed': '观察记录', 'reported': '转述，未独立核实',
              'inferred': '推断，未独立核实', 'unmarked': '来源未标注'}.get(basis, '')
    if item.get('gate_decision') == 'ask_first':
        source = (source + '；' if source else '') + '展开前先确认'
    elif item.get('presentation') == 'neutral_hint' or item.get('candidate_only'):
        source = (source + '；' if source else '') + '仅作线索'
    if item.get('excerpt'):
        source = (source + '；' if source else '') + '摘要节选'
    return source


def _entry(item: Mapping, module: str = '', *, include_sources: bool = False) -> dict:
    ref = item.get('item_ref') or item.get('memory_ref')
    if not ref and module == 'emotion' and item.get('memory_id'):
        ref = 'emotion://' + item['memory_id']
    if not ref:
        return {}
    content = item.get('content', {})
    text = item.get('auto_summary', item.get('summary'))
    if text is None:
        text = content.get('summary', content.get('reminder', content.get('scene_summary', item.get('text'))))
    # The gate's ask-first wording takes precedence over any private original.
    if item.get('presentation') == 'neutral_hint' and item.get('text'):
        text = item['text']
    if not isinstance(text, str) or not text:
        return {}
    result = {'编号': short_ref(ref), '摘要': text}
    source = _source(item) if include_sources else ""
    if source:
        result['来源'] = source
    return result


def continuity_notice(value: Mapping) -> str:
    window = value.get('source_transition') == 'changed'
    model = value.get('model_transition') == 'changed'
    if not window and not model:
        return ''
    def label(route):
        public, upstream = route.get('public_model', ''), route.get('upstream_model', '')
        return upstream if not public or public == upstream else f'{public}（{upstream}）'
    if window:
        before, after = value.get('previous_source_key', ''), value.get('source_key', '')
        window_text = (f'本轮已换窗，对话窗口从窗口{before[:8]}变为窗口{after[:8]}。'
                       if before and after else '本轮已换窗。')
    else:
        window_text = '本轮对话窗口未改变。'
    if model:
        old, new = label(value.get('previous_route', {})), label(value.get('route', {}))
        model_text = (f'当前对话模型身份改变，模型已从{old}变为{new}。'
                      if old and new else '当前对话模型身份改变。')
    else:
        model_text = '模型未改变。'
    return window_text + model_text


def project_dynamic(dynamic: Mapping, *, include_sources: bool = False) -> dict:
    """Short references and authored summaries. Sources remain in explicit query receipts.

    include_sources is used ONLY when replaying a previously committed v1 snapshot.
    """
    output, memories, seen = {}, [], set()
    def add(item, module=''):
        row = _entry(item, module, include_sources=include_sources)
        if row and row['编号'] not in seen:
            seen.add(row['编号']); memories.append(row)
    for item in dynamic.get('force_recall', {}).get('items', []):
        add(item)
    for item in dynamic.get('planning_memory', {}).get('envelopes', []):
        add(item)
    for item in dynamic.get('emotional_memory', {}).get('memories', []):
        add(item, 'emotion')
    for key in ('learning_memory', 'tool_guidance'):
        for item in dynamic.get(key, {}).get('envelopes', []):
            add(item)
    for item in dynamic.get('emotion_layer', {}).get('memories', []):
        add(item)
    continuity = dynamic.get('session_continuity', {})
    for item in continuity.get('recent_memories', []):
        add(item)
    if memories:
        output['记忆'] = memories
    omitted = dynamic.get('force_recall', {}).get('omitted_count', 0)
    if omitted:
        output['必浮现省略'] = f'{omitted}条未放下，可查询必浮现回执。'
    pins = [p['display_text'] for p in dynamic.get('emotional_memory', {}).get('pins', []) if p.get('display_text')]
    if pins:
        output['常驻提醒'] = pins
    notice = continuity_notice(continuity)
    if notice:
        output['变化'] = notice
    facets = dynamic.get('self_facets', {})
    facet_view = {}
    if facets.get('facets'):
        facet_view['原文'] = dict(facets['facets'])
    if facets.get('summaries'):
        if facets.get('summary_separator'):
            facet_view['提示'] = facets['summary_separator']
        facet_view['摘要'] = dict(facets['summaries'])
    if facet_view:
        output['人格侧面'] = facet_view
    governance = dynamic.get('self_governance_profile', {})
    if isinstance(governance, Mapping):
        rules = [s['text'] for s in governance.get('scopes', []) if s.get('text')]
        if rules:
            output['自定准则'] = rules
    feelings = dynamic.get('emotion_layer', {}).get('primary_emotions', [])
    if feelings:
        output['情绪释义'] = [{x['name']: x['description']} for x in feelings]
    desires = dynamic.get('desires', {})
    notes = [x['text'] for x in desires.get('attachments', []) if x.get('text')]
    if desires.get('reminder'):
        notes.append(desires['reminder'])
    if notes:
        output['欲望'] = notes
    images = dynamic.get('memory_images', {}).get('attachments', [])
    if images:
        output['附图'] = [{'编号': short_ref(x['target_ref']), '张数': x['count']} for x in images]
    # Optional capability or pending-work notices remain short and factual.
    notices = [dynamic.get('st_start_entry', {}).get('message'), dynamic.get('emotional_memory', {}).get('reminder')]
    if any(notices):
        output['提示'] = [x for x in notices if x]
    vault = dynamic.get('hallucination_vault_status')
    if vault:
        output['黑匣子状态'] = dict(vault.get('counts', {}))
    return output


def estimate_dynamic(value: Mapping) -> int:
    # The mixed selector also asks for the cost of a single tool envelope.
    projected = _entry(value) if value.get('item_ref') else project_dynamic(value)
    text = json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    ascii_count = sum(ord(c) < 128 for c in text)
    return max(1, (ascii_count + 3) // 4 + len(text) - ascii_count)
