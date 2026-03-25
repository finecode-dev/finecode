# ADR-0003: One Extension Runner process per project execution environment

- **Status:** accepted
- **Date:** 2026-03-19
- **Deciders:** @Aksem
- **Tags:** architecture, extension-runner

## Context

FineCode executes action handlers contributed by extensions. Each handler
declares the **execution environment** (`env`) it runs in and its own set of
dependencies. An execution environment is a named, isolated context serving a
specific purpose (e.g. `runtime` for the project's own runtime code,
`dev_workspace` for workspace tooling, `dev_no_runtime` for dev tools without
runtime deps). In Python, each execution environment is materialized as a
project-local virtual environment.

The **Extension Runner (ER)** is an inter-language concept — a process that
executes handler code inside a specific execution environment. The current
implementation, `finecode_extension_runner`, is Python-specific. Future
implementations for other languages (e.g. JavaScript, Rust) would follow the
same one-process-per-execution-environment model.

The primary requirement is to separate dependencies needed by the project's
own runtime from dependencies needed only by tooling. FineCode must be able to
run project code in one execution environment and run development tooling in
other execution environments without forcing them into a single shared
dependency set.

Once execution environments are isolated, they can also be made more
fine-grained by purpose. This allows tooling dependencies to be grouped
according to their role and makes it possible to move tools with incompatible
dependency requirements into separate execution environments when needed.

The Workspace Manager (WM) is a long-running server that must stay stable
across the full user session. A handler bug, crash, or blocking call in one
execution environment must not take down the WM or interfere with other
execution environments.

## Related ADRs Considered

None — process isolation model has no overlap with other ADRs at the time of writing.

## Decision

Each execution environment in a project runs as an independent
**Extension Runner (ER)** subprocess. In the Python implementation, the ER is
launched using the interpreter from the corresponding project-local virtual
environment, so each ER has a fully isolated dependency set.

Key properties of this design:

- **One ER per (project, execution environment) pair.** ERs are keyed by
  `(project_dir_path, env_name)` in the WM's workspace context.
- **Lazy startup with bootstrap exception.** An ER is started only when the
  first action request requiring its execution environment arrives, then cached
  and reused for subsequent requests. The `dev_workspace` execution
  environment is the exception because it must be started first to resolve
  presets for other execution environments.
- **JSON-RPC over TCP.** Each ER binds to a random loopback port on startup
  and advertises it to the WM. The WM connects via TCP and communicates using
  JSON-RPC with Content-Length framing (the same wire format as LSP).
- **Independent lifecycle.** An ER can crash and be restarted without
  affecting the WM or ERs for other execution environments. Shutdown is
  cooperative: the WM sends `shutdown` + `exit` JSON-RPC calls; the ER exits
  cleanly.
- **`dev_workspace` bootstrap execution environment.** The `dev_workspace`
  execution environment is always started first; it resolves presets for all
  other execution environments before they are configured or started.

## Consequences

- **Dependency isolation**: project runtime dependencies and tooling
  dependencies are kept separate, and tooling can be split further into
  purpose-specific execution environments when conflicts or different
  dependency sets require it.
- **Fault isolation**: a crash or hang in one ER does not affect the WM or
  other ERs. The WM can restart a failed ER independently.
- **Startup cost**: launching a Python subprocess and importing handler modules
  takes time. Mitigated by lazy startup and long-lived reuse.
- **Higher memory usage**: running multiple ER processes per project uses more
  RAM than a single shared process. The overhead is expected to be acceptable
  relative to the benefits of dependency isolation, fault isolation, and
  long-lived per-environment state.
- **One virtual environment per execution environment per project**:
  `prepare-envs` must create and populate the project-local virtual
  environment for each declared execution environment before the ER can start.
  Missing virtual environments result in `RunnerStatus.NO_VENV` rather than a
  crash.
- **`dev_workspace` is a prerequisite**: preset resolution depends on the
  `dev_workspace` ER being available. Actions in other execution environments
  cannot be configured until `dev_workspace` is initialized.

### Alternatives Considered

- **Single shared process for all handlers**: eliminates subprocess overhead
  but forces runtime code and tooling into one shared dependency set, makes
  fine-grained environment separation impractical, and means one handler crash
  can corrupt or kill the entire tool.
- **Thread per handler invocation**: handlers run in the same process and
  virtual environment. No dependency isolation; a blocking or crashing handler
  affects all others.
- **In-process plugin loading**: simplest architecture but handlers can import
  conflicting packages and accidentally mutate shared WM state.
- **New subprocess per handler invocation**: full isolation per call, but
  Python startup cost makes interactive use (e.g. format-on-save) too slow.
  It also prevents effective in-process caching between calls because each
  invocation starts with cold process state. The long-lived ER model amortizes
  startup cost across many invocations and allows caches to be retained in
  process when appropriate.
