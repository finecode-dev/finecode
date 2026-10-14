from __future__ import annotations

import ast
import collections.abc
import contextlib
import enum
import hashlib
import importlib
import importlib.util
import inspect
import os
import sys
import sysconfig
import time
import traceback
import types
from pathlib import Path

from finecode_extension_api.code_action import Action, HandlerExecution

_CHUNK = 1 << 20
_RACY_WINDOW_NS = 2_000_000_000


def file_loc(cls: type, project_dir: Path | None) -> str | None:
    try:
        source_file = inspect.getfile(cls)
        _, lineno = inspect.getsourcelines(cls)
    except (OSError, TypeError):
        return None

    path = Path(source_file)
    if project_dir is not None:
        with contextlib.suppress(ValueError):
            path = path.relative_to(project_dir)
    return f"{path}:{lineno}"


def action_meta(cls: type, project_dir: Path | None) -> dict:
    if not (isinstance(cls, type) and issubclass(cls, Action)):
        raise TypeError(f"{cls!r} is not a subclass of Action")
    parent = getattr(cls, "PARENT_ACTION", None)
    return {
        "canonical_source": f"{cls.__module__}.{cls.__qualname__}",
        "runs_concurrently": cls.HANDLER_EXECUTION == HandlerExecution.CONCURRENT,
        "scope": cls.SCOPE.value,
        "parentActionSource": (
            f"{parent.__module__}.{parent.__qualname__}" if parent is not None else None
        ),
        "language": getattr(cls, "LANGUAGE", None),
        "fileLoc": file_loc(cls, project_dir),
    }


def _is_stdlib(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    try:
        stdlib = Path(sysconfig.get_paths()["stdlib"]).resolve()
        platstdlib = Path(sysconfig.get_paths()["platstdlib"]).resolve()
    except (KeyError, OSError):
        return False
    return resolved.is_relative_to(stdlib) or resolved.is_relative_to(platstdlib)


def _fingerprint_uncached(path: Path) -> dict | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
    except OSError:
        return None
    try:
        ctime_ns = stat.st_ctime_ns
    except AttributeError:
        ctime_ns = stat.st_mtime_ns
    return {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": ctime_ns,
        "sha256": digest.hexdigest(),
    }


def _module_chain(module_name: str) -> list[Path]:
    parts = module_name.split(".")
    files: list[Path] = []
    for i in range(1, len(parts) + 1):
        prefix = ".".join(parts[:i])
        try:
            spec = importlib.util.find_spec(prefix)
        except (ImportError, ValueError):
            continue
        if spec is None or spec.origin is None:
            continue
        if spec.origin in ("built-in", "frozen"):
            continue
        candidate = Path(spec.origin)
        if candidate.suffix == ".pyc":
            sibling = candidate.with_suffix(".py")
            if sibling.exists():
                candidate = sibling
        if candidate.exists() and candidate.is_file():
            files.append(candidate)
    return files


def _reexporters(value: object, name: str) -> list[str]:
    found: list[str] = []
    for mod_name, mod in list(sys.modules.items()):
        if not isinstance(mod, types.ModuleType):
            continue
        try:
            candidate = mod.__dict__.get(name)
        except Exception:  # noqa: BLE001, S112 - extension module dicts can raise; untestable here
            continue
        if candidate is value:
            found.append(mod_name)
    return found


def _parse_cached(
    path: Path, ast_cache: dict[str, ast.Module | None]
) -> ast.Module | None:
    key = str(path)
    if key in ast_cache:
        return ast_cache[key]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        ast_cache[key] = None
        return None
    try:
        tree = ast.parse(text, filename=key)
    except SyntaxError:
        ast_cache[key] = None
        return None
    ast_cache[key] = tree
    return tree


def _find_classdef(tree: ast.Module, qualname: str) -> ast.ClassDef | None:
    parts = qualname.split(".")
    body: list[ast.stmt] = list(tree.body)
    current: ast.ClassDef | None = None
    for part in parts:
        match = None
        for node in body:
            if isinstance(node, ast.ClassDef) and node.name == part:
                match = node
                break
        if match is None:
            return None
        current = match
        body = list(match.body)
    return current


def _class_attr_rhs(classdef: ast.ClassDef, attr: str) -> ast.expr | None:
    matches: list[ast.expr] = []
    for node in classdef.body:
        if isinstance(node, ast.Assign):
            matches.extend(
                node.value
                for target in node.targets
                if isinstance(target, ast.Name) and target.id == attr
            )
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == attr
            and node.value is not None
        ):
            matches.append(node.value)
    if len(matches) != 1:
        return None
    return matches[0]


