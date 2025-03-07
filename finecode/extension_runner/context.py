from dataclasses import dataclass, field

from finecode.extension_runner import domain
from finecode_extension_api.code_action import CodeAction


@dataclass
class RunnerContext:
    project: domain.Project
    actions_instances_by_name: dict[str, CodeAction] = field(default_factory=dict)
    action_handlers_exec_info_by_name: dict[str, domain.ActionHandlerExecInfo] = field(
        default_factory=dict
    )
    # don't overwrite, only append and remove
    docs_owned_by_client: list[str] = field(default_factory=list)
