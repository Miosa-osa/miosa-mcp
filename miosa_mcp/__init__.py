"""MIOSA MCP server - exposes MIOSA infrastructure tools to MCP clients."""

__version__ = "0.3.3"

from .server import main

__all__ = ["main", "__version__"]
