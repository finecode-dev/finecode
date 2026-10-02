import pytest

from finecode.wm_server.config.env_selection import resolve_env_selection
from finecode.wm_server.services.prepare_envs_service import (
    build_create_envs_params,
    build_install_envs_params,
    dev_workspace_recreate_requested,
)


def _env(interpreter: str | None = None) -> dict:
    return {"interpreter": interpreter} if interpreter is not None else {}


def _matrix_base(base: str, versions: list[str]) -> dict[str, dict]:
    return {f"{base}@cpython-{v}": _env(interpreter=f"cpython@{v}") for v in versions}


class TestBuildCreateEnvsParams:
    """`--recreate` must reach `fine_envs.CreateEnvsAction` regardless of whether
    an `--env`/`--interpreter` selection is active — this is the fix for the bug
    where step 5's `create_envs` silently dropped `recreate` on the floor."""

    def test_recreate_true_with_no_selection_forwards_recreate(self) -> None:
        env_table = {"dev_no_runtime": _env(), "docs": _env()}
        sel = resolve_env_selection(env_table, [], [], "cli")

        params = build_create_envs_params(sel, env_table, recreate=True)

        assert params["recreate"] is True
        assert "env_names" not in params

    def test_recreate_false_with_no_selection_forwards_recreate(self) -> None:
        env_table = {"dev_no_runtime": _env(), "docs": _env()}
        sel = resolve_env_selection(env_table, [], [], "cli")

        params = build_create_envs_params(sel, env_table, recreate=False)

        assert params["recreate"] is False
        assert "env_names" not in params

    def test_recreate_true_with_active_env_selection_forwards_both(self) -> None:
        """`--env=testing --recreate`: env_names narrows to testing's children,
        and recreate must still be forwarded."""
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12"]),
            "dev_no_runtime": _env(),
        }
        sel = resolve_env_selection(env_table, ["testing"], [], "cli")

        params = build_create_envs_params(sel, env_table, recreate=True)

        assert params["recreate"] is True
        assert params["env_names"] == sorted(
            {"testing@cpython-3.11", "testing@cpython-3.12"}
        )

    def test_recreate_false_with_active_env_selection_still_forwards_recreate_key(
        self,
    ) -> None:
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12"]),
            "dev_no_runtime": _env(),
        }
        sel = resolve_env_selection(env_table, ["testing@cpython-3.11"], [], "cli")

        params = build_create_envs_params(sel, env_table, recreate=False)

        assert params["recreate"] is False
        assert params["env_names"] == sorted({"testing@cpython-3.11"})


class TestBuildCreateEnvsParamsExcludesDevWorkspace:
    """`dev_workspace` is already created for every project by steps 2-3's
    dedicated bootstrap (executed on the *root* project's runner). By step 5 the
    project's own `dev_workspace` runner is already started, so re-including
    `dev_workspace` here would make that runner recreate the very venv it is
    executing from — deleting its own `uv`/`python` before it can run them.
    `dev_workspace` must therefore never appear in step 5's `create_envs`
    env set, with or without an active `--env`/`--interpreter` selection."""

    def test_no_selection_excludes_dev_workspace(self) -> None:
        env_table = {
            "dev_workspace": _env(),
            "dev_no_runtime": _env(),
            "docs": _env(),
        }
        sel = resolve_env_selection(env_table, [], [], "cli")

        params = build_create_envs_params(sel, env_table, recreate=True)

        assert params["recreate"] is True
        assert params["env_names"] == sorted({"dev_no_runtime", "docs"})
        assert "dev_workspace" not in params["env_names"]

    def test_no_selection_and_no_dev_workspace_in_universe_omits_env_names(
        self,
    ) -> None:
        """Unaffected case: a project whose universe has no `dev_workspace` key
        keeps the original no-selection behavior of omitting `env_names`."""
        env_table = {"dev_no_runtime": _env(), "docs": _env()}
        sel = resolve_env_selection(env_table, [], [], "cli")

        params = build_create_envs_params(sel, env_table, recreate=True)

        assert "env_names" not in params

    def test_active_selection_excludes_dev_workspace(self) -> None:
        env_table = {
            "dev_workspace": _env(),
            **_matrix_base("testing", ["3.11", "3.12"]),
            "dev_no_runtime": _env(),
        }
        sel = resolve_env_selection(env_table, ["testing"], [], "cli")

        params = build_create_envs_params(sel, env_table, recreate=True)

        assert params["env_names"] == sorted(
            {"testing@cpython-3.11", "testing@cpython-3.12"}
        )
        assert "dev_workspace" not in params["env_names"]

    def test_explicit_env_dev_workspace_selector_still_excludes_it(self) -> None:
        """Even an explicit `--env=dev_workspace` must not reach step 5's
        create_envs call: `dev_workspace` creation is exclusively owned by
        steps 2-3's root-executed bootstrap, regardless of selector."""
        env_table = {"dev_workspace": _env(), "dev_no_runtime": _env()}
        sel = resolve_env_selection(env_table, ["dev_workspace"], [], "cli")

        params = build_create_envs_params(sel, env_table, recreate=True)

        assert params["env_names"] == []


