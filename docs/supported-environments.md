# Supported Development Environments

FineCode is designed to expose the same actions across the development surfaces teams already use: terminal, editor, CI, git hooks, and AI tooling.

Enable FineCode in each environment where your team works. For example, a team that uses VS Code, CI, git hooks, and an MCP-compatible AI assistant should configure all of those integrations so they run the same FineCode actions.

The CLI is still useful as the common command surface for local runs, scripts, and debugging, while IDE, CI, git hook, and MCP integrations let the same actions run where developers already work.

## Support matrix

| Environment | Status | Setup | Notes |
|---|---|---|---|
| VSCode | Supported | [IDE and MCP Setup](getting-started-ide-mcp.md) | Enable this if your team works in VS Code. Includes LSP features, action sidebar, formatting, diagnostics, testing integration, and Copilot MCP registration through the extension. |
| Other IDEs and editors | Planned / custom integration | [LSP and MCP Architecture](reference/lsp-mcp-architecture.md) | FineCode ships an LSP server, but the only packaged IDE integration documented and tested today is VSCode. For now, other IDE integrations would need to come through LSP-based clients. |
| CLI | Supported | [Getting Started](getting-started.md) and [CLI Reference](cli.md) | Enable this for local runs, scripts, and debugging. |
| CI | Supported | [CLI Reference](cli.md) and [Multi-Project Workspace](guides/workspace.md#ci-usage) | Enable this for pipelines. FineCode auto-detects CI via the `CI` environment variable. |
| Git hooks | Supported | [Using Git Hooks](guides/using-git-hooks.md) | Enable this for staged-file checks before commit. |
| AI assistants via MCP | Supported | [IDE and MCP Setup](getting-started-ide-mcp.md#mcp-setup-for-ai-clients) | Enable this for MCP-compatible clients such as Claude Code. In VS Code, the extension can manage MCP registration. |

## CLI

The CLI provides the command surface used by local workflows, scripts, CI, and debugging:

```bash
python -m finecode run lint
python -m finecode run format
python -m finecode prepare-envs
```

Use it anywhere you need to run FineCode directly from a shell.

## IDE

VSCode is the primary IDE integration today through the FineCode extension.

That path gives you:

- inline diagnostics
- formatting and code actions
- action discovery in the sidebar
- test discovery and execution
- automatic MCP registration for VS Code Copilot

If you need another IDE, FineCode already exposes an LSP server, but there is not yet another official packaged integration in the user docs.

## CI

CI should reuse the same FineCode actions you run locally instead of duplicating tool-specific wiring:

```bash
python -m finecode run --concurrently lint check_formatting
```

In CI environments, FineCode automatically detects `CI=...` and marks the run as `ci`.

## Git Hooks

Git hooks are the lightweight enforcement layer before code even reaches CI.

Install the hook with:

```bash
python -m finecode run install_git_hooks
```

The installed hook runs FineCode's `precommit` action on staged files. You can supply that action through the `fine_precommit` preset or by declaring your own `precommit` handlers.

## AI Assistants and MCP

FineCode exposes actions through an MCP server, so AI clients can call the same action surface that developers use in the CLI and IDE.

At a minimum, MCP clients launch:

```bash
.venvs/dev_workspace/bin/python -m finecode start-mcp
```

For VS Code users, the extension can manage MCP registration. For other MCP clients, use the client-specific configuration described in [IDE and MCP Setup](getting-started-ide-mcp.md#mcp-setup-for-ai-clients).
