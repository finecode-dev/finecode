import pytest

from finecode.wm_server.config.interpreter_matrix import (
    ConcreteEnv,
    EnvSpec,
    ExpansionResult,
    HandlerRef,
    Interpreter,
    InvalidInterpreterError,
    MatrixSetMismatchError,
    MixedMatrixError,
    expand,
    parse_interpreter,
    validate,
)


class TestParseInterpreter:
    @pytest.mark.parametrize(
        "value,expected_implementation,expected_version",
        [
            ("cpython@3.11", "cpython", "3.11"),
            ("pypy@3.11", "pypy", "3.11"),
            ("CPython@3.11", "cpython", "3.11"),
            ("3.11", "cpython", "3.11"),
            ("3.12.1", "cpython", "3.12.1"),
            ("  pypy@3.10  ", "pypy", "3.10"),
        ],
    )
    def test_parses_recognized_interpreter_string_formats(
        self, value: str, expected_implementation: str, expected_version: str
    ) -> None:
        """A user's interpreter string in either "impl@version" or bare-version form resolves to the exact interpreter they specified, so their matrix environment expands against the interpreter they actually asked for."""
        result = parse_interpreter(value)
        assert result.implementation == expected_implementation
        assert result.version == expected_version

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "cpython@",
            "@3.11",
            "cpython@3.11@x",
            "cpython@ ",
        ],
    )
    def test_rejects_malformed_interpreter_string(self, value: str) -> None:
        """A malformed interpreter string in config is rejected at parse time with a clear error, instead of silently producing a bogus or empty interpreter that would only surface as a confusing venv failure later."""
        with pytest.raises(InvalidInterpreterError):
            parse_interpreter(value)

    @pytest.mark.parametrize(
        "implementation,version,expected_canonical,expected_env_suffix",
        [
            ("cpython", "3.11", "cpython@3.11", "cpython-3.11"),
            ("pypy", "3.11", "pypy@3.11", "pypy-3.11"),
        ],
    )
    def test_canonical_and_env_suffix_representations(
        self,
        implementation: str,
        version: str,
        expected_canonical: str,
        expected_env_suffix: str,
    ) -> None:
        """The canonical and env-suffix forms of an interpreter are stable, predictable strings a user can recognize both in diagnostics and in generated venv names."""
        interpreter = Interpreter(implementation, version)
        assert interpreter.canonical == expected_canonical
        assert interpreter.env_suffix == expected_env_suffix

    def test_interpreters_with_same_implementation_and_version_are_equal(self) -> None:
        """Two interpreter values parsed from equivalent config entries compare equal, so config diffing and deduplication treat them as the same interpreter rather than spuriously different ones."""
        assert Interpreter("cpython", "3.11") == Interpreter("cpython", "3.11")

    def test_interpreters_with_same_implementation_and_version_hash_equal(self) -> None:
        """Equal interpreter values hash equal, which is required for them to behave correctly as dict keys or set members when the matrix logic deduplicates or indexes by interpreter."""
        assert hash(Interpreter("cpython", "3.11")) == hash(Interpreter("cpython", "3.11"))

    def test_interpreters_with_different_implementation_are_not_equal(self) -> None:
        """Interpreters that differ only by implementation are treated as genuinely distinct, so a CPython and PyPy entry are never silently merged into one matrix slot."""
        assert Interpreter("cpython", "3.11") != Interpreter("pypy", "3.11")

    def test_interpreter_usable_as_dict_key(self) -> None:
        """Interpreter values can key a dict by value rather than identity, which the matrix layer relies on to look up per-interpreter data without keeping the original instance around."""
        mapping = {Interpreter("cpython", "3.11"): "value"}
        assert mapping[Interpreter("cpython", "3.11")] == "value"

    def test_interpreter_usable_as_set_member_and_dedups_by_value(self) -> None:
        """Interpreter values collapse to a single set member when value-equal, which matters because the matrix validator compares interpreter sets."""
        members = {Interpreter("cpython", "3.11"), Interpreter("cpython", "3.11")}
        assert len(members) == 1


class TestExpand:
    def test_worked_example_expands_matrix_environment_and_rewrites_its_handlers(self) -> None:
        """A matrix environment declared once in config becomes one concrete venv per interpreter, and every handler that targeted it is rewritten to target each concrete venv — this is the mechanism that lets a single action run under multiple interpreters without hand-duplicated config."""
        envs = [
            EnvSpec(
                "testing",
                [
                    Interpreter("cpython", "3.11"),
                    Interpreter("cpython", "3.12"),
                    Interpreter("pypy", "3.11"),
                ],
            ),
            EnvSpec("lint"),
        ]
        handlers = [
            HandlerRef("run_tests", "pytest", "testing"),
            HandlerRef("lint", "ruff", "lint"),
        ]

        result = expand(envs, handlers)

        assert result == ExpansionResult(
            concrete_envs=[
                ConcreteEnv("testing@cpython-3.11", Interpreter("cpython", "3.11")),
                ConcreteEnv("testing@cpython-3.12", Interpreter("cpython", "3.12")),
                ConcreteEnv("testing@pypy-3.11", Interpreter("pypy", "3.11")),
                ConcreteEnv("lint", None),
            ],
            handlers=[
                HandlerRef("run_tests", "pytest", "testing@cpython-3.11"),
                HandlerRef("run_tests", "pytest", "testing@cpython-3.12"),
                HandlerRef("run_tests", "pytest", "testing@pypy-3.11"),
                HandlerRef("lint", "ruff", "lint"),
            ],
            matrix_environments={
                "testing": [
                    Interpreter("cpython", "3.11"),
                    Interpreter("cpython", "3.12"),
                    Interpreter("pypy", "3.11"),
                ]
            },
        )

    def test_project_with_no_matrix_environments_expands_as_a_passthrough(self) -> None:
        """A project that declares no interpreter matrices at all gets its envs and handlers back unchanged, so adopting this feature has zero effect on projects that never opt into it."""
        envs = [EnvSpec("lint")]
        handlers = [HandlerRef("lint", "ruff", "lint")]

        result = expand(envs, handlers)

        assert result.concrete_envs == [ConcreteEnv("lint", None)]
        assert result.handlers == [HandlerRef("lint", "ruff", "lint")]
        assert result.matrix_environments == {}

    def test_matrix_environment_with_single_interpreter_expands_to_one_concrete_env(self) -> None:
        """A matrix environment that happens to declare only one interpreter still expands through the matrix machinery rather than being special-cased, keeping the config format and its expansion consistent regardless of axis size."""
        envs = [EnvSpec("solo", [Interpreter("cpython", "3.11")])]
        handlers = [HandlerRef("build", "build_wheel", "solo")]

        result = expand(envs, handlers)

        assert result.concrete_envs == [
            ConcreteEnv("solo@cpython-3.11", Interpreter("cpython", "3.11"))
        ]
        assert result.handlers == [
            HandlerRef("build", "build_wheel", "solo@cpython-3.11")
        ]
        assert result.matrix_environments == {"solo": [Interpreter("cpython", "3.11")]}


