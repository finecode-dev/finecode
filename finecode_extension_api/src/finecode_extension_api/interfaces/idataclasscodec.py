from __future__ import annotations

import enum
import typing

from finecode_extension_api import service

__all__ = ["IDataclassCodec", "OutputSchemaError", "StructureError", "UnknownFields"]

T = typing.TypeVar("T")


class StructureError(Exception):
    """Structuring data into a dataclass failed.

    ``message`` is the same path-precise text the exception prints, exposed as
    an attribute because task handlers put it in a result's ``error`` field and
    the string is what a caller reads.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class OutputSchemaError(TypeError):
    """A dataclass field has no complete JSON Schema mapping.

    Raised by ``json_schema`` for a field whose type cannot be described. The
    message names the field path and the type. The implementation raises its own
    equivalent (see ``finecode_extension_runner.schema_utils``) and translates
    it here, so this module depends on no implementation.
    """


class UnknownFields(enum.Enum):
    """What ``structure`` does with a key the target dataclass does not declare."""

    FORBID = enum.auto()
    IGNORE = enum.auto()


class IDataclassCodec(service.Service, typing.Protocol):
    """Turn a dataclass into a JSON Schema, and decoded JSON back into one.

    The two directions are deliberately asymmetric. ``json_schema`` is complete
    or it raises: a field it cannot describe would be presented to the model as
    no constraint at all, which silently produces output that cannot be
    structured. ``structure`` is strict by default in the same spirit: a key
    the dataclass does not declare is an error, because model output is not a
    cross-env payload and tolerating extra keys would hide a model inventing
    fields. ``UnknownFields.IGNORE`` is for decoded external JSON whose
    producer adds metadata the caller does not read: it drops only undeclared
    keys, at every dataclass object level including list items, while required
    fields and declared types stay validated. ``json_schema`` is unaffected by
    the option.

    Use enums, not ``typing.Literal``, for a closed set of values: output mode
    has no ``Literal`` mapping and says so where it fails.
    """

    def json_schema(self, cls: type) -> dict[str, typing.Any]:
        """Return a complete JSON Schema for *cls*, a dataclass.

        The result carries ``"additionalProperties": false`` at every object
        level, an explicit ``null`` arm for an optional field, and a
        ``description`` for each field that has an attribute docstring.

        Raises:
            OutputSchemaError: a field's type has no mapping. The message names
                the field path and the type, and suggests an enum where
                ``typing.Literal`` was used.
            TypeError: *cls* is not a dataclass.
        """
        ...

    def structure(
        self,
        data: typing.Any,
        cls: type[T],
        *,
        unknown_fields: UnknownFields = UnknownFields.FORBID,
    ) -> T:
        """Structure decoded JSON *data* into an instance of *cls*.

        Args:
            data: A decoded JSON value. Typed ``Any`` because the public API
                has no JSON value type, and the implementation's is
                deliberately internal.
            cls: The dataclass to build.
            unknown_fields: ``FORBID`` (the default) rejects a key the
                dataclass does not declare; ``IGNORE`` drops such keys at
                every dataclass object level.

        Returns:
            An instance of *cls*, with enum fields as real enum members.

        Raises:
            StructureError: *data* does not fit *cls*. ``message`` names the
                offending path, so a task handler can return it as the run's
                error without re-deriving what went wrong.
        """
        ...
