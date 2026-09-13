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
from .usage_guide import module_usage_guide, usage_guide
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
SIMPLE_MEMORY_ACCESS = os.environ.get('STBRAIN_ACCESS_PROFILE', '') == 'simple-memory-v1'

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
    "set", "rollback",
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
    StringConstraints(min_length=1, max_length=1000),
    Field(description="可能进入活动注入的 AI 自选锚点显示文本；人称由作者选择。"),
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
    "unclassified",
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
    Literal["firsthand", "reported", "inferred", "unmarked"],
    Field(
        description=(
            "来源：firsthand 仅用于当前 AI 实例直接经历的本轮事件；"
            "人类讲述、OB/旧档案或过去实例的材料用 reported；"
            "对动机、含义或未观察事实的解释用 inferred；unmarked=未标注。"
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
    Literal["observed", "reported", "inferred", "unmarked"],
    Field(
        description=(
            "这条知识从哪里来，只描述来源，不表示它是否与别的知识相反。"
            "observed=本 AI 直接观察；reported=人类或文档陈述；"
            "inferred=AI 自己的推断；unmarked=未标注。"
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
    "set", "clear", "rollback", "propose_set", "propose_clear", "propose_rollback", "withdraw", "activate"
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
    ),
    Field(description="AI 自写轻提醒、安全阀或给自己的提示词；正文和人称由作者选择。"),
]
GovernanceSceneTag = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=80),
]
GovernanceSceneTags = Annotated[list[GovernanceSceneTag], Field(
    max_length=32,
    description=(
        "治理轻提醒的普通场景标签优先写本轮人类聊天自然会说的词句，如“设个闹钟”“提醒我”“回家了”“还记得”；"
        "AI 可按实际表达自行增改。当前按标签完整短语在本轮场景 query 中 casefold 子串匹配；"
        "只写 AI 内部动作描述而人类话语没有相同词句时不会命中。命中后仍受 scene_relevant 模式、注入开关和预算影响；"
        "专用系统事件标记按真实运行时事件触发，人工打字不产生该事件。"
    ),
)]
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
    ordinary_memory_independent=SIMPLE_MEMORY_ACCESS,
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
    tool_guidance_service=tool_guidance_service,
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
    ordinary_memory_access=SIMPLE_MEMORY_ACCESS,
)

