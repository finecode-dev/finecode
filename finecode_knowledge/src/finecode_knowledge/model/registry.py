from __future__ import annotations

import typing

from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.fields import Field
from finecode_knowledge.model.naming import declaring_package, qualify, split_qualified
from finecode_knowledge.model.relationship import Relationship

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.entity_type import EntityType
    from finecode_knowledge.model.fields import AnyField
    from finecode_knowledge.model.provider import EntityProvider
    from finecode_knowledge.model.relationship import AnyRelationship
    from finecode_knowledge.query.predicate import DerivedPredicate
    from finecode_knowledge.query.rule import Rule

__all__ = ["SchemaRegistry", "default_registry", "set_default_registry"]

_Key: typing.TypeAlias = tuple[str, ...]


class _Namespace:
    """One named namespace: a key shape, plus ADR-0017 D5's collision policy.

    The registry holds namespaces rather than hand-maintained dicts (D1), so
    attribution, spelling and collision behaviour have exactly one
    implementation and a namespace the query layer adds later -- predicates,
    rule ids, templates -- is a declaration rather than new code.
    """

    def __init__(self, label: str) -> None:
        self._label = label
        self._members: dict[_Key, typing.Any] = {}
        self._modules: dict[_Key, str] = {}

    def add(self, key: _Key, member: object, module: str) -> None:
        """Register *member* under *key*, applying D5.

        Same key with an identical definition is a no-op -- load-bearing, not a
        convenience: ``register_entity_type`` registers a type's ``CORE``
        fields and ``register_namespace`` registers the same objects, so
        without it D2's own two calls would conflict.

        Raises:
            SchemaError: *key* already holds a different definition -- one
                package declaring one name twice, differently.
        """
        existing = self._members.get(key)
        if existing is not None:
            if existing is member or existing == member:
                return
            raise SchemaError(
                f"Conflicting {self._label} {'.'.join(key)!r}:\n"
                f"  already declared in {self._modules[key]}: {existing!r}\n"
                f"  redeclared in {module}: {member!r}\n"
                f"One package may not declare one name twice with different definitions. "
                f"Rename one, or make the two definitions identical."
            )
        self._members[key] = member
        self._modules[key] = module

    def get(self, key: _Key) -> typing.Any | None:
        return self._members.get(key)

    def module_of(self, key: _Key) -> str | None:
        return self._modules.get(key)

    def items(self) -> list[tuple[_Key, typing.Any]]:
        return list(self._members.items())

    def values(self) -> list[typing.Any]:
        return list(self._members.values())

    def __contains__(self, key: _Key) -> bool:
        return key in self._members


_default: SchemaRegistry | None = None


def set_default_registry(registry: SchemaRegistry) -> None:
    """Nominate *registry* as the one a schema-less ``rule``/``derived``/``query`` uses.

    The package that declares a schema calls this on import, which is the same
    shape ADR-0017 already uses for registration: a declaration populates the
    registry, rather than the core reaching for a well-known module to find it.
    Before this hook the core reached for ``fine_knowledge.schema`` by name, so
    the engine knew one specific tool's schema -- an R20 violation with nothing
    to catch it, since a deferred import fails no contract.

    Setting the same registry again is a no-op, so re-import is harmless.

    Raises:
        SchemaError: a *different* registry is already the default. Two packages
            each claiming the process-wide default would otherwise resolve by
            import order, and the loser's rules would silently validate against
            the winner's schema.
    """
    global _default
    if _default is not None and _default is not registry:
        raise SchemaError(
            "A different schema registry is already the default. Two packages may "
            "not both claim the process-wide default -- the winner would be decided "
            "by import order. Pass `schema=` explicitly at each rule, predicate and "
            "query instead."
        )
    _default = registry


def default_registry() -> SchemaRegistry:
    """The nominated default registry.

    Raises:
        SchemaError: nothing has nominated one. The engine has no schema of its
            own to fall back to (R20), so there is nothing to guess.
    """
    if _default is None:
        raise SchemaError(
            "No default schema registry has been set, so a rule, predicate or query "
            "declared without `schema=` has no schema to validate against. Import the "
            "package that declares your schema -- declaring it calls "
            "`set_default_registry` -- or pass `schema=` explicitly."
        )
    return _default


