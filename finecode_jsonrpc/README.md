# finecode_jsonrpc

JSON-RPC client implementation for FineCode.

## Local opt-in hooks and drains

- `feature_sync(name, impl)`: opt-in synchronous notification registry,
  separate from ordinary async `feature` handlers. Used for runner
  `$/progress` admission: minimal decode and routing with no await, user
  callback, or blocking I/O. Rejects coroutine/async-generator functions
  (including callable objects and partials) and disposes unexpected
  awaitable returns as owned failures.
- `send_request(..., on_response)`: per-request `ResponseCompletion` capture
  runs during matching response dispatch, before future completion. Hook
  faults are inspectable without replacing the received response; entries
  are removed on every exit. Never fires for timeout/cancellation/EOF, and
  duplicate/late responses cannot recapture.
- `notify_and_drain(method, params)`: awaited notification send. Enqueues
  framing bytes plus an internal `OutputDrain` marker (process-local, never
  serialized) and resolves only after the IO sender drains preceding bytes
  through the writer. Write/drain failure, queue closure, sender
  cancellation and shutdown fail all pending markers.

Ordering guarantee (proposed ADR-0134/ADR-0135): the response hook freezes each subscribed
stream prefix before the response is exposed, so streamed items complete
before run completion. Ordinary fire-and-forget `notify` keeps its existing
behavior.
