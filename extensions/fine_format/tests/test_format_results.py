import pytest
from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri

from fine_format import format_file_action, format_files_action
from fine_format.format_file_action import FormatFileStatus


def _plain(text: str | textstyler.StyledText) -> str:
    if isinstance(text, str):
        return text
    return "".join(
        part if isinstance(part, str) else str(part["text"]) for part in text.text_parts
    )


def test_formatter_contribution_keeps_the_saved_status() -> None:
    """Formatter handlers leave the status unset; merging their results must not
    erase the status the save handler reported."""
    accumulated = format_file_action.FormatFileRunResult(
        changed=False, code="", status=FormatFileStatus.SAVED
    )

    accumulated.update(format_file_action.FormatFileRunResult(changed=True, code="x"))

    assert accumulated.status is FormatFileStatus.SAVED
    assert accumulated.changed is True


def test_format_files_text_names_what_happened_to_each_file() -> None:
    result = format_files_action.FormatFilesRunResult(
        result_by_file_path={
            ResourceUri("file:///saved.py"): format_files_action.FormatRunFileResult(
                changed=True, code="x", status=FormatFileStatus.SAVED
            ),
            ResourceUri("file:///dry.py"): format_files_action.FormatRunFileResult(
                changed=True, code="x", status=FormatFileStatus.CHANGED
            ),
            ResourceUri("file:///refused.py"): format_files_action.FormatRunFileResult(
                changed=True, code="x", status=FormatFileStatus.SAVE_REFUSED
            ),
            ResourceUri("file:///same.py"): format_files_action.FormatRunFileResult(
                changed=False, code="", status=FormatFileStatus.UNCHANGED
            ),
        }
    )

    text = _plain(result.to_text())

    assert "reformatted file:///saved.py" in text
    assert "would reformat file:///dry.py" in text
    assert "not saved (changed since it was read): file:///refused.py" in text
    assert "file:///same.py" not in text
    assert "1 files unchanged." in text


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (FormatFileStatus.UNCHANGED, code_action.RunReturnCode.SUCCESS),
        (FormatFileStatus.CHANGED, code_action.RunReturnCode.SUCCESS),
        (FormatFileStatus.SAVED, code_action.RunReturnCode.SUCCESS),
        (FormatFileStatus.SAVE_REFUSED, code_action.RunReturnCode.ERROR),
    ],
)
def test_a_refused_save_fails_the_run(
    status: FormatFileStatus, expected: code_action.RunReturnCode
) -> None:
    """Saving was requested but did not happen, so the run must not report
    success. The other outcomes did what was asked."""
    file_result = format_file_action.FormatFileRunResult(
        changed=True, code="x", status=status
    )
    files_result = format_files_action.FormatFilesRunResult(
        result_by_file_path={
            ResourceUri("file:///ok.py"): format_files_action.FormatRunFileResult(
                changed=True, code="x", status=FormatFileStatus.SAVED
            ),
            ResourceUri("file:///subject.py"): format_files_action.FormatRunFileResult(
                changed=True, code="x", status=status
            ),
        }
    )

    assert file_result.return_code is expected
    assert files_result.return_code is expected
