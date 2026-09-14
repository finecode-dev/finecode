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

**Exactly one handler.** Unlike most FineCode actions, this one gains nothing
from merging several handlers' results: two agents independently attempting the
same task would both write to the same files, and their answers cannot be
meaningfully combined. Swap backends by replacing the registered handler, never
by adding a second one.

See [`docs/reference/actions.md`](../../docs/reference/actions.md#run_agent_task)
for the payload, result and handler configuration.
