"""Ordinary memories with lossless source timelines; no review/approval queue.

The author supplies the new summary. Stores own ordinary validation, versions,
recall rules and audit; this module makes creation, transfer and archival one
transaction. Provenance is historical data, never a second type of memory.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import copy
from datetime import datetime, timezone
import inspect
import json
import re
import sqlite3
import uuid

from .credential_guard import contains_credential_or_secret
from .emotion_plan_bindings import normalize_bound_plan_refs

MODULES = {'emotion': 'emotional_memory', 'learning': 'learning_memory',
           'plan': 'planning_memory', 'tool-card': 'tool_guidance'}
TABLES = {'emotion': ('emotion_memories', 'memory_id', 'emotion_module_state'),
          'learning': ('learning_items', 'learning_id', 'learning_module_state'),
          'plan': ('planning_items', 'plan_id', 'planning_module_state'),
          'tool-card': ('tool_cards', 'card_id', 'tool_module_state')}
SCHEMA = '''CREATE TABLE IF NOT EXISTS memory_consolidations (
 owner_id TEXT NOT NULL, model_id TEXT NOT NULL, target_module TEXT NOT NULL,
 target_id TEXT NOT NULL, sources_json TEXT NOT NULL, created_at TEXT NOT NULL,
 parents_json TEXT NOT NULL DEFAULT '[]',
 PRIMARY KEY(owner_id,model_id,target_module,target_id))'''
ALIASES = '''CREATE TABLE IF NOT EXISTS memory_consolidation_sources (
 owner_id TEXT NOT NULL, model_id TEXT NOT NULL, source_module TEXT NOT NULL,
 source_id TEXT NOT NULL, target_module TEXT NOT NULL, target_id TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(owner_id,model_id,source_module,source_id))'''


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def unique(values):
    result, seen = [], set()
    for value in values:
        key = value.casefold() if isinstance(value, str) else encode(value)
        if key not in seen:
            seen.add(key); result.append(value)
    return result


def identity(ref):
    if not isinstance(ref, str):
        raise ValueError('memory_reference_required')
    match = re.fullmatch(r'(emotion|learning|plan|tool-card)://([^@/]+)(?:@([1-9][0-9]*))?', ref.strip())
    if not match:
        raise ValueError('invalid_memory_reference')
    return match[1], match[2]


def now():
    return datetime.now(timezone.utc).isoformat()


def time_key(item):
    try:
        stamp = datetime.fromisoformat(item['event_at'].replace('Z', '+00:00'))
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp(), item['ref']
    except (ValueError, TypeError, KeyError):
        return float('inf'), item['ref']


def related(left, right):
    """Cheap candidate grouping, not an assertion that two facts are identical."""
    a, b = ({v.casefold() for v in x.get('keywords', [])} for x in (left, right))
    if a & b:
        return True
    texts = [(x.get('title', '') + ' ' + x.get('summary', '')).casefold().strip() for x in (left, right)]
    if texts[0] and texts[0] == texts[1]:
        return True
    grams = [{t[i:i+2] for i in range(len(t)-1) if t[i:i+2].strip()} for t in texts]
    return bool(grams[0] and grams[1] and len(grams[0] & grams[1]) / len(grams[0] | grams[1]) >= .55)


class MemoryConsolidationStore:
    def __init__(self, stores):
        self.stores = dict(stores)
        self.database = next(iter(stores.values())).database
        # Ordinary brains are in the same ST database; never partially merge
        # independent files using four separately committed writes.
        from pathlib import Path
        if len({Path(s.database).resolve() for s in stores.values()}) != 1:
            raise ValueError('consolidation_requires_shared_database')
        with self.connection() as db:
            db.execute(SCHEMA); db.execute(ALIASES)
            if "parents_json" not in {r["name"] for r in db.execute("PRAGMA table_info(memory_consolidations)")}:
                db.execute("ALTER TABLE memory_consolidations ADD COLUMN parents_json TEXT NOT NULL DEFAULT '[]'")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _exists(db, table):
        return db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone() is not None

    def _read(self, db, scope, module, memory_id):
        table, key, _ = TABLES[module]
        row = db.execute(f'SELECT * FROM {table} WHERE owner_id=? AND model_id=? AND {key}=?', (*scope, memory_id)).fetchone()
        if row is None:
            raise ValueError('memory_not_found')
        store = self.stores[module]
        version = row['current_version']
        if module == 'emotion':
            fields = {**store._mutable(row), **{k: row[k] for k in ('original_text', 'memory_type', 'source_timestamp')}}
        else:
            versions = {'learning': 'learning_versions', 'plan': 'planning_versions', 'tool-card': 'tool_card_versions'}[module]
            raw = db.execute(f'SELECT {"mutable_json" if module == "learning" else "content_json"} FROM {versions} WHERE {key}=? AND version=?', (memory_id, version)).fetchone()
            fields = json.loads(raw[0])
        time_field = next((key for key in ('source_timestamp','start_at','valid_as_of') if fields.get(key)), None)
        event = fields.get(time_field) if time_field else None
        # A date is not inferred from prose. Missing event dates explicitly use
        # creation time, leaving the AI free to correct dates in its summary.
        content = fields.get('original_text', fields.get('current_understanding', fields.get('documentation_note') or fields.get('call_notes') or fields.get('purpose', '')))
        lifecycle = row['recall_lifecycle'] if module == 'plan' else row['lifecycle']
        state = store._projection(db, *scope, memory_id)['state'] if module == 'plan' else lifecycle
        item = {'module': module, 'id': memory_id, 'ref': f'{module}://{memory_id}@{version}',
                'version': version, 'title': fields.get('title') or fields.get('display_label') or fields.get('summary', ''),
                'summary': fields.get('summary', fields.get('purpose', '')), 'content': content,
                'keywords': fields.get('keywords', []), 'event_at': event or row['created_at'],
                'time_basis': time_field or 'created_at_fallback', 'created_at': row['created_at'],
                'state': state, 'lifecycle': lifecycle, 'fields': fields}
        origin = db.execute('SELECT sources_json,parents_json FROM memory_consolidations WHERE owner_id=? AND model_id=? AND target_module=? AND target_id=?', (*scope, module, memory_id)).fetchone()
        item['sources'] = json.loads(origin[0]) if origin else []
        item['prior_summaries'] = json.loads(origin[1]) if origin else []
        item['bound_plan_refs'] = fields.get('bound_plan_refs', [])
        return item

    @staticmethod
    def _leaf(item):
        return {k: item[k] for k in ('module','id','ref','title','summary','content','keywords','event_at','time_basis','created_at','state','bound_plan_refs')}

    def _leaves(self, items):
        leaves = {}
        for item in items:
            for source in item['sources'] or [self._leaf(item)]:
                leaves[(source['module'], source['id'])] = source
        return sorted(leaves.values(), key=time_key)

    def _incoming_refs(self, db, scope, modules):
        """Load incoming native references once for the whole timeline query."""
        incoming = {}
        def add(target, source):
            incoming.setdefault(identity(target), []).append(source)
        if 'plan' in modules:
            for row in db.execute("SELECT memory_id,bound_plan_refs_json FROM emotion_memories WHERE owner_id=? AND model_id=? AND lifecycle='active'", scope):
                for ref in json.loads(row['bound_plan_refs_json']):
                    add(ref, 'emotion://' + row['memory_id'])
            for row in db.execute("SELECT plan_id FROM planning_items WHERE owner_id=? AND model_id=? AND recall_lifecycle='active'", scope):
                fields = self._read(db, scope, 'plan', row['plan_id'])['fields']
                targets = fields.get('dependency_refs', [])
                if fields.get('parent_ref'): targets = [*targets, fields['parent_ref']]
                for ref in targets: add(ref, 'plan://' + row['plan_id'])
        if 'tool-card' in modules:
            for row in db.execute("SELECT card_id FROM tool_cards WHERE owner_id=? AND model_id=? AND lifecycle='active'", scope):
                fields = self._read(db, scope, 'tool-card', row['card_id'])['fields']
                for ref in fields.get('linked_tool_refs', []):
                    add(ref, 'tool-card://' + row['card_id'])
        return incoming

    def _binding_refs(self, db, scope, items, incoming):
        refs = []
        for item in items:
            refs += item['bound_plan_refs']
            refs += incoming.get((item['module'], item['id']), [])
            f = item['fields']
            refs += f.get('dependency_refs', []) + f.get('linked_tool_refs', [])
            refs += [r for r in f.get('related_refs', []) if isinstance(r,str) and r.split('://')[0] in MODULES]
            if f.get('parent_ref'): refs.append(f['parent_ref'])
            if item['module'] == 'emotion':
                for edge in db.execute("SELECT * FROM emotion_edges WHERE owner_id=? AND model_id=? AND lifecycle='active' AND (from_memory_id=? OR to_memory_id=?)",(*scope,item['id'],item['id'])):
                    other = edge['to_memory_id'] if edge['from_memory_id']==item['id'] else edge['from_memory_id']
                    refs.append('emotion://'+other)
            if item['module'] == 'learning':
                for edge in db.execute('SELECT * FROM learning_links WHERE owner_id=? AND model_id=?',scope):
                    left,right=identity(edge['from_ref']),identity(edge['to_ref'])
                    if left == ('learning',item['id']):refs.append(edge['to_ref'])
                    elif right == ('learning',item['id']):refs.append(edge['from_ref'])
            if self._exists(db, 'memory_relation_items'):
                for row in db.execute('SELECT * FROM memory_relation_items WHERE owner_id=? AND model_id=? AND active=1 AND ((from_module=? AND from_id=?) OR (to_module=? AND to_id=?))', (*scope, item['module'], item['id'], item['module'], item['id'])):
                    side = 'to' if (row['from_module'], row['from_id']) == (item['module'], item['id']) else 'from'
                    refs.append(f"{row[side+'_module']}://{row[side+'_id']}")
        selected = {(x['module'], x['id']) for x in items}
        return unique([ref for ref in refs if identity(ref) not in selected])

    def timelines(self, *, owner_id, model_id, query='', module=None, refs=None, include_content=False, offset=0, limit=20, include_archived=False):
        scope = owner_id, model_id
        if module in MODULES.values():
            module = next(k for k,v in MODULES.items() if v == module)
        if module is not None and module not in MODULES:
            raise ValueError('invalid_memory_module')
        if not isinstance(query, str) or type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise ValueError('invalid_timeline_arguments')
        semantic_status = 'not_requested'
        with self.connection() as db:
            db.execute('BEGIN')
            if refs:
                items = [self._read(db, scope, *identity(ref)) for ref in unique(refs)]
            else:
                items = []
                for brain in ([module] if module else self.stores):
                    table, key, _ = TABLES[brain]
                    for row in db.execute(f'SELECT {key} FROM {table} WHERE owner_id=? AND model_id=?', scope):
                        item = self._read(db, scope, brain, row[0])
                        if include_archived or (item['lifecycle'] == 'active' and item['state'] != 'archived'):
                            items.append(item)
            if query.strip():
                # Reuse the configured local semantic retrieval channel. It is
                # optional: keyword/text matches remain usable without a model.
                from .semantic_retrieval import Document, search
                docs = [Document(x['ref'], x['version'], x['title']+' '+x['summary']+' '+' '.join(x['keywords'])) for x in items]
                semantic = search(database=self.database, owner_id=owner_id, model_id=model_id, module='memory_timelines', query=query, documents=docs, explicit_wait=False)
                semantic_status = semantic.status
                scores = semantic.scores
                items = [x for x in items if query.casefold() in encode([x['title'],x['summary'],x['keywords'], x['content'] if include_content else '']).casefold() or scores.get(x['ref'], 0) >= .55]
            # Connected topic components, independently within each brain.
            groups = []
            for item in items:
                matches = [g for g in groups if g[0]['module'] == item['module'] and (bool(query.strip()) or any(related(item, x) for x in g))]
                merged = [item]
                for group in matches:
                    merged += group; groups.remove(group)
                groups.append(merged)
            incoming = self._incoming_refs(db, scope, {x['module'] for x in items})
            result = []
            for group in groups:
                group.sort(key=time_key)
                leaves = self._leaves(group)
                counts = {}
                for x in group:
                    for word in unique(x['keywords']):
                        counts[word] = counts.get(word, 0) + 1
                shared = sorted(counts, key=lambda w: (-counts[w], w))
                keywords = unique(w for x in group for w in x['keywords'])
                bindings = self._binding_refs(db, scope, group, incoming)
                entries = []
                for x in leaves:
                    entries.append(dict(x) if include_content else {k:v for k,v in x.items() if k != 'content'})
                result.append({'title': query.strip() or ' · '.join(shared[:3]) or group[-1]['title'],
                    'module': group[0]['module'], 'memory_refs': [x['ref'] for x in group],
                    'memory_count': len(group), 'source_count': len(leaves), 'keywords': keywords,
                    'bound_memory_count': len({identity(r) for r in bindings}), 'bound_memory_refs': bindings,
                    'first_event_at': leaves[0]['event_at'], 'last_event_at': leaves[-1]['event_at'],
                    'timeline': entries, 'merge_suggested': len(group) > 3,
                    'prior_summaries': [{k:v for k,v in x.items() if include_content or k != 'content'} for x in unique(y for x in group for y in x['prior_summaries'])],
                    'field_differences': self._differences(group),
                    'summaries': [{'ref': x['ref'], 'summary': x['summary']} for x in group]})
            result.sort(key=lambda x: (-x['memory_count'], x['first_event_at'], x['title']))
            return {'groups': result[offset:offset+limit], 'total': len(result), 'semantic_status':semantic_status,
                    'next_offset': offset+limit if offset+limit < len(result) else None,
                    'notice': '分组是整理候选，不代表事实相同；AI选择来源并重写摘要。time_basis标明来源时间、计划开始时间、知识截至时间或存入时间，不从正文猜日期；created_at始终是实际存入时间。'}

    @staticmethod
    def _differences(items):
        keys = set().union(*(x['fields'] for x in items)) - {'keywords','original_text','current_understanding','summary','title','documentation_note','call_notes'}
        return {key: [{'ref': x['ref'], 'value': x['fields'].get(key)} for x in items]
                for key in sorted(keys) if len({encode(x['fields'].get(key)) for x in items}) > 1}

    def _bound_store(self, module, db):
        result = copy(self.stores[module])
        result._connect = lambda: nullcontext(db)
        result._begin = lambda connection: None
        return result

    @staticmethod
    def _version(db, scope, module):
        row = db.execute(f'SELECT row_version FROM {TABLES[module][2]} WHERE owner_id=? AND model_id=?', scope).fetchone()
        return row[0] if row else 0

    def merge(self, *, owner_id, model_id, source_refs, summary, access, title=None, content=None, keywords=None, fields=None, reason='AI 自主整理记忆'):
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError('ai_written_summary_required')
        if not isinstance(source_refs, list) or len(set(source_refs)) < 2:
            raise ValueError('choose_two_or_more_memories')
        if contains_credential_or_secret([summary,title,content,keywords,fields,reason]):
            raise ValueError('credential_or_secret_detected')
        scope = owner_id, model_id
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            items = [self._read(db, scope, *key) for key in dict.fromkeys(identity(r) for r in source_refs)]
            for ref in source_refs:
                if '@' in ref and int(ref.rsplit('@',1)[1]) != next(x['version'] for x in items if identity(ref) == (x['module'],x['id'])):
                    raise ValueError('source_changed_read_current_version')
            # A merge remains an ordinary record in its original brain. Related
            # records in other brains stay linked, rather than changing type.
            module = items[0]['module']
            if len(items) < 2 or any(x['module'] != module for x in items):
                raise ValueError('choose_memories_from_one_brain_keep_other_brains_linked')
            if any(x['lifecycle'] != 'active' or x['state'] == 'archived' for x in items):
                raise ValueError('source_already_archived_use_current_summary')
            leaves = self._leaves(items)
            combined = dict(items[0]['fields'])
            for key in combined:
                if isinstance(combined[key], list) and key not in {'referent_bindings','secondary_emotions'}:
                    combined[key] = unique(value for x in items for value in x['fields'].get(key, []))
            # A new narrative needs its own pronoun offsets; old offsets remain
            # in immutable source versions and cannot be copied into new prose.
            combined['referent_bindings'] = []
            combined.update(fields or {})
            combined['keywords'] = unique([*(w for x in items for w in x['keywords']), *(keywords or []), *combined.get('keywords', [])])
            combined['summary'] = summary
            combined['lifecycle'] = 'active'
            if module == 'tool-card':
                selected_ids = {x['id'] for x in items}
                combined['linked_tool_refs'] = [r for r in combined.get('linked_tool_refs',[]) if identity(r)[1] not in selected_ids]
            body = content if content is not None else '\n\n'.join(f"[{x['event_at']} · {x['time_basis']}]\n{x['content']}" for x in leaves)
            if content is None:
                body += ''.join('\n\n[此前整理正文 · '+x['ref']+']\n'+x['content'] for x in items if x['sources'])
            label = title if title is not None else (' · '.join(combined['keywords'][:3]) or items[0]['title'])[:4000]
            store = self._bound_store(module, db)
            base = dict(owner_id=owner_id, model_id=model_id, wake_id=access['wake_id'], expected_row_version=self._version(db, scope, module), reason=reason)
            if module == 'emotion':
                combined.update(original_text=body, source_timestamp=(fields or {}).get('source_timestamp',leaves[0]['event_at']))
                combined['bound_plan_refs'] = normalize_bound_plan_refs([r for x in items for r in x['bound_plan_refs']] + combined.get('bound_plan_refs', []))
                if combined.get('memory_type') == 'integration':
                    combined['memory_type'] = 'unclassified'
                allowed = inspect.signature(store.remember).parameters
                result = store.remember(**base, preserve_original_text=True, **{k:v for k,v in combined.items() if k in allowed and k not in base})
            elif module == 'learning':
                combined.update(title=label, current_understanding=body)
                result = store.remember(**base, wake_seq=access['wake_seq'], correctness_assessment='整理来源，真实性判断沿用原记录。', preserve_original_text=True, **combined)
            elif module == 'plan':
                allowed = inspect.signature(store.remember_ordinary).parameters
                combined.update(title=label, content=body)
                selected_ids = {x['id'] for x in items}
                if combined.get('parent_ref') and identity(combined['parent_ref'])[1] in selected_ids:
                    combined['parent_ref'] = None
                combined['dependency_refs'] = [r for r in combined.get('dependency_refs',[]) if identity(r)[1] not in selected_ids]
                result = store.remember_ordinary(**base, wake_seq=access['wake_seq'], write_context_ref=access['write_context_ref'], idempotency_key=uuid.uuid4().hex,
                    **{k:v for k,v in combined.items() if k in allowed and k not in base})
            else:
                combined.update(tool_name=combined.get('tool_name',combined.pop('canonical_tool_name')), purpose=summary, display_label=label, documentation_note=body, operation_key='merge_'+uuid.uuid4().hex)
                allowed = inspect.signature(store._normalize_card_content).parameters
                result = store.remember(**base, **{k:v for k,v in combined.items() if k in allowed and k not in base})
            table, key, _ = TABLES[module]
            # Read the inserted ordinary identity in this same transaction.
            new_id = (result['memory']['memory_id'] if module == 'emotion' else result['learning_id'] if module == 'learning' else result['plan_id'] if module == 'plan' else result['card']['card_id'])
            new_ref = f'{module}://{new_id}@1'
            parents = unique([*(y for x in items for y in x['prior_summaries']), *[{'ref':x['ref'],'summary':x['summary'],'content':x['content']} for x in items if x['sources']]])
            db.execute('INSERT INTO memory_consolidations VALUES(?,?,?,?,?,?,?)', (*scope, module, new_id, encode(leaves), now(),encode(parents)))
            self._transfer(db, scope, items, module, new_id, new_ref, access)
            if module == 'plan' and len({x['state'] for x in items}) == 1:
                event = {'completed':'complete','paused':'pause','abandoned':'abandon'}.get(items[0]['state'])
                if event:
                    store._append_event(db, owner_id=owner_id, model_id=model_id, plan_id=new_id, event_type=event, reason=reason, evidence=[], wake_id=access['wake_id'], wake_seq=access['wake_seq'])
            for item in items:
                base['expected_row_version'] = self._version(db, scope, module)
                if module == 'emotion':
                    store.revise(**base, memory_id=item['id'], expected_memory_version=item['version'], changes={'lifecycle':'archived'})
                elif module == 'learning':
                    store.revise_ordinary(**base, wake_seq=access['wake_seq'], learning_id=item['id'], expected_item_version=item['version'], changes={'lifecycle':'archived'})
                elif module == 'tool-card':
                    store.revise(**base, wake_seq=access['wake_seq'], card_id=item['id'], expected_card_version=item['version'], intent='retire')
                else:
                    store._append_event(db, owner_id=owner_id, model_id=model_id, plan_id=item['id'], event_type='archive', reason=reason, evidence=[], wake_id=access['wake_id'], wake_seq=access['wake_seq'])
                db.execute('INSERT INTO memory_consolidation_sources VALUES(?,?,?,?,?,?,?)', (*scope,module,item['id'],module,new_id,now()))
            return {'decision':'merged','ref':new_ref,'version':1,'module':MODULES[module], 'state_changed':True,
                    'source_count':len(leaves),'archived_refs':[x['ref'] for x in items], 'keywords':combined['keywords'],
                    'bound_plan_refs':combined.get('bound_plan_refs', []),
                    'inherited_field_differences':self._differences(items),
                    'message':'已存为普通记忆；原文归档可查。新记忆照常修改和浮现。其他标量字段未指定时沿用首条；差异可继续自行修改。'}

    def _transfer(self, db, scope, items, module, new_id, new_ref, access):
        selected = {(module,x['id']) for x in items}
        if self._exists(db, 'memory_relation_items'):
            from .memory_relations import validate_relation, _hash
            edges = list(db.execute('SELECT * FROM memory_relation_items WHERE owner_id=? AND model_id=? AND active=1',scope))
            for row in edges:
                left, right = (row['from_module'],row['from_id']), (row['to_module'],row['to_id'])
                if left not in selected and right not in selected: continue
                left = (module,new_id) if left in selected else left
                right = (module,new_id) if right in selected else right
                if left == right: continue
                lref, rref = (self._read(db,scope,*value)['ref'] for value in (left,right))
                first, second, lref, rref, kind, label, reverse = validate_relation(lref,rref,row['relation_type'],row['label'],row['reverse_label'])
                key = _hash([first[:2],second[:2],kind,label,reverse])
                if db.execute('SELECT 1 FROM memory_relation_items WHERE owner_id=? AND model_id=? AND relation_key=?',(*scope,key)).fetchone(): continue
                rid, stamp = uuid.uuid4().hex, now()
                db.execute('INSERT INTO memory_relation_items VALUES(?,?,?,?,?,?,?,?,?,?,?,1,1,?,?)',(*scope,rid,key,*first[:2],*second[:2],kind,label,reverse,stamp,stamp))
                db.execute('INSERT INTO memory_relation_versions VALUES(?,?,?,?,1,?,?,?,?)',(*scope,rid,1,lref,rref,'attach',stamp))
        if self._exists(db, 'emotion_layer_tags'):
            for item in items:
                db.execute('INSERT OR IGNORE INTO emotion_layer_tags SELECT owner_id,model_id,?,?,emotion_id FROM emotion_layer_tags WHERE owner_id=? AND model_id=? AND module=? AND memory_id=?',(module,new_id,*scope,module,item['id']))
        if self._exists(db, 'memory_force_recall'):
            enabled = False
            for item in items:
                row = db.execute('SELECT * FROM memory_force_recall WHERE owner_id=? AND model_id=? AND module=? AND memory_id=?',(*scope,module,item['id'])).fetchone()
                if row and row['force_recall']:
                    enabled = True
                    stamp = now()
                    db.execute('UPDATE memory_force_recall SET force_recall=0,revision=revision+1,updated_at=? WHERE owner_id=? AND model_id=? AND module=? AND memory_id=?',(stamp,*scope,module,item['id']))
                    db.execute('INSERT INTO memory_force_recall_history VALUES(?,?,?,?,?,?,?)',(*scope,module,item['id'],row['revision']+1,0,stamp))
            if enabled:
                stamp = now()
                db.execute('INSERT INTO memory_force_recall VALUES(?,?,?,?,1,1,?)',(*scope,module,new_id,stamp))
                db.execute('INSERT INTO memory_force_recall_history VALUES(?,?,?,?,1,1,?)',(*scope,module,new_id,stamp))
        if module == 'plan':
            # Transfer incoming bindings by ordinary emotion revisions. Existing
            # plan versions and each source plan's event ledger stay intact.
            emotion = self._bound_store('emotion',db)
            ids = {x['id'] for x in items}
            for row in list(db.execute('SELECT * FROM emotion_memories WHERE owner_id=? AND model_id=? AND lifecycle=\'active\'',scope)):
                refs = json.loads(row['bound_plan_refs_json'])
                if any(identity(r)[1] in ids for r in refs):
                    emotion.revise(owner_id=scope[0],model_id=scope[1],wake_id=access['wake_id'],expected_row_version=self._version(db,scope,'emotion'),memory_id=row['memory_id'],expected_memory_version=row['current_version'],changes={'bound_plan_refs':normalize_bound_plan_refs([new_ref if identity(r)[1] in ids else r for r in refs])},reason='计划整理后自动转绑')
        self._transfer_native_links(db, scope, items, module, new_id, new_ref, access)
        # Image and historical ancillary records are accessible through source
        # timelines. Images themselves also follow the new ordinary target.
        if self._exists(db, 'memory_image_items'):
            columns = [r['name'] for r in db.execute('PRAGMA table_info(memory_image_items)')]
            for item in items:
                for row in list(db.execute('SELECT * FROM memory_image_items WHERE owner_id=? AND model_id=? AND target_module=? AND target_id=? AND active=1',(*scope,module,item['id']))):
                    values = dict(row); values.update(image_id=uuid.uuid4().hex,target_ref=new_ref,target_module=module,target_id=new_id)
                    db.execute('INSERT INTO memory_image_items ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',[values[k] for k in columns])

    def _transfer_native_links(self, db, scope, items, module, new_id, new_ref, access):
        ids = {x['id'] for x in items}
        def clone(table, row, changes):
            values = {**dict(row), **changes}
            db.execute('INSERT INTO '+table+' ('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',list(values.values()))
        if module == 'emotion':
            seen = set()
            for row in list(db.execute("SELECT * FROM emotion_edges WHERE owner_id=? AND model_id=? AND lifecycle='active'",scope)):
                if row['from_memory_id'] not in ids and row['to_memory_id'] not in ids: continue
                left = new_id if row['from_memory_id'] in ids else row['from_memory_id']
                right = new_id if row['to_memory_id'] in ids else row['to_memory_id']
                key = left,right,row['edge_type']
                if left == right or key in seen: continue
                seen.add(key)
                clone('emotion_edges',row,dict(edge_id='emed_'+uuid.uuid4().hex,from_memory_id=left,to_memory_id=right,source_memory_id=new_id if row['source_memory_id'] in ids else row['source_memory_id']))
            for row in list(db.execute("SELECT pin_id,source_ref FROM brain_pins WHERE owner_id=? AND model_id=? AND status IN ('active','pending')",scope)):
                if row['source_ref'].startswith('emotion://') and identity(row['source_ref'])[1] in ids:
                    db.execute('UPDATE brain_pins SET source_ref=?,updated_at=? WHERE pin_id=?',(new_ref,now(),row['pin_id']))
        if module == 'learning':
            seen=set()
            for item in items:
                for row in list(db.execute('SELECT * FROM learning_evidence WHERE learning_id=?',(item['id'],))):
                    key=row['source_ref'],row['content_hash']
                    if key in seen:continue
                    seen.add(key)
                    clone('learning_evidence',row,dict(evidence_id='lev_'+uuid.uuid4().hex,learning_id=new_id))
            seen=set()
            for row in list(db.execute('SELECT * FROM learning_links WHERE owner_id=? AND model_id=?',scope)):
                left,right=identity(row['from_ref']),identity(row['to_ref'])
                if left[1] not in ids and right[1] not in ids:continue
                lref=new_ref if left[1] in ids else row['from_ref'];rref=new_ref if right[1] in ids else row['to_ref']
                key=lref,rref,row['relation_type']
                if lref==rref or key in seen:continue
                seen.add(key)
                clone('learning_links',row,dict(link_id='llink_'+uuid.uuid4().hex,from_ref=lref,to_ref=rref))
        if module == 'plan':
            store=self._bound_store('plan',db)
            for row in list(db.execute("SELECT plan_id FROM planning_items WHERE owner_id=? AND model_id=? AND recall_lifecycle='active'",scope)):
                pid=row['plan_id']
                if pid in ids or pid == new_id:continue
                item=self._read(db,scope,'plan',pid);f=item['fields'];changes={}
                parent=f.get('parent_ref')
                if parent and identity(parent)[1] in ids: changes['parent_ref']=new_ref
                deps=f.get('dependency_refs',[])
                if any(identity(r)[1] in ids for r in deps):changes['dependency_refs']=unique(new_ref if identity(r)[1] in ids else r for r in deps)
                if changes:store.revise_ordinary(owner_id=scope[0],model_id=scope[1],wake_id=access['wake_id'],wake_seq=access['wake_seq'],expected_row_version=self._version(db,scope,'plan'),plan_id=pid,expected_plan_version=item['version'],changes=changes,reason='整理后自动转移计划关系')
        if module == 'tool-card':
            store=self._bound_store('tool-card',db)
            for row in list(db.execute("SELECT card_id FROM tool_cards WHERE owner_id=? AND model_id=? AND lifecycle='active'",scope)):
                cid=row['card_id']
                if cid in ids or cid == new_id:continue
                item=self._read(db,scope,'tool-card',cid);refs=item['fields'].get('linked_tool_refs',[])
                if any(identity(r)[1] in ids for r in refs):
                    store.revise(owner_id=scope[0],model_id=scope[1],wake_id=access['wake_id'],wake_seq=access['wake_seq'],expected_row_version=self._version(db,scope,'tool-card'),card_id=cid,expected_card_version=item['version'],linked_tool_refs=unique(new_ref if identity(r)[1] in ids else r for r in refs),reason='整理后自动转移工具关系')


def append_consolidation_notice(db, *, owner_id, model_id, query, dynamic, enabled):
    """Count ordinary, policy-eligible keyword hits without changing recall ranking.

    This is a short optional reminder, not a request to generate another turn or
    to merge anything automatically. No age, salience or independent-wake gate.
    """
    if not query:
        return dynamic
    from .force_recall import literal_position, _target, _blocked
    candidates = []
    for module, (table, key, _) in TABLES.items():
        if not enabled(MODULES[module]) or not MemoryConsolidationStore._exists(db, table):
            continue
        rows = list(db.execute(f'SELECT {key} FROM {table} WHERE owner_id=? AND model_id=?',(owner_id,model_id)))
        for row in rows:
            item = _target(db,(owner_id,model_id),module,row[0])
            if item is None or _blocked(module,item,query):
                continue
            words = item.get('keywords', [])
            matched = [word for word in words if literal_position(query,[word]) >= 0]
            if matched:
                candidates.append((module,row[0],matched))
    groups = {}
    for module, mid, words in candidates:
        for word in words:
            groups.setdefault((module,word.casefold()),set()).add(mid)
    hits = [(key,len(ids)) for key,ids in groups.items() if len(ids)>3]
    if not hits:
        return dynamic
    return {**dynamic,'memory_consolidation_notice': {
        'topic_count':len(hits), 'message': '本轮关键词命中中有'+str(len(hits))+'个主题各超过3条普通记忆；可用memory_timelines查看时间线，再自行决定是否merge_memories并重写摘要。'}}
