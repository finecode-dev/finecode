"""Blocks a shell — or a shell-shaped command string — from re-entering
FineCode's subprocess spawns.

`ICommandRunner` executes argv vectors, never a shell command line (ADR-0099):
quoting conventions are per-shell and per-platform, so a command built by
joining strings can be misparsed by a shell the builder never accounted for
(cmd.exe on Windows was the observed failure). These gates make the old shape a
CI failure rather than a review comment, so the regression is blocked even when
the reviewer does not notice it:

- **G1** — no call to `create_subprocess_shell`, `os.system`, `os.popen`, or
  any call with `shell=True`, anywhere.
- **G2** — no `shlex.join`/`shlex.quote`/`shlex.split` in the packages whose
  handlers drive `ICommandRunner`: quoting is never the caller's problem there.
- **G3** — no `.run(...)`/`.run_sync(...)` on a `command_runner` receiver whose
  command argument is a string literal, f-string, `+` concatenation, or
  `.join(...)` result. G3 is a *syntactic* backstop: it catches inline string
  commands but not a string bound to a variable first, nor a runner under
  another name. Those are caught by `check_argv` — in the real runner on every
  OS and in every fake — which is to say by the first test or real run that
  reaches the call.
- **G4** — no direct `subprocess.*`/`asyncio.create_subprocess_exec` spawn in
  `extensions/`/`presets/` outside the allowlist below: process spawns there
  are supposed to go through `ICommandRunner` so the ER's process-slot gate,
  teardown semantics and argv handling apply uniformly.

The guards scan tracked `.py` files from `git ls-files`, so they cannot be
silently bypassed by an untracked local file, and they skip when `.git` is
absent (sdist installs, vendored checkouts) where "is this tracked?" has no
answer.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
from collections.abc import Iterable

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not (_REPO_ROOT / ".git").exists(),
    reason="needs a git checkout to tell tracked files from local-only ones",
)

_COMMAND_RUNNER_SUFFIX = "command_runner"

_G1_GATE_CALLS = frozenset(
    {
        "create_subprocess_shell",
        "asyncio.create_subprocess_shell",
        "os.system",
        "os.popen",
    }
)
_G1_SHELL_KEYWORD = "shell"

_G2_SHLEX_CALLS = frozenset({"shlex.join", "shlex.quote", "shlex.split"})
_G2_SCOPE_PREFIXES = (
    "extensions/",
    "presets/",
    "finecode_extension_runner/",
    "finecode_extension_api/",
)

_G4_SUBPROCESS_CALLS = frozenset(
    {
        "asyncio.create_subprocess_exec",
        "subprocess.Popen",
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
    }
)
_G4_SCOPE_PREFIXES = ("extensions/", "presets/")

# Direct process spawns in extensions/presets that bypass `ICommandRunner`,
# each with the reason it may.
_G4_ALLOWLIST: dict[str, str] = {
    "extensions/fine_docs_mkdocs/fine_docs_mkdocs/serve_docs_handler.py": (
        "serves a long-lived dev server; routing it through ICommandRunner would "
        "hold an ADR-0090 work slot for the server's whole lifetime, and the spawn "
        "is already argv/exec with an absolute binary path (no shell, no quoting)"
    ),
}


def _dotted_name(node: ast.expr | None) -> str | None:
    """`a.b.c` from Attribute chains and bare names."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _g1_violations(tree: ast.AST) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted_name(node.func)
        if name in _G1_GATE_CALLS:
            violations.append(f"{name}: spawned through a shell")
        for keyword in node.keywords:
            if (
                keyword.arg == _G1_SHELL_KEYWORD
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                violations.append(f"{name or '<call>'}: shell=True")
    return violations


def _g2_violations(tree: ast.AST, rel_path: str) -> list[str]:
    if not any(rel_path.startswith(prefix) for prefix in _G2_SCOPE_PREFIXES):
        return []
    return [
        f"{_dotted_name(node.func)}: quoting is never the caller's problem"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _dotted_name(node.func) in _G2_SHLEX_CALLS
    ]


def _is_string_command_arg(node: ast.expr) -> bool:
    """A command argument the runner would treat as a shell string: a literal,
    f-string, `+` concatenation, or `" ".join(...)` result."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return True
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
    ):
        return True
    return False


def _g3_violations(tree: ast.AST) -> list[str]:
    """String commands passed to a `command_runner` receiver, anywhere.

    The receiver must end in `command_runner` (`self.command_runner.run(...)` or
    a parameter named `command_runner`), so an unrelated `.run` on another
    object is not flagged.
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("run", "run_sync"):
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Attribute):
            receiver_is_runner = receiver.attr == _COMMAND_RUNNER_SUFFIX
        elif isinstance(receiver, ast.Name):
            receiver_is_runner = receiver.id == _COMMAND_RUNNER_SUFFIX
        else:
            receiver_is_runner = False
        if not receiver_is_runner:
            continue

        command_arg: ast.expr | None = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg == "cmd":
                command_arg = keyword.value
        if command_arg is not None and _is_string_command_arg(command_arg):
            violations.append(
                f"{_dotted_name(node.func)}: command argument is a string; pass argv"
            )
    return violations


