"""Synthetic pre-simplification learning candidates; never a public write path.

Keep old pending migration/review coverage without asking the current integrate
API to manufacture pending candidates. All callers use temporary test stores.
"""
import json

from runtime.learning_memory import (
    LearningMemoryError, _iso, _new_id, _now_dt, _parse_iso,
)


def legacy_integration_candidate(store, *, owner_id, model_id, wake_id, wake_seq,
                                 expected_row_version, source_learning_ids,
                                 synthesis_kind, classification_basis=None,
                                 correctness_assessment="", diff="", calm_check=None,
                                 reason="", source_action="keep", merge_suggestion_id=None,
                                 classification_actor=None, create_idea=False, **fields):
    """Write exactly a historical pending row and frozen source edges."""
    content = store._validate_content(fields)
    target_ref = f"learning://{_new_id('learn')}@0"
    with store._connect() as connection:
        store._begin(connection)
        sources, contents = [], []
        for source_id in source_learning_ids:
            row = store._item(connection, owner_id, model_id, source_id)
            contents.append(json.loads(row["current_json"]))
            sources.append({"learning_id": source_id,
                            "ref": f"learning://{source_id}@{row['current_version']}",
                            "version": row["current_version"], "hash": row["current_hash"]})
        if merge_suggestion_id:
            suggestion = connection.execute(
                "SELECT * FROM learning_merge_suggestions WHERE owner_id=? AND model_id=? "
                "AND suggestion_id=? AND status='open'", (owner_id, model_id, merge_suggestion_id)
            ).fetchone()
            if suggestion is None:
                raise LearningMemoryError("merge_suggestion_not_open")
            if _parse_iso(suggestion["expires_at"]) <= _now_dt():
                raise LearningMemoryError("merge_suggestion_expired")
            if set(json.loads(suggestion["member_refs_json"])) != {item["ref"] for item in sources}:
                raise LearningMemoryError("merge_suggestion_source_mismatch")
        candidate_id, candidate_hash, version = store._create_candidate(
            connection, owner_id=owner_id, model_id=model_id, wake_id=wake_id,
            wake_seq=wake_seq, expected_row_version=expected_row_version,
            target_ref=target_ref, base_version=0, change_class="generalization",
            classification_basis=classification_basis or [], proposed=content,
            ai_diff=diff, correctness_assessment=correctness_assessment,
            calm_check=calm_check or {}, reason=reason,
            source_snapshot={"sources": sources, "synthesis_kind": synthesis_kind,
                             "merge_suggestion_id": merge_suggestion_id},
            source_action=source_action, review_source_contents=contents,
        )
        for position, source in enumerate(sources):
            connection.execute(
                "INSERT INTO learning_integrations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (candidate_id, source["learning_id"], position, source["version"],
                 source["hash"], source_action, _iso()),
            )
    return {"decision": "candidate_pending", "candidate_id": candidate_id,
            "candidate_hash": candidate_hash, "candidate_version": 1,
            "base_version": 0, "target_ref": target_ref, "learning_row_version": version,
            "state_changed": True, "pointer_changed": False}
