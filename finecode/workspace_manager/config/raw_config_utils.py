from typing import Any

from finecode.workspace_manager import domain


def get_subactions(names: list[str], project_raw_config: dict[str, Any]) -> list[domain.Action]:
    subactions: list[domain.Action] = []
    for name in names:
        try:
            action_raw = project_raw_config["tool"]["finecode"]["action"][name]
        except KeyError:
            raise ValueError("Action definition not found")
        try:
            subactions.append(domain.Action(name=name, source=action_raw["source"]))
        except KeyError:
            raise ValueError("Action has no source")

    return subactions