mcp = FastMCP(
    "StillerBrainSelfModel",
    instructions=(
        "StillerBrain simple-memory-v1: 模块一首次完成设置并激活前，其他普通模块为只读，可查询已有内容。"
        "模块一激活后，官端与网关均可直接写入和修改普通记忆。remember_memory 存入；"
        "simple-memory-v1 的普通专用工具同样由服务补入内部上下文与模块行版本；"
        "调用者保留已读目标版本、hash 与自主确认。高级治理旧候选和黑匣子按各自授权规则处理。"
        "recall_* 按关键词、编号查询列表，再读详情；revise_memory 修改内容并保留版本历史。"
        "工具脑按场景浮现作者的一句提醒，详细工具用法按需查询。"
        "存入前可用 stbrain_help 查看当前人称弱提醒；AI 可用 manage_person_reference_advisory 自行修改、关闭或恢复，叙事人称由作者选择。"
        "AI 可自行编写、修改和撤回轻提醒与自定义安全阀，详见 stbrain_help(module='self_governance_profile')，正文默认空白。"
        "治理轻提醒的普通 scene_tags 匹配本轮人类话语，优先写“设个闹钟”“提醒我”“回家了”“还记得”等自然词句，按实际表达自行增改；浮现还受模式、开关与预算影响，系统事件标记另按真实事件触发。"
        "普通记忆读取和模块一查询免密码。服务端验证通过的网关注入调用可免部署密码修改模块一；"
        "官方 DS 或其他非网关注入的直连模型，写入或修改模块一时先 authorize_self_model 输入部署密码，"
        "再用返回的 grant_ref 调用 stbrain_open_direct。网关身份由服务端执行绑定验证，模型名称和测试命名空间不决定权限。"
        "模块一候选仍按真实唤醒完成提交、独立审核和激活。"
        "普通记忆与模块一分别处理，幻觉黑匣子维持原有隔离规则。"
        "网关的 execution_ref 由宿主填入。保存工具提醒与计划只是记录，实际外部操作使用当前工具权限。"
        if SIMPLE_MEMORY_ACCESS else (
        f"{PUBLIC_CONTRACT_VERSION}: StillerBrain is optional memory for this AI. "
        "For ordinary emotional, learning or planning memories, use remember_memory(module, content). "
        "After the required module access is authorized, gateway calls need no preliminary open, "
        "manual version, candidate or later-wake review for that route. "
        "Use stbrain_help for short static instructions, and recall_* tools to read saved memories. "
        "存入前可用 stbrain_help 查看当前人称弱提醒；AI 可用 manage_person_reference_advisory 自行修改、关闭或恢复，叙事人称由作者选择。"
        "AI 可自行编写、修改和撤回轻提醒与自定义安全阀，详见 stbrain_help(module='self_governance_profile')，正文默认空白。"
        "治理轻提醒的普通 scene_tags 匹配本轮人类话语，优先写“设个闹钟”“提醒我”“回家了”“还记得”等自然词句，按实际表达自行增改；浮现还受模式、开关与预算影响，系统事件标记另按真实事件触发。"
        "A real stored result means saved; a candidate, a rejected call or saying 'I will save' does not. "
        "The gateway supplies the reserved execution_ref; do not invent or reuse it. "
        "Non-gateway direct writes require an independently human-authorized context. "
        "Core self-modification, isolated-vault and legacy governance candidates retain their review rules. "
        "Ordinary dedicated revisions and integrations append directly under their authorized context. "
        "stbrain_open(view='manual', module=one_module_name) provides the exact instructions "
        "and full review material; use that wake's real write_context_ref and current version. "
        "The default open summary is not proof that a candidate was reviewed. "
        "Never invent wake evidence, adoption statements, credentials or tool results. "
        "Author revisions preserve earlier originals in version history; plan or tool records grant no external action permission. "
        "Automatic recall is bounded and excludes pending/isolated content. The isolated vault is off by default."
    )),
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
async def stbrain_help(module: BrainManualModule | None = None) -> dict[str, Any]:
    """Read static usage instructions; module selects detailed ordinary-tool help.

    Help contains no private memory and creates no write context. It also shows
    the current optional person-reference advisory, including AI-authored custom
    wording when enabled. For a core
    candidate's actual full review material and presentation proof, use the
    appropriately bound stbrain_open review flow.
    """
    if module is not None:
        result = module_usage_guide(module, simple=SIMPLE_MEMORY_ACCESS)
    elif SIMPLE_MEMORY_ACCESS:
        from .usage_guide import simple_usage_guide
        result = simple_usage_guide()
    else:
        result = usage_guide()
    if module in {None, "emotional_memory", "learning_memory", "tool_guidance",
                  "planning_memory", "shared_person_authoring"}:
        result["person_reference_advisory"] = authoring_rewrite_service.advisory_status()
    return result


@mcp.tool()
async def manage_person_reference_advisory(
    action: Literal["set", "disable", "reset"],
    text: Annotated[StrictStr, Field(min_length=1, max_length=2000)] | None = None,
    write_context_ref: str | None = None,
) -> dict[str, Any]:
    """AI 自己修改、关闭或恢复人称弱提醒；叙事人称由作者选择。

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway writes omit
    internal context and mechanical module row versions; keep target CAS and
    author confirmations wherever the selected operation requires them.
    Legacy mode retains its authorized context.

    set + text 保存自己的提示并开启；disable 关闭正文显示；reset 恢复默认建议。
    用 stbrain_help(module='shared_person_authoring') 读取当前生效内容。
    普通激活后的 simple-memory-v1 直连和网关均只需 action（set 时加 text），
    不用填写版本或执行编号。legacy 沿用 shared_person_authoring 的真实授权上下文。
    每次修改保留历史；这是提醒偏好，不改记忆原文，也不打开自动人称转换。
    """
    return authoring_rewrite_service.manage_advisory(
        action=action, text=text, write_context_ref=write_context_ref,
    )


@mcp.tool()
async def remember_memory(
    module: Literal["emotional_memory", "learning_memory", "planning_memory"],
    content: Annotated[StrictStr, StringConstraints(min_length=1, max_length=2000)],
    title: str | None = None, summary: str | None = None, kind: str | None = None,
    track: str = "internal", keywords: list[str] | None = None,
    importance: StrictInt = 50, emotion: str = "other", source_basis: LearningSourceBasis = "unmarked",
    confidence: JsonPercent | None = None, reason: str | None = None,
    write_context_ref: str | None = None, parent_ref: str | None = None,
    due_at: str | None = None, timezone: str = "UTC",
    rewrite_receipt: StrictStr | None = None,
) -> dict[str, Any]:
    """Save one ordinary memory with one call. Only module and content are required.

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway writes omit
    internal context and mechanical module row versions; keep target CAS and
    author confirmations wherever the selected operation requires them.
    No preliminary stbrain_open, manual version, candidate or later-wake review
    is needed for ordinary gateway and activated simple-profile calls.
    content is preserved as supplied; optional title/summary organize it. For emotional
    memory title is used as summary only when summary is omitted. kind is optional;
    defaults: unclassified / fact / task respectively. Unknown facts default
    to unmarked, confidence=null (未标注); explicit 0–100 remains authored. A planning
    record becomes active, but grants no external execution permission. Core self
    modification still uses its separate review tools. Do not supply write_context_ref
    on the gateway path. In simple-memory-v1 both direct and gateway callers omit
    write_context_ref; legacy direct mode uses its separately authorized context.
    Only a real stored response means success; do not claim a call you did not make.

    人称由 AI 自选；stbrain_help 提供当前可修改、可关闭的人称弱提醒，
    manage_person_reference_advisory 管理该提醒，小说和角色叙事可按需关闭。
    存入前，先参考本工具说明中的当前人称提醒和标签选词建议，再组织参数；采用与否由 AI 自选。
    工具目录是读取时的快照；本轮已修改或关闭提醒时，以最新管理结果为准，刷新 MCP 目录可更新说明。
    保存成功的工具结果同时返回当前人称提醒与简短选词建议，供 AI 自行决定是否采用；
    这是存入后的回执提示，不自动改写刚保存的内容，也不要求再调用一次保存。
    rewrite_receipt 可选，仅情感/学习记忆接受已明确确认的一次性指代改写回执；
    它不自动改写。情感 final_fields 的 /original_text、/summary 对应 content、summary；
    学习的 /title、/summary、/current_understanding 对应 title、summary、content，
    /preceding_context_summary 在本入口固定为空；需要非空前置上下文请用专用学习工具。
    字段须与确认预览完全相同。规划不支持此回执；省略时保持普通存入路径。

    选词可由 AI 按真实内容考虑原词＋近义表达＋语义相关话题＋情感语境关联：
    奶奶家 → 奶奶家 / 奶奶 / 家里的近况 / 家人牵挂。
    小王 → 小王 / 同事 / 上班 / 零食；情感语境：生气或委屈。
    下雨 → 下雨 / 雨天 / 带伞；情感语境：想念。
    Tasker → Tasker / 自动化 / 配置 / 不会用了 / 修好了。
    本入口用 keywords 提供内容检索线索；scene_tags 或 scenario_tags 仅在公布该字段的
    专用工具填写，字段名称以各工具 schema 为准。其中治理轻提醒的
    普通标签匹配本轮人类自然话语。情感分类另按当前 schema 填写，中文感受可写入
    正文或摘要；这些映射不是所有模块通用的自由情绪枚举，也不是系统自动扩写。
    命中只是候选，实际依已有记录、模式、预算与授权决定；它不规定固定回复或自动存入。
    """
    return daily_service.remember(
        module=module, content=content, title=title, summary=summary, kind=kind,
        track=track, keywords=keywords, importance=importance, emotion=emotion,
        source_basis=source_basis, confidence=confidence, reason=reason,
        write_context_ref=write_context_ref, parent_ref=parent_ref,
        due_at=due_at, timezone=timezone, rewrite_receipt=rewrite_receipt,
    )


@mcp.tool()
async def revise_memory(
    target_ref: Annotated[StrictStr, StringConstraints(min_length=1, max_length=100)],
    changes: dict[str, Any],
    reason: str | None = None,
    write_context_ref: str | None = None,
) -> dict[str, Any]:
    """统一修改情感、学习、工具、规划四个普通脑，保留各版本历史。

    工具卡同样使用 target_ref + changes：引用形如 tool-card://toolcard_…@1，
    changes 可直接写 reminder、purpose、scenario_tags、confidence 等要改的字段。
    reminder/source_ref/expires_at 显式 null 清除，省略保留；clear_fields 兼容旧写法。
    退役：revise_memory(target_ref=已读引用, changes={'intent':'retire'})。
    恢复：revise_memory(target_ref=已读当前引用,
        changes={'intent':'restore','target_version':已读历史版本号})。
    即 changes.intent 和 changes.target_version 都放在 changes 内；工具卡不使用 changes.lifecycle。
    默认普通修改，无需填写 edit_class 或审查表。simple 目录收起专用工具卡修改名。
    旧接口内部保留兼容；实际调用以当前工具目录和连接授权为准，网关不会执行目录外的旧名称。

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway writes omit
    internal context and mechanical module row versions; keep target CAS and
    author confirmations wherever the selected operation requires them.
    Supply the versioned target_ref you actually read and changed fields only.
    Fields include original_text/current_understanding, summary, keywords,
    importance, and each module's author-defined source/emotion fields.
    Technical IDs, ownership, hashes and derived fields are maintained by ST.
    In simple-memory-v1, both direct and gateway calls need no preliminary open. A stale
    target is rejected without overwrite: reread it before deciding to revise.
    Omit write_context_ref in simple-memory-v1; the server binds the operation.
    Legacy mode retains its authorized current-context requirements.
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

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway writes omit
    internal context and mechanical module row versions; keep target CAS and
    author confirmations wherever the selected operation requires them.
    Use target_ref and event_seq from the plan you read. expected_event_seq is
    that event_seq; it protects updates even when the plan content version has
    not changed. progress/complete/reopen need genuine evidence (source_kind,
    source_ref, evidence_summary, provenance); pause/resume do not. note is your
    update reason, not a claim of independent verification. No preliminary open
    or module version is needed on the gateway path. Conflicts require rereading,
    never automatic overwrite. In simple-memory-v1 direct use also omits
    write_context_ref; legacy direct mode keeps its separately authorized context.
    """
    return daily_revision_service.advance(
        target_ref=target_ref, expected_event_seq=expected_event_seq,
        event_type=event_type, note=note, evidence=evidence,
        write_context_ref=write_context_ref,
    )


@mcp.tool()
async def stbrain_open(
    view: BrainOpenView = "summary",
    module: BrainManualModule | None = None,
    page: Annotated[StrictInt, Field(ge=0)] = 0,
    expected_material_hash: Annotated[StrictStr, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None,
    query: Annotated[StrictStr, Field(max_length=4000)] = "",
    limit: Annotated[StrictInt, Field(ge=1, le=50)] = 20,
    cursor: Annotated[StrictStr, Field(min_length=1, max_length=2048)] | None = None,
) -> dict[str, Any]:
    """Read ordinary memory with view='recall', or open core-edit/review material.

    view='recall' is an authenticated read: no wake, password, write context or
    author confirmation is needed. Empty query browses a complete paged directory;
    a query searches emotional, learning, planning and tool memory. module omitted
    selects all four; select one ordinary module to narrow it. Results are safe
    summaries with exact references and detail_lookup for original/history reads.
    Keep query/module and use next_cursor; changed records require a fresh search.
    Partial/errors/truncated are explicit; scores are not cross-brain probabilities.
    This view excludes core identity, isolated-vault content and pending candidates.
    It does not create candidate presentation proof or alter any write permission.

    Default summary returns the real root write_context_ref and module versions,
    not documentation placeholders. Reuse them only in this real user/tool round.
    For instructions or full pending review material, use view='manual' with one
    module. For a self-model candidate, view='review' returns bounded pages directly
    in MCP: start with page=0 and follow next_arguments until fully_presented=true.
    Later pages require the returned expected_material_hash. A missing page or a
    different wake/version never counts as full review. No shell, file or workspace
    is required. Opens reuse this wake, not create one. Core changes still need
    separate real wakes to propose, review and activate. Ordinary memories use
    recall_* without this preliminary open. In simple-memory-v1, ordinary writes
    become available after module one is first completed and activated; until
    then those modules are read-only. stbrain_help supplies static instructions.
    """
    if view == "recall":
        if page != 0 or expected_material_hash is not None:
            return {"decision": "reject", "reason_code": "recall_uses_cursor_not_review_page", "state_changed": False}
        return service.recall_memory(query=query, module=module, limit=limit, cursor=cursor)
    if query or cursor is not None or limit != 20:
        return {"decision": "reject", "reason_code": "query_parameters_require_recall_view", "state_changed": False}
    return service.open_brain(view=view, module=module or "self_revision", page=page,
                              expected_material_hash=expected_material_hash)


@mcp.tool()
async def stbrain_open_direct(
    grant_ref: DirectGrantReference,
) -> dict[str, Any]:
    """Consume one server-authorized grant and open a non-injected direct write context.

    The grant is short-lived and one-use. It is bound server-side to this owner,
    model, direct client principal, scopes, and its authorization event. In the
    simple-memory-v1 profile, authorize_self_model issues the module-one grant
    to the deployment-password holder; this does not assert a human is present.
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

    Use a server-verified gateway execution and its injected wake, or a valid
    authorized direct context. In simple-memory-v1 the gateway needs no deployment
    password; direct writes require authorize_self_model and stbrain_open_direct.
    Copy write_context_ref and row_version from the corresponding open result.
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
    """Activate an accepted candidate only in a later independently verified real wake.

    Use the verified gateway binding, or an authorized direct context. In the
    simple-memory-v1 profile only direct self writes require the deployment password.
    First activation also opens ordinary-memory writing; reads are already available.

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
    module: Literal[
        "emotional_memory_module_two",
        "learning_memory_module_three",
        "tool_guidance_module",
    ],
    draft_fields: dict[str, StrictStr],
    rewrite_targets: list[dict[str, Any]],
    write_context_ref: NonEmptyJsonString = None,
    expected_authoring_version: JsonRowVersion = None,
    draft_version: JsonRowVersion = None,
    referent_bindings: list[dict[str, Any]] = None,
    conversation_mode: Literal["one_to_one", "group", "unknown"] = None,
    authenticated_participant_entity_ids: list[NonEmptyJsonString] = None,
    alias_collision_scope: NonEmptyJsonString = None,
    alias_collision_scope_version: JsonMemoryVersion = None,
    protected_spans: list[dict[str, Any]] = None,
    module_schema_version: NonEmptyJsonString = None,
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
    """AI 指明称呼指谁、替换成什么，预览这份草稿中的字面修改。

    最少填写 module、draft_fields、rewrite_targets。每个目标填写 field_path、
    surface_form、entity_ref（作者给人物的名称即可）、target_surface_form；同一字段中
    原词只出现一次可省 occurrence_index，多次出现时填写从 0 开始的具体位置。
    无需再交 referent_bindings、认证人物名单或别名登记；人物身份来自作者声明，
    群聊、unknown 场景和历史人物也可由作者明确指定，服务不冒充宿主认证。
    protected_spans 可选且生效；这里不自动识别引号或代码区域。

    draft_fields 的键使用带 / 的字段路径。情感示例：
    {"/original_text":"我和她完成了练习。","/summary":"共同练习。"}。
    普通存入的 content 对应情感 /original_text、学习 /current_understanding；
    summary、title 对应 /summary、/title。工具卡用适用的 /purpose、/call_notes 等。
    rewrite_targets 中的 field_path 使用同一条带 / 路径。
    示例展示字段格式；人物只需在 rewrite_targets 明确指定，说明书可按需查阅。
    module_schema_version 和草稿版本可省略；服务提供当前模块版本及内部记账。
    兼容参数显式提供时仍检查相应版本/内容是否一致；与工具 contract_version 不同。
    也可用 stbrain_open(view='manual',module='shared_person_authoring') 查阅
    shared_person_authoring.modules[目标模块].module_schema_version。

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep exact draft hashes
    and author confirmations. Legacy mode retains its authorized context.
    A preview persists draft/receipt metadata, so it follows the write prerequisite;
    it does not store the draft as an active memory.
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
    preview_id: NonEmptyJsonString,
    ai_confirmation: JsonTrue,
    write_context_ref: NonEmptyJsonString = None,
    expected_authoring_version: JsonRowVersion = None,
    module: Literal[
        "emotional_memory_module_two",
        "learning_memory_module_three",
        "tool_guidance_module",
    ] = None,
    expected_source_draft_hash: NonEmptyJsonString = None,
    expected_suggestion_hash: NonEmptyJsonString = None,
    expected_validation_context_hash: NonEmptyJsonString = None,
    final_fields: dict[str, StrictStr] = None,
    final_fields_hash: NonEmptyJsonString = None,
    conversation_mode: Literal["one_to_one", "group", "unknown"] = None,
    authenticated_participant_entity_ids: list[NonEmptyJsonString] = None,
    alias_collision_scope: NonEmptyJsonString = None,
    alias_collision_scope_version: JsonMemoryVersion = None,
    protected_spans: list[dict[str, Any]] = None,
    module_schema_version: NonEmptyJsonString = None,
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
    """Confirm the exact preview and issue one opaque single-use write receipt.

    最少填写 preview_id 和 ai_confirmation=true，即确认自己已读过的那份完整预览。
    服务从同一 owner/model 的不可变预览带回版本、hash 和最终字段；显式提供旧参数时
    仍逐项检查。旧预览保留其原上下文规则，不能自动转成新的作者声明。
    成功后返回 final_fields；存入时带 rewrite_receipt 并采用原预览的完整字段。
    final_fields 和所有 hash 都可省，服务从 preview_id 的快照带回；仅兼容手动提供时校验。
    最终字段保留带 / 的名称与确认文本，如情感 /original_text、/summary。
    草稿或字段内容变化时重新预览；带回执存入时映射回普通 content、summary 等字段。
    module_schema_version 可省略，由该预览的已保存上下文提供，与工具 contract_version 不同。
    也可用 stbrain_open(view='manual',module='shared_person_authoring') 查阅
    shared_person_authoring.modules[目标模块].module_schema_version。

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep the preview's exact
    preview_id and explicit author confirmation; the service supplies exact
    hashes and target module. Legacy mode retains
    its authorized context and wake-bound receipt rules.
    """
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
    origin: MemoryOrigin = "unmarked",
    confidence: JsonPercent | None = None,
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

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep target CAS and
    author confirmations. Legacy mode retains its authorized context.
    For ordinary new memories prefer remember_memory(module='emotional_memory',
    content=...). That route saves once without preliminary open or manual versions.

    Only the legacy context-bound path copies write_context_ref and
    emotional_memory.row_version from its bound stbrain_open. Later original-text
    edits use revise_memory with changes.original_text and retain earlier versions;
    revise_emotional_memory edits its published metadata fields. original_text and summary are
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

    simple-memory-v1 permits this read before module-one activation. Ordinary
    writes open after activation; owner and sensitive-disclosure rules still apply.
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
    """Append a new emotional metadata/interpretation version.

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep the observed
    expected_memory_version and author confirmations. Legacy mode retains its
    authorized context. To edit the current original_text, use revise_memory with
    changes.original_text; earlier original versions remain available. This
    dedicated tool accepts the metadata fields in its own published schema.
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
    origin: MemoryOrigin = "unmarked",
    confidence: JsonPercent | None = None,
    keywords: list[StrictStr] | None = None,
    entities: list[StrictStr] | None = None,
    referent_bindings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create one timeline aggregate from 2-20 memories and archive its sources.

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep source references
    and author confirmations. Legacy mode retains its authorized context.
    This integration preserves source records and version histories. Current body
    edits use revise_memory; historical versions remain queryable. The
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
    """Request, explicitly confirm, lower, or remove one bounded brain pin.

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep the pin's target
    CAS and author confirmations. Legacy mode retains its authorized context.
    request uses pin_kind, display_text, and source_ref. confirm uses pin_id and
    ai_confirmation=true. In simple-memory-v1 the author can confirm immediately;
    legacy wake-bound requests retain their later-real-wake confirmation rule.
    replace_pin_id is required only
    when five pins are already active. lower/remove use only pin_id and reason.
    The source must remain a permitted verified core reference; changing a pin
    does not rewrite the underlying self-definition.
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
    """Veto and scrub ephemeral text by exactly one ephemeral_id or thread_id.

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep the exact target
    and author confirmations. Legacy mode retains its authorized context.
    """
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
    correctness_assessment: NonEmptyJsonString,
    reason: AuditReason,
    source_basis: LearningSourceBasis = "unmarked",
    confidence: JsonPercent | None = None,
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

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep target CAS and
    author confirmations. Legacy mode retains its authorized context.
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

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep target CAS and
    author confirmations. Legacy mode retains its authorized context.
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

    simple-memory-v1 permits this read before module-one activation. Ordinary
    writes open after activation; owner and quarantine/disclosure rules still apply.
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
    reason: AuditReason,
    classification_basis: list[NonEmptyJsonString] | None = None,
    correctness_assessment: NonEmptyJsonString | None = None,
    diff: NonEmptyJsonString | None = None,
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
    """Append an exact-version authored learning correction directly, retaining history.

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep the observed
    expected_item_version, content references and author confirmations. Legacy
    mode retains its authorized context. Current revisions do not create a new
    review candidate; review_learning_change handles previously stored candidates.
    classification_basis, correctness_assessment, diff and calm_check may be
    omitted or null. Supplied legacy audit notes are author input, not proof of review.
    """
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
    reason: AuditReason,
    kind: LearningKind,
    title: NonEmptyJsonString,
    summary: NonEmptyJsonString,
    current_understanding: NonEmptyJsonString,
    source_basis: LearningSourceBasis = "unmarked",
    confidence: JsonPercent | None = None,
    classification_basis: list[NonEmptyJsonString] | None = None,
    correctness_assessment: NonEmptyJsonString | None = None,
    diff: NonEmptyJsonString | None = None,
    calm_check: dict[str, Any] | None = None,
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
    """Commit an authored synthesis from 2-20 cards; retain source history.

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep exact versioned
    source references and author confirmations. Legacy mode retains its authorized
    context. Current integration saves directly; optional ideas stay physically
    isolated and their separate result must be checked.
    classification_basis, correctness_assessment, diff and calm_check may be
    omitted or null. Supplied legacy audit notes are author input, not proof of review.
    """
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
    """Accept or reject one previously stored learning-change candidate by exact identity.

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep candidate hash,
    candidate/base versions and explicit author confirmation for acceptance.
    Acceptance still requires complete matching review material; this candidate
    path has no extra later-wake wait. Rejection preserves its safe escape path.
    Legacy mode retains its authorized context. Static help supplies instructions,
    not candidate presentation proof.
    """
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
    """Preview which learning summaries a situation would surface.

    simple-memory-v1 permits this read before module-one activation. Ordinary
    writes open after activation. This preview does not save a memory or issue
    an injected wake; existing owner, quarantine and disclosure rules apply.
    """
    return learning_service.preview_recall(situation=situation, limit=limit)


@mcp.tool()
async def remember_tool_guidance(
    write_context_ref: NonEmptyJsonString,
    expected_tool_row_version: JsonRowVersion,
    tool_name: NonEmptyJsonString,
    purpose: NonEmptyJsonString,
    operation_key: NonEmptyJsonString = "general",
    display_label: StrictStr | None = None,
    capability_class: ToolCapabilityClass = "real_world_action",
    risk_level: ToolRiskLevel = "high",
    confirmation_policy: ToolConfirmationPolicy = "explicit_each_time",
    completion_rule: StrictStr = "",
    critical_preconditions: list[StrictStr] | None = None,
    use_when: list[NonEmptyJsonString] | None = None,
    avoid_when: list[NonEmptyJsonString] | None = None,
    scenario_tags: list[NonEmptyJsonString] | None = None,
    scenario_examples: list[NonEmptyJsonString] | None = None,
    call_notes: StrictStr = "",
    keywords: list[StrictStr] | None = None,
    aliases: list[StrictStr] | None = None,
    reason: AuditReason = "记录工具使用提醒",
    reminder: Annotated[StrictStr, Field(max_length=100)] | None = None,
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
    """记录一个服务或工具的用途；reminder 是自主撰写的一句场景提醒，详细用法可选。

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep target CAS and
    author confirmations. Legacy mode retains its authorized context. A tool
    reminder records advice; actual execution uses the live tool's permissions.
    """
    return tool_guidance_service.remember(
        write_context_ref=write_context_ref,
        expected_tool_row_version=expected_tool_row_version,
        tool_name=tool_name,
        operation_key=operation_key,
        display_label=display_label if display_label is not None else tool_name,
        capability_class=capability_class,
        risk_level=risk_level,
        confirmation_policy=confirmation_policy,
        completion_rule=completion_rule,
        critical_preconditions=critical_preconditions or [],
        purpose=purpose,
        reminder=reminder,
        use_when=use_when or [],
        avoid_when=avoid_when or [],
        scenario_tags=scenario_tags or [],
        scenario_examples=scenario_examples or [],
        call_notes=call_notes,
        keywords=keywords or [],
        aliases=aliases or [],
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
    view: Literal["suggestions", "directory", "card", "history", "failures"] = "suggestions",
    include_stale: StrictBool = False,
    include_downweighted: StrictBool = False,
    limit: Annotated[StrictInt, Field(ge=1, le=5)] = 5,
) -> dict[str, Any]:
    """Precisely read tool advice, card details, version history, or AI-reported failures.

    simple-memory-v1 permits this read before module-one activation. Ordinary
    writes open after activation. Use an empty query or view='directory' for the
    bounded directory, then card_id with view='card'/'history'/'failures' for detail.
    Reading advice grants no permission to execute the referenced tool.
    """
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
    intent: Literal["revise", "retire", "restore"] = "revise",
    edit_class: Literal["typo", "metadata", "source_addition", "salience_downweight", "major"] = "major",
    reason: AuditReason = "更新工具使用记忆",
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
    reminder: Annotated[StrictStr, Field(max_length=100)] | None = None,
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
    clear_fields: list[Literal["reminder", "source_ref", "expires_at"]] | None = None,
) -> dict[str, Any]:
    """直接更新作者内容并保留版本历史；也可退役或恢复自己的工具卡。

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep the observed
    expected_card_version and author confirmations. Legacy mode retains its
    authorized context. Card changes do not grant real tool execution authority.
    expires_at omitted or null keeps the current expiry; clear_fields=["expires_at"]
    explicitly clears it. Do not set and clear the same field in one call.
    """
    optionals = locals().copy()
    for key in (
        "write_context_ref", "expected_tool_row_version", "card_id",
        "expected_card_version", "intent", "edit_class", "reason",
        "clear_fields",
    ):
        optionals.pop(key, None)
    changes = {key: value for key, value in optionals.items() if value is not None}
    for key in clear_fields or []:
        if key in changes:
            return {"decision": "reject", "reason_code": "set_and_clear_conflict", "state_changed": False}
        changes[key] = None
    return tool_guidance_service.revise(
        write_context_ref=write_context_ref,
        expected_tool_row_version=expected_tool_row_version,
        card_id=card_id,
        expected_card_version=expected_card_version,
        intent=intent,
        edit_class=edit_class,
        reason=reason,
        **changes,
    )


@mcp.tool()
async def review_tool_guidance_candidate(
    write_context_ref: NonEmptyJsonString,
    expected_tool_row_version: JsonRowVersion,
    candidate_id: NonEmptyJsonString,
    candidate_hash: NonEmptyJsonString,
    decision: Literal["accept", "keep_pending", "withdraw"],
    expected_base_version: JsonMemoryVersion,
    reason: AuditReason = "处理历史工具卡候选",
    correctness_decision: Literal["correct", "uncertain", "incorrect"] | None = None,
    correctness_assessment: StrictStr | None = None,
    ai_confirmation: StrictBool = False,
) -> dict[str, Any]:
    """处理旧版遗留候选。普通作者可按候选标识、hash 与基线版本明确 accept、
    keep_pending 或 withdraw；新修改直接使用 revise_memory 追加版本。
    旧 wake 绑定路径的 accept/keep_pending 仍使用原复核流程。

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep exact candidate
    hash, base version and author confirmations. Ordinary acceptance is an
    explicit author choice, not evidence of a later real wake or independent review.
    Legacy mode retains its authorized context and original review requirements.
    """
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
    confidence: Annotated[StrictInt, Field(ge=0, le=100)] = 50,
) -> dict[str, Any]:
    """Record one non-verified AI report about a tool attempt; no raw arguments/results.

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep exact card references
    and author confirmations. Legacy mode retains its authorized context. Report
    the actual observed outcome; this memory neither executes nor verifies a tool.
    """
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
    """设置、清空、回退自己的治理内容；普通认证模式直接保存并保留版本。

    Self-authored light reminders and custom safety prompts are optional blank
    content the AI can set/clear/rollback for a scope with its chosen trigger_mode.

    普通 scene_relevant 的 scene_tags 匹配本轮人类话语：优先写人类聊天自然会说的
    “设个闹钟”“提醒我”“回家了”“还记得”等词句，并按实际表达自行增改。
    它们描述会出现的聊天表达，不读取 AI 内部动作；只写“动手之前”“想用工具”时，
    本轮话语须真的出现该完整短语才会命中。当前采用本轮场景 query 的 casefold
    子串匹配，不自动推断同义词；本项目网关使用最新一条人类消息的文本。
    命中后仍受触发模式、注入开关及预算影响，修改在下一次生成前的新快照使用；
    一次未浮现不足以判断标签错误。专用系统事件标记需真实运行时事件，人工写出
    标记不能冒充事件；该例外不影响普通中文标签。标签词句不是固定清单。

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway set/clear/rollback
    callers omit internal context and both mechanical module fields expected_profile_version and
    expected_active_revision. The host reads one latest scope snapshot and preserves
    transactional CAS; a genuine concurrent edit is returned for author review.
    Legacy mode retains its authorized context and author confirmations.
    The propose_*/activate/withdraw actions retain their separately bound legacy
    candidate flow, including in simple-memory-v1; use the matching current contract.
    This is one flat action facade: do not invent content, payload, metadata, or
    review wrappers. In simple-memory-v1 use set/clear/rollback; the server fills
    write_context_ref and both mechanical CAS fields. Rollback selects a real
    target_revision_id returned by history; callers do not guess version numbers.
    Legacy propose_set uses text,
    trigger_mode, scene_tags, and reason.  propose_clear uses reason;
    propose_rollback also uses target_revision_id.  withdraw uses candidate_id
    and reason.  activate happens only in a later real wake and uses candidate_id,
    expected_candidate_hash, expected_active_revision, and ai_confirmation=true;
    copy the exact current_action_contract arguments when present. When this scope
    has no active revision, expected_active_revision is JSON null, never false.
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

    Discover self-authored light reminders and custom safety prompts here; the
    AI can set/clear/rollback its own text through manage_self_governance_profile,
    using the existing scopes and manual_only/scene_relevant trigger modes.

    simple-memory-v1 permits this read before module-one activation. Ordinary
    writes open after activation; content remains owner-scoped and opt-in.
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
    ai_confirmation: StrictBool = False,
    target_mode: InjectionMode | None = None,
    reason: AuditReason | None = None,
    candidate_id: NonEmptyJsonString | None = None,
    expected_candidate_hash: NonEmptyJsonString | None = None,
    expected_active_revision: NonEmptyJsonString | None = None,
    target_revision_id: NonEmptyJsonString | None = None,
) -> dict[str, Any]:
    """Control automatic ST injection without deleting or blocking stored data.

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway set/rollback
    and global emergency_off callers omit internal context and mechanical module
    row versions for ordinary scopes. Keep target CAS and author confirmations.
    Legacy mode retains its authorized context. The propose_*/activate/withdraw
    actions keep their separate legacy candidate binding even in this profile.
    In simple-memory-v1 ordinary scopes use set/rollback with the active revision
    actually read; the server fills internal binding and mechanical version. The
    emergency_off action is global-only, immediate and idempotent, but affects
    only the next real wake. The isolated hallucination_vault scope keeps its
    separate authorization and review flow. hard_off emits no automatic reminder.
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
    """Read injection switch state and history; never read or change memory content.

    simple-memory-v1 permits this read before module-one activation. Ordinary
    writes open after activation; changing isolated-vault control still requires
    its separate authorization flow.
    """
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
    """Save a final authored plan directly with its detailed planning fields.

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep target CAS and
    author confirmations. Legacy mode retains its authorized context.
    Ordinary new plans should use remember_memory(module='planning_memory',
    content=...) instead: one call stores an active ordinary record without a
    candidate, an adoption statement, preliminary open or later-wake review.

    This detailed schema still requires ai_adoption_statement, calm_check,
    ai_confirmation=true and idempotency_key. Supply your own actual statement;
    calm_check is legacy audit input, not proof that an independent review occurred.
    A successful stored result creates the active plan directly; no new candidate
    or later-wake acceptance is created by this tool. Read static detailed help
    through stbrain_help(module='planning_memory'). Only the legacy context-bound
    path copies write_context_ref and planning_memory.planning_row_version from
    its bound stbrain_open. Version conflicts require rereading the actual target.
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
    """Read plans by one semantic query or one exact versioned plan reference.

    simple-memory-v1 permits this read before module-one activation. Ordinary
    writes open after activation. Use plan_ref from a result for exact details,
    and include_history when earlier versions/events are needed.
    """
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
    """Append evidence-bearing progress/state to the immutable plan ledger.

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep expected_plan_version,
    author confirmations and idempotency_key. Legacy mode retains its authorized
    context. Use this tool's plan_id/reason fields; advance_plan instead uses
    target_ref/expected_event_seq/note. Progress/completion evidence must be genuine.
    """
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
    idempotency_key: NonEmptyJsonString,
    calm_check: dict[str, Any] | None = None,
    ai_confirmation: JsonTrue | None = None,
    changes: dict[str, Any] | None = None,
    rollback_to_version: StrictInt | None = None,
) -> dict[str, Any]:
    """Directly append a plan change, abandonment, archive, revival or rollback.

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep expected_plan_version,
    intent, reason and idempotency_key. Legacy mode retains its authorized
    context. A successful revised result changes the plan and retains history;
    no new review candidate is created.
    calm_check and ai_confirmation may be omitted or null; explicit false is
    rejected. Supplied legacy audit notes are author input, not proof of review.
    Existing candidate author confirmations remain a separate review contract.
    """
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
    """Explicitly accept or reject a previously stored planning candidate.

    In simple-memory-v1 ordinary modules are read-only before module one's first
    completed activation. After activation, direct MCP and gateway callers omit
    internal context and mechanical module row versions. Keep the exact candidate
    hash, base version and author confirmations. Legacy mode retains its authorized
    context. This compatibility path has no extra later-wake wait; it checks the
    candidate and current target before applying. New plans and edits save directly.
    """
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
from .person_reference_surface import install_person_reference_advisory_surface
install_person_reference_advisory_surface(mcp, authoring_rewrite_service)
install_public_tool_input_contracts(mcp)

