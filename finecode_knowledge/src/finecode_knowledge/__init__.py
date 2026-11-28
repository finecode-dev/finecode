"""The knowledge model engine: entity/fact model, query IR, interpreter, memo DAG.

**This package holds no schema.** It knows what a field, a relationship and a
rule *are*; it does not know that FineCode has ``Project``s or that a preset
includes another. R20 puts language- and tool-specific logic behind the provider
contract, and the packaging is what enforces it here: a schema lives in a
separate distribution that hands its ``SchemaRegistry`` to this one, so the WM
can import the engine without any rule code entering its process.

FineCode's own schema, providers and rules are ``fine_knowledge``.

Nothing is re-exported at this level. Import from the subpackage that owns the
concept -- ``finecode_knowledge.model`` for the fact model,
``finecode_knowledge.query`` for the rule and query surface.
"""

from __future__ import annotations
