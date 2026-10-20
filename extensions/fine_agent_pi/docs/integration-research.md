# pi.dev integration research

Research notes for integrating [pi](https://pi.dev/) ([earendil-works/pi](https://github.com/earendil-works/pi))
into FineCode as an agent backend callable from action handlers.

**Date:** 2026-08-10 — pi and DeepSeek both move fast; re-verify before acting on anything here.
**Status:** research only, no decision ratified. Today this package only installs the `pi` CLI
(`fine_agent_pi/install_pi_handler.py`).

---

## 1. Surfacing progress: RPC vs. TUI

### The constraint that decides everything

pi picks **one mode per process** — interactive TUI, `-p`/print, `--mode json`, or `--mode rpc` —
and RPC is JSONL over that process's **stdin/stdout**.

Consequences, all verified against the docs and none of them worked around by configuration:

- **TUI and RPC cannot run simultaneously.** Modes are mutually exclusive.
- **There is no daemon or server mode**, no socket, and no documented way for a second process to
  observe a live session. So there is no "terminal app that also speaks RPC" to plug into.
- **The stdio pipe has exactly one consumer** — whoever spawned pi. If FineCode owns the pipe,
  FineCode must fan out events itself to any other viewer.

Second constraint: pi's SDK, TUI library (`@earendil-works/pi-tui`), and extensions are all
**TypeScript/Node**. The TUI docs state components are coupled to pi's runtime context
(`ctx`, `tui`, `theme`, `keybindings`) and are explicitly *not* positioned as a standalone library
for external terminal UIs. **RPC is the only language-neutral seam** available to a Python handler.

### The RPC event stream is rich enough to drive a UI

From `docs/rpc.md`:

| Category | Events / methods |
| --- | --- |
| Agent lifecycle | `agent_start`, `agent_end`, `agent_settled`, `turn_start`, `turn_end` |
| Message streaming | `message_start`, `message_update` (`text_delta`, `thinking_delta`, `toolcall_delta`), `message_end` |
| Tool execution | `tool_execution_start`, `tool_execution_update`, `tool_execution_end` |
| Queue / system | `queue_update`, `compaction_start`/`end`, `auto_retry_start`/`end` |
| Requests | `prompt`, `steer`, `follow_up`, `abort`, `get_state`, `get_messages`, `get_entries`, `export_html`, … |

`get_entries` accepts a `since` cursor, and an entry id works as a durable cursor — usable for
replay/resume after a viewer disconnects.

**Framing gotcha:** RPC uses strict LF-delimited JSONL. The docs warn against generic line readers
(Node `readline` is named) because they also split on Unicode separators appearing inside JSON
payloads. Split on `\n` only; strip a trailing `\r`.

### Options considered

| Option | What it is | Verdict |
| --- | --- | --- |
| **A. Own UI fed by RPC** | Handler drives `--mode rpc`, forwards events into FineCode's existing LSP progress / `user_messages` channels | **Recommended.** No new renderer, progress lands in the editor the developer already uses |
| **B. Invert control** | Human runs `pi` interactively; a pi extension (TypeScript, in `.pi/extensions/`) bridges to the WM via `pi.exec` or `fetch` | Most TUI for least work, but inverts FineCode's execution model — the handler becomes callee, not driver. A product decision, not a UI choice |
| **C. Headless + session-file viewer** | Handler drives RPC; separate viewer tails the persisted session (`~/.pi/agent/sessions/`, per-cwd, documented session file format) | Works, but reads a file another process is writing with no concurrency contract. Format drift is a standing maintenance cost |
| **D. `export_html`** | RPC method dumping the session as a page | Not live, but zero-effort visual record. Composes with any of the above |

### Conclusion

**Drive pi over RPC from the handler and forward events into FineCode's existing LSP/user-message
surfaces.** This keeps FineCode the orchestrator — consistent with how it already delegates to
Extension Runners — and avoids putting a Node UI process in the loop. Keep `export_html` as the
cheap after-the-fact artifact.

Reach for option B only if the decision is made that the human's primary console should be pi
rather than their editor.

### Must plan for: `extension_ui_request`

RPC emits `extension_ui_request` events for `select`, `confirm`, `input`, and `editor`, expecting a
matching `extension_ui_response` with the same `id`. Fire-and-forget methods (`notify`, `setStatus`,
`setWidget`, `setTitle`, `set_editor_text`) need no reply.

**If FineCode owns the pipe and never answers, the server auto-resolves with defaults after a
server-side timeout — silently.** So if a loaded pi extension ever prompts, or if approval gates on
tool calls are wanted, the chosen surface must be able to *answer*, not just display. This is the
strongest argument against a read-only progress view.

---

## 2. Model backend: DeepSeek cost analysis

Prompted by "what is the cheapest way to test DeepSeek."

### No subscription exists, in either direction

DeepSeek's consumer chat (chat.deepseek.com, mobile app) is free with **no paid tier at all** — no
Plus, no Pro. The API is pay-per-token with no monthly fee and no per-seat cost.

There is therefore no Claude-Code-style "subscription covers agent usage" path — and equally no
subscription being wasted by going API-only. For contrast, pi's provider docs list OAuth
subscription support for ChatGPT Plus, Claude Pro/Max and GitHub Copilot; **DeepSeek is API-key
only**.

### Access paths, cheapest first

1. **Evaluate the model for free** — chat.deepseek.com, no card. Useless for integration, zero cost
   for "is this model good at my kind of task."
2. **From pi — first-class provider, one env var.** No custom provider config needed:
   ```bash
   export DEEPSEEK_API_KEY=sk-...
   ```
   or `~/.pi/agent/auth.json`: `{"deepseek": {"type": "api_key", "key": "sk-..."}}`
3. **From anything else — two compatible surfaces.** Official docs: *"The DeepSeek API uses an API
   format compatible with OpenAI/Anthropic."*
   - OpenAI format: `https://api.deepseek.com`
   - **Anthropic format: `https://api.deepseek.com/anthropic`** — the useful surprise. Any
     Anthropic-shaped client reading `ANTHROPIC_BASE_URL` can point at DeepSeek with no shim.

### Rates (official pricing page, per 1M tokens, USD)

| Model | Input (cache hit) | Input (cache miss) | Output |
| --- | --- | --- | --- |
| `deepseek-v4-flash` | $0.0028 | $0.14 | $0.28 |
| `deepseek-v4-pro` | $0.003625 | $0.435 | $0.87 |

Both: 1M context, 384K max output.

Cache hits are ~50× cheaper than misses, so **caching behaviour dominates real cost** in an agent
loop that resends a growing conversation. Arithmetic on the posted rates: 1M cache-miss input +
100K output on flash ≈ **$0.17**.

### Caveats

- **Prices are going up, per DeepSeek themselves.** The official pricing page states: *"We plan to
  raise the overall pricing for DeepSeek API services in the near future, with a significant
  increase expected."* Do not build a cost model on these numbers.
- **`deepseek-chat` / `deepseek-reasoner` are deprecated** as of 2026/07/24 — they were legacy
  aliases of `deepseek-v4-flash` (non-thinking / thinking). Use the `v4` names.
- **"5M free signup tokens" is unverified.** Several third-party pricing blogs assert it; the
  official docs and pricing page mention no free tier, trial credits, or minimum top-up. The absence
  of a minimum top-up does mean a test can be funded with a couple of dollars.

### Conclusion

Cheapest test that exercises the *actual integration path* (not just the model): pi with
`DEEPSEEK_API_KEY` set and `deepseek-v4-flash`.

---

## Open questions

- Which FineCode surface receives forwarded pi events — LSP progress, `user_messages`, or both?
- Does any planned use of pi involve extensions that prompt? If yes, the UI must answer
  `extension_ui_response` (see above) and a read-only view is ruled out.
- Session lifetime: one pi session per action run, or a longer-lived session reused across runs?
  Affects whether `--no-session` or `--session-dir` scoping is wanted.
- Cancellation mapping: pi's `abort` / `abort_bash` vs. FineCode's in-flight run cancellation
  (ADR-0080).

## Sources

- [pi coding-agent README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)
- [rpc.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)
- [usage.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/usage.md)
- [extensions.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)
- [tui.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/tui.md)
- [providers.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md)
- [DeepSeek API docs](https://api-docs.deepseek.com/)
- [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing)
