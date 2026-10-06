# LSP and MCP Architecture

FineCode uses one shared Workspace Manager Server (WM Server) for CLI, IDE (LSP), and AI-agent (MCP) clients.

## Component model

```text
FineCode WM Server (TCP JSON-RPC, auto-managed)
├── WorkspaceContext, runners, services
└── auto-stops when no clients remain

LSP Server (start-lsp, started by IDE)
└── connects to WM Server (starts one if needed)

MCP Server (start-mcp, started by MCP client)
└── connects to WM Server (starts one if needed)
```

The WM Server writes its port to `.venvs/dev_workspace/cache/finecode/wm_port` for client discovery.

## LSP request flow

```mermaid
sequenceDiagram
    participant IDE as IDE Extension
    participant WM as Workspace Manager (LSP)
    participant ER as Extension Runner

    IDE->>WM: textDocument/didOpen
    WM->>ER: run lint_files action
    ER-->>WM: LintFilesRunResult (diagnostics)
    WM-->>IDE: textDocument/publishDiagnostics

    IDE->>WM: textDocument/formatting
    WM->>ER: run format_files action
    ER-->>WM: FormatFilesRunResult (edits)
    WM-->>IDE: TextEdit[]
```

The LSP layer translates protocol messages into FineCode actions, delegates execution to extension runners, then translates results back into LSP responses.

## Lifecycle behavior

- Any client (CLI, LSP, MCP) can start the WM Server if it is not already running. Whichever gets there first starts it; the rest find it through the discovery file and attach to that one.
- Each connected client keeps the WM Server alive.
- When the last client disconnects, the WM Server exits automatically — after `--disconnect-timeout` seconds (default 30), and after 30 seconds if no client ever connected. Disconnecting itself discards nothing: the loaded configuration and started runners are the exiting process's, and they survive until it exits.
- `start-wm-server --keep-alive` disables both of those timers, for a server whose lifetime something else owns (a devcontainer, a supervisor). See [CLI reference — start-wm-server](../cli.md#start-wm-server).

Because the first starter wins, the log level a client asks for is ignored when a
server is already up — including the one the LSP passes. `restart-wm` is what applies
a new one.

## Manual server startup for debugging

Most users should not start servers manually. IDE and MCP clients usually manage startup automatically.

Use manual startup when:

- Debugging LSP/MCP behavior
- Developing a new IDE integration
- Developing a new MCP client integration

```bash
# LSP server (for custom IDE clients)
python -m finecode start-lsp --stdio

# MCP server
python -m finecode start-mcp
```

For setup instructions, see [IDE and MCP Setup](../getting-started-ide-mcp.md).
