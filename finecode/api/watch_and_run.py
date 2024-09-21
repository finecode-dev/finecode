from loguru import logger

import finecode.api as api
import finecode.api.watcher as watcher
import finecode.workspace_context as workspace_context


async def watch_and_run(
    ws_context: workspace_context.WorkspaceContext,
):
    # watch workspace directories in which there are watch-triggered actions and run those actions
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
                # TODO: currently only linter and format are hardcoded, this list should be dynamic
                # TODO: on which files should it be applied? format only on changed and lint?
                for action in ["lint", "format"]:
                    # TODO: this can be cached
                    project_root = api.find_package_with_action_for_file(
                        file_path=path_to_apply_on,
                        action_name=action,
                        workspace_path=None,
                        ws_context=ws_context,
                    )
                    await api.run(
                        action=action,
                        apply_on=path_to_apply_on,
                        project_root=project_root,
                        ws_context=ws_context,
                    )
