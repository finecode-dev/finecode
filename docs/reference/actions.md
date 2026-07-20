# Built-in Actions

All built-in actions are re-exported from `finecode_extension_api.actions`. Use the short form `finecode_extension_api.actions.<ClassName>` as the `source` when declaring actions in `pyproject.toml` or `preset.toml`.

---

## `lint`

Run linting on a source artifact or specific files.

- **Source:** `finecode_extension_api.actions.LintAction`
- **Default handler execution:** concurrent

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `target` | `"project"` \| `"files"` | `"project"` | Lint the whole source artifact (`target="project"`) or specific files |
| `file_paths` | `list[Path]` | `[]` | Files to lint (required when `target="files"`) |

**Result:** list of diagnostics (file, line, column, message, severity)

---

## `lint_files`

Lint a specific set of files. Internal action dispatched by `lint`.

- **Source:** `finecode_extension_api.actions.LintFilesAction`
- **Default handler execution:** concurrent

The built-in `LintFilesDispatchHandler` groups the given files by language and dispatches each file individually to the matching language subaction — any action declaring `PARENT_ACTION = LintFilesAction` and the corresponding `LANGUAGE`. Files of unknown language are skipped.

---

## `lint_python_files`

Lint Python source files and report diagnostics. Language-specific subaction of `lint_files`.

- **Source:** `finecode_extension_api.actions.LintPythonFilesAction`
- **Default handler execution:** concurrent

**Payload fields:** same as `lint_files`.

Register Python linting tools (ruff, mypy, …) as handlers for this action.

---

## `format`

Format a source artifact or specific files.

- **Source:** `finecode_extension_api.actions.FormatAction`
- **Default handler execution:** sequential

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `save` | `bool` | `true` | Write formatted content back to disk |
| `target` | `"project"` \| `"files"` | `"project"` | Format the whole source artifact (`target="project"`) or specific files |
| `file_paths` | `list[Path]` | `[]` | Files to format (required when `target="files"`) |

!!! note
    The `save` payload field controls whether changes are written to disk. The built-in `SaveFormatFileHandler` reads this flag. If you omit the save handler from your preset, files won't be written regardless.

---

## `format_files`

Format a specific set of files. Internal action dispatched by `format`.

- **Source:** `finecode_extension_api.actions.FormatFilesAction`
- **Default handler execution:** sequential

The built-in `FormatFilesIterateHandler` iterates over all files and delegates each to `format_file`. Language routing is handled by `format_file` via its dispatch handler — `format_files` has no language awareness.

---

## `format_file`

Format a single file. Item-level action; handlers run sequentially as a pipeline.

- **Source:** `finecode_extension_api.actions.FormatFileAction`
- **Default handler execution:** sequential

**Payload fields:**

| Field | Type | Description |
|---|---|---|
| `file_path` | `ResourceUri` | The single file to format |
| `save` | `bool` | Whether to write the result back to disk |

**Run context kwargs** (`FormatFileCallerRunContextKwargs`):

| Field | Type | Default | Description |
|---|---|---|---|
| `file_editor_session` | `IFileEditorSession \| None` | `None` | Shared session from a parent action. If absent, the context opens its own. |
| `file_info` | `FileInfo \| None` | `None` | Pre-read file content. If absent, the context reads the file itself (with `block=True`). |

When called standalone (e.g. IDE on-save), no kwargs are needed — the context creates its own session and reads the file. When called from `format_files`, the iterate handler passes the parent session. When called from the dispatch handler into a language subaction, both session and file info are passed to avoid redundant reads.

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `changed` | `bool` | Whether the file content was modified |
| `code` | `str` | The formatted content |

Handlers read and update `run_context.file_info` to pass formatted content to the next handler in the pipeline.

---

## `format_python_file`

Format a single Python file. Language-specific item-level subaction of `format_file`.

- **Source:** `finecode_extension_api.actions.FormatPythonFileAction`
- **Default handler execution:** sequential

**Payload fields:** same as `format_file`.

Register Python formatting tools (ruff, isort, …) as handlers for this action. Handler order matters — they run sequentially as a pipeline.

---

## `build_artifact`

Build a distributable artifact (e.g. a Python wheel).

- **Source:** `finecode_extension_api.actions.BuildArtifactAction`

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `src_artifact_def_path` | `Path \| None` | `None` | Path to the artifact definition. If omitted, builds the current source artifact. |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `src_artifact_def_path` | `Path` | Path of the artifact that was built |
| `build_output_paths` | `list[Path]` | Paths of the generated build outputs |

---

## `get_src_artifact_version`

Get the current version of a source artifact.