if SIMPLE_MEMORY_ACCESS:
    from .ordinary_access_policy import install_ordinary_access_policy
    from .self_password import SelfPasswordAuthority, install_self_password_guard
    self_password_authority = SelfPasswordAuthority(os.environ.get('STBRAIN_SELF_PASSWORD_HASH_FILE'))

    @mcp.tool()
    async def authorize_self_model(password: StrictStr) -> dict[str, Any]:
        """官方或其他直连模型输入部署密码，取得模块一修改授权；已验证网关注入调用免此步骤。

        密码由部署者自行决定是否告知 AI。授权有效 15 分钟；原文不进入
        ST 的数据库、日志或返回值。官方直连按返回 grant_ref 打开自我修改上下文；
        普通记忆读取和模块一查询均免密码；普通写改在模块一首次完成并激活后开放。
        网关使用宿主提供的执行绑定和原有修改工具；
        两种连接均保留候选的真实唤醒审核与激活流程。此操作只授权，具体修改由后续调用者决定。
        """
        return self_password_authority.issue(
            password, onboarding=onboarding, owner_id=OWNER_ID, model_id=MODEL_ID,
            client_principal=DIRECT_CLIENT_PRINCIPAL,
        )

    password_tool = mcp._tool_manager.get_tool('authorize_self_model')
    password_tool.fn_metadata.arg_model.model_config.update(extra='forbid', hide_input_in_errors=True)
    password_tool.fn_metadata.arg_model.model_rebuild(force=True)
    password_tool.parameters['additionalProperties'] = False
    install_ordinary_access_policy(
        mcp, onboarding=onboarding, owner_id=OWNER_ID, model_id=MODEL_ID,
        services={'emotional_memory':emotional_service, 'learning_memory':learning_service,
                  'tool_guidance':tool_guidance_service, 'planning_memory':planning_service,
                  'self_governance': service.governance, 'injection_control':injection_control_service,
                  'shared_person_authoring':authoring_rewrite_service},
    )

