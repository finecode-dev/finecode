from __future__ import annotations

import dataclasses

from fine_git.create_git_tag_action import CreateGitTagAction, CreateGitTagRunPayload
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner

from fine_release.release_package_action import (
    RegistryPublishOutcome,
    ReleasePackageAction,
    ReleasePackageRunContext,
    ReleasePackageRunPayload,
    ReleasePackageRunResult,
    result_from_state,
)


@dataclasses.dataclass
class RecordReleaseTagHandlerConfig(code_action.ActionHandlerConfig): ...


class RecordReleaseTagHandler(
    code_action.ActionHandler[
        ReleasePackageAction,
        RecordReleaseTagHandlerConfig,
    ]
):
    """Record a released version as a git tag named ``<package>@<version>``
    (ADR-0060).

    The tag is a *record*, not a publication gate: it never causes a re-publish,
    because the registry alone decides whether a version is already released. But
    a tag that fails to write is not silent — it is recorded in
    ``state.error``, which fails the release outcome (non-zero return code) so a
    lost tag surfaces in CI instead of hiding behind a green publish. The
    registry entry stays ``PUBLISHED`` in ``registries[]`` for traceability.

    Recording is reconciling and idempotent: the tag is (re)attempted whenever
    the version is present in a registry — published this run or already there
    (``SKIPPED``) — so a tag that failed on an earlier run is retried on the next
    run, and ``create_git_tag`` is a no-op when the tag already exists.

    The ref is created but not pushed — the workspace release publishes every
    ref a run produced in one operation (ADR-0065). The ref is offered to the
    push on every reconciling run, even when the tag already existed locally, so
    a tag that was created but never pushed is re-pushed on the next run (the
    push is a remote no-op when the ref is already there).
    """

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: ReleasePackageRunPayload,
        run_context: ReleasePackageRunContext,
    ) -> ReleasePackageRunResult:
        state = run_context.state

        if state.error is not None:
            return result_from_state(payload, state)

        if payload.dry_run:
            return result_from_state(payload, state)

        released_registries = [
            registry.registry
            for registry in state.registry_results
            if registry.outcome
            in (RegistryPublishOutcome.PUBLISHED, RegistryPublishOutcome.SKIPPED)
        ]
        if not released_registries:
            # The version is present in no registry (every one FAILED, or none
            # resolved), so there is no release to record. A registry that
            # already had it (SKIPPED) still counts: the tag is reconciled even
            # when nothing was published this run.
            return result_from_state(payload, state)

        tag = f"{payload.package_name}@{payload.version}"
        message = (
            f"Release {payload.package_name} {payload.version} "
            f"({', '.join(released_registries)})"
        )

        try:
            tag_result = await self.action_runner.run_action(
                action_type=iprojectactionrunner.ActionRef.from_type(
                    CreateGitTagAction
                ),
                payload=CreateGitTagRunPayload(tag=tag, message=message),
                meta=run_context.meta,
            )
        except Exception as exception:
            state.error = f"Failed to create release tag {tag}: {exception}"
            self.logger.warning(state.error)
            return result_from_state(payload, state)

        if tag_result.error is not None:
            state.error = f"Failed to create release tag {tag}: {tag_result.error}"
            self.logger.warning(state.error)
            return result_from_state(payload, state)

        # Offer the ref to the push regardless of whether it was newly created:
        # a tag created but not pushed on an earlier run must be re-pushed, and a
        # push of an already-present ref is a remote no-op.
        state.created_refs = [*state.created_refs, tag]

        return result_from_state(payload, state)
