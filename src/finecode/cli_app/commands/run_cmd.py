# docs: docs/cli.md
import asyncio
import json
import pathlib
import sys
import threading
import time
import typing
import uuid

import click
from loguru import logger

from finecode.cli_app import payload_uris, utils
from finecode.cli_app.log_render import render_log_records, user_message_log_level
from finecode.wm_client import ApiClient, ApiError, ReconnectPolicy
from finecode.wm_server import wm_lifecycle
from finecode.wm_server.runner import runner_client


class RunFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


def _ask_in_terminal(message: str, options: list[str], default: str | None) -> dict:
    """Put one question to the person at this terminal. Blocking; run off-loop.

    Everything is written to stderr, including the prompt itself: the answer
    belongs to the interaction, not to the run's output, and a caller piping
    stdout somewhere should get the same bytes whether or not something asked.

    Returns the ``client/elicit`` result: ``answered`` with the chosen option,
    or ``declined`` when the person ends the interaction (Ctrl-D).
    """
    click.echo("", err=True)
    click.echo(click.style(message, bold=True), err=True)
    for index, option in enumerate(options, start=1):
        marker = " (default)" if option == default else ""
        click.echo(f"  {index}) {option}{marker}", err=True)

    has_default = default in options
    hint = f" [{default}]" if has_default else ""
    while True:
        # The prompt goes out separately rather than as `input(...)`'s argument:
        # `input` writes its argument to *stdout*, which is the one stream this
        # function promises not to touch.
        click.echo(f"Choose 1-{len(options)}{hint}: ", nl=False, err=True)
        try:
            answer = input().strip()
        except EOFError:
            # Ctrl-D here ends *the question*, not the run: the person was asked
            # and did not choose, which is a decision the handler can act on
            # (ADR-0082 rule 3). Ctrl-C is not catchable here — CPython delivers
            # SIGINT to the main thread and this runs off it — and ends the whole
            # run, which is what Ctrl-C means everywhere else in this CLI.
            click.echo("", err=True)
            return {"outcome": "declined"}

        if not answer and has_default:
            # Enter on a defaulted question is a person accepting the default,
            # which is an answer — unlike the handler applying it unasked.
            return {"outcome": "answered", "value": default}
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return {"outcome": "answered", "value": options[int(answer) - 1]}
        if answer in options:
            return {"outcome": "answered", "value": answer}
        click.echo("Not one of the options. Try again.", err=True)


async def _ask_off_loop(message: str, options: list[str], default: str | None) -> dict:
    """Await :func:`_ask_in_terminal` on a thread this loop never has to join.

    Deliberately not ``asyncio.to_thread``: that borrows the default executor,
    whose threads are non-daemon and joined by ``asyncio.run`` on the way out. A
    prompt still parked in ``input()`` when the run ends — Ctrl-C, or the client
    closing while a question is outstanding — would hold that join open (forever
    before Python 3.12, five minutes after) for an answer the server has already
    stopped waiting for. A daemon thread is abandoned instead, which is the
    honest end for a question nobody is going to answer.
    """
    loop = asyncio.get_running_loop()
    answered: asyncio.Future[dict] = loop.create_future()

    def _settle(outcome: dict | None, error: BaseException | None) -> None:
        if answered.done():  # the run was torn down while the prompt was open
            return
        if error is not None:
            answered.set_exception(error)
        else:
            answered.set_result(outcome or {})

    def _prompt() -> None:
        try:
            result = _ask_in_terminal(message, options, default)
        except BaseException as exception:  # noqa: BLE001 - reported, not swallowed
            loop.call_soon_threadsafe(_settle, None, exception)
        else:
            loop.call_soon_threadsafe(_settle, result, None)

    threading.Thread(target=_prompt, name="finecode-prompt", daemon=True).start()
    return await answered


def _make_elicit_handler(prompt_idle: asyncio.Event) -> typing.Callable:
    """Return the ``client/elicit`` request handler for an attached terminal.

    Registered only when this CLI can actually answer; see ``run_actions``.
    While the question is on screen, *prompt_idle* is cleared so the streamed
    output still arriving does not print between the question and the cursor.
    """
    # One question at a time. A multi-project or multi-env run can have two ERs
    # ask at once; without this they would race two threads on the same stdin,
    # and whichever finished first would set `prompt_idle` again while the other
    # prompt was still on screen.
    asking = asyncio.Lock()

    async def handler(params: dict | None) -> dict:
        value = params or {}
        options = [str(option) for option in value.get("options") or []]
        if not options:
            # Nobody can be asked a question with no answers. Not "declined":
            # that means a person refused, which handlers are documented to
            # honour by aborting or taking the safe branch — a malformed
            # question must not read as a decision somebody took.
            return {"outcome": "unavailable"}

        async with asking:
            prompt_idle.clear()
            try:
                return await _ask_off_loop(
                    str(value.get("message", "")),
                    options,
                    value.get("default"),
                )
            finally:
                prompt_idle.set()

    return handler


