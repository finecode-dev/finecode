"""Tests for the strict dataclass codec.

The codec is what turns a task handler's report type into the schema a model is
asked to satisfy, and what checks the model's answer against that type. Its two
rules -- a complete schema or an error, and no tolerance for extra keys -- are
what keep a model from silently returning a report the handler cannot read.
"""

from __future__ import annotations

import dataclasses
import enum
import pathlib
import typing

import cattrs
import pytest
from finecode_extension_api.interfaces import idataclasscodec
from finecode_extension_api.interfaces.idataclasscodec import IDataclassCodec

from finecode_extension_runner._converter import payload_converter
from finecode_extension_runner.impls.dataclass_codec import DataclassCodec
from finecode_extension_runner.testing import handler_test_session


class _TaskStatus(enum.StrEnum):
    DONE = "done"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclasses.dataclass
class _TaskOutcome:
    number: int
    """The plan task number."""
    title: str
    """The task title."""
    status: _TaskStatus
    """How the task ended."""
    detail: str = ""
    """What changed, or why the task did not finish."""


@dataclasses.dataclass
class _Report:
    tasks: list[_TaskOutcome]
    """One entry per plan task."""
    deviations: list[str]
    """Anything that differed from the plan."""
    note: str | None = None
    """An optional free-form note."""


@dataclasses.dataclass
class _BadDict:
    value: dict[str, int]


@dataclasses.dataclass
class _BadAny:
    value: typing.Any


@dataclasses.dataclass
class _BadLiteral:
    value: typing.Literal["a", "b"]


@dataclasses.dataclass
class _BadBareList:
    value: list


@dataclasses.dataclass
class _BadUnion:
    value: int | str


@dataclasses.dataclass
class _BadNested:
    items: list[typing.Any]


def test_json_schema_is_complete_and_strict() -> None:
    """A report with an extra key must be describable as closed and optional
    fields must carry an explicit null arm.

    A schema that leaves an object open tells the model nothing about the keys
    it may add, and the handler rejects those keys. A `T | None` field described
    only as `T` drops the only legal way to say "absent", so a model with
    nothing to report is forced to invent a value.
    """
    schema = DataclassCodec().json_schema(_Report)

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["tasks", "deviations"]
    assert schema["properties"]["note"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "description": "An optional free-form note.",
    }

    task = schema["properties"]["tasks"]["items"]
    assert task["type"] == "object"
    assert task["additionalProperties"] is False
    assert task["required"] == ["number", "title", "status"]
    assert task["properties"]["status"] == {
        "type": "string",
        "enum": ["done", "blocked", "skipped"],
        "description": "How the task ended.",
    }
    assert task["properties"]["number"]["description"] == "The plan task number."
    assert task["properties"]["detail"]["description"] == (
        "What changed, or why the task did not finish."
    )


@pytest.mark.parametrize(
    ("cls", "path", "type_name"),
    [
        (_BadDict, "$.value", "dict"),
        (_BadAny, "$.value", "Any"),
        (_BadLiteral, "$.value", "Literal"),
        (_BadBareList, "$.value", "list"),
        (_BadUnion, "$.value", "int | str"),
        (_BadNested, "$.items[]", "Any"),
    ],
)
def test_json_schema_rejects_unmapped_types_with_path(
    cls: type, path: str, type_name: str
) -> None:
    """An unmapped field type must fail naming the field and its type.

    A silent `{}` for such a field gives the model no constraint and produces an
    answer the handler cannot structure; the error has to say which field, and
    at what depth, so the dataclass can be fixed.
    """
    with pytest.raises(idataclasscodec.OutputSchemaError) as exc_info:
        DataclassCodec().json_schema(cls)

    message = str(exc_info.value)
    assert path in message
    assert type_name in message


def test_json_schema_suggests_enum_for_literal() -> None:
    """`Literal` is not mapped, and the error says what to use instead.

    The advice is the whole value of raising here: the fix is mechanical, and a
    message that only names the type leaves the author guessing.
    """
    with pytest.raises(idataclasscodec.OutputSchemaError) as exc_info:
        DataclassCodec().json_schema(_BadLiteral)

    assert "use an enum.Enum" in str(exc_info.value)


def test_json_schema_rejects_non_dataclass() -> None:
    """Asking for a schema of a non-dataclass is a caller error, not a mapping gap."""
    with pytest.raises(TypeError):
        DataclassCodec().json_schema(int)


def test_structure_returns_real_enum_members() -> None:
    """Structuring must yield the dataclass with enum members, not raw values.

    A caller comparing `status is PlanTaskStatus.DONE` gets a wrong answer if
    the enum degrades to its string value, and that comparison is the whole
    point of having a typed result.
    """
    report = DataclassCodec().structure(
        {
            "tasks": [{"number": 1, "title": "t", "status": "done"}],
            "deviations": [],
        },
        _Report,
    )

    assert isinstance(report, _Report)
    assert report.tasks[0].status is _TaskStatus.DONE


