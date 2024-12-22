from pathlib import Path

from loguru import logger

# import finecode.api as api
import finecode.domain as domain
import finecode.extension_runner.run_utils as run_utils
import finecode.extension_runner.schemas as schemas
import finecode.extension_runner.context as context
import finecode.extension_runner.global_state as global_state
from finecode.api.collect_actions import get_subaction
from finecode.code_action import (CodeFormatAction, FormatRunPayload,
                                  FormatRunResult, RunActionResult,
                                  RunOnManyPayload, RunOnManyResult)

# temporary global storage
# _project_root: Path | None = None


class ActionFailedException(Exception):
    ...


# def _init_project(working_dir: Path):
#     ...
#     # global _project_root
#     # _project_root = working_dir
#     # api.read_configs_in_dir(_project_root, global_state.runner_context)
#     # api.collect_actions(project_path=_project_root, ws_context=global_state.runner_context)


async def update_config(
    request: schemas.UpdateConfigRequest,
) -> schemas.UpdateConfigResponse:
    # _init_project(Path(request.working_dir))
    # TODO: save config
    return schemas.UpdateConfigResponse()


async def run_action(
    request: schemas.RunActionRequest,
) -> schemas.RunActionResponse:
    # TODO: check whether config is set
    # TODO: validate that action exists
    # TODO: validate that path exists
    if global_state.runner_context is None:
        # TODO: raise error
        return schemas.RunActionResponse(result_text="")

    project = global_state.runner_context.project

    try:
        action_obj = next(
            action_obj for action_obj in project.actions if action_obj.name == request.action_name
        )
    except StopIteration:
        logger.warning(
            f"Action {request.action_name} not found. Available actions: {','.join([action_obj.name for action_obj in project.actions])}"
        )
        # TODO: raise error
        return schemas.RunActionResponse(result_text="")

    try:
        result = await __run_action(
            action_obj,
            Path(request.apply_on) if request.apply_on != "" else None,
            apply_on_text=request.apply_on_text,
            project_root=global_state.runner_context.project_dir_path,
            runner_context=global_state.runner_context,
        )
    except Exception as e:  # TODO: concrete exceptions?
        logger.exception(e)
        raise ActionFailedException("Failed to run action")

    return schemas.RunActionResponse(
        result_text=(
            result.code
            if result is not None
            and isinstance(result, FormatRunResult)
            and result.code is not None
            else ""
        )
    )


async def __run_action(
    action: domain.Action,
    apply_on: Path | None,
    apply_on_text: str,
    project_root: Path,
    runner_context: context.RunnerContext,
) -> RunActionResult | RunOnManyResult | None:
    logger.trace(f"Execute action {action.name} on {apply_on}")

    if global_state.runner_context is None:
        # TODO: raise error
        return

    project_def = global_state.runner_context.project

    if project_def.actions is None:
        logger.error("Project actions are not read yet")
        return

    result: RunActionResult | RunOnManyResult | None = None
    # run in current env
    if len(action.subactions) > 0:
        # TODO: handle circular deps
        # can be optimized: find files for run_on_many in root action, not in each subaction
        # individually
        #
        # apply_on_text can change after subaction run, apply_on stays always the same
        current_apply_on_text = apply_on_text
        for subaction in action.subactions:
            try:
                subaction_obj = get_subaction(
                    name=subaction, project_path=project_root, ws_context=ws_context
                )
            except ValueError:
                raise ValueError(f"Action {subaction} not found")

            result = await __run_action(
                subaction_obj,
                apply_on,
                current_apply_on_text,
                project_root=project_root,
                runner_context=runner_context,
            )
            if (
                result is not None
                and isinstance(result, FormatRunResult)
                and result.code is not None
            ):
                current_apply_on_text = result.code
    elif action.source is not None:
        logger.debug(
            f"Run {action.name} on {str(apply_on.absolute() if apply_on is not None else '')}"
        )
        try:
            # TODO: cache
            action_cls = run_utils.import_class_by_source_str(action.source)
            action_config_cls = run_utils.import_class_by_source_str(action.source + "Config")
        except ModuleNotFoundError:
            logger.error(f"Source of action {action.name} '{action.source}' could not be imported")
            return

        try:
            action_config = project_def.actions_configs[action.name]
        except KeyError:
            action_config = {}

        config = action_config_cls(**action_config)
        action_instance = action_cls(config=config)

        if apply_on is not None and apply_on.is_dir():
            # temporary solution, should be dependency injection or similar approach
            if isinstance(action_instance, CodeFormatAction):
                # TODO: dynamic suffix for files
                py_files_in_dir = apply_on.rglob("*.py")
                payload = RunOnManyPayload(
                    single_payloads=[
                        FormatRunPayload(apply_on=py_file_path, apply_on_text="")
                        for py_file_path in py_files_in_dir
                    ],
                    dir_path=apply_on,
                )
            else:
                raise NotImplementedError()

            try:
                result = await action_instance.run_on_many(payload)
            except NotImplementedError:
                # Action.run_on_many is optional. If it isn't implemented, run Action.run on each
                # file
                result = {}
                for single_payload in payload.single_payloads:
                    try:
                        single_result = await action_instance.run(single_payload)
                        assert single_payload.apply_on is not None
                        result[single_payload.apply_on] = single_result
                    except Exception as e:
                        logger.exception(e)
                        # TODO: error
            except Exception as e:
                logger.exception(e)
                return
                # TODO: error

            # both run and run_on_many don't modify original files, in case of changes they return
            # changed code in result. Then FineCode desides whether to change the file or just
            # return the changes. run_on_many supports currently only in-place changes, so save
            # them
            if isinstance(action_instance, CodeFormatAction) and result is not None:
                for file_path, file_result in result.items():
                    assert isinstance(file_result, FormatRunResult)
                    if file_result.changed and file_result.code is not None:
                        with open(file_path, "w") as f:
                            f.write(file_result.code)
        else:
            # temporary solution, should be dependency injection or similar approach
            if isinstance(action_instance, CodeFormatAction):
                payload = FormatRunPayload(apply_on=apply_on, apply_on_text=apply_on_text)
            else:
                raise NotImplementedError()

            try:
                result = await action_instance.run(payload)
            except Exception as e:
                logger.exception(e)
                return
                # TODO: error
    else:
        logger.warning(f"Action {action.name} has neither source nor subactions, skip it")
        return

    logger.trace(f"End of execution of action {action.name} on {apply_on}")
    return result
