import json

from loguru import logger
from lsprotocol import types
from pygls.lsp.server import LanguageServer

from finecode.extension_runner import schemas, services, domain


def create_lsp_server() -> LanguageServer:
    server = LanguageServer("FineCode_Extension_Runner_Server", "v1")

    register_initialized_feature = server.feature(types.INITIALIZED)
    register_initialized_feature(_on_initialized)

    register_document_did_open_feature = server.feature(types.TEXT_DOCUMENT_DID_OPEN)
    register_document_did_open_feature(_document_did_open)

    register_document_did_close_feature = server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
    register_document_did_close_feature(_document_did_close)

    register_update_config_feature = server.command("finecodeRunner/updateConfig")
    register_update_config_feature(update_config)

    register_run_task_cmd = server.command("actions/run")
    register_run_task_cmd(run_action)

    register_reload_action_cmd = server.command("actions/reload")
    register_reload_action_cmd(reload_action)

    register_resolve_package_path_cmd = server.command("packages/resolvePath")
    register_resolve_package_path_cmd(resolve_package_path)

    async def document_requester(uri: str):
        document = await server.protocol.send_request_async('documents/get', params={ "uri": uri })
        return domain.TextDocumentInfo(uri=document.uri, version=document.version, text=document.text)

    services.document_requester = document_requester

    return server


def _on_initialized(ls: LanguageServer, params: types.InitializedParams):
    logger.info(f"initialized {params}")


def _document_did_open(ls: LanguageServer, params: types.DidOpenTextDocumentParams):
    logger.info(f'document did open: {params.text_document.uri}')
    services.document_did_open(params.text_document.uri)


def _document_did_close(ls: LanguageServer, params: types.DidCloseTextDocumentParams):
    logger.info(f'document did close: {params.text_document.uri}')
    services.document_did_close(params.text_document.uri)


async def update_config(ls: LanguageServer, params):
    logger.trace(f"Update config: {params}")
    working_dir = params[0]
    project_name = params[1]
    actions = params[2]
    actions_configs = params[3]

    request = schemas.UpdateConfigRequest(
        working_dir=working_dir,
        project_name=project_name,
        actions={
            action_name: schemas.Action(
                name=action["name"], actions=action["subactions"], source=action["source"]
            )
            for action_name, action in actions.items()
        },
        actions_configs=actions_configs,
    )
    response = await services.update_config(request=request)
    return response.to_dict()


async def run_action(ls: LanguageServer, params):
    logger.trace(f"Run action: {params}")
    request = schemas.RunActionRequest(action_name=params[0], params=params[1])
    response = await services.run_action(request=request)
    # dict key can be path, but pygls fails to handle slashes in dict keys, use strings
    # representation of result instead until the problem is properly solved
    return {"result": json.dumps(response.to_dict()["result"])}


async def reload_action(ls: LanguageServer, params):
    logger.trace(f"Reload action: {params}")
    services.reload_action(params[0])
    return {}


async def resolve_package_path(ls: LanguageServer, params):
    logger.trace(f"Resolve package path: {params}")
    result = services.resolve_package_path(params[0])
    return {"packagePath": result}
