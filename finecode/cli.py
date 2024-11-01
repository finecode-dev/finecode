import asyncio
import os
from pathlib import Path

import click
from loguru import logger

import finecode.api as api
import finecode.workspace_manager.watcher as watcher
import finecode.extension_runner as extension_runner
import finecode.workspace_context as workspace_context
import finecode.workspace_manager as workspace_manager


@click.group()
def cli(): ...


@cli.group()
def action(): ...


async def _watch_and_run(
    action: str,
    apply_on: Path,
    project_root: Path,
    ws_context: workspace_context.WorkspaceContext,
):
    path_to_apply_on = apply_on
    with watcher.watch_workspace_dirs(ws_context) as watch_iterator:
        async for change in watch_iterator:
            logger.warning(change)
            if change.kind == watcher.ChangeKind.DELETE:
                ...  # TODO: stop
            else:
                if (
                    change.kind == watcher.ChangeKind.RENAME
                    or change.kind == watcher.ChangeKind.MOVE
                ):
                    path_to_apply_on = change.new_path
                    assert path_to_apply_on is not None
                else:
                    path_to_apply_on = change.path
                logger.trace(f"Change: {change.kind} {change.path}")
                # TODO
                # await api.run(
                #     action=action,
                #     apply_on=path_to_apply_on,
                #     project_root=project_root,
                #     ws_context=ws_context,
                # )


def find_project_root(
    path: Path,
    project_root: Path | None,
) -> Path:
    if project_root is not None:
        # path provided by user has always higher priority
        return project_root

    current_path = path
    while len(current_path.parts) > 1:
        pyproject_path = current_path / "pyproject.toml"
        if pyproject_path.exists():
            return current_path
        current_path = current_path.parent

    return Path(os.getcwd())


@action.command("run")
@click.argument("action")
@click.argument("apply_on", type=click.Path(exists=True))
@click.option("-p", "project_root", type=click.Path(exists=True))
@click.option("-w", "watch", is_flag=True)
def run_action(
    action: str, apply_on: str, project_root: Path | None = None, watch: bool = False
) -> None:
    logger.trace(f"Run action: {action} on {apply_on}")

    apply_on_path = Path(apply_on)
    ws_dirs_paths: list[Path] = []
    _project_root = find_project_root(apply_on_path, project_root)
    ws_dirs_paths.append(_project_root)
    cwd = Path(os.getcwd())
    if _project_root != cwd:
        # started project action from root
        ws_dirs_paths.append(cwd)
    ws_context = workspace_context.WorkspaceContext(ws_dirs_paths=ws_dirs_paths)
    api.read_configs(ws_context=ws_context)
    if not watch:
        ...
        # TODO
        # asyncio.run(api.run(
        #     action=action,
        #     apply_on=apply_on_path,
        #     project_root=_project_root,
        #     ws_context=ws_context,
        # ))
    else:
        ...  # TODO: restore
        # asyncio.run(
        #     _watch_and_run(
        #         action=action,
        #         apply_on=Path(apply_on),
        #         project_root=_project_root,
        #         ws_context=ws_context,
        #     )
        # )


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


async def _start_and_run_forever(ws_root: Path) -> None:
    ws_context = workspace_context.WorkspaceContext([ws_root])
    await workspace_manager.start()
    root_package_path = Path("/home/user/Development/FineCode/finecode")
    await _watch_and_run(
        action='format',  # temporary for testing
        apply_on=root_package_path,
        project_root=root_package_path,
        ws_context=ws_context,
    )


@cli.command()
@click.option("--trace", "trace", is_flag=True)
def start(trace: bool = False):
    if trace:
        _enable_trace_logging()

    ws_root = Path(os.getcwd())
    asyncio.run(_start_and_run_forever(ws_root))


@cli.command()
@click.option("--trace", "trace", is_flag=True)
def start_api(trace: bool = False):
    if trace:
        _enable_trace_logging()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(workspace_manager.start())
    loop.run_forever()


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


async def _start_runner():
    app = extension_runner.create_extension_app()
    try:
        await app.run_async()
        while True:
            await asyncio.sleep(1)
    finally:
        app.stop()


@cli.command()
def runner():
    asyncio.run(_start_runner())


def _enable_trace_logging():
    import sys

    logger.remove()
    logger.add(sys.stderr, level="TRACE")


if __name__ == "__main__":
    _enable_trace_logging()
    cli()
