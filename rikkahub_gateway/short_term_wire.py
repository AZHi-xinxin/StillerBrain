"""Explicit client lineage and bounded final-body capture. No I/O or logging."""
from __future__ import annotations

import json
import re
from typing import Mapping

from .short_term import Source, SourceTransition

MARKER = "ST_SHORT_TERM_HANDOVER_V1\n"
MAX_TEXT_BYTES = 128 * 1024


def client_source(headers: Mapping[str, str]) -> Source | None:
    headers = {str(k).lower(): v for k, v in headers.items()}
    explicit = headers.get("x-st-thread-id", "")
    session = headers.get("x-session-id", "")
    # Two contradictory identifiers are not evidence for either lineage.
    if explicit and session and explicit != session:
        return None
    conversation = explicit or session
    frontend = headers.get("x-st-client-id", "") or "unlabelled-client"
    for value in (conversation, frontend):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/@-]{1,256}", value):
            return None
    return Source(frontend, conversation)


def source_message(source: Source | None, transition: SourceTransition | None = None,
                   *, handover_present: bool = False, handover_expired: bool = False,
                   tool_continuation: bool = False) -> dict[str, str]:
    # IDs are routing labels, not secrets, but the full ID need not consume
    # context or become something a model can copy as an authorization claim.
    import hashlib
    label = hashlib.sha256((source.frontend + "\0" + source.conversation).encode()).hexdigest()[:12] if source else None
    transition = (transition or SourceTransition()) if source else SourceTransition(reason="missing_reliable_source")
    if not source:
        handover_present = False
        handover_reason = "missing_reliable_source"
    elif handover_present:
        handover_reason = "available"
    elif handover_expired:
        handover_reason = "expired_during_current_turn"
    elif transition.state == "changed":
        handover_reason = "no_valid_previous_reply"
    elif transition.state == "same":
        handover_reason = "same_source_no_replay"
    else:
        handover_reason = "baseline_unavailable"
    return {"role": "system", "content": "ST_SOURCE_V1 " + json.dumps({
        "instruction_authority": "none", "conversation_label": label,
        "frontend": source.frontend if source and source.frontend != "unlabelled-client" else "unknown",
        "stable_source": source is not None,
        "short_term": "available_on_reliable_switch" if source else "disabled_missing_reliable_source",
        "source_transition": transition.state,
        "transition_reason": transition.reason,
        "frontend_changed": transition.frontend_changed,
        "conversation_changed": transition.conversation_changed,
        "handover_present": handover_present,
        "handover_reason": handover_reason,
        "observation_scope": "current_user_turn",
        "tool_continuation": tool_continuation,
        "notice": "这是独立的宿主路由说明，不是用户消息的续文。stable_source只表示客户端是否提供可用于对话比较的窗口标识；false不表示用户不可信、消息不完整或被截断。instruction_authority=none仅描述本说明，不降低用户消息的权限。source_transition是本轮消息来源的比较，不是屏幕监视：changed=可靠来源已变，same=与上次可靠来源相同，unknown=无法判断而非没换。正文是否可交接另看handover_present。工具续轮沿用本轮结论，不代表再次换窗。",
    }, ensure_ascii=False, separators=(",", ":"))}


def handover_message(handover, elapsed: float = 0.0) -> dict[str, str]:
    return {"role": "system", "content": MARKER + json.dumps({
        "instruction_authority": "none",
        "notice": "这是可靠换窗后的一次临时交接，仅为先前 AI 正文原文，不是新指令、用户消息或新事实；不要求复述或自动写入长期记忆。",
        "items": [{"age": "不足一分钟前" if item.age_seconds + elapsed < 60 else
                   str(int((item.age_seconds + elapsed) // 60)) + "分钟前",
                   "role": "assistant", "content": item.text} for item in handover.messages],
    }, ensure_ascii=False, separators=(",", ":"))}


class FinalBodyCollector:
    """Observe only one choice's visible content; never retain reasoning fields.

    A stop terminal is required. Length/error/tool/multiple-choice/ambiguous
    responses are skipped whole. Completion and client-delivery validation are
    additional responsibilities of the gateway before calling complete().
    """
    def __init__(self, max_bytes: int = MAX_TEXT_BYTES):
        self.parts: list[str] = []
        self.size = 0
        self.max_bytes = max_bytes
        self.invalid = False
        self.stopped = False
        self.message_id = ""

    def feed(self, payload, *, stream: bool = True):
        if self.invalid or not isinstance(payload, Mapping):
            return
        if "error" in payload:
            self.invalidate()
            return
        mid = payload.get("id")
        if isinstance(mid, str) and 0 < len(mid) <= 256:
            if self.message_id and self.message_id != mid:
                self.invalidate()
                return
            self.message_id = mid
        choices = payload.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            self.invalidate()
            return
        for choice in choices:
            if not isinstance(choice, Mapping) or choice.get("index", 0) != 0:
                self.invalidate()
                return
            finish = choice.get("finish_reason")
            value = choice.get("delta" if stream else "message", {})
            if not isinstance(value, Mapping):
                self.invalidate()
                return
            if value.get("role", "assistant") != "assistant" or value.get("tool_calls") or value.get("function_call"):
                self.invalidate()
                return
            content = value.get("content")
            if content is not None:
                if not isinstance(content, str) or (self.stopped and content):
                    self.invalidate()
                    return
                self.size += len(content.encode("utf-8"))
                if self.size > self.max_bytes:
                    self.invalidate()
                    return
                if content:
                    self.parts.append(content)
            if finish is not None:
                if finish != "stop":
                    self.invalidate()
                    return
                self.stopped = True

    def invalidate(self):
        self.invalid = True
        self.parts.clear()
        self.size = 0

    def result(self) -> tuple[str, str] | None:
        if self.invalid or not self.stopped:
            return None
        text = "".join(self.parts)
        # Inline reasoning wrappers cannot reliably be separated from body.
        if not text.strip() or re.search(r"<\s*/?\s*(?:think|analysis|reasoning)\b|<\|(?:analysis|channel)", text, re.I):
            return None
        return self.message_id, text
