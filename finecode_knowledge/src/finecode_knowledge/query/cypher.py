"""``Query`` -> openCypher 9. A **compiler, not a second executing backend** (ADR-0015 D2).

It emits a string and executes nothing: no server, no driver, no engine in the
test path (NFR3, NFR7). Two things justify shipping it anyway, and the second is
why a comment saying "compilation is possible" would not do:

- **NFR4's retargeting claim gets evidence.** ADR-0003's insurance argument --
  that the IR's shape keeps a second target reachable -- is otherwise
  unfalsifiable. A compiler exercised on every rule is the falsification test.
- **NFR5 gets teeth.** "Emitted Cypher is human-readable, pasteable into a
  browser" is a requirement no execution test can check. The golden files put
  the emitted string in the review diff, where a human reads it.

**Dialect: §3.7's probed openCypher 9 subset, not Neo4j 5.** Probed 2026-07-18
against FalkorDB: ``WHERE NOT EXISTS { MATCH ... }`` **fails to parse**, so
negation lowers to a *pattern predicate* (``NOT (a)-[:k]->(b)``), which works.
Pattern predicates, ``-[:a|b]->``, ``OPTIONAL MATCH`` and edge properties in
``RETURN`` all work.

## The graph encoding

| Fact model | Property graph |
| --- | --- |
| entity | node labelled with its qualified type, ``KEY`` fields as properties |
| edge fact | relationship typed by the qualified kind; provenance on the relationship |
| field fact | a ``:Fact`` node reached by ``-[:asserted]->``, carrying ``value`` and provenance |
| ``KEY`` literal | a property on the entity node -- **never** a match over a fact (ADR-0019 D7) |

A field fact is a node rather than a property because provenance is bindable
(FR5): ``ProjectFields.def_path(subject, _, at=expected_in)`` needs ``at`` to
name something, and a property has no identity to name. Encoding it as a node
makes ``at=`` work identically on fields and edges, which is what keeps one rule
compiling to both targets unchanged.
"""

from __future__ import annotations

import dataclasses
import re
import typing

from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.literal import Conjunction, Literal, LiteralKind
from finecode_knowledge.query.terms import Prov, Var

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.registry import SchemaRegistry
    from finecode_knowledge.query.query import Query
    from finecode_knowledge.query.rule import Rule

__all__ = ["CypherCompilationError", "compile_query", "compile_rule"]

_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CypherCompilationError(SchemaError):
    """A body this compiler cannot lower, named precisely enough to act on."""


def _ident(name: str) -> str:
    """Backtick-quote anything that is not a bare openCypher identifier.

    Every schema name is qualified (``fine_knowledge.Preset``), so in practice
    almost everything needs quoting -- which is why this is applied uniformly
    rather than remembered per call site.
    """
    return name if _PLAIN.match(name) else f"`{name}`"


def _literal_value(value: object) -> str:
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


class _Names:
    """Stable, readable variable names -- head parameters keep theirs."""

    def __init__(self, head: dict[int, str] | None = None) -> None:
        self._names: dict[int, str] = dict(head or {})
        self._next = 0

    def of(self, term: object) -> str:
        if not isinstance(term, Var):
            return _literal_value(term)
        if id(term) not in self._names:
            self._names[id(term)] = f"v{self._next}"
            self._next += 1
        return self._names[id(term)]

    def is_named(self, term: object) -> bool:
        return isinstance(term, Var) and id(term) in self._names


# ---- flattening -------------------------------------------------------


