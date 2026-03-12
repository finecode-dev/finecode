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
- Integrates with the native VS Code Testing panel (discover and run tests)

### Requirements

- FineCode installed in `.venvs/dev_workspace` (see [Setup](getting-started.md))
- `python -m finecode prepare-envs` run at least once

### Configuration

The extension auto-discovers `.venvs/dev_workspace/`. No extra extension-side project configuration is required.

### Testing integration

The Testing panel (beaker icon) is populated automatically when the workspace loads. It is driven by two actions from `finecode_extension_api`:

- `ListTestsAction` — discovers tests and builds the file → class → function tree
- `RunTestsAction` — executes tests and reports pass/fail/skip/error per test case

To use the Testing panel, you need handlers registered for both actions. The `fine_python_test` preset provides pytest-based handlers for both. If you are already using `fine_python_recommended`, testing support is included — no extra preset needed, since `fine_python_recommended` already pulls in `fine_python_test`.

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
