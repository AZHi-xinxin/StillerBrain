"""Small deterministic excerpts for automatically supplied reminders only.

No model call, new authoring rule or stored-memory rewrite is performed here.
When a complete sentence does not fit, an ellipsis marks the partial quotation.
"""
from __future__ import annotations

import re
import unicodedata


_CLOSERS = r'''[”’」』》】)）"']'''
_SENTENCE_END = re.compile(r"(?:[。！？!?]+|\.(?=" + _CLOSERS + r"*(?:\s|$)))" + _CLOSERS + "*")
_CLAUSE_END = re.compile(r"[，,；;\r\n]")


def _safe_prefix(value: str, end: int) -> str:
    """Avoid leaving half a combining/emoji sequence at the excerpt boundary."""
    while 0 < end < len(value):
        following, previous = value[end], value[end - 1]
        if (unicodedata.category(following).startswith("M")
                or 0x1F3FB <= ord(following) <= 0x1F3FF
                or following == "\u200d" or previous == "\u200d"):
            end -= 1
            continue
        if 0x1F1E6 <= ord(following) <= 0x1F1FF:
            run = end
            while run > 0 and 0x1F1E6 <= ord(value[run - 1]) <= 0x1F1FF:
                run -= 1
            if (end - run) % 2:
                end -= 1
                continue
        break
    return value[:end].rstrip()


def reminder_excerpt(value: str, maximum: int) -> str:
    """Prefer a whole first sentence, then a clause, then a marked excerpt.

    The existing display limit stays intact. Author-provided reminders should
    bypass this helper. An ellipsis makes an unavoidable long unpunctuated
    excerpt explicit rather than presenting half a sentence as complete.
    """
    if maximum < 1:
        return ""
    text = value.strip()
    # Keep following closing quotation marks with the sentence they close.
    for match in _SENTENCE_END.finditer(text):
        if match.end() <= maximum and text[:match.start()].strip():
            return text[:match.end()]
        if match.end() > maximum:
            break
    if len(text) <= maximum:
        return text
    for match in reversed(list(_CLAUSE_END.finditer(text[:maximum]))):
        # A comma within a number is not a meaningful clause boundary.
        index = match.start()
        if (text[index] == "," and index > 0 and index + 1 < len(text)
                and text[index - 1].isdigit() and text[index + 1].isdigit()):
            continue
        prefix = text[:index].rstrip()
        if prefix and len(prefix) < maximum:
            return prefix + "…"
    prefix = _safe_prefix(text, maximum - 1)
    # For space-delimited prose, keep the final word whole when possible.
    if len(prefix) < len(text) and not text[len(prefix):len(prefix) + 1].isspace():
        boundary = max(prefix.rfind(" "), prefix.rfind("\t"))
        if boundary > 0:
            prefix = prefix[:boundary].rstrip()
    return prefix + "…"