- **Source:** `finecode_extension_api.actions.GetSrcArtifactVersionAction`

Default handler in this repo: `fine_python_setuptools_scm.GetSrcArtifactVersionSetuptoolsScmHandler`

---

## `get_dist_artifact_version`

Get the version of a distributable artifact.

- **Source:** `finecode_extension_api.actions.GetDistArtifactVersionAction`

---

## `get_src_artifact_language`

Get the primary programming language of a source artifact. Used by language-aware dispatch handlers (e.g. `lock_dependencies`) to route to the appropriate language-specific subaction.

- **Source:** `finecode_extension_api.actions.GetSrcArtifactLanguageAction`

**Payload fields:**

| Field | Type | Description |
|---|---|---|
| `src_artifact_def_path` | `Path` | Path to the artifact definition file |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `language` | `str` | Language identifier, e.g. `"python"`, `"javascript"`, `"rust"` |

---

## `get_src_artifact_registries`

List available registries for publishing an artifact.

- **Source:** `finecode_extension_api.actions.GetSrcArtifactRegistriesAction`

---

## `publish_artifact`

Publish a built artifact.

- **Source:** `finecode_extension_api.actions.PublishArtifactAction`

---

## `publish_artifact_to_registry`

Publish an artifact to a specific registry.

- **Source:** `finecode_extension_api.actions.PublishArtifactToRegistryAction`

---

## `is_artifact_published_to_registry`

Check whether a specific version of an artifact is already published.

- **Source:** `finecode_extension_api.actions.IsArtifactPublishedToRegistryAction`

---

## `verify_artifact_published_to_registry`

Verify that publishing succeeded by checking the registry.

- **Source:** `finecode_extension_api.actions.VerifyArtifactPublishedToRegistryAction`

---

## `list_src_artifact_files_by_lang`

List source files grouped by programming language.

- **Source:** `finecode_extension_api.actions.ListSrcArtifactFilesByLangAction`

---

## `group_src_artifact_files_by_lang`

Group source files by language (internal, used by language-aware actions).

- **Source:** `finecode_extension_api.actions.GroupSrcArtifactFilesByLangAction`

---

## `create_envs`

Create virtual environments for all envs discovered from the project's dependency-groups.

- **Source:** `finecode_extension_api.actions.CreateEnvsAction`

---

## `install_envs`

Install handler dependencies into virtualenvs.

- **Source:** `finecode_extension_api.actions.InstallEnvsAction`

The `python -m finecode prepare-envs` CLI command runs `create_envs` and `install_envs` in sequence.

---

## `install_deps_in_env`

Install dependencies into a specific environment.

- **Source:** `finecode_extension_api.actions.InstallDepsInEnvAction`

---

## `sync_toolchains`

Derive each environment's toolchain axis from the project's declared support range and write it into the project definition file.

- **Source:** `fine_envs.SyncToolchainsAction`

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `project_def_path` | `Path \| None` | `None` | Project definition file declaring the envs. `None` means the current project. |
| `save` | `bool` | `True` | Write the derived axis to the file. `False` derives and reports without writing. |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `axes` | `list[EnvToolchainAxis]` | Per env: `declared`, `derived`, and whether it `changed` |
| `saved` | `bool` | Whether a derived axis was written |

