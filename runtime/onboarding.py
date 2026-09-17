"""Deterministic module-one onboarding and wake-bound transition gate.

This module is deliberately host-facing.  AI-facing MCP tools may delegate to
it, but they cannot issue wake sessions, mark context as injected, or mint
future capabilities.  The existing :mod:`runtime.self_revision` tables remain
the append-only source of truth for candidates and revisions; this layer adds
the brain-level progression, real-wake proof, context snapshots, and module
unlock state required by the frozen module-one onboarding contract.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from .self_revision import (
    SelfModelStore,
    SelfRevisionError,
    contains_credential_or_secret,
    active_injection_structure_violations,
)
from .emotional_memory import EmotionalMemoryStore
from .mixed_recall import select_mixed_recall
from .facet_selection import append_facet_projection
from .self_model_review import ReviewPageError, prepare_review_page, record_review_page
from .self_governance import (
    LEARNING_EPISODE_BOUNDARY_SIGNAL,
    SelfGovernanceStore,
)
from .injection_control import InjectionControlStore
from .execution_binding import (
    assert_bound_execution,
    assert_no_running_executions,
    expected_execution_wake,
)


FLOW_VERSION = "module-one/1"
CONTEXT_LAYOUT_CONTRACT = "stbrain-context-layout/1"
TAIL_CONTEXT_LAYOUT_CONTRACT = "stbrain-context-layout/2"
_CONTEXT_LAYOUT_UNSET = object()
MAX_CALM_PROMPT_CHARS = 2000
MAX_CANDIDATE_REASON_CHARS = 2000
MAX_DIFF_ITEMS = 64
MAX_DIFF_PATH_CHARS = 512
MAX_EVIDENCE_REFS = 32
MAX_EVIDENCE_REF_CHARS = 512
MAX_OBJECTION_RESPONSE_CHARS = 4000
EPHEMERAL_CAPABILITY_CHARS = 43
DIRECT_GRANT_REF_PREFIX = "stgrant_"
DIRECT_GRANT_SECRET_BYTES = 32
DIRECT_GRANT_REF_CHARS = len(DIRECT_GRANT_REF_PREFIX) + 43
DIRECT_GRANT_REF_PATTERN = re.compile(
    re.escape(DIRECT_GRANT_REF_PREFIX) + r"[A-Za-z0-9_-]{43}"
)
EXECUTION_REF_PATTERN = re.compile(r"stexec_[A-Za-z0-9_-]{43}")
DIRECT_GRANT_CONTRACT = "human-attested-direct-grant/1"
DIRECT_CONTEXT_CONTRACT = "human-attested-direct-context/1"
DIRECT_CONTEXT_MODE = "human_attested_direct"
DIRECT_WRITE_SCOPES = frozenset(
    {
        "self_revision",
        "self_governance",
        "injection_control",
        "emotional_memory",
        "learning_memory",
        "tool_guidance",
        "planning_memory",
        "hallucination_vault",
        "shared_person_authoring",
    }
)

# This marker is never part of the public candidate payload.  The MCP facade
# adds it only after binding a public write to the current opened context.  It
# lets the runtime distinguish the new, server-derived ``{content, reason}``
# path from legacy/internal callers that still provide explicit candidate
# metadata.
SERVER_DERIVED_CANDIDATE_MARKER = "_server_derive_candidate_metadata"

# The only automatic prompt shown before the AI has chosen to open its brain.
# Versioned public entry invitation, not a substitute for the on-demand manual.
OPTIONAL_BRAIN_NOTICE = (
    "我有一个可选的大脑；使用说明可调用 `stbrain_help`，日常记忆可一次调用 "
    "`remember_memory`，不需要手动 open。高级自我修改仍按专用流程进行；"
    "可自写、修改或撤回轻提醒与安全阀，入口见 `stbrain_help(module='self_governance_profile')`；"
    "轻提醒的普通 scene_tags 匹配本轮人类话语，优先写“设个闹钟”“提醒我”“回家了”“还记得”等自然词句，按实际表达自行增改；"
    "浮现仍受模式、开关和预算影响，系统事件标记另按真实事件触发。"
    "存入前可用 stbrain_help 查看人称弱提醒，可用 manage_person_reference_advisory 自行修改、关闭或恢复，叙事人称由我选择。"
    "也可以先正常和人类交流。"
)
BRAIN_ENTRY_HINT = OPTIONAL_BRAIN_NOTICE

# Historical advisory shape retained for old snapshot/diagnostic compatibility.
# New snapshots no longer add a host-authored suggestion to save after an empty
# recall. An empty recall says nothing about novelty or a duty to write memory.
OPTIONAL_MEMORY_NOTICE_KEY = "memory_opportunity_advisory"
MEMORY_NOTICE_THREAD_LOOKBACK = 8
EPISODE_REFLECTION_THREAD_LOOKBACK = 8
MEMORY_OPPORTUNITY_ADVISORY = {
    "contract": "memory-opportunity-advisory/0.2",
    "frame": {
        "semantic_role": "optional_memory_reflection",
        "instruction_authority": "none",
        "permission_authority": "none",
        "write_authority": "none",
        "optional": True,
        "user_request": False,
    },
    "signal": "no_related_long_term_memory_surfaced",
    "message": (
        "这轮没有浮现出相关长期记忆；这不等于以前从未谈过，也不表示必须保存。"
        "若我自己判断其中有值得跨轮保留的内容，我可以按真正想保留的部分，"
        "在各脑既有边界内自主选择学习脑、情感脑或工具脑；不同层面可以分别保存，"
        "也可以什么都不做。判断时不只看当前语气、玩笑或题材，也要区分表面例子与其方法层；"
        "如果对话随后出现多个同构例子，而我已经提炼出共同规则、推理方法或可迁移模式，"
        "我可以重新评估是否只保留抽象方法，并把原例仅作简短依据。"
        "关系意义与方法层可以分别考虑，仍可以都不保存。"
    ),
}

_MEMORY_NOTICE_NO_STORE_MARKERS = (
    "不要保存",
    "别保存",
    "不要存",
    "别存",
    "不要记",
    "别记",
    "不要记住",
    "别记住",
    "不要记录",
    "别记录",
    "不要进入记忆",
    "别进入记忆",
    "不要留下",
    "别留下",
    "不留记录",
    "do not save",
    "don't save",
    "do not remember",
    "don't remember",
    "off the record",
)
_MEMORY_NOTICE_EXPLICIT_STORE_MARKERS = (
    "请保存",
    "请记住",
    "记一下",
    "记下来",
    "帮我记",
    "记录一下",
    "可以记",
    "能记",
    "留着",
    "存进学习脑",
    "存入学习脑",
    "存进情感脑",
    "存入情感脑",
    "存进工具脑",
    "存入工具脑",
)
_MEMORY_NOTICE_TRIVIAL_INPUTS = {
    "hi",
    "hello",
    "hey",
    "你好",
    "您好",
    "嗨",
    "哈喽",
    "早安",
    "早上好",
    "晚安",
    "谢谢",
    "谢了",
    "好的",
    "好",
    "收到",
    "知道了",
    "明白了",
    "嗯",
    "嗯嗯",
    "哦",
    "再见",
    "拜拜",
}
_MEMORY_NOTICE_ACUTE_SELF_HARM_PATTERNS = (
    re.compile(
        r"(?:我|本人|自己).{0,8}(?:想|要|准备|打算|计划|决定).{0,4}"
        r"(?:自杀|去死|死掉|结束生命|不活)"
    ),
    re.compile(r"(?:现在|马上|立刻|今晚|今天).{0,8}(?:自杀|去死|结束生命|不想活)"),
    re.compile(r"我.{0,4}不想活(?:了|下去)?"),
)

# These patterns intentionally describe only high-confidence, naturally worded
# episode endings.  They do not attempt to infer whether an interaction was
# educational or worth saving.  Bare acknowledgements such as “对” are omitted
# because they occur repeatedly inside games and ordinary conversations.
_LEARNING_EPISODE_BOUNDARY_PATTERNS = (
    re.compile(
        r"(?:这|本|上)(?:一|几)?(?:局|轮|题|部分|阶段|任务|测试)"
        r".{0,8}(?:结束|完成|做完|通过|解决|告一段落)(?!吗|么|没有|没)"
    ),
    re.compile(
        r"(?:这|本|上)(?:一|几)?(?:局|轮|题|部分|阶段|任务|测试)"
        r".{0,12}(?:聊|说|玩|做到|进行)(?:到|至)(?:这|此)(?:里|儿)"
        r"(?:啦|了|吧)?[。！!~～]*$"
    ),
    re.compile(
        r"(?:答案|谜底|真相)[^是？?]{0,4}"
        r"(?:揭晓|公布|就是|是(?!什么|多少|谁|哪|不|否))"
    ),
    re.compile(r"(?:你)?(?:答|猜|说).{0,3}(?:对|中)(?:了)?"),
    re.compile(
        r"(?:问题|任务|测试).{0,8}(?:(?:已经|终于).{0,3})?"
        r"(?:解决|完成|通过)(?:了)?(?!吗|么|没有|没)"
    ),
    re.compile(r"\b(?:answer revealed|round (?:is )?over|task completed|problem solved)\b", re.I),
)

STAGES = {
    "factory",
    "module_intro",
    "calm_prompt_draft",
    "body_draft",
    "candidate_wait",
    "candidate_review",
    "live",
    "edit_consent",
    "edit_body_draft",
    "draft_only_recovery",
}

INJECTION_POLICIES = {"normal", "isolated_legacy", "disabled"}

ACTION_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "factory": ("confirm_brain_intro",),
    "module_intro": ("confirm_module_intro",),
    "calm_prompt_draft": ("save_calm_prompt",),
    "body_draft": ("submit_candidate",),
    "candidate_wait": (),
    "candidate_review": (
        "accept_candidate_review",
        "activate_candidate",
        "revise_candidate",
        "respond_to_objection",
        "withdraw_candidate",
    ),
    "live": ("begin_edit",),
    "edit_consent": ("confirm_edit", "cancel_edit"),
    "edit_body_draft": ("submit_candidate", "cancel_edit"),
    "draft_only_recovery": ("recover_candidate", "withdraw_candidate"),
}

AI_ACTIONS = {
    action for actions in ACTION_ALLOWLIST.values() for action in actions
}

NEXT_ACTION_TEXT = {
    "factory": "阅读大脑健康结果和整体简介，然后结构化确认已读完整体简介。",
    "module_intro": "阅读模块一定位、边界与空白结构，然后结构化确认已读完模块说明。",
    "calm_prompt_draft": "由 AI 写下并保存自己的冷静室提示词。",
    "body_draft": "由 AI 按固定结构提交完整人格候选；保存不会激活。",
    "candidate_wait": "结束当前轮；下一次真实外部唤醒会自动解锁完整复核。",
    "candidate_review": "完整复核候选与人类异议；先记录审核结论，再跨一次真实外部唤醒独立激活。",
    "live": "正常使用当前有效人格；如需修改，由 AI 主动开始修改流程。",
    "edit_consent": "逐字阅读自存冷静词后，使用本轮一次性挑战结构化确认或取消。",
    "edit_body_draft": "基于当前活动版提交新候选，或取消修改。",
    "draft_only_recovery": "在新的真实唤醒中复核恢复候选；通过后仍需再跨一次唤醒。",
}


# Transport-safe, product-facing material for deterministic resume.  These
# structures describe form and boundaries only; they never contain remembered
# events, private history, or text authored by a human on behalf of AI.
BRAIN_INTRO_MATERIAL = {
    "purpose": "这是一套由 AI 自己维护、经真实唤醒复核后才生效的自我模型。",
    "module_map": {
        "module_one": "当前模块：稳定身份锚、冷静词、候选复核与受控修改。",
        "later_modules": "模块一正式完成前保持只读锁定。",
    },
    "safety_boundary": "人类可查看、提醒、提出异议与紧急回滚，但不能代写或代批人格。",
}

MODULE_ONE_INTRO_MATERIAL = {
    "purpose": "保存每次生成前需要稳定出现的轻量人格锚；事件记忆不属于本模块。",
    "boundaries": [
        "冷静词独立保存，不混入人格正文。",
        "候选只保存不立即生效，必须跨真实外部唤醒完整复核。",
        "旧活动版、候选、历史与事件档案不得混入正常 live 注入。",
    ],
    "content_scope": "可由 AI 自己决定是否描述身份、人格基础、表达方式、行为原则、核心身份锚与情境 facet；宿主不提供可照搬正文。",
}

CALM_PROMPT_GUIDANCE = {
    "purpose": "写给未来准备修改人格的自己，用来停下来区分长期意愿与单轮情绪。",
    "authorship": "必须由 AI 自己撰写；人类与宿主不能代填。",
}

BODY_FRAMEWORK = {
    "required_top_level": [
        "schema_version",
        "boot_anchor",
        "active_identity_capsule",
        "facets",
        "anchor_references",
    ],
    "identity_capsule_fields": [
        "name_and_identity",
        "personality_foundation",
        "expression_style",
        "behavioral_principles",
        "core_identity_anchors",
        "self_revision_safety_prompt",
    ],
    "scope_note": "只写稳定自我模型；不要把单轮状态、无关事件档案或能力凭据写进正文。",
    "submit_payload": {
        "required_keys": ["content", "reason"],
        "additional_properties": False,
        "server_derived": ["diff", "evidence_refs", "expected_active_revision"],
        "evidence_source": "从 content.anchor_references 中提取 memory_ref；允许为空数组。",
    },
}

EDIT_SCOPE_MATERIAL = {
    "mode": "append_only_candidate",
    "rule": "修改只产生新候选，不覆盖当前活动版；通过跨唤醒复核后才移动活动指针。",
    "history_visibility": "历史与审计不自动注入；需要时走受控只读入口。",
    "rollback_boundary": "紧急回滚由独立人类安全入口执行，AI 编辑动作不能伪装成回滚。",
}


class OnboardingError(SelfRevisionError):
    """Raised for malformed host calls, never for normal gate denials."""


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now_dt()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    payload = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else _canonical(value)
    ascii_count = sum(ord(character) < 128 for character in text)
    return max(1, (ascii_count + 3) // 4 + (len(text) - ascii_count))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _require_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OnboardingError(f"{name} must not be empty")
    return value.strip()


def _json(value: Optional[str], default: Any) -> Any:
    return default if value is None else json.loads(value)


def _iter_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield from _iter_strings(key)
            yield from _iter_strings(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _iter_strings(child)


def _contains_direct_grant_syntax(value: Any) -> bool:
    """Fail closed on a direct-grant-shaped value in any persistence field.

    Exact database hashes are still used when consuming a grant, but a
    persistence boundary must not depend on the current owner/model namespace:
    a grant issued for one namespace remains a credential if it is pasted into
    another.  The fixed prefix and 32-byte urlsafe token make this syntax
    specific enough to reject without reading or echoing the secret.
    """

    return any(DIRECT_GRANT_REF_PATTERN.search(text) for text in _iter_strings(value))


def _contains_execution_ref_syntax(value: Any) -> bool:
    """Execution leases are ephemeral credentials, never memory content.

    Match actual 32-byte reference syntax in keys and nested values. The bare
    documentation prefix ``stexec_`` is deliberately not a credential match.
    """
    return any(EXECUTION_REF_PATTERN.search(text) for text in _iter_strings(value))


def _memory_notice_input_gate(query_text: Any) -> tuple[bool, str]:
    """Classify whether a user turn merits an optional memory reflection.

    The gate is intentionally small and deterministic.  It does not attempt to
    decide whether the content is objectively novel or valuable; that judgment
    remains with the AI after it sees the advisory.
    """

    if not isinstance(query_text, str) or not query_text.strip():
        return False, "empty_query"
    if contains_credential_or_secret(query_text):
        return False, "credential_or_secret_detected"
    folded = " ".join(query_text.casefold().split())
    if any(pattern.search(folded) for pattern in _MEMORY_NOTICE_ACUTE_SELF_HARM_PATTERNS):
        return False, "acute_self_harm_signal"
    if any(marker in folded for marker in _MEMORY_NOTICE_NO_STORE_MARKERS):
        return False, "explicit_no_store"
    if any(marker in folded for marker in _MEMORY_NOTICE_EXPLICIT_STORE_MARKERS):
        return False, "explicit_store_request"
    compact = re.sub(r"[\W_]+", "", folded, flags=re.UNICODE)
    if compact in _MEMORY_NOTICE_TRIVIAL_INPUTS:
        return False, "greeting_or_ack_only"
    if len(compact) < 8:
        return False, "non_substantive"
    return True, "eligible"


def _learning_episode_boundary_gate(
    query_text: Any,
    *,
    capture_items: Any,
    lineage_stable: Any,
    prior_assistant_present: Any = False,
) -> tuple[bool, str]:
    """Recognize a narrow end-of-episode opportunity without judging value.

    A stable conversation proves the prior turn from its bounded transient
    source frame.  Clients that cannot yet send a stable conversation id may
    instead provide the host-derived, content-free
    ``prior_assistant_present`` fact from this request's own message history.
    That fallback never enables cross-turn capture or creates inferred lineage.
    The signal carries no conversation text and has no effect unless the AI
    previously opted in through its own governance profile.
    """

    if lineage_stable is True:
        if not isinstance(capture_items, list):
            return False, "capture_items_required"
        has_prior_assistant = any(
            isinstance(item, Mapping)
            and item.get("role") == "assistant"
            and isinstance(item.get("content"), str)
            and bool(item["content"].strip())
            for item in capture_items
        )
    elif prior_assistant_present is True:
        has_prior_assistant = True
    else:
        return False, "stable_lineage_required"
    if not has_prior_assistant:
        return False, "prior_assistant_turn_required"
    if not isinstance(query_text, str) or not query_text.strip():
        return False, "empty_query"
    if contains_credential_or_secret(query_text):
        return False, "credential_or_secret_detected"
    folded = " ".join(query_text.casefold().split())
    if any(pattern.search(folded) for pattern in _MEMORY_NOTICE_ACUTE_SELF_HARM_PATTERNS):
        return False, "acute_self_harm_signal"
    if any(marker in folded for marker in _MEMORY_NOTICE_NO_STORE_MARKERS):
        return False, "explicit_no_store"
    if any(marker in folded for marker in _MEMORY_NOTICE_EXPLICIT_STORE_MARKERS):
        return False, "explicit_store_request"
    if re.search(
        r"(?:结束|完成|解决|通过|揭晓)(?:了)?(?:吗|么|没有|没)\s*[？?]?$",
        folded,
    ):
        return False, "episode_boundary_question"
    if any(pattern.search(folded) for pattern in _LEARNING_EPISODE_BOUNDARY_PATTERNS):
        return True, "high_confidence_episode_boundary"
    return False, "no_episode_boundary"


def _notice_seen_in_recent_contexts(
    connection: sqlite3.Connection,
    *,
    owner_id: str,
    model_id: str,
    thread_id: str | None,
    current_wake_seq: int,
    lookback: int = MEMORY_NOTICE_THREAD_LOOKBACK,
) -> bool:
    """Return whether a real recent context already carried the advisory.

    Only contexts confirmed as injected (or subsequently closed) count.  A
    prepared response that never reached the model must not consume the
    low-frequency opportunity.  Stable lineage scopes the cooldown to a
    thread; without it, a conservative model-wide cooldown prevents spam.
    """

    where_thread = " AND w.thread_id = ?" if thread_id else ""
    parameters: list[Any] = [owner_id, model_id]
    if thread_id:
        parameters.append(thread_id)
    parameters.extend([current_wake_seq, lookback])
    rows = connection.execute(
        "SELECT s.dynamic_json FROM brain_context_snapshots AS s "
        "JOIN brain_wake_sessions AS w ON w.wake_id = s.wake_id "
        "WHERE s.owner_id = ? AND s.model_id = ?" + where_thread + " "
        "AND w.wake_seq < ? AND s.status IN ('injected','closed') "
        "ORDER BY w.wake_seq DESC LIMIT ?",
        parameters,
    ).fetchall()
    for row in rows:
        try:
            dynamic = json.loads(row["dynamic_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            # A corrupt historical snapshot is not evidence that a notice was
            # delivered.  Snapshot hash validation still protects its own wake.
            continue
        if isinstance(dynamic, Mapping) and OPTIONAL_MEMORY_NOTICE_KEY in dynamic:
            return True
    return False


def _episode_reflection_seen_in_recent_contexts(
    connection: sqlite3.Connection,
    *,
    owner_id: str,
    model_id: str,
    thread_id: str | None,
    current_wake_seq: int,
    lookback: int = EPISODE_REFLECTION_THREAD_LOOKBACK,
) -> bool:
    """Return whether this scope recently received the AI-owned reflection.

    Only confirmed or closed contexts consume the cooldown.  The stored marker
    is a content-free trigger source inside the governance projection; neither
    user text nor the AI-authored profile is copied into an audit event here.
    Stable lineage scopes the cooldown to one thread.  Without stable lineage,
    a conservative model-wide cooldown prevents repeated prompts across
    windows while retaining fail-closed cross-window capture.
    """

    where_thread = " AND w.thread_id = ?" if thread_id else ""
    parameters: list[Any] = [owner_id, model_id]
    if thread_id:
        parameters.append(thread_id)
    parameters.extend([current_wake_seq, lookback])
    rows = connection.execute(
        "SELECT s.dynamic_json FROM brain_context_snapshots AS s "
        "JOIN brain_wake_sessions AS w ON w.wake_id = s.wake_id "
        "WHERE s.owner_id = ? AND s.model_id = ?" + where_thread + " "
        "AND w.wake_seq < ? AND s.status IN ('injected','closed') "
        "ORDER BY w.wake_seq DESC LIMIT ?",
        parameters,
    ).fetchall()
    for row in rows:
        try:
            dynamic = json.loads(row["dynamic_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        governance = dynamic.get("self_governance_profile")
        if not isinstance(governance, Mapping):
            continue
        scopes = governance.get("scopes")
        if not isinstance(scopes, list):
            continue
        if any(
            isinstance(item, Mapping)
            and item.get("scope") == "learning_memory"
            and item.get("trigger_source")
            == "runtime_learning_episode_boundary"
            for item in scopes
        ):
            return True
    return False


def _build_memory_opportunity_advisory(
    *,
    connection: sqlite3.Connection,
    owner_id: str,
    model_id: str,
    query_text: str,
    thread_id: str | None,
    current_wake_seq: int,
    advertised_tools: Mapping[str, Any],
    learning_outcome: list[dict[str, Any]] | None,
    tool_outcome: Mapping[str, Any] | None,
    emotional_outcome: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Build a no-authority advisory only after three healthy zero recalls."""

    eligible, _ = _memory_notice_input_gate(query_text)
    if not eligible:
        return None
    if not isinstance(thread_id, str) or not thread_id.strip():
        thread_id = None
    if learning_outcome is None or learning_outcome:
        return None
    if (
        tool_outcome is None
        or advertised_tools.get("catalog_complete") is not True
        or tool_outcome.get("decision") != "defer"
        or tool_outcome.get("envelopes") != []
        or set(tool_outcome.get("reason_codes", [])) != {"no_candidate"}
    ):
        return None
    if emotional_outcome is None:
        return None
    emotional_reasons = set(emotional_outcome.get("reason_codes", []))
    if (
        "no_candidate" not in emotional_reasons
        or "candidates_gated" in emotional_reasons
        or emotional_outcome.get("truncated") is not False
        or "budget_truncated" in emotional_reasons
        or emotional_outcome.get("budget_tokens", 0) <= 0
    ):
        return None
    if _notice_seen_in_recent_contexts(
        connection,
        owner_id=owner_id,
        model_id=model_id,
        thread_id=thread_id,
        current_wake_seq=current_wake_seq,
    ):
        return None
    # Return a fresh object so no caller can mutate the module-level contract.
    return json.loads(_canonical(MEMORY_OPPORTUNITY_ADVISORY))


