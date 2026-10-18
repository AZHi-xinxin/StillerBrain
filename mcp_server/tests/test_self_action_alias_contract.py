"""Contract-only fixtures: no live candidate, wake or external service."""
import asyncio
import unittest
from typing import Any
from mcp.server.fastmcp import FastMCP
from jsonschema import Draft202012Validator
from mcp_server.compact_tool_dispatch import install_compact_tool_dispatch
from mcp_server.public_contract import PUBLIC_SUBMIT_INPUT_SCHEMA, _INTERNAL_ACTION_TO_PUBLIC_CALL
from runtime.compact_tool_routes import compact_action_target, resolve_compact_action


class SelfActionAliasContractTests(unittest.TestCase):
    def test_every_displayed_status_action_resolves_to_its_public_route(self):
        for alias, route in _INTERNAL_ACTION_TO_PUBLIC_CALL.items():
            with self.subTest(alias=alias):
                self.assertEqual(route, compact_action_target(alias))

    def test_accept_alias_discovery_schema_accepts_exact_next_call_without_intent(self):
        mcp = FastMCP('synthetic-alias-schema')
        @mcp.tool()
        async def submit_self_model_candidate(intent: str, write_context_ref: str,
                                              expected_row_version: int, payload: dict[str, Any]) -> dict:
            raise AssertionError('discovery must never execute')
        mcp._tool_manager.get_tool('submit_self_model_candidate').parameters = PUBLIC_SUBMIT_INPUT_SCHEMA
        install_compact_tool_dispatch(mcp)
        _, detail = asyncio.run(mcp.call_tool('stbrain_tools', {'action': 'accept_candidate_review'}))
        args = {'write_context_ref': 'synthetic-ref', 'expected_row_version': 1,
                'payload': {'ai_confirmation': True}}
        Draft202012Validator(detail['arguments_schema']).validate(args)
        canonical, normalized = resolve_compact_action({'action': 'accept_candidate_review', 'arguments': args})
        self.assertEqual('submit_self_model_candidate', canonical)
        Draft202012Validator(PUBLIC_SUBMIT_INPUT_SCHEMA).validate(normalized)
        self.assertNotIn('intent', args)
