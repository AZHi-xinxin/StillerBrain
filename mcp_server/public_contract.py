"""Single source of truth for the AI-facing ``public-tools/20`` candidate contract.

This module contains transport-safe schemas and stage routing only.  It never
contains owner data, wake capabilities, challenge responses, or credentials.
The MCP server publishes these schemas, while the service facade uses the same
definitions to validate an intent before delegating to the runtime.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, TypeAlias


PUBLIC_CONTRACT_VERSION = "public-tools/20"

BrainOpenView: TypeAlias = Literal["summary", "manual", "review"]
BrainManualModule: TypeAlias = Literal[
    "self_revision", "emotional_memory", "learning_memory", "tool_guidance",
    "planning_memory", "self_governance_profile", "injection_control",
    "hallucination_vault", "shared_person_authoring",
]
BRAIN_MANUAL_MODULES = (
    "self_revision", "emotional_memory", "learning_memory", "tool_guidance",
    "planning_memory", "self_governance_profile", "injection_control",
    "hallucination_vault", "shared_person_authoring",
)

PUBLIC_TOOL_NAMES: tuple[str, ...] = (
    "stbrain_health",
    "stbrain_help",
    "remember_memory",
    "revise_memory",
    "advance_plan",
    "stbrain_open",
    "stbrain_open_direct",
    "submit_self_model_candidate",
    "activate_self_model_candidate",
    "query_self_model",
    "preview_person_reference_rewrite",
    "confirm_person_reference_rewrite",
    "remember_emotional_memory",
    "recall_emotional_memory",
    "revise_emotional_memory",
    "integrate_emotional_memories",
    "manage_brain_pin",
    "veto_ephemeral_memory",
    "remember_learning_memory",
    "remember_learning_contrast_pair",
    "recall_learning_memory",
    "revise_learning_memory",
    "integrate_learning_memories",
    "review_learning_change",
    "preview_learning_recall",
    "remember_tool_guidance",
    "recall_tool_guidance",
    "revise_tool_guidance",
    "review_tool_guidance_candidate",
    "record_tool_experience",
    "manage_self_governance_profile",
    "query_self_governance_profile",
    "manage_injection_control",
    "query_injection_control",
    "remember_planning_memory",
    "recall_planning_memory",
    "record_planning_event",
    "revise_planning_memory",
    "review_planning_change",
    "hold_hallucination_record",
    "open_hallucination_vault",
    "transfer_hallucination_record",
    "review_hallucination_restore",
)

SubmitIntent: TypeAlias = Literal[
    "acknowledge",
    "save_calm_prompt",
    "submit",
    "accept_review",
    "revise",
    "respond_to_objection",
    "withdraw",
    "begin_edit",
    "confirm_edit",
    "cancel_edit",
    "recover",
]

_SELF_MODEL_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "schemas" / "self-model.schema.json"
)
_PLANNING_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "schemas" / "planning-memory.schema.json"
)
_TOOL_GUIDANCE_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "schemas" / "tool-guidance.schema.json"
)
_HALLUCINATION_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "schemas" / "hallucination-vault.schema.json"
)


def _load_embedded_self_model_schema() -> dict[str, Any]:
    try:
        schema = json.loads(_SELF_MODEL_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("public self-model schema is unavailable") from exc
    # An embedded definition must not rebase local references through its own ID.
    schema.pop("$schema", None)
    schema.pop("$id", None)
    return schema


def _load_json_schema(path: Path, label: str) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"public {label} schema is unavailable") from exc


_PLANNING_SCHEMA = _load_json_schema(_PLANNING_SCHEMA_PATH, "planning")
_PLANNING_DEFS = _PLANNING_SCHEMA["$defs"]
_PLANNING_CONTENT_SCHEMA = copy.deepcopy(_PLANNING_DEFS["legacy_plan_content"])
_PLANNING_CALM_CHECK_INPUT_SCHEMA = copy.deepcopy(_PLANNING_DEFS["calm_check"])
_PLANNING_EVIDENCE_SCHEMA = copy.deepcopy(_PLANNING_DEFS["evidence_anchor"])
_TOOL_GUIDANCE_SCENARIO_TAGS_INPUT_SCHEMA = copy.deepcopy(
    _load_json_schema(_TOOL_GUIDANCE_SCHEMA_PATH, "tool guidance")["$defs"]["tagList"]
)
_TOOL_GUIDANCE_SCENARIO_TAGS_INPUT_SCHEMA.update(
    {
        "minItems": 1,
        "description": (
            "机器场景标签，例如 home.arrival；1–16 条且不可重复，每条 1–128 字符，"
            "以 ASCII 字母或数字开头，仅含 ASCII 字母、数字、点、下划线、连字符。"
            "中文场景叙述请写入 scenario_examples 或 keywords，不要填在本字段。"
        ),
    }
)
_HALLUCINATION_RECORD_SCHEMA = _load_json_schema(
    _HALLUCINATION_SCHEMA_PATH, "hallucination-vault"
)


def _inline_planning_input_schema(
    value: Any, *, active_refs: frozenset[str] = frozenset()
) -> Any:
    """Publish self-contained planning fields for property-only MCP clients.

    Some clients rebuild the root from type/properties/required and drop $defs.
    A copied property containing a local reference would then be impossible to
    validate.  Resolve the reviewed module definitions at publication time, not
    at invocation time, and keep the storage schema and all its constraints.
    """

    if isinstance(value, list):
        return [
            _inline_planning_input_schema(item, active_refs=active_refs)
            for item in value
        ]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    if "$ref" in value:
        reference = value["$ref"]
        prefix = "#/$defs/"
        if (
            not isinstance(reference, str)
            or not reference.startswith(prefix)
            or reference in active_refs
            or reference[len(prefix):] not in _PLANNING_DEFS
        ):
            raise RuntimeError("public planning schema has an unresolved reference")
        resolved = _inline_planning_input_schema(
            _PLANNING_DEFS[reference[len(prefix):]],
            active_refs=active_refs | {reference},
        )
        siblings = {key: item for key, item in value.items() if key != "$ref"}
        if siblings:
            # JSON Schema reference siblings are conjunctive, not overwrites.
            return {
                "allOf": [
                    resolved,
                    _inline_planning_input_schema(siblings, active_refs=active_refs),
                ]
            }
        return resolved
    return {
        key: _inline_planning_input_schema(item, active_refs=active_refs)
        for key, item in value.items()
    }


def _planning_content_input_properties() -> dict[str, Any]:
    properties = _inline_planning_input_schema(_PLANNING_CONTENT_SCHEMA["properties"])
    descriptions = {
        "reminder": (
            "由当前 AI 自己写的短提醒，最多 50 字符；track=internal 时必须非空，"
            "relational 时可以留空。"
        ),
        "ai_adoption_statement": (
            "当前 AI 自己决定采纳计划的声明，必须以‘我’、‘I’或‘My’开头，"
            "最多 500 字符；用户的请求本身不是 AI 的采纳声明。"
        ),
        "parent_ref": (
            "已落地、未隔离计划的当前精确 plan://plan_<32位小写十六进制>@版本 引用。"
            "goal 必须有 vision 父项，milestone 必须有 goal 父项；"
            "task 与 commitment 可独立创建，不必虚构父项。"
        ),
        "dependency_refs": (
            "已落地、未隔离计划的当前精确版本引用，最多 8 条、不可重复；"
            "没有依赖时省略或填写 []，不要填写 null，也不要填写待审核候选。"
        ),
        "scene_tags": "可为空的场景标签列表，支持中文；最多 16 条、每条 160 字符、不可重复。",
        "keywords": "可为空的关键词列表，支持中文；最多 16 条、每条 160 字符、不可重复。",
    }
    for field, description in descriptions.items():
        properties[field]["description"] = description
    return properties


_SELF_MODEL_CONTENT_SCHEMA = _load_embedded_self_model_schema()

_EMPTY_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "maxProperties": 0,
}

_CALM_PROMPT_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {
        "text": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000,
            "pattern": r"\S",
            "description": "AI 自己撰写的非空冷静词。",
        }
    },
}

_CANDIDATE_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["content", "reason"],
    "properties": {
        "content": copy.deepcopy(_SELF_MODEL_CONTENT_SCHEMA),
        "reason": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000,
            "pattern": r"\S",
            "description": (
                "AI 自己给出的非空候选理由；公开键名精确为 reason，绝不是 ai_reason。"
            ),
        },
    },
    "description": (
        "public-tools/16 的模块一候选载荷只有 content 与 reason。diff、evidence_refs "
        "和版本绑定由服务端从当前受控上下文派生，客户端不得提交。"
    ),
}

_REVIEW_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ai_confirmation"],
    "properties": {"ai_confirmation": {"const": True}},
}

_OBJECTION_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["response"],
    "properties": {
        "response": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4000,
            "pattern": r"\S",
        },
        "resolution": {
            "type": "string",
            "enum": ["continue", "withdraw"],
            "default": "continue",
        },
    },
}

_WITHDRAW_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reason"],
    "properties": {
        "reason": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000,
            "pattern": r"\S",
        }
    },
}

_CONFIRM_EDIT_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["challenge_id", "ai_confirmation"],
    "properties": {
        "challenge_id": {"type": "string", "minLength": 1, "pattern": r"\S"},
        "ai_confirmation": {"const": True},
    },
}

_RECOVER_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "automatic_state_signal": {
            "type": "string",
            "enum": ["grounded", "suspect", "frozen"],
            "default": "grounded",
        },
        "human_state_signal": {
            "type": "string",
            "enum": ["grounded", "suspect", "frozen"],
            "default": "grounded",
        },
    },
}


PUBLIC_INTENT_PAYLOAD_SCHEMAS: dict[str, dict[str, Any]] = {
    "acknowledge": _EMPTY_PAYLOAD_SCHEMA,
    "save_calm_prompt": _CALM_PROMPT_PAYLOAD_SCHEMA,
    "submit": _CANDIDATE_PAYLOAD_SCHEMA,
    "accept_review": _REVIEW_PAYLOAD_SCHEMA,
    "revise": _CANDIDATE_PAYLOAD_SCHEMA,
    "respond_to_objection": _OBJECTION_PAYLOAD_SCHEMA,
    "withdraw": _WITHDRAW_PAYLOAD_SCHEMA,
    "begin_edit": _EMPTY_PAYLOAD_SCHEMA,
    "confirm_edit": _CONFIRM_EDIT_PAYLOAD_SCHEMA,
    "cancel_edit": _EMPTY_PAYLOAD_SCHEMA,
    "recover": _RECOVER_PAYLOAD_SCHEMA,
}

STAGE_INTENT_ROUTES: dict[str, tuple[str, ...]] = {
    "factory": ("acknowledge",),
    "module_intro": ("acknowledge",),
    "calm_prompt_draft": ("save_calm_prompt",),
    "body_draft": ("submit",),
    "candidate_wait": (),
    "candidate_review": (
        "accept_review",
        "revise",
        "respond_to_objection",
        "withdraw",
    ),
    "live": ("begin_edit",),
    "edit_consent": ("confirm_edit", "cancel_edit"),
    "edit_body_draft": ("submit", "cancel_edit"),
    "draft_only_recovery": ("recover", "withdraw"),
}

_INTERNAL_ACTION_TO_PUBLIC_CALL: dict[str, tuple[str, str | None]] = {
    "confirm_brain_intro": ("submit_self_model_candidate", "acknowledge"),
    "confirm_module_intro": ("submit_self_model_candidate", "acknowledge"),
    "save_calm_prompt": ("submit_self_model_candidate", "save_calm_prompt"),
    "submit_candidate": ("submit_self_model_candidate", "submit"),
    "accept_candidate_review": ("submit_self_model_candidate", "accept_review"),
    "revise_candidate": ("submit_self_model_candidate", "revise"),
    "respond_to_objection": ("submit_self_model_candidate", "respond_to_objection"),
    "withdraw_candidate": ("submit_self_model_candidate", "withdraw"),
    "begin_edit": ("submit_self_model_candidate", "begin_edit"),
    "confirm_edit": ("submit_self_model_candidate", "confirm_edit"),
    "cancel_edit": ("submit_self_model_candidate", "cancel_edit"),
    "recover_candidate": ("submit_self_model_candidate", "recover"),
    "activate_candidate": ("activate_self_model_candidate", None),
}


def payload_schema_for_intent(intent: str) -> dict[str, Any]:
    """Return a defensive copy of one exact public payload schema."""

    try:
        schema = PUBLIC_INTENT_PAYLOAD_SCHEMAS[intent]
    except KeyError as exc:
        raise ValueError(f"unknown public submit intent: {intent}") from exc
    return copy.deepcopy(schema)


def _payload_ref(intent: str) -> dict[str, str]:
    return {"$ref": f"#/$defs/payload_{intent}"}


def _payload_is_optional(intent: str) -> bool:
    return intent in {"acknowledge", "begin_edit", "cancel_edit", "recover"}


def _build_submit_input_schema() -> dict[str, Any]:
    definitions: dict[str, Any] = {
        f"payload_{intent}": copy.deepcopy(schema)
        for intent, schema in PUBLIC_INTENT_PAYLOAD_SCHEMAS.items()
    }
    branches: list[dict[str, Any]] = []
    for intent in PUBLIC_INTENT_PAYLOAD_SCHEMAS:
        payload_contract: dict[str, Any]
        if _payload_is_optional(intent):
            payload_contract = {
                "anyOf": [{"type": "null"}, _payload_ref(intent)]
            }
        else:
            payload_contract = _payload_ref(intent)
        branch: dict[str, Any] = {
            "title": f"intent_{intent}",
            "properties": {
                "intent": {"const": intent},
                "payload": payload_contract,
            },
        }
        if not _payload_is_optional(intent):
            branch["required"] = ["payload"]
        branches.append(branch)

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "public-tools/16 submit_self_model_candidate arguments",
        "type": "object",
        "additionalProperties": False,
        "required": ["intent", "write_context_ref", "expected_row_version"],
        "properties": {
            "intent": {
                "type": "string",
                "enum": list(PUBLIC_INTENT_PAYLOAD_SCHEMAS),
            },
            "write_context_ref": {"type": "string", "minLength": 1},
            "expected_row_version": {"type": "integer", "minimum": 0},
            "payload": {"type": ["object", "null"]},
        },
        "allOf": [{"oneOf": branches}],
        "$defs": definitions,
    }


PUBLIC_SUBMIT_INPUT_SCHEMA = _build_submit_input_schema()

PUBLIC_ACTIVATE_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "public-tools/16 activate_self_model_candidate arguments",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "candidate_id",
        "write_context_ref",
        "expected_row_version",
        "expected_active_revision",
        "ai_confirmation",
    ],
    "properties": {
        "candidate_id": {"type": "string", "minLength": 1},
        "write_context_ref": {"type": "string", "minLength": 1},
        "expected_row_version": {"type": "integer", "minimum": 0},
        "expected_active_revision": {
            "anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}],
            "description": (
                "必须原样使用 stbrain_open 当前候选给出的 base_revision_id；"
                "首次激活时该值为 null，编辑候选激活时为当前活动修订 ID。"
            ),
        },
        "ai_confirmation": {
            "const": True,
            "description": "必须是 JSON boolean true；数字或字符串不构成确认。",
        },
    },
}

_CONTRAST_BASIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "subject_key",
        "scope_signature",
        "time_condition",
        "predicate_signature",
        "mutual_exclusivity_basis",
    ],
    "properties": {
        "subject_key": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000,
            "description": "两张卡共同讨论的同一主体。",
        },
        "scope_signature": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000,
            "description": "两种说法共同适用的同一范围。",
        },
        "time_condition": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000,
            "description": "两种说法共同适用的同一时间条件；无特殊限制时写 timeless。",
        },
        "predicate_signature": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000,
            "description": "被比较的同一谓词或问题，例如 蒜瓣毛的主要成因。",
        },
        "mutual_exclusivity_basis": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000,
            "description": "为什么两种结论在上述主体、范围和时间下不能同时为主要解释。",
        },
    },
}

_LEARNING_CALM_CHECK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "evidence_sufficient",
        "counterevidence_checked",
        "scope_changed",
        "affected_links_checked",
        "single_turn_pressure_absent",
        "rollback_understood",
        "notes",
        "evidence_refs",
    ],
    "properties": {
        "evidence_sufficient": {
            "type": "boolean",
            "const": True,
            "description": "现有证据足以提出或复核本次变化；必须是 JSON true。",
        },
        "counterevidence_checked": {
            "type": "boolean",
            "const": True,
            "description": "已检查可见反证和 contrast 链；必须是 JSON true。",
        },
        "scope_changed": {
            "type": "boolean",
            "description": (
                "本次是否改变适用主体、范围或时间条件。为 true 时，notes 必须说明新旧范围。"
            ),
        },
        "affected_links_checked": {
            "type": "boolean",
            "const": True,
            "description": "已检查受影响的知识与上下文关系；必须是 JSON true。",
        },
        "single_turn_pressure_absent": {
            "type": "boolean",
            "const": True,
            "description": "本次变化不是由单轮催促、强情绪或赶快完成的压力推动；必须是 JSON true。",
        },
        "rollback_understood": {
            "type": "boolean",
            "const": True,
            "description": "理解接受后仍保留旧版，并理解 rollback_ref 的作用；必须是 JSON true。",
        },
        "notes": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000,
            "pattern": r"\S",
            "description": "说明逐项检查结果和仍存疑处，不能只是冷静保证。",
        },
        "evidence_refs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 300,
                "pattern": r"\S",
            },
            "description": "本次检查依据的 1–16 个非空引用；不能只引用当前情绪。",
        },
    },
    "allOf": [
        {
            "if": {
                "required": ["scope_changed"],
                "properties": {"scope_changed": {"const": True}},
            },
            "then": {
                "properties": {
                    "notes": {
                        "pattern": r"([Ss][Cc][Oo][Pp][Ee]|范围)",
                    }
                }
            },
        }
    ],
    "description": (
        "学习脑语义级候选的固定复核对象。必须逐字段填写；不接受自由文本替代、缺键或额外键。"
    ),
}


def learning_calm_check_input_schema() -> dict[str, Any]:
    """Return the exact public learning calm-check schema for action contracts."""

    return copy.deepcopy(_LEARNING_CALM_CHECK_INPUT_SCHEMA)

_LEARNING_LINKS_INPUT_SCHEMA: dict[str, Any] = {
    "anyOf": [
        {
            "type": "array",
            "maxItems": 16,
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["relation_type", "target_ref", "basis"],
                        "properties": {
                            "relation_type": {"const": "contrast"},
                            "target_ref": {
                                "type": "string",
                                "pattern": r"^learning://[^@]+@[1-9][0-9]*$",
                                "description": "第一张现存卡返回的完整 item_ref。",
                            },
                            "weight": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 100,
                                "default": 50,
                            },
                            "basis": copy.deepcopy(_CONTRAST_BASIS_SCHEMA),
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["relation_type", "target_ref"],
                        "properties": {
                            "relation_type": {
                                "enum": [
                                    "supports", "difference", "related", "prerequisite",
                                    "generalizes", "specializes", "analogy", "applies_to",
                                    "tool_context", "emotional_context",
                                ]
                            },
                            "target_ref": {
                                "type": "string",
                                "pattern": r"^learning://[^@]+@[1-9][0-9]*$",
                            },
                            "weight": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 100,
                                "default": 50,
                            },
                            "basis": {
                                "type": "object",
                                "maxProperties": 12,
                                "propertyNames": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 80,
                                },
                                "additionalProperties": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 1000,
                                },
                            },
                        },
                    },
                ]
            },
        },
        {"type": "null"},
    ],
    "default": None,
    "description": (
        "显式知识关系。相同 scene_tags 只帮助召回，绝不会自动建链。"
        "保存第二张相反卡时，用 contrast 分支指向第一张卡的 item_ref。"
    ),
}

_LEARNING_NON_CONTRAST_LINKS_INPUT_SCHEMA: dict[str, Any] = copy.deepcopy(
    _LEARNING_LINKS_INPUT_SCHEMA
)
_LEARNING_NON_CONTRAST_LINKS_INPUT_SCHEMA["anyOf"][0]["items"] = copy.deepcopy(
    _LEARNING_LINKS_INPUT_SCHEMA["anyOf"][0]["items"]["oneOf"][1]
)
_LEARNING_NON_CONTRAST_LINKS_INPUT_SCHEMA["description"] = (
    "普通非对比知识关系。相反知识不能从这个入口分步创建；"
    "请使用 remember_learning_contrast_pair 原子保存两侧和对比边。"
)

_LEARNING_CONTRAST_CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title", "summary", "current_understanding", "source_basis",
        "confidence", "uncertainties",
    ],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 120},
        "summary": {"type": "string", "minLength": 1, "maxLength": 240},
        "current_understanding": {
            "type": "string", "minLength": 1, "maxLength": 2000,
        },
        "source_basis": {
            "enum": ["observed", "reported", "inferred"],
            "description": "只描述这侧说法的来源；相反性由 pair 关系表达。",
        },
        "confidence": {
            "type": "integer", "minimum": 0, "maximum": 60,
            "description": "尚未裁定的相反说法不得伪装成高确信结论。",
        },
        "uncertainties": {
            "type": "array", "minItems": 1, "maxItems": 8,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "steps": {
            "type": "array", "maxItems": 12,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "preceding_context_summary": {
            "type": "string", "maxLength": 240,
        },
    },
}

_REWRITE_MODULES = [
    "emotional_memory_module_two",
    "learning_memory_module_three",
    "tool_guidance_module",
]
_REWRITE_FIELD_PATHS = [
    "/original_text",
    "/summary",
    "/title",
    "/current_understanding",
    "/preceding_context_summary",
    "/display_label",
    "/completion_rule",
    "/purpose",
    "/call_notes",
    "/documentation_note",
    "/salience_reason",
    "/handoff_condition",
]
_REWRITE_FIELDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {
        path: {"type": "string"} for path in _REWRITE_FIELD_PATHS
    },
}
_REFERENT_BINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "field_path",
        "surface_form",
        "occurrence_index",
        "entity_ref",
        "resolution_status",
        "confidence",
    ],
    "properties": {
        "field_path": {"type": "string", "enum": _REWRITE_FIELD_PATHS},
        "surface_form": {"type": "string", "minLength": 1, "maxLength": 200},
        "occurrence_index": {"type": "integer", "minimum": 0},
        "entity_ref": {
            "anyOf": [{"type": "string", "minLength": 1, "maxLength": 300}, {"type": "null"}]
        },
        "resolution_status": {"enum": ["resolved", "unresolved", "ambiguous"]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    },
}
_REWRITE_TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "field_path",
        "surface_form",
        "occurrence_index",
        "entity_ref",
        "target_surface_form",
        "mention_kind",
        "target_alias_ref",
        "target_alias_version",
        "unique_in_scope",
    ],
    "properties": {
        "field_path": {"type": "string", "enum": _REWRITE_FIELD_PATHS},
        "surface_form": {"type": "string", "minLength": 1, "maxLength": 200},
        "occurrence_index": {"type": "integer", "minimum": 0},
        "entity_ref": {"type": "string", "minLength": 1, "maxLength": 300},
        "target_surface_form": {"type": "string", "minLength": 1, "maxLength": 80},
        "mention_kind": {"enum": ["pronoun", "person_name", "relationship_name"]},
        "target_alias_ref": {"type": "string", "minLength": 1, "maxLength": 300},
        "target_alias_version": {"type": "integer", "minimum": 1},
        "unique_in_scope": {"type": "boolean"},
    },
}
_PROTECTED_SPAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["field_path", "byte_start", "byte_end"],
    "properties": {
        "field_path": {"type": "string", "enum": _REWRITE_FIELD_PATHS},
        "byte_start": {"type": "integer", "minimum": 0},
        "byte_end": {"type": "integer", "minimum": 1},
    },
}

PUBLIC_REWRITE_PREVIEW_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "public-tools/16 preview_person_reference_rewrite arguments",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "write_context_ref",
        "expected_authoring_version",
        "module",
        "draft_version",
        "draft_fields",
        "referent_bindings",
        "rewrite_targets",
        "conversation_mode",
        "authenticated_participant_entity_ids",
        "alias_collision_scope",
        "alias_collision_scope_version",
        "protected_spans",
        "module_schema_version",
    ],
    "properties": {
        "write_context_ref": {"type": "string", "minLength": 1},
        "expected_authoring_version": {"type": "integer", "minimum": 0},
        "module": {"enum": _REWRITE_MODULES},
        "draft_version": {
            "type": "integer",
            "minimum": 0,
            "description": "本次源草稿的版本；源草稿或上下文改变后重新 preview，不复用旧预览。",
        },
        "draft_fields": copy.deepcopy(_REWRITE_FIELDS_SCHEMA),
        "referent_bindings": {
            "type": "array",
            "maxItems": 64,
            "items": copy.deepcopy(_REFERENT_BINDING_SCHEMA),
        },
        "rewrite_targets": {
            "type": "array",
            "maxItems": 64,
            "items": copy.deepcopy(_REWRITE_TARGET_SCHEMA),
        },
        "conversation_mode": {"enum": ["one_to_one", "group", "unknown"]},
        "authenticated_participant_entity_ids": {
            "type": "array",
            "maxItems": 32,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "alias_collision_scope": {"type": "string", "minLength": 1, "maxLength": 300},
        "alias_collision_scope_version": {"type": "integer", "minimum": 1},
        "protected_spans": {
            "type": "array",
            "maxItems": 128,
            "items": copy.deepcopy(_PROTECTED_SPAN_SCHEMA),
        },
        "module_schema_version": {"type": "string", "minLength": 1},
        "rewrite_eligible_allowlist_version": {
            "const": "person-rewrite-allowlist/1",
            "default": "person-rewrite-allowlist/1",
        },
        "mention_parser_rule_version": {
            "const": "literal-person-reference/1",
            "default": "literal-person-reference/1",
        },
        "alias_comparison_profile_version": {
            "const": "alias-comparison/nfc-casefold/1",
            "default": "alias-comparison/nfc-casefold/1",
        },
    },
}

PUBLIC_REWRITE_CONFIRM_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "public-tools/16 confirm_person_reference_rewrite arguments",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "write_context_ref",
        "expected_authoring_version",
        "module",
        "preview_id",
        "expected_source_draft_hash",
        "expected_suggestion_hash",
        "expected_validation_context_hash",
        "final_fields",
        "final_fields_hash",
        "conversation_mode",
        "authenticated_participant_entity_ids",
        "alias_collision_scope",
        "alias_collision_scope_version",
        "protected_spans",
        "module_schema_version",
        "ai_confirmation",
    ],
    "properties": {
        "write_context_ref": {"type": "string", "minLength": 1},
        "expected_authoring_version": {"type": "integer", "minimum": 0},
        "module": {"enum": _REWRITE_MODULES},
        "preview_id": {"type": "string", "pattern": "^rwprev_[0-9a-f]{32}$"},
        "expected_source_draft_hash": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
            "description": (
                "精确匹配该 preview 保存的 source_draft_hash；它只证明预览源快照一致，"
                "不能证明窗口中的源草稿此后未改变。源草稿、版本或验证上下文变化时，"
                "必须重新 preview，不要把旧 hash 当成当前草稿未变的证明。"
            ),
        },
        "expected_suggestion_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "expected_validation_context_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "final_fields": copy.deepcopy(_REWRITE_FIELDS_SCHEMA),
        "final_fields_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "conversation_mode": {"enum": ["one_to_one", "group", "unknown"]},
        "authenticated_participant_entity_ids": {
            "type": "array",
            "maxItems": 32,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "alias_collision_scope": {"type": "string", "minLength": 1, "maxLength": 300},
        "alias_collision_scope_version": {"type": "integer", "minimum": 1},
        "protected_spans": {
            "type": "array",
            "maxItems": 128,
            "items": copy.deepcopy(_PROTECTED_SPAN_SCHEMA),
        },
        "module_schema_version": {"type": "string", "minLength": 1},
        "rewrite_eligible_allowlist_version": {
            "const": "person-rewrite-allowlist/1",
            "default": "person-rewrite-allowlist/1",
        },
        "mention_parser_rule_version": {
            "const": "literal-person-reference/1",
            "default": "literal-person-reference/1",
        },
        "alias_comparison_profile_version": {
            "const": "alias-comparison/nfc-casefold/1",
            "default": "alias-comparison/nfc-casefold/1",
        },
        "ai_confirmation": {"const": True},
    },
}


class PublicPayloadValidationError(ValueError):
    """One non-secret, field-addressed public payload contract failure."""

    def __init__(self, issues: Sequence[Mapping[str, Any]]) -> None:
        self.issues = tuple(dict(issue) for issue in issues)
        super().__init__("public payload does not match its intent contract")


def _string_issue(
    value: Any,
    *,
    path: str,
    maximum: int | None = None,
) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return {
            "path": path,
            "code": "required_string",
            "expected": "non-empty string",
        }
    if maximum is not None and len(value.strip()) > maximum:
        return {
            "path": path,
            "code": "string_too_long",
            "expected": {"type": "string", "maxLength": maximum},
        }
    return None


def validate_public_payload(
    intent: str,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate the exact outer payload shape and return a shallow normalized copy.

    Deep self-model validation remains the runtime's responsibility.  This guard
    exists to reject guessed wrapper/container names and to name ``reason``
    precisely before the payload reaches that runtime.
    """

    if intent not in PUBLIC_INTENT_PAYLOAD_SCHEMAS:
        raise PublicPayloadValidationError(
            [{"path": "intent", "code": "unknown_intent", "expected": list(PUBLIC_INTENT_PAYLOAD_SCHEMAS)}]
        )

    if payload is None:
        body: dict[str, Any] = {}
    elif isinstance(payload, Mapping):
        body = dict(payload)
    else:
        raise PublicPayloadValidationError(
            [{"path": "payload", "code": "invalid_type", "expected": "object"}]
        )

    schema = PUBLIC_INTENT_PAYLOAD_SCHEMAS[intent]
    properties = set(schema.get("properties", {}))
    required = set(schema.get("required", []))
    issues: list[dict[str, Any]] = []
    for key in sorted(set(body) - properties):
        issues.append(
            {
                "path": f"payload.{key}",
                "code": "unexpected_property",
                "expected": sorted(properties),
            }
        )
    for key in sorted(required - set(body)):
        issues.append(
            {
                "path": f"payload.{key}",
                "code": "missing_reason" if key == "reason" else "required",
                "expected": schema["properties"][key],
            }
        )

    if intent == "save_calm_prompt" and "text" in body:
        issue = _string_issue(body["text"], path="payload.text", maximum=2000)
        if issue:
            issues.append(issue)
    if intent in {"submit", "revise"}:
        if "content" in body and not isinstance(body["content"], Mapping):
            issues.append(
                {"path": "payload.content", "code": "invalid_type", "expected": "object"}
            )
        if "reason" in body:
            issue = _string_issue(body["reason"], path="payload.reason", maximum=2000)
            if issue:
                issues.append(issue)
    if intent == "accept_review" and body.get("ai_confirmation") is not True:
        issues.append(
            {"path": "payload.ai_confirmation", "code": "required_true", "expected": True}
        )
    if intent == "respond_to_objection":
        if "response" in body:
            issue = _string_issue(body["response"], path="payload.response", maximum=4000)
            if issue:
                issues.append(issue)
        if "resolution" in body and body["resolution"] not in {"continue", "withdraw"}:
            issues.append(
                {
                    "path": "payload.resolution",
                    "code": "invalid_enum",
                    "expected": ["continue", "withdraw"],
                }
            )
    if intent == "withdraw" and "reason" in body:
        issue = _string_issue(body["reason"], path="payload.reason", maximum=2000)
        if issue:
            issues.append(issue)
    if intent == "confirm_edit":
        if "challenge_id" in body:
            issue = _string_issue(body["challenge_id"], path="payload.challenge_id")
            if issue:
                issues.append(issue)
        if body.get("ai_confirmation") is not True:
            issues.append(
                {"path": "payload.ai_confirmation", "code": "required_true", "expected": True}
            )
    if intent == "recover":
        for key in ("automatic_state_signal", "human_state_signal"):
            if key in body and body[key] not in {"grounded", "suspect", "frozen"}:
                issues.append(
                    {
                        "path": f"payload.{key}",
                        "code": "invalid_enum",
                        "expected": ["grounded", "suspect", "frozen"],
                    }
                )

    if issues:
        raise PublicPayloadValidationError(issues)
    return body