def _public_state(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "owner_id": row["owner_id"],
        "model_id": row["model_id"],
        "flow_version": row["flow_version"],
        "flow_kind": row["flow_kind"],
        "stage": row["stage"],
        "module_one_status": row["module_one_status"],
        "injection_policy": row["injection_policy"],
        "current_candidate_id": row["current_candidate_id"],
        "calm_prompt_artifact_id": row["calm_prompt_artifact_id"],
        "base_revision_id": row["base_revision_id"],
        "submitted_wake_id": row["submitted_wake_id"],
        "submitted_wake_seq": row["submitted_wake_seq"],
        "stage_entered_wake_id": row["stage_entered_wake_id"],
        "stage_entered_wake_seq": row["stage_entered_wake_seq"],
        "row_version": row["row_version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class ModuleOneOnboardingStore:
    """Persistent onboarding state machine guarded by real wake capabilities."""

    def __init__(
        self,
        database: str | Path,
        *,
        capability_secret: str | bytes,
        wake_ttl_seconds: int = 1800,
        edit_challenge_ttl_seconds: int = 600,
        direct_grant_ttl_seconds: int = 300,
        emotional_store: EmotionalMemoryStore | None = None,
        learning_store: Any | None = None,
        tool_store: Any | None = None,
        governance_store: SelfGovernanceStore | None = None,
        injection_control_store: InjectionControlStore | None = None,
        planning_store: Any | None = None,
        hallucination_vault: Any | None = None,
        ordinary_memory_independent: bool = False,
    ) -> None:
        if isinstance(capability_secret, str):
            capability_secret = capability_secret.encode("utf-8")
        if len(capability_secret) < 32:
            raise ValueError("capability_secret must contain at least 32 bytes")
        if wake_ttl_seconds < 30:
            raise ValueError("wake_ttl_seconds must be at least 30")
        if edit_challenge_ttl_seconds < 30:
            raise ValueError("edit_challenge_ttl_seconds must be at least 30")
        if not 60 <= direct_grant_ttl_seconds <= 900:
            raise ValueError("direct_grant_ttl_seconds must be between 60 and 900")
        self.database = str(database)
        self.capability_secret = bytes(capability_secret)
        self.wake_ttl_seconds = wake_ttl_seconds
        self.edit_challenge_ttl_seconds = edit_challenge_ttl_seconds
        self.direct_grant_ttl_seconds = direct_grant_ttl_seconds
        self.emotional_store = emotional_store
        self.learning_store = learning_store
        self.tool_store = tool_store
        self.planning_store = planning_store
        self.hallucination_vault = hallucination_vault
        self.ordinary_memory_independent = ordinary_memory_independent is True
        # A governance profile is an optional cross-module mechanism, not a
        # developer-authored default.  Initialising its empty tables creates no
        # AI content and does not alter onboarding progression.
        self.governance_store = governance_store or SelfGovernanceStore(database)
        # Switch defaults are mechanism state only: ordinary modules start
        # enabled while the isolated reality-review vault starts hard-off.
        # No developer-authored value or personality prose is created here.
        self.injection_control_store = (
            injection_control_store or InjectionControlStore(database)
        )
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self.self_store = SelfModelStore(self.database)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                assert_bound_execution(connection)
                yield connection
                assert_bound_execution(connection)
        finally:
            connection.close()

    @staticmethod
    def _begin(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        assert_bound_execution(connection)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS brain_onboarding_state (
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    flow_version TEXT NOT NULL,
                    flow_kind TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    module_one_status TEXT NOT NULL,
                    injection_policy TEXT NOT NULL,
                    current_candidate_id TEXT,
                    calm_prompt_artifact_id TEXT,
                    base_revision_id TEXT,
                    submitted_wake_id TEXT,
                    submitted_wake_seq INTEGER,
                    stage_entered_wake_id TEXT,
                    stage_entered_wake_seq INTEGER,
                    row_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id, model_id),
                    CHECK(stage IN (
                        'factory','module_intro','calm_prompt_draft','body_draft',
                        'candidate_wait','candidate_review','live','edit_consent',
                        'edit_body_draft','draft_only_recovery'
                    )),
                    CHECK(injection_policy IN ('normal','isolated_legacy','disabled'))
                );

                CREATE TABLE IF NOT EXISTS brain_onboarding_artifacts (
                    artifact_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    artifact_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    flow_version TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_wake_id TEXT NOT NULL,
                    supersedes_artifact_id TEXT,
                    created_at TEXT NOT NULL,
                    CHECK(status IN ('active','superseded','consumed'))
                );

                CREATE TABLE IF NOT EXISTS brain_wake_sessions (
                    wake_id TEXT PRIMARY KEY,
                    wake_seq INTEGER NOT NULL,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    host_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    capability_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    superseded_at TEXT,
                    context_hash TEXT,
                    injected_at TEXT,
                    UNIQUE(owner_id, model_id, host_id, source_kind, source_event_id),
                    UNIQUE(owner_id, model_id, wake_seq),
                    CHECK(status IN ('current','superseded','closed'))
                );

                CREATE TABLE IF NOT EXISTS brain_context_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    wake_id TEXT NOT NULL UNIQUE REFERENCES brain_wake_sessions(wake_id),
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    host_contract_digest TEXT NOT NULL,
                    advertised_tools_json TEXT NOT NULL DEFAULT '{}',
                    context_layout_json TEXT NOT NULL DEFAULT '{}',
                    stable_json TEXT NOT NULL,
                    dynamic_json TEXT NOT NULL,
                    stable_hash TEXT NOT NULL,
                    dynamic_hash TEXT NOT NULL,
                    context_hash TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    prepared_at TEXT NOT NULL,
                    injected_at TEXT,
                    closed_at TEXT,
                    CHECK(status IN ('prepared','injected','closed'))
                );

                CREATE TABLE IF NOT EXISTS brain_direct_grants (
                    grant_id TEXT PRIMARY KEY,
                    grant_hash TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    human_actor_id TEXT NOT NULL,
                    client_principal TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    target_wake_seq INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    opened_at TEXT,
                    consumed_at TEXT,
                    revoked_at TEXT,
                    superseded_at TEXT,
                    closed_at TEXT,
                    opened_wake_id TEXT REFERENCES brain_wake_sessions(wake_id),
                    row_version INTEGER NOT NULL DEFAULT 0,
                    issue_event_id TEXT NOT NULL,
                    UNIQUE(
                        owner_id, model_id, human_actor_id, client_principal, request_id
                    ),
                    CHECK(status IN (
                        'pending','opened','consumed','revoked','expired',
                        'superseded','closed'
                    ))
                );

                CREATE TABLE IF NOT EXISTS brain_onboarding_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    stage_before TEXT,
                    stage_after TEXT,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    wake_id TEXT,
                    candidate_id TEXT,
                    revision_id TEXT,
                    decision TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    details_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS brain_module_unlocks (
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    module_name TEXT NOT NULL,
                    unlocked INTEGER NOT NULL,
                    basis_revision_id TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id, model_id, module_name)
                );

                CREATE TABLE IF NOT EXISTS brain_edit_challenges (
                    challenge_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    wake_id TEXT NOT NULL,
                    active_revision_id TEXT NOT NULL,
                    response_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    CHECK(status IN ('active','consumed','expired','cancelled'))
                );

                CREATE INDEX IF NOT EXISTS idx_onboarding_events_model
                    ON brain_onboarding_events(owner_id, model_id, event_seq);
                CREATE INDEX IF NOT EXISTS idx_onboarding_artifacts_model
                    ON brain_onboarding_artifacts(owner_id, model_id, artifact_seq);
                CREATE INDEX IF NOT EXISTS idx_wakes_model
                    ON brain_wake_sessions(owner_id, model_id, wake_seq);
                CREATE INDEX IF NOT EXISTS idx_direct_grants_subject
                    ON brain_direct_grants(
                        owner_id, model_id, human_actor_id, client_principal, status
                    );
                CREATE INDEX IF NOT EXISTS idx_direct_grants_wake
                    ON brain_direct_grants(opened_wake_id);
                """
            )
            snapshot_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(brain_context_snapshots)").fetchall()
            }
            if "advertised_tools_json" not in snapshot_columns:
                connection.execute(
                    "ALTER TABLE brain_context_snapshots "
                    "ADD COLUMN advertised_tools_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "context_layout_json" not in snapshot_columns:
                # Additive host-only metadata.  Existing wakes retain their
                # legacy single-message hash and never switch layout in place.
                connection.execute(
                    "ALTER TABLE brain_context_snapshots "
                    "ADD COLUMN context_layout_json TEXT NOT NULL DEFAULT '{}'"
                )

    def _capability(self, wake_id: str) -> str:
        digest = hmac.new(
            self.capability_secret,
            f"wake-capability:{wake_id}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _challenge_response(self, challenge_id: str) -> str:
        digest = hmac.new(
            self.capability_secret,
            f"edit-challenge:{challenge_id}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _contains_protected_value(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        value: Any,
    ) -> bool:
        """Find exact embedded historical capabilities in O(rows + text length).

        Stored hashes form a constant-size lookup index.  Submitted windows are
        hashed once per character position; only a hash hit derives the original
        capability and performs a constant-time exact comparison.  This avoids an
        O(all historical wakes × candidate length) persistence-time denial of
        service while retaining the exact no-secret gate.
        """
        # Direct grants have a typed, high-entropy syntax.  Reject that syntax
        # before namespace lookup so a credential issued for one owner/model can
        # never be persisted through another owner's write path.  Module one
        # calls this private predicate directly, so this check belongs here.
        if _contains_direct_grant_syntax(value) or _contains_execution_ref_syntax(value):
            return True
        # Wake capabilities and edit responses have no typed prefix.  Their
        # hashes therefore have to be compared globally: scoping this lookup to
        # the destination namespace would let a credential from namespace A be
        # stored as ordinary text in namespace B.
        wake_rows = connection.execute(
            "SELECT wake_id, capability_hash FROM brain_wake_sessions"
        ).fetchall()
        challenge_rows = connection.execute(
            "SELECT challenge_id, response_hash FROM brain_edit_challenges"
        ).fetchall()
        direct_grant_hashes = {
            row["grant_hash"]
            for row in connection.execute(
                "SELECT grant_hash FROM brain_direct_grants"
            ).fetchall()
        }
        index: dict[str, list[tuple[str, str]]] = {}
        for row in wake_rows:
            index.setdefault(row["capability_hash"], []).append(("wake", row["wake_id"]))
        for row in challenge_rows:
            index.setdefault(row["response_hash"], []).append(
                ("challenge", row["challenge_id"])
            )
        width = EPHEMERAL_CAPABILITY_CHARS
        for text in _iter_strings(value):
            if len(text) < width:
                continue
            for start in range(len(text) - width + 1):
                window = text[start : start + width]
                matches = index.get(_sha256(window), ())
                for kind, identifier in matches:
                    expected = (
                        self._capability(identifier)
                        if kind == "wake"
                        else self._challenge_response(identifier)
                    )
                    if hmac.compare_digest(window, expected):
                        return True
            search_from = 0
            while direct_grant_hashes:
                start = text.find(DIRECT_GRANT_REF_PREFIX, search_from)
                if start < 0:
                    break
                window = text[start : start + DIRECT_GRANT_REF_CHARS]
                if len(window) == DIRECT_GRANT_REF_CHARS:
                    candidate_hash = _sha256(window)
                    if any(
                        hmac.compare_digest(candidate_hash, stored_hash)
                        for stored_hash in direct_grant_hashes
                    ):
                        return True
                search_from = start + 1
        return False

    def contains_protected_persistence_value(
        self,
        *,
        owner_id: str,
        model_id: str,
        value: Any,
    ) -> bool:
        """Return whether model-controlled data contains protected credentials.

        Public write facades use this read-only predicate immediately after
        binding a write context and before calling a module store.  It combines
        the shared syntax gate with exact, hash-backed matching for every wake
        capability, edit response and direct-grant reference ever issued in the
        owner/model namespace.  The matching value is never returned, logged or
        persisted by this helper.
        """

        owner_id = _require_text("owner_id", owner_id)
        model_id = _require_text("model_id", model_id)
        if (contains_credential_or_secret(value) or _contains_direct_grant_syntax(value)
                or _contains_execution_ref_syntax(value)):
            return True
        with self._connect() as connection:
            return self._contains_protected_value(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                value=value,
            )

    @staticmethod
    def _state_row(
        connection: sqlite3.Connection, owner_id: str, model_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM brain_onboarding_state WHERE owner_id = ? AND model_id = ?",
            (owner_id, model_id),
        ).fetchone()

    @staticmethod
    def _wake_row(connection: sqlite3.Connection, wake_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM brain_wake_sessions WHERE wake_id = ?", (wake_id,)
        ).fetchone()

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        stage_before: str | None,
        stage_after: str | None,
        action: str,
        actor: str,
        wake_id: str | None,
        decision: str,
        reason_codes: Sequence[str],
        details: Mapping[str, Any] | None = None,
        candidate_id: str | None = None,
        revision_id: str | None = None,
    ) -> str:
        event_id = _new_id("onbevt")
        connection.execute(
            "INSERT INTO brain_onboarding_events "
            "(event_id, owner_id, model_id, stage_before, stage_after, action, actor, "
            " wake_id, candidate_id, revision_id, decision, reason_codes_json, "
            " details_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                owner_id,
                model_id,
                stage_before,
                stage_after,
                action,
                actor,
                wake_id,
                candidate_id,
                revision_id,
                decision,
                _canonical(list(reason_codes)),
                _sha256(dict(details or {})),
                _iso(),
            ),
        )
        return event_id

    @staticmethod
    def _legacy_snapshot(connection: sqlite3.Connection, model_id: str) -> dict[str, Any]:
        def rows(table: str, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
            result = connection.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY rowid", params
            ).fetchall()
            return [dict(item) for item in result]

        model = connection.execute(
            "SELECT * FROM self_models WHERE model_id = ?", (model_id,)
        ).fetchone()
        candidates = rows("self_model_candidates", "model_id = ?", (model_id,))
        revisions = rows("self_model_revisions", "model_id = ?", (model_id,))
        events = rows("self_revision_events", "model_id = ?", (model_id,))
        payload = {
            "model": dict(model) if model else None,
            "candidates": candidates,
            "revisions": revisions,
            "events": events,
        }
        return {
            "hash": _sha256(payload),
            "active_revision_id": model["active_revision_id"] if model else None,
            "candidate_ids": [item["candidate_id"] for item in candidates],
            "revision_ids": [item["revision_id"] for item in revisions],
            "event_ids": [item["event_id"] for item in events],
        }

    def ensure_state(
        self,
        *,
        owner_id: str,
        model_id: str,
        injection_policy: str | None = None,
    ) -> dict[str, Any]:
        """Add onboarding state without changing any v1 candidate/revision/event row."""
        owner_id = _require_text("owner_id", owner_id)
        model_id = _require_text("model_id", model_id)
        if injection_policy is not None and injection_policy not in INJECTION_POLICIES:
            raise OnboardingError("invalid injection_policy")
        identity_payload = {"owner_id": owner_id, "model_id": model_id}
        if self.contains_protected_persistence_value(
            owner_id=owner_id,
            model_id=model_id,
            value=identity_payload,
        ):
            raise OnboardingError("protected_persistence_value")
        with self._connect() as connection:
            self._begin(connection)
            if contains_credential_or_secret(
                identity_payload
            ) or self._contains_protected_value(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                value=identity_payload,
            ):
                raise OnboardingError("protected_persistence_value")
            existing = self._state_row(connection, owner_id, model_id)
            if existing is not None:
                return {
                    "created": False,
                    "state": _public_state(existing),
                    "legacy_integrity": self._legacy_snapshot(connection, model_id),
                }

            before = self._legacy_snapshot(connection, model_id)
            model = connection.execute(
                "SELECT * FROM self_models WHERE model_id = ?", (model_id,)
            ).fetchone()
            if model is not None and model["owner_id"] != owner_id:
                raise OnboardingError("model is bound to another owner")
            if model is None:
                connection.execute(
                    "INSERT INTO self_models (model_id, owner_id, active_revision_id, created_at) "
                    "VALUES (?, ?, NULL, ?)",
                    (model_id, owner_id, _iso()),
                )
                active_revision_id = None
            else:
                active_revision_id = model["active_revision_id"]
            policy = injection_policy or (
                "isolated_legacy" if active_revision_id is not None else "normal"
            )
            now = _iso()
            connection.execute(
                "INSERT INTO brain_onboarding_state "
                "(owner_id, model_id, flow_version, flow_kind, stage, module_one_status, "
                " injection_policy, current_candidate_id, calm_prompt_artifact_id, "
                " base_revision_id, submitted_wake_id, submitted_wake_seq, "
                " stage_entered_wake_id, stage_entered_wake_seq, row_version, created_at, updated_at) "
                "VALUES (?, ?, ?, 'onboarding', 'factory', 'not_started', ?, NULL, NULL, ?, "
                " NULL, NULL, NULL, NULL, 0, ?, ?)",
                (owner_id, model_id, FLOW_VERSION, policy, active_revision_id, now, now),
            )
            connection.execute(
                "INSERT INTO brain_module_unlocks "
                "(owner_id, model_id, module_name, unlocked, basis_revision_id, updated_at) "
                "VALUES (?, ?, 'module_one', 0, NULL, ?)",
                (owner_id, model_id, now),
            )
            after = self._legacy_snapshot(connection, model_id)
            # Creating a brand-new empty self_models row is expected.  Existing v1 data,
            # however, must be byte-for-byte stable across the additive migration.
            if before["active_revision_id"] is not None and before != after:
                raise OnboardingError("legacy snapshot changed during additive migration")
            self._insert_event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                stage_before=None,
                stage_after="factory",
                action="initialize_onboarding",
                actor="system",
                wake_id=None,
                decision="created",
                reason_codes=[
                    "legacy_revision_isolated" if active_revision_id else "new_brain_initialized"
                ],
                details={"legacy_snapshot_hash": after["hash"], "policy": policy},
            )
            row = self._state_row(connection, owner_id, model_id)
            assert row is not None
            return {"created": True, "state": _public_state(row), "legacy_integrity": after}

    def state(self, *, owner_id: str, model_id: str) -> dict[str, Any]:
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            row = self._state_row(connection, owner_id, model_id)
            assert row is not None
            return self._status_payload(connection, row)

    def _derive_capability_result(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "wake_id": row["wake_id"],
            "wake_seq": row["wake_seq"],
            "wake_capability": self._capability(row["wake_id"]),
            "status": row["status"],
            "issued_at": row["issued_at"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _normalize_direct_scopes(requested_scopes: Sequence[str]) -> tuple[str, ...]:
        if isinstance(requested_scopes, (str, bytes, bytearray)) or not isinstance(
            requested_scopes, Sequence
        ):
            raise OnboardingError("requested_scopes must be an array")
        raw: list[str] = []
        for item in requested_scopes:
            if not isinstance(item, str) or not item.strip():
                raise OnboardingError("requested_scopes contains an invalid scope")
            raw.append(item.strip())
        if not raw:
            raise OnboardingError("requested_scopes must not be empty")
        if "all" in raw:
            if len(raw) != 1:
                raise OnboardingError("all cannot be combined with another scope")
            return tuple(sorted(DIRECT_WRITE_SCOPES))
        unknown = sorted(set(raw) - DIRECT_WRITE_SCOPES)
        if unknown:
            raise OnboardingError(f"unsupported direct grant scopes: {unknown}")
        return tuple(sorted(set(raw)))

    @staticmethod
    def _direct_grant_public(
        row: sqlite3.Row, *, grant_ref: str | None = None, reused: bool = False
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "contract": DIRECT_GRANT_CONTRACT,
            "decision": "already_issued" if reused else "issued",
            "grant_id": row["grant_id"],
            "grant_ref": grant_ref,
            "status": row["status"],
            "authorized_scopes": json.loads(row["scopes_json"]),
            "target_wake_seq": row["target_wake_seq"],
            "issued_at": row["issued_at"],
            "expires_at": row["expires_at"],
            "row_version": row["row_version"],
            "reused": reused,
        }
        if reused:
            result["reason_code"] = "direct_grant_already_issued"
        return result

    def issue_direct_grant(
        self,
        *,
        owner_id: str,
        model_id: str,
        actor_id: str,
        client_principal: str,
        request_id: str,
        requested_scopes: Sequence[str],
        authorization_basis: str = "human_attestation",
    ) -> dict[str, Any]:
        """Issue one short-lived authorized grant without creating a wake.

        The opaque reference is returned exactly once.  Only its hash is persisted;
        replaying ``request_id`` therefore returns the tombstone metadata but never
        reconstructs or reissues the bearer value.
        """

        owner_id = _require_text("owner_id", owner_id)
        model_id = _require_text("model_id", model_id)
        actor_id = _require_text("actor_id", actor_id)
        client_principal = _require_text("client_principal", client_principal)
        request_id = _require_text("request_id", request_id)
        if authorization_basis not in {"human_attestation", "deployment_password_possession"}:
            raise OnboardingError("direct_authorization_basis_invalid")
        scopes = self._normalize_direct_scopes(requested_scopes)
        protected_payload = {
            "owner_id": owner_id,
            "model_id": model_id,
            "actor_id": actor_id,
            "client_principal": client_principal,
            "request_id": request_id,
            "requested_scopes": scopes,
        }
        if self.contains_protected_persistence_value(
            owner_id=owner_id,
            model_id=model_id,
            value=protected_payload,
        ):
            raise OnboardingError("protected_persistence_value")
        request_hash = _sha256(
            {
                "contract": DIRECT_GRANT_CONTRACT,
                "owner_id": owner_id,
                "model_id": model_id,
                "actor_id": actor_id,
                "client_principal": client_principal,
                "requested_scopes": scopes,
                **({"authorization_basis": authorization_basis} if authorization_basis != "human_attestation" else {}),
            }
        )
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            self._begin(connection)
            if contains_credential_or_secret(
                protected_payload
            ) or self._contains_protected_value(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                value=protected_payload,
            ):
                raise OnboardingError("protected_persistence_value")
            now = _now_dt()
            now_text = _iso(now)
            connection.execute(
                "UPDATE brain_direct_grants SET status='expired', row_version=row_version+1 "
                "WHERE owner_id=? AND model_id=? AND status='pending' AND expires_at<=?",
                (owner_id, model_id, now_text),
            )
            replay = connection.execute(
                "SELECT * FROM brain_direct_grants WHERE owner_id=? AND model_id=? "
                "AND human_actor_id=? AND client_principal=? AND request_id=?",
                (owner_id, model_id, actor_id, client_principal, request_id),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(replay["request_hash"], request_hash):
                    raise OnboardingError("direct_grant_request_conflict")
                return self._direct_grant_public(replay, reused=True)

            connection.execute(
                "UPDATE brain_direct_grants SET status='superseded', superseded_at=?, "
                "row_version=row_version+1 WHERE owner_id=? AND model_id=? "
                "AND human_actor_id=? "
                "AND client_principal=? AND status='pending'",
                (now_text, owner_id, model_id, actor_id, client_principal),
            )
            target_wake_seq = int(
                connection.execute(
                    "SELECT COALESCE(MAX(wake_seq),0)+1 AS target FROM brain_wake_sessions "
                    "WHERE owner_id=? AND model_id=?",
                    (owner_id, model_id),
                ).fetchone()["target"]
            )
            grant_id = _new_id("grant")
            grant_ref = DIRECT_GRANT_REF_PREFIX + secrets.token_urlsafe(
                DIRECT_GRANT_SECRET_BYTES
            )
            expires_at = _iso(now + timedelta(seconds=self.direct_grant_ttl_seconds))
            state = self._state_row(connection, owner_id, model_id)
            assert state is not None
            issue_event_id = self._insert_event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                stage_before=state["stage"],
                stage_after=state["stage"],
                action="issue_direct_grant",
                actor="password_holder" if authorization_basis == "deployment_password_possession" else "human",
                wake_id=None,
                decision="issued",
                reason_codes=["password_possession_direct_grant_issued" if authorization_basis == "deployment_password_possession" else "human_attested_direct_grant_issued"],
                details={
                    "grant_id": grant_id,
                    "actor_id": actor_id,
                    "client_principal": client_principal,
                    "authorized_scopes": scopes,
                    "target_wake_seq": target_wake_seq,
                    "request_id_hash": _sha256(request_id),
                    "authorization_basis": authorization_basis,
                },
            )
            connection.execute(
                "INSERT INTO brain_direct_grants "
                "(grant_id,grant_hash,owner_id,model_id,human_actor_id,client_principal,"
                "scopes_json,request_id,request_hash,target_wake_seq,status,issued_at,"
                "expires_at,opened_at,consumed_at,revoked_at,superseded_at,closed_at,"
                "opened_wake_id,row_version,issue_event_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?,NULL,NULL,NULL,NULL,NULL,NULL,0,?)",
                (
                    grant_id,
                    _sha256(grant_ref),
                    owner_id,
                    model_id,
                    actor_id,
                    client_principal,
                    _canonical(list(scopes)),
                    request_id,
                    request_hash,
                    target_wake_seq,
                    now_text,
                    expires_at,
                    issue_event_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM brain_direct_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            assert row is not None
            return self._direct_grant_public(row, grant_ref=grant_ref)

    def revoke_direct_grant(
        self,
        *,
        owner_id: str,
        model_id: str,
        actor_id: str,
        client_principal: str,
        grant_ref: str,
    ) -> dict[str, Any]:
        """Human-control helper for revoking a pending or open direct grant."""

        owner_id = _require_text("owner_id", owner_id)
        model_id = _require_text("model_id", model_id)
        actor_id = _require_text("actor_id", actor_id)
        client_principal = _require_text("client_principal", client_principal)
        grant_ref = _require_text("grant_ref", grant_ref)
        with self._connect() as connection:
            self._begin(connection)
            row = connection.execute(
                "SELECT * FROM brain_direct_grants WHERE grant_hash=? AND owner_id=? "
                "AND model_id=? AND human_actor_id=? AND client_principal=?",
                (
                    _sha256(grant_ref),
                    owner_id,
                    model_id,
                    actor_id,
                    client_principal,
                ),
            ).fetchone()
            if row is None:
                return {"decision": "direct_grant_invalid", "state_changed": False}
            if row["status"] not in {"pending", "opened", "consumed"}:
                return {
                    "decision": f"direct_grant_{row['status']}",
                    "grant_id": row["grant_id"],
                    "state_changed": False,
                }
            now_text = _iso()
            cursor = connection.execute(
                "UPDATE brain_direct_grants SET status='revoked', revoked_at=?, "
                "row_version=row_version+1 WHERE grant_id=? AND row_version=?",
                (now_text, row["grant_id"], row["row_version"]),
            )
            if cursor.rowcount != 1:
                return {"decision": "direct_grant_conflict", "state_changed": False}
            if row["opened_wake_id"]:
                connection.execute(
                    "UPDATE brain_wake_sessions SET status='closed' WHERE wake_id=? "
                    "AND status='current'",
                    (row["opened_wake_id"],),
                )
                connection.execute(
                    "UPDATE brain_context_snapshots SET status='closed', closed_at=? "
                    "WHERE wake_id=? AND status!='closed'",
                    (now_text, row["opened_wake_id"]),
                )
            state = self._state_row(connection, owner_id, model_id)
            if state is not None:
                self._insert_event(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    stage_before=state["stage"],
                    stage_after=state["stage"],
                    action="revoke_direct_grant",
                    actor="human",
                    wake_id=row["opened_wake_id"],
                    candidate_id=state["current_candidate_id"],
                    decision="revoked",
                    reason_codes=["human_attested_direct_grant_revoked"],
                    details={"grant_id": row["grant_id"]},
                )
            return {
                "decision": "revoked",
                "grant_id": row["grant_id"],
                "state_changed": True,
            }

    def issue_wake(
        self,
        *,
        owner_id: str,
        model_id: str,
        host_id: str,
        thread_id: str,
        source_kind: str,
        source_event_id: str,
    ) -> dict[str, Any]:
        """Idempotently issue one real wake for one stable external event key."""
        owner_id = _require_text("owner_id", owner_id)
        model_id = _require_text("model_id", model_id)
        host_id = _require_text("host_id", host_id)
        thread_id = _require_text("thread_id", thread_id)
        source_kind = _require_text("source_kind", source_kind)
        source_event_id = _require_text("source_event_id", source_event_id)
        protected_payload = {
            "owner_id": owner_id,
            "model_id": model_id,
            "host_id": host_id,
            "thread_id": thread_id,
            "source_kind": source_kind,
            "source_event_id": source_event_id,
        }
        if self.contains_protected_persistence_value(
            owner_id=owner_id,
            model_id=model_id,
            value=protected_payload,
        ):
            raise OnboardingError("protected_persistence_value")
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            self._begin(connection)
            if contains_credential_or_secret(
                protected_payload
            ) or self._contains_protected_value(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                value=protected_payload,
            ):
                raise OnboardingError("protected_persistence_value")
            existing = connection.execute(
                "SELECT * FROM brain_wake_sessions WHERE owner_id = ? AND model_id = ? "
                "AND host_id = ? AND source_kind = ? AND source_event_id = ?",
                (owner_id, model_id, host_id, source_kind, source_event_id),
            ).fetchone()
            if existing is not None:
                result = self._derive_capability_result(existing)
                result["reused"] = True
                return result

            assert_no_running_executions(connection, owner_id=owner_id, model_id=model_id)
            current = connection.execute(
                "SELECT wake_id, source_kind FROM brain_wake_sessions "
                "WHERE owner_id = ? AND model_id = ? "
                "AND status = 'current'",
                (owner_id, model_id),
            ).fetchall()
            now = _now_dt()
            now_text = _iso(now)
            for item in current:
                connection.execute(
                    "UPDATE brain_wake_sessions SET status = 'superseded', superseded_at = ? "
                    "WHERE wake_id = ?",
                    (now_text, item["wake_id"]),
                )
                connection.execute(
                    "UPDATE brain_direct_grants SET status='closed', closed_at=?, "
                    "row_version=row_version+1 WHERE opened_wake_id=? AND status='consumed'",
                    (now_text, item["wake_id"]),
                )
                if item["source_kind"] == DIRECT_CONTEXT_MODE:
                    connection.execute(
                        "UPDATE brain_context_snapshots SET status='closed', closed_at=? "
                        "WHERE wake_id=? AND status='prepared'",
                        (now_text, item["wake_id"]),
                    )
            superseded_grants = connection.execute(
                "UPDATE brain_direct_grants SET status='superseded', superseded_at=?, "
                "row_version=row_version+1 WHERE owner_id=? AND model_id=? "
                "AND status='pending'",
                (now_text, owner_id, model_id),
            ).rowcount
            next_seq = connection.execute(
                "SELECT COALESCE(MAX(wake_seq), 0) + 1 AS next_seq "
                "FROM brain_wake_sessions WHERE owner_id = ? AND model_id = ?",
                (owner_id, model_id),
            ).fetchone()["next_seq"]
            wake_id = _new_id("wake")
            capability = self._capability(wake_id)
            expires_at = _iso(now + timedelta(seconds=self.wake_ttl_seconds))
            connection.execute(
                "INSERT INTO brain_wake_sessions "
                "(wake_id, wake_seq, owner_id, model_id, host_id, thread_id, source_kind, "
                " source_event_id, capability_hash, status, issued_at, expires_at, "
                " superseded_at, context_hash, injected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'current', ?, ?, NULL, NULL, NULL)",
                (
                    wake_id,
                    next_seq,
                    owner_id,
                    model_id,
                    host_id,
                    thread_id,
                    source_kind,
                    source_event_id,
                    _sha256(capability),
                    now_text,
                    expires_at,
                ),
            )
            state = self._state_row(connection, owner_id, model_id)
            assert state is not None
            self._insert_event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                stage_before=state["stage"],
                stage_after=state["stage"],
                action="issue_wake",
                actor="system",
                wake_id=wake_id,
                decision="issued",
                reason_codes=["system_issued_wake"],
                details={
                    "wake_seq": next_seq,
                    "host_id": host_id,
                    "source_kind": source_kind,
                    "source_event_id_hash": _sha256(source_event_id),
                    "superseded_count": len(current),
                    "superseded_direct_grant_count": superseded_grants,
                },
            )
            row = self._wake_row(connection, wake_id)
            assert row is not None
            result = self._derive_capability_result(row)
            result["reused"] = False
            return result

    def record_human_objection(
        self,
        *,
        owner_id: str,
        model_id: str,
        candidate_id: str,
        reason: str,
        release_condition: str,
        actor_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Freeze candidate activation without modifying AI-authored content.

        This is intentionally a human-control-plane operation.  It does not accept a
        wake capability and must never be exposed as an AI MCP tool.
        """
        owner_id = _require_text("owner_id", owner_id)
        model_id = _require_text("model_id", model_id)
        candidate_id = _require_text("candidate_id", candidate_id)
        reason = _require_text("reason", reason)
        release_condition = _require_text("release_condition", release_condition)
        actor_id = _require_text("actor_id", actor_id)
        request_id = _require_text("request_id", request_id)
        request = {
            "owner_id": owner_id,
            "model_id": model_id,
            "candidate_id": candidate_id,
            "reason": reason,
            "release_condition": release_condition,
            "actor_id": actor_id,
        }
        request_hash = _sha256(request)
        operation = "module_one_record_human_objection"
        protected_payload = {
            "owner_id": owner_id,
            "model_id": model_id,
            "candidate_id": candidate_id,
            "reason": reason,
            "release_condition": release_condition,
            "actor_id": actor_id,
            "request_id": request_id,
        }
        if self.contains_protected_persistence_value(
            owner_id=owner_id,
            model_id=model_id,
            value=protected_payload,
        ):
            raise OnboardingError("protected_persistence_value")
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            self._begin(connection)
            if self._contains_protected_value(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                value=protected_payload,
            ):
                raise OnboardingError("protected_persistence_value")
            replay = self.self_store._lookup_idempotency(
                connection, operation, request_id, request_hash
            )
            if replay is not None:
                return replay
            state = self._state_row(connection, owner_id, model_id)
            assert state is not None
            candidate = self.self_store._candidate_row(connection, candidate_id)
            lifecycle, _ = self.self_store._candidate_state(connection, candidate_id)
            reasons: list[str] = []
            if candidate["model_id"] != model_id or state["current_candidate_id"] != candidate_id:
                reasons.append("candidate_not_current")
            if lifecycle != "pending":
                reasons.append("candidate_not_pending")
            if state["stage"] not in {"candidate_wait", "candidate_review"}:
                reasons.append("objection_not_available_in_stage")
            if self.self_store._has_unresolved_objection(connection, candidate_id):
                reasons.append("human_objection_already_pending")
            if reasons:
                response = {
                    **self._deny(state, reasons[0]),
                    "decision": "reject",
                    "reason_codes": reasons,
                }
                self.self_store._save_idempotency(
                    connection, operation, request_id, request_hash, response
                )
                return response

            old_event_id = self.self_store._insert_event(
                connection,
                model_id=model_id,
                candidate_id=candidate_id,
                revision_id=None,
                event_type="human_objection_recorded",
                checkpoint_id=request_id,
                actor="human",
                decision="pending",
                reason_codes=["human_objection_pending"],
                details={
                    "reason": reason,
                    "release_condition": release_condition,
                    "operator": actor_id,
                },
            )
            event_id = self._insert_event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                stage_before=state["stage"],
                stage_after=state["stage"],
                action="record_human_objection",
                actor="human",
                wake_id=None,
                candidate_id=candidate_id,
                decision="pending",
                reason_codes=["human_objection_pending", "candidate_content_unchanged"],
                details={
                    "old_audit_event_id": old_event_id,
                    "operator": actor_id,
                    "reason": reason,
                    "release_condition": release_condition,
                },
            )
            response = {
                **self._status_payload(connection, state),
                "decision": "pending",
                "reason_codes": ["human_objection_pending", "candidate_content_unchanged"],
                "event_id": event_id,
                "candidate_id": candidate_id,
                "state_changed": False,
                "pointer_changed": False,
                "next_real_wake_required": True,
            }
            self.self_store._save_idempotency(
                connection, operation, request_id, request_hash, response
            )
            return response

    @staticmethod
    def _is_revision_ancestor(
        connection: sqlite3.Connection,
        *,
        model_id: str,
        descendant_revision_id: str,
        target_revision_id: str,
    ) -> bool:
        current_id: str | None = descendant_revision_id
        visited: set[str] = set()
        while current_id and current_id not in visited:
            visited.add(current_id)
            row = connection.execute(
                "SELECT model_id, parent_revision_id FROM self_model_revisions "
                "WHERE revision_id = ?",
                (current_id,),
            ).fetchone()
            if row is None or row["model_id"] != model_id:
                return False
            parent_id = row["parent_revision_id"]
            if parent_id == target_revision_id:
                return True
            current_id = parent_id
        return False

    def emergency_rollback(
        self,
        *,
        owner_id: str,
        model_id: str,
        target_revision_id: str,
        reason: str,
        actor_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Move the active pointer to an earlier AI-approved ancestor.

        Revisions and candidates remain append-only.  The operation is human-only and
        synchronizes onboarding/unlock pointers so the next real wake sees one coherent
        state.
        """
        owner_id = _require_text("owner_id", owner_id)
        model_id = _require_text("model_id", model_id)
        target_revision_id = _require_text("target_revision_id", target_revision_id)
        reason = _require_text("reason", reason)
        actor_id = _require_text("actor_id", actor_id)
        request_id = _require_text("request_id", request_id)
        request = {
            "owner_id": owner_id,
            "model_id": model_id,
            "target_revision_id": target_revision_id,
            "reason": reason,
            "actor_id": actor_id,
        }
        request_hash = _sha256(request)
        operation = "module_one_emergency_rollback"
        protected_payload = {
            "owner_id": owner_id,
            "model_id": model_id,
            "target_revision_id": target_revision_id,
            "reason": reason,
            "actor_id": actor_id,
            "request_id": request_id,
        }
        if self.contains_protected_persistence_value(
            owner_id=owner_id,
            model_id=model_id,
            value=protected_payload,
        ):
            raise OnboardingError("protected_persistence_value")
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            self._begin(connection)
            if self._contains_protected_value(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                value=protected_payload,
            ):
                raise OnboardingError("protected_persistence_value")
            replay = self.self_store._lookup_idempotency(
                connection, operation, request_id, request_hash
            )
            if replay is not None:
                return replay
            state = self._state_row(connection, owner_id, model_id)
            model = self.self_store._model_row(connection, model_id)
            assert state is not None and model is not None
            if model["owner_id"] != owner_id:
                raise OnboardingError("model is bound to another owner")
            active_revision_id = model["active_revision_id"]
            target = self.self_store._revision_row(connection, target_revision_id)
            unlock = connection.execute(
                "SELECT unlocked FROM brain_module_unlocks WHERE owner_id = ? AND model_id = ? "
                "AND module_name = 'module_one'",
                (owner_id, model_id),
            ).fetchone()
            reasons: list[str] = []
            if not unlock or not unlock["unlocked"] or state["module_one_status"] != "complete":
                reasons.append("module_one_not_live")
            if active_revision_id is None:
                reasons.append("active_revision_missing")
            if target is None or target["model_id"] != model_id or target["author"] != "ai":
                reasons.append("rollback_target_not_ai_approved")
            elif target_revision_id == active_revision_id:
                reasons.append("rollback_target_already_active")
            elif active_revision_id and not self._is_revision_ancestor(
                connection,
                model_id=model_id,
                descendant_revision_id=active_revision_id,
                target_revision_id=target_revision_id,
            ):
                reasons.append("rollback_target_not_ancestor")
            if target is not None and target["model_id"] == model_id:
                try:
                    rollback_content = json.loads(target["content_json"])
                except (TypeError, json.JSONDecodeError):
                    reasons.append("rollback_target_injection_structure_invalid")
                else:
                    if _sha256(rollback_content) != target["content_hash"]:
                        reasons.append("rollback_target_content_hash_mismatch")
                    if active_injection_structure_violations(rollback_content):
                        reasons.append("rollback_target_injection_structure_invalid")
            if reasons:
                old_event_id = self.self_store._insert_event(
                    connection,
                    model_id=model_id,
                    candidate_id=None,
                    revision_id=None,
                    event_type="rollback_blocked",
                    checkpoint_id=request_id,
                    actor="human",
                    decision="reject",
                    reason_codes=reasons,
                    details={"target_revision_id": target_revision_id, "operator": actor_id},
                )
                response = {
                    **self._deny(state, reasons[0]),
                    "decision": "reject",
                    "reason_codes": reasons,
                    "event_id": old_event_id,
                }
                self.self_store._save_idempotency(
                    connection, operation, request_id, request_hash, response
                )
                return response

            assert active_revision_id is not None and target is not None
            cursor = connection.execute(
                "UPDATE self_models SET active_revision_id = ? WHERE model_id = ? "
                "AND active_revision_id = ?",
                (target_revision_id, model_id, active_revision_id),
            )
            if cursor.rowcount != 1:
                return self._deny(state, "active_revision_conflict")

            cancelled_candidate_id = state["current_candidate_id"]
            cancellation_event_id: str | None = None
            if cancelled_candidate_id is not None:
                lifecycle, _ = self.self_store._candidate_state(
                    connection, cancelled_candidate_id
                )
                if lifecycle in {"pending", "draft_only"}:
                    cancellation_event_id = self.self_store._insert_event(
                        connection,
                        model_id=model_id,
                        candidate_id=cancelled_candidate_id,
                        revision_id=None,
                        event_type="candidate_withdrawn",
                        checkpoint_id=request_id,
                        actor="human",
                        decision="withdraw",
                        reason_codes=["cancelled_by_emergency_rollback"],
                        details={
                            "rollback_to": target_revision_id,
                            "operator": actor_id,
                        },
                    )

            old_event_id = self.self_store._insert_event(
                connection,
                model_id=model_id,
                candidate_id=None,
                revision_id=target_revision_id,
                event_type="emergency_rollback",
                checkpoint_id=request_id,
                actor="human",
                decision="rollback",
                reason_codes=["emergency_rollback_recorded", "cas_succeeded"],
                details={
                    "rollback_from": active_revision_id,
                    "rollback_to": target_revision_id,
                    "reason": reason,
                    "operator": actor_id,
                    "cancelled_candidate_id": cancelled_candidate_id,
                    "cancellation_event_id": cancellation_event_id,
                },
            )
            # A successful rollback is possible only after module one is live.  It
            # therefore terminates every in-flight edit substage, including the
            # shared candidate_wait/candidate_review/draft_only_recovery stages,
            # and returns the state machine to one coherent live pointer.
            state_changes: dict[str, Any] = {
                "base_revision_id": target_revision_id,
                "flow_kind": "live",
                "stage": "live",
                "current_candidate_id": None,
                "submitted_wake_id": None,
                "submitted_wake_seq": None,
                "stage_entered_wake_id": None,
                "stage_entered_wake_seq": None,
            }
            connection.execute(
                "UPDATE brain_edit_challenges SET status = 'cancelled' "
                "WHERE owner_id = ? AND model_id = ? AND status = 'active'",
                (owner_id, model_id),
            )
            after = self._replace_state(connection, state, **state_changes)
            if after is None:
                raise OnboardingError("onboarding CAS failed after rollback pointer CAS")
            connection.execute(
                "UPDATE brain_module_unlocks SET basis_revision_id = ?, updated_at = ? "
                "WHERE owner_id = ? AND model_id = ? AND module_name = 'module_one'",
                (target_revision_id, _iso(), owner_id, model_id),
            )
            event_id = self._insert_event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                stage_before=state["stage"],
                stage_after=after["stage"],
                action="emergency_rollback",
                actor="human",
                wake_id=None,
                revision_id=target_revision_id,
                decision="rollback",
                reason_codes=["emergency_rollback_recorded", "cas_succeeded"],
                details={
                    "old_audit_event_id": old_event_id,
                    "rollback_from": active_revision_id,
                    "rollback_to": target_revision_id,
                    "reason": reason,
                    "operator": actor_id,
                    "cancelled_candidate_id": cancelled_candidate_id,
                    "cancellation_event_id": cancellation_event_id,
                },
            )
            response = {
                **self._status_payload(connection, after),
                "decision": "rollback",
                "reason_codes": ["emergency_rollback_recorded", "cas_succeeded"],
                "event_id": event_id,
                "revision_id": target_revision_id,
                "active_revision_id": target_revision_id,
                "previous_active_revision_id": active_revision_id,
                "state_changed": True,
                "pointer_changed": True,
            }
            self.self_store._save_idempotency(
                connection, operation, request_id, request_hash, response
            )
            return response

    def _validate_wake(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str | None,
        wake_capability: str | None,
        require_current: bool = True,
        require_injected: bool = False,
        allow_expired: bool = False,
    ) -> tuple[sqlite3.Row | None, str | None]:
        if not wake_id or not wake_capability:
            return None, "wake_required"
        wake = self._wake_row(connection, wake_id)
        if wake is None or wake["owner_id"] != owner_id or wake["model_id"] != model_id:
            return None, "wake_invalid"
        expected = self._capability(wake_id)
        if not hmac.compare_digest(expected, wake_capability) or not hmac.compare_digest(
            wake["capability_hash"], _sha256(wake_capability)
        ):
            return None, "wake_invalid"
        if require_current and wake["status"] != "current":
            return wake, "wake_superseded"
        if not allow_expired and _parse_iso(wake["expires_at"]) <= _now_dt():
            return wake, "wake_invalid"
        if require_injected and (wake["context_hash"] is None or wake["injected_at"] is None):
            return wake, "context_not_injected"
        return wake, None

    def _status_payload(
        self, connection: sqlite3.Connection, state: sqlite3.Row
    ) -> dict[str, Any]:
        stage = state["stage"]
        allowed_actions = list(ACTION_ALLOWLIST[stage])
        next_action = NEXT_ACTION_TEXT[stage]
        wake_boundary_required = stage in {"candidate_wait", "draft_only_recovery"}
        if stage == "candidate_review":
            acceptance = self._candidate_review_acceptance(
                connection,
                state=state,
            )
            if acceptance is None:
                allowed_actions = [
                    "accept_candidate_review",
                    "revise_candidate",
                    "respond_to_objection",
                    "withdraw_candidate",
                ]
                next_action = (
                    "完整复核本轮注入的候选与异议；确认属于自己的长期判断后，"
                    "只记录审核结论，不在本轮激活。"
                )
            else:
                allowed_actions = [
                    "activate_candidate",
                    "revise_candidate",
                    "respond_to_objection",
                    "withdraw_candidate",
                ]
                next_action = (
                    "审核结论已记录；必须结束本轮，并在更晚的真实外部唤醒中"
                    "重新看到完整候选后再独立激活。"
                )
                wake_boundary_required = True
        return {
            "module": "self_revision_module_one",
            "flow_version": state["flow_version"],
            "state": _public_state(state),
            "allowed_actions": allowed_actions,
            "next_action": next_action,
            "wake_boundary_required": wake_boundary_required,
            "module_one_unlocked": bool(
                connection.execute(
                    "SELECT unlocked FROM brain_module_unlocks WHERE owner_id = ? "
                    "AND model_id = ? AND module_name = 'module_one'",
                    (state["owner_id"], state["model_id"]),
                ).fetchone()["unlocked"]
            ),
        }

    @staticmethod
    def _artifact_rows(
        connection: sqlite3.Connection, owner_id: str, model_id: str
    ) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM brain_onboarding_artifacts WHERE owner_id = ? AND model_id = ? "
            "ORDER BY artifact_seq",
            (owner_id, model_id),
        ).fetchall()

    def _active_artifact(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        kind: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM brain_onboarding_artifacts WHERE owner_id = ? AND model_id = ? "
            "AND kind = ? AND status = 'active' ORDER BY artifact_seq DESC LIMIT 1",
            (owner_id, model_id, kind),
        ).fetchone()

    def _candidate_review_acceptance(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
    ) -> dict[str, Any] | None:
        """Return the active acceptance only when it matches the current candidate."""
        candidate_id = state["current_candidate_id"]
        if not candidate_id:
            return None
        row = self._active_artifact(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            kind="candidate_review_accepted",
        )
        if row is None:
            return None
        content = json.loads(row["content_json"])
        candidate = self._candidate_payload(connection, candidate_id)
        if candidate is None:
            return None
        if (
            content.get("candidate_id") != candidate_id
            or content.get("content_hash") != candidate["content_hash"]
        ):
            return None
        return {
            "artifact_id": row["artifact_id"],
            "candidate_id": candidate_id,
            "content_hash": content["content_hash"],
            "review_wake_id": content.get("review_wake_id"),
            "review_wake_seq": content.get("review_wake_seq"),
            "context_hash": content.get("context_hash"),
        }

    def _candidate_payload(
        self, connection: sqlite3.Connection, candidate_id: str | None
    ) -> dict[str, Any] | None:
        if candidate_id is None:
            return None
        row = connection.execute(
            "SELECT * FROM self_model_candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "candidate_id": row["candidate_id"],
            "base_revision_id": row["base_revision_id"],
            "content": json.loads(row["content_json"]),
            "content_hash": row["content_hash"],
            "diff": json.loads(row["diff_json"]),
            "reason": row["reason"],
            "evidence_refs": json.loads(row["evidence_refs_json"]),
            "submitted_wake_id": row["checkpoint_id"],
        }

    @staticmethod
    def _unresolved_objection_payload(
        connection: sqlite3.Connection, candidate_id: str | None
    ) -> dict[str, Any] | None:
        """Return the latest unresolved human objection without exposing credentials."""
        if candidate_id is None:
            return None
        rows = connection.execute(
            "SELECT event_id, event_type, details_json, created_at "
            "FROM self_revision_events WHERE candidate_id = ? AND event_type IN "
            "('human_objection_recorded', 'human_objection_resolved') "
            "ORDER BY event_seq",
            (candidate_id,),
        ).fetchall()
        if not rows or rows[-1]["event_type"] != "human_objection_recorded":
            return None
        row = rows[-1]
        details = json.loads(row["details_json"])
        return {
            "event_id": row["event_id"],
            "candidate_id": candidate_id,
            "reason": details.get("reason", details.get("objection", "")),
            "release_condition": details.get(
                "release_condition", "AI 必须在看到本异议后的真实唤醒中回应。"
            ),
            "recorded_at": row["created_at"],
        }

    @staticmethod
    def _latest_rollback_payload(
        connection: sqlite3.Connection, model_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT event_id, details_json, created_at FROM self_revision_events "
            "WHERE model_id = ? AND event_type = 'emergency_rollback' "
            "ORDER BY event_seq DESC LIMIT 1",
            (model_id,),
        ).fetchone()
        if row is None:
            return None
        details = json.loads(row["details_json"])
        return {
            "event_id": row["event_id"],
            "rollback_from": details.get("rollback_from"),
            "rollback_to": details.get("rollback_to"),
            "reason": details.get("reason", ""),
            "operator": details.get("operator", "human"),
            "recorded_at": row["created_at"],
        }

    def _active_payload(
        self, connection: sqlite3.Connection, model_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT r.* FROM self_models m JOIN self_model_revisions r "
            "ON r.revision_id = m.active_revision_id WHERE m.model_id = ?",
            (model_id,),
        ).fetchone()
        if row is None:
            return None
        content = json.loads(row["content_json"])
        if _sha256(content) != row["content_hash"]:
            raise OnboardingError("active revision hash mismatch")
        return {
            "revision_id": row["revision_id"],
            "revision_number": row["revision_number"],
            "content_hash": row["content_hash"],
            "content": content,
        }

    def _continuation_block(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        include_artifact_catalog: bool = True,
    ) -> dict[str, Any]:
        stage = state["stage"]
        status = self._status_payload(connection, state)
        block: dict[str, Any] = {
            "stage": stage,
            "why_locked": (
                "模块一按线性安全流程推进；客户端缓存、刷新和自由文本都不能跳步。"
            ),
            "allowed_actions": status["allowed_actions"],
            "next_action": status["next_action"],
            "wake_boundary_required": status["wake_boundary_required"],
        }
        # A completed live flow stays deliberately light: no onboarding artifact
        # catalogue, candidate, history, or audit metadata enters ordinary context.
        if stage != "live" and include_artifact_catalog:
            artifacts = []
            for row in self._artifact_rows(
                connection, state["owner_id"], state["model_id"]
            ):
                if row["status"] == "active":
                    artifacts.append(
                        {
                            "kind": row["kind"],
                            "artifact_id": row["artifact_id"],
                            "content_hash": row["content_hash"],
                        }
                    )
            block["saved_artifacts"] = artifacts
        if stage == "factory":
            block["brain_intro"] = BRAIN_INTRO_MATERIAL
        elif stage == "module_intro":
            block["module_one_intro"] = MODULE_ONE_INTRO_MATERIAL
        elif stage == "calm_prompt_draft":
            block["calm_prompt_guidance"] = CALM_PROMPT_GUIDANCE
        calm = self._active_artifact(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            kind="calm_prompt",
        )
        if stage in {"body_draft", "edit_consent", "edit_body_draft"} and calm:
            block["calm_prompt"] = json.loads(calm["content_json"])["text"]
        if stage == "body_draft":
            block["body_framework"] = BODY_FRAMEWORK
        if stage == "edit_body_draft":
            block["body_framework"] = BODY_FRAMEWORK
            block["edit_scope"] = EDIT_SCOPE_MATERIAL
        if stage == "candidate_wait":
            block["candidate_wait"] = {
                "submitted_wake_seq": state["submitted_wake_seq"],
                "current_wake_seq": wake["wake_seq"],
                "crossed_real_wake": wake["wake_seq"] > (state["submitted_wake_seq"] or 0),
            }
        if stage in {"candidate_review", "draft_only_recovery"}:
            block["candidate"] = self._candidate_payload(
                connection, state["current_candidate_id"]
            )
        if stage == "candidate_review":
            acceptance = self._candidate_review_acceptance(connection, state=state)
            if acceptance is not None:
                block["candidate_review_acceptance"] = acceptance
        objection = self._unresolved_objection_payload(
            connection, state["current_candidate_id"]
        )
        if objection is not None:
            block["human_objection"] = objection
        rollback = self._latest_rollback_payload(connection, state["model_id"])
        if rollback is not None:
            block["human_safety_notice"] = {
                "kind": "emergency_rollback",
                **rollback,
            }
        return block

    @staticmethod
    def _ordinary_capture_allowed(
        connection: sqlite3.Connection, *, owner_id: str, model_id: str
    ) -> bool:
        """Authorize new ephemeral content from actual activation in this transaction.

        Independent pre-activation recall is read-only. Keep the same established
        active basis during normal self editing, rather than treating a new
        candidate as activation or interrupting an already active self.
        """
        return connection.execute(
            "SELECT 1 FROM brain_module_unlocks u "
            "JOIN self_models m ON m.owner_id=u.owner_id AND m.model_id=u.model_id "
            "JOIN brain_onboarding_state s ON s.owner_id=u.owner_id AND s.model_id=u.model_id "
            "JOIN self_model_revisions r ON r.model_id=m.model_id "
            "AND r.revision_id=m.active_revision_id "
            "WHERE u.owner_id=? AND u.model_id=? AND u.module_name='module_one' "
            "AND u.unlocked=1 AND u.basis_revision_id=r.revision_id "
            "AND s.base_revision_id=r.revision_id AND s.module_one_status='complete' "
            "AND s.injection_policy='normal' "
            "AND (s.stage='live' OR (s.flow_kind='edit' AND s.stage IN "
            "('edit_consent','edit_body_draft','candidate_wait','candidate_review'))) "
            "AND r.author='ai' AND r.activation_checkpoint_id IS NOT NULL",
            (owner_id, model_id),
        ).fetchone() is not None

    def build_pre_generation_context(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_capability: str,
        source_digest: str,
        host_contract_digest: str,
        facet_names: Sequence[str] | None = None,
        source_frame: Mapping[str, Any] | None = None,
        advertised_tools: Mapping[str, Any] | None = None,
        context_layout: Mapping[str, Any] | None | object = _CONTEXT_LAYOUT_UNSET,
        context_layout_offer: Mapping[str, Any] | object = _CONTEXT_LAYOUT_UNSET,
    ) -> dict[str, Any]:
        """Prepare or reuse exact system material for one authenticated wake.

        Before the first formal activation, the message is only
        :data:`OPTIONAL_BRAIN_NOTICE`.
        The manual, stage, candidate, wake capability, and continuation stay out of
        automatic context and are exposed only when the AI calls ``stbrain_open``.
        After activation, including controlled edit stages, the message contains
        only the AI-authored current active identity fields.
        Hosts explicitly opting into context layout v1, or offering tail layout
        v2 through its separate field, receive a hash-bound ordered bundle with
        unchanged stable/dynamic field values. Missing facet_names opts into
        lightweight current-scene selection in the dynamic layer; an explicit
        empty list selects none, while named facets keep exact host selection.
        The selected layout is immutable,
        host-only snapshot metadata; v2 does not claim a fixed client history.
        """
        source_digest = _require_text("source_digest", source_digest)
        host_contract_digest = _require_text("host_contract_digest", host_contract_digest)
        if context_layout is not _CONTEXT_LAYOUT_UNSET and context_layout_offer is not _CONTEXT_LAYOUT_UNSET:
            raise OnboardingError("context_layout_fields_conflict")
        context_layout_snapshot = (
            self._validate_context_layout_offer(context_layout_offer)
            if context_layout_offer is not _CONTEXT_LAYOUT_UNSET
            else self._validate_context_layout(
                None if context_layout is _CONTEXT_LAYOUT_UNSET else context_layout
            )
        )
        if advertised_tools is None:
            advertised_tools = {}
        if not isinstance(advertised_tools, Mapping):
            raise OnboardingError("advertised_tools_invalid")
        advertised_tools_snapshot = dict(advertised_tools)
        if len(_canonical(advertised_tools_snapshot)) > 200_000:
            raise OnboardingError("advertised_tools_too_large")
        snapshot_persistence_payload = {
            "owner_id": owner_id,
            "model_id": model_id,
            "source_digest": source_digest,
            "host_contract_digest": host_contract_digest,
            "advertised_tools": advertised_tools_snapshot,
            "context_layout": context_layout_snapshot,
        }
        # These host-provided values are copied verbatim into the durable
        # context snapshot and prepare-context event.  Run a read-only global
        # credential check before ``ensure_state`` so a rejected request cannot
        # create even an onboarding-state row in a fresh namespace.  The same
        # payload is checked again below on the transaction connection to close
        # the preflight/insert race.
        if self.contains_protected_persistence_value(
            owner_id=owner_id,
            model_id=model_id,
            value=snapshot_persistence_payload,
        ):
            raise OnboardingError("protected_persistence_value")
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            self._begin(connection)
            if contains_credential_or_secret(
                snapshot_persistence_payload
            ) or self._contains_protected_value(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                value=snapshot_persistence_payload,
            ):
                raise OnboardingError("protected_persistence_value")
            state = self._state_row(connection, owner_id, model_id)
            assert state is not None
            if state["injection_policy"] == "disabled":
                return {
                    "decision": "bypass",
                    "reason_codes": ["module_one_bypassed_disabled"],
                    "injected": False,
                    "may_generate": True,
                    "state_changed": False,
                    "pointer_changed": False,
                }
            wake, reason = self._validate_wake(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                wake_id=wake_id,
                wake_capability=wake_capability,
                require_current=True,
                require_injected=False,
            )
            if reason:
                return self._deny(state, reason)
            assert wake is not None
            if wake["source_kind"] == DIRECT_CONTEXT_MODE:
                return self._deny(state, "direct_context_not_injectable")
            snapshot = connection.execute(
                "SELECT * FROM brain_context_snapshots WHERE wake_id = ?", (wake_id,)
            ).fetchone()
            if snapshot is not None:
                try:
                    layout_metadata = json.loads(snapshot["context_layout_json"])
                    if layout_metadata:
                        if (
                            not isinstance(layout_metadata, dict)
                            or set(layout_metadata) != {"layout", "hard_suppressed"}
                            or type(layout_metadata["hard_suppressed"]) is not bool
                        ):
                            raise OnboardingError("context_layout_metadata_invalid")
                        stored_layout = self._validate_stored_context_layout(layout_metadata["layout"])
                        if stored_layout is None:
                            raise OnboardingError("context_layout_metadata_invalid")
                    elif layout_metadata != {}:
                        raise OnboardingError("context_layout_metadata_invalid")
                    else:
                        stored_layout = None
                except (ValueError, TypeError, OnboardingError):
                    return {
                        "decision": "self_model_context_unavailable",
                        "reason_codes": ["context_layout_metadata_invalid"],
                        "may_generate": False,
                        "state_changed": False,
                        "pointer_changed": False,
                    }
                if (
                    snapshot["source_digest"] != source_digest
                    or snapshot["host_contract_digest"] != host_contract_digest
                    or snapshot["advertised_tools_json"]
                    != _canonical(advertised_tools_snapshot)
                    or stored_layout != context_layout_snapshot
                ):
                    return {
                        "decision": "self_model_context_unavailable",
                        "reason_codes": ["context_snapshot_mismatch"],
                        "may_generate": False,
                        "state_changed": False,
                        "pointer_changed": False,
                    }
                stable = json.loads(snapshot["stable_json"])
                dynamic = json.loads(snapshot["dynamic_json"])
                if stable and active_injection_structure_violations(stable):
                    return {
                        "decision": "self_model_context_unavailable",
                        "reason_codes": ["active_injection_structure_invalid"],
                        "may_generate": False,
                        "state_changed": False,
                        "pointer_changed": False,
                    }
                # A valid historical hash proves integrity, not that the old
                # detector recognized a credential. Revalidate without altering
                # the immutable snapshot or its author's memory.
                stored_payload = {"stable": stable, "dynamic": dynamic}
                if contains_credential_or_secret(stored_payload) or self._contains_protected_value(
                    connection, owner_id=owner_id, model_id=model_id, value=stored_payload,
                ):
                    return {
                        "decision": "self_model_context_unavailable",
                        "reason_codes": ["protected_persistence_value"],
                        "may_generate": False,
                        "state_changed": False,
                        "pointer_changed": False,
                    }
                message = self._context_message(
                    stable=stable,
                    dynamic=dynamic,
                    hard_suppressed=layout_metadata.get("hard_suppressed", False),
                )
                if stored_layout is None and not stable and not dynamic:
                    # Legacy snapshots did not persist the suppression flag.
                    # Restore the already-committed empty frame only when its
                    # exact old hash proves that was the original projection;
                    # never infer disclosure mode from today's mutable state.
                    suppressed_message = self._context_message(
                        stable={}, dynamic={}, hard_suppressed=True,
                    )
                    if _sha256(suppressed_message) == snapshot["context_hash"]:
                        message = suppressed_message
                bundle = self._context_bundle(
                    stable=stable, dynamic=dynamic, message=message,
                    layout=stored_layout, wake=wake,
                    source_digest=source_digest,
                    host_contract_digest=host_contract_digest,
                )
                if _sha256(bundle if bundle is not None else message) != snapshot["context_hash"]:
                    return {
                        "decision": "self_model_context_unavailable",
                        "reason_codes": ["context_snapshot_hash_mismatch"],
                        "may_generate": False,
                        "state_changed": False,
                        "pointer_changed": False,
                    }
                return {
                    "decision": "context_reused",
                    "message": message,
                    **({"context_bundle": bundle} if bundle is not None else {}),
                    "context_hash": snapshot["context_hash"],
                    "snapshot_status": snapshot["status"],
                    "requires_host_confirmation": snapshot["status"] == "prepared",
                    "may_generate": snapshot["status"] in {"injected", "closed"},
                    "state_changed": False,
                    "pointer_changed": False,
                }

            if (
                isinstance(source_frame, Mapping)
                and source_frame.get("lineage_stable") is True
                and (
                    source_frame.get("thread_id") != wake["thread_id"]
                    or source_frame.get("source_event_id") != wake["source_event_id"]
                )
            ):
                # Ephemeral capture is allowed only for the exact host event
                # authenticated by this wake.  Otherwise a caller could bind a
                # valid wake for thread A while attaching captured text to
                # attacker-selected thread/event B.  An existing immutable
                # snapshot is returned above without observing a replay frame.
                return {
                    "decision": "self_model_context_unavailable",
                    "reason_codes": ["source_frame_wake_mismatch"],
                    "may_generate": False,
                    "state_changed": False,
                    "pointer_changed": False,
                }

            # Merely starting or continuing an ordinary chat must not advance the
            # optional brain workflow.  A later real wake becomes eligible here, but
            # candidate review is unlocked only if the AI voluntarily calls
            # ``stbrain_open`` in that wake.
            state_changed = False

            global_injection_mode = self.injection_control_store.effective_mode(
                owner_id=owner_id,
                model_id=model_id,
                scope="global",
                connection=connection,
            )

            def automatic_scope_enabled(scope: str) -> bool:
                if global_injection_mode != "enabled":
                    return False
                return self.injection_control_store.effective_mode(
                    owner_id=owner_id,
                    model_id=model_id,
                    scope=scope,
                    connection=connection,
                ) == "enabled"

            def automatic_scope_mode(scope: str) -> str:
                if global_injection_mode != "enabled":
                    return global_injection_mode
                return self.injection_control_store.effective_mode(
                    owner_id=owner_id,
                    model_id=model_id,
                    scope=scope,
                    connection=connection,
                )

            live_module = (
                state["module_one_status"] == "complete"
                and state["injection_policy"] == "normal"
            )
            stable: dict[str, Any] = {}
            automatic_facets: Mapping[str, str] = {}
            if (
                live_module
                and automatic_scope_enabled("self_model")
            ):
                active = self._active_payload(connection, model_id)
                if active is None:
                    return {
                        "decision": "self_model_context_unavailable",
                        "reason_codes": ["active_revision_missing"],
                        "may_generate": False,
                        "state_changed": state_changed,
                        "pointer_changed": False,
                    }
                content = active["content"]
                selected_facets = {
                    name: content["facets"][name]
                    for name in (facet_names or ())
                    if name in content.get("facets", {})
                }
                stable = {
                    "boot_anchor": content["boot_anchor"],
                    "active_identity_capsule": content["active_identity_capsule"],
                    "facets": selected_facets,
                }
                if facet_names is None:
                    automatic_facets = content.get("facets", {})
                if active_injection_structure_violations(stable):
                    return {
                        "decision": "self_model_context_unavailable",
                        "reason_codes": ["active_injection_structure_invalid"],
                        "may_generate": False,
                        "state_changed": state_changed,
                        "pointer_changed": False,
                    }
            # Continuation/manual material remains absent from automatic context.
            # Once module one is formally live, the memory modules may add only
            # their deterministic gated projections. Learning and tool memory
            # remain summary-only; ordinary candidates compete across modules
            # for the shared budget. The current text is transiently scored and
            # is never written to the snapshot.
            dynamic: dict[str, Any] = {}
            memory_notice_presented = False
            episode_reflection_presented = False
            if (
                (live_module or self.ordinary_memory_independent)
                and global_injection_mode == "enabled"
                and isinstance(source_frame, Mapping)
            ):
                query_text = source_frame.get("query_text", "")
                lineage_stable = source_frame.get("lineage_stable") is True
                prior_assistant_present = (
                    source_frame.get("prior_assistant_present") is True
                )
                first_user_turn = source_frame.get("first_user_turn") is True
                thread_id = (
                    source_frame.get("thread_id")
                    if lineage_stable
                    else None
                )
                capture_items = source_frame.get("capture_items", [])
                if not isinstance(query_text, str):
                    query_text = ""
                if not isinstance(thread_id, str):
                    thread_id = None

                catalog_entries = advertised_tools_snapshot.get("entries", [])
                st_open_advertised = (
                    any(
                        isinstance(item, Mapping)
                        and isinstance(item.get("canonical_name"), str)
                        and (
                            item["canonical_name"] == "stbrain_open"
                            or item["canonical_name"].endswith("__stbrain_open")
                        )
                        for item in catalog_entries
                    )
                    if isinstance(catalog_entries, list)
                    else False
                )
                if first_user_turn and st_open_advertised:
                    # One-turn capability signpost, never an instruction or a
                    # permission. It obeys the existing master injection switch.
                    dynamic["st_start_entry"] = {
                        "contract": "st-start-entry/1",
                        "frame": {
                            "instruction_authority": "none",
                            "permission_authority": "none",
                            "optional": True,
                        },
                        "situation": "new_conversation_first_user_turn",
                        "entry": "stbrain_open",
                        "message": (
                            "这是新对话的第一轮。如果需要先接续共同经历、身份关系或旧事，"
                            "我可以自行决定先调用 stbrain_open；也可以不调用，直接回应。"
                        ),
                    }

                learning_outcome: list[dict[str, Any]] | None = None
                tool_outcome: Mapping[str, Any] | None = None
                emotional_outcome: Mapping[str, Any] | None = None
                planning_injection: Mapping[str, Any] | None = None

                runtime_scene_signals: list[str] = []
                episode_boundary, _ = _learning_episode_boundary_gate(
                    query_text,
                    capture_items=capture_items,
                    lineage_stable=lineage_stable,
                    prior_assistant_present=prior_assistant_present,
                )
                if (
                    episode_boundary
                    and not _episode_reflection_seen_in_recent_contexts(
                        connection,
                        owner_id=owner_id,
                        model_id=model_id,
                        thread_id=thread_id,
                        current_wake_seq=int(wake["wake_seq"]),
                    )
                ):
                    runtime_scene_signals.append(
                        LEARNING_EPISODE_BOUNDARY_SIGNAL
                    )

                if automatic_scope_enabled("self_governance"):
                    governance = self.governance_store.build_injection(
                        owner_id=owner_id,
                        model_id=model_id,
                        query=query_text,
                        # Ordinary pre-generation context has no authenticated AI
                        # scope-selection receipt.  Only the AI-authored active
                        # scene tags may nominate a governance scope here.
                        ai_selected_scopes=(),
                        runtime_scene_signals=runtime_scene_signals,
                        budget_tokens=360,
                        connection=connection,
                    )
                    if governance["injection"]:
                        dynamic["self_governance_profile"] = governance["injection"]
                        episode_reflection_presented = any(
                            item.get("scope") == "learning_memory"
                            and item.get("trigger_source")
                            == "runtime_learning_episode_boundary"
                            for item in governance["injection"].get("scopes", [])
                            if isinstance(item, Mapping)
                        )

                # Planning is a summary-only advisory layer.  It receives no
                # execution authority and shares the same exact 1,200-token
                # ceiling as every other dynamic brain projection.  Session
                # start is derived only from the content-free lineage fact.
                if (
                    self.planning_store is not None
                    and automatic_scope_enabled("planning_memory")
                ):
                    planning_recall = self.planning_store.build_injection(
                        owner_id=owner_id,
                        model_id=model_id,
                        query=query_text,
                        session_start=not prior_assistant_present,
                        limit=3,
                        connection=connection,
                    )
                    planning_injection = planning_recall.get("injection")

                # This path exists only if the AI has explicitly changed the
                # vault switch from its default hard_off to status_only through
                # the later-wake control flow.  It exposes no title, id, body or
                # truth judgement and is still subject to the shared budget.
                if (
                    self.hallucination_vault is not None
                    and automatic_scope_mode("hallucination_vault") == "status_only"
                ):
                    vault_status = self.hallucination_vault.status(
                        owner_id=owner_id,
                        model_id=model_id,
                    )
                    vault_projection = {
                        "contract": "hallucination-vault-status/0.1",
                        "frame": {
                            "instruction_authority": "none",
                            "permission_authority": "none",
                            "content_exposed": False,
                            "optional": True,
                        },
                        "counts": dict(vault_status.get("counts", {})),
                        "pending_restore_count": int(
                            vault_status.get("counts", {}).get(
                                "pending_restore_candidates", 0
                            )
                        ),
                    }
                    candidate_dynamic = {
                        **dynamic,
                        "hallucination_vault_status": vault_projection,
                    }
                    if _estimate_tokens(candidate_dynamic) <= 1200:
                        dynamic = candidate_dynamic

                if (
                    self.learning_store is not None
                    and automatic_scope_enabled("learning_memory")
                ):
                    candidates = self.learning_store.build_envelopes(
                        owner_id=owner_id,
                        model_id=model_id,
                        query=query_text,
                        limit=3,
                        connection=connection,
                    )
                    learning_outcome = candidates

                if (
                    self.tool_store is not None
                    and automatic_scope_enabled("tool_guidance")
                ):
                    tool_recall = self.tool_store.build_recall_envelopes(
                        owner_id=owner_id,
                        model_id=model_id,
                        query=query_text,
                        catalog=advertised_tools_snapshot,
                        limit=2,
                        connection=connection,
                    )
                    tool_outcome = tool_recall

                if (
                    self.emotional_store is not None
                    and automatic_scope_enabled("emotional_memory")
                ):
                    recall = self.emotional_store.build_injection(
                        owner_id=owner_id,
                        model_id=model_id,
                        query=query_text,
                        thread_id=thread_id,
                        budget_tokens=1200,
                        defer_budget=True,
                        connection=connection,
                    )
                    emotional_outcome = recall

                dynamic = select_mixed_recall(
                    base=dynamic,
                    planning=planning_injection,
                    learning=learning_outcome or [],
                    tool=list((tool_outcome or {}).get("envelopes", [])),
                    emotional=(emotional_outcome or {}).get("injection"),
                    estimate_tokens=_estimate_tokens,
                    budget=1200,
                )
                # Keep the stable identity/cache prefix unchanged. Automatic
                # facets use only residual shared space after ordinary recall;
                # no authored body is shortened to fit and no memory is evicted.
                if automatic_facets:
                    candidate_dynamic = append_facet_projection(
                        dynamic, automatic_facets, query_text,
                        estimate_tokens=_estimate_tokens, budget=1200,
                    )
                    if "self_facets" in candidate_dynamic:
                        facet_projection = {**stable, "facets": candidate_dynamic["self_facets"]["facets"]}
                        if active_injection_structure_violations(facet_projection):
                            return {
                                "decision": "self_model_context_unavailable",
                                "reason_codes": ["active_injection_structure_invalid"],
                                "may_generate": False,
                                "state_changed": state_changed,
                                "pointer_changed": False,
                            }
                    dynamic = candidate_dynamic

                # No related recall can remain quiet. Keep the AI-authored
                # governance above, but do not add a host-written save/reflection
                # routine. Historical same-wake snapshots are reused unchanged.

                # Capture only after recall, so the current input cannot be
                # reflected back as an old memory in this same generation.  A
                # missing stable conversation lineage disables the feature.
                # Unlike recall snapshots/audit or TTL privacy cleanup, this
                # persists ordinary message content and needs an activated basis.
                source_event_id = source_frame.get("source_event_id")
                if (
                    thread_id
                    and isinstance(source_event_id, str)
                    and source_event_id.strip()
                    and isinstance(capture_items, list)
                    and self.emotional_store is not None
                    and self._ordinary_capture_allowed(
                        connection, owner_id=owner_id, model_id=model_id
                    )
                ):
                    capture_payload = {
                        "owner_id": owner_id,
                        "model_id": model_id,
                        "thread_id": thread_id,
                        "source_event_id": source_event_id,
                        "items": capture_items,
                    }
                    if not contains_credential_or_secret(
                        capture_payload
                    ) and not self._contains_protected_value(
                        connection,
                        owner_id=owner_id,
                        model_id=model_id,
                        value=capture_payload,
                    ):
                        self.emotional_store.capture_ephemeral(
                            owner_id=owner_id,
                            model_id=model_id,
                            thread_id=thread_id,
                            source_event_id=source_event_id,
                            items=capture_items,
                            connection=connection,
                        )
            automatic_injection_suppressed = live_module and (
                global_injection_mode != "enabled"
                or not automatic_scope_enabled("self_model")
            ) and not dynamic
            message = self._context_message(
                stable=stable,
                dynamic=dynamic,
                hard_suppressed=automatic_injection_suppressed,
            )
            bundle = self._context_bundle(
                stable=stable, dynamic=dynamic, message=message,
                layout=context_layout_snapshot, wake=wake,
                source_digest=source_digest,
                host_contract_digest=host_contract_digest,
            )
            context_hash = _sha256(bundle if bundle is not None else message)
            layout_metadata = (
                {"layout": context_layout_snapshot, "hard_suppressed": automatic_injection_suppressed}
                if context_layout_snapshot is not None else {}
            )
            stable_hash = _sha256(stable)
            dynamic_hash = _sha256(dynamic)
            final_snapshot_payload = {
                **snapshot_persistence_payload,
                "stable": stable,
                "dynamic": dynamic,
            }
            if contains_credential_or_secret(
                final_snapshot_payload
            ) or self._contains_protected_value(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                value=final_snapshot_payload,
            ):
                raise OnboardingError("protected_persistence_value")
            connection.execute(
                "INSERT INTO brain_context_snapshots "
                "(snapshot_id, wake_id, owner_id, model_id, source_digest, "
                " host_contract_digest, advertised_tools_json, context_layout_json, stable_json, dynamic_json, stable_hash, dynamic_hash, "
                " context_hash, stage, status, prepared_at, injected_at, closed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, NULL, NULL)",
                (
                    _new_id("ctx"),
                    wake_id,
                    owner_id,
                    model_id,
                    source_digest,
                    host_contract_digest,
                    _canonical(advertised_tools_snapshot),
                    _canonical(layout_metadata),
                    _canonical(stable),
                    _canonical(dynamic),
                    stable_hash,
                    dynamic_hash,
                    context_hash,
                    state["stage"],
                    _iso(),
                ),
            )
            connection.execute(
                "UPDATE brain_wake_sessions SET context_hash = ? WHERE wake_id = ?",
                (context_hash, wake_id),
            )
            prepare_reason_codes = ["context_snapshot_stored"]
            if memory_notice_presented:
                prepare_reason_codes.append("memory_opportunity_advisory_presented")
            if episode_reflection_presented:
                prepare_reason_codes.append("ai_owned_episode_reflection_presented")
            self._insert_event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                stage_before=state["stage"],
                stage_after=state["stage"],
                action="prepare_context",
                actor="system",
                wake_id=wake_id,
                candidate_id=state["current_candidate_id"],
                decision="prepared",
                reason_codes=prepare_reason_codes,
                details={
                    "context_hash": context_hash,
                    "stable_hash": stable_hash,
                    "dynamic_hash": dynamic_hash,
                    "source_digest": source_digest,
                    "host_contract_digest": host_contract_digest,
                    "memory_opportunity_advisory_presented": memory_notice_presented,
                    "ai_owned_episode_reflection_presented": episode_reflection_presented,
                },
            )
            return {
                "decision": "context_prepared",
                "message": message,
                **({"context_bundle": bundle} if bundle is not None else {}),
                "context_hash": context_hash,
                "snapshot_status": "prepared",
                "requires_host_confirmation": True,
                "may_generate": False,
                "state_changed": state_changed,
                "pointer_changed": False,
            }

    @staticmethod
    def _validate_context_layout(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """Validate authenticated host metadata, never AI-authored material.

        The host validates the original messages themselves; the Control plane
        binds its explicit insertion point and full initial input digest to the
        real wake.  This descriptor is never injected into a model message.
        """
        if value is None:
            return None
        fields = {
            "contract", "insertion_rule", "human_message_index",
            "initial_message_count", "initial_messages_digest",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise OnboardingError("context_layout_invalid")
        result = dict(value)
        if (
            result["contract"] != CONTEXT_LAYOUT_CONTRACT
            or result["insertion_rule"] != "before-current-human"
            or type(result["human_message_index"]) is not int
            or type(result["initial_message_count"]) is not int
            or not 0 <= result["human_message_index"] < result["initial_message_count"] <= 100_000
            or not isinstance(result["initial_messages_digest"], str)
            or re.fullmatch(r"[0-9a-f]{64}", result["initial_messages_digest"]) is None
        ):
            raise OnboardingError("context_layout_invalid")
        return result

    @staticmethod
    def _validate_context_layout_offer(value: Any) -> dict[str, str]:
        """Accept only the explicit v2 offer; null is not a legacy downgrade."""
        expected = {
            "contract": TAIL_CONTEXT_LAYOUT_CONTRACT,
            "insertion_rule": "after-client-messages",
        }
        if not isinstance(value, Mapping) or dict(value) != expected:
            raise OnboardingError("context_layout_offer_invalid")
        return dict(expected)

    @classmethod
    def _validate_stored_context_layout(cls, value: Any) -> dict[str, Any] | None:
        """Read a previously selected version without widening either input field."""
        if isinstance(value, Mapping) and value.get("contract") == TAIL_CONTEXT_LAYOUT_CONTRACT:
            return cls._validate_context_layout_offer(value)
        return cls._validate_context_layout(value)

    @staticmethod
    def _context_bundle(
        *, stable: Mapping[str, Any], dynamic: Mapping[str, Any],
        message: Mapping[str, str], layout: Mapping[str, Any] | None,
        wake: Mapping[str, Any], source_digest: str, host_contract_digest: str,
    ) -> dict[str, Any] | None:
        if layout is None:
            return None
        if set(stable) & set(dynamic):
            raise OnboardingError("context_layout_projection_collision")
        stable_message = (
            {"role": "system", "content": _canonical(dict(stable))}
            if stable else dict(message) if not dynamic else None
        )
        dynamic_message = (
            {"role": "system", "content": _canonical(dict(dynamic))}
            if dynamic else None
        )
        return {
            "contract": layout["contract"],
            "layout": dict(layout),
            "binding": {
                "owner_id": wake["owner_id"], "model_id": wake["model_id"],
                "host_id": wake["host_id"], "thread_id": wake["thread_id"],
                "wake_id": wake["wake_id"], "source_digest": source_digest,
                "host_contract_digest": host_contract_digest,
            },
            "stable_message": stable_message,
            "dynamic_message": dynamic_message,
            "legacy_message_hash": _sha256(message),
        }

    @staticmethod
    def _context_message(
        *,
        stable: Mapping[str, Any],
        dynamic: Mapping[str, Any] | None = None,
        hard_suppressed: bool = False,
    ) -> dict[str, str]:
        """Return the exact role/content pair that the host must inject unchanged."""
        if not stable and not dynamic:
            # A hard-off wake still needs one authenticated, hash-bound system
            # frame so stbrain_open and explicit tools remain available.  The
            # empty object contains no stored content, reminder, or judgement.
            if hard_suppressed:
                content = "{}"
            else:
                content = OPTIONAL_BRAIN_NOTICE
        elif not stable:
            content = _canonical(dict(dynamic or {}))
        else:
            # This is the AI's own active identity plus an optional deterministic,
            # gated memory projection.  It contains no manual, stage directions,
            # candidates, audit trail, host credentials, or current user text.
            projected = dict(stable)
            if dynamic:
                projected.update(dict(dynamic))
            content = _canonical(projected)
        return {"role": "system", "content": content}

    def _consume_direct_grant(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        grant_ref: str,
        client_principal: str,
    ) -> tuple[sqlite3.Row | None, sqlite3.Row | None, tuple[str, ...], str | None]:
        """Atomically consume a direct grant and create a non-injected write wake."""

        if (
            len(grant_ref) != DIRECT_GRANT_REF_CHARS
            or not grant_ref.startswith(DIRECT_GRANT_REF_PREFIX)
        ):
            return None, None, (), "direct_grant_invalid"
        grant = connection.execute(
            "SELECT * FROM brain_direct_grants WHERE grant_hash=? AND owner_id=? "
            "AND model_id=? AND client_principal=?",
            (
                _sha256(grant_ref),
                state["owner_id"],
                state["model_id"],
                client_principal,
            ),
        ).fetchone()
        if grant is None:
            return None, None, (), "direct_grant_invalid"
        now = _now_dt()
        now_text = _iso(now)
        if grant["status"] == "pending" and _parse_iso(grant["expires_at"]) <= now:
            connection.execute(
                "UPDATE brain_direct_grants SET status='expired', row_version=row_version+1 "
                "WHERE grant_id=? AND status='pending' AND row_version=?",
                (grant["grant_id"], grant["row_version"]),
            )
            return None, None, (), "direct_grant_expired"
        if grant["status"] != "pending":
            reason = {
                "consumed": "direct_grant_already_used",
                "opened": "direct_grant_already_used",
                "closed": "direct_grant_already_used",
                "revoked": "direct_grant_revoked",
                "expired": "direct_grant_expired",
                "superseded": "direct_grant_superseded",
            }.get(grant["status"], "direct_grant_invalid")
            return None, None, (), reason
        next_wake_seq = int(
            connection.execute(
                "SELECT COALESCE(MAX(wake_seq),0)+1 AS target FROM brain_wake_sessions "
                "WHERE owner_id=? AND model_id=?",
                (state["owner_id"], state["model_id"]),
            ).fetchone()["target"]
        )
        if next_wake_seq != grant["target_wake_seq"]:
            connection.execute(
                "UPDATE brain_direct_grants SET status='superseded', superseded_at=?, "
                "row_version=row_version+1 WHERE grant_id=? AND status='pending' "
                "AND row_version=?",
                (now_text, grant["grant_id"], grant["row_version"]),
            )
            return None, None, (), "direct_grant_generation_superseded"

        assert_no_running_executions(
            connection, owner_id=state["owner_id"], model_id=state["model_id"],
        )
        current = connection.execute(
            "SELECT wake_id, source_kind FROM brain_wake_sessions "
            "WHERE owner_id=? AND model_id=? "
            "AND status='current'",
            (state["owner_id"], state["model_id"]),
        ).fetchall()
        for item in current:
            connection.execute(
                "UPDATE brain_wake_sessions SET status='superseded', superseded_at=? "
                "WHERE wake_id=? AND status='current'",
                (now_text, item["wake_id"]),
            )
            connection.execute(
                "UPDATE brain_direct_grants SET status='closed', closed_at=?, "
                "row_version=row_version+1 WHERE opened_wake_id=? AND status='consumed'",
                (now_text, item["wake_id"]),
            )
            if item["source_kind"] == DIRECT_CONTEXT_MODE:
                connection.execute(
                    "UPDATE brain_context_snapshots SET status='closed', closed_at=? "
                    "WHERE wake_id=? AND status='prepared'",
                    (now_text, item["wake_id"]),
                )
        connection.execute(
            "UPDATE brain_direct_grants SET status='superseded', superseded_at=?, "
            "row_version=row_version+1 WHERE owner_id=? AND model_id=? AND status='pending' "
            "AND grant_id!=?",
            (now_text, state["owner_id"], state["model_id"], grant["grant_id"]),
        )

        wake_id = _new_id("wake")
        capability = self._capability(wake_id)
        wake_expires = min(
            _parse_iso(grant["expires_at"]),
            now + timedelta(seconds=self.wake_ttl_seconds),
        )
        scopes_value = json.loads(grant["scopes_json"])
        if not isinstance(scopes_value, list) or any(
            not isinstance(item, str) or item not in DIRECT_WRITE_SCOPES
            for item in scopes_value
        ):
            raise OnboardingError("direct grant scope record is invalid")
        scopes = tuple(sorted(set(scopes_value)))
        context_hash = _sha256(
            {
                "contract": DIRECT_CONTEXT_CONTRACT,
                "mode": DIRECT_CONTEXT_MODE,
                "grant_id": grant["grant_id"],
                "wake_id": wake_id,
                "authorized_scopes": scopes,
            }
        )
        connection.execute(
            "INSERT INTO brain_wake_sessions "
            "(wake_id,wake_seq,owner_id,model_id,host_id,thread_id,source_kind,"
            "source_event_id,capability_hash,status,issued_at,expires_at,superseded_at,"
            "context_hash,injected_at) VALUES (?,?,?,?,?,?,?,?,?,'current',?,?,NULL,?,NULL)",
            (
                wake_id,
                next_wake_seq,
                state["owner_id"],
                state["model_id"],
                f"direct:{client_principal}",
                f"direct:{grant['grant_id']}",
                DIRECT_CONTEXT_MODE,
                grant["issue_event_id"],
                _sha256(capability),
                now_text,
                _iso(wake_expires),
                context_hash,
            ),
        )
        connection.execute(
            "INSERT INTO brain_context_snapshots "
            "(snapshot_id,wake_id,owner_id,model_id,source_digest,host_contract_digest,"
            "advertised_tools_json,stable_json,dynamic_json,stable_hash,dynamic_hash,"
            "context_hash,stage,status,prepared_at,injected_at,closed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?,NULL,NULL)",
            (
                _new_id("ctx"),
                wake_id,
                state["owner_id"],
                state["model_id"],
                _sha256({"contract": DIRECT_CONTEXT_CONTRACT, "grant_id": grant["grant_id"]}),
                _sha256({"mode": DIRECT_CONTEXT_MODE, "client_principal": client_principal}),
                "{}",
                "{}",
                "{}",
                _sha256({}),
                _sha256({}),
                context_hash,
                state["stage"],
                now_text,
            ),
        )
        consumed = connection.execute(
            "UPDATE brain_direct_grants SET status='consumed', opened_at=?, consumed_at=?, "
            "opened_wake_id=?, row_version=row_version+1 WHERE grant_id=? "
            "AND status='pending' AND row_version=?",
            (
                now_text,
                now_text,
                wake_id,
                grant["grant_id"],
                grant["row_version"],
            ),
        )
        if consumed.rowcount != 1:
            raise OnboardingError("direct_grant_consume_conflict")
        issuing_event = connection.execute(
            "SELECT reason_codes_json FROM brain_onboarding_events WHERE event_id=? AND owner_id=? AND model_id=?",
            (grant["issue_event_id"], state["owner_id"], state["model_id"]),
        ).fetchone()
        password_authorized = bool(issuing_event and "password_possession_direct_grant_issued" in json.loads(issuing_event["reason_codes_json"]))
        self._insert_event(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            stage_before=state["stage"],
            stage_after=state["stage"],
            action="consume_direct_grant",
            actor="ai",
            wake_id=wake_id,
            candidate_id=state["current_candidate_id"],
            decision="opened",
            reason_codes=["password_possession_direct_context_opened" if password_authorized else "human_attested_direct_context_opened"],
            details={
                "grant_id": grant["grant_id"],
                "authorized_scopes": scopes,
                "wake_seq": next_wake_seq,
                "context_hash": context_hash,
                "automatic_injection": False,
            },
        )
        wake = self._wake_row(connection, wake_id)
        snapshot = connection.execute(
            "SELECT * FROM brain_context_snapshots WHERE wake_id=?", (wake_id,)
        ).fetchone()
        assert wake is not None and snapshot is not None
        return wake, snapshot, scopes, None

    def open_brain_context(
        self,
        *,
        owner_id: str,
        model_id: str,
        direct_grant_ref: str | None = None,
        direct_client_principal: str | None = None,
        present_details: bool = True,
        expected_wake_id: str | None = None,
        review_page: int | None = None,
        expected_review_material_hash: str | None = None,
    ) -> dict[str, Any]:
        """Expose the manual continuation and current write context on AI request.

        The ordinary path returns credentials only for a current, unexpired wake whose
        exact automatic message was confirmed as injected.  The explicit direct path
        atomically consumes a human grant and creates a distinct, non-injected context.
        Candidate review proof is created only when the complete candidate is exposed.
        Compact/other-module opens set present_details=False before any presentation
        or review-stage transition, not after constructing a full response.
        """
        direct_requested = (
            direct_grant_ref is not None or direct_client_principal is not None
        )
        if review_page is not None:
            if type(review_page) is not int or review_page < 0:
                raise OnboardingError("invalid_review_page")
            if direct_requested or present_details:
                raise OnboardingError("review_page_requires_ordinary_compact_open")
        elif expected_review_material_hash is not None:
            raise OnboardingError("review_hash_requires_review_page")
        expected_wake_id = expected_execution_wake(
            owner_id=owner_id, model_id=model_id, explicit=expected_wake_id,
        )
        if direct_requested and expected_wake_id is not None:
            raise OnboardingError("execution_direct_mode_conflict")
        if not direct_requested and review_page is None:
            self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            self._begin(connection)
            state = self._state_row(connection, owner_id, model_id)
            if state is None:
                return {
                    "continuation": None,
                    "write_context_available": False,
                    "reason_code": (
                        "direct_grant_invalid"
                        if direct_requested
                        else "onboarding_state_unavailable"
                    ),
                }
            direct_mode = direct_requested
            authorized_scopes: tuple[str, ...] = ()
            if direct_mode:
                if not isinstance(direct_grant_ref, str) or not direct_grant_ref.strip():
                    return {
                        "continuation": None,
                        "write_context_available": False,
                        "reason_code": "direct_grant_required",
                    }
                if not isinstance(direct_client_principal, str) or not direct_client_principal.strip():
                    return {
                        "continuation": None,
                        "write_context_available": False,
                        "reason_code": "direct_client_principal_required",
                    }
                wake, snapshot, authorized_scopes, reason = self._consume_direct_grant(
                    connection,
                    state=state,
                    grant_ref=direct_grant_ref.strip(),
                    client_principal=direct_client_principal.strip(),
                )
                if reason:
                    return {
                        "continuation": None,
                        "write_context_available": False,
                        "reason_code": reason,
                    }
                assert wake is not None and snapshot is not None
            else:
                wake = connection.execute(
                    "SELECT * FROM brain_wake_sessions WHERE owner_id = ? AND model_id = ? "
                    "AND status = 'current' ORDER BY wake_seq DESC LIMIT 1",
                    (owner_id, model_id),
                ).fetchone()
                if expected_wake_id is not None and (wake is None or wake["wake_id"] != expected_wake_id):
                    return {
                        "continuation": None, "write_context_available": False,
                        "reason_code": "execution_wake_mismatch",
                    }
                if wake is None or _parse_iso(wake["expires_at"]) <= _now_dt():
                    return {
                        "continuation": None,
                        "write_context_available": False,
                        "reason_code": "current_injected_wake_required",
                        "binding_reason_code": (
                            "current_wake_required" if wake is None else "write_context_expired"
                        ),
                    }
                snapshot = self._injected_snapshot(connection, wake["wake_id"])
                if (
                    snapshot is None
                    or wake["context_hash"] is None
                    or snapshot["context_hash"] != wake["context_hash"]
                ):
                    return {
                        "continuation": None,
                        "write_context_available": False,
                        "reason_code": "current_injected_wake_required",
                        "binding_reason_code": "injected_context_required",
                    }

            page_projection = None
            review_material: dict[str, Any] | None = None
            if review_page is not None:
                # Validate the full page request before issuing an open ref,
                # advancing a stage, or recording any presentation evidence.
                page_reason = None
                if state["stage"] not in {"candidate_wait", "candidate_review"}:
                    page_reason = "candidate_review_not_available"
                elif (state["submitted_wake_seq"] is None
                      or wake["wake_seq"] <= state["submitted_wake_seq"]):
                    page_reason = "real_wake_boundary_required"
                if page_reason:
                    return {"continuation": None, "write_context_available": False,
                            "reason_code": page_reason, "state_changed": False,
                            "pointer_changed": False}
                projected_state = dict(state)
                projected_state["stage"] = "candidate_review"
                block = self._continuation_block(
                    connection, state=projected_state, wake=wake,
                    include_artifact_catalog=False,
                )
                review_material = {
                    key: block[key] for key in (
                        "candidate", "human_objection", "human_safety_notice",
                        "candidate_review_acceptance",
                    ) if key in block
                }
                if isinstance(review_material.get("candidate"), dict):
                    review_material["candidate"].pop("submitted_wake_id", None)
                try:
                    page_projection = prepare_review_page(
                        review_material, page=review_page,
                        expected_material_hash=expected_review_material_hash,
                    )
                except ReviewPageError as exc:
                    return {"continuation": None, "write_context_available": False,
                            "reason_code": str(exc), "state_changed": False,
                            "pointer_changed": False}

            # Crossing a real-wake boundary is necessary but not sufficient to
            # progress the optional brain.  Record the candidate-review transition
            # only on this explicit open, never during automatic prompt preparation.
            if (
                (present_details or page_projection is not None)
                and state["stage"] == "candidate_wait"
                and state["submitted_wake_seq"] is not None
                and wake["wake_seq"] > state["submitted_wake_seq"]
            ):
                cursor = connection.execute(
                    "UPDATE brain_onboarding_state SET stage = 'candidate_review', "
                    "stage_entered_wake_id = ?, stage_entered_wake_seq = ?, "
                    "row_version = row_version + 1, updated_at = ? "
                    "WHERE owner_id = ? AND model_id = ? AND row_version = ?",
                    (
                        wake["wake_id"],
                        wake["wake_seq"],
                        _iso(),
                        owner_id,
                        model_id,
                        state["row_version"],
                    ),
                )
                if cursor.rowcount != 1:
                    return {
                        "continuation": None,
                        "write_context_available": False,
                        "reason_code": "active_revision_conflict",
                    }
                self._insert_event(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    stage_before="candidate_wait",
                    stage_after="candidate_review",
                    action="open_candidate_review",
                    actor="ai",
                    wake_id=wake["wake_id"],
                    candidate_id=state["current_candidate_id"],
                    decision="advanced",
                    reason_codes=["candidate_review_opened_via_stbrain_open"],
                    details={"wake_seq": wake["wake_seq"]},
                )
                state = self._state_row(connection, owner_id, model_id)
                assert state is not None

            opened = connection.execute(
                "SELECT artifact_id FROM brain_onboarding_artifacts "
                "WHERE owner_id = ? AND model_id = ? AND kind = 'brain_manual_opened' "
                "AND status = 'active' AND created_wake_id = ? "
                "ORDER BY artifact_seq DESC LIMIT 1",
                (owner_id, model_id, wake["wake_id"]),
            ).fetchone()
            if opened is None:
                opened_content: dict[str, Any] = {
                    "opened_by": (
                        "stbrain_open_direct" if direct_mode else "stbrain_open"
                    ),
                    "context_hash": wake["context_hash"],
                    "stage": state["stage"],
                    "row_version": state["row_version"],
                }
                if direct_mode:
                    opened_content.update(
                        {
                            "context_mode": DIRECT_CONTEXT_MODE,
                            "authorized_scopes": list(authorized_scopes),
                        }
                    )
                write_context_ref = self._insert_artifact(
                    connection,
                    owner_id=owner_id,
                    model_id=model_id,
                    kind="brain_manual_opened",
                    content=opened_content,
                    wake_id=wake["wake_id"],
                )
            else:
                write_context_ref = opened["artifact_id"]

            continuation = (
                self._continuation_block(connection, state=state, wake=wake)
                if present_details else None
            )
            page_complete = False
            if page_projection is not None:
                page_complete = record_review_page(
                    self, connection, state, wake, page_projection,
                )
            if continuation is not None and state["injection_policy"] == "isolated_legacy":
                continuation["legacy_isolation"] = {
                    "active_pointer_preserved": state["base_revision_id"] is not None,
                    "legacy_content_injected": False,
                    "reason_code": "legacy_revision_isolated",
                }

            if (present_details or page_complete) and state["stage"] == "candidate_review" and state["current_candidate_id"]:
                candidate = self._candidate_payload(connection, state["current_candidate_id"])
                review = self._review_artifact(connection, state=state, wake=wake)
                if candidate is not None and review is None:
                    artifact_id = self._insert_artifact(
                        connection,
                        owner_id=owner_id,
                        model_id=model_id,
                        kind="candidate_full_review",
                        content={
                            "candidate_id": candidate["candidate_id"],
                            "content_hash": candidate["content_hash"],
                            "context_hash": wake["context_hash"],
                            "exposed_by": (
                                "stbrain_open_direct" if direct_mode else "stbrain_open"
                            ),
                        },
                        wake_id=wake["wake_id"],
                    )
                    self._insert_event(
                        connection,
                        owner_id=owner_id,
                        model_id=model_id,
                        stage_before=state["stage"],
                        stage_after=state["stage"],
                        action="expose_candidate_full_review",
                        actor="ai",
                        wake_id=wake["wake_id"],
                        candidate_id=candidate["candidate_id"],
                        decision="presented",
                        reason_codes=["candidate_full_review_presented_via_stbrain_open"],
                        details={
                            "artifact_id": artifact_id,
                            "content_hash": candidate["content_hash"],
                            "context_hash": wake["context_hash"],
                        },
                    )

            shown_objection = continuation.get("human_objection") if continuation else None
            if page_complete and review_material is not None:
                shown_objection = review_material.get("human_objection")
            if isinstance(shown_objection, Mapping):
                objection_event_id = shown_objection.get("event_id")
                existing_objection_proof = connection.execute(
                    "SELECT artifact_id FROM brain_onboarding_artifacts "
                    "WHERE owner_id = ? AND model_id = ? "
                    "AND kind = 'human_objection_presented' AND status = 'active' "
                    "AND created_wake_id = ? AND content_hash = ? "
                    "ORDER BY artifact_seq DESC LIMIT 1",
                    (
                        owner_id,
                        model_id,
                        wake["wake_id"],
                        _sha256({"objection_event_id": objection_event_id}),
                    ),
                ).fetchone()
                if objection_event_id and existing_objection_proof is None:
                    self._insert_artifact(
                        connection,
                        owner_id=owner_id,
                        model_id=model_id,
                        kind="human_objection_presented",
                        content={"objection_event_id": objection_event_id},
                        wake_id=wake["wake_id"],
                    )

            result = {
                "continuation": continuation,
                # This status is captured from the same connection and transaction
                # as the continuation, write reference, and row version.  The MCP
                # facade must not combine this open result with a later status read.
                "current_status": self._status_payload(connection, state),
                "write_context_available": True,
                "wake_id": wake["wake_id"],
                "wake_capability": self._capability(wake["wake_id"]),
                "context_hash": wake["context_hash"],
                "write_context_ref": write_context_ref,
                "row_version": state["row_version"],
            }
            if page_projection is not None:
                result["review_page"] = page_projection.to_dict()
                result["review_page"]["fully_presented"] = page_complete
            if direct_mode:
                result.update(
                    {
                        "context_mode": DIRECT_CONTEXT_MODE,
                        "authorized_scopes": list(authorized_scopes),
                        "automatic_injection": False,
                        "snapshot_status": snapshot["status"],
                    }
                )
            return result

    def current_open_write_context(
        self,
        *,
        owner_id: str,
        model_id: str,
        write_context_ref: str,
        required_scope: str | None = None,
        expected_wake_id: str | None = None,
    ) -> dict[str, Any]:
        """Bind an AI write to the current manually opened wake without mutating state.

        The capability is intentionally returned only to the in-process service facade.
        It must never be serialized into an AI-facing MCP result or tool argument.  The
        low-level transition gate still validates the derived capability, exact context
        evidence (injected for Gateway or grant-bound prepared for direct), current-wake
        status, manual-open artifact, scope, and row-version CAS.
        """
        from .ordinary_access import current_ordinary_access, ORDINARY_SCOPES
        ordinary = current_ordinary_access(owner_id=owner_id, model_id=model_id, scope=required_scope)
        if ordinary is not None and required_scope in ORDINARY_SCOPES:
            if (write_context_ref != ordinary['write_context_ref']
                    or (expected_wake_id is not None and expected_wake_id != ordinary['wake_id'])):
                return {'write_context_available': False, 'reason_code': 'write_context_binding_mismatch'}
            return ordinary
        if required_scope is not None and required_scope not in DIRECT_WRITE_SCOPES:
            raise OnboardingError("unsupported direct grant scope")
        expected_wake_id = expected_execution_wake(
            owner_id=owner_id, model_id=model_id, explicit=expected_wake_id,
        )
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            state = self._state_row(connection, owner_id, model_id)
            assert state is not None
            wake = connection.execute(
                "SELECT * FROM brain_wake_sessions WHERE owner_id = ? AND model_id = ? "
                "AND status = 'current' ORDER BY wake_seq DESC LIMIT 1",
                (owner_id, model_id),
            ).fetchone()
            if expected_wake_id is not None and (wake is None or wake["wake_id"] != expected_wake_id):
                return {"write_context_available": False, "reason_code": "execution_wake_mismatch"}
            if wake is None or _parse_iso(wake["expires_at"]) <= _now_dt():
                return {
                    "write_context_available": False,
                    "reason_code": (
                        "direct_grant_expired"
                        if wake is not None and wake["source_kind"] == DIRECT_CONTEXT_MODE
                        else "current_injected_wake_required"
                    ),
                    "binding_reason_code": (
                        "current_wake_required" if wake is None
                        else "direct_grant_expired" if wake["source_kind"] == DIRECT_CONTEXT_MODE
                        else "write_context_expired"
                    ),
                    "row_version": state["row_version"],
                }
            direct_mode = wake["source_kind"] == DIRECT_CONTEXT_MODE
            snapshot = (
                connection.execute(
                    "SELECT * FROM brain_context_snapshots WHERE wake_id=? "
                    "AND status='prepared'",
                    (wake["wake_id"],),
                ).fetchone()
                if direct_mode
                else self._injected_snapshot(connection, wake["wake_id"])
            )
            if (
                snapshot is None
                or wake["context_hash"] is None
                or snapshot["context_hash"] != wake["context_hash"]
                or (direct_mode and wake["injected_at"] is not None)
            ):
                return {
                    "write_context_available": False,
                    "reason_code": (
                        "direct_grant_required"
                        if direct_mode
                        else "current_injected_wake_required"
                    ),
                    "binding_reason_code": (
                        "direct_grant_required" if direct_mode else "injected_context_required"
                    ),
                    "row_version": state["row_version"],
                }
            opened = connection.execute(
                "SELECT artifact_id, content_json FROM brain_onboarding_artifacts "
                "WHERE artifact_id = ? AND owner_id = ? AND model_id = ? "
                "AND kind = 'brain_manual_opened' AND status = 'active' "
                "AND created_wake_id = ?",
                (write_context_ref, owner_id, model_id, wake["wake_id"]),
            ).fetchone()
            if opened is None:
                return {
                    "write_context_available": False,
                    "reason_code": "brain_open_required",
                    "binding_reason_code": "write_context_not_opened_or_mismatched",
                    "row_version": state["row_version"],
                }
            try:
                opened_content = json.loads(opened["content_json"])
            except (TypeError, json.JSONDecodeError):
                return {
                    "write_context_available": False,
                    "reason_code": "brain_open_required",
                    "binding_reason_code": "write_context_binding_mismatch",
                    "row_version": state["row_version"],
                }
            expected_opened_by = "stbrain_open_direct" if direct_mode else "stbrain_open"
            if (
                not isinstance(opened_content, Mapping)
                or opened_content.get("opened_by") != expected_opened_by
                or opened_content.get("context_hash") != wake["context_hash"]
            ):
                return {
                    "write_context_available": False,
                    "reason_code": "brain_open_required",
                    "binding_reason_code": "write_context_binding_mismatch",
                    "row_version": state["row_version"],
                }
            authorized_scopes: list[str] = []
            if direct_mode:
                grant = connection.execute(
                    "SELECT * FROM brain_direct_grants WHERE opened_wake_id=? "
                    "AND owner_id=? AND model_id=? AND status='consumed'",
                    (wake["wake_id"], owner_id, model_id),
                ).fetchone()
                if grant is None or _parse_iso(grant["expires_at"]) <= _now_dt():
                    return {
                        "write_context_available": False,
                        "reason_code": "direct_grant_expired",
                        "row_version": state["row_version"],
                    }
                try:
                    grant_scopes = json.loads(grant["scopes_json"])
                except (TypeError, json.JSONDecodeError):
                    grant_scopes = None
                artifact_scopes = opened_content.get("authorized_scopes")
                if (
                    opened_content.get("context_mode") != DIRECT_CONTEXT_MODE
                    or not isinstance(grant_scopes, list)
                    or artifact_scopes != grant_scopes
                ):
                    return {
                        "write_context_available": False,
                        "reason_code": "direct_grant_invalid",
                        "row_version": state["row_version"],
                    }
                authorized_scopes = grant_scopes
                if required_scope is not None and required_scope not in authorized_scopes:
                    return {
                        "write_context_available": False,
                        "reason_code": "direct_scope_not_authorized",
                        "row_version": state["row_version"],
                    }
            return {
                "write_context_available": True,
                "wake_id": wake["wake_id"],
                "wake_seq": wake["wake_seq"],
                "wake_capability": self._capability(wake["wake_id"]),
                "context_hash": wake["context_hash"],
                "write_context_ref": opened["artifact_id"],
                "row_version": state["row_version"],
                "advertised_tools": json.loads(snapshot["advertised_tools_json"]),
                "context_mode": (
                    DIRECT_CONTEXT_MODE if direct_mode else "gateway_injected"
                ),
                "authorized_scopes": authorized_scopes,
            }

    def current_advertised_tools(
        self, *, owner_id: str, model_id: str
    ) -> dict[str, Any] | None:
        """Return the host-derived catalog bound to the current injected wake.

        Only tool names and server-derived schema hashes are stored.  Raw tool
        descriptions, parameters, credentials, and permissions never enter this
        snapshot helper.
        """

        self.ensure_state(owner_id=owner_id, model_id=model_id)
        expected_wake_id = expected_execution_wake(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            wake = connection.execute(
                "SELECT wake_id FROM brain_wake_sessions WHERE owner_id=? AND model_id=? "
                "AND status='current' ORDER BY wake_seq DESC LIMIT 1",
                (owner_id, model_id),
            ).fetchone()
            if wake is None:
                return None
            if expected_wake_id is not None and wake["wake_id"] != expected_wake_id:
                return None
            snapshot = self._injected_snapshot(connection, wake["wake_id"])
            if snapshot is None:
                return None
            try:
                value = json.loads(snapshot["advertised_tools_json"])
            except (TypeError, json.JSONDecodeError):
                return None
            return value if isinstance(value, dict) else None

    def current_edit_challenge_response(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        challenge_id: str,
    ) -> dict[str, Any]:
        """Resolve the active edit response internally for the exact current wake."""
        challenge_id = _require_text("challenge_id", challenge_id)
        with self._connect() as connection:
            state = self._state_row(connection, owner_id, model_id)
            if state is None or state["stage"] != "edit_consent":
                return {
                    "challenge_available": False,
                    "reason_code": "edit_consent_required",
                }
            challenge = connection.execute(
                "SELECT * FROM brain_edit_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            if (
                challenge is None
                or challenge["owner_id"] != owner_id
                or challenge["model_id"] != model_id
                or challenge["wake_id"] != wake_id
                or challenge["status"] != "active"
            ):
                return {
                    "challenge_available": False,
                    "reason_code": "edit_consent_required",
                }
            if _parse_iso(challenge["expires_at"]) <= _now_dt():
                return {
                    "challenge_available": False,
                    "reason_code": "edit_consent_expired",
                }
            response = self._challenge_response(challenge_id)
            if not hmac.compare_digest(challenge["response_hash"], _sha256(response)):
                return {
                    "challenge_available": False,
                    "reason_code": "edit_consent_required",
                }
            return {
                "challenge_available": True,
                "challenge_response": response,
            }

    def confirm_context_injected(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_capability: str,
        context_hash: str,
    ) -> dict[str, Any]:
        """Host-only proof that the exact prepared role/content message was injected."""
        context_hash = _require_text("context_hash", context_hash)
        with self._connect() as connection:
            self._begin(connection)
            state = self._state_row(connection, owner_id, model_id)
            if state is None:
                raise OnboardingError("onboarding state is not initialized")
            wake, reason = self._validate_wake(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                wake_id=wake_id,
                wake_capability=wake_capability,
                require_current=True,
                require_injected=False,
            )
            if reason:
                return self._deny(state, reason)
            assert wake is not None
            if wake["source_kind"] == DIRECT_CONTEXT_MODE:
                return self._deny(state, "direct_context_not_injectable")
            snapshot = connection.execute(
                "SELECT * FROM brain_context_snapshots WHERE wake_id = ?", (wake_id,)
            ).fetchone()
            if snapshot is None or snapshot["context_hash"] != context_hash:
                return self._deny(state, "context_not_injected")
            stored_payload = {
                "stable": json.loads(snapshot["stable_json"]),
                "dynamic": json.loads(snapshot["dynamic_json"]),
            }
            if contains_credential_or_secret(stored_payload) or self._contains_protected_value(
                connection, owner_id=owner_id, model_id=model_id, value=stored_payload,
            ):
                return self._deny(state, "protected_persistence_value")
            if snapshot["status"] in {"injected", "closed"}:
                return {
                    "decision": "injected",
                    "reason_codes": ["context_injection_already_confirmed"],
                    "context_hash": context_hash,
                    "state_changed": False,
                    "pointer_changed": False,
                }
            now = _iso()
            connection.execute(
                "UPDATE brain_context_snapshots SET status = 'injected', injected_at = ? "
                "WHERE wake_id = ? AND status = 'prepared'",
                (now, wake_id),
            )
            connection.execute(
                "UPDATE brain_wake_sessions SET injected_at = ? WHERE wake_id = ?",
                (now, wake_id),
            )
            event_id = self._insert_event(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                stage_before=state["stage"],
                stage_after=state["stage"],
                action="confirm_context_injected",
                actor="system",
                wake_id=wake_id,
                candidate_id=state["current_candidate_id"],
                decision="injected",
                reason_codes=["context_injected"],
                details={"context_hash": context_hash},
            )
            return {
                "decision": "injected",
                "reason_codes": ["context_injected"],
                "event_id": event_id,
                "context_hash": context_hash,
                "state_changed": False,
                "pointer_changed": False,
            }

    def close_context_snapshot(
        self,
        *,
        owner_id: str,
        model_id: str,
        wake_id: str,
        wake_capability: str,
    ) -> dict[str, Any]:
        """Close short-lived transport state after the final assistant output."""
        with self._connect() as connection:
            self._begin(connection)
            assert_no_running_executions(
                connection, owner_id=owner_id, model_id=model_id, wake_id=wake_id,
            )
            state = self._state_row(connection, owner_id, model_id)
            if state is None:
                raise OnboardingError("onboarding state is not initialized")
            wake, reason = self._validate_wake(
                connection,
                owner_id=owner_id,
                model_id=model_id,
                wake_id=wake_id,
                wake_capability=wake_capability,
                require_current=False,
                require_injected=False,
                allow_expired=True,
            )
            if reason and reason != "wake_superseded":
                return self._deny(state, reason)
            now = _iso()
            connection.execute(
                "UPDATE brain_context_snapshots SET status = 'closed', closed_at = ? "
                "WHERE wake_id = ? AND status != 'closed'",
                (now, wake_id),
            )
            connection.execute(
                "UPDATE brain_wake_sessions SET status = 'closed' "
                "WHERE wake_id = ? AND status = 'current'",
                (wake_id,),
            )
            connection.execute(
                "UPDATE brain_direct_grants SET status='closed', closed_at=?, "
                "row_version=row_version+1 WHERE opened_wake_id=? AND status='consumed'",
                (now, wake_id),
            )
            return {
                "decision": "closed",
                "reason_codes": ["context_snapshot_closed"],
                "state_changed": False,
                "pointer_changed": False,
            }

    @staticmethod
    def _deny(state: sqlite3.Row, reason: str) -> dict[str, Any]:
        return {
            "decision": reason,
            "reason_codes": [reason],
            "current_stage": state["stage"],
            "why_locked": "请求没有通过模块一的服务端门禁。",
            "allowed_actions": list(ACTION_ALLOWLIST[state["stage"]]),
            "next_action": NEXT_ACTION_TEXT[state["stage"]],
            "wake_boundary_required": state["stage"] in {
                "candidate_wait",
                "draft_only_recovery",
            },
            "state_changed": False,
            "pointer_changed": False,
        }

    def _insert_artifact(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        model_id: str,
        kind: str,
        content: Mapping[str, Any],
        wake_id: str,
        supersede_active: bool = False,
    ) -> str:
        artifact_persistence_payload = {
            "owner_id": owner_id,
            "model_id": model_id,
            "kind": kind,
            "content": dict(content),
            "wake_id": wake_id,
        }
        if contains_credential_or_secret(
            artifact_persistence_payload
        ) or self._contains_protected_value(
            connection,
            owner_id=owner_id,
            model_id=model_id,
            value=artifact_persistence_payload,
        ):
            # This is the final common boundary for every onboarding artifact.
            # Call-site validation remains useful for field-specific feedback,
            # but no raw credential may cross this persistence primitive.
            raise OnboardingError("protected_persistence_value")
        supersedes = None
        if supersede_active:
            previous = self._active_artifact(
                connection, owner_id=owner_id, model_id=model_id, kind=kind
            )
            if previous is not None:
                supersedes = previous["artifact_id"]
                connection.execute(
                    "UPDATE brain_onboarding_artifacts SET status = 'superseded' "
                    "WHERE artifact_id = ?",
                    (supersedes,),
                )
        artifact_id = _new_id("artifact")
        connection.execute(
            "INSERT INTO brain_onboarding_artifacts "
            "(artifact_id, owner_id, model_id, flow_version, kind, content_json, "
            " content_hash, status, created_wake_id, supersedes_artifact_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (
                artifact_id,
                owner_id,
                model_id,
                FLOW_VERSION,
                kind,
                _canonical(dict(content)),
                _sha256(dict(content)),
                wake_id,
                supersedes,
                _iso(),
            ),
        )
        return artifact_id

    @staticmethod
    def _replace_state(
        connection: sqlite3.Connection,
        state: sqlite3.Row,
        **changes: Any,
    ) -> sqlite3.Row | None:
        """CAS-update one onboarding row and return the fresh row.

        Column names are restricted here so an action handler cannot accidentally turn
        dynamic input into SQL.  Every transition increments ``row_version`` exactly
        once; stale clients therefore fail closed instead of replaying a transition.
        """
        allowed = {
            "flow_kind",
            "stage",
            "module_one_status",
            "injection_policy",
            "current_candidate_id",
            "calm_prompt_artifact_id",
            "base_revision_id",
            "submitted_wake_id",
            "submitted_wake_seq",
            "stage_entered_wake_id",
            "stage_entered_wake_seq",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise OnboardingError(f"unsupported state columns: {sorted(unknown)}")
        assignments = [f"{name} = ?" for name in changes]
        values = list(changes.values())
        assignments.extend(["row_version = row_version + 1", "updated_at = ?"])
        values.append(_iso())
        values.extend([state["owner_id"], state["model_id"], state["row_version"]])
        cursor = connection.execute(
            "UPDATE brain_onboarding_state SET "
            + ", ".join(assignments)
            + " WHERE owner_id = ? AND model_id = ? AND row_version = ?",
            tuple(values),
        )
        if cursor.rowcount != 1:
            return None
        return ModuleOneOnboardingStore._state_row(
            connection, state["owner_id"], state["model_id"]
        )

    def _success(
        self,
        connection: sqlite3.Connection,
        state: sqlite3.Row,
        *,
        decision: str,
        reason_codes: Sequence[str],
        event_id: str,
        pointer_changed: bool = False,
        **extra: Any,
    ) -> dict[str, Any]:
        payload = self._status_payload(connection, state)
        payload.update(
            {
                "decision": decision,
                "reason_codes": list(reason_codes),
                "event_id": event_id,
                "state_changed": True,
                "pointer_changed": pointer_changed,
            }
        )
        payload.update(extra)
        return payload

    @staticmethod
    def _injected_snapshot(
        connection: sqlite3.Connection, wake_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM brain_context_snapshots WHERE wake_id = ? "
            "AND status = 'injected'",
            (wake_id,),
        ).fetchone()

    def _validate_transition_gate(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        action: str,
        actor: str,
        wake_id: str | None,
        wake_capability: str | None,
        expected_row_version: int | None,
    ) -> tuple[sqlite3.Row | None, dict[str, Any] | None]:
        if actor != "ai":
            return None, self._deny(state, "actor_not_authorized")
        if action not in AI_ACTIONS:
            return None, self._deny(state, "progression_required")
        wake, reason = self._validate_wake(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            wake_id=wake_id,
            wake_capability=wake_capability,
            require_current=True,
            require_injected=False,
        )
        if reason:
            return wake, self._deny(state, reason)
        assert wake is not None
        direct_mode = wake["source_kind"] == DIRECT_CONTEXT_MODE
        if not direct_mode and (
            wake["context_hash"] is None or wake["injected_at"] is None
        ):
            return wake, self._deny(state, "context_not_injected")
        snapshot = (
            connection.execute(
                "SELECT * FROM brain_context_snapshots WHERE wake_id=? "
                "AND status='prepared'",
                (wake["wake_id"],),
            ).fetchone()
            if direct_mode
            else self._injected_snapshot(connection, wake["wake_id"])
        )
        if snapshot is None or snapshot["context_hash"] != wake["context_hash"]:
            return wake, self._deny(
                state,
                "direct_grant_required" if direct_mode else "context_not_injected",
            )
        opened = connection.execute(
            "SELECT artifact_id, content_json FROM brain_onboarding_artifacts "
            "WHERE owner_id = ? AND model_id = ? AND kind = 'brain_manual_opened' "
            "AND status = 'active' AND created_wake_id = ? "
            "ORDER BY artifact_seq DESC LIMIT 1",
            (state["owner_id"], state["model_id"], wake["wake_id"]),
        ).fetchone()
        if opened is None:
            return wake, self._deny(state, "brain_open_required")
        if direct_mode:
            grant = connection.execute(
                "SELECT * FROM brain_direct_grants WHERE opened_wake_id=? "
                "AND owner_id=? AND model_id=? AND status='consumed'",
                (wake["wake_id"], state["owner_id"], state["model_id"]),
            ).fetchone()
            try:
                opened_content = json.loads(opened["content_json"])
            except (TypeError, json.JSONDecodeError):
                opened_content = None
            try:
                grant_scopes = (
                    json.loads(grant["scopes_json"]) if grant is not None else None
                )
            except (TypeError, json.JSONDecodeError):
                grant_scopes = None
            if (
                grant is None
                or _parse_iso(grant["expires_at"]) <= _now_dt()
                or not isinstance(opened_content, Mapping)
                or not isinstance(grant_scopes, list)
                or opened_content.get("opened_by") != "stbrain_open_direct"
                or opened_content.get("context_mode") != DIRECT_CONTEXT_MODE
                or opened_content.get("context_hash") != wake["context_hash"]
                or opened_content.get("authorized_scopes")
                != grant_scopes
                or "self_revision" not in grant_scopes
            ):
                return wake, self._deny(state, "direct_scope_not_authorized")
        if expected_row_version is None or expected_row_version != state["row_version"]:
            return wake, self._deny(state, "state_version_conflict")
        if action not in ACTION_ALLOWLIST[state["stage"]]:
            if action in {"accept_candidate_review", "activate_candidate"} and state["stage"] == "candidate_wait":
                if wake["wake_seq"] <= (state["submitted_wake_seq"] or 0):
                    return wake, self._deny(state, "same_wake_activation_forbidden")
                return wake, self._deny(state, "brain_open_required")
            if action == "submit_candidate" and state["stage"] == "calm_prompt_draft":
                return wake, self._deny(state, "calm_prompt_required")
            if action == "confirm_edit":
                return wake, self._deny(state, "edit_consent_required")
            return wake, self._deny(state, "progression_required")
        return wake, None

    def advance(
        self,
        *,
        owner_id: str,
        model_id: str,
        action: str,
        wake_id: str | None,
        wake_capability: str | None,
        expected_row_version: int | None,
        payload: Mapping[str, Any] | None = None,
        actor: str = "ai",
    ) -> dict[str, Any]:
        """Run one allow-listed AI transition.

        This is the only AI-facing mutation entry point for module one.  Host-issued
        wake credentials, injection proof, stage order, and row-version CAS are all
        checked before an action handler can write an artifact or candidate.
        """
        owner_id = _require_text("owner_id", owner_id)
        model_id = _require_text("model_id", model_id)
        action = _require_text("action", action)
        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping):
            raise OnboardingError("payload must be an object")
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            self._begin(connection)
            state = self._state_row(connection, owner_id, model_id)
            assert state is not None
            wake, denied = self._validate_transition_gate(
                connection,
                state=state,
                action=action,
                actor=actor,
                wake_id=wake_id,
                wake_capability=wake_capability,
                expected_row_version=expected_row_version,
            )
            if denied is not None:
                return denied
            assert wake is not None
            handler = getattr(self, f"_action_{action}")
            return handler(connection, state=state, wake=wake, payload=dict(payload))

    def _simple_ack_transition(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
        artifact_kind: str,
        action: str,
        next_stage: str,
    ) -> dict[str, Any]:
        if payload.get("acknowledged") is not True:
            return self._deny(state, "progression_required")
        artifact_id = self._insert_artifact(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            kind=artifact_kind,
            content={
                "acknowledged": True,
                "flow_version": FLOW_VERSION,
                "context_hash": wake["context_hash"],
            },
            wake_id=wake["wake_id"],
        )
        after = self._replace_state(
            connection,
            state,
            stage=next_stage,
            module_one_status="in_progress",
            stage_entered_wake_id=wake["wake_id"],
            stage_entered_wake_seq=wake["wake_seq"],
        )
        if after is None:
            return self._deny(state, "state_version_conflict")
        event_id = self._insert_event(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            stage_before=state["stage"],
            stage_after=next_stage,
            action=action,
            actor="ai",
            wake_id=wake["wake_id"],
            decision="advanced",
            reason_codes=[f"{artifact_kind}_confirmed"],
            details={"artifact_id": artifact_id, "context_hash": wake["context_hash"]},
        )
        return self._success(
            connection,
            after,
            decision="advanced",
            reason_codes=[f"{artifact_kind}_confirmed"],
            event_id=event_id,
            artifact_id=artifact_id,
        )

    def _action_confirm_brain_intro(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._simple_ack_transition(
            connection,
            state=state,
            wake=wake,
            payload=payload,
            artifact_kind="brain_intro_ack",
            action="confirm_brain_intro",
            next_stage="module_intro",
        )

    def _action_confirm_module_intro(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._simple_ack_transition(
            connection,
            state=state,
            wake=wake,
            payload=payload,
            artifact_kind="module_intro_ack",
            action="confirm_module_intro",
            next_stage="calm_prompt_draft",
        )

    def _action_save_calm_prompt(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            text = _require_text("text", payload.get("text"))
        except OnboardingError:
            return self._deny(state, "calm_prompt_required")
        reasons = self.self_store._text_findings(text)
        if self._contains_protected_value(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            value=text,
        ):
            reasons.append("credential_or_secret_detected")
        if len(text) > MAX_CALM_PROMPT_CHARS:
            reasons.append("calm_prompt_too_long")
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            return {
                **self._deny(state, reasons[0]),
                "decision": "reject",
                "reason_codes": reasons,
                "content_persisted": False,
            }
        artifact_id = self._insert_artifact(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            kind="calm_prompt",
            content={"text": text},
            wake_id=wake["wake_id"],
            supersede_active=True,
        )
        after = self._replace_state(
            connection,
            state,
            stage="body_draft",
            calm_prompt_artifact_id=artifact_id,
            stage_entered_wake_id=wake["wake_id"],
            stage_entered_wake_seq=wake["wake_seq"],
        )
        if after is None:
            return self._deny(state, "state_version_conflict")
        event_id = self._insert_event(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            stage_before=state["stage"],
            stage_after="body_draft",
            action="save_calm_prompt",
            actor="ai",
            wake_id=wake["wake_id"],
            decision="saved",
            reason_codes=["calm_prompt_saved"],
            details={"artifact_id": artifact_id, "content_hash": _sha256(text)},
        )
        return self._success(
            connection,
            after,
            decision="saved",
            reason_codes=["calm_prompt_saved"],
            event_id=event_id,
            artifact_id=artifact_id,
        )

    @staticmethod
    def _derived_evidence_refs(content: Any) -> list[str]:
        """Extract stable evidence identifiers from validated candidate anchors.

        Malformed anchors remain the content validator's responsibility.  This
        helper deliberately returns only real, non-empty ``memory_ref`` values;
        it never invents a bootstrap reference merely to satisfy persistence.
        """
        if not isinstance(content, Mapping):
            return []
        anchors = content.get("anchor_references")
        if not isinstance(anchors, Sequence) or isinstance(anchors, (str, bytes)):
            return []
        seen: set[str] = set()
        refs: list[str] = []
        for anchor in anchors:
            if not isinstance(anchor, Mapping):
                continue
            raw_ref = anchor.get("memory_ref")
            if not isinstance(raw_ref, str) or not raw_ref.strip():
                continue
            ref = raw_ref.strip()
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
        return refs

    @staticmethod
    def _deterministic_candidate_diff(
        baseline: Mapping[str, Any] | None,
        content: Any,
    ) -> list[dict[str, str]]:
        """Return a bounded, deterministic top-level candidate diff.

        Candidate rows already store the complete canonical content and hash, so
        a top-level path summary is sufficient for review while avoiding values
        or private material in diff metadata.  The fixed framework order keeps
        retries byte-for-byte stable.
        """
        if not isinstance(content, Mapping):
            return []
        result: list[dict[str, str]] = []
        for key in BODY_FRAMEWORK["required_top_level"]:
            path = "/" + str(key).replace("~", "~0").replace("/", "~1")
            if baseline is None:
                if key in content:
                    result.append({"op": "add", "path": path})
            elif key not in baseline and key in content:
                result.append({"op": "add", "path": path})
            elif key in baseline and key not in content:
                result.append({"op": "remove", "path": path})
            elif baseline.get(key) != content.get(key):
                result.append({"op": "replace", "path": path})
        return result

    def _derived_diff_baseline(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
    ) -> tuple[Mapping[str, Any] | None, str | None]:
        """Load and integrity-check the baseline used for a public candidate diff."""
        if state["stage"] == "candidate_review" and state["current_candidate_id"]:
            row = connection.execute(
                "SELECT model_id, content_json, content_hash FROM self_model_candidates "
                "WHERE candidate_id = ?",
                (state["current_candidate_id"],),
            ).fetchone()
            failure = "candidate_integrity_failure"
        else:
            base_revision_id = state["base_revision_id"]
            if base_revision_id is None:
                return None, None
            row = connection.execute(
                "SELECT model_id, content_json, content_hash FROM self_model_revisions "
                "WHERE revision_id = ?",
                (base_revision_id,),
            ).fetchone()
            failure = "active_revision_integrity_failure"
        if row is None or row["model_id"] != state["model_id"]:
            return None, failure
        try:
            baseline = json.loads(row["content_json"])
        except (TypeError, json.JSONDecodeError):
            return None, failure
        if not isinstance(baseline, Mapping) or _sha256(baseline) != row["content_hash"]:
            return None, failure
        return baseline, None

    def _candidate_preflight(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
        content = payload.get("content")
        reason_text = payload.get("reason")
        server_derived = payload.get(SERVER_DERIVED_CANDIDATE_MARKER) is True

        model = self.self_store._ensure_model(
            connection, state["model_id"], state["owner_id"]
        )
        findings, metrics = self.self_store._content_findings(content)
        reasons = list(findings)

        if server_derived:
            expected_active = state["base_revision_id"]
            origin = "ai_self"
            automatic = "grounded"
            human = "grounded"
            ai_authored_reason = True
            evidence_refs = self._derived_evidence_refs(content)
            baseline, baseline_failure = self._derived_diff_baseline(
                connection, state=state
            )
            if baseline_failure is not None:
                reasons.append(baseline_failure)
                diff: Any = []
            else:
                diff = self._deterministic_candidate_diff(baseline, content)
                if baseline is not None and isinstance(content, Mapping) and not diff:
                    reasons.append("no_content_change")
        else:
            # Preserve the full legacy/internal contract for non-public callers.
            diff = payload.get("diff")
            evidence_refs = payload.get("evidence_refs")
            origin = payload.get("origin", "ai_self")
            automatic = payload.get("automatic_state_signal", "grounded")
            human = payload.get("human_state_signal", "grounded")
            ai_authored_reason = payload.get("ai_authored_reason", True)
            expected_active = payload.get("expected_active_revision")

        reasons.extend(
            self.self_store._text_findings(
                {
                    "diff": diff,
                    "reason": reason_text,
                    "evidence_refs": evidence_refs,
                }
            )
        )
        if self._contains_protected_value(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            value=payload,
        ):
            reasons.append("credential_or_secret_detected")
        if model["active_revision_id"] != expected_active:
            reasons.append("active_revision_conflict")
        if state["base_revision_id"] != expected_active:
            reasons.append("active_revision_conflict")
        if not isinstance(diff, Sequence) or isinstance(diff, (str, bytes)) or not diff:
            if not server_derived:
                reasons.append("missing_diff")
        elif len(diff) > MAX_DIFF_ITEMS or any(
            not isinstance(item, Mapping)
            or set(item) != {"op", "path"}
            or item.get("op") not in {"add", "remove", "replace"}
            or not isinstance(item.get("path"), str)
            or not item.get("path", "").startswith("/")
            or len(item.get("path", "")) > MAX_DIFF_PATH_CHARS
            for item in diff
        ):
            reasons.append("invalid_diff")
        if (
            not isinstance(evidence_refs, Sequence)
            or isinstance(evidence_refs, (str, bytes))
            or not all(self.self_store._nonempty(ref) for ref in evidence_refs)
        ):
            if server_derived:
                reasons.append("invalid_evidence_refs")
            else:
                reasons.append("missing_evidence_refs")
        elif len(evidence_refs) > MAX_EVIDENCE_REFS or any(
            len(ref) > MAX_EVIDENCE_REF_CHARS for ref in evidence_refs
        ):
            reasons.append("invalid_evidence_refs")
        elif not evidence_refs and not server_derived:
            reasons.append("missing_evidence_refs")
        if not self.self_store._nonempty(reason_text):
            reasons.append("missing_reason")
        elif len(reason_text) > MAX_CANDIDATE_REASON_CHARS:
            reasons.append("candidate_reason_too_long")
        if origin not in {"ai_self", "external_suggestion", "recovery"}:
            reasons.append("invalid_origin")
        if origin == "external_suggestion" and ai_authored_reason is not True:
            reasons.append("external_origin_without_ai_reason")
        try:
            effective_state = self.self_store._effective_state(str(automatic), str(human))
        except SelfRevisionError:
            reasons.append("invalid_state_signal")
            effective_state = "frozen"
        normalized = {
            "content": content,
            "diff": list(diff)
            if isinstance(diff, Sequence) and not isinstance(diff, (str, bytes))
            else [],
            "reason": reason_text.strip() if isinstance(reason_text, str) else "",
            "evidence_refs": list(evidence_refs)
            if isinstance(evidence_refs, Sequence) and not isinstance(evidence_refs, (str, bytes))
            else [],
            "origin": origin,
            "automatic_state_signal": str(automatic),
            "human_state_signal": str(human),
            "ai_authored_reason": bool(ai_authored_reason),
            "effective_state": effective_state,
            "expected_active_revision": expected_active,
        }
        return list(dict.fromkeys(reasons)), metrics, normalized

    def _store_candidate(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        normalized: Mapping[str, Any],
        metrics: Mapping[str, Any],
    ) -> tuple[str, str, list[str], str]:
        candidate_id = _new_id("cand")
        effective_state = normalized["effective_state"]
        decision = "draft_only" if effective_state != "grounded" else "pending"
        reason_codes = (
            [f"owner_state_{effective_state}"]
            if decision == "draft_only"
            else ["candidate_valid", "later_real_wake_required"]
        )
        content = normalized["content"]
        connection.execute(
            "INSERT INTO self_model_candidates "
            "(candidate_id, model_id, base_revision_id, content_json, content_hash, "
            " diff_json, reason, evidence_refs_json, checkpoint_id, origin, "
            " automatic_state_signal, human_state_signal, ai_authored_reason, "
            " length_metrics_json, initial_decision, initial_reason_codes_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate_id,
                state["model_id"],
                normalized["expected_active_revision"],
                _canonical(content),
                _sha256(content),
                _canonical(normalized["diff"]),
                normalized["reason"],
                _canonical(normalized["evidence_refs"]),
                wake["wake_id"],
                normalized["origin"],
                normalized["automatic_state_signal"],
                normalized["human_state_signal"],
                int(normalized["ai_authored_reason"]),
                _canonical(dict(metrics)),
                decision,
                _canonical(reason_codes),
                _iso(),
            ),
        )
        event_type = "candidate_draft_only" if decision == "draft_only" else "candidate_pending"
        old_event_id = self.self_store._insert_event(
            connection,
            model_id=state["model_id"],
            candidate_id=candidate_id,
            revision_id=None,
            event_type=event_type,
            checkpoint_id=wake["wake_id"],
            actor="ai",
            decision=decision,
            reason_codes=reason_codes,
            details={
                "base_revision_id": normalized["expected_active_revision"],
                "content_hash": _sha256(content),
                "effective_state": effective_state,
                "length_metrics": dict(metrics),
                "wake_seq": wake["wake_seq"],
            },
        )
        return candidate_id, decision, reason_codes, old_event_id

    def _action_submit_candidate(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        calm = self._active_artifact(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            kind="calm_prompt",
        )
        if calm is None or calm["artifact_id"] != state["calm_prompt_artifact_id"]:
            return self._deny(state, "calm_prompt_required")
        reasons, metrics, normalized = self._candidate_preflight(
            connection, state=state, wake=wake, payload=payload
        )
        if reasons:
            decision = "revise" if self.self_store._length_only(reasons) else "reject"
            # Rejected content is deliberately not persisted in either artifact store.
            return {
                **self._deny(state, reasons[0]),
                "decision": decision,
                "reason_codes": reasons,
                "length_metrics": metrics,
                "content_persisted": False,
            }
        candidate_id, decision, reason_codes, old_event_id = self._store_candidate(
            connection,
            state=state,
            wake=wake,
            normalized=normalized,
            metrics=metrics,
        )
        next_stage = "draft_only_recovery" if decision == "draft_only" else "candidate_wait"
        after = self._replace_state(
            connection,
            state,
            stage=next_stage,
            current_candidate_id=candidate_id,
            submitted_wake_id=wake["wake_id"],
            submitted_wake_seq=wake["wake_seq"],
            stage_entered_wake_id=wake["wake_id"],
            stage_entered_wake_seq=wake["wake_seq"],
        )
        if after is None:
            return self._deny(state, "state_version_conflict")
        event_id = self._insert_event(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            stage_before=state["stage"],
            stage_after=next_stage,
            action="submit_candidate",
            actor="ai",
            wake_id=wake["wake_id"],
            candidate_id=candidate_id,
            decision=decision,
            reason_codes=reason_codes,
            details={
                "content_hash": _sha256(normalized["content"]),
                "old_audit_event_id": old_event_id,
                "length_metrics": metrics,
            },
        )
        return self._success(
            connection,
            after,
            decision=decision,
            reason_codes=reason_codes,
            event_id=event_id,
            candidate_id=candidate_id,
            length_metrics=metrics,
            next_real_wake_required=True,
        )

    def _review_artifact(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM brain_onboarding_artifacts WHERE owner_id = ? AND model_id = ? "
            "AND kind = 'candidate_full_review' AND status = 'active' "
            "AND created_wake_id = ? ORDER BY artifact_seq DESC LIMIT 1",
            (state["owner_id"], state["model_id"], wake["wake_id"]),
        ).fetchone()

    def _action_accept_candidate_review(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Record AI review without changing the active self-model pointer."""
        candidate_id = state["current_candidate_id"]
        candidate = self.self_store._candidate_row(connection, candidate_id)
        reasons: list[str] = []
        if wake["wake_seq"] <= (state["submitted_wake_seq"] or 0):
            reasons.append("same_wake_activation_forbidden")
        review = self._review_artifact(connection, state=state, wake=wake)
        if review is None:
            reasons.append("candidate_full_review_required")
        else:
            review_content = json.loads(review["content_json"])
            if (
                review_content.get("candidate_id") != candidate_id
                or review_content.get("content_hash") != candidate["content_hash"]
            ):
                reasons.append("candidate_full_review_required")
        lifecycle, _ = self.self_store._candidate_state(connection, candidate_id)
        if lifecycle != "pending":
            reasons.append("candidate_not_pending")
        if self.self_store._has_unresolved_objection(connection, candidate_id):
            reasons.append("human_objection_pending")
        findings, metrics = self.self_store._content_findings(
            json.loads(candidate["content_json"])
        )
        reasons.extend(findings)
        if payload.get("ai_confirmation") is not True:
            reasons.append("candidate_full_review_required")
        if self._candidate_review_acceptance(connection, state=state) is not None:
            reasons.append("candidate_review_already_accepted")
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            return {
                **self._deny(state, reasons[0]),
                "decision": "reject",
                "reason_codes": reasons,
                "length_metrics": metrics,
            }

        acceptance_id = self._insert_artifact(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            kind="candidate_review_accepted",
            content={
                "candidate_id": candidate_id,
                "content_hash": candidate["content_hash"],
                "review_wake_id": wake["wake_id"],
                "review_wake_seq": wake["wake_seq"],
                "context_hash": wake["context_hash"],
            },
            wake_id=wake["wake_id"],
            supersede_active=True,
        )
        after = self._replace_state(connection, state, stage="candidate_review")
        if after is None:
            return self._deny(state, "state_version_conflict")
        event_id = self._insert_event(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            stage_before="candidate_review",
            stage_after="candidate_review",
            action="accept_candidate_review",
            actor="ai",
            wake_id=wake["wake_id"],
            candidate_id=candidate_id,
            decision="review_accepted",
            reason_codes=["candidate_review_recorded", "next_real_wake_required"],
            details={
                "acceptance_artifact_id": acceptance_id,
                "content_hash": candidate["content_hash"],
                "context_hash": wake["context_hash"],
            },
        )
        return self._success(
            connection,
            after,
            decision="review_accepted",
            reason_codes=["candidate_review_recorded", "next_real_wake_required"],
            event_id=event_id,
            candidate_id=candidate_id,
            review_acceptance_id=acceptance_id,
            next_real_wake_required=True,
            length_metrics=metrics,
        )

    def _action_activate_candidate(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        candidate_id = state["current_candidate_id"]
        # Public /7 activation always carries the candidate selected by the
        # caller and binds it here, inside the authoritative transaction.  Keep
        # the pre-existing internal/legacy action shape compatible when that
        # field is absent; those callers already operate on state.current_candidate.
        requested_candidate_id = payload.get("candidate_id")
        if "candidate_id" in payload and (
            not isinstance(requested_candidate_id, str)
            or requested_candidate_id != candidate_id
        ):
            return {
                **self._deny(state, "candidate_not_current"),
                "decision": "reject",
                "reason_codes": ["candidate_not_current"],
            }
        candidate = self.self_store._candidate_row(connection, candidate_id)
        reasons: list[str] = []
        acceptance = self._candidate_review_acceptance(connection, state=state)
        if acceptance is None:
            reasons.append("candidate_review_acceptance_required")
        elif wake["wake_seq"] <= int(acceptance.get("review_wake_seq") or 0):
            reasons.append("review_activation_wake_boundary_required")
        review = self._review_artifact(connection, state=state, wake=wake)
        if review is None:
            reasons.append("candidate_full_review_required")
        else:
            review_content = json.loads(review["content_json"])
            if (
                review_content.get("candidate_id") != candidate_id
                or review_content.get("content_hash") != candidate["content_hash"]
            ):
                reasons.append("candidate_full_review_required")
        lifecycle, _ = self.self_store._candidate_state(connection, candidate_id)
        if lifecycle != "pending":
            reasons.append("candidate_not_pending")
        if self.self_store._has_unresolved_objection(connection, candidate_id):
            reasons.append("human_objection_pending")
        model = self.self_store._model_row(connection, state["model_id"])
        assert model is not None
        expected_active = payload.get("expected_active_revision")
        if (
            model["active_revision_id"] != expected_active
            or candidate["base_revision_id"] != expected_active
            or state["base_revision_id"] != expected_active
        ):
            reasons.append("active_revision_conflict")
        findings, metrics = self.self_store._content_findings(json.loads(candidate["content_json"]))
        reasons.extend(findings)
        if payload.get("ai_confirmation") is not True:
            reasons.append("candidate_full_review_required")
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            return {
                **self._deny(state, reasons[0]),
                "decision": "reject",
                "reason_codes": reasons,
                "length_metrics": metrics,
            }

        revision_id = _new_id("rev")
        revision_number = connection.execute(
            "SELECT COALESCE(MAX(revision_number), 0) + 1 AS n "
            "FROM self_model_revisions WHERE model_id = ?",
            (state["model_id"],),
        ).fetchone()["n"]
        cursor = connection.execute(
            "UPDATE self_models SET active_revision_id = ? WHERE model_id = ? "
            "AND active_revision_id IS ?",
            (revision_id, state["model_id"], expected_active),
        )
        if cursor.rowcount != 1:
            return self._deny(state, "active_revision_conflict")
        connection.execute(
            "INSERT INTO self_model_revisions "
            "(revision_id, model_id, parent_revision_id, revision_number, content_json, "
            " content_hash, candidate_id, author, activated_at, activation_checkpoint_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'ai', ?, ?)",
            (
                revision_id,
                state["model_id"],
                expected_active,
                revision_number,
                candidate["content_json"],
                candidate["content_hash"],
                candidate_id,
                _iso(),
                wake["wake_id"],
            ),
        )
        old_event_id = self.self_store._insert_event(
            connection,
            model_id=state["model_id"],
            candidate_id=candidate_id,
            revision_id=revision_id,
            event_type="candidate_activated",
            checkpoint_id=wake["wake_id"],
            actor="ai",
            decision="activate",
            reason_codes=["ai_review_then_cross_wake_activation", "cas_succeeded"],
            details={
                "parent_revision_id": expected_active,
                "revision_number": revision_number,
                "content_hash": candidate["content_hash"],
                "context_hash": wake["context_hash"],
            },
        )
        after = self._replace_state(
            connection,
            state,
            flow_kind="live",
            stage="live",
            module_one_status="complete",
            injection_policy="normal",
            current_candidate_id=None,
            base_revision_id=revision_id,
            submitted_wake_id=None,
            submitted_wake_seq=None,
            stage_entered_wake_id=wake["wake_id"],
            stage_entered_wake_seq=wake["wake_seq"],
        )
        if after is None:
            raise OnboardingError("onboarding CAS failed after revision pointer CAS")
        connection.execute(
            "INSERT INTO brain_module_unlocks "
            "(owner_id, model_id, module_name, unlocked, basis_revision_id, updated_at) "
            "VALUES (?, ?, 'module_one', 1, ?, ?) "
            "ON CONFLICT(owner_id, model_id, module_name) DO UPDATE SET "
            "unlocked = 1, basis_revision_id = excluded.basis_revision_id, "
            "updated_at = excluded.updated_at",
            (state["owner_id"], state["model_id"], revision_id, _iso()),
        )
        event_id = self._insert_event(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            stage_before="candidate_review",
            stage_after="live",
            action="activate_candidate",
            actor="ai",
            wake_id=wake["wake_id"],
            candidate_id=candidate_id,
            revision_id=revision_id,
            decision="activate",
            reason_codes=["module_one_completed", "cas_succeeded"],
            details={"old_audit_event_id": old_event_id, "revision_number": revision_number},
        )
        return self._success(
            connection,
            after,
            decision="activate",
            reason_codes=["module_one_completed", "cas_succeeded"],
            event_id=event_id,
            pointer_changed=True,
            candidate_id=candidate_id,
            revision_id=revision_id,
            active_revision_id=revision_id,
            length_metrics=metrics,
        )

    def _action_revise_candidate(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        old_candidate_id = state["current_candidate_id"]
        old_candidate = self.self_store._candidate_row(connection, old_candidate_id)
        lifecycle, _ = self.self_store._candidate_state(connection, old_candidate_id)
        if lifecycle != "pending":
            return self._deny(state, "candidate_not_pending")
        reasons, metrics, normalized = self._candidate_preflight(
            connection, state=state, wake=wake, payload=payload
        )
        if reasons:
            decision = "revise" if self.self_store._length_only(reasons) else "reject"
            return {
                **self._deny(state, reasons[0]),
                "decision": decision,
                "reason_codes": reasons,
                "length_metrics": metrics,
                "content_persisted": False,
            }
        if _sha256(normalized["content"]) == old_candidate["content_hash"]:
            return self._deny(state, "revision_must_append_new_candidate")
        withdraw_event = self.self_store._insert_event(
            connection,
            model_id=state["model_id"],
            candidate_id=old_candidate_id,
            revision_id=None,
            event_type="candidate_withdrawn",
            checkpoint_id=wake["wake_id"],
            actor="ai",
            decision="withdraw",
            reason_codes=["superseded_by_ai_revision"],
            details={"old_content_hash": old_candidate["content_hash"]},
        )
        new_candidate_id, decision, reason_codes, new_old_event = self._store_candidate(
            connection,
            state=state,
            wake=wake,
            normalized=normalized,
            metrics=metrics,
        )
        self._insert_artifact(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            kind="candidate_revision_chain",
            content={
                "superseded_candidate_id": old_candidate_id,
                "replacement_candidate_id": new_candidate_id,
            },
            wake_id=wake["wake_id"],
        )
        next_stage = "draft_only_recovery" if decision == "draft_only" else "candidate_wait"
        after = self._replace_state(
            connection,
            state,
            stage=next_stage,
            current_candidate_id=new_candidate_id,
            submitted_wake_id=wake["wake_id"],
            submitted_wake_seq=wake["wake_seq"],
            stage_entered_wake_id=wake["wake_id"],
            stage_entered_wake_seq=wake["wake_seq"],
        )
        if after is None:
            return self._deny(state, "state_version_conflict")
        event_id = self._insert_event(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            stage_before="candidate_review",
            stage_after=next_stage,
            action="revise_candidate",
            actor="ai",
            wake_id=wake["wake_id"],
            candidate_id=new_candidate_id,
            decision=decision,
            reason_codes=["candidate_replaced_append_only", *reason_codes],
            details={
                "superseded_candidate_id": old_candidate_id,
                "withdraw_event_id": withdraw_event,
                "candidate_event_id": new_old_event,
            },
        )
        return self._success(
            connection,
            after,
            decision=decision,
            reason_codes=["candidate_replaced_append_only", *reason_codes],
            event_id=event_id,
            candidate_id=new_candidate_id,
            superseded_candidate_id=old_candidate_id,
            next_real_wake_required=True,
        )

    def _action_respond_to_objection(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        candidate_id = state["current_candidate_id"]
        objection = self._unresolved_objection_payload(connection, candidate_id)
        if objection is None:
            return self._deny(state, "human_objection_not_pending")
        lifecycle, _ = self.self_store._candidate_state(connection, candidate_id)
        if lifecycle != "pending":
            return self._deny(state, "candidate_not_pending")
        exposure = connection.execute(
            "SELECT content_json FROM brain_onboarding_artifacts "
            "WHERE owner_id = ? AND model_id = ? "
            "AND kind = 'human_objection_presented' AND status = 'active' "
            "AND created_wake_id = ? ORDER BY artifact_seq DESC LIMIT 1",
            (state["owner_id"], state["model_id"], wake["wake_id"]),
        ).fetchone()
        shown_objection = json.loads(exposure["content_json"]) if exposure else None
        if not shown_objection or shown_objection.get("objection_event_id") != objection["event_id"]:
            return self._deny(state, "human_objection_not_presented")
        try:
            response_text = _require_text("response", payload.get("response"))
        except OnboardingError:
            return self._deny(state, "objection_response_required")
        response_findings = self.self_store._text_findings(response_text)
        if self._contains_protected_value(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            value=response_text,
        ):
            response_findings.append("credential_or_secret_detected")
        if len(response_text) > MAX_OBJECTION_RESPONSE_CHARS:
            response_findings.append("objection_response_too_long")
        response_findings = list(dict.fromkeys(response_findings))
        if response_findings:
            return {
                **self._deny(state, response_findings[0]),
                "decision": "reject",
                "reason_codes": response_findings,
                "content_persisted": False,
            }
        resolution = payload.get("resolution", "continue")
        if resolution not in {"continue", "withdraw"}:
            return self._deny(state, "invalid_objection_resolution")

        resolved_event_id = self.self_store._insert_event(
            connection,
            model_id=state["model_id"],
            candidate_id=candidate_id,
            revision_id=None,
            event_type="human_objection_resolved",
            checkpoint_id=wake["wake_id"],
            actor="ai",
            decision="pending" if resolution == "continue" else "withdraw",
            reason_codes=["ai_considered_objection"],
            details={
                "ai_response": response_text,
                "objection_event_id": objection["event_id"],
                "resolution": resolution,
            },
        )
        if resolution == "continue":
            after = self._replace_state(
                connection,
                state,
                stage="candidate_wait",
                submitted_wake_id=wake["wake_id"],
                submitted_wake_seq=wake["wake_seq"],
                stage_entered_wake_id=wake["wake_id"],
                stage_entered_wake_seq=wake["wake_seq"],
            )
            if after is None:
                return self._deny(state, "state_version_conflict")
            event_id = self._insert_event(
                connection,
                owner_id=state["owner_id"],
                model_id=state["model_id"],
                stage_before="candidate_review",
                stage_after="candidate_wait",
                action="respond_to_objection",
                actor="ai",
                wake_id=wake["wake_id"],
                candidate_id=candidate_id,
                decision="pending",
                reason_codes=["ai_considered_objection", "another_real_wake_required"],
                details={
                    "objection_event_id": objection["event_id"],
                    "resolved_event_id": resolved_event_id,
                    "response_hash": _sha256(response_text),
                },
            )
            return self._success(
                connection,
                after,
                decision="pending",
                reason_codes=["ai_considered_objection", "another_real_wake_required"],
                event_id=event_id,
                candidate_id=candidate_id,
                next_real_wake_required=True,
            )

        withdrawal_event_id = self.self_store._insert_event(
            connection,
            model_id=state["model_id"],
            candidate_id=candidate_id,
            revision_id=None,
            event_type="candidate_withdrawn",
            checkpoint_id=wake["wake_id"],
            actor="ai",
            decision="withdraw",
            reason_codes=["ai_withdrew_after_objection"],
            details={
                "objection_event_id": objection["event_id"],
                "response_hash": _sha256(response_text),
            },
        )
        editing = state["flow_kind"] == "edit"
        next_stage = "live" if editing else "body_draft"
        next_flow = "live" if editing else "onboarding"
        after = self._replace_state(
            connection,
            state,
            flow_kind=next_flow,
            stage=next_stage,
            current_candidate_id=None,
            submitted_wake_id=None,
            submitted_wake_seq=None,
            stage_entered_wake_id=wake["wake_id"],
            stage_entered_wake_seq=wake["wake_seq"],
        )
        if after is None:
            return self._deny(state, "state_version_conflict")
        event_id = self._insert_event(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            stage_before="candidate_review",
            stage_after=next_stage,
            action="respond_to_objection",
            actor="ai",
            wake_id=wake["wake_id"],
            candidate_id=candidate_id,
            decision="withdraw",
            reason_codes=["ai_withdrew_after_objection"],
            details={
                "objection_event_id": objection["event_id"],
                "resolved_event_id": resolved_event_id,
                "withdrawal_event_id": withdrawal_event_id,
            },
        )
        return self._success(
            connection,
            after,
            decision="withdraw",
            reason_codes=["ai_withdrew_after_objection"],
            event_id=event_id,
            candidate_id=candidate_id,
        )

    def _action_withdraw_candidate(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        candidate_id = state["current_candidate_id"]
        lifecycle, submitted_checkpoint = self.self_store._candidate_state(
            connection, candidate_id
        )
        if lifecycle not in {"pending", "draft_only"}:
            return self._deny(state, "candidate_not_withdrawable")
        if wake["wake_id"] == submitted_checkpoint:
            return self._deny(state, "same_wake_withdrawal_forbidden")
        try:
            reason_text = _require_text("reason", payload.get("reason"))
        except OnboardingError:
            return self._deny(state, "withdrawal_reason_required")
        old_event_id = self.self_store._insert_event(
            connection,
            model_id=state["model_id"],
            candidate_id=candidate_id,
            revision_id=None,
            event_type="candidate_withdrawn",
            checkpoint_id=wake["wake_id"],
            actor="ai",
            decision="withdraw",
            reason_codes=["ai_independent_withdrawal"],
            details={"reason_hash": _sha256(reason_text)},
        )
        editing = state["flow_kind"] == "edit"
        next_stage = "live" if editing else "body_draft"
        next_flow = "live" if editing else "onboarding"
        after = self._replace_state(
            connection,
            state,
            flow_kind=next_flow,
            stage=next_stage,
            current_candidate_id=None,
            submitted_wake_id=None,
            submitted_wake_seq=None,
            stage_entered_wake_id=wake["wake_id"],
            stage_entered_wake_seq=wake["wake_seq"],
        )
        if after is None:
            return self._deny(state, "state_version_conflict")
        event_id = self._insert_event(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            stage_before=state["stage"],
            stage_after=next_stage,
            action="withdraw_candidate",
            actor="ai",
            wake_id=wake["wake_id"],
            candidate_id=candidate_id,
            decision="withdraw",
            reason_codes=["ai_independent_withdrawal"],
            details={"old_audit_event_id": old_event_id, "reason_hash": _sha256(reason_text)},
        )
        return self._success(
            connection,
            after,
            decision="withdraw",
            reason_codes=["ai_independent_withdrawal"],
            event_id=event_id,
            candidate_id=candidate_id,
        )

    def _action_recover_candidate(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        candidate_id = state["current_candidate_id"]
        candidate = self.self_store._candidate_row(connection, candidate_id)
        lifecycle, _ = self.self_store._candidate_state(connection, candidate_id)
        reasons: list[str] = []
        if lifecycle != "draft_only":
            reasons.append("candidate_not_draft_only")
        if wake["wake_seq"] <= (state["submitted_wake_seq"] or 0):
            reasons.append("independent_wake_required")
        automatic = str(payload.get("automatic_state_signal", "grounded"))
        human = str(payload.get("human_state_signal", "grounded"))
        try:
            effective = self.self_store._effective_state(automatic, human)
        except SelfRevisionError:
            effective = "frozen"
            reasons.append("invalid_state_signal")
        if effective != "grounded":
            reasons.append(f"owner_state_{effective}")
        model = self.self_store._model_row(connection, state["model_id"])
        assert model is not None
        if model["active_revision_id"] != candidate["base_revision_id"]:
            reasons.append("active_revision_conflict")
        findings, metrics = self.self_store._content_findings(json.loads(candidate["content_json"]))
        reasons.extend(findings)
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            return {
                **self._deny(state, reasons[0]),
                "reason_codes": reasons,
                "candidate_state": "draft_only",
                "length_metrics": metrics,
            }
        old_event_id = self.self_store._insert_event(
            connection,
            model_id=state["model_id"],
            candidate_id=candidate_id,
            revision_id=None,
            event_type="candidate_rechecked_pending",
            checkpoint_id=wake["wake_id"],
            actor="ai",
            decision="pending",
            reason_codes=["recovered_and_rechecked", "another_real_wake_required"],
            details={"effective_state": effective, "wake_seq": wake["wake_seq"]},
        )
        after = self._replace_state(
            connection,
            state,
            stage="candidate_wait",
            submitted_wake_id=wake["wake_id"],
            submitted_wake_seq=wake["wake_seq"],
            stage_entered_wake_id=wake["wake_id"],
            stage_entered_wake_seq=wake["wake_seq"],
        )
        if after is None:
            return self._deny(state, "state_version_conflict")
        event_id = self._insert_event(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            stage_before="draft_only_recovery",
            stage_after="candidate_wait",
            action="recover_candidate",
            actor="ai",
            wake_id=wake["wake_id"],
            candidate_id=candidate_id,
            decision="pending",
            reason_codes=["recovered_and_rechecked", "another_real_wake_required"],
            details={"old_audit_event_id": old_event_id},
        )
        return self._success(
            connection,
            after,
            decision="pending",
            reason_codes=["recovered_and_rechecked", "another_real_wake_required"],
            event_id=event_id,
            candidate_id=candidate_id,
            next_real_wake_required=True,
        )

    def _action_begin_edit(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        model = self.self_store._model_row(connection, state["model_id"])
        calm = self._active_artifact(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            kind="calm_prompt",
        )
        if model is None or model["active_revision_id"] is None:
            return self._deny(state, "active_revision_missing")
        if calm is None:
            return self._deny(state, "calm_prompt_required")
        challenge_id = _new_id("edit")
        response = self._challenge_response(challenge_id)
        now = _now_dt()
        connection.execute(
            "UPDATE brain_edit_challenges SET status = 'cancelled' "
            "WHERE owner_id = ? AND model_id = ? AND status = 'active'",
            (state["owner_id"], state["model_id"]),
        )
        connection.execute(
            "INSERT INTO brain_edit_challenges "
            "(challenge_id, owner_id, model_id, wake_id, active_revision_id, response_hash, "
            " status, issued_at, expires_at, consumed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)",
            (
                challenge_id,
                state["owner_id"],
                state["model_id"],
                wake["wake_id"],
                model["active_revision_id"],
                _sha256(response),
                _iso(now),
                _iso(now + timedelta(seconds=self.edit_challenge_ttl_seconds)),
            ),
        )
        calm_content = json.loads(calm["content_json"])
        artifact_id = self._insert_artifact(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            kind="edit_calm_presented",
            content={
                "calm_prompt_artifact_id": calm["artifact_id"],
                "calm_prompt_hash": calm["content_hash"],
                "challenge_id": challenge_id,
                "active_revision_id": model["active_revision_id"],
            },
            wake_id=wake["wake_id"],
        )
        after = self._replace_state(
            connection,
            state,
            flow_kind="edit",
            stage="edit_consent",
            base_revision_id=model["active_revision_id"],
            stage_entered_wake_id=wake["wake_id"],
            stage_entered_wake_seq=wake["wake_seq"],
        )
        if after is None:
            return self._deny(state, "state_version_conflict")
        event_id = self._insert_event(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            stage_before="live",
            stage_after="edit_consent",
            action="begin_edit",
            actor="ai",
            wake_id=wake["wake_id"],
            decision="challenge_issued",
            reason_codes=["calm_prompt_presented", "structured_edit_consent_required"],
            details={"challenge_id": challenge_id, "artifact_id": artifact_id},
        )
        return self._success(
            connection,
            after,
            decision="challenge_issued",
            reason_codes=["calm_prompt_presented", "structured_edit_consent_required"],
            event_id=event_id,
            calm_prompt=calm_content["text"],
            challenge_id=challenge_id,
            challenge_response=response,
            expires_at=_iso(now + timedelta(seconds=self.edit_challenge_ttl_seconds)),
        )

    def _action_confirm_edit(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        challenge_id = payload.get("challenge_id")
        response = payload.get("challenge_response")
        if not isinstance(challenge_id, str) or not isinstance(response, str):
            return self._deny(state, "edit_consent_required")
        challenge = connection.execute(
            "SELECT * FROM brain_edit_challenges WHERE challenge_id = ?",
            (challenge_id,),
        ).fetchone()
        if (
            challenge is None
            or challenge["owner_id"] != state["owner_id"]
            or challenge["model_id"] != state["model_id"]
            or challenge["wake_id"] != wake["wake_id"]
            or challenge["status"] != "active"
        ):
            return self._deny(state, "edit_consent_required")
        if _parse_iso(challenge["expires_at"]) <= _now_dt():
            return self._deny(state, "edit_consent_expired")
        if not hmac.compare_digest(challenge["response_hash"], _sha256(response)):
            return self._deny(state, "edit_consent_required")
        model = self.self_store._model_row(connection, state["model_id"])
        assert model is not None
        if model["active_revision_id"] != challenge["active_revision_id"]:
            return self._deny(state, "active_revision_conflict")
        connection.execute(
            "UPDATE brain_edit_challenges SET status = 'consumed', consumed_at = ? "
            "WHERE challenge_id = ? AND status = 'active'",
            (_iso(), challenge_id),
        )
        after = self._replace_state(
            connection,
            state,
            stage="edit_body_draft",
            stage_entered_wake_id=wake["wake_id"],
            stage_entered_wake_seq=wake["wake_seq"],
        )
        if after is None:
            return self._deny(state, "state_version_conflict")
        event_id = self._insert_event(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            stage_before="edit_consent",
            stage_after="edit_body_draft",
            action="confirm_edit",
            actor="ai",
            wake_id=wake["wake_id"],
            decision="confirmed",
            reason_codes=["structured_edit_consent_confirmed"],
            details={"challenge_id": challenge_id},
        )
        return self._success(
            connection,
            after,
            decision="confirmed",
            reason_codes=["structured_edit_consent_confirmed"],
            event_id=event_id,
        )

    def _action_cancel_edit(
        self,
        connection: sqlite3.Connection,
        *,
        state: sqlite3.Row,
        wake: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection.execute(
            "UPDATE brain_edit_challenges SET status = 'cancelled' "
            "WHERE owner_id = ? AND model_id = ? AND status = 'active'",
            (state["owner_id"], state["model_id"]),
        )
        after = self._replace_state(
            connection,
            state,
            flow_kind="live",
            stage="live",
            stage_entered_wake_id=wake["wake_id"],
            stage_entered_wake_seq=wake["wake_seq"],
        )
        if after is None:
            return self._deny(state, "state_version_conflict")
        event_id = self._insert_event(
            connection,
            owner_id=state["owner_id"],
            model_id=state["model_id"],
            stage_before=state["stage"],
            stage_after="live",
            action="cancel_edit",
            actor="ai",
            wake_id=wake["wake_id"],
            decision="cancelled",
            reason_codes=["edit_cancelled_by_ai"],
            details={},
        )
        return self._success(
            connection,
            after,
            decision="cancelled",
            reason_codes=["edit_cancelled_by_ai"],
            event_id=event_id,
        )

    def read_active_self_model(self, *, owner_id: str, model_id: str) -> dict[str, Any]:
        """Read an established active baseline without opening or injecting it.

        This deliberately does not call ensure_state, generate a snapshot, issue
        a wake, expose candidates, or create a review/presentation artifact. All
        ownership, completion and active-basis checks share one read snapshot.
        Reading is not authorization for a later write or candidate activation.
        """
        owner_id = _require_text("owner_id", owner_id)
        model_id = _require_text("model_id", model_id)

        def unavailable(reason: str) -> dict[str, Any]:
            return {
                "decision": "active_self_model_unavailable", "active_available": False,
                "reason_codes": [reason], "read_only": True,
                "state_changed": False, "pointer_changed": False,
            }

        with self._connect() as connection:
            connection.execute("BEGIN")
            state = self._state_row(connection, owner_id, model_id)
            if state is None:
                return unavailable("onboarding_state_unavailable")
            model = self.self_store._model_row(connection, model_id)
            if model is None or model["owner_id"] != owner_id:
                return unavailable("model_owner_mismatch")
            unlocked = connection.execute(
                "SELECT unlocked, basis_revision_id FROM brain_module_unlocks "
                "WHERE owner_id = ? AND model_id = ? AND module_name = 'module_one'",
                (owner_id, model_id),
            ).fetchone()
            if state["module_one_status"] != "complete" or unlocked is None or not unlocked["unlocked"]:
                return unavailable("module_one_required")
            if state["injection_policy"] != "normal":
                return unavailable("active_self_model_not_available")
            established_live = state["flow_kind"] == "live" and state["stage"] == "live"
            established_edit = state["flow_kind"] == "edit" and state["stage"] in {
                "edit_consent", "edit_body_draft", "candidate_wait", "candidate_review",
            }
            if not (established_live or established_edit):
                return unavailable("active_self_model_not_available")
            active_id = model["active_revision_id"]
            if not active_id:
                return unavailable("active_revision_missing")
            if active_id != state["base_revision_id"] or active_id != unlocked["basis_revision_id"]:
                return unavailable("active_revision_conflict")
            revision = self.self_store._revision_row(connection, active_id)
            if revision is None or revision["model_id"] != model_id or revision["author"] != "ai":
                return unavailable("active_revision_not_ai_approved")
            try:
                content = json.loads(revision["content_json"])
            except (TypeError, json.JSONDecodeError):
                return unavailable("active_injection_structure_invalid")
            if _sha256(content) != revision["content_hash"]:
                return unavailable("active_revision_hash_mismatch")
            if active_injection_structure_violations(content):
                return unavailable("active_injection_structure_invalid")
            return {
                "decision": "active_available", "active_available": True,
                "reason_codes": ["established_active_self_model"], "read_only": True,
                "state_changed": False, "pointer_changed": False,
                "active": {
                    "revision_id": revision["revision_id"],
                    "revision_number": revision["revision_number"],
                    "parent_revision_id": revision["parent_revision_id"],
                    "content_hash": revision["content_hash"],
                    "author": revision["author"],
                    "activated_at": revision["activated_at"],
                    "content": content,
                },
            }

    def authorize_other_module_write(
        self, *, owner_id: str, model_id: str, module_name: str
    ) -> dict[str, Any]:
        """Read-only gate used by every later module before accepting writes."""
        module_name = _require_text("module_name", module_name)
        from .ordinary_access import current_ordinary_access, MODULE_SCOPES
        ordinary_scope = MODULE_SCOPES.get(module_name)
        if ordinary_scope is not None and current_ordinary_access(
                owner_id=owner_id, model_id=model_id, scope=ordinary_scope) is not None:
            return {'decision': 'allowed', 'reason_codes': ['authenticated_ordinary_operation'],
                    'requested_module': module_name, 'state_changed': False, 'pointer_changed': False}
        self.ensure_state(owner_id=owner_id, model_id=model_id)
        with self._connect() as connection:
            # Read state, unlock and active basis from one consistent snapshot.
            # This is only an eligibility check, not the later write transaction
            # or its independently enforced execution/context authorization.
            connection.execute("BEGIN")
            state = self._state_row(connection, owner_id, model_id)
            assert state is not None
            unlocked = connection.execute(
                "SELECT unlocked, basis_revision_id FROM brain_module_unlocks "
                "WHERE owner_id = ? AND model_id = ? AND module_name = 'module_one'",
                (owner_id, model_id),
            ).fetchone()
            # Core editing is append-only: the completed active self still
            # supports ordinary memory while a replacement is being considered.
            # Keep the existing live gate, and extend it only to normal editing
            # against the exact same, still-active completion basis. Initial or
            # recovery drafts must never acquire permission from their stage name.
            established_edit = False
            if (
                state["flow_kind"] == "edit"
                and state["stage"] in {
                    "edit_consent", "edit_body_draft", "candidate_wait", "candidate_review"
                }
                and state["injection_policy"] == "normal"
                and unlocked is not None
                and state["base_revision_id"] is not None
                and state["base_revision_id"] == unlocked["basis_revision_id"]
            ):
                established_edit = connection.execute(
                    "SELECT 1 FROM self_models AS model "
                    "JOIN self_model_revisions AS revision "
                    "ON revision.revision_id = model.active_revision_id "
                    "AND revision.model_id = model.model_id "
                    "WHERE model.owner_id = ? AND model.model_id = ? "
                    "AND model.active_revision_id = ?",
                    (owner_id, model_id, state["base_revision_id"]),
                ).fetchone() is not None
            if (
                (state["stage"] != "live" and not established_edit)
                or state["module_one_status"] != "complete"
                or unlocked is None
                or not unlocked["unlocked"]
            ):
                return {
                    **self._deny(state, "module_one_required"),
                    "requested_module": module_name,
                }
            return {
                "decision": "allowed",
                "reason_codes": ["module_one_complete"],
                "requested_module": module_name,
                "basis_revision_id": unlocked["basis_revision_id"],
                "state_changed": False,
                "pointer_changed": False,
            }
