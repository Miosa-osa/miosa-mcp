"""MIOSA MCP server — wraps the MIOSA Python SDK as Model Context Protocol tools.

Run with:
    python -m miosa_mcp

Or add to .claude/mcp.json:
    {
      "mcpServers": {
        "miosa": {
          "command": "python",
          "args": ["-m", "miosa_mcp"],
          "env": { "MIOSA_API_KEY": "msk_u_..." }
        }
      }
    }
"""

from __future__ import annotations

import base64
import logging
import os
import sys
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from miosa import AsyncMiosa, MiosaError
from miosa.resources.computer import AsyncComputer
from miosa_mcp.tools.egress import EGRESS_TOOLS, dispatch_egress

logger = logging.getLogger("miosa-mcp")

# ---------------------------------------------------------------------------
# Computer cache — tracks active computers by ID; auto-selects the most recent
# ---------------------------------------------------------------------------

MAX_CACHED = 50

_computers: dict[str, AsyncComputer] = {}
_last_computer_id: str | None = None


def _set_active(computer: AsyncComputer) -> None:
    global _last_computer_id
    if len(_computers) >= MAX_CACHED and computer.id not in _computers:
        oldest = next(iter(_computers))
        del _computers[oldest]
    _computers[computer.id] = computer
    _last_computer_id = computer.id


async def _resolve(client: AsyncMiosa, computer_id: str | None) -> AsyncComputer:
    """Return computer from cache or fetch from API.

    When computer_id is explicitly provided, always re-fetches from the API to
    avoid stale references. When computer_id is None, uses the most recently
    touched computer from the cache.
    Raises ValueError if no computer is available.
    """
    global _last_computer_id

    if computer_id is not None:
        computer = await client.computers.get(computer_id)
        _set_active(computer)
        return computer

    cid = _last_computer_id
    if cid is None:
        raise ValueError(
            "No computer_id provided and no active computer in cache. "
            "Call computer_create or computer_list first."
        )

    if cid not in _computers:
        computer = await client.computers.get(cid)
        _set_active(computer)

    _last_computer_id = cid
    return _computers[cid]


# ---------------------------------------------------------------------------
# Tool result helpers
# ---------------------------------------------------------------------------

def _ok(text: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=text)]


def _err(msg: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=f"Error: {msg}")]


def _image(png_bytes: bytes) -> list[types.ImageContent]:
    return [
        types.ImageContent(
            type="image",
            data=base64.b64encode(png_bytes).decode("ascii"),
            mimeType="image/png",
        )
    ]


# ---------------------------------------------------------------------------
# Build the MCP server
# ---------------------------------------------------------------------------

INSTRUCTIONS = """\
MIOSA cloud infrastructure — Firecracker microVMs you control via API.

## Concepts
- **Computer**: full Linux desktop VM (GUI, browser, apps). Use for visual tasks, browser automation, desktop control.
- **Sandbox**: headless Linux VM. Use for code execution, builds, CI, scripts. No desktop.
- **Deployment**: git-based app hosting with builds, releases, domains.
- **Storage**: S3-compatible object storage (buckets + objects).
- **Database**: managed Postgres, MySQL, or Redis.
- **Volume**: persistent block storage, attachable to computers.

## Core workflows

### Run code (sandbox)
create_sandbox → exec (or exec_python) → read output → destroy_sandbox

### Desktop automation (computer)
computer_create → computer_screenshot → computer_click/type/key → computer_screenshot → repeat

### Deploy an app
deployment_create(repo_url) → deployment_publish → custom_domain_add (optional)

### Store files
storage_bucket_create → storage_object_upload → storage_object_presign (for public URL)

### Provision a database
database_create(engine="postgres") → database_credentials → use connection string

## Conventions
- IDs: pass `computer_id` or `sandbox_id` to every tool that operates on a resource.
- Sizes: xs (1cpu/2GB), small (2cpu/4GB), medium (4cpu/8GB), large (8cpu/16GB), xl (16cpu/32GB).
- Status: "running" = ready. Poll with get/list until status is "running".
- File paths inside VMs: `/workspace` is the default working directory. `/home`, `/root`, `/tmp`, `/opt` also writable.
- exec timeout: default 30s. For installs (npm/pip), set timeout_ms=120000.
- Screenshots: PNG bytes, 1024x768 default. Coordinates are absolute pixels from top-left (0,0).

## Tool naming
`{resource}_{action}` — e.g. `computer_create`, `sandbox_exec`, `storage_bucket_list`.
Desktop tools: `computer_screenshot`, `computer_click`, `computer_type`, `computer_key`, `computer_scroll`.
"""


