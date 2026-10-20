import pytest

from finecode.wm_server.config.env_selection import (
    EnvSelection,
    EnvSelectionError,
    compute_prepare_set,
    env_selector_known_in,
    interpreter_selector_known_in,
    resolve_env_selection,
    resolve_run_selection,
)
from finecode.wm_server.config.interpreter_matrix import InvalidInterpreterError


def _env(
    interpreter: str | None = None, default_interpreters: dict | None = None
) -> dict:
    entry: dict = {}
    if interpreter is not None:
        entry["interpreter"] = interpreter
    if default_interpreters is not None:
        entry["default_interpreters"] = default_interpreters
    return entry


def _matrix_base(
    base: str,
    versions: list[str],
    default_interpreters: dict | None = None,
    implementation: str = "cpython",
) -> dict[str, dict]:
    """Build env-table entries for a matrix base with the given (impl,version) axis.

    `versions` items may be "3.11" (defaults to cpython) or "pypy@3.11".
    """
    table: dict[str, dict] = {}
    for v in versions:
        if "@" in v:
            impl, version = v.split("@")
        else:
            impl, version = implementation, v
        table[f"{base}@{impl}-{version}"] = _env(
            interpreter=f"{impl}@{version}", default_interpreters=default_interpreters
        )
    return table


def _ac_table() -> dict[str, dict]:
    """Shared AC1-AC3 table: dev_workspace, dev_no_runtime + testing 3.11-3.14 newest."""
    return {
        "dev_workspace": _env(),
        "dev_no_runtime": _env(),
        **_matrix_base(
            "testing", ["3.11", "3.12", "3.13", "3.14"], {"local": "newest"}
        ),
    }


class TestNoSelectors:
    def test_no_selectors_and_no_default_selects_all_and_is_inactive(self) -> None:
        """With nothing constraining anything, every env is selected and the
        selection is inactive — today's full-axis, create-all behaviour
        (PRD-0003-R7)."""
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12", "3.13"]),
            "dev_no_runtime": _env(),
        }

        selection = resolve_env_selection(env_table, [], "cli")

        assert selection.active is False
        assert selection.selected_env_names == set(env_table.keys())


class TestEnvSelector:
    def test_env_equal_to_base_name_selects_its_default_which_is_all_without_a_policy(
        self,
    ) -> None:
        """`--env=<base>` selects the base's default subset, which is all children
        when no policy is declared."""
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12", "3.13"]),
            "dev_no_runtime": _env(),
        }

        selection = resolve_env_selection(env_table, ["testing"], "cli")

        assert selection.active is True
        assert selection.selected_env_names == {
            "testing@cpython-3.11",
            "testing@cpython-3.12",
            "testing@cpython-3.13",
        }

    def test_env_equal_to_concrete_child_name_selects_only_that_child(self) -> None:
        """`--env=<base>@cpython-3.11` (the exact concrete env name) selects only
        that one child, not its siblings."""
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12", "3.13"]),
            "dev_no_runtime": _env(),
        }

        selection = resolve_env_selection(env_table, ["testing@cpython-3.11"], "cli")

        assert selection.selected_env_names == {"testing@cpython-3.11"}

    def test_non_matrix_env_name_passes_through(self) -> None:
        """`--env=<non-matrix-name>`: only the
        named non-matrix env is selected."""
        env_table = {
            "dev_no_runtime": _env(),
            "docs": _env(),
        }

        selection = resolve_env_selection(env_table, ["dev_no_runtime"], "cli")

        assert selection.active is True
        assert selection.selected_env_names == {"dev_no_runtime"}

    def test_unknown_env_selector_selects_nothing_and_does_not_raise(self) -> None:
        """An `--env` value that matches nothing in this project's table selects
        nothing here — cross-project validation happens at the service layer,
        not in the pure resolver."""
        env_table = {"dev_no_runtime": _env()}

        selection = resolve_env_selection(env_table, ["nonexistent"], "cli")

        assert selection.selected_env_names == set()

    def test_named_base_excludes_unnamed_base_entirely(self) -> None:
        """With an explicit `--env`, a matrix base not named in any form
        contributes nothing (ADR-0103)."""
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12", "3.13"], {"local": "newest"}),
            **_matrix_base("stubs", ["3.11", "3.12"], {"local": "oldest"}),
        }

        selection = resolve_env_selection(env_table, ["testing"], "cli")

        assert selection.selected_env_names == {"testing@cpython-3.13"}


