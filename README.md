# FineCode

FineCode gives you one workflow for code quality and developer tooling across CLI, IDE, CI, and AI assistants.

## What FineCode Solves

- Unifies tool execution across local CLI, IDE, CI and AI
- Keeps tooling config reusable via presets
- Supports multi-project workspaces
- Isolates tooling dependencies in dedicated virtual environments

## Prerequisites

- Python 3.11-3.14 or [uv](https://docs.astral.sh/uv/)

## Quick Start (5 minutes)

1. Add dependencies and FineCode config to `pyproject.toml`:

```toml
[dependency-groups]
dev_workspace = ["finecode==0.3.*", "fine_python_recommended==0.3.*"]

[tool.finecode]
presets = [{ source = "fine_python_recommended" }]
```

2. Bootstrap and activate the `dev_workspace` environment:

```bash
# Recommended: bootstrap the environment
pipx run finecode bootstrap
# or
uvx finecode bootstrap

# macOS/Linux
source .venvs/dev_workspace/bin/activate
# Windows (PowerShell)
.venvs\dev_workspace\Scripts\Activate.ps1
# Windows (cmd.exe)
.venvs\dev_workspace\Scripts\activate.bat
```

3. Prepare environments and run FineCode:

```bash
python -m finecode prepare-envs
python -m finecode run lint
```

4. (Optional, but recommended) Enable FineCode in development environments you use:

- IDE (VSCode): [IDE Integration docs](https://finecode-dev.github.io/ide-integration/) and [FineCode VSCode extension](https://marketplace.visualstudio.com/items?itemName=VladyslavHnatiuk.finecode-vscode)
- AI assistants (MCP): [MCP setup](https://finecode-dev.github.io/ide-integration/#setup-for-claude-code)
- Git hooks: [Use the same actions in local/CI/hooks workflows](https://finecode-dev.github.io/#one-command-surface-for-local-ci-ide-and-ai)

For full setup and recommended presets, see: [Getting Started](https://finecode-dev.github.io/getting-started/)

## Documentation

- [Start here](https://finecode-dev.github.io/)
- [Concepts](https://finecode-dev.github.io/concepts/)
- [CLI](https://finecode-dev.github.io/cli/)
- [Configuration](https://finecode-dev.github.io/configuration/)
- [IDE Integration (LSP, VSCode, MCP)](https://finecode-dev.github.io/ide-integration/)
- [Guide: Creating an Extension](https://finecode-dev.github.io/guides/creating-extension/)
- [Guide: Creating a Preset](https://finecode-dev.github.io/guides/creating-preset/)
- [Guide: Multi-Project Workspace](https://finecode-dev.github.io/guides/workspace/)
- [Reference: Built-in Actions](https://finecode-dev.github.io/reference/actions/)
- [Reference: Extensions](https://finecode-dev.github.io/reference/extensions/)

## Contributing

See [Development](https://finecode-dev.github.io/development/) for local development workflow.

## License

[MIT](LICENSE)