def _flatten(
    body: Conjunction, schema: SchemaRegistry, *, depth: int = 0
) -> list[list[Literal]]:
    """Inline derived predicates, turning clause disjunction into whole-query UNIONs.

    A multi-clause predicate multiplies the result: ``references_preset`` has two
    clauses, so rule 3 compiles to a ``UNION`` of two queries. That is real
    openCypher rather than a compiler artifact -- a predicate's value *is* the
    union of its clauses.
    """
    if depth > 16:
        raise CypherCompilationError(
            "derived-predicate expansion exceeded 16 levels; a recursive predicate "
            "has no openCypher 9 lowering in this compiler (FR10 is expressible, "
            "not evaluated)."
        )

    alternatives: list[list[Literal]] = [[]]
    for literal in body:
        if literal.kind is not LiteralKind.DERIVED:
            alternatives = [branch + [literal] for branch in alternatives]
            continue

        if literal.negated:
            # A negated predicate is one indivisible pattern predicate. Expanding it
            # into separately-negated literals would compute `NOT a AND NOT b` where
            # the body means `NOT (a AND b)` -- a different, weaker question.
            alternatives = [branch + [literal] for branch in alternatives]
            continue

        predicate = schema.predicate(literal.predicate).predicate
        expansions: list[list[Literal]] = []
        for clause in predicate.clauses:
            renamed = _substitute(clause.body, _head_substitution(clause, literal))
            expansions.extend(_flatten(renamed, schema, depth=depth + 1))
        alternatives = [
            branch + expansion for branch in alternatives for expansion in expansions
        ]
    return alternatives


def _head_substitution(clause: object, literal: Literal) -> dict[int, object]:
    return {
        id(head_term): call_term
        for head_term, call_term in zip(
            clause.head,
            literal.terms,
            strict=True,  # type: ignore[attr-defined]
        )
    }


def _substitute(body: Conjunction, substitution: dict[int, object]) -> Conjunction:
    fresh: dict[int, object] = {}

    def swap(term: object) -> object:
        if not isinstance(term, Var):
            return term
        if id(term) in substitution:
            return substitution[id(term)]
        if id(term) not in fresh:
            fresh[id(term)] = Prov() if isinstance(term, Prov) else Var(term.type)
        return fresh[id(term)]

    return Conjunction(
        literals=tuple(
            dataclasses.replace(
                literal,
                terms=tuple(swap(t) for t in literal.terms),
                at=None if literal.at is None else swap(literal.at),
            )
            for literal in body
        )
    )


# ---- lowering one flat conjunction ------------------------------------