class TestValidate:
    def test_action_referencing_only_single_envs_is_classified_single(self) -> None:
        """An action whose handlers only ever target single-interpreter envs is classified as an ordinary, non-matrixed action, so its downstream execution behaves exactly as before this feature existed."""
        envs = [EnvSpec("lint")]
        handlers = [HandlerRef("lint", "ruff", "lint")]

        assert validate(envs, handlers) == {"lint": "single"}

    def test_action_referencing_one_matrix_environment_is_classified_matrixed(self) -> None:
        """An action whose handler targets a matrix environment is classified as matrixed even with just one matrix environment involved, signalling downstream that this action must fan out across its interpreter axis."""
        envs = [
            EnvSpec(
                "testing",
                [Interpreter("cpython", "3.11"), Interpreter("cpython", "3.12")],
            )
        ]
        handlers = [HandlerRef("run_tests", "pytest", "testing")]

        assert validate(envs, handlers) == {"run_tests": "matrixed"}

    def test_action_spanning_two_matrix_environments_with_equal_interpreter_sets_is_matrixed(
        self,
    ) -> None:
        """An action whose handlers span two different matrix environments is accepted as matrixed as long as both cover the exact same interpreters, since only then can each interpreter's handlers be paired up consistently at run time."""
        axis = [Interpreter("cpython", "3.11"), Interpreter("cpython", "3.12")]
        envs = [EnvSpec("testing", axis), EnvSpec("stubs", list(axis))]
        handlers = [
            HandlerRef("type_check", "pyrefly", "testing"),
            HandlerRef("type_check", "stub_check", "stubs"),
        ]

        assert validate(envs, handlers) == {"type_check": "matrixed"}

    def test_action_spanning_matrix_environments_with_a_subset_mismatch_is_rejected(self) -> None:
        """An action whose matrix environments cover different interpreter sets is rejected at config-validation time, so a project misconfiguration surfaces immediately instead of causing an interpreter's handler to silently never run."""
        envs = [
            EnvSpec(
                "testing",
                [
                    Interpreter("cpython", "3.11"),
                    Interpreter("cpython", "3.12"),
                    Interpreter("pypy", "3.11"),
                ],
            ),
            EnvSpec(
                "stubs",
                [Interpreter("cpython", "3.11"), Interpreter("cpython", "3.12")],
            ),
        ]
        handlers = [
            HandlerRef("type_check", "pyrefly", "testing"),
            HandlerRef("type_check", "stub_check", "stubs"),
        ]

        with pytest.raises(MatrixSetMismatchError):
            validate(envs, handlers)

    def test_action_spanning_matrix_environments_with_a_superset_mismatch_is_rejected(self) -> None:
        """A matrix environment that covers extra interpreters beyond what a sibling matrix environment in the same matrixed action declares is just as invalid as a missing one — validation must not treat "more interpreters" as automatically compatible."""
        envs = [
            EnvSpec("testing", [Interpreter("cpython", "3.11")]),
            EnvSpec(
                "stubs",
                [Interpreter("cpython", "3.11"), Interpreter("pypy", "3.11")],
            ),
        ]
        handlers = [
            HandlerRef("t", "a", "testing"),
            HandlerRef("t", "b", "stubs"),
        ]

        with pytest.raises(MatrixSetMismatchError):
            validate(envs, handlers)

    def test_action_mixing_matrix_and_single_envs_is_rejected(self) -> None:
        """An action that mixes a matrixed handler with a plain single-env handler is rejected outright, because there is no well-defined way to run one handler once and another once per interpreter within the same action."""
        envs = [
            EnvSpec(
                "build",
                [Interpreter("cpython", "3.11"), Interpreter("cpython", "3.12")],
            ),
            EnvSpec("index"),
        ]
        handlers = [
            HandlerRef("build_artifact", "build_wheel", "build"),
            HandlerRef("build_artifact", "update_index", "index"),
        ]

        with pytest.raises(MixedMatrixError):
            validate(envs, handlers)

    def test_action_with_no_handlers_is_omitted_from_the_result(self) -> None:
        """An action name that has no handlers at all does not appear in the classification map, so callers can safely iterate the result without encountering a meaningless "single" or "matrixed" label for something that never runs."""
        envs = [EnvSpec("lint")]
        handlers = [HandlerRef("lint", "ruff", "lint")]

        result = validate(envs, handlers)

        assert "nonexistent_action" not in result
        assert result == {"lint": "single"}
