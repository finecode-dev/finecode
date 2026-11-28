"""The command a caller should run when a recovery could not finish on its own.

A recovery that fails because the environment no longer matches the
configuration surfaces, by default, whatever the runner said — usually an import
error naming a module the caller never heard of. The environment command that
fixes it is derivable from the state the runner stopped in, and naming it is the
difference between a report someone can act on and one they have to diagnose
(PRD-0008 R11).
"""

from __future__ import annotations

import pathlib

from finecode.wm_server import domain
from finecode.wm_server.runner import runner_manager


def for_runner_failure(
    project_dir: pathlib.Path,
    env_name: str | None,
    status: domain.ExtensionRunnerStatus | None,
    exception: BaseException | None,
) -> str | None:
    """Return the command that would let the recovery succeed, if one applies.

    ``None`` when the failure is not an environment problem: a next step that
    does not follow from the failure is worse than none, because it sends the
    caller to reinstall an environment that was never the cause.
    """
    env_is_missing = status is domain.ExtensionRunnerStatus.NO_VENV
    stale_failure = (
        exception
        if isinstance(exception, runner_manager.EnvironmentOutOfDateError)
        else None
    )
    if not env_is_missing and stale_failure is None:
        return None

    if env_name is None and stale_failure is not None:
        # A caller recovering a whole project has no environment of its own to
        # name; the failure knows which one it was raised for.
        env_name = stale_failure.env_name

    command = f"python -m finecode prepare-envs --project={project_dir.name}"
    if env_name is not None:
        command += f" --env={env_name}"

    # Named when it is known and left indefinite when it is not: interpolating
    # the missing name reads as an environment literally called 'None', which
    # sends the caller looking for something that does not exist.
    subject = (
        f"the '{env_name}' environment of {project_dir.name}"
        if env_name is not None
        else f"an environment of {project_dir.name}"
    )
    if env_is_missing:
        reason = f"{subject} does not exist"
    else:
        reason = f"{subject} no longer has everything its configuration declares"
    return f"{reason} — run: {command}"
