"""Reading the project's declared toolchain range from a Python tool handler.

Every tool that targets a language level (ruff's ``target-version``, black's
``--target-version``, isort's ``py_version``) needs the same answer, wants it once, and
must not fail because of it. This keeps that in one place instead of in each handler.
"""

from __future__ import annotations

import asyncio
import dataclasses

from fine_src_artifacts import get_src_artifact_toolchain_range_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner


@dataclasses.dataclass(frozen=True)
class PythonSupportRange:
    """The ends of the project's declared support range, as ``X.Y`` strings."""

    min_version: str | None = None
    max_version: str | None = None


class PythonSupportRangeResolver:
    """Resolves the range once per handler and hands out the cached answer.

    Handlers construct one in ``__init__`` (nothing async happens there) and await
    ``get`` from ``run``, where a ``RunActionMeta`` is available.

    The answer is cached for the runner's life. A change to ``requires-python``
    therefore takes effect when the runner next starts, which is the same lifetime the
    settings of a tool's language server already have.
    """

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self._action_runner = action_runner
        self._logger = logger
        self._lock = asyncio.Lock()
        self._range: PythonSupportRange | None = None

    async def get(self, meta: code_action.RunActionMeta) -> PythonSupportRange:
        """Return the declared range, empty when there is nothing to derive from.

        Never raises: a tool that cannot learn the range falls back to its own default,
        which is a worse lint run, while raising here would be no lint run at all.
        """
        if self._range is not None:
            return self._range

        async with self._lock:
            if self._range is None:
                self._range = await self._resolve(meta)
        return self._range

    async def _resolve(self, meta: code_action.RunActionMeta) -> PythonSupportRange:
        action = get_src_artifact_toolchain_range_action
        try:
            result = await self._action_runner.run_action(
                action_type=iprojectactionrunner.ActionRef.from_type(
                    action.GetSrcArtifactToolchainRangeAction
                ),
                payload=action.GetSrcArtifactToolchainRangeRunPayload(),
                meta=meta,
            )
        except iprojectactionrunner.ActionNotFound:
            # expected whenever the preset providing the language handler is not in use
            self._logger.debug(
                "get_src_artifact_toolchain_range is not registered; the tool keeps its"
                " own default language level"
            )
            return PythonSupportRange()
        except Exception as error:
            # deliberately broad. The caller is a linter or a formatter about to do its
            # actual job, and this is one optional input to it: whatever went wrong --
            # an unregistered action reported some other way, an unreachable env, a
            # malformed requires-python -- degrading to the tool's own default language
            # level is a worse run, while propagating is no run at all.
            message = getattr(error, "message", None) or str(error)
            self._logger.warning(
                f"Could not determine the toolchain support range: {message}."
                " The tool keeps its own default language level."
            )
            return PythonSupportRange()

        self._logger.debug(
            f"Toolchain support range: {result.min_version} .. {result.max_version}"
            f" (derived from: {result.derived_from})"
        )
        return PythonSupportRange(
            min_version=result.min_version, max_version=result.max_version
        )
