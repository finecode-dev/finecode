import pathlib

from fine_envs import env_inventory
from fine_envs.env_inventory import EnvState


def _make_venv(venvs_dir: pathlib.Path, name: str) -> pathlib.Path:
    venv_dir = venvs_dir / name
    venv_dir.mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")
    return venv_dir


def test_read_existing_envs_classifies_dirs(tmp_path: pathlib.Path) -> None:
    venvs_dir = tmp_path / ".venvs"
    _make_venv(venvs_dir, "dev")
    (venvs_dir / "half_created").mkdir()
    (venvs_dir / ".gitignore").write_text("*\n")

    existing = env_inventory.read_existing_envs(venvs_dir)

    assert existing == {
        "dev": EnvState.CREATED,
        # A directory without pyvenv.cfg is not a usable venv, but it is still
        # something occupying an env's slot and must be reportable/removable.
        "half_created": EnvState.BROKEN,
    }


def test_read_existing_envs_without_venvs_dir(tmp_path: pathlib.Path) -> None:
    """A project whose envs were never created must not fail listing."""
    assert env_inventory.read_existing_envs(tmp_path / ".venvs") == {}


def test_matrix_base_venv_is_the_only_orphan(tmp_path: pathlib.Path) -> None:
    """The regression this feature exists for: `testing` became a matrix env,
    so config declares only its concrete children and the old `.venvs/testing`
    is left referenced by nothing."""
    venvs_dir = tmp_path / ".venvs"

    entries = env_inventory.scan_envs(
        declared_names=[
            "dev_workspace",
            "testing@cpython-3.11",
            "testing@cpython-3.12",
        ],
        venvs_dir_path=venvs_dir,
        existing={
            "dev_workspace": EnvState.CREATED,
            "testing": EnvState.CREATED,
            "testing@cpython-3.11": EnvState.CREATED,
            "testing@cpython-3.12": EnvState.CREATED,
        },
    )

    assert [entry.name for entry in entries if entry.orphaned] == ["testing"]


def test_declared_env_without_venv_is_missing_not_orphaned(
    tmp_path: pathlib.Path,
) -> None:
    """A matrix child that was never created (e.g. deselected by
    `default_interpreters`) is still declared — reporting it as an orphan
    would offer to delete an env the user wants."""
    entries = env_inventory.scan_envs(
        declared_names=["testing@cpython-3.14"],
        venvs_dir_path=tmp_path / ".venvs",
        existing={},
    )

    assert len(entries) == 1
    assert entries[0].state is EnvState.MISSING
    assert entries[0].orphaned is False


def test_scan_envs_orders_declared_first_then_orphans(tmp_path: pathlib.Path) -> None:
    entries = env_inventory.scan_envs(
        declared_names=["dev_workspace", "dev"],
        venvs_dir_path=tmp_path / ".venvs",
        existing={
            "dev_workspace": EnvState.CREATED,
            "zeta": EnvState.CREATED,
            "alpha": EnvState.BROKEN,
        },
    )

    assert [entry.name for entry in entries] == [
        "dev_workspace",
        "dev",
        "alpha",
        "zeta",
    ]


def test_scan_envs_uses_venvs_dir_for_paths(tmp_path: pathlib.Path) -> None:
    venvs_dir = tmp_path / ".venvs"

    entries = env_inventory.scan_envs(
        declared_names=["dev"],
        venvs_dir_path=venvs_dir,
        existing={"dev": EnvState.CREATED},
    )

    assert entries[0].venv_dir_path == (venvs_dir / "dev").as_uri()
