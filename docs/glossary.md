# Glossary

## Action

A named operation (for example `lint`, `format`, `build_artifact`).

## Action Handler

A concrete implementation of an action. Multiple handlers can be registered for a single action, and they run sequentially or concurrently.

## Coherent

Every requirement on a [workspace package](#workspace-package) reached by the workspace's own requirements is satisfied by its workspace source. This is an operational requirement check; it does not prove general software compatibility.

## Collection Action

An action whose payload carries multiple items and whose result describes that batch, often with per-item entries. Used when handlers need to iterate over, correlate, or optimize across the full set of items.

## Concrete Environment

One of the per-toolchain execution environments a matrix environment expands into (for example `testing@cpython-3.11`). Each is an ordinary execution environment bound to a single toolchain — in Python, a single interpreter — and materialized as its own virtual environment.

## Main Handler Action

When both an item action and a collection action exist for the same operation, the main handler action is the one where the main handler logic naturally lives. The other action mainly adapts to it. This term is for action design, not for telling callers which action they should prefer in every situation.

## Execution Environment

A named, isolated context in which handlers and project code execute (e.g. `runtime`, `dev_workspace`, `dev_no_runtime`). Each execution environment has its own dependency set, serving a specific purpose — for example, the project's runtime, dev tooling, or test execution. The concept is inter-language; in Python each execution environment is materialized as a virtual environment. Configuration uses the shorthand `env`. A `dev_workspace` is a project's named execution environment containing preparation packages and presets. The workspace root and each subproject can each have one.

## Extension

A distributable capability package: handlers, services, action contracts, and explicitly selected reusable configuration.

## Extension Runner (ER)

A process that runs inside a specific execution environment and executes action handler code. The Workspace Manager spawns one ER per (project, execution environment) pair, on demand. ERs communicate with the WM over JSON-RPC. The concept is inter-language — `finecode_extension_runner` is the Python implementation.

## External Dependency

A package outside the declared [workspace package](#workspace-package) set, including a published release of a component of the same product. See [Product](#product).

## Interpreter

The Python materialization of a [Toolchain](#toolchain): a specific Python interpreter identified by its implementation and version together (for example CPython 3.11, PyPy 3.11). Two interpreters that share a version but differ in implementation are distinct; identity is never reduced to a bare version number.

## Item Action

An action whose payload carries one item and whose result describes that one item. Used when handlers naturally operate on a single unit of work.

## Matrix Environment

An execution environment that declares a [toolchain matrix](#toolchain-matrix). It is not run directly; at configuration-resolution time it expands into one concrete environment per toolchain. Handlers and services attached to it apply to every concrete environment.

## Matrixed Action

An action whose handlers run in a matrix environment, and therefore execute once per toolchain. It returns a variant-keyed result rather than a single one. An action whose handlers all run in ordinary (single-toolchain) environments is not matrixed and is unaffected.

## Preset

Reusable configuration supplied by an extension package. Users reference a preset in their project configuration; its declarations merge with the project's own configuration, giving full control to override or disable individual handlers.The concept is inter-language — in Python, presets are distributed as packages installed into the `dev_workspace` execution environment.

## Product

The software developed through one or more workspaces. A component is a [source artifact](#source-artifact) of that product. FineCode does not model or act on the relation between those workspaces.

## Service

A long-lived dependency injected into handlers by interface.

## Source Artifact

A unit of source code that build/publish-style actions operate on. It is identified by a **source artifact definition file** (for example `pyproject.toml` or `package.json`). This is what many tools call a “project”, but FineCode uses **source artifact** to be more concrete.

## Source Artifact Definition

The definition file for a source artifact (for example content of `pyproject.toml`).

## Toolchain

The language-specific implementation-and-version that project code is built or executed against, identified by implementation and version together and never reduced to a bare version number. Toolchains are the dimension a [matrix environment](#matrix-environment) expands over. The concept is inter-language: in Python a toolchain is an [interpreter](#interpreter) (for example CPython 3.11 or PyPy 3.11); in other ecosystems it is the analogous runtime or compiler toolchain (for example a Rust toolchain, a Node runtime, or a JDK).

## Toolchain Matrix

The set of toolchains an execution environment is declared to run against. In Python, for example, CPython 3.11 and 3.12 plus PyPy 3.11, declared with `interpreters` on the environment. Analogous to a Hatch environment matrix or a CI build matrix.

## Variant

One toolchain's slice of a matrixed action: its run in a single concrete environment and the result entry attributed to that toolchain. A matrixed action's result is keyed by variant.

## Virtual Environment

The Python-specific materialization of an execution environment. FineCode creates one virtual environment per environment name per project at `.venvs/{env_name}/` and installs the declared handler dependencies into it. Created by `prepare-envs`.

## Workspace

A development context for working with the current package source versions of a checkout. A workspace ("development workspace") has exactly one root — the directory the WM is anchored to — its own environments, and one current [workspace state](#workspace-state). It may have several workspace directories, including the root; additional registered directories do not become additional roots of that workspace. Opened at a checkout's root, a workspace normally represents that checkout. FineCode can run actions across all source artifacts that include FineCode configuration. The root does not require a workspace config file to exist, and no upward search selects the root. (Some CLI flags and protocol fields still use the word “project” for compatibility.)

## Workspace Manager (WM)

A long-running server that discovers source artifacts, resolves merged configuration, manages execution environments, exposes an LSP and MCP API to clients, and delegates action execution to Extension Runners. Typically one shared WM instance runs per virtual environment.

## Workspace Package

A source artifact declared by the workspace root's configuration as belonging to the [workspace](#workspace). By default every discovered named project belongs; `extra` adds explicit paths, including forks not discovered through FineCode configuration. Membership is declaration-based, not physical containment. See [External Dependency](#external-dependency) and [Workspace State](#workspace-state).

## Workspace State

A workspace state describes the package source versions you are working with at a given moment, together with which packages are declared as [workspace packages](#workspace-package). Membership is part of the state because different releases can contain different packages. For a package with a dynamic version, its source version is the version its build backend computes from the checkout.
