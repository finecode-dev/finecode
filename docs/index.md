# FineCode

**FineCode gives you one workflow for code quality and developer tooling across CLI, IDE, CI, and AI assistants.**

FineCode organizes your tooling so tasks like linting, formatting, type checking, build, and publish follow one consistent workflow and can be reused across projects.

Start in one repository in minutes. Then package the same setup and reuse it across your other projects.

## Start in minutes

Add FineCode and a preset to your `pyproject.toml`:

```toml
[dependency-groups]
dev_workspace = ["finecode==0.3.*", "fine_python_recommended==0.3.*"]

[tool.finecode]
presets = [{ source = "fine_python_recommended" }]
```

Run FineCode:

```bash
# initial one-time setup
python -m venv .venvs/dev_workspace
source .venvs/dev_workspace/bin/activate   # Windows: .venvs\dev_workspace\Scripts\activate
python -m pip install --group="dev_workspace"
python -m finecode prepare-envs

# now you are ready to use finecode
# e.g. lint the whole workspace:
python -m finecode run lint
```

NOTE: `pip install --group` requires pip 25.1+.

This gives you a working Python baseline with Ruff, Flake8, and isort through one shared config entry point.

## From one project to reusable standard

Once this works in one project, turn it into a shared preset and use it across repositories:

```toml
[dependency-groups]
dev_workspace = ["finecode==0.3.*", "my_team_standards==0.1.*"]

[tool.finecode]
presets = [{ source = "my_team_standards" }]
```

```bash
python -m pip install --group="dev_workspace"
python -m finecode prepare-envs
```

Projects can then adopt updates through normal dependency updates.
See [Creating a Preset](guides/creating-preset.md) for the packaging flow.

You can also combine presets in one project (for example, a language preset and a team preset):

```toml
[tool.finecode]
presets = [
    { source = "fine_python_recommended" },
    { source = "my_team_standards" },
]
```

Typical rollout:

1. Start with a preset in one repository.
2. Tune handlers and action config to fit your workflow.
3. Publish that setup as a preset package for your team.
4. Reuse it across repositories.

## Why developers use FineCode

### Core benefits for every project

- Keep tooling config in one place instead of per-tool config sprawl
- Use the same actions in terminal, IDE, and AI-assisted workflows
- Spend less time wiring tools together and more time shipping code
- Keep local runs and CI behavior aligned around the same actions

#### One command surface for local, CI, IDE, and AI

Use the same actions and config everywhere:

- IDE: [VSCode extension setup](getting-started-ide-mcp.md#vscode-setup)
- AI assistants: [MCP setup for AI clients](getting-started-ide-mcp.md#mcp-setup-for-ai-clients)
- Local CLI: `python -m finecode run lint check_formatting`
- CI: `python -m finecode run lint check_formatting`
- Git hooks: run FineCode actions before commit without requiring `pre-commit`

#### Isolated environments by purpose

FineCode keeps developer tooling separate from runtime dependencies:

```text
.venvs/
  dev_workspace/   <- FineCode and presets
  dev_no_runtime/  <- lint/format/type-check handlers
  dev/             <- tooling that imports project code during execution
  runtime/         <- project runtime dependencies
```

This reduces dependency cross-talk and makes tool execution more predictable.

These environment roles are examples, not fixed requirements. You can shape the layout to match your workflow.

#### Workspace-aware by design

FineCode understands your workspace as a whole, including how individual subprojects fit together.

Actions can target a single project or the entire workspace, so tasks like linting every subproject run from one command.

#### Polyglot workflow, one action surface

FineCode actions are not tied to a single language. A single action can include handlers for different file types (for example Python code, Markdown docs, and JSON/TOML config) while keeping one shared command surface.

In practice, this means you can keep using the same `lint` and `format` actions across mixed repositories today. Broader first-class preset coverage for combinations such as Python + Rust is on the roadmap.

### Additional benefits for teams

- Keep standards centralized in a shared preset package
- Roll out toolchain changes through normal dependency updates
- Keep rollout predictable by updating a shared preset package instead of editing each repository separately

## Flexible, no lock-in

Presets are a starting point, not a ceiling.

Disable or tune individual handlers:

```toml
[[tool.finecode.action_handler]]
source = "fine_python_ruff.RuffLintFilesHandler"
config.line_length = 120

[tool.finecode.action.lint]
handlers = [
    { name = "flake8", enabled = false },
    { name = "my_linter", source = "my_team.MyLinterHandler" },
]
```

Replace an action handler set completely:

```toml
[tool.finecode.action.lint]
handlers_mode = "replace"
handlers = [
    { name = "my_linter", source = "my_team.MyLinterHandler" },
]
```

You can also add custom actions and handlers for project-specific workflows.

## Proven in this repository

FineCode is used to run quality actions in the FineCode repository itself.

- TODO: Add repository-scale metrics (actions/day, CI duration impact, setup time before/after)

## Ready to try FineCode?

[Get started in 5 minutes ->](getting-started.md)

See also: [Concepts](concepts.md), [Configuration](configuration.md), [available presets and extensions](reference/extensions.md)

## Community

Have questions or feedback? Join [Discord](https://discord.gg/nwb3CRVN).
