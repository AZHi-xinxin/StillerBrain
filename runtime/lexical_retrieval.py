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
# Matching is confined to saved keywords and explicit searches. Adding a family
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
