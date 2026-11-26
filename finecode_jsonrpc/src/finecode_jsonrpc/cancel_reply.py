"""Types for the await-reply-after-cancel send mode.

Both JSON-RPC peers (``JsonRpcClient`` and ``JsonRpcServerSession``) offer a
send option that keeps waiting for the reply after the awaiting task is
cancelled, so a cancelled delegation can learn how the child ended. These
records describe that wait and its outcome.
"""

from __future__ import annotations

import dataclasses
import enum
import typing


@dataclasses.dataclass(frozen=True)
class AwaitReplyAfterCancel:
    """Keep waiting for the reply after the awaiting task is cancelled.

    ``timeout`` is seconds after the first cancellation; ``None`` waits until
    the reply arrives or the transport ends.
    """

    timeout: float | None


class ReplyAfterCancelKind(enum.Enum):
    RESULT = "result"
    ERROR = "error"
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"


@dataclasses.dataclass(frozen=True)
class ReplyAfterCancel:
    """What the post-cancel wait observed."""

    kind: ReplyAfterCancelKind
    result: typing.Any = None
    error_code: int | None = None
    error_data: typing.Any = None