def build_server(client: AsyncMiosa) -> Server:
    app = Server("miosa-mcp", instructions=INSTRUCTIONS)

    # ── Tool list ─────────────────────────────────────────────────────────

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            # Lifecycle
            types.Tool(
                name="computer_create",
                description=(
                    "Create a new MIOSA computer and wait until it is active. "
                    "Returns the computer ID. The new computer becomes the active "
                    "computer for subsequent calls."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Human-readable name for the computer",
                        },
                        "template_type": {
                            "type": "string",
                            "description": "Template to boot from (default: miosa-desktop)",
                            "default": "miosa-desktop",
                        },
                        "size": {
                            "type": "string",
                            "enum": ["small", "medium", "large", "xl"],
                            "description": "VM size (default: small)",
                            "default": "small",
                        },
                        "workspace_id": {
                            "type": "string",
                            "description": "Workspace ID to assign the computer to (optional)",
                        },
                        "external_workspace_id": {
                            "type": "string",
                            "description": "Your internal workspace ID for attribution (optional)",
                        },
                        "external_project_id": {
                            "type": "string",
                            "description": "Your internal project ID for attribution (optional)",
                        },
                        "gpu_model": {
                            "type": "string",
                            "description": "GPU model to attach (e.g. 'nvidia-a10g', 'nvidia-t4'). Omit for CPU-only.",
                        },
                        "gpu_count": {
                            "type": "integer",
                            "description": "Number of GPUs to attach (default: 1 when gpu_model is set).",
                            "default": 1,
                        },
                    },
                    "required": ["name"],
                },
            ),
            types.Tool(
                name="computer_list",
                description="List all computers in the tenant.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="computer_destroy",
                description="Permanently destroy a computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {
                            "type": "string",
                            "description": "ID of the computer to destroy. Omit to use the active computer.",
                        },
                    },
                },
            ),
            types.Tool(
                name="computer_get",
                description="Get details and current status of a computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {
                            "type": "string",
                            "description": "ID of the computer to fetch.",
                        },
                    },
                    "required": ["computer_id"],
                },
            ),
            types.Tool(
                name="computer_start",
                description="Start a stopped computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {
                            "type": "string",
                            "description": "Computer ID. Omit to use the active computer.",
                        },
                    },
                },
            ),
            types.Tool(
                name="computer_stop",
                description="Stop a running computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {
                            "type": "string",
                            "description": "Computer ID. Omit to use the active computer.",
                        },
                    },
                },
            ),
            types.Tool(
                name="computer_restart",
                description="Restart a computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {
                            "type": "string",
                            "description": "Computer ID. Omit to use the active computer.",
                        },
                    },
                },
            ),
            types.Tool(
                name="computer_update",
                description="Rename a computer or update its metadata.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {
                            "type": "string",
                            "description": "Computer ID. Omit to use the active computer.",
                        },
                        "name": {
                            "type": "string",
                            "description": "New human-readable name for the computer (optional).",
                        },
                        "metadata": {
                            "type": "object",
                            "description": "Arbitrary key/value metadata to attach (optional).",
                        },
                    },
                },
            ),
            # Desktop — screenshot
            types.Tool(
                name="computer_screenshot",
                description=(
                    "Capture a PNG screenshot of the computer desktop. "
                    "Returns the image so you can see the current screen state."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {
                            "type": "string",
                            "description": "Computer ID. Omit to use the active computer.",
                        },
                    },
                },
            ),
            # Desktop — pointer
            types.Tool(
                name="computer_click",
                description="Click a mouse button at the given screen coordinates.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string", "description": "Computer ID (optional)"},
                        "x": {"type": "integer", "description": "X coordinate in pixels"},
                        "y": {"type": "integer", "description": "Y coordinate in pixels"},
                        "button": {
                            "type": "string",
                            "enum": ["left", "right", "middle"],
                            "description": "Mouse button (default: left)",
                            "default": "left",
                        },
                    },
                    "required": ["x", "y"],
                },
            ),
            types.Tool(
                name="computer_double_click",
                description="Double-click at the given screen coordinates.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                    },
                    "required": ["x", "y"],
                },
            ),
            types.Tool(
                name="computer_move_cursor",
                description="Move the mouse cursor to the given coordinates without clicking.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                    },
                    "required": ["x", "y"],
                },
            ),
            types.Tool(
                name="computer_drag",
                description="Click-and-drag from one screen position to another.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "from_x": {"type": "integer"},
                        "from_y": {"type": "integer"},
                        "to_x": {"type": "integer"},
                        "to_y": {"type": "integer"},
                    },
                    "required": ["from_x", "from_y", "to_x", "to_y"],
                },
            ),
            types.Tool(
                name="computer_scroll",
                description="Scroll in a direction on the desktop.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "direction": {
                            "type": "string",
                            "enum": ["up", "down", "left", "right"],
                            "default": "down",
                        },
                        "clicks": {
                            "type": "integer",
                            "description": "Number of scroll detents (default: 3)",
                            "default": 3,
                        },
                        "x": {"type": "integer", "description": "Optional X position for scroll"},
                        "y": {"type": "integer", "description": "Optional Y position for scroll"},
                    },
                },
            ),
            # Desktop — keyboard
            types.Tool(
                name="computer_type",
                description="Type text into the currently focused field.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "text": {"type": "string", "description": "Text to type"},
                    },
                    "required": ["text"],
                },
            ),
            types.Tool(
                name="computer_key",
                description=(
                    "Press a single key. Use standard key names: "
                    "Return, Tab, Escape, BackSpace, Delete, space, F1-F12, "
                    "ctrl, shift, alt, super."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "key": {"type": "string", "description": "Key name to press"},
                    },
                    "required": ["key"],
                },
            ),
            types.Tool(
                name="computer_hotkey",
                description=(
                    "Press a keyboard shortcut (multiple keys simultaneously). "
                    "Example: ['ctrl', 'c'] for copy, ['ctrl', 'alt', 't'] for terminal."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Keys to press together, e.g. ['ctrl', 'c']",
                        },
                    },
                    "required": ["keys"],
                },
            ),
            # Desktop — display info
            types.Tool(
                name="computer_get_screen_size",
                description="Get the screen resolution (width and height in pixels).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                    },
                },
            ),
            types.Tool(
                name="computer_get_cursor_position",
                description="Get the current mouse cursor position (x, y).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                    },
                },
            ),
            # Clipboard
            types.Tool(
                name="computer_get_clipboard",
                description="Read the current clipboard text content.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                    },
                },
            ),
            types.Tool(
                name="computer_set_clipboard",
                description="Set the clipboard text content.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "text": {"type": "string", "description": "Text to put in clipboard"},
                    },
                    "required": ["text"],
                },
            ),
            # Window management
            types.Tool(
                name="computer_windows",
                description="List all open windows on the desktop with their IDs, titles, and positions.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                    },
                },
            ),
            types.Tool(
                name="computer_launch",
                description="Launch an application by name (e.g. 'firefox', 'gedit', 'xterm').",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "app": {"type": "string", "description": "Application name to launch"},
                    },
                    "required": ["app"],
                },
            ),
            # Shell & Files
            types.Tool(
                name="computer_bash",
                description=(
                    "Execute a bash command on the computer and return stdout + stderr. "
                    "Commands run as the default user inside the VM."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "command": {"type": "string", "description": "Bash command to run"},
                        "timeout": {
                            "type": "integer",
                            "description": "Timeout in seconds (optional)",
                        },
                    },
                    "required": ["command"],
                },
            ),
            types.Tool(
                name="computer_write_file",
                description="Write text content to a file path inside the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "path": {"type": "string", "description": "Absolute path inside the VM"},
                        "content": {"type": "string", "description": "File content to write"},
                    },
                    "required": ["path", "content"],
                },
            ),
            types.Tool(
                name="computer_read_file",
                description="Read a file from inside the computer and return its content as text.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "path": {"type": "string", "description": "Absolute path inside the VM"},
                    },
                    "required": ["path"],
                },
            ),
            # Desktop — extended pointer / keyboard / window / env
            types.Tool(
                name="computer_right_click",
                description="Right-click at the given screen coordinates.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "x": {"type": "integer", "description": "X coordinate in pixels"},
                        "y": {"type": "integer", "description": "Y coordinate in pixels"},
                    },
                    "required": ["x", "y"],
                },
            ),
            types.Tool(
                name="computer_mouse_down",
                description="Press and hold a mouse button at (x, y). Pair with computer_mouse_up to release.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "button": {
                            "type": "string",
                            "enum": ["left", "right", "middle"],
                            "default": "left",
                        },
                    },
                    "required": ["x", "y"],
                },
            ),
            types.Tool(
                name="computer_mouse_up",
                description="Release a held mouse button at (x, y).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "button": {
                            "type": "string",
                            "enum": ["left", "right", "middle"],
                            "default": "left",
                        },
                    },
                    "required": ["x", "y"],
                },
            ),
            types.Tool(
                name="computer_key_down",
                description="Press and hold a key without releasing it. Pair with computer_key_up.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "key": {"type": "string", "description": "Key name to hold down"},
                    },
                    "required": ["key"],
                },
            ),
            types.Tool(
                name="computer_key_up",
                description="Release a previously held key.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "key": {"type": "string", "description": "Key name to release"},
                    },
                    "required": ["key"],
                },
            ),
            types.Tool(
                name="computer_wait",
                description="Pause execution inside the computer for N seconds.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "seconds": {
                            "type": "number",
                            "description": "Seconds to wait (may be fractional, e.g. 0.5)",
                        },
                    },
                    "required": ["seconds"],
                },
            ),
            types.Tool(
                name="computer_focus_window",
                description="Bring a window to the foreground by its window ID.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "window_id": {"type": "string", "description": "Window ID from computer_windows"},
                    },
                    "required": ["window_id"],
                },
            ),
            types.Tool(
                name="computer_set_window_size",
                description="Resize a window to the given width and height in pixels.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "window_id": {"type": "string", "description": "Window ID from computer_windows"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                    "required": ["window_id", "width", "height"],
                },
            ),
            types.Tool(
                name="computer_set_window_position",
                description="Move a window to the given screen coordinates.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "window_id": {"type": "string", "description": "Window ID from computer_windows"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                    },
                    "required": ["window_id", "x", "y"],
                },
            ),
            types.Tool(
                name="computer_maximize_window",
                description="Maximize a window.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "window_id": {"type": "string", "description": "Window ID from computer_windows"},
                    },
                    "required": ["window_id"],
                },
            ),
            types.Tool(
                name="computer_minimize_window",
                description="Minimize (iconify) a window.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "window_id": {"type": "string", "description": "Window ID from computer_windows"},
                    },
                    "required": ["window_id"],
                },
            ),
            types.Tool(
                name="computer_close_window",
                description="Close a window.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "window_id": {"type": "string", "description": "Window ID from computer_windows"},
                    },
                    "required": ["window_id"],
                },
            ),
            types.Tool(
                name="computer_get_desktop_env",
                description="Get desktop environment info (name, resolution, session type).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                    },
                },
            ),
            types.Tool(
                name="computer_set_wallpaper",
                description=(
                    "Set the desktop wallpaper. Accepts an absolute path inside "
                    "the VM (e.g. '/home/ubuntu/bg.png') or an https:// URL."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "path": {
                            "type": "string",
                            "description": "Absolute VM path or https:// URL for the wallpaper image",
                        },
                    },
                    "required": ["path"],
                },
            ),
            types.Tool(
                name="computer_accessibility_tree",
                description=(
                    "Get the AT-SPI accessibility tree for the current desktop state. "
                    "Returns a nested JSON structure describing all visible UI elements "
                    "with roles, names, bounding boxes, and parent/child relationships."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                    },
                },
            ),
            # ── Files — extended ───────────────────────────────────────────
            types.Tool(
                name="computer_list_files",
                description="List directory contents on the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "path": {
                            "type": "string",
                            "description": "Directory path to list (default: /)",
                        },
                    },
                },
            ),
            types.Tool(
                name="computer_stat_file",
                description="Get file/directory metadata (size, type, permissions, mtime) inside the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "path": {"type": "string", "description": "Absolute path inside the VM"},
                    },
                    "required": ["path"],
                },
            ),
            types.Tool(
                name="computer_mkdir",
                description="Create a directory (and any missing parents) inside the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "path": {"type": "string", "description": "Absolute directory path to create"},
                        "recursive": {
                            "type": "boolean",
                            "description": "Create parent directories if missing (default: true)",
                            "default": True,
                        },
                    },
                    "required": ["path"],
                },
            ),
            types.Tool(
                name="computer_rename_file",
                description="Rename or move a file/directory inside the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "source": {"type": "string", "description": "Current absolute path"},
                        "dest": {"type": "string", "description": "New absolute path"},
                    },
                    "required": ["source", "dest"],
                },
            ),
            types.Tool(
                name="computer_copy_file",
                description="Copy a file or directory tree inside the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "source": {"type": "string", "description": "Source absolute path"},
                        "dest": {"type": "string", "description": "Destination absolute path"},
                        "recursive": {
                            "type": "boolean",
                            "description": "Copy directory trees recursively (default: false)",
                            "default": False,
                        },
                    },
                    "required": ["source", "dest"],
                },
            ),
            types.Tool(
                name="computer_delete_file",
                description="Delete a file or directory inside the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "path": {"type": "string", "description": "Absolute path to delete"},
                    },
                    "required": ["path"],
                },
            ),
            types.Tool(
                name="computer_upload_file",
                description="Upload a local file from the host machine into the computer at a given path.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "local_path": {
                            "type": "string",
                            "description": "Absolute path on the local host to upload",
                        },
                        "remote_path": {
                            "type": "string",
                            "description": "Destination absolute path inside the VM",
                        },
                    },
                    "required": ["local_path", "remote_path"],
                },
            ),
            # ── Checkpoints ────────────────────────────────────────────────
            types.Tool(
                name="computer_checkpoint_create",
                description="Save the current state of the computer as a checkpoint (snapshot).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "comment": {
                            "type": "string",
                            "description": "Optional human-readable label for the checkpoint",
                        },
                    },
                },
            ),
            types.Tool(
                name="computer_checkpoint_list",
                description="List all saved checkpoints for the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                    },
                },
            ),
            types.Tool(
                name="computer_checkpoint_restore",
                description="Restore the computer to a previously saved checkpoint.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "checkpoint_id": {
                            "type": "string",
                            "description": "ID of the checkpoint to restore",
                        },
                    },
                    "required": ["checkpoint_id"],
                },
            ),
            types.Tool(
                name="computer_checkpoint_delete",
                description="Delete a saved checkpoint.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "checkpoint_id": {
                            "type": "string",
                            "description": "ID of the checkpoint to delete",
                        },
                    },
                    "required": ["checkpoint_id"],
                },
            ),
            # ── Services ───────────────────────────────────────────────────
            types.Tool(
                name="computer_service_create",
                description=(
                    "Create and start a long-running background service on the computer "
                    "(like systemd — process manager for your VM)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "name": {"type": "string", "description": "Service name (must be unique)"},
                        "command": {"type": "string", "description": "Shell command to run"},
                        "working_dir": {
                            "type": "string",
                            "description": "Working directory for the service (optional)",
                        },
                        "port": {
                            "type": "integer",
                            "description": "Port the service listens on (optional)",
                        },
                        "restart_policy": {
                            "type": "string",
                            "enum": ["always", "on-failure", "never"],
                            "description": "Restart behaviour (default: always)",
                        },
                    },
                    "required": ["name", "command"],
                },
            ),
            types.Tool(
                name="computer_service_list",
                description="List all background services registered on the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                    },
                },
            ),
            types.Tool(
                name="computer_service_start",
                description="Start a stopped background service.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "service_id": {"type": "string", "description": "Service ID to start"},
                    },
                    "required": ["service_id"],
                },
            ),
            types.Tool(
                name="computer_service_stop",
                description="Stop a running background service (sends SIGTERM).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "service_id": {"type": "string", "description": "Service ID to stop"},
                    },
                    "required": ["service_id"],
                },
            ),
            types.Tool(
                name="computer_service_restart",
                description="Restart a background service (stop then start).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "service_id": {"type": "string", "description": "Service ID to restart"},
                    },
                    "required": ["service_id"],
                },
            ),
            types.Tool(
                name="computer_service_logs",
                description="Retrieve recent log output from a background service (last 100 lines).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "service_id": {"type": "string", "description": "Service ID"},
                    },
                    "required": ["service_id"],
                },
            ),
            types.Tool(
                name="computer_service_delete",
                description="Delete a background service (stops it first if running).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "service_id": {"type": "string", "description": "Service ID to delete"},
                    },
                    "required": ["service_id"],
                },
            ),
            # ── Env vars ───────────────────────────────────────────────────
            types.Tool(
                name="computer_env_list",
                description="List all environment variables set on the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                    },
                },
            ),
            types.Tool(
                name="computer_env_set",
                description="Set (create or update) an environment variable on the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "name": {"type": "string", "description": "Variable name"},
                        "value": {"type": "string", "description": "Variable value"},
                    },
                    "required": ["name", "value"],
                },
            ),
            types.Tool(
                name="computer_env_delete",
                description="Delete an environment variable from the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "name": {"type": "string", "description": "Variable name to delete"},
                    },
                    "required": ["name"],
                },
            ),
            # ── Logs ───────────────────────────────────────────────────────
            types.Tool(
                name="computer_logs",
                description="Get recent VM-level logs from the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "lines": {
                            "type": "integer",
                            "description": "Number of recent log lines to return (optional)",
                        },
                    },
                },
            ),
            # ── Domains ────────────────────────────────────────────────────
            types.Tool(
                name="computer_domain_add",
                description=(
                    "Add a custom domain to the computer. Returns CNAME verification "
                    "instructions to add to your DNS registrar."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "fqdn": {
                            "type": "string",
                            "description": "Fully-qualified domain name (e.g. app.example.com)",
                        },
                    },
                    "required": ["fqdn"],
                },
            ),
            types.Tool(
                name="computer_domain_list",
                description="List all custom domains registered for the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                    },
                },
            ),
            types.Tool(
                name="computer_domain_delete",
                description="Delete a custom domain mapping from the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string"},
                        "domain_id": {
                            "type": "string",
                            "description": "Domain ID to delete",
                        },
                    },
                    "required": ["domain_id"],
                },
            ),
            # ── Sandbox lifecycle ─────────────────────────────────────────
            types.Tool(
                name="sandbox_create",
                description="Create a new lightweight code sandbox (Firecracker microVM without desktop).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Human-readable name for the sandbox"},
                        "template_id": {"type": "string", "description": "Template / image ID (default: miosa-sandbox)"},
                        "cpu_count": {"type": "integer", "description": "vCPU count"},
                        "memory_mb": {"type": "integer", "description": "Memory in MB"},
                        "timeout_sec": {"type": "integer", "description": "Idle timeout in seconds"},
                    },
                },
            ),
            types.Tool(
                name="sandbox_list",
                description="List all sandboxes in the tenant.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "enum": ["provisioning", "running", "paused", "destroyed", "error"],
                            "description": "Filter by state (optional)",
                        },
                    },
                },
            ),
            types.Tool(
                name="sandbox_get",
                description="Get details of a specific sandbox.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                    },
                    "required": ["sandbox_id"],
                },
            ),
            types.Tool(
                name="sandbox_destroy",
                description="Destroy a sandbox permanently.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID to destroy"},
                    },
                    "required": ["sandbox_id"],
                },
            ),
            types.Tool(
                name="sandbox_pause",
                description="Pause a running sandbox (suspends it to save compute).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                    },
                    "required": ["sandbox_id"],
                },
            ),
            types.Tool(
                name="sandbox_resume",
                description="Resume a paused sandbox.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                    },
                    "required": ["sandbox_id"],
                },
            ),
            # ── Sandbox files ─────────────────────────────────────────────
            types.Tool(
                name="sandbox_write_file",
                description="Write text or binary content to a file path inside a sandbox.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                        "path": {"type": "string", "description": "Absolute path inside the sandbox"},
                        "content": {"type": "string", "description": "File content (text)"},
                    },
                    "required": ["sandbox_id", "path", "content"],
                },
            ),
            types.Tool(
                name="sandbox_read_file",
                description="Read a file from inside a sandbox and return its content as text.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                        "path": {"type": "string", "description": "Absolute path inside the sandbox"},
                    },
                    "required": ["sandbox_id", "path"],
                },
            ),
            types.Tool(
                name="sandbox_list_files",
                description="List files in a directory inside a sandbox.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                        "path": {
                            "type": "string",
                            "description": "Directory path to list (default: /workspace)",
                            "default": "/workspace",
                        },
                        "depth": {"type": "integer", "description": "Recursion depth (optional)"},
                    },
                    "required": ["sandbox_id"],
                },
            ),
            types.Tool(
                name="sandbox_upload",
                description="Upload a file (base64-encoded) into a sandbox.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                        "path": {"type": "string", "description": "Destination path inside the sandbox"},
                        "content": {"type": "string", "description": "File content as UTF-8 text or base64-encoded binary"},
                    },
                    "required": ["sandbox_id", "path", "content"],
                },
            ),
            # ── Sandbox exec ─────────────────────────────────────────────
            types.Tool(
                name="sandbox_exec",
                description="Run a bash command inside a sandbox and return the output.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                        "command": {"type": "string", "description": "Command to run"},
                        "cwd": {"type": "string", "description": "Working directory (optional)"},
                        "timeout": {"type": "integer", "description": "Timeout in seconds (optional)"},
                    },
                    "required": ["sandbox_id", "command"],
                },
            ),
            types.Tool(
                name="sandbox_python",
                description="Run Python code inside a sandbox and return stdout, stderr, and exit code.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                        "code": {"type": "string", "description": "Python code to execute"},
                        "timeout": {"type": "integer", "description": "Timeout in seconds (optional)"},
                    },
                    "required": ["sandbox_id", "code"],
                },
            ),
            # ── Sandbox snapshots ─────────────────────────────────────────
            types.Tool(
                name="sandbox_snapshot_create",
                description="Create a snapshot of the current sandbox state.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                        "comment": {"type": "string", "description": "Optional comment for the snapshot"},
                    },
                    "required": ["sandbox_id"],
                },
            ),
            types.Tool(
                name="sandbox_snapshot_list",
                description="List all snapshots for a sandbox.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                    },
                    "required": ["sandbox_id"],
                },
            ),
            types.Tool(
                name="sandbox_snapshot_restore",
                description="Restore a sandbox from a specific snapshot.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                        "snapshot_id": {"type": "string", "description": "Snapshot ID to restore from"},
                    },
                    "required": ["sandbox_id", "snapshot_id"],
                },
            ),
            # ── Sandbox logs ──────────────────────────────────────────────
            types.Tool(
                name="sandbox_logs",
                description="Get logs from a sandbox.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                        "lines": {
                            "type": "integer",
                            "description": "Number of log lines to return (optional)",
                        },
                    },
                    "required": ["sandbox_id"],
                },
            ),
            # ── Sandbox preview ───────────────────────────────────────────
            types.Tool(
                name="sandbox_expose",
                description="Expose a port on a sandbox and return a publicly accessible preview URL.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                        "port": {"type": "integer", "description": "Port number to expose (optional; exposes default port if omitted)"},
                    },
                    "required": ["sandbox_id"],
                },
            ),
            # ── Sandbox deploy ────────────────────────────────────────────
            types.Tool(
                name="sandbox_deploy",
                description="Deploy sandbox contents to production.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sandbox_id": {"type": "string", "description": "Sandbox ID"},
                        "name": {"type": "string", "description": "Deployment name (optional)"},
                        "output_path": {"type": "string", "description": "Path inside the sandbox to deploy (optional)"},
                        "entrypoint": {"type": "string", "description": "Entrypoint command or file (optional)"},
                        "domain": {"type": "string", "description": "Custom domain (optional)"},
                    },
                    "required": ["sandbox_id"],
                },
            ),
            # ── Sandbox templates ─────────────────────────────────────────
            types.Tool(
                name="sandbox_template_list",
                description="List available sandbox templates.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="sandbox_template_create",
                description="Create a custom sandbox template from a build spec.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Template name"},
                        "build_spec": {"type": "object", "description": "Build specification (Dockerfile, packages, etc.)"},
                        "slug": {"type": "string", "description": "URL-friendly identifier (optional)"},
                        "description": {"type": "string", "description": "Human-readable description (optional)"},
                    },
                    "required": ["name", "build_spec"],
                },
            ),
            # ── Deployments ────────────────────────────────────────────────
            types.Tool(
                name="deployment_list",
                description="List all deployments in the tenant.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="deployment_get",
                description="Get details of a specific deployment.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "deployment_id": {"type": "string", "description": "Deployment ID"},
                    },
                    "required": ["deployment_id"],
                },
            ),
            types.Tool(
                name="deployment_create",
                description="Create a new deployment.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Deployment name"},
                        "type": {"type": "string", "description": "Deployment type (e.g. web, worker)"},
                        "source": {"type": "object", "description": "Source configuration"},
                        "env_vars": {"type": "object", "description": "Environment variables as key-value pairs"},
                        "region": {"type": "string", "description": "Deployment region (optional)"},
                    },
                    "required": ["name"],
                },
            ),
            types.Tool(
                name="deployment_delete",
                description="Delete a deployment permanently.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "deployment_id": {"type": "string", "description": "Deployment ID to delete"},
                    },
                    "required": ["deployment_id"],
                },
            ),
            types.Tool(
                name="deployment_publish",
                description="Publish a new version of a deployment.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "deployment_id": {"type": "string", "description": "Deployment ID"},
                        "source": {"type": "object", "description": "Source configuration for the new version"},
                    },
                    "required": ["deployment_id"],
                },
            ),
            types.Tool(
                name="deployment_rollback",
                description="Rollback a deployment to a previous version.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "deployment_id": {"type": "string", "description": "Deployment ID"},
                        "version_id": {"type": "string", "description": "Version ID to roll back to"},
                    },
                    "required": ["deployment_id", "version_id"],
                },
            ),
            types.Tool(
                name="deployment_env_list",
                description="List environment variables for a deployment.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "deployment_id": {"type": "string", "description": "Deployment ID"},
                    },
                    "required": ["deployment_id"],
                },
            ),
            types.Tool(
                name="deployment_env_set",
                description="Set (create or update) an environment variable for a deployment.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "deployment_id": {"type": "string", "description": "Deployment ID"},
                        "key": {"type": "string", "description": "Environment variable name"},
                        "value": {"type": "string", "description": "Environment variable value"},
                    },
                    "required": ["deployment_id", "key", "value"],
                },
            ),
            types.Tool(
                name="deployment_logs",
                description="Get logs for a deployment.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "deployment_id": {"type": "string", "description": "Deployment ID"},
                        "lines": {"type": "integer", "description": "Number of log lines to return (default: 100)", "default": 100},
                        "since": {"type": "string", "description": "ISO 8601 timestamp to fetch logs from (optional)"},
                    },
                    "required": ["deployment_id"],
                },
            ),
            types.Tool(
                name="deployment_version_list",
                description="List all versions of a deployment.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "deployment_id": {"type": "string", "description": "Deployment ID"},
                    },
                    "required": ["deployment_id"],
                },
            ),
            types.Tool(
                name="deployment_version_promote",
                description="Promote a specific version to be the active deployment.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "deployment_id": {"type": "string", "description": "Deployment ID"},
                        "version_id": {"type": "string", "description": "Version ID to promote"},
                    },
                    "required": ["deployment_id", "version_id"],
                },
            ),
            # ── Storage ────────────────────────────────────────────────────
            types.Tool(
                name="storage_bucket_list",
                description="List all storage buckets in the tenant.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="storage_bucket_create",
                description="Create a new storage bucket.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Bucket name"},
                        "region": {"type": "string", "description": "Bucket region (optional)"},
                        "public": {"type": "boolean", "description": "Whether the bucket is publicly readable (default: false)", "default": False},
                    },
                    "required": ["name"],
                },
            ),
            types.Tool(
                name="storage_bucket_delete",
                description="Delete a storage bucket.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "bucket_id": {"type": "string", "description": "Bucket ID or name to delete"},
                    },
                    "required": ["bucket_id"],
                },
            ),
            types.Tool(
                name="storage_object_list",
                description="List objects in a storage bucket, optionally filtered by prefix.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "bucket_id": {"type": "string", "description": "Bucket ID or name"},
                        "prefix": {"type": "string", "description": "Key prefix to filter by (optional)"},
                    },
                    "required": ["bucket_id"],
                },
            ),
            types.Tool(
                name="storage_object_upload",
                description="Upload an object to a storage bucket.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "bucket_id": {"type": "string", "description": "Bucket ID or name"},
                        "key": {"type": "string", "description": "Object key (path within bucket)"},
                        "content": {"type": "string", "description": "Object content (text or base64-encoded binary)"},
                        "content_type": {"type": "string", "description": "MIME type of the object (optional)"},
                    },
                    "required": ["bucket_id", "key", "content"],
                },
            ),
            types.Tool(
                name="storage_object_download",
                description="Download an object from a storage bucket.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "bucket_id": {"type": "string", "description": "Bucket ID or name"},
                        "key": {"type": "string", "description": "Object key to download"},
                    },
                    "required": ["bucket_id", "key"],
                },
            ),
            types.Tool(
                name="storage_object_delete",
                description="Delete an object from a storage bucket.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "bucket_id": {"type": "string", "description": "Bucket ID or name"},
                        "key": {"type": "string", "description": "Object key to delete"},
                    },
                    "required": ["bucket_id", "key"],
                },
            ),
            types.Tool(
                name="storage_presign",
                description="Get a presigned URL for temporary access to a storage object.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "bucket_id": {"type": "string", "description": "Bucket ID or name"},
                        "key": {"type": "string", "description": "Object key"},
                        "expires_in": {"type": "integer", "description": "URL expiry in seconds (default: 3600)", "default": 3600},
                        "method": {"type": "string", "enum": ["GET", "PUT"], "description": "HTTP method (default: GET)", "default": "GET"},
                    },
                    "required": ["bucket_id", "key"],
                },
            ),
            # ── Databases ──────────────────────────────────────────────────
            types.Tool(
                name="database_list",
                description="List all managed databases in the tenant.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="database_create",
                description="Create a new managed database.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Database name"},
                        "engine": {"type": "string", "enum": ["postgres", "mysql", "redis"], "description": "Database engine"},
                        "version": {"type": "string", "description": "Engine version (optional)"},
                        "size": {"type": "string", "description": "Database size/tier (optional)"},
                        "region": {"type": "string", "description": "Region (optional)"},
                    },
                    "required": ["name", "engine"],
                },
            ),
            types.Tool(
                name="database_get",
                description="Get details of a specific database.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "database_id": {"type": "string", "description": "Database ID"},
                    },
                    "required": ["database_id"],
                },
            ),
            types.Tool(
                name="database_delete",
                description="Delete a managed database permanently.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "database_id": {"type": "string", "description": "Database ID to delete"},
                    },
                    "required": ["database_id"],
                },
            ),
            types.Tool(
                name="database_credentials",
                description="Get the connection string and credentials for a database.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "database_id": {"type": "string", "description": "Database ID"},
                    },
                    "required": ["database_id"],
                },
            ),
            types.Tool(
                name="database_logs",
                description="Get logs for a managed database.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "database_id": {"type": "string", "description": "Database ID"},
                        "lines": {"type": "integer", "description": "Number of log lines to return (default: 100)", "default": 100},
                        "since": {"type": "string", "description": "ISO 8601 timestamp to fetch logs from (optional)"},
                    },
                    "required": ["database_id"],
                },
            ),
            # ── Workspaces ─────────────────────────────────────────────────
            types.Tool(
                name="workspace_list",
                description="List all workspaces in the tenant.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="workspace_create",
                description="Create a new workspace.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Workspace name"},
                        "description": {"type": "string", "description": "Workspace description (optional)"},
                    },
                    "required": ["name"],
                },
            ),
            types.Tool(
                name="workspace_get",
                description="Get details of a specific workspace.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string", "description": "Workspace ID"},
                    },
                    "required": ["workspace_id"],
                },
            ),
            types.Tool(
                name="workspace_update",
                description="Update a workspace's name or description.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string", "description": "Workspace ID"},
                        "name": {"type": "string", "description": "New workspace name (optional)"},
                        "description": {"type": "string", "description": "New description (optional)"},
                    },
                    "required": ["workspace_id"],
                },
            ),
            types.Tool(
                name="workspace_stats",
                description="Get resource statistics for a workspace (computers, sandboxes, databases, etc.).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string", "description": "Workspace ID"},
                    },
                    "required": ["workspace_id"],
                },
            ),
            types.Tool(
                name="workspace_usage",
                description="Get usage data (compute hours, storage, bandwidth) for a workspace.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string", "description": "Workspace ID"},
                        "period": {"type": "string", "description": "Billing period (e.g. '2026-05'). Defaults to current month."},
                    },
                    "required": ["workspace_id"],
                },
            ),
            # ── Billing ────────────────────────────────────────────────────
            types.Tool(
                name="billing_usage",
                description="Get current billing period usage for the tenant.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="billing_plan",
                description="Get the current billing plan details for the tenant.",
                inputSchema={"type": "object", "properties": {}},
            ),
            # ── Tunnels / Port forwarding ───────────────────────────────────
            types.Tool(
                name="computer_expose_port",
                description=(
                    "Expose a port on the computer and return the public URL. "
                    "The URL follows the pattern https://{port}-{slug}.computer.miosa.ai."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string", "description": "Computer ID. Omit to use the active computer."},
                        "port": {"type": "integer", "description": "Port number to expose"},
                        "protocol": {
                            "type": "string",
                            "enum": ["http", "https", "tcp"],
                            "description": "Protocol (default: http)",
                        },
                    },
                    "required": ["port"],
                },
            ),
            types.Tool(
                name="computer_list_ports",
                description="List all currently exposed ports on the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string", "description": "Computer ID. Omit to use the active computer."},
                    },
                },
            ),
            types.Tool(
                name="computer_preview_url",
                description=(
                    "Return the public preview URL for a given port on the computer. "
                    "Format: https://{port}-{slug}.computer.miosa.ai"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string", "description": "Computer ID. Omit to use the active computer."},
                        "port": {"type": "integer", "description": "Port number"},
                    },
                    "required": ["port"],
                },
            ),
            # ── Network policy ──────────────────────────────────────────────
            types.Tool(
                name="computer_network_policy_get",
                description="Get the current network policy (firewall rules) for the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string", "description": "Computer ID. Omit to use the active computer."},
                    },
                },
            ),
            types.Tool(
                name="computer_network_policy_set",
                description="Set the network policy (firewall rules) for the computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string", "description": "Computer ID. Omit to use the active computer."},
                        "rules": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "List of firewall rule objects (e.g. {direction, protocol, port, action})",
                        },
                        "default_effect": {
                            "type": "string",
                            "enum": ["allow", "deny"],
                            "description": "Default action when no rule matches (default: allow)",
                        },
                    },
                    "required": ["rules"],
                },
            ),
            types.Tool(
                name="computer_network_policy_reset",
                description="Reset the network policy for the computer to the platform default (allow all).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string", "description": "Computer ID. Omit to use the active computer."},
                    },
                },
            ),
            # ── Webhooks ────────────────────────────────────────────────────
            types.Tool(
                name="webhook_list",
                description="List all webhooks registered in the tenant.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="webhook_create",
                description="Create a new webhook endpoint.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "HTTPS URL to deliver webhook events to"},
                        "events": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of event types to subscribe to (e.g. ['computer.started', 'computer.stopped'])",
                        },
                    },
                    "required": ["url", "events"],
                },
            ),
            types.Tool(
                name="webhook_delete",
                description="Delete a webhook.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "webhook_id": {"type": "string", "description": "Webhook ID to delete"},
                    },
                    "required": ["webhook_id"],
                },
            ),
            types.Tool(
                name="webhook_test",
                description="Send a test event delivery to a webhook endpoint.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "webhook_id": {"type": "string", "description": "Webhook ID to test"},
                    },
                    "required": ["webhook_id"],
                },
            ),
            # ── Functions ───────────────────────────────────────────────────
            types.Tool(
                name="function_list",
                description="List all serverless functions in the tenant.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="function_create",
                description="Create a new serverless function.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Function name"},
                        "runtime": {
                            "type": "string",
                            "description": "Runtime identifier (e.g. 'node20', 'python311', 'go122')",
                        },
                        "code": {
                            "type": "string",
                            "description": "Inline function source code (optional)",
                        },
                    },
                    "required": ["name", "runtime"],
                },
            ),
            types.Tool(
                name="function_invoke",
                description="Invoke a serverless function and return its response.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "function_id": {"type": "string", "description": "Function ID to invoke"},
                        "payload": {
                            "type": "object",
                            "description": "JSON payload to pass to the function (optional)",
                        },
                    },
                    "required": ["function_id"],
                },
            ),
            types.Tool(
                name="function_delete",
                description="Delete a serverless function permanently.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "function_id": {"type": "string", "description": "Function ID to delete"},
                    },
                    "required": ["function_id"],
                },
            ),
            # ── API Keys ────────────────────────────────────────────────────
            types.Tool(
                name="api_key_list",
                description="List all API keys for the tenant.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="api_key_create",
                description="Create a new API key.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Human-readable label for the key"},
                        "scopes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Permission scopes for the key (optional; defaults to full access)",
                        },
                    },
                    "required": ["name"],
                },
            ),
            types.Tool(
                name="api_key_delete",
                description="Revoke and delete an API key.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key_id": {"type": "string", "description": "API key ID to delete"},
                    },
                    "required": ["key_id"],
                },
            ),
            # ── Cron jobs ──────────────────────────────────────────────────
            types.Tool(
                name="cron_list",
                description="List all cron jobs in the tenant, optionally filtered by computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {
                            "type": "string",
                            "description": "Filter cron jobs by computer ID (optional)",
                        },
                    },
                },
            ),
            types.Tool(
                name="cron_create",
                description="Create a new cron job that runs a command on a schedule.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {
                            "type": "string",
                            "description": "ID of the computer to run the cron job on",
                        },
                        "schedule": {
                            "type": "string",
                            "description": "Cron schedule expression (e.g. '0 * * * *' for hourly)",
                        },
                        "command": {
                            "type": "string",
                            "description": "Shell command to execute",
                        },
                        "name": {
                            "type": "string",
                            "description": "Human-readable name for the cron job (optional)",
                        },
                    },
                    "required": ["computer_id", "schedule", "command"],
                },
            ),
            types.Tool(
                name="cron_get",
                description="Get details of a specific cron job.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "cron_id": {
                            "type": "string",
                            "description": "Cron job ID",
                        },
                    },
                    "required": ["cron_id"],
                },
            ),
            types.Tool(
                name="cron_delete",
                description="Delete a cron job permanently.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "cron_id": {
                            "type": "string",
                            "description": "Cron job ID to delete",
                        },
                    },
                    "required": ["cron_id"],
                },
            ),
            types.Tool(
                name="cron_pause",
                description="Pause a cron job so it stops running on schedule.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "cron_id": {
                            "type": "string",
                            "description": "Cron job ID to pause",
                        },
                    },
                    "required": ["cron_id"],
                },
            ),
            types.Tool(
                name="cron_resume",
                description="Resume a paused cron job.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "cron_id": {
                            "type": "string",
                            "description": "Cron job ID to resume",
                        },
                    },
                    "required": ["cron_id"],
                },
            ),
            types.Tool(
                name="cron_run_now",
                description="Trigger an immediate one-off execution of a cron job outside its schedule.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "cron_id": {
                            "type": "string",
                            "description": "Cron job ID to run immediately",
                        },
                    },
                    "required": ["cron_id"],
                },
            ),
            types.Tool(
                name="cron_executions",
                description="List recent execution history for a cron job.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "cron_id": {
                            "type": "string",
                            "description": "Cron job ID",
                        },
                    },
                    "required": ["cron_id"],
                },
            ),
            # ── Regions ────────────────────────────────────────────────────
            types.Tool(
                name="region_list",
                description="List available regions and their GPU availability.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="computer_list_regions",
                description="List available compute regions with GPU availability details.",
                inputSchema={"type": "object", "properties": {}},
            ),
            # ── Computer templates ─────────────────────────────────────────
            types.Tool(
                name="computer_template_list",
                description="List computer templates available in a workspace.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {
                            "type": "string",
                            "description": "Workspace ID to list templates for",
                        },
                    },
                    "required": ["workspace_id"],
                },
            ),
            types.Tool(
                name="computer_template_create",
                description="Create a new computer template in a workspace.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workspace_id": {
                            "type": "string",
                            "description": "Workspace ID to create the template in",
                        },
                        "name": {
                            "type": "string",
                            "description": "Human-readable name for the template",
                        },
                        "template_type": {
                            "type": "string",
                            "description": "Base template type (e.g. miosa-desktop)",
                        },
                        "size": {
                            "type": "string",
                            "enum": ["small", "medium", "large", "xl"],
                            "description": "VM size for the template",
                        },
                        "selected_apps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of app identifiers to pre-install",
                        },
                        "settings": {
                            "type": "object",
                            "description": "Additional template settings (key-value pairs)",
                        },
                    },
                    "required": ["workspace_id", "name"],
                },
            ),
            # ── Settings ───────────────────────────────────────────────────
            types.Tool(
                name="settings_get",
                description="Get all tenant-level settings.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="settings_get_branding",
                description="Get tenant branding settings (logo, wallpaper, colours).",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="settings_update_branding",
                description="Update tenant branding settings.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "desktop_wallpaper_url": {
                            "type": "string",
                            "description": "HTTPS URL for the default desktop wallpaper (optional)",
                        },
                        "logo_url": {
                            "type": "string",
                            "description": "HTTPS URL for the tenant logo (optional)",
                        },
                    },
                },
            ),
            types.Tool(
                name="settings_compute_pricing",
                description="Get compute pricing information for available sizes and GPU types.",
                inputSchema={"type": "object", "properties": {}},
            ),
            # ── Sandbox template extensions ────────────────────────────────
            types.Tool(
                name="sandbox_template_get",
                description="Get details of a specific sandbox template.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "template_id": {
                            "type": "string",
                            "description": "Sandbox template ID or slug",
                        },
                    },
                    "required": ["template_id"],
                },
            ),
            types.Tool(
                name="sandbox_template_builds",
                description="List builds for a sandbox template.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "template_id": {
                            "type": "string",
                            "description": "Sandbox template ID or slug",
                        },
                    },
                    "required": ["template_id"],
                },
            ),
            # ── Volumes ────────────────────────────────────────────────────
            types.Tool(
                name="volume_list",
                description="List all volumes in the tenant.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="volume_create",
                description="Create a new persistent volume.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Human-readable name for the volume"},
                        "size_gb": {"type": "integer", "description": "Size of the volume in GB (optional)"},
                        "region": {"type": "string", "description": "Region to create the volume in (optional)"},
                    },
                    "required": ["name"],
                },
            ),
            types.Tool(
                name="volume_get",
                description="Get details of a specific volume.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "volume_id": {"type": "string", "description": "Volume ID"},
                    },
                    "required": ["volume_id"],
                },
            ),
            types.Tool(
                name="volume_delete",
                description="Delete a volume permanently.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "volume_id": {"type": "string", "description": "Volume ID to delete"},
                    },
                    "required": ["volume_id"],
                },
            ),
            types.Tool(
                name="volume_attach",
                description="Attach a volume to a computer at a given mount path.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string", "description": "Computer ID to attach the volume to"},
                        "volume_id": {"type": "string", "description": "Volume ID to attach"},
                        "mount_path": {"type": "string", "description": "Path inside the VM to mount the volume (optional)"},
                    },
                    "required": ["computer_id", "volume_id"],
                },
            ),
            types.Tool(
                name="volume_detach",
                description="Detach a volume attachment from a computer.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "computer_id": {"type": "string", "description": "Computer ID"},
                        "attachment_id": {"type": "string", "description": "Attachment ID to remove"},
                    },
                    "required": ["computer_id", "attachment_id"],
                },
            ),
            # ── Egress — secrets, network policy, audit, OAuth catalog ─────
            *EGRESS_TOOLS,
        ]

    # ── Tool call dispatch ─────────────────────────────────────────────────

    @app.call_tool()
    async def call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[types.TextContent | types.ImageContent]:
        try:
            return await _dispatch(client, name, arguments)
        except MiosaError as exc:
            logger.error("MIOSA API error in tool %s: %s", name, exc)
            return _err(str(exc))
        except ValueError as exc:
            logger.warning("Bad arguments for tool %s: %s", name, exc)
            return _err(str(exc))
        except Exception as exc:
            logger.exception("Unexpected error in tool %s", name)
            return _err(f"Unexpected error: {exc}")

    return app