A **toolchain** is the implementation-and-version a project is executed against; in Python it is an [interpreter](../glossary.md#interpreter). Every ecosystem declares its support range somewhere (`requires-python`, `engines`, `required_ruby_version`), and a language handler expands that range into toolchain identities. The action dispatches on project language to the matching subaction.

The axis is *materialized* — written to the file rather than recomputed on each run — so that config resolution stays a pure read of already-declared data. See [ADR-0053](../adr/0053-derived-interpreter-axis-is-materialized-into-config.md) for why, and note the consequence: the axis is wholly generated, so extra toolchains are configured as *inputs to the source* (`extra_interpreters`) rather than hand-added to its output.

---

## `check_toolchains`

Check whether each environment's materialized toolchain axis still matches what the source derives. Fails with a non-zero return code on drift.

- **Source:** `fine_envs.CheckToolchainsAction`

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `project_def_path` | `Path \| None` | `None` | Project definition file declaring the envs. `None` means the current project. |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `stale_axes` | `list[EnvToolchainAxis]` | Envs whose declared axis differs from the derived one |

A generated, committed axis can go stale — the support range changes, or the source learns about a newer toolchain. That is the same staleness a lock file has, and it is caught the same way: re-derive and compare. Wire this into `precommit` and CI. Runs `sync_toolchains` with `save = False` and reports what would change.

---

## `sync_python_interpreters`

Derive an environment's Python interpreter axis from `requires-python`. Language-specific subaction of `sync_toolchains`.

- **Source:** `fine_python_lang.SyncPythonInterpretersAction`
- **Handler:** `fine_python_package_info.SyncPythonInterpretersPyHandler`
- **Preset:** `fine_python_envs`

**Payload fields:** same as `sync_toolchains`. **Result fields:** same as `sync_toolchains`.

**Handler config:**

| Field | Type | Default | Description |
|---|---|---|---|
| `envs` | `list[str]` | `[]` | Envs whose interpreter axis is derived. Empty means none — the action is a no-op. An env either derives its axis or has one pinned, never both. |
| `max_supported_python` | `str \| None` | `None` | Cap the newest CPython to derive. `None` means no cap beyond what is obtainable. |
| `extra_interpreters` | `list[str]` | `[]` | Interpreters beyond the derived CPython rows, e.g. `["pypy@3.11"]`. |

`requires-python` is a *specifier*, not an enumeration, so it is expanded against the set of **obtainable** interpreters (see `list_obtainable_toolchains` below). An open upper bound (`>=3.11`) — the correct form for a published package — is bounded by that set rather than rejected. The result therefore depends on something outside the specifier, which is exactly why it is persisted.

`requires-python` constrains version only and carries no implementation, so the derived axis is CPython-only. PyPy and friends are configured via `extra_interpreters`.

Matrices stay opt-in: with no `envs` configured, nothing is derived and every action keeps running in a single environment with an unchanged result shape.

The derived axis is written into the **project's own** definition file, and project config beats preset config. So if a preset pins `interpreters` for an env that is also listed in `envs`, the derived axis is materialized over it and the run warns once, since the pin stops having any effect. To keep the preset's axis instead, drop that env from `envs` in your own config. Deriving into your own file is also the only way to override a pinned axis at all, because config layering can replace a key but never unset one.

Materializing once per project rather than sharing one axis from a preset is deliberate: the axis derives from `requires-python`, which is per-project, and a project that states its own axis cannot have its matrix changed by a preset bump without a diff. See [ADR-0053](../adr/0053-derived-interpreter-axis-is-materialized-into-config.md) and `SyncPythonInterpretersPyHandler`'s docstring.

---

## `list_obtainable_toolchains`

List the toolchains the environment provisioner is able to obtain.

- **Source:** `fine_envs.ListObtainableToolchainsAction`

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `include_prereleases` | `bool` | `False` | Include prerelease toolchains (e.g. a Python beta). |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `toolchains` | `list[str]` | Canonical identities, e.g. `cpython@3.13` — no patch level, variant, or platform tag |

**"Obtainable" is deliberately not "installed".** This reports what the *provisioner* can get — a property of a locked dependency — not what happens to be present on this machine. Only the former may feed a derived matrix axis: an axis sourced from local installs would differ between developers on the same commit. Whether a toolchain is available *here* is a separate question, and would be a separate action.

The provisioner is the authority because deriving a version it cannot obtain yields an axis whose environments cannot be created. This is what `sync_toolchains` expands `requires-python` against.

---

## `list_obtainable_python_interpreters`

Language-specific subaction of `list_obtainable_toolchains`. Backed by uv.

- **Source:** `fine_python_lang.ListObtainablePythonInterpretersAction`
- **Handler:** `fine_python_uv.UvListObtainablePythonInterpretersHandler`
- **Preset:** `fine_python_envs`

**Payload and result fields:** same as `list_obtainable_toolchains`.

**Handler config:**

| Field | Type | Default | Description |
|---|---|---|---|
| `variant` | `str` | `"default"` | Build variant to report. `freethreaded` builds are a separate variant the `(implementation, version)` identity cannot express. |

Runs `uv python list --only-downloads`, which reports uv's own manifest rather than the machine's installed Pythons. uv's listing is far finer-grained than a matrix axis — patch levels, prereleases, freethreaded variants, platform tags — and all of that is collapsed to one identity per implementation and minor version. Prereleases are excluded by default, so a released beta (`cpython-3.15.0b1`) never enters an axis.

---


## `dump_config`

Dump the resolved configuration for a source artifact that includes FineCode configuration.

- **Source:** `finecode_extension_api.actions.DumpConfigAction`

Also available as `python -m finecode dump-config`.

---

## `init_repository_provider`

Initialize a repository provider (used in artifact publishing flows).

- **Source:** `finecode_extension_api.actions.InitRepositoryProviderAction`

---

## `clean_finecode_logs`

Remove FineCode log files.

- **Source:** `finecode_extension_api.actions.CleanFinecodeLogsAction`
