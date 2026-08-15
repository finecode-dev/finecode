# Services

Services are long-lived dependencies that handlers (and other services) can request via dependency injection. This page lists the services that ship in this repo and where they are registered. Availability depends on whether the Extension Runner provides the service, a preset declares it, or an extension activates it.

For the normative rules on *designing* a service — interface shape, provisioning, registration, and the init-action vs. service-config dividing line — see [Designing Services Rules](../guides/designing-services.md).

## Core services (always available)

These services are registered by the Extension Runner at startup and are available in every handler without extra configuration.

| Interface | Default implementation | Notes |
| --- | --- | --- |
| `finecode_extension_api.interfaces.ilogger.ILogger` | `loguru.logger` via `finecode_extension_runner.impls.loguru_logger.get_logger` | Logging (trace/debug/info/warn/error/exception). |
| `finecode_extension_api.interfaces.icommandrunner.ICommandRunner` | `finecode_extension_runner.impls.command_runner.CommandRunner` | Async and sync subprocess execution. |
| `finecode_extension_api.interfaces.ifilemanager.IFileManager` | `finecode_extension_runner.impls.file_manager.FileManager` | File system IO abstraction (read/write/list/create/delete). |
| `finecode_extension_api.interfaces.ifileeditor.IFileEditor` | `finecode_extension_runner.impls.file_editor.FileEditor` | Open-file tracking, change subscriptions, read/write with editor awareness. |
| `finecode_extension_api.interfaces.icache.ICache` | `finecode_extension_runner.impls.inmemory_cache.InMemoryCache` | In-memory, file-versioned cache. |
| `finecode_extension_api.interfaces.iprojectactionrunner.IProjectActionRunner` | `finecode_extension_runner.impls.project_action_runner.ProjectActionRunnerImpl` | Run an action at project scope, routing through WM so the correct env-runner is chosen. If all handlers are in the current environment, communication with WM is omitted. |
| `finecode_extension_api.interfaces.iworkspaceactionrunner.IWorkspaceActionRunner` | `finecode_extension_runner.impls.workspace_action_runner.WorkspaceActionRunnerImpl` | Fan-out an action across all workspace projects. |
| `finecode_extension_api.interfaces.irepositorycredentialsprovider.IRepositoryCredentialsProvider` | `finecode_extension_runner.impls.repository_credentials_provider.ConfigRepositoryCredentialsProvider` | Read-only repository credentials and registry list. Routed through `register_impl` like `ICommandRunner` (see below), rather than registered as an eager instance. |
| `finecode_extension_api.interfaces.iprojectinfoprovider.IProjectInfoProvider` | `finecode_extension_runner.impls.project_info_provider.ProjectInfoProvider` | Current project paths and raw config access. |
| `finecode_extension_api.interfaces.iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider` | `finecode_extension_runner.impls.extension_runner_info_provider.ExtensionRunnerInfoProvider` | Runtime env info (venv paths, cache dir). |
| `finecode_extension_api.interfaces.iworkspaceactionregistry.IWorkspaceActionRegistry` | `finecode_extension_runner.impls.workspace_action_registry.WorkspaceActionRegistryImpl` | Read-only view of every action and handler in the workspace, across all projects and envs. See [Reading the action registry](#reading-the-action-registry-with-iworkspaceactionregistry). |
| `finecode_extension_api.interfaces.iknowledgestore.IKnowledgeStore` | `finecode_extension_runner.impls.knowledge_store.KnowledgeStoreImpl` | Query the workspace's knowledge store, which the WM owns. See [Querying the knowledge store](#querying-the-knowledge-store-with-iknowledgestore). |

## Provisioning `IRepositoryCredentialsProvider`

The default implementation, `ConfigRepositoryCredentialsProvider`, is seeded from
`[[tool.finecode.service]]` config rather than by a runtime init action
(ADR-0068). Registry *definitions* are not secret and can sit in a preset or
project `pyproject.toml`. *Credentials* are service config like any other and
resolve through the standard [config-source chain](../configuration.md#where-configuration-lives);
they MUST come from a non-VCS source — environment variables or a personal
[`finecode-user.toml`](../configuration.md#finecode-usertoml) (ADR-0040), never
committed alongside the definitions. The example below uses `finecode-user.toml`;
the [environment-variable form](../configuration.md#service-config-environment-variables)
is equivalent and is the only one of the two that also works on a CI runner with
no writable home directory:

```toml
# pyproject.toml (or a preset's preset.toml) -- non-secret
[[tool.finecode.service]]
interface = "finecode_extension_api.interfaces.irepositorycredentialsprovider.IRepositoryCredentialsProvider"
source = "finecode_extension_runner.impls.repository_credentials_provider.ConfigRepositoryCredentialsProvider"
env = "dev_no_runtime"
config.repositories = [
    { name = "pypi", index_url = "https://pypi.org/simple/", upload_url = "https://upload.pypi.org/legacy/" },
    { name = "testpypi", index_url = "https://test.pypi.org/simple/", upload_url = "https://test.pypi.org/legacy/" },
]
```

```toml
# finecode-user.toml -- secret, gitignored (see ADR-0040)
# Service entries merge by `interface` across config layers, and `source`/`env`
# are optional -- so naming the interface and adding only
# `config.credentials_by_repository` layers credentials on top of the repositories
# declared above, without restating (or pinning) the implementation.
[[tool.finecode.service]]
interface = "finecode_extension_api.interfaces.irepositorycredentialsprovider.IRepositoryCredentialsProvider"
config.credentials_by_repository.testpypi = { username = "__token__", password = "pypi-..." }
```

Or, as an environment variable (no `finecode-user.toml` needed, and nothing written
to disk):

```bash
FINECODE_SERVICE_CONFIG_REPOSITORY_CREDENTIALS_PROVIDER__CREDENTIALS_BY_REPOSITORY__TESTPYPI__PASSWORD=pypi-...
```

`REPOSITORY_CREDENTIALS_PROVIDER` here is this service's override name, derived
from `IRepositoryCredentialsProvider` (see
[Service names](../configuration.md#service-names)). Names are always derived;
there is nothing to declare.

**Init action vs. service config.** The [`init_repository_provider`](actions.md#init_repository_provider)
action still exists, but only for *dynamic runtime seeding* — credentials
fetched or rotated mid-session, which a static config value cannot express. It
is not needed for the common case above. This is the ADR-0068 dividing line:
an init action earns its keep by connecting and validating (ADR-0038); a pure
config carrier with no I/O and nothing to validate is service config instead.

## Reading process output with `ICommandRunner`

A process spawned by `ICommandRunner.run()` offers its output two ways, and each
stream chooses independently.

**Buffered** — the default, and what most handlers want:

```python
process = await self.command_runner.run(cmd)
await process.wait_for_end()
output = process.get_output()
```

**Streamed** — for a long-lived child whose output must be read while it is still
running (a watch-mode tool, or a line-framed RPC protocol):

```python
process = await self.command_runner.run(cmd)
async for line in process.stdout_lines():
    ...  # lines arrive as the child writes them, newline stripped
```

Both streams are drained from the moment the process is spawned, whether or not
anyone reads them, so a child cannot block on an unread pipe. A stream
accumulates its output until someone subscribes to it; from then on it belongs to
the subscriber:

- lines produced before the subscription are **replayed first**, so subscribing
  late loses nothing;
- `get_output()` on a subscribed stream **raises** — the output is no longer being
  accumulated, and returning `""` would be indistinguishable from a child that
  printed nothing;
- only **one subscriber per stream** is supported; a second call raises;
- the streams are independent, so streaming stdout leaves `get_error_output()`
  working as usual — which is normally what you want for a failure message.

Streaming is available only on `IAsyncProcess`. `run_sync()` has no way to
interleave reads with anything else, so `ISyncProcess` is buffered only.

## Stopping a process with `ICommandRunner`

A handler that owns a long-lived child — an agent run, a watch-mode tool — has to
be able to end it, because its own timeout otherwise only stops the *waiting*:

```python
process = await self.command_runner.run(cmd, new_process_group=True)
...
if process.is_alive():
    process.terminate()          # SIGTERM, returns immediately
    with contextlib.suppress(TimeoutError):
        await process.wait_for_end(timeout=2.0)
if process.is_alive():
    process.kill()               # SIGKILL
```

Two things are easy to get wrong here:

- **Ask `is_alive()`, not `get_exit_code()`.** Commands are spawned through a
  shell, so the exit code belongs to the shell. A shell that forks rather than
  execs exits the moment it is signalled — reporting a returncode that reads
  exactly like a clean death — while the command it started, which may be
  ignoring that signal, keeps running. `is_alive()` reports on the process group
  when the process owns one, which is the question a teardown actually has.
- **`new_process_group=True` is what makes the signal reach the tree.** Without
  it the signal goes to the process alone, so anything the command spawned
  (an agent's tool calls) survives. It is off by default because it also detaches
  the command from the terminal's signals; a caller that never tears its process
  down would only lose the Ctrl-C that used to reach it.

The subprocess slot the process holds (ADR-0056) is released when the process
actually exits, not when the handler returns — which is why a teardown that stops
one rung early leaks a slot for the ER's lifetime.

## Caching with `ICache`

FineCode has no framework-level rebuilder: deciding whether a cached result lets a handler skip recomputation is the handler's own responsibility. For the rationale behind this design, see [Caching is the handler's responsibility](../theory/why-action-model.md#caching-is-the-handlers-responsibility). `ICache` is the service that makes it convenient.

The protocol is intentionally small and **file-versioned**:

```python
class ICache(Protocol):
    async def save_file_cache(
        self, file_path: Path, file_version: str, key: str, value: Any
    ) -> None: ...

    async def get_file_cache(self, file_path: Path, key: str) -> Any: ...


class CacheMissException(Exception):
    pass
```

- `key` namespaces the cached value so independent handlers (and independent computations within one handler) never collide. By convention a handler declares a `CACHE_KEY` class attribute.
- `file_version` is the version of the file the value was computed from — obtained from `IFileEditor` (`file_info.version`). The cache stores the value against that version.
- `get_file_cache` raises `CacheMissException` when there is no entry **or** when the file has changed since the entry was stored. A handler treats the miss as "do the real work".

The default implementation (`InMemoryCache`) compares the cached file version against the file's current version on every read, and on write discards a value whose source file already changed while the work was in flight — so a stale result is never served or stored.

Handler pattern (read-through cache, one file at a time):

```python
class Flake8LintFilesHandler(...):
    CACHE_KEY = "flake8"

    async def run_on_single_file(self, file_uri):
        file_path = resource_uri_to_path(file_uri)
        try:
            cached = await self.cache.get_file_cache(file_path, self.CACHE_KEY)
            return DiagnosticFilesRunResult(messages={file_uri: cached})
        except icache.CacheMissException:
            pass

        # read the current file version, compute the result, then cache it
        file_version = ...  # from IFileEditor
        messages = await self._lint(file_path)
        await self.cache.save_file_cache(
            file_path, file_version, self.CACHE_KEY, messages
        )
        return DiagnosticFilesRunResult(messages={file_uri: messages})
```

### `ICache` is the default, not the only option

The default `InMemoryCache` lives for the Extension Runner's lifetime, so cache hits span repeated invocations within one session (for example, an IDE relinting on every keystroke) but do not persist across runner restarts. Caching is a handler concern resolved through dependency injection, so you are free to go further:

- **Swap the `ICache` implementation.** Bind your own `ICache` via `[[tool.finecode.service]]` — for example a persistent, on-disk store that survives restarts, or a content-addressed cache. Every handler that injects `ICache` picks it up with no code change. A persistent built-in cache is a planned FineCode direction.
- **Use a different caching service entirely.** A handler is not required to use `ICache` at all. It may inject a caching service of its own design (with whatever key model and invalidation rules its work warrants), or rely on the underlying tool's native cache. The framework imposes no single caching mechanism.

## Reading the action registry with `IWorkspaceActionRegistry`

`IWorkspaceActionRegistry.list_actions()` returns every action in the workspace — across all projects and all envs — as a list of `ActionInfo`. An Extension Runner only knows the actions its *own* env executes, so this whole-workspace picture is the only way a handler can reason about actions it does not itself run. It is what powers tooling like the knowledge extractor and `which_handlers`.

```python
actions = await self.registry.list_actions()
for action in actions:
    for handler in action.handlers:
        ...
```

`ActionInfo` carries `name`, `source`, `canonical_source`, `scope`, `project`, `language`, `parent_action_source`, `file_loc`, and `handlers` (a list of `HandlerInfo`, each with `name`, `source`, `canonical_source`, `env`, and `file_loc`).

Two properties of this data decide whether your code is correct:

**`source` is an alias; `canonical_source` is the identity.** `source` is the string as written in the definition file. For handlers it is almost always a package-level re-export (`fine_python_ruff.RuffLintFilesHandler`), not the module the class is defined in (`fine_python_ruff.lint_handler.RuffLintFilesHandler`). The same class can be re-exported under several aliases, so **two different `source` values may name one real handler**. `canonical_source` is `cls.__module__ + "." + cls.__qualname__` as the runner that hosts the class resolved it, and it is the same string no matter which alias was used. Key by `canonical_source`; treat `source` as a display/config label.

**Fields are resolved lazily, so `canonical_source` may be `None`.** It is populated per env by that env's own runner, and it stays `None` until that runner has started, or permanently if the class cannot be imported there. A newly written action or handler that the running instance has not picked up yet is `None` for the same reason — it exists in source and in config, but no runner has resolved it.

The consequence is that one real class can be **resolved on some rows and `None` on others at the same instant**, because it is registered by many projects. So do not key each row on `row.canonical_source or row.source` — that splits one class into two identities, one canonical-keyed and one alias-keyed. Build an alias→canonical index across the whole list first, then key everything through it, under one rule: **an unresolved row must never overwrite a resolved mapping.** A single resolving runner anywhere then fixes the key everywhere, regardless of row order. (`fine_knowledge`'s `_index` in `providers/wm_registry.py` is a worked example.)

Registry rows are also **not deduplicated**: a PROJECT-scope action appears once per project that registers it, so expect many rows per logical action.

## Preset-provided services

These services are declared in presets in this repo. They are available when the preset is active, or when you copy the same `[[tool.finecode.service]]` entry into your own config.

| Interface | Implementation | Declared by |
| --- | --- | --- |
| `finecode_extension_api.interfaces.ijsonrpcclient.IJsonRpcClient` | `finecode_jsonrpc.jsonrpc_client.JsonRpcClientImpl` | `presets/fine_python_lint` |
| `finecode_extension_api.interfaces.ilspclient.ILspClient` | `finecode_extension_runner.impls.lsp_client.LspClientImpl` | `presets/fine_python_lint` (wraps `IJsonRpcClient`) |

## Extension-activated services

Extensions can register services via the `finecode.activator` entry point using `IServiceRegistry`. The following activators ship in this repo and register services when their packages are installed.

| Extension package | Interface | Implementation |
| --- | --- | --- |
| `finecode_httpclient` | `finecode_extension_api.interfaces.ihttpclient.IHttpClient` | `finecode_httpclient.client.HttpClient` |
| `fine_python_ast` | `fine_python_ast.iast_provider.IPythonSingleAstProvider` | `fine_python_ast.ast_provider.PythonSingleAstProvider` |
| `fine_python_mypy` | `fine_python_mypy.iast_provider.IMypySingleAstProvider` | `fine_python_mypy.ast_provider.MypySingleAstProvider` |
| `fine_python_package_info` | `fine_python_package_info.ipypackagelayoutinfoprovider.IPyPackageLayoutInfoProvider` | `fine_python_package_info.py_package_layout_info_provider.PyPackageLayoutInfoProvider` |
| `fine_python_package_info` | `finecode_extension_api.interfaces.isrcartifactfileclassifier.ISrcArtifactFileClassifier` | `fine_python_package_info.py_src_artifact_file_classifier.PySrcArtifactFileClassifier` |
| `fine_python_ruff` | `fine_python_ruff.ruff_lsp_service.RuffLspService` | `fine_python_ruff.ruff_lsp_service.RuffLspService` |
| `fine_python_pyrefly` | `fine_python_pyrefly.pyrefly_lsp_service.PyreflyLspService` | `fine_python_pyrefly.pyrefly_lsp_service.PyreflyLspService` |

## Service registry for extensions

Extension activators receive an `IServiceRegistry` instance (not injected into handlers) and call `register_impl()` to bind interfaces to implementations. See `finecode_extension_api.interfaces.iserviceregistry.IServiceRegistry` for the protocol and the activators above for concrete examples.

## How services are registered and resolved

A service binding is stored in one of two ways inside the Extension Runner's DI registry:

- **As a ready instance** — the [core services](#core-services-always-available) are constructed at runner bootstrap and registered as instances.
- **As a factory** — every binding created through `IServiceRegistry.register_impl()` (extension activators) and every `[[tool.finecode.service]]` declaration registers a *factory* keyed by interface. The implementation is constructed lazily on first injection, then cached as a singleton (and `Service.init()` runs at that point).

When a handler requests an interface, the registry returns a cached instance if one exists, otherwise it invokes the factory. Instances therefore take priority over factories, so the core services registered as instances (logger, command runner, file manager, etc.) are fixed and cannot be rebound by activators or config.

### One binding per interface

An interface resolves to exactly one implementation instance per Extension Runner: the factory runs once and the result is cached for the runner's life. Two concurrent instances of one interface are not supported, by design — see [ADR-0070](../../../finecode_internal_docs/adr/0070-one-binding-per-service-interface-addressed-by-derived-name.md) and rule [S-100](../guides/designing-services.md#s-100-one-binding-per-interface--model-plurality-as-types-or-as-a-keyed-collection) for what to do instead when you need several like-shaped things.

One consequence is worth stating plainly: **`register_impl`'s `singleton` parameter does not control lifetime.** Every resolved service is a singleton regardless. The concrete type is always alias-bound to the interface's instance, so a handler injecting the implementation class and a handler injecting the interface share one object (rule S-304) — `singleton` is accepted for compatibility but decides nothing.

A service is dropped from the cache when it is disposed (its last using handler shut down), so the next request rebuilds it from the still-registered factory rather than receiving a disposed object.

### Precedence — what overrides what

Bindings stored as factories are keyed by interface, and **the last registration for an interface wins**. Registration happens in this order at runner startup:

1. Core services (Extension Runner bootstrap)
2. Extension activators (`finecode.activator` entry points)
3. `[[tool.finecode.service]]` declarations (merged from presets, then the project's `pyproject.toml` on top)

So a `[[tool.finecode.service]]` declaration overrides an activator-registered default for the same interface. To replace the default `IHttpClient` implementation, for example, declare the same `interface` with your own `source` in `pyproject.toml` — it is applied last and wins.

### Activation phases

Extension activators run in two phases.

**Eager activation** happens at runner startup. The runner seeds from the handler packages active in the current env, walks their declared dependency graph, and immediately activates every reachable package that exposes a `finecode.activator` entry point. Handler package activators (which register their own LSP services, AST providers, etc.) are activated here.

**Deferred activation** handles the rest. All installed packages that expose a `finecode.activator` entry point but were not reached by the eager walk are queued in alphabetical order. When a handler requests a service interface for which no factory is registered yet, the runner tries these deferred activators one by one in alphabetical order, stopping as soon as the interface is registered. Each deferred activator fires at most once. This is how service-only packages such as `finecode_httpclient` are activated: they have no handlers (so they are never a seed or a reachable dep), but their activator runs the first time `IHttpClient` is requested.

The full precedence — last registration for a given interface wins within a phase, and later phases cannot override earlier ones because deferred activators only fire on a miss:

1. **Core services** (registered as instances at startup — cannot be rebound)
2. **Eager activators** (registered as factories at startup)
3. **`[[tool.finecode.service]]` declarations** (applied last at startup, overrides eager activators)
4. **Deferred activators** (fired on first request, only when no factory exists — cannot override the above)

### Where to register a reusable service

For a service whose interface lives in `finecode_extension_api` and whose implementation is a separate, replaceable package (for example `IHttpClient`/`finecode_httpclient`):

- Ship the **default binding** in the implementation package's own activator. It will be picked up via deferred activation whenever the interface is first requested, with no dependency coupling between consumer packages and the implementation package.
- Reserve `[[tool.finecode.service]]` with a `source` for **overrides** — swapping in an alternative implementation where being explicit is the point. Such a declaration is applied at startup (phase 3) and therefore prevents the deferred activator from firing at all.

Configuring the service is *not* an override. Config attaches to the binding without restating it — a TOML entry may carry `interface` and `config` alone, and an env-var override needs no TOML at all. See [configuring a service you did not declare](../configuration.md#configuring-a-service-you-did-not-declare).

For a service whose interface and implementation are owned by the same extension (the AST providers above), register it directly in that extension's activator; it will be activated eagerly when the extension is active.

## Querying the knowledge store with `IKnowledgeStore`

The knowledge store — the extracted facts about a workspace, plus everything
derived from them — is owned by the **Workspace Manager**, not by whichever
Extension Runner happens to want to read it. A handler that hosts rule code does
not open the store, connect to a backend, or hold credentials. It sends the WM a
query and gets rows plus a freshness verdict back.

Three things follow from that, and the third is why it is not merely tidier:

- **One access path.** Revision pinning and consistency are centralized in the
  owner rather than reimplemented per runner.
- **The WM sees what each query read**, so it can tell which stored facts an
  answer depended on. That is what makes memoized answers possible at all.
- **Derived relations are answerable.** A derived fact is not *in* the store —
  it is computed on demand where the derivation lives. A direct connection to a
  storage backend could serve only base facts and would silently miss half the
  model.

You normally do not build the query payloads yourself. The knowledge engine
adapts this service into an ordinary query backend:

```python
from finecode_knowledge import query as q
from finecode_knowledge.query.remote import RemoteBackend

backend = RemoteBackend(self.knowledge_store)      # IKnowledgeStore, injected
result = await my_rule.violations(backend)
for violation in result.rows:
    ...
if not result.freshness.verified:
    for reservation in result.freshness.reservations:
        ...                                        # which input, and why
```

Because every query terminal is `async`, the same rule body runs unchanged
against an in-process `InterpreterBackend` — the pip-only, no-WM case — and
against `RemoteBackend`. Which side executes is a placement decision, not a
change to the rule.

**Rendering a whole entity.** A query asks about fields it can name. Code that
renders *everything* known about an entity — a projection, a hover card, an
inspector — cannot name them, because other extensions may declare fields on
entity types they did not define. For that, both backends offer `records`:

```python
records = await backend.records([some_ref, another_ref])
for record in records:
    for field_name, field_value in record.fields.items():
        ...                                     # value, provenance, band
    for conflict in record.conflicts:
        ...                                     # providers disagree here
```

Pass every reference you need in one call: the reply is positional and one
message covers all of them, so the cost does not grow with how much you are
rendering. Do not reach past these two methods for a fact store of your own —
a read the WM never sees contributes nothing to what it knows an answer depended
on, so nothing built on it can be invalidated when the workspace changes.

**Register the schema first.** The WM must not import the package that declares
your schema, so the schema travels as data: call
`register_schema(registry_to_json(MY_SCHEMA))` once, before the first query.
Querying before that fails with an error naming the missing step.

**Read the verdict.** Every result carries a `Freshness`. An empty
`reservations` tuple means the answer's inputs were all confirmed; a non-empty
one names which inputs were not and why — a source file that changed since
extraction, an input nothing can fingerprint, a field two providers disagree
about, or an answer served from the memo without being re-verified. Rendering it
is the handler's job: a diagnostic surface that shows findings while silently
dropping "one of these inputs is stale" is the failure this design exists to
prevent.

**Choose the read mode when latency matters.** The default blocks until the
answer is verified, which is what a lint gate or a CI check wants — a stale
gate result is worse than a slow one. A surface that would rather show something
now and correct it shortly (LSP navigation, a graph view, an agent loop) can
pass `mode=q.Mode.CACHED`:

```python
result = await my_rule.violations(backend, mode=q.Mode.CACHED)
```

That returns whatever the WM last computed for this query, immediately, with a
`CACHED` reservation attached saying it was not re-verified. With nothing
memoized it simply computes, so a cached read never returns an empty answer in
place of a real one. Both modes read the same memoized value, so a cached read
benefits from the last verified pass — and because the memo lives for the WM
process's lifetime, the mode pays off against a running WM rather than a one-shot
CLI invocation.

The wire format is documented under `knowledge/registerSchema` and
`knowledge/query` in [the WM/ER protocol](../wm-er-protocol.md#er---wm).