def _make_progress_handler(is_tty: bool) -> typing.Callable:
    """Return an async ``actions/progress`` notification handler.

    TTY: overwrites the current line in-place using ANSI escape sequences.
    Non-TTY: prints plain-text lines to stderr, throttled to avoid log flooding.
    """
    last_message: list[str] = [""]
    last_print_time: list[float] = [0.0]

    async def handler(params: dict) -> None:
        value = params.get("value", {}) if params else {}
        progress_type = value.get("type")

        if progress_type == "begin":
            title = value.get("title", "")
            if is_tty:
                click.echo(f"\r\033[K{title}...", nl=False, err=True)
            else:
                click.echo(f"Starting: {title}", err=True)
            last_message[0] = title

        elif progress_type == "report":
            message = value.get("message") or ""
            percentage = value.get("percentage")
            if not message:
                return
            now = time.monotonic()
            # Throttle to at most once per second for TTY; always print for non-TTY
            # unless the message hasn't changed.
            if is_tty:
                if now - last_print_time[0] < 1.0:
                    return
                last_print_time[0] = now
                if percentage is not None:
                    click.echo(f"\r\033[K{percentage}% {message}", nl=False, err=True)
                else:
                    click.echo(f"\r\033[K{message}", nl=False, err=True)
            else:
                if message == last_message[0] and now - last_print_time[0] < 1.0:
                    return
                last_print_time[0] = now
                last_message[0] = message
                if percentage is not None:
                    click.echo(f"{percentage}% {message}", err=True)
                else:
                    click.echo(message, err=True)

        elif progress_type == "end":
            if is_tty:
                click.echo("\r\033[K", nl=False, err=True)
            else:
                end_message = value.get("message")
                if end_message:
                    click.echo(f"Done: {end_message}", err=True)

    return handler


