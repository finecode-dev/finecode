import types
import typing

from fine_envs.dump_config_handler import DumpConfigHandler
from fine_envs.dump_config_render import attribution_comment, render_config_dump


class _FakeProjectInfoProvider:
    def __init__(self, selection: dict[str, list[str]]) -> None:
        self._selection = selection

    async def get_workspace_extra_selection(self) -> dict[str, list[str]]:
        return self._selection


async def test_handler_strips_resolved_keys_and_renders_the_dump() -> None:
    """Keys config resolution already consumed are gone from the rendered
    dump, and the attribution block comes from the provider's selection."""
    selection = {"finecode-dev-common-preset": ["lint_fix"]}
    raw_config = {
        "dependency-groups": {
            "runtime": ["finecode_dev_common_preset[lint_fix]~=0.3.0a0"]
        },
        "tool": {"finecode": {"presets": [{"source": "x"}], "extra": {}}},
    }
    handler = DumpConfigHandler(
        project_info_provider=typing.cast(
            typing.Any, _FakeProjectInfoProvider(selection)
        )
    )
    run_context = types.SimpleNamespace(
        raw_config_dump=raw_config, config_dump_content=None
    )

    await handler.run(typing.cast(typing.Any, None), run_context)

    content = run_context.config_dump_content
    assert content == render_config_dump(raw_config, selection)
    assert content.startswith("# Dependency specs rewritten by")
    assert "presets" not in content
    assert "extra" not in raw_config["tool"]["finecode"]


def test_attribution_comment_names_selection_file_and_extra() -> None:
    """A rewritten spec is attributed to the selection file and its extra."""
    selection = {"finecode-dev-common-preset": ["lint_fix"]}
    raw_config = {
        "dependency-groups": {
            "runtime": ["finecode_dev_common_preset[lint_fix]~=0.3.0a0"]
        }
    }

    comment = attribution_comment(selection, raw_config)

    assert "finecode-workspace-user.toml" in comment
    assert "lint_fix" in comment
    assert "finecode_dev_common_preset[lint_fix]~=0.3.0a0" in comment


def test_attribution_comment_empty_selection_is_empty() -> None:
    raw_config = {
        "dependency-groups": {
            "runtime": ["finecode_dev_common_preset[lint_fix]~=0.3.0a0"]
        }
    }

    assert attribution_comment({}, raw_config) == ""


def test_attribution_comment_no_matching_spec_is_empty() -> None:
    selection = {"finecode-dev-common-preset": ["lint_fix"]}
    raw_config = {"dependency-groups": {"runtime": ["other~=1.0"]}}

    assert attribution_comment(selection, raw_config) == ""
