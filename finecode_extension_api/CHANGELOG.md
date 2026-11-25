# Changelog for `finecode_extension_api`

## 0.6.0 — BREAKING

Breaking changes (minor bump is breaking in `0.x`):

- `ICommandRunner.run()` is now a plain method returning an async context
  manager that owns its bounded process: `async with runner.run(...) as proc`.
  The scope stops whatever is still alive at exit, waits a bounded time,
  releases the work slot and local-cap permit, and propagates under the
  body's exception precedence. Entering the scope twice raises
  `RuntimeError`.
- `ICommandRunner.run()` and `run_to_completion()` default
  `new_process_group` to `True`: the child runs in a session of its own.
- Buffered output reads (`get_output`/`get_error_output`) on a bounded
  process raise `ProcessOutputIncompleteError` once the stream's drain failed
  or was abandoned, instead of returning a prefix.
- A program the OS cannot start raises `ProgramStartError` instead of
  `OSError` from every `ICommandRunner` spawning method (`run`,
  `run_to_completion`, `run_sync`, `start_long_running`). The original
  `OSError` is its `__cause__`, and `.errno`, `.strerror`, `.program` and
  `.cwd` are set. Replace `except OSError` (or `FileNotFoundError`) around a
  spawn with `except CommandStartError`.
- `CommandNotLaunchableError` (and so `UnsafeBatchArgumentError` and
  `UnlaunchableProgramError`) no longer subclasses `ValueError`; it is a
  `CommandStartError`. Replace `except (OSError, CommandNotLaunchableError)`
  with `except CommandStartError`.

Additions:

- `ICommandRunner.run_to_completion()` — run a command as one unit of work
  and return its finished process.
- `OwnedProcessNotStoppedError` (with `.pids`) and
  `ProcessOutputIncompleteError` (with `.stream`).
- `CommandStartError`, the base of every "the command could not be started"
  error, and its subtype `ProgramStartError`.
