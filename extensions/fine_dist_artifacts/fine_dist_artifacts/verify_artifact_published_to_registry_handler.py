# docs: docs/reference/actions.md
import asyncio
import dataclasses
import typing

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    iprojectactionrunner,
    irepositorycredentialsprovider,
)
from finecode_extension_api.resource_uri import resource_uri_to_path

from fine_dist_artifacts import (
    list_published_artifacts_action,
    verify_artifact_published_to_registry_action,
)

_DEADLINE_SECONDS = 60.0
_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)
_MAX_ATTEMPTS = len(_RETRY_DELAYS) + 1

# A listing abandoned at the deadline runs until it acknowledges cancellation,
# possibly after the handler returned. The strong reference keeps it from being
# garbage-collected mid-flight.
_ABANDONED: set[asyncio.Task[typing.Any]] = set()


def _abandon(task: asyncio.Task[typing.Any]) -> None:
    _ = task.cancel()
    _ABANDONED.add(task)
    task.add_done_callback(_forget)


def _forget(task: asyncio.Task[typing.Any]) -> None:
    _ABANDONED.discard(task)
    if not task.cancelled():
        # Retrieve a late failure so the event loop does not report it as unhandled.
        _ = task.exception()


@dataclasses.dataclass
class VerifyArtifactPublishedToRegistryHandlerConfig(
    code_action.ActionHandlerConfig
): ...


class VerifyArtifactPublishedToRegistryHandler(
    code_action.ActionHandler[
        verify_artifact_published_to_registry_action.VerifyArtifactPublishedToRegistryAction,
        VerifyArtifactPublishedToRegistryHandlerConfig,
    ]
):
    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        repository_credentials_provider: irepositorycredentialsprovider.IRepositoryCredentialsProvider,
    ) -> None:
        self.action_runner = action_runner
        self.repository_credentials_provider = repository_credentials_provider

    async def run(
        self,
        payload: verify_artifact_published_to_registry_action.VerifyArtifactPublishedToRegistryRunPayload,
        run_context: verify_artifact_published_to_registry_action.VerifyArtifactPublishedToRegistryRunContext,
    ) -> verify_artifact_published_to_registry_action.VerifyArtifactPublishedToRegistryRunResult:
        requested = [
            resource_uri_to_path(uri).name for uri in payload.dist_artifact_paths
        ]
        if not requested:
            return verify_artifact_published_to_registry_action.VerifyArtifactPublishedToRegistryRunResult(
                errors=[]
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _DEADLINE_SECONDS
        listing_payload = (
            list_published_artifacts_action.ListPublishedArtifactsRunPayload(
                src_artifact_def_path=payload.src_artifact_def_path,
                version=payload.version,
                registry_name=payload.registry_name,
            )
        )
        missing = requested
        last_error: Exception | None = None
        attempts = 0
        deadline_reached = False

        for delay in (*_RETRY_DELAYS, None):
            remaining = deadline - loop.time()
            if remaining <= 0:
                deadline_reached = True
                break
            # Created before awaiting, so source-config reads count against the deadline.
            listing: asyncio.Task[
                list_published_artifacts_action.ListPublishedArtifactsRunResult
            ] = asyncio.create_task(
                self.action_runner.run_action(
                    action_type=iprojectactionrunner.ActionRef.from_type(
                        list_published_artifacts_action.ListPublishedArtifactsAction
                    ),
                    payload=listing_payload,
                    meta=run_context.meta,
                )
            )
            attempts += 1
            # Not `wait_for`/`TaskGroup`/`timeout`: they wait for the cancelled child.
            try:
                done, _ = await asyncio.wait({listing}, timeout=remaining)
            except asyncio.CancelledError:
                _abandon(listing)
                raise
            if not done:
                _abandon(listing)
                deadline_reached = True
                break

            try:
                published = set(listing.result().filenames)
            except iprojectactionrunner.ActionRunCancelled:
                raise
            except Exception as exception:  # noqa: BLE001 -- any listing failure is retried
                last_error = exception
            else:
                missing = [name for name in requested if name not in published]
                if not missing:
                    return verify_artifact_published_to_registry_action.VerifyArtifactPublishedToRegistryRunResult(
                        errors=[]
                    )
                last_error = None

            remaining = deadline - loop.time()
            if delay is not None and remaining > 0:
                await asyncio.sleep(min(delay, remaining))

        repository = self.repository_credentials_provider.get_repository(
            payload.registry_name
        )
        index_url = repository.index_url if repository is not None else "unknown"
        exhausted = (
            f"{_DEADLINE_SECONDS:g} s deadline" if deadline_reached else "attempt limit"
        )
        outcome = (
            f"after {attempts} of {_MAX_ATTEMPTS} attempts ({exhausted} exhausted)"
        )
        registry = f"Registry '{payload.registry_name}' ({index_url})"
        if last_error is not None:
            raise code_action.ActionFailedException(
                f"{registry}: verification failed {outcome}: {last_error}"
            ) from last_error
        return verify_artifact_published_to_registry_action.VerifyArtifactPublishedToRegistryRunResult(
            errors=[
                f"{registry}: file '{name}' not confirmed {outcome}" for name in missing
            ]
        )
