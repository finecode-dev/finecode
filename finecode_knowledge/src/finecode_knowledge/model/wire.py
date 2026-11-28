from __future__ import annotations

from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.entity_type import EntityRef
from finecode_knowledge.model.facts import (
    EdgeFact,
    Emission,
    FieldFact,
    Provenance,
    RunStamp,
    SourceLoc,
)

__all__ = [
    "fact_from_json",
    "fact_to_json",
    "prov_from_json",
    "prov_to_json",
    "ref_from_json",
    "ref_to_json",
]
"""``prov``/``ref`` are public because a *query result* carries the same shapes a
fact does -- a row may bind an ``EntityRef`` or a whole ``Provenance`` (FR5) --
and ``query/serialize.py`` must put them on the wire identically. One encoding
for one type, wherever it crosses."""


def prov_to_json(prov: Provenance) -> dict:
    data: dict = {
        "band": prov.band.value,
        "provider": prov.provider,
        "run": {"id": prov.run.id, "at": prov.run.observed_at},
    }
    if prov.location is not None:
        data["loc"] = {
            "project": prov.location.project,
            "file": prov.location.file,
            "line": prov.location.line,
        }
    return data


def prov_from_json(data: dict) -> Provenance:
    location = None
    if "loc" in data:
        loc_data = data["loc"]
        if not isinstance(loc_data, dict):
            raise ValueError(
                f"Unsupported source location format: {loc_data!r}; expected an object with "
                "project/file/line keys (the old 'file:line' string format is no longer supported)"
            )
        location = SourceLoc(
            project=loc_data["project"], file=loc_data["file"], line=loc_data["line"]
        )
    run_data = data["run"]
    return Provenance(
        band=Band(data["band"]),
        provider=data["provider"],
        run=RunStamp(id=run_data["id"], observed_at=run_data["at"]),
        location=location,
    )


def ref_to_json(ref: EntityRef) -> dict:
    return {"type": ref.type, "key": list(ref.key)}


def ref_from_json(data: dict) -> EntityRef:
    return EntityRef(type=data["type"], key=tuple(data["key"]))


def fact_to_json(fact: Emission) -> dict:
    if isinstance(fact, FieldFact):
        return {
            "fact": "field",
            "entity": ref_to_json(fact.entity),
            "field": fact.field,
            "value": fact.value,
            "prov": prov_to_json(fact.prov),
        }
    return {
        "fact": "edge",
        "kind": fact.kind,
        "src": ref_to_json(fact.src),
        "dst": ref_to_json(fact.dst),
        "prov": prov_to_json(fact.prov),
    }


def fact_from_json(data: dict) -> Emission:
    discriminator = data["fact"]
    if discriminator == "field":
        return FieldFact(
            entity=ref_from_json(data["entity"]),
            field=data["field"],
            value=data["value"],
            prov=prov_from_json(data["prov"]),
        )
    if discriminator == "edge":
        return EdgeFact(
            kind=data["kind"],
            src=ref_from_json(data["src"]),
            dst=ref_from_json(data["dst"]),
            prov=prov_from_json(data["prov"]),
        )
    raise ValueError(f"Unknown fact discriminator: {discriminator!r}")
