# ADR-0002: Port-file discovery for the WM server

- **Status:** accepted
- **Date:** 2026-03-19
- **Deciders:** @Aksem
- **Tags:** ipc, wm-server

## Context

The WM (Workspace Manager) server binds to a random available TCP port on
startup to avoid conflicts between multiple instances (e.g. different workspaces,
test runs). Clients such as the LSP server, MCP server, and CLI commands are
started independently and need a way to find the WM server's port without
prior coordination or a hard-coded value.

Two modes of use must be supported:

- **Shared mode**: a single long-lived WM server shared by multiple clients in the
  same workspace (the typical IDE session).
- **Dedicated mode**: a private WM server started by one client (e.g. MCP,
  CLI `run`) that must not interfere with the shared instance.

## Related ADRs Considered

None — port/discovery mechanism has no overlap with other ADRs at the time of writing.

## Decision

The WM server writes its listening port as a plain text number to a
*discovery file* immediately after binding:

- **Shared discovery file** (default): `{venv}/cache/finecode/wm_port`, where
  `{venv}` is venv where finecode WM server is installed.
- **Dedicated discovery file**: a caller-specified path passed via
  `--port-file`. Dedicated instances write to this path instead, leaving the
  shared file untouched.

Clients discover the server by reading the file and probing the TCP connection. The probe distinguishes a live server
from a stale file left by a crashed process. The file is deleted on any clean or signal-driven shutdown, and the server directory is created
recursively (including parent directories) on first startup.

## Consequences

- **No port conflicts**: random binding means multiple WM instances (different
  workspace, concurrent test runs) coexist without configuration.
- **Stale-file resilience**: client verifies the TCP connection, not
  just file existence, so a crashed server does not block future starts.
- **Test isolation**: each e2e test can pass its own file path as
  the dedicated port file, running a private WM instance without touching the
  developer's live shared server or conflicting with other tests.
- **Cross-process discovery**: any process that can read a file can find the
  WM, regardless of parent–child relationship (IDE extensions, CLI tools, MCP
  hosts).
- **Crash cleanup gap**: if the server process is killed with SIGKILL or
  crashes before port file is removed, the discovery file is not removed. Clients
  handle this via the TCP probe, but the stale file persists on disk until the
  next server start overwrites it.

### Alternatives Considered

- **Fixed/configured port**: eliminates the discovery file but requires port
  coordination across concurrent instances and breaks test isolation.
- **Unix domain socket file**: the socket path serves as both identity and
  transport endpoint, avoiding the TCP-probe step. Rejected because Unix
  sockets are not available on Windows.
- **Environment variable**: works only for direct child processes; IDE
  extensions and independently launched CLI commands cannot inherit it.
