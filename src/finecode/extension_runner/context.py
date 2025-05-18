from dataclasses import dataclass, field

from finecode.extension_runner import domain
from finecode_extension_api import code_action


@dataclass
class RunnerContext:
    project: domain.Project
    action_exec_info_by_name: dict[str, domain.ActionExecInfo] = field(
        default_factory=dict
    )
    action_handlers_instances_by_name: dict[str, code_action.ActionHandler] = field(
        default_factory=dict
    )
    action_handlers_exec_info_by_name: dict[str, domain.ActionHandlerExecInfo] = field(
        default_factory=dict
    )
    # don't overwrite, only append and remove
    docs_owned_by_client: list[str] = field(default_factory=list)
