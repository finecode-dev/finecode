# Built-in Actions

Built-in actions live in their respective presets. Use the short form `<preset_package>.<ClassName>` as the `source` when declaring actions in `pyproject.toml` or `preset.toml`.

**Unhandled inputs.** Every action result carries a `coverage` list. A dispatch
handler that finds no subaction for an input records a miss against it
(`NO_SUBACTIONS`, `NO_LANGUAGE_DETECTED`, or `NO_SUBACTION_FOR_LANGUAGE` with
the detected language as detail — R-310, ADR-0098). A caller distinguishes
"no handler covered this input" from "a handler ran and found nothing" by
reading `result.unhandled`; the CLI printout ends with a grouped, counted
`unhandled:` block when any miss reached the run. "Not handled" is an
answer, not an error.

---

## `lint`

Run linting across the workspace and report diagnostics.

- **Source:** `fine_lint.LintAction`
- **Scope:** `workspace` — dispatched once and routed to the workspace root project; the handler fans out `lint_files` per project internally
- **Default handler execution:** concurrent

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `target` | `"project"` \| `"files"` | `"project"` | Lint the whole workspace (`target="project"`) or specific files |
| `file_paths` | `list[ResourceUri]` | `[]` | Files to lint (required when `target="files"`) |
| `project_paths` | `list[Path] \| None` | `None` | Restrict the workspace operation to these project paths; `None` means all workspace projects |

**Result:** list of diagnostics (file, line, column, message, severity, fixability)

---

## `lint_files`

Lint a specific set of files. Internal action dispatched by `lint`.

- **Source:** `fine_lint.LintFilesAction`
- **Default handler execution:** concurrent

The built-in `LintFilesDispatchHandler` groups the given files by language and dispatches one call per language to the matching language subaction — any action declaring `PARENT_ACTION = LintFilesAction` and the corresponding `LANGUAGE`. Files of unknown language are skipped.

---

## `lint_python_files`

Lint Python source files and report diagnostics. Language-specific subaction of `lint_files`.

- **Source:** `fine_python_lang.LintPythonFilesAction`
- **Default handler execution:** concurrent

**Payload fields:** same as `lint_files`.

Register Python linting tools (ruff, mypy, …) as handlers for this action.

---

## `apply_code_actions`

Apply a batch of code-action selections to disk. The sole writer: providers
compute edits and file operations; this action validates the whole batch and
commits it.

- **Source:** `fine_lint.ApplyCodeActionsAction`
- **Scope:** project

A selection carries an ordered list of operations — text edits, file creation,
rename, and delete (including recursive directory deletes, which require an
`allow_recursive_delete` opt-in). The whole batch is validated before anything
is written; cross-selection text edits stay simultaneous, while whole-file
operations (create/rename/delete) conflict with every other selection touching
the same paths. A dry run previews the resulting content without writing.

---

## `audit_code`

Run all registered on-demand code audit tools and aggregate results. Peer of `inspect_code` (see [ADR-0044](../adr/0044-continuous-inspection-and-on-demand-audit-are-separate-umbrellas.md)): same result shape and `target="files"` existence-check guarantee, but invoked at deliberate checkpoints (explicit CLI/MCP call, precommit, CI) rather than per-keystroke, so its handlers may be whole-project and slow.

- **Source:** `fine_audit_code.AuditCodeAction`
- **Scope:** `workspace` — dispatched once and routed to the workspace root project; bridge handlers (e.g. `check_imports`) fan out per project internally
- **Default handler execution:** concurrent

**Payload fields:** same shape as `inspect_code` (`target`, `file_paths`, `project_paths`).

**Result:** list of diagnostics (file, line, column, message, severity, fixability)

