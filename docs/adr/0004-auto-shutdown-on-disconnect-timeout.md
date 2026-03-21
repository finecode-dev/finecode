# ADR-0004: Auto-shutdown on disconnect timeout

- **Status:** accepted
- **Date:** 2026-03-19
- **Deciders:** @Aksem
- **Tags:** lifecycle, wm-server

## Context

The WM server is a long-running process started on demand by clients (LSP
server, MCP server, CLI). Clients may terminate without sending an explicit
shutdown request — for example, when the IDE is force-closed, crashes, or the
extension is reloaded. Without a self-termination mechanism, the WM would run
indefinitely as a ghost process, holding the discovery file and consuming
resources.

Clients may also intentionally stop or restart the WM through an explicit
shutdown request. This ADR addresses the complementary case where no such
request is sent and the WM must determine on its own when to exit.

Two distinct scenarios require handling:

1. **No client ever connects** — the WM started successfully but the client
   failed to connect (e.g. misconfiguration, client crash during startup).
2. **Last client disconnects** — a normal session end or unexpected client
   termination.

## Related ADRs Considered

Reviewed [ADR-0002](0002-port-file-discovery-for-wm-server.md) — related topic:
the WM's shutdown flow performs the discovery-file cleanup defined there.

## Decision

The WM server uses two independent timeout-based shutdown mechanisms:

- **No-client timeout** (default 30 s): started immediately after the server
  begins listening. If no client connects within this window, the WM performs
  its normal shutdown and exits.
- **Disconnect timeout** (default 30 s): started when the last client
  disconnects. If no client reconnects within this window, the WM performs its
  normal shutdown and exits.

These timeouts complement, rather than replace, explicit shutdown requests used
by clients that intentionally stop or restart the WM.

Both timeout paths use the WM's normal shutdown flow, including discovery-file
cleanup (see [ADR-0002](0002-port-file-discovery-for-wm-server.md)).

The disconnect timeout is configurable so that tests and dedicated instances
can use a shorter grace period when needed.

Using the same 30-second default for both timeouts keeps lifecycle behavior
simple and provides a reasonable reconnection window for IDE extension reloads
and brief transient disconnects without leaving orphaned processes running for
long.

## Consequences

- **Ghost process prevention**: the WM exits automatically after a client
  disconnects, without requiring clients to explicitly decide when the shared
  WM should stop. This is the primary defense against orphaned processes after
  IDE close or crash.
- **Reconnection window**: the grace period allows clients to reconnect within
  the timeout — for example, after an IDE extension reload or a brief
  disconnection. The WM does not need to be restarted for each reconnection.
- **Warm reuse across brief idle gaps**: the grace period allows a shared WM
  to survive short pauses between independent clients, such as sequential CLI
  commands, preserving in-process state and caches between commands and
  reducing restart overhead.
- **Connection-driven lifecycle**: shutdown depends on client liveness rather
  than completion of previously requested work. Once no clients remain past the
  grace period, the WM exits through its normal shutdown path.
- **Discovery file cleanup**: normal shutdown removes the discovery file, so a
  stale file is never left behind after a timeout-driven shutdown (unlike a
  SIGKILL).

### Alternatives Considered

- **Immediate shutdown on last disconnect**: safe but breaks IDE extension
  reload scenarios and brief idle gaps between independent clients, such as
  sequential CLI commands using a shared WM.
- **Never auto-shutdown (persistent daemon)**: WM runs until explicitly
  stopped. Requires external process management and makes
  it harder to reason about lifecycle in tests and CI.
- **Client heartbeat / keepalive**: client sends periodic pings; WM shuts down
  if pings stop. More precise than a fixed timeout for detecting dead connected
  clients, but it still does not answer how long the WM should remain alive
  when no clients are connected at all. Shared-WM use cases with brief idle
  gaps between clients, such as sequential CLI commands, would still require a
  grace-period timeout or a different persistent-daemon policy. It also
  requires all clients to implement the heartbeat protocol.
- **Parent PID tracking**: WM monitors its parent process and exits when the
  parent dies. Does not work when the WM is started independently of its client
  (e.g. shared WM).
