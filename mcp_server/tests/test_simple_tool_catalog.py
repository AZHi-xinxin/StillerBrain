"""Discovery-only compatibility filtering, without stores or a server process."""
import unittest
from types import SimpleNamespace

from mcp_server.simple_tool_catalog import install_simple_tool_catalog


class SimpleToolCatalogTests(unittest.TestCase):
    def test_only_discovery_changes_and_original_guards_remain(self):
        names = ('revise_memory', 'revise_tool_guidance', 'stbrain_help')
        tools = {name: SimpleNamespace(name=name) for name in names}
        guarded_call = object()
        manager = SimpleNamespace(list_tools=lambda: list(tools.values()),
                                  get_tool=tools.get, call_tool=guarded_call)
        server = SimpleNamespace(_tool_manager=manager)
        install_simple_tool_catalog(server)
        self.assertEqual(['revise_memory', 'stbrain_help'],
                         [tool.name for tool in manager.list_tools()])
        self.assertIs(tools['revise_tool_guidance'], manager.get_tool('revise_tool_guidance'))
        self.assertIs(guarded_call, manager.call_tool)
        self.assertEqual(3, len(tools))
        with self.assertRaisesRegex(RuntimeError, 'simple_catalog_already_installed'):
            install_simple_tool_catalog(server)

    def test_missing_replacement_does_not_hide_old_tool(self):
        old = SimpleNamespace(name='revise_tool_guidance')
        manager = SimpleNamespace(list_tools=lambda: [old], get_tool=lambda name: None)
        with self.assertRaisesRegex(RuntimeError, 'unified_revision_tool_missing'):
            install_simple_tool_catalog(SimpleNamespace(_tool_manager=manager))
        self.assertEqual([old], manager.list_tools())
