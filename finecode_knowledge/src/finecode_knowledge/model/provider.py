from __future__ import annotations

import inspect
import pathlib
import sys
import typing

from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.naming import declaring_package

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.fields import AnyField
    from finecode_knowledge.model.registry import SchemaRegistry
    from finecode_knowledge.model.relationship import AnyRelationship

__all__ = ["EntityProvider"]


class EntityProvider:
    ID: typing.ClassVar[str]
    """The **local** id. The declaring package supplies the qualifier, exactly
    as for every other registered name (ADR-0017 D3/D4)."""
    SUPPLIES_FIELDS: typing.ClassVar[list[AnyField]]
    """The field objects this provider may emit.

    Objects rather than ``(type_name, field)`` pairs: a registered ``Field``
    already carries its qualified entity and its own qualified name, so the
    pair's string half was a second hand-typed spelling of a fact the object
    holds -- N2's drift one level down. It is also what lets ``ingest``
    resolve an emission's local field name to its qualified form without an
    unqualified registry lookup (ADR-0017 D4/D6)."""
    SUPPLIES_EDGES: typing.ClassVar[list[AnyRelationship]]
    DETERMINISTIC: typing.ClassVar[bool] = True
    """Whether re-running on unchanged input yields an identical fact set (C7).

    Declared and not yet read: R9's untracked-input handling turns on it, and
    that consumes it once the memo DAG serves memoized values. Under R19 a
    third-party provider declares its own, and nothing checks it -- recorded as
    finding 4 of ``use-cases/0002-developer-defined-knowledge.md``."""

    @classmethod
    def qualified_id(cls) -> str:
        return f"{declaring_package(cls)}.{cls.ID}"

    @classmethod
    def source_inputs(cls, schema: SchemaRegistry) -> tuple[pathlib.Path, ...]:
        """The code files whose content changes what this provider emits.

        The provider's own module (ADR-0023 D1) plus the module declaring every
        member of ``SUPPLIES_FIELDS`` / ``SUPPLIES_EDGES`` (ADR-0026 D1),
        de-duplicated in first-seen order. They are fingerprinted like any data
        input, because the fact file *is* cached extraction: editing an
        extractor, or renaming a field it emits, changes what was extracted
        while every scanned source file stays byte-identical.

        **Derived, not declared** -- ``SUPPLIES`` already enumerates the schema
        surface, so R9's *forgotten* failure is unreachable here. That is the
        difference from the provider's own helpers, which are not discoverable
        and stay an explicit override.

        Raises:
            SchemaError: a module has no file on disk (a zipimported provider,
                a namespace package). Loud rather than skipped: a silently
                dropped code input is exactly the untracked-but-not-declared
                state R9 exists to forbid. Override and declare it in the
                unit's ``untracked`` instead (ADR-0023 D4).
        """
        modules = [cls.__module__]
        for member in (*cls.SUPPLIES_FIELDS, *cls.SUPPLIES_EDGES):
            modules.append(schema.declaring_module(member))

        paths: dict[pathlib.Path, None] = {}
        for name in modules:
            paths.setdefault(_module_file(name), None)
        return tuple(paths)


def _module_file(name: str) -> pathlib.Path:
    module = sys.modules.get(name)
    found = getattr(module, "__file__", None) if module is not None else None
    if found is None:
        # inspect.getfile raises TypeError for builtins and modules without a
        # file; both mean the same thing here, so the message is one message.
        raise SchemaError(
            f"Module {name!r} has no file on disk, so its code cannot be fingerprinted. "
            "Declare it as an untracked input on the unit instead (ADR-0023 D4)."
        )
    return pathlib.Path(inspect.getfile(module)).resolve()
