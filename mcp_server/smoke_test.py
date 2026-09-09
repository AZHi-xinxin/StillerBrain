"""End-to-end catalog and read-surface smoke test for the public MCP endpoint.

Run this only against the staging harness or another disposable wake.  Although
the queried views do not edit model content, ``stbrain_open`` intentionally
records that the current wake opened the manual.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


MCP_URL = os.environ.get(
    "STBRAIN_MCP_TEST_URL", "http://127.0.0.1:8794/mcp"
).strip()
MCP_TOKEN = os.environ.get("STBRAIN_MCP_TOKEN", "").strip()
EXPECTED_TOOLS = {
    "stbrain_health",
    "stbrain_open",
    "submit_self_model_candidate",
    "activate_self_model_candidate",
    "query_self_model",
    "remember_emotional_memory",
    "recall_emotional_memory",
    "revise_emotional_memory",
    "integrate_emotional_memories",
    "manage_brain_pin",
    "veto_ephemeral_memory",
}


async def _expect_unauthorized() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": "unauthorized-probe",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "stbrain-smoke", "version": "1"},
        },
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(MCP_URL, json=payload)
    if response.status_code != 401:
        raise RuntimeError(
            f"unauthenticated request returned {response.status_code}, expected 401"
        )


async def _call_read_only(
    session: ClientSession, name: str, arguments: dict
) -> None:
    result = await session.call_tool(name, arguments)
    if getattr(result, "isError", False):
        raise RuntimeError(f"{name} returned an MCP tool error")


async def main() -> None:
    if len(MCP_TOKEN) < 32:
        raise RuntimeError("STBRAIN_MCP_TOKEN is missing or too short")
    if not MCP_URL:
        raise RuntimeError("STBRAIN_MCP_TEST_URL is empty")

    await _expect_unauthorized()

    headers = {"Authorization": f"Bearer {MCP_TOKEN}"}
    async with httpx.AsyncClient(headers=headers, timeout=40.0) as http_client:
        async with streamable_http_client(
            MCP_URL,
            http_client=http_client,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                missing = EXPECTED_TOOLS - names
                unexpected = names - EXPECTED_TOOLS
                if missing or unexpected:
                    raise RuntimeError(
                        f"unexpected tool catalog; missing={sorted(missing)}, "
                        f"unexpected={sorted(unexpected)}"
                    )
                submit_tool = next(
                    tool
                    for tool in listed.tools
                    if tool.name == "submit_self_model_candidate"
                )
                submit_schema = submit_tool.inputSchema
                if submit_schema.get("additionalProperties") is not False:
                    raise RuntimeError("submit tool root must reject unknown arguments")
                if set(submit_schema.get("required", [])) != {
                    "intent",
                    "write_context_ref",
                    "expected_row_version",
                }:
                    raise RuntimeError("submit tool has an unexpected required argument set")
                branches = submit_schema.get("allOf", [{}])[0].get("oneOf", [])
                if len(branches) != 11:
                    raise RuntimeError("submit tool must publish all 11 intent branches")
                definitions = submit_schema.get("$defs", {})
                for intent in ("submit", "revise"):
                    candidate = definitions.get(f"payload_{intent}", {})
                    if candidate.get("additionalProperties") is not False or set(
                        candidate.get("properties", {})
                    ) != {"content", "reason"}:
                        raise RuntimeError(
                            f"{intent} payload must contain only content and reason"
                        )
                serialized_schema = json.dumps(submit_schema, sort_keys=True)
                for forbidden in (
                    "wake_capability",
                    "challenge_response",
                    "_server_derive_candidate_metadata",
                ):
                    if forbidden in serialized_schema:
                        raise RuntimeError(
                            f"submit tool schema exposed private field: {forbidden}"
                        )
                await _call_read_only(session, "stbrain_health", {})
                await _call_read_only(session, "stbrain_open", {})
                await _call_read_only(
                    session,
                    "query_self_model",
                    {"view": "status"},
                )
                await _call_read_only(
                    session,
                    "query_self_model",
                    {"view": "active"},
                )
                await _call_read_only(
                    session,
                    "query_self_model",
                    {
                        "view": "search",
                        "scope": "all",
                        "limit": 1,
                        "include_content": False,
                    },
                )

    print("STBRAIN_MCP_SMOKE_OK")
    print("AUTH=unauthorized-rejected,authorized-accepted")
    print("TOOLS=" + ",".join(sorted(EXPECTED_TOOLS)))


if __name__ == "__main__":
    asyncio.run(main())
