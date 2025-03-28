import pathlib
import re

from loguru import logger

from finecode_extension_api.actions.lint import (
    LintMessage,
    LintMessageSeverity,
    Position,
    Range,
)

# reuse output parsing from vscode-mypy (as much as possible, it was adapted for this
# use case)
DIAGNOSTIC_RE = re.compile(
    r"^(?P<location>(?P<filepath>..[^:]*):(?P<line>\d+)(?::(?P<char>\d+))?(?::(?P<end_line>\d+):(?P<end_char>\d+))?): (?P<type>\w+): (?P<message>.*?)(?:  )?(?:\[(?P<code>[\w-]+)\])?$"
)
ERROR_CODE_BASE_URL = "https://mypy.readthedocs.io/en/latest/_refs.html#code-"
SEE_HREF_PREFIX = "See https://mypy.readthedocs.io"
SEE_PREFIX_LEN = len("See ")
LINE_OFFSET = 0
CHAR_OFFSET = 1
NOTE_CODE = "note"


def _get_group_dict(line: str) -> dict[str, str | None] | None:
    match = DIAGNOSTIC_RE.match(line)
    if match:
        return match.groupdict()

    return None


def absolute_path(file_path: str) -> str:
    """Returns absolute path without symlink resolve."""
    return str(pathlib.Path(file_path).absolute())


def _get_severity(
    code: str, code_type: str, severity: dict[str, str]
) -> LintMessageSeverity:
    value = severity.get(code, None) or severity.get(code_type, "error")
    try:
        return LintMessageSeverity[value.upper()]
    except ValueError:
        logger.debug(f"Severity {value.upper()} doesn't exist in LintMessageSeverity")
        pass

    return LintMessageSeverity.INFO


def parse_output_using_regex(
    content: str, severity: dict[str, str]
) -> dict[str, list[LintMessage]]:
    lines: list[str] = content.splitlines()
    diagnostics: dict[str, list[LintMessage]] = {}

    notes = []
    see_href = None

    for i, line in enumerate(lines):
        if line.startswith("'") and line.endswith("'"):
            line = line[1:-1]

        data = _get_group_dict(line)

        if not data:
            continue

        filepath = absolute_path(data["filepath"])
        type_ = data.get("type")
        code = data.get("code")

        if type_ == "note":
            if see_href is None and data["message"].startswith(SEE_HREF_PREFIX):
                see_href = data["message"][SEE_PREFIX_LEN:]

            notes.append(data["message"])

            if i + 1 < len(lines):
                next_line = lines[i + 1]
                next_data = _get_group_dict(next_line)
                if (
                    next_data
                    and next_data["type"] == "note"
                    and next_data["location"] == data["location"]
                ):
                    # the note is not finished yet
                    continue

            message = "\n".join(notes)
            href = see_href
        else:
            message = data["message"]
            href = ERROR_CODE_BASE_URL + code if code else None

        start_line = int(data["line"])
        start_char = int(data["char"] if data["char"] is not None else 1)

        end_line = data["end_line"]
        end_char = data["end_char"]

        end_line = int(end_line) if end_line is not None else start_line
        end_char = int(end_char) + 1 if end_char is not None else start_char

        start = Position(
            line=max(start_line - LINE_OFFSET, 0),
            character=start_char - CHAR_OFFSET,
        )

        end = Position(
            line=max(end_line - LINE_OFFSET, 0),
            character=end_char - CHAR_OFFSET,
        )

        diagnostic = LintMessage(
            range=Range(start=start, end=end),
            message=message,
            severity=_get_severity(code or "", data["type"], severity),
            code=code if code else NOTE_CODE if see_href else None,
            code_description=href,
            source="mypy",
        )
        if filepath in diagnostics:
            diagnostics[filepath].append(diagnostic)
        else:
            diagnostics[filepath] = [diagnostic]

        notes = []
        see_href = None

    return diagnostics
