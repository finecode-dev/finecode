"""Shared inventory of a project's environments: what config declares and
what exists on disk.

An environment is *declared* when its name is a key of the project's resolved
``[dependency-groups]``. Because the WM merges presets and expands interpreter
matrices before a handler sees the config (ADR-0047, see
``IProjectInfoProvider.get_project_raw_config``), a matrix base name such as
``testing`` is *not* declared — only its concrete children
(``testing@cpython-3.11``, ...) are. A venv directory left behind by a base
name that was matrixed, by a renamed env, or by a preset change is therefore
*orphaned*: nothing references it, no Extension Runner will ever start in it,
and ``prepare-envs --recreate`` will not touch it because that only rebuilds
envs discovery found.
"""

import dataclasses
import enum
import pathlib
from collections.abc import Iterable, Mapping

from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri

PYVENV_CFG_NAME = "pyvenv.cfg"


class EnvState(enum.StrEnum):
    CREATED = "created"
    """The venv directory exists and carries a `pyvenv.cfg`."""
    BROKEN = "broken"
    """Something exists at the venv path, but it is not a usable venv."""
    MISSING = "missing"
    """Declared in config, but nothing exists on disk yet."""


@dataclasses.dataclass
class EnvEntry:
    name: str
    venv_dir_path: ResourceUri
    declared: bool
    """Whether the project's resolved `[dependency-groups]` still names this env."""
    state: EnvState

    @property
    def orphaned(self) -> bool:
        """Exists on disk but nothing declares it — safe to remove."""
        return not self.declared


def read_existing_envs(venvs_dir_path: pathlib.Path) -> dict[str, EnvState]:
    """Map every entry of ``.venvs/`` to its on-disk state.

    Entries whose name starts with a dot are skipped: they are tooling
    artifacts (`.gitignore`, editor state), not environments.
    """
    if not venvs_dir_path.is_dir():
        return {}

    existing: dict[str, EnvState] = {}
    for entry in venvs_dir_path.iterdir():
        if entry.name.startswith("."):
            continue
        if entry.is_dir() and (entry / PYVENV_CFG_NAME).exists():
            existing[entry.name] = EnvState.CREATED
        else:
            existing[entry.name] = EnvState.BROKEN

    return existing


def scan_envs(
    declared_names: Iterable[str],
    venvs_dir_path: pathlib.Path,
    existing: Mapping[str, EnvState],
) -> list[EnvEntry]:
    """Combine declared env names with what `read_existing_envs` found.

    Declared envs come first in declaration order, then orphans sorted by name
    — the reading order of `list_envs`, where the interesting rows are last.

    The declared set must be the *full* one. Selection narrowed by ``--env`` /
    ``--interpreter`` / ``default_interpreters`` restricts a single
    prepare-envs run; it does not un-declare an env, and treating a
    deselected matrix child as an orphan would delete envs the user still
    wants.
    """
    entries: list[EnvEntry] = []
    declared_set: set[str] = set()

    for name in declared_names:
        declared_set.add(name)
        entries.append(
            EnvEntry(
                name=name,
                venv_dir_path=path_to_resource_uri(venvs_dir_path / name),
                declared=True,
                state=existing.get(name, EnvState.MISSING),
            )
        )

    for name in sorted(existing.keys() - declared_set):
        entries.append(
            EnvEntry(
                name=name,
                venv_dir_path=path_to_resource_uri(venvs_dir_path / name),
                declared=False,
                state=existing[name],
            )
        )

    return entries
