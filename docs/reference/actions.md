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
