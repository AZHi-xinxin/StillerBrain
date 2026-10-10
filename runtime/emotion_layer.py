"""Owner-scoped authored feelings, linked to ordinary memories without copying bodies."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import sqlite3
import uuid

from .credential_guard import contains_credential_or_secret

CONTRACT = "emotion-layer/1"
SCOPES = {"emotion": "emotional_memory", "learning": "learning_memory",
          "plan": "planning_memory", "tool-card": "tool_guidance", "work": "learning_memory"}
REFERENCE = re.compile(r"(emotion|learning|plan|tool-card|work)://([A-Za-z0-9_-]{1,256})@([1-9][0-9]{0,9})\Z")
GUIDE = "情绪名称、定义和数量由你自定；喜怒哀乐仅为可改可删的占位。标签可选，可用名字或 id。删除只解绑，不删记忆。同感觉连带只带短摘要与定义；默认最多1条、20轮冷却，共用原上下文预算。"
_PENDING = ContextVar("st_pending_emotion_tags", default=None)


class EmotionLayerError(ValueError):
    pass


def parse_ref(value):
    found = REFERENCE.fullmatch(value) if isinstance(value, str) else None
    if not found:
        raise EmotionLayerError("invalid_memory_ref")
    return found[1], found[2], int(found[3])


def _now():
    return datetime.now(timezone.utc).isoformat()


def _validate(name, description):
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise EmotionLayerError("emotion_name_invalid")
    if not isinstance(description, str) or len(description) > 200:
        raise EmotionLayerError("emotion_description_too_long")
    if contains_credential_or_secret([name, description]):
        raise EmotionLayerError("credential_or_secret_detected")
    return name.strip(), description


@contextmanager
def pending_emotion_tags(store, identity, emotions, guard):
    status = {"attached": False}
    token = _PENDING.set((store, identity, emotions, guard, status) if emotions is not None else None)
    try:
        yield status
    finally:
        _PENDING.reset(token)


def attach_pending_emotions(connection, owner_id, model_id, memory_ref):
    pending = _PENDING.get()
    if pending is None:
        return
    store, identity, emotions, guard, status = pending
    if identity != (owner_id, model_id):
        raise EmotionLayerError("emotion_identity_mismatch")
    guard(connection)
    store._tag(connection, identity, memory_ref, emotions, "add")
    status["attached"] = True


class EmotionLayerStore:
    def __init__(self, database):
        self.database = str(Path(database).resolve())
        with self.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS emotion_layer_settings (
                    owner_id TEXT NOT NULL, model_id TEXT NOT NULL,
                    secondary_limit INTEGER NOT NULL DEFAULT 1 CHECK(secondary_limit BETWEEN 0 AND 3),
                    cooldown_rounds INTEGER NOT NULL DEFAULT 20 CHECK(cooldown_rounds BETWEEN 0 AND 10000),
                    round_no INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner_id,model_id));
                CREATE TABLE IF NOT EXISTS emotion_layer_terms (
                    owner_id TEXT NOT NULL, model_id TEXT NOT NULL, emotion_id TEXT NOT NULL,
                    name TEXT NOT NULL, description TEXT NOT NULL CHECK(length(description)<=200),
                    state TEXT NOT NULL CHECK(state IN ('active','archived')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id,model_id,emotion_id), UNIQUE(owner_id,model_id,name));
                CREATE TABLE IF NOT EXISTS emotion_layer_tags (
                    owner_id TEXT NOT NULL, model_id TEXT NOT NULL,
                    module TEXT NOT NULL, memory_id TEXT NOT NULL, emotion_id TEXT NOT NULL,
                    PRIMARY KEY(owner_id,model_id,module,memory_id,emotion_id),
                    FOREIGN KEY(owner_id,model_id,emotion_id)
                    REFERENCES emotion_layer_terms(owner_id,model_id,emotion_id) ON DELETE CASCADE);
                CREATE INDEX IF NOT EXISTS emotion_layer_by_term
                    ON emotion_layer_tags(owner_id,model_id,emotion_id,module,memory_id);
                CREATE TABLE IF NOT EXISTS emotion_layer_seen (
                    owner_id TEXT NOT NULL, model_id TEXT NOT NULL, module TEXT NOT NULL,
                    memory_id TEXT NOT NULL, last_round INTEGER NOT NULL,
                    PRIMARY KEY(owner_id,model_id,module,memory_id));
                CREATE TABLE IF NOT EXISTS emotion_layer_wakes (
                    owner_id TEXT NOT NULL, model_id TEXT NOT NULL, wake_id TEXT NOT NULL,
                    round_no INTEGER NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(owner_id,model_id,wake_id));
                CREATE TABLE IF NOT EXISTS emotion_layer_metrics (
                    owner_id TEXT NOT NULL, model_id TEXT NOT NULL,
                    associated_wakes INTEGER NOT NULL DEFAULT 0,
                    cooldown_skips INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner_id,model_id));
            """)

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
        except sqlite3.IntegrityError:
            raise EmotionLayerError("emotion_conflict") from None
        finally:
            connection.close()

    def _seed(self, connection, identity):
        inserted = connection.execute("INSERT OR IGNORE INTO emotion_layer_settings(owner_id,model_id) VALUES (?,?)", identity)
        if inserted.rowcount:
            for name in ("喜", "怒", "哀", "乐"):
                connection.execute("INSERT INTO emotion_layer_terms VALUES (?,?,?,?,?,'active',?,?)",
                    (*identity, uuid.uuid4().hex, name, "", _now(), _now()))

    def _terms(self, connection, identity, emotions):
        if not isinstance(emotions, list) or any(not isinstance(value, str) or not value.strip() for value in emotions):
            raise EmotionLayerError("emotions_array_required")
        rows = []
        for value in dict.fromkeys(emotions):
            matches = connection.execute("SELECT * FROM emotion_layer_terms WHERE owner_id=? AND model_id=? AND (emotion_id=? OR name=?)",
                (*identity, value, value)).fetchall()
            if len(matches) != 1:
                raise EmotionLayerError("emotion_not_found_or_ambiguous")
            rows.append(matches[0])
        return rows

    def list(self, *, identity, guard):
        with self.connection() as connection:
            guard(connection)
            self._seed(connection, identity)
            rows = connection.execute("SELECT emotion_id AS id,name,description,state,created_at,updated_at FROM emotion_layer_terms WHERE owner_id=? AND model_id=? ORDER BY created_at,emotion_id", identity).fetchall()
            settings = dict(connection.execute("SELECT secondary_limit,cooldown_rounds FROM emotion_layer_settings WHERE owner_id=? AND model_id=?", identity).fetchone())
            tagged = connection.execute("SELECT count(*) FROM emotion_layer_tags WHERE owner_id=? AND model_id=?", identity).fetchone()[0]
            surfaced = connection.execute("SELECT count(*) FROM emotion_layer_seen WHERE owner_id=? AND model_id=?", identity).fetchone()[0]
            tagged_memories = connection.execute("SELECT count(*) FROM (SELECT module,memory_id FROM emotion_layer_tags WHERE owner_id=? AND model_id=? GROUP BY module,memory_id)", identity).fetchone()[0]
            memory_count = 0
            for table in ("emotion_memories", "learning_items", "planning_items", "tool_cards", "work_memory_items"):
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    memory_count += connection.execute(f"SELECT count(*) FROM {table} WHERE owner_id=? AND model_id=?", identity).fetchone()[0]
            counters = connection.execute("SELECT associated_wakes,cooldown_skips FROM emotion_layer_metrics WHERE owner_id=? AND model_id=?", identity).fetchone()
            rounds = connection.execute("SELECT round_no FROM emotion_layer_settings WHERE owner_id=? AND model_id=?", identity).fetchone()[0]
            return {"contract": CONTRACT, "emotions": [dict(row) for row in rows], "settings": settings,
                    "metrics": {"term_count": len(rows), "tag_count": tagged, "associated_memory_count": surfaced,
                                "tagged_memory_count": tagged_memories, "ordinary_memory_count": memory_count,
                                "tag_usage_rate": tagged_memories / memory_count if memory_count else 0,
                                "association_hit_rate": counters[0] / rounds if counters and rounds else 0,
                                "cooldown_skips": counters[1] if counters else 0}, "guide": GUIDE}

    def manage(self, *, identity, guard, action, emotion=None, name=None, description=None, secondary_limit=None, cooldown_rounds=None):
        with self.connection() as connection:
            guard(connection)
            self._seed(connection, identity)
            if action == "create":
                name, description = _validate(name, "" if description is None else description)
                emotion_id = uuid.uuid4().hex
                connection.execute("INSERT INTO emotion_layer_terms VALUES (?,?,?,?,?,'active',?,?)",
                    (*identity, emotion_id, name, description, _now(), _now()))
            elif action == "settings":
                for value, maximum in ((secondary_limit, 3), (cooldown_rounds, 10000)):
                    if value is not None and (type(value) is not int or not 0 <= value <= maximum):
                        raise EmotionLayerError("emotion_settings_invalid")
                connection.execute("UPDATE emotion_layer_settings SET secondary_limit=coalesce(?,secondary_limit),cooldown_rounds=coalesce(?,cooldown_rounds) WHERE owner_id=? AND model_id=?",
                    (secondary_limit, cooldown_rounds, *identity))
                emotion_id = None
            else:
                row = self._terms(connection, identity, [emotion])[0]
                emotion_id = row["emotion_id"]
                if action == "delete":
                    connection.execute("DELETE FROM emotion_layer_terms WHERE owner_id=? AND model_id=? AND emotion_id=?", (*identity, emotion_id))
                elif action in {"update", "archive", "restore"}:
                    name, description = _validate(row["name"] if name is None else name, row["description"] if description is None else description)
                    state = {"archive": "archived", "restore": "active"}.get(action, row["state"])
                    connection.execute("UPDATE emotion_layer_terms SET name=?,description=?,state=?,updated_at=? WHERE owner_id=? AND model_id=? AND emotion_id=?",
                        (name, description, state, _now(), *identity, emotion_id))
                else:
                    raise EmotionLayerError("emotion_action_invalid")
            return {"contract": CONTRACT, "decision": action, "id": emotion_id, "state_changed": True, "guide": GUIDE}

    def endpoint(self, connection, identity, module, memory_id, *, query=None, recall=False):
        if module == "emotion":
            row = connection.execute("SELECT * FROM emotion_memories WHERE owner_id=? AND model_id=? AND memory_id=?", (*identity, memory_id)).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["allow_contexts"] = json.loads(item["allow_contexts_json"])
            item["deny_contexts"] = json.loads(item["deny_contexts_json"])
            item["state"] = item["lifecycle"]
            if recall and item.get("default_decision") in {"ask_first", "defer"}:
                return None
        elif module == "learning":
            row = connection.execute("SELECT * FROM learning_items WHERE owner_id=? AND model_id=? AND learning_id=?", (*identity, memory_id)).fetchone()
            if row is None:
                return None
            item = json.loads(row["current_json"])
            from .learning_memory import _claim_review_status, _source_basis
            if recall and _claim_review_status(item) != "ordinary":
                return None
            if recall and _source_basis(item) in {"reported", "inferred"} and item.get("confidence") is not None and item["confidence"] < 50:
                return None
            item.update(current_version=row["current_version"], state=row["lifecycle"])
        elif module == "plan":
            row = connection.execute("SELECT i.current_version,i.recall_lifecycle,v.content_json FROM planning_items i JOIN planning_versions v ON v.plan_id=i.plan_id AND v.version=i.current_version WHERE i.owner_id=? AND i.model_id=? AND i.plan_id=?", (*identity, memory_id)).fetchone()
            if row is None:
                return None
            item = json.loads(row["content_json"])
            item.update(current_version=row["current_version"], lifecycle=row["recall_lifecycle"])
            from .planning_memory import PlanningMemoryStore, fold_plan_events
            state = fold_plan_events(PlanningMemoryStore._event_rows(connection, *identity, memory_id))
            if state.get("state") != "active" and recall:
                return None
            item["state"] = item["lifecycle"]
        elif module == "tool-card":
            row = connection.execute("SELECT c.current_version,c.lifecycle AS state,v.content_json FROM tool_cards c JOIN tool_card_versions v ON v.card_id=c.card_id AND v.version=c.current_version WHERE c.owner_id=? AND c.model_id=? AND c.card_id=?", (*identity, memory_id)).fetchone()
            item = dict(row) if row else {}
            content = json.loads(item.pop("content_json")) if row else {}
            if recall and (content.get("lifecycle") != "active" or content.get("auto_recall_mode") == "never_auto"):
                return None
        elif module == "work":
            row = connection.execute("SELECT i.current_version,coalesce(l.lifecycle,'active') AS state FROM work_memory_items i LEFT JOIN work_memory_lifecycle_versions l ON l.owner_id=i.owner_id AND l.model_id=i.model_id AND l.work_id=i.work_id AND l.version=i.current_version WHERE i.owner_id=? AND i.model_id=? AND i.work_id=?", (*identity, memory_id)).fetchone()
            item = dict(row) if row else {}
        else:
            return None
        if item.get("state") not in {"active", "archived"}:
            return None
        if recall and (item.get("state") != "active" or item.get("sensitivity") in {"intimate", "restricted"} or item.get("context_policy", "normal") != "normal" or item.get("recall_mode", "normal") != "normal"):
            return None
        if recall and query is not None:
            # Learning's native gate ignores all whitespace, not only case.
            # Associations must use the same policy comparison as ordinary
            # learning recall (including its empty-value handling).
            if module == "learning":
                from .learning_memory import _normalized as normalize_context
            else:
                normalize_context = str.casefold
            folded = normalize_context(query)
            if any(normalize_context(value) in folded for value in item.get("deny_contexts", []) if value):
                return None
            allowed = item.get("allow_contexts", [])
            if allowed and not any(normalize_context(value) in folded for value in allowed if value):
                return None
        return item

    def _tag(self, connection, identity, memory_ref, emotions, action):
        self._seed(connection, identity)
        module, memory_id, version = parse_ref(memory_ref)
        endpoint = self.endpoint(connection, identity, module, memory_id)
        if endpoint is None or endpoint["current_version"] != version:
            raise EmotionLayerError("emotion_memory_unavailable_or_stale")
        terms = self._terms(connection, identity, emotions)
        if action not in {"add", "remove", "replace"}:
            raise EmotionLayerError("emotion_tag_action_invalid")
        if action == "replace":
            connection.execute("DELETE FROM emotion_layer_tags WHERE owner_id=? AND model_id=? AND module=? AND memory_id=?", (*identity, module, memory_id))
        for row in terms:
            args = (*identity, module, memory_id, row["emotion_id"])
            if action == "remove":
                connection.execute("DELETE FROM emotion_layer_tags WHERE owner_id=? AND model_id=? AND module=? AND memory_id=? AND emotion_id=?", args)
            else:
                if row["state"] != "active":
                    raise EmotionLayerError("emotion_archived")
                connection.execute("INSERT OR IGNORE INTO emotion_layer_tags VALUES (?,?,?,?,?)", args)
        return {"decision": "tagged", "memory_ref": memory_ref, "state_changed": True}

    def tag(self, *, identity, guard, memory_ref, emotions, action="add"):
        with self.connection() as connection:
            guard(connection)
            return self._tag(connection, identity, memory_ref, emotions, action)

    @staticmethod
    def _main_hits(dynamic, *, include_unranked=False):
        hits = []
        for module, payload in dynamic.items():
            if module not in {"emotional_memory", "learning_memory", "planning_memory", "tool_guidance"}:
                continue
            items = payload if isinstance(payload, list) else payload.get("memories", payload.get("envelopes", []))
            for item in items:
                if not isinstance(item, dict) or (not include_unranked and item.get("presentation") in {"neutral_hint", "author_reminder"}):
                    continue
                ref = item.get("item_ref") or item.get("card_ref")
                if module == "emotional_memory" and isinstance(item.get("memory_id"), str):
                    ref = "emotion://" + item["memory_id"] + "@1"
                if isinstance(ref, str) and REFERENCE.fullmatch(ref):
                    score = item.get("score", item.get("semantic_score", 0))
                    if module == "planning_memory":
                        score = max(score, 0.75 * item.get("temporal_score", 0))
                    if isinstance(score, (int, float)) and not isinstance(score, bool) and (score > 0 or include_unranked):
                        hits.append((score, ref))
        return sorted(hits, key=lambda item: (-item[0], item[1]))

    def append_recall(self, connection, *, owner_id, model_id, wake_id, query, dynamic, enabled, estimate_tokens, budget=1200, tool_candidates=()):
        identity = (owner_id, model_id)
        self._seed(connection, identity)
        previous = connection.execute("SELECT payload_json FROM emotion_layer_wakes WHERE owner_id=? AND model_id=? AND wake_id=?", (*identity, wake_id)).fetchone()
        if previous is not None:
            payload = json.loads(previous[0])
            proposed = {**dynamic, "emotion_layer": payload} if payload else dynamic
            return proposed if estimate_tokens(proposed) <= budget else dynamic
        connection.execute("UPDATE emotion_layer_settings SET round_no=round_no+1 WHERE owner_id=? AND model_id=?", identity)
        settings = connection.execute("SELECT * FROM emotion_layer_settings WHERE owner_id=? AND model_id=?", identity).fetchone()
        result, selected, primary_terms, cooldown_skipped = dynamic, [], [], set()
        gated_tools = {item.get("item_ref"): item for item in tool_candidates if isinstance(item, dict)}
        hits = self._main_hits(dynamic)
        if hits:
            primary_module, primary_id, _ = parse_ref(hits[0][1])
            primary = self.endpoint(connection, identity, primary_module, primary_id, query=query, recall=True)
            if primary is not None and enabled(SCOPES[primary_module]):
                terms = connection.execute("SELECT e.* FROM emotion_layer_terms e JOIN emotion_layer_tags t ON e.owner_id=t.owner_id AND e.model_id=t.model_id AND e.emotion_id=t.emotion_id WHERE t.owner_id=? AND t.model_id=? AND t.module=? AND t.memory_id=? AND e.state='active' ORDER BY e.emotion_id", (*identity, primary_module, primary_id)).fetchall()
                candidates = {}
                existing = {parse_ref(ref)[:2] for _, ref in self._main_hits(dynamic, include_unranked=True)}
                def projection(memories, terms=primary_terms):
                    return {"contract": CONTRACT, "source": "同情绪联想摘要", "primary_ref": f"{primary_module}://{primary_id}@{primary['current_version']}",
                            "primary_emotions": list(terms), "memories": memories,
                            "frame": {"instruction_authority": "none", "permission_authority": "none", "optional": True}}
                for term in terms:
                    definition = {"name": term["name"], "description": term["description"]}
                    if contains_credential_or_secret(definition):
                        continue
                    proposed = {**dynamic, "emotion_layer": projection([], [*primary_terms, definition])}
                    if estimate_tokens(proposed) <= budget:
                        primary_terms.append(definition)
                        result = proposed
                    if not settings["secondary_limit"]:
                        continue
                    rows = connection.execute("SELECT t.module,t.memory_id,s.last_round FROM emotion_layer_tags t LEFT JOIN emotion_layer_seen s ON s.owner_id=t.owner_id AND s.model_id=t.model_id AND s.module=t.module AND s.memory_id=t.memory_id WHERE t.owner_id=? AND t.model_id=? AND t.emotion_id=? ORDER BY coalesce(s.last_round,-1),t.module,t.memory_id LIMIT 1024", (*identity, term["emotion_id"])).fetchall()
                    for row in rows:
                        key = (row["module"], row["memory_id"])
                        if key in existing or key[0] == "work" or not enabled(SCOPES[key[0]]):
                            continue
                        if row["last_round"] is not None and settings["round_no"] - row["last_round"] <= settings["cooldown_rounds"]:
                            cooldown_skipped.add(key)
                            continue
                        item = self.endpoint(connection, identity, *key, query=query, recall=True)
                        if item is not None and key[0] == "tool-card":
                            gated = gated_tools.get(f"tool-card://{key[1]}@{item['current_version']}", {})
                            item["summary"] = gated.get("content", {}).get("scene_summary")
                        if item is None or not isinstance(item.get("summary"), str) or not item["summary"].strip():
                            continue
                        summary = item["summary"][:160]
                        score = SequenceMatcher(None, query.casefold(), summary.casefold(), autojunk=False).ratio()
                        candidate = candidates.setdefault(key, {"score": score, "last": row["last_round"] if row["last_round"] is not None else -1,
                            "memory_ref": f"{key[0]}://{key[1]}@{item['current_version']}", "summary": summary, "emotions": []})
                        candidate["emotions"].append({"name": term["name"], "description": term["description"]})
                for key, candidate in sorted(candidates.items(), key=lambda entry: (-entry[1]["score"], entry[1]["last"], entry[0])):
                    item = {key: candidate[key] for key in ("memory_ref", "summary", "emotions")}
                    if contains_credential_or_secret(item):
                        continue
                    proposed = {**dynamic, "emotion_layer": projection([*selected, item])}
                    if estimate_tokens(proposed) > budget:
                        continue
                    result = proposed
                    selected.append(item)
                    connection.execute("INSERT INTO emotion_layer_seen VALUES (?,?,?,?,?) ON CONFLICT(owner_id,model_id,module,memory_id) DO UPDATE SET last_round=excluded.last_round", (*identity, *key, settings["round_no"]))
                    if len(selected) >= settings["secondary_limit"]:
                        break
        connection.execute("INSERT INTO emotion_layer_wakes VALUES (?,?,?,?,?)", (*identity, wake_id, settings["round_no"], json.dumps(result.get("emotion_layer"), ensure_ascii=False)))
        connection.execute("INSERT INTO emotion_layer_metrics VALUES (?,?,?,?) ON CONFLICT(owner_id,model_id) DO UPDATE SET associated_wakes=associated_wakes+excluded.associated_wakes,cooldown_skips=cooldown_skips+excluded.cooldown_skips", (*identity, int(bool(selected)), len(cooldown_skipped)))
        # The canonical brain_context_snapshots owns long-lived wake reuse. This
        # small intra-build ledger only protects repeated decoration in one build.
        connection.execute("DELETE FROM emotion_layer_wakes WHERE owner_id=? AND model_id=? AND round_no < ?", (*identity, settings["round_no"] - 255))
        return result
