"""Pure lexical query features and explicit-search candidate hints.

No vectors, network, storage, disclosure decisions, or learned synonyms. Alias
hints are deliberately a separate channel, never a replacement for recall score.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Sequence


@dataclass(frozen=True)
class LexicalQuery:
    normalized: str
    folded: str
    grams: frozenset[str]
    characters: frozenset[str]

    @classmethod
    def prepare(cls, query: str, normalized: str, grams: Iterable[str]) -> "LexicalQuery":
        return cls(normalized, query.casefold(), frozenset(grams), frozenset(normalized))

    def similarity(self, normalized_text: str, text_grams: Iterable[str]) -> float:
        """The existing containment/gram/sequence formula, with query reuse."""
        a, b = self.normalized, normalized_text
        if not a or not b:
            return 0.0
        grams_b = frozenset(text_grams)
        if self.characters.isdisjoint(b) and self.grams.isdisjoint(grams_b):
            # All three terms are exactly zero, not an approximate prefilter.
            return 0.0
        containment = min(len(a), len(b)) / max(len(a), len(b)) if a in b or b in a else 0.0
        union = self.grams | grams_b
        jaccard = len(self.grams & grams_b) / len(union) if union else 0.0
        sequence = SequenceMatcher(None, a, b, autojunk=False).ratio()
        return min(1.0, max(containment, (jaccard + sequence) / 2))


# A small, reviewable vocabulary of lexical alternatives, not semantic inference.
# Matching is confined to saved retrieval fields and explicit searches. Adding a family
# changes candidate recall only, and must have both retrieval and privacy tests.
LEXICAL_ALIAS_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("合照", "合影"),
    ("散步", "遛弯"),
    ("搬家", "迁居"),
    ("重启", "重新启动"),
    ("拍照", "拍摄照片"),
    ("断网", "网络断开"),
    ("报错", "错误提示"),
    ("停电", "断电"),
)

# Complete, common expressions only: a bare "回家" can describe a future plan.
# These are retrieval alternatives, never claims about the present situation.
EXPLICIT_EXPRESSION_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("回家了", "到家了", "刚到家", "刚回家", "已经到家", "刚进家门", "已经回到家"),
    ("准备睡觉", "准备睡了", "要睡了", "准备上床睡觉"),
    ("出门了", "刚出门", "已经出门"),
)
_CLAUSE_BOUNDARY = re.compile(r"[，,。.!！?？;；\n\r]")
_NONCURRENT_CUE = re.compile(
    r"没|不|未|明天|后天|昨天|前天|上周|下周|上次|下次|曾经|以前|"
    r"如果|假如|要是|打算|计划|将要|之后|以后|等到|可能|也许"
)


def _expression_occurs(text: str, family: Sequence[str]) -> bool:
    # Intentionally conservative and clause-local, not a temporal NLP model.
    # Reject only the added expression hint; ordinary literal search is intact.
    return any(
        not _NONCURRENT_CUE.search(clause)
        and any(_alias_occurs(clause, alias) for alias in family)
        for clause in _CLAUSE_BOUNDARY.split(text)
    )


@dataclass(frozen=True)
class ExplicitAliasQuery:
    lexical_families: tuple[tuple[str, ...], ...]
    expression_families: tuple[tuple[str, ...], ...]


def prepare_explicit_alias_query(query: str) -> ExplicitAliasQuery:
    """Prepare a small lexical vocabulary for a deliberate read-only search."""
    folded = unicodedata.normalize("NFKC", query).casefold()
    return ExplicitAliasQuery(
        alias_query_families(query),
        tuple(family for family in EXPLICIT_EXPRESSION_FAMILIES if _expression_occurs(folded, family)),
    )


def explicit_alias_match(
    query: str | ExplicitAliasQuery, saved_fields: Iterable[str],
) -> dict[str, object] | None:
    """A miss-only candidate hint; no text rewrite, inference or disclosure grant.

    Callers keep normal/exact results first and enforce their existing scope,
    lifecycle and disclosure rules. Automatic recall must not call this helper.
    Only fixed explanation fields are returned; saved text is never copied out.
    """
    prepared = prepare_explicit_alias_query(query) if isinstance(query, str) else query
    if not prepared.lexical_families and not prepared.expression_families:
        return None
    fields = [unicodedata.normalize("NFKC", value).casefold()
              for value in saved_fields if isinstance(value, str) and value]
    matches = sum(
        any(_alias_occurs(field, alias) for field in fields for alias in family)
        for family in prepared.lexical_families
    ) + sum(
        any(_expression_occurs(field, family) for field in fields)
        for family in prepared.expression_families
    )
    if not matches:
        return None
    return {
        "score": round(min(0.45, 0.28 + 0.04 * (matches - 1)), 4),
        "match_kind": "lexical_alias_candidate",
        "candidate_only": True,
        "matched_family_count": matches,
        "interpretation": "按常见词句找到的相关候选，保留原记录的时间与语境。",
    }


def _alias_occurs(text: str, alias: str) -> bool:
    if any("\u4e00" <= char <= "\u9fff" for char in alias):
        return alias in text
    return re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text) is not None


def alias_query_families(query: str) -> tuple[tuple[str, ...], ...]:
    folded = unicodedata.normalize("NFKC", query).casefold()
    return tuple(family for family in LEXICAL_ALIAS_FAMILIES
                 if any(_alias_occurs(folded, alias) for alias in family))


def alias_candidate_score(
    families: Sequence[Sequence[str]], keywords: Sequence[str],
) -> float:
    """Candidate ordering hint only; never confidence or disclosure authority."""
    if not families or not keywords:
        return 0.0
    folded = [unicodedata.normalize("NFKC", keyword).casefold() for keyword in keywords]
    matches = sum(any(_alias_occurs(keyword, alias) for alias in family for keyword in folded)
                  for family in families)
    return round(min(0.45, 0.28 + 0.04 * (matches - 1)), 4) if matches else 0.0