class TestPrepareSelectionForms:
    def test_dev_workspace_only_selects_dev_workspace(self) -> None:
        env_table = _ac_table()

        selection = resolve_env_selection(env_table, ["dev_workspace"], "cli")

        assert selection.selected_env_names == {"dev_workspace"}

    def test_no_env_selects_default_subset_locally_and_all_in_ci(self) -> None:
        env_table = _ac_table()

        cli_selection = resolve_env_selection(env_table, [], "cli")

        assert cli_selection.selected_env_names == {
            "dev_workspace",
            "dev_no_runtime",
            "testing@cpython-3.14",
        }
        ci_selection = resolve_env_selection(env_table, [], "ci")

        assert ci_selection.selected_env_names == set(env_table.keys())
        assert ci_selection.active is False

    def test_testing_base_forms(self) -> None:
        env_table = _ac_table()

        assert resolve_env_selection(
            env_table, ["testing"], "cli"
        ).selected_env_names == {"testing@cpython-3.14"}
        assert resolve_env_selection(
            env_table, ["testing"], "ci"
        ).selected_env_names == {
            "testing@cpython-3.11",
            "testing@cpython-3.12",
            "testing@cpython-3.13",
            "testing@cpython-3.14",
        }
        assert resolve_env_selection(
            env_table, ["testing@all"], "cli"
        ).selected_env_names == {
            "testing@cpython-3.11",
            "testing@cpython-3.12",
            "testing@cpython-3.13",
            "testing@cpython-3.14",
        }
        assert resolve_env_selection(
            env_table, ["testing@cpython-3.12"], "cli"
        ).selected_env_names == {"testing@cpython-3.12"}

    def test_env_forms_union(self) -> None:
        env_table = _ac_table()

        selection = resolve_env_selection(
            env_table, ["testing", "testing@cpython-3.11"], "cli"
        )

        assert selection.selected_env_names == {
            "testing@cpython-3.14",
            "testing@cpython-3.11",
        }


class TestRunSelection:
    def test_no_selector_selects_default_subset(self) -> None:
        env_table = _ac_table()

        assert resolve_run_selection(env_table, [], "cli") == {"testing@cpython-3.14"}

    def test_explicit_interpreter_selects_matching_children(self) -> None:
        env_table = _ac_table()

        assert resolve_run_selection(env_table, ["3.12"], "cli") == {
            "testing@cpython-3.12"
        }

    def test_all_selects_full_axis_as_none(self) -> None:
        env_table = _ac_table()

        assert resolve_run_selection(env_table, ["all"], "cli") is None

    def test_all_with_other_selectors_is_still_none(self) -> None:
        env_table = _ac_table()

        assert resolve_run_selection(env_table, ["all", "3.12"], "cli") is None

    def test_malformed_value_next_to_all_still_raises(self) -> None:
        env_table = _ac_table()

        with pytest.raises(InvalidInterpreterError):
            resolve_run_selection(env_table, ["all", "a@b@c"], "cli")

    def test_non_matrix_env_never_in_run_selection(self) -> None:
        env_table = _ac_table()

        result = resolve_run_selection(env_table, [], "cli")

        assert result is not None
        assert "dev_workspace" not in result
        assert "dev_no_runtime" not in result

    def test_interpreter_shorthand_selects_matching_children_across_bases(self) -> None:
        """`--interpreter=3.11` selects the cpython@3.11 child of every matrix
        base that declares it."""
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12"]),
            **_matrix_base("stubs", ["3.11", "3.12"]),
            "dev_no_runtime": _env(),
        }

        assert resolve_run_selection(env_table, ["3.11"], "cli") == {
            "testing@cpython-3.11",
            "stubs@cpython-3.11",
        }

    def test_explicit_interpreter_overrides_config_default(self) -> None:
        """An explicit `--interpreter` selector wins outright over the base's
        config-declared default_interpreters policy."""
        env_table = _matrix_base(
            "testing", ["3.11", "3.12", "3.13"], {"local": "newest"}
        )

        assert resolve_run_selection(env_table, ["3.11"], "cli") == {
            "testing@cpython-3.11"
        }

    def test_two_bases_each_contribute_their_own_default(self) -> None:
        env_table = {
            **_matrix_base(
                "testing", ["3.11", "3.12", "3.13", "3.14"], {"local": "newest"}
            ),
            **_matrix_base("stubs", ["3.10", "3.11"], {"local": "oldest"}),
        }

        assert resolve_run_selection(env_table, [], "cli") == {
            "testing@cpython-3.14",
            "stubs@cpython-3.10",
        }

    def test_config_default_naming_interpreter_outside_axis_raises(self) -> None:
        env_table = _matrix_base("testing", ["3.11", "3.12"], {"cli": ["cpython@3.14"]})

        with pytest.raises(EnvSelectionError):
            resolve_run_selection(env_table, [], "cli")


