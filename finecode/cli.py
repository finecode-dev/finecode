import os
from pathlib import Path

import click
from loguru import logger

import finecode.api as api
import finecode.workspace_context as workspace_context


@click.group()
def cli(): ...


@cli.group()
def action(): ...


@action.command("run")
@click.argument("action")
@click.argument("apply_on", type=click.Path(exists=True))
@click.option("-p", "project_root", type=click.Path(exists=True))
def run_action(action: str, apply_on: str, project_root: Path | None = None) -> None:
    logger.trace(f"Run action: {action} on {apply_on}")
    if project_root is not None:
        _project_root = project_root
    else:
        _project_root = Path(os.getcwd())
    ws_context = workspace_context.WorkspaceContext([_project_root])
    api.read_configs(ws_context=ws_context)
    api.run(
        action=action,
        apply_on=Path(apply_on),
        project_root=_project_root,
        ws_context=ws_context,
    )


@action.command("list")
def list_actions(project_root: Path | None = None) -> None:
    if project_root is not None:
        _project_root = project_root
    else:
        _project_root = Path(os.getcwd())
    ws_context = workspace_context.WorkspaceContext([_project_root])
    api.read_configs(ws_context=ws_context)
    actions = api.collect_actions.collect_actions(_project_root, ws_context=ws_context)
    logger.info(f"Available actions: {','.join([action.name for action in actions])}")


# TODO: action tree


@cli.group()
def view(): ...


@view.command("list")
def list_views(project_root: Path | None = None) -> None:
    if project_root is not None:
        _project_root = project_root
    else:
        _project_root = Path(os.getcwd())
    ws_context = workspace_context.WorkspaceContext([_project_root])
    api.read_configs(ws_context=ws_context)
    views = api.collect_views_in_packages(
        list(ws_context.ws_packages.values()), ws_context=ws_context
    )

    package = ws_context.ws_packages.get(_project_root, None)
    if package is not None:
        package_views = views.get(package.path, None)
        if package_views is not None:
            logger.info(
                f"Available views: {','.join([view.name for view in views[package.path]])}"
            )
        else:
            logger.info(f"No views in package {package.name}")
    else:
        logger.info("No package")


@view.command("show")
@click.argument("view_name")
def show_view(
    view_name: str, element_path: str | None = None, project_root: Path | None = None
) -> None:
    if project_root is not None:
        _project_root = project_root
    else:
        _project_root = Path(os.getcwd())

    ws_context = workspace_context.WorkspaceContext([_project_root])
    api.read_configs(ws_context=ws_context)
    api.collect_views_in_packages(list(ws_context.ws_packages.values()), ws_context=ws_context)
    view_root_els = api.show_view(
        view_name=view_name, package_path=_project_root, ws_context=ws_context
    )
    for root_el in view_root_els:
        logger.info(root_el.label)


# TODO: json or similar output

# TODO: print configuration


if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, level="TRACE")
    cli()
