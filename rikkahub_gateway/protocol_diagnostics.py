"""Fixed, value-free protocol diagnostics safe for clients and anonymous logs."""
from collections.abc import Mapping
from typing import Any

STREAM_REASONS = {
    "invalid_utf8": "上游事件不是有效的 UTF-8。",
    "invalid_json": "上游事件不是完整、有效的 JSON。",
    "non_object_event": "上游事件应为 JSON 对象。",
    "non_finite_json": "上游事件含有无法序列化的数值。",
    "duplicate_done": "上游重复返回了结束标记。",
    "invalid_choice_index": "上游回复分支编号的类型不正确。",
    "output_after_finish": "上游在结束标记后又返回了生成内容。",
    "missing_terminal": "上游连接在回复完成前结束。",
    "missing_data": "上游没有返回可识别的回复事件。",
}
LINEAGE_REASONS = frozenset({
    "declaration_missing", "declaration_invalid", "tail_not_tool_results",
    "result_id_missing", "result_id_duplicate", "result_id_noncanonical",
    "declaration_result_set_mismatch", "current_batch_order_mismatch",
    "settled_prefix_order_mismatch", "expected_id_set_mismatch",
    "lineage_validation_failed",
})
MISMATCH_MESSAGES = {
    "ids_changed": "回传的工具调用编号与本批原始编号不同，请保留原始编号和执行结果。",
    "order_changed": "回传的工具调用顺序与本批原始顺序不同，请按原始顺序回传。",
    "batch_incomplete": "回传的工具调用批次不完整，请保留已执行结果并补齐本批回传。",
}
COUNT_FIELDS = frozenset({
    "event_index", "declared_count", "result_count", "missing_result_count",
    "unexpected_result_count", "expected_count", "terminal_declared_count",
    "segment_declared_count", "segment_result_count",
})


def normalize_protocol_diagnostic(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    kind, reason = value.get("kind"), value.get("reason")
    if type(kind) is not str or type(reason) is not str:
        return None
    allowed = STREAM_REASONS if kind == "stream" else LINEAGE_REASONS if kind == "lineage" else ()
    if reason not in allowed:
        return None
    result = {"kind": kind, "reason": reason}
    for key in COUNT_FIELDS:
        count = value.get(key)
        if type(count) is int and count >= 0:
            result[key] = min(count, 1_000_000)
    mismatch = value.get("mismatch_kind")
    if kind == "lineage" and type(mismatch) is str and mismatch in MISMATCH_MESSAGES:
        result["mismatch_kind"] = mismatch
    return result


def protocol_error_detail(code: str, value: Any) -> str:
    diagnostic = normalize_protocol_diagnostic(value)
    if diagnostic is None:
        return ""
    if diagnostic["kind"] == "stream":
        detail = STREAM_REASONS[diagnostic["reason"]] + "本轮未完成。"
    else:
        detail = MISMATCH_MESSAGES.get(
            diagnostic.get("mismatch_kind"),
            "工具调用与回传结果无法对应，请检查原始调用编号、顺序和完整批次。",
        ) + "已经执行的操作不应重复执行。"
    return "[ST 网关 · " + code + "] " + detail
