import pytest

from finecode.wm_server.config.env_selection import (
    EnvSelection,
    EnvSelectionError,
    compute_create_set,
    compute_install_set,
    env_selector_known_in,
    interpreter_selector_known_in,
    resolve_env_selection,
    resolve_selected_interpreters,
)


def _env(interpreter: str | None = None, default_interpreters: dict | None = None) -> dict:
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


class TestNoSelectors:
    def test_no_selectors_and_no_default_selects_all_and_is_inactive(self) -> None:
        """With nothing constraining anything, every env is selected and the
        selection is inactive — today's full-axis, create-all behaviour
        (PRD-0003-R7)."""
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12", "3.13"]),
            "dev_no_runtime": _env(),
        }

        selection = resolve_env_selection(env_table, [], [], "cli")

        assert selection.active is False
        assert selection.selected_env_names == set(env_table.keys())


class TestEnvSelector:
    def test_env_equal_to_base_name_selects_all_its_children(self) -> None:
        """`--env=<base>` selects every concrete child of that matrix base."""
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12", "3.13"]),
            "dev_no_runtime": _env(),
        }

        selection = resolve_env_selection(env_table, ["testing"], [], "cli")

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

        selection = resolve_env_selection(
            env_table, ["testing@cpython-3.11"], [], "cli"
        )

        assert selection.selected_env_names == {"testing@cpython-3.11"}

    def test_non_matrix_env_name_passes_through(self) -> None:
        """`--env=<non-matrix-name>`: only the
        named non-matrix env is selected."""
        env_table = {
            "dev_no_runtime": _env(),
            "docs": _env(),
        }

        selection = resolve_env_selection(env_table, ["dev_no_runtime"], [], "cli")

        assert selection.active is True
        assert selection.selected_env_names == {"dev_no_runtime"}

    def test_unknown_env_selector_selects_nothing_and_does_not_raise(self) -> None:
        """An `--env` value that matches nothing in this project's table selects
        nothing here — cross-project validation happens at the service layer,
        not in the pure resolver."""
        env_table = {"dev_no_runtime": _env()}

        selection = resolve_env_selection(env_table, ["nonexistent"], [], "cli")

        assert selection.selected_env_names == set()

    def test_named_base_overrides_its_default_to_all_children_while_unnamed_base_keeps_its_default(
        self,
    ) -> None:
        """`--env=<base>` overrides that base's own default_interpreters policy
        to all children; a sibling matrix base not named by `--env` keeps its
        own config-default subset (not excluded, not widened)."""
        env_table = {
            **_matrix_base(
                "testing", ["3.11", "3.12", "3.13"], {"local": "newest"}
            ),
            **_matrix_base("stubs", ["3.11", "3.12"], {"local": "oldest"}),
        }

        selection = resolve_env_selection(env_table, ["testing"], [], "cli")

        assert selection.selected_env_names == {
            "testing@cpython-3.11",
            "testing@cpython-3.12",
            "testing@cpython-3.13",
            "stubs@cpython-3.11",  # stubs' own "oldest" default, kept
        }


class TestInterpreterSelector:
    def test_interpreter_shorthand_selects_matching_children_across_bases(self) -> None:
        """`--interpreter=3.11` (cpython shorthand) selects the cpython@3.11
        child of every matrix base that declares it, and unions in every
        non-matrix env (no `--env` given)."""
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12"]),
            **_matrix_base("stubs", ["3.11", "3.12"]),
            "dev_no_runtime": _env(),
        }

        selection = resolve_env_selection(env_table, [], ["3.11"], "cli")

        assert selection.selected_env_names == {
            "testing@cpython-3.11",
            "stubs@cpython-3.11",
            "dev_no_runtime",
        }

    def test_explicit_interpreter_overrides_config_default(self) -> None:
        """An explicit `--interpreter` selector wins outright over the base's
        config-declared default_interpreters policy."""
        env_table = _matrix_base(
            "testing", ["3.11", "3.12", "3.13"], {"local": "newest"}
        )

        selection = resolve_env_selection(env_table, [], ["3.11"], "cli")

        assert selection.selected_env_names == {"testing@cpython-3.11"}

    def test_both_env_and_interpreter_selectors_intersect(self) -> None:
        """Both axes constrained: the result is the intersection of the
        `--env`-selected children and the `--interpreter`-effective set."""
        env_table = _matrix_base("testing", ["3.11", "3.12", "3.13"])

        selection = resolve_env_selection(env_table, ["testing"], ["3.12"], "cli")

        assert selection.selected_env_names == {"testing@cpython-3.12"}