class TestConfigDefault:
    def test_default_newest_selects_the_max_version_child(self) -> None:
        env_table = _matrix_base(
            "testing", ["3.11", "3.12", "3.13"], {"local": "newest", "ci": "all"}
        )

        selection = resolve_env_selection(env_table, [], "cli")

        assert selection.active is True
        assert selection.selected_env_names == {"testing@cpython-3.13"}

    def test_default_all_for_ci_dev_env_selects_everything_and_is_inactive(
        self,
    ) -> None:
        env_table = _matrix_base(
            "testing", ["3.11", "3.12", "3.13"], {"local": "newest", "ci": "all"}
        )

        selection = resolve_env_selection(env_table, [], "ci")

        assert selection.active is False
        assert selection.selected_env_names == set(env_table.keys())

    def test_default_oldest_selects_the_min_version_child(self) -> None:
        env_table = _matrix_base(
            "testing", ["3.11", "3.12", "3.13"], {"local": "oldest"}
        )

        selection = resolve_env_selection(env_table, [], "cli")

        assert selection.selected_env_names == {"testing@cpython-3.11"}

    def test_default_explicit_list_selects_named_interpreters(self) -> None:
        env_table = _matrix_base(
            "testing",
            ["3.11", "3.12", "3.13"],
            {"local": ["cpython@3.11", "cpython@3.13"]},
        )

        selection = resolve_env_selection(env_table, [], "cli")

        assert selection.selected_env_names == {
            "testing@cpython-3.11",
            "testing@cpython-3.13",
        }

    def test_exact_dev_env_key_beats_bucket_key(self) -> None:
        """A `default_interpreters` key exactly matching the active dev_env
        (e.g. "cli") takes precedence over the "local"/"ci" bucket key."""
        env_table = _matrix_base(
            "testing", ["3.11", "3.12", "3.13"], {"cli": "oldest", "local": "newest"}
        )

        selection = resolve_env_selection(env_table, [], "cli")

        assert selection.selected_env_names == {"testing@cpython-3.11"}

    def test_absent_default_selects_all(self) -> None:
        env_table = _matrix_base("testing", ["3.11", "3.12"])

        selection = resolve_env_selection(env_table, [], "cli")

        assert selection.active is False
        assert selection.selected_env_names == set(env_table.keys())

    def test_newest_tie_across_two_implementations_selects_both(self) -> None:
        """When the max version is shared by two implementations, both are
        selected — a shared version must never be arbitrarily dropped
        (PRD-0003-R10)."""
        env_table = _matrix_base(
            "testing",
            ["3.11", "3.12", "pypy@3.12"],
            {"local": "newest"},
        )

        selection = resolve_env_selection(env_table, [], "cli")

        assert selection.selected_env_names == {
            "testing@cpython-3.12",
            "testing@pypy-3.12",
        }

    def test_default_explicit_list_naming_interpreter_outside_axis_raises(self) -> None:
        env_table = _matrix_base(
            "testing", ["3.11", "3.12"], {"local": ["cpython@3.14"]}
        )

        with pytest.raises(EnvSelectionError):
            resolve_env_selection(env_table, [], "cli")


