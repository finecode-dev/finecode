from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "data"
REEXPORTED = "action_meta_fixtures.SimpleAction"
WITH_PARENT = "action_meta_fixtures.with_parent.ChildAction"
BAD_SOURCE = "action_meta_fixtures.nope.Missing"


@pytest.fixture()
def fixtures_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    for name in [m for m in list(sys.modules) if m.startswith("action_meta_fixtures")]:
        del sys.modules[name]
    return FIXTURES_DIR


def _names(files: list[dict]) -> list[str]:
    return sorted(Path(item["path"]).name for item in files)


def test_dump_returns_versioned_document_with_stamps(
    tmp_path, fixtures_on_path
) -> None:
    """A dump answers with everything the cache needs to trust it later, not just the metadata."""
    from finecode_extension_runner import action_meta, services
    from finecode_extension_runner import context as er_context
    from finecode_extension_runner import domain as er_domain
    from finecode_extension_runner.di.registry import Registry

    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    (site_packages / "dummy.pth").write_text("import os\n", encoding="utf-8")
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    doc = action_meta.dump(
        [REEXPORTED, WITH_PARENT, BAD_SOURCE],
        project_dir=project_dir,
        site_packages=site_packages,
    )

    assert doc["format"] == 1
    assert isinstance(doc["startedNs"], int) and doc["startedNs"] > 0
    header = doc["header"]
    assert isinstance(header["sysPath"], list)
    assert header["pythonpath"] == os.environ.get("PYTHONPATH")
    assert header["sitePackages"]["path"] == str(site_packages)
    assert isinstance(header["sitePackages"]["dir_mtime_ns"], int)
    assert header["headerStable"] is True
    assert doc["actionMetaFile"]["path"].endswith("action_meta.py")

    for source in (REEXPORTED, WITH_PARENT):
        entry = doc["entries"][source]
        cls = action_meta._import_member(source)
        assert entry["meta"] == action_meta.action_meta(cls, project_dir)
        names = _names(entry["files"])
        assert "__init__.py" in names
        assert "code_action.py" in names
        for item in entry["files"]:
            assert {"path", "size", "mtime_ns", "sha256"} <= set(item)
        assert entry["dirs"]

    simple_names = _names(doc["entries"][REEXPORTED]["files"])
    assert "simple.py" in simple_names
    child_names = _names(doc["entries"][WITH_PARENT]["files"])
    assert "with_parent.py" in child_names
    assert "base.py" in child_names

    project = er_domain.Project(
        name="p",
        dir_path=project_dir,
        def_path=project_dir / "pyproject.toml",
        actions={
            "a": er_domain.ActionDeclaration(
                name="a", config={}, handlers=[], source=REEXPORTED
            ),
            "b": er_domain.ActionDeclaration(
                name="b", config={}, handlers=[], source=WITH_PARENT
            ),
        },
        action_handler_configs={},
    )
    runner_context = er_context.RunnerContext(project=project, di_registry=Registry())
    import asyncio

    resolved = asyncio.run(services.resolve_action_meta(runner_context))
    assert resolved["actions"][REEXPORTED] == doc["entries"][REEXPORTED]["meta"]
    assert resolved["actions"][WITH_PARENT] == doc["entries"][WITH_PARENT]["meta"]

    failure = doc["failures"][BAD_SOURCE]
    assert failure["error"]
    assert isinstance(failure["files"], list)


def test_dump_marks_header_unstable_when_a_pth_changes_at_import(
    tmp_path, fixtures_on_path
) -> None:
    """A .pth rewritten while the dump runs cannot be trusted, so the document says so."""
    from finecode_extension_runner import action_meta

    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    pth = site_packages / "late.pth"
    pth.write_text("x\n", encoding="utf-8")
    trigger = tmp_path / "trigger_mod.py"
    trigger.write_text(
        f'from pathlib import Path\nPath({str(pth)!r}).write_text("changed\\n", encoding="utf-8")\n'
        "from finecode_extension_api.code_action import Action\n"
        "class TriggerAction(Action):\n"
        '    LANGUAGE = "python"\n',
        encoding="utf-8",
    )
    import sys as _sys

    _sys.path.insert(0, str(tmp_path))
    try:
        doc = action_meta.dump(
            ["trigger_mod.TriggerAction"],
            project_dir=tmp_path,
            site_packages=site_packages,
        )
    finally:
        _sys.path.remove(str(tmp_path))
        _sys.modules.pop("trigger_mod", None)
    assert doc["header"]["headerStable"] is False


