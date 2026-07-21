# Services

Services are long-lived dependencies that handlers (and other services) can request via dependency injection. This page lists the services that ship in this repo and where they are registered. Availability depends on whether the Extension Runner provides the service, a preset declares it, or an extension activates it.

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
| `finecode_extension_api.interfaces.irepositorycredentialsprovider.IRepositoryCredentialsProvider` | `finecode_extension_runner.impls.repository_credentials_provider.ConfigRepositoryCredentialsProvider` | In-memory repository credentials and registry list. |
| `finecode_extension_api.interfaces.iprojectinfoprovider.IProjectInfoProvider` | `finecode_extension_runner.impls.project_info_provider.ProjectInfoProvider` | Current project paths and raw config access. |
| `finecode_extension_api.interfaces.iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider` | `finecode_extension_runner.impls.extension_runner_info_provider.ExtensionRunnerInfoProvider` | Runtime env info (venv paths, cache dir). |

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
- Reserve `[[tool.finecode.service]]` for **overrides** — swapping in an alternative implementation where being explicit is the point. A service declaration is applied at startup (phase 3) and therefore prevents the deferred activator from firing at all.

For a service whose interface and implementation are owned by the same extension (the AST providers above), register it directly in that extension's activator; it will be activated eagerly when the extension is active.