def current_action_contract(
    stage: str,
    *,
    allowed_actions: Sequence[str] | None = None,
    base_revision_id: str | None = None,
) -> dict[str, Any]:
    """Build the public, stage-exact call contract returned by ``stbrain_open``."""

    calls: list[dict[str, Any]] = []
    if allowed_actions is None:
        public_calls = [
            ("submit_self_model_candidate", intent)
            for intent in STAGE_INTENT_ROUTES.get(stage, ())
        ]
    else:
        public_calls = [
            _INTERNAL_ACTION_TO_PUBLIC_CALL[action]
            for action in allowed_actions
            if action in _INTERNAL_ACTION_TO_PUBLIC_CALL
        ]

    for tool_name, intent in public_calls:
        if tool_name == "activate_self_model_candidate":
            calls.append(
                {
                    "tool": tool_name,
                    "argument_schema": copy.deepcopy(PUBLIC_ACTIVATE_INPUT_SCHEMA),
                    "required_arguments": list(PUBLIC_ACTIVATE_INPUT_SCHEMA["required"]),
                    "argument_sources": {
                        "candidate_id": "$.continuation.candidate.candidate_id",
                        "write_context_ref": "$.write_context_ref",
                        "expected_row_version": "$.row_version",
                        "expected_active_revision": (
                            "$.continuation.candidate.base_revision_id"
                        ),
                    },
                    "expected_active_revision": base_revision_id,
                    "first_activation": base_revision_id is None,
                    "required_confirmation": {"ai_confirmation": True},
                    "unknown_arguments": "rejected",
                }
            )
            continue
        assert intent is not None
        call: dict[str, Any] = {
            "tool": tool_name,
            "intent": intent,
            "payload_schema": payload_schema_for_intent(intent),
            "argument_sources": {
                "write_context_ref": "$.write_context_ref",
                "expected_row_version": "$.row_version",
            },
            "unknown_payload_keys": "rejected",
        }
        if intent in {"submit", "revise"}:
            call["server_derived_fields"] = [
                "diff",
                "evidence_refs",
                "expected_active_revision",
            ]
            call["base_revision_id"] = base_revision_id
        calls.append(call)

    return {
        "contract_version": PUBLIC_CONTRACT_VERSION,
        "stage": stage,
        "allowed_calls": calls,
    }


