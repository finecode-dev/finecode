# Built-in Actions

All built-in actions are defined in `finecode_extension_api.actions`. Use their import paths as the `source` when declaring actions in `pyproject.toml` or `preset.toml`.

---

## `lint`

Run linting on a project or specific files.

- **Source:** `finecode_extension_api.actions.lint.LintAction`
- **Default handler execution:** concurrent

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `target` | `"project"` \| `"files"` | `"project"` | Lint the whole project or specific files |
| `file_paths` | `list[Path]` | `[]` | Files to lint (required when `target="files"`) |

**Result:** list of diagnostics (file, line, column, message, severity)

---

## `lint_files`

Lint a specific set of files, with language filtering.

- **Source:** `finecode_extension_api.actions.lint_files.LintFilesAction`
- **Default handler execution:** concurrent

Similar to `lint` but designed for language-aware per-file linting. Used internally by the LSP server for real-time diagnostics.

---

## `format`

Format a project or specific files.

- **Source:** `finecode_extension_api.actions.format.FormatAction`
- **Default handler execution:** sequential

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `save` | `bool` | `true` | Write formatted content back to disk |
| `target` | `"project"` \| `"files"` | `"project"` | Format whole project or specific files |
| `file_paths` | `list[Path]` | `[]` | Files to format (required when `target="files"`) |

!!! note
    The `save` payload field controls whether changes are written to disk. The built-in `SaveFormatFilesHandler` reads this flag. If you omit the save handler from your preset, files won't be written regardless.

---

## `format_files`

Format a specific set of files, with language filtering.

- **Source:** `finecode_extension_api.actions.format_files.FormatFilesAction`
- **Default handler execution:** sequential

Used internally by the LSP server for on-save formatting.

---

## `check_formatting`

Check whether files are formatted correctly, without modifying them.

- **Source:** `finecode_extension_api.actions.check_formatting.CheckFormattingAction`

Returns a non-zero exit code if any file is not properly formatted.

---

## `build_artifact`

Build a distributable artifact (e.g. a Python wheel).

- **Source:** `finecode_extension_api.actions.build_artifact_action.BuildArtifactAction`

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `src_artifact_def_path` | `Path \| None` | `None` | Path to the artifact definition. If omitted, builds the current project. |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `src_artifact_def_path` | `Path` | Path of the artifact that was built |
| `build_output_paths` | `list[Path]` | Paths of the generated build outputs |

---

## `get_src_artifact_version`

Get the current version of a source artifact.

- **Source:** `finecode_extension_api.actions.get_src_artifact_version.GetSrcArtifactVersionAction`

Default handler in this repo: `fine_python_setuptools_scm.GetSrcArtifactVersionSetuptoolsScmHandler`

---

## `get_dist_artifact_version`

Get the version of a distributable artifact.

- **Source:** `finecode_extension_api.actions.get_dist_artifact_version.GetDistArtifactVersionAction`

---

## `get_src_artifact_registries`

List available registries for publishing an artifact.

- **Source:** `finecode_extension_api.actions.get_src_artifact_registries.GetSrcArtifactRegistriesAction`

---

## `publish_artifact`

Publish a built artifact.

- **Source:** `finecode_extension_api.actions.publish_artifact.PublishArtifactAction`

---

## `publish_artifact_to_registry`

Publish an artifact to a specific registry.

- **Source:** `finecode_extension_api.actions.publish_artifact_to_registry.PublishArtifactToRegistryAction`

---

## `is_artifact_published_to_registry`

Check whether a specific version of an artifact is already published.

- **Source:** `finecode_extension_api.actions.is_artifact_published_to_registry.IsArtifactPublishedToRegistryAction`

---

## `verify_artifact_published_to_registry`

Verify that publishing succeeded by checking the registry.

- **Source:** `finecode_extension_api.actions.verify_artifact_published_to_registry.VerifyArtifactPublishedToRegistryAction`

---

## `list_src_artifact_files_by_lang`

List source files grouped by programming language.

- **Source:** `finecode_extension_api.actions.list_src_artifact_files_by_lang.ListSrcArtifactFilesByLangAction`

---

## `group_src_artifact_files_by_lang`

Group source files by language (internal, used by language-aware actions).

- **Source:** `finecode_extension_api.actions.group_src_artifact_files_by_lang.GroupSrcArtifactFilesByLangAction`

---

## `prepare_envs`

Set up virtual environments for all handler dependencies.

- **Source:** `finecode_extension_api.actions.prepare_envs.PrepareEnvsAction`

Also available as the `python -m finecode prepare-envs` CLI command.

---

## `install_deps_in_env`

Install dependencies into a specific environment.

- **Source:** `finecode_extension_api.actions.install_deps_in_env.InstallDepsInEnvAction`

---

## `dump_config`

Dump the resolved configuration for a project.

- **Source:** `finecode_extension_api.actions.dump_config.DumpConfigAction`

Also available as `python -m finecode dump-config`.

---

## `init_repository_provider`

Initialize a repository provider (used in artifact publishing flows).

- **Source:** `finecode_extension_api.actions.init_repository_provider.InitRepositoryProviderAction`

---

## `prepare_runners`

Prepare Extension Runners (internal, called by the Workspace Manager).

- **Source:** `finecode_extension_api.actions.prepare_runners.PrepareRunnersAction`

---

## `clean_finecode_logs`

Remove FineCode log files.

- **Source:** `finecode_extension_api.actions.clean_finecode_logs.CleanFineCodeLogsAction`