def test_structure_rejects_extra_top_level_key() -> None:
    """A key the report type does not declare must be an error, not ignored.

    `payload_converter` deliberately tolerates extra keys for cross-env
    dispatch; model output has no such need, and tolerating them would let a
    model invent a field the handler never reads while the run reports success.
    """
    with pytest.raises(idataclasscodec.StructureError) as exc_info:
        DataclassCodec().structure({"tasks": [], "deviations": [], "extra": 1}, _Report)

    assert "extra fields found" in exc_info.value.message
    assert "$" in exc_info.value.message


def test_structure_rejects_extra_nested_key() -> None:
    """Strictness applies at every depth, not only the top level."""
    with pytest.raises(idataclasscodec.StructureError) as exc_info:
        DataclassCodec().structure(
            {
                "tasks": [{"number": 1, "title": "t", "status": "done", "extra": 2}],
                "deviations": [],
            },
            _Report,
        )

    assert "extra fields found" in exc_info.value.message


def test_structure_rejects_bool_for_int() -> None:
    """JSON `true` must not satisfy an integer field."""
    with pytest.raises(idataclasscodec.StructureError) as exc_info:
        DataclassCodec().structure(
            {
                "tasks": [{"number": True, "title": "t", "status": "done"}],
                "deviations": [],
            },
            _Report,
        )

    assert "expected int" in exc_info.value.message
    assert "$.tasks.number" in exc_info.value.message


def test_structure_rejects_unknown_enum_value() -> None:
    """An enum value outside the declared members must fail."""
    with pytest.raises(idataclasscodec.StructureError) as exc_info:
        DataclassCodec().structure(
            {
                "tasks": [{"number": 1, "title": "t", "status": "nope"}],
                "deviations": [],
            },
            _Report,
        )

    assert "expected _TaskStatus" in exc_info.value.message


def test_structure_rejects_scalar_for_list() -> None:
    """A scalar for a list field must fail rather than iterate its characters."""
    with pytest.raises(idataclasscodec.StructureError) as exc_info:
        DataclassCodec().structure({"tasks": [], "deviations": "not a list"}, _Report)

    assert "expected list" in exc_info.value.message
    assert "$.deviations" in exc_info.value.message


@dataclasses.dataclass
class _Inner:
    level: int
    flag: bool


@dataclasses.dataclass
class _Outer:
    inner: _Inner
    tasks: list[_TaskOutcome]
    label: str


def _outer_body_with_extras() -> dict:
    return {
        "inner": {"level": 1, "flag": True, "extra": 1},
        "tasks": [{"number": 1, "title": "t", "status": "done", "extra": 2}],
        "label": "x",
        "extra": 0,
    }


def test_structure_explicit_forbid_matches_default() -> None:
    """An explicit opt-out must reject exactly like the default call.

    Callers that spell out the default policy rely on it meaning strict; if it
    silently tolerated extras, a report with invented fields would pass the
    check meant to catch it.
    """
    bodies: list[dict[str, object]] = [
        {"tasks": [], "deviations": [], "extra": 1},
        {
            "tasks": [{"number": 1, "title": "t", "status": "done", "extra": 2}],
            "deviations": [],
        },
    ]
    for body in bodies:
        with pytest.raises(idataclasscodec.StructureError) as exc_info:
            DataclassCodec().structure(
                body,
                _Report,
                unknown_fields=idataclasscodec.UnknownFields.FORBID,
            )
        assert "extra fields found" in exc_info.value.message


def test_ignore_drops_unknown_fields_at_every_level() -> None:
    """Opt-in tolerance must drop undeclared keys wherever they appear.

    External JSON producers add root and per-item metadata the caller never
    reads; rejecting it would fail listings that carry exactly the files being
    asked for.
    """
    outer = DataclassCodec().structure(
        _outer_body_with_extras(),
        _Outer,
        unknown_fields=idataclasscodec.UnknownFields.IGNORE,
    )

    assert outer.inner.level == 1
    assert outer.inner.flag is True
    assert outer.tasks[0].status is _TaskStatus.DONE
    assert outer.label == "x"


