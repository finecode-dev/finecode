# Developing FineCode

This guide is for developers contributing to FineCode itself — the monorepo structure, conventions, and workflows used internally.

## Repository structure

The repo is a monorepo. Each package has its own `pyproject.toml`. The root directory is the workspace.

```text
finecode/                          # Main package (Workspace Manager)
finecode_extension_api/            # Public API for extension authors
finecode_extension_runner/         # Extension execution engine
finecode_jsonrpc/                  # JSON-RPC client/transport layer
finecode_httpclient/               # HTTP client for extensions
finecode_knowledge/                # Knowledge model engine (schema-free; see below)
extensions/                        # Extension packages (ruff, flake8, mypy, ...)
presets/                           # Preset packages (recommended, lint, format)
finecode_dev_common_preset/        # Preset used for developing FineCode itself
tests/                             # Test suite
```

### The knowledge packages split in two

`finecode_knowledge` is the **engine**: the entity/fact model, the query IR and its
interpreter, the memoization DAG. It is stdlib-only, carries no schema of its own, and is a
runtime dependency of the WM — the WM loads the fact store and runs the DAG.

`presets/fine_knowledge` is **FineCode's own schema**: entity types, providers,
predicates, rules and the `extract_knowledge` / `which_handlers` / `audit_preset_deps`
actions. It is an ordinary extension, reached through the preset, and it depends on the
engine.

The split is R20 ("the core contains no language- or tool-specific logic") made a *packaging*
fact rather than a lint rule: the WM cannot import a rule, because the distribution holding
rules is not installed in its environment. A schema hands its `SchemaRegistry` to the engine
via `set_default_registry`; the engine never reaches for a schema module by name. Two tests
guard the boundary — `finecode_knowledge/tests/test_packaging.py` and the last case in
`tests/unit/test_knowledge_service.py` — and both read source text rather than the import
graph, because the violations this replaced were *deferred* imports inside function bodies
that no import-graph walk would traverse.

### Knowledge freshness: what refreshes itself, and what does not

`audit_code` and `which_handlers` answer from a fact store the WM owns. You do **not** normally
need to run `extract_knowledge` first: a verified read re-fingerprints each fact bucket's declared
sources, and any bucket whose sources moved is re-extracted on demand — one provider over one file,
not a workspace sweep. Facts derived from `pyproject.toml`, `preset.toml` and scanned Python
sources all work this way.

**One provider is exempt, and it is the one supplying the most-used facts.** `wm_registry` reads
the WM's *resolved configuration* through an in-process API — a merge of the preset chain and each
project's `pyproject.toml`, with no single file behind it — so it has nothing to fingerprint and its
bucket is permanently `UNTRACKED`. An untracked input means *we could not check*, not *it changed*,
so it never marks the bucket as moved and nothing ever re-extracts it. The facts this covers are
`Action`, `Handler` and `Environment`, and the `serves` / `handled_by` / `runs_in` edges — plus
`calls`, which inherits the same verdict.

The practical rule: **after changing a preset or a project's `[tool.finecode]` config, run
`extract_knowledge` manually.** Editing ordinary source files needs no such step.

Results always disclose this — every answer carries a freshness verdict, and the `UNTRACKED`
reservation shows up as an `Information` diagnostic. But because it is *permanent* rather than
raised when something actually changes, it cannot tell you whether these facts are currently stale;
it only tells you they are uncheckable. Giving `wm_registry` a fingerprintable input (the resolved
config dump, or the set of files feeding it) is what would retire that reservation, and is tracked
as finding 2 of `knowledge-model-eval/use-cases/0002-developer-defined-knowledge.md` in the internal
docs.

## Setting up the development environment

```bash
# From the repo root, inside the dev_workspace venv:
python -m finecode prepare-envs

# Re-prepare a single environment (e.g. after changing its dependencies):
python -m finecode prepare-envs --env=dev_no_runtime

# Multiple envs at once:
python -m finecode prepare-envs --env=dev --env=dev_no_runtime

# Prepare only a specific project:
python -m finecode prepare-envs --project=finecode_extension_api

# Combine filters — one env in one project:
python -m finecode prepare-envs --project=finecode_extension_api --env=dev_no_runtime
```

