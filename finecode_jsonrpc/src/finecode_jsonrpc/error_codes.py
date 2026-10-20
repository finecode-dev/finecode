"""Wire error codes used in JSON-RPC responses.

The codes below -32000 are defined by JSON-RPC 2.0 itself; the spec additionally
reserves the whole -32768..-32000 range for future revisions of the spec, so
implementations must not invent codes inside it.

See: https://www.jsonrpc.org/specification#error_object
"""

from __future__ import annotations

# JSON-RPC 2.0 standard error codes.
PARSE_ERROR = -32700
"""Invalid JSON was received. An error occurred while parsing the JSON text."""

INVALID_REQUEST = -32600
"""The JSON sent is not a valid Request object."""

METHOD_NOT_FOUND = -32601
"""The method does not exist / is not available."""

INVALID_PARAMS = -32602
"""Invalid method parameter(s)."""

INTERNAL_ERROR = -32603
"""Internal JSON-RPC error."""

# -32000 to -32099: reserved for implementation-defined server errors.

DEFAULT_REQUEST_CANCELLED = -32800
"""Default wire code for "the request was cancelled before it completed".

JSON-RPC 2.0 has no notion of cancellation, so there is no spec code for it:
every protocol that supports cancellation picks its own value from application
space (outside the reserved range above). -32800 is the value LSP chose, and is
the most widely recognised one, which makes it a reasonable default -- but it is
only a default. Sessions accept the code as a parameter, so a peer speaking a
protocol that numbers cancellation differently can say so, and this package
keeps no opinion of its own about which protocol it is carrying.

This module is the only place in the package that knows this number.
"""
