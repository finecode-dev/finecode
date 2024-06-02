from modapp.routing import RouteMeta, Cardinality


class ExtensionRunnerService:
    UpdateConfig = RouteMeta(
        path="/finecode.extension_runner.ExtensionRunnerService/UpdateConfig",
        cardinality=Cardinality.UNARY_UNARY,
    )
    RunAction = RouteMeta(
        path="/finecode.extension_runner.ExtensionRunnerService/RunAction",
        cardinality=Cardinality.UNARY_UNARY,
    )
