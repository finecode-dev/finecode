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
extensions/                        # Extension packages (ruff, flake8, mypy, ...)
presets/                           # Preset packages (recommended, lint, format)
finecode_dev_common_preset/        # Preset used for developing FineCode itself
tests/                             # Test suite
```

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

## Running checks

```bash
python -m finecode run lint
python -m finecode run check_formatting
pytest tests/
```

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
python -m finecode run lock_dependencies \
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