def test_dump_covers_named_constants_and_alias_reexports(fixtures_on_path) -> None:
    """Constants imported from another module still stamp that module, so edits there invalidate."""
    from finecode_extension_runner import action_meta

    project_dir = FIXTURES_DIR
    doc = action_meta.dump(
        [
            "action_meta_fixtures.alias_scope.AliasScopeAction",
            "action_meta_fixtures.non_literal.NonLiteralAction",
            "action_meta_fixtures.alias_parent.AliasParentAction",
        ],
        project_dir=project_dir,
    )
    assert "consts.py" in _names(
        doc["entries"]["action_meta_fixtures.alias_scope.AliasScopeAction"]["files"]
    )
    assert "consts.py" in _names(
        doc["entries"]["action_meta_fixtures.non_literal.NonLiteralAction"]["files"]
    )
    parent_files = _names(
        doc["entries"]["action_meta_fixtures.alias_parent.AliasParentAction"]["files"]
    )
    assert "reexport_mid.py" in parent_files
    assert "parent_mod.py" in parent_files


def test_dump_fails_closed_to_every_loaded_file(fixtures_on_path) -> None:
    """An unrecognised construct stamps everything, so the cache cannot go stale silently."""
    import action_meta_fixtures.simple  # noqa: F401 - ensures an unrelated file is loaded
    from finecode_extension_runner import action_meta

    project_dir = FIXTURES_DIR
    doc = action_meta.dump(
        [
            "action_meta_fixtures.call_rhs.CallRhsAction",
            "action_meta_fixtures.star_reexport.StarAction",
            "action_meta_fixtures.double_binding.DoubleAction",
            "action_meta_fixtures.try_binding.TryAction",
        ],
        project_dir=project_dir,
    )
    narrow = action_meta.dump(
        ["action_meta_fixtures.simple.SimpleAction"],
        project_dir=project_dir,
    )["entries"]["action_meta_fixtures.simple.SimpleAction"]["files"]
    narrow_names = set(_names(narrow))
    for source in (
        "action_meta_fixtures.call_rhs.CallRhsAction",
        "action_meta_fixtures.star_reexport.StarAction",
        "action_meta_fixtures.double_binding.DoubleAction",
        "action_meta_fixtures.try_binding.TryAction",
    ):
        names = _names(doc["entries"][source]["files"])
        assert "simple.py" in names
        assert len(names) > len(narrow_names)


def test_dump_plain_literal_action_stays_narrow(fixtures_on_path) -> None:
    """A plain literal action stamps only its own chain, not the whole environment."""
    import action_meta_fixtures.call_rhs  # noqa: F401 - loaded but must stay out of the narrow set
    from finecode_extension_runner import action_meta

    doc = action_meta.dump(
        ["action_meta_fixtures.simple.SimpleAction"],
        project_dir=FIXTURES_DIR,
    )
    names = _names(doc["entries"]["action_meta_fixtures.simple.SimpleAction"]["files"])
    assert "simple.py" in names
    assert "call_rhs.py" not in names


def test_dump_subcommand_is_lean_and_print_safe(tmp_path) -> None:
    """Import-time prints cannot corrupt the document, and the dump stays out of the server stack."""
    script = (
        "import sys\n"
        "from finecode_extension_runner import cli\n"
        f"cli.main(['dump-action-meta', '--project-path={tmp_path}'], standalone_mode=False)\n"
        "print('DUMP_RETURNED')\n"
        "print('LOGURU_ABSENT' if 'loguru' not in sys.modules else 'LOGURU_PRESENT')\n"
        "print('ERSERVER_ABSENT' if 'finecode_extension_runner.er_server' not in sys.modules else 'ERSERVER_PRESENT')\n"
    )
    payload = json.dumps({"sources": ["action_meta_fixtures.noisy.NoisyAction"]})
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(FIXTURES_DIR),
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["format"] == 1
    assert "action_meta_fixtures.noisy.NoisyAction" in document["entries"]
    assert "noisy stdout at import" in completed.stderr
    assert "noisy fd1 at import" in completed.stderr
    assert "DUMP_RETURNED" in completed.stderr
    assert "LOGURU_ABSENT" in completed.stderr
    assert "ERSERVER_ABSENT" in completed.stderr
    assert "DUMP_RETURNED" not in completed.stdout

    for product in ("cli.py", "action_meta.py"):
        text = (
            Path(__file__).parent.parent / "src" / "finecode_extension_runner" / product
        ).read_text(encoding="utf-8")
        assert "DUMP_RETURNED" not in text


def test_cli_version_and_start_help_regression() -> None:
    """The lean-import move keeps the existing commands working."""
    from click.testing import CliRunner

    from finecode_extension_runner import cli

    runner = CliRunner()
    version = runner.invoke(cli.main, ["version"])
    assert version.exit_code == 0
    assert "FineCode Extension Runner " in version.output
    help_result = runner.invoke(cli.main, ["start", "--help"])
    assert help_result.exit_code == 0
    assert "--project-path" in help_result.output
    assert "--env-name" in help_result.output
