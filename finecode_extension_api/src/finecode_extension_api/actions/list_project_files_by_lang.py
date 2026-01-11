import dataclasses
import pathlib
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class ListProjectFilesByLangRunPayload(code_action.RunActionPayload):
    langs: list[str] | None = None


class ListProjectFilesByLangRunContext(code_action.RunActionContext):
    def __init__(
        self,
        run_id: int,
        initial_payload: ListProjectFilesByLangRunPayload,
        meta: code_action.RunActionMeta
    ) -> None:
        super().__init__(run_id=run_id, initial_payload=initial_payload, meta=meta)


@dataclasses.dataclass
class ListProjectFilesByLangRunResult(code_action.RunActionResult):
    files_by_lang: dict[str, list[pathlib.Path]]

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, ListProjectFilesByLangRunResult):
            return

        for lang, files in other.files_by_lang.items():
            if lang not in self.files_by_lang:
                self.files_by_lang[lang] = files
            else:
                self.files_by_lang[lang] += files

    def to_text(self) -> str | textstyler.StyledText:
        formatted_result = textstyler.StyledText()
        for language, files in self.files_by_lang.items():
            formatted_result.append_styled(text=language + "\n", bold=True)
            for file_path in files:
                formatted_result.append(file_path.as_posix() + "\n")
        return formatted_result


class ListProjectFilesByLangAction(code_action.Action[ListProjectFilesByLangRunPayload, ListProjectFilesByLangRunContext, ListProjectFilesByLangRunResult]):
    PAYLOAD_TYPE = ListProjectFilesByLangRunPayload
    RUN_CONTEXT_TYPE = ListProjectFilesByLangRunContext
    RESULT_TYPE = ListProjectFilesByLangRunResult