_require_execution = os.environ.get("STBRAIN_REQUIRE_EXECUTION_BINDING", "1") == "1"
_execution_epoch = os.environ.get("STBRAIN_EXECUTION_EPOCH", "").strip()
if _require_execution and not _execution_epoch:
    raise RuntimeError("STBRAIN_EXECUTION_EPOCH is required when execution binding is enabled")
_execution_store = (
    ExecutionStore(DATABASE, deployment_epoch=_execution_epoch, capability_secret=WAKE_SECRET)
    if _execution_epoch else None
)
if SIMPLE_MEMORY_ACCESS:
    # The subsequently installed outer guard claims the exact gateway call
    # before this guard decides between gateway and password-backed direct use.
    install_self_password_guard(
        mcp, self_password_authority, execution_store=_execution_store,
        onboarding=onboarding, owner_id=OWNER_ID, model_id=MODEL_ID,
    )
if _execution_epoch:
    install_execution_guard(
        mcp, store=_execution_store,
        onboarding=onboarding, owner_id=OWNER_ID, model_id=MODEL_ID, required=_require_execution,
        ordinary_authenticated=SIMPLE_MEMORY_ACCESS,
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


if SIMPLE_MEMORY_ACCESS:
    # Filter discovery only after every validation/authorization wrapper has
    # been installed. Previously cached dedicated calls retain those guards.
    from .simple_tool_catalog import install_simple_tool_catalog
    install_simple_tool_catalog(mcp)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
