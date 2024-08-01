from modapp.routing import RouteMeta, Cardinality


class WorkspaceManagerService:
    AddWorkspaceDir = RouteMeta(
        path="/finecode.workspace_manager.WorkspaceManagerService/AddWorkspaceDir",
        cardinality=Cardinality.UNARY_UNARY,
    )
    DeleteWorkspaceDir = RouteMeta(
        path="/finecode.workspace_manager.WorkspaceManagerService/DeleteWorkspaceDir",
        cardinality=Cardinality.UNARY_UNARY,
    )

    ListActions = RouteMeta(
        path="/finecode.workspace_manager.WorkspaceManagerService/ListActions",
        cardinality=Cardinality.UNARY_UNARY,
    )
    RunAction = RouteMeta(
        path="/finecode.workspace_manager.WorkspaceManagerService/RunAction",
        cardinality=Cardinality.UNARY_UNARY,
    )