class TestConfigDefault:
    def test_default_newest_selects_the_max_version_child(self) -> None:
        env_table = _matrix_base(
            "testing", ["3.11", "3.12", "3.13"], {"local": "newest", "ci": "all"}
        )

        selection = resolve_env_selection(env_table, [], [], "cli")

        assert selection.active is True
        assert selection.selected_env_names == {"testing@cpython-3.13"}

    def test_default_all_for_ci_dev_env_selects_everything_and_is_inactive(self) -> None:
        env_table = _matrix_base(
            "testing", ["3.11", "3.12", "3.13"], {"local": "newest", "ci": "all"}
        )

        selection = resolve_env_selection(env_table, [], [], "ci")

        assert selection.active is False
        assert selection.selected_env_names == set(env_table.keys())

    def test_default_oldest_selects_the_min_version_child(self) -> None:
        env_table = _matrix_base(
            "testing", ["3.11", "3.12", "3.13"], {"local": "oldest"}
        )

        selection = resolve_env_selection(env_table, [], [], "cli")

        assert selection.selected_env_names == {"testing@cpython-3.11"}

    def test_default_explicit_list_selects_named_interpreters(self) -> None:
        env_table = _matrix_base(
            "testing",
            ["3.11", "3.12", "3.13"],
            {"local": ["cpython@3.11", "cpython@3.13"]},
        )

        selection = resolve_env_selection(env_table, [], [], "cli")

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

        selection = resolve_env_selection(env_table, [], [], "cli")

        assert selection.selected_env_names == {"testing@cpython-3.11"}

    def test_absent_default_selects_all(self) -> None:
        env_table = _matrix_base("testing", ["3.11", "3.12"])

        selection = resolve_env_selection(env_table, [], [], "cli")

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

        selection = resolve_env_selection(env_table, [], [], "cli")

        assert selection.selected_env_names == {
            "testing@cpython-3.12",
            "testing@pypy-3.12",
        }

    def test_default_explicit_list_naming_interpreter_outside_axis_raises(self) -> None:
        env_table = _matrix_base(
            "testing", ["3.11", "3.12"], {"local": ["cpython@3.14"]}
        )

        with pytest.raises(EnvSelectionError):
            resolve_env_selection(env_table, [], [], "cli")


class TestMatrixChildNames:
    def test_matrix_child_names_includes_all_children_regardless_of_selection(self) -> None:
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12"]),
            "dev_no_runtime": _env(),
        }

        selection = resolve_env_selection(env_table, ["testing@cpython-3.11"], [], "cli")

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

        assert compute_create_set(selection, all_names) == all_names
        assert compute_install_set(selection, all_names) == all_names

    def test_active_selection_excludes_unselected_matrix_children_from_create_but_keeps_non_matrix(
        self,
    ) -> None:
        """An unselected matrix child is skipped in the create set (AC8); a
        non-matrix env is always in the create set regardless of selection."""
        all_names = {"testing@cpython-3.11", "testing@cpython-3.12", "dev"}
        selection = EnvSelection(
            active=True,
            selected_env_names={"testing@cpython-3.11"},
            matrix_child_names={"testing@cpython-3.11", "testing@cpython-3.12"},
        )

        create_set = compute_create_set(selection, all_names)
        install_set = compute_install_set(selection, all_names)

        assert create_set == {"testing@cpython-3.11", "dev"}
        assert "testing@cpython-3.12" not in create_set
        assert install_set == {"testing@cpython-3.11"}


class TestEnvSelectorKnownIn:
    """`env_selector_known_in` — pure predicate used for cross-project
    validation by both `prepare_envs_service` and the run entry points'
    `run_selection.validate_run_selectors` (PRD-0003 AC8)."""

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


class TestInterpreterSelectorKnownIn:
    """`interpreter_selector_known_in` — pure predicate mirroring
    `env_selector_known_in` for `--interpreter` selectors."""

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


class TestResolveSelectedInterpreters:
    """`resolve_selected_interpreters` — selectors -> a set of interpreter
    canonicals for the run fan-out sites (PRD-0003 AC8)."""

    def test_no_selectors_and_no_default_returns_none(self) -> None:
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12", "3.13"]),
            "dev_no_runtime": _env(),
        }

        result = resolve_selected_interpreters(env_table, [], [], "cli")

        assert result is None

    def test_interpreter_selector_returns_matching_canonical(self) -> None:
        env_table = _matrix_base("testing", ["3.11", "3.12", "3.13"])

        result = resolve_selected_interpreters(env_table, [], ["3.11"], "cli")

        assert result == {"cpython@3.11"}

    def test_config_default_newest_returns_max_version_canonical(self) -> None:
        env_table = _matrix_base(
            "testing", ["3.11", "3.12", "3.13"], {"cli": "newest"}
        )

        result = resolve_selected_interpreters(env_table, [], [], "cli")

        assert result == {"cpython@3.13"}

    def test_env_selector_naming_one_child_returns_that_one_canonical(self) -> None:
        env_table = _matrix_base("testing", ["3.11", "3.12", "3.13"])

        result = resolve_selected_interpreters(
            env_table, ["testing@cpython-3.11"], [], "cli"
        )

        assert result == {"cpython@3.11"}

    def test_selected_non_matrix_env_is_not_included(self) -> None:
        env_table = {
            **_matrix_base("testing", ["3.11", "3.12"]),
            "dev_no_runtime": _env(),
        }

        result = resolve_selected_interpreters(
            env_table, ["testing@cpython-3.11", "dev_no_runtime"], [], "cli"
        )

        assert result == {"cpython@3.11"}

    def test_config_default_naming_interpreter_outside_axis_raises(self) -> None:
        """A `default_interpreters` policy (not an explicit `--interpreter`
        selector — those simply contribute nothing outside a base's axis, per
        `resolve_env_selection`) naming an interpreter outside the declared
        axis is rejected, propagating through the wrapper."""
        env_table = _matrix_base(
            "testing", ["3.11", "3.12"], {"cli": ["cpython@3.14"]}
        )

        with pytest.raises(EnvSelectionError):
            resolve_selected_interpreters(env_table, [], [], "cli")
