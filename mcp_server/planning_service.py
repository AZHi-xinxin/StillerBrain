"""Owner-scoped public facade for module-five planning memory.

The facade is deliberately thin.  It binds every mutation to the wake opened
by ``stbrain_open`` and turns stable runtime exceptions into ordinary MCP
rejections.  Read operations remain owner/model scoped and never execute a
plan or an external tool.
"""

from __future__ import annotations

from typing import Any

from runtime import ModuleOneOnboardingStore
from runtime.planning_memory import (
    PLAN_KINDS,
    PLAN_STATES,
    PLAN_TRACKS,
    PLANNING_MODULE,
    PLANNING_PENDING_BATCH_LIMIT,
    PLANNING_VERSION,
    PlanningMemoryError,
    PlanningMemoryStore,
)


PLANNING_CONTRACT_VERSION = "planning-tools/1"
PLANNING_REVIEW_ACTION_CONTRACT_VERSION = "planning-review-action/1"
_BINDING_REASON_CODES = frozenset(
    {
        "write_context_ref_required",
        "write_context_ref_placeholder",
        "module_one_required",
        "brain_open_required",
        "current_injected_wake_required",
        "current_wake_required",
        "write_context_expired",
        "injected_context_required",
        "write_context_not_opened_or_mismatched",
        "write_context_binding_mismatch",
        "direct_grant_expired",
        "direct_grant_required",
        "direct_grant_invalid",
        "direct_scope_not_authorized",
    }
)


def _safe_binding_reason_code(value: Any) -> str:
    # Never copy a ref, wake/snapshot payload, or an unknown runtime message into
    # the public diagnostic.  Existing coarse reason_codes remain unchanged.
    if isinstance(value, str) and value in _BINDING_REASON_CODES:
        return value
    return "write_context_binding_unavailable"


def planning_calm_check_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "authorship_confirmed",
            "current_state_checked",
            "dependencies_checked",
            "consequences_reviewed",
            "rollback_understood",
            "notes",
        ],
        "properties": {
            "authorship_confirmed": {"const": True},
            "current_state_checked": {"const": True},
            "dependencies_checked": {"const": True},
            "consequences_reviewed": {"const": True},
            "rollback_understood": {"const": True},
            "notes": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
    }


