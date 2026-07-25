# miosa-mcp

MCP (Model Context Protocol) bridge that exposes MIOSA cloud sandboxes and desktops to any MCP-aware client (Claude Code, Cursor, Gemini CLI, etc.).

## Two ways to run it

### 1. Hosted (recommended) - no install

MIOSA ships a public MCP endpoint at `https://api.miosa.ai/api/v1/mcp`.
Point any MCP client at it with your `msk_u_*` API key as a Bearer token:

```bash
claude mcp add --transport http miosa \
  https://api.miosa.ai/api/v1/mcp \
  --header "Authorization: Bearer msk_u_your_key_here"
```

Full guide: [`docs/api/mcp-connect.md`](../docs/api/mcp-connect.md).

### 2. Local stdio (this Python package)

Install:

```bash
pip install miosa-mcp
```

Add to `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "miosa": {
      "command": "python",
      "args": ["-m", "miosa_mcp"],
      "env": {
        "MIOSA_API_KEY": "msk_u_your_key_here",
        "MIOSA_TENANT": "optional-organization-slug"
      }
    }
  }
}
```

Use stdio when you need to wrap the MCP layer with custom local logic, or your client does not yet support remote MCP.
Both modes hit the same MIOSA REST API under the hood.

Get your API key at <https://miosa.ai/dashboard/api-keys>.

`MIOSA_TENANT` selects an initial organization for every API request.
You can also change organization context during a session with `organization_switch`.
The backend verifies membership before MCP updates its active context.

## Action authority

Governed MCP tools ask MIOSA's control-plane action authority immediately before dispatch.
The control plane owns capability versions, grants, one-time approvals, revocation, and receipts.
The MCP package only maps tool names to canonical capabilities from a generated contract and never copies policy locally.
Every registered tool has exactly one canonical capability; a tool with no mapping fails closed before any dispatch.
Pending approval, denial, a missing catalog capability, or an unavailable authority fails closed before the SDK or REST operation runs.

## Tools

### Organizations

| Tool | Description |
|------|-------------|
| `organization_list()` | List organizations available to the authenticated user. |
| `organization_current()` | Show the organization currently used for MCP requests. |
| `organization_switch(organization)` | Select an organization by UUID or slug and propagate it through `X-MIOSA-Tenant`. |

### Durable runs

| Tool | Description |
|------|-------------|
| `run_create(target_kind, target_id, instruction?, command?, ...)` | Start a durable agent or command run on a Sandbox or Computer. |
| `run_list(...)` | List runs with target, status, and external attribution filters. |
| `run_get(run_id)` | Get current run state. |
| `run_cancel(run_id)` | Cancel a run. |
| `run_outputs(run_id)` | Get structured run outputs. |
| `run_messages(run_id)` | Get structured run messages. |
| `run_files(run_id)` | List files produced by a run. |

Runs are the canonical agent execution contract exposed by MCP.
Use `run_create` instead of legacy agent-run terminology in new integrations.

### Lifecycle

| Tool | Description |
|------|-------------|
| `computer_create(name, template_type?, size?)` | Create a computer and start it. Becomes the active computer. |
| `computer_list()` | List all computers in your active organization. |
| `computer_destroy(computer_id?)` | Permanently destroy a computer. |

### Sandboxes

| Tool | Description |
|------|-------------|
| `sandbox_create(name?, size="small", ...)` | Create a sandbox using `xs`, `small`, `medium`, `large`, or `xl`. |
| `sandbox_list()` | List sandboxes in the active organization. |
| `sandbox_get(sandbox_id)` | Get current sandbox state and details. |
| `sandbox_destroy(sandbox_id)` | Permanently destroy a sandbox. |

`sandbox_create` uses canonical named resource contracts and defaults to `small` (2 vCPU, 4 GB RAM, 10 GB disk).
Legacy callers may provide `cpu_count`, `memory_mb`, and `disk_size_mb` only as a complete triple that exactly matches one named size; MCP normalizes the triple to `size` before calling the SDK.

### Desktop - Visual

| Tool | Description |
|------|-------------|
| `computer_screenshot(computer_id?)` | Capture a PNG screenshot. Claude can see and reason about it. |
| `computer_get_screen_size(computer_id?)` | Get screen resolution in pixels. |
| `computer_get_cursor_position(computer_id?)` | Get current cursor x/y. |

### Desktop - Pointer

