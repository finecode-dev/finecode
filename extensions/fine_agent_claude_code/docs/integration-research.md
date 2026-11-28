# Claude Code integration research

What the Claude Code CLI's non-interactive mode actually emits, and which of
its fields the `run_agent_task` handler can stand behind. Everything below was
read off the wire from `claude --version 2.1.233` rather than from
documentation, because the fields that matter here (`modelUsage`,
`permission_denials`) are the ones documentation summarises rather than
specifies.

The sibling integration for `pi` is written up in
[`fine_agent_pi/docs/integration-research.md`](../../fine_agent_pi/docs/integration-research.md);
the two backends differ more in their control channel than in their event
stream.

## 1. Driving the CLI: print mode

`claude -p` runs one non-interactive turn and exits. Three flags make it
machine-drivable:

- `--output-format stream-json` — one JSON object per line on stdout, as the run
  progresses. `json` returns only the terminal object and `text` only the final
  answer, so neither can drive progress reporting.
- `--verbose` — **required**: the CLI rejects `stream-json` output in print mode
  without it. It is not a debug switch here.
- the prompt on **stdin** — with no prompt argument the CLI reads stdin to EOF.
  Preferred over the command line, which would need the prompt escaped and caps
  its length.

There is no control channel back into a print-mode run. `pi --mode rpc` accepts
commands on stdin for the run's whole duration; here stdin carries the prompt
and is closed with it, so an abort is a process teardown rather than a message.

The CLI persists the session transcript by default, and the `system`/`init`
frame names it. That is worth keeping: this handler runs an agent with write
access to a project, and `claude --resume <session_id>` is how a human
afterwards inspects what it did. The handler logs the id rather than returning
it — the action's result type has nowhere to put a backend-specific handle.

## 2. The event stream

Frames observed, in order, on a run that used one tool:

| `type` | Carries |
| --- | --- |
| `system` (`subtype: init`) | `session_id`, `model`, `cwd`, `permissionMode`, the enabled tool list |
| `rate_limit_event` | Rate-limit window state; nothing the handler acts on |
| `assistant` | An Anthropic message: `content` blocks of `text`, `thinking`, `tool_use` |
| `user` | Tool results fed back to the model |
| `result` | The terminal frame — see below |

Unknown types are ignored rather than treated as errors: `rate_limit_event` is
not in any documentation this integration was written against, and more will
appear.

### The `result` frame is the outcome

`subtype` (`success` / `error_max_turns` / `error_during_execution` / …) plus
`is_error` decide the run's status. Both are checked rather than either alone:
they are separate fields, and a future subtype may be neither clearly a success
nor clearly a failure.

The exit code is **not** the outcome. It is read afterwards and only added to a
reason already established from the stream — the same rule the pi handler
follows, for the same reason: a run can end badly and still exit `0`.

`result.result` holds the final answer on a settled run and the **error
message** on a failed one. Reporting that field as `output` unconditionally
would present an error as the agent's work product, so it is read as the answer
only when the run settled; the streamed `assistant` text is the fallback for a
run that never produced a `result` frame at all.

### `result.usage` is not the run's usage

This is the field most likely to be reported wrongly. On a run that spent
~$0.033, the frame said:

```json
"usage":      {"input_tokens": 2, "output_tokens": 4, "cache_creation_input_tokens": 3275},
"modelUsage": {
  "claude-opus-5":    {"inputTokens": 2,   "outputTokens": 4,  "costUSD": 0.03286},
  "claude-haiku-4-5": {"inputTokens": 521, "outputTokens": 13, "costUSD": 0.00059}
}
```

`usage` covers the last request to the main model only. The CLI delegates its
own bookkeeping to a cheaper model, and that spend appears solely in
`modelUsage` — so `modelUsage` is the source, summed across its entries, with
`usage` kept as the fallback for a frame that omits it.

Summing across models is not the derivation `AgentRunUsage` forbids: every term
was reported by the backend and they measure the same thing. `total_tokens`
stays `None` regardless — no total is reported, and input plus output would not
be one. `approx_cost_usd` comes from the frame's own `total_cost_usd`, which
matched the sum of the per-model figures to the cent.

`provider` and `model` are reported only when every entry agrees, since a run
that used two models has no single model to attribute a cost to.

## 3. Permissions: the closest thing to pi's UI requests

pi asks the client questions (`extension_ui_request`) and waits. A
non-opted-in Claude Code run does not ask in print mode: a tool call that
needs approval it cannot get is denied, the agent is told, and it continues —
the denial is recorded in `result.permission_denials`. An opted-in run (section
5) routes every permission request to the run's person instead.

That makes denials weaker evidence than a pi UI request, and the handler treats
them accordingly:

- **settled run with denials** → `SETTLED`. The agent asked, was told no, and
  found another way. Failing it would fail a run that produced what was asked
  for.
- **failed run with denials** → `REFUSED_INTERACTION`. The run needed a decision
  this setup was configured not to make, which is the distinction a
  non-interactive caller (CI) acts on, and not the same as something going
  wrong.

`--permission-mode` is left unset by default, so the CLI's own default applies
and nothing unapproved happens. A task that must edit files therefore needs
`permission_mode = "acceptEdits"` or an explicit `allowed_tools` — a decision
the setup has to make deliberately, because this handler runs an agent with
write access to the user's project.

## 4. Bounding a run

Two ceilings, because they bound different things:

- `max_budget_usd` → `--max-budget-usd`, enforced by the CLI against real spend.
- `settle_timeout_sec` → wall clock, enforced by the handler. An agent loop has
  no natural bound, and a wedged run would otherwise hold an ER subprocess slot
  indefinitely.

This CLI version exposes no `--max-turns`; `result.subtype` still carries
`error_max_turns`, so the failure is handled generically by subtype rather than
by a limit this handler sets.

## 5. Questions over the control wire

An opted-in run (`may_ask_questions = true` on the handler or profile, with a
client that can answer choice, text and multi-select questions) attaches
FineCode as the CLI's permission host: `--input-format stream-json
--permission-prompt-tool stdio` after `--verbose`. The shapes below were
recorded against the listed versions; `--permission-prompt-tool stdio` is not
in the CLI reference, so a CLI update may change them.

The handler writes an `initialize` control request first and the `user` frame
only after its success response, keeps stdin open until the `result` frame,
then closes it. Each `can_use_tool` request is answered with one complete
`control_response` line:

- `AskUserQuestion` → one `ask_choice` per entry (question prefixed
  `"Question k of n. "` for groups, an extra `"Other (type an answer)"`
  option with a free-text follow-up), `ask_multi_choice` for `multiSelect`
  entries. Answers are encoded as the smoke recorded: single-select the label
  or the typed text; multi-select the chosen labels in offered order plus any
  typed text, joined with `", "`. A decline denies with the answers so far
  named; an unrepresentable shape is denied with the shared
  "not supported yet" text and the run continues.
- any other tool → one `"Allow once"`/`"Deny"` choice. Allow replies with
  the original input (never `updatedPermissions`); deny carries the reply
  text. A `disallowed_tools` entry denies without asking — a parenthesised
  pattern denies every call of that tool.
- malformed requests and unknown control subtypes are denied (respectively
  error-replied) without asking; a request without an id is logged and
  ignored.

Only one ask is put to the person at a time (FIFO); a queued request counts
as pending for the clock but is not asked until its turn. A run that is not
opted in, or is overridden with `start_without_questions`, is byte-for-byte
today's run: the same argv without the wire flags, the prompt on stdin, EOF.

The settle budget pauses while claude is blocked on the host (R7): at least
one routed request pending, and every `tool_use` id without a `tool_result`
belongs to a pending routed request. A routed request without a `tool_use_id`
never pauses, but still counts when the budget expires. Budget expiry with a
request pending is `refused_interaction` naming the question, with the session
reported; expiry with nothing pending keeps today's `failed` result.

Supported versions: 2.1.329 (M1/M2: wire shapes, denial entries), 2.1.330 (M3:
cutoff, plus the live smoke). Live smoke record (devcontainer, authenticated
`claude`, `--model haiku --max-budget-usd 0.25` per run): L1 codename question
round trip, L2 multiSelect with Other, L3 Write allow/deny, L4 delayed answer,
L5 question delivery under `default`/`acceptEdits`, L6 `disallowed_tools`, L7
continuation in question mode; L8 holds a question for 7,300 s to check claude
never resolves a pending request on its own. Results live under
`issues/921-claude-code-agent-interactive-questions/probes/`.

Known gaps: the M3 evidence gap (no claude-side resolution seen, checked to
7,300 s by L8 — if a CLI ever answers its own pending request, return to
`fine-design-amend`); modes that never call the host (`bypassPermissions`,
`dontAsk`) bypass it by design and were recorded, not routed.

## Open questions

- **Exit codes are undocumented.** Non-zero is treated as failure with stderr
  attached; codes observed in practice should be recorded here as they are
  found, rather than pretending to a precision the integration does not have
  (R-501).
- **`permission_denials` entry shape** was never observed non-empty — every
  probe was either granted or needed no approval. The parser reads `tool_name`
  and falls back to `tool`, then to `"unknown"`, so a shape change degrades the
  message rather than the status.
- **No streaming of partial messages.** `--include-partial-messages` would give
  token-level progress; complete `assistant` frames are enough to report tool
  names and turns, which is all progress may carry (R-304).