class TestBuildCreateEnvsParamsFollowsEnvFilter:
    """An `--env` filter narrows step 5 the same way it narrows step 6, so a
    filtered run never creates venvs it will not install into."""

    def test_env_filter_selects_only_the_named_env(self) -> None:
        """A filtered run covers only the named env; the venvs it skips stay
        untouched until the next unfiltered run or on-demand repair."""
        env_table = {
            "dev_workspace": _env(),
            "dev_no_runtime": _env(),
            "docs": _env(),
            "runtime": _env(),
        }
        sel = resolve_env_selection(env_table, ["dev_no_runtime"], [], "cli")

        params = build_create_envs_params(sel, env_table, recreate=True)

        assert params["env_names"] == ["dev_no_runtime"]
        assert params["recreate"] is True

    def test_interpreter_only_filter_keeps_every_non_matrix_env(self) -> None:
        """`--interpreter` alone narrows matrix children but keeps non-matrix
        envs covered, so they are still created and installed."""
        env_table = {
            "dev_workspace": _env(),
            **_matrix_base("testing", ["3.11", "3.12"]),
            "dev_no_runtime": _env(),
            "docs": _env(),
        }
        sel = resolve_env_selection(env_table, [], ["3.12"], "cli")

        params = build_create_envs_params(sel, env_table, recreate=False)

        assert params["env_names"] == [
            "dev_no_runtime",
            "docs",
            "testing@cpython-3.12",
        ]

    @pytest.mark.parametrize(
        ("env_selectors", "interpreter_selectors"),
        [
            ([], []),
            (["dev_no_runtime"], []),
            (["testing"], []),
            ([], ["3.12"]),
            (["dev_workspace"], []),
        ],
    )
    def test_create_install_parity(
        self, env_selectors: list[str], interpreter_selectors: list[str]
    ) -> None:
        """Step 5 and step 6 always cover the same envs, so an install never
        targets a venv this run did not create. The table contains
        `dev_workspace`, so both builders always produce `env_names`."""
        env_table = {
            "dev_workspace": _env(),
            "dev_no_runtime": _env(),
            "docs": _env(),
            **_matrix_base("testing", ["3.11", "3.12"]),
        }
        sel = resolve_env_selection(
            env_table, env_selectors, interpreter_selectors, "cli"
        )

        create_params = build_create_envs_params(sel, env_table, recreate=False)
        install_params = build_install_envs_params(sel, env_table)

        assert create_params["env_names"] == install_params["env_names"]


class TestBuildInstallEnvsParams:
    """Step 6 covers exactly the selected envs, never the already-installed
    `dev_workspace`, so preset-resolved deps are not reinstalled from the env
    being replaced."""

    def test_no_selection_installs_everything_minus_dev_workspace(self) -> None:
        env_table = {
            "dev_workspace": _env(),
            "dev_no_runtime": _env(),
            "docs": _env(),
        }
        sel = resolve_env_selection(env_table, [], [], "cli")

        params = build_install_envs_params(sel, env_table)

        assert params["env_names"] == ["dev_no_runtime", "docs"]

    def test_env_filter_installs_only_selected_matrix_children(self) -> None:
        env_table = {
            "dev_workspace": _env(),
            **_matrix_base("testing", ["3.11", "3.12"]),
            "dev_no_runtime": _env(),
        }
        sel = resolve_env_selection(env_table, ["testing"], [], "cli")

        params = build_install_envs_params(sel, env_table)

        assert params["env_names"] == [
            "testing@cpython-3.11",
            "testing@cpython-3.12",
        ]

    def test_no_selection_without_dev_workspace_installs_every_env(self) -> None:
        """Unlike the create builder (which omits `env_names` here), the install
        builder always names its envs, so callers never branch on the key."""
        env_table = {"dev_no_runtime": _env(), "docs": _env()}
        sel = resolve_env_selection(env_table, [], [], "cli")

        params = build_install_envs_params(sel, env_table)

        assert params["env_names"] == ["dev_no_runtime", "docs"]


class TestDevWorkspaceRecreateRequested:
    """`--recreate` follows the `--env` filter, so a filtered rebuild never
    wipes the bootstrap venv every later step runs on."""

    @pytest.mark.parametrize(
        ("recreate", "env_names", "expected"),
        [
            (False, None, False),
            (False, ["dev_workspace"], False),
            (True, None, True),
            (True, [], True),
            (True, ["dev_no_runtime"], False),
            (True, ["testing"], False),
            (True, ["dev_no_runtime", "dev_workspace"], True),
        ],
    )
    def test_truth_table(
        self, recreate: bool, env_names: list[str] | None, expected: bool
    ) -> None:
        assert dev_workspace_recreate_requested(recreate, env_names) is expected
