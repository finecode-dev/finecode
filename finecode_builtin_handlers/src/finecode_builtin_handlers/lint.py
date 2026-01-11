import asyncio
import dataclasses
import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.actions import (
    lint as lint_action,
    lint_files as lint_files_action,
    list_project_files_by_lang as list_project_files_by_lang_action,
    group_project_files_by_lang as group_project_files_by_lang_action
)
from finecode_extension_api.interfaces import (
    iactionrunner,
    ifileeditor,
    ilogger,
)


@dataclasses.dataclass
class LintHandlerConfig(code_action.ActionHandlerConfig): ...


class LintHandler(
    code_action.ActionHandler[
        lint_action.LintAction, LintHandlerConfig
    ]
):
    def __init__(
        self, action_runner: iactionrunner.IActionRunner, logger: ilogger.ILogger, file_editor: ifileeditor.IFileEditor
    ) -> None:
        self.action_runner = action_runner
        self.file_editor = file_editor
        self.logger = logger

    async def run(
        self,
        payload: lint_action.LintRunPayload,
        run_context: lint_action.LintRunContext,
    ) -> lint_action.LintRunResult:
        # files_to_lint: list[pathlib.Path] = []
        files_by_lang: dict[str, list[pathlib.Path]] = {}
        
        # first get languages for which linters are available, they change rarely
        # only on project config change
        all_actions = self.action_runner.get_actions_names()
        lint_files_prefix = 'lint_files_'
        lint_files_actions = [action_name for action_name in all_actions if action_name.startswith(lint_files_prefix)]
        # TODO: ordered set?
        # TODO: cache and update on project config change
        langs_supported_by_lint = list(set([action_name[len(lint_files_prefix):] for action_name in lint_files_actions]))
        run_meta = run_context.meta

        if payload.target == lint_action.LintTarget.PROJECT:
            if run_meta.dev_env == code_action.DevEnv.IDE and run_meta.trigger == code_action.RunActionTrigger.SYSTEM:
                # performance optimization: if IDE automatically(=`trigger == SYSTEM`)
                # tries to lint the whole project, lint only files owned by IDE(usually
                # these are opened files).
                # In future it could be improved by linting opened files + dependencies
                # or e.g. files changed according to git + dependencies.
                files_to_lint: list[pathlib.Path] = self.file_editor.get_opened_files()
                group_project_files_action = self.action_runner.get_action_by_name('group_project_files_by_lang')
                group_project_files_by_lang_payload = group_project_files_by_lang_action.GroupProjectFilesByLangRunPayload(file_paths=files_to_lint, langs=langs_supported_by_lint)
                files_by_lang_result = await self.action_runner.run_action(
                    action=group_project_files_action,
                    payload=group_project_files_by_lang_payload,
                    meta=run_meta
                )
                files_by_lang = files_by_lang_result.files_by_lang
            else:
                # not automatic check of IDE, lint the whole project.
                # Instead of getting all files in the project and then grouping them by
                # language, use `list_project_files_by_lang_action` action which returns
                # only files with supported languages
                list_project_file_by_lang_action_instance = self.action_runner.get_action_by_name('list_project_files_by_lang')
                list_project_files_by_lang_payload = list_project_files_by_lang_action.ListProjectFilesByLangRunPayload(langs=langs_supported_by_lint)
                files_by_lang_result = await self.action_runner.run_action(
                    action=list_project_file_by_lang_action_instance,
                    payload=list_project_files_by_lang_payload,
                    meta=run_meta
                )
                files_by_lang = files_by_lang_result.files_by_lang

        else:
            # lint target are files, lint them
            files_to_lint = payload.file_paths
            group_project_files_by_lang_action_instance = self.action_runner.get_action_by_name('group_project_files_by_lang')
            group_project_files_by_lang_payload = group_project_files_by_lang_action.GroupProjectFilesByLangRunPayload(file_paths=files_to_lint, langs=langs_supported_by_lint)
            files_by_lang_result = await self.action_runner.run_action(
                action=group_project_files_by_lang_action_instance,
                payload=group_project_files_by_lang_payload,
                meta=run_meta
            )
            files_by_lang = files_by_lang_result.files_by_lang

        # TODO: handle errors
        lint_tasks = []
        try:
            async with asyncio.TaskGroup() as tg:
                for lang, lang_files in files_by_lang.items():
                    # TODO: handle errors
                    # TODO: handle KeyError?
                    action = self.action_runner.get_action_by_name(lint_files_prefix + lang)
                    lint_files_payload = lint_files_action.LintFilesRunPayload(file_paths=lang_files)
                    lint_task = tg.create_task(self.action_runner.run_action(
                        action=action,
                        payload=lint_files_payload,
                        meta=run_meta
                    ))
                    lint_tasks.append(lint_task)
        except ExceptionGroup as eg:
            error_str = ". ".join([str(exception) for exception in eg.exceptions])
            raise code_action.ActionFailedException(error_str)

        lint_results = [task.result() for task in lint_tasks]
        if len(lint_results) > 0:
            result = lint_action.LintRunResult(messages={})
            for subresult in lint_results:
                result.update(subresult)
            return result
        else:
            return lint_action.LintRunResult(messages={})
