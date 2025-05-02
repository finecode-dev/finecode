# FineCode

NOT INTENDED FOR EXTERNAL USE YET. CONFIGURATION AND API ARE UNSTABLE AND IN ACTIVE DEVELOPMENT.

The first beta release indended for public testing is planned in May 2025.

## Personalize and improve your development experience

FineCode is a tool runner and set of utilities for creating tools for software developers.

With FineCode you can:

- make tool configuration in your project reusable and distributable(see Presets below)
- improve integration of tools used in the project with IDE, especially in workspace setup
- create your own tools with IDE integration out of the box, and IDE extensios as well

## Getting started: example how to setup linting and formatting in your project

1.1 Install FineCode. The exact command depends on the package manager you are using.

    `pip install finecode`

1.2 Create `finecode.sh` in root of your project with path to python executable from virtualenv of the project. We recommend also to add it to .gitignore. Example:

    `.venv/bin/python`

1.3 Using existing preset

Install package with the preset, for example:

`pip install fine_python_recommended`

For list of presets from FineCode authors see 'Presets' section below.

1.4 Enable finecode and preset

```toml
[tool.finecode]
presets = [
    { source = "fine_python_recommended" }
]
```

1.5 For integration with VSCode, install [FineCode extension](https://github.com/finecode-dev/finecode-vscode)

## CLI

In virtualenv of your project you can use the following command:

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
