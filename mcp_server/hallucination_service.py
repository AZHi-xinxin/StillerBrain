"""Owner-scoped facade for the physically isolated hallucination vault."""

from __future__ import annotations

from typing import Any

from runtime.hallucination_vault import (
    HALLUCINATION_VAULT_CONTRACT_VERSION,
    HALLUCINATION_VAULT_MODULE,
    HallucinationVaultError,
    HallucinationVaultStore,
)


HALLUCINATION_RESTORE_ACTION_CONTRACT_VERSION = "hallucination-restore-action/0.1"


class HallucinationVaultAccessService:
    """Bind all vault mutations to an explicitly opened real AI wake."""

    def __init__(
        self,
        store: HallucinationVaultStore,
        *,
        onboarding: Any,
        owner_id: str,
        model_id: str,
    ) -> None:
        self.store = store
        self.onboarding = onboarding
        self.owner_id = owner_id.strip()
        self.model_id = model_id.strip()
        if not self.owner_id or not self.model_id:
            raise ValueError("owner_id and model_id must not be empty")

    def status(self) -> dict[str, Any]:
        return self.store.status(owner_id=self.owner_id, model_id=self.model_id)

    @staticmethod
    def _reject(reason: str, *, status: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "module": HALLUCINATION_VAULT_MODULE,
            "contract_version": HALLUCINATION_VAULT_CONTRACT_VERSION,
            "decision": "reject",
            "reason_codes": [reason],
            "status": status,
            "state_changed": False,
            "pointer_changed": False,
        }

    def _permission(self) -> dict[str, Any]:
        return self.onboarding.authorize_other_module_write(
            owner_id=self.owner_id,
            model_id=self.model_id,
            module_name=HALLUCINATION_VAULT_MODULE,
        )

    def _binding(self, write_context_ref: str) -> dict[str, Any] | None:
        if self._permission().get("decision") != "allowed":
            return None
        binding = self.onboarding.current_open_write_context(
            owner_id=self.owner_id,
            model_id=self.model_id,
            write_context_ref=write_context_ref,
            required_scope="hallucination_vault",
        )
        return binding if binding.get("write_context_available") is True else None

    def _write(
        self, write_context_ref: str, model_values: Any, callback: Any
    ) -> dict[str, Any]:
        if not isinstance(write_context_ref, str) or not write_context_ref.strip():
            return self._reject("brain_open_required", status=self.status())
        binding = self._binding(write_context_ref.strip())
        if binding is None:
            reason = (
                "module_one_required"
                if self._permission().get("decision") != "allowed"
                else "brain_open_required"
            )
            return self._reject(reason, status=self.status())
        if self.onboarding.contains_protected_persistence_value(
            owner_id=self.owner_id,
            model_id=self.model_id,
            value=model_values,
        ):
            return self._reject("credential_or_secret_detected", status=self.status())
        try:
            result = callback(binding)
        except HallucinationVaultError as exc:
            return self._reject(str(exc), status=self.status())
        except TypeError:
            # Public tools are intentionally flat and several intents share one
            # facade.  A missing/irrelevant intent field is an ordinary
            # transport rejection, never an unhandled server error.
            return self._reject("invalid_arguments", status=self.status())
        return {
            "module": HALLUCINATION_VAULT_MODULE,
            "contract_version": HALLUCINATION_VAULT_CONTRACT_VERSION,
            **result,
        }

    def manual(self, *, write_context_ref: str | None = None) -> dict[str, Any]:
        status = self.status()
        pending = self.store.pending_restore_candidates(
            owner_id=self.owner_id, model_id=self.model_id
        )
        blocked: list[dict[str, Any]] = []
        allowed: list[dict[str, Any]] = []
        binding = self._binding(write_context_ref.strip()) if write_context_ref else None
        current_wake_seq = int(binding["wake_seq"]) if binding is not None else None
        for index, candidate in enumerate(pending):
            path = f"$.hallucination_vault.pending_restore_candidates[{index}]"
            if current_wake_seq is None or current_wake_seq <= candidate["created_wake_seq"]:
                blocked.append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "reason": "later_real_wake_required",
                    }
                )
                continue
            allowed.append(
                {
                    "tool": "review_hallucination_restore",
                    "review_mode": "open_record_then_review",
                    "required_precondition": (
                        "In this wake, call open_hallucination_vault for the record and "
                        "repeat the current AI-authored warning before activation."
                    ),
                    "fixed_arguments": {
                        "candidate_id": candidate["candidate_id"],
                        "expected_candidate_version": candidate["candidate_version"],
                        "expected_candidate_hash": candidate["candidate_hash"],
                        "expected_base_record_version": candidate["base_record_version"],
                        "expected_destination_row_version": candidate[
                            "destination_row_version"
                        ],
                    },
                    "argument_sources": {
                        "write_context_ref": "$.write_context_ref",
                        "expected_vault_version": "$.hallucination_vault.vault_row_version",
                        "candidate_id": f"{path}.candidate_id",
                        "expected_candidate_version": f"{path}.candidate_version",
                        "expected_candidate_hash": f"{path}.candidate_hash",
                        "expected_base_record_version": f"{path}.base_record_version",
                    },
                    "allowed_action_values": ["activate", "reject", "withdraw"],
                    "caller_authored_arguments": ["action", "reason"],
                    "required_confirmation": {"ai_confirmation": True},
                    "unknown_arguments": "rejected",
                }
            )
        return {
            "contract_version": HALLUCINATION_VAULT_CONTRACT_VERSION,
            "module": HALLUCINATION_VAULT_MODULE,
            "purpose": (
                "这是我可以主动选择使用的隔离存储；它保存我自己判断应当隔离的内容，"
                "不替我判断真假，也不会自动提醒我什么是幻觉。"
            ),
            "principles": [
                "默认 hard_off：零自动注入、零自动提醒、零普通检索或索引。",
                "服务端只检查结构、来源绑定、版本与迁移完整性；真假判断只由我作出。",
                "目录只显示六项中性元数据；正文一次只开一条，并先回填我自己写的警示词。",
                "迁移是转移而非静默删除：普通位置只留 redirect，正文与关系快照进入独立 Vault。",
                "解封先形成候选，必须跨新的真实外部唤醒、重新开正文后才可激活。",
                "我可以保存、迁移、查询、解封、更新警示词，也可以完全不使用。",
            ],
            "tools": {
                "hold_hallucination_record": "原子配置安全带并保存首条，或保存后续条目/更新警示词。",
                "open_hallucination_vault": "无 ID 看安全目录；带 ID 先看警示；回填警示后一次看一条正文。",
                "transfer_hallucination_record": "预检并原子迁移普通记忆；当前支持 learning://，未知模块故障关闭。",
                "review_hallucination_restore": "在较晚真实唤醒激活、拒绝或撤回解封候选。",
            },
            "write_rule": (
                "所有写入先调用 stbrain_open，复用本轮 write_context_ref，并使用上一成功结果"
                "返回的 vault_row_version 作为下一次 expected_vault_version。"
            ),
            "automatic_injection": False,
            "automatic_exposure": status["automatic_exposure"],
            "vault_row_version": status["row_version"],
            "warning_version": status["warning_version"],
            "pending_restore_candidates": pending,
            "current_action_contract": {
                "contract_version": HALLUCINATION_RESTORE_ACTION_CONTRACT_VERSION,
                "allowed_calls": allowed,
                "blocked_candidates": blocked,
            },
            "status": status,
        }

    def hold(
        self,
        *,
        write_context_ref: str,
        expected_vault_version: int,
        intent: str = "record",
        **fields: Any,
    ) -> dict[str, Any]:
        if intent not in {"record", "update_warning"}:
            return self._reject("invalid_hold_intent", status=self.status())
        if intent == "record":
            return self._write(
                write_context_ref,
                {
                    "expected_vault_version": expected_vault_version,
                    "intent": intent,
                    "fields": fields,
                },
                lambda binding: self.store.hold(
                    owner_id=self.owner_id,
                    model_id=self.model_id,
                    wake_id=binding["wake_id"],
                    wake_seq=binding["wake_seq"],
                    expected_row_version=expected_vault_version,
                    **fields,
                ),
            )
        return self._write(
            write_context_ref,
            {
                "expected_vault_version": expected_vault_version,
                "intent": intent,
                "fields": fields,
            },
            lambda binding: self.store.update_warning(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                expected_row_version=expected_vault_version,
                **fields,
            ),
        )

    def open(
        self,
        *,
        write_context_ref: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        if self._permission().get("decision") != "allowed":
            return self._reject("module_one_required", status=self.status())
        binding = None
        if write_context_ref is not None:
            binding = self._binding(write_context_ref.strip())
            if binding is None:
                return self._reject("brain_open_required", status=self.status())
            if self.onboarding.contains_protected_persistence_value(
                owner_id=self.owner_id,
                model_id=self.model_id,
                value=fields,
            ):
                return self._reject(
                    "credential_or_secret_detected", status=self.status()
                )
        try:
            result = self.store.open(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"] if binding is not None else None,
                wake_seq=binding["wake_seq"] if binding is not None else None,
                **fields,
            )
        except HallucinationVaultError as exc:
            return self._reject(str(exc), status=self.status())
        except TypeError:
            return self._reject("invalid_arguments", status=self.status())
        return {
            "module": HALLUCINATION_VAULT_MODULE,
            "contract_version": HALLUCINATION_VAULT_CONTRACT_VERSION,
            **result,
        }

    def transfer(
        self,
        *,
        write_context_ref: str,
        intent: str,
        expected_vault_version: int | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        if intent not in {"preview", "commit", "propose_restore"}:
            return self._reject("invalid_transfer_intent", status=self.status())
        if intent == "preview":
            return self._write(
                write_context_ref,
                {
                    "expected_vault_version": expected_vault_version,
                    "intent": intent,
                    "fields": fields,
                },
                lambda _binding: self.store.preview_transfer(
                    owner_id=self.owner_id, model_id=self.model_id, **fields
                ),
            )
        if expected_vault_version is None:
            return self._reject("expected_vault_version_required", status=self.status())
        if intent == "commit":
            return self._write(
                write_context_ref,
                {
                    "expected_vault_version": expected_vault_version,
                    "intent": intent,
                    "fields": fields,
                },
                lambda binding: self.store.commit_transfer(
                    owner_id=self.owner_id,
                    model_id=self.model_id,
                    wake_id=binding["wake_id"],
                    wake_seq=binding["wake_seq"],
                    expected_row_version=expected_vault_version,
                    **fields,
                ),
            )
        return self._write(
            write_context_ref,
            {
                "expected_vault_version": expected_vault_version,
                "intent": intent,
                "fields": fields,
            },
            lambda binding: self.store.propose_restore(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                wake_seq=binding["wake_seq"],
                expected_row_version=expected_vault_version,
                **fields,
            ),
        )

    def review_restore(
        self,
        *,
        write_context_ref: str,
        expected_vault_version: int,
        **fields: Any,
    ) -> dict[str, Any]:
        return self._write(
            write_context_ref,
            {
                "expected_vault_version": expected_vault_version,
                "fields": fields,
            },
            lambda binding: self.store.review_restore(
                owner_id=self.owner_id,
                model_id=self.model_id,
                wake_id=binding["wake_id"],
                wake_seq=binding["wake_seq"],
                expected_row_version=expected_vault_version,
                **fields,
            ),
        )
