"""The nomination hook that replaced the engine's reach into a known schema module.

Three call sites -- ``@q.rule``, ``@q.derived`` and ``q.query()`` -- let a schema
be omitted. Before this hook each resolved the omission by importing
``fine_knowledge.schema`` by name, so the engine knew one specific tool's schema
(an R20 violation, and a deferred import that no contract could catch). They now
read a registry the schema *gave* them.

The direction is the whole point and is what the first test pins: a registry
arrives by being handed over. Nothing here imports a schema.
"""

from __future__ import annotations

import typing

import pytest

from finecode_knowledge import query as q
from finecode_knowledge.model import registry as registry_module
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.registry import (
    SchemaRegistry,
    default_registry,
    set_default_registry,
)


@pytest.fixture(autouse=True)
def _isolated_default() -> typing.Iterator[None]:
    """The default is process-wide, so each test gets it back the way it found it."""
    saved = registry_module._default
    registry_module._default = None
    yield
    registry_module._default = saved


def test_a_nominated_registry_is_what_default_registry_returns() -> None:
    schema = SchemaRegistry()
    set_default_registry(schema)
    assert default_registry() is schema


def test_asking_before_anything_nominates_one_raises_rather_than_guessing() -> None:
    """The engine has no schema of its own to fall back to, so there is nothing to guess.

    The error has to name both ways out, because a developer hitting it has
    either forgotten an import or wanted ``schema=`` all along.
    """
    with pytest.raises(SchemaError) as excinfo:
        default_registry()
    message = str(excinfo.value)
    assert "set_default_registry" in message
    assert "schema=" in message


def test_nominating_the_same_registry_twice_is_a_no_op() -> None:
    """Load-bearing: a schema module re-imported under a second name must not fail."""
    schema = SchemaRegistry()
    set_default_registry(schema)
    set_default_registry(schema)
    assert default_registry() is schema


def test_a_second_different_registry_raises_rather_than_resolving_by_import_order() -> (
    None
):
    """Two packages both claiming the default would otherwise let import order decide.

    The loser's rules would then validate against the winner's schema and fail
    with an unrelated "unregistered field" -- the silent failure this is loud
    about instead.
    """
    first, second = SchemaRegistry(), SchemaRegistry()
    set_default_registry(first)
    with pytest.raises(SchemaError):
        set_default_registry(second)
    assert default_registry() is first


def test_all_three_schema_less_call_sites_reach_the_hook() -> None:
    """``query()``, ``@q.rule`` and ``@q.derived`` were the three back-edges.

    Asserted by the failure they now share when nothing is nominated: each
    raises, so none of them still has a schema of its own to fall back to. Miss
    one and that one keeps a private import of somebody's schema module.
    """
    with pytest.raises(SchemaError):
        q.query()

    with pytest.raises(SchemaError):

        @q.rule
        def a_rule() -> q.Conjunction:  # type: ignore[empty-body]
            """A rule."""

    with pytest.raises(SchemaError):

        @q.derived
        def a_predicate() -> q.Conjunction:  # type: ignore[empty-body]
            """A predicate."""