| Tool | Description |
|------|-------------|
| `computer_click(x, y, button?, computer_id?)` | Click at coordinates. |
| `computer_double_click(x, y, computer_id?)` | Double-click at coordinates. |
| `computer_move_cursor(x, y, computer_id?)` | Move cursor without clicking. |
| `computer_drag(from_x, from_y, to_x, to_y, computer_id?)` | Click-drag between positions. |
| `computer_scroll(direction?, clicks?, x?, y?, computer_id?)` | Scroll up/down/left/right. |

### Desktop - Keyboard

| Tool | Description |
|------|-------------|
| `computer_type(text, computer_id?)` | Type text into the focused field. |
| `computer_key(key, computer_id?)` | Press a single key (Return, Tab, Escape, etc.). |
| `computer_hotkey(keys, computer_id?)` | Press a key combo (e.g. `["ctrl", "c"]`). |

### Clipboard

| Tool | Description |
|------|-------------|
| `computer_get_clipboard(computer_id?)` | Read clipboard text. |
| `computer_set_clipboard(text, computer_id?)` | Write clipboard text. |

### Window Management

| Tool | Description |
|------|-------------|
| `computer_windows(computer_id?)` | List open windows with IDs, titles, positions. |
| `computer_launch(app, computer_id?)` | Launch an app by name (firefox, gedit, xterm…). |

### Shell & Files

| Tool | Description |
|------|-------------|
| `computer_bash(command, timeout?, computer_id?)` | Run a bash command; returns stdout, stderr, exit_code. |
| `computer_write_file(path, content, computer_id?)` | Write a file inside the VM. |
| `computer_read_file(path, computer_id?)` | Read a file from inside the VM. |

### Docker Deploy

Use this sequence for app/container publishing:

| Step | Tool | Purpose |
|------|------|---------|
| 1 | `docker_deploy_template_list` / `docker_deploy_template_get` | Choose a Docker Deploy app template. Template data may include `DESIGN.md` context and design reference presets. |
| 2 | `docker_deploy_host_ensure` | Create or reuse the workspace Docker appliance. |
| 3 | `sandbox_deploy_docker` | Publish the sandbox app to Docker Deploy. Pass `docker_deploy_template_id` when the app came from a template. |
| 4 | `docker_deploy_doctor` | Verify product marker, host health, route metadata, and public URL before reporting success. |

Agents should treat `docker_deploy_doctor.ok=false` as not live, even if the URL returns HTTP 200.
The doctor detects the platform gateway JSON response that can otherwise look like a successful app response.

Sandbox and deployment creation tools preserve `external_workspace_id`, `external_user_id`, and `external_project_id` attribution.
URL-bearing results are returned as JSON and keep `preview_url` or `public_url` alongside the resource data instead of flattening the response into prose.

## Errors

Failed tool calls use MCP `isError=true` and include a structured error object.
The object preserves the backend error code, HTTP status, request ID, response details, tool name, and human-readable message.
Clients can branch on stable codes such as `INVALID_TENANT_CONTEXT` without parsing display text.

## Active Computer

All tools accept an optional `computer_id`. When omitted, the server uses the most recently created or accessed computer automatically. This means you can `computer_create` once and omit the ID for the rest of the session.

## Example session

```
> computer_create(name="my-dev-box")
Created computer 'my-dev-box' (id=comp_abc123, status=running). This is now the active computer.

> computer_screenshot()
[PNG image of the desktop appears in Claude's context]

> computer_bash(command="ls /home")
stdout:
ubuntu
exit_code: 0

> computer_launch(app="firefox")
Launched: firefox

> computer_screenshot()
[Firefox is now open]

> computer_click(x=640, y=50)
Clicked (640, 50) button=left

> computer_type(text="https://miosa.ai")
Typed: 'https://miosa.ai'

> computer_key(key="Return")
Pressed key: Return
```

## Development

```bash
# Install deps (requires Python 3.10+)
pip install -e ".[dev]"

# Type-check
mypy miosa_mcp/

# Lint
ruff check miosa_mcp/
```

## Architecture

The server is a single async Python process that:
1. Reads `MIOSA_API_KEY` from the environment
2. Applies optional `MIOSA_TENANT` context through `X-MIOSA-Tenant`
3. Initializes an `AsyncMiosa` client
4. Maintains an in-process cache of `AsyncComputer` objects
5. Serves MCP tools over stdio (the protocol Claude Code uses)
6. Maps every tool call to the corresponding MIOSA SDK or REST contract
7. Returns screenshots as base64-encoded PNG images (MCP `ImageContent`)

All desktop action tools (`click`, `type`, `key`, etc.) call the MIOSA platform API which proxies commands through to the running VM's envd daemon at port 49983.
