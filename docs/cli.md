# CLI Reference

All commands are run from the workspace or project root directory, inside the `dev_workspace` virtual environment.

```bash
source .venvs/dev_workspace/bin/activate
python -m finecode <command> [options]
```

---

## Usage modes

The `run` command supports two usage modes.

### Standalone (one-shot) — default

Each `run` invocation is fully independent. FineCode starts a dedicated WM Server subprocess for the duration of the command, then shuts it down on exit. This is the default behavior.

```bash
python -m finecode run lint
```

Use this in CI/CD pipelines or any context where you don't want persistent background processes. Results from one action can be saved to the file cache and referenced by a later action via `--map-payload-fields` (see the `run` reference below).

### Persistent server

A long-lived WM Server holds warm state — loaded configuration, started runners — across multiple `run` calls. Use `--shared-server` to connect to a running shared instance instead of starting a dedicated one.

```bash
# Connect to the shared server (start it first if needed):
python -m finecode run --shared-server lint
python -m finecode run --shared-server format
```

This mode is used automatically by the LSP and MCP integrations. It gives faster repeated runs because configuration loading and runner startup are amortized across calls.

You do not have to start the shared server first — `run`, `prepare-envs` and `dump-config` start one if none is listening, and find it afterwards through the discovery file. (The [recovery commands](#recovery-commands) are the exception: they deliberately refuse to start one.)

**What amortizes it is the server staying up, not the flag.** A client disconnecting does not discard the loaded configuration or the started runners — only the server exiting does, and it exits 30 seconds after the last client disconnects (`--disconnect-timeout` on `start-wm-server`). So two `run --shared-server` calls further apart than that each pay full startup, exactly as if the flag had not been passed. To hold the state for longer, run the shared server under something that owns its lifetime and pass `--keep-alive` (see [`start-wm-server`](#start-wm-server)).

---

## `run`

Run one or more actions across projects.

```
python -m finecode run [options] <action> [<action> ...] [payload] [--config.<key>=<value> ...]
```

### Options

| Option | Description |
|---|---|
| `--workdir=<path>` | Use `<path>` as the workspace root instead of `cwd` |
| `--project=<name>` | Run only in this project (matched by `[project].name` from `pyproject.toml`). Repeatable for multiple projects. |
| `--concurrently` | Run actions concurrently within each project |
| `--shared-server` | Connect to the shared persistent WM Server instead of starting a dedicated one |
| `--wal` | Enable WM write-ahead log (WAL) for the dedicated WM server started by this run command |
| `--log-level=<level>` | Set log level: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
| `--verbose` / `-v` | Stream WM and ER diagnostic logs to stderr live over the protocol (`server/logRecords`). Auto-enabled in CI. |
| `--no-env-config` | Ignore `FINECODE_CONFIG_*` and `FINECODE_SERVICE_CONFIG_*` environment variables |
| `--no-save-results` | Do not write action results to the cache directory |
| `--results-file=<path>` | Also write *this run's* results to `<path>`, unmerged, on every exit path, and report the path on stderr. See [Per-run results file](#per-run-results-file) |
| `--dev-env=<env>` | Override the detected dev environment. One of: `ai`, `ci`, `cli`, `ide`, `precommit` (default: auto-detected — see [Dev environment detection](#dev-environment-detection)) |
| `--env=<name>` | For a matrixed action (ADR-0047), restrict execution to the named interpreter environment(s) — a matrix base selects all of its children, a concrete child selects only itself. Repeatable. Non-matrix envs are unaffected. See [Preparing Environments — filtering by environment name](guides/preparing-environments.md#filtering-by-environment-name). |
| `--interpreter=<impl>@<version>` | For a matrixed action, restrict execution to the named interpreter(s) across every matrix env the action touches. Repeatable; a bare version means `cpython`. See [Preparing Environments — filtering by interpreter](guides/preparing-environments.md#filtering-by-interpreter). |

In a multi-project workspace, `run` fans out across every project that declares the action, bounded by `FINECODE_WM_RUN_MAX_CONCURRENT_PROJECTS` (default: derived from the machine's CPU budget). Fan-out is throttled, never refused — workspace size does not limit which actions you can run. See [Run fan-out concurrency](guides/wm-server-internals.md#run-fan-out-concurrency).

`--env` and `--interpreter` on `run` use the same selector semantics as `prepare-envs` (ADR-0050): they compose by intersection, and a matrix env's config-declared `default_interpreters` policy (see [Preparing Environments — default interpreter subset](guides/preparing-environments.md#default-interpreter-subset)) applies as the default when neither is given — so a plain `run` can execute only a local subset of a matrix (e.g. the newest interpreter) while CI still runs the full axis, mirroring `prepare-envs`.

WAL environment variable and storage settings are shared with `start-wm-server` — see [`start-wm-server`](#start-wm-server) for details.

### Per-run results file

By default results go to `<venv>/cache/finecode/results/<action-source>.json`, which is
**read-modify-written on every run**: each run adds or replaces one project key and
leaves every other key in place. That file is what `--map-payload-fields` reads, so
it stays as it is — but it means a reader cannot tell entries this run produced from
entries left by earlier ones, possibly for projects the run never touched.

`--results-file=<path>` writes a second file describing one run and nothing else:

```bash
python -m finecode run --results-file=/tmp/lint.json lint \
  --project_paths='["file:///ws/backend"]'
```

```json
{
  "finecode_results_version": 1,
  "return_code": 1,
  "projects_requested": null,
  "project_paths_requested": null,
  "payload": {"project_paths": ["file:///ws/backend"]},
  "actions": {
    "fine_lint.LintAction": {
      "scope": "workspace",
      "results": {
        "/ws": {"return_code": 1, "result": {"messages": {}}}
      }
    }
  }
}
```

`scope` is the field to read before anything else. A **workspace-scoped** action runs
once and files its result under the project that *hosted* it — the workspace root —
however many projects it was pointed at, so the key under `results` is not a project
identity and looking up your project by key finds nothing. Take project membership
from the file URIs inside the result instead. For a **project-scoped** action the key
is the project, and a lookup is correct. Nothing else in the payload distinguishes the
two cases, which is why `scope` is recorded.

`scope` is `null` when the run could not resolve it — an action the WM reports without
a declared scope, or a result filed under a source that was never listed. Treat `null`
as "unknown", not as either case above: a key lookup may or may not be a project, so
read project membership out of the result the way a workspace-scoped action requires.

Each entry under `results` carries its own `return_code`, because the document's
top-level `return_code` is the whole run's and cannot say which action or which project
produced a failure. `result` is the action's JSON result, or `null` when the action
returned none — a fully streamed matrixed action merges to no JSON payload at all.

`projects_requested`, `project_paths_requested` and `payload` record the request rather
than the outcome, which is what separates "ran and found nothing" from "was dispatched
to nothing at all". `projects_requested` holds the `--project=<name>` values as typed;
`project_paths_requested` holds what those names resolved to, and it is the one to join
against the keys under `results`, which are paths. Both are `null` when the run was not
restricted to a subset of projects, and `project_paths_requested` is also `null` when
the run failed before resolving them.

The file is written on **every** exit path, including runs that failed before any action
executed (`actions` is then `{}`). A failed run must not leave the previous run's file in
place: it is complete, well-formed, carries the same version, and nothing in it says it
describes a different run. The write goes through a temporary file in the same directory
and is renamed into place, so a concurrent reader never sees a half-written document.

`--results-file` implies the JSON result format, so it works alongside
`--no-save-results` when you want this run's record without touching the shared cache.
The confirmation line is printed to stderr, leaving stdout to the action's own output.

### Payload

Named parameters passed to the action payload. All must use `--<name>=<value>` form:

```bash
python -m finecode run format --save=true
python -m finecode run lint --target=files --file-paths='["src/main.py"]'
```

### Config overrides

Override handler configuration inline:

```bash
# Action-level (applies to all handlers)
python -m finecode run lint --config.line_length=120

# Handler-specific
python -m finecode run lint --config.ruff.line_length=120 --config.mypy.strict=true
```

See [Configuration](configuration.md) for full details on config precedence.

### Behavior

- With no `--project`: FineCode treats `cwd` (or `--workdir`) as the workspace root, discovers all projects, and runs the action in each project that defines it.
- With `--project`: the action must exist in every specified project.
- Action results are saved to `<venv>/cache/finecode/results/<action>.json` (one entry per project path).
- WAL options on `run` apply only when FineCode starts a dedicated WM server (default mode). In `--shared-server` mode, configure WAL on the shared WM server process.

### Examples

```bash
# Lint all projects
python -m finecode run lint

# Lint and check_formatting concurrently
python -m finecode run --concurrently lint check_formatting

# Run only in two specific projects
python -m finecode run --project=fine_python_mypy --project=fine_python_ruff run lint

# Run from a different directory
python -m finecode --workdir=./finecode_extension_api run lint

# Override ruff line length
python -m finecode run lint --config.ruff.line_length=120

# Run a matrixed action's "testing" env only for its cpython@3.11 child
python -m finecode run run_tests --env=testing@cpython-3.11

# Run every matrix env's 3.12 interpreter
python -m finecode run run_tests --interpreter=3.12
```

---

## `prepare-envs`

Create and populate virtual environments for all handler dependencies.

```
python -m finecode prepare-envs [--recreate] [--env=<name>]...
                                 [--project=<name>]... [--max-concurrent-projects=<n>]
                                 [--log-level=<level>] [--verbose] [--debug]
```

Must be run from the workspace or project root. Creates venvs under `.venvs/<env_name>/` and installs each handler's declared dependencies.

By default (no `--verbose` needed), the command prints a progress line to stderr for each orchestration step (project discovery, dev_workspace bootstrap, runner startup, `create_envs`, `install_envs`), plus a running `N/total` counter as each project finishes `create_envs`/`install_envs` — the two steps that run package-manager subprocesses and can otherwise appear to hang for a while on a large workspace. `--verbose` additionally streams full WM/ER diagnostic logs.

See [Preparing Environments](guides/preparing-environments.md) for a full explanation of the three-step sequence and filtering options.

| Option | Description |
|---|---|
| `--recreate` | Delete and recreate all venvs from scratch |
| `--env=<name>` | Restrict handler dependency installation to the named env(s). Repeatable. See note below. |
| `--project=<name>` | Restrict preparation to the named project(s) (matched by `[project].name` from `pyproject.toml`). Repeatable. |
| `--max-concurrent-projects=<n>` | Cap on concurrent projects during `create_envs`/`install_envs`. Defaults to a machine-based value (same env var: `FINECODE_WM_PREPARE_ENVS_MAX_CONCURRENT_PROJECTS`). See [Preparing Environments — bounding concurrency](guides/preparing-environments.md#bounding-concurrency). |
| `--log-level=<level>` | Set log level: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
| `--verbose` / `-v` | Stream WM and ER diagnostic logs to stderr live over the protocol (`server/logRecords`). Auto-enabled in CI. |
| `--debug` | Wait for a debugpy client on port 5680 before starting |
| `--dev-env=<env>` | Override the detected dev environment. One of: `ai`, `ci`, `cli`, `ide`, `precommit` (default: auto-detected) |


!!! note `--env` restricts only the `install_envs` step. The `create_envs` step still runs for **all** envs regardless of this flag — virtualenvs must exist for every env even when you only need to update dependencies in one of them.

---

## `dump-config`

Dump the fully resolved configuration for a project to disk, useful for debugging preset and config merging.

```
python -m finecode dump-config --project=<name> [--log-level=<level>] [--debug]
```

Output is written to `<cwd>/finecode_config_dump/`.

| Option | Description |
|---|---|
| `--project=<name>` | **(Required)** Project to dump config for (matched by `[project].name` from `pyproject.toml`) |
| `--log-level=<level>` | Set log level: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
| `--debug` | Wait for a debugpy client on port 5680 |
| `--dev-env=<env>` | Override the detected dev environment. One of: `ai`, `ci`, `cli`, `ide`, `precommit` (default: auto-detected) |

---

## Recovery commands

`reload-action`, `restart-runner`, `reload-config` and `restart-wm` make a *running*
workspace pick up something that changed on disk, without restarting the editor or
agent that is using it. They form a ladder from cheapest to widest; pick the
narrowest one that covers what you edited, because nothing detects staleness for you.

| Command | Covers | Does not cover |
| --- | --- | --- |
| `reload-action` | the packages owning an action and its handlers | any other package; configuration |
| `restart-runner` | any code a runner imported, and stuck or crashed runners | configuration |
| `reload-config` | `pyproject.toml`, `finecode.toml` and presets — and, since it replaces runners, all code too | FineCode's own source |
| `restart-wm` | everything, including FineCode's own source | — |

**All four require `--shared-server`.** Without it each command would start a
workspace server of its own, recover that, and exit — leaving the workspace an editor
or agent is actually using untouched while reporting success. They exit with status 1
and name the mode as the reason.

Addressing follows the same rule everywhere: `--project` *or* `--all-projects`,
never both and never neither, so workspace-wide recovery is always asked for
explicitly (`reload-action` is the exception: it defaults to every project exposing
the action, because reloading one unnecessarily costs almost nothing).

A recovery that would replace runners is refused while an action is running in the
target project, and the refusal names the run. `--kill-in-flight-runs` proceeds
anyway; it is the remedy for a run that is hung, and it kills the run.

```bash
# after editing a handler
python -m finecode reload-action --shared-server --action=lint --project=/abs/path

# after editing pyproject.toml
python -m finecode reload-config --shared-server --project=/abs/path

# after adding a project directory
python -m finecode reload-config --shared-server --all-projects --rescan

# after editing FineCode itself
python -m finecode restart-wm --shared-server
```

## Dev environment detection

FineCode tracks which environment triggered an action run (e.g. IDE, CLI, CI/CD). This value is passed to handlers via `RunActionMeta.dev_env` and can be used to adjust behavior — for example, to emit machine-readable output in CI.

The `run`, `prepare-envs`, and `dump-config` commands detect the environment automatically:

| Condition | Detected value |
|---|---|
| `CI` environment variable is set (any non-empty value) | `ci` |
| Default | `cli` |

The `CI` variable is set automatically by GitHub Actions, GitLab CI, CircleCI, Travis CI, Bitbucket Pipelines, and most other CI systems.

Use `--dev-env=<value>` on any command to override the detected value explicitly:

```bash
# Force CI/CD mode locally
python -m finecode run --dev-env=ci lint

# Mark as a pre-commit run
python -m finecode run --dev-env=precommit lint
```

Valid values: `ai`, `ci`, `cli`, `ide`, `precommit`.

---

## Diagnostic logs in CI

FineCode runs actions in Extension Runner (ER) subprocesses. Their logs, and the WM's own, are normally written to files — invisible in a CI job log where they matter most, since a failed CI run usually can't be reproduced interactively.

Two independent flags control this:

- **`--verbose` / `-v`** decides *whether* WM and ER logs are streamed back to the CLI's stderr (over `server/logRecords`). It is **auto-enabled when `dev_env` is `ci`**, so their diagnostics land in the CI job log without any extra configuration.
- **`--log-level`** decides *at what level* — it is a single knob that applies uniformly to the CLI, the WM, and every ER (their logs share one stream). It defaults to `INFO`.

So in CI you get full subprocess visibility at `INFO` by default. `DEBUG` is noisy, so rather than forcing it on every run, **compute the level in your CI configuration and pass it as `--log-level`** — INFO normally, DEBUG only when you ask for it. This keeps the debug-vs-info decision in your pipeline; FineCode simply honors the flag.

The recommended trigger on GitHub Actions is its built-in **"Re-run with debug logging"** button, which sets `RUNNER_DEBUG=1`:

```yaml
# Compute once, expose to later steps via $GITHUB_ENV
- name: Determine FineCode log level
  run: |
    if [ "${RUNNER_DEBUG:-0}" = "1" ]; then
      echo "FINECODE_LOG_LEVEL=DEBUG" >> "$GITHUB_ENV"
    else
      echo "FINECODE_LOG_LEVEL=INFO" >> "$GITHUB_ENV"
    fi

# Pass it to every finecode invocation
- run: python -m finecode run --log-level="$FINECODE_LOG_LEVEL" lint
```

The equivalent on other systems is any variable your CI can toggle per run (a pipeline parameter, a `workflow_dispatch` input, a branch/commit convention) mapped to `INFO`/`DEBUG` and passed through `--log-level`.

> **Note:** on-demand DEBUG only helps for *reproducible* failures. A flaky, non-deterministic failure may not recur on a debug re-run, so its DEBUG detail is lost. If that class of failure is common in your pipeline, default the computed level to `DEBUG` instead.

---

## `start-lsp`

Start the FineCode LSP server. Used by the IDE extension — you typically don't call this directly.

```
python -m finecode start-lsp --stdio | --socket <port> | --ws [--host <host>] [--port <port>]
```

| Option | Description |
|---|---|
| `--stdio` | Communicate over stdin/stdout |
| `--socket <port>` | Start a TCP server on the given port |
| `--ws` | Start a WebSocket server |
| `--host <host>` | Host for TCP/WS server (default: 127.0.0.1 for TCP) |
| `--port <port>` | Port for TCP/WS server |
| `--log-level=<level>` | Set log level: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
| `--debug` | Wait for a debugpy client on port 5680 |

The LSP server connects to the **FineCode WM Server** on startup (starting one if needed). See [LSP and MCP Architecture](reference/lsp-mcp-architecture.md) for details.

---

## `start-mcp`

Start the FineCode MCP server on stdio. Connects to a running FineCode WM Server (or starts one) and exposes FineCode tools via the Model Context Protocol.

```text
.venvs/dev_workspace/bin/python -m finecode start-mcp [--workdir=<path>] [--log-level=<level>]
```

| Option | Description |
| --- | --- |
| `--workdir=<path>` | Workspace root directory (default: current directory). |
| `--log-level=<level>` | Set log level: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |

Typically started automatically by MCP-compatible clients (for example, Claude Code) or by VS Code Copilot when the FineCode VSCode extension registers the MCP provider.

For setup details, see [IDE and MCP Setup](getting-started-ide-mcp.md#mcp-setup-for-ai-clients). If you use VS Code without the FineCode extension, use the fallback `.vscode/mcp.json` configuration from that page.

---

## `start-wm-server`

Start the FineCode Workspace Manager Server standalone (TCP JSON-RPC), listen for client connections. Unless `--keep-alive` is given, it shuts down after the last client disconnects and the disconnect timeout expires.

```text
python -m finecode start-wm-server [--log-level=<level>] [--disconnect-timeout=<seconds>]
                                   [--keep-alive] [--detach] [--wal]
```

| Option | Description |
| --- | --- |
| `--log-level=<level>` | Set log level: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
| `--disconnect-timeout=<seconds>` | Seconds to wait after the last client disconnects before shutting down (default: 30) |
| `--keep-alive` | Never auto-stop — neither when no client connects after startup nor when the last one disconnects. `server/shutdown` (and so `restart-wm`) still stops it. |
| `--detach` | Start the shared server in the background and exit, doing nothing if one is already listening. Cannot be combined with `--port-file`. |
| `--wal` | Enable WM write-ahead log (WAL) for run lifecycle events. |

`--keep-alive` is for a server whose lifetime something else owns — a devcontainer, a
supervisor — where both auto-stop timers would end a workspace that is meant to stay
warm. Three consequences come with it: extension runners stay resident for as long as
that owner runs, closing the editor stops discarding them, so picking up changed code
is entirely on the [recovery commands](#recovery-commands), and the log level the
server started with is the one it keeps — configuring a different WM log level in an
editor takes effect only after a `restart-wm`.

`--detach` ensures *a* server is running, not a keep-alive one: if one is already
listening it does nothing, whatever that server's own settings are. On its own it is
rarely what you want — nothing connects to the server it starts, so the disconnect
timeout ends it seconds later. The other options are passed on to the server it
starts.

Together the two flags are how a workspace is kept warm: start the server from
whatever owns that lifetime — a container start script, a systemd unit, a supervisor
— and the CLI, LSP and MCP all find it through the usual discovery file.

```bash
python -m finecode start-wm-server --detach --keep-alive
```

Keep-alive has no environment variable and is never inherited: it is passed
explicitly by whoever starts the server, so the dedicated per-command servers cannot
pick it up and stop stopping. The flip side is that a server started *lazily* by a
client — the first `run --shared-server` after a crash, or the replacement
`restart-wm` starts — is a plain one, and the warm state is gone until the command
above is run again.

Usually started automatically by `start-lsp`, `start-mcp` or a CLI command in
`--shared-server` mode. Can also be started manually for debugging.

### Write-ahead log

`--wal` has an environment variable equivalent: `FINECODE_WAL_ENABLED=1` (or
`true`/`yes`/`on`).

WAL storage and retention are fixed in this version:

- WAL directory: `<venv>/state/finecode/wal/wm`
- Max segment size: `1048576` bytes
- Retention: last `20` segment files
