"""Follow stored explicit relations from memories already present in this turn.

One hop is an expansion of an actual recall, not another search or a new wake.
Bound emotion plans are appended afterwards. No timer, write or model call.
"""
from __future__ import annotations

from .force_recall import _target, _blocked, _projection
from .memory_relations import SCOPES, REVERSE


def selected_references(dynamic):
    result = {}
    def add(item, module=None):
        if item.get('presentation') == 'neutral_hint':
            return
        ref = item.get('item_ref') or item.get('memory_ref')
        if not ref and module == 'emotion' and item.get('memory_id'):
            ref = 'emotion://' + item['memory_id']
        if isinstance(ref, str) and '://' in ref:
            kind, identity = ref.split('://', 1)
            if kind in SCOPES:
                result[(kind, identity.split('@', 1)[0])] = ref
    for item in dynamic.get('emotional_memory', {}).get('memories', []):
        add(item, 'emotion')
    for section, key in (('planning_memory','envelopes'),('learning_memory','envelopes'),
                         ('tool_guidance','envelopes'),('force_recall','items'),
                         ('emotion_layer','memories'),('session_continuity','recent_memories')):
        for item in dynamic.get(section, {}).get(key, []):
            add(item)
    return result


def append_associated_recall(db, *, owner_id, model_id, dynamic, enabled, query):
    roots = selected_references(dynamic)
    if not roots:
        return dynamic
    scope = (owner_id, model_id)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cached = {}
    def target(key):
        if key not in cached:
            cached[key] = _target(db, scope, *key) if key[0] in SCOPES and enabled(SCOPES[key[0]]) else None
        return cached[key]
    items, paths, seen_paths = {}, [], set()
    def follow(left, right, kind, label=None, exact_refs=None):
        if left not in roots or left == right:
            return
        source, found = target(left), target(right)
        if source is None or found is None:
            return
        if exact_refs and (source['item_ref'], found['item_ref']) != exact_refs:
            return
        blocked = _blocked(right[0], found, query)
        # Completed/paused plans remain useful context; their status is shown.
        if blocked and blocked != 'plan_not_active_or_paused':
            return
        edge = (source['item_ref'], found['item_ref'], kind, label)
        if edge in seen_paths:
            return
        seen_paths.add(edge)
        paths.append({'from_ref':edge[0], 'to_ref':edge[1], 'type':kind, 'label':label})
        if right in roots or right in items:
            return
        entry = _projection(right[0], found)
        if right[0] != 'emotion' or entry.get('presentation') != 'neutral_hint':
            # Keep the AI's authored summary intact. No new count/cooldown or
            # authoring-size restriction is introduced by following a link.
            entry['text'] = found.get('summary', '') if right[0] != 'tool-card' else found.get('reminder') or found.get('purpose', '')
            entry.pop('excerpt', None)
        items[right] = entry

    if 'memory_relation_items' in tables:
        for row in db.execute("SELECT from_module,from_id,to_module,to_id,relation_type,label,reverse_label "
                              "FROM memory_relation_items WHERE owner_id=? AND model_id=? AND active=1 ORDER BY relation_id",scope):
            a,b=(row[0],row[1]),(row[2],row[3])
            if a in roots:
                follow(a,b,row[4],row[5])
            else:
                follow(b,a,REVERSE.get(row[4],row[4]),row[6])
    if 'emotion_edges' in tables:
        for row in db.execute("SELECT from_memory_id,to_memory_id,edge_type FROM emotion_edges "
                              "WHERE owner_id=? AND model_id=? AND lifecycle='active' AND weight>0 ORDER BY edge_id",scope):
            follow(('emotion',row[0]),('emotion',row[1]),row[2])
    if 'learning_links' in tables:
        for row in db.execute("SELECT from_ref,to_ref,relation_type FROM learning_links WHERE owner_id=? AND model_id=? ORDER BY link_id",scope):
            a,b = tuple(row[0].split('://',1)),tuple(row[1].split('://',1))
            a=(a[0],a[1].split('@')[0]); b=(b[0],b[1].split('@')[0])
            follow(a,b,row[2],exact_refs=(row[0],row[1]))
    if 'planning_edges' in tables:
        for row in db.execute("SELECT e.source_plan_id,e.target_plan_id,e.edge_type FROM planning_edges e "
                              "JOIN planning_items i ON i.plan_id=e.source_plan_id AND i.owner_id=e.owner_id AND i.model_id=e.model_id "
                              "WHERE e.owner_id=? AND e.model_id=? AND e.active=1 AND e.source_version=i.current_version ORDER BY e.edge_id",scope):
            follow(('plan',row[0]),('plan',row[1]),row[2])
    for key in roots:
        if key[0] != 'tool-card':
            continue
        card = target(key)
        if card is not None:
            for field in ('linked_tool_refs','related_refs'):
                for ref in card.get(field,[]):
                    module, identity = ref.split('://',1)
                    follow(key,(module,identity.split('@')[0]),field)
    return {**dynamic,'associated_recall': {'items':list(items.values()), 'paths':paths}}
