"""Golden-file guard for ``schema_utils.extract_payload_schema``.

The payload schema is a wire contract: ``actions/getPayloadSchemas`` hands it to
the WM, which presents it to MCP clients as tool parameters. A change to the
type mapping that is invisible in one action still changes what every client is
told about every action, so the mapping is pinned here rather than only asserted
through the actions that happen to use it.

The golden file was captured before output-mode support was added to
``_type_to_schema``; keeping it captured from the unmodified code is what makes
it evidence that output mode left the payload path alone.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import pathlib
import typing

from finecode_extension_api.resource_uri import ResourceUri

from finecode_extension_runner import schema_utils

_GOLDEN_PATH = pathlib.Path(__file__).parent / "data" / "payload_schema_golden.json"


class _Color(enum.StrEnum):
    RED = "red"
    GREEN = "green"


@dataclasses.dataclass
class _Nested:
    inner: int
    label: str = ""
    """A label."""


@dataclasses.dataclass
class _ScalarsPayload:
    a_bool: bool
    an_int: int
    a_float: float
    a_str: str
    a_path: pathlib.Path


@dataclasses.dataclass
class _ResourceUriPayload:
    uri: ResourceUri


@dataclasses.dataclass
class _EnumPayload:
    color: _Color
    """The chosen colour."""


@dataclasses.dataclass
class _ListPayload:
    ints: list[int]
    bare: list


@dataclasses.dataclass
class _OptionalPayload:
    maybe_int: int | None
    maybe_str: str | None = None


@dataclasses.dataclass
class _UnionPayload:
    int_or_str: int | str


@dataclasses.dataclass
class _NestedPayload:
    nested: _Nested
    """The nested object."""


@dataclasses.dataclass
class _MappingPayload:
    mapping: dict[str, int]


@dataclasses.dataclass
class _LiteralPayload:
    literal: typing.Literal["a", "b"]


@dataclasses.dataclass
class _AnyPayload:
    anything: typing.Any


_FIXTURE_CLASSES: tuple[type, ...] = (
    _ScalarsPayload,
    _ResourceUriPayload,
    _EnumPayload,
    _ListPayload,
    _OptionalPayload,
    _UnionPayload,
    _NestedPayload,
    _MappingPayload,
    _LiteralPayload,
    _AnyPayload,
)


def _golden_name(cls: type) -> str:
    return cls.__qualname__


def _build_golden() -> dict[str, dict]:
    return {
        _golden_name(cls): schema_utils.extract_payload_schema(cls)
        for cls in _FIXTURE_CLASSES
    }


def _load_golden() -> dict[str, dict]:
    return json.loads(_GOLDEN_PATH.read_text("utf-8"))


def test_payload_schema_matches_golden() -> None:
    """Every branch of the payload type mapping stays byte-identical.

    A client that receives a schema it did not expect cannot build a valid
    call, and the failure surfaces as a rejected payload far from the mapping
    that changed. Pinning the mapping here reports the change at the mapping.
    """
    golden = _load_golden()
    current = _build_golden()
    for name, expected in golden.items():
        actual = current[name]
        assert json.dumps(actual, sort_keys=True) == json.dumps(
            expected, sort_keys=True
        ), name


def _regen() -> None:
    _GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _GOLDEN_PATH.write_text(
        json.dumps(_build_golden(), indent=2, sort_keys=True) + "\n", "utf-8"
    )


if __name__ == "__main__":
    import sys

    if "--regen" in sys.argv:
        _regen()
    else:
        raise SystemExit("pass --regen to rewrite the golden file")