def _g4_violations(tree: ast.AST, rel_path: str) -> list[str]:
    if not any(rel_path.startswith(prefix) for prefix in _G4_SCOPE_PREFIXES):
        return []
    if "/tests/" in rel_path:
        return []
    if rel_path in _G4_ALLOWLIST:
        return []
    return [
        f"{_dotted_name(node.func)}: direct process spawn outside ICommandRunner"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _dotted_name(node.func) in _G4_SUBPROCESS_CALLS
    ]


def _violations(source: str, rel_path: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ["unparsable source"]
    return [
        *_g1_violations(tree),
        *_g2_violations(tree, rel_path),
        *_g3_violations(tree),
        *_g4_violations(tree, rel_path),
    ]


def _tracked_py_files() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [path for path in output.split("\0") if path.endswith(".py")]


def test_no_shell_command_strings_in_tracked_python() -> None:
    """Every tracked `.py` file passes G1–G4, so a shell command string or a
    bypassed `ICommandRunner` is a CI failure, not a review comment."""
    violations: dict[str, list[str]] = {}
    for rel_path in _tracked_py_files():
        path = _REPO_ROOT / rel_path
        # tracked but deleted in the working tree (`rm` before `git rm`)
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        found = _violations(source, rel_path)
        if found:
            violations[rel_path] = found

    assert not violations, (
        "shell spawning or shell-shaped command strings were introduced:\n"
        + "\n".join(
            f"{path}:\n  " + "\n  ".join(items)
            for path, items in sorted(violations.items())
        )
        + "\n\nCommands are executed without a shell: pass argv lists to "
        "ICommandRunner.run/run_sync and never quote or join arguments (ADR-0099)."
    )


def test_g1_flags_shell_spawns_in_embedded_snippets() -> None:
    """The planted-violation fixtures: G1 flags every shell entry point."""
    for snippet in (
        "asyncio.create_subprocess_shell('echo hi')\n",
        "os.system('echo hi')\n",
        "os.popen('echo hi')\n",
        "subprocess.run('echo hi', shell=True)\n",
    ):
        assert _g1_violations(ast.parse(snippet)), snippet


def test_g2_flags_shlex_calls_in_embedded_snippets() -> None:
    for snippet in (
        "cmd = shlex.join(parts)\n",
        "cmd = shlex.quote(arg)\n",
        "parts = shlex.split(cmd)\n",
    ):
        assert _g2_violations(ast.parse(snippet), "extensions/x_handler.py"), snippet


def test_g3_flags_string_commands_in_embedded_snippets() -> None:
    """The 14 callers that used to embed literal `'…'`/`\"…\"` around argv
    tokens would all fail G3 today."""
    for snippet in (
        "await self.command_runner.run('git status')\n",
        "await command_runner.run_sync(f'git {ref}')\n",
        "await self.command_runner.run(cmd='git ' + 'status')\n",
        "await self.command_runner.run(' '.join(parts))\n",
    ):
        assert _g3_violations(ast.parse(snippet)), snippet


def test_g3_accepts_argv_lists_in_embedded_snippets() -> None:
    assert not _g3_violations(
        ast.parse("await self.command_runner.run(['git', 'status'])\n")
    )
    assert not _g3_violations(
        ast.parse("await self.command_runner.run(cmd=['git', 'status'])\n")
    )
    # a command computed somewhere else (a variable) is G3's known blind spot,
    # left to `check_argv` in the runner and the fakes
    assert not _g3_violations(ast.parse("await self.command_runner.run(cmd)\n"))


def test_g4_flags_direct_spawns_outside_the_allowlist() -> None:
    assert _g4_violations(
        ast.parse("proc = await asyncio.create_subprocess_exec('mkdocs')\n"),
        "extensions/foo/foo/handler.py",
    )
    # tests may spawn processes legitimately (fakes, integration tests)
    assert not _g4_violations(
        ast.parse("proc = await asyncio.create_subprocess_exec('mkdocs')\n"),
        "extensions/foo/tests/test_x.py",
    )
    # every allowlist entry must parse as a plausible tracked path
    assert all(path.startswith(tuple(_G4_SCOPE_PREFIXES)) for path in _G4_ALLOWLIST)