@pytest.mark.parametrize(
    ("body", "path"),
    [
        (
            {
                "inner": {"level": 1, "flag": True},
                "tasks": [],
                "extra": 0,
            },
            "$.label",
        ),
        (
            {
                "inner": {"level": True, "flag": True, "extra": 1},
                "tasks": [],
                "label": "x",
                "extra": 0,
            },
            "$.inner.level",
        ),
        (
            {
                "inner": {"level": 1, "flag": 1, "extra": 1},
                "tasks": [],
                "label": "x",
                "extra": 0,
            },
            "$.inner.flag",
        ),
        (
            {
                "inner": {"level": 1, "flag": True},
                "tasks": [],
                "label": 1,
                "extra": 0,
            },
            "$.label",
        ),
        (
            {
                "inner": {"level": 1, "flag": True},
                "tasks": [{"number": 1, "title": "t", "status": "nope"}],
                "label": "x",
                "extra": 0,
            },
            "$.tasks",
        ),
        (
            {
                "inner": {"level": 1, "flag": True},
                "tasks": "x",
                "label": "x",
                "extra": 0,
            },
            "$.tasks",
        ),
    ],
)
def test_ignore_still_rejects_invalid_declared_fields(body: dict, path: str) -> None:
    """Tolerance must not weaken validation of fields the type declares.

    Dropping unknown keys is only safe while a wrong-typed declared field still
    fails; otherwise a corrupt listing entry would structure into a value the
    caller trusts.
    """
    with pytest.raises(idataclasscodec.StructureError) as exc_info:
        DataclassCodec().structure(
            body, _Outer, unknown_fields=idataclasscodec.UnknownFields.IGNORE
        )

    assert path in exc_info.value.message
    assert "extra fields found" not in exc_info.value.message


def test_ignore_on_report_returns_real_enum_members() -> None:
    """Tolerant structuring must still return enum members, not raw values.

    Callers compare the result against enum members; a string where a member
    belongs silently inverts that comparison.
    """
    report = DataclassCodec().structure(
        {
            "tasks": [{"number": 1, "title": "t", "status": "done", "extra": 2}],
            "deviations": [],
            "extra": 1,
        },
        _Report,
        unknown_fields=idataclasscodec.UnknownFields.IGNORE,
    )

    assert report.tasks[0].status is _TaskStatus.DONE


def test_json_schema_unchanged_by_tolerant_structuring() -> None:
    """A tolerant call must not change the schema the model is asked to satisfy.

    The schema is the contract handed to the model; if structuring rewrote it,
    later runs would constrain the model differently depending on call history.
    """
    before = DataclassCodec().json_schema(_Report)
    DataclassCodec().structure(
        {"tasks": [], "deviations": [], "extra": 1},
        _Report,
        unknown_fields=idataclasscodec.UnknownFields.IGNORE,
    )
    after = DataclassCodec().json_schema(_Report)

    assert before == after
    with pytest.raises(idataclasscodec.OutputSchemaError):
        DataclassCodec().json_schema(_BadDict)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [["F", "I", "F"], ["I", "F", "I"]])
async def test_policies_do_not_leak_through_one_injected_codec(
    tmp_path: pathlib.Path, order: list[str]
) -> None:
    """Each call through one injected codec must honour its own policy.

    Handlers share a single codec instance; a tolerant call that loosened the
    next strict call would let invented model fields slip through unnoticed.
    """
    body: dict[str, object] = {"tasks": [], "deviations": [], "extra": 1}
    async with handler_test_session(project_dir=tmp_path, actions={}) as session:
        codec = await session.service(IDataclassCodec)
        assert isinstance(codec, DataclassCodec)
        for policy in order:
            if policy == "F":
                with pytest.raises(idataclasscodec.StructureError) as exc_info:
                    codec.structure(
                        body,
                        _Report,
                        unknown_fields=idataclasscodec.UnknownFields.FORBID,
                    )
                assert "extra fields found" in exc_info.value.message
            else:
                report = codec.structure(
                    body,
                    _Report,
                    unknown_fields=idataclasscodec.UnknownFields.IGNORE,
                )
                assert isinstance(report, _Report)


def test_payload_converter_tolerance_survives_codec_calls() -> None:
    """Codec calls must not change the shared payload converter's tolerance.

    Cross-env dispatch relies on the shared converter ignoring extra keys while
    still type-checking; if a codec call rewrote it, subaction dispatch would
    start failing or mistyping after the first strict call.
    """
    codec = DataclassCodec()
    codec.structure(
        {"tasks": [], "deviations": [], "extra": 1},
        _Report,
        unknown_fields=idataclasscodec.UnknownFields.IGNORE,
    )
    codec.structure({"tasks": [], "deviations": []}, _Report)

    assert (
        payload_converter.structure(
            {"tasks": [], "deviations": [], "extra": 1}, _Report
        ).deviations
        == []
    )
    with pytest.raises(cattrs.BaseValidationError):
        payload_converter.structure(
            {
                "tasks": [{"number": True, "title": "t", "status": "done"}],
                "deviations": [],
            },
            _Report,
        )


def test_old_two_argument_form_still_strict() -> None:
    """The default call must stay strict even after a tolerant one.

    Existing callers pass no policy; if a prior tolerant call loosened the
    default, those callers would silently accept invented fields.
    """
    codec = DataclassCodec()
    codec.structure(
        {"tasks": [], "deviations": [], "extra": 1},
        _Report,
        unknown_fields=idataclasscodec.UnknownFields.IGNORE,
    )
    with pytest.raises(idataclasscodec.StructureError):
        codec.structure(
            {
                "tasks": [{"number": 1, "title": "t", "status": "done", "extra": 2}],
                "deviations": [],
            },
            _Report,
        )
