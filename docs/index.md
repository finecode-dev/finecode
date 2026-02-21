# FineCode

**Stop configuring tools. Start using them.**

Every Python project needs linting, formatting, type checking. And in every project you end up doing the same thing: installing the same tools, writing the same configuration, wiring them up to your IDE — again.

FineCode solves this once.

## One line to get linting and formatting

```toml
# pyproject.toml
[dependency-groups]
dev_workspace = ["finecode==0.3.*", "fine_python_recommended==0.3.*"]

[tool.finecode]
presets = [{ source = "fine_python_recommended" }]
```

```bash
python -m finecode prepare-envs
python -m finecode run lint check_formatting
```

That's it. Ruff, Flake8, and isort — installed, configured, and running. No per-tool setup, no config files to write.

## Your IDE just works

Install the [VSCode extension](ide-integration.md) and get inline diagnostics, quick fixes, and format-on-save — powered by the same configuration as your CLI. No separate language server setup, no per-project extension configuration.

## Share configuration across projects

Package your tool configuration and share it across your team's projects as a regular Python package:

```toml
# Any project that wants your standard setup:
[tool.finecode]
presets = [{ source = "my_team_standards" }]
```

Update the preset package — all projects pick it up on next install. No drift, no copy-paste.

## Tools stay out of your project

Dev tools, runtime dependencies, and your project stay in separate virtual environments. Ruff's dependencies don't mix with your project's dependencies. Mypy doesn't break because something else updated a package. Everything is contained.

## Your rules, not ours

Presets give you a working setup instantly, but nothing is locked in. Every default can be overridden:

```toml
# Adjust a single handler's config
[[tool.finecode.action_handler]]
source = "fine_python_ruff.RuffLintFilesHandler"
config.line_length = 120

# Swap out individual tools while keeping the rest of the preset
[tool.finecode.action.lint]
handlers = [
    { name = "flake8", disabled = true },
    { name = "my_linter", source = "my_team.MyLinterHandler", ... },
]

# Or replace everything and build from scratch
[tool.finecode.action.lint]
handlers_mode = "replace"
handlers = [...]
```

You can adopt FineCode incrementally — start with a preset, customise as needed, replace entirely if you want. There's no framework lock-in.

## Virtual environment management included

FineCode manages virtual environments for you, with a clear separation by purpose:

```text
.venvs/
  dev_workspace/   ← FineCode itself, presets, dev tools
  dev_no_runtime/  ← linters, formatters, type checkers
  runtime/         ← your project's runtime dependencies
  docs/            ← documentation tools
```

Each tool runs in the right environment. Runtime dependencies never get polluted by dev tools, and dev tools never break because a runtime package updated.

In a monorepo with many packages, this becomes especially valuable — FineCode handles environment setup across all of them automatically. No manual venv juggling, no shared environment where everything mixes together.

```bash
# One command sets up all environments across all packages
python -m finecode prepare-envs
```

## Not just linting and formatting

FineCode ships with built-in actions for the most common workflows — lint, format, type-check, build, publish — but actions are just Python classes. You can define your own for anything that fits your development process: running migrations, generating code, validating architecture, checking licenses, or anything specific to your project.

Your custom actions get the same CLI interface, IDE integration, and environment isolation as the built-in ones — for free.

## Extend it with your own tools

FineCode has a clean handler interface. If you have an internal tool, a custom linter, or anything that fits into a lint/format/build workflow — you can plug it in and get CLI and IDE integration for free.

```python
class MyLinterHandler(ActionHandler[...]):
    action = LintFilesAction

    async def run(self, payload, context) -> LintFilesRunResult:
        ...  # your tool logic here
```

## Your AI assistant knows your tools

FineCode exposes an [MCP server](ide-integration.md#mcp-server) that AI assistants connect to. Instead of guessing which linter you use, how to run it, or what flags to pass — the assistant gets the exact tool configuration from your project directly.

No explanations needed. No wrong commands. The assistant just knows.

## Ready to try it?

[Get started in 5 minutes →](getting-started.md)

Or browse what's included: [available presets and extensions](reference/extensions.md).
