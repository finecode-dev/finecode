"""e2e tests for `finecode bootstrap`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_bootstrap(cwd: Path, *extra_args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "finecode", "bootstrap"] + list(extra_args),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _python_in_venv(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def bootstrap_workspace(tmp_path: Path) -> Path:
    """Minimal FineCode project ready for bootstrapping.

    ``finecode`` must appear in the ``dev_workspace`` dependency group so that
    the workspace scanner assigns the project ``CONFIG_VALID`` status.  Without
    it the project is marked ``NO_FINECODE``, no runner is started, and
    ``create_envs`` / ``install_envs`` are never collected.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\n'
        'name = "test-project"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        '\n'
        '[tool.finecode]\n'
        '\n'
        '[dependency-groups]\n'
        'dev_workspace = ["finecode"]\n',
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_bootstrap_creates_dev_workspace_venv(bootstrap_workspace: Path) -> None:
    """bootstrap creates .venvs/dev_workspace/ with a working Python binary."""
    result = _run_bootstrap(bootstrap_workspace)

    assert result.returncode == 0, (
        f"bootstrap exited {result.returncode}\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )

    venv_dir = bootstrap_workspace / ".venvs" / "dev_workspace"
    assert venv_dir.exists(), "dev_workspace venv directory was not created"

    python = _python_in_venv(venv_dir)
    assert python.exists(), f"Python binary not found at {python}"

    # Venv is functional: pip is importable
    check = subprocess.run(
        [str(python), "-c", "import pip"],
        capture_output=True,
        timeout=10,
    )
    assert check.returncode == 0, "pip is not importable in the new venv"


def test_bootstrap_idempotent(bootstrap_workspace: Path) -> None:
    """Running bootstrap twice without --recreate is a no-op on the second run."""
    r1 = _run_bootstrap(bootstrap_workspace)
    assert r1.returncode == 0, f"First bootstrap failed:\n{r1.stderr}"

    venv_mtime = (bootstrap_workspace / ".venvs" / "dev_workspace").stat().st_mtime

    # Second run should skip immediately (venv already exists)
    r2 = _run_bootstrap(bootstrap_workspace, timeout=30)
    assert r2.returncode == 0, f"Second bootstrap failed:\n{r2.stderr}"

    assert (bootstrap_workspace / ".venvs" / "dev_workspace").stat().st_mtime == venv_mtime, (
        "Venv was modified on the second run — bootstrap is not idempotent"
    )


def test_bootstrap_recreate_rebuilds_venv(bootstrap_workspace: Path) -> None:
    """--recreate deletes the existing venv and creates a fresh one."""
    r1 = _run_bootstrap(bootstrap_workspace)
    assert r1.returncode == 0, f"Initial bootstrap failed:\n{r1.stderr}"

    r2 = _run_bootstrap(bootstrap_workspace, "--recreate")
    assert r2.returncode == 0, f"bootstrap --recreate failed:\n{r2.stderr}"

    venv_dir = bootstrap_workspace / ".venvs" / "dev_workspace"
    assert venv_dir.exists(), "dev_workspace venv was not recreated"
    assert _python_in_venv(venv_dir).exists(), "Python binary missing after recreate"


def test_bootstrap_fails_without_pyproject(tmp_path: Path) -> None:
    """bootstrap exits non-zero and mentions pyproject.toml when it is absent."""
    result = _run_bootstrap(tmp_path, timeout=30)

    assert result.returncode != 0
    assert "pyproject.toml" in result.stderr


def test_bootstrap_fails_without_dev_workspace_group(tmp_path: Path) -> None:
    """bootstrap exits non-zero when the dev_workspace dependency group is absent."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test-project"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    result = _run_bootstrap(tmp_path, timeout=30)

    assert result.returncode != 0
    assert "dev_workspace" in result.stderr


def test_bootstrap_fails_without_finecode_in_dev_workspace(tmp_path: Path) -> None:
    """bootstrap exits non-zero when dev_workspace exists but 'finecode' is not listed."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test-project"\nversion = "0.1.0"\n\n[dependency-groups]\ndev_workspace = []\n',
        encoding="utf-8",
    )

    result = _run_bootstrap(tmp_path, timeout=30)

    assert result.returncode != 0
    assert "finecode" in result.stderr