class SchemaRegistry:
    """The authority on what the schema contains (ADR-0011).

    Names are qualified without exception -- core's own on identical terms with
    everyone else's (R19) -- and every lookup takes the qualified form. See
    ``model/naming.py`` for why there is no unqualified spelling to fall back
    to.
    """

    def __init__(self) -> None:
        self._entity_types = _Namespace("entity type")
        self._fields = _Namespace("field")
        self._relationships = _Namespace("relationship")
        """Relationship kinds and derived-relation names share one namespace
        (ADR-0012 D-B): a rule body cannot tell whether ``uses_preset`` is
        stored or derived, so the two must not be able to collide."""
        self._providers = _Namespace("provider")
        self._rules = _Namespace("rule")
        """Rule ids are user-visible, stable identifiers -- they appear in
        `Violation.rule`, in diagnostics, and in whatever configuration later
        suppresses or re-severities a rule. Two rules claiming one id must be an
        error rather than a silent overwrite (ADR-0010), which is the same defect
        as N1 and gets the same mechanism, declaring-package qualification
        included."""

    # ---- registration -------------------------------------------------

    def register_namespace(self, ns: type) -> None:
        """Register every schema member *ns* declares, routing each by type (ADR-0017 D2).

        ``Field`` attributes go to the field namespace, ``Relationship``
        attributes to the relationship namespace; a field already carries its
        entity, so nothing needs restating. The declaring package is *ns*'s own
        (D3).

        **A member is registered by existing.** Adding a field to a registered
        ``*Fields`` class registers it; there is no second line to forget, and
        missing registration stops being representable for members of a
        registered namespace. The one hole left -- forgetting
        ``register_namespace`` for a whole class -- is closed by the
        completeness guard in ``tests/test_registry_completeness.py``, not by a
        mechanism.

        Raises:
            SchemaError: *ns* holds a public attribute that is neither a
                ``Field`` nor a ``Relationship``. A namespace class is
                single-purpose, and a silently-ignored member is precisely the
                silent failure D2 exists to remove.
        """
        package = declaring_package(ns)
        for attr_name, member in vars(ns).items():
            if attr_name.startswith("_"):
                continue
            if isinstance(member, Field):
                self._add_field(member, package=package, module=ns.__module__)
            elif isinstance(member, Relationship):
                self._add_relationship(member, package=package, module=ns.__module__)
            else:
                raise SchemaError(
                    f"{ns.__module__}.{ns.__qualname__}.{attr_name} is a "
                    f"{type(member).__name__}, which `register_namespace` cannot route. "
                    "A namespace class holds only Field and Relationship members."
                )

    def register_entity_type(self, t: type[EntityType]) -> None:
        package = declaring_package(t)
        self._entity_types.add((package, t.NAME), t, t.__module__)
        # KEY as well as CORE: a key field is definitionally a field of the entity,
        # and a type whose KEY is not a subset of its CORE would otherwise have
        # unregistered -- so unqualified, so unspellable -- identity fields.
        # Overlap between the two lists lands on D5's no-op clause.
        for f in (*t.KEY, *t.CORE):
            self._add_field(f, package=package, module=t.__module__)

    def register_field(self, f: AnyField, *, package: str) -> None:
        """Register an ad-hoc field -- one with no declaring class to attribute it to.

        ``package=`` is required and explicit (ADR-0017 D2): there is no class
        whose ``__module__`` could supply it, and guessing would be worse than
        asking.
        """
        self._add_field(f, package=package, module=f"<ad hoc: {package}>")

    def register_relationship(self, r: AnyRelationship, *, package: str) -> None:
        """Register an ad-hoc relationship. See ``register_field`` for why ``package=`` is explicit."""
        self._add_relationship(r, package=package, module=f"<ad hoc: {package}>")

    def register_predicate(self, p: DerivedPredicate) -> None:
        """Register a derived predicate into the *relationship* namespace (ADR-0012 D-B).

        One namespace, not two, and that is a strengthening rather than a port.
        ``_relationships`` and ``_derived_relations`` used to be independent
        dicts, so a name could be registered as both -- at which point a
        provider could declare it in ``SUPPLIES_EDGES`` and band discipline was
        bypassed by shadowing. Sharing the namespace makes a name a relationship
        or a predicate, never both.

        Band discipline then lands in three places, none of them new machinery:
        registration (here), ``SUPPLIES`` enforcement (a predicate has no
        ``Relationship`` object for a provider to declare), and ``ingest``'s
        provenance-band check. There is no ``BAND`` to configure -- being a
        predicate *is* being derived.
        """
        package, local_name = split_qualified(p.id)
        self._relationships.add((package, local_name), p, p.id)

    def register_rule(self, r: Rule) -> None:
        package, local_name = split_qualified(r.id)
        self._rules.add((package, local_name), r, r.id)

    def rule(self, rule_id: str) -> Rule:
        found = self._rules.get(split_qualified(rule_id))
        if found is None:
            raise SchemaError(f"Unregistered rule: {rule_id!r}")
        return typing.cast("Rule", found)

    def rules(self) -> list[Rule]:
        return self._rules.values()

    def predicate(self, predicate_id: str) -> DerivedPredicate:
        found = self._relationships.get(split_qualified(predicate_id))
        if found is None or isinstance(found, Relationship):
            raise SchemaError(f"Unregistered derived predicate: {predicate_id!r}")
        return typing.cast("DerivedPredicate", found)

    def register_provider(self, p: type[EntityProvider]) -> None:
        self._providers.add((declaring_package(p), p.ID), p, p.__module__)

    def _add_field(self, f: AnyField, *, package: str, module: str) -> None:
        qualified_entity = qualify(package, f.entity)
        if self._entity_types.get(split_qualified(qualified_entity)) is None:
            raise SchemaError(
                f"Unregistered entity type {qualified_entity!r} for field {f.id!r}. "
                "Register the entity type before the fields that name it."
            )
        f._bind(package, qualified_entity)
        self._fields.add((qualified_entity, package, f.id), f, module)

    def _add_relationship(
        self, r: AnyRelationship, *, package: str, module: str
    ) -> None:
        endpoints = {"src": qualify(package, r.src), "dst": qualify(package, r.dst)}
        for term, qualified in endpoints.items():
            if self._entity_types.get(split_qualified(qualified)) is None:
                raise SchemaError(
                    f"Unregistered {term} entity type {qualified!r} for "
                    f"relationship {r.kind!r}."
                )
        r._bind(package, endpoints["src"], endpoints["dst"])
        self._relationships.add((package, r.kind), r, module)

    # ---- lookup -------------------------------------------------------

    def declaring_module(self, member: AnyField | AnyRelationship) -> str:
        """The module that declared *member* -- ADR-0026 D2's per-member resolution.

        The *module*, not the package: a package may declare its schema across
        several modules, and under UC-0002 a provider supplies from the core
        vocabulary and its own at once. Fingerprinting the declaring module of
        each supplied member gets both right; a well-known ``schema.py`` path
        would be correct only for core-only providers.

        Raises:
            SchemaError: *member* is not registered, so nothing knows where it
                was declared.
        """
        if isinstance(member, Field):
            key = (member.entity, member.package or "", member.id)
            found = self._fields.module_of(key)
        else:
            key = (member.package or "", member.kind)
            found = self._relationships.module_of(key)
        if found is None:
            raise SchemaError(
                f"{member} is not registered, so its declaring module is unknown. "
                "Register its declaring class with `register_namespace` first."
            )
        return found

    def entity_type(self, type_name: str) -> type[EntityType]:
        found = self._entity_types.get(split_qualified(type_name))
        if found is None:
            raise SchemaError(f"Unregistered entity type: {type_name!r}")
        return typing.cast("type[EntityType]", found)

    def relationship(self, kind: str) -> AnyRelationship:
        found = self._relationships.get(split_qualified(kind))
        if not isinstance(found, Relationship):
            raise SchemaError(f"Unregistered relationship kind: {kind!r}")
        return found

    def provider(self, provider_id: str) -> type[EntityProvider]:
        found = self._providers.get(split_qualified(provider_id))
        if found is None:
            raise SchemaError(f"Unregistered provider: {provider_id!r}")
        return typing.cast("type[EntityProvider]", found)

    def providers(self) -> list[type[EntityProvider]]:
        """Every registered provider.

        Needed to attribute a footprint key to the buckets that could have
        answered it: the mapping runs from a key to the providers whose
        ``SUPPLIES`` covers it, so it has to enumerate them."""
        return list(self._providers.values())

    def entity_types(self) -> list[type[EntityType]]:
        """Every registered entity type.

        The three iterators here and ``providers()`` above exist for the same
        reason: a registry *snapshot* (``query/snapshot.py``) has to reproduce
        what is registered, and reading it from the registry rather than from a
        hand-listed set of schema modules is what makes the snapshot unable to
        omit a member somebody added (ADR-0017 D2 -- a member is registered by
        existing).
        """
        return self._entity_types.values()

    def fields(self) -> list[AnyField]:
        """Every registered field, of every entity type and declaring package."""
        return self._fields.values()

    def relationships(self) -> list[AnyRelationship]:
        """Every registered *stored* relationship, excluding derived predicates.

        The two share one namespace (ADR-0012 D-B), so they are separated on the
        way out rather than on the way in.
        """
        return [r for r in self._relationships.values() if isinstance(r, Relationship)]

    def derived_predicates(self) -> list[DerivedPredicate]:
        """Every registered derived predicate -- the other half of that namespace."""
        return [
            r for r in self._relationships.values() if not isinstance(r, Relationship)
        ]

    def fields_of(self, type_name: str) -> list[AnyField]:
        """Every field declared on *type_name*, by any package."""
        return [f for key, f in self._fields.items() if key[0] == type_name]

    def relationships_of(self, type_name: str) -> list[AnyRelationship]:
        """Every stored relationship with *type_name* as ``src`` (ADR-0017 D8).

        Replaces ``EntityType.RELATIONS``: a filter over the registry cannot
        drift from the registry, which a hand-listed class attribute could and
        did (``Project.RELATIONS = []`` against four relationships naming
        ``Project`` as src).
        """
        return [
            r
            for r in self._relationships.values()
            if isinstance(r, Relationship) and r.src == type_name
        ]

    # ---- description --------------------------------------------------

    def describe(self) -> dict:
        """The registry's contents, qualified throughout and grouped by declaring package.

        Names are never abbreviated "when unambiguous" (ADR-0017 D7) -- that is
        the rule that renders ``my_ext.score`` and ``sec_ext.score`` as two rows
        both labelled ``score``. Verbosity is managed by ``packages``, which
        carries the grouping every renderer needs to put the qualifier in a
        section heading instead of on every row.
        """
        entity_types = {
            qualify(package, name): {
                "package": package,
                "key": [f.qualified_name for f in t.KEY],
                "fields": [
                    f.qualified_name for f in self.fields_of(qualify(package, name))
                ],
            }
            for (package, name), t in self._entity_types.items()
        }
        relationships: dict[str, dict] = {}
        derived_predicates: dict[str, dict] = {}
        for (package, local_name), r in self._relationships.items():
            name = qualify(package, local_name)
            if isinstance(r, Relationship):
                relationships[name] = {
                    "package": package,
                    "src": r.src,
                    "dst": r.dst,
                    "band": r.band.value,
                }
            else:
                # `describe()` reports *every* predicate, not only the relation-shaped
                # ones (ADR-0012 D-A): addressability is one of the reasons the fold is
                # worth doing, and a predicate `describe()` cannot name is not
                # addressable. `band` stays "derived" so the output reads uniformly with
                # `relationships` -- being a predicate *is* being derived, so there is
                # nothing to configure.
                src, dst = r.endpoints
                derived_predicates[name] = {
                    "package": package,
                    "shape": r.shape.value,
                    "params": list(r.params),
                    "src": src,
                    "dst": dst,
                    "band": Band.DERIVED.value,
                }
        providers = {
            qualify(package, provider_id): {"package": package}
            for (package, provider_id), _ in self._providers.items()
        }

        packages: dict[str, dict[str, list[str]]] = {}
        for section, described in (
            ("entity_types", entity_types),
            ("relationships", relationships),
            ("derived_predicates", derived_predicates),
            ("providers", providers),
        ):
            for name, spec in described.items():
                packages.setdefault(
                    spec["package"],
                    {
                        "entity_types": [],
                        "relationships": [],
                        "derived_predicates": [],
                        "providers": [],
                    },
                )[section].append(name)

        return {
            "entity_types": entity_types,
            "relationships": relationships,
            "derived_predicates": derived_predicates,
            "providers": providers,
            "packages": packages,
        }
