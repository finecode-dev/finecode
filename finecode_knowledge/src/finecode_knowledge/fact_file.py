from __future__ import annotations

import hashlib
import json
import pathlib
import typing

from finecode_knowledge.model.fact_source import Revision
from finecode_knowledge.model.store import FactStore

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.registry import SchemaRegistry

__all__ = ["read_facts", "write_facts"]


def write_facts(store: FactStore, path: pathlib.Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = store.to_json()
    path.write_text(json.dumps(data))
    return len(data["facts"])


def read_facts(schema: SchemaRegistry, path: pathlib.Path) -> FactStore:
    """Load *path*'s facts, pinning the file's own content digest as the store's revision.

    This is the file-loaded path's answer to ADR-0013 D6.2. Note what the digest
    does and does not say: it identifies exactly the facts served, and nothing
    about whether the sources they were extracted from have moved since. That
    gap is why every execution on this path also carries an ``UNTRACKED``
    reservation (ADR-0014 D7) rather than reporting itself verified.
    """
    raw = path.read_bytes()
    data = json.loads(raw)
    return FactStore.from_json(
        schema, data, revision=Revision(hashlib.sha256(raw).hexdigest())
    )