def _enum_chain(value: object, extra: set[Path]) -> None:
    if isinstance(value, enum.Enum):
        extra.update(_module_chain(type(value).__module__))


def _classify_rhs(
    rhs: ast.expr,
    defining_module_name: str,
    actual_value: object,
) -> tuple[bool, set[Path]]:
    extra: set[Path] = set()
    if isinstance(rhs, ast.Constant):
        return True, extra
    defining_mod = sys.modules.get(defining_module_name)
    if defining_mod is None:
        return False, extra
    if isinstance(rhs, ast.Name):
        name = rhs.id
        try:
            holder = defining_mod.__dict__.get(name)
        except Exception:  # noqa: BLE001
            return False, extra
        if holder is not actual_value:
            return False, extra
        for mod_name in _reexporters(actual_value, name):
            extra.update(_module_chain(mod_name))
        _enum_chain(actual_value, extra)
        if isinstance(actual_value, type):
            extra.update(_module_chain(actual_value.__module__))
        if not extra:
            return False, extra
        return True, extra
    if isinstance(rhs, ast.Attribute) and isinstance(rhs.value, ast.Name):
        alias = rhs.value.id
        member = rhs.attr
        try:
            base = defining_mod.__dict__.get(alias)
        except Exception:  # noqa: BLE001
            return False, extra
        if isinstance(base, types.ModuleType):
            if getattr(base, member, None) is not actual_value:
                return False, extra
            extra.update(_module_chain(base.__name__))
            for mod_name in _reexporters(actual_value, member):
                extra.update(_module_chain(mod_name))
            if isinstance(actual_value, type):
                extra.update(_module_chain(actual_value.__module__))
            _enum_chain(actual_value, extra)
            return True, extra
        if isinstance(base, type) and issubclass(base, enum.Enum):
            if getattr(base, member, None) is not actual_value:
                return False, extra
            extra.update(_module_chain(base.__module__))
            return True, extra
        return False, extra
    return False, extra


def _stmt_binds_top(stmt: ast.stmt, member: str) -> bool:
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            for node in ast.walk(target):
                if isinstance(node, ast.Name) and node.id == member:
                    return True
        return False
    if isinstance(stmt, ast.AnnAssign):
        for node in ast.walk(stmt.target):
            if isinstance(node, ast.Name) and node.id == member:
                return True
        return False
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return stmt.name == member
    if isinstance(stmt, ast.Import):
        for alias in stmt.names:
            bound = alias.asname or alias.name.split(".")[0]
            if bound == member:
                return True
        return False
    if isinstance(stmt, ast.ImportFrom):
        for alias in stmt.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name.split(".")[0]
            if bound == member:
                return True
        return False
    return False


def _binds_inside_branch(node: ast.AST, member: str) -> bool:
    for inner in ast.walk(node):
        if inner is node:
            continue
        if (
            isinstance(inner, ast.Name)
            and inner.id == member
            and isinstance(inner.ctx, ast.Store)
        ):
            return True
        if (
            isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and inner.name == member
        ):
            return True
    return False


