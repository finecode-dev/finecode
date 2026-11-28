"""Makes ``tests/fixtures/`` importable as top-level packages.

``libcat`` and ``annex_ext`` have to be *top-level* packages for the tests over
them to mean anything: the declaring package is the top level of ``__module__``
(ADR-0017 D3), so a fixture living at ``tests.fixtures.libcat`` would attribute
every name in it to ``tests`` and the qualification tests would silently be about
nothing.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent / "fixtures"))
