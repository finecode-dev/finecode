# Glossary

## Action

A named operation (for example `lint`, `format`, `build_artifact`).

## Action Handler

A concrete implementation of an action. Multiple handlers can be registered for a single action, and they run sequentially or concurrently.

## Execution Environment

A named, isolated context in which handlers and project code execute (e.g. `runtime`, `dev_workspace`, `dev_no_runtime`). Each execution environment has its own dependency set, serving a specific purpose — for example, the project's runtime, dev tooling, or test execution. The concept is inter-language; in Python each execution environment is materialized as a virtual environment. Configuration uses the shorthand `env`.

## Extension Runner (ER)

A process that runs inside a specific execution environment and executes action handler code. The Workspace Manager spawns one ER per (project, execution environment) pair, on demand. ERs communicate with the WM over JSON-RPC. The concept is inter-language — `finecode_extension_runner` is the Python implementation.

## Preset

A reusable, distributable bundle of action and handler declarations. Users reference a preset in their project configuration; its declarations merge with the project's own configuration, giving full control to override or disable individual handlers. The concept is inter-language — in Python, presets are distributed as packages installed into the `dev_workspace` execution environment.

## Service

A long-lived dependency injected into handlers by interface.

## Source Artifact

A unit of source code that build/publish-style actions operate on. It is identified by a **source artifact definition file** (for example `pyproject.toml` or `package.json`). This is what many tools call a “project”, but FineCode uses **source artifact** to be more concrete.

## Source Artifact Definition

The definition file for a source artifact (for example content of `pyproject.toml`).

## Virtual Environment

The Python-specific materialization of an execution environment. FineCode creates one virtual environment per environment name per project at `.venvs/{env_name}/` and installs the declared handler dependencies into it. Created by `prepare-envs`.

## Workspace

A set of related source artifacts a developer is working on. Often this is a single directory root, but it can also be multiple directories (workspace roots). FineCode can run actions across all source artifacts that include FineCode configuration. (Some CLI flags and protocol fields still use the word “project” for compatibility.)

## Workspace Manager (WM)

A long-running server that discovers source artifacts, resolves merged configuration, manages execution environments, exposes an LSP and MCP API to clients, and delegates action execution to Extension Runners. Typically one shared WM instance runs per virtual environment.
