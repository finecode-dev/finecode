from pathlib import Path
from modapp import APIRouter
from .endpoints import finecode
import finecode.extension_runner.schemas as schemas
import finecode.api as api
import finecode.workspace_context as workspace_context

router = APIRouter()
ws_context = workspace_context.WorkspaceContext([])

# temporary global storage
_project_root: Path | None = None

@router.endpoint(finecode.extension_runner.ExtensionRunnerService.UpdateConfig)
async def update_config(
    request: schemas.UpdateConfigRequest,
) -> schemas.UpdateConfigResponse:
    print(request)
    global _project_root
    _project_root = Path(request.working_dir)
    return schemas.UpdateConfigResponse()


@router.endpoint(finecode.extension_runner.ExtensionRunnerService.RunAction)
async def run_action(
    request: schemas.RunActionRequest,
) -> schemas.RunActionResponse:
    # TODO: check whether config is set
    # TODO: validate that action exists
    # TODO: validate that path exists
    api.run(
        action=request.action_name,
        apply_on=Path(request.apply_on),
        project_root=_project_root,
        ws_context=ws_context,
    )
    return schemas.RunActionResponse()