async def run_actions(
    workdir_path: pathlib.Path,
    projects_names: list[str] | None,
    actions: list[str],
    action_payload: dict[str, typing.Any],
    concurrently: bool,
    handler_config_overrides: dict[str, dict[str, dict[str, str]]] | None = None,
    service_config_overrides: dict[str, dict[str, typing.Any]] | None = None,
    save_results: bool = True,
    map_payload_fields: set[str] | None = None,
    own_server: bool = False,
    log_level: str = "INFO",
    dev_env: str = "cli",
    wal_enabled: bool = False,
    verbose: bool = False,
    env_selectors: list[str] | None = None,
    interpreter_selectors: list[str] | None = None,
) -> utils.RunActionsResult:
    port_file = None
    try:
        if own_server:
            port_file = wm_lifecycle.start_own_server(
                workdir_path,
                log_level=log_level,
                wal_enabled=wal_enabled,
            )
            try:
                port = await wm_lifecycle.wait_until_ready_from_file(port_file)
            except TimeoutError as exc:
                raise RunFailed(str(exc)) from exc
        else:
            wm_lifecycle.ensure_running(workdir_path)
            try:
                port = await wm_lifecycle.wait_until_ready()
            except TimeoutError as exc:
                raise RunFailed(str(exc)) from exc

        client = ApiClient()

        # Notification handlers are registered before connecting: connecting
        # establishes the session, and anything the WM pushes during that must
        # not hit the "unhandled notification" fallback.
        # Tree-change notifications are irrelevant in CLI (run-and-exit) mode;
        # a no-op keeps them off that path.
        async def _ignore_tree_changed(params: dict) -> None:
            pass

        client.on_notification("actions/treeChanged", _ignore_tree_changed)

        async def _on_user_message(params: dict) -> None:
            value = params or {}
            level = user_message_log_level(value.get("type", "INFO"))
            logger.log(level, value.get("message", ""))

        client.on_notification("server/userMessage", _on_user_message)

        async def _on_log_records(params: dict) -> None:
            for line in render_log_records(params):
                click.echo(line, err=True)

        if verbose:
            client.on_notification("server/logRecords", _on_log_records)

        # Whether this *connection* can put a question to a person, decided by
        # the terminal rather than by the binary (ADR-0082 rule 2). In a
        # pipeline or on CI nothing is declared, so every ask a run makes is
        # answered "nobody could be asked" without a round trip — which is the
        # difference between an interactive action that works unattended and one
        # that hangs until its deadline.
        can_answer_questions = sys.stdin.isatty()
        # Set except while a question is on screen; the partial-result renderer
        # waits on it so streamed output does not interleave with the prompt.
        prompt_idle = asyncio.Event()
        prompt_idle.set()
        if can_answer_questions:
            client.on_request("client/elicit", _make_elicit_handler(prompt_idle))

        # When a project filter is given and we own the server, discover
        # projects first (no runners), resolve names to paths, then start
        # runners only for the requested projects.  In shared-server mode
        # runners are already running, so always use the normal path.
        deferred_runner_start = own_server and projects_names is not None

        async def _attach_session(*, first_connect: bool) -> None:
            """Establish the session state the WM holds for this client.

            Called by ``ApiClient`` on first connect and again after every
            reconnect, so a shared server that restarts mid-command does not
            leave this one talking to a WM that has never heard of it.
            """
            if verbose:
                await client.subscribe_logs(log_level)
            logger.info("Initializing workspace...")
            await client.add_dir(
                workdir_path,
                start_runners=not deferred_runner_start,
                initialize_all_handlers=not own_server,
            )

        client.configure_reconnect(
            # A dedicated server was started for this command alone; if it is
            # gone, resurrecting it would run against a different process than
            # the one the command was given (ADR-0074 rule 4).
            None if own_server else ReconnectPolicy(workdir=workdir_path),
            on_reattach=_attach_session,
        )

        try:
            # Capabilities travel with `client/initialize`, which `connect` sends
            # again on every reconnect — so a WM that restarted mid-run learns
            # this client can answer without the re-attach hook repeating it.
            await client.connect(
                "127.0.0.1",
                port,
                capabilities={"elicitation": {"choice": True}}
                if can_answer_questions
                else None,
            )
        except BaseException as exc:
            # `connect` runs `_attach_session`, so a config error surfaces here
            # rather than from a later call. The socket and its reader task are
            # already up at that point and nothing else closes them: the block
            # below owns that, and this never reaches it. A dedicated server
            # would then wait out its whole disconnect timeout with no client.
            await client.close()
            if isinstance(exc, ApiError):
                raise RunFailed(str(exc)) from exc
            raise
        try:
            if handler_config_overrides or service_config_overrides:
                if own_server:
                    await client.set_config_overrides(
                        handler_config_overrides or {}, service_config_overrides
                    )
                else:
                    click.echo(
                        "Warning: --config overrides are ignored in --shared-server mode. ",
                        err=True,
                    )

            # Resolve project names (CLI option) to paths (canonical API identifier).
            project_paths: list[str] | None = None
            if projects_names is not None:
                all_projects = await client.list_projects()
                unknown = [
                    n
                    for n in projects_names
                    if not any(p["name"] == n for p in all_projects)
                ]
                if unknown:
                    raise RunFailed(f"Unknown project(s): {unknown}")
                project_paths = [
                    p["path"] for p in all_projects if p["name"] in projects_names
                ]

            if deferred_runner_start:
                try:
                    await client.start_runners(projects=project_paths)
                except ApiError as exc:
                    raise RunFailed(str(exc)) from exc

            # Resolve action names to sources (ADR-0019).
            all_actions = await client.list_actions()
            name_to_source: dict[str, str] = {
                a["name"]: a["source"] for a in all_actions
            }
            source_to_name: dict[str, str] = {
                a["source"]: a["name"] for a in all_actions
            }
            unknown_actions = [a for a in actions if a not in name_to_source]
            if unknown_actions:
                raise RunFailed(f"Unknown action(s): {unknown_actions}")
            action_sources = [name_to_source[a] for a in actions]

            action_payload = await _absolutize_payload_resources(
                client=client,
                action_payload=action_payload,
                action_sources=action_sources,
                project_paths=project_paths,
                base_dir=workdir_path,
            )

            # Workspace-scoped actions run once on the root project and stream all
            # their sub-project output tagged with that single root path.  Repeating
            # the root header for every partial adds no information, so suppress it
            # when every requested action is workspace-scoped.
            scope_by_source = {a["source"]: a.get("scope") for a in all_actions}
            show_project_header = not (
                action_sources
                and all(
                    scope_by_source.get(src) == "workspace" for src in action_sources
                )
            )

            params_by_project: dict[str, dict[str, typing.Any]] = {}
            if map_payload_fields:
                params_by_project = _resolve_mapped_payload_fields(
                    map_payload_fields=map_payload_fields,
                    action_payload=action_payload,
                )

            result_formats = ["string", "json"] if save_results else ["string"]

            # Always stream via partial-result notifications, even for single-project runs.
            #
            # The non-streaming path (progress_token only) assumed: one project → one direct
            # result in result_by_format.  That holds for project-scope actions, but
            # workspace-scope actions (e.g. inspect_code) fan out to sub-projects internally
            # and deliver all output via partial_result_sender — the final RunActionResponse
            # has result_by_format={} regardless of how many --project filters are given.
            # Streaming works correctly for both cases, so there is no reason to branch.
            batch_options = {
                "concurrently": concurrently,
                "resultFormats": result_formats,
                "trigger": "user",
                "devEnv": dev_env,
                # Ask the WM to type-safely merge streamed partials per project/action
                # and return the merged result, so the returned/saved data is complete
                # even when one project streams many partials.
                "mergeResults": True,
                # PRD-0003 AC8: WM-only selectors restricting a matrixed
                # action's fan-out to a subset of its declared interpreter axis.
                # Never forwarded to an ER.
                "envSelectors": env_selectors or [],
                "interpreterSelectors": interpreter_selectors or [],
            }

            partial_result_token = str(uuid.uuid4())

            async def _on_partial_result(params: dict) -> None:
                # Hold output back while a question is on screen, rather than
                # printing between the prompt and the cursor.
                await prompt_idle.wait()
                value = params.get("value", {}) if params else {}
                project_str = value.get("project", "")
                results = value.get("results", {})
                interpreter = value.get("interpreter")
                block = _format_project_block(
                    project_str,
                    results,
                    source_to_name,
                    show_project_header,
                    interpreter,
                )
                # A partial with no rendered content (e.g. a project with nothing to
                # report) would otherwise print just the project header with an empty
                # body; skip it. The merged result still lands in the final response.
                if block is None:
                    return

                # split blocks with newline
                block = "\n" + block

                click.echo(block, nl=False)

            client.on_notification("actions/partialResult", _on_partial_result)

            logger.info(f"Running {', '.join(actions)}...")
            try:
                batch_result = await client.run_batch(
                    action_sources=action_sources,
                    projects=project_paths,
                    params=action_payload,
                    params_by_project=params_by_project or None,
                    options=batch_options,
                    partial_result_token=partial_result_token,
                )
            except ApiError as exc:
                raise RunFailed(str(exc)) from exc

            # Use the WM's type-safely merged per-project results (requested via
            # mergeResults) for the saved/returned data.
            return _build_streaming_result(
                batch_result.get("results", {}),
                batch_result.get("returnCode", 0),
                scope_by_action_source={
                    source: scope_by_source.get(source) for source in action_sources
                },
                project_paths_requested=project_paths,
            )
        finally:
            await client.close()
    finally:
        if port_file is not None and port_file.exists():
            port_file.unlink(missing_ok=True)


