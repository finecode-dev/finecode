import os
from pathlib import Path

import click
from loguru import logger

import finecode.api as api
import finecode.workspace_context as workspace_context


@click.group()
def cli():
    ...


@cli.command()
@click.argument("action")
@click.argument("apply_on", type=click.Path(exists=True))
@click.option("-p", "project_root", type=click.Path(exists=True))
def run(action: str, apply_on: str, project_root: Path | None = None) -> None:
    if project_root is not None:
        _project_root = project_root
    else:
        _project_root = Path(os.getcwd())
    ws_context = workspace_context.WorkspaceContext(_project_root)
    api.run(
        action=action,
        apply_on=Path(apply_on),
        project_root=_project_root,
        ws_context=ws_context,
    )


def list_actions(project_root: Path) -> None:
    ws_context = workspace_context.WorkspaceContext(project_root)
    root_actions, _ = api.collect_actions.collect_actions(
        project_root, ws_context=ws_context
    )
    logger.info(f"Available actions: {','.join(root_actions)}")


# TODO: json or similar output

# TODO: print configuration


if __name__ == "__main__":
    cli()

#     pkg_root = Path(__file__).parent.parent
#     list_actions(pkg_root / 'pyproject.toml')
#     # run("lint", pkg_root / "finecode", pkg_root)
