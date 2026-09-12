"""Entry point of the Claude Desktop bundle for cinii-mcp.

The bundle vendors no libraries. Its manifest declares server.type "uv" and
keeps pyproject.toml, uv.lock and this file together at the bundle root,
which is the layout Claude Desktop's UV runtime expects. When the extension
is installed the app runs `uv sync` there with uv (one on the PATH, else a
copy it downloads), using a
Python already on the machine that satisfies requires-python (downloading one
only if there is none) and installing the locked dependencies into
<bundle>/.venv. Each launch then runs, from the bundle folder:

    uv run --directory <bundle> --frozen main.py

which reuses that environment and starts the same stdio server the console
script starts. Where a host skipped the install-time step, the same command
builds the environment on first launch instead; later launches do not.
"""
import os
import sys

# Claude Desktop substitutes "${user_config.KEY}" in the manifest's env block
# only when the user gave that field a value. A field left blank arrives as
# the placeholder itself, verbatim (measured on 1.46 with this bundle: the
# ledger wrote its file into a folder named "${user_config.receipts_dir}" and
# stamped "${user_config.receipt_session}" on the line), and older hosts sent
# an empty string. Either means "unset", for every variable: a credential
# left blank must not be sent to the provider as a key, and a receipts folder
# left blank must not become a folder. Strip both before the package imports.
for _k, _v in list(os.environ.items()):
    if "${user_config." in _v or (_k.startswith("MCP_RECEIPT") and not _v.strip()):
        del os.environ[_k]

try:
    from cinii_mcp import main
except ImportError as exc:
    # Say what is wrong, not merely that something is. Claude Desktop shows
    # "Server disconnected"; this line is what the log will carry.
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.stderr.write(
        f"cinii-mcp: cannot import its package under Python {sys.version.split()[0]} "
        f"at {sys.executable}: {exc}\n"
        f"cinii-mcp: supported Python is >=3.10; this file is meant to be run by uv "
        f'(uv --directory "{_here}" run --frozen main.py), which provisions the '
        f"interpreter and the libraries from the pyproject.toml and uv.lock beside it. "
        f"If uv could not build that environment its own message is above this line.\n"
    )
    raise

if __name__ == "__main__":
    main()
