from __future__ import annotations

import dataclasses
import pathlib
import typing

from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.entity_type import EntityRef
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.fields import AnyField

__all__ = [
    "EdgeFact",
    "Emission",
    "FieldFact",
    "Provenance",
    "RunStamp",
    "SourceLoc",
    "emit_entity",
    "resolve_source_loc",
]


@dataclasses.dataclass(frozen=True)
class RunStamp:
    id: str
    observed_at: str


@dataclasses.dataclass(frozen=True)
class SourceLoc:
    project: str | None
    """The defining project's package name; ``None`` when the location falls
    outside every known project (e.g. an installed dependency)."""
    file: str
    """POSIX path to the source file. Relative to the defining project's dir
    when ``project`` is set; absolute otherwise."""
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


def resolve_source_loc(
    raw: str, base_dir: pathlib.Path, projects: dict[pathlib.Path, str]
) -> SourceLoc:
    """Resolve a raw ``"<path>:<line>"`` location into a project-anchored ``SourceLoc``.

    *base_dir* is the directory the emitting side relativized *raw*'s path
    against (if it is relative at all) -- for ``wm_registry`` that is the
    row's own project dir; for ``ast_definitions`` it is the workspace root.
    The absolute path is then matched against *projects* (project dir ->
    package name), picking the **longest** matching project dir so a nested
    project wins over an ancestor. A path outside every known project
    resolves with ``project=None`` and an absolute ``file``.

    Pure path arithmetic: never touches the filesystem (no ``stat()``, no
    symlink resolution), so the same raw location always resolves the same
    way regardless of what is actually on disk.

    Raises:
        ValueError: *raw* is not a valid ``"<path>:<line>"`` string.
    """
    file_part, sep, line_part = raw.rpartition(":")
    if not sep:
        raise ValueError(f"Not a valid source location: {raw!r}")
    try:
        line = int(line_part)
    except ValueError as exc:
        raise ValueError(f"Not a valid source location: {raw!r}") from exc

    candidate = pathlib.Path(file_part)
    abs_path = candidate if candidate.is_absolute() else base_dir / candidate

    best_dir: pathlib.Path | None = None
    best_name: str | None = None
    for project_dir, name in projects.items():
        if project_dir not in abs_path.parents:
            continue
        if best_dir is None or len(project_dir.parts) > len(best_dir.parts):
            best_dir = project_dir
            best_name = name

    if best_dir is not None:
        return SourceLoc(
            project=best_name, file=abs_path.relative_to(best_dir).as_posix(), line=line
        )
    return SourceLoc(project=None, file=abs_path.as_posix(), line=line)


@dataclasses.dataclass(frozen=True)
class Provenance:
    band: Band
    provider: str
    run: RunStamp
    location: SourceLoc | None = None


@dataclasses.dataclass(frozen=True)
class FieldFact:
    entity: EntityRef
    field: str
    value: object
    prov: Provenance = dataclasses.field(compare=False)
    """Carried, not part of identity (T2/R5): two facts with the same
    entity/field/value are the same fact regardless of which run observed
    them, so re-extraction without change hashes and compares equal and a
    set/hash-based early cutoff can fire."""


@dataclasses.dataclass(frozen=True)
class EdgeFact:
    kind: str
    src: EntityRef
    dst: EntityRef
    prov: Provenance = dataclasses.field(compare=False)
    """See ``FieldFact.prov`` -- carried, not part of identity (T2/R5)."""


Emission: typing.TypeAlias = FieldFact | EdgeFact


def emit_entity(
    ref: EntityRef, fields: dict[AnyField, object], prov: Provenance
) -> list[FieldFact]:
    facts: list[FieldFact] = []
    for field, value in fields.items():
        if field.entity != ref.type:
            raise SchemaError(
                f"Field {field} cannot be emitted onto entity of type {ref.type!r}"
            )
        if value is None:
            continue
        facts.append(FieldFact(entity=ref, field=field.id, value=value, prov=prov))
    return facts
