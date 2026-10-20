from __future__ import annotations

import typing

from finecode_extension_api import service

__all__ = ["IDataclassCodec", "OutputSchemaError", "StructureError"]

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


class IDataclassCodec(service.Service, typing.Protocol):
    """Turn a dataclass into a JSON Schema, and model output back into one.

    The two directions are deliberately asymmetric. ``json_schema`` is complete
    or it raises: a field it cannot describe would be presented to the model as
    no constraint at all, which silently produces output that cannot be
    structured. ``structure`` is strict in the same spirit: a key the dataclass
    does not declare is an error, because model output is not a cross-env
    payload and tolerating extra keys would hide a model inventing fields.

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

    def structure(self, data: typing.Any, cls: type[T]) -> T:
        """Structure decoded JSON *data* into an instance of *cls*.

        Args:
            data: A decoded JSON value, as it came from the model. Typed
                ``Any`` because the public API has no JSON value type, and the
                implementation's is deliberately internal.
            cls: The dataclass to build.

        Returns:
            An instance of *cls*, with enum fields as real enum members.

        Raises:
            StructureError: *data* does not fit *cls*. ``message`` names the
                offending path, so a task handler can return it as the run's
                error without re-deriving what went wrong.
        """
        ...
