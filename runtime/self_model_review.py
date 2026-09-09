"""Bounded module-one review pages; internal runtime helpers, not MCP tools.

The caller owns authentication, wake validation, stage transitions and full-review
proof issuance. These helpers never open a database, create a wake, activate a
candidate, write a file, or treat one page as a complete presentation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping


PAGE_CHARACTERS = 4000
PAGE_ARTIFACT_KIND = "candidate_review_page"


class ReviewPageError(ValueError):
    """A fixed, content-free validation reason suitable for the runtime facade."""


def _canonical(value: Any) -> str:
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False)
        result.encode("utf-8")
        return result
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReviewPageError("review_material_invalid") from exc


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True, repr=False)
class ReviewPage:
    candidate_id: str
    content_hash: str
    material_hash: str
    page: int
    pages: int
    text: str = field(repr=False)
    next_page: int | None
    _canonical_material: str = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Only page content and non-secret identifiers; never private bindings."""
        return {key: getattr(self, key) for key in (
            "candidate_id", "content_hash", "material_hash", "page", "pages", "text", "next_page"
        )}

    def __repr__(self) -> str:
        return f"ReviewPage(page={self.page}, pages={self.pages}, content_hidden=True)"


def prepare_review_page(
    material: Mapping[str, Any], page: int = 0,
    expected_material_hash: str | None = None,
) -> ReviewPage:
    """Validate and project already-sanitized, complete review material, with no IO.

    ``material`` comes from the authenticated runtime continuation in the same
    transaction. It must include ``candidate`` with its stored content and hash.
    The caller, not this helper, determines which notices are required and strips
    host-only fields. Exact canonical text can be reconstructed by joining pages.
    """
    if type(page) is not int or page < 0:
        raise ReviewPageError("review_page_invalid")
    if not isinstance(material, Mapping):
        raise ReviewPageError("review_material_invalid")
    candidate = material.get("candidate")
    if not isinstance(candidate, Mapping):
        raise ReviewPageError("review_candidate_unavailable")
    candidate_id, content_hash = candidate.get("candidate_id"), candidate.get("content_hash")
    if not isinstance(candidate_id, str) or not candidate_id.strip() or not _digest(content_hash):
        raise ReviewPageError("review_candidate_invalid")
    if not isinstance(candidate.get("content"), dict):
        raise ReviewPageError("review_candidate_invalid")
    if _hash(_canonical(candidate["content"])) != content_hash:
        raise ReviewPageError("review_candidate_content_hash_mismatch")
    for key in ("base_revision_id", "diff", "reason", "evidence_refs"):
        if key not in candidate:
            raise ReviewPageError("review_candidate_incomplete")
    canonical = _canonical(dict(material))
    material_hash = _hash(canonical)
    if page > 0 and expected_material_hash is None:
        raise ReviewPageError("review_material_hash_required")
    if expected_material_hash is not None and expected_material_hash != material_hash:
        raise ReviewPageError("review_material_hash_mismatch")
    pages = max(1, (len(canonical) + PAGE_CHARACTERS - 1) // PAGE_CHARACTERS)
    if page >= pages:
        raise ReviewPageError("review_page_out_of_range")
    start = page * PAGE_CHARACTERS
    return ReviewPage(
        candidate_id=candidate_id, content_hash=content_hash, material_hash=material_hash,
        page=page, pages=pages, text=canonical[start:start + PAGE_CHARACTERS],
        next_page=page + 1 if page + 1 < pages else None, _canonical_material=canonical,
    )


def _validated_projection(projection: ReviewPage) -> ReviewPage:
    if type(projection) is not ReviewPage:
        raise ReviewPageError("review_page_projection_invalid")
    try:
        material = json.loads(projection._canonical_material)
        rebuilt = prepare_review_page(material, projection.page, projection.material_hash)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReviewPageError("review_page_projection_invalid") from exc
    if rebuilt != projection:
        raise ReviewPageError("review_page_projection_invalid")
    return rebuilt


def record_review_page(store: Any, connection: Any, state: Mapping[str, Any],
                       wake: Mapping[str, Any], page_projection: ReviewPage) -> bool:
    """Record one metadata-only page receipt; return whether all exact pages exist.

    An authenticated onboarding transaction must already hold the current state
    and wake. Full-review and objection proofs remain the caller's responsibility.
    No transaction is started/committed here, so any caller failure rolls this back.
    """
    projection = _validated_projection(page_projection)
    if not getattr(connection, "in_transaction", False):
        raise ReviewPageError("review_bound_transaction_required")
    try:
        owner, model, wake_id = state["owner_id"], state["model_id"], wake["wake_id"]
        current = store._state_row(connection, owner, model)
        current_wake = store._wake_row(connection, wake_id)
        if (
            current is None or current_wake is None
            or not all(isinstance(value, str) and value for value in (owner, model, wake_id))
            or state["stage"] != "candidate_review" or current["stage"] != "candidate_review"
            or state["current_candidate_id"] != projection.candidate_id
            or current["current_candidate_id"] != projection.candidate_id
            or state["row_version"] != current["row_version"]
            or wake["owner_id"] != owner or wake["model_id"] != model
            or current_wake["owner_id"] != owner or current_wake["model_id"] != model
            or wake["status"] != "current" or current_wake["status"] != "current"
            or wake["context_hash"] != current_wake["context_hash"]
            or not _digest(current_wake["context_hash"])
        ):
            raise ReviewPageError("review_page_binding_mismatch")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ReviewPageError("review_page_binding_required") from exc

    raw = connection.execute(
        "SELECT * FROM self_model_candidates WHERE candidate_id = ? AND model_id = ?",
        (projection.candidate_id, model),
    ).fetchone()
    if raw is None:
        raise ReviewPageError("review_candidate_unavailable")
    try:
        candidate = json.loads(projection._canonical_material)["candidate"]
        raw_content = json.loads(raw["content_json"])
        if (_hash(_canonical(raw_content)) != raw["content_hash"]
                or raw["content_hash"] != projection.content_hash):
            raise ReviewPageError("review_candidate_content_hash_mismatch")
        expected_candidate = {
            "candidate_id": raw["candidate_id"], "base_revision_id": raw["base_revision_id"],
            "content_hash": raw["content_hash"], "content": raw_content,
            "diff": json.loads(raw["diff_json"]), "reason": raw["reason"],
            "evidence_refs": json.loads(raw["evidence_refs_json"]),
        }
        if any(_canonical(candidate.get(key)) != _canonical(value)
               for key, value in expected_candidate.items()):
            raise ReviewPageError("review_candidate_material_mismatch")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ReviewPageError("review_candidate_invalid") from exc

    metadata = {
        "candidate_id": projection.candidate_id, "content_hash": projection.content_hash,
        "material_hash": projection.material_hash, "page": projection.page,
        "pages": projection.pages, "page_text_hash": _hash(projection.text),
    }
    coverage: set[int] = set()
    rows = connection.execute(
        "SELECT content_json, content_hash FROM brain_onboarding_artifacts "
        "WHERE owner_id = ? AND model_id = ? AND created_wake_id = ? "
        "AND kind = ? AND status = 'active'",
        (owner, model, wake_id, PAGE_ARTIFACT_KIND),
    ).fetchall()
    for row in rows:
        try:
            value = json.loads(row["content_json"])
            if not isinstance(value, dict) or _hash(_canonical(value)) != row["content_hash"]:
                continue
            if any(value.get(key) != metadata[key] for key in (
                "candidate_id", "content_hash", "material_hash", "pages"
            )):
                continue
            index = value.get("page")
            if type(index) is not int or not 0 <= index < projection.pages:
                continue
            start = index * PAGE_CHARACTERS
            expected_text = projection._canonical_material[start:start + PAGE_CHARACTERS]
            if value.get("page_text_hash") == _hash(expected_text):
                coverage.add(index)
        except (TypeError, ValueError, UnicodeError):
            continue
    if projection.page not in coverage:
        store._insert_artifact(
            connection, owner_id=owner, model_id=model, kind=PAGE_ARTIFACT_KIND,
            content=metadata, wake_id=wake_id,
        )
        coverage.add(projection.page)
    return coverage == set(range(projection.pages))
