# Changelog

## 0.2.0a0

`[project.optional-dependencies]` is now resolved by the installer; extras named on a dependency spec are installed. Previously ignored.

The `dump-config` text layout changed and the parsed content did not: `tomli-w` replaces `tomlkit` as a dependency.
