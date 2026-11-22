"""AI-owned literal-cue priority; metadata and receipts never rewrite a memory."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

from .emotion_layer import EmotionLayerError, parse_ref
from .memory_relations import _ddl

SCOPES = {"emotion": "emotional_memory", "learning": "learning_memory",
          "plan": "planning_memory", "tool-card": "tool_guidance"}
GUIDE = ("关键词必浮现默认关闭，仅所属 AI 管理，普通四脑合计最多10条；到顶先关闭旧项。"
         "只按本轮新人类正文的明确关键词匹配，英文按词边界，中文按词句，不扫描旧历史或工具回声。"
         "优先注入允许披露的短摘要；禁召、模块关闭、隔离和先询问仍有效，不授予执行权限。"
         "超过预算保住当前最后明确提及的条目，其余按重要度；显示省略数量，原文不变、仍可精确查询。"
         "hit_count 是已确认上下文中的命中次数，injected_count 是实际纳入并经宿主确认的次数；"
         "总浮现关闭时不扫描；单条/模块被挡时会记录已确认命中及未注入原因。"
         "预览、工具续轮及重试不重复计数。它们不证明模型已理解或已正确回答。")
SETTING_SQL = """CREATE TABLE IF NOT EXISTS memory_force_recall (
 owner_id TEXT NOT NULL, model_id TEXT NOT NULL, module TEXT NOT NULL, memory_id TEXT NOT NULL,
 force_recall INTEGER NOT NULL DEFAULT 0 CHECK(force_recall IN (0,1)),
 revision INTEGER NOT NULL CHECK(revision>=1), updated_at TEXT NOT NULL,
 PRIMARY KEY(owner_id,model_id,module,memory_id))"""
HISTORY_SQL = """CREATE TABLE IF NOT EXISTS memory_force_recall_history (
 owner_id TEXT NOT NULL, model_id TEXT NOT NULL, module TEXT NOT NULL, memory_id TEXT NOT NULL,
 revision INTEGER NOT NULL, force_recall INTEGER NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(owner_id,model_id,module,memory_id,revision))"""
RECEIPT_SQL = """CREATE TABLE IF NOT EXISTS memory_force_recall_receipts (
 owner_id TEXT NOT NULL, model_id TEXT NOT NULL, source_key TEXT NOT NULL,
 module TEXT NOT NULL, memory_id TEXT NOT NULL, wake_id TEXT NOT NULL,
 matched_at TEXT NOT NULL, selected INTEGER NOT NULL CHECK(selected IN (0,1)),
 reason TEXT NOT NULL, confirmed_at TEXT,
 PRIMARY KEY(owner_id,model_id,source_key,module,memory_id))"""
DDL = {"memory_force_recall": SETTING_SQL, "memory_force_recall_history": HISTORY_SQL,
       "memory_force_recall_receipts": RECEIPT_SQL}


class ForceRecallError(EmotionLayerError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _parse(ref):
    # An unavailable parent's list entry has no invented content version.
    # It can still be read or disabled to release its explicit slot.
    bare = isinstance(ref, str) and "@" not in ref
    module, memory_id, version = parse_ref(ref + "@1" if bare else ref)
    if module not in SCOPES:
        raise ForceRecallError("force_recall_ordinary_brains_only")
    return module, memory_id, 0 if bare else version


def literal_position(text, keywords):
    """Last explicit mention in this input, never a semantic/substring score."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    position = -1
    for keyword in keywords:
        needle = unicodedata.normalize("NFKC", keyword).casefold().strip()
        if not needle:
            continue
        if re.search(r"[\u3400-\u9fff]", needle):
            position = max(position, folded.rfind(needle))
        else:
            # CJK neighbours are not part of a Latin identifier; Unicode
            # letters/digits/underscore remain boundaries for all other text.
            boundary = r"[^\W\u3400-\u9fff]"
            for match in re.finditer(rf"(?<!{boundary}){re.escape(needle)}(?!{boundary})", folded):
                position = max(position, match.start())
    return position