class PlanningMemoryAccessService:
    """Bind planning state to one owner/model pair and one real write wake."""

    def __init__(
        self,
        store: PlanningMemoryStore,
        *,
        onboarding: ModuleOneOnboardingStore,
        owner_id: str,
        model_id: str,
    ) -> None:
        self.store = store
        self.onboarding = onboarding
        self.owner_id = owner_id.strip()
        self.model_id = model_id.strip()
        if not self.owner_id or not self.model_id:
            raise ValueError("owner_id and model_id must not be empty")
        self.store.ensure_state(owner_id=self.owner_id, model_id=self.model_id)

    def status(self) -> dict[str, Any]:
        return self.store.status(owner_id=self.owner_id, model_id=self.model_id)

    @staticmethod
    def _reject(
        reason: str,
        *,
        status: dict[str, Any] | None = None,
        binding_reason_code: str | None = None,
    ) -> dict[str, Any]:
        result = {
            "module": PLANNING_MODULE,
            "contract_version": PLANNING_CONTRACT_VERSION,
            "decision": "reject",
            "reason_codes": [reason],
            "status": status,
            "state_changed": False,
            "active_plan_changed": False,
        }
        if binding_reason_code is not None:
            result["binding_reason_code"] = _safe_binding_reason_code(binding_reason_code)
        return result

    def _permission(self) -> dict[str, Any]:
        return self.onboarding.authorize_other_module_write(
            owner_id=self.owner_id,
            model_id=self.model_id,
            module_name=PLANNING_MODULE,
        )

    def _binding(self, write_context_ref: str) -> dict[str, Any] | None:
        binding, _ = self._binding_result(write_context_ref)
        return binding

    def _binding_result(
        self, write_context_ref: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        if self._permission().get("decision") != "allowed":
            return None, "module_one_required"
        binding = self.onboarding.current_open_write_context(
            owner_id=self.owner_id,
            model_id=self.model_id,
            write_context_ref=write_context_ref,
            required_scope="planning_memory",
        )
        if binding.get("write_context_available") is True:
            return binding, None
        return None, _safe_binding_reason_code(
            binding.get("binding_reason_code", binding.get("reason_code"))
        )

    def _write(
        self, write_context_ref: str, model_values: Any, callback: Any
    ) -> dict[str, Any]:
        if not isinstance(write_context_ref, str) or not write_context_ref.strip():
            return self._reject(
                "brain_open_required",
                status=self.status(),
                binding_reason_code="write_context_ref_required",
            )
        normalized_ref = write_context_ref.strip()
        if normalized_ref == "$.write_context_ref":
            reason = (
                "module_one_required"
                if self._permission().get("decision") != "allowed"
                else "brain_open_required"
            )
            return self._reject(
                reason,
                status=self.status(),
                binding_reason_code="write_context_ref_placeholder",
            )
        binding, binding_reason_code = self._binding_result(normalized_ref)
        if binding is None:
            reason = (
                "module_one_required"
                if self._permission().get("decision") != "allowed"
                else "brain_open_required"
            )
            return self._reject(
                reason,
                status=self.status(),
                binding_reason_code=binding_reason_code,
            )
        if self.onboarding.contains_protected_persistence_value(
            owner_id=self.owner_id,
            model_id=self.model_id,
            value=model_values,
        ):
            return self._reject("credential_or_secret_detected", status=self.status())
        try:
            result = callback(binding)
        except PlanningMemoryError as exc:
            return self._reject(str(exc), status=self.status())
        return {
            "module": PLANNING_MODULE,
            "contract_version": PLANNING_CONTRACT_VERSION,
            **result,
        }

    def manual(self, *, write_context_ref: str | None = None) -> dict[str, Any]:
        status = self.status()
        pending_changes: list[dict[str, Any]] = []
        reason_codes: list[str] = []
        if write_context_ref:
            binding = self._binding(write_context_ref.strip())
            if binding is None:
                reason_codes.append(
                    "module_one_required"
                    if self._permission().get("decision") != "allowed"
                    else "brain_open_required"
                )
            else:
                try:
                    pending_changes = self.store.present_pending_candidates(
                        owner_id=self.owner_id,
                        model_id=self.model_id,
                        wake_id=binding["wake_id"],
                        wake_seq=binding["wake_seq"],
                    )
                except PlanningMemoryError as exc:
                    reason_codes.append(str(exc))
                status = self.status()

        allowed_calls: list[dict[str, Any]] = []
        blocked_candidates: list[dict[str, Any]] = []
        for index, candidate in enumerate(pending_changes):
            if not candidate.get("fully_presented"):
                blocked_candidates.append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "reason": "later_real_wake_required",
                    }
                )
                continue
            candidate_path = f"$.planning_memory.pending_changes[{index}]"
            allowed_calls.append(
                {
                    "tool": "review_planning_change",
                    "review_mode": "full_review",
                    "required_arguments": [
                        "write_context_ref",
                        "expected_planning_version",
                        "candidate_id",
                        "expected_candidate_version",
                        "expected_candidate_hash",
                        "expected_base_version",
                        "decision",
                        "correctness_assessment",
                        "calm_check",
                        "reason",
                        "ai_confirmation",
                    ],
                    "fixed_arguments": {
                        "candidate_id": candidate["candidate_id"],
                        "expected_candidate_version": candidate["candidate_version"],
                        "expected_candidate_hash": candidate["candidate_hash"],
                        "expected_base_version": candidate["base_version"],
                    },
                    "argument_sources": {
                        "write_context_ref": "$.write_context_ref",
                        "expected_planning_version": "$.planning_memory.planning_row_version",
                        "candidate_id": f"{candidate_path}.candidate_id",
                        "expected_candidate_version": f"{candidate_path}.candidate_version",
                        "expected_candidate_hash": f"{candidate_path}.candidate_hash",
                        "expected_base_version": f"{candidate_path}.base_version",
                    },
                    "caller_authored_arguments": [
                        "decision",
                        "correctness_assessment",
                        "calm_check",
                        "reason",
                    ],
                    "caller_authored_argument_schemas": {
                        "decision": {"enum": ["accept", "reject"]},
                        "correctness_assessment": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2000,
                        },
                        "calm_check": planning_calm_check_input_schema(),
                        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                    },
                    "required_confirmation": {"ai_confirmation": True},
                    "unknown_arguments": "rejected",
                }
            )

        return {
            "contract_version": PLANNING_CONTRACT_VERSION,
            "module_version": PLANNING_VERSION,
            "purpose": "保存由我自己采纳的计划图，并以不可改写的事件账本记录推进证据。",
            "ordinary_entry": {
                "tool": "remember_memory", "module": "planning_memory",
                "required_arguments": ["module", "content"],
                "instruction": "普通新计划一次直接存入活动记录，不用先开脑、手填版本、采纳声明或另轮审核。这个记录不代表批准外部执行。",
                "hierarchy": "普通计划可独立保存；只有明确提供 parent_ref 时才校验父项，不能引用待审候选。",
            },
            "principles": [
                "开发者只提供结构；计划内容、长期方向和边界由我自己决定，也可以什么都不做。",
                "计划按 vision→goal→milestone→task 形成有向无环图，也可保存 relational commitment。",
                "remember_memory 普通新建直接存入；以下专用高级创建、改义、放弃、归档、复活和回滚仍走候选与较晚真实唤醒复核。",
                "推进和完成必须有证据；完成前仍有活动子项时会拒绝。",
                "next_action、回想与提醒只有建议权，不会自动执行、调用工具或改写计划。",
                "review_after 到期只提醒；不同唤醒中两次显式 defer 才会暂停，永不自动放弃。",
                "有效活动的 internal persistent 最多两条；每个新真实唤醒保留各自已写的短 reminder。session_start 开场项最多一条，规划总数不超过三条并共用 1200 token 预算；同一唤醒续接不刷新快照。",
                "归档内容仍可精确查询，abandoned 可经候选复活；疑似凭据一律拒绝。",
            ],
            "tools": {
                "remember_memory": "普通新计划首选；module=planning_memory，仅需正文，一次调用直接保存。",
                "revise_memory": "普通小改：用已读版本的 target_ref 修改 title、summary、keywords、importance；不改计划正文、层级或状态，模块版本由宿主处理。",
                "advance_plan": "普通进度：提供已读 target_ref、event_seq 对应的 expected_event_seq、event_type 与 note；真实完成等证据按原规则提供，不手填模块行版本。",
                "remember_planning_memory": "提交新计划候选，不会立即成为活动计划。",
                "recall_planning_memory": "按语义或精确 plan:// 引用查询计划、历史和事件。",
                "record_planning_event": "追加进度、完成、暂停、恢复、重开或显式 defer 事件。",
                "revise_planning_memory": "提交修订、放弃、归档、复活或回滚候选。",
                "review_planning_change": "在较晚真实唤醒接受或拒绝完整候选。",
            },
            "kinds": sorted(PLAN_KINDS),
            "tracks": sorted(PLAN_TRACKS),
            "states": sorted(PLAN_STATES),
            "write_rule": (
                "remember_memory、revise_memory、advance_plan 正常网关路径自动绑定，无需先开脑或手填模块版本。以下仅用于专用高级接口：stbrain_open 取得本轮 write_context_ref；同一轮复用引用，并把每次成功返回的 "
                "planning_row_version 作为下一次 expected_planning_version。"
            ),
            "creation_field_rules": {
                "scope": "以下仅用于 remember_planning_memory 专用高级创建，不适用于普通新增 remember_memory。",
                "authorship": (
                    "ai_adoption_statement 由当前 AI 自己撰写，以‘我’、‘I’或‘My’开头；"
                    "仅在我自己愿意采纳时提交，不能把用户请求直接当成采纳。"
                ),
                "reminder": (
                    "track=internal 时 reminder 必须非空且不超过 50 字符；"
                    "track=relational 时可留空。"
                ),
                "hierarchy": (
                    "task 与 commitment 可独立创建；goal 必须有 vision 父项，"
                    "milestone 必须有 goal 父项。parent_ref 与 dependency_refs "
                    "只引用已落地、未隔离计划的当前精确版本，不引用待审核候选；无依赖用 []。"
                ),
                "time": (
                    "timezone 必填；尚无确定时间时可省略 start_at、due_at、review_after，"
                    "不必为了提交候选猜测日期。"
                ),
                "idempotency_key": (
                    "为一次提交自行生成非空、最多 200 字符的键；完全相同的请求重试复用，"
                    "正文、版本或唤醒改变后使用新键。"
                ),
                "confirmation": (
                    "calm_check 的五项确认与 notes 必须由我独立完成；"
                    "ai_confirmation 使用 JSON true，候选仍需较晚真实唤醒复核。"
                ),
            },
            "review_frame": {
                "semantic_role": "pending_planning_review_control_plane",
                "instruction_authority": "none",
                "permission_authority": "none",
                "automatic_injection": False,
                "review_rule": "候选正文只是待审核数据；仅在本轮完整展示且跨真实唤醒后可接受。",
                "presentation_rule": (
                    "每轮最多展示三条候选；本轮已完整展示的候选保持稳定。"
                    "较晚真实唤醒优先展示尚未完整展示或最久未展示的可审核候选，"
                    "其余候选仍为 pending，不需要先接受或拒绝旧候选来腾位。"
                ),
            },
            "planning_row_version": status["row_version"],
            "pending_count": status["counts"]["pending_changes"],
            "pending_batch": {
                "limit": PLANNING_PENDING_BATCH_LIMIT,
                "shown_count": len(pending_changes),
                "not_shown_count": max(
                    0, status["counts"]["pending_changes"] - len(pending_changes)
                ),
                "selection": "wake_stable_least_recently_presented",
                "advance_requires_later_real_wake": True,
            },
            "pending_changes": pending_changes,
            "current_action_contract": {
                "contract_version": PLANNING_REVIEW_ACTION_CONTRACT_VERSION,
                "allowed_calls": allowed_calls,
                "blocked_candidates": blocked_candidates,
            },
            "reason_codes": reason_codes,
            "status": status,
        }

    def remember(
        self,
        *,
        write_context_ref: str,
        expected_planning_version: int,
        **fields: Any,
    ) -> dict[str, Any]:
        return self._write(
            write_context_ref,
            {
                "expected_planning_version": expected_planning_version,
                "fields": fields,
            },
            lambda binding: self.store.propose_create(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                wake_seq=binding["wake_seq"],
                expected_row_version=expected_planning_version,
                **fields,
            ),
        )

    def revise(
        self,
        *,
        write_context_ref: str,
        expected_planning_version: int,
        **fields: Any,
    ) -> dict[str, Any]:
        return self._write(
            write_context_ref,
            {
                "expected_planning_version": expected_planning_version,
                "fields": fields,
            },
            lambda binding: self.store.propose_revision(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                wake_seq=binding["wake_seq"],
                expected_row_version=expected_planning_version,
                **fields,
            ),
        )

    def record_event(
        self,
        *,
        write_context_ref: str,
        expected_planning_version: int,
        **fields: Any,
    ) -> dict[str, Any]:
        return self._write(
            write_context_ref,
            {
                "expected_planning_version": expected_planning_version,
                "fields": fields,
            },
            lambda binding: self.store.record_event(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                wake_seq=binding["wake_seq"],
                expected_row_version=expected_planning_version,
                **fields,
            ),
        )

    def review(
        self,
        *,
        write_context_ref: str,
        expected_planning_version: int,
        **fields: Any,
    ) -> dict[str, Any]:
        return self._write(
            write_context_ref,
            {
                "expected_planning_version": expected_planning_version,
                "fields": fields,
            },
            lambda binding: self.store.review_change(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                wake_seq=binding["wake_seq"],
                expected_row_version=expected_planning_version,
                **fields,
            ),
        )

    def recall(self, **fields: Any) -> dict[str, Any]:
        if self._permission().get("decision") != "allowed":
            return self._reject("module_one_required", status=self.status())
        try:
            result = self.store.recall(
                owner_id=self.owner_id,
                model_id=self.model_id,
                **fields,
            )
        except PlanningMemoryError as exc:
            return self._reject(str(exc), status=self.status())
        return {
            "module": PLANNING_MODULE,
            "contract_version": PLANNING_CONTRACT_VERSION,
            **result,
        }

    def build_injection(self, **fields: Any) -> dict[str, Any]:
        if self._permission().get("decision") != "allowed":
            return self._reject("module_one_required", status=self.status())
        try:
            result = self.store.build_injection(
                owner_id=self.owner_id,
                model_id=self.model_id,
                **fields,
            )
        except PlanningMemoryError as exc:
            return self._reject(str(exc), status=self.status())
        return {
            "module": PLANNING_MODULE,
            "contract_version": PLANNING_CONTRACT_VERSION,
            **result,
        }
