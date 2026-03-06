# IDE and MCP Setup

After completing the base setup in [Getting Started](getting-started.md), connect FineCode to your IDE and AI tooling.

## VSCode setup

Install the [FineCode VSCode extension](https://marketplace.visualstudio.com/items?itemName=VladyslavHnatiuk.finecode-vscode).

The extension:

- Starts the FineCode LSP server when you open a workspace
- Shows diagnostics inline
- Provides code actions and quick fixes
- Supports formatting on save
- Exposes FineCode actions in the sidebar

### Requirements

- FineCode installed in `.venvs/dev_workspace` (see [Setup](getting-started.md))
- `python -m finecode prepare-envs` run at least once

### Configuration

The extension auto-discovers `.venvs/dev_workspace/`. No extra extension-side project configuration is required.

## MCP setup for AI clients

FineCode exposes an MCP server so any MCP-compatible client can invoke FineCode actions directly.

At a minimum, your client should launch:

```bash
.venvs/dev_workspace/bin/python -m finecode start-mcp
```

Client configuration format depends on the MCP client.

### Example: Claude Code

Create `.mcp.json` in the workspace root:

```json
{
  "mcpServers": {
    "finecode": {
      "type": "stdio",
      "command": ".venvs/dev_workspace/bin/python",
      "args": ["-m", "finecode", "start-mcp", "--workdir=."]
    }
  }
}
```

Claude Code discovers this file and prompts for approval on first use.

Manual server startup is mainly for debugging and custom integration development.
See [LSP and MCP Architecture](reference/lsp-mcp-architecture.md#manual-server-startup-for-debugging).
