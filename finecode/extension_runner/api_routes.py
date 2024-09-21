from pathlib import Path
from loguru import logger
from modapp import APIRouter

from finecode.api.collect_actions import get_subaction
from .endpoints import finecode
import finecode.extension_runner.schemas as schemas
import finecode.api as api
import finecode.api.run_utils as run_utils
import finecode.domain as domain
import finecode.workspace_context as workspace_context

router = APIRouter()
ws_context = workspace_context.WorkspaceContext([])

# temporary global storage
_project_root: Path | None = None

def _init_project(working_dir: Path):
    global _project_root
    _project_root = working_dir
    api.read_configs_in_dir(_project_root, ws_context)
    api.collect_actions.collect_actions(package_path=_project_root, ws_context=ws_context)


@router.endpoint(finecode.extension_runner.ExtensionRunnerService.UpdateConfig)
async def update_config(
    request: schemas.UpdateConfigRequest,
) -> schemas.UpdateConfigResponse:
    _init_project(Path(request.working_dir))
    return schemas.UpdateConfigResponse()


@router.endpoint(finecode.extension_runner.ExtensionRunnerService.RunAction)
async def run_action(
    request: schemas.RunActionRequest,
) -> schemas.RunActionResponse:
    # TODO: check whether config is set
    # TODO: validate that action exists
    # TODO: validate that path exists

    try:
        package = ws_context.ws_packages[_project_root]
    except StopIteration:
        logger.warning("Package not found")
        # TODO: raise error
        return schemas.RunActionResponse()
    
    try:
        action_obj = next(action_obj for action_obj in package.actions if action_obj.name == request.action_name)
    except StopIteration:
        logger.warning(
            f"Action {request.action_name} not found. Available actions: {','.join([action_obj.name for action_obj in actions])}"
        )
        # TODO: raise error
        return schemas.RunActionResponse()

    assert _project_root is not None
    await __run_action(
        action_obj,
        Path(request.apply_on),
        project_root=_project_root,
        ws_context=ws_context,
    )
    
    return schemas.RunActionResponse()



async def __run_action(
    action: domain.Action,
    apply_on: Path,
    project_root: Path,
    ws_context: workspace_context.WorkspaceContext,
) -> None:
    logger.trace(f"Execute action {action.name} on {apply_on}")

    try:
        project_package = ws_context.ws_packages[project_root]
    except KeyError:
        logger.error(f"Project package not found: {project_root}")
        return

    if project_package.actions is None:
        logger.error("Project actions are not read yet")
        return

    ws_context.ignore_watch_paths.add(apply_on)
    # run in current env
    if len(action.subactions) > 0:
        # TODO: handle circular deps
        for subaction in action.subactions:
            try:
                subaction_obj = get_subaction(
                    name=subaction, package_path=project_root, ws_context=ws_context
                )
            except ValueError:
                raise Exception(f"Action {subaction} not found")
            await __run_action(
                subaction_obj,
                apply_on,
                project_root=project_root,
                ws_context=ws_context,
            )
    elif action.source is not None:
        logger.debug(f"Run {action.name} on {str(apply_on.absolute())}")
        try:
            action_cls = run_utils.import_class_by_source_str(action.source)
            action_config_cls = run_utils.import_class_by_source_str(action.source + "Config")
        except ModuleNotFoundError:
            logger.error(f"Source of action {action.name} '{action.source}' could not be imported")
            ws_context.ignore_watch_paths.remove(apply_on)
            return

        try:
            action_config = ws_context.ws_packages[project_root].actions_configs[action.name]
        except KeyError:
            action_config = {}

        config = action_config_cls(**action_config)
        action_instance = action_cls(config=config)
        try:
            action_instance.run(apply_on)
        except Exception as e:
            logger.exception(e)
            # TODO: exit code != 0
    else:
        logger.warning(f"Action {action.name} has neither source nor subactions, skip it")
        return
    try:
        ws_context.ignore_watch_paths.remove(apply_on)
    except KeyError:
        # path could be removed by subaction if its execution wasn't successful, ignore this case
        ...

    logger.trace(f"End of execution of action {action.name} on {apply_on}")
