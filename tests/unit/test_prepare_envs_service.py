from finecode.wm_server.config.env_selection import resolve_env_selection
from finecode.wm_server.services import prepare_envs_service
from finecode.wm_server.services.prepare_envs_service import build_create_envs_params


def _env(interpreter: str | None = None) -> dict:
    return {"interpreter": interpreter} if interpreter is not None else {}


def _matrix_base(base: str, versions: list[str]) -> dict[str, dict]:
    return {
        f"{base}@cpython-{v}": _env(interpreter=f"cpython@{v}") for v in versions
    }


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
            {"testing@cpython-3.11", "testing@cpython-3.12", "dev_no_runtime"}
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
        assert params["env_names"] == sorted({"testing@cpython-3.11", "dev_no_runtime"})


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

    def test_no_selection_and_no_dev_workspace_in_universe_omits_env_names(self) -> None:
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
            {"testing@cpython-3.11", "testing@cpython-3.12", "dev_no_runtime"}
        )
        assert "dev_workspace" not in params["env_names"]

    def test_explicit_env_dev_workspace_selector_still_excludes_it(self) -> None:
        """Even an explicit `--env=dev_workspace` must not reach step 5's
        create_envs call: `dev_workspace` creation is exclusively owned by
        steps 2-3's root-executed bootstrap, regardless of selector."""
        env_table = {"dev_workspace": _env(), "dev_no_runtime": _env()}
        sel = resolve_env_selection(env_table, ["dev_workspace"], [], "cli")

        params = build_create_envs_params(sel, env_table, recreate=True)

        assert "dev_workspace" not in params.get("env_names", [])


class TestResolveProjectConcurrency:
    """Layer 1 of the prepare-envs concurrency bound (ADR-0055): how many
    projects may be prepared in parallel. Override chain is CLI flag > env
    var > machine-based default."""

    def test_prefers_cli_value(self, monkeypatch) -> None:
        monkeypatch.setenv("FINECODE_WM_PREPARE_ENVS_MAX_CONCURRENT_PROJECTS", "9")
        monkeypatch.setattr(
            prepare_envs_service, "default_layered_concurrency", lambda: 3
        )

        decision = prepare_envs_service.resolve_project_concurrency(5)
        assert decision.value == 5
        assert "flag" in decision.source

    def test_clamps_non_positive_cli_value_to_one(self, monkeypatch) -> None:
        monkeypatch.setattr(
            prepare_envs_service, "default_layered_concurrency", lambda: 3
        )

        assert prepare_envs_service.resolve_project_concurrency(0).value == 1
        assert prepare_envs_service.resolve_project_concurrency(-4).value == 1

    def test_falls_back_to_env_var_when_cli_unset(self, monkeypatch) -> None:
        monkeypatch.setenv("FINECODE_WM_PREPARE_ENVS_MAX_CONCURRENT_PROJECTS", "6")
        monkeypatch.setattr(
            prepare_envs_service, "default_layered_concurrency", lambda: 3
        )

        decision = prepare_envs_service.resolve_project_concurrency(None)
        assert decision.value == 6
        assert "env var" in decision.source

    def test_falls_back_to_default_when_nothing_set(self, monkeypatch) -> None:
        monkeypatch.delenv(
            "FINECODE_WM_PREPARE_ENVS_MAX_CONCURRENT_PROJECTS", raising=False
        )
        monkeypatch.setattr(
            prepare_envs_service, "default_layered_concurrency", lambda: 3
        )

        decision = prepare_envs_service.resolve_project_concurrency(None)
        assert decision.value == 3
        assert "default" in decision.source