No `--shared-server` here: on first setup the `dev_workspace` venv does not exist yet, so
the devcontainer's autostart script is a no-op and there is no server to connect to. Once
the workspace is prepared, add the flag to re-prepare runs as well — see
[Running checks](#running-checks).

## Continuous Integration

CI must exercise the current branch's source, so it has the same requirement as any contributor's dev environment: `finecode`, `finecode_dev_common_preset`, and the other monorepo packages need to be installed **editable, from local source** — not from a released version.

`pipx run finecode bootstrap` / `uvx finecode bootstrap` (see [Getting Started](../getting-started.md)) cannot be used for this. Bootstrap is designed for external consumers: it uses a throwaway pipx-installed `finecode` purely to drive the install, then installs *pinned released versions* declared in the consumer's own `pyproject.toml`. This repo's own `pyproject.toml` marks `finecode` and `finecode_dev_common_preset` with no version specifier precisely because they are always installed editable — there is no released version to pin bootstrap to that matches the working tree. A venv can't hold both an editable local install and a PyPI release of the same package name at once, so mixing the two approaches isn't an option either.

The canonical, maintained implementation of "install all local packages editable" is `scripts/setup-dev-workspace.sh` — extracted from the devcontainer's post-create step so both the devcontainer and CI call the same script instead of maintaining separate `-e` package lists that can drift apart the way the old `pipx run finecode bootstrap` step did. The editable package list itself is generated by `scripts/list_dev_workspace_editables.py` rather than hand-maintained, so it can't silently go stale either.

**Private packages behind a workspace selection.** Packages held in private repositories and never published (for example `fine_lint_fix`) are enabled by the gitignored `finecode-workspace-user.toml` at the workspace root, whose only permitted key is `extras`: `extras = { finecode_dev_common_preset = ["lint_fix"] }`. Enabling one requires two switches — the private clones present on disk *and* the file present selecting the extra. A developer with the clones but no file gets a working checkout with the private layer off; a clean public checkout has neither and is unaffected.

**Private-layer audit in CI.** The public CI pipeline runs a separate, non-required `audit-private` job that clones the two private repositories (`fine_knowledge` and `finecode_internal_experiments`) with an App token minted per run, copies `.github/ci/finecode-user.ci.toml` into place as the tracked counterpart to a developer's gitignored `finecode-user.toml`, and then runs `extract_knowledge` followed by `audit_code`. The job is gated so fork pull requests skip it and the remaining steps are gated on the App credentials being present; it is separate from the `build` job so a break in a private repository reds one non-required check instead of blocking every merge. `extract_knowledge` must precede `audit_code` because `AuditPresetDepsHandler` never re-extracts a missing fact store itself — without it the audit emits a diagnostic telling the operator to run `extract_knowledge` first. The `.ci` infix in the filename is load-bearing, not cosmetic: `.gitignore`'s unanchored `finecode-user.toml` pattern matches at any depth, so the plain name could not be tracked at all.

The devcontainer also runs a FalkorDB sidecar (used by `fine_dep_graph_falkordb`), but no test currently depends on a live FalkorDB instance, so CI does not start one. If tests start needing it, add it as a separate ubuntu-only job with a `services:` container — GitHub-hosted Windows/macOS runners don't support job services at all, so it can't simply be added to the existing matrix job.

## Running checks

```bash
python -m finecode run --shared-server inspect_code      # lint + type_check together
python -m finecode run --shared-server check_formatting
python -m finecode run --shared-server run_tests
python -m finecode run --shared-server audit_code        # checkpoint checks — before a commit or a PR
```

**Always pass `--shared-server` when developing FineCode.** The devcontainer's
`postStartCommand` already runs `start-wm-server --detach --keep-alive`
(see [.devcontainer/README.md](../../.devcontainer/README.md#persistent-wm-server)), so a
warm server with resident extension runners is there for every command to connect to.
Without the flag each `run` starts a dedicated WM server, reloads the workspace config and
restarts the runners, then throws all of it away — seconds of setup per command, paid
again on the next one. `prepare-envs` and `dump-config` take the same flag, for the same
reason.

Three cases where you drop it: CI (no shared server exists there — see
[Continuous Integration](#continuous-integration) above), deliberately testing standalone
behaviour, and raising the log level (below) — a shared server keeps the level it was
started with, so `--log-level` only reaches a server the command starts itself.

`inspect_code` is the continuous-inspection umbrella: both `lint` and `type_check`
register into it, so one call reports what either would. Run them separately only when
you want one of them alone. `audit_code` is the deliberate-checkpoint peer
([ADR-0044](../../../finecode_internal_docs/adr/0044-continuous-inspection-and-on-demand-audit-are-separate-umbrellas.md))
— import-linter, toolchain checks and other whole-project checks reach it through bridge
handlers. It belongs at the end of a piece of work, not in the edit loop, and narrowing
it does not reliably shorten it: a bridge fans out per project on its own, so
`--project_paths` narrows what is *reported*, not what is *run* — a single-project
`audit_code` was still running after seven minutes on this workspace.

### Narrowing a check to one project or one file

A workspace-wide check across ~70 projects takes a minute or more; the same check
narrowed to the file you just edited takes a couple of seconds against a warm
`--shared-server`. Narrow by **scope**:

| Action | Scope | Narrow to a project with | Narrow to files with |
|---|---|---|---|
| `lint`, `type_check`, `inspect_code`, `audit_code` | workspace | `--project_paths='["<path>"]'` (payload) | `--target=files --file_paths='[...]'` |
| `check_formatting`, `format` | project | `--project=<name>` (option) | `--target=files --file_paths='[...]'` |
| `run_tests`, `list_tests` | project | `--project=<name>` (option) | `--file_paths='[...]'` (absolute paths) |

```bash
# workspace-scoped: the project selector is a payload field
python -m finecode run --shared-server inspect_code --project_paths='["finecode_extension_api"]'

# project-scoped: the project selector is a run option, before the action name
python -m finecode run --shared-server --project=finecode_extension_api check_formatting
python -m finecode run --shared-server --project=finecode_extension_runner --interpreter=3.13 run_tests

# one file — the fast edit loop
python -m finecode run --shared-server inspect_code --target=files \
  --file_paths='["finecode_extension_runner/src/finecode_extension_runner/impls/file_editor.py"]'
```

`--project=<name>` takes the `[project].name` from `pyproject.toml`, not a path;
`--project_paths` takes paths, and relative, absolute and `file://` forms all work.
`--interpreter=3.13` runs one interpreter of the matrixed `testing` env instead of the
whole axis — drop it for the full check.

Payload fields are validated at the CLI against the action's schema before anything
runs: an unknown field name is refused, and a value that cannot be the field's declared
type (for example a scalar where the action declares a list) is refused with a message
showing the expected form. Fields routed through `--map-payload-fields` keep their
placeholder value and are not type-checked.

Three ways to get this wrong. The first two fail **silently**: the command exits
normally and looks like it did what you asked.

1. **Run options go before the action name; payload fields go after it.** The option
   parser stops at the first non-`--` argument, so `run check_formatting --project=X`
   is rejected — `project` is parsed as a *payload field*, and payload fields are
   validated against the action's schema, so a field the action does not declare stops
   the run with an exit-code-1 error naming the field. Before that validation the field
   was silently ignored and the check ran in every workspace project. The correct form
   is `run --project=X check_formatting`.
2. **`--project` on a workspace-scoped action is an error**, not a narrowing:
   `Action 'fine_lint.LintAction' is workspace-scoped; do not pass a project path`. Use
   `--project_paths`.

A file-scoped run of a *project*-scoped action (`check_formatting`, `run_tests`) still
fans out to every project — the file list is not a project selector — so the output
carries a block per project even though only the named files were checked. Add
`--project=` to keep it to one block.

## Test documentation

Test docstrings answer one question: **why does this behavior matter to someone operating this system?**  They do not describe how the code achieves it, nor restate what the test body already shows.

### What to put in a test docstring

- The **observable contract** — what an operator or developer can verify from the outside.
- The **consequence of failure** — what goes wrong for a real user if the test regresses.
- Non-obvious constraints on the test mechanics themselves (why a specific timeout was chosen, why a sleep is present, why exit code 0 or 1 are both accepted).

### What to omit

- Internal function names, module paths, or flag values — these belong as comments in the source code that sets them, not in the test that verifies their effect.  When that function is renamed, the test still passes but the docstring silently lies.
- Step-by-step sequences that restate what the test body already shows — a reader who wants the steps reads the code.

### Methodology

This follows **Specification by Example** (Gojko Adzic) and the **BDD** observable-behavior framing: a test is the living specification of a contract.  Because the docstring does not run, anything in it that references an implementation detail will rot without warning.  Observable-behavior descriptions degrade gracefully — they stay true across refactors.

### Example

```python
# Before — names internals, restates test steps
def test_child_wm_dies_on_mcp_sigkill(workspace_dir, tmp_path):
    """WM child process dies when the MCP process group receives SIGKILL.

    ``start_own_server()`` intentionally does *not* use ``start_new_session``
    when spawning the dedicated WM subprocess, so both MCP and WM share the
    same process group. ...

    Sequence:
      1. Start the MCP server; it spawns a dedicated WM child.
      2. Poll the per-test WM port file — proves WM is up.
      ...
    """

# After — observable contract and consequence of failure
def test_child_wm_dies_on_mcp_sigkill(workspace_dir, tmp_path):
    """WM child process dies when the MCP process is force-killed.

    When an IDE crashes or the process is OOM-killed, the WM spawned by MCP
    must die with it. A surviving WM occupies a port and blocks the next MCP
    startup — a ghost process the user cannot easily discover.
    """
```

## Logging strategy (development policy)

This section defines the logging policy contributors should follow when adding or changing logs in FineCode.

The policy below defines the approach for reducing noise while keeping deep diagnostics available.

### Goals

- keep logs useful in normal development and CI runs
- allow deep diagnostics only when needed
- make noisy areas controllable per module
- avoid logging sensitive data

### Level policy

- `ERROR`: operation failed and needs attention; include actionable context
- `WARNING`: recoverable problem, degraded behavior, or skipped step
- `INFO`: lifecycle milestones and key business events (start/stop, action run result)
- `DEBUG`: developer diagnostics for branch decisions and compact internal state
- `TRACE`: high-volume details (payload previews, loop-level details, per-item processing)

Rules:

- default global level must be `INFO`
- `TRACE` must be disabled by default
- `TRACE` should be opt-in for specific modules or short debugging sessions
- avoid `INFO` in tight loops; use `TRACE`/`DEBUG` instead

### WM log groups

Use per-logger-group levels so WM diagnostics can be enabled surgically without turning on global trace.

A *log group* is a named logger. By convention the name matches the module path, but a single group can span multiple modules. Prefix matching applies: setting a level for `"finecode.wm_server"` covers all sub-modules under that path.

WM log groups are configured under `[workspace.wm.logging]` in `finecode-workspace.toml` (not in project config — this section controls only the WM process):

```toml
[workspace.wm.logging.log_groups]
"finecode.wm_server.runner.runner_manager" = "DEBUG"
"finecode_jsonrpc.client" = "TRACE"
```

Env var overrides (uppercase group name, `.` → `_`):

```bash
FINECODE_WM_LOG_GROUP_FINECODE_WM_SERVER_RUNNER_RUNNER_MANAGER=DEBUG
FINECODE_WM_LOG_GROUP_FINECODE_JSONRPC_CLIENT=TRACE
```

CLI log level override:

```bash
python -m finecode run --log-level=TRACE lint
python -m finecode start-wm-server --log-level=DEBUG
```

Note the absent `--shared-server`: a running server keeps the level it was started with,
so `--log-level` on `run` only takes effect on a WM that same command starts. To raise the
level of the devcontainer's persistent server, restart it with
`python -m finecode start-wm-server --detach --keep-alive --log-level=DEBUG`.

Notes:

- `--log-level` is supported by all commands: `run`, `prepare-envs`, `dump-config`, `start-lsp`, `start-wm-server`, `start-mcp`
- `prepare-envs --env=<name>` limits environment preparation to the named env(s); the flag may be repeated
- `prepare-envs --project=<name>` limits to the named project(s); the flag may be repeated; can be combined with `--env`
- when a CLI command spawns a dedicated WM server subprocess, the log level is propagated automatically
- log group overrides take precedence over the global level (prefix matching: the longest matching prefix wins)

### ER logging configuration

Each Extension Runner (ER) is a separate subprocess. Its logging is configured via a dedicated `[tool.finecode.er]` section, separate from the WM logging section above. The WM reads this config, merges it with env var overrides, and delivers the final resolved config to the ER via the `finecodeRunner/updateConfig` protocol call — the ER never reads config files or env vars for logging directly.

#### Config shape

```toml
# project-level fallback — applies to all ERs in this project
[tool.finecode.er.logging]
default_level = "INFO"

[tool.finecode.er.logging.log_groups]
"finecode_extension_runner" = "WARNING"   # suppress ER framework noise everywhere

# per-env override — applies only to the dev_no_runtime ER
[tool.finecode.er.envs.dev_no_runtime.logging]
default_level = "DEBUG"

[tool.finecode.er.envs.dev_no_runtime.logging.log_groups]
"fine_python_ruff" = "TRACE"
# "finecode_extension_runner" = "WARNING" is still inherited from the project fallback
```

Merge rules:

1. Start from the hardcoded default `INFO`.
2. Apply `tool.finecode.er.logging` (project-level fallback) if present.
3. Apply `tool.finecode.er.envs.<env_name>.logging` (per-env) if present — `default_level` replaces; `log_groups` merges additively (per-env entries win on collision).
4. Apply env var overrides last.

#### Env var overrides

| Variable | Effect |
| --- | --- |
| `FINECODE_ER_LOG_LEVEL` | project-level fallback `default_level` |
| `FINECODE_ER_ENV_<ENV>_LOG_LEVEL` | per-env `default_level` (`<ENV>` uppercased, `-`→`_`) |
| `FINECODE_ER_LOG_GROUP_<GROUP>` | project-level `log_groups` entry (`<GROUP>` uppercased, `.`→`_`) |
| `FINECODE_ER_ENV_<ENV>_LOG_GROUP_<GROUP>` | per-env `log_groups` entry |

Example — trace ruff in `dev_no_runtime` without editing any file:

```bash
FINECODE_ER_ENV_DEV_NO_RUNTIME_LOG_LEVEL=DEBUG
FINECODE_ER_ENV_DEV_NO_RUNTIME_LOG_GROUP_FINE_PYTHON_RUFF=TRACE
```

#### Log groups in ER

The two most useful groups for debugging:

| Group prefix | What it covers |
| --- | --- |
| `finecode_extension_runner` | ER framework internals (DI, RPC, handler dispatch) |
| `fine_python_ruff` / `fine_python_mypy` / … | individual extension/handler code |

Prefix matching applies: `"fine_python_ruff"` covers `fine_python_ruff.linter`, `fine_python_ruff.formatter`, etc.

#### `dev_workspace` ER: startup log level

The `dev_workspace` ER has a bootstrapping constraint: it must be started before the project config can be collected, because collecting the config (preset resolution) requires a running ER. When the ER process is first launched, the project is not yet a `CollectedProject`, so `env_configs` are unavailable and the ER always starts with `--log-level=INFO`.

The configured level from `[tool.finecode.er.envs.dev_workspace.logging]` is applied afterward via `finecodeRunner/updateConfig`, once `collect_project` completes. This means:

- Logs from the ER startup and preset-resolution phase are always at `INFO`, regardless of config.
- Logs from actions dispatched after initialization (e.g. `create_envs`, `install_envs`) use the configured level.

On subsequent restarts (when the project is already a `ResolvedProject`), the ER starts directly at the configured level because `env_configs` are available at that point.

### What to log

Log at boundaries where failures or latency matter:

- request start/end with identifiers (`request_id`, `run_id`, `project`, `action`)
- external process and RPC boundaries (spawn, send, receive, timeout, cancel)
- retries, fallbacks, and decision points
- final result summary (status, duration, item counts)

For high-volume objects:

- log previews and metadata instead of full payloads
- include sizes/counts (`len`, keys, return code) rather than full dumps
- use full payload logs only at `TRACE`

### Structured fields

Prefer structured fields over f-string interpolation when the data has independent query value — i.e., when you would want to filter or aggregate by that value in Loki or a log viewer.

```python
# preferred — each field is queryable independently
logger.bind(action=name, project=project_name, file_count=n).info("action executed")

# avoid for queryable data — the values are buried in a string
logger.info(f"action {name} executed on {n} files in {project_name}")
```

Use the loguru `bind` / `contextualize` APIs:

```python
# one-off: attach fields to a single log call
logger.bind(env=env_name, duration_ms=elapsed).info("ER started")

# contextual: fields attach to all calls inside the block
with logger.contextualize(run_id=run_id, action=action_name):
    logger.info("dispatch started")
    ...
    logger.debug("result ready")
```

**When to add structured fields:**

- identifiers that appear in multiple log lines and you would want to correlate (`action`, `project`, `env`, `run_id`, `request_id`)
- numeric measurements with clear semantics (`duration_ms`, `file_count`, `error_count`, `return_code`)
- outcome categories (`status`, `error_type`)

**When not to add structured fields:**

- purely narrative context that has no independent query value (`"starting up"`, `"done"`)
- large, free-form strings — keep those in the message body

Structured fields are forwarded automatically to OTel/Loki (via the loguru→OTel sink in `telemetry.py`) when `FINECODE_OTLP_ENDPOINT` is set. No call-site change is needed to enable that.

### Safety and performance guardrails

- never log secrets or tokens (API keys, auth headers, credentials, full env dumps)
- redact known sensitive keys (`token`, `password`, `secret`, `authorization`)
- prefer lazy/cheap log construction on hot paths
- guard expensive `TRACE` formatting with level checks

### Incident workflow

- keep production/dev default at `INFO`
- during incident analysis, enable `TRACE` only for affected modules
- ~~prefer time-bounded overrides (TTL) so verbose logging auto-reverts~~
- once resolved, remove temporary overrides and keep only useful `INFO`/`WARNING`

### Local observability stack

The repo ships a local observability stack (`grafana/otel-lgtm`, bundling the OTel
Collector, Tempo, Prometheus, Loki, and Grafana, plus a standalone Jaeger) in
`docker-compose.otel.yml` for inspecting traces, metrics, logs, and the WAL timeline. It
is **opt-in and down by default** — the devcontainer starts
lightweight — because a dev tool needs observability only occasionally, and WAL events
are written to disk regardless and can be ingested retroactively (see
[ADR-0052](../../../finecode_internal_docs/adr/0052-observability-stack-opt-in-via-compose-profile.md)).

Capturing telemetry needs **two** things: the observability stack running, and
`FINECODE_OTLP_ENDPOINT` set so the WM/ERs export to it. The endpoint arms telemetry
**at WM startup** — it is read once when the process starts, so changing it takes effect
only on the next WM (IDE) restart. Once armed, the collector itself can be started and
stopped freely: a collector brought up after the WM is picked up automatically, because
exporters buffer and retry.

- **Persistent (enables telemetry)** — uncomment both `COMPOSE_PROFILES=otel` and
  `FINECODE_OTLP_ENDPOINT` in `.env` (see `.env.example`), then **rebuild/recreate** the
  devcontainer — a plain reopen is not enough. Compose only resolves `.env` into a
  container's environment when that container is *created*; if it already exists,
  reopening just reattaches to it with whatever environment it was created with, and
  `FINECODE_OTLP_ENDPOINT` stays empty with no error to point at it. Use the Dev
  Containers "Rebuild Container" command, or from the CLI:
  `devcontainer up --workspace-folder . --remove-existing-container`. Once the container
  is actually recreated, Compose reads the current `.env` and the WM starts with the
  endpoint armed.
- **On demand (manages the stack only)** — run `scripts/observability.sh {up,down,status}`
  on the host (the devcontainer does not mount the Docker socket). This brings the stack
  up or down but does **not** arm the endpoint. If the WM was already started with
  `FINECODE_OTLP_ENDPOINT` set, data flows as soon as the stack is up; if it was not,
  set the endpoint and restart the WM first — starting the stack alone records nothing.

Once telemetry is flowing: Grafana at `http://localhost:3000`, Jaeger at
`http://localhost:16686` (Jaeger's UI is the better view for FineCode traces).

If you change a var in `docker-compose.otel.yml`'s `environment:` block (e.g. to add
`ENABLE_LOGS_OTELCOL=true`, see below), the same rule applies: recreate that service, a
restart isn't enough —
`docker compose -f docker-compose.otel.yml up -d --force-recreate otel-lgtm`.

By default, `otel-lgtm`'s own startup script suppresses each bundled component's
stdout/stderr to `/dev/null` (no file-based fallback) unless that component's
`ENABLE_LOGS_<NAME>` env var (e.g. `ENABLE_LOGS_OTELCOL`, `ENABLE_LOGS_GRAFANA`) or the
blanket `ENABLE_LOGS_ALL` is `"true"`. An empty `docker compose logs otel-lgtm` doesn't
mean nothing is happening — it may just mean logging for that component was never
enabled. See [Troubleshooting](observability.md#troubleshooting-stack-is-up-but-no-data-appears-anywhere)
in the observability guide for the general env-var/container-recreate pitfall this
stack is also subject to.

## Dependency lock files

FineCode uses [pylock.toml](https://packaging.python.org/en/latest/specifications/pylock-toml/) lock files for reproducible dependency installation.

### Why lock files

Without lock files, `prepare-envs` resolves dependency versions from the ranges declared in `pyproject.toml` at install time. This means two developers (or CI runs) can end up with different versions depending on when they ran the command. Lock files pin exact versions for reproducible environments.

### Canonical lock strategy

FineCode standardizes on a single canonical lock file as the source of truth:

```text
pylock.toml
```

The canonical lock should encode the supported target matrix (environment, platform, interpreter, architecture) using PEP 751 semantics (for example, marker-based package selection), rather than splitting truth across many authoritative files.

The architecture decision is documented in ADR-0023.

### Generating lock files

Use the `lock_dependencies` action:

```bash
python -m finecode run --shared-server lock_dependencies \
    --src_artifact_def_path=pyproject.toml \
    --output_dir=.
```

For Python, prefer handlers that can operate on standardized pylock data directly. `uv` is currently the preferred backend where available.

### Installing from lock files

There are two lock-file handlers depending on the pipeline you use:

- **`PrepareEnvInstallDepsFromLockHandler`** — used in the per-environment `prepare_env` pipeline (the default). Reads `pylock.<env_name>.toml` and passes pinned versions to `install_deps_in_env` for that single env.
- **`PrepareEnvsInstallDepsFromLockHandler`** — legacy multi-env variant that handles all environments in one handler. Use only if you are running a custom `prepare_envs` pipeline that does not go through `PrepareEnvsDispatchHandler`.

During migration, existing per-env lock handlers can continue to consume derived files such as `pylock.<env_name>.toml`. Long-term direction is canonical-first consumption with projection only when required for compatibility.

### Lock files in CI

Lock files should be committed to the repository. CI should install from them, not regenerate them:

```bash
# CI installs from existing lock files — reproducible
python -m finecode prepare-envs
```

To update lock files, run `lock_dependencies` locally or in a scheduled CI job and commit the result. For multi-platform projects, use a CI matrix to generate lock files on each target platform.

## `requires-python`: no upper bound

FineCode's own packages declare a **lower bound only** on `requires-python` (e.g. `>=3.11`), never an upper bound (`< 3.15`, `<= 3.14`).

An upper bound is a packaging anti-pattern for published packages: a resolver that cannot satisfy the cap **backtracks to an older release** of the package rather than failing cleanly, so a consumer on a newer Python silently gets a stale version instead of a clear "not supported yet" error. See [ADR-0053](../../../finecode_internal_docs/adr/0053-derived-interpreter-axis-is-materialized-into-config.md) for the full rationale.

The cap also has no remaining job now that the interpreter matrix exists. The set of Python versions an action is tested against is **derived from `requires-python` and bounded by what the provisioning toolchain (uv) can actually obtain** (ADR-0053, part 5), not by a hand-written ceiling. So removing the upper bound does not widen the test matrix to unreleased versions — the obtainable-versions ceiling does that job, on the developer's clock and in a reviewable diff.

If a genuinely newer Python breaks a package, fix it when that version exists — do not pre-emptively cap. New packages must follow this: declare `requires-python = ">=<min>"` with no upper component.

## JSON-RPC key naming convention

All JSON-RPC channels in FineCode use **camelCase** for message keys:

| Channel | Convention | Reason |
| --- | --- | --- |
| WM server ↔ any client (internal TCP) | **camelCase** | Standard for JSON-based protocols; language-agnostic (clients may be written in Go, TypeScript, Rust, etc.) |
| LSP command handlers → IDE | **camelCase** | Same convention; no conversion needed |
| ER ↔ WM (pygls custom commands) | **camelCase** | Consistent with WM protocol |

### Rule: write keys explicitly, no auto-conversion

Handler return dicts must use camelCase keys **written explicitly**. There is no automatic snake_case → camelCase conversion in the WM server. Auto-conversion is fragile — it was the root cause of the `return_code` bug in `_handle_run_action` where only the inner value was wrapped in `_NoConvert` but the outer keys were still silently converted.

```python
# correct — keys written as camelCase explicitly
return {"returnCode": result.return_code, "resultByFormat": result.result_by_format}

# wrong — snake_case keys in a JSON response
return {"return_code": result.return_code, "result_by_format": result.result_by_format}
```

Python **internal** data structures (dataclass fields, local variables, function parameters) stay snake_case per Python convention. Only the dict keys that cross a JSON-RPC boundary are camelCase.

### What this means per layer

**WM server handlers** (`wm_server.py`): return dicts with camelCase keys directly. No `_NoConvert` wrapper, no `_convert_to_camel_case` call.

**`wm_client.py`**: accesses response keys in camelCase.

**Python CLI clients** (`prepare_envs_cmd.py`, `run_cmd.py`): access camelCase keys from responses.

**LSP command handlers** (`lsp_server/endpoints/`): pass WM responses through to the IDE as-is — no conversion needed since the WM already produces camelCase.

**ER response dicts** (`finecode_extension_runner`): use camelCase keys (`returnCode`, `resultByFormat`, `status`).

## Ambient state: when a `ContextVar` is allowed

Some values belong to "the work currently being done" rather than to any one function: which run is executing, which client asked for it, which trace it belongs to. A `contextvars.ContextVar` makes such a value readable anywhere inside a task without every function in between carrying it.

That convenience is also the cost. A value passed as a parameter is visible in the signature, checked by the type checker, and impossible to forget at a call site without a failure. An ambient value is none of those: a path that forgets to set it reads the default, and the resulting bug is a *silent wrong answer* — a question routed to nobody, a span attached to no trace — rather than a crash.

### Rule: pass explicitly unless a structural fact prevents it

Reach for a `ContextVar` only when explicit passing is blocked by something about the code that you can name. "It would touch a lot of signatures" is not such a fact; it is a cost, and usually the right one to pay. Record the blocker in the module docstring, so the next person can tell whether it still holds.

Blockers that qualify, with the examples in this repo:

- **The consumer is an object built before the value exists, and reused after it changes.** ER services are registered once per configuration with `register_instance` (`di/bootstrap.py`), and handler instances are cached in `RunnerContext.action_cache_by_name` and reused across runs. A per-run value therefore cannot reach a handler through the service it holds. This is why the current run id is ambient (`run_context.current_run_id()`), read by `UserPrompt` and the action-runner impls at the moment they call the WM.
- **The alternative is a public API change that moves responsibility onto extension authors.** Putting `run_id` in `IUserPrompt.ask_choice` would make every handler responsible for naming its own run correctly, and a stale value there misroutes a question silently.

Blockers that do **not** qualify:

- *Many intermediate signatures would change.* Prefer one object that carries the values a dispatch needs over a long parameter list — and prefer either to ambient state. The WM's elicitation origin was ambient for this reason alone, which was not good enough: it is now `elicitation_bridge.RunDispatchOrigin`, an explicit dispatch descriptor constructed at the request handler (or, for a nested ER→WM→ER dispatch, derived from the calling run's connection) and threaded down to `in_flight_runs.track`, the one choke point every dispatch passes through.
- *Tasks would have to be passed the value.* `asyncio.create_task` copying the current context is a convenience, not a justification: a coroutine can take a parameter.

### Rule: a parameter replacing ambient state gets no default

The reason to prefer a parameter is that it cannot be forgotten at a call site without a failure. A default takes that back: the argument can be dropped at any one hop of a long chain, it still type-checks, and the result is the same silent wrong answer the `ContextVar` produced. This is not hypothetical — the two `progressToken` request handlers in `_streaming.py` were left dispatching without an origin exactly this way, so a connected client asking for a run with live progress was told nobody could be asked.

So `origin: RunDispatchOrigin | None` has no default at any hop, from `bind_run` up to the request handlers. `None` stays a legal *value* — it is the honest answer for a run the WM started on its own behalf, or one whose handler never held a connection — but it has to be written down, next to a comment saying which of those it is. A required parameter whose value is sometimes "nobody" is cheap; a default that silently means "nobody" is the bug.

### Rule: ambient inside a process, explicit on the wire

A `ContextVar` never crosses a process boundary. Anything the other side needs is a field in the message, written explicitly like every other key. The WM→ER dispatch carries `runId` and `traceparent` as options for exactly this reason, and the ER re-establishes both as ambient state on its own side after reading them.

### Rule: set it at a choke point, not at each call site

Bind an ambient value in the one place every path already passes through, so a new path cannot forget it. The run→client binding for elicitation lives inside `in_flight_runs.track()`, which every dispatch already enters, rather than beside each dispatch. Setting it per call site is how ambient state rots: the sites multiply, one of them omits it, and nothing fails.

### Rule: read it at the edge, never store it

Read the value where it is used and let it go. Copying it into a long-lived object (a service field, a module global, an attribute on a cached handler) reintroduces the sharing that ambient state exists to avoid: two runs executing concurrently in the same env would overwrite each other's value, where a `ContextVar` gives each task its own. A module-level global is never an acceptable substitute for a `ContextVar` — it is the same invisibility plus a race.

## Async generator handlers

A handler's `run()` method can be either a regular coroutine (returns a result) or an **async generator** (yields one or more partial results). The framework detects which one it is at call time using `inspect.isasyncgen()`.

### When to use an async generator

Use `yield` when your handler produces results incrementally — especially when the caller should receive data before the handler finishes:

- Processing a collection and sending per-item results (see `LintHandler` — `presets/fine_lint/fine_lint/lint_handler.py`)
- Long-running handlers (servers, watchers) that should emit an initial result (address, port, status) before entering a blocking loop

### How it works

Each `yield`ed value is treated as a partial result. The framework:
1. Sends it to the LSP/MCP client immediately (if a `partial_result_token` was supplied by the client)
2. Forwards it to a parent handler's `run_action_iter()` loop (if called as a sub-action)
3. Accumulates all yielded values using the result type's `update()` method

The final accumulated result becomes the action's return value. If no value is accumulated (generator yields nothing), the result is `None`.

### Pattern: yield before blocking

For handlers that start a server or watcher and then block indefinitely, yield the result as soon as the resource is ready, then enter the blocking loop:

```python
async def run(self, payload, run_context):
    server = _start_server(payload.host, payload.port)
    bound_host, bound_port = server.server_address

    # Yield immediately — callers get address/port without waiting for cancellation
    yield MyRunResult(base_url=f"http://{bound_host}:{bound_port}", ...)

    async with run_context.progress("Serving", cancellable=True) as prog:
        await prog.report(message=f"http://{bound_host}:{bound_port}")
        try:
            while True:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
    # generator exhausts here; cleanup in finally block
```

Without the `yield`, the caller would only receive the result after the action is cancelled — never during normal operation.

### Canonical examples

- `ServeWalExplorerFromStoreHandler` (`extensions/fine_wal_explorer/`) — yield-before-blocking pattern
- `LintHandler` (`presets/fine_lint/fine_lint/lint_handler.py`) — iterates a sub-action with `run_action_iter` and re-yields each partial

## Partial result internals

Understanding how partial results are forwarded is useful when debugging why a caller does (or does not) receive incremental data.

### Two forward paths

When a handler yields a partial result, the framework forwards it via one or both paths depending on how the action was invoked:

| Path | Set when | Transport |
| --- | --- | --- |
| `partial_result_token` | Client sent a token with the request | `partial_result_sender.schedule_sending()` → WM notification → LSP/MCP client |
| `partial_result_queue` | Parent handler called `run_action_iter()` | `asyncio.Queue.put()` → parent's `async for` loop |

Both checks happen in the same place in `execute_action_handler` (`finecode_extension_runner/_services/run_action.py`). A comment there notes the future opportunity to unify them into a single `PartialResultForwarder` abstraction.

### Sub-action partial results

Calling `run_action(sub_action, ...)` discards all intermediate yields — only the final accumulated result is returned. To receive intermediate yields from a sub-action, use `run_action_iter(sub_action, ...)` instead. The queue path above is what makes this work.

### MCP real-time streaming

The MCP server (`src/finecode/mcp_server.py`) forwards **both** partial results and progress notifications as real-time `send_log_message` calls to the AI client. This means both mechanisms surface to the user immediately — there is no buffering at the MCP layer.

## Referencing ADRs in source code

When code implements a non-obvious constraint or design choice, add a comment referencing the relevant ADR. This prevents future contributors from accidentally "fixing" something that was intentionally designed that way.

```python
# Single shared IO thread services all active ERs — see docs/adr/0003-*.md
_io_thread = threading.Thread(target=_service_loop, daemon=True)
```

**When to add an ADR reference:**

- The implementation looks like it could be simplified but cannot be
- There is a temptation to refactor in a way that would violate the decision
- The constraint is not derivable from the code itself

**When not to add one:**

- The code is self-explanatory
- The ADR covers a broad design area — reference it only at the specific site that enforces the decision, not everywhere related code appears

ADR references differ from user-doc references: user docs explain the *API surface* for consumers; ADRs explain *why a constraint exists* for contributors.

## Comments

A comment must carry information that is not recoverable from the code it annotates. Before writing one, ask: *could a competent reader derive this by reading the next five lines?* If yes, delete it.

What earns a comment:

- **Why, not what** — the constraint, tradeoff, or rejected alternative
- Non-obvious external behavior (an API that lies, a protocol quirk, an ordering requirement imposed from outside)
- A deliberate deviation from the obvious implementation, with the reason
- An ADR reference at the site that enforces the decision (see above)

Never write:

- Restatements: `# increment the counter`, `# loop over the files`
- Section banners: `# --- Helpers ---`, `# Main logic`
- Change narration: `# now also handles X`, `# previously this used Y`, `# updated to support Z`. The comment describes the code as it is; the diff and commit message carry the history.
- Docstring padding that repeats the signature — parameter lists that add nothing to the type annotations, `Returns: the result`
- Tutorial voice: `# Note that`, `# Important:`, `# As you can see`, `# This is a common pattern`
- Comments on self-naming code — a comment above `def stop_runner` saying `# stops the runner`
- Type restatements the annotation already makes: `# a list of paths`

Prefer no comment over a weak one. Renaming a variable or extracting a function is usually the better fix — a comment that exists to explain an unclear name is a bug report against the name.

Match the density of the surrounding module. A file with three comments in 200 lines is not under-documented; adding twelve to a new function in that file makes it look foreign.

## Generality in comments, docstrings, and messages

Code outlives the bug or feature that motivated it. A comment, docstring, or exception/log message that names the specific tool, ticket, or scenario that prompted the change becomes misleading once that scenario stops being the only — or even the main — case the code handles.

Write for the *mechanism*: what the code does and why, in terms of its general contract — not the specific incident that led to writing it.

```python
# wrong — ties a general-purpose exception to the one tool that happened to
# motivate it; misleading the moment a different handler raises it too
class ActionCancelledError(ActionError):
    """Raised when pyrefly cancels a hover request due to a concurrent
    file open."""

# correct — describes the general contract; any handler, for any reason,
# can trigger this
class ActionCancelledError(ActionError):
    """Action execution was cancelled rather than failing — either a
    downstream dependency the handler relies on cancelled an in-flight
    operation, or the handler itself decided to abort. Not an error."""
```

**Exception**: naming a specific tool or protocol detail is fine when the code is *permanently and structurally* scoped to that tool — e.g. a module that only ever deals with LSP servers may legitimately say "e.g. pyrefly, like rust-analyzer" to explain a real, general behavior shared by a class of LSP servers. The test: would this sentence still be true and useful if the code were reused for an unrelated cause tomorrow? If the code is general-purpose (multiple causes, multiple callers), its docstring must be too — push the concrete example down to the narrowest type/module that is actually specific to it.

This applies equally to inline comments and to exception/log message text, not just docstrings. It is the mirror image of "Referencing ADRs in source code" above: reference *design decisions* that explain a non-obvious constraint; do not reference the *motivating bug or task* that led you to write the code.

## Docstrings

### Format

Write a prose summary, then add a `Raises:` section when the function can raise. Omit `Args:` and `Returns:` — type annotations already carry that information; repeating it in prose adds maintenance cost without value.

```python
async def get_project_raw_config(project_def_path: pathlib.Path) -> dict[str, Any]:
    """Return the raw TOML config for the given project.

    Raises:
        ActionFailedException: WM did not respond within 10s.
    """
```

Use Google-style formatting when sections are present (indented `Key: description` under a section header). This format is parsed by [`griffe`](https://mkdocstrings.github.io/griffe/), which means a future static analysis tool can consume it without writing a parser from scratch.

### Where to add docstrings

Add docstrings at **architectural boundaries** — not everywhere:

- Interface/Protocol methods (`finecode_extension_api/interfaces/`)
- Functions that cross process or network boundaries (WM calls, subprocess calls)
- Public API methods (`ApiClient`, `WmClient`)

Do not add docstrings to internal helpers where the name and signature are self-explanatory, or to simple delegating methods that add no behavior.

### Documenting exceptions

List every exception that can propagate to the caller — both intentional and unhandled leaks.

**Intentional**: translated at the boundary before reaching the caller.

```python
"""
Raises:
    ActionFailedException: WM did not respond within 10s.
"""
```

**Unhandled leak**: an exception from a lower layer that is not yet caught and translated. Mark it with `[untranslated]` so it is visible as a gap and machine-readable by future tooling:

```python
"""
Raises:
    ActionFailedException: WM did not respond within 10s.
    JsonRpcError: WM returned an error response. [untranslated]
"""
```

### Interfaces vs. implementations

**Protocol/interface** methods document the **intended contract**: what every correct implementation must satisfy. Only list exceptions that all implementations are expected to raise.

**Implementation** methods document **actual behavior**, including any exceptions not declared on the interface (mark those `[untranslated]`). A mismatch between interface and implementation is a gap to fix.

## Code Style

### Typing

- type the code
-- use complete types, no holes in generics like `list` instead of `list[int]`

**This rule loses to the surrounding code unless you make it win.** Neither check
that would catch a violation is switched on today, and the codebase has **589
bare-generic annotation sites across 74 files** in `src/` and
`finecode_extension_runner/src/` (measured 2026-08-22 with pyrefly's
`implicit-any-type-argument`, see `presets/fine_python_lint/LINT_COVERAGE.md`).
Some of them sit on the exact call chain you are extending. They are legacy, not
the convention: matching a violating neighbour is how the count got that high.
Write the complete type and leave the neighbour alone.

#### `Any` is honest for an open value, a hole for a known shape

Both are spelled `typing.Any`, and the difference is not stylistic — it is
whether the type is *unknown at runtime* or merely *unwritten*.

```python
# correct — a payload field's value is whatever the user typed; the type is
# genuinely open, and narrowing happens by isinstance at the point of use
def absolutize_payload(payload: dict[str, typing.Any]) -> dict[str, typing.Any]: ...

# wrong — a payload *schema* fragment has a closed, documented shape, produced by
# our own `schema_utils.extract_payload_schema`. `Any` here is a shape nobody
# wrote down, and it spreads: every `.get()` off it returns `Any` too
def coerce_raw_value(raw: str, field_schema: dict) -> typing.Any: ...

# correct — the shape says what it holds, and `.get("type")` now narrows to
# `str | None` instead of `Any`
class FieldSchema(typing.TypedDict, total=False):
    type: str
    format: str
    ...

def coerce_raw_value(raw: str, field_schema: FieldSchema) -> JsonValue: ...
```

The test is: **can you write the shape down?** If the answer is yes and you
reached for `Any` anyway, that is the hole. A `TypedDict` is usually the cheapest
way to write it — especially for JSON-ish data whose producer is in this repo.

Two consequences worth knowing:

- **`Any` is contagious.** One `Any`-typed parameter silently turns every
  expression derived from it into `Any`, so the hole is never confined to the
  annotation that introduced it.
- **`Any` as a *value* is not a hole.** `converter.structure(item, typing.Any)`
  passes `Any` as a runtime argument to a library that expects a type object.
  That is correct usage and this rule does not apply to it.

#### Sentinels must not erase the return type

A `object()` sentinel has no type to narrow against, so every function returning
one is forced to `-> typing.Any`, and callers end up comparing against a private
module member to find out what they got.

```python
# wrong — the return type is `Any`, and the caller reaches across a module
# boundary for a private name to interpret it
_NO_OPINION = object()

def coerce_raw_value(raw: str, schema: FieldSchema) -> typing.Any: ...
...
if coerced is payload_uris._NO_OPINION:   # private access at every call site

# correct — no sentinel at all: the caller already knows the "no opinion" answer,
# so pass it in and let the function return it
def coerce_raw_value(raw: str, schema: FieldSchema, fallback: JsonValue) -> JsonValue: ...
```

When a sentinel is genuinely unavoidable (the fallback is expensive, or "absent"
must be distinguishable from a legitimate `None`), use a one-member `enum` rather
than `object()` — it is narrowable via `typing.Literal` and keeps the return type
concrete:

```python
class _Unset(enum.Enum):
    TOKEN = enum.auto()

def lookup(key: str) -> JsonValue | typing.Literal[_Unset.TOKEN]: ...
```

This is the same reasoning as [Closed sets of values](#closed-sets-of-values):
a value drawn from a fixed set gets an enum, and "present or absent" is a fixed
set of two.

#### A hole in a generic is not only a style problem

`list` and `list[str]` are different at runtime to any code that introspects the
annotation — and this repo has such code. `cattrs` structure-hook factories read
`cls.__args__`, which bare `list` does not have, so a payload field annotated
`files: list` raises `AttributeError` from inside the converter instead of
producing a validation error. Anything reachable by
`typing.get_type_hints` — payload dataclasses, handler configs, service
configs — is introspected somewhere, so a hole there is a latent crash, not a
missing annotation.

When you must handle a possibly-bare generic in such code, read it with
`typing.get_args(cls)`, which returns `()` for bare `list`, rather than
`cls.__args__`, which raises.

### Imports

Keep all imports at the top of the module, at module (root) level. Do not use local imports inside functions or methods.

Exceptions — local imports are acceptable only when:

- avoiding a circular dependency (usually a signal of a structural problem — prefer fixing the structure)
- deferring an expensive module load to speed up startup (e.g. CLI: don't import all command handlers when only one is invoked)

This rule is enforced by ruff rule `PLC0415` (`import-outside-toplevel`).

### Fallbacks

Do not add fallbacks by default. A fallback — `dict.get(key, default)`, `getattr(obj, attr, default)`, a `try/except` that swallows or substitutes, an `or default_value` expression — hides the fact that something is missing or broken.

Use a fallback only when the absent or error case is **genuinely expected and has defined behavior**:

```python
# correct — absence is expected; the caller checks for None
timeout = config.get("timeout")

# correct — a meaningful operational default that is part of the contract
level = config.get("log_level", "INFO")

# wrong — masks a missing key that must always be present; failure is silent
name = config.get("project_name", "unknown")
```

The same applies to `try/except`: only catch an exception if you have a specific recovery action. A bare `except Exception: pass` or `except Exception: return None` is almost always wrong — it turns a loud failure into a silent one.
  
### Exports

- explicitly export public module members using `__all__`
-- it may not contain dynamic elements, only literal strings

### Exception naming

Name exceptions from the **caller's perspective** — what observable thing went wrong — not from the implementation's perspective.

```python
# correct — describes the outcome the caller experiences
class ProjectInfoUnavailableError(Exception): ...

# wrong — leaks that the implementation talks to a WM over a specific protocol
class WmCommunicationError(Exception): ...
```

Use the `Error` suffix (Python standard library convention: `ValueError`, `TimeoutError`, etc.).

Define exceptions **alongside the interface or layer they belong to**, not inside the implementation. An interface-level exception must not reference implementation details in its name or message template.

### Boolean flags

Boolean parameters must be keyword-only (`*`). A positional bool is unreadable at the call site — it's not obvious what it's toggling without checking the signature.

```python
# correct
async def remove_dir(self, dir_path: Path, *, tolerant: bool = False) -> None: ...
# call site is self-documenting: remove_dir(path, tolerant=True)

# wrong — allows remove_dir(path, True), meaningless without checking the signature
async def remove_dir(self, dir_path: Path, tolerant: bool = False) -> None: ...
```

### Closed sets of values

A parameter, field, or constant whose value comes from a **fixed, known set** must be an enum, not a bare `int` or `str` — even when the set is defined by an external protocol and even when the value never leaves the module.

```python
# correct — the call site says what it means
class _FileChangeType(enum.IntEnum):
    """`FileChangeType` of ``workspace/didChangeWatchedFiles``."""

    CREATED = 1
    CHANGED = 2
    DELETED = 3

await self._send_watched_file_change(uri, _FileChangeType.DELETED)

# wrong — the reader has to know the LSP spec by heart to review this line
await self._send_watched_file_change(uri, 3)
```

Notes:

- **`IntEnum`/`StrEnum` when the value is a wire code**, so it serializes as the protocol expects; plain `enum.Enum` when the value is ours alone.
- **The set is the spec's, not ours.** Define every member the protocol defines, including ones no current call site emits — an enum that mirrors only today's usage stops being a description of the protocol and turns into a second thing to keep in sync.
- **Private when it is internal.** An enum used by one module is module-private (`_FileChangeType`); an enum in a signature callers depend on belongs to the interface and is exported.
- **Do not reach for `lsprotocol` to get one.** Its types are confined to the LSP server subsystem (`wm-use-runner-client`) and `finecode_extension_api` does not depend on it. Declare the enum locally.
- **Test-support code is not exempt.** `finecode_extension_runner.testing` is public API — extension authors assert against it, so a stringly-typed recorder shows up in their tests too.

For enums on data crossing the LSP boundary, the conversion pattern is in `docs/guides/implementing-lsp-features.md` → "LSP ↔ FineCode conversion utilities": the FineCode side holds the enum, and `int()`/`Enum(...)` conversion happens at the boundary.

### Tuples as records

A tuple that is stored or returned as a **named part of an API** gets a `typing.NamedTuple`. A bare `tuple[...]` annotation documents the element types, not what the elements mean, so every use site ends up reading positionally and every reader has to reconstruct the shape from the code that produced it.

```python
# correct — the shape names itself
class FileOperation(typing.NamedTuple):
    kind: FileOperationKind
    paths: tuple[pathlib.Path, ...]

operations: list[FileOperation]

# wrong — what is the str? what are the paths, and how many are there?
operations: list[tuple[str, tuple[pathlib.Path, ...]]]
```

A plain tuple is still right for a pair that is unpacked where it is produced (`for name, value in mapping.items():`) and for genuinely positional data (a coordinate, a start/end range). Reach for a `NamedTuple` when the elements mean different things **and** the tuple outlives the expression that built it.

Notes:

- **`NamedTuple`, not a dataclass, when callers already compare against plain tuples.** A `NamedTuple` compares equal to the tuple of its fields, so the typed shape can be introduced without breaking existing assertions; a dataclass breaks all of them.
- **Variadic arity is a contract, not a detail.** If the length varies by case — one path for a create, two for a rename — say so in the field docstring. A `NamedTuple` gives you somewhere to write that down; `tuple[Path, ...]` does not.

### Layered exception translation

Each architectural layer defines its own exception vocabulary. When a layer calls into a lower layer, it is responsible for catching lower-layer exceptions and re-raising them as its own layer's exceptions before they cross the boundary upward.

```
er_server.py (WM communication layer)
    raises WmCommunicationError

ProjectInfoProvider (IProjectInfoProvider implementation)
    catches WmCommunicationError
    raises ProjectInfoUnavailableError

Handler (action layer)
    catches ProjectInfoUnavailableError
    — never sees WmCommunicationError
```

A lower-layer exception that escapes upward without translation is a gap — document it with `[untranslated]` in the `Raises:` section and fix it.

This rule applies in both directions of the naming principle: the implementation layer knows its own internals (`WmCommunicationError` is appropriate there), while the interface layer must not expose them (`ProjectInfoUnavailableError` hides the WM detail).
