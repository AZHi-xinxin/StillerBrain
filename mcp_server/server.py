#!/usr/bin/env python3
"""Authenticated Streamable HTTP MCP server for Stiller Brain."""

from __future__ import annotations

import hmac
import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import (
    AfterValidator,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
)

from runtime import (
    ALIAS_COMPARISON_PROFILE_VERSION,
    AUTHORING_SCHEMA_VERSIONS,
    MENTION_PARSER_RULE_VERSION,
    REWRITE_ELIGIBLE_ALLOWLIST_VERSION,
    AuthoringRewriteStore,
    EmotionalMemoryStore,
    HallucinationVaultStore,
    InjectionControlStore,
    LearningQuarantineAdapter,
    LearningMemoryStore,
    ModuleOneOnboardingStore,
    PlanningMemoryStore,
    SelfModelStore,
    ToolGuidanceStore,
)

from .authoring_service import AuthoringRewriteAccessService
from .emotional_service import EmotionalMemoryAccessService
from .learning_service import LearningMemoryAccessService
from .injection_control_service import InjectionControlAccessService
from .hallucination_service import HallucinationVaultAccessService
from .planning_service import PlanningMemoryAccessService
from .daily_memory_service import DailyMemoryAccessService
from .daily_revision_service import DailyRevisionAccessService
from .execution_guard import install_execution_guard
from .usage_guide import usage_guide
from runtime.execution_binding import ExecutionStore
from .public_contract import (
    BrainManualModule,
    BrainOpenView,
    PUBLIC_CONTRACT_VERSION,
    SubmitIntent,
    install_public_tool_input_contracts,
)
from .service import SelfModelAccessService
from .tool_guidance_service import ToolGuidanceAccessService


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _csv_env(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


MCP_TOKEN = _required_env("STBRAIN_MCP_TOKEN")
WAKE_SECRET = _required_env("STBRAIN_WAKE_SECRET")
MODEL_ID = _required_env("STBRAIN_MODEL_ID")
OWNER_ID = _required_env("STBRAIN_OWNER_ID")
DATABASE = Path(_required_env("STBRAIN_DB_PATH"))
MCP_HOST = os.environ.get("STBRAIN_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("STBRAIN_MCP_PORT", "8794"))
MCP_ISSUER_URL = os.environ.get(
    "STBRAIN_MCP_ISSUER_URL", f"http://127.0.0.1:{MCP_PORT}"
)
MCP_RESOURCE_URL = os.environ.get(
    "STBRAIN_MCP_RESOURCE_URL", f"http://127.0.0.1:{MCP_PORT}/mcp"
)
ALLOWED_HOSTS = _csv_env(
    "STBRAIN_MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*"
)
ALLOWED_ORIGINS = _csv_env("STBRAIN_MCP_ALLOWED_ORIGINS")
WAKE_TTL_SECONDS = int(os.environ.get("STBRAIN_WAKE_TTL_SECONDS", "1800"))
EDIT_CHALLENGE_TTL_SECONDS = int(
    os.environ.get("STBRAIN_EDIT_CHALLENGE_TTL_SECONDS", "600")
)
DIRECT_CLIENT_PRINCIPAL = os.environ.get(
    "STBRAIN_DIRECT_CLIENT_PRINCIPAL", "official-deepseek-direct"
).strip()

if len(MCP_TOKEN) < 32:
    raise RuntimeError("STBRAIN_MCP_TOKEN must contain at least 32 characters")
if len(WAKE_SECRET) < 32:
    raise RuntimeError("STBRAIN_WAKE_SECRET must contain at least 32 characters")
if not 1 <= MCP_PORT <= 65535:
    raise RuntimeError("STBRAIN_MCP_PORT must be a valid TCP port")
if WAKE_TTL_SECONDS < 60:
    raise RuntimeError("STBRAIN_WAKE_TTL_SECONDS must be at least 60")
if EDIT_CHALLENGE_TTL_SECONDS < 60:
    raise RuntimeError("STBRAIN_EDIT_CHALLENGE_TTL_SECONDS must be at least 60")
if not DIRECT_CLIENT_PRINCIPAL:
    raise RuntimeError("STBRAIN_DIRECT_CLIENT_PRINCIPAL must not be empty")


class StaticBearerVerifier:
    """Validate one owner-scoped credential without logging its value."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, MCP_TOKEN):
            return None
        return AccessToken(
            token=token,
            client_id="stiller-brain-owner",
            scopes=["stbrain:self-model"],
            subject=MODEL_ID,
        )


def _require_json_true(value: bool) -> bool:
    if value is not True:
        raise ValueError("ai_confirmation must be JSON true")
    return value


JsonTrue = Annotated[StrictBool, AfterValidator(_require_json_true)]
NonEmptyJsonString = Annotated[StrictStr, StringConstraints(min_length=1)]
DirectGrantReference = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=256),
    Field(
        description=(
            "Opaque, short-lived, one-use reference returned by the human-only "
            "direct-grant endpoint. It is not an MCP, host, or human bearer token."
        )
    ),
]
LearningRecallQuery = Annotated[
    StrictStr,
    Field(
        description=(
            "view='search' 时是语义搜索文本并与 target_ref 二选一；view='inventory' 时"
            "留空可列出全部活动卡，也可只给一个简短主题（如“海龟汤”）列出该主题全部卡。"
            "不要把‘学习脑都有什么’整句当成普通语义搜索。若自动浮现已给出 versioned "
            "item_ref，应让本字段为空并使用 target_ref；语义零命中不能否定该引用。"
        )
    ),
]
LearningRecallView = Annotated[
    Literal["search", "inventory"],
    Field(
        description=(
            "search=相关性查询或精确 ref；inventory=浏览完整活动目录/某主题全部卡，"
            "用于回答‘都有什么、多少张、为综合收集来源’。"
        )
    ),
]
LearningTargetRef = Annotated[
    StrictStr,
    Field(
        description=(
            "精确版本引用。把自动学习 envelope 的 versioned item_ref 原样复制到这里；"
            "不要把 pending 候选的 @0 目标当作已落地学习卡。"
        )
    ),
]
JsonRowVersion = Annotated[StrictInt, Field(ge=0)]
JsonQueryLimit = Annotated[StrictInt, Field(ge=1, le=100)]
JsonEmotionQueryLimit = Annotated[StrictInt, Field(ge=1, le=50)]
JsonMemoryVersion = Annotated[StrictInt, Field(ge=1)]
JsonPercent = Annotated[StrictInt, Field(ge=0, le=100)]
JsonLearningQueryLimit = Annotated[StrictInt, Field(ge=1, le=50)]
JsonLearningInventoryOffset = Annotated[StrictInt, Field(ge=0, le=100000)]
InjectionScope = Literal[
    "global",
    "self_model",
    "self_governance",
    "emotional_memory",
    "learning_memory",
    "tool_guidance",
    "planning_memory",
    "hallucination_vault",
]
InjectionMode = Literal["enabled", "paused", "hard_off", "status_only"]
InjectionControlAction = Literal[
    "emergency_off",
    "propose_mode",
    "propose_rollback",
    "withdraw",
    "activate",
]
PlanningKind = Literal["vision", "goal", "milestone", "task", "commitment"]
PlanningTrack = Literal["internal", "relational"]
PlanningPresenceMode = Literal["relevant", "session_start", "persistent"]
PlanningRevisionIntent = Literal[
    "revise", "abandon", "archive", "revive", "rollback"
]
PlanningEventType = Literal[
    "progress", "complete", "pause", "resume", "reopen", "defer_review"
]
PlanningReviewDecision = Literal["accept", "reject"]
VaultHoldIntent = Literal["record", "update_warning"]
VaultTransferIntent = Literal["preview", "commit", "propose_restore"]
VaultUncertaintyStatus = Literal["ai_isolated", "still_uncertain"]
VaultRestoreAction = Literal["activate", "reject", "withdraw"]

_FIRST_PERSON_MEMORY_PATTERN = (
    r"^\s*(?:我|I(?:\s|['’])|My(?:\s|$)|"
    r"[^。！？\r\n]{0,80}(?:(?:对|跟|告诉|让|给|问|向|与|和)我|[，,;；]\s*我)|"
    r"[^.!?\r\n]{0,120}[,;:]\s*I(?:\s|['’]))"
)
MemoryOriginal = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=2000),
    Field(
        description=(
            "AI 自己选择叙述人称的记忆原文；第一、第二或第三人称均可。"
            "可选 referent_bindings 用于标注指代，但缺失或含糊不会拒绝写入。"
        )
    ),
]
MemorySummary = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=200),
    Field(
        description="不超过 200 字、由 AI 自己选择叙述人称的摘要。"
    ),
]
FirstPersonPinText = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=1000, pattern=_FIRST_PERSON_MEMORY_PATTERN),
    Field(description="可能进入活动注入的 AI 第一人称锚点显示文本。"),
]
AuditReason = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=2000),
    Field(
        description=(
            "本次写入或修改的审计理由。此字段不会自动注入，必须说明原因，"
            "但不要求以‘我’开头。"
        )
    ),
]

MemoryType = Literal[
    "shared_event",
    "feeling",
    "relationship",
    "meaningful_dialogue",
    "emotional_reflection",
]
EmotionLabel = Literal[
    "joy",
    "affection",
    "trust",
    "gratitude",
    "longing",
    "relief",
    "pride",
    "hope",
    "sadness",
    "fear",
    "anger",
    "hurt",
    "disappointment",
    "shame",
    "guilt",
    "loneliness",
    "tenderness",
    "concern",
    "surprise",
    "calm",
    "mixed",
    "other",
]
SecondaryEmotionList = Annotated[list[EmotionLabel], Field(max_length=1)]
Sensitivity = Literal["public", "internal", "private", "intimate", "restricted"]
ContextPolicy = Literal["normal", "neutral_hint", "ask_first", "never_auto"]
MemoryOrigin = Annotated[
    Literal["firsthand", "reported", "inferred"],
    Field(
        description=(
            "来源：firsthand 仅用于当前 AI 实例直接经历的本轮事件；"
            "人类讲述、OB/旧档案或过去实例的材料用 reported；"
            "对动机、含义或未观察事实的解释用 inferred。混合内容应拆开保存。"
        )
    ),
]
RecallMode = Literal["normal", "summary_only", "never"]
DefaultDecision = Literal["background_reference", "defer", "ask_first"]
ExplicitRequestOverride = Literal["never", "ask_first", "allow_after_confirmation"]
Disclosure = Literal["summary_only", "bounded_excerpt", "full_if_explicit"]
MemoryLifecycle = Literal["active", "archived", "quarantined"]
PinAction = Literal["request", "confirm", "lower", "remove"]
PinKind = Literal["identity_anchor", "safety_boundary", "human_standing_rule"]
LearningKind = Literal["concept", "fact", "procedure", "skill", "lesson", "strategy"]
LearningSourceBasis = Annotated[
    Literal["observed", "reported", "inferred"],
    Field(
        description=(
            "这条知识从哪里来，只描述来源，不表示它是否与别的知识相反。"
            "observed=本 AI 直接观察；reported=人类或文档陈述；"
            "inferred=AI 自己的推断。"
        )
    ),
]
LearningClaimReviewStatus = Annotated[
    Literal["ordinary", "challenged", "rejected"],
    Field(
        description=(
            "断言审查状态，与来源和相反关系分开。ordinary=没有独立的实质性质疑；"
            "challenged/rejected 仅在能同时说明谁质疑、质疑哪项断言、依据及证据引用时使用，"
            "并会强制隔离。仅有另一条相反说法仍应使用 ordinary。"
        )
    ),
]
TimeSensitivity = Literal["timeless", "stable", "volatile"]
LearningLifecycle = Literal["active", "pending_review", "archived", "quarantined", "superseded"]
LearningChangeClass = Literal[
    "typo", "metadata", "source_addition", "semantic_change", "generalization", "contradiction", "status_promotion"
]
LearningRevisionAction = Literal["change", "rollback"]
LearningReviewAction = Literal["accept", "reject"]
ToolCapabilityClass = Literal["information_query", "real_world_action"]
ToolRiskLevel = Literal["low", "medium", "high", "critical"]
ToolConfirmationPolicy = Literal["none", "contextual", "explicit_each_time"]
ToolRecallMode = Literal["normal", "downweighted", "never_auto"]
ToolChainRole = Literal["standalone", "entry", "middle", "terminal"]
ToolSourceType = Literal["ai_firsthand", "external_document", "human_reported", "ai_inferred"]
ToolLifecycle = Literal["active", "retired"]
GovernanceAction = Literal[
    "propose_set", "propose_clear", "propose_rollback", "withdraw", "activate"
]
GovernanceScope = Literal[
    "global", "self_revision", "emotional_memory", "learning_memory", "tool_use"
]
GovernanceTriggerMode = Literal["manual_only", "scene_relevant"]
GovernanceText = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=2000,
        pattern=r"^\s*(?:我|I(?:\s|['’])|My(?:\s|$))",
    ),
    Field(description="由当前 AI 自己撰写的第一人称自我治理正文。"),
]
GovernanceSceneTag = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=80),
]
GovernanceSceneTags = Annotated[list[GovernanceSceneTag], Field(max_length=32)]
GovernanceExpectedActiveRevision = Annotated[
    NonEmptyJsonString | None,
    Field(
        description=(
            "仅在 stbrain_open 的治理 current_action_contract.arguments 中出现时原样复制。"
            "首次激活的精确契约会省略此可选字段（等价于 JSON null）；不得自行补成"
            " false、空字符串或字符串 'null'。"
        )
    ),
]
GovernanceReason = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=2000),
    Field(
        description=(
            "候选/撤回时的审计理由；activate 时也允许作为 AI 在较晚真实唤醒中的"
            "可选确认理由。不得把理由放进 text 或其他字段。"
        )
    ),
]


transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=ALLOWED_HOSTS,
    allowed_origins=ALLOWED_ORIGINS,
)

auth_settings = AuthSettings(
    issuer_url=MCP_ISSUER_URL,
    resource_server_url=MCP_RESOURCE_URL,
    required_scopes=["stbrain:self-model"],
)

emotional_store = EmotionalMemoryStore(DATABASE)
LEARNING_IDEA_DATABASE = Path(
    os.environ.get(
        "STBRAIN_LEARNING_IDEA_DB_PATH",
        str(DATABASE.with_name(f"{DATABASE.stem}-learning-ideas{DATABASE.suffix or '.sqlite3'}")),
    )
)
learning_store = LearningMemoryStore(DATABASE, idea_database=LEARNING_IDEA_DATABASE)
tool_store = ToolGuidanceStore(DATABASE)
injection_control_store = InjectionControlStore(DATABASE)
planning_store = PlanningMemoryStore(DATABASE)
HALLUCINATION_VAULT_DATABASE = Path(
    os.environ.get(
        "STBRAIN_HALLUCINATION_VAULT_DB_PATH",
        str(DATABASE.with_name(f"{DATABASE.stem}-hallucination-vault.sqlite3")),
    )
)
learning_quarantine_adapter = LearningQuarantineAdapter(DATABASE)
hallucination_store = HallucinationVaultStore(
    HALLUCINATION_VAULT_DATABASE,
    source_adapters={"learning": learning_quarantine_adapter},
)
authoring_store = AuthoringRewriteStore(DATABASE, receipt_secret=WAKE_SECRET)

onboarding = ModuleOneOnboardingStore(
    DATABASE,
    capability_secret=WAKE_SECRET,
    wake_ttl_seconds=WAKE_TTL_SECONDS,
    edit_challenge_ttl_seconds=EDIT_CHALLENGE_TTL_SECONDS,
    emotional_store=emotional_store,
    learning_store=learning_store,
    tool_store=tool_store,
    injection_control_store=injection_control_store,
    planning_store=planning_store,
    hallucination_vault=hallucination_store,
)

emotional_service = EmotionalMemoryAccessService(
    emotional_store,
    onboarding=onboarding,
    model_id=MODEL_ID,
    owner_id=OWNER_ID,
)
learning_service = LearningMemoryAccessService(
    learning_store,
    onboarding=onboarding,
    model_id=MODEL_ID,
    owner_id=OWNER_ID,
)


def _catalog_provider(binding: Any | None = None) -> dict[str, Any] | None:
    if isinstance(binding, dict) and isinstance(binding.get("advertised_tools"), dict):
        return binding["advertised_tools"]
    return onboarding.current_advertised_tools(owner_id=OWNER_ID, model_id=MODEL_ID)


tool_guidance_service = ToolGuidanceAccessService(
    tool_store,
    onboarding=onboarding,
    model_id=MODEL_ID,
    owner_id=OWNER_ID,
    catalog_provider=_catalog_provider,
)
authoring_rewrite_service = AuthoringRewriteAccessService(
    authoring_store,
    onboarding=onboarding,
    model_id=MODEL_ID,
    owner_id=OWNER_ID,
)
injection_control_service = InjectionControlAccessService(
    injection_control_store,
    onboarding=onboarding,
    model_id=MODEL_ID,
    owner_id=OWNER_ID,
)
planning_service = PlanningMemoryAccessService(
    planning_store,
    onboarding=onboarding,
    model_id=MODEL_ID,
    owner_id=OWNER_ID,
)
daily_service = DailyMemoryAccessService(
    onboarding, emotional_service, learning_service, planning_service, OWNER_ID, MODEL_ID,
)
daily_revision_service = DailyRevisionAccessService(
    onboarding, emotional_service, learning_service, planning_service, OWNER_ID, MODEL_ID,
)
hallucination_service = HallucinationVaultAccessService(
    hallucination_store,
    onboarding=onboarding,
    model_id=MODEL_ID,
    owner_id=OWNER_ID,
)

service = SelfModelAccessService(
    SelfModelStore(DATABASE),
    model_id=MODEL_ID,
    owner_id=OWNER_ID,
    onboarding=onboarding,
    emotional=emotional_service,
    learning=learning_service,
    tool_guidance=tool_guidance_service,
    authoring_rewrite=authoring_rewrite_service,
    injection_control=injection_control_service,
    planning=planning_service,
    hallucination_vault=hallucination_service,
    direct_client_principal=DIRECT_CLIENT_PRINCIPAL,
)

mcp = FastMCP(
    "StillerBrainSelfModel",
    instructions=(
        f"{PUBLIC_CONTRACT_VERSION}: StillerBrain is optional memory for this AI. "
        "For ordinary emotional, learning or planning memories, call remember_memory(module, content) once. "
        "No preliminary open, manual version, candidate or later-wake review is needed for that route. "
        "Use stbrain_help for short static instructions, and recall_* tools to read saved memories. "
        "A real stored result means saved; a candidate, a rejected call or saying 'I will save' does not. "
        "The gateway supplies the reserved execution_ref; do not invent or reuse it. "
        "Non-gateway direct writes require an independently human-authorized context. "
        "Core self-modification and dedicated advanced changes retain their separate review rules. "
        "For those, stbrain_open(view='manual', module=one_module_name) provides the exact instructions "
        "and full review material; use that wake's real write_context_ref and current version. "
        "The default open summary is not proof that a candidate was reviewed. "
        "Never invent wake evidence, adoption statements, credentials or tool results. "
        "Original memory text stays immutable; storing a plan or tool guidance grants no external action permission. "
        "Automatic recall is bounded and excludes pending/isolated content. The isolated vault is off by default."
    ),
    token_verifier=StaticBearerVerifier(),
    auth=auth_settings,
    host=MCP_HOST,
    port=MCP_PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=transport_security,
)


@mcp.tool()
async def stbrain_health() -> dict[str, Any]:
    """Check Stiller Brain health without returning private self-model content."""
    return service.health()


@mcp.tool()
async def stbrain_help() -> dict[str, Any]:
    """Read short usage instructions without opening a write context or private memory."""
    return usage_guide()


@mcp.tool()
async def remember_memory(
    module: Literal["emotional_memory", "learning_memory", "planning_memory"],
    content: Annotated[StrictStr, StringConstraints(min_length=1, max_length=2000)],
    title: str | None = None, summary: str | None = None, kind: str | None = None,
    track: str = "internal", keywords: list[str] | None = None,
    importance: StrictInt = 50, emotion: str = "other", source_basis: str = "reported",
    confidence: StrictInt = 50, reason: str | None = None,
    write_context_ref: str | None = None, parent_ref: str | None = None,
    due_at: str | None = None, timezone: str = "UTC",
) -> dict[str, Any]:
    """Save one ordinary memory with one call. Only module and content are required.

    No preliminary stbrain_open, manual version, candidate or later-wake review.
    content is preserved as supplied; optional title/summary organize it. For emotional
    memory title is used as summary only when summary is omitted. kind is optional;
    defaults: meaningful_dialogue / fact / task respectively. Unknown facts default
    to reported, confidence=50; storing is not independent verification. A planning
    record becomes active, but grants no external execution permission. Core self
    modification still uses its separate review tools. Do not supply write_context_ref
    on the gateway path; it is only for an already human-authorized direct context.
    Only a real stored response means success; do not claim a call you did not make.
    """
    return daily_service.remember(
        module=module, content=content, title=title, summary=summary, kind=kind,
        track=track, keywords=keywords, importance=importance, emotion=emotion,
        source_basis=source_basis, confidence=confidence, reason=reason,
        write_context_ref=write_context_ref, parent_ref=parent_ref,
        due_at=due_at, timezone=timezone,
    )


@mcp.tool()
async def revise_memory(
    target_ref: Annotated[StrictStr, StringConstraints(min_length=1, max_length=100)],
    changes: dict[str, Any],
    reason: str | None = None,
    write_context_ref: str | None = None,
) -> dict[str, Any]:
    """Revise ordinary summary/search metadata once, preserving the exact history.

    Supply the versioned target_ref you actually read and changed fields only.
    Common fields: summary, keywords, importance. Learning/planning also title;
    emotional/learning also entities; learning also domain. Original event/body,
    source confidence, disclosure rules and core self are not changed here.
    No preliminary open or manual module version on the gateway path. A stale
    target is rejected without overwrite: reread it before deciding to revise.
    write_context_ref is only for an already human-authorized direct context.
    """
    return daily_revision_service.revise(target_ref, changes, reason, write_context_ref)


@mcp.tool()
async def advance_plan(
    target_ref: Annotated[StrictStr, StringConstraints(min_length=1, max_length=100)],
    expected_event_seq: Annotated[StrictInt, Field(ge=0)],
    event_type: Literal["progress", "complete", "pause", "resume", "reopen"],
    note: Annotated[StrictStr, StringConstraints(min_length=1, max_length=2000)],
    evidence: list[dict[str, Any]] | None = None,
    write_context_ref: str | None = None,
) -> dict[str, Any]:
    """Append a plan update once, without executing the plan or inventing evidence.

    Use target_ref and event_seq from the plan you read. expected_event_seq is
    that event_seq; it protects updates even when the plan content version has
    not changed. progress/complete/reopen need genuine evidence (source_kind,
    source_ref, evidence_summary, provenance); pause/resume do not. note is your
    update reason, not a claim of independent verification. No preliminary open
    or module version is needed on the gateway path. Conflicts require rereading,
    never automatic overwrite. Direct use needs an authorized write_context_ref.
    """
    return daily_revision_service.advance(
        target_ref=target_ref, expected_event_seq=expected_event_seq,
        event_type=event_type, note=note, evidence=evidence,
        write_context_ref=write_context_ref,
    )


@mcp.tool()
async def stbrain_open(
    view: BrainOpenView = "summary",
    module: BrainManualModule = "self_revision",
    page: Annotated[StrictInt, Field(ge=0)] = 0,
    expected_material_hash: Annotated[StrictStr, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None,
) -> dict[str, Any]:
    """Open ST's core-edit context or review material; not a whole-memory recall.

    Default summary returns the real root write_context_ref and module versions,
    not documentation placeholders. Reuse them only in this real user/tool round.
    For instructions or full pending review material, use view='manual' with one
    module. For a self-model candidate, view='review' returns bounded pages directly
    in MCP: start with page=0 and follow next_arguments until fully_presented=true.
    Later pages require the returned expected_material_hash. A missing page or a
    different wake/version never counts as full review. No shell, file or workspace
    is required. Opens reuse this wake, not create one. Core changes still need
    separate real wakes to propose, review and activate. Ordinary memories use
    remember_memory and recall_* without this preliminary open.
    """
    return service.open_brain(view=view, module=module, page=page,
                              expected_material_hash=expected_material_hash)


@mcp.tool()
async def stbrain_open_direct(
    grant_ref: DirectGrantReference,
) -> dict[str, Any]:
    """Consume one human-issued grant and open a non-injected direct write context.

    The grant is short-lived and one-use. It is bound server-side to this owner,
    model, official direct client principal, scopes, and human issuance event.
    This tool cannot turn an ordinary MCP token, an old grant, or a tool
    continuation into a new wake. It never creates automatic-injection evidence.
    """
    return service.open_brain_direct(grant_ref=grant_ref)


@mcp.tool()
async def submit_self_model_candidate(
    intent: SubmitIntent,
    write_context_ref: NonEmptyJsonString,
    expected_row_version: JsonRowVersion,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare, save, revise, recover, or review a candidate through one safe facade.

    The host must issue the wake and confirm injection of the exact prepared context before
    this call. First call stbrain_open and copy its write_context_ref and row_version.
    For save_calm_prompt, payload is exactly {"text": <AI-authored string>}. For submit
    and revise, payload is exactly {"content": <five-key self model>, "reason": <string>}.
    The reason key is never named ai_reason. For confirm_edit, payload must contain
    challenge_id and ai_confirmation=true; it must never contain a challenge response or
    wake capability. Unknown payload keys are invalid.
    Candidate review acceptance is recorded but never activates in the same real wake.
    """
    return service.submit_self_model_candidate(
        intent=intent,
        write_context_ref=write_context_ref,
        expected_row_version=expected_row_version,
        payload=payload,
    )


@mcp.tool()
async def activate_self_model_candidate(
    candidate_id: NonEmptyJsonString,
    write_context_ref: NonEmptyJsonString,
    expected_row_version: JsonRowVersion,
    expected_active_revision: NonEmptyJsonString | None,
    ai_confirmation: JsonTrue,
) -> dict[str, Any]:
    """Activate an accepted candidate only in a later independently injected real wake.

    Copy expected_active_revision from the current candidate returned by stbrain_open:
    it is null for the first activation and the base revision ID for an edited candidate.
    ai_confirmation must be the JSON boolean true, never a number or string.
    """
    return service.activate_self_model_candidate(
        candidate_id=candidate_id,
        write_context_ref=write_context_ref,
        expected_row_version=expected_row_version,
        expected_active_revision=expected_active_revision,
        ai_confirmation=ai_confirmation,
    )


@mcp.tool()
async def query_self_model(
    view: Literal["status", "active", "search", "edit_basis"] = "status",
    query: StrictStr = "",
    scope: Literal["active", "candidates", "revisions", "events", "all"] = "all",
    limit: JsonQueryLimit = 20,
    include_content: StrictBool = False,
    facet_names: list[StrictStr] | None = None,
    include_anchor_references: StrictBool = False,
) -> dict[str, Any]:
    """Read ST's current self-model status, active identity or owner-scoped archive.

    Active is the existing approved version, also readable while considering an
    edit; it is never a pending candidate and reading it is not automatic-injection
    or candidate-review evidence. For composing an edit, view='edit_basis' returns
    the complete five-key active.content, including existing facets and references;
    preserve fields you did not change. Use stbrain_open(view='review') for pending full
    review pages without a workspace. Archive content is omitted unless
    include_content is explicitly true. For personal memories use recall_* instead.
    """
    return service.query_self_model(
        view=view,
        query=query,
        scope=scope,
        limit=limit,
        include_content=include_content,
        facet_names=facet_names,
        include_anchor_references=include_anchor_references,
    )


@mcp.tool()
async def preview_person_reference_rewrite(
    write_context_ref: NonEmptyJsonString,
    expected_authoring_version: JsonRowVersion,
    module: Literal[
        "emotional_memory_module_two",
        "learning_memory_module_three",
        "tool_guidance_module",
    ],
    draft_version: JsonRowVersion,
    draft_fields: dict[str, StrictStr],
    referent_bindings: list[dict[str, Any]],
    rewrite_targets: list[dict[str, Any]],
    conversation_mode: Literal["one_to_one", "group", "unknown"],
    authenticated_participant_entity_ids: list[NonEmptyJsonString],
    alias_collision_scope: NonEmptyJsonString,
    alias_collision_scope_version: JsonMemoryVersion,
    protected_spans: list[dict[str, Any]],
    module_schema_version: NonEmptyJsonString,
    rewrite_eligible_allowlist_version: Literal[
        REWRITE_ELIGIBLE_ALLOWLIST_VERSION
    ] = REWRITE_ELIGIBLE_ALLOWLIST_VERSION,
    mention_parser_rule_version: Literal[
        MENTION_PARSER_RULE_VERSION
    ] = MENTION_PARSER_RULE_VERSION,
    alias_comparison_profile_version: Literal[
        ALIAS_COMPARISON_PROFILE_VERSION
    ] = ALIAS_COMPARISON_PROFILE_VERSION,
) -> dict[str, Any]:
    """Preview optional literal mention patches for this draft only.

    The tool never guesses an entity, changes grammar, saves a memory, or makes
    a preview live.  No applicable safe patch returns continue_original_path so
    the ordinary module write remains available without another confirmation.
    """
    return authoring_rewrite_service.preview(
        write_context_ref=write_context_ref,
        expected_authoring_version=expected_authoring_version,
        module=module,
        draft_version=draft_version,
        draft_fields=draft_fields,
        referent_bindings=referent_bindings,
        rewrite_targets=rewrite_targets,
        conversation_mode=conversation_mode,
        authenticated_participant_entity_ids=authenticated_participant_entity_ids,
        alias_collision_scope=alias_collision_scope,
        alias_collision_scope_version=alias_collision_scope_version,
        protected_spans=protected_spans,
        module_schema_version=module_schema_version,
        rewrite_eligible_allowlist_version=rewrite_eligible_allowlist_version,
        mention_parser_rule_version=mention_parser_rule_version,
        alias_comparison_profile_version=alias_comparison_profile_version,
    )


@mcp.tool()
async def confirm_person_reference_rewrite(
    write_context_ref: NonEmptyJsonString,
    expected_authoring_version: JsonRowVersion,
    module: Literal[
        "emotional_memory_module_two",
        "learning_memory_module_three",
        "tool_guidance_module",
    ],
    preview_id: NonEmptyJsonString,
    expected_source_draft_hash: NonEmptyJsonString,
    expected_suggestion_hash: NonEmptyJsonString,
    expected_validation_context_hash: NonEmptyJsonString,
    final_fields: dict[str, StrictStr],
    final_fields_hash: NonEmptyJsonString,
    conversation_mode: Literal["one_to_one", "group", "unknown"],
    authenticated_participant_entity_ids: list[NonEmptyJsonString],
    alias_collision_scope: NonEmptyJsonString,
    alias_collision_scope_version: JsonMemoryVersion,
    protected_spans: list[dict[str, Any]],
    module_schema_version: NonEmptyJsonString,
    ai_confirmation: JsonTrue,
    rewrite_eligible_allowlist_version: Literal[
        REWRITE_ELIGIBLE_ALLOWLIST_VERSION
    ] = REWRITE_ELIGIBLE_ALLOWLIST_VERSION,
    mention_parser_rule_version: Literal[
        MENTION_PARSER_RULE_VERSION
    ] = MENTION_PARSER_RULE_VERSION,
    alias_comparison_profile_version: Literal[
        ALIAS_COMPARISON_PROFILE_VERSION
    ] = ALIAS_COMPARISON_PROFILE_VERSION,
) -> dict[str, Any]:
    """Confirm the exact preview and issue one opaque single-use write receipt."""
    return authoring_rewrite_service.confirm(
        write_context_ref=write_context_ref,
        expected_authoring_version=expected_authoring_version,
        module=module,
        preview_id=preview_id,
        expected_source_draft_hash=expected_source_draft_hash,
        expected_suggestion_hash=expected_suggestion_hash,
        expected_validation_context_hash=expected_validation_context_hash,
        final_fields=final_fields,
        final_fields_hash=final_fields_hash,
        conversation_mode=conversation_mode,
        authenticated_participant_entity_ids=authenticated_participant_entity_ids,
        alias_collision_scope=alias_collision_scope,
        alias_collision_scope_version=alias_collision_scope_version,
        protected_spans=protected_spans,
        module_schema_version=module_schema_version,
        ai_confirmation=ai_confirmation,
        rewrite_eligible_allowlist_version=rewrite_eligible_allowlist_version,
        mention_parser_rule_version=mention_parser_rule_version,
        alias_comparison_profile_version=alias_comparison_profile_version,
    )


@mcp.tool()
async def remember_emotional_memory(
    write_context_ref: NonEmptyJsonString,
    expected_emotion_version: JsonRowVersion,
    memory_type: MemoryType,
    original_text: MemoryOriginal,
    summary: MemorySummary,
    primary_emotion: EmotionLabel,
    reason: AuditReason,
    secondary_emotions: SecondaryEmotionList | None = None,
    importance: JsonPercent = 50,
    sensitivity: Sensitivity = "private",
    context_policy: ContextPolicy = "normal",
    origin: MemoryOrigin = "firsthand",
    confidence: JsonPercent = 100,
    keywords: list[StrictStr] | None = None,
    entities: list[StrictStr] | None = None,
    allow_contexts: list[StrictStr] | None = None,
    deny_contexts: list[StrictStr] | None = None,
    default_decision: DefaultDecision = "background_reference",
    explicit_request_override: ExplicitRequestOverride = "allow_after_confirmation",
    disclosure: Disclosure = "bounded_excerpt",
    associations: list[dict[str, Any]] | None = None,
    referent_bindings: list[dict[str, Any]] | None = None,
    source_timestamp: StrictStr | None = None,
    rewrite_receipt: StrictStr | None = None,
) -> dict[str, Any]:
    """Advanced emotional-memory creation with explicit policies and context.

    For ordinary new memories prefer remember_memory(module='emotional_memory',
    content=...). That route saves once without preliminary open or manual versions.

    First call stbrain_open and copy write_context_ref plus
    emotional_memory.row_version. original_text is immutable after this call;
    later changes use revise_emotional_memory. original_text and summary are
    AI-authored and may use the grammatical person the AI chooses. Optional
    referent_bindings help disambiguate entities but never gate eligibility.
    Do not send recall_mode to this creation tool: a new memory starts in the
    server-managed normal mode, and any later mode change uses
    revise_emotional_memory.
    Use origin=firsthand
    only for this instance's direct current experience; human/OB/legacy material
    is reported, and interpretation is inferred. secondary_emotions has at most
    one item from the published enum. associations, when present, is a list of
    {target_memory_id, edge_type, weight, occurred_at?} objects.
    """
    return emotional_service.remember(
        write_context_ref=write_context_ref,
        expected_emotion_version=expected_emotion_version,
        memory_type=memory_type,
        original_text=original_text,
        summary=summary,
        primary_emotion=primary_emotion,
        secondary_emotions=secondary_emotions,
        importance=importance,
        sensitivity=sensitivity,
        context_policy=context_policy,
        origin=origin,
        confidence=confidence,
        keywords=keywords,
        entities=entities,
        allow_contexts=allow_contexts,
        deny_contexts=deny_contexts,
        default_decision=default_decision,
        explicit_request_override=explicit_request_override,
        disclosure=disclosure,
        associations=associations,
        referent_bindings=referent_bindings,
        source_timestamp=source_timestamp,
        reason=reason,
        rewrite_receipt=rewrite_receipt,
    )


@mcp.tool()
async def recall_emotional_memory(
    query: StrictStr = "",
    memory_id: StrictStr | None = None,
    limit: JsonEmotionQueryLimit = 10,
    include_archived: StrictBool = False,
    include_originals: StrictBool = True,
    explicit_request: StrictBool = False,
    include_sensitive_originals: StrictBool = False,
    ai_confirmation: StrictBool = False,
    safety_emergency: StrictBool = False,
) -> dict[str, Any]:
    """Recall personal experiences and relationships saved in ST (StillerBrain).

    After a restart, compression or other wake, use this when you choose to look
    up ST relationship continuity by query or exact memory_id. It is not another
    service's breath and not a mandatory ritual before every reply. It does not
    read the whole brain or open a write context; zero matches do not mean an empty
    brain. Learning, plans and tool experience have their own recall_* tools.

    Supply either a non-empty query or memory_id. Intimate/restricted originals
    remain withheld unless explicit_request, include_sensitive_originals, and
    ai_confirmation are all the JSON boolean true and the memory policy permits
    disclosure; such reads are audited. safety_emergency never grants extra
    access: it clamps the response to at most three summary-only results.
    """
    return emotional_service.recall(
        query=query,
        memory_id=memory_id,
        limit=limit,
        include_archived=include_archived,
        include_originals=include_originals,
        explicit_request=explicit_request,
        include_sensitive_originals=include_sensitive_originals,
        ai_confirmation=ai_confirmation,
        safety_emergency=safety_emergency,
    )


@mcp.tool()
async def revise_emotional_memory(
    write_context_ref: NonEmptyJsonString,
    expected_emotion_version: JsonRowVersion,
    memory_id: NonEmptyJsonString,
    expected_memory_version: JsonMemoryVersion,
    reason: AuditReason,
    summary: MemorySummary | None = None,
    primary_emotion: EmotionLabel | None = None,
    secondary_emotions: SecondaryEmotionList | None = None,
    importance: JsonPercent | None = None,
    sensitivity: Sensitivity | None = None,
    context_policy: ContextPolicy | None = None,
    origin: MemoryOrigin | None = None,
    confidence: JsonPercent | None = None,
    keywords: list[StrictStr] | None = None,
    entities: list[StrictStr] | None = None,
    recall_mode: RecallMode | None = None,
    allow_contexts: list[StrictStr] | None = None,
    deny_contexts: list[StrictStr] | None = None,
    default_decision: DefaultDecision | None = None,
    explicit_request_override: ExplicitRequestOverride | None = None,
    disclosure: Disclosure | None = None,
    lifecycle: MemoryLifecycle | None = None,
    associations: list[dict[str, Any]] | None = None,
    referent_bindings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Append a new interpretation/version; original_text cannot be supplied.

    Put changed fields directly at the tool root. Do not invent payload,
    content, meta, diff, evidence_refs, or candidate wrappers. An association-
    only revision is valid when associations is non-empty.
    """
    optional_changes = {
        "summary": summary,
        "primary_emotion": primary_emotion,
        "secondary_emotions": secondary_emotions,
        "importance": importance,
        "sensitivity": sensitivity,
        "context_policy": context_policy,
        "origin": origin,
        "confidence": confidence,
        "keywords": keywords,
        "entities": entities,
        "recall_mode": recall_mode,
        "allow_contexts": allow_contexts,
        "deny_contexts": deny_contexts,
        "default_decision": default_decision,
        "explicit_request_override": explicit_request_override,
        "disclosure": disclosure,
        "lifecycle": lifecycle,
        "referent_bindings": referent_bindings,
    }
    changes = {key: value for key, value in optional_changes.items() if value is not None}
    return emotional_service.revise(
        write_context_ref=write_context_ref,
        expected_emotion_version=expected_emotion_version,
        memory_id=memory_id,
        expected_memory_version=expected_memory_version,
        reason=reason,
        changes=changes,
        associations=associations,
    )


@mcp.tool()
async def integrate_emotional_memories(
    write_context_ref: NonEmptyJsonString,
    expected_emotion_version: JsonRowVersion,
    source_memory_ids: list[NonEmptyJsonString],
    original_text: MemoryOriginal,
    summary: MemorySummary,
    primary_emotion: EmotionLabel,
    reason: AuditReason,
    secondary_emotions: SecondaryEmotionList | None = None,
    importance: JsonPercent = 50,
    sensitivity: Sensitivity = "private",
    context_policy: ContextPolicy = "normal",
    origin: MemoryOrigin = "firsthand",
    confidence: JsonPercent = 100,
    keywords: list[StrictStr] | None = None,
    entities: list[StrictStr] | None = None,
    referent_bindings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create one timeline aggregate from 2-20 memories and archive its sources.

    Source originals and version histories remain immutable and queryable. The
    aggregate text is AI-authored in the grammatical person the AI chooses; pass fields directly without
    a payload or candidate wrapper.
    """
    return emotional_service.integrate(
        write_context_ref=write_context_ref,
        expected_emotion_version=expected_emotion_version,
        source_memory_ids=source_memory_ids,
        original_text=original_text,
        summary=summary,
        primary_emotion=primary_emotion,
        secondary_emotions=secondary_emotions,
        importance=importance,
        sensitivity=sensitivity,
        context_policy=context_policy,
        origin=origin,
        confidence=confidence,
        keywords=keywords,
        entities=entities,
        referent_bindings=referent_bindings,
        reason=reason,
    )


@mcp.tool()
async def manage_brain_pin(
    write_context_ref: NonEmptyJsonString,
    expected_emotion_version: JsonRowVersion,
    action: PinAction,
    reason: AuditReason,
    pin_id: StrictStr | None = None,
    pin_kind: PinKind | None = None,
    display_text: FirstPersonPinText | None = None,
    source_ref: StrictStr | None = None,
    replace_pin_id: StrictStr | None = None,
    ai_confirmation: StrictBool = False,
) -> dict[str, Any]:
    """Request, later confirm, lower, or remove one bounded brain pin.

    request uses pin_kind, display_text, and source_ref. confirm uses pin_id and
    ai_confirmation=true in a later real wake; replace_pin_id is required only
    when five pins are already active. lower/remove use only pin_id and reason.
    """
    return emotional_service.manage_pin(
        write_context_ref=write_context_ref,
        expected_emotion_version=expected_emotion_version,
        action=action,
        reason=reason,
        pin_id=pin_id,
        pin_kind=pin_kind,
        display_text=display_text,
        source_ref=source_ref,
        replace_pin_id=replace_pin_id,
        ai_confirmation=ai_confirmation,
    )


@mcp.tool()
async def veto_ephemeral_memory(
    write_context_ref: NonEmptyJsonString,
    expected_emotion_version: JsonRowVersion,
    reason: AuditReason,
    ephemeral_id: StrictStr | None = None,
    thread_id: StrictStr | None = None,
) -> dict[str, Any]:
    """Veto and scrub ephemeral text by exactly one ephemeral_id or thread_id."""
    return emotional_service.veto_ephemeral(
        write_context_ref=write_context_ref,
        expected_emotion_version=expected_emotion_version,
        reason=reason,
        ephemeral_id=ephemeral_id,
        thread_id=thread_id,
    )


@mcp.tool()
async def remember_learning_memory(
    write_context_ref: NonEmptyJsonString,
    expected_learning_version: JsonRowVersion,
    kind: LearningKind,
    title: NonEmptyJsonString,
    summary: NonEmptyJsonString,
    current_understanding: NonEmptyJsonString,
    source_basis: LearningSourceBasis,
    confidence: JsonPercent,
    correctness_assessment: NonEmptyJsonString,
    reason: AuditReason,
    claim_review_status: LearningClaimReviewStatus = "ordinary",
    challenged_claim: StrictStr = "",
    challenge_actor: StrictStr = "",
    challenge_basis: StrictStr = "",
    challenge_evidence_refs: list[StrictStr] | None = None,
    steps: list[StrictStr] | None = None,
    application_contexts: list[StrictStr] | None = None,
    scene_tags: list[StrictStr] | None = None,
    preceding_context_summary: StrictStr = "",
    uncertainties: list[StrictStr] | None = None,
    domain: StrictStr = "",
    keywords: list[StrictStr] | None = None,
    entities: list[StrictStr] | None = None,
    time_sensitivity: TimeSensitivity = "stable",
    valid_as_of: StrictStr = "",
    review_after: StrictStr = "",
    importance: JsonPercent = 50,
    sensitivity: Sensitivity = "private",
    context_policy: ContextPolicy = "normal",
    recall_mode: RecallMode = "normal",
    allow_contexts: list[StrictStr] | None = None,
    deny_contexts: list[StrictStr] | None = None,
    default_decision: DefaultDecision = "background_reference",
    explicit_request_override: ExplicitRequestOverride = "allow_after_confirmation",
    disclosure: Disclosure = "bounded_excerpt",
    lifecycle: LearningLifecycle = "active",
    referent_bindings: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    links: list[dict[str, Any]] | None = None,
    rewrite_receipt: StrictStr | None = None,
) -> dict[str, Any]:
    """Advanced learning record with evidence/review metadata; injection stays summary-only.

    For ordinary new memories prefer remember_memory(module='learning_memory',
    content=...). No preliminary open or manual versions are needed on that route.

    For cross-window recall under different wording, add concise scene_tags and
    application_contexts when meaningful.  They may stay empty, but a bare
    keyword only nominates a candidate and does not guarantee automatic recall.

    source_basis says only where the knowledge came from. claim_review_status
    says whether this exact assertion has independent challenge evidence. A
    merely opposite claim remains ordinary; use remember_learning_contrast_pair
    to save both sides atomically. challenged/rejected require all four
    challenge fields and matching evidence source_refs, and are quarantined.
    """
    claim_review: dict[str, Any] = {"status": claim_review_status}
    if claim_review_status != "ordinary" or any((
        challenged_claim, challenge_actor, challenge_basis,
        challenge_evidence_refs,
    )):
        claim_review.update({
            "challenged_claim": challenged_claim,
            "challenge_actor": challenge_actor,
            "challenge_basis": challenge_basis,
            "challenge_evidence_refs": challenge_evidence_refs or [],
        })
    return learning_service.remember(
        write_context_ref=write_context_ref,
        expected_learning_version=expected_learning_version,
        kind=kind,
        title=title,
        summary=summary,
        current_understanding=current_understanding,
        source_basis=source_basis,
        claim_review=claim_review,
        confidence=confidence,
        correctness_assessment=correctness_assessment,
        reason=reason,
        steps=steps,
        application_contexts=application_contexts,
        scene_tags=scene_tags,
        preceding_context_summary=preceding_context_summary,
        uncertainties=uncertainties,
        domain=domain,
        keywords=keywords,
        entities=entities,
        time_sensitivity=time_sensitivity,
        valid_as_of=valid_as_of,
        review_after=review_after,
        importance=importance,
        sensitivity=sensitivity,
        context_policy=context_policy,
        recall_mode=recall_mode,
        allow_contexts=allow_contexts,
        deny_contexts=deny_contexts,
        default_decision=default_decision,
        explicit_request_override=explicit_request_override,
        disclosure=disclosure,
        lifecycle=lifecycle,
        referent_bindings=referent_bindings,
        evidence=evidence,
        links=links,
        rewrite_receipt=rewrite_receipt,
    )


@mcp.tool()
async def remember_learning_contrast_pair(
    write_context_ref: NonEmptyJsonString,
    expected_learning_version: JsonRowVersion,
    kind: LearningKind,
    first_claim: dict[str, Any],
    second_claim: dict[str, Any],
    contrast_basis: dict[str, Any],
    application_contexts: list[NonEmptyJsonString],
    scene_tags: list[NonEmptyJsonString],
    correctness_assessment: NonEmptyJsonString,
    reason: AuditReason,
    first_evidence: list[dict[str, Any]] | None = None,
    second_evidence: list[dict[str, Any]] | None = None,
    domain: StrictStr = "",
    keywords: list[StrictStr] | None = None,
    entities: list[StrictStr] | None = None,
    time_sensitivity: TimeSensitivity = "stable",
    valid_as_of: StrictStr = "",
    review_after: StrictStr = "",
    importance: JsonPercent = 50,
    sensitivity: Sensitivity = "private",
    allow_contexts: list[StrictStr] | None = None,
    deny_contexts: list[StrictStr] | None = None,
    default_decision: DefaultDecision = "background_reference",
    explicit_request_override: ExplicitRequestOverride = "allow_after_confirmation",
    disclosure: Disclosure = "bounded_excerpt",
) -> dict[str, Any]:
    """Atomically save two ordinary opposing claims plus their contrast link.

    Use this when both statements should remain available without deciding that
    either is false. The server fixes both cards to active neutral hints and
    creates the version-bound relation in one transaction. Each claim object
    contains exactly title, summary, current_understanding, source_basis,
    confidence, uncertainties, with optional steps and
    preceding_context_summary. Confidence is at most 60 and uncertainties must
    be non-empty. Any failure leaves neither card behind.
    """
    return learning_service.remember_contrast_pair(
        write_context_ref=write_context_ref,
        expected_learning_version=expected_learning_version,
        first_claim=first_claim,
        second_claim=second_claim,
        contrast_basis=contrast_basis,
        correctness_assessment=correctness_assessment,
        reason=reason,
        first_evidence=first_evidence,
        second_evidence=second_evidence,
        kind=kind,
        application_contexts=application_contexts,
        scene_tags=scene_tags,
        domain=domain,
        keywords=keywords,
        entities=entities,
        time_sensitivity=time_sensitivity,
        valid_as_of=valid_as_of,
        review_after=review_after,
        importance=importance,
        sensitivity=sensitivity,
        allow_contexts=allow_contexts,
        deny_contexts=deny_contexts,
        default_decision=default_decision,
        explicit_request_override=explicit_request_override,
        disclosure=disclosure,
    )


@mcp.tool()
async def recall_learning_memory(
    view: LearningRecallView = "search",
    query: LearningRecallQuery = "",
    target_ref: LearningTargetRef | None = None,
    limit: JsonLearningQueryLimit = 10,
    offset: JsonLearningInventoryOffset = 0,
    include_archived: StrictBool = False,
    include_pending: StrictBool = False,
    include_versions: StrictBool = False,
    include_evidence: StrictBool = False,
    include_contrasts: StrictBool = True,
    include_merge_suggestions: StrictBool = False,
    include_verification_events: StrictBool = False,
    include_idea_box: StrictBool = False,
    explicit_request: StrictBool = False,
    include_sensitive_evidence: StrictBool = False,
    ai_confirmation: StrictBool = False,
    safety_emergency: StrictBool = False,
) -> dict[str, Any]:
    """Search one topic/ref or browse complete learning-card inventory.

    Use ``view='inventory'`` for "what is in my learning brain", counts,
    topic-wide card collection, or gathering source refs before integration.
    Inventory returns summary-only cards, never quarantined bodies.  A search
    result is deliberately non-exhaustive and must not be reported as the whole
    inventory.

    When an automatic learning envelope supplies a versioned ``item_ref``, copy
    it unchanged into ``target_ref`` and leave ``query`` empty.  A semantic
    query returning zero results does not prove that the referenced card is
    absent.
    """
    return learning_service.recall(
        view=view,
        query=query,
        target_ref=target_ref,
        limit=limit,
        offset=offset,
        include_archived=include_archived,
        include_pending=include_pending,
        include_versions=include_versions,
        include_evidence=include_evidence,
        include_contrasts=include_contrasts,
        include_merge_suggestions=include_merge_suggestions,
        include_verification_events=include_verification_events,
        include_idea_box=include_idea_box,
        explicit_request=explicit_request,
        include_sensitive_evidence=include_sensitive_evidence,
        ai_confirmation=ai_confirmation,
        safety_emergency=safety_emergency,
    )


@mcp.tool()
async def revise_learning_memory(
    write_context_ref: NonEmptyJsonString,
    expected_learning_version: JsonRowVersion,
    target_ref: NonEmptyJsonString,
    expected_target_version: JsonMemoryVersion,
    action: LearningRevisionAction,
    change_class: LearningChangeClass,
    classification_basis: list[NonEmptyJsonString],
    correctness_assessment: NonEmptyJsonString,
    diff: NonEmptyJsonString,
    reason: AuditReason,
    classification_actor: Literal["ai_self"] = "ai_self",
    calm_check: dict[str, Any] | None = None,
    ai_confirmation: StrictBool = False,
    rollback_to_version: StrictInt | None = None,
    title: StrictStr | None = None,
    summary: StrictStr | None = None,
    current_understanding: StrictStr | None = None,
    steps: list[StrictStr] | None = None,
    application_contexts: list[StrictStr] | None = None,
    scene_tags: list[StrictStr] | None = None,
    preceding_context_summary: StrictStr | None = None,
    uncertainties: list[StrictStr] | None = None,
    domain: StrictStr | None = None,
    keywords: list[StrictStr] | None = None,
    entities: list[StrictStr] | None = None,
    source_basis: LearningSourceBasis | None = None,
    claim_review_status: LearningClaimReviewStatus | None = None,
    challenged_claim: StrictStr = "",
    challenge_actor: StrictStr = "",
    challenge_basis: StrictStr = "",
    challenge_evidence_refs: list[StrictStr] | None = None,
    confidence: JsonPercent | None = None,
    time_sensitivity: TimeSensitivity | None = None,
    valid_as_of: StrictStr | None = None,
    review_after: StrictStr | None = None,
    importance: JsonPercent | None = None,
    sensitivity: Sensitivity | None = None,
    context_policy: ContextPolicy | None = None,
    recall_mode: RecallMode | None = None,
    allow_contexts: list[StrictStr] | None = None,
    deny_contexts: list[StrictStr] | None = None,
    default_decision: DefaultDecision | None = None,
    explicit_request_override: ExplicitRequestOverride | None = None,
    disclosure: Disclosure | None = None,
    lifecycle: LearningLifecycle | None = None,
    referent_bindings: list[dict[str, Any]] | None = None,
    add_evidence: list[dict[str, Any]] | None = None,
    add_verification_event: dict[str, Any] | None = None,
    links: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Append a small reversible fix or create a later-wake major learning candidate."""
    optionals = {
        "title": title,
        "summary": summary,
        "current_understanding": current_understanding,
        "steps": steps,
        "application_contexts": application_contexts,
        "scene_tags": scene_tags,
        "preceding_context_summary": preceding_context_summary,
        "uncertainties": uncertainties,
        "domain": domain,
        "keywords": keywords,
        "entities": entities,
        "source_basis": source_basis,
        "confidence": confidence,
        "time_sensitivity": time_sensitivity,
        "valid_as_of": valid_as_of,
        "review_after": review_after,
        "importance": importance,
        "sensitivity": sensitivity,
        "context_policy": context_policy,
        "recall_mode": recall_mode,
        "allow_contexts": allow_contexts,
        "deny_contexts": deny_contexts,
        "default_decision": default_decision,
        "explicit_request_override": explicit_request_override,
        "disclosure": disclosure,
        "lifecycle": lifecycle,
        "referent_bindings": referent_bindings,
    }
    if claim_review_status is not None or any((
        challenged_claim, challenge_actor, challenge_basis,
        challenge_evidence_refs,
    )):
        review_status = claim_review_status or "ordinary"
        claim_review: dict[str, Any] = {"status": review_status}
        if review_status != "ordinary" or any((
            challenged_claim, challenge_actor, challenge_basis,
            challenge_evidence_refs,
        )):
            claim_review.update({
                "challenged_claim": challenged_claim,
                "challenge_actor": challenge_actor,
                "challenge_basis": challenge_basis,
                "challenge_evidence_refs": challenge_evidence_refs or [],
            })
        optionals["claim_review"] = claim_review
    return learning_service.revise(
        write_context_ref=write_context_ref,
        expected_learning_version=expected_learning_version,
        target_ref=target_ref,
        expected_target_version=expected_target_version,
        action=action,
        change_class=change_class,
        classification_actor=classification_actor,
        classification_basis=classification_basis,
        correctness_assessment=correctness_assessment,
        diff=diff,
        reason=reason,
        changes={key: value for key, value in optionals.items() if value is not None},
        add_evidence=add_evidence,
        add_verification_event=add_verification_event,
        links=links,
        calm_check=calm_check,
        ai_confirmation=ai_confirmation,
        rollback_to_version=rollback_to_version,
    )


@mcp.tool()
async def integrate_learning_memories(
    write_context_ref: NonEmptyJsonString,
    expected_learning_version: JsonRowVersion,
    source_learning_ids: list[NonEmptyJsonString],
    synthesis_kind: Literal["summary", "generalization", "contrast", "procedure"],
    classification_basis: list[NonEmptyJsonString],
    correctness_assessment: NonEmptyJsonString,
    diff: NonEmptyJsonString,
    calm_check: dict[str, Any],
    reason: AuditReason,
    kind: LearningKind,
    title: NonEmptyJsonString,
    summary: NonEmptyJsonString,
    current_understanding: NonEmptyJsonString,
    source_basis: LearningSourceBasis,
    confidence: JsonPercent,
    classification_actor: Literal["ai_self"] = "ai_self",
    source_action: Literal["keep", "archive_after_accept"] = "keep",
    merge_suggestion_id: StrictStr | None = None,
    steps: list[StrictStr] | None = None,
    application_contexts: list[StrictStr] | None = None,
    scene_tags: list[StrictStr] | None = None,
    preceding_context_summary: StrictStr = "",
    uncertainties: list[StrictStr] | None = None,
    domain: StrictStr = "",
    keywords: list[StrictStr] | None = None,
    entities: list[StrictStr] | None = None,
    time_sensitivity: TimeSensitivity = "stable",
    importance: JsonPercent = 50,
    sensitivity: Sensitivity = "private",
    context_policy: ContextPolicy = "normal",
    recall_mode: RecallMode = "normal",
    disclosure: Disclosure = "bounded_excerpt",
    referent_bindings: list[dict[str, Any]] | None = None,
    create_idea: StrictBool = False,
    idea_kind: StrictStr | None = None,
    idea_text: StrictStr | None = None,
    idea_inference_chain: list[StrictStr] | None = None,
    idea_uncertainties: list[StrictStr] | None = None,
) -> dict[str, Any]:
    """Create a reviewable synthesis from 2-20 cards; optional ideas stay physically isolated."""
    return learning_service.integrate(
        write_context_ref=write_context_ref,
        expected_learning_version=expected_learning_version,
        source_learning_ids=source_learning_ids,
        synthesis_kind=synthesis_kind,
        classification_actor=classification_actor,
        classification_basis=classification_basis,
        correctness_assessment=correctness_assessment,
        diff=diff,
        calm_check=calm_check,
        reason=reason,
        source_action=source_action,
        merge_suggestion_id=merge_suggestion_id,
        create_idea=create_idea,
        idea_kind=idea_kind,
        idea_text=idea_text,
        idea_inference_chain=idea_inference_chain,
        idea_uncertainties=idea_uncertainties,
        kind=kind,
        title=title,
        summary=summary,
        current_understanding=current_understanding,
        source_basis=source_basis,
        claim_review={"status": "ordinary"},
        confidence=confidence,
        steps=steps,
        application_contexts=application_contexts,
        scene_tags=scene_tags,
        preceding_context_summary=preceding_context_summary,
        uncertainties=uncertainties,
        domain=domain,
        keywords=keywords,
        entities=entities,
        time_sensitivity=time_sensitivity,
        importance=importance,
        sensitivity=sensitivity,
        context_policy=context_policy,
        recall_mode=recall_mode,
        disclosure=disclosure,
        referent_bindings=referent_bindings,
    )


@mcp.tool()
async def review_learning_change(
    write_context_ref: NonEmptyJsonString,
    expected_learning_version: JsonRowVersion,
    candidate_id: NonEmptyJsonString,
    expected_candidate_version: JsonMemoryVersion,
    expected_candidate_hash: NonEmptyJsonString,
    expected_base_version: JsonRowVersion,
    action: LearningReviewAction,
    correctness_assessment: NonEmptyJsonString,
    calm_check: dict[str, Any],
    reason: AuditReason,
    ai_confirmation: JsonTrue,
) -> dict[str, Any]:
    """Accept or reject one fully displayed major change in a later real wake."""
    return learning_service.review(
        write_context_ref=write_context_ref,
        expected_learning_version=expected_learning_version,
        candidate_id=candidate_id,
        expected_candidate_version=expected_candidate_version,
        expected_candidate_hash=expected_candidate_hash,
        expected_base_version=expected_base_version,
        action=action,
        correctness_assessment=correctness_assessment,
        calm_check=calm_check,
        reason=reason,
        ai_confirmation=ai_confirmation,
    )


@mcp.tool()
async def preview_learning_recall(
    situation: NonEmptyJsonString,
    limit: Annotated[StrictInt, Field(ge=1, le=3)] = 3,
) -> dict[str, Any]:
    """Preview, without side effects, which learning summaries a situation would surface."""
    return learning_service.preview_recall(situation=situation, limit=limit)


@mcp.tool()
async def remember_tool_guidance(
    write_context_ref: NonEmptyJsonString,
    expected_tool_row_version: JsonRowVersion,
    tool_name: NonEmptyJsonString,
    operation_key: NonEmptyJsonString,
    display_label: NonEmptyJsonString,
    capability_class: ToolCapabilityClass,
    risk_level: ToolRiskLevel,
    confirmation_policy: ToolConfirmationPolicy,
    completion_rule: NonEmptyJsonString,
    critical_preconditions: list[StrictStr],
    purpose: NonEmptyJsonString,
    use_when: list[NonEmptyJsonString],
    avoid_when: list[NonEmptyJsonString],
    scenario_tags: list[NonEmptyJsonString],
    scenario_examples: list[NonEmptyJsonString],
    call_notes: NonEmptyJsonString,
    keywords: list[StrictStr],
    aliases: list[StrictStr],
    reason: AuditReason,
    salience: JsonPercent = 50,
    auto_recall_mode: ToolRecallMode = "normal",
    salience_reason: StrictStr = "",
    linked_tool_refs: list[StrictStr] | None = None,
    chain_role: ToolChainRole = "standalone",
    handoff_condition: StrictStr = "",
    related_refs: list[StrictStr] | None = None,
    source_type: ToolSourceType = "ai_inferred",
    source_ref: StrictStr | None = None,
    additional_source_refs: list[StrictStr] | None = None,
    confidence: JsonPercent = 50,
    documentation_note: StrictStr = "",
    referent_bindings: list[dict[str, Any]] | None = None,
    rewrite_receipt: StrictStr | None = None,
    expires_at: StrictStr | None = None,
    lifecycle: ToolLifecycle = "active",
) -> dict[str, Any]:
    """Save detailed advice for one advertised callable operation; never execute it."""
    return tool_guidance_service.remember(
        write_context_ref=write_context_ref,
        expected_tool_row_version=expected_tool_row_version,
        tool_name=tool_name,
        operation_key=operation_key,
        display_label=display_label,
        capability_class=capability_class,
        risk_level=risk_level,
        confirmation_policy=confirmation_policy,
        completion_rule=completion_rule,
        critical_preconditions=critical_preconditions,
        purpose=purpose,
        use_when=use_when,
        avoid_when=avoid_when,
        scenario_tags=scenario_tags,
        scenario_examples=scenario_examples,
        call_notes=call_notes,
        keywords=keywords,
        aliases=aliases,
        reason=reason,
        salience=salience,
        auto_recall_mode=auto_recall_mode,
        salience_reason=salience_reason,
        linked_tool_refs=linked_tool_refs,
        chain_role=chain_role,
        handoff_condition=handoff_condition,
        related_refs=related_refs,
        source_type=source_type,
        source_ref=source_ref,
        additional_source_refs=additional_source_refs,
        confidence=confidence,
        documentation_note=documentation_note,
        referent_bindings=referent_bindings,
        rewrite_receipt=rewrite_receipt,
        expires_at=expires_at,
        lifecycle=lifecycle,
    )


@mcp.tool()
async def recall_tool_guidance(
    query: StrictStr = "",
    tool_name: StrictStr | None = None,
    card_id: StrictStr | None = None,
    view: Literal["suggestions", "card", "history", "failures"] = "suggestions",
    include_stale: StrictBool = False,
    include_downweighted: StrictBool = False,
    limit: Annotated[StrictInt, Field(ge=1, le=5)] = 5,
) -> dict[str, Any]:
    """Precisely read tool advice, card details, version history, or AI-reported failures."""
    return tool_guidance_service.recall(
        query=query,
        tool_name=tool_name,
        card_id=card_id,
        view=view,
        include_stale=include_stale,
        include_downweighted=include_downweighted,
        limit=limit,
    )


@mcp.tool()
async def revise_tool_guidance(
    write_context_ref: NonEmptyJsonString,
    expected_tool_row_version: JsonRowVersion,
    card_id: NonEmptyJsonString,
    expected_card_version: JsonMemoryVersion,
    intent: Literal["revise", "retire", "restore"],
    edit_class: Literal["typo", "metadata", "source_addition", "salience_downweight", "major"],
    reason: AuditReason,
    target_version: StrictInt | None = None,
    correctness_assessment: StrictStr | None = None,
    calm_check_stability: StrictStr | None = None,
    calm_check_necessity: StrictStr | None = None,
    calm_check_consequences: StrictStr | None = None,
    calm_check_alternatives: StrictStr | None = None,
    tool_name: StrictStr | None = None,
    operation_key: StrictStr | None = None,
    field_name: StrictStr | None = None,
    before_text: StrictStr | None = None,
    after_text: StrictStr | None = None,
    display_label: StrictStr | None = None,
    documentation_note: StrictStr | None = None,
    purpose: StrictStr | None = None,
    use_when: list[StrictStr] | None = None,
    avoid_when: list[StrictStr] | None = None,
    scenario_tags: list[StrictStr] | None = None,
    scenario_examples: list[StrictStr] | None = None,
    call_notes: StrictStr | None = None,
    keywords: list[StrictStr] | None = None,
    aliases: list[StrictStr] | None = None,
    capability_class: ToolCapabilityClass | None = None,
    risk_level: ToolRiskLevel | None = None,
    confirmation_policy: ToolConfirmationPolicy | None = None,
    completion_rule: StrictStr | None = None,
    critical_preconditions: list[StrictStr] | None = None,
    linked_tool_refs: list[StrictStr] | None = None,
    chain_role: ToolChainRole | None = None,
    handoff_condition: StrictStr | None = None,
    related_refs: list[StrictStr] | None = None,
    source_type: ToolSourceType | None = None,
    source_ref: StrictStr | None = None,
    confidence: JsonPercent | None = None,
    salience: JsonPercent | None = None,
    auto_recall_mode: ToolRecallMode | None = None,
    salience_reason: StrictStr | None = None,
    expires_at: StrictStr | None = None,
) -> dict[str, Any]:
    """Append a reversible small edit or create a cross-wake major candidate."""
    optionals = locals().copy()
    for key in (
        "write_context_ref", "expected_tool_row_version", "card_id",
        "expected_card_version", "intent", "edit_class", "reason",
    ):
        optionals.pop(key, None)
    return tool_guidance_service.revise(
        write_context_ref=write_context_ref,
        expected_tool_row_version=expected_tool_row_version,
        card_id=card_id,
        expected_card_version=expected_card_version,
        intent=intent,
        edit_class=edit_class,
        reason=reason,
        **{key: value for key, value in optionals.items() if value is not None},
    )


@mcp.tool()
async def review_tool_guidance_candidate(
    write_context_ref: NonEmptyJsonString,
    expected_tool_row_version: JsonRowVersion,
    candidate_id: NonEmptyJsonString,
    candidate_hash: NonEmptyJsonString,
    decision: Literal["accept", "keep_pending", "withdraw"],
    correctness_decision: Literal["correct", "uncertain", "incorrect"],
    correctness_assessment: NonEmptyJsonString,
    reason: AuditReason,
    ai_confirmation: StrictBool,
    expected_base_version: JsonMemoryVersion,
) -> dict[str, Any]:
    """Review a fully exposed major tool-guidance candidate in a later real wake."""
    return tool_guidance_service.review(
        write_context_ref=write_context_ref,
        expected_tool_row_version=expected_tool_row_version,
        candidate_id=candidate_id,
        candidate_hash=candidate_hash,
        decision=decision,
        correctness_decision=correctness_decision,
        correctness_assessment=correctness_assessment,
        reason=reason,
        ai_confirmation=ai_confirmation,
        expected_base_version=expected_base_version,
    )


@mcp.tool()
async def record_tool_experience(
    write_context_ref: NonEmptyJsonString,
    expected_tool_row_version: JsonRowVersion,
    card_id: NonEmptyJsonString,
    outcome: Literal[
        "success", "partial_success", "invalid_arguments", "permission_denied",
        "unavailable", "timeout", "network_error", "provider_error",
        "user_cancelled", "unknown"
    ],
    reason_code: NonEmptyJsonString,
    attempt_summary: NonEmptyJsonString,
    lesson: StrictStr | None = None,
    confidence: Annotated[StrictInt, Field(ge=0, le=80)] = 50,
) -> dict[str, Any]:
    """Record one non-verified AI report about a tool attempt; no raw arguments/results."""
    return tool_guidance_service.record_experience(
        write_context_ref=write_context_ref,
        expected_tool_row_version=expected_tool_row_version,
        card_id=card_id,
        outcome=outcome,
        reason_code=reason_code,
        attempt_summary=attempt_summary,
        lesson=lesson,
        confidence=confidence,
    )


@mcp.tool()
async def manage_self_governance_profile(
    action: GovernanceAction,
    scope: GovernanceScope,
    write_context_ref: NonEmptyJsonString,
    expected_profile_version: JsonRowVersion,
    reason: GovernanceReason | None = None,
    text: GovernanceText | None = None,
    trigger_mode: GovernanceTriggerMode | None = None,
    scene_tags: GovernanceSceneTags | None = None,
    candidate_id: NonEmptyJsonString | None = None,
    expected_candidate_hash: NonEmptyJsonString | None = None,
    expected_active_revision: GovernanceExpectedActiveRevision = None,
    target_revision_id: NonEmptyJsonString | None = None,
    ai_confirmation: StrictBool = False,
) -> dict[str, Any]:
    """Create, clear, roll back, withdraw, or later activate an AI-owned boundary.

    This is one flat action facade: do not invent content, payload, metadata, or
    review wrappers.  First call stbrain_open and copy write_context_ref plus the
    selected scope's row_version and active_revision_id.  propose_set uses text,
    trigger_mode, scene_tags, and reason.  propose_clear uses reason;
    propose_rollback also uses target_revision_id.  withdraw uses candidate_id
    and reason.  activate happens only in a later real wake and uses candidate_id,
    expected_candidate_hash, expected_active_revision, and ai_confirmation=true;
    copy the exact current_action_contract arguments when present.  First activation
    omits expected_active_revision (equivalent to JSON null), never use false.
    activate may also include one optional reason.
    The profile can make the AI choose stricter behavior, but never grants an
    external tool, account, data, or execution permission.
    """
    assert service.governance is not None
    return service.governance.manage(
        action=action,
        scope=scope,
        write_context_ref=write_context_ref,
        expected_profile_version=expected_profile_version,
        text=text,
        trigger_mode=trigger_mode,
        scene_tags=list(scene_tags) if scene_tags is not None else None,
        reason=reason,
        candidate_id=candidate_id,
        expected_candidate_hash=expected_candidate_hash,
        expected_active_revision=expected_active_revision,
        target_revision_id=target_revision_id,
        ai_confirmation=ai_confirmation,
    )


@mcp.tool()
async def query_self_governance_profile(
    view: Literal["status", "manual", "revisions"] = "status",
    scope: GovernanceScope | None = None,
    include_content: StrictBool = False,
) -> dict[str, Any]:
    """Read the optional mechanism, exact active/pending state, or one scope's history.

    All scopes may remain empty.  The manual contains a blank structure and
    mechanism facts, never developer-authored value or personality examples.
    """
    assert service.governance is not None
    return service.governance.query(
        view=view,
        scope=scope,
        include_content=include_content,
    )


@mcp.tool()
async def manage_injection_control(
    action: InjectionControlAction,
    scope: InjectionScope,
    write_context_ref: NonEmptyJsonString,
    expected_control_version: JsonRowVersion,
    ai_confirmation: JsonTrue,
    target_mode: InjectionMode | None = None,
    reason: AuditReason | None = None,
    candidate_id: NonEmptyJsonString | None = None,
    expected_candidate_hash: NonEmptyJsonString | None = None,
    expected_active_revision: NonEmptyJsonString | None = None,
    target_revision_id: NonEmptyJsonString | None = None,
) -> dict[str, Any]:
    """Control automatic ST injection without deleting or blocking stored data.

    First call stbrain_open and copy this scope's current row version.  The
    emergency_off action is global-only, immediate and idempotent, but affects
    only the next real wake.  All less restrictive changes first create a
    candidate and can be activated only in a later real wake using the exact
    current_action_contract.  hard_off emits no automatic reminder.
    """
    assert service.injection_control is not None
    return service.injection_control.manage(
        action=action,
        scope=scope,
        write_context_ref=write_context_ref,
        expected_control_version=expected_control_version,
        target_mode=target_mode,
        reason=reason,
        candidate_id=candidate_id,
        expected_candidate_hash=expected_candidate_hash,
        expected_active_revision=expected_active_revision,
        target_revision_id=target_revision_id,
        ai_confirmation=ai_confirmation,
    )


@mcp.tool()
async def query_injection_control(
    view: Literal["status", "manual", "history"] = "status",
) -> dict[str, Any]:
    """Read injection switch state and history; never read or change memory content."""
    assert service.injection_control is not None
    return service.injection_control.query(view=view)


@mcp.tool()
async def remember_planning_memory(
    write_context_ref: NonEmptyJsonString,
    expected_planning_version: JsonRowVersion,
    kind: PlanningKind,
    track: PlanningTrack,
    title: NonEmptyJsonString,
    original_text: NonEmptyJsonString,
    summary: NonEmptyJsonString,
    reminder: StrictStr,
    importance: JsonPercent,
    presence_mode: PlanningPresenceMode,
    scene_tags: list[StrictStr],
    keywords: list[StrictStr],
    timezone: NonEmptyJsonString,
    ai_adoption_statement: NonEmptyJsonString,
    reason: AuditReason,
    calm_check: dict[str, Any],
    ai_confirmation: JsonTrue,
    idempotency_key: NonEmptyJsonString,
    start_at: StrictStr | None = None,
    due_at: StrictStr | None = None,
    review_after: StrictStr | None = None,
    allow_coordination_hint: StrictBool = False,
    parent_ref: StrictStr | None = None,
    dependency_refs: list[StrictStr] | None = None,
) -> dict[str, Any]:
    """Advanced reviewed plan candidate; it cannot become active this wake.

    Ordinary new plans should use remember_memory(module='planning_memory',
    content=...) instead: one call stores an active ordinary record without a
    candidate, an adoption statement, preliminary open or later-wake review.

    The human request is evidence, not adoption.  ``ai_adoption_statement`` and
    the calm check must be authored by the current AI.  The plan becomes active
    only after its full candidate is independently accepted in a later wake.
    Copy the actual root write_context_ref and planning_memory.planning_row_version
    from this wake's stbrain_open summary. If the field rules are unfamiliar, ask
    stbrain_open(view='manual', module='planning_memory'); no shell/file extraction
    is required. Do not pass a literal JSONPath as the reference. binding_reason_code explains binding
    rejections; planning_row_version_conflict is a distinct CAS rejection.
    """
    return planning_service.remember(
        write_context_ref=write_context_ref,
        expected_planning_version=expected_planning_version,
        content={
            "kind": kind,
            "track": track,
            "title": title,
            "original_text": original_text,
            "summary": summary,
            "reminder": reminder,
            "importance": importance,
            "presence_mode": presence_mode,
            "scene_tags": scene_tags,
            "keywords": keywords,
            "start_at": start_at,
            "due_at": due_at,
            "timezone": timezone,
            "review_after": review_after,
            "allow_coordination_hint": allow_coordination_hint,
            "parent_ref": parent_ref,
            "dependency_refs": dependency_refs or [],
            "ai_adoption_statement": ai_adoption_statement,
        },
        reason=reason,
        calm_check=calm_check,
        ai_confirmation=ai_confirmation,
        idempotency_key=idempotency_key,
    )


@mcp.tool()
async def recall_planning_memory(
    query: StrictStr = "",
    plan_ref: StrictStr | None = None,
    limit: Annotated[StrictInt, Field(ge=1, le=50)] = 10,
    include_terminal: StrictBool = False,
    include_history: StrictBool = False,
    include_quarantined: StrictBool = False,
) -> dict[str, Any]:
    """Read plans by one semantic query or one exact versioned plan reference."""
    return planning_service.recall(
        query=query,
        plan_ref=plan_ref,
        limit=limit,
        include_terminal=include_terminal,
        include_history=include_history,
        include_quarantined=include_quarantined,
    )


@mcp.tool()
async def record_planning_event(
    write_context_ref: NonEmptyJsonString,
    expected_planning_version: JsonRowVersion,
    plan_id: NonEmptyJsonString,
    expected_plan_version: JsonMemoryVersion,
    event_type: PlanningEventType,
    reason: AuditReason,
    evidence: list[dict[str, Any]],
    ai_confirmation: JsonTrue,
    idempotency_key: NonEmptyJsonString,
) -> dict[str, Any]:
    """Append evidence-bearing progress/state to the immutable plan ledger."""
    return planning_service.record_event(
        write_context_ref=write_context_ref,
        expected_planning_version=expected_planning_version,
        plan_id=plan_id,
        expected_plan_version=expected_plan_version,
        event_type=event_type,
        reason=reason,
        evidence=evidence,
        ai_confirmation=ai_confirmation,
        idempotency_key=idempotency_key,
    )


@mcp.tool()
async def revise_planning_memory(
    write_context_ref: NonEmptyJsonString,
    expected_planning_version: JsonRowVersion,
    plan_id: NonEmptyJsonString,
    expected_plan_version: JsonMemoryVersion,
    intent: PlanningRevisionIntent,
    reason: AuditReason,
    calm_check: dict[str, Any],
    ai_confirmation: JsonTrue,
    idempotency_key: NonEmptyJsonString,
    changes: dict[str, Any] | None = None,
    rollback_to_version: StrictInt | None = None,
) -> dict[str, Any]:
    """Propose a reversible plan change, abandonment, archive, revival or rollback."""
    return planning_service.revise(
        write_context_ref=write_context_ref,
        expected_planning_version=expected_planning_version,
        plan_id=plan_id,
        expected_plan_version=expected_plan_version,
        intent=intent,
        reason=reason,
        calm_check=calm_check,
        ai_confirmation=ai_confirmation,
        idempotency_key=idempotency_key,
        changes=changes,
        rollback_to_version=rollback_to_version,
    )


@mcp.tool()
async def review_planning_change(
    write_context_ref: NonEmptyJsonString,
    expected_planning_version: JsonRowVersion,
    candidate_id: NonEmptyJsonString,
    expected_candidate_version: JsonMemoryVersion,
    expected_candidate_hash: NonEmptyJsonString,
    expected_base_version: JsonRowVersion,
    decision: PlanningReviewDecision,
    correctness_assessment: NonEmptyJsonString,
    calm_check: dict[str, Any],
    reason: AuditReason,
    ai_confirmation: JsonTrue,
) -> dict[str, Any]:
    """Accept or reject one fully displayed planning candidate in a later wake."""
    return planning_service.review(
        write_context_ref=write_context_ref,
        expected_planning_version=expected_planning_version,
        candidate_id=candidate_id,
        expected_candidate_version=expected_candidate_version,
        expected_candidate_hash=expected_candidate_hash,
        expected_base_version=expected_base_version,
        decision=decision,
        correctness_assessment=correctness_assessment,
        calm_check=calm_check,
        reason=reason,
        ai_confirmation=ai_confirmation,
    )


@mcp.tool()
async def hold_hallucination_record(
    write_context_ref: NonEmptyJsonString,
    expected_vault_version: JsonRowVersion,
    intent: VaultHoldIntent,
    reason: AuditReason,
    ai_confirmation: JsonTrue,
    neutral_title: StrictStr | None = None,
    isolated_content: StrictStr | None = None,
    current_account: StrictStr | None = None,
    basis: StrictStr | None = None,
    reflection: StrictStr = "",
    uncertainty_status: VaultUncertaintyStatus = "ai_isolated",
    expected_warning_version: StrictInt | None = None,
    warning_text: StrictStr | None = None,
    warning_suffix: StrictStr | None = None,
) -> dict[str, Any]:
    """Save AI-selected isolated content or revise the AI's own warning text.

    The server validates structure and CAS only; it never decides that the
    content is false.  The first record and first warning are one transaction.
    """
    if intent == "update_warning":
        return hallucination_service.hold(
            write_context_ref=write_context_ref,
            expected_vault_version=expected_vault_version,
            intent=intent,
            expected_warning_version=expected_warning_version,
            warning_text=warning_text,
            warning_suffix=warning_suffix,
            reason=reason,
            ai_confirmation=ai_confirmation,
        )
    return hallucination_service.hold(
        write_context_ref=write_context_ref,
        expected_vault_version=expected_vault_version,
        intent=intent,
        neutral_title=neutral_title,
        isolated_content=isolated_content,
        current_account=current_account,
        basis=basis,
        reflection=reflection,
        uncertainty_status=uncertainty_status,
        reason=reason,
        ai_confirmation=ai_confirmation,
        warning_text=warning_text,
        warning_suffix=warning_suffix,
    )


@mcp.tool()
async def open_hallucination_vault(
    record_id: StrictStr | None = None,
    warning_confirmation: StrictStr | None = None,
    expected_warning_version: StrictInt | None = None,
    offset: Annotated[StrictInt, Field(ge=0)] = 0,
    limit: Annotated[StrictInt, Field(ge=1, le=200)] = 50,
    write_context_ref: StrictStr | None = None,
) -> dict[str, Any]:
    """Explicitly list neutral metadata, view the warning, or open one record.

    No-id calls expose only a six-field directory.  A record body requires the
    exact current AI-authored warning; a write context is additionally required
    when opening a pending restore for later-wake review.
    """
    return hallucination_service.open(
        write_context_ref=write_context_ref,
        record_id=record_id,
        warning_confirmation=warning_confirmation,
        expected_warning_version=expected_warning_version,
        offset=offset,
        limit=limit,
    )


@mcp.tool()
async def transfer_hallucination_record(
    write_context_ref: NonEmptyJsonString,
    intent: VaultTransferIntent,
    source_ref: StrictStr | None = None,
    expected_source_row_version: StrictInt | None = None,
    expected_vault_version: StrictInt | None = None,
    journal_id: StrictStr | None = None,
    expected_preview_hash: StrictStr | None = None,
    neutral_title: StrictStr | None = None,
    isolated_content: StrictStr | None = None,
    current_account: StrictStr | None = None,
    basis: StrictStr | None = None,
    reflection: StrictStr = "",
    uncertainty_status: VaultUncertaintyStatus = "ai_isolated",
    reason: StrictStr | None = None,
    ai_confirmation: StrictBool = False,
    warning_text: StrictStr | None = None,
    warning_suffix: StrictStr | None = None,
    record_id: StrictStr | None = None,
    expected_record_version: StrictInt | None = None,
    destination_module: Literal["learning_memory"] | None = None,
    destination_row_version: StrictInt | None = None,
) -> dict[str, Any]:
    """Preview/commit a reversible physical transfer or propose a later-wake restore."""
    if intent == "preview":
        return hallucination_service.transfer(
            write_context_ref=write_context_ref,
            intent=intent,
            source_ref=source_ref,
            expected_source_row_version=expected_source_row_version,
        )
    if intent == "propose_restore":
        return hallucination_service.transfer(
            write_context_ref=write_context_ref,
            intent=intent,
            expected_vault_version=expected_vault_version,
            record_id=record_id,
            expected_record_version=expected_record_version,
            destination_module=destination_module,
            destination_row_version=destination_row_version,
            reason=reason,
            ai_confirmation=ai_confirmation,
        )
    return hallucination_service.transfer(
        write_context_ref=write_context_ref,
        intent=intent,
        expected_vault_version=expected_vault_version,
        expected_source_row_version=expected_source_row_version,
        journal_id=journal_id,
        expected_preview_hash=expected_preview_hash,
        neutral_title=neutral_title,
        isolated_content=isolated_content,
        current_account=current_account,
        basis=basis,
        reflection=reflection,
        uncertainty_status=uncertainty_status,
        reason=reason,
        ai_confirmation=ai_confirmation,
        warning_text=warning_text,
        warning_suffix=warning_suffix,
    )


@mcp.tool()
async def review_hallucination_restore(
    write_context_ref: NonEmptyJsonString,
    expected_vault_version: JsonRowVersion,
    candidate_id: NonEmptyJsonString,
    expected_candidate_version: JsonMemoryVersion,
    expected_candidate_hash: NonEmptyJsonString,
    expected_base_record_version: JsonMemoryVersion,
    action: VaultRestoreAction,
    reason: AuditReason,
    ai_confirmation: JsonTrue,
    expected_destination_row_version: StrictInt | None = None,
) -> dict[str, Any]:
    """Activate/reject/withdraw a restore after the later-wake warning-body review."""
    return hallucination_service.review_restore(
        write_context_ref=write_context_ref,
        expected_vault_version=expected_vault_version,
        candidate_id=candidate_id,
        expected_candidate_version=expected_candidate_version,
        expected_candidate_hash=expected_candidate_hash,
        expected_base_record_version=expected_base_record_version,
        action=action,
        reason=reason,
        ai_confirmation=ai_confirmation,
        expected_destination_row_version=expected_destination_row_version,
    )


# Keep every public call flat.  The pinned MCP SDK cannot generate the reviewed
# intent discriminator or strict-extra behavior from ordinary signatures alone,
# so install both the published schemas and the matching runtime enforcement only
# after all public tools have been registered.
install_public_tool_input_contracts(mcp)

_require_execution = os.environ.get("STBRAIN_REQUIRE_EXECUTION_BINDING", "1") == "1"
_execution_epoch = os.environ.get("STBRAIN_EXECUTION_EPOCH", "").strip()
if _require_execution and not _execution_epoch:
    raise RuntimeError("STBRAIN_EXECUTION_EPOCH is required when execution binding is enabled")
if _execution_epoch:
    install_execution_guard(
        mcp, store=ExecutionStore(DATABASE, deployment_epoch=_execution_epoch, capability_secret=WAKE_SECRET),
        onboarding=onboarding, owner_id=OWNER_ID, model_id=MODEL_ID, required=_require_execution,
    )

# The gateway fingerprints only the canonical parameters JSON.  Bind the
# detail-lookup affordance to that exact public schema so a changed/hidden
# recall tool can never be advertised from a stale tool card.
_recall_tool = mcp._tool_manager.get_tool("recall_tool_guidance")
tool_store.detail_lookup_schema_hash = hashlib.sha256(
    json.dumps(
        _recall_tool.parameters,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