class _Clause:
    def __init__(self, names: _Names, schema: SchemaRegistry, wanted: set[int]) -> None:
        self.names = names
        self.schema = schema
        self.wanted = wanted
        """Ids of value variables anything else refers to. A value bound once and
        never read is a placeholder (`ProjectFields.def_path(subject, q.var(str),
        at=expected_in)` binds one), and emitting a `WITH` for it is noise in a
        string whose readability is the requirement (NFR5)."""
        self.lines: list[str] = []
        self.where: list[str] = []
        self.bound: set[int] = set()
        self._key_props: dict[int, dict[str, str]] = {}
        self._labelled: set[int] = set()

    # -- node patterns --

    def _node(self, term: object, entity_type: str | None = None) -> str:
        name = self.names.of(term)
        parts = [name]
        if entity_type is not None and id(term) not in self._labelled:
            parts.append(f":{_ident(entity_type)}")
            if isinstance(term, Var):
                self._labelled.add(id(term))
        props = self._key_props.pop(id(term), None) if isinstance(term, Var) else None
        if props:
            rendered = ", ".join(f"{_ident(k)}: {v}" for k, v in props.items())
            parts.append(" {" + rendered + "}")
        return "(" + "".join(parts) + ")"

    def _endpoints(self, term: object, entity_type: str) -> str:
        if isinstance(term, Var) and id(term) in self.bound:
            return f"({self.names.of(term)})"
        if isinstance(term, Var):
            self.bound.add(id(term))
        return self._node(term, entity_type)

    # -- literals --

    def add(self, literal: Literal) -> None:
        if literal.negated:
            self.where.append(self._negation(literal))
            return
        if literal.kind is LiteralKind.EDGE:
            self._edge(literal)
        elif literal.kind is LiteralKind.KEY:
            self._key(literal)
        elif literal.kind is LiteralKind.FIELD:
            self._field(literal)
        elif literal.kind is LiteralKind.KNOWN:
            self._known(literal)
        else:  # pragma: no cover - _flatten removes DERIVED
            raise CypherCompilationError(
                f"unexpanded derived literal {literal.predicate!r}"
            )

    def _known(self, literal: Literal) -> None:
        """Existence: the node has at least one asserted fact.

        Not a bare node match. In this encoding an entity node can be created as
        an edge endpoint without anything having been asserted about it, which is
        precisely the difference ``LiteralKind.KNOWN`` exists to express -- so it
        compiles to the existence of an ``asserted`` fact, matching what
        ``FactSource.contains`` answers in the interpreter.
        """
        entity_type = typing.cast(str, literal.entity_type)
        holder = self._reference(literal.terms[0], entity_type)
        self.where.append(f"{holder}-[:asserted]->(:Fact)")

    def _edge(self, literal: Literal) -> None:
        relationship = self.schema.relationship(literal.predicate)
        src = self._endpoints(literal.terms[0], relationship.src)
        dst = self._endpoints(literal.terms[1], relationship.dst)
        variable = self.names.of(literal.at) if literal.at is not None else ""
        if literal.at is not None:
            self.bound.add(id(literal.at))
        self.lines.append(
            f"MATCH {src}-[{variable}:{_ident(literal.predicate)}]->{dst}"
        )

    def _key(self, literal: Literal) -> None:
        """A KEY literal is the node's identity encoding, never a match over a fact."""
        entity_type = typing.cast(str, literal.entity_type)
        entity = literal.terms[0]
        entity_class = self.schema.entity_type(entity_type)
        key_ids = [f.id for f in entity_class.KEY]

        if isinstance(entity, Var) and id(entity) not in self.bound:
            # Construct: fold the components into the node pattern, so a negated
            # `declares_dep` stays a single pattern predicate.
            props = {
                name: self.names.of(term)
                for name, term in zip(
                    literal.key_fields, literal.terms[1:], strict=True
                )
            }
            unbound = [
                name
                for name, term in zip(
                    literal.key_fields, literal.terms[1:], strict=True
                )
                if isinstance(term, Var) and id(term) not in self.bound
            ]
            if unbound or set(literal.key_fields) != set(key_ids):
                raise CypherCompilationError(
                    f"{entity_type}.key(): cannot address an entity from a partial key "
                    f"(unbound {unbound}, unnamed {sorted(set(key_ids) - set(literal.key_fields))})."
                )
            self._key_props[id(entity)] = props
            self.bound.add(id(entity))
            self.lines.append(f"MATCH {self._node(entity, entity_type)}")
            return

        # Project or test: read the component off the already-bound node.
        holder = self.names.of(entity)
        for name, term in zip(literal.key_fields, literal.terms[1:], strict=True):
            access = f"{holder}.{_ident(name)}"
            if isinstance(term, Var) and id(term) not in self.bound:
                if id(term) not in self.wanted:
                    continue
                self.bound.add(id(term))
                self.lines.append(f"WITH *, {access} AS {self.names.of(term)}")
            else:
                self.where.append(f"{access} = {self.names.of(term)}")

    def _field(self, literal: Literal) -> None:
        """A field fact is a node, so its provenance is nameable (FR5)."""
        entity_type = typing.cast(str, literal.entity_type)
        entity = literal.terms[0]
        value = literal.terms[1]
        holder = self._endpoints(entity, entity_type)
        fact = (
            self.names.of(literal.at)
            if literal.at is not None
            else f"f{len(self.lines)}"
        )
        if literal.at is not None:
            self.bound.add(id(literal.at))
        self.lines.append(
            f"MATCH {holder}-[:asserted]->"
            f"({fact}:Fact {{field: {_literal_value(literal.predicate)}}})"
        )
        if isinstance(value, Var) and id(value) not in self.bound:
            if id(value) in self.wanted:
                self.bound.add(id(value))
                self.lines.append(f"WITH *, {fact}.value AS {self.names.of(value)}")
        else:
            self.where.append(f"{fact}.value = {self.names.of(value)}")

    def _negation(self, literal: Literal) -> str:
        """Negation is a **pattern predicate**, because §3.7 probed
        ``WHERE NOT EXISTS {{ MATCH ... }}`` and it fails to parse on the target.

        The negated body must reduce to one path. That covers every rule here --
        ``declares_dep``'s ``Package.key`` folds into the endpoint node -- and
        anything else raises rather than emitting Cypher that would not run.
        """
        if literal.kind is LiteralKind.EDGE:
            relationship = self.schema.relationship(literal.predicate)
            src = self._reference(literal.terms[0], relationship.src)
            dst = self._reference(literal.terms[1], relationship.dst)
            return f"NOT {src}-[:{_ident(literal.predicate)}]->{dst}"
        if literal.kind is LiteralKind.KEY:
            holder = self.names.of(literal.terms[0])
            tests = " AND ".join(
                f"{holder}.{_ident(name)} = {self.names.of(term)}"
                for name, term in zip(
                    literal.key_fields, literal.terms[1:], strict=True
                )
            )
            return f"NOT ({tests})"
        if literal.kind is LiteralKind.KNOWN:
            entity_type = typing.cast(str, literal.entity_type)
            holder = self._reference(literal.terms[0], entity_type)
            return f"NOT {holder}-[:asserted]->(:Fact)"
        if literal.kind is LiteralKind.FIELD:
            entity_type = typing.cast(str, literal.entity_type)
            holder = self._reference(literal.terms[0], entity_type)
            props = f"field: {_literal_value(literal.predicate)}"
            value = literal.terms[1]
            # Negation safety (§5.4) guarantees the value term is already bound, so
            # there is never an unbindable variable to fold in here.
            props += f", value: {self.names.of(value)}"
            return f"NOT {holder}-[:asserted]->(:Fact {{{props}}})"
        if literal.kind is LiteralKind.DERIVED:
            # Clauses are disjunction, so NOT(a OR b) really is NOT a AND NOT b --
            # correct at the *clause* level, and only there.
            predicate = self.schema.predicate(literal.predicate).predicate
            return " AND ".join(
                self._body_pattern(
                    _substitute(clause.body, _head_substitution(clause, literal)),
                    literal.predicate,
                )
                for clause in predicate.clauses
            )
        raise CypherCompilationError(
            f"cannot lower a negated {literal.kind.value} literal to a pattern predicate. "
            "§3.7's dialect has no `WHERE NOT EXISTS { MATCH ... }`, so a negated body "
            "must reduce to a single path."
        )

    def _body_pattern(self, body: Conjunction, predicate_id: str) -> str:
        """One clause body as a single negated path pattern.

        Exactly one edge, plus KEY literals that fold into its endpoints -- which
        is what `declares_dep` is, and why `Package.key(pkg, name=pkg_name)`
        becomes `{name: pkg_name}` on the destination node rather than a second
        conjunct nothing can attach to.
        """
        edges = [x for x in body if x.kind is LiteralKind.EDGE and not x.negated]
        keys = [x for x in body if x.kind is LiteralKind.KEY and not x.negated]
        if len(edges) != 1 or len(edges) + len(keys) != len(body.literals):
            raise CypherCompilationError(
                f"cannot lower negated {predicate_id!r}: its body is not a single path "
                f"({len(edges)} edge literal(s), {len(body.literals) - len(edges)} other). "
                "§3.7's dialect has no `WHERE NOT EXISTS { MATCH ... }`."
            )
        edge = edges[0]
        folded: dict[int, dict[str, str]] = {}
        for key in keys:
            folded.setdefault(id(key.terms[0]), {}).update(
                {
                    name: self.names.of(term)
                    for name, term in zip(key.key_fields, key.terms[1:], strict=True)
                }
            )
        relationship = self.schema.relationship(edge.predicate)
        src = self._folded_node(edge.terms[0], relationship.src, folded)
        dst = self._folded_node(edge.terms[1], relationship.dst, folded)
        return f"NOT {src}-[:{_ident(edge.predicate)}]->{dst}"

    def _folded_node(
        self, term: object, entity_type: str, folded: dict[int, dict[str, str]]
    ) -> str:
        props = folded.get(id(term))
        if props:
            rendered = ", ".join(f"{_ident(k)}: {v}" for k, v in props.items())
            return f"(:{_ident(entity_type)} {{{rendered}}})"
        if isinstance(term, Var) and id(term) in self.bound:
            return f"({self.names.of(term)})"
        return f"(:{_ident(entity_type)})"

    def _reference(self, term: object, entity_type: str) -> str:
        if isinstance(term, Var) and id(term) in self.bound:
            return f"({self.names.of(term)})"
        props = self._key_props.pop(id(term), None) if isinstance(term, Var) else None
        if props:
            rendered = ", ".join(f"{_ident(k)}: {v}" for k, v in props.items())
            return f"(:{_ident(entity_type)} {{{rendered}}})"
        return f"(:{_ident(entity_type)})"


