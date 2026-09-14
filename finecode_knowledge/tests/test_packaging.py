"""Guards the two properties that are packaging facts rather than code (C1, R20).

``py.typed``: the marker exists on disk but is only shipped in the built wheel if
setuptools is told to include it via ``package-data``. Without it, a third-party
rule module importing ``finecode_knowledge.query`` sees ``Any`` everywhere and
the NFR2 typing guarantee silently evaporates.

**The R20 boundary**: memo-dag-plan D-1 makes the engine/schema split a
*packaging* fact -- the WM cannot import a rule because the distribution holding
rules is not installed in its environment -- rather than a lint rule that decays
on the first convenient import. That only holds while this distribution contains
no schema, so the containment is asserted here, at the boundary it protects.
"""

import pathlib
import re
import tomllib

_PACKAGE_ROOT = pathlib.Path(__file__).parent.parent
_SRC = _PACKAGE_ROOT / "src" / "finecode_knowledge"


def test_py_typed_marker_exists_on_disk() -> None:
    assert (_SRC / "py.typed").is_file()


def test_py_typed_is_declared_as_package_data() -> None:
    pyproject = tomllib.loads((_PACKAGE_ROOT / "pyproject.toml").read_text())
    package_data = pyproject["tool"]["setuptools"]["package-data"]["finecode_knowledge"]
    assert "py.typed" in package_data


_IMPORTS_THE_SCHEMA_HALF = re.compile(
    r"^\s*(?:from|import)\s+fine_knowledge\b", re.MULTILINE
)


def test_the_engine_distribution_never_imports_the_finecode_schema() -> None:
    """No module here may import ``fine_knowledge`` -- the schema half.

    A deferred import inside a function is exactly how the three back-edges this
    split cut got in: ``query/rule.py``, ``query/predicate.py`` and
    ``query/query.py`` each did ``from fine_knowledge.schema import
    FINECODE_SCHEMA`` at call time. So this reads the source text rather than the
    import graph -- nothing fails at import time when the edge is deferred, and a
    graph walk over a passing test run would never traverse it. The leading
    ``\\s*`` is the whole point: an unindented-only check would have missed all
    three.

    Prose is left alone deliberately. ``fine_knowledge`` is also the *qualifier*
    on every core schema name (``fine_knowledge.Package``), so the docstrings in
    ``model/naming.py`` and ``model/fields.py`` say it constantly while importing
    nothing.
    """
    offenders = sorted(
        str(path.relative_to(_SRC))
        for path in _SRC.rglob("*.py")
        if _IMPORTS_THE_SCHEMA_HALF.search(path.read_text())
    )
    assert offenders == [], (
        f"{offenders} import `fine_knowledge`, the FineCode schema distribution. "
        "The engine is handed a SchemaRegistry; it does not go looking for one (R20)."
    )


def test_the_engine_declares_no_dependency_on_the_schema_half() -> None:
    """The source check above is only as good as the dependency list under it."""
    pyproject = tomllib.loads((_PACKAGE_ROOT / "pyproject.toml").read_text())
    runtime = pyproject["project"]["dependencies"]
    assert not any(spec.startswith("fine_knowledge") for spec in runtime), runtime
