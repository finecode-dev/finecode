"""Verdict collection for the child-ER requests of one delegated call.

A delegated call's WM run can send several requests to child ERs (multi-env
fan-out, matrix variants, or two projects). ``DelegatedChildStops`` records
how each of those child requests ended, so the delegated call's own reply can
report whether the whole subtree stopped. It lives in the ``runner`` layer so
that ``runner_client`` (which sends the child requests) and
``elicitation_bridge`` (which carries the dispatch descriptor) can both import
it without a cycle.
"""

from __future__ import annotations

__all__ = ["DelegatedChildStops", "subtree_stopped_of"]


def subtree_stopped_of(value: object) -> bool:
    """Whether *value* is a mapping carrying ``subtreeStopped: true``."""
    return isinstance(value, dict) and value.get("subtreeStopped") is True


class DelegatedChildStops:
    """Whether every child-ER request of one delegated call ended stopped."""

    def __init__(self) -> None:
        self._pending = 0
        self._all_stopped = True

    def subtree_stopped(self) -> bool:
        """``True`` iff no child request is still pending and every recorded
        one ended with a reply carrying ``subtreeStopped: true``.

        A delegated call that sent no child request reads ``True``: it never
        started a child.
        """
        return self._pending == 0 and self._all_stopped

    def child_started(self) -> None:
        """Record that a child-ER request was sent and is now pending."""
        self._pending += 1

    def child_finished(self, *, stopped: bool) -> None:
        """Record how a pending child-ER request ended.

        ``stopped`` is ``True`` only for a reply carrying
        ``subtreeStopped: true``; a lost connection, a missing key, or an
        explicit ``false`` all record unconfirmed.
        """
        self._pending -= 1
        if not stopped:
            self._all_stopped = False