def _wanted(literals: list[Literal], projection: tuple[object, ...]) -> set[int]:
    """Value variables something other than their binding literal refers to."""
    counts: dict[int, int] = {}
    for literal in literals:
        for term in (*literal.terms, literal.at):
            if isinstance(term, Var):
                counts[id(term)] = counts.get(id(term), 0) + 1
    wanted = {ref for ref, count in counts.items() if count > 1}
    wanted |= {id(term) for term in projection if isinstance(term, Var)}
    return wanted


def _lower(
    literals: list[Literal],
    projection: tuple[object, ...],
    names: _Names,
    schema: SchemaRegistry,
) -> str:
    clause = _Clause(names, schema, _wanted(literals, projection))
    # Negations last: a pattern predicate can only reference variables the MATCHes
    # above it have already bound, and the interpreter's own §5.4 safety check
    # guarantees they are bound by *some* earlier positive literal.
    for literal in [x for x in literals if not x.negated]:
        clause.add(literal)
    pending_key_negations = [
        x for x in literals if x.negated and x.kind is LiteralKind.KEY
    ]
    for literal in [x for x in literals if x.negated and x.kind is not LiteralKind.KEY]:
        clause.add(literal)
    for literal in pending_key_negations:
        clause.add(literal)

    lines = list(clause.lines)
    if clause.where:
        lines.append("WHERE " + "\n  AND ".join(clause.where))
    lines.append("RETURN " + ", ".join(names.of(term) for term in projection))
    return "\n".join(lines)


# ---- entry points -----------------------------------------------------


def compile_query(query: Query, *, head: dict[int, str] | None = None) -> str:
    """Lower *query* to an openCypher 9 string.

    Multi-clause predicates become a ``UNION`` of whole queries, which is what a
    predicate's value already is.
    """
    alternatives = _flatten(query.body, query.schema)
    parts = [
        _lower(literals, query.projection, _Names(head), query.schema)
        for literals in alternatives
    ]
    return "\nUNION\n".join(parts)


def compile_rule(rule: Rule) -> str:
    """Lower *rule* to openCypher 9, keeping its head parameter names as variables.

    One ``Rule`` object drives both compilers: a rule does not change when the
    backend does.
    """
    query = rule.query
    head = {
        id(term): name
        for term, name in zip(query.projection, rule.params, strict=True)
        if isinstance(term, Var)
    }
    return f"// {rule.id}\n" + compile_query(query, head=head)