def _check_module_binding(
    module_name: str, member: str, ast_cache: dict[str, ast.Module | None]
) -> bool:
    mod = sys.modules.get(module_name)
    if mod is None:
        return False
    filename = getattr(mod, "__file__", None)
    if not filename:
        return False
    path = Path(filename)
    if path.suffix == ".pyc":
        sibling = path.with_suffix(".py")
        if sibling.exists():
            path = sibling
    if not path.exists() or path.suffix not in (".py", ".pyw"):
        return False
    tree = _parse_cached(path, ast_cache)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    return False
    count = 0
    for stmt in tree.body:
        if _stmt_binds_top(stmt, member):
            count += 1
    if count != 1:
        return False
    for stmt in tree.body:
        if isinstance(
            stmt,
            (
                ast.If,
                ast.Try,
                ast.TryStar,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.With,
                ast.AsyncWith,
            ),
        ):
            if _binds_inside_branch(stmt, member):
                return False
            for inner in ast.walk(stmt):
                if (
                    isinstance(inner, ast.Name)
                    and inner.id == member
                    and isinstance(inner.ctx, ast.Store)
                ):
                    return False
                if (
                    isinstance(
                        inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    )
                    and inner.name == member
                ):
                    return False
    return True


def _capture_pths(site_packages: Path) -> tuple[list[str], list[dict]]:
    try:
        names = sorted(p.name for p in site_packages.glob("*.pth") if p.is_file())
    except OSError:
        return [], []
    fps: list[dict] = []
    for name in names:
        fp = _fingerprint_uncached(site_packages / name)
        if fp is not None:
            fps.append(fp)
    return names, fps