class TestMatrixChildNames:
    def test_matrix_child_names_includes_all_children_regardless_of_selection(
        self,
    ) -> None:
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12"]),
            "dev_no_runtime": _env(),
        }

        selection = resolve_env_selection(env_table, ["testing@cpython-3.11"], "cli")

        assert selection.matrix_child_names == {
            "testing@cpython-3.11",
            "testing@cpython-3.12",
        }


class TestDerivedGating:
    def test_inactive_selection_creates_and_installs_everything(self) -> None:
        all_names = {"testing@cpython-3.11", "testing@cpython-3.12", "dev"}
        selection = EnvSelection(
            active=False,
            selected_env_names=set(),
            matrix_child_names={"testing@cpython-3.11", "testing@cpython-3.12"},
        )

        assert compute_prepare_set(selection, all_names) == all_names

    def test_active_selection_prepares_exactly_the_selected_envs(
        self,
    ) -> None:
        """A non-matrix env outside the selection is neither created nor installed,
        the same as an unselected matrix child."""
        all_names = {"testing@cpython-3.11", "testing@cpython-3.12", "dev"}
        selection = EnvSelection(
            active=True,
            selected_env_names={"testing@cpython-3.11"},
            matrix_child_names={"testing@cpython-3.11", "testing@cpython-3.12"},
        )

        assert compute_prepare_set(selection, all_names) == {"testing@cpython-3.11"}


class TestEnvSelectorKnownIn:
    """`env_selector_known_in` — pure predicate used for cross-project
    validation by `prepare_envs_service` (PRD-0003 AC8)."""

    def test_matrix_base_name_is_known(self) -> None:
        env_table = _matrix_base("testing", ["3.11", "3.12"])

        assert env_selector_known_in("testing", env_table) is True

    def test_concrete_matrix_child_name_is_known(self) -> None:
        env_table = _matrix_base("testing", ["3.11", "3.12"])

        assert env_selector_known_in("testing@cpython-3.11", env_table) is True

    def test_non_matrix_env_name_is_known(self) -> None:
        env_table = {"dev_no_runtime": _env()}

        assert env_selector_known_in("dev_no_runtime", env_table) is True

    def test_unknown_selector_is_not_known(self) -> None:
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12"]),
            "dev_no_runtime": _env(),
        }

        assert env_selector_known_in("nonexistent", env_table) is False

    def test_all_suffix_known_iff_base_is_matrix(self) -> None:
        matrix_table = _matrix_base("testing", ["3.11", "3.12"])

        assert env_selector_known_in("testing@all", matrix_table) is True
        assert env_selector_known_in("testing@all", {"testing": _env()}) is False
        assert env_selector_known_in("dev@all", {"dev": _env()}) is False


class TestInterpreterSelectorKnownIn:
    """`interpreter_selector_known_in` — pure predicate for `--interpreter` selectors."""

    def test_interpreter_declared_by_a_matrix_child_is_known(self) -> None:
        env_table = _matrix_base("testing", ["3.11", "3.12"])

        assert interpreter_selector_known_in("cpython@3.11", env_table) is True

    def test_version_only_shorthand_is_known(self) -> None:
        """A version-only selector (e.g. "3.11") is parsed via
        `parse_interpreter` the same way explicit `--interpreter` selectors
        are, so it resolves to the cpython shorthand."""
        env_table = _matrix_base("testing", ["3.11", "3.12"])

        assert interpreter_selector_known_in("3.11", env_table) is True

    def test_interpreter_not_declared_by_any_child_is_not_known(self) -> None:
        env_table = _matrix_base("testing", ["3.11", "3.12"])

        assert interpreter_selector_known_in("cpython@3.14", env_table) is False

    def test_malformed_interpreter_selector_is_not_known(self) -> None:
        """A selector `parse_interpreter` rejects outright (more than one
        "@") is treated as unknown rather than raising."""
        env_table = _matrix_base("testing", ["3.11", "3.12"])

        assert interpreter_selector_known_in("cpython@3.11@extra", env_table) is False

    def test_all_is_always_known(self) -> None:
        assert (
            interpreter_selector_known_in("all", _matrix_base("testing", ["3.11"]))
            is True
        )
        assert interpreter_selector_known_in("all", {"dev": _env()}) is True
