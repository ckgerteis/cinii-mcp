"""MCPB entry point for cinii-mcp.

The bundle vendors every dependency under server/lib (built on the platform it
targets, because pydantic-core is a native wheel). This file puts that folder
first on sys.path and starts the same stdio server the console script starts.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

# Claude Desktop substitutes an empty string for an optional user_config field
# the user left blank. The servers treat an empty value as unset, but the
# ledger's "is a destination configured" test should not see an empty folder
# name as a folder, so strip blanks before the package imports anything.
for _k in list(os.environ):
    if _k.startswith("MCP_RECEIPT") and not os.environ[_k].strip():
        del os.environ[_k]

from cinii_mcp import main  # noqa: E402

if __name__ == "__main__":
    main()
