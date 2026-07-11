from finecode_extension_runner.impls.workspace_action_registry import (
    ActionInfo,
    HandlerInfo,
    parse_workspace_actions,
)


def test_parse_workspace_actions_maps_camel_case_wire_fields_to_snake_case_dataclasses() -> None:
    """Every camelCase field on the wire (including nested handler fields) must survive the parse into its snake_case counterpart, or which_handlers silently loses data for callers."""
    payload = {
        "actions": [
            {
                "name": "LintFilesAction",
                "source": "fine_lint.LintFilesAction",
                "canonicalSource": "fine_lint.lint_files_action.LintFilesAction",
                "scope": "project",
                "project": "my_project",
                "language": None,
                "parentActionSource": None,
                "fileLoc": "fine_lint/lint_files_action.py:10",
                "handlers": [
                    {
                        "name": "LintFilesHandler",
                        "source": "fine_lint.lint_files_handler.LintFilesHandler",
                        "env": "default",
                        "fileLoc": "fine_lint/lint_files_handler.py:20",
                    }
                ],
            },
            {
                "name": "LintPythonFilesAction",
                "source": "fine_python_lint.LintPythonFilesAction",
                "canonicalSource": None,
                "scope": "project",
                "project": "my_project",
                "language": "python",
                "parentActionSource": "fine_lint.LintFilesAction",
                "fileLoc": None,
            },
        ]
    }

    result = parse_workspace_actions(payload)

    expected = [
        ActionInfo(
            name="LintFilesAction",
            source="fine_lint.LintFilesAction",
            canonical_source="fine_lint.lint_files_action.LintFilesAction",
            scope="project",
            project="my_project",
            language=None,
            parent_action_source=None,
            file_loc="fine_lint/lint_files_action.py:10",
            handlers=[
                HandlerInfo(
                    name="LintFilesHandler",
                    source="fine_lint.lint_files_handler.LintFilesHandler",
                    env="default",
                    file_loc="fine_lint/lint_files_handler.py:20",
                )
            ],
        ),
        ActionInfo(
            name="LintPythonFilesAction",
            source="fine_python_lint.LintPythonFilesAction",
            canonical_source=None,
            scope="project",
            project="my_project",
            language="python",
            parent_action_source="fine_lint.LintFilesAction",
            file_loc=None,
            handlers=[],
        ),
    ]
    assert result == expected


def test_parse_workspace_actions_defaults_explicit_empty_handlers_list_to_empty_list() -> None:
    """An action with an explicit empty handlers array parses to handlers == [], not None or an error."""
    payload = {
        "actions": [
            {
                "name": "NoopAction",
                "source": "fine_noop.NoopAction",
                "canonicalSource": "fine_noop.noop_action.NoopAction",
                "scope": "workspace",
                "project": "my_project",
                "language": None,
                "parentActionSource": None,
                "fileLoc": None,
                "handlers": [],
            }
        ]
    }

    result = parse_workspace_actions(payload)

    expected = [
        ActionInfo(
            name="NoopAction",
            source="fine_noop.NoopAction",
            canonical_source="fine_noop.noop_action.NoopAction",
            scope="workspace",
            project="my_project",
            language=None,
            parent_action_source=None,
            file_loc=None,
            handlers=[],
        )
    ]
    assert result == expected
