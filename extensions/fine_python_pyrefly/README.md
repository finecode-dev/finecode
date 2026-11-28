# fine_python_pyrefly

FineCode extension for the [Pyrefly](https://pyrefly.org) type checker.

## Topology

`PyreflyConfig` (`config.topology`) selects how language servers are arranged
across the workspace:

- **`"workspace"` (default)** — one pyrefly server runs in the workspace-root
  project; every other project forwards its diagnostics and IDE requests to it.
  All projects must use the same pyrefly **major.minor** (patch differences are
  fine). The workspace shares one server's resolution environment, and only one
  pyrefly process is resident.
- **`"per-project"`** — one server per project. No version constraint across
  projects; each project keeps its own resolution environment. Costs one
  resident server per project.

Set it in a project's definition file:

```toml
[[tool.finecode.service]]
interface = "fine_python_pyrefly.pyrefly_config.PyreflyConfig"
config.topology = "per-project"
```

## Configuration

Pyrefly is configured only through the `PyreflyConfig` service. A user-owned
`pyrefly.toml` or `[tool.pyrefly]` table is **not** supported in either
topology; a project that has one fails its runner start with the path to
remove. Error-kind severities go in the service's `errors` table:

```toml
[[tool.finecode.service]]
interface = "fine_python_pyrefly.pyrefly_config.PyreflyConfig"
config.errors.deprecated = "error"
```

FineCode generates the pyrefly config it runs with, under
`<project>/.finecode/pyrefly/<env>/pyrefly.toml`; do not edit it.