def _stat_eq(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return (
        a.get("size") == b.get("size")
        and a.get("mtime_ns") == b.get("mtime_ns")
        and a.get("ctime_ns") == b.get("ctime_ns")
    )


def _traceback_files(exc: BaseException) -> list[dict]:
    files: list[dict] = []
    seen: set[str] = set()
    tb = exc.__traceback__
    frames = traceback.extract_tb(tb) if tb is not None else []
    for frame in frames:
        filename = frame.filename
        if not filename or filename in seen:
            continue
        seen.add(filename)
        path = Path(filename)
        if not path.exists() or not path.is_file():
            continue
        if _is_stdlib(path):
            continue
        fp = _fingerprint_uncached(path)
        if fp is not None:
            files.append(fp)
    return files


def _non_stdlib_loaded_files() -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for mod in list(sys.modules.values()):
        if not isinstance(mod, types.ModuleType):
            continue
        filename = getattr(mod, "__file__", None)
        if not filename or filename in seen:
            continue
        seen.add(filename)
        path = Path(filename)
        if path.suffix == ".pyc":
            sibling = path.with_suffix(".py")
            if sibling.exists():
                path = sibling
        if not path.exists() or not path.is_file():
            continue
        if _is_stdlib(path):
            continue
        result.append(path)
    return result


def _import_member(source: str) -> object:
    member_name = source.split(".")[-1]
    module_path = ".".join(source.split(".")[:-1])
    if not module_path:
        raise ModuleNotFoundError(f"No module in source {source!r}")
    module = importlib.import_module(module_path)
    try:
        return module.__dict__[member_name]
    except KeyError as exc:
        raise ModuleNotFoundError(
            f"Member {member_name} not found in module {module_path}"
        ) from exc


def dump(
    sources: collections.abc.Sequence[str],
    project_dir: Path,
    *,
    site_packages: Path | None = None,
) -> dict:
    started_ns = time.time_ns()
    project_dir = Path(project_dir)
    purelib = (
        Path(sysconfig.get_paths()["purelib"])
        if site_packages is None
        else Path(site_packages)
    )
    try:
        pythonpath = os.environ.get("PYTHONPATH")
    except Exception:  # noqa: BLE001 - environ access cannot fail usefully here
        pythonpath = None
    start_pth_names, start_pth_fps = _capture_pths(purelib)
    try:
        start_pth_by_name = {fp["path"]: fp for fp in start_pth_fps}
    except (KeyError, TypeError):
        start_pth_by_name = {}
    start_action_meta_fp = _fingerprint_uncached(Path(__file__))

    ast_cache: dict[str, ast.Module | None] = {}
    fp_cache: dict[str, dict | None] = {}

    def cached_fp(path: Path) -> dict | None:
        key = str(path)
        if key in fp_cache:
            return fp_cache[key]
        fp = _fingerprint_uncached(path)
        fp_cache[key] = fp
        return fp

    pending: dict[str, dict] = {}
    failures: dict[str, dict] = {}

    for source in sources:
        try:
            cls = _import_member(source)
            if not isinstance(cls, type):
                raise TypeError(f"{source} is not a class")
            meta = action_meta(cls, project_dir)
        except Exception as exc:  # noqa: BLE001 - reachable set is open at import
            failures[source] = {
                "error": f"{type(exc).__name__}: {exc}",
                "files": _traceback_files(exc),
            }
            continue
        try:
            narrow, covered = _narrow_files(source, cls, ast_cache)
        except Exception:  # noqa: BLE001 - stamp computation must not fail the dump
            narrow, covered = set(), False
        pending[source] = {
            "meta": meta,
            "narrow": narrow,
            "covered": covered,
            "cls": cls,
        }

    full_loaded = _non_stdlib_loaded_files()
    full_loaded = sorted(set(full_loaded))
    full_fps: list[dict] = []
    for path in full_loaded:
        fp = cached_fp(path)
        if fp is not None:
            full_fps.append(fp)
    full_fps = sorted(full_fps, key=lambda fp: fp["path"])

    entries: dict[str, dict] = {}
    for source, item in pending.items():
        if item["covered"]:
            files = sorted(item["narrow"])
            fps: list[dict] = []
            for path in files:
                fp = cached_fp(path)
                if fp is not None:
                    fps.append(fp)
            entries[source] = {"meta": item["meta"], "files": fps, "dirs": []}
        else:
            entries[source] = {"meta": item["meta"], "files": full_fps, "dirs": []}

    sys_path = list(sys.path)
    stdlib_names: set[str] = set(getattr(sys, "stdlib_module_names", ()))
    top_levels: set[str] = set()
    for source, item in pending.items():
        cls = item["cls"]
        for mod_name in _candidate_tops(source, cls):
            top = mod_name.split(".")[0]
            if top in stdlib_names:
                continue
            top_levels.add(top)

    cwd = Path.cwd()
    resolved_syspath: list[str] = []
    for entry in sys_path:
        if entry == "":
            resolved_syspath.append(str(cwd))
        else:
            resolved_syspath.append(entry)

    dir_mtime_cache: dict[str, int | None] = {}

    def dir_mtime(path_str: str) -> int | None:
        if path_str in dir_mtime_cache:
            return dir_mtime_cache[path_str]
        try:
            mtime = Path(path_str).stat().st_mtime_ns
        except OSError:
            mtime = None
        dir_mtime_cache[path_str] = mtime
        return mtime

    per_source_dirs: dict[str, dict[str, set[str]]] = {source: {} for source in pending}
    for source, item in pending.items():
        files = full_loaded if not item["covered"] else sorted(item["narrow"])
        local: dict[str, set[str]] = {}
        for path in files:
            if path.name == "__init__.py":
                d = str(path.parent.parent)
                n = path.parent.name
            else:
                d = str(path.parent)
                n = path.stem
            local.setdefault(d, set()).add(n)
        for top in sorted(top_levels):
            found_at = None
            for idx, entry in enumerate(resolved_syspath):
                base = Path(entry)
                if (base / f"{top}.py").exists() or (
                    base / top / "__init__.py"
                ).exists():
                    found_at = idx
                    break
            if found_at is None:
                continue
            for idx in range(found_at):
                local.setdefault(resolved_syspath[idx], set()).add(top)
        per_source_dirs[source] = local

    for source in pending:
        local = per_source_dirs[source]
        dir_list: list[dict] = []
        for d in sorted(local):
            mtime = dir_mtime(d)
            if mtime is None:
                continue
            dir_list.append({"path": d, "mtime_ns": mtime, "names": sorted(local[d])})
        entries[source]["dirs"] = dir_list

    try:
        site_dir_mtime = purelib.stat().st_mtime_ns
    except OSError:
        site_dir_mtime = 0
    _, end_pth_fps = _capture_pths(purelib)
    try:
        end_pth_names = sorted(p.name for p in purelib.glob("*.pth") if p.is_file())
    except OSError:
        end_pth_names = []
    end_action_meta_fp = _fingerprint_uncached(Path(__file__))

    header_stable = True
    if start_pth_names != end_pth_names:
        header_stable = False
    else:
        end_by_path = {fp["path"]: fp for fp in end_pth_fps}
        if set(start_pth_by_name) != set(end_by_path):
            header_stable = False
        else:
            for key, start_fp in start_pth_by_name.items():
                if not _stat_eq(start_fp, end_by_path.get(key)):
                    header_stable = False
                    break
    if not _stat_eq(start_action_meta_fp, end_action_meta_fp):
        header_stable = False

    header = {
        "sysPath": sys_path,
        "pythonpath": pythonpath,
        "sitePackages": {
            "path": str(purelib),
            "dir_mtime_ns": site_dir_mtime,
            "pth": end_pth_fps,
        },
        "headerStable": header_stable,
    }
    return {
        "format": 1,
        "startedNs": started_ns,
        "actionMetaFile": end_action_meta_fp,
        "header": header,
        "entries": entries,
        "failures": failures,
    }


def _candidate_tops(source: str, cls: type) -> set[str]:
    tops: set[str] = set()
    module_part = ".".join(source.split(".")[:-1])
    if module_part:
        tops.add(module_part)
    with contextlib.suppress(AttributeError):
        tops.add(cls.__module__)
    for base in getattr(cls, "__mro__", ()):
        mod = getattr(base, "__module__", None)
        if isinstance(mod, str):
            tops.add(mod)
    parent = getattr(cls, "PARENT_ACTION", None)
    if parent is not None:
        mod = getattr(parent, "__module__", None)
        if isinstance(mod, str):
            tops.add(mod)
    return tops


def _narrow_files(
    source: str, cls: type, ast_cache: dict[str, ast.Module | None]
) -> tuple[set[Path], bool]:
    narrow: set[Path] = set()
    module_part = ".".join(source.split(".")[:-1])
    member = source.split(".")[-1]
    if module_part:
        narrow.update(_module_chain(module_part))
    narrow.update(_module_chain(cls.__module__))
    for mod_name in _reexporters(cls, cls.__name__):
        narrow.update(_module_chain(mod_name))
    for base in getattr(cls, "__mro__", ()):
        mod = getattr(base, "__module__", None)
        if isinstance(mod, str):
            narrow.update(_module_chain(mod))
    parent = getattr(cls, "PARENT_ACTION", None)
    if parent is not None and isinstance(parent, type):
        narrow.update(_module_chain(parent.__module__))
    narrow = {p for p in narrow if p.exists() and p.is_file() and not _is_stdlib(p)}

    for attr in ("PARENT_ACTION", "LANGUAGE", "SCOPE", "HANDLER_EXECUTION"):
        holder: type | None = None
        for base in getattr(cls, "__mro__", ()):
            if not isinstance(base, type):
                continue
            if attr in base.__dict__:
                holder = base
                break
        if holder is None:
            return narrow, False
        actual = holder.__dict__[attr]
        filename = None
        try:
            filename = inspect.getsourcefile(holder)
        except (OSError, TypeError):
            filename = None
        if filename is None:
            module_file = sys.modules.get(holder.__module__)
            if module_file is not None:
                filename = getattr(module_file, "__file__", None)
        if not filename:
            return narrow, False
        path = Path(filename)
        if path.suffix == ".pyc":
            sibling = path.with_suffix(".py")
            if sibling.exists():
                path = sibling
        if not path.exists() or path.suffix not in (".py", ".pyw"):
            return narrow, False
        tree = _parse_cached(path, ast_cache)
        if tree is None:
            return narrow, False
        classdef = _find_classdef(tree, holder.__qualname__)
        if classdef is None:
            return narrow, False
        rhs = _class_attr_rhs(classdef, attr)
        if rhs is None:
            return narrow, False
        covered, extra = _classify_rhs(rhs, holder.__module__, actual)
        if not covered:
            return narrow, False
        narrow.update(
            p for p in extra if p.exists() and p.is_file() and not _is_stdlib(p)
        )

    reexporter_mods = set(_reexporters(cls, cls.__name__))
    if module_part:
        reexporter_mods.add(module_part)
    for mod_name in sorted(reexporter_mods):
        if mod_name not in sys.modules:
            return narrow, False
        if not _check_module_binding(mod_name, member, ast_cache):
            return narrow, False
    return narrow, True
