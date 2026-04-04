from mcp_manager import MCPManager

_mcp_manager: MCPManager | None = None


def set_mcp_manager(manager: MCPManager):
    global _mcp_manager
    _mcp_manager = manager


def get_mcp_manager() -> MCPManager | None:
    return _mcp_manager
