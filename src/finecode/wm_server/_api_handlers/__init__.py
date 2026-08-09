"""Re-exports for the _api_handlers subpackage.

``wm_server.py`` imports all handler symbols from here.
"""

from finecode.wm_server._api_handlers._actions import (
    _handle_actions_reload,
    _handle_get_payload_schemas,
    _handle_get_tree,
    _handle_run_action,
    _handle_run_batch,
    _handle_set_config_overrides,
)
from finecode.wm_server._api_handlers._runners import (
    _handle_runners_check_env,
    _handle_runners_list,
    _handle_runners_remove_env,
    _handle_runners_restart,
    _handle_start_runners,
)
from finecode.wm_server._api_handlers._streaming import (
    _handle_run_action_with_partial_results_task,
    _handle_run_action_with_progress_task,
    _handle_run_batch_with_partial_results_task,
    _handle_run_batch_with_progress_task,
)
from finecode.wm_server._api_handlers._workspace import (
    _handle_add_dir,
    _handle_find_project_for_file,
    _handle_get_project_raw_config,
    _handle_get_workspace_editable_packages,
    _handle_list_actions,
    _handle_list_projects,
    _handle_prepare_envs,
    _handle_reload_config,
    _handle_remove_dir,
)
from finecode.wm_server.services.document_sync import (
    handle_documents_changed,
    handle_documents_closed,
    handle_documents_opened,
)

__all__ = [
    "_handle_actions_reload",
    "_handle_add_dir",
    "_handle_find_project_for_file",
    "_handle_get_payload_schemas",
    "_handle_get_project_raw_config",
    "_handle_get_tree",
    "_handle_get_workspace_editable_packages",
    "_handle_list_actions",
    "_handle_list_projects",
    "_handle_prepare_envs",
    "_handle_reload_config",
    "_handle_remove_dir",
    "_handle_run_action",
    "_handle_run_action_with_partial_results_task",
    "_handle_run_action_with_progress_task",
    "_handle_run_batch",
    "_handle_run_batch_with_partial_results_task",
    "_handle_run_batch_with_progress_task",
    "_handle_runners_check_env",
    "_handle_runners_list",
    "_handle_runners_remove_env",
    "_handle_runners_restart",
    "_handle_set_config_overrides",
    "_handle_start_runners",
    "handle_documents_changed",
    "handle_documents_closed",
    "handle_documents_opened",
]
