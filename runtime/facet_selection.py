"""Small, deterministic candidate selection for AI-authored active self facets.

Facet names remain arbitrary identifiers. The vocabulary below is only a set
of lexical alternatives found in the author's key/body, never a list of
required names or a classifier of the AI's personality. No model, network,
storage, current-user text persistence, or authorship changes are involved.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import re
import unicodedata
from typing import Any


# These alternatives permit an English key to match an ordinary Chinese scene.
# Generic same-language phrases are matched separately, so custom names and
# domains do not have to belong to this small vocabulary.
_SCENES = (
    ("亲近", "亲密", "亲人", "家人", "爱人", "熟人", "恋人", "close ones", "loved ones", "intimate"),
    ("陌生人", "陌生场合", "初次见面", "第一次见面", "对外", "stranger", "strangers", "public facing"),
    ("独处", "一个人待", "自己待一会", "安静待一会", "alone", "solitude"),
    ("工作", "协作", "技术", "调试", "修复代码", "写代码", "working", "work", "technical", "coding"),
    ("难过", "伤心", "低落", "安慰", "委屈", "哭了", "sad", "comfort", "comforting", "distress"),
    ("拿不准", "不确定", "不知道怎么办", "犹豫", "uncertain", "uncertainty", "unsure"),
    ("刚醒", "醒来了", "刚睡醒", "刚唤醒", "重新醒来", "waking", "wakeup", "awakening"),
)
_SPLIT = re.compile(r"[，,。.!！?？;；\n\r]")
_QUOTED = re.compile(r'''```.*?```|`[^`]*`|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"\n]*"|(?<!\w)'[^'\n]*' ''', re.S | re.X)
_NONCURRENT = re.compile(
    r"昨天|前天|上周|上次|当年|曾经|以前|过去|明天|后天|下周|下次|"
    r"如果|假如|假设|要是|打算|计划|将要|以后|未来|据说|转述|引用|"
    r"他说|她说|他说过|她说过|文中|原文|书里|故事里|教程里|"
    r"\b(?:yesterday|previously|tomorrow|hypothetically|if|quoted|reported)\b"
)
_NEGATION = re.compile(r"没|未|并非|别|不|\b(?:not|never|without)\b")
_UNCERTAIN_EXPRESSIONS = re.compile(r"拿不准|不确定|不知道怎么办")
_POLITE_REQUESTS = re.compile(r"能不能|可不可以|不如")
_REQUEST_CONTRAST_BOUNDARY = re.compile(
    r"((?:能不能|可不可以|不如)[^。.!！?？;；\n\r]*?)[，,]\s*(?=但|可是|不过)"
)
_ENGLISH_WORD = re.compile(r"[a-z][a-z0-9]{3,}")
_ENGLISH_STOP = frozenset({"this", "that", "with", "when", "have", "will", "from", "your", "myself", "about", "into", "then", "they", "their", "would", "should", "what", "been", "being"})
_CHINESE = re.compile(r"[\u3400-\u9fff]+")
_TRIVIAL = frozenset({"的时候", "我自己", "你自己", "我觉得", "我认为", "我想要", "我希望", "我可以", "我愿意", "我们都", "现在我", "这件事", "我会先", "我会在", "对方的", "自己的", "我保持", "我重视"})


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _occurs(text: str, term: str) -> bool:
    if _CHINESE.search(term):
        return term in text
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def _current_clauses(query: str) -> tuple[str, ...]:
    # A conservative clause-local guard, not complete temporal NLP. Quoted
    # material is not interpreted as the current interpersonal situation.
    query = _QUOTED.sub(" ", _fold(query[-6000:]))
    # A request followed by "but I am not ..." stays one conservative unit,
    # including the common comma before the contrast. Do not let removal of
    # a polite question form erase a separate, genuine negation.
    query = _REQUEST_CONTRAST_BOUNDARY.sub(r"\1 ", query)
    result = []
    for clause in _SPLIT.split(query):
        if not clause.strip() or _NONCURRENT.search(clause):
            continue
        negation_copy = _POLITE_REQUESTS.sub(" ", _UNCERTAIN_EXPRESSIONS.sub(" ", clause))
        if _NEGATION.search(negation_copy):
            continue
        result.append(clause)
    return tuple(result)


def _grams(text: str) -> set[str]:
    return {run[index:index + 3] for run in _CHINESE.findall(text)
            for index in range(len(run) - 2)
            if run[index:index + 3] not in _TRIVIAL}


def rank_facet_candidates(facets: Mapping[str, str], query: str) -> list[str]:
    """Return relevant candidate keys, scored solely from saved key/body text.

    Empty/unrecognized/negative/noncurrent input has no automatic fallback.
    Exact requested keys, common scene alternatives and shared lexical phrases
    are hints only; this function never asserts that a scene actually occurred.
    """
    if not isinstance(query, str) or not isinstance(facets, Mapping):
        return []
    clauses = _current_clauses(query)
    if not clauses:
        return []
    current = " ".join(clauses)
    query_words = set(_ENGLISH_WORD.findall(current)) - _ENGLISH_STOP
    query_grams = _grams(current)
    query_scenes = [family for family in _SCENES if any(_occurs(current, term) for term in family)]
    scored = []
    for name, body in facets.items():
        if not isinstance(name, str) or not isinstance(body, str) or not body.strip():
            continue
        original_key = _fold(name)
        text = _fold(name.replace("_", " ").replace("-", " ").replace(".", " ") + " " + body)
        direct_key = len(original_key) >= 3 and _occurs(current, original_key)
        scenes = sum(any(_occurs(text, term) for term in family) for family in query_scenes)
        words = len(query_words & (set(_ENGLISH_WORD.findall(text)) - _ENGLISH_STOP))
        grams = len(query_grams & _grams(text))
        score = int(direct_key) * 12 + scenes * 6 + min(words, 3) * 3 + min(grams, 5)
        if score:
            scored.append((score, name))
    return [name for _, name in sorted(scored, key=lambda pair: (-pair[0], pair[1]))]


def append_facet_projection(
    dynamic: Mapping[str, Any], facets: Mapping[str, str], query: str, *,
    estimate_tokens: Callable[[Any], int], budget: int = 1200,
) -> dict[str, Any]:
    """Add at most three whole authored facets to unused shared dynamic budget.

    Existing memory projections always retain their space. Oversized facets
    are skipped rather than truncated or rewritten; explicit host selection
    uses the original exact-selection path instead of this automatic helper.
    """
    result = dict(dynamic)
    selected: dict[str, str] = {}
    for name in rank_facet_candidates(facets, query):
        proposed = {**selected, name: facets[name]}
        candidate = {**result, "self_facets": {
            "contract": "self-facets-context/0.1",
            "frame": {"authorship": "ai_active_self_revision", "selection": "local_lexical_candidate", "permission_authority": "none"},
            "facets": proposed,
        }}
        if estimate_tokens(candidate) <= budget:
            selected = proposed
            result = candidate
        if len(selected) >= 3:
            break
    return result