Whole-project checks reach this umbrella through bridge handlers rather than being invoked separately — [`check_imports`](#check_imports) and [`check_toolchains`](#check_toolchains) both do. Such a bridge ignores per-file scoping for *what* it checks (a violation is a property of the project, not of one file) and anchors its diagnostics at the relevant config file. Adding a new checkpoint check therefore means registering a bridge, not adding a CI step.

---

## `check_imports`

Check a project's import graph against configured architectural contracts (e.g. import-linter). Internal category action bridged onto `audit_code`.

- **Source:** `fine_check_imports.CheckImportsAction`

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `src_artifact_def_path` | `ResourceUri \| None` | `None` | Path to the artifact definition file (e.g. pyproject.toml). `None` = the current project's own definition file |

Whole-project scope: analyzes the full import graph, not individual files — a violation is a relationship between modules, not a property of one file. A project with no import-graph tooling configured is a no-op (empty `messages`, not an error).

The built-in `CheckImportsDispatchHandler` detects the project's language (via `get_src_artifact_language`) and dispatches to the matching language subaction — any action declaring `PARENT_ACTION = CheckImportsAction` and the corresponding `LANGUAGE`.

---

## `check_python_imports`

Check Python import-graph contracts (e.g. import-linter) and report diagnostics. Language-specific subaction of `check_imports`.

- **Source:** `fine_python_lang.CheckPythonImportsAction`
- **Default handler execution:** concurrent

**Payload fields:** same as `check_imports`.

Register Python import-graph tools (import-linter, …) as handlers for this action.

---

## `format`

Format a source artifact or specific files.

- **Source:** `fine_format.FormatAction`
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

- **Source:** `fine_format.FormatFilesAction`
- **Default handler execution:** sequential

The built-in `FormatFilesIterateHandler` iterates over all files and delegates each to `format_file`. Language routing is handled by `format_file` via its dispatch handler — `format_files` has no language awareness.

---

## `format_file`

Format a single file. Item-level action; handlers run sequentially as a pipeline.

- **Source:** `fine_format.FormatFileAction`
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

- **Source:** `fine_python_lang.FormatPythonFileAction`
- **Default handler execution:** sequential

**Payload fields:** same as `format_file`.

Register Python formatting tools as handlers for this action. Handler order matters — they run sequentially as a pipeline.

The bundled `ruff` handler organizes imports (via ruff's `source.organizeImports`) before formatting, so import ordering is included in this action's output: a file with unsorted imports is fully fixed in one call. Import order remains owned by ruff's `I001` rule — the handler composes that action rather than adding a second sorter.

---

## `precommit`

Run configured code quality checks on git-staged files before commit. This action is not registered by default; add it through the `fine_precommit` preset or declare `tool.finecode.action.precommit` yourself.

- **Source:** `fine_git_hooks.PrecommitAction`
- **Default handler execution:** sequential

**Payload fields:**

| Field        | Type         | Default | Description                                                                                                |
|--------------|--------------|---------|------------------------------------------------------------------------------------------------------------|
| `file_paths` | `list[Path]` | `[]`    | Explicit file list. Empty means auto-detect staged files from git (done by `StagedFilesDiscoveryHandler`). |

**Result fields:**

| Field            | Type                         | Description                                                                                                                  |
|------------------|------------------------------|------------------------------------------------------------------------------------------------------------------------------|
| `action_results` | `dict[str, RunActionResult]` | Results keyed by action name (for example, `"lint"`). The overall `return_code` is `ERROR` if any sub-result is an error. |

**Run context (`PrecommitRunContext`):**

| Field          | Type                 | Description                                                                                                                                         |
|----------------|----------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------|
| `staged_files` | `list[Path] \| None` | Populated by `StagedFilesDiscoveryHandler`. `None` = discovery has not run (bridge handlers raise); `[]` = no staged files (bridge handlers skip). |

**Handler roles:**

- **`StagedFilesDiscoveryHandler`** — must be first; detects staged files via `git diff --cached --name-only --diff-filter=ACMR` and writes to `run_context.staged_files`.
- **Bridge handlers** (for example, `LintPrecommitBridgeHandler`) — each delegates to one existing action passing staged files. Register additional bridge handlers to run more tools.

!!! note "One orchestrator per repository"
    When multiple projects share a single git repository, only the project at
    the repository root runs precommit checks. `StagedFilesDiscoveryHandler`
    detects this automatically: if the current project directory does not match
    the git repository root, it sets `staged_files = []` and returns — all
    bridge handlers skip. See [ADR-0031](../adr/0031-precommit-git-root-guard.md).

See [Using Git Hooks](../guides/using-git-hooks.md) for setup instructions.

---

## `list_tests`

Discover tests and return their hierarchical structure without running them.

- **Source:** `fine_test.ListTestsAction`

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `file_paths` | `list[ResourceUri]` | `[]` | Files or directories to search. Empty means the handler uses its own defaults (e.g. `testpaths` in pytest.ini). |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `tests` | `list[TestItem]` | Tree of discovered tests. Each node carries a `test_id` usable in `run_tests`. |

---

## `run_tests`

Execute tests and return structured pass/fail results.

- **Source:** `fine_test.RunTestsAction`

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `file_paths` | `list[ResourceUri]` | `[]` | Test files or directories to run. Empty means handler defaults. |
| `test_ids` | `list[TestId]` | `[]` | Specific tests to run, obtained from `list_tests`. |
| `markers` | `list[str]` | `[]` | Marker/tag names to filter (e.g. `["unit", "slow"]`). Handlers map these to runner-specific flags. |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `test_results` | `list[TestCaseResult]` | One entry per executed test with outcome, duration, and failure message. |

---

## `build_artifact`

Build a distributable artifact (e.g. a Python wheel).

- **Source:** `fine_src_artifacts.BuildArtifactAction`

A dispatch handler detects the artifact's language and delegates to the
language-specific subaction (e.g. `build_python_artifact`).

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `src_artifact_def_path` | `Path \| None` | `None` | Path to the artifact definition. If omitted, builds the current source artifact. |
| `output_dir` | `Path \| None` | `None` | Directory to write the built artifact(s) into. `None` uses the handler default (for Python, `<project>/dist`). |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `src_artifact_def_path` | `Path` | Path of the artifact that was built |
| `build_output_paths` | `list[Path]` | Paths of the generated build outputs |

---

## `build_python_artifact`

Build the wheel and/or sdist distributions of a Python artifact.

- **Source:** `fine_python_lang.BuildPythonArtifactAction`
- **Parent action:** `build_artifact`

**Payload fields:** extends `BuildArtifactRunPayload` with

| Field | Type | Default | Description |
|---|---|---|---|
| `distributions` | `list["sdist" \| "wheel"] \| None` | `None` | Distribution formats to build. `None` builds the default (sdist, then wheel from it); `["wheel"]` builds only the wheel. |

---

## `get_src_artifact_version`

Get the current version of a source artifact.

- **Source:** `fine_src_artifacts.GetSrcArtifactVersionAction`

Default handler in this repo: `fine_python_setuptools_scm.GetSrcArtifactVersionSetuptoolsScmHandler`

---

## `get_src_artifact_toolchain_range`

Read the range of toolchain versions a source artifact supports — in Python, from
`project.requires-python`.

- **Source:** `fine_src_artifacts.GetSrcArtifactToolchainRangeAction`

Default Python handler: `fine_python_package_info.GetSrcArtifactToolchainRangePyHandler`
(registered by the `fine_python_src_artifacts` preset).

This is the single source the language-level settings of the Python tools come from —
ruff's `target-version`, black's `target_versions` — so they cannot disagree with each
other or with what the project declares. Each tool still accepts an explicit value in
its own handler config, which wins; leaving it unset is what defers to this action.

It is deliberately **not** the interpreter axis (`sync_toolchains`). The axis is this
range intersected with what the environment provisioner can obtain — what a project is
*tested on*. This is what it *promises*, and the promise is what a language level must
encode.

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `src_artifact_def_path` | `ResourceUri \| None` | `None` | Definition file to read. If omitted, the current project's. |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `min_version` | `str \| None` | Oldest supported version, e.g. `3.11`. `None` when nothing is declared. |
| `max_version` | `str \| None` | Newest supported version. `None` when the upper end is open — the normal case for a published package. |
| `derived_from` | `str \| None` | Where the range came from, so a surprising target version can be traced without reading handler source. |

Contributions from multiple handlers **intersect**: merging can narrow a declared range
but never widen one.

**Handler config (`GetSrcArtifactToolchainRangePyHandler`):**

| Field | Type | Default | Description |
|---|---|---|---|
| `min_version` | `str \| None` | `None` | Pin the oldest supported version instead of deriving it. |
| `max_version` | `str \| None` | `None` | Pin the newest supported version instead of deriving it. |

Replacing the handler replaces the algorithm for every tool at once — that is the point
of the range being an action rather than a config field on each tool.

---

## `get_dist_artifact_version`

Get the version of a distributable artifact.

- **Source:** `fine_dist_artifacts.GetDistArtifactVersionAction`

---

## `get_src_artifact_language`

Get the primary programming language of a source artifact. Used by language-aware dispatch handlers (e.g. `lock_dependencies`) to route to the appropriate language-specific subaction.

- **Source:** `fine_src_artifacts.GetSrcArtifactLanguageAction`

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

- **Source:** `fine_src_artifacts.GetSrcArtifactRegistriesAction`

**Payload fields:**

| Field | Type | Description |
|---|---|---|
| `src_artifact_def_path` | `ResourceUri` | Path to the artifact definition file (e.g. `pyproject.toml`) |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `registries` | `list[Registry]` | Registries configured for this artifact |

`Registry` fields:

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Registry name, used by publishing actions to select it |
| `index_url` | `str` | Index (read) endpoint |
| `upload_url` | `str` | Upload (write) endpoint |

The two URLs are the ones configured for `IRepositoryCredentialsProvider` — see
[Provisioning `IRepositoryCredentialsProvider`](services.md#provisioning-irepositorycredentialsprovider)
and [Why two URLs](#why-two-urls).

---

## `lock_dependencies`

Lock the dependencies of a source artifact.

- **Source:** `fine_src_artifacts.LockDependenciesAction`

**Payload fields:**

| Field | Type | Description |
|---|---|---|
| `src_artifact_def_path` | `Path` | Path to the artifact definition file (e.g. `pyproject.toml`, `package.json`) |
| `output_dir` | `Path` | Directory where lock files will be written. The handler decides filenames. |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `lock_file_paths` | `list[Path]` | All lock files generated — one entry for single-lock, N entries for multi-lock |
---

## `lock_python_dependencies`

Lock Python dependencies for a specific Python version and platform. Language-specific subaction of `lock_dependencies`.

- **Source:** `fine_python_lang.LockPythonDependenciesAction`

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `src_artifact_def_path` | `Path` | | Path to the artifact definition file (e.g. `pyproject.toml`) |
| `output_dir` | `Path` | | Directory where lock files will be written |
| `target_python_version` | `str \| None` | `None` | Python version to target, e.g. `"3.11"`. Defaults to the running interpreter. |
| `target_platform` | `str \| None` | `None` | Wheel platform tag to target, e.g. `"linux_x86_64"`. Defaults to the current platform. |

**Result fields:** same as `lock_dependencies`.

`target_python_version` and `target_platform` are typically used for target projection or target-specific lock generation.

See the [Designing Actions Rules](../guides/designing-actions-rules.md) and [Designing Actions Reference](../guides/designing-actions-reference.md) for the rationale behind generic vs. language-specific actions.

---

## `publish_artifact`

Publish a built artifact to every configured registry.

- **Source:** `fine_dist_artifacts.PublishArtifactAction`

Registries are independent publish targets: one registry failing does not cancel
or abort the uploads to the others. The result reports `published_registries`
(accepted the upload) and `failed_registries` (registry name → error) separately,
so a partial publish is visible rather than collapsed into a single failure. A
registry that was already up to date appears in neither. The return code is
`ERROR` whenever `failed_registries` is non-empty.

---

## `publish_artifact_to_registry`

Publish an artifact to a specific registry.

- **Source:** `fine_dist_artifacts.PublishArtifactToRegistryAction`

A publish failure is reported as `error` in the result (with `return_code` `ERROR`
and empty `published_paths`), not raised. This lets `publish_artifact` attribute a
failure to one registry and still report what the other registries did.

---

## `list_published_artifacts`

List the distribution filenames a registry holds for a given version, without
requiring any local distribution paths. A caller derives what it needs:
`bool(filenames)` answers "is this version published at all" (used by a
dry-run preview, before anything is built); filtering local dist paths by
filename membership answers "which of my files still need uploading".

- **Source:** `fine_dist_artifacts.ListPublishedArtifactsAction`

---

## `verify_artifact_published_to_registry`

Verify that publishing succeeded by checking the registry.

- **Source:** `fine_dist_artifacts.VerifyArtifactPublishedToRegistryAction`

---

## `list_src_artifact_files_by_lang`

List source files grouped by programming language.

- **Source:** `fine_src_artifacts.ListSrcArtifactFilesByLangAction`

Handlers list the files of **their own project only**. A recursive walk does not stop at
the root of a nested project, so a handler must prune those roots as it walks:
`workspace_utils.nested_project_dirs` names the boundaries (from
`actionable_project_paths` — a nested directory that is *not* an actionable project has
no runner of its own, so its files stay with the enclosing project), and
`workspace_utils.walk_project_files` walks without descending into them or into hidden
directories.

Without this, an operation restricted to the outer project (`lint --project-paths=...`)
silently processes the inner project too, and an unrestricted workspace run processes
those files twice — once for each project claiming them. Pruning during the walk rather
than filtering afterwards is also what makes a workspace-root listing affordable: on
FineCode's own repository the unpruned walk visits every nested project's virtualenv and
takes ~17s, against ~0.1s pruned.

---

## `group_src_artifact_files_by_lang`

Group source files by language (internal, used by language-aware actions).

- **Source:** `fine_src_artifacts.GroupSrcArtifactFilesByLangAction`

---

## `create_envs`

Create virtual environments for all envs discovered from the project's dependency-groups.

- **Source:** `fine_envs.CreateEnvsAction`

---

## `install_envs`

Install handler dependencies into virtualenvs.

- **Source:** `fine_envs.InstallEnvsAction`

The `python -m finecode prepare-envs` CLI command runs `create_envs` and `install_envs` in sequence.

---

## `install_deps_in_env`

Install dependencies into a specific environment.

- **Source:** `fine_envs.InstallDepsInEnvAction`

---

## `list_envs`

List the project's environments, showing which are declared in configuration,
which exist on disk, and which are **orphaned** — present in `.venvs/` while
nothing declares them. See
[Preparing Environments — Orphaned environments](../guides/preparing-environments.md#orphaned-environments).

- **Source:** `fine_envs.ListEnvsAction`

The payload is empty. Each entry of the `envs` result field carries:

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Environment name |
| `venv_dir_path` | `ResourceUri` | `<project>/.venvs/<name>` |
| `declared` | `bool` | Whether the resolved `[dependency-groups]` still names it. `false` means orphaned |
| `state` | `created` \| `broken` \| `missing` | On-disk state |

State is derived from the filesystem alone — no interpreter is executed, so
listing stays cheap across a whole workspace. `created` therefore means "looks
like a venv" (a `pyvenv.cfg` is present), not "verified runnable"; `broken`
means something occupies the path but is not a usable venv.

---

## `remove_envs`

Remove environments from disk. Nothing else does: `prepare-envs --recreate`
rebuilds only the envs discovery found, so an env dropped from configuration
keeps its venv forever.

- **Source:** `fine_envs.RemoveEnvsAction`

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `env_names` | `list[str] \| None` | `None` | Environments to remove. `None` means discover — which resolves to the project's orphaned envs. An empty list is an explicit no-op |
| `force` | `bool` | `False` | Allow removing an environment configuration still declares |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `removed` | `list[str]` | Environments whose directory was deleted |
| `errors` | `list[str]` | Per-env failures. One undeletable env does not abort the rest; `return_code` is `ERROR` whenever this is non-empty |

Two guards apply to the names asked for, before existence is checked — a
rejection must not depend on whether a venv happens to be on disk right now:

- A **declared** env is refused unless `force = true`. An Extension Runner may
  be running in it, and deleting a venv from under a live process breaks it in
  a way that is hard to diagnose. Orphans by definition have no runner, so the
  default path is unguarded. After a forced removal, run `prepare-envs` to
  recreate the env.
- The **current** env (the `dev_workspace` the handler itself runs in) is never
  removable, `force` or not.

Removal tolerates broken state: a half-created venv, one whose files lost write
permission, a dangling symlink, or a plain file at the venv path are all
removed, and an already-absent path is treated as success.

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
| `project_def_path` | `ResourceUri \| None` | Definition file the axes were checked against — what per-file consumers anchor the drift to |

A generated, committed axis can go stale — the support range changes, or the source learns about a newer toolchain. That is the same staleness a lock file has, and it is caught the same way: re-derive and compare. Runs `sync_toolchains` with `save = False` and reports what would change.

Rather than being invoked as its own CI step, this is bridged onto [`audit_code`](#audit_code) by `fine_envs.check_toolchains_audit_code_bridge_handler.CheckToolchainsAuditCodeBridgeHandler`, which flattens each stale axis into an ERROR diagnostic anchored at `project_def_path`. The bridge is registered by whichever preset knows both actions exist (in this repo, `finecode_dev_common_preset`) and needs the `fine_envs[audit]` extra — `fine_envs` itself is the mandatory base preset, so it never depends on the audit umbrella. A `precommit` bridge exists too (`fine_git_hooks.CheckToolchainsPrecommitBridgeHandler`), unregistered by default because re-deriving the axis spawns a package-manager subprocess on every commit.

---

## `sync_python_interpreters`

Derive an environment's Python interpreter axis from the project's declared support. Language-specific subaction of `sync_toolchains`.

- **Source:** `fine_python_lang.SyncPythonInterpretersAction`
- **Handler:** `fine_python_package_info.SyncPythonInterpretersHandler`
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

Materializing once per project rather than sharing one axis from a preset is deliberate: the axis derives from `requires-python`, which is per-project, and a project that states its own axis cannot have its matrix changed by a preset bump without a diff. See [ADR-0053](../adr/0053-derived-interpreter-axis-is-materialized-into-config.md) and `SyncPythonInterpretersHandler`'s docstring.

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

List the Python interpreters the environment provisioner can obtain. Language-specific subaction of `list_obtainable_toolchains`.

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

## `setup_system`

Install and configure system-level dependencies and tools.

- **Source:** `fine_system_setup.SetupSystemAction`
- **Default handler execution:** sequential

Handles OS packages, IDE extensions, non-Python language tooling, and any other
dependencies that fall outside Python's package management and cannot be handled
automatically by `prepare-envs`.

The `fine_system_setup` preset declares this action with an empty handler list — a
safe no-op until handlers are registered. Add team-shared handlers in a shared preset
or project config; add personal handlers in `finecode-user.toml`.

**Handler contract:**

- Check whether the dependency or tool is already present before acting (idempotency).
- Populate `installed` on success, `skipped` when already present, `failed` on error.
- All handlers always run; failures are collected and reported in aggregate.

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `installed` | `list[str]` | Steps that completed installation or configuration |
| `skipped` | `list[str]` | Steps skipped because the dependency or tool was already present |
| `failed` | `list[str]` | Steps that failed; non-empty means `return_code` is `ERROR` |

**Handlers shipped today:** `fine_agent_pi.InstallPiHandler` installs the `pi` CLI
itself; `fine_agent_pi.InstallPiPackagesHandler` installs configured pi packages into
the project. The packages handler writes `<project>/.pi/settings.json`,
`<project>/.pi/npm/` and (for git sources, which project discovery does not skip)
`<project>/.pi/git/`, and never `~/.pi/agent`. List it after `install_pi`. pi resolves
project packages from the session's cwd only, so register it where pi sessions start —
not in a widely included preset — and give each project its own `.pi/`.

The FineCode `packages` config is authoritative for additions and pins; the settings
file's `packages` key is generated output (pi merges it and keeps pi's other keys). Stale
entries are logged and left in place — remove one with `pi remove <src> -l`. In a project
that commits `.pi/settings.json`, register only shared packages in tracked config;
personal ones belong in `finecode-user.toml` and only while that file is untracked.
Configure one version per npm package; a second spec of the same package fails. The first
install makes pi prompt for trust once, and RPC/print sessions (`PiAgentHandler`
included) load none of the packages until the project is trusted. `--approve` extends
FineCode's trust to the project's `.pi/settings.json` for the handler's own commands,
including a project `npmCommand`, which pi executes; it loads no project extensions and
persists nothing. npm lifecycle scripts are off for the handler's own installs only, and
only while npm is the command (`allow_lifecycle_scripts = true` opts out); pi's startup
self-heal, `pi update`, manual installs and custom `npmCommand`s still run them, with
`ignore-scripts=true` in `~/.npmrc` as the machine-wide control. Pin npm versions and
git refs — pi's docs warn that packages run with full system access.

```toml
# finecode-user.toml (no [finecode] wrapper)
[action.setup_system]
handlers = [
  { name = "install_pi_packages", source = "fine_agent_pi.InstallPiPackagesHandler", env = "dev_workspace", dependencies = [
    "fine_agent_pi~=0.1.0a0",
  ], config.packages = ["npm:pi-clear@0.1.1", "npm:@ff-labs/pi-fff@0.10.6"] },
]
```

```toml
# pyproject.toml
[tool.finecode.action.setup_system]
handlers = [
  { name = "install_pi", source = "fine_agent_pi.InstallPiHandler", env = "dev_workspace", dependencies = [
    "fine_agent_pi~=0.1.0a0",
  ] },
  { name = "install_pi_packages", source = "fine_agent_pi.InstallPiPackagesHandler", env = "dev_workspace", dependencies = [
    "fine_agent_pi~=0.1.0a0",
  ], config.packages = ["npm:pi-clear@0.1.1"] },
]
```

---

## `run_agent_task`

Delegate a task to an AI coding agent and return its output.

- **Source:** `fine_agent.RunAgentTaskAction`
- **Default handler execution:** sequential

The `fine_agent` preset declares the action with no handler, so including it alone
is a no-op. A backend extension supplies the implementation. Two exist today:
`fine_agent_pi.PiAgentHandler`, which drives the `pi` CLI in its RPC mode, and
`fine_agent_claude_code.ClaudeCodeAgentHandler`, which drives the `claude` CLI in
its non-interactive print mode.

**Exactly one handler.** Unlike most actions, this one gains nothing from merging
several handlers' results: two agents independently attempting the same task would
both write to the same files, and there is no meaningful way to combine their
answers. Swap backends by *replacing* the registered handler, never by adding a
second one. Nothing enforces this — the result merge degrades to last-writer-wins.

**Payload fields:**

| Field | Type | Description |
|---|---|---|
| `prompt` | `str` | The task, in natural language |

Which model runs the task is handler configuration, not payload, so the same task
definition is portable across setups.

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `status` | `AgentRunStatus` | `settled`, `failed`, `aborted`, or `refused_interaction` |
| `output` | `str` | The agent's final text |
| `turns` | `int \| None` | Assistant turns taken, and a sign of looping; `None` where the backend has no turn concept |
| `usage` | `AgentRunUsage \| None` | What the run consumed; `None` when the backend reported nothing |
| `duration_sec` | `float \| None` | Wall-clock time, measured by the handler rather than reported by the backend |
| `error` | `str \| None` | Why the run did not settle; `None` when `status` is `settled` |

`refused_interaction` is deliberately distinct from `failed`: nothing went wrong,
the agent asked the user a question and no interactive channel was available. A
non-interactive caller (CI) needs to tell those apart.

**`AgentRunUsage` fields**, all optional and independently so:

| Field | Type | Description |
|---|---|---|
| `input_tokens` | `int \| None` | |
| `output_tokens` | `int \| None` | |
| `cache_read_tokens` | `int \| None` | Not universal — some backends report a single "cached" figure, or none |
| `cache_write_tokens` | `int \| None` | |
| `total_tokens` | `int \| None` | Reported as-is, never derived; may exceed input + output |
| `approx_cost_usd` | `float \| None` | The backend's own estimate, in USD |
| `provider`, `model` | `str \| None` | What produced the figures above |

Backends differ in what they can account for, so a partially filled `AgentRunUsage`
is the normal case rather than a degraded one. **`None` always means "the backend
did not report this", never zero** — a handler that fills a gap with `0` would
report a run as free, or as having read no input, when the truth is that nobody
said. For the same reason handlers report only what the backend reported: no
derived totals, no pricing against a table the handler carries, no currency
conversion.

`approx_cost_usd` is an estimate, not a bill. It is the backend's price table
applied to its own token counts: the table can be stale, absent for a model the
backend does not know, and for a subscription-covered agent it is what the run
*would* have cost at API rates rather than anything charged.

Usage is reported on failed and refused runs too, not only settled ones — a run
that spent real money and then failed is exactly when the number is worth having.
The one gap is cancellation: nothing is returned on that path, so what the run
spent before being withdrawn is lost with it.

### `PiAgentHandler` configuration

| Option | Default | Description |
|---|---|---|
| `model` | `None` | Passed to `pi --model`; `None` leaves pi's own default |
| `provider` | `None` | Passed to `pi --provider` |
| `ui_policy` | `{}` | Per dialog method (`select`, `confirm`, `input`, `editor`) → how to answer |
| `default_ui_policy` | `"abort"` | Applied to methods absent from `ui_policy` |
| `settle_timeout_sec` | `900.0` | Ceiling on one run; an agent loop has no natural bound |

A `ui_policy` value of `abort` refuses and ends the run, `cancel` declines and lets
the agent continue, and any other string is sent back as the literal answer.

Every dialog gets an answer, always. Leaving one unanswered is not neutral: pi
resolves it with its own default once its timeout expires, silently — which for a
tool-approval gate is an unlogged auto-approval. A dialog method FineCode does not
recognise is refused rather than ignored, for the same reason.

### `ClaudeCodeAgentHandler` configuration

| Option | Default | Description |
|---|---|---|
| `model` | `None` | Passed to `claude --model`, as an alias (`opus`, `sonnet`) or a full name; `None` leaves the CLI's own default |
| `permission_mode` | `None` | Passed to `claude --permission-mode` (`acceptEdits`, `bypassPermissions`, `plan`, …); `None` leaves the CLI's default, which approves nothing |
| `allowed_tools` | `[]` | Tool patterns granted without asking, e.g. `["Read", "Bash(git *)"]` |
| `disallowed_tools` | `[]` | Tool patterns denied outright, applied over `allowed_tools` |
| `append_system_prompt` | `None` | Extra instructions appended to the CLI's own system prompt |
| `max_budget_usd` | `None` | Ceiling on what one run may spend on API calls, enforced by the CLI |
| `settle_timeout_sec` | `900.0` | Ceiling on one run; an agent loop has no natural bound |

The default permission mode approves nothing, so a task that must edit files needs
`permission_mode = "acceptEdits"` or an explicit `allowed_tools`. That is a
deliberate decision to make: this handler runs an agent with write access to the
project.

Unlike pi, Claude Code does not ask the client questions in this mode — a tool call
it cannot get approved is denied, and the agent is told and carries on. So a run
that **settled** despite a denial is reported as `settled` (the agent found another
way), while a run that **failed** with a denial recorded is `refused_interaction`:
the run needed a decision this setup was configured not to make. The transcript is
persisted; the handler logs the session id so `claude --resume <id>` can show what
the agent actually did.

---

## `dump_config`

Dump the resolved configuration for a source artifact that includes FineCode configuration.

- **Source:** `fine_envs.DumpConfigAction`

Handlers run in order: `dump_config` renders the dump, `dump_config_format`
runs the rendered content through `format_file` for the target file, and
`dump_config_save` writes the result once. When no formatter covers the target
file — no `format_file` action, no subactions, or none for the file's language
— the dump is still written unformatted, and the result's `unhandled` names the
target file, per the *Unhandled inputs* note at the top of this page. A
formatter that fails fails the run; disable the `dump_config_format` handler to
write the dump unformatted.

Also available as `python -m finecode dump-config`.

---

## `init_repository_provider`

**Optional, dynamic-runtime-seeding only** — not a required setup step. Static
provisioning of registry definitions and credentials for
`IRepositoryCredentialsProvider` is `[[tool.finecode.service]]` config,
resolved at Extension Runner bootstrap with no init step; see
[Provisioning `IRepositoryCredentialsProvider`](services.md#provisioning-irepositorycredentialsprovider)
and [ADR-0068](../../../finecode_internal_docs/adr/0068-service-provisioning-belongs-to-implementation-not-interface.md).
This action remains only for a consumer that must push credentials in *at run
time* (e.g. a token fetched or rotated mid-session) — nothing in this repo's
default presets exercises that path today; it is registered by
`fine_dist_artifacts` for opt-in use.

- **Source:** `fine_dist_artifacts.InitRepositoryProviderAction`

**Payload fields:**

| Field | Type | Description |
|---|---|---|
| `repositories` | `list[Repository]` | Registries to register, addressed by name |
| `credentials_by_repository` | `dict[str, RepositoryCredentials]` | Credentials keyed by repository name |

`Repository` fields:

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Name the publishing actions refer to this registry by, e.g. `"pypi"` |
| `index_url` | `str` | Index (read) endpoint, e.g. `https://pypi.org/simple/` |
| `upload_url` | `str` | Upload (write) endpoint, e.g. `https://upload.pypi.org/legacy/` |

`RepositoryCredentials` fields:

| Field | Type | Description |
|---|---|---|
| `username` | `str` | Username, or `__token__` when authenticating with an API token |
| `password` | `str` | Password or API token |

### Why two URLs

Reading a registry's index and uploading to it are separate APIs. Most
registries (Artifactory, Nexus, devpi, GitLab) serve both from one host and
differ only by path, but **PyPI splits them across hosts**: the index is on
`pypi.org` and uploads go to `upload.pypi.org`. Both endpoints are therefore
configured explicitly and neither is derived from the other. TestPyPI serves
both roles from `test.pypi.org`, so its two values share a host.

The two fields are complete URLs, but they are not used the same way:

- `index_url` is a **prefix** — the package being looked up is appended to it,
  so `https://pypi.org/simple/` is queried as `https://pypi.org/simple/<package>/`.
  A trailing slash is optional.
- `upload_url` is **terminal** — it is the endpoint itself, used verbatim.

Both match what the ecosystem's own tools already use: `index_url` is pip's
`index-url`, `upload_url` is twine's `repository` (the value in `.pypirc`).

Pointing a role at the wrong host is rejected with a message naming the right
one, rather than failing later as an opaque 404.

**Example** — `finecode_dev_common_preset` provisions the same registry
definitions this action's payload shape describes, but as service config
rather than a handler binding for this action:

```toml
[[tool.finecode.service]]
interface = "finecode_extension_api.interfaces.irepositorycredentialsprovider.IRepositoryCredentialsProvider"
source = "finecode_extension_runner.impls.repository_credentials_provider.ConfigRepositoryCredentialsProvider"
env = "dev_no_runtime"
config.repositories = [
    { name = "pypi", index_url = "https://pypi.org/simple/", upload_url = "https://upload.pypi.org/legacy/" },
    { name = "testpypi", index_url = "https://test.pypi.org/simple/", upload_url = "https://test.pypi.org/legacy/" },
]
```

See [Provisioning `IRepositoryCredentialsProvider`](services.md#provisioning-irepositorycredentialsprovider)
for where credentials go.

---

## `ingest_wal_to_store`

Ingest write-ahead-log events from one or more generic sources into a durable store.

- **Source:** `fine_wal_events.IngestWalToStoreAction`


**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `source_specs` | `list[WalSourceSpec]` | | Source definitions to ingest from |
| `since_ts_iso` | `str \| None` | `None` | Ignore events older than this ISO8601 UTC timestamp |
| `store_uri` | `ResourceUri \| None` | `None` | Destination store URI. If omitted, handler chooses a default path |

`WalSourceSpec` fields:

| Field | Type | Default | Description |
|---|---|---|---|
| `source_id` | `str` | | Stable source identifier used in summaries |
| `format` | `str` | | Source format, e.g. `jsonl_events` |
| `location_uri` | `ResourceUri` | | File or directory URI containing events |
| `include_glob` | `str \| None` | `None` | Include pattern for directory scanning |
| `exclude_glob` | `str \| None` | `None` | Exclude pattern for directory scanning |
| `field_mapping` | `dict[str, str] \| None` | `None` | Optional canonical-to-source mapping (`ts`, `event_type`, `run_id`, `action_name`, `payload`) |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `schema_version` | `int` | Result schema version |
| `source_summary` | `list[SourceIngestSummary]` | Per-source ingest counters |
| `events_ingested` | `int` | Successfully inserted event count |
| `events_skipped_duplicate` | `int` | Duplicate event count |
| `events_failed_parse` | `int` | Parse/normalization failure count |
| `first_event_ts_iso` | `str \| None` | Earliest inserted event timestamp |
| `last_event_ts_iso` | `str \| None` | Latest inserted event timestamp |
| `store_uri` | `ResourceUri` | Final store URI |
| `warnings` | `list[str]` | Non-fatal ingest warnings |

---

## `serve_wal_explorer_from_store` (extension action)

Start a read-only HTTP API over the WAL DuckDB store and serve until interrupted.

- **Source:** `fine_wal_events.ServeWalExplorerFromStoreAction`
- **Default handler execution:** sequential

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `store_uri` | `ResourceUri \| None` | `None` | Path to the DuckDB store. Defaults to `<venv>/state/finecode/wal_explorer/store.duckdb`. |
| `host` | `str` | `"127.0.0.1"` | Interface to bind the HTTP server to. |
| `port` | `int` | `8765` | Port number. If the default port is already occupied, the handler auto-selects a free port. |
| `read_only` | `bool` | `True` | Open the DuckDB store in read-only mode. |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `schema_version` | `int` | Store schema version. |
| `base_url` | `str` | Full base URL the server is listening on. |
| `bound_host` | `str` | Resolved host after binding. |
| `bound_port` | `int` | Resolved port after binding. |
| `store_uri` | `ResourceUri` | Resolved store path used. |
| `warnings` | `list[str]` | Non-fatal warnings. |

**Endpoints:**

| Path | Description |
|---|---|
| `GET /health` | Server status, schema version, event/run totals. |
| `GET /runs` | Run summaries. Query params: `source_id`, `from_ts`, `to_ts`, `limit`. |
| `GET /timeline` | Ordered event stream. Query params: `run_id`, `source_id`, `from_ts`, `to_ts`, `event_type`, `limit`. |
| `GET /metrics` | Aggregate counters and duration percentiles. |
| `GET /events` | Raw event rows. Query params: `run_id`, `source_id`, `from_ts`, `to_ts`, `limit`. |

The action runs until its invocation is cancelled. Cancellation triggers deterministic cleanup: HTTP server shutdown and DuckDB connection close.

---

## `release_workspace_packages`

Release every workspace package whose declared version is absent from its
registry, in dependency order (PRD-0006). Owns only what is cross-package:
candidate discovery, ordering, dependent blocking, and publishing the refs a run
produced. Per-package work is delegated to each package's own `release_package`
chain (ADR-0065). Composes `get_src_artifact_version`, `fine_dep_graph`'s
dependency graph actions, `release_package`, and `fine_git`'s `push_git_refs` —
it does not reimplement any of them.

- **Source:** `fine_release.ReleaseWorkspacePackagesAction`
- **Scope:** workspace
- **Default handler execution:** sequential (discovery → ordering → sweep, sharing `run_context.state`)

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | Preview mode: never calls build/publish/verify/tag/push. |
| `project_paths` | `list[ResourceUri] \| None` | `None` | `None` releases every actionable workspace project; otherwise restricts the candidate set. |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `dry_run` | `bool` | Echoes the payload. |
| `packages` | `list[PackageReleaseResult]` | One entry per candidate, in the dependency order used. |
| `error` | `str \| None` | A run-level failure not attributable to a single package — for example a push that did not reach the remote. Per-package failures are carried by each package's own `outcome`. |

The run's return code is `ERROR` when any package's outcome is `FAILED` or a
run-level `error` is set. A failed publish blocks only its transitive dependents
(`BLOCKED`, ADR-0062); a dependency cycle among candidates fails the whole action
before any package is touched. Refs created by the released packages are pushed
once, after the sweep, and unconditionally — a package that failed never discards
the records of packages that succeeded. A push that never reaches the remote sets
`error` and fails the run (ADR-0060), so lost refs surface in CI instead of hiding
behind a green result; the reconciling per-package tag step re-offers every ref on
the next run, so the push is retried.

---

## `release_package`

Release one package: build it, publish and verify it to every configured
registry, and record the publish as a git tag. This is the chain a package
*owns* — a package inserts, replaces or drops a step by editing its handler
list, without touching the workspace release (ADR-0065). Composes
`build_artifact`, `get_src_artifact_registries`, `list_published_artifacts`,
`publish_and_verify_artifact` and `create_git_tag`.

- **Source:** `fine_release.ReleasePackageAction`
- **Scope:** project
- **Default handler execution:** sequential (build → publish → record tag, sharing `run_context.state`)

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `package_name` | `str` | — | Used for the tag name and diagnostics. |
| `version` | `str` | — | The declared version being released. |
| `src_artifact_def_path` | `ResourceUri \| None` | `None` | `None` uses the receiving project's own artifact definition. |
| `dry_run` | `bool` | `False` | Preview mode: resolves registries and reports what would happen, without building, publishing or tagging. |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `outcome` | `PackageReleaseOutcome` | Never `BLOCKED` — only the workspace orchestrator produces that. |
| `registries` | `list[RegistryPublishResult]` | One entry per configured registry; empty when the release ended before any registry outcome was determined. |
| `created_refs` | `list[str]` | Refs created for this package. The caller publishes them; this action never pushes. |
| `error` | `str \| None` | Non-registry failure (build, registry resolution, a publish that raised before dispatch, or a tag that failed to record after a successful publish). |

Handlers record failures in `run_context.state.error` rather than raising, so a
failed release returns a `FAILED` result carrying the failing step's own
message. Every handler returns early while that error is set, so a failed build
is never published or tagged. Per-registry outcomes are never fabricated for
registries nothing was attempted against.

The tag step is a reconciling record, not a publication gate (ADR-0060): it never
causes a re-publish, but a tag that fails to write is recorded in `error` and so
fails the release (non-zero return code), rather than being silently logged — a
lost tag would otherwise hide behind a green publish. The registry entry stays
`PUBLISHED` in `registries[]` for traceability. Recording is idempotent and
reconciling — the tag is (re)attempted whenever the version is present in a
registry, published this run or already there — so a tag that failed on an
earlier run is retried on the next run.

---

## `create_git_tag` (`fine_git`)

Create a git tag. Generic, no release semantics — idempotent no-op if the tag
already exists; a git failure is reported (`created=False`, `error=<stderr>`),
never raised.

- **Source:** `fine_git.CreateGitTagAction`

---

## `push_git_refs` (`fine_git`)

Push git refs to a remote. Generic, no release semantics — a push failure is
reported (`pushed_refs=[]`, `error=<stderr>`), never raised.

- **Source:** `fine_git.PushGitRefsAction`

---

## `get_git_status` (`fine_git`)

Report the git status of paths in the project. Both porcelain columns
(index vs HEAD, worktree vs index) are reported for every path rather than
collapsing them into a single `staged` boolean, so a caller can project
whichever view it needs — the staged set, the dirty set, the untracked set —
from one complete answer. `repo_root=None` means the project is not inside a
git repository, which is a result state, not an error.

- **Source:** `fine_git.GetGitStatusAction`

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `paths` | `list[ResourceUri] \| None` | `None` | `None` -> the whole project directory. Empty list -> nothing requested, the result is empty |
| `include_untracked` | `bool` | `True` | |
| `include_ignored` | `bool` | `False` | |

---

## `get_git_diff` (`fine_git`)

Get the git diff of paths in the project. Each file carries both the
verbatim `patch` (for a human or a model reviewing the change) and the
parsed `added_lines`/`removed_lines` (so mechanical consumers do not each
have to write their own unified-diff parser) — both are produced for every
file, one answer in two projections.

- **Source:** `fine_git.GetGitDiffAction`

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `paths` | `list[ResourceUri] \| None` | `None` | `None` -> the whole project directory. Empty list -> nothing requested |
| `source` | `GitDiffSource` (`worktree`\|`staged`\|`worktree_and_head`) | `worktree` | Which comparison to diff: unstaged only, staged only, or both vs `HEAD` |
| `context_lines` | `int` | `3` | Lines of context around each hunk; `0` yields hunks with only changed lines |

---

## `restore_git_files` (`fine_git`)

Restore files to their committed state, discarding local changes. `paths`
is required with no wildcard for "restore everything", since this action
destroys uncommitted work; a path outside the project directory is refused
and reported in `skipped`, never restored; and deleting an untracked path
is opt-in via `remove_untracked`, off by default because it is
unrecoverable.

- **Source:** `fine_git.RestoreGitFilesAction`

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `paths` | `list[ResourceUri]` | | Required, no wildcard |
| `target` | `GitRestoreTarget` (`worktree`\|`index`\|`both`) | `worktree` | |
| `source_ref` | `str` | `"HEAD"` | The commit to restore file content from |
| `remove_untracked` | `bool` | `False` | Delete listed paths that are untracked; unrecoverable |

---

## `list_tasks` (`fine_tasks`)

List tasks across configured task providers (e.g. GitHub issues). A task is
identified by its URL (`https://github.com/owner/repo/issues/42`), never a
provider-opaque id or `owner/repo#42` shorthand.

- **Source:** `fine_tasks.ListTasksAction`
- **Scope:** `workspace` — several projects can share one repository
  configuration, so per-project dispatch would duplicate results (R-108
  trigger (a))
- **Default handler execution:** concurrent

No handlers are registered by default — this preset ships the contract only.
Register a provider handler (e.g. a future `fine_github_tasks`) to make the
action runnable.

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `assignee` | `str` | `"@me"` | `"@me"` resolves to the authenticated user; `"@any"` disables the filter; anything else is a provider username |
| `state` | `TaskState` (`open`\|`closed`\|`all`) | `open` | |
| `status` | `list[str]` | `[]` | Canonical status names to filter by. Empty means no status filtering, not "found nothing" |
| `repositories` | `list[str]` | `[]` | Empty means use handler config. A non-empty list overrides it, for ad-hoc queries |
| `sort` | `TaskSort` (`updated_desc`\|`created_desc`) | `updated_desc` | |
| `limit_per_source` | `int` | `50` | Maximum tasks fetched per source (one repository of one provider); a query over three repositories may return up to three times this number |
| `page` | `dict[str, str]` | `{}` | Cursors from a previous result's `next_page`. Empty means the first page. A handler reads only the keys for sources it owns |
| `include_pull_requests` | `bool` | `False` | GitHub's issues endpoint returns pull requests as issues; excluded by default. Providers without that conflation ignore this field |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `tasks` | `list[TaskSummary]` | |
| `errors` | `list[ProviderError]` | |
| `has_more` | `bool` | Stored field, not derived from `next_page` |
| `next_page` | `dict[str, str]` | Keys are `"{provider}:{repository}"`; values are opaque and provider-defined |

`update()` extends `tasks` and `errors`, ORs `has_more`, and merges
`next_page`. `return_code` is `ERROR` whenever `errors` is non-empty — a
partial list plus a non-success code is the intended combination, so
`to_text()` leads with the tasks and puts failures after them.

---

## `get_task` (`fine_tasks`)

Get one task by its URL.

- **Source:** `fine_tasks.GetTaskAction`
- **Scope:** `workspace` — the lookup must execute exactly once per
  invocation (R-108 trigger (d))

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `task_url` | `str` | | Task identity, e.g. `https://github.com/owner/repo/issues/42` |
| `include_comments` | `bool` | `True` | |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `task` | `Task \| None` | `None` means no registered handler owns `task_url` |

`return_code` is `ERROR` when `task` is `None`.

---

## `add_task_comment` (`fine_tasks`)

Post a comment to a task.

- **Source:** `fine_tasks.AddTaskCommentAction`
- **Scope:** `workspace` — posting a comment is a single external effect;
  per-project dispatch would post N duplicate comments (R-108 trigger (d))

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `task_url` | `str` | | Task identity |
| `body` | `str` | | |
| `attachments` | `list[Path]` | `[]` | Local files to attach to the comment |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `comment` | `TaskComment \| None` | `None` means no registered handler owns `task_url` |

`return_code` is `ERROR` when `comment` is `None`.

---

## `edit_task_comment` (`fine_tasks`)

Edit an existing task comment, identified by its permalink.

- **Source:** `fine_tasks.EditTaskCommentAction`
- **Scope:** `workspace` — editing must execute exactly once; per-project
  dispatch would rewrite the same comment N times (R-108 trigger (d))

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `comment_url` | `str` | | Comment identity, its permalink, e.g. `https://github.com/owner/repo/issues/42#issuecomment-123` |
| `body` | `str` | | |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `comment` | `TaskComment \| None` | `None` means no registered handler owns `comment_url` |

`return_code` is `ERROR` when `comment` is `None`.

---

## `update_task_status` (`fine_tasks`)

Update a task's canonical status.

- **Source:** `fine_tasks.UpdateTaskStatusAction`
- **Scope:** `workspace` — the transition must execute exactly once;
  per-project dispatch would attempt it N times (R-108 trigger (d))

**Payload fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `task_url` | `str` | | Task identity |
| `status` | `str` | | Canonical status name; handler config maps it to the provider's representation |

**Result fields:**

| Field | Type | Description |
|---|---|---|
| `task` | `TaskSummary \| None` | `None` means no registered handler owns `task_url` |

`return_code` is `ERROR` when `task` is `None`.
