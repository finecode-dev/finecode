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
| `finecode_extension_api.interfaces.iactionrunner.IActionRunner` | `finecode_extension_runner.impls.action_runner.ActionRunner` | Run actions and query action declarations. |
| `finecode_extension_api.interfaces.irepositorycredentialsprovider.IRepositoryCredentialsProvider` | `finecode_extension_runner.impls.repository_credentials_provider.ConfigRepositoryCredentialsProvider` | In-memory repository credentials and registry list. |
| `finecode_extension_api.interfaces.iprojectinfoprovider.IProjectInfoProvider` | `finecode_extension_runner.impls.project_info_provider.ProjectInfoProvider` | Current project paths and raw config access. |
| `finecode_extension_api.interfaces.iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider` | `finecode_extension_runner.impls.extension_runner_info_provider.ExtensionRunnerInfoProvider` | Runtime env info (venv paths, cache dir). |

## Preset-provided services

These services are declared in presets in this repo. They are available when the preset is active, or when you copy the same `[[tool.finecode.service]]` entry into your own config.

| Interface | Implementation | Declared by |
| --- | --- | --- |
| `finecode_extension_api.interfaces.ihttpclient.IHttpClient` | `finecode_httpclient.HttpClient` | `finecode_dev_common_preset` |
| `finecode_extension_api.interfaces.ijsonrpcclient.IJsonRpcClient` | `finecode_jsonrpc.jsonrpc_client.JsonRpcClientImpl` | `presets/fine_python_lint` |
| `finecode_extension_api.interfaces.ilspclient.ILspClient` | `finecode_extension_runner.impls.lsp_client.LspClientImpl` | `presets/fine_python_lint` (wraps `IJsonRpcClient`) |

## Extension-activated services

Extensions can register services via the `finecode.activator` entry point using `IServiceRegistry`. The following activators ship in this repo and register services when their packages are installed.

| Extension package | Interface | Implementation |
| --- | --- | --- |
| `fine_python_ast` | `fine_python_ast.iast_provider.IPythonSingleAstProvider` | `fine_python_ast.ast_provider.PythonSingleAstProvider` |
| `fine_python_mypy` | `fine_python_mypy.iast_provider.IMypySingleAstProvider` | `fine_python_mypy.ast_provider.MypySingleAstProvider` |
| `fine_python_package_info` | `fine_python_package_info.ipypackagelayoutinfoprovider.IPyPackageLayoutInfoProvider` | `fine_python_package_info.py_package_layout_info_provider.PyPackageLayoutInfoProvider` |
| `fine_python_package_info` | `finecode_extension_api.interfaces.isrcartifactfileclassifier.ISrcArtifactFileClassifier` | `fine_python_package_info.py_src_artifact_file_classifier.PySrcArtifactFileClassifier` |
| `fine_python_ruff` | `fine_python_ruff.ruff_lsp_service.RuffLspService` | `fine_python_ruff.ruff_lsp_service.RuffLspService` |
| `fine_python_pyrefly` | `fine_python_pyrefly.pyrefly_lsp_service.PyreflyLspService` | `fine_python_pyrefly.pyrefly_lsp_service.PyreflyLspService` |

## Service registry for extensions

Extension activators receive an `IServiceRegistry` instance (not injected into handlers) and call `register_impl()` to bind interfaces to implementations. See `finecode_extension_api.interfaces.iserviceregistry.IServiceRegistry` for the protocol and the activators above for concrete examples.
