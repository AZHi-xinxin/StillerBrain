"""Host-side binding for native tool calls crossing the RikkaHub gateway.

The gateway is not a generic tool executor.  This module only validates the
model-authored native call against the exact schema advertised in the same
request, binds its immutable hashes to the current wake and later witnesses a
matching tool-result message.  It never fills, rewrites, or executes arguments.

An optional policy adapter can add tool-brain or product-specific permission
checks.  The default transport boundary does not claim that authentication,
human confirmation, or operation success was verified; those facts need an
adapter at the actual executor/UI boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable, Collection, Mapping, Protocol, Sequence

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from referencing.exceptions import Unresolvable


EXECUTION_BINDING_CONTRACT = "native-tool-execution-binding/1"
HOST_RECEIPT_CONTRACT = "native-tool-host-receipt/1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# These are diagnostic labels, not an author-field allowlist. Unknown tools and
# schema properties still validate normally; only their diagnostic field labels
# are redacted. A schema property can itself contain private data, so accepting
# every syntactically valid ASCII identifier would not be a safe log boundary.
_DIAGNOSTIC_FIELDS = frozenset({
    "action", "text", "enabled", "mode", "module", "query", "limit", "cursor",
    "target_ref", "changes", "write_context_ref", "execution_ref", "reason",
    "memory_ref", "item_ref", "plan_ref", "tool_ref", "card_ref", "source_ref",
    "memory_id", "item_id", "plan_id", "card_id", "expected_version",
    "expected_state_version", "expected_event_seq", "target_version", "version",
    "original_text", "memory_type", "source_timestamp", "summary", "primary_emotion",
    "secondary_emotions", "importance", "sensitivity", "context_policy", "origin",
    "confidence", "keywords", "entities", "referent_bindings", "recall_mode",
    "allow_contexts", "deny_contexts", "default_decision", "explicit_request_override",
    "disclosure", "lifecycle", "kind", "title", "current_understanding", "steps",
    "application_contexts", "scene_tags", "preceding_context_summary", "uncertainties",
    "domain", "source_basis", "claim_review", "time_sensitivity", "valid_as_of",
    "review_after", "track", "reminder", "presence_mode", "start_at", "due_at",
    "timezone", "allow_coordination_hint", "parent_ref", "dependency_refs",
    "ai_adoption_statement", "tool_name", "operation_key", "field_name", "before_text",
    "after_text", "display_label", "documentation_note", "purpose", "use_when",
    "avoid_when", "scenario_tags", "scenario_examples", "call_notes", "aliases",
    "capability_class", "risk_level", "confirmation_policy", "completion_rule",
    "critical_preconditions", "linked_tool_refs", "chain_role", "handoff_condition",
    "related_refs", "source_type", "additional_source_refs", "salience", "auto_recall_mode",
    "salience_reason", "expires_at", "intent", "edit_class", "correctness_assessment",
    "calm_check_stability", "calm_check_necessity", "calm_check_consequences",
    "calm_check_alternatives", "clear_fields", "content", "context", "metadata",
    "facets", "key", "value", "triggers", "situation", "scene", "bindings",
    "speaker", "referent", "occurrence", "scope", "scopes", "status", "options",
})
_DIAGNOSTIC_VALIDATORS = frozenset({
    "type", "required", "additionalProperties", "unevaluatedProperties", "properties",
    "patternProperties", "propertyNames", "enum", "const", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
    "pattern", "format", "minItems", "maxItems", "uniqueItems", "items", "prefixItems",
    "additionalItems", "unevaluatedItems", "contains", "minContains", "maxContains",
    "minProperties", "maxProperties", "dependentRequired", "dependentSchemas",
    "dependencies", "allOf", "anyOf", "oneOf", "not", "if", "then", "else", "$ref",
})
_DIAGNOSTIC_PATH_MARKERS = frozenset({"<field>", "[]", "<truncated>"})


def _diagnostic_field(value: Any) -> str:
    return value if type(value) is str and value in _DIAGNOSTIC_FIELDS else "<field>"


def _schema_field_path(error: ValidationError) -> list[str]:
    """Project schema structure, never instance paths or arbitrary map keys."""

    path: list[str] = []
    parts = iter(error.absolute_schema_path)
    for part in parts:
        if part == "properties":
            path.append(_diagnostic_field(next(parts, None)))
        elif part in {"patternProperties", "$defs", "definitions"}:
            next(parts, None)  # A regex/definition label may contain private data.
            if part == "patternProperties":
                path.append("<field>")
        elif part in {"additionalProperties", "unevaluatedProperties", "propertyNames"}:
            path.append("<field>")
        elif part in {"items", "prefixItems", "additionalItems", "unevaluatedItems", "contains"}:
            path.append("[]")
        if len(path) >= 10:
            path.append("<truncated>")
            break
    return path


def _validation_detail(error: ValidationError) -> dict[str, Any]:
    validator = error.validator
    detail: dict[str, Any] = {
        "validator": validator if type(validator) is str and validator in _DIAGNOSTIC_VALIDATORS else "other",
        "field_path": _schema_field_path(error),
    }
    if validator == "required" and isinstance(error.schema, Mapping) and isinstance(error.instance, Mapping):
        required = error.schema.get("required")
        if isinstance(required, list):
            missing: list[str] = []
            for field in required[:32]:
                # Membership is used only to identify absence. Never copy the
                # instance, actual keys, values, schema text or error message.
                if type(field) is str and field not in error.instance:
                    label = _diagnostic_field(field)
                    if label not in missing:
                        missing.append(label)
                    if len(missing) >= 6:
                        break
            if missing:
                detail["required_fields"] = missing
    return detail


def _validation_diagnostic(tool_name: str, error: ValidationError) -> dict[str, Any]:
    detail = _validation_detail(error)
    detail["tool_name"] = tool_name
    if error.validator in {"oneOf", "anyOf"}:
        branches: list[dict[str, Any]] = []
        # Bound both traversal and output. No recursive flattening of arbitrary
        # branch schemas, enum alternatives or validator messages is performed.
        for child in error.context[:16]:
            branch = _validation_detail(child)
            if branch not in branches:
                branches.append(branch)
            if len(branches) >= 4:
                break
        if branches:
            detail["branch_errors"] = branches
    return detail


def normalize_validation_diagnostic(
    value: Any, *, allowed_tool_names: Collection[str] | None = None
) -> dict[str, Any] | None:
    """Keep the exception's optional metadata value-free even for future callers.

    The gateway supplies names from its current advertised catalog, then scans
    the entire result against current protected values and omits the whole
    metadata object on any collision. Syntax alone cannot authenticate a tool
    name supplied by an adapter. Internal exception copies omit that optional
    membership check; only bind_call builds diagnostics from validated calls.
    """

    if not isinstance(value, Mapping):
        return None
    tool_name = value.get("tool_name")
    if type(tool_name) is not str or _TOOL_NAME.fullmatch(tool_name) is None:
        return None
    if allowed_tool_names is not None and tool_name not in allowed_tool_names:
        return None

    def copy_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
        validator = detail.get("validator")
        result: dict[str, Any] = {
            "validator": validator if type(validator) is str and validator in _DIAGNOSTIC_VALIDATORS else "other",
            "field_path": [],
        }
        fields = detail.get("field_path")
        if isinstance(fields, (list, tuple)):
            result["field_path"] = [
                item if type(item) is str and item in _DIAGNOSTIC_PATH_MARKERS else _diagnostic_field(item)
                for item in fields[:11]
            ]
        required = detail.get("required_fields")
        if isinstance(required, (list, tuple)) and required:
            result["required_fields"] = list(dict.fromkeys(_diagnostic_field(item) for item in required[:6]))
        return result

    result = copy_detail(value)
    result["tool_name"] = tool_name
    branches = value.get("branch_errors")
    if result["validator"] in {"oneOf", "anyOf"} and isinstance(branches, (list, tuple)):
        cleaned = [copy_detail(branch) for branch in branches[:4] if isinstance(branch, Mapping)]
        if cleaned:
            result["branch_errors"] = cleaned
    return result


class ToolExecutionBoundaryError(RuntimeError):
    """A stable, value-free boundary rejection."""

    def __init__(self, code: str, *, validation_diagnostic: Mapping[str, Any] | None = None) -> None:
        super().__init__(code)
        self._validation_diagnostic = (
            normalize_validation_diagnostic(validation_diagnostic)
            if code == "tool_arguments_schema_invalid" else None
        )

    @property
    def validation_diagnostic(self) -> dict[str, Any] | None:
        # Give consumers an independent, re-sanitized projection. They cannot
        # accidentally append raw error data to metadata held by the exception.
        return normalize_validation_diagnostic(self._validation_diagnostic)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _object_without_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ToolExecutionBoundaryError("tool_arguments_duplicate_key")
        result[key] = value
    return result


def parse_arguments(arguments: Any) -> dict[str, Any]:
    """Parse one model-authored argument object without accepting ambiguity."""

    if not isinstance(arguments, str):
        raise ToolExecutionBoundaryError("tool_arguments_must_be_json_string")
    try:
        value = json.loads(
            arguments,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ToolExecutionBoundaryError("tool_arguments_non_finite_number")
            ),
        )
    except ToolExecutionBoundaryError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ToolExecutionBoundaryError("tool_arguments_invalid_json") from exc
    if not isinstance(value, dict):
        raise ToolExecutionBoundaryError("tool_arguments_must_be_object")
    # Re-encoding catches non-JSON objects returned by unusual decoder hooks.
    try:
        canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise ToolExecutionBoundaryError("tool_arguments_not_canonicalizable") from exc
    return value


def advertised_schemas(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Return exact schemas from the native request, failing on ambiguity.

    Descriptions and other function metadata are deliberately ignored.  The
    returned schema objects are read only for validation and are never stored in
    tool memory or receipts.
    """

    definitions: list[Any] = []
    if "tools" in payload:
        tools = payload.get("tools")
        if not isinstance(tools, list):
            raise ToolExecutionBoundaryError("advertised_tool_catalog_incomplete")
        for tool in tools:
            if (
                not isinstance(tool, Mapping)
                or tool.get("type") != "function"
                or not isinstance(tool.get("function"), Mapping)
            ):
                raise ToolExecutionBoundaryError("advertised_tool_catalog_incomplete")
            definitions.append(tool["function"])
    if "functions" in payload:
        functions = payload.get("functions")
        if not isinstance(functions, list):
            raise ToolExecutionBoundaryError("advertised_tool_catalog_incomplete")
        definitions.extend(functions)

    result: dict[str, Mapping[str, Any]] = {}

    def has_external_reference(value: Any) -> bool:
        if isinstance(value, Mapping):
            reference = value.get("$ref")
            if isinstance(reference, str) and not reference.startswith("#"):
                return True
            return any(has_external_reference(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(has_external_reference(item) for item in value)
        return False

    for definition in definitions:
        if not isinstance(definition, Mapping):
            raise ToolExecutionBoundaryError("advertised_tool_catalog_incomplete")
        name = definition.get("name")
        schema = definition.get("parameters")
        if (
            not isinstance(name, str)
            or _TOOL_NAME.fullmatch(name) is None
            or name in result
            or not isinstance(schema, Mapping)
        ):
            raise ToolExecutionBoundaryError("advertised_tool_catalog_incomplete")
        if has_external_reference(schema):
            # Schema validation is an execution boundary and must not perform
            # network retrieval or depend on a mutable remote document.
            raise ToolExecutionBoundaryError("advertised_tool_schema_remote_ref_forbidden")
        try:
            Draft202012Validator.check_schema(dict(schema))
        except SchemaError as exc:
            raise ToolExecutionBoundaryError("advertised_tool_schema_invalid") from exc
        result[name] = schema
    return result


@dataclass(frozen=True)
class NativeToolCall:
    tool_call_id: str
    tool_name: str
    arguments_text: str


@dataclass(frozen=True)
class ToolExecutionBinding:
    contract: str
    wake_id: str
    tool_call_id: str
    tool_name: str
    catalog_hash: str
    schema_hash: str
    arguments_hash: str
    issued_at_ms: int

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ToolExecutionPolicyDecision:
    decision: str
    reason_codes: tuple[str, ...]
    managed_card_id: str | None = None


class ToolExecutionPolicyAdapter(Protocol):
    """Optional product policy at the real host/executor boundary.

    ``authorize`` runs before a tool call is released to the external client.
    ``classify_result`` may label a result only when the adapter understands the
    target provider's response contract.  Generic callers must return
    ``unknown``.  ``observe_receipt`` receives hashes only, never raw arguments
    or raw results.
    """

    def authorize(
        self,
        binding: ToolExecutionBinding,
        *,
        catalog: Mapping[str, Any],
    ) -> ToolExecutionPolicyDecision: ...

    def classify_result(
        self,
        binding: ToolExecutionBinding,
        *,
        tool_message: Mapping[str, Any],
    ) -> str: ...

    def observe_receipt(self, receipt: Mapping[str, Any]) -> None: ...


class NativeTransportPolicy:
    """Content-neutral default: validate transport, claim no extra authority."""

    def authorize(
        self,
        binding: ToolExecutionBinding,
        *,
        catalog: Mapping[str, Any],
    ) -> ToolExecutionPolicyDecision:
        return ToolExecutionPolicyDecision(
            decision="allowed_to_attempt",
            reason_codes=("native_schema_validated", "target_authorization_not_claimed"),
        )

    def classify_result(
        self,
        binding: ToolExecutionBinding,
        *,
        tool_message: Mapping[str, Any],
    ) -> str:
        return "unknown"

    def observe_receipt(self, receipt: Mapping[str, Any]) -> None:
        return


class ToolGuidancePolicyAdapter:
    """Testable bridge from the gateway boundary to the tool-brain gate.

    A provider at the actual client/UI boundary must supply the three current
    facts.  This adapter never infers user intent, account authorization, or a
    confirmation from prose.  With no provider, managed cards therefore fail
    closed.  Tools that have no matching active card remain native/unmanaged;
    tool memory is not a universal permission proxy.
    """

    def __init__(
        self,
        store: Any,
        *,
        owner_id: str,
        model_id: str,
        signal_provider: Callable[[ToolExecutionBinding], Mapping[str, Any]] | None = None,
        result_classifier: Callable[
            [ToolExecutionBinding, Mapping[str, Any]], str
        ]
        | None = None,
        receipt_sink: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.owner_id = owner_id
        self.model_id = model_id
        self.signal_provider = signal_provider
        self.result_classifier = result_classifier
        self.receipt_sink = receipt_sink

    def authorize(
        self,
        binding: ToolExecutionBinding,
        *,
        catalog: Mapping[str, Any],
    ) -> ToolExecutionPolicyDecision:
        recalled = self.store.recall(
            owner_id=self.owner_id,
            model_id=self.model_id,
            query="",
            tool_name=binding.tool_name,
            view="card",
            limit=5,
            catalog=catalog,
        )
        matching = [
            item
            for item in recalled.get("results", [])
            if item.get("schema_status") == "matched"
            and item.get("lifecycle") == "active"
        ]
        if not matching:
            return ToolExecutionPolicyDecision(
                decision="allowed_to_attempt",
                reason_codes=("tool_guidance_not_managed", "native_schema_validated"),
            )
        if len(matching) != 1:
            return ToolExecutionPolicyDecision(
                decision="denied",
                reason_codes=("tool_guidance_operation_ambiguous",),
            )
        card_id = matching[0].get("card_id")
        if not isinstance(card_id, str):
            return ToolExecutionPolicyDecision(
                decision="denied",
                reason_codes=("tool_guidance_card_invalid",),
            )
        signals = self.signal_provider(binding) if self.signal_provider else {}
        required = (
            "current_user_intent",
            "authorization_verified",
            "current_confirmation",
        )
        if set(signals) != set(required) or not all(
            isinstance(signals.get(key), bool) for key in required
        ):
            return ToolExecutionPolicyDecision(
                decision="denied",
                reason_codes=("host_execution_signals_required",),
                managed_card_id=card_id,
            )
        gate = self.store.execution_gate(
            owner_id=self.owner_id,
            model_id=self.model_id,
            card_id=card_id,
            catalog=catalog,
            current_user_intent=signals["current_user_intent"],
            authorization_verified=signals["authorization_verified"],
            current_confirmation=signals["current_confirmation"],
        )
        return ToolExecutionPolicyDecision(
            decision=str(gate.get("decision", "denied")),
            reason_codes=tuple(str(code) for code in gate.get("reason_codes", [])),
            managed_card_id=card_id,
        )

    def classify_result(
        self,
        binding: ToolExecutionBinding,
        *,
        tool_message: Mapping[str, Any],
    ) -> str:
        if self.result_classifier is None:
            return "unknown"
        status = self.result_classifier(binding, tool_message)
        if status not in {"accepted", "succeeded", "failed", "partial", "unknown"}:
            raise ToolExecutionBoundaryError("invalid_host_result_classification")
        return status

    def observe_receipt(self, receipt: Mapping[str, Any]) -> None:
        if self.receipt_sink is not None:
            self.receipt_sink(receipt)


class HostExecutionBoundary:
    """Bind calls and produce signed, result-body-free host receipts."""

    def __init__(
        self,
        secret: bytes,
        *,
        policy: ToolExecutionPolicyAdapter | None = None,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("execution receipt secret must contain at least 32 bytes")
        # Reuse the existing host secret only through a fixed domain-separated
        # derivation.  No new deploy-time credential is required and neither the
        # derived key nor receipt signature enters model-visible context.
        self._secret = hmac.new(
            secret,
            b"stiller-brain/native-tool-host-receipt/1",
            hashlib.sha256,
        ).digest()
        self.policy = policy or NativeTransportPolicy()

    @staticmethod
    def _catalog_entry(catalog: Mapping[str, Any], tool_name: str) -> str:
        if catalog.get("contract") != "advertised-tools/1":
            raise ToolExecutionBoundaryError("advertised_tool_catalog_incomplete")
        if catalog.get("catalog_complete") is not True:
            raise ToolExecutionBoundaryError("advertised_tool_catalog_incomplete")
        catalog_hash = catalog.get("catalog_hash")
        if not isinstance(catalog_hash, str) or _SHA256.fullmatch(catalog_hash) is None:
            raise ToolExecutionBoundaryError("advertised_tool_catalog_incomplete")
        matches = [
            entry
            for entry in catalog.get("entries", [])
            if isinstance(entry, Mapping) and entry.get("canonical_name") == tool_name
        ]
        if len(matches) != 1:
            raise ToolExecutionBoundaryError("tool_not_advertised")
        schema_hash = matches[0].get("schema_hash")
        if not isinstance(schema_hash, str) or _SHA256.fullmatch(schema_hash) is None:
            raise ToolExecutionBoundaryError("advertised_tool_catalog_incomplete")
        return schema_hash

    def bind_call(
        self,
        *,
        wake_id: str,
        catalog: Mapping[str, Any],
        schemas: Mapping[str, Mapping[str, Any]],
        call: NativeToolCall,
    ) -> ToolExecutionBinding:
        if not call.tool_call_id.strip():
            raise ToolExecutionBoundaryError("tool_call_id_required")
        if _TOOL_NAME.fullmatch(call.tool_name) is None:
            raise ToolExecutionBoundaryError("tool_call_name_invalid")
        schema_hash = self._catalog_entry(catalog, call.tool_name)
        schema = schemas.get(call.tool_name)
        if not isinstance(schema, Mapping):
            raise ToolExecutionBoundaryError("tool_schema_not_available")
        if canonical_hash(schema) != schema_hash:
            raise ToolExecutionBoundaryError("tool_schema_hash_mismatch")
        arguments = parse_arguments(call.arguments_text)
        validation_diagnostic = None
        try:
            Draft202012Validator(dict(schema)).validate(arguments)
        except Unresolvable as exc:
            # A client may preserve #/$defs references while dropping $defs.
            # This is an advertised contract failure, not an argument error.
            # Never let jsonschema's exception (which embeds the whole schema)
            # escape the boundary or reach a request-handler traceback.
            raise ToolExecutionBoundaryError(
                "advertised_tool_schema_reference_unresolved"
            ) from exc
        except RecursionError as exc:
            # Non-terminating local references (for example {"$ref": "#"})
            # must fail inside the selected-tool boundary, not break the SSE.
            raise ToolExecutionBoundaryError(
                "advertised_tool_schema_recursion_unsupported"
            ) from exc
        except ValidationError as exc:
            validation_diagnostic = _validation_diagnostic(call.tool_name, exc)
        if validation_diagnostic is not None:
            # Raise outside the handler, so neither __cause__ nor __context__
            # retains ValidationError's instance, full schema or message.
            raise ToolExecutionBoundaryError(
                "tool_arguments_schema_invalid",
                validation_diagnostic=validation_diagnostic,
            ) from None
        binding = ToolExecutionBinding(
            contract=EXECUTION_BINDING_CONTRACT,
            wake_id=wake_id,
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            catalog_hash=str(catalog["catalog_hash"]),
            schema_hash=schema_hash,
            arguments_hash=canonical_hash(arguments),
            issued_at_ms=int(time.time() * 1000),
        )
        decision = self.policy.authorize(binding, catalog=catalog)
        if decision.decision != "allowed_to_attempt":
            if decision.decision == "confirmation_required":
                raise ToolExecutionBoundaryError("tool_execution_confirmation_required")
            raise ToolExecutionBoundaryError("tool_execution_policy_denied")
        return binding

    def verify_history_call(
        self,
        *,
        expected: ToolExecutionBinding,
        catalog: Mapping[str, Any],
        schemas: Mapping[str, Mapping[str, Any]],
        call: NativeToolCall,
    ) -> None:
        # Rebind using the same structural rules, but do not run policy twice.
        if call.tool_call_id != expected.tool_call_id or call.tool_name != expected.tool_name:
            raise ToolExecutionBoundaryError("tool_continuation_binding_mismatch")
        schema_hash = self._catalog_entry(catalog, call.tool_name)
        schema = schemas.get(call.tool_name)
        if not isinstance(schema, Mapping) or canonical_hash(schema) != schema_hash:
            raise ToolExecutionBoundaryError("tool_continuation_binding_mismatch")
        arguments = parse_arguments(call.arguments_text)
        try:
            Draft202012Validator(dict(schema)).validate(arguments)
        except Unresolvable as exc:
            raise ToolExecutionBoundaryError(
                "advertised_tool_schema_reference_unresolved"
            ) from exc
        except RecursionError as exc:
            raise ToolExecutionBoundaryError(
                "advertised_tool_schema_recursion_unsupported"
            ) from exc
        except ValidationError as exc:
            raise ToolExecutionBoundaryError("tool_continuation_binding_mismatch") from exc
        if (
            expected.catalog_hash != catalog.get("catalog_hash")
            or expected.schema_hash != schema_hash
            or expected.arguments_hash != canonical_hash(arguments)
        ):
            raise ToolExecutionBoundaryError("tool_continuation_binding_mismatch")

    def _signature(self, record: Mapping[str, Any]) -> str:
        return hmac.new(
            self._secret,
            canonical_json(record).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def witness_result(
        self,
        *,
        binding: ToolExecutionBinding,
        tool_message: Mapping[str, Any],
    ) -> dict[str, Any]:
        if tool_message.get("role") != "tool":
            raise ToolExecutionBoundaryError("tool_result_message_required")
        if tool_message.get("tool_call_id") != binding.tool_call_id:
            raise ToolExecutionBoundaryError("tool_result_binding_mismatch")
        result_hash = canonical_hash(
            {
                "tool_call_id": binding.tool_call_id,
                "content": tool_message.get("content"),
            }
        )
        completion_state = self.policy.classify_result(
            binding,
            tool_message=tool_message,
        )
        if completion_state not in {
            "accepted",
            "succeeded",
            "failed",
            "partial",
            "unknown",
        }:
            raise ToolExecutionBoundaryError("invalid_host_result_classification")
        record = {
            "contract": HOST_RECEIPT_CONTRACT,
            "receipt_id": "hostreceipt_" + uuid.uuid4().hex,
            "wake_id": binding.wake_id,
            "tool_call_id": binding.tool_call_id,
            "tool_name": binding.tool_name,
            "catalog_hash": binding.catalog_hash,
            "schema_hash": binding.schema_hash,
            "arguments_hash": binding.arguments_hash,
            "result_hash": result_hash,
            "transport_state": "result_observed",
            "completion_state": completion_state,
            "observed_at_ms": int(time.time() * 1000),
        }
        receipt = {**record, "host_signature": self._signature(record)}
        self.policy.observe_receipt(receipt)
        return receipt

    def verify_receipt(self, receipt: Mapping[str, Any]) -> bool:
        signature = receipt.get("host_signature")
        if not isinstance(signature, str) or _SHA256.fullmatch(signature) is None:
            return False
        record = {key: value for key, value in receipt.items() if key != "host_signature"}
        return hmac.compare_digest(signature, self._signature(record))
