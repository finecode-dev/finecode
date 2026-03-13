# Glossary

## Action

A named operation (for example `lint`, `format`, `build_artifact`).

## Action Handler

A concrete implementation of an action. Multiple handlers can be registered for a single action, and they run sequentially or concurrently.

## Source Artifact

A unit of source code that build/publish-style actions operate on. It is identified by a **source artifact definition file** (for example `pyproject.toml` or `package.json`). This is what many tools call a “project”, but FineCode uses **source artifact** to be more concrete. Some actions accept `src_artifact_def_path` so they can target any source artifact, not only those with FineCode configuration.

## Source Artifact Definition

The definition file for a source artifact (for example content of `pyproject.toml`).

## Workspace

A set of related source artifacts a developer is working on. Often this is a single directory root, but it can also be multiple directories (workspace roots). FineCode can run actions across all source artifacts that include FineCode configuration. (Some CLI flags and protocol fields still use the word “project” for compatibility.)
