# Changelog

## 0.2.0a0

`create_envs` prepares one project config dump per project for every nonempty batch, mixed batches included, and lends each env its project's file through the `create_env` payload field `resolved_project_def_path`. There is no setting. Any project's preparation failure fails the batch naming that project. The run directory holding one subdirectory per project is removed after every started child ends; when the delegated-calls read is unconfirmed the run logs one WARNING naming the batch's projects.

`[project.optional-dependencies]` is now resolved by the installer; extras named on a dependency spec are installed. Previously ignored.

The `dump-config` text layout changed and the parsed content did not: `tomli-w` replaces `tomlkit` as a dependency.
