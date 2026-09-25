"""Strict dataclass ⇄ JSON Schema service (``IDataclassCodec``).

Reuses the ER's own mechanics so model output is structured by the same rules
an action payload is: :mod:`finecode_extension_runner.schema_utils` for the
schema and the strict cattrs converter for structuring. The converter used here
is a *copy* with ``forbid_extra_keys=True``; the original deliberately tolerates
extra keys for cross-env subaction dispatch and is untouched.
"""

from __future__ import annotations

import typing

import cattrs
from finecode_extension_api.interfaces import idataclasscodec

from finecode_extension_runner import schema_utils
from finecode_extension_runner._converter import payload_converter

T = typing.TypeVar("T")


class DataclassCodec(idataclasscodec.IDataclassCodec):
    """See :class:`finecode_extension_api.interfaces.idataclasscodec.IDataclassCodec`."""

    def __init__(self) -> None:
        self._strict = payload_converter.copy(forbid_extra_keys=True)

    def json_schema(self, cls: type) -> dict[str, typing.Any]:
        try:
            return schema_utils.extract_output_schema(cls)
        except schema_utils.OutputSchemaError as error:
            # The API may not import the ER, so the ER's exception becomes the
            # API's at the boundary.
            raise idataclasscodec.OutputSchemaError(str(error)) from error

    def structure(self, data: typing.Any, cls: type[T]) -> T:
        try:
            return self._strict.structure(data, cls)
        except cattrs.BaseValidationError as error:
            raise idataclasscodec.StructureError(
                "; ".join(cattrs.transform_error(error))
            ) from error
        except (ValueError, TypeError) as error:
            # cattrs only wraps validation failures it raises itself. A hook
            # that raises outside that machinery -- a union that matched no
            # member, for instance -- surfaces here instead.
            raise idataclasscodec.StructureError(str(error)) from error
