from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from finecode_knowledge.model.fingerprint import InputFingerprint, unchanged
from loguru import logger

from finecode.wm_server import context, domain
from finecode.wm_server.runner import action_meta_dump, finecode_cmd, runner_counts

RACY_WINDOW_SEC = 2.0
RACY_WINDOW_NS = 2_000_000_000
CACHE_VERSION = 1
ACCEPTED_FORMATS = [1]


@dataclasses.dataclass
class LookupResult:
    metas: dict[str, dict]
    failures: dict[str, context.ActionMetaFailure | None]


def _cache_file(venv: Path) -> Path:
    return venv / "cache" / "finecode" / "action_meta.json"


def _site_packages_dirs(venv: Path) -> list[Path]:
    win = venv / "Lib" / "site-packages"
    if win.is_dir():
        return [win]
    return sorted(venv.glob("lib/python*/site-packages"))


def _site_packages_dir(venv: Path) -> Path | None:
    dirs = _site_packages_dirs(venv)
    return dirs[0] if dirs else None


def _fp_unchanged(stored: dict, workspace_root: Path) -> bool:
    try:
        fingerprint = InputFingerprint.from_json(stored)
    except (KeyError, TypeError, ValueError):
        return False
    try:
        return unchanged(fingerprint, workspace_root)
    except Exception:  # noqa: BLE001 - a bad record must read as a miss, not a crash
        return False


def _is_racy(stored: dict, started_ns: int) -> bool:
    try:
        mtime_ns = int(stored["mtime_ns"])
    except (KeyError, TypeError, ValueError):
        return True
    try:
        ctime_ns = int(stored.get("ctime_ns", mtime_ns))
    except (TypeError, ValueError):
        ctime_ns = mtime_ns
    if sys.platform == "win32":
        latest = mtime_ns
    else:
        latest = max(mtime_ns, ctime_ns)
    return latest >= started_ns - RACY_WINDOW_NS


def er_identity(venv: Path) -> dict | None:
    matches: list[Path] = []
    for site_packages in _site_packages_dirs(venv):
        try:
            matches.extend(
                sorted(site_packages.glob("finecode_extension_runner-*.dist-info"))
            )
        except OSError:
            continue
    if len(matches) != 1:
        return None
    dist_info = matches[0]
    record_path = dist_info / "RECORD"
    record: dict | None = None
    try:
        if record_path.is_file():
            stat = record_path.stat()
            digest = hashlib.sha256()
            with record_path.open("rb") as handle:
                while chunk := handle.read(1 << 20):
                    digest.update(chunk)
            try:
                ctime_ns = stat.st_ctime_ns
            except AttributeError:
                ctime_ns = stat.st_mtime_ns
            record = {
                "path": str(record_path),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": ctime_ns,
                "sha256": digest.hexdigest(),
            }
    except OSError:
        record = None
    editable = False
    try:
        direct_url = json.loads(
            (dist_info / "direct_url.json").read_text(encoding="utf-8")
        )
        dir_info = (
            direct_url.get("dir_info", {}) if isinstance(direct_url, dict) else {}
        )
        editable = bool(dir_info.get("editable") is True)
    except (OSError, ValueError, UnicodeError):
        editable = False
    return {"distInfo": dist_info.name, "record": record, "editable": editable}