def _format_project_block(
    project_path_str: str,
    actions_results: dict,
    source_to_name: dict[str, str] | None = None,
    show_project_header: bool = True,
    interpreter: str | None = None,
) -> str | None:
    """Format one project's action results as a printable block.

    Prepends the project path header unless *show_project_header* is False — the
    caller suppresses it for workspace-scoped runs, where every partial carries
    the same root path and the header would just repeat.  Returns ``None`` when
    there is nothing to render, so the caller can skip printing an empty body.

    *interpreter* is the canonical interpreter string (``"<impl>@<version>"``)
    carried by partials of a matrixed action; when given, a sub-heading is
    rendered under the project header, before the action block, so live output
    from concurrently running interpreter variants is distinguishable.
    """
    run_many_actions = len(actions_results) > 1
    project_output_parts: list[str] = []

    for action_source, action_data in actions_results.items():
        result_by_format = action_data.get("resultByFormat", {})
        return_code = action_data.get("returnCode", 0)
        response = runner_client.RunActionResponse(
            result_by_format=result_by_format,
            return_code=return_code,
        )
        display_name = (source_to_name or {}).get(action_source, action_source)
        action_output = ""
        if run_many_actions:
            action_output += f"{click.style(display_name, bold=True)}:"
        action_output += utils.run_result_to_str(response.text(), display_name)
        project_output_parts.append(action_output)

    content = "".join(project_output_parts)
    if not content.strip():
        return None

    if interpreter:
        content = f"{click.style(interpreter, dim=True)}\n" + content

    if show_project_header:
        block = (
            f"{click.style(project_path_str, bold=True, underline=True)}\n" + content
        )
    else:
        block = content

    if not block.endswith("\n"):
        block += "\n"

    return block


