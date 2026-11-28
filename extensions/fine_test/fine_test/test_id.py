"""
Unified test identifier shared by ``list_tests`` and ``run_tests``.

Design decisions
----------------
**Handlers own the conversion, not the action layer.**
    Different test runners use incompatible identifier formats.
    Rather than keeping format strings in the action API (which leaks
    runner details to callers) or normalising to a lowest common
    denominator (lossy), ``TestId`` is a structured representation that
    every handler converts to and from its native format internally.
    Callers remain fully agnostic: they receive ``TestId`` objects from
    ``list_tests`` and pass them back to ``run_tests`` unchanged.

**``class_name`` reflects actual source structure — never synthetic grouping.**
    This is the critical contract for tree consistency.  If a test runner
    supports optional classes (e.g. pytest top-level functions have no
    class), ``class_name`` must be ``None`` for those tests — not a
    made-up grouping like ``"<module>"`` or ``"default"``.

    Why it matters: ``TestItem`` trees produced by ``list_tests`` are
    derived directly from ``TestId`` fields.  A handler that sets
    ``class_name`` on class-less tests adds an extra level to the tree
    that callers (e.g. a VSCode TestController) do not expect.  Honouring
    this contract is what makes tree shape consistent across handlers,
    even when no single utility function can enforce it.

**``variant`` always requires ``test_name``.**
    A parametrized variant has no meaning without the test function it
    belongs to.  ``TestId(file_path=..., variant="[p1]")`` is invalid.

**Round-trip guarantee.**
    Every ``TestId`` returned by a ``list_tests`` handler is a valid
    input to the same (or a compatible) ``run_tests`` handler.  Handlers
    must ensure that converting a ``TestId`` to their native format and
    back produces an equivalent ``TestId``.

Valid field combinations
------------------------
+---------------+------------+-----------+---------+-----------------------------------+
| ``file_path`` |``class_name``|``test_name``|``variant``| Meaning                        |
+===============+============+===========+=========+===================================+
| ✓             |            |           |         | Whole file (run all tests in it)  |
+---------------+------------+-----------+---------+-----------------------------------+
| ✓             | ✓          |           |         | Whole class / suite               |
+---------------+------------+-----------+---------+-----------------------------------+
| ✓             |            | ✓         |         | Top-level test function           |
+---------------+------------+-----------+---------+-----------------------------------+
| ✓             | ✓          | ✓         |         | Method inside a class             |
+---------------+------------+-----------+---------+-----------------------------------+
| ✓             |            | ✓         | ✓       | Parametrized top-level function   |
+---------------+------------+-----------+---------+-----------------------------------+
| ✓             | ✓          | ✓         | ✓       | Parametrized method               |
+---------------+------------+-----------+---------+-----------------------------------+
"""

from __future__ import annotations

import dataclasses

from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass(frozen=True)
class TestId:
    """Unified, handler-agnostic test identifier.

    Represents a test or test group at any level of the hierarchy:
    file, class, function, or parametrized variant.

    ``file_path`` is a :class:`~finecode_extension_api.resource_uri.ResourceUri`
    (``file://`` URI) pointing to the test file.  Handlers convert to and
    from their native format (e.g. pytest node IDs, unittest dotted names)
    internally — see module docstring.

    Conversion examples — pytest
    ----------------------------
    ``"tests/test_foo.py"``
        → ``TestId(file_path=path_to_resource_uri(project_dir / "tests/test_foo.py"))``
    ``"tests/test_foo.py::MyClass"``
        → ``TestId(file_path=..., class_name="MyClass")``
    ``"tests/test_foo.py::test_bar"``
        → ``TestId(file_path=..., test_name="test_bar")``
    ``"tests/test_foo.py::MyClass::test_bar[p1-p2]"``
        → ``TestId(file_path=..., class_name="MyClass",
                   test_name="test_bar", variant="[p1-p2]")``

    Conversion examples — unittest
    -------------------------------
    ``"my_module.MyClass.test_bar"``
        → ``TestId(file_path=path_to_resource_uri(resolved_file),
                   class_name="MyClass", test_name="test_bar")``
        (handler resolves module → file path)
    """

    file_path: ResourceUri
    """``file://`` URI of the test file, e.g.
    ``"file:///home/user/project/tests/test_foo.py"``.

    Always set.  For runners that use module names (e.g. unittest), the
    handler is responsible for resolving the module to a file URI.
    """

    class_name: str | None = None
    """Test class or suite name, e.g. ``"MyClass"``.

    Set **only** when the test or group belongs to an actual class or
    suite in the source file.  Must be ``None`` for top-level functions
    that have no enclosing class — even if the runner would accept a
    synthetic class name.  Violating this produces extra levels in the
    ``TestItem`` tree that callers do not expect.
    """

    test_name: str | None = None
    """Test function or method name, e.g. ``"test_bar"``.

    ``None`` when the ``TestId`` identifies a file or class scope rather
    than a specific test.  Required when ``variant`` is set.
    """

    variant: str | None = None
    """Parametrize variant string **including brackets**, e.g. ``"[p1-p2]"``.

    Only set for parametrized tests.  Requires ``test_name`` to be set.
    The string is taken verbatim from the runner output; handlers must
    not strip or reformat the brackets.
    """

    def __str__(self) -> str:
        """Return a ``::``-separated representation for display and logging."""
        parts = [self.file_path]
        if self.class_name:
            parts.append(self.class_name)
        if self.test_name:
            name = self.test_name
            if self.variant:
                name += self.variant
            parts.append(name)
        return "::".join(parts)
