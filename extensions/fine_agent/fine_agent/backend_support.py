"""Helpers shared by the agent backends.

Backends differ in how they obtain a machine-readable final answer -- one has a
native schema flag, another has to be asked for a fenced block -- but the
profile error and the fenced-block convention itself are backend-independent, so
they live here rather than in each handler.
"""

from __future__ import annotations

import json
import re
import typing

__all__ = [
    "InvalidJsonBlock",
    "MissingJsonBlock",
    "StructuredOutputError",
    "extract_last_json_block",
    "json_output_instruction",
    "spawn_error",
    "unknown_profile_error",
]

_JSON_BLOCK_RE = re.compile(r"```json[ \t]*\n(.*?)\n```", re.DOTALL)


class StructuredOutputError(ValueError):
    """A backend could not extract the requested structured output.

    `str()` is the text a backend puts in the result's `error`, so it reads as
    a complete explanation on its own.
    """


class MissingJsonBlock(StructuredOutputError):
    def __init__(self) -> None:
        super().__init__("no fenced json block")


class InvalidJsonBlock(StructuredOutputError):
    def __init__(self, json_error: str) -> None:
        super().__init__(f"invalid JSON in the final json block: {json_error}")


def unknown_profile_error(name: str, configured: typing.Iterable[str]) -> str:
    """The failure message for a profile name the handler config does not declare.

    Identical across backends so a caller sees one wording however the run was
    dispatched, and naming the configured profiles so the fix is visible without
    reading config.
    """
    names = ", ".join(sorted(configured)) or "none"
    return f"unknown agent profile {name!r}; configured profiles: {names}"


def spawn_error(program: str, error: BaseException) -> str:
    """The failure message for a backend process that could not be started.

    `run()` raises instead of returning an exit code for an unstartable
    program -- missing from PATH being only one cause (a missing `cwd` is the
    same `FileNotFoundError`) -- so the message names the program without
    claiming where it should have come from.
    """
    detail = getattr(error, "strerror", None) or str(error)
    return f"could not start {program}: {detail}"


def json_output_instruction(schema: dict[str, typing.Any]) -> str:
    """The prompt suffix asking for a schema-matching fenced JSON block.

    Appended to the caller's prompt rather than replacing any part of it, so the
    task wording is unchanged and the instruction is the only thing a model sees
    about output shape.
    """
    return (
        "\n\nEnd your reply with exactly one fenced ```json block containing a "
        "single JSON value that matches this JSON Schema. Put nothing after it."
        "\n\n```json\n"
        f"{json.dumps(schema, indent=2)}"
        "\n```\n"
    )


def extract_last_json_block(text: str) -> typing.Any:
    """Decode the last fenced ```json block in *text*.

    The last block is taken because a model that shows intermediate JSON and
    then a final answer puts the answer last; an earlier block is context, not
    the result. Raises :class:`MissingJsonBlock` or :class:`InvalidJsonBlock`
    with a message the backend can use as its error unchanged.
    """
    matches = _JSON_BLOCK_RE.findall(text)
    if not matches:
        raise MissingJsonBlock()
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError as error:
        raise InvalidJsonBlock(str(error)) from error