def _build_streaming_result(
    streaming_results: dict[str, dict],
    overall_return_code: int,
    scope_by_action_source: dict[str, str | None] | None = None,
    project_paths_requested: list[str] | None = None,
) -> utils.RunActionsResult:
    """Build a RunActionsResult from collected partial-result notifications.

    Output is empty because each project block was already printed to stdout as
    the notification arrived.  ``result_by_project`` is populated for callers
    that need the structured data (e.g. ``--save-results``).
    """
    result_by_project: dict[
        pathlib.Path, dict[str, runner_client.RunActionResponse]
    ] = {}
    for project_path_str, actions_results in streaming_results.items():
        project_path = pathlib.Path(project_path_str)
        project_responses: dict[str, runner_client.RunActionResponse] = {}
        for action_source, action_data in actions_results.items():
            project_responses[action_source] = runner_client.RunActionResponse(
                result_by_format=action_data.get("resultByFormat", {}),
                return_code=action_data.get("returnCode", 0),
            )
        result_by_project[project_path] = project_responses

    return utils.RunActionsResult(
        output="",
        return_code=overall_return_code,
        result_by_project=result_by_project,
        scope_by_action_source=scope_by_action_source,
        project_paths_requested=project_paths_requested,
    )


async def _absolutize_payload_resources(
    client: ApiClient,
    action_payload: dict[str, typing.Any],
    action_sources: list[str],
    project_paths: list[str] | None,
    base_dir: pathlib.Path,
) -> dict[str, typing.Any]:
    """Make every resource in *action_payload* absolute before it leaves the CLI.

    Which fields hold resources comes from the actions' own payload schemas, so
    a plain path is accepted wherever an action declares a ``ResourceUri`` and
    nowhere else.

    A relative ``file://`` URI that no schema accounts for stops the run.  It
    cannot be left alone — each ER would resolve it against its own directory,
    silently reading a different file per project — and it cannot be rewritten
    either, because without a schema there is nothing saying the field is a
    resource at all.  Refusing is the only answer that never acts on a guess.
    """
    # Any project the action runs in resolves the same payload types; the first
    # requested one is as good as any, and the workspace root serves when the
    # run is not restricted to a subset.
    schema_project = project_paths[0] if project_paths else str(base_dir)
    try:
        schemas = await client.get_payload_schemas(schema_project, action_sources)
    except ApiError as exc:
        logger.debug(f"Could not read payload schemas from '{schema_project}': {exc}")
        schemas = {}

    properties = payload_uris.merge_payload_properties(schemas)
    resolved = payload_uris.absolutize_payload(action_payload, properties, base_dir)

    unresolved = payload_uris.find_unresolved_relative_uris(resolved)
    if unresolved:
        unschemad = [source for source in action_sources if not schemas.get(source)]
        detail = (
            f" No payload schema was available for {', '.join(unschemad)}"
            f" in '{schema_project}', so these fields could not be confirmed to"
            " hold resources."
            if unschemad
            else ""
        )
        raise RunFailed(
            "Relative file:// URIs cannot be sent to extension runners — each"
            " runner would resolve them against its own project directory."
            f" Use absolute paths for: {'; '.join(unresolved)}.{detail}"
        )
    return resolved


def _resolve_mapped_payload_fields(
    map_payload_fields: set[str],
    action_payload: dict[str, typing.Any],
) -> dict[str, dict[str, typing.Any]]:
    """Resolve mapped payload fields from saved action results.

    Returns a dict keyed by project path string, where each value is a dict
    of field overrides for that project.
    """
    results_dir = (
        pathlib.Path(sys.executable).parent.parent / "cache" / "finecode" / "results"
    )
    params_by_project: dict[str, dict[str, typing.Any]] = {}

    for field_name in map_payload_fields:
        raw_value = action_payload.get(field_name)
        if raw_value is None:
            raise RunFailed(
                f"Mapped payload field '{field_name}' not found in action payload"
            )

        action_name, field_path = str(raw_value).split(".", 1)
        result_file = results_dir / f"{action_name}.json"
        if not result_file.exists():
            raise RunFailed(
                f"Results file '{result_file}' not found for mapped field '{field_name}'"
            )

        results_by_project: dict[str, typing.Any] = json.loads(result_file.read_text())
        for project_path, project_result in results_by_project.items():
            resolved_value = project_result
            for key in field_path.split("."):
                if not isinstance(resolved_value, dict):
                    raise RunFailed(
                        f"Cannot resolve '{field_path}' in results of '{action_name}'"
                        f" for project '{project_path}'"
                    )
                resolved_value = resolved_value.get(key)

            if project_path not in params_by_project:
                params_by_project[project_path] = {}
            params_by_project[project_path][field_name] = resolved_value

    return params_by_project


__all__ = ["run_actions"]
