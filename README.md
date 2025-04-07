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
