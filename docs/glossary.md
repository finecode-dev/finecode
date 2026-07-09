# Glossary

## Action

A named operation (for example `lint`, `format`, `build_artifact`).

## Action Handler

A concrete implementation of an action. Multiple handlers can be registered for a single action, and they run sequentially or concurrently.

## Collection Action

An action whose payload carries multiple items and whose result describes that batch, often with per-item entries. Used when handlers need to iterate over, correlate, or optimize across the full set of items.

## Concrete Environment

One of the per-toolchain execution environments a matrix environment expands into (for example `testing@cpython-3.11`). Each is an ordinary execution environment bound to a single toolchain — in Python, a single interpreter — and materialized as its own virtual environment.

## Main Handler Action

When both an item action and a collection action exist for the same operation, the main handler action is the one where the main handler logic naturally lives. The other action mainly adapts to it. This term is for action design, not for telling callers which action they should prefer in every situation.

## Execution Environment

A named, isolated context in which handlers and project code execute (e.g. `runtime`, `dev_workspace`, `dev_no_runtime`). Each execution environment has its own dependency set, serving a specific purpose — for example, the project's runtime, dev tooling, or test execution. The concept is inter-language; in Python each execution environment is materialized as a virtual environment. Configuration uses the shorthand `env`.

## Extension Runner (ER)

A process that runs inside a specific execution environment and executes action handler code. The Workspace Manager spawns one ER per (project, execution environment) pair, on demand. ERs communicate with the WM over JSON-RPC. The concept is inter-language — `finecode_extension_runner` is the Python implementation.

## Interpreter

The Python materialization of a [Toolchain](#toolchain): a specific Python interpreter identified by its implementation and version together (for example CPython 3.11, PyPy 3.11). Two interpreters that share a version but differ in implementation are distinct; identity is never reduced to a bare version number.

## Item Action

An action whose payload carries one item and whose result describes that one item. Used when handlers naturally operate on a single unit of work.

## Matrix Environment

An execution environment that declares a [toolchain matrix](#toolchain-matrix). It is not run directly; at configuration-resolution time it expands into one concrete environment per toolchain. Handlers and services attached to it apply to every concrete environment.

## Matrixed Action

An action whose handlers run in a matrix environment, and therefore execute once per toolchain. It returns a variant-keyed result rather than a single one. An action whose handlers all run in ordinary (single-toolchain) environments is not matrixed and is unaffected.

## Preset

A reusable, distributable bundle of action and handler declarations. Users reference a preset in their project configuration; its declarations merge with the project's own configuration, giving full control to override or disable individual handlers. The concept is inter-language — in Python, presets are distributed as packages installed into the `dev_workspace` execution environment.

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

A set of related source artifacts a developer is working on. Often this is a single directory root, but it can also be multiple directories (workspace roots). FineCode can run actions across all source artifacts that include FineCode configuration. (Some CLI flags and protocol fields still use the word “project” for compatibility.)

## Workspace Manager (WM)

A long-running server that discovers source artifacts, resolves merged configuration, manages execution environments, exposes an LSP and MCP API to clients, and delegates action execution to Extension Runners. Typically one shared WM instance runs per virtual environment.
