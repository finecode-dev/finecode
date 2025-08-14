# FineCode

**Status Update (04.06.2025):** FineCode is available for use, but the current version is intended only for personal testing, not for production use, as we actively work on improving execution environments, which will undergo significant changes. Initial release with new execution environments is expected in July 2025.

## Personalize and improve your development experience

FineCode is a tool runner and set of utilities for creating tools for software developers.

With FineCode you can:

- make tool configuration in your project reusable and distributable(see Presets below)
- improve integration of tools used in the project with IDE, especially in workspace setup
- create your own tools with IDE integration out of the box, and IDE extensios as well

## Getting started: example how to setup linting and formatting in your project

1.1 Install FineCode. We recommend using dependency groups ([PEP-735](https://peps.python.org/pep-0735/)) but not all package managers support them. We will show a recommended way with which you don't need package manager at all, only `pip`, but you can adapt it also for package manager used in your project.

1.1.1 Add `dev_workspace` dependency group to `pyproject.toml`:

  ```toml
  [dependency-groups]
  dev_workspace = ["finecode==0.2.*"]
  ```

1.1.2 Create dev_workspace venv: `python -m venv .venvs/dev_workspace` ([Python Docs](https://docs.python.org/3/library/venv.html#creating-virtual-environments ))

1.1.3 Activate this venv and install dependencies from `dev_workspace` group:
  ```
  source .venvs/dev_workspace/bin/activate
  python -m pip install --group="dev_workspace"
  ```

NOTE: `pip install` supports `--group` parameter since pip 25.1. Make sure you have new enough version, otherwise update it: `python -m pip install --upgrade pip`.

1.2 Using existing preset

1.2.1 Add `dev_no_runtime` dependency group and put `fine_python_recommended` dependency in it:

  ```toml
  [dependency-groups]
  dev_workspace = ["finecode==0.2.*"]
  dev_no_runtime = ["fine_python_recommended==0.1.*"]
  ```

For list of presets from FineCode authors see 'Presets' section below.

1.2.1 Run `prepare_envs` finecode command:

  ```bash
  python -m finecode prepare_envs
  ```

1.3 Enable finecode and preset

```toml
[tool.finecode]
presets = [
    { source = "fine_python_recommended" }
]
```

1.4 That's it! Now you can lint and format your codebase by running `lint` and `format` actions(see 'IDE Integration' and 'CLI' sections for more details).

You can customize preset, create your own, implement own actions, action handlers and more. All these possibilities are explained in details below.

## IDE Integration

### VSCode

For integration with VSCode, install [FineCode extension](https://github.com/finecode-dev/finecode-vscode)

## CLI

1. In virtualenv of your project you can use the following command:

`python -m finecode run [run_options] <list_of_actions> [actions_payload]`

Available run options:

- `--workdir="<path>"` ... use provided directory as work directory
- `--project="<name>"` ... run actions only in this project. Multiple projects can be selected by providing multiple `--project="<name>"` options
- `--concurrently` ... run actions concurrently. Single projects are always handled concurrently, this option determines whether actions inside of single project are run concurrently or not
- `--trace` ... activate trace(more detailed) logging

If no projects are provided via options, FineCode will interpret working directory as workspace root, find all projects in it and run provided actions in all projects, in which they exist.

If projects are provided, actions are expected to exist in all of them.

Actions payload: if actions require payload or you want to run them with payload other than configured, you can provide it after names of actions.

Examples:

- `python -m finecode run --concurrently lint check_formatting` ... run `lint` and `check_formatting` actions concurrently in all projects in the workspace, root of which is in current working directory
- `python -m finecode --workdir="./finecode_extension_api" run lint check_formatting` ... run `lint` and `check_formatting` sequentially in `finecode_extension_api` directory (project is there)
- `python -m finecode --project="fine_python_mypy" --project="fine_python_ruff" run lint` ... run `lint` action in projects `fine_python_mypy` and `fine_python_ruff`. They should be discoverable from the working directory.

2. You can dump project config with fully resolved configuration using following command:

`python -m finecode dump-config [--trace] [--project="<project_name>"]`

The result will be saved in `<cwd>/finecode_config_dump/` directory.

Options:

- `--trace` ... activate trace(more detailed) logging
- `--project` ... by default config of project in current working directory is dumped. If current working directory contains multiple projects and you want to dump config of particular one, provide its name using this option

## Extensions from FineCode authors

### Presets

- fine_python_recommended
- fine_python_format
- fine_python_lint

### Actions and action handlers

[Directory with actions](https://github.com/finecode-dev/finecode/tree/main/finecode_extension_api/finecode_extension_api/actions)

- lint
  - Flake8
  - Ruff
  - Mypy
- format
  - Black
  - isort

IDE

TODO: list all from LSP

## Workspace with multiple subprojects

### Reusing config

To reuse configuration in multiple subprojects, put it in a separate package in your workspace and add it as dev dependency in all subprojects in which you want to use it.

Design decision: there are multiple ways to achieve the same result:

- separate package
  - configuration of subprojects doesn't depend on file structure of the workspace. Subproject can be moved in another place or even outside of workspace and this will not affect its configuration, only if path to package with common configuration was file path, it should be changed.
  - fully transparent: the full configuration is known in a subproject without searching workspace root and analyzing the workspace
- hierarchical configuration
  - makes subprojects more dependent on workspace, in case of moving subproject, additional actions with configurations are needed to keep it the same
- letting to define reusable part on workspace level and provide it automatically to all subprojects
  - not transparent, part of configuration is implicit
  - FineCode needs to check whether it was started in workspace or in subproject, go deeper in file tree and find workspace root to resolve all configurations
