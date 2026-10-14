# fine_agent

FineCode preset providing `run_agent_task` — delegate a task to an AI coding
agent and return its output.

This preset declares the action and **no handler**, so including it on its own is
a safe no-op. A backend extension supplies the implementation. Two exist today:

| Extension | Backend | Handler |
|---|---|---|
| [`fine_agent_pi`](../../extensions/fine_agent_pi) | the `pi` CLI, in its RPC mode | `fine_agent_pi.PiAgentHandler` |
| [`fine_agent_claude_code`](../../extensions/fine_agent_claude_code) | the `claude` CLI, in print mode | `fine_agent_claude_code.ClaudeCodeAgentHandler` |

```toml
[tool.finecode]
presets = [{ source = "fine_agent" }]

[tool.finecode.action.run_agent_task]
handlers = [
  { name = "pi_agent", source = "fine_agent_pi.PiAgentHandler", env = "dev_no_runtime", dependencies = [
    "fine_agent_pi~=0.1.0a0",
  ] },
]
```

To run the task with Claude Code instead, **replace** that entry — see below —
with `handlers_mode = "replace"` if the handler being replaced comes from an
included preset:

```toml
[tool.finecode.action.run_agent_task]
handlers_mode = "replace"
handlers = [
  { name = "claude_code_agent", source = "fine_agent_claude_code.ClaudeCodeAgentHandler", env = "dev_no_runtime", dependencies = [
    "fine_agent_claude_code~=0.1.0a0",
  ] },
]

[tool.finecode.action.run_agent_task.handlers.claude_code_agent]
config.permission_mode = "acceptEdits"  # the CLI's default approves nothing
```

A caller passes a named `profile` to run a task under a different model without
editing the task definition. A field left unset on a profile inherits the
top-level value:

```toml
[tool.finecode.action.run_agent_task.handlers.pi_agent]
config.model = "anthropic/claude-sonnet-5"
config.settle_timeout_sec = 900
config.profiles.my_task = { model = "anthropic/claude-opus-5:high", settle_timeout_sec = 3600 }
```

An unknown profile is `FAILED` before any backend process is spawned. A caller
can also pass `output_schema` to ask for structured output; the backend returns
the decoded JSON in `structured_output` and the caller validates the type.

**Exactly one handler.** Unlike most FineCode actions, this one gains nothing
from merging several handlers' results: two agents independently attempting the
same task would both write to the same files, and their answers cannot be
meaningfully combined. Swap backends by replacing the registered handler, never
by adding a second one.

See [`docs/reference/actions.md`](../../docs/reference/actions.md#run_agent_task)
for the payload, result and handler configuration.
