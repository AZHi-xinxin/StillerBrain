"""Bounded, summary-only cross-window continuity with delivery receipts.

This is not a semantic-recall replacement. A switch nominates the latest three
eligible versions, independent of relevance. Preparation/confirmation never
acknowledges them; only an authenticated host's completed-delivery receipt does.
The private cursor contains hashes/sequence numbers, not memory bodies.
"""
from __future__ import annotations

import hashlib
import json
import re


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_frame(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"source_key", "public_model", "upstream_model"}:
        raise ValueError("continuity_frame_invalid")
    if not isinstance(value["source_key"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["source_key"]):
        raise ValueError("continuity_source_invalid")
    for key in ("public_model", "upstream_model"):
        if (not isinstance(value[key], str) or not value[key].strip()
                or len(value[key]) > 256 or any(ord(c) < 32 for c in value[key])):
            raise ValueError("continuity_model_invalid")
    return dict(value)


def initialize(connection):
    # execute, not executescript: never implicitly commit a caller transaction.
    for sql in (
        "CREATE TABLE IF NOT EXISTS continuity_versions (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "owner_id TEXT NOT NULL, model_id TEXT NOT NULL, ref_hash TEXT NOT NULL, "
        "UNIQUE(owner_id,model_id,ref_hash))",
        "CREATE TABLE IF NOT EXISTS continuity_heads (owner_id TEXT NOT NULL, model_id TEXT NOT NULL, "
        "source_key TEXT NOT NULL, route_json TEXT NOT NULL, wake_seq INTEGER NOT NULL, "
        "baseline_seq INTEGER NOT NULL, PRIMARY KEY(owner_id,model_id))",
        "CREATE TABLE IF NOT EXISTS continuity_windows (owner_id TEXT NOT NULL, model_id TEXT NOT NULL, "
        "source_key TEXT NOT NULL, cutoff_seq INTEGER NOT NULL, pending_json TEXT NOT NULL, "
        "PRIMARY KEY(owner_id,model_id,source_key))",
        "CREATE TABLE IF NOT EXISTS continuity_proposals (wake_id TEXT PRIMARY KEY, "
        "proposal_json TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0)",
    ):
        connection.execute(sql)


def candidates(store, connection, owner_id, model_id, query, enabled, planning=None):
    """Only ordinary, unrestricted summaries; restrictive policies fail closed.

    Existing context-specific allow/deny gates are checked against the actual
    current human text, never against a synthetic query made from the memory.
    Sensitive, ask-first, neutral-only and epistemically disputed items are not
    candidates for this independent recency channel.
    """
    from .learning_memory import _claim_review_status, _source_basis, _source_confidence_label
    from .emotional_memory import _normalized as emotional_normalized
    from .tool_guidance import _reminder_text

    result = []

    def add(ref, updated, summary, module, **extra):
        if isinstance(summary, str) and summary.strip():
            result.append({"ref_hash": _hash(ref), "updated": updated,
                           "envelope": {"item_ref": ref, "module": module,
                                        "summary": summary, "updated_at": updated, **extra}})

    if store.emotional_store is not None and enabled("emotional_memory"):
        for row in connection.execute(
            "SELECT * FROM emotion_memories WHERE owner_id=? AND model_id=? AND lifecycle='active'",
            (owner_id, model_id),
        ):
            # The ordinary daily writer uses content.strip()[:summary_chars]
            # when no separate summary is supplied. Short records therefore
            # legitimately have identical original/summary text. Equality is
            # not a privacy policy: retain the stored, bounded summary while
            # still respecting every disclosure/context gate below. Do not
            # use lossy normalization for this exception or copy original_text
            # into the envelope; prefixed full-original copies stay excluded.
            compact_summary = (row["summary"] == row["original_text"].strip()
                               and len(row["summary"]) <= store.emotional_store.limits.summary_chars)
            if (row["sensitivity"] in {"intimate", "restricted"}
                    or row["context_policy"] != "normal" or row["recall_mode"] != "normal"
                    or row["default_decision"] != "background_reference"
                    or not store.emotional_store._allowed_context(row, query)
                    or store.emotional_store._denied(row, query)
                    or (not compact_summary and emotional_normalized(row["original_text"])
                        and emotional_normalized(row["original_text"]) in emotional_normalized(row["summary"]))):
                continue
            add(f"emotion://{row['memory_id']}@{row['current_version']}", row["updated_at"],
                row["summary"], "emotional_memory", confidence=row["confidence"], origin=row["origin"])
    if store.learning_store is not None and enabled("learning_memory"):
        for row in connection.execute(
            "SELECT * FROM learning_items WHERE owner_id=? AND model_id=? AND lifecycle='active'",
            (owner_id, model_id),
        ):
            content = json.loads(row["current_json"])
            # Keep the same normalization as ordinary learning recall.
            from .learning_memory import _normalized
            q = _normalized(query)
            if (content["sensitivity"] in {"intimate", "restricted"}
                    or content["context_policy"] != "normal" or content["recall_mode"] == "never"
                    or _claim_review_status(content) != "ordinary"
                    or any(_normalized(v) in q for v in content.get("deny_contexts", []) if v)
                    or (content.get("allow_contexts") and not any(
                        _normalized(v) in q for v in content["allow_contexts"] if v))
                    or (_source_basis(content) in {"reported", "inferred"}
                        and content["confidence"] is not None and content["confidence"] < 50)):
                continue
            add(f"learning://{row['learning_id']}@{row['current_version']}", row["updated_at"],
                content["summary"], "learning_memory", uncertainties=content["uncertainties"],
                epistemic_label=content["epistemic_status"], source_basis=_source_basis(content),
                source_confidence_label=_source_confidence_label(content))
    if store.tool_store is not None and enabled("tool_guidance"):
        for card, version in store.tool_store._candidate_rows(connection, owner_id=owner_id, model_id=model_id):
            content = json.loads(version["content_json"])
            if (card["lifecycle"] != "active" or content["lifecycle"] != "active"
                    or content["auto_recall_mode"] != "normal"):
                continue
            add(f"tool-card://{card['card_id']}@{version['version']}", card["updated_at"],
                _reminder_text(content, store.tool_store.limits.summary_chars), "tool_guidance")
    # Planning keeps its existing state/presence/review gates. Only already
    # eligible projections may be nominated; recency cannot wake a paused plan.
    if store.planning_store is not None and enabled("planning_memory"):
        for envelope in (planning or {}).get("envelopes", []):
            ref = envelope["item_ref"]
            plan_id = ref.split("://", 1)[1].rsplit("@", 1)[0]
            row = connection.execute("SELECT updated_at FROM planning_items WHERE owner_id=? AND model_id=? AND plan_id=?",
                                     (owner_id, model_id, plan_id)).fetchone()
            if row is None:
                continue
            add(ref, row["updated_at"], envelope["content"]["summary"], "planning_memory",
                state=envelope["state"], due_hint=envelope["content"]["due_hint"])
    return sorted(result, key=lambda item: (item["updated"], item["ref_hash"]))


def needs_candidates(connection, owner_id, model_id, frame):
    """The ordinary same-window path never rescans the memory library."""
    head = connection.execute("SELECT source_key FROM continuity_heads WHERE owner_id=? AND model_id=?",
                              (owner_id, model_id)).fetchone()
    if head is None or head["source_key"] != frame["source_key"]:
        return True
    row = connection.execute("SELECT pending_json FROM continuity_windows WHERE owner_id=? AND model_id=? AND source_key=?",
                             (owner_id, model_id, frame["source_key"])).fetchone()
    return row is not None and bool(json.loads(row["pending_json"]))


def prepare(connection, *, owner_id, model_id, wake_id, wake_seq, frame, items, base, estimate, budget=1200):
    frame = validate_frame(frame)
    if frame is None:
        return base
    scope = (owner_id, model_id)
    for item in items:
        connection.execute("INSERT OR IGNORE INTO continuity_versions(owner_id,model_id,ref_hash) VALUES(?,?,?)",
                           (*scope, item["ref_hash"]))
    versions = dict(connection.execute(
        "SELECT ref_hash,seq FROM continuity_versions WHERE owner_id=? AND model_id=?", scope)) if items else {}
    observed = connection.execute(
        "SELECT COALESCE(MAX(seq),0) FROM continuity_versions WHERE owner_id=? AND model_id=?", scope).fetchone()[0]
    head = connection.execute("SELECT * FROM continuity_heads WHERE owner_id=? AND model_id=?", scope).fetchone()
    window = connection.execute("SELECT * FROM continuity_windows WHERE owner_id=? AND model_id=? AND source_key=?",
                                (*scope, frame["source_key"])).fetchone()
    changed = head is not None and head["source_key"] != frame["source_key"]
    route = {key: frame[key] for key in ("public_model", "upstream_model")}
    cutoff = window["cutoff_seq"] if window else (head["baseline_seq"] if head else 0)
    available = {item["ref_hash"]: item for item in items}
    pending = [ref for ref in json.loads(window["pending_json"]) if ref in available] if window else []
    if changed or head is None:
        newest = sorted((item for item in items if versions[item["ref_hash"]] > cutoff
                         and item["ref_hash"] not in pending),
                        key=lambda item: (item["updated"], versions[item["ref_hash"]]), reverse=True)[:3]
        pending.extend(item["ref_hash"] for item in newest)
        cutoff = observed
    model_frame = {"contract": "st-session-continuity/1",
                   "frame": {"instruction_authority": "none", "permission_authority": "none"},
                   "source_transition": "unknown" if head is None else "changed" if changed else "same",
                   "route": route,
                   "model_transition": "unknown" if head is None else
                       "same" if head["route_json"] == _json(route) else "changed",
                   "model_identity_basis": "configured_upstream_route_not_weight_attestation",
                   "recent_memories": []}
    output = {**base, "session_continuity": model_frame}
    accepted = []
    # Exact summaries, never raw memory bodies or truncation to fit.
    for ref in pending:
        if len(accepted) >= 3:
            break
        trial = {**model_frame, "recent_memories": [*model_frame["recent_memories"], available[ref]["envelope"]]}
        if estimate({**base, "session_continuity": trial}) <= budget:
            model_frame = trial
            output["session_continuity"] = trial
            accepted.append(ref)
    if estimate(output) > budget:
        output = base
        accepted = []
    proposal = {"source_key": frame["source_key"], "route": route, "wake_seq": wake_seq,
                "observed": observed, "cutoff": cutoff, "pending": pending, "accepted": accepted}
    connection.execute("INSERT INTO continuity_proposals(wake_id,proposal_json) VALUES(?,?)", (wake_id, _json(proposal)))
    return output


def acknowledge(connection, *, owner_id, model_id, wake_id):
    row = connection.execute("SELECT * FROM continuity_proposals WHERE wake_id=?", (wake_id,)).fetchone()
    if row is None or row["delivered"]:
        return False
    proposal = json.loads(row["proposal_json"])
    scope = (owner_id, model_id)
    head = connection.execute("SELECT * FROM continuity_heads WHERE owner_id=? AND model_id=?", scope).fetchone()
    if head is not None and head["wake_seq"] >= proposal["wake_seq"]:
        return False  # Late transport callbacks cannot regress a newer source.
    remaining = [ref for ref in proposal["pending"] if ref not in proposal["accepted"]]
    connection.execute("INSERT OR REPLACE INTO continuity_windows VALUES(?,?,?,?,?)",
        (*scope, proposal["source_key"], proposal["cutoff"], _json(remaining)))
    connection.execute("INSERT OR REPLACE INTO continuity_heads VALUES(?,?,?,?,?,?)",
        (*scope, proposal["source_key"], _json(proposal["route"]), proposal["wake_seq"],
         head["baseline_seq"] if head else proposal["observed"]))
    connection.execute("UPDATE continuity_proposals SET delivered=1 WHERE wake_id=?", (wake_id,))
    return True
