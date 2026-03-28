# FineCode

**FineCode gives you one intent-based workflow for developer tooling across CLI, IDE, CI, and AI assistants.**

Most teams end up wiring the same tooling logic multiple times: shell scripts for local use, YAML for CI, editor integration, and now MCP or AI-specific glue. FineCode gives you one reusable layer instead, so `lint`, `format`, `type-check`, environment preparation, and other workflows follow the same model everywhere. In FineCode, those intents are modeled as reusable actions.

For example, you can define `lint` once and then reuse that same intent:

- in the terminal: `python -m finecode run lint`
- in CI: run `lint` as part of your pipeline
- in the IDE: surface `lint` through the editor integration
- in AI workflows: expose `lint` through MCP

Start in one repository. Reuse the same standards across many. Keep the setup flexible instead of locking your project to one tool or one interface.

## Why teams use FineCode

### One workflow across local runs, CI, IDE, and AI

FineCode lets you define actions once and run them through the surfaces developers already use:

- Local CLI: `python -m finecode run lint`
- CI: run the same actions instead of re-creating tool wiring in pipelines
- IDE: connect through the [VSCode and MCP setup guide](getting-started-ide-mcp.md)
- AI assistants: expose the same action surface through MCP-compatible clients

That means less duplicated glue code and fewer mismatches between "what works on my machine" and "what CI expects".

### Explicit action model, not ad-hoc scripts

FineCode is built around explicit actions and handlers: define the operation once, then plug in the tooling that implements it.

That makes it easier to:

- swap tools without redesigning the whole workflow
- keep a stable command surface as your stack evolves
- support mixed-language workspaces with a clearer abstraction boundary
- reason about what your tooling setup actually does

If you want the deeper reasoning, the docs now explain it directly:

- [Why FineCode's Action Model Works](theory/why-action-model.md)
- [Designing Actions](guides/designing-actions.md)

### Reusable standards for teams

FineCode presets let you package your tooling setup once and share it across repositories. Start with a built-in preset, then turn your own conventions into a reusable standard for your team.

- adopt a working baseline quickly
- refine it in a real project
- publish it as a shared preset
- roll out updates through normal dependency updates

See [Creating a Preset](guides/creating-preset.md) for the packaging flow.

### Isolation without fragmentation

FineCode keeps developer tooling isolated from runtime dependencies while still presenting one coherent workflow to the team. You get more predictable execution without forcing every tool into a bespoke setup.

### Built to grow from one repo to a workspace

You can start small, but the model is designed for larger codebases too:

- single-project repositories
- multi-project workspaces
- mixed-language setups
- team-maintained tooling standards

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

This gives you a ready-to-use baseline for Python quality tooling through one shared entry point.

For the full setup flow, see [Getting Started](getting-started.md).

## Why FineCode feels different

FineCode is not just a wrapper around linters and formatters. It gives those tools a shared execution model, so the workflow stays coherent as your project grows.

- Your team sees the same actions in terminal, CI, IDE, and AI-assisted flows
- Your configuration has a clearer structure than a pile of unrelated tool configs
- Your standards can move from "this repo's setup" to "our team's reusable preset"
- Your docs can explain the model, not just list commands

## Where to go next

- [Getting Started](getting-started.md) for the full setup path
- [IDE and MCP Setup](getting-started-ide-mcp.md) to connect editors and AI clients
- [Concepts](concepts.md) for the core mental model
- [Why FineCode's Action Model Works](theory/why-action-model.md) for the design rationale
- [Designing Actions](guides/designing-actions.md) for action design guidance
- [Extensions](reference/extensions.md) for available integrations

## Community

Have questions or feedback? Join [Discord](https://discord.gg/nwb3CRVN).
