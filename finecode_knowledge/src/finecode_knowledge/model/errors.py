from __future__ import annotations

__all__ = ["BandViolationError", "SchemaError", "SuppliesViolationError"]


class SchemaError(Exception):
    """Bad registration, bad ref, or wrong relationship endpoint."""


class SuppliesViolationError(Exception):
    """A provider ingested a field or edge kind it never declared it supplies (C4)."""


class BandViolationError(Exception):
    """A fact claiming DERIVED band was ingested; derived relations are never stored (C5)."""


# `ConflictError` was here. It had exactly one raise site -- `record()` merging two
# providers' values -- and ADR-0014 D5 removed it: a conflict is a fact *about the
# store*, carried in `Record.conflicts` and surfaced as a CONTESTED reservation,
# not a failure that destroys a whole audit's output. Ingest-time rejection, if it
# is ever wanted, is a different decision on the write path.
