"""Host-only upload discovery plus exact source selection for memory images."""
from copy import deepcopy
import json

from jsonschema import Draft202012Validator
from mcp import types

from runtime.memory_image_uploads import UPLOAD_CONTRACT, UPLOAD_TOOL


def install_memory_image_upload_contract(mcp):
    manager = mcp._tool_manager
    upload = manager.get_tool(UPLOAD_TOOL)
    store = manager.get_tool("store_memory_image")
    if upload is None or store is None:
        raise RuntimeError("image_upload_tools_missing")
    upload.fn_metadata.arg_model.model_config.update(extra="forbid", hide_input_in_errors=True)
    upload.fn_metadata.arg_model.model_rebuild(force=True)
    upload.meta = {"stbrain/hostOnly": True, "stbrain/contract": UPLOAD_CONTRACT}
    schema = deepcopy(upload.parameters)
    schema.update({"additionalProperties": False, "x-stbrain-host-only": True,
                   "x-stbrain-contract": UPLOAD_CONTRACT})
    schema["properties"]["protocol"] = {"type": "string", "enum": [UPLOAD_CONTRACT]}
    upload.parameters = schema
    # Null is equivalent to omitted for the unused source. Empty strings are
    # invalid, never an instruction to fall back to a different source.
    store.parameters["oneOf"] = [
        {"required": ["data_base64"], "properties": {
            "data_base64": {"type": "string", "minLength": 1}, "upload_ref": {"type": "null"}}},
        {"required": ["upload_ref"], "properties": {
            "upload_ref": {"type": "string", "minLength": 1}, "data_base64": {"type": "null"}}},
    ]
    # SDK schema errors interpolate user-supplied values and even unknown key
    # names before FastMCP's hide_input_in_errors takes effect. Validate this
    # byte-transport boundary first, without formatting ValidationError at all.
    validator = Draft202012Validator(schema)

    def rejected(code):
        return {"contract": UPLOAD_CONTRACT, "decision": "reject", "reason_code": code,
                "state_changed": False, "memory_written": False, "write_authority_granted": False}

    original_call = manager.call_tool

    async def guarded_call(name, arguments, context=None, convert_result=False):
        if name != UPLOAD_TOOL:
            return await original_call(name, arguments, context=context, convert_result=convert_result)
        if not validator.is_valid(arguments):
            result = rejected("image_upload_arguments_invalid")
            return upload.fn_metadata.convert_result(result) if convert_result else result
        try:
            return await original_call(name, arguments, context=context, convert_result=convert_result)
        except Exception:
            # No exception text, original bytes, key names, or paths leave this
            # host-only endpoint. Unknown result never claims a formal write.
            result = rejected("image_upload_operation_unavailable")
            return upload.fn_metadata.convert_result(result) if convert_result else result

    manager.call_tool = guarded_call
    original_handler = mcp._mcp_server.request_handlers[types.CallToolRequest]

    async def guarded_handler(request):
        if request.params.name == UPLOAD_TOOL and not validator.is_valid(request.params.arguments):
            result = rejected("image_upload_arguments_invalid")
            return types.ServerResult(types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(result))],
                structuredContent=result, isError=True))
        return await original_handler(request)

    mcp._mcp_server.request_handlers[types.CallToolRequest] = guarded_handler