def _target(db, identity, module, memory_id):
    if module == "emotion":
        row = db.execute("SELECT * FROM emotion_memories WHERE owner_id=? AND model_id=? AND memory_id=?",
                         (*identity, memory_id)).fetchone()
        if row is None:
            return None
        item = dict(row)
        for name in ("keywords", "allow_contexts", "deny_contexts"):
            item[name] = json.loads(item[name + "_json"])
    elif module == "learning":
        row = db.execute("SELECT * FROM learning_items WHERE owner_id=? AND model_id=? AND learning_id=?",
                         (*identity, memory_id)).fetchone()
        if row is None:
            return None
        item = {**json.loads(row["current_json"]), "current_version": row["current_version"],
                "lifecycle": row["lifecycle"]}
    elif module == "plan":
        row = db.execute("SELECT i.current_version,i.recall_lifecycle,v.content_json FROM planning_items i "
                         "JOIN planning_versions v ON v.plan_id=i.plan_id AND v.version=i.current_version "
                         "WHERE i.owner_id=? AND i.model_id=? AND i.plan_id=?", (*identity, memory_id)).fetchone()
        if row is None:
            return None
        from .planning_memory import PlanningMemoryStore, fold_plan_events
        item = {**json.loads(row["content_json"]), "current_version": row["current_version"],
                "lifecycle": row["recall_lifecycle"],
                "plan_state": fold_plan_events(PlanningMemoryStore._event_rows(db, *identity, memory_id))["state"]}
    elif module == "tool-card":
        row = db.execute("SELECT c.current_version,c.lifecycle,v.content_json FROM tool_cards c "
                         "JOIN tool_card_versions v ON v.card_id=c.card_id AND v.version=c.current_version "
                         "WHERE c.owner_id=? AND c.model_id=? AND c.card_id=?", (*identity, memory_id)).fetchone()
        if row is None:
            return None
        item = {**json.loads(row["content_json"]), "current_version": row["current_version"],
                "card_lifecycle": row["lifecycle"]}
    else:
        raise ForceRecallError("force_recall_ordinary_brains_only")
    item["item_ref"] = f"{module}://{memory_id}@{item['current_version']}"
    return item


def _blocked(module, item, query=None):
    if item is None:
        return "memory_unavailable"
    if item.get("lifecycle") != "active" or item.get("card_lifecycle", "active") != "active":
        return "memory_inactive"
    if item.get("recall_mode") == "never" or item.get("auto_recall_mode") == "never_auto":
        return "recall_disabled"
    if item.get("context_policy") == "never_auto" or item.get("default_decision") == "defer":
        return "context_policy_disabled"
    if module == "learning":
        from .learning_memory import _claim_review_status
        if _claim_review_status(item) != "ordinary":
            return "claim_review_required"
        if item.get("context_policy") == "ask_first":
            return "ask_first"
    if module == "plan" and item["plan_state"] not in {"active", "paused"}:
        return "plan_not_active_or_paused"
    if module == "tool-card" and item.get("expires_at"):
        from .tool_guidance import _card_expired
        if _card_expired(item["expires_at"]):
            return "tool_card_expired"
    if query is not None:
        if module == "learning":
            from .learning_memory import _normalized as normalize
        else:
            normalize = str.casefold
        folded = normalize(query)
        if any(normalize(s) in folded for s in item.get("deny_contexts", []) if s):
            return "denied_context"
        if item.get("allow_contexts") and not any(normalize(s) in folded for s in item["allow_contexts"] if s):
            return "allowed_context_not_matched"
    return None


def _projection(module, item):
    """Only authored summaries/reminders, with original disclosure qualifiers."""
    presentation = "summary"
    text = item.get("summary", "")
    if module == "tool-card":
        text = item.get("reminder") or item.get("purpose", "")
    if module == "emotion" and (item.get("context_policy") in {"ask_first", "neutral_hint"}
            or item.get("default_decision") == "ask_first" or item.get("sensitivity") in {"intimate", "restricted"}):
        text = "有相关记忆；仅提示存在，展开前遵守原披露设置。"
        presentation = "neutral_hint"
    entry = {"item_ref": item["item_ref"], "presentation": presentation, "text": text[:80]}
    if module == "emotion":
        from .emotional_memory import _origin_label
        entry.update(origin_label=_origin_label(item["origin"], item["confidence"]),
                     confidence=item["confidence"], gate_decision=item["default_decision"])
        if presentation == "neutral_hint":
            entry["gate_decision"] = "ask_first"
    if len(text) > 80:
        entry["excerpt"] = True
    if module == "learning":
        from .learning_memory import _source_basis
        entry.update(source_basis=_source_basis(item), confidence=item.get("confidence"),
                     epistemic_status=item.get("epistemic_status"))
        if item.get("context_policy") == "neutral_hint" or (_source_basis(item) in {"reported", "inferred"}
                and item.get("confidence") is not None and item["confidence"] < 50):
            entry["presentation"] = "neutral_hint"
    if module == "plan":
        entry["state"] = item["plan_state"]
    return entry