def _read_file(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        document = json.loads(text)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _project_actions(project: Any) -> list[domain.Action]:
    actions = project.actions
    if isinstance(actions, dict):
        return list(actions.values())
    return list(actions)


def _full_sources(project: Any, env_name: str) -> list[str]:
    sources: list[str] = []
    for action in _project_actions(project):
        handlers = getattr(action, "handlers", [])
        if not handlers:
            continue
        if handlers[0].env != env_name:
            continue
        source = getattr(action, "source", None)
        if source:
            sources.append(source)
    return sources


def _memo_valid(
    key: tuple[Path, str | None],
    entry: context.ActionMetaFailure,
    venv: Path,
) -> bool:
    _, source = key
    if source is not None:
        for stamp in entry.stamps:
            if not isinstance(stamp, dict) or not _fp_unchanged(stamp):
                return False
        return True
    current = _dir_mtime(_site_packages_dir(venv))
    return current == entry.site_packages_mtime_ns


def _dir_mtime(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _check_entry_files(files: list[dict]) -> bool:
    for item in files:
        if not isinstance(item, dict) or not _fp_unchanged(item):
            return False
    return True


def _check_entry_dirs(entry_files: list[dict], dirs: list[dict]) -> bool:
    known = set()
    for item in entry_files:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            known.add(item["path"])
    for entry in dirs:
        if not isinstance(entry, dict):
            return False
        dir_path = entry.get("path")
        names = entry.get("names")
        stored_mtime = entry.get("mtime_ns")
        if not isinstance(dir_path, str) or not isinstance(names, list):
            return False
        try:
            current_mtime = Path(dir_path).stat().st_mtime_ns
        except OSError:
            continue
        if current_mtime == stored_mtime:
            continue
        base = Path(dir_path)
        for name in names:
            if not isinstance(name, str):
                return False
            candidates: list[Path] = [
                base / f"{name}.py",
                base / name / "__init__.py",
                base / f"{name}.so",
                base / f"{name}.pyd",
            ]
            try:
                globbed = list(base.glob(f"{name}.*"))
            except OSError:
                globbed = []
            candidates.extend(globbed)
            try:
                globbed_init = list((base / name).glob("__init__.*"))
            except OSError:
                globbed_init = []
            candidates.extend(globbed_init)
            for candidate in candidates:
                try:
                    candidate_str = str(candidate)
                except Exception:  # noqa: BLE001 - a bad path reads as a miss
                    return False
                if candidate_str in known:
                    continue
                try:
                    if candidate.exists() and candidate.is_file():
                        return False
                except OSError:
                    continue
    return True


def _header_valid(stored_file: dict, venv: Path) -> bool:
    header = stored_file.get("header")
    if not isinstance(header, dict):
        return False
    if header.get("pythonpath", None) != os.environ.get("PYTHONPATH"):
        return False
    site_packages = header.get("sitePackages")
    if not isinstance(site_packages, dict):
        return False
    stored_path = site_packages.get("path")
    current_dir = _site_packages_dir(venv)
    if (current_dir is None or str(current_dir) != stored_path) and (
        stored_path is not None or current_dir is not None
    ):
        return False
    stored_mtime = site_packages.get("dir_mtime_ns")
    current_mtime = _dir_mtime(current_dir)
    stored_pth = site_packages.get("pth", [])
    if not isinstance(stored_pth, list):
        return False
    if current_mtime != stored_mtime:
        try:
            current_names = (
                sorted(p.name for p in current_dir.glob("*.pth") if p.is_file())
                if current_dir is not None
                else []
            )
        except OSError:
            current_names = []
        stored_names = sorted(
            Path(item["path"]).name
            for item in stored_pth
            if isinstance(item, dict) and "path" in item
        )
        if current_names != stored_names:
            return False
    for item in stored_pth:
        if not isinstance(item, dict) or not _fp_unchanged(item):
            return False
    return True


def _identity_valid(
    stored_identity: dict | None, current: dict | None, *, is_skew: bool
) -> bool:
    if current is None or not isinstance(stored_identity, dict):
        return False
    if stored_identity.get("distInfo") != current.get("distInfo"):
        return False
    if stored_identity.get("editable") != current.get("editable"):
        return False
    stored_record = stored_identity.get("record")
    current_record = current.get("record")
    if stored_record is None or current_record is None:
        if stored_record is not current_record and not (
            stored_record is None and current_record is None
        ):
            return False
    else:
        if not _fp_unchanged(stored_record):
            return False
    stored_action_meta = stored_identity.get("actionMeta")
    if is_skew:
        return True
    if not isinstance(stored_action_meta, dict):
        return False
    return _fp_unchanged(stored_action_meta)


def _merge_key(document_header: dict, er_identity: dict) -> tuple | None:
    try:
        site_packages = document_header["sitePackages"]
        pth_pairs = tuple(
            sorted(
                (item["path"], item["sha256"])
                for item in site_packages["pth"]
                if isinstance(item, dict)
            )
        )
        record = er_identity.get("record")
        action_meta = er_identity.get("actionMeta")
        return (
            er_identity.get("distInfo"),
            er_identity.get("editable"),
            record.get("sha256") if isinstance(record, dict) else None,
            action_meta.get("sha256") if isinstance(action_meta, dict) else None,
            tuple(document_header.get("sysPath", [])),
            document_header.get("pythonpath"),
            site_packages.get("path"),
            pth_pairs,
        )
    except (KeyError, TypeError, AttributeError):
        return None


def _skew_failure(reason: str) -> context.ActionMetaFailure:
    return context.ActionMetaFailure(
        kind="skew", reason=reason, stamps=(), site_packages_mtime_ns=None
    )


async def resolve_unresolved(
    project: Any,
    ws_context: context.WorkspaceContext,
    *,
    actions: list[domain.Action] | None = None,
) -> dict[str, context.ActionMetaFailure | None]:
    candidates = actions if actions is not None else _project_actions(project)
    targets = [
        action
        for action in candidates
        if getattr(action, "canonical_source", None) is None
        and getattr(action, "handlers", None)
    ]
    by_env: dict[str, list[domain.Action]] = {}
    for action in targets:
        env_name = action.handlers[0].env
        if env_name is None:
            continue
        by_env.setdefault(env_name, []).append(action)
    results = await asyncio.gather(
        *(
            lookup(project, env_name, [a.source for a in group], ws_context)
            for env_name, group in by_env.items()
        ),
        return_exceptions=True,
    )
    metas_by_source: dict[str, dict] = {}
    failures_by_source: dict[str, context.ActionMetaFailure | None] = {}
    for (_env_name, _), result in zip(by_env.items(), results, strict=True):
        if isinstance(result, BaseException):
            continue
        assert isinstance(result, LookupResult)
        metas_by_source.update(result.metas)
        failures_by_source.update(result.failures)
    applied = 0
    for action in targets:
        meta = metas_by_source.get(action.source)
        if meta is None:
            continue
        action.canonical_source = meta["canonical_source"]
        action.parent_action_source = meta.get("parentActionSource")
        action.language = meta.get("language")
        action.file_loc = meta.get("fileLoc")
        with contextlib.suppress(KeyError, ValueError):
            action.scope = domain.ActionScope(meta["scope"])
        action.runs_concurrently = bool(meta.get("runs_concurrently", False))
        action.meta_from_cache = True
        applied += 1
    if applied:
        ws_context.ws_action_schemas.pop(project.dir_path, None)
    return {
        action.source: failures_by_source.get(action.source)
        for action in targets
        if action.source not in metas_by_source
    }


async def lookup(
    project: Any,
    env_name: str,
    sources: list[str],
    ws_context: context.WorkspaceContext,
    *,
    use_memo: bool = True,
) -> LookupResult:
    venv = finecode_cmd.get_venv_dir_path(project.dir_path, env_name)
    metas: dict[str, dict] = {}
    failures: dict[str, context.ActionMetaFailure | None] = {}
    wanted = [s for s in sources if s]
    if use_memo:
        env_memo = ws_context.action_meta_failures.get((venv, None))
        if env_memo is not None:
            if _memo_valid((venv, None), env_memo, venv):
                for source in wanted:
                    failures[source] = env_memo
                return LookupResult(metas=metas, failures=failures)
            ws_context.action_meta_failures.pop((venv, None), None)
    stored_file = _read_file(_cache_file(venv))
    if stored_file is not None and str(stored_file.get("version")) == str(
        CACHE_VERSION
    ):
        current_identity = er_identity(venv)
        stored_identity = stored_file.get("erIdentity")
        is_skew_file = isinstance(stored_file.get("skew"), dict)
        if _identity_valid(
            stored_identity if isinstance(stored_identity, dict) else None,
            current_identity,
            is_skew=is_skew_file,
        ) and _header_valid(stored_file, venv):
            if is_skew_file:
                skew = stored_file["skew"]
                accepted = skew.get("acceptedFormats")
                if accepted == ACCEPTED_FORMATS:
                    ws_context.action_meta_dump_stats.skew_from_cache += 1
                    if venv not in ws_context.action_meta_skew_logged:
                        ws_context.action_meta_skew_logged.add(venv)
                        logger.info(
                            f"action metadata skew for env={env_name} in {project.dir_path}: "
                            f"{skew.get('reason')}; run python -m finecode prepare-envs --env={env_name}"
                        )
                    for source in wanted:
                        failures[source] = _skew_failure(
                            str(skew.get("reason", "skew"))
                        )
                    return LookupResult(metas=metas, failures=failures)
            else:
                entries = stored_file.get("entries", {})
                if isinstance(entries, dict):
                    remaining: list[str] = []
                    for source in wanted:
                        entry = entries.get(source)
                        if not isinstance(entry, dict):
                            remaining.append(source)
                            continue
                        files = entry.get("files", [])
                        dirs = entry.get("dirs", [])
                        if not isinstance(files, list) or not isinstance(dirs, list):
                            remaining.append(source)
                            continue
                        if _check_entry_files(files) and _check_entry_dirs(files, dirs):
                            metas[source] = entry["meta"]
                        else:
                            remaining.append(source)
                    wanted = remaining
                    if not wanted:
                        return LookupResult(metas=metas, failures=failures)
    if use_memo:
        still_missing: list[str] = []
        for source in wanted:
            if source in metas:
                continue
            memo = ws_context.action_meta_failures.get((venv, source))
            if memo is not None:
                if _memo_valid((venv, source), memo, venv):
                    failures[source] = memo
                else:
                    ws_context.action_meta_failures.pop((venv, source), None)
                    still_missing.append(source)
            else:
                still_missing.append(source)
        wanted = still_missing
        if not wanted:
            return LookupResult(metas=metas, failures=failures)
    task = _ensure_dump_task(venv, project, env_name, ws_context)
    if task is None:
        for source in wanted:
            if source not in metas and source not in failures:
                failures[source] = None
        return LookupResult(metas=metas, failures=failures)
    try:
        outcome = await asyncio.shield(task)
    except asyncio.CancelledError:
        for source in wanted:
            if source not in metas and source not in failures:
                failures[source] = None
        return LookupResult(metas=metas, failures=failures)
    if outcome.kind == "ok" and outcome.document is not None:
        entries = outcome.document.get("entries", {})
        doc_failures = outcome.document.get("failures", {})
        for source in wanted:
            if source in metas or source in failures:
                continue
            if isinstance(entries, dict) and source in entries:
                entry = entries[source]
                if isinstance(entry, dict) and "meta" in entry:
                    metas[source] = entry["meta"]
                else:
                    failures[source] = None
            elif isinstance(doc_failures, dict) and source in doc_failures:
                detail = doc_failures[source]
                error = (
                    detail.get("error", "import failed")
                    if isinstance(detail, dict)
                    else "import failed"
                )
                failures[source] = context.ActionMetaFailure(
                    kind="import_failed",
                    reason=str(error)[:300],
                    stamps=(),
                    site_packages_mtime_ns=None,
                )
            else:
                failures[source] = None
    elif outcome.kind == "skew":
        for source in wanted:
            if source in metas or source in failures:
                continue
            failures[source] = context.ActionMetaFailure(
                kind="skew",
                reason=outcome.reason,
                stamps=(),
                site_packages_mtime_ns=None,
            )
        if venv not in ws_context.action_meta_skew_logged:
            ws_context.action_meta_skew_logged.add(venv)
            logger.info(
                f"action metadata skew for env={env_name} in {project.dir_path}: "
                f"{outcome.reason}; run python -m finecode prepare-envs --env={env_name}"
            )
    elif outcome.kind in ("env_unusable", "timeout"):
        for source in wanted:
            if source in metas or source in failures:
                continue
            failures[source] = context.ActionMetaFailure(
                kind=outcome.kind,
                reason=outcome.reason,
                stamps=(),
                site_packages_mtime_ns=None,
            )
    else:
        for source in wanted:
            if source not in metas and source not in failures:
                failures[source] = None
    return LookupResult(metas=metas, failures=failures)


def _ensure_dump_task(
    venv: Path,
    project: Any,
    env_name: str,
    ws_context: context.WorkspaceContext,
) -> asyncio.Task | None:
    existing = ws_context.action_meta_dump_tasks.get(venv)
    if existing is not None and not existing.done():
        return existing
    if ws_context.shutting_down:
        return None
    task: asyncio.Task = asyncio.ensure_future(
        _dump_task(venv, project, env_name, ws_context)
    )
    ws_context.action_meta_dump_tasks[venv] = task

    def _drop(_: asyncio.Task) -> None:
        ws_context.action_meta_dump_tasks.pop(venv, None)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.warning(f"action metadata dump for {venv} failed: {exc}")

    task.add_done_callback(_drop)
    return task


async def _dump_task(
    venv: Path,
    project: Any,
    env_name: str,
    ws_context: context.WorkspaceContext,
) -> action_meta_dump.DumpOutcome:
    try:
        try:
            python_cmd = finecode_cmd.get_python_cmd(project.dir_path, env_name)
        except ValueError as exception:
            outcome = action_meta_dump.DumpOutcome(
                kind="env_unusable", document=None, reason=str(exception)
            )
            _replace_memo_from_outcome(venv, outcome, ws_context)
            _bump(outcome, ws_context)
            return outcome
        identity_before = er_identity(venv)
        sources = _full_sources(project, env_name)
        outcome = action_meta_dump.DumpOutcome(
            kind="env_unusable", document=None, reason="no attempt"
        )
        for timeout in action_meta_dump.DUMP_TIMEOUTS_SEC:
            ws_context.action_meta_dump_stats.waiting += 1
            runner_counts.record_runner_peaks(ws_context)
            try:
                await ws_context.er_startup_semaphore.acquire()
            except asyncio.CancelledError:
                ws_context.action_meta_dump_stats.waiting -= 1
                raise
            ws_context.action_meta_dump_stats.waiting -= 1
            ws_context.action_meta_dump_stats.running += 1
            ws_context.action_meta_dump_stats.spawned += 1
            try:
                outcome = await action_meta_dump.run_dump(
                    python_cmd, project.dir_path, sources, attempt_timeout=timeout
                )
            except asyncio.CancelledError:
                ws_context.action_meta_dump_stats.running -= 1
                ws_context.er_startup_semaphore.release()
                raise
            except Exception as exception:  # noqa: BLE001 - a dump must not kill its caller
                ws_context.action_meta_dump_stats.running -= 1
                ws_context.er_startup_semaphore.release()
                outcome = action_meta_dump.DumpOutcome(
                    kind="env_unusable", document=None, reason=str(exception)
                )
                break
            ws_context.action_meta_dump_stats.running -= 1
            ws_context.er_startup_semaphore.release()
            if outcome.kind != "timeout":
                break
        _bump(outcome, ws_context)
        _persist(venv, outcome, identity_before)
        _replace_memo_from_outcome(venv, outcome, ws_context)
        return outcome
    finally:
        ws_context.action_meta_dump_tasks.pop(venv, None)


def _bump(
    outcome: action_meta_dump.DumpOutcome, ws_context: context.WorkspaceContext
) -> None:
    stats = ws_context.action_meta_dump_stats
    if outcome.kind == "ok":
        stats.ok += 1
    elif outcome.kind == "skew":
        stats.skew += 1
    elif outcome.kind == "env_unusable":
        stats.env_unusable += 1
    elif outcome.kind == "timeout":
        stats.timeout += 1


def _persist(
    venv: Path,
    outcome: action_meta_dump.DumpOutcome,
    identity_before: dict | None,
) -> None:
    identity_after = er_identity(venv)
    if identity_after is None:
        return
    if identity_before != identity_after:
        return
    if outcome.kind == "skew":
        if bool(identity_after.get("editable")) is True:
            return
        marker = {
            "version": CACHE_VERSION,
            "erIdentity": {
                "distInfo": identity_after.get("distInfo"),
                "record": identity_after.get("record"),
                "actionMeta": None,
                "editable": identity_after.get("editable", False),
            },
            "header": _header_for_file(venv),
            "skew": {
                "reason": outcome.reason,
                "acceptedFormats": list(ACCEPTED_FORMATS),
            },
        }
        _atomic_write(_cache_file(venv), marker)
        return
    if outcome.kind != "ok" or outcome.document is None:
        return
    document = outcome.document
    header = document.get("header", {})
    if not isinstance(header, dict) or header.get("headerStable") is not True:
        return
    started_ns = document.get("startedNs")
    if not isinstance(started_ns, int):
        return
    site_packages = header.get("sitePackages", {}) if isinstance(header, dict) else {}
    for item in site_packages.get("pth", []) if isinstance(site_packages, dict) else []:
        if isinstance(item, dict) and _is_racy(item, started_ns):
            return
    action_meta_file = document.get("actionMetaFile")
    if isinstance(action_meta_file, dict) and _is_racy(action_meta_file, started_ns):
        return
    record = identity_after.get("record") or {}
    if isinstance(record, dict) and record and _is_racy(record, started_ns):
        return
    entries = document.get("entries", {})
    if not isinstance(entries, dict):
        return
    keep: dict[str, dict] = {}
    for source, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        files = entry.get("files", [])
        if not isinstance(files, list):
            continue
        if any(isinstance(item, dict) and _is_racy(item, started_ns) for item in files):
            continue
        keep[source] = {
            "meta": entry.get("meta"),
            "files": files,
            "dirs": entry.get("dirs", []),
        }
    action_meta_fp = document.get("actionMetaFile")
    new_identity = {
        "distInfo": identity_after.get("distInfo"),
        "record": identity_after.get("record"),
        "actionMeta": action_meta_fp,
        "editable": identity_after.get("editable", False),
    }
    new_header = {
        "sysPath": header.get("sysPath", []) if isinstance(header, dict) else [],
        "pythonpath": header.get("pythonpath") if isinstance(header, dict) else None,
        "sitePackages": site_packages,
    }
    path = _cache_file(venv)
    existing = _read_file(path)
    if (
        isinstance(existing, dict)
        and existing.get("version") == CACHE_VERSION
        and isinstance(existing.get("erIdentity"), dict)
        and isinstance(existing.get("header"), dict)
        and _merge_key(existing["header"], existing["erIdentity"])
        == _merge_key(new_header, new_identity)
    ):
        merged_entries = dict(existing.get("entries", {}))
        merged_entries.update(keep)
        _atomic_write(
            path,
            {
                "version": CACHE_VERSION,
                "erIdentity": new_identity,
                "header": new_header,
                "entries": merged_entries,
            },
        )
    else:
        _atomic_write(
            path,
            {
                "version": CACHE_VERSION,
                "erIdentity": new_identity,
                "header": new_header,
                "entries": keep,
            },
        )


def _header_for_file(venv: Path) -> dict:
    current_dir = _site_packages_dir(venv)
    fps: list[dict] = []
    try:
        names = (
            sorted(p.name for p in current_dir.glob("*.pth") if p.is_file())
            if current_dir is not None
            else []
        )
    except OSError:
        names = []
    for name in names:
        assert current_dir is not None
        try:
            stat = (current_dir / name).stat()
        except OSError:
            continue
        digest = hashlib.sha256()
        try:
            with (current_dir / name).open("rb") as handle:
                while chunk := handle.read(1 << 20):
                    digest.update(chunk)
        except OSError:
            continue
        try:
            ctime_ns = stat.st_ctime_ns
        except AttributeError:
            ctime_ns = stat.st_mtime_ns
        fps.append(
            {
                "path": str(current_dir / name),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": ctime_ns,
                "sha256": digest.hexdigest(),
            }
        )
    return {
        "sysPath": list(sys.path),
        "pythonpath": os.environ.get("PYTHONPATH"),
        "sitePackages": {
            "path": str(current_dir) if current_dir is not None else None,
            "dir_mtime_ns": _dir_mtime(current_dir),
            "pth": fps,
        },
    }


def _atomic_write(path: Path, document: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            tmp_path.write_text(json.dumps(document), encoding="utf-8")
            os.replace(tmp_path, path)
        except OSError as exception:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            logger.warning(
                f"action metadata cache write failed for {path}: {exception}"
            )
    except OSError as exception:
        logger.warning(f"action metadata cache write failed for {path}: {exception}")


def _replace_memo_from_outcome(
    venv: Path,
    outcome: action_meta_dump.DumpOutcome,
    ws_context: context.WorkspaceContext,
) -> None:
    for key in [k for k in ws_context.action_meta_failures if k[0] == venv]:
        del ws_context.action_meta_failures[key]
    if outcome.kind == "ok" and outcome.document is not None:
        doc_failures = outcome.document.get("failures", {})
        if isinstance(doc_failures, dict):
            for source, detail in doc_failures.items():
                error = (
                    detail.get("error", "import failed")
                    if isinstance(detail, dict)
                    else "import failed"
                )
                files = detail.get("files", []) if isinstance(detail, dict) else []
                stamps = tuple(item for item in files if isinstance(item, dict))
                ws_context.action_meta_failures[(venv, source)] = (
                    context.ActionMetaFailure(
                        kind="import_failed",
                        reason=str(error)[:300],
                        stamps=stamps,
                        site_packages_mtime_ns=None,
                    )
                )
                logger.info(
                    f"action metadata import failed for env={venv.name} source={source}: "
                    f"{str(error)[:300]}"
                )
        return
    if outcome.kind == "skew":
        current = er_identity(venv)
        editable = bool((current or {}).get("editable", False))
        if editable:
            ws_context.action_meta_failures[(venv, None)] = context.ActionMetaFailure(
                kind="skew",
                reason=outcome.reason,
                stamps=(),
                site_packages_mtime_ns=_dir_mtime(_site_packages_dir(venv)),
            )
        logger.info(
            f"action metadata dump for {venv} finished: skew; will retry on restart"
        )
        return
    ws_context.action_meta_failures[(venv, None)] = context.ActionMetaFailure(
        kind=outcome.kind,
        reason=outcome.reason,
        stamps=(),
        site_packages_mtime_ns=_dir_mtime(_site_packages_dir(venv)),
    )
    logger.info(
        f"action metadata dump for {venv} finished: {outcome.kind}; will retry on use"
    )


async def prefill(
    project: Any,
    env_names: list[str],
    ws_context: context.WorkspaceContext,
) -> None:
    await asyncio.sleep(RACY_WINDOW_SEC)
    actions = _project_actions(project)
    for env_name in env_names:
        sources = [
            action.source
            for action in actions
            if getattr(action, "handlers", None)
            and action.handlers[0].env == env_name
            and getattr(action, "source", None)
        ]
        if not sources:
            continue
        try:
            result = await lookup(
                project, env_name, sources, ws_context, use_memo=False
            )
        except Exception as exception:  # noqa: BLE001 - prefill never fails the install
            logger.info(
                f"action metadata prefill for env {env_name} in {project.dir_path}: "
                f"{exception}; it will be resolved on first use"
            )
            continue
        if result.failures:
            reasons = sorted(
                {
                    failure.reason
                    for failure in result.failures.values()
                    if failure is not None
                }
            )
            logger.info(
                f"action metadata prefill for env {env_name} in {project.dir_path}: "
                f"{'; '.join(reasons) if reasons else 'unresolved'}; it will be resolved on first use"
            )
        else:
            logger.info(
                f"action metadata prefill for env {env_name} in {project.dir_path}: ok"
            )


def forget_failures(
    ws_context: context.WorkspaceContext, venv_dir: Path | None = None
) -> None:
    if venv_dir is None:
        ws_context.action_meta_failures.clear()
        return
    for key in [k for k in ws_context.action_meta_failures if k[0] == venv_dir]:
        del ws_context.action_meta_failures[key]


def cancel_dumps(ws_context: context.WorkspaceContext) -> list[asyncio.Task]:
    tasks = list(ws_context.action_meta_dump_tasks.values())
    for task in tasks:
        task.cancel()
    return tasks
