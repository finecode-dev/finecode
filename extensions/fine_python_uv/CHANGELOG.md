# Changelog

## 0.2.0a0

`create_env` reads only `resolved_project_def_path`: with none it fails each env that needs creation and keeps the venv. The D1.G13 cases and the start failure read the supplied file; the `action_runner`/`project_info_provider` dependencies are gone.

`[project.optional-dependencies]` is now resolved by the installer; extras named on a dependency spec are installed. Previously ignored.