def install_submit_input_schema(mcp: Any) -> None:
    """Install the exact root schema while retaining FastMCP's flat call signature.

    MCP Python SDK 1.29 builds function schemas from individual parameters and
    cannot express the required cross-parameter intent discriminator without an
    extra request wrapper.  The reviewed deployment pins that SDK version, so we
    replace only the registered tool's published parameters after registration;
    invocation still uses the original flat function metadata.
    """

    manager = getattr(mcp, "_tool_manager", None)
    tool = manager.get_tool("submit_self_model_candidate") if manager else None
    if tool is None:
        raise RuntimeError("submit_self_model_candidate must be registered before schema install")
    tool.parameters = copy.deepcopy(PUBLIC_SUBMIT_INPUT_SCHEMA)


def install_public_tool_input_contracts(mcp: Any) -> None:
    """Make the published schemas and FastMCP runtime reject unknown arguments.

    FastMCP's generated Pydantic argument models ignore extra fields by default.
    Merely publishing ``additionalProperties: false`` therefore is not an
    enforcement boundary.  Rebuild every public tool's runtime model with
    ``extra=forbid`` and value-free validation errors, then install the two
    reviewed hand-authored write schemas.
    """

    manager = getattr(mcp, "_tool_manager", None)
    if manager is None:
        raise RuntimeError("FastMCP tool manager is unavailable")

    for name in PUBLIC_TOOL_NAMES:
        tool = manager.get_tool(name)
        if tool is None:
            raise RuntimeError(f"public tool must be registered before contract install: {name}")
        argument_model = tool.fn_metadata.arg_model
        argument_model.model_config.update(
            extra="forbid",
            hide_input_in_errors=True,
        )
        argument_model.model_rebuild(force=True)
        parameters = copy.deepcopy(tool.parameters)
        parameters["additionalProperties"] = False
        tool.parameters = parameters

    manager.get_tool("submit_self_model_candidate").parameters = copy.deepcopy(
        PUBLIC_SUBMIT_INPUT_SCHEMA
    )
    manager.get_tool("activate_self_model_candidate").parameters = copy.deepcopy(
        PUBLIC_ACTIVATE_INPUT_SCHEMA
    )
    manager.get_tool("preview_person_reference_rewrite").parameters = copy.deepcopy(
        PUBLIC_REWRITE_PREVIEW_INPUT_SCHEMA
    )
    manager.get_tool("confirm_person_reference_rewrite").parameters = copy.deepcopy(
        PUBLIC_REWRITE_CONFIRM_INPUT_SCHEMA
    )
    for name in ("remember_tool_guidance", "revise_tool_guidance"):
        parameters = copy.deepcopy(manager.get_tool(name).parameters)
        tags = copy.deepcopy(_TOOL_GUIDANCE_SCENARIO_TAGS_INPUT_SCHEMA)
        parameters["properties"]["scenario_tags"] = (
            tags
            if name == "remember_tool_guidance"
            else {
                "anyOf": [tags, {"type": "null"}],
                "default": None,
                "description": "省略或 null 表示不修改；提供新标签时遵循机器场景标签格式。",
            }
        )
        parameters["properties"]["risk_level"]["description"] = (
            "由当前 AI 评估风险。real_world_action 的运行时下限为 high，"
            "不是一律要求 critical；high 与 critical 都必须搭配 explicit_each_time。"
        )
        parameters["properties"]["confirmation_policy"]["description"] = (
            "real_world_action 必须使用 explicit_each_time；high/critical 同样如此。"
            "工具卡只保存历史建议，不授予执行权限；提交 major 候选也不绕过运行时下限。"
        )
        manager.get_tool(name).parameters = parameters
    for name in ("remember_learning_memory", "revise_learning_memory"):
        parameters = copy.deepcopy(manager.get_tool(name).parameters)
        parameters["properties"]["links"] = copy.deepcopy(
            _LEARNING_NON_CONTRAST_LINKS_INPUT_SCHEMA
            if name == "remember_learning_memory"
            else _LEARNING_LINKS_INPUT_SCHEMA
        )
        manager.get_tool(name).parameters = parameters
    pair_parameters = copy.deepcopy(
        manager.get_tool("remember_learning_contrast_pair").parameters
    )
    pair_parameters["properties"]["first_claim"] = copy.deepcopy(
        _LEARNING_CONTRAST_CLAIM_SCHEMA
    )
    pair_parameters["properties"]["second_claim"] = copy.deepcopy(
        _LEARNING_CONTRAST_CLAIM_SCHEMA
    )
    pair_parameters["properties"]["contrast_basis"] = copy.deepcopy(
        _CONTRAST_BASIS_SCHEMA
    )
    pair_parameters["properties"]["scene_tags"]["minItems"] = 2
    pair_parameters["properties"]["application_contexts"]["minItems"] = 1
    manager.get_tool("remember_learning_contrast_pair").parameters = pair_parameters
    for name in (
        "revise_learning_memory",
        "integrate_learning_memories",
        "review_learning_change",
    ):
        parameters = copy.deepcopy(manager.get_tool(name).parameters)
        parameters["properties"]["calm_check"] = copy.deepcopy(
            _LEARNING_CALM_CHECK_INPUT_SCHEMA
        )
        manager.get_tool(name).parameters = parameters

    # Planning uses flat public calls for ease of use while the runtime stores
    # one exact content object.  Inline its reviewed definitions so clients that
    # retain only root type/properties/required cannot strand local references.
    planning_content_properties = _planning_content_input_properties()
    remember_plan = copy.deepcopy(manager.get_tool("remember_planning_memory").parameters)
    remember_plan.pop("$defs", None)
    for field, schema in planning_content_properties.items():
        if field in remember_plan["properties"]:
            remember_plan["properties"][field] = copy.deepcopy(schema)
    remember_plan["properties"]["calm_check"] = copy.deepcopy(
        _PLANNING_CALM_CHECK_INPUT_SCHEMA
    )
    manager.get_tool("remember_planning_memory").parameters = remember_plan

    revise_plan = copy.deepcopy(manager.get_tool("revise_planning_memory").parameters)
    revise_plan.pop("$defs", None)
    revise_plan["properties"]["calm_check"] = copy.deepcopy(
        _PLANNING_CALM_CHECK_INPUT_SCHEMA
    )
    revise_plan["properties"]["changes"] = {
        "anyOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "minProperties": 1,
                "properties": copy.deepcopy(planning_content_properties),
            },
            {"type": "null"},
        ],
        "default": None,
    }
    manager.get_tool("revise_planning_memory").parameters = revise_plan

    for parameters in (remember_plan, revise_plan):
        parameters["properties"]["idempotency_key"].update(
            {
                "maxLength": 200,
                "description": (
                    "当前 AI 为一次提交自行生成的非空幂等键，最多 200 字符。"
                    "完全相同的请求重试复用此键；正文、版本或唤醒改变后使用新键。"
                ),
            }
        )
        parameters["properties"]["ai_confirmation"]["const"] = True

    review_plan = copy.deepcopy(manager.get_tool("review_planning_change").parameters)
    review_plan["properties"]["calm_check"] = copy.deepcopy(
        _PLANNING_CALM_CHECK_INPUT_SCHEMA
    )
    manager.get_tool("review_planning_change").parameters = review_plan

    event_plan = copy.deepcopy(manager.get_tool("record_planning_event").parameters)
    event_plan["properties"]["evidence"] = {
        "type": "array",
        "maxItems": 16,
        "items": copy.deepcopy(_PLANNING_EVIDENCE_SCHEMA),
    }
    manager.get_tool("record_planning_event").parameters = event_plan

    ordinary_revision = manager.get_tool("revise_memory")
    ordinary_revision.parameters["properties"]["changes"] = {
        "type": "object", "minProperties": 1, "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
            "summary": {"type": "string", "minLength": 1, "maxLength": 240,
                        "description": "learning: up to 240 characters; emotion/planning: up to 200."},
            "domain": {"type": "string", "maxLength": 160},
            "keywords": {"type": "array", "maxItems": 24,
                         "items": {"type": "string", "minLength": 1, "maxLength": 200}},
            "entities": {"type": "array", "maxItems": 24,
                         "items": {"type": "string", "minLength": 1, "maxLength": 200}},
            "importance": {"type": "integer", "minimum": 0, "maximum": 100},
        },
        "description": "Only changed ordinary fields. title: learning/planning; entities: emotion/learning; domain: learning. Planning keywords: at most 16 items, 160 chars each; other lists: 24 items, 200 chars each. Other fields use the module's advanced interface.",
    }
    ordinary_event = manager.get_tool("advance_plan")
    ordinary_event.parameters["properties"]["evidence"] = {
        "anyOf": [{"type": "array", "maxItems": 16,
                   "items": copy.deepcopy(_PLANNING_EVIDENCE_SCHEMA)}, {"type": "null"}],
        "default": None,
    }

    # Vault bodies use the isolated record schema.  Intent conditionals turn
    # missing fields into immediate, field-addressed tool validation instead of
    # a long sequence of server retries.
    vault_fields = _HALLUCINATION_RECORD_SCHEMA["properties"]
    hold_vault = copy.deepcopy(manager.get_tool("hold_hallucination_record").parameters)
    for field in (
        "neutral_title", "isolated_content", "current_account", "basis",
        "reflection", "uncertainty_status",
    ):
        schema = copy.deepcopy(vault_fields[field])
        if field != "reflection":
            schema = {"anyOf": [schema, {"type": "null"}], "default": None}
        hold_vault["properties"][field] = schema
    hold_vault["properties"]["warning_text"] = {
        "anyOf": [
            {"type": "string", "minLength": 1, "maxLength": 2000},
            {"type": "null"},
        ],
        "default": None,
    }
    hold_vault["properties"]["warning_suffix"] = {
        "anyOf": [
            {"type": "string", "minLength": 1, "maxLength": 1000},
            {"type": "null"},
        ],
        "default": None,
    }
    hold_vault["allOf"] = [
        {
            "if": {"properties": {"intent": {"const": "record"}}},
            "then": {
                "required": [
                    "neutral_title", "isolated_content", "current_account", "basis"
                ]
            },
        },
        {
            "if": {"properties": {"intent": {"const": "update_warning"}}},
            "then": {
                "required": [
                    "expected_warning_version", "warning_text", "warning_suffix"
                ]
            },
        },
    ]
    manager.get_tool("hold_hallucination_record").parameters = hold_vault

    transfer_vault = copy.deepcopy(
        manager.get_tool("transfer_hallucination_record").parameters
    )
    for field in (
        "neutral_title", "isolated_content", "current_account", "basis",
        "reflection", "uncertainty_status",
    ):
        schema = copy.deepcopy(vault_fields[field])
        if field != "reflection":
            schema = {"anyOf": [schema, {"type": "null"}], "default": None}
        transfer_vault["properties"][field] = schema
    transfer_vault["allOf"] = [
        {
            "if": {"properties": {"intent": {"const": "preview"}}},
            "then": {"required": ["source_ref", "expected_source_row_version"]},
        },
        {
            "if": {"properties": {"intent": {"const": "commit"}}},
            "then": {
                "required": [
                    "expected_vault_version", "expected_source_row_version",
                    "journal_id", "expected_preview_hash", "neutral_title",
                    "isolated_content", "current_account", "basis", "reason",
                    "ai_confirmation",
                ],
                "properties": {"ai_confirmation": {"const": True}},
            },
        },
        {
            "if": {"properties": {"intent": {"const": "propose_restore"}}},
            "then": {
                "required": [
                    "expected_vault_version", "record_id", "expected_record_version",
                    "destination_module", "destination_row_version", "reason",
                    "ai_confirmation",
                ],
                "properties": {"ai_confirmation": {"const": True}},
            },
        },
    ]
    manager.get_tool("transfer_hallucination_record").parameters = transfer_vault
