# FineCode

**FineCode gives you one intent-based workflow for developer tooling across CLI, IDE, CI, and AI assistants.**

Most teams wire the same tooling logic multiple times: shell scripts for local use, YAML for CI, editor plugins, and now MCP glue for AI. FineCode replaces all of that with one reusable layer. Define `lint`, `format`, or `test` once — then run the same action everywhere:

```bash
python -m finecode run lint           # terminal
python -m finecode run format         # CI pipeline
# Same actions surface in VSCode and MCP-compatible AI clients automatically
```

## What you get

FineCode organizes developer tooling into **features** — linting, formatting, testing, and more. Each feature is packaged as a **preset** you add to your project.

With `fine_python_recommended` you get all of these out of the box:

| Feature | Tools | Where it works |
|---------|-------|----------------|
| **Linting** | Ruff, Flake8 | CLI, IDE diagnostics, CI, AI |
| **Formatting** | Ruff formatter, isort | CLI, IDE format-on-save, CI, AI |
| **Type checking** | Pyrefly | CLI, IDE diagnostics, CI, AI |
| **Import checking** | import-linter | CLI, CI, AI |
| **Testing** | pytest | CLI, IDE test panel, CI, AI |
| **IDE language support** | Pyrefly | IDE (hover, go-to-definition, references, call hierarchy, inlay hints, semantic tokens) |
| **TOML support** | Tombi | IDE (linting, formatting, semantic tokens) |

Want only specific features? Pick individual presets instead — see the [feature catalog](getting-started.md#2-choose-your-features) in Getting Started.

## Try it in minutes

Add FineCode and a preset to your `pyproject.toml`:

```toml
[dependency-groups]
dev_workspace = ["finecode==0.3.*", "fine_python_recommended==0.3.*"]

[tool.finecode]
presets = [{ source = "fine_python_recommended" }]
```

Bootstrap the workspace tooling environment:

```bash
pipx run finecode bootstrap
# or
uvx finecode bootstrap
```

Then activate the bootstrapped environment:

```bash
source .venvs/dev_workspace/bin/activate
# Windows: .venvs\dev_workspace\Scripts\activate
```

Then prepare tool environments and run your first action:

```bash
python -m finecode prepare-envs
python -m finecode run lint
```

For the full setup flow, see [Getting Started](getting-started.md).

## Customize your setup

Presets give you sensible defaults. Override anything in your `pyproject.toml` — no Python code needed.

**Add stricter lint rules:**

```toml
[[tool.finecode.action_handler]]
source = "fine_python_ruff.RuffLintFilesHandler"
config.extend_select = ["B", "I", "UP", "SIM"]
```

**Disable a tool you don't need:**

```toml
[[tool.finecode.action_handler]]
source = "fine_python_flake8.Flake8LintFilesHandler"
enabled = false
```

**Pin a tool version:**

```toml
[tool.finecode.extension.fine_python_ruff]
dependencies_override = ["ruff==0.15.*"]
```

See [Configuration](configuration.md) for the full reference.

## Why teams use FineCode

- **One workflow everywhere** — the same actions run in terminal, CI, IDE, and AI assistants. No duplicated glue code, no mismatches between local and CI.
- **Swap tools, keep the workflow** — replace ruff with another linter, or add a new formatter, without redesigning your setup. The action surface stays stable.
- **Reusable standards** — package your team's tooling setup as a [preset](guides/creating-preset.md) and share it across repositories. Roll out updates through normal dependency management.
- **Isolation without fragmentation** — developer tooling runs in isolated environments, separate from your runtime dependencies, while still presenting one coherent workflow.
- **Scales with your codebase** — works the same for a single project, a multi-project workspace, or a mixed-language monorepo.

For the deeper design reasoning, see [Why FineCode's Action Model Works](theory/why-action-model.md).

## Where to go next

- [Getting Started](getting-started.md) — full setup path with feature catalog
- [Supported Environments](supported-environments.md) — CLI, IDE, CI, git hooks, and AI support matrix
- [IDE and MCP Setup](getting-started-ide-mcp.md) — VSCode extension and MCP configuration
- [Concepts](concepts.md) — how Actions, Handlers, Presets, and Services fit together
- [Designing Actions](guides/designing-actions.md) — action design guidance for extension authors
- [Extensions](reference/extensions.md) — available tool integrations

## Community

Have questions or feedback? Join [Matrix](https://matrix.to/#/#finecode:matrix.org) or [Discord](https://discord.gg/AbpKU39cgY).