async def _dispatch(
    client: AsyncMiosa,
    name: str,
    args: dict[str, Any],
) -> list[types.TextContent | types.ImageContent]:
    """Route tool calls to the appropriate MIOSA SDK method."""

    global _last_computer_id
    cid: str | None = args.get("computer_id") or None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    if name == "computer_create":
        create_kwargs: dict[str, Any] = {
            "name": args["name"],
            "template_type": args.get("template_type", "miosa-desktop"),
            "size": args.get("size", "small"),
            "workspace_id": args.get("workspace_id") or None,
            "external_workspace_id": args.get("external_workspace_id") or None,
            "external_project_id": args.get("external_project_id") or None,
        }
        if args.get("gpu_model"):
            create_kwargs["gpu_model"] = args["gpu_model"]
            create_kwargs["gpu_count"] = int(args.get("gpu_count", 1))
        computer = await client.computers.create(**create_kwargs)
        # Start and cache
        await computer.start()
        _set_active(computer)
        return _ok(
            f"Created computer '{computer.name}' (id={computer.id}, status={computer.status}). "
            "This is now the active computer."
        )

    if name == "computer_list":
        computers = await client.computers.list()
        if not computers:
            return _ok("No computers found.")
        lines = ["Available computers:"]
        for c in computers:
            marker = " [active]" if c.id == _last_computer_id else ""
            lines.append(f"  {c.id}  {c.name}  {c.status}{marker}")
            # Populate cache for fast subsequent access
            _computers[c.id] = c
        return _ok("\n".join(lines))

    if name == "computer_destroy":
        computer = await _resolve(client, cid)
        await computer.destroy()
        _computers.pop(computer.id, None)
        if _last_computer_id == computer.id:
            _last_computer_id = None
        return _ok(f"Computer {computer.id} destroyed.")

    if name == "computer_get":
        required_cid = args.get("computer_id") or None
        if not required_cid:
            return _err("computer_id is required")
        computer = await client.computers.get(required_cid)
        _set_active(computer)
        return _ok(
            f"id={computer.id}  name={computer.name!r}  status={computer.status}"
        )

    if name == "computer_start":
        computer = await _resolve(client, cid)
        result = await computer.start()
        return _ok(f"Computer {computer.id} start issued (status={getattr(result, 'status', 'ok')}).")

    if name == "computer_stop":
        computer = await _resolve(client, cid)
        result = await computer.stop()
        return _ok(f"Computer {computer.id} stop issued (status={getattr(result, 'status', 'ok')}).")

    if name == "computer_restart":
        computer = await _resolve(client, cid)
        result = await computer.restart()
        return _ok(f"Computer {computer.id} restart issued (status={getattr(result, 'status', 'ok')}).")

    if name == "computer_update":
        computer = await _resolve(client, cid)
        updated = await client.computers.update(
            computer.id,
            name=args.get("name") or None,
            metadata=args.get("metadata") or None,
        )
        return _ok(f"Computer {updated.id} updated: name={updated.name!r}.")

    # ── Screenshot ────────────────────────────────────────────────────────

    if name == "computer_screenshot":
        computer = await _resolve(client, cid)
        png = await computer.screenshot()
        return _image(png)

    # ── Pointer ───────────────────────────────────────────────────────────

    if name == "computer_click":
        computer = await _resolve(client, cid)
        await computer.click(
            x=int(args["x"]),
            y=int(args["y"]),
            button=args.get("button", "left"),
        )
        return _ok(f"Clicked ({args['x']}, {args['y']}) button={args.get('button', 'left')}")

    if name == "computer_double_click":
        computer = await _resolve(client, cid)
        await computer.double_click(x=int(args["x"]), y=int(args["y"]))
        return _ok(f"Double-clicked ({args['x']}, {args['y']})")

    if name == "computer_move_cursor":
        computer = await _resolve(client, cid)
        await computer.move_cursor(x=int(args["x"]), y=int(args["y"]))
        return _ok(f"Moved cursor to ({args['x']}, {args['y']})")

    if name == "computer_drag":
        computer = await _resolve(client, cid)
        await computer.drag(
            from_x=int(args["from_x"]),
            from_y=int(args["from_y"]),
            to_x=int(args["to_x"]),
            to_y=int(args["to_y"]),
        )
        return _ok(
            f"Dragged from ({args['from_x']}, {args['from_y']}) "
            f"to ({args['to_x']}, {args['to_y']})"
        )

    if name == "computer_scroll":
        computer = await _resolve(client, cid)
        direction = args.get("direction", "down")
        clicks = int(args.get("clicks", 3))
        x = args.get("x")
        y = args.get("y")
        scroll_kwargs: dict[str, Any] = {}
        if x is not None:
            scroll_kwargs["x"] = int(x)
        if y is not None:
            scroll_kwargs["y"] = int(y)
        await computer.scroll(direction=direction, clicks=clicks, **scroll_kwargs)
        return _ok(f"Scrolled {direction} by {clicks} clicks")

    # ── Keyboard ──────────────────────────────────────────────────────────

    if name == "computer_type":
        computer = await _resolve(client, cid)
        await computer.type(args["text"])
        preview = args["text"][:40] + ("..." if len(args["text"]) > 40 else "")
        return _ok(f"Typed: {preview!r}")

    if name == "computer_key":
        computer = await _resolve(client, cid)
        await computer.key(args["key"])
        return _ok(f"Pressed key: {args['key']}")

    if name == "computer_hotkey":
        computer = await _resolve(client, cid)
        keys: list[str] = args["keys"]
        await computer.hotkey(*keys)
        return _ok(f"Pressed hotkey: {'+'.join(keys)}")

    # ── Display info ──────────────────────────────────────────────────────

    if name == "computer_get_screen_size":
        computer = await _resolve(client, cid)
        size = await computer.get_screen_size()
        return _ok(f"Screen size: {size.width}x{size.height} px")

    if name == "computer_get_cursor_position":
        computer = await _resolve(client, cid)
        pos = await computer.get_cursor_position()
        return _ok(f"Cursor position: x={pos.x}, y={pos.y}")

    # ── Clipboard ─────────────────────────────────────────────────────────

    if name == "computer_get_clipboard":
        computer = await _resolve(client, cid)
        text = await computer.get_clipboard()
        return _ok(f"Clipboard content:\n{text}")

    if name == "computer_set_clipboard":
        computer = await _resolve(client, cid)
        await computer.set_clipboard(args["text"])
        return _ok("Clipboard updated.")

    # ── Window management ─────────────────────────────────────────────────

    if name == "computer_windows":
        computer = await _resolve(client, cid)
        windows = await computer.windows()
        if not windows:
            return _ok("No open windows found.")
        lines = ["Open windows:"]
        for w in windows:
            focused = " [focused]" if w.focused else ""
            lines.append(
                f"  id={w.id}  title={w.title!r}  app={w.app!r}"
                f"  pos=({w.x},{w.y})  size={w.width}x{w.height}{focused}"
            )
        return _ok("\n".join(lines))

    if name == "computer_launch":
        computer = await _resolve(client, cid)
        await computer.launch(args["app"])
        return _ok(f"Launched: {args['app']}")

    # ── Shell & Files ─────────────────────────────────────────────────────

    if name == "computer_bash":
        computer = await _resolve(client, cid)
        timeout = args.get("timeout")
        result = await computer.bash(
            args["command"],
            timeout=int(timeout) if timeout is not None else None,
        )
        parts = []
        if result.output:
            parts.append(f"stdout:\n{result.output}")
        if result.stderr:
            parts.append(f"stderr:\n{result.stderr}")
        parts.append(f"exit_code: {result.exit_code}")
        return _ok("\n".join(parts))

    if name == "computer_write_file":
        computer = await _resolve(client, cid)
        await computer.write_file(args["path"], args["content"])
        return _ok(f"Wrote {len(args['content'])} bytes to {args['path']}")

    if name == "computer_read_file":
        computer = await _resolve(client, cid)
        content = await computer.read_file(args["path"])
        return _ok(content)

    # ── Extended pointer ──────────────────────────────────────────────────

    if name == "computer_right_click":
        computer = await _resolve(client, cid)
        await computer.right_click(x=int(args["x"]), y=int(args["y"]))
        return _ok(f"Right-clicked ({args['x']}, {args['y']})")

    if name == "computer_mouse_down":
        computer = await _resolve(client, cid)
        await computer.mouse_down(
            x=int(args["x"]),
            y=int(args["y"]),
            button=args.get("button", "left"),
        )
        return _ok(f"Mouse down at ({args['x']}, {args['y']}) button={args.get('button', 'left')}")

    if name == "computer_mouse_up":
        computer = await _resolve(client, cid)
        await computer.mouse_up(
            x=int(args["x"]),
            y=int(args["y"]),
            button=args.get("button", "left"),
        )
        return _ok(f"Mouse up at ({args['x']}, {args['y']}) button={args.get('button', 'left')}")

    # ── Extended keyboard ─────────────────────────────────────────────────

    if name == "computer_key_down":
        computer = await _resolve(client, cid)
        await computer.key_down(args["key"])
        return _ok(f"Key down: {args['key']}")

    if name == "computer_key_up":
        computer = await _resolve(client, cid)
        await computer.key_up(args["key"])
        return _ok(f"Key up: {args['key']}")

    # ── Wait ──────────────────────────────────────────────────────────────

    if name == "computer_wait":
        computer = await _resolve(client, cid)
        await computer.wait(float(args["seconds"]))
        return _ok(f"Waited {args['seconds']}s")

    # ── Window management (extended) ──────────────────────────────────────

    if name == "computer_focus_window":
        computer = await _resolve(client, cid)
        await computer.focus_window(args["window_id"])
        return _ok(f"Focused window {args['window_id']}")

    if name == "computer_set_window_size":
        computer = await _resolve(client, cid)
        await computer.set_window_size(
            args["window_id"],
            width=int(args["width"]),
            height=int(args["height"]),
        )
        return _ok(f"Resized window {args['window_id']} to {args['width']}x{args['height']}")

    if name == "computer_set_window_position":
        computer = await _resolve(client, cid)
        await computer.set_window_position(
            args["window_id"],
            x=int(args["x"]),
            y=int(args["y"]),
        )
        return _ok(f"Moved window {args['window_id']} to ({args['x']}, {args['y']})")

    if name == "computer_maximize_window":
        computer = await _resolve(client, cid)
        await computer.maximize_window(args["window_id"])
        return _ok(f"Maximized window {args['window_id']}")

    if name == "computer_minimize_window":
        computer = await _resolve(client, cid)
        await computer.minimize_window(args["window_id"])
        return _ok(f"Minimized window {args['window_id']}")

    if name == "computer_close_window":
        computer = await _resolve(client, cid)
        await computer.close_window(args["window_id"])
        return _ok(f"Closed window {args['window_id']}")

    # ── Desktop environment ───────────────────────────────────────────────

    if name == "computer_get_desktop_env":
        computer = await _resolve(client, cid)
        env = await computer.get_desktop_env()
        return _ok(
            f"desktop_env: name={getattr(env, 'name', '?')!r}  "
            f"resolution={getattr(env, 'resolution', '?')}  "
            f"session_type={getattr(env, 'session_type', '?')!r}"
        )

    if name == "computer_set_wallpaper":
        computer = await _resolve(client, cid)
        await computer.set_wallpaper(args["path"])
        return _ok(f"Wallpaper set to: {args['path']}")

    if name == "computer_accessibility_tree":
        computer = await _resolve(client, cid)
        import json as _json
        tree = await computer.accessibility_tree()
        return _ok(_json.dumps(tree, indent=2)[:8000])  # cap at 8KB to avoid token overflow

    # ── Files — extended ──────────────────────────────────────────────────

    if name == "computer_list_files":
        computer = await _resolve(client, cid)
        path = args.get("path") or None
        entries = await computer.files.list(path)
        if not entries:
            return _ok(f"No files found at {path or '/'}.")
        lines = [f"Files at {path or '/'}:"]
        for e in entries:
            lines.append(f"  {e.name}  {e.type}  {e.size}")
        return _ok("\n".join(lines))

    if name == "computer_stat_file":
        computer = await _resolve(client, cid)
        stat = await computer.files.stat(args["path"])
        lines = [
            f"path: {args['path']}",
            f"type: {stat.type}",
            f"size: {stat.size}",
            f"mode: {stat.mode}",
            f"mtime: {stat.mtime}",
        ]
        return _ok("\n".join(lines))

    if name == "computer_mkdir":
        computer = await _resolve(client, cid)
        recursive = bool(args.get("recursive", True))
        await computer.files.mkdir(args["path"], recursive=recursive)
        return _ok(f"Created directory {args['path']}")

    if name == "computer_rename_file":
        computer = await _resolve(client, cid)
        await computer.files.rename(args["source"], args["dest"])
        return _ok(f"Renamed {args['source']} -> {args['dest']}")

    if name == "computer_copy_file":
        computer = await _resolve(client, cid)
        recursive = bool(args.get("recursive", False))
        await computer.files.copy(args["source"], args["dest"], recursive=recursive)
        return _ok(f"Copied {args['source']} -> {args['dest']}")

    if name == "computer_delete_file":
        computer = await _resolve(client, cid)
        await computer.files.delete(args["path"])
        return _ok(f"Deleted {args['path']}")

    if name == "computer_upload_file":
        computer = await _resolve(client, cid)
        await computer.files.upload(args["local_path"], args["remote_path"])
        return _ok(f"Uploaded {args['local_path']} -> {args['remote_path']}")

    # ── Checkpoints ───────────────────────────────────────────────────────

    if name == "computer_checkpoint_create":
        computer = await _resolve(client, cid)
        comment = args.get("comment") or None
        snap = await computer.checkpoints.create(comment=comment)
        return _ok(
            f"Checkpoint created: id={snap.id}  status={snap.status}"
            + (f"  comment={snap.comment!r}" if snap.comment else "")
        )

    if name == "computer_checkpoint_list":
        computer = await _resolve(client, cid)
        snaps = await computer.checkpoints.list()
        if not snaps:
            return _ok("No checkpoints found.")
        lines = ["Checkpoints:"]
        for s in snaps:
            comment_part = f"  {s.comment!r}" if s.comment else ""
            lines.append(f"  {s.id}  {s.status}  {s.created_at}{comment_part}")
        return _ok("\n".join(lines))

    if name == "computer_checkpoint_restore":
        computer = await _resolve(client, cid)
        restored = await computer.checkpoints.restore(args["checkpoint_id"])
        _set_active(restored)
        return _ok(
            f"Restored checkpoint {args['checkpoint_id']} -> "
            f"new computer id={restored.id}  status={restored.status}. "
            "This is now the active computer."
        )

    if name == "computer_checkpoint_delete":
        computer = await _resolve(client, cid)
        snap = await computer.checkpoints.delete(args["checkpoint_id"])
        return _ok(f"Checkpoint {snap.id} deleted (status={snap.status}).")

    # ── Services ──────────────────────────────────────────────────────────

    if name == "computer_service_create":
        computer = await _resolve(client, cid)
        svc = await computer.services.create(
            args["name"],
            args["command"],
            working_dir=args.get("working_dir") or None,
            port=int(args["port"]) if args.get("port") is not None else None,
            restart_policy=args.get("restart_policy") or None,
        )
        return _ok(f"Service created: id={svc.id}  name={svc.name!r}  status={svc.status}")

    if name == "computer_service_list":
        computer = await _resolve(client, cid)
        services = await computer.services.list()
        if not services:
            return _ok("No services found.")
        lines = ["Services:"]
        for s in services:
            lines.append(f"  {s.id}  {s.name!r}  {s.status}")
        return _ok("\n".join(lines))

    if name == "computer_service_start":
        computer = await _resolve(client, cid)
        svc = await computer.services.start(args["service_id"])
        return _ok(f"Service {svc.id} started (status={svc.status}).")

    if name == "computer_service_stop":
        computer = await _resolve(client, cid)
        svc = await computer.services.stop(args["service_id"])
        return _ok(f"Service {svc.id} stopped (status={svc.status}).")

    if name == "computer_service_restart":
        computer = await _resolve(client, cid)
        svc = await computer.services.restart(args["service_id"])
        return _ok(f"Service {svc.id} restarted (status={svc.status}).")

    if name == "computer_service_logs":
        computer = await _resolve(client, cid)
        lines: list[str] = []
        async for event in computer.services.logs(args["service_id"], follow=False):
            lines.append(f"[{event.stream}] {event.line}")
            if len(lines) >= 100:
                break
        return _ok("\n".join(lines) if lines else "No log output.")

    if name == "computer_service_delete":
        computer = await _resolve(client, cid)
        await computer.services.delete(args["service_id"])
        return _ok(f"Service {args['service_id']} deleted.")

    # ── Env vars ──────────────────────────────────────────────────────────

    if name == "computer_env_list":
        computer = await _resolve(client, cid)
        env_vars = await computer.env.list()
        if not env_vars:
            return _ok("No environment variables set.")
        lines = ["Environment variables:"]
        for e in env_vars:
            name_key = e.get("name", e.get("key", ""))
            val = e.get("value", "")
            lines.append(f"  {name_key}={val}")
        return _ok("\n".join(lines))

    if name == "computer_env_set":
        computer = await _resolve(client, cid)
        await computer.env.set(args["name"], args["value"])
        return _ok(f"Set env var {args['name']}.")

    if name == "computer_env_delete":
        computer = await _resolve(client, cid)
        await computer.env.delete(args["name"])
        return _ok(f"Deleted env var {args['name']}.")

    # ── Logs ──────────────────────────────────────────────────────────────

    if name == "computer_logs":
        computer = await _resolve(client, cid)
        lines_count = int(args["lines"]) if args.get("lines") is not None else None
        result = await computer.logs.get(lines=lines_count)
        # result may be {lines: [...]} or a string payload
        if isinstance(result, dict):
            log_lines = result.get("lines") or result.get("data") or result.get("logs") or []
            if isinstance(log_lines, list):
                return _ok("\n".join(str(l) for l in log_lines) if log_lines else "No logs.")
            return _ok(str(log_lines))
        return _ok(str(result))

    # ── Domains ───────────────────────────────────────────────────────────

    if name == "computer_domain_add":
        computer = await _resolve(client, cid)
        domain = await computer.domains.register(args["fqdn"])
        lines = [
            f"Domain registered: id={domain.id}  fqdn={domain.fqdn}  status={domain.status}",
        ]
        if hasattr(domain, "verification_target") and domain.verification_target:
            lines.append(f"  CNAME target: {domain.verification_target}")
        if hasattr(domain, "instructions") and domain.instructions:
            lines.append(f"  Instructions: {domain.instructions}")
        return _ok("\n".join(lines))

    if name == "computer_domain_list":
        computer = await _resolve(client, cid)
        domains = await computer.domains.list()
        if not domains:
            return _ok("No custom domains registered.")
        lines = ["Custom domains:"]
        for d in domains:
            lines.append(f"  {d.id}  {d.fqdn}  {d.status}")
        return _ok("\n".join(lines))

    if name == "computer_domain_delete":
        computer = await _resolve(client, cid)
        await computer.domains.delete(args["domain_id"])
        return _ok(f"Domain {args['domain_id']} deleted.")

    # ── Sandbox helpers ────────────────────────────────────────────────────

    async def _get_sandbox(sandbox_id: str) -> AsyncSandbox:
        return await client.sandboxes.get(sandbox_id)

    # ── Sandbox lifecycle ──────────────────────────────────────────────────

    if name == "sandbox_create":
        body: dict[str, Any] = {}
        if args.get("name"):
            body["name"] = args["name"]
        if args.get("template_id"):
            body["template_id"] = args["template_id"]
        if args.get("cpu_count") is not None:
            body["cpu_count"] = int(args["cpu_count"])
        if args.get("memory_mb") is not None:
            body["memory_mb"] = int(args["memory_mb"])
        if args.get("timeout_sec") is not None:
            body["timeout_sec"] = int(args["timeout_sec"])
        sandbox = await client.sandboxes.create(**body)
        return _ok(
            f"Created sandbox '{sandbox.data.get('name', sandbox.id)}' "
            f"(id={sandbox.id}, state={sandbox.state})."
        )

    if name == "sandbox_list":
        state_filter = args.get("state") or None
        sandboxes = await client.sandboxes.list(state=state_filter)
        if not sandboxes:
            return _ok("No sandboxes found.")
        lines = ["Sandboxes:"]
        for s in sandboxes:
            lines.append(
                f"  {s.id}  {s.data.get('name', '')}  {s.state}  "
                f"template={s.template_id}"
            )
        return _ok("\n".join(lines))

    if name == "sandbox_get":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        d = sandbox.data
        lines = [
            f"id: {sandbox.id}",
            f"state: {sandbox.state}",
            f"template_id: {sandbox.template_id}",
            f"ready: {sandbox.ready}",
        ]
        if d.get("name"):
            lines.insert(1, f"name: {d['name']}")
        if sandbox.preview_url:
            lines.append(f"preview_url: {sandbox.preview_url}")
        if sandbox.boot_ms is not None:
            lines.append(f"boot_ms: {sandbox.boot_ms}")
        return _ok("\n".join(lines))

    if name == "sandbox_destroy":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        await sandbox.destroy()
        return _ok(f"Sandbox {sid} destroyed.")

    if name == "sandbox_pause":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        await sandbox.pause()
        return _ok(f"Sandbox {sid} paused.")

    if name == "sandbox_resume":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        await sandbox.resume()
        return _ok(f"Sandbox {sid} resumed.")

    # ── Sandbox files ──────────────────────────────────────────────────────

    if name == "sandbox_write_file":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        await sandbox.write_file(args["path"], args["content"])
        return _ok(f"Wrote {len(args['content'])} bytes to {args['path']}")

    if name == "sandbox_read_file":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        content = await sandbox.read_file(args["path"])
        return _ok(str(content))

    if name == "sandbox_list_files":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        path = args.get("path", "/workspace")
        depth = args.get("depth")
        result = await sandbox.list_files(path, depth=int(depth) if depth is not None else None)
        import json as _json
        return _ok(_json.dumps(result, indent=2))

    if name == "sandbox_upload":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        await sandbox.upload(args["path"], args["content"])
        return _ok(f"Uploaded {args['path']} to sandbox {sid}.")

    # ── Sandbox exec ───────────────────────────────────────────────────────

    if name == "sandbox_exec":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        from miosa.resources.sandboxes import ExecOptions as _ExecOpts
        opts: _ExecOpts = {}
        if args.get("cwd"):
            opts["cwd"] = args["cwd"]
        if args.get("timeout") is not None:
            opts["timeout"] = int(args["timeout"])
        result = await sandbox._run_exec(args["command"], opts or None)
        parts = []
        if result.stdout:
            parts.append(f"stdout:\n{result.stdout}")
        if result.stderr:
            parts.append(f"stderr:\n{result.stderr}")
        parts.append(f"exit_code: {result.exit_code}")
        return _ok("\n".join(parts))

    if name == "sandbox_python":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        from miosa.resources.sandboxes import ExecOptions as _ExecOpts
        # Write code to a temp file then execute with python3
        code = args["code"]
        tmp_path = "/tmp/_mcp_run.py"
        await sandbox.write_file(tmp_path, code)
        opts2: _ExecOpts = {}
        if args.get("timeout") is not None:
            opts2["timeout"] = int(args["timeout"])
        result = await sandbox._run_exec(f"python3 {tmp_path}", opts2 or None)
        parts = []
        if result.stdout:
            parts.append(f"stdout:\n{result.stdout}")
        if result.stderr:
            parts.append(f"stderr:\n{result.stderr}")
        parts.append(f"exit_code: {result.exit_code}")
        return _ok("\n".join(parts))

    # ── Sandbox snapshots ──────────────────────────────────────────────────

    if name == "sandbox_snapshot_create":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        comment = args.get("comment") or None
        snap = await sandbox.create_snapshot(comment=comment)
        snap_id = snap.get("id") or snap.get("snapshot_id") if isinstance(snap, dict) else str(snap)
        return _ok(f"Snapshot created: id={snap_id}")

    if name == "sandbox_snapshot_list":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        snapshots = await sandbox.list_snapshots()
        if not snapshots:
            return _ok("No snapshots found.")
        lines = ["Snapshots:"]
        for s in snapshots:
            lines.append(
                f"  {s.get('id')}  {s.get('created_at', '')}  "
                f"{s.get('comment', '')}"
            )
        return _ok("\n".join(lines))

    if name == "sandbox_snapshot_restore":
        sid = args["sandbox_id"]
        snapshot_id = args["snapshot_id"]
        sandbox = await client.sandboxes.get(sid)
        restored = await sandbox.restore_snapshot(snapshot_id)
        return _ok(
            f"Sandbox {sid} restored from snapshot {snapshot_id} "
            f"(new state={restored.state})."
        )

    # ── Sandbox logs ───────────────────────────────────────────────────────

    if name == "sandbox_logs":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        lines_count = args.get("lines")
        logs = await sandbox.get_logs(
            lines=int(lines_count) if lines_count is not None else None
        )
        if isinstance(logs, dict):
            import json as _json
            return _ok(_json.dumps(logs, indent=2))
        return _ok(str(logs))

    # ── Sandbox preview ────────────────────────────────────────────────────

    if name == "sandbox_expose":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        port = args.get("port")
        url = await sandbox.expose(port=int(port) if port is not None else None)
        return _ok(f"Preview URL: {url}")

    # ── Sandbox deploy ─────────────────────────────────────────────────────

    if name == "sandbox_deploy":
        sid = args["sandbox_id"]
        sandbox = await client.sandboxes.get(sid)
        deploy_result = await sandbox.deploy(
            name=args.get("name") or None,
            output_path=args.get("output_path") or None,
            entrypoint=args.get("entrypoint") or None,
            domain=args.get("domain") or None,
        )
        url = deploy_result.get("url") or deploy_result.get("preview_url") or ""
        did = deploy_result.get("id") or deploy_result.get("deployment_id") or "unknown"
        return _ok(f"Deployed sandbox {sid} (deployment id={did}, url={url}).")

    # ── Sandbox templates ──────────────────────────────────────────────────

    if name == "sandbox_template_list":
        templates = await client.sandboxes.list_templates()
        import json as _json
        if isinstance(templates, dict):
            items = (
                templates.get("data")
                or templates.get("templates")
                or templates.get("items")
                or list(templates.values())
            )
            if isinstance(items, list):
                if not items:
                    return _ok("No templates found.")
                lines = ["Templates:"]
                for t in items:
                    if isinstance(t, dict):
                        lines.append(
                            f"  {t.get('id') or t.get('slug')}  {t.get('name')}  "
                            f"{t.get('description', '')}"
                        )
                    else:
                        lines.append(f"  {t}")
                return _ok("\n".join(lines))
            return _ok(_json.dumps(templates, indent=2))
        return _ok(str(templates))

    if name == "sandbox_template_create":
        result = await client.sandboxes.create_template(
            name=args["name"],
            build_spec=args["build_spec"],
            slug=args.get("slug") or None,
            description=args.get("description") or None,
        )
        import json as _json
        d = result if isinstance(result, dict) else {}
        return _ok(
            f"Created template '{d.get('name', args['name'])}' (id={d.get('id')})."
        )

    # ── Deployments ────────────────────────────────────────────────────────

    def _unwrap(raw: Any) -> Any:
        """Unwrap { data: ... } envelope if present."""
        if isinstance(raw, dict) and "data" in raw:
            return raw["data"]
        return raw

    if name == "deployment_list":
        raw = await client._transport.request("GET", "/api/v1/deployments")
        deployments = _unwrap(raw)
        if not deployments:
            return _ok("No deployments found.")
        lines = ["Deployments:"]
        for d in deployments:
            lines.append(f"  {d.get('id')}  {d.get('name')}  {d.get('status', d.get('state', ''))}")
        return _ok("\n".join(lines))

    if name == "deployment_get":
        did = args["deployment_id"]
        raw = await client._transport.request("GET", f"/api/v1/deployments/{did}")
        return _ok(str(raw))

    if name == "deployment_create":
        body: dict[str, Any] = {"name": args["name"]}
        if args.get("type"):
            body["type"] = args["type"]
        if args.get("source"):
            body["source"] = args["source"]
        if args.get("env_vars"):
            body["env_vars"] = args["env_vars"]
        if args.get("region"):
            body["region"] = args["region"]
        raw = await client._transport.request("POST", "/api/v1/deployments", json_body=body)
        d = _unwrap(raw)
        return _ok(f"Created deployment '{d.get('name', args['name'])}' (id={d.get('id')})")

    if name == "deployment_delete":
        did = args["deployment_id"]
        await client._transport.request("DELETE", f"/api/v1/deployments/{did}")
        return _ok(f"Deployment {did} deleted.")

    if name == "deployment_publish":
        did = args["deployment_id"]
        body = {}
        if args.get("source"):
            body["source"] = args["source"]
        raw = await client._transport.request("POST", f"/api/v1/deployments/{did}/publish", json_body=body)
        d = _unwrap(raw)
        return _ok(f"Published deployment {did} (version id={d.get('id', 'unknown')})")

    if name == "deployment_rollback":
        did = args["deployment_id"]
        vid = args["version_id"]
        await client._transport.request("POST", f"/api/v1/deployments/{did}/rollback", json_body={"version_id": vid})
        return _ok(f"Deployment {did} rolled back to version {vid}.")

    if name == "deployment_env_list":
        did = args["deployment_id"]
        raw = await client._transport.request("GET", f"/api/v1/deployments/{did}/env")
        env_vars = _unwrap(raw)
        if not env_vars:
            return _ok("No environment variables set.")
        lines = ["Environment variables:"]
        if isinstance(env_vars, dict):
            for k, v in env_vars.items():
                lines.append(f"  {k}={v}")
        else:
            for e in env_vars:
                lines.append(f"  {e.get('key')}={e.get('value')}")
        return _ok("\n".join(lines))

    if name == "deployment_env_set":
        did = args["deployment_id"]
        await client._transport.request(
            "POST",
            f"/api/v1/deployments/{did}/env",
            json_body={"key": args["key"], "value": args["value"]},
        )
        return _ok(f"Set {args['key']} on deployment {did}.")

    if name == "deployment_logs":
        did = args["deployment_id"]
        log_params: dict[str, Any] = {"lines": args.get("lines", 100)}
        if args.get("since"):
            log_params["since"] = args["since"]
        raw = await client._transport.request("GET", f"/api/v1/deployments/{did}/logs", params=log_params)
        logs = _unwrap(raw)
        if isinstance(logs, list):
            return _ok("\n".join(str(line) for line in logs) if logs else "No logs.")
        return _ok(str(logs))

    if name == "deployment_version_list":
        did = args["deployment_id"]
        raw = await client._transport.request("GET", f"/api/v1/deployments/{did}/versions")
        versions = _unwrap(raw)
        if not versions:
            return _ok("No versions found.")
        lines = ["Versions:"]
        for v in versions:
            lines.append(f"  {v.get('id')}  {v.get('created_at', '')}  {v.get('status', '')}")
        return _ok("\n".join(lines))

    if name == "deployment_version_promote":
        did = args["deployment_id"]
        vid = args["version_id"]
        await client._transport.request("POST", f"/api/v1/deployments/{did}/versions/{vid}/promote", json_body={})
        return _ok(f"Version {vid} promoted on deployment {did}.")

    # ── Storage ────────────────────────────────────────────────────────────

    if name == "storage_bucket_list":
        raw = await client._transport.request("GET", "/api/v1/storage/buckets")
        buckets = _unwrap(raw)
        if not buckets:
            return _ok("No buckets found.")
        lines = ["Buckets:"]
        for b in buckets:
            lines.append(f"  {b.get('id')}  {b.get('name')}  {b.get('region', '')}")
        return _ok("\n".join(lines))

    if name == "storage_bucket_create":
        body = {"name": args["name"]}
        if args.get("region"):
            body["region"] = args["region"]
        if args.get("public") is not None:
            body["public"] = args["public"]
        raw = await client._transport.request("POST", "/api/v1/storage/buckets", json_body=body)
        b = _unwrap(raw)
        return _ok(f"Created bucket '{b.get('name', args['name'])}' (id={b.get('id')})")

    if name == "storage_bucket_delete":
        bid = args["bucket_id"]
        await client._transport.request("DELETE", f"/api/v1/storage/buckets/{bid}")
        return _ok(f"Bucket {bid} deleted.")

    if name == "storage_object_list":
        bid = args["bucket_id"]
        obj_params: dict[str, Any] = {}
        if args.get("prefix"):
            obj_params["prefix"] = args["prefix"]
        raw = await client._transport.request("GET", f"/api/v1/storage/buckets/{bid}/objects", params=obj_params)
        objects = _unwrap(raw)
        if not objects:
            return _ok("No objects found.")
        lines = ["Objects:"]
        for o in objects:
            lines.append(f"  {o.get('key')}  {o.get('size', '')}  {o.get('last_modified', '')}")
        return _ok("\n".join(lines))

    if name == "storage_object_upload":
        bid = args["bucket_id"]
        await client._transport.request(
            "POST",
            f"/api/v1/storage/buckets/{bid}/objects",
            json_body={
                "key": args["key"],
                "content": args["content"],
                "content_type": args.get("content_type", "application/octet-stream"),
            },
        )
        return _ok(f"Uploaded {args['key']} to bucket {bid}.")

    if name == "storage_object_download":
        bid = args["bucket_id"]
        raw = await client._transport.request("GET", f"/api/v1/storage/buckets/{bid}/objects/{args['key']}")
        d = _unwrap(raw)
        content = d.get("content", str(d)) if isinstance(d, dict) else str(d)
        return _ok(content)

    if name == "storage_object_delete":
        bid = args["bucket_id"]
        await client._transport.request("DELETE", f"/api/v1/storage/buckets/{bid}/objects/{args['key']}")
        return _ok(f"Deleted {args['key']} from bucket {bid}.")

    if name == "storage_presign":
        bid = args["bucket_id"]
        raw = await client._transport.request(
            "POST",
            f"/api/v1/storage/buckets/{bid}/presign",
            json_body={
                "key": args["key"],
                "expires_in": args.get("expires_in", 3600),
                "method": args.get("method", "GET"),
            },
        )
        d = _unwrap(raw)
        url = d.get("url", str(d)) if isinstance(d, dict) else str(d)
        return _ok(f"Presigned URL: {url}")

    # ── Databases ──────────────────────────────────────────────────────────

    if name == "database_list":
        raw = await client._transport.request("GET", "/api/v1/databases")
        databases = _unwrap(raw)
        if not databases:
            return _ok("No databases found.")
        lines = ["Databases:"]
        for db in databases:
            lines.append(f"  {db.get('id')}  {db.get('name')}  {db.get('engine', '')}  {db.get('status', '')}")
        return _ok("\n".join(lines))

    if name == "database_create":
        body = {"name": args["name"], "engine": args["engine"]}
        for key in ("version", "size", "region"):
            if args.get(key):
                body[key] = args[key]
        raw = await client._transport.request("POST", "/api/v1/databases", json_body=body)
        d = _unwrap(raw)
        return _ok(f"Created database '{d.get('name', args['name'])}' (id={d.get('id')})")

    if name == "database_get":
        dbid = args["database_id"]
        raw = await client._transport.request("GET", f"/api/v1/databases/{dbid}")
        return _ok(str(raw))

    if name == "database_delete":
        dbid = args["database_id"]
        await client._transport.request("DELETE", f"/api/v1/databases/{dbid}")
        return _ok(f"Database {dbid} deleted.")

    if name == "database_credentials":
        dbid = args["database_id"]
        raw = await client._transport.request("GET", f"/api/v1/databases/{dbid}/credentials")
        d = _unwrap(raw)
        if not isinstance(d, dict):
            return _ok(str(d))
        lines = ["Database credentials:"]
        for field in ("connection_string", "host", "port", "database", "username", "password"):
            if d.get(field):
                lines.append(f"  {field}: {d[field]}")
        return _ok("\n".join(lines))

    if name == "database_logs":
        dbid = args["database_id"]
        db_params: dict[str, Any] = {"lines": args.get("lines", 100)}
        if args.get("since"):
            db_params["since"] = args["since"]
        raw = await client._transport.request("GET", f"/api/v1/databases/{dbid}/logs", params=db_params)
        logs = _unwrap(raw)
        if isinstance(logs, list):
            return _ok("\n".join(str(line) for line in logs) if logs else "No logs.")
        return _ok(str(logs))

    # ── Workspaces ─────────────────────────────────────────────────────────

    if name == "workspace_list":
        raw = await client._transport.request("GET", "/api/v1/workspaces")
        workspaces = _unwrap(raw)
        if not workspaces:
            return _ok("No workspaces found.")
        lines = ["Workspaces:"]
        for w in workspaces:
            lines.append(f"  {w.get('id')}  {w.get('name')}")
        return _ok("\n".join(lines))

    if name == "workspace_create":
        body = {"name": args["name"]}
        if args.get("description"):
            body["description"] = args["description"]
        raw = await client._transport.request("POST", "/api/v1/workspaces", json_body=body)
        w = _unwrap(raw)
        return _ok(f"Created workspace '{w.get('name', args['name'])}' (id={w.get('id')})")

    if name == "workspace_get":
        wid = args["workspace_id"]
        raw = await client._transport.request("GET", f"/api/v1/workspaces/{wid}")
        return _ok(str(raw))

    if name == "workspace_update":
        wid = args["workspace_id"]
        body = {}
        if args.get("name"):
            body["name"] = args["name"]
        if args.get("description"):
            body["description"] = args["description"]
        await client._transport.request("PATCH", f"/api/v1/workspaces/{wid}", json_body=body)
        return _ok(f"Workspace {wid} updated.")

    if name == "workspace_stats":
        wid = args["workspace_id"]
        raw = await client._transport.request("GET", f"/api/v1/workspaces/{wid}/stats")
        return _ok(str(raw))

    if name == "workspace_usage":
        wid = args["workspace_id"]
        ws_params: dict[str, Any] = {}
        if args.get("period"):
            ws_params["period"] = args["period"]
        raw = await client._transport.request("GET", f"/api/v1/workspaces/{wid}/usage", params=ws_params)
        return _ok(str(raw))

    # ── Billing ────────────────────────────────────────────────────────────

    if name == "billing_usage":
        raw = await client._transport.request("GET", "/api/v1/billing/usage")
        return _ok(str(raw))

    if name == "billing_plan":
        raw = await client._transport.request("GET", "/api/v1/billing/plan")
        return _ok(str(raw))

    # ── Tunnels / Port forwarding ──────────────────────────────────────────

    if name == "computer_expose_port":
        computer = await _resolve(client, cid)
        port_body: dict[str, Any] = {"port": int(args["port"])}
        if args.get("protocol"):
            port_body["protocol"] = args["protocol"]
        raw = await client._transport.request(
            "POST", f"/api/v1/computers/{computer.id}/ports", json_body=port_body
        )
        d = _unwrap(raw)
        url = d.get("url", d.get("public_url", "")) if isinstance(d, dict) else str(d)
        return _ok(f"Port {args['port']} exposed. URL: {url}")

    if name == "computer_list_ports":
        computer = await _resolve(client, cid)
        raw = await client._transport.request("GET", f"/api/v1/computers/{computer.id}/ports")
        ports = _unwrap(raw)
        if not ports:
            return _ok("No ports currently exposed.")
        lines = ["Exposed ports:"]
        port_list = ports if isinstance(ports, list) else [ports]
        for p in port_list:
            lines.append(
                f"  port={p.get('port')}  protocol={p.get('protocol', 'http')}  "
                f"url={p.get('url', p.get('public_url', ''))}"
            )
        return _ok("\n".join(lines))

    if name == "computer_preview_url":
        computer = await _resolve(client, cid)
        raw = await client._transport.request(
            "GET", f"/api/v1/computers/{computer.id}/ports/{args['port']}/url"
        )
        d = _unwrap(raw)
        if isinstance(d, dict):
            url = d.get("url") or d.get("public_url") or (
                f"https://{args['port']}-{d.get('slug', computer.id)}.computer.miosa.ai"
            )
        else:
            url = str(d)
        return _ok(f"Preview URL: {url}")

    # ── Network policy ─────────────────────────────────────────────────────

    if name == "computer_network_policy_get":
        computer = await _resolve(client, cid)
        raw = await client._transport.request("GET", f"/api/v1/computers/{computer.id}/network-policy")
        d = _unwrap(raw)
        import json as _json
        return _ok(_json.dumps(d, indent=2) if isinstance(d, dict) else str(d))

    if name == "computer_network_policy_set":
        computer = await _resolve(client, cid)
        np_body: dict[str, Any] = {"rules": args["rules"]}
        if args.get("default_effect"):
            np_body["default_effect"] = args["default_effect"]
        await client._transport.request(
            "PUT", f"/api/v1/computers/{computer.id}/network-policy", json_body=np_body
        )
        return _ok(f"Network policy updated for computer {computer.id}.")

    if name == "computer_network_policy_reset":
        computer = await _resolve(client, cid)
        await client._transport.request("DELETE", f"/api/v1/computers/{computer.id}/network-policy")
        return _ok(f"Network policy reset to default for computer {computer.id}.")

    # ── Webhooks ───────────────────────────────────────────────────────────

    if name == "webhook_list":
        raw = await client._transport.request("GET", "/api/v1/webhooks")
        webhooks = _unwrap(raw)
        if not webhooks:
            return _ok("No webhooks found.")
        lines = ["Webhooks:"]
        for w in (webhooks if isinstance(webhooks, list) else [webhooks]):
            lines.append(f"  {w.get('id')}  {w.get('url')}  events={w.get('events', [])}")
        return _ok("\n".join(lines))

    if name == "webhook_create":
        raw = await client._transport.request(
            "POST", "/api/v1/webhooks",
            json_body={"url": args["url"], "events": args["events"]},
        )
        w = _unwrap(raw)
        return _ok(f"Created webhook (id={w.get('id') if isinstance(w, dict) else '?'}).")

    if name == "webhook_delete":
        wh_id = args["webhook_id"]
        await client._transport.request("DELETE", f"/api/v1/webhooks/{wh_id}")
        return _ok(f"Webhook {wh_id} deleted.")

    if name == "webhook_test":
        wh_id = args["webhook_id"]
        raw = await client._transport.request("POST", f"/api/v1/webhooks/{wh_id}/test", json_body={})
        d = _unwrap(raw)
        status = d.get("status", "delivered") if isinstance(d, dict) else str(d)
        return _ok(f"Test event sent to webhook {wh_id} (status={status}).")

    # ── Functions ──────────────────────────────────────────────────────────

    if name == "function_list":
        raw = await client._transport.request("GET", "/api/v1/functions")
        functions = _unwrap(raw)
        if not functions:
            return _ok("No functions found.")
        lines = ["Functions:"]
        for f in (functions if isinstance(functions, list) else [functions]):
            lines.append(f"  {f.get('id')}  {f.get('name')}  runtime={f.get('runtime', '')}")
        return _ok("\n".join(lines))

    if name == "function_create":
        fn_body: dict[str, Any] = {"name": args["name"], "runtime": args["runtime"]}
        if args.get("code"):
            fn_body["code"] = args["code"]
        raw = await client._transport.request("POST", "/api/v1/functions", json_body=fn_body)
        f = _unwrap(raw)
        fn_name = f.get("name", args["name"]) if isinstance(f, dict) else args["name"]
        fn_id = f.get("id") if isinstance(f, dict) else "?"
        return _ok(f"Created function '{fn_name}' (id={fn_id}).")

    if name == "function_invoke":
        fn_id = args["function_id"]
        invoke_body: dict[str, Any] = {}
        if args.get("payload") is not None:
            invoke_body["payload"] = args["payload"]
        raw = await client._transport.request("POST", f"/api/v1/functions/{fn_id}/invoke", json_body=invoke_body)
        d = _unwrap(raw)
        import json as _json2
        return _ok(_json2.dumps(d, indent=2) if isinstance(d, dict) else str(d))

    if name == "function_delete":
        fn_id = args["function_id"]
        await client._transport.request("DELETE", f"/api/v1/functions/{fn_id}")
        return _ok(f"Function {fn_id} deleted.")

    # ── API Keys ───────────────────────────────────────────────────────────

    if name == "api_key_list":
        raw = await client._transport.request("GET", "/api/v1/api-keys")
        keys = _unwrap(raw)
        if not keys:
            return _ok("No API keys found.")
        lines = ["API keys:"]
        for k in (keys if isinstance(keys, list) else [keys]):
            scopes_str = ", ".join(k.get("scopes", [])) if isinstance(k.get("scopes"), list) else str(k.get("scopes", ""))
            lines.append(f"  {k.get('id')}  {k.get('name')}  scopes=[{scopes_str}]")
        return _ok("\n".join(lines))

    if name == "api_key_create":
        ak_body: dict[str, Any] = {"name": args["name"]}
        if args.get("scopes"):
            ak_body["scopes"] = args["scopes"]
        raw = await client._transport.request("POST", "/api/v1/api-keys", json_body=ak_body)
        k = _unwrap(raw)
        key_id = k.get("id") if isinstance(k, dict) else "?"
        key_value = (k.get("key") or k.get("token") or k.get("secret") or "") if isinstance(k, dict) else ""
        msg = f"Created API key '{args['name']}' (id={key_id})."
        if key_value:
            msg += f"\nKey value (shown once): {key_value}"
        return _ok(msg)

    if name == "api_key_delete":
        key_id = args["key_id"]
        await client._transport.request("DELETE", f"/api/v1/api-keys/{key_id}")
        return _ok(f"API key {key_id} deleted.")

    # ── Cron jobs ──────────────────────────────────────────────────────────

    if name == "cron_list":
        params: dict[str, Any] = {}
        if args.get("computer_id"):
            params["computer_id"] = args["computer_id"]
        raw = await client._transport.request("GET", "/api/v1/cron-jobs", params=params if params else None)
        jobs = _unwrap(raw)
        if not jobs:
            return _ok("No cron jobs found.")
        lines = ["Cron jobs:"]
        for j in (jobs if isinstance(jobs, list) else [jobs]):
            lines.append(
                f"  {j.get('id')}  {j.get('name', '')}  {j.get('schedule', '')}  "
                f"status={j.get('status', j.get('state', ''))}"
            )
        return _ok("\n".join(lines))

    if name == "cron_create":
        body: dict[str, Any] = {
            "computer_id": args["computer_id"],
            "schedule": args["schedule"],
            "command": args["command"],
        }
        if args.get("name"):
            body["name"] = args["name"]
        raw = await client._transport.request("POST", "/api/v1/cron-jobs", json_body=body)
        j = _unwrap(raw)
        j = j if isinstance(j, dict) else {}
        return _ok(f"Created cron job '{j.get('name', args.get('name', ''))}' (id={j.get('id')}).")

    if name == "cron_get":
        cron_id = args["cron_id"]
        raw = await client._transport.request("GET", f"/api/v1/cron-jobs/{cron_id}")
        return _ok(str(_unwrap(raw)))

    if name == "cron_delete":
        cron_id = args["cron_id"]
        await client._transport.request("DELETE", f"/api/v1/cron-jobs/{cron_id}")
        return _ok(f"Cron job {cron_id} deleted.")

    if name == "cron_pause":
        cron_id = args["cron_id"]
        raw = await client._transport.request("POST", f"/api/v1/cron-jobs/{cron_id}/pause", json_body={})
        return _ok(f"Cron job {cron_id} paused.")

    if name == "cron_resume":
        cron_id = args["cron_id"]
        raw = await client._transport.request("POST", f"/api/v1/cron-jobs/{cron_id}/resume", json_body={})
        return _ok(f"Cron job {cron_id} resumed.")

    if name == "cron_run_now":
        cron_id = args["cron_id"]
        raw = await client._transport.request("POST", f"/api/v1/cron-jobs/{cron_id}/run-now", json_body={})
        d = _unwrap(raw)
        exec_id = d.get("id") if isinstance(d, dict) else None
        msg = f"Cron job {cron_id} triggered."
        if exec_id:
            msg += f" Execution id={exec_id}."
        return _ok(msg)

    if name == "cron_executions":
        cron_id = args["cron_id"]
        raw = await client._transport.request("GET", f"/api/v1/cron-jobs/{cron_id}/executions")
        execs = _unwrap(raw)
        if not execs:
            return _ok("No executions found.")
        lines = ["Executions:"]
        for e in (execs if isinstance(execs, list) else [execs]):
            lines.append(
                f"  {e.get('id')}  {e.get('started_at', e.get('created_at', ''))}  "
                f"status={e.get('status', '')}  exit_code={e.get('exit_code', '')}"
            )
        return _ok("\n".join(lines))

    # ── Regions ───────────────────────────────────────────────────────────

    if name in ("region_list", "computer_list_regions"):
        raw = await client._transport.request("GET", "/api/v1/regions")
        regions = _unwrap(raw)
        if not regions:
            return _ok("No regions found.")
        lines = ["Regions:"]
        for r in (regions if isinstance(regions, list) else [regions]):
            gpu_info = ""
            if isinstance(r, dict):
                gpu_types = r.get("gpu_types") or r.get("gpus") or []
                if gpu_types:
                    gpu_info = f"  gpus={gpu_types}"
                lines.append(
                    f"  {r.get('id', r.get('slug', ''))}  {r.get('name', '')}  "
                    f"status={r.get('status', 'available')}{gpu_info}"
                )
        return _ok("\n".join(lines))

    # ── Computer templates ────────────────────────────────────────────────

    if name == "computer_template_list":
        wid = args["workspace_id"]
        raw = await client._transport.request(
            "GET", f"/api/v1/workspaces/{wid}/computer-templates"
        )
        templates = _unwrap(raw)
        if not templates:
            return _ok("No computer templates found.")
        lines = ["Computer templates:"]
        for t in (templates if isinstance(templates, list) else [templates]):
            if isinstance(t, dict):
                lines.append(
                    f"  {t.get('id')}  {t.get('name')}  "
                    f"type={t.get('template_type', '')}  size={t.get('size', '')}"
                )
        return _ok("\n".join(lines))

    if name == "computer_template_create":
        wid = args["workspace_id"]
        body: dict[str, Any] = {"name": args["name"]}
        for key in ("template_type", "size", "selected_apps", "settings"):
            if args.get(key) is not None:
                body[key] = args[key]
        raw = await client._transport.request(
            "POST", f"/api/v1/workspaces/{wid}/computer-templates", json_body=body
        )
        t = _unwrap(raw)
        name_str = t.get("name", args["name"]) if isinstance(t, dict) else args["name"]
        tid = t.get("id") if isinstance(t, dict) else ""
        return _ok(f"Created computer template '{name_str}' (id={tid}).")

    # ── Settings ──────────────────────────────────────────────────────────

    if name == "settings_get":
        raw = await client._transport.request("GET", "/api/v1/settings")
        return _ok(str(raw))

    if name == "settings_get_branding":
        raw = await client._transport.request("GET", "/api/v1/settings/branding")
        return _ok(str(raw))

    if name == "settings_update_branding":
        body = {}
        if args.get("desktop_wallpaper_url"):
            body["desktop_wallpaper_url"] = args["desktop_wallpaper_url"]
        if args.get("logo_url"):
            body["logo_url"] = args["logo_url"]
        raw = await client._transport.request(
            "PUT", "/api/v1/settings/branding", json_body=body
        )
        return _ok(f"Branding updated: {raw}")

    if name == "settings_compute_pricing":
        raw = await client._transport.request("GET", "/api/v1/settings/compute-pricing")
        return _ok(str(raw))

    # ── Sandbox template extensions ───────────────────────────────────────

    if name == "sandbox_template_get":
        tid = args["template_id"]
        raw = await client._transport.request(
            "GET", f"/api/v1/sandbox-templates/{tid}"
        )
        return _ok(str(raw))

    if name == "sandbox_template_builds":
        tid = args["template_id"]
        raw = await client._transport.request(
            "GET", f"/api/v1/sandbox-templates/{tid}/builds"
        )
        builds = _unwrap(raw)
        if not builds:
            return _ok("No builds found.")
        lines = ["Builds:"]
        for b in (builds if isinstance(builds, list) else [builds]):
            if isinstance(b, dict):
                lines.append(
                    f"  {b.get('id')}  {b.get('status', '')}  "
                    f"created_at={b.get('created_at', '')}"
                )
        return _ok("\n".join(lines))

    # ── Volumes ────────────────────────────────────────────────────────────

    if name == "volume_list":
        raw = await client._transport.request("GET", "/api/v1/volumes")
        volumes = _unwrap(raw)
        if not volumes:
            return _ok("No volumes found.")
        lines = ["Volumes:"]
        for v in (volumes if isinstance(volumes, list) else [volumes]):
            lines.append(
                f"  {v.get('id')}  {v.get('name')}  "
                f"size_gb={v.get('size_gb', '')}  region={v.get('region', '')}  "
                f"status={v.get('status', '')}"
            )
        return _ok("\n".join(lines))

    if name == "volume_create":
        vol_body: dict[str, Any] = {"name": args["name"]}
        if args.get("size_gb") is not None:
            vol_body["size_gb"] = int(args["size_gb"])
        if args.get("region"):
            vol_body["region"] = args["region"]
        raw = await client._transport.request("POST", "/api/v1/volumes", json_body=vol_body)
        v = _unwrap(raw)
        vol_id = v.get("id") if isinstance(v, dict) else "?"
        vol_name = v.get("name", args["name"]) if isinstance(v, dict) else args["name"]
        return _ok(f"Created volume '{vol_name}' (id={vol_id}).")

    if name == "volume_get":
        vid = args["volume_id"]
        raw = await client._transport.request("GET", f"/api/v1/volumes/{vid}")
        v = _unwrap(raw)
        if not isinstance(v, dict):
            return _ok(str(v))
        return _ok(
            f"id={v.get('id')}  name={v.get('name')!r}  "
            f"size_gb={v.get('size_gb', '')}  region={v.get('region', '')}  "
            f"status={v.get('status', '')}"
        )

    if name == "volume_delete":
        vid = args["volume_id"]
        await client._transport.request("DELETE", f"/api/v1/volumes/{vid}")
        return _ok(f"Volume {vid} deleted.")

    if name == "volume_attach":
        attach_cid = args["computer_id"]
        attach_body: dict[str, Any] = {"volume_id": args["volume_id"]}
        if args.get("mount_path"):
            attach_body["mount_path"] = args["mount_path"]
        raw = await client._transport.request(
            "POST", f"/api/v1/computers/{attach_cid}/volumes", json_body=attach_body
        )
        a = _unwrap(raw)
        att_id = a.get("id") if isinstance(a, dict) else "?"
        return _ok(
            f"Volume {args['volume_id']} attached to computer {attach_cid} "
            f"(attachment id={att_id})."
        )

    if name == "volume_detach":
        detach_cid = args["computer_id"]
        att_id = args["attachment_id"]
        await client._transport.request(
            "DELETE", f"/api/v1/computers/{detach_cid}/volumes/{att_id}"
        )
        return _ok(f"Attachment {att_id} removed from computer {detach_cid}.")

    # ── Egress — delegate to the egress tool module ────────────────────────

    if name.startswith("miosa_"):
        return await dispatch_egress(client._transport, name, args)

    return _err(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run() -> None:
    api_key = os.environ.get("MIOSA_API_KEY")
    if not api_key:
        print(
            "Error: MIOSA_API_KEY environment variable is not set.\n"
            "Set it in your .claude/mcp.json env block or export it before running.",
            file=sys.stderr,
        )
        sys.exit(1)

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    async with AsyncMiosa(api_key=api_key) as client:
        app = build_server(client)
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )


def main() -> None:
    import asyncio
    asyncio.run(_run())


if __name__ == "__main__":
    main()
