"""Keep ordinary discovery concise while retaining guarded legacy calls."""

SIMPLE_COMPATIBILITY_TOOLS = frozenset({'revise_tool_guidance'})


def install_simple_tool_catalog(mcp):
    manager = mcp._tool_manager
    if getattr(manager, '_simple_catalog_installed', False):
        raise RuntimeError('simple_catalog_already_installed')
    if manager.get_tool('revise_memory') is None:
        raise RuntimeError('unified_revision_tool_missing')
    original_list = manager.list_tools

    def list_tools():
        return [tool for tool in original_list()
                if tool.name not in SIMPLE_COMPATIBILITY_TOOLS]

    manager.list_tools = list_tools
    manager._simple_catalog_installed = True
