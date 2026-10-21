# fine_agent_codex

Installs the [OpenAI Codex CLI](https://github.com/openai/codex) (`codex`)
through FineCode's `setup_system` action, next to the pi and Claude Code
installers.

## What the handler does

`InstallCodexHandler` checks whether `codex` is on `PATH`:

- Present → reports `skipped: ["codex"]` and runs nothing.
- Absent → runs `npm install -g --ignore-scripts @openai/codex` once and
  reports `installed: ["codex"]` on success, `failed: ["codex: <npm error>"]`
  otherwise.

What it never does:

- **No upgrade.** A present `codex` is left alone, even if outdated. See
  [Updating](#updating) for how to upgrade manually.
- **No authentication.** Signing in stays outside `setup_system`, which runs
  non-interactively. See [Authentication](#authentication).

## Supported platforms

Platform selection is npm's job: `@openai/codex` ships its native binary as
optional dependencies per OS/architecture, and `--ignore-scripts` still
installs those (the package declares no lifecycle scripts, so skipping them
only avoids running arbitrary code from the dependency tree). The six
targets are:

- linux-x64, linux-arm64
- darwin-x64 (Intel), darwin-arm64 (Apple silicon)
- win32-x64, win32-arm64

## Prerequisites

- Node.js **≥ 16** with `npm` on `PATH` (`engines.node >= 16` per the
  package metadata; no separate version check — npm itself reports a
  too-old Node).
- A globally writable npm prefix (nvm, or `npm config set prefix`), so the
  global install does not fail with `EACCES`.

Without `npm` on `PATH` the step fails with
`codex: npm not found in PATH, install Node.js first` and spawns nothing.

## Updating

The handler never upgrades. To update a previously installed CLI:

```bash
codex update
```

or, equivalently:

```bash
npm install -g @openai/codex@latest
```

## Authentication

After install, sign in once (outside FineCode):

```bash
codex login            # browser sign-in
printenv OPENAI_API_KEY | codex login --with-api-key
codex login status     # verify
```

## Registration

Register the handler in the root project's `setup_system` handler list,
after `install_pi`:

```toml
{ name = "install_codex", source = "fine_agent_codex.InstallCodexHandler", env = "dev_workspace", dependencies = ["fine_agent_codex~=0.1.0"] },
```

## Running agent tasks (opt-in backend)

`CodexAgentHandler` runs `fine_agent`'s `run_agent_task` with `codex exec` in
non-interactive mode: one fresh process per run, native JSONL progress, and
native `--output-schema` structured output. It lives in this package next to
the installer above; install and authentication stay prerequisites, not part
of the run.

```toml
[tool.finecode.action.run_agent_task]
handlers_mode = "replace"
handlers = [
  { name = "codex_agent", source = "fine_agent_codex.CodexAgentHandler", env = "dev_no_runtime", dependencies = [
    "fine_agent_codex~=0.1.0",
  ] },
]

[tool.finecode.action.run_agent_task.handlers.codex_agent]
config.sandbox_mode = "workspace-write"  # the default is read-only
```

Options (`config.`): `model` (`None` leaves the CLI's default),
`sandbox_mode` (`read-only` by default), `skip_git_repo_check` (`false`),
`settle_timeout_sec` (`900.0`), and a `profiles` map of named `{ model,
settle_timeout_sec }` overrides that inherit whatever they leave unset. A
profile can never widen the sandbox: `sandbox_mode` stays top-level.

Explicit limitations, not missing features: the handler never reports
`refused_interaction` (exec has no interactive channel and no documented
denial signal, so unapproved work fails as `failed`); `turns` is always `None`
(an exec request is not a count of internal turns); usage carries only the
counters the CLI reported, never derived totals or prices. Cancellation
re-raises instead of returning a result. No live Codex process runs as part
of the normal tests — they drive a fake CLI over real pipes.
