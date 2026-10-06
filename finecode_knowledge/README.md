# finecode_knowledge

The knowledge model engine: the entity/fact model, the Datalog-shaped query IR and its
interpreter, and (from Phase 2 of the memo-DAG slice) the memoization DAG.

It carries **no schema of its own**. A schema — entity types, fields, relationships,
providers, predicates and rules — is declared by a separate distribution and handed to the
engine as a `SchemaRegistry`. That is R20 ("the core contains no language- or tool-specific
logic") made a packaging fact rather than a lint rule: the WM imports this distribution to
run the DAG, and cannot reach a rule because the distribution holding rules is not installed
in its environment.

FineCode's own schema, providers and rules live in `fine_knowledge`, an extension reached
through the preset.

## Layout

| Path | Holds |
| --- | --- |
| `model/` | entity types, fields, relationships, facts, the store, fingerprints, units, the registry |
| `query/` | the query IR, rules, derived predicates, the interpreter, footprints, freshness, the Cypher compiler |
| `fact_file.py` | reading and writing a fact file, content-digest revision included |

## Declaring a schema against it

```python
from finecode_knowledge.model.registry import SchemaRegistry, set_default_registry

MY_SCHEMA = SchemaRegistry()
set_default_registry(MY_SCHEMA)   # so `@q.rule` without `schema=` finds it
MY_SCHEMA.register_entity_type(...)
MY_SCHEMA.register_namespace(...)
```

`set_default_registry` is a nomination, not a lookup: the engine never goes looking for a
well-known schema module. One process has at most one default; a second, different registry
raises rather than resolving by import order. Passing `schema=` explicitly at each rule,
predicate and query needs no default at all.
