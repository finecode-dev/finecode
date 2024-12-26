import json

from pygls.lsp.server import LanguageServer
from lsprotocol import types
from loguru import logger

import finecode.extension_runner.schemas as schemas
import finecode.extension_runner.services as services


def create_lsp_server() -> LanguageServer:
    server = LanguageServer("FineCode_Extension_Runner_Server", "v1")
    # register_formatting_feature = server.feature(types.TEXT_DOCUMENT_FORMATTING)
    # register_formatting_feature(_format_document)
    
    register_initialized_feature = server.feature(types.INITIALIZED)
    register_initialized_feature(_on_initialized)

    register_update_config_feature = server.command('finecodeRunner/updateConfig')
    register_update_config_feature(update_config)
    
    register_run_task_cmd = server.command('actions/run')
    register_run_task_cmd(run_action)

    return server


# def _format_document(ls: LanguageServer, params: types.DocumentFormattingParams):
#     """Format the entire document"""
#     # logging.debug("%s", params)

#     # doc = ls.workspace.get_text_document(params.text_document.uri)
#     # rows = parse_document(doc)
#     # return format_table(rows)
#     return None


def _on_initialized(ls: LanguageServer, params: types.InitializedParams):
    logger.info(f"initialized {params}")


async def update_config(ls: LanguageServer, params):
    logger.trace(f'Update config: {params}')
    working_dir = params[0]
    project_name = params[1]
    actions = params[2]
    actions_configs = params[3]

    request = schemas.UpdateConfigRequest(
        working_dir=working_dir,
        project_name=project_name,
        actions={action_name: schemas.Action(name=action['name'], actions=action['subactions'], source=action['source']) for action_name, action in actions.items()},
        actions_configs=actions_configs)
    response = await services.update_config(request=request)
    return response.to_dict()


async def run_action(ls: LanguageServer, params):
    logger.trace(f'Run action: {params}')
    request = schemas.RunActionRequest(action_name=params[0], params=params[1])
    response = await services.run_action(request=request)
    # dict key can be path, but pygls fails to handle slashes in dict keys, use strings
    # representation of result instead until the problem is properly solved
    return { 'result':  json.dumps(response.to_dict()['result']) }
