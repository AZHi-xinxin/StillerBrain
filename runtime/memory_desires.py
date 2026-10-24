"""Optional short wishes attached to emotional memories, independent of parent lifecycle."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import sqlite3

from .emotion_layer import EmotionLayerError
from .memory_relations import MemoryRelationStore, MemoryRelationError, _ddl, _now, parse_memory_ref

GUIDE = ("欲望是依附情感记忆的可选想法，最多200字；不填也可正常存入。"
         "可改写、删除、归档或恢复，归档不删除父记忆。只在该记忆已获准浮现且检索分数大于0.8时附带；"
         "这是检索阈值，不是80%准确率。至少10条未归档时给出简短提醒。"
         "要形成计划，可先用plan_draft取得草稿，调用规划脑保存成功后再归档欲望。")
ITEM_SQL = """CREATE TABLE IF NOT EXISTS memory_desires (
 owner_id TEXT NOT NULL, model_id TEXT NOT NULL, memory_id TEXT NOT NULL,
 text TEXT NOT NULL CHECK(length(text)<=200), state TEXT NOT NULL CHECK(state IN ('active','archived','deleted')),
 revision INTEGER NOT NULL CHECK(revision>=1), updated_at TEXT NOT NULL,
 PRIMARY KEY(owner_id,model_id,memory_id))"""
HISTORY_SQL = """CREATE TABLE IF NOT EXISTS memory_desire_history (
 owner_id TEXT NOT NULL, model_id TEXT NOT NULL, memory_id TEXT NOT NULL,
 revision INTEGER NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(owner_id,model_id,memory_id,revision))"""
DDL = {"memory_desires": ITEM_SQL, "memory_desire_history": HISTORY_SQL}
_PENDING = ContextVar("pending_memory_desire", default=None)

class DesireError(EmotionLayerError):
    pass

def validate_desire(text):
    if not isinstance(text, str) or len(text) > 200 or "\x00" in text:
        raise DesireError("desire_text_invalid")
    return text.strip()

@contextmanager
def pending_desire(store, identity, text, guard):
    status = {"attached": False}
    token = _PENDING.set((store, identity, text, guard, status) if text else None)
    try: yield status
    finally: _PENDING.reset(token)

def attach_pending_desire(connection, owner_id, model_id, memory_ref):
    pending = _PENDING.get()
    if pending is None: return
    store, identity, text, guard, status = pending
    if identity != (owner_id, model_id): raise DesireError("desire_identity_mismatch")
    guard(connection)
    store._put(connection, identity, memory_ref, text, expected_revision=0)
    status["attached"] = True

class MemoryDesireStore(MemoryRelationStore):
    def __init__(self, database):
        from pathlib import Path
        self.database = str(Path(database).resolve())
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute("SELECT name FROM sqlite_master WHERE name IN (?,?)", tuple(DDL)).fetchall()
            if not found:
                for sql in DDL.values(): db.execute(sql)
            self._schema(db); db.commit()

    @staticmethod
    def _schema(db):
        for name, sql in DDL.items():
            row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if row is None or row[0] != 'table' or _ddl(row[1]) != _ddl(sql): raise DesireError("desire_storage_unavailable")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?,?)", tuple(DDL)).fetchone():
            raise DesireError("desire_storage_unavailable")

    @staticmethod
    def _target(db, identity, memory_ref, current=False):
        endpoint = parse_memory_ref(memory_ref)
        if endpoint[0] != 'emotion': raise DesireError("desire_requires_emotional_memory")
        target = MemoryRelationStore._endpoint(db, identity, endpoint, current=current)
        if target['restricted_stub']: raise DesireError("desire_sensitive_target_unavailable")
        row = db.execute("SELECT explicit_request_override FROM emotion_memories WHERE owner_id=? AND model_id=? AND memory_id=?", (*identity, endpoint[1])).fetchone()
        if row[0] == 'never': raise DesireError("desire_sensitive_target_unavailable")
        return endpoint[1], target['target_ref']

    @staticmethod
    def _view(row, ref):
        return {"memory_ref": ref, "text": row['text'] if row['state'] != 'deleted' else '',
                "state": row['state'], "revision": row['revision'], "updated_at": row['updated_at']}

    def _put(self, db, identity, memory_ref, text, expected_revision=None, action='put'):
        self._schema(db)
        memory_id, ref = self._target(db, identity, memory_ref, current=True)
        before = db.execute("SELECT * FROM memory_desires WHERE owner_id=? AND model_id=? AND memory_id=?", (*identity, memory_id)).fetchone()
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != (before['revision'] if before else 0)):
            raise DesireError("desire_revision_conflict")
        if action == 'put':
            text = validate_desire(text)
            if not text: raise DesireError("desire_text_empty_use_delete")
            state = 'active'
        else:
            if before is None: raise DesireError("desire_not_found")
            text = '' if action == 'delete' else before['text']
            state = {'archive':'archived','restore':'active','delete':'deleted'}[action]
            if state == 'active' and not text: raise DesireError("desire_deleted_use_put")
        if before and (before['text'], before['state']) == (text,state):
            return {"decision":"unchanged", "state_changed":False, "desire":self._view(before,ref)}
        revision = before['revision']+1 if before else 1
        if revision > 10000: raise DesireError("desire_history_full")
        now = _now()
        db.execute("INSERT INTO memory_desires VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_id,model_id,memory_id) DO UPDATE SET text=excluded.text,state=excluded.state,revision=excluded.revision,updated_at=excluded.updated_at", (*identity,memory_id,text,state,revision,now))
        db.execute("INSERT INTO memory_desire_history VALUES(?,?,?,?,?,?,?)", (*identity,memory_id,revision,text,state,now))
        return {"decision":"saved", "state_changed":True, "desire":{"memory_ref":ref,"text":text,"state":state,"revision":revision,"updated_at":now}, "parent_memory_changed":False}

    def manage(self, *, identity, guard, action, memory_ref=None, text=None, expected_revision=None, offset=0, limit=20):
        self._identity(*identity)
        if action not in {'put','archive','restore','delete','read','list','plan_draft'}: raise DesireError("desire_action_invalid")
        write = action in {'put','archive','restore','delete'}
        with self._connection(write=write) as db:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN'); self._schema(db); guard(db)
            if write:
                result = self._put(db,identity,memory_ref,text,expected_revision,action); db.commit(); return result
            if action == 'list':
                if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50: raise DesireError('desire_page_invalid')
                # Join policy fields before pagination, so hidden records never alter counts or hints.
                rows = db.execute("SELECT d.*,e.current_version FROM memory_desires d JOIN emotion_memories e USING(owner_id,model_id,memory_id) WHERE d.owner_id=? AND d.model_id=? AND d.state!='deleted' AND e.lifecycle='active' AND e.sensitivity IN ('public','private','internal') AND e.explicit_request_override!='never' ORDER BY d.updated_at DESC,d.memory_id LIMIT ? OFFSET ?", (*identity,limit+1,offset)).fetchall()
                return {"decision":"listed","state_changed":False,"desires":[self._view(row,f"emotion://{row['memory_id']}@{row['current_version']}") for row in rows[:limit]], "next_offset":offset+limit if len(rows)>limit else None}
            memory_id, ref = self._target(db,identity,memory_ref)
            row = db.execute("SELECT * FROM memory_desires WHERE owner_id=? AND model_id=? AND memory_id=?", (*identity,memory_id)).fetchone()
            if row is None or row['state']=='deleted': raise DesireError('desire_not_found')
            result = {"decision":"read","state_changed":False,"desire":self._view(row,ref)}
            if action == 'plan_draft':
                result['next_call']={"tool":"remember_memory","arguments":{"module":"planning_memory","content":row['text'],"title":row['text'][:120],"kind":"task"}}
                result['notice']='这是计划草稿，尚未保存计划。规划脑成功返回后再归档欲望；父记忆保持原样。'
            return result

def append_desire_recall(db, *, owner_id, model_id, dynamic, estimate_tokens, budget=1200):
    """Enrich already gated, high-relevance originals only; never override disclosure policy."""
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_desires'").fetchone(): return dynamic
        MemoryDesireStore._schema(db)
        selected = dynamic.get('emotional_memory',{}).get('memories',[])
        additions=[]
        for item in selected:
            if item.get('presentation')!='original' or item.get('candidate_only') or item.get('gate_decision')!='speak' or not isinstance(item.get('score'), (int,float)) or item['score']<=.8: continue
            row=db.execute("SELECT d.* FROM memory_desires d JOIN emotion_memories e USING(owner_id,model_id,memory_id) WHERE d.owner_id=? AND d.model_id=? AND d.memory_id=? AND d.state!='deleted' AND e.lifecycle='active' AND e.sensitivity IN ('public','private','internal') AND e.explicit_request_override!='never' AND e.context_policy!='never_auto' AND e.recall_mode!='never'",(owner_id,model_id,item['memory_id'])).fetchone()
            if row: additions.append({'memory_id':item['memory_id'], 'text':row['text'] if row['state']=='active' else '这条记忆下有相应欲望已归档', 'state':row['state']})
        # Caller checks the emotional module's automatic-injection switch, even with no recall.
        count = db.execute("SELECT count(*) FROM memory_desires d JOIN emotion_memories e USING(owner_id,model_id,memory_id) WHERE d.owner_id=? AND d.model_id=? AND d.state='active' AND e.lifecycle='active' AND e.sensitivity IN ('public','private','internal') AND e.explicit_request_override!='never' AND e.context_policy!='never_auto' AND e.recall_mode!='never'",(owner_id,model_id)).fetchone()[0]
        extra={'attachments':[], 'permission_authority':'none','score_basis':'retrieval_score_not_probability'}
        result=dynamic
        if count>=10:
            candidate={**extra,'reminder':f'我有 {count} 件想做却未完成的事，可以查看欲望，归档已完成的，或自行形成计划。'}
            if estimate_tokens({**dynamic,'desires':candidate})<=budget: extra=candidate;result={**dynamic,'desires':extra}
        for addition in additions:
            candidate={**extra,'attachments':[*extra['attachments'],addition]}
            if estimate_tokens({**dynamic,'desires':candidate})<=budget: extra=candidate;result={**dynamic,'desires':extra}
        return result
    except (MemoryRelationError,DesireError,sqlite3.Error,TypeError,AttributeError): return dynamic
