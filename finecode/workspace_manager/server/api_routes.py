from pathlib import Path
from modapp import APIRouter
from .endpoints import finecode
import finecode.workspace_manager.server.schemas as schemas
import finecode.api as api
import finecode.domain as finecode_domain
import finecode.workspace_context as workspace_context

router = APIRouter()
ws_context = workspace_context.WorkspaceContext([])


@router.endpoint(finecode.workspace_manager.WorkspaceManagerService.AddWorkspaceDir)
async def add_workspace_dir(
    request: schemas.AddWorkspaceDirRequest,
) -> schemas.AddWorkspaceDirResponse:
    dir_path = Path(request.dir_path)
    ws_context.ws_dirs_paths.append(dir_path)
    api.read_configs_in_dir(dir_path=dir_path, ws_context=ws_context)
    ws_context.ws_dirs_paths_changed.set()
    return schemas.AddWorkspaceDirResponse()


@router.endpoint(finecode.workspace_manager.WorkspaceManagerService.DeleteWorkspaceDir)
async def delete_workspace_dir(
    request: schemas.DeleteWorkspaceDirRequest,
) -> schemas.DeleteWorkspaceDirResponse:
    ws_context.ws_dirs_paths.remove(Path(request.dir_path))
    ws_context.ws_dirs_paths_changed.set()
    return schemas.DeleteWorkspaceDirResponse()


def _normalize_action_tree(
    package: finecode_domain.Package,
) -> dict[str, schemas.NormalizedAction]:
    packages_to_process: set[finecode_domain.Package] = set([package])
    processed_packages: set[finecode_domain.Package] = set()
    action_by_path: dict[str, schemas.NormalizedAction] = {}

    while len(packages_to_process) > 0:
        current_package = packages_to_process.pop()
        processed_packages.add(current_package)
        new_packages = set(current_package.subpackages) - processed_packages
        packages_to_process |= new_packages

        if current_package.path.as_posix() not in action_by_path:
            if current_package.actions is None:
                try:
                    api.collect_actions.collect_actions(package_path=current_package.path, ws_context=ws_context)
                except ValueError:
                    continue
            
            assert current_package.actions is not None
            # don't overwrite recursive packages, they are already included in result
            action_by_path[current_package.path.as_posix()] = schemas.NormalizedAction(
                name=current_package.name,
                project_path=current_package.path.as_posix(),
                is_package=True,
                subactions=[
                    f"{current_package.path.as_posix()}::{action.name}"
                    for action in current_package.actions
                ]
                + [
                    subpackage.path.as_posix()
                    for subpackage in current_package.subpackages
                ],
            )
            action_by_path.update(
                {
                    f"{current_package.path.as_posix()}::{action.name}": schemas.NormalizedAction(
                        name=action.name,
                        project_path=current_package.path.as_posix(),
                        subactions=[
                            f"{current_package.path.as_posix()}::{subaction}"
                            for subaction in action.subactions
                        ],
                        is_package=False,
                    )
                    for action in current_package.actions
                }
            )

    return action_by_path


@router.endpoint(finecode.workspace_manager.WorkspaceManagerService.ListActions)
async def list_actions(
    request: schemas.ListActionsRequest,
) -> schemas.ListActionsResponse:
    if len(ws_context.ws_dirs_paths) == 0:
        return schemas.ListActionsResponse(root_action='', actions_by_path={})

    print('list actions', request)
    root_package: finecode_domain.Package = api.collect_actions_recursively(
        # TODO
        ws_context.ws_dirs_paths[0], ws_context=ws_context
    )
    actions_by_path: dict[str, schemas.NormalizedAction] = _normalize_action_tree(
        package=root_package
    )
    return schemas.ListActionsResponse(root_action=root_package.path.as_posix(), actions_by_path=actions_by_path)


@router.endpoint(finecode.workspace_manager.WorkspaceManagerService.RunAction)
async def run_action(
    request: schemas.RunActionRequest,
) -> schemas.RunActionResponse:
    print('run action', request)
    return schemas.RunActionResponse()