class ForceRecallStore:
    def __init__(self, database):
        self.database = str(Path(database).resolve())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for sql in DDL.values():
                db.execute(sql)
            self._schema(db)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _schema(db):
        for name, sql in DDL.items():
            row = db.execute("SELECT sql FROM sqlite_master WHERE name=? AND type='table'", (name,)).fetchone()
            if row is None or _ddl(row[0]) != _ddl(sql):
                raise ForceRecallError("force_recall_storage_unavailable")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?,?,?)", tuple(DDL)).fetchone():
            raise ForceRecallError("force_recall_storage_unavailable")

    def _view(self, db, identity, module, memory_id, row=None):
        item = _target(db, identity, module, memory_id)
        if item is None and row is None:
            raise ForceRecallError("memory_unavailable")
        stats = db.execute("SELECT count(*) AS hits,coalesce(sum(selected),0) AS injected,max(matched_at) AS last_hit,"
                           "max(CASE WHEN selected=1 THEN confirmed_at END) AS last_injected "
                           "FROM memory_force_recall_receipts WHERE owner_id=? AND model_id=? AND module=? AND memory_id=? "
                           "AND confirmed_at IS NOT NULL", (*identity, module, memory_id)).fetchone()
        last = db.execute("SELECT reason FROM memory_force_recall_receipts WHERE owner_id=? AND model_id=? AND module=? "
                          "AND memory_id=? AND confirmed_at IS NOT NULL ORDER BY matched_at DESC LIMIT 1",
                          (*identity, module, memory_id)).fetchone()
        return {"target_ref": item["item_ref"] if item else f"{module}://{memory_id}",
                "force_recall": bool(row and row["force_recall"]), "revision": row["revision"] if row else 0,
                "policy_block_reason": _blocked(module, item), "hit_count": stats["hits"],
                "injected_count": stats["injected"], "last_hit_at": stats["last_hit"],
                "last_injected_at": stats["last_injected"], "last_delivery_reason": last[0] if last else None}

    def manage(self, *, identity, guard, action, target_ref=None, expected_revision=None):
        if action not in {"enable", "disable", "read", "list"}:
            raise ForceRecallError("force_recall_action_invalid")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE" if action in {"enable", "disable"} else "BEGIN")
            self._schema(db)
            guard(db)
            rows = db.execute("SELECT * FROM memory_force_recall WHERE owner_id=? AND model_id=?", identity).fetchall()
            count = sum(r["force_recall"] for r in rows)
            if action == "list":
                return {"decision": "listed", "state_changed": False, "limit": 10, "enabled_count": count,
                        "items": [self._view(db, identity, r["module"], r["memory_id"], r)
                                  for r in rows if r["force_recall"]], "guide": GUIDE}
            module, memory_id, version = _parse(target_ref)
            row = next((r for r in rows if (r["module"], r["memory_id"]) == (module, memory_id)), None)
            item = _target(db, identity, module, memory_id)
            if item is None and not (row is not None and action in {"read", "disable"}):
                raise ForceRecallError("memory_unavailable")
            if action == "read":
                return {"decision": "read", "state_changed": False, "item": self._view(db, identity, module, memory_id, row), "guide": GUIDE}
            if item is not None and version != item["current_version"]:
                raise ForceRecallError("force_recall_memory_version_conflict")
            if expected_revision is not None and (type(expected_revision) is not int or expected_revision != (row["revision"] if row else 0)):
                raise ForceRecallError("force_recall_revision_conflict")
            enabled = action == "enable"
            if enabled and not any(isinstance(k, str) and k.strip() for k in item.get("keywords", [])):
                raise ForceRecallError("force_recall_keywords_required")
            if bool(row and row["force_recall"]) == enabled:
                return {"decision": "unchanged", "state_changed": False, "enabled_count": count,
                        "item": self._view(db, identity, module, memory_id, row), "guide": GUIDE}
            if enabled and count >= 10:
                raise ForceRecallError("force_recall_limit_disable_one_first")
            revision, now = (row["revision"] + 1 if row else 1), _now()
            db.execute("INSERT INTO memory_force_recall VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_id,model_id,module,memory_id) "
                       "DO UPDATE SET force_recall=excluded.force_recall,revision=excluded.revision,updated_at=excluded.updated_at",
                       (*identity, module, memory_id, int(enabled), revision, now))
            db.execute("INSERT INTO memory_force_recall_history VALUES(?,?,?,?,?,?,?)",
                       (*identity, module, memory_id, revision, int(enabled), now))
            current = {"force_recall": enabled, "revision": revision}
            return {"decision": "saved", "state_changed": True, "parent_memory_changed": False,
                    "enabled_count": count + (1 if enabled else -1),
                    "item": self._view(db, identity, module, memory_id, current), "guide": GUIDE}

    def candidates(self, db, *, identity, query, scope_enabled):
        self._schema(db)
        result = []
        for row in db.execute("SELECT * FROM memory_force_recall WHERE owner_id=? AND model_id=? AND force_recall=1", identity):
            module, memory_id = row["module"], row["memory_id"]
            item = _target(db, identity, module, memory_id)
            if item is None:
                continue
            position = literal_position(query, item.get("keywords", []))
            if position < 0:
                continue
            reason = "module_disabled" if not scope_enabled(SCOPES[module]) else _blocked(module, item, query)
            result.append({"module": module, "memory_id": memory_id, "ref": item["item_ref"],
                           "position": position, "importance": item.get("importance", item.get("salience", 50)),
                           "blocked": reason, "entry": None if reason else _projection(module, item)})
        # The most recently mentioned current cue is first; remaining matches
        # use importance, never last-hit counters (which would self-reinforce).
        if result:
            recent = max([x for x in result if x["entry"] is not None] or result,
                         key=lambda x: (x["position"], x["importance"], x["ref"]))
            result.sort(key=lambda x: (x is not recent, -x["importance"], x["ref"]))
        return result

    def record_prepared(self, db, *, identity, wake_id, source_event_id, candidates, dynamic):
        if not candidates:
            return
        selected = delivered_refs(dynamic)
        source_key = hashlib.sha256(source_event_id.encode()).hexdigest()
        for item in candidates:
            delivered = item["ref"] in selected
            db.execute("INSERT INTO memory_force_recall_receipts VALUES(?,?,?,?,?,?,?,?,?,NULL) "
                       "ON CONFLICT(owner_id,model_id,source_key,module,memory_id) DO UPDATE SET "
                       "wake_id=excluded.wake_id,matched_at=excluded.matched_at,selected=excluded.selected,reason=excluded.reason "
                       "WHERE memory_force_recall_receipts.confirmed_at IS NULL",
                       (*identity, source_key, item["module"], item["memory_id"], wake_id, _now(), int(delivered),
                        "selected" if delivered else item["blocked"] or "budget_omitted"))

    @staticmethod
    def confirm(db, *, identity, wake_id):
        db.execute("UPDATE memory_force_recall_receipts SET confirmed_at=? WHERE owner_id=? AND model_id=? "
                   "AND wake_id=? AND confirmed_at IS NULL", (_now(), *identity, wake_id))


