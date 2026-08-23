"""cinii-mcp — MCP server for CiNii Research.

Importing this package does not start the server; call `main()`, run
`python -m cinii_mcp`, or use the installed `cinii-mcp` console script.
"""
from .server import __version__, main

__all__ = ["main", "__version__"]
