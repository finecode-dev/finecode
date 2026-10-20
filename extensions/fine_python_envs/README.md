# fine_python_envs

Sources the Python interpreter axis of an environment from `requires-python`.

`fine_envs` owns the language-agnostic `sync_toolchains` / `check_toolchains` contracts.
This preset registers the Python implementation of them: `sync_python_interpreters`
expands the project's `requires-python` specifier into `cpython@<version>` identities and
materializes them as `interpreters` on the env table, so config resolution stays a pure
read of already-declared data.

Enable it, then name the envs whose axis should be derived:

```toml
[tool.finecode]
presets = [{ source = "fine_python_envs" }]

[[tool.finecode.action_handler]]
source = "fine_python_package_info.SyncPythonInterpretersHandler"
config.envs = ["testing"]
```

Run `sync_toolchains` to write the axis and `check_toolchains` to fail when it has gone
stale, the same way a lock file is checked. The staleness check does not need its own CI
step: registering `fine_envs.check_toolchains_audit_code_bridge_handler.CheckToolchainsAuditCodeBridgeHandler`
(with the `fine_envs[audit]` extra) reports drift as an `audit_code` diagnostic.

See ADR-0053 for why the derived axis is written to the file rather than computed on the
fly, and `docs/reference/actions.md` for the action contracts.