def delivered_refs(dynamic):
    return ({x["item_ref"] for x in dynamic.get("force_recall", {}).get("items", [])}
            | {x["item_ref"] for x in dynamic.get("planning_memory", {}).get("envelopes", [])})


def select_forced(dynamic, candidates, *, estimate_tokens, budget):
    present = delivered_refs(dynamic)
    eligible = [x for x in candidates if x["entry"] is not None and x["ref"] not in present]
    if not eligible:
        return dynamic
    payload = {"items": [], "omitted_count": len(eligible), "instruction_authority": "none", "execution_authority": "none", "permission_authority": "none",
               "notice": "关键词必浮现；省略项可用 memory_force_recall 查询，原文按引用读取。"}
    result = {**dynamic, "force_recall": payload}
    if estimate_tokens(result) > budget:
        # The caller exposes a host diagnostic; never report a memory delivered.
        return dynamic
    selected = []
    for item in eligible:
        entry = item["entry"]
        proposed = {**payload, "items": [*selected, entry], "omitted_count": len(eligible) - len(selected) - 1}
        if estimate_tokens({**dynamic, "force_recall": proposed}) > budget and not selected:
            # Keep the latest hit's ref and an explicitly marked exact excerpt.
            entry = {**entry, "text": entry["text"][:12], "excerpt": True}
            proposed["items"] = [entry]
        if estimate_tokens({**dynamic, "force_recall": proposed}) <= budget:
            selected.append(entry)
            payload = proposed
            result = {**dynamic, "force_recall": payload}
    return result
