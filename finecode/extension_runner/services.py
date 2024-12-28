from pathlib import Path

from loguru import logger

from finecode.code_action import (CodeFormatAction, CodeLintAction, FormatRunPayload,
                                  FormatRunResult, LintRunPayload, RunActionResult,
                                  RunOnManyPayload, RunOnManyResult, ActionContext)
import finecode.extension_runner.domain as domain
import finecode.extension_runner.run_utils as run_utils
import finecode.extension_runner.schemas as schemas
import finecode.extension_runner.context as context
import finecode.extension_runner.global_state as global_state
import finecode.extension_runner.project_dirs as project_dirs

class ActionFailedException(Exception):
    ...


async def update_config(
    request: schemas.UpdateConfigRequest,
) -> schemas.UpdateConfigResponse:
    project_path = Path(request.working_dir)
    global_state.runner_context = context.RunnerContext(
        project=domain.Project(
            name=request.project_name,
            path=project_path,
            actions={action_name: domain.Action(name=action.name, subactions=action.actions, source=action.source) for action_name, action in request.actions.items()},
            actions_configs=request.actions_configs
        ),
    )

    return schemas.UpdateConfigResponse()


async def run_action(
    request: schemas.RunActionRequest,
) -> schemas.RunActionResponse:
    # TODO: check whether config is set
    # TODO: validate that action exists
    # TODO: validate that path exists
    if global_state.runner_context is None:
        # TODO: raise error
        return schemas.RunActionResponse({})

    project = global_state.runner_context.project

    if project.actions is None:
        logger.error("Project actions are not read yet")
        # TODO: raise error
        return schemas.RunActionResponse({})

    try:
        action_obj = project.actions[request.action_name]
    except KeyError:
        logger.warning(
            f"Action {request.action_name} not found. Available actions: {','.join([action_name for action_name in project.actions])}"
        )
        # TODO: raise error
        return schemas.RunActionResponse({})

    try:
        result = await __run_action(
            action=action_obj,
            apply_on=request.params['apply_on'],
            apply_on_text=request.params['apply_on_text'],
            project_root=global_state.runner_context.project.path,
            runner_context=global_state.runner_context,
        )
    except Exception as e:  # TODO: concrete exceptions?
        logger.exception(e)
        raise ActionFailedException("Failed to run action")

    result_dict = {}
    if isinstance(result, dict): # RunOnManyResult
        result_dict = { path.as_posix(): file_result.model_dump() for path, file_result in result.items() }
    elif isinstance(result, RunActionResult):
        result_dict = result.model_dump()

    return schemas.RunActionResponse(result=result_dict)


async def __run_action(
    action: domain.Action,
    apply_on: list[Path] | None,
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
                subaction_obj = runner_context.project.actions[subaction]
            except KeyError:
                raise ValueError(f"Action {subaction} not found")

            subaction_result = await __run_action(
                subaction_obj,
                apply_on,
                current_apply_on_text,
                project_root=project_root,
                runner_context=runner_context,
            )
            if (
                subaction_result is not None
                and isinstance(subaction_result, FormatRunResult)
                and subaction_result.code is not None
            ):
                current_apply_on_text = subaction_result.code
            
            if isinstance(subaction_result, FormatRunResult) and subaction_result.changed is False and result is not None:
                # if format subaction didn't change the code, save it only if there are no result at all
                # if one subaction changed code and the next one didn't, expected result is changed code of the first one
                continue
            else:
                result = subaction_result
    elif action.source is not None:
        logger.debug(
            f"Run {action.name} on {str(apply_on if apply_on is not None else '')}"
        )
        try:
            # TODO: cache
            action_cls = run_utils.import_class_by_source_str(action.source)
            action_config_cls = run_utils.import_class_by_source_str(action.source + "Config")
        except ModuleNotFoundError as error:
            logger.error(f"Source of action {action.name} '{action.source}' could not be imported")
            logger.error(error)
            return

        try:
            action_config = project_def.actions_configs[action.name]
        except KeyError:
            action_config = {}

        config = action_config_cls(**action_config)
        project_path = runner_context.project.path
        project_cache_dir = project_dirs.get_project_dir(project_path=project_path) # , base_dir=root_cache_dir
        context = ActionContext(
            project_dir=runner_context.project.path,
            cache_dir=project_cache_dir)
        action_instance = action_cls(config=config, context=context)

        if apply_on is not None and len(apply_on) > 1:
            # temporary solution, should be dependency injection or similar approach
            if isinstance(action_instance, CodeFormatAction):
                payload = RunOnManyPayload(
                    single_payloads=[
                        FormatRunPayload(apply_on=py_file_path, apply_on_text="")
                        for py_file_path in apply_on
                    ],
                    # it's temporary path of project, but it should be directory of corresponding representation
                    dir_path=runner_context.project.path,
                )
            else:
                raise NotImplementedError()

            logger.debug("Run on many")
            try:
                result = await action_instance.run_on_many(payload)
            except NotImplementedError:
                # Action.run_on_many is optional. If it isn't implemented, run Action.run on each
                # file
                logger.debug("Run on many is not implemented, run on single files")
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
                        logger.trace(f'Saving {file_path}')
                        with open(file_path, "w") as f:
                            f.write(file_result.code)
                    else:
                        logger.trace(f'File {file_path} was not changed or there is no result')
        else:
            # temporary solution, should be dependency injection or similar approach
            if isinstance(action_instance, CodeFormatAction):
                payload = FormatRunPayload(apply_on=apply_on[0] if isinstance(apply_on, list) else None, apply_on_text=apply_on_text)
            elif isinstance(action_instance, CodeLintAction):
                payload = LintRunPayload(apply_on=apply_on[0] if isinstance(apply_on, list) else None, apply_on_text=apply_on_text)
            else:
                raise NotImplementedError()

            logger.debug('Run on single')
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
