import asyncio
import json
import os
import pathlib
import sys
import typing

import click
from loguru import logger

import finecode.lsp_server.main as wm_lsp_server
from finecode import logger_utils, user_messages
from finecode.lsp_server import communication_utils
from finecode.cli_app.commands import dump_config_cmd, prepare_envs_cmd, run_cmd
from finecode.api_server.config.config_models import ConfigurationError


FINECODE_CONFIG_ENV_PREFIX = "FINECODE_CONFIG_"

# TODO: unify possibilities of CLI options and env vars
def parse_handler_config_from_env() -> dict[str, dict[str, dict[str, str]]]:
    """
    Parse handler config overrides from environment variables.

    Format:
    - FINECODE_CONFIG_<ACTION>__<PARAM>=value
      -> action-level config for all handlers of action
    - FINECODE_CONFIG_<ACTION>__<HANDLER>__<PARAM>=value
      -> handler-specific config

    Returns nested dict: {action_name: {handler_name_or_empty: {param: value}}}
    Empty string key "" means action-level (applies to all handlers).
    """
    config_overrides: dict[str, dict[str, dict[str, str]]] = {}

    for env_name, env_value in os.environ.items():
        if not env_name.startswith(FINECODE_CONFIG_ENV_PREFIX):
            continue

        # Remove prefix and split by double underscore
        config_key = env_name[len(FINECODE_CONFIG_ENV_PREFIX) :]
        parts = config_key.split("__")

        if len(parts) < 2:
            logger.warning(
                f"Invalid config env var format: {env_name}. "
                f"Expected FINECODE_CONFIG_<ACTION>__<PARAM> or "
                f"FINECODE_CONFIG_<ACTION>__<HANDLER>__<PARAM>"
            )
            continue

        # Convert to lowercase for matching
        action_name = parts[0].lower()

        if len(parts) == 2:
            # Action-level config: FINECODE_CONFIG_ACTION__PARAM
            handler_name = ""  # empty means all handlers
            param_name = parts[1].lower()
        else:
            # Handler-specific config: FINECODE_CONFIG_ACTION__HANDLER__PARAM
            handler_name = parts[1].lower()
            param_name = "__".join(parts[2:]).lower()

        if action_name not in config_overrides:
            config_overrides[action_name] = {}
        if handler_name not in config_overrides[action_name]:
            config_overrides[action_name][handler_name] = {}

        try:
            parsed_value = json.loads(env_value)
        except json.JSONDecodeError as e:
            raise ConfigurationError(
                f"Failed to parse JSON value for env var '{env_name}': {env_value!r}"
            ) from e

        config_overrides[action_name][handler_name][param_name] = parsed_value

    return config_overrides


def parse_handler_config_from_cli(
    config_args: list[str], actions: list[str]
) -> dict[str, dict[str, dict[str, str]]]:
    """
    Parse handler config overrides from CLI arguments.

    Format:
    - --config.<param>=value
      -> action-level config for all handlers of all specified actions
    - --config.<handler>.<param>=value
      -> handler-specific config for all specified actions

    Returns nested dict: {action_name: {handler_name_or_empty: {param: value}}}
    Empty string key "" means action-level (applies to all handlers).
    """
    config_overrides: dict[str, dict[str, dict[str, str]]] = {}

    for arg in config_args:
        if not arg.startswith("--config."):
            continue

        if "=" not in arg:
            logger.warning(
                f"Invalid config CLI arg format: {arg}. "
                f"Expected --config.<param>=value or --config.<handler>.<param>=value"
            )
            continue

        # Remove --config. prefix and split by =
        config_part = arg[len("--config.") :]
        key_part, raw_value = config_part.split("=", 1)
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            # fallback for literal string, all other types can be parsed by json.loads
            value = raw_value

        # Split by . to determine if it's action-level or handler-specific
        parts = key_part.split(".")

        if len(parts) == 1:
            # Action-level config: --config.<param>=value
            handler_name = ""  # empty means all handlers
            param_name = parts[0].lower().replace("-", "_")
        else:
            # Handler-specific config: --config.<handler>.<param>=value
            handler_name = parts[0].lower().replace("-", "_")
            param_name = ".".join(parts[1:]).lower().replace("-", "_")

        # Apply to all specified actions
        for action_name in actions:
            action_name_lower = action_name.lower()
            if action_name_lower not in config_overrides:
                config_overrides[action_name_lower] = {}
            if handler_name not in config_overrides[action_name_lower]:
                config_overrides[action_name_lower][handler_name] = {}

            config_overrides[action_name_lower][handler_name][param_name] = value

    return config_overrides


def merge_config_overrides(
    env_overrides: dict[str, dict[str, dict[str, str]]],
    cli_overrides: dict[str, dict[str, dict[str, str]]],
) -> dict[str, dict[str, dict[str, str]]]:
    """
    Merge env var and CLI config overrides. CLI takes precedence.
    """
    merged = {}

    # Copy env overrides
    for action, handlers in env_overrides.items():
        merged[action] = {}
        for handler, params in handlers.items():
            merged[action][handler] = dict(params)

    # Merge CLI overrides (takes precedence)
    for action, handlers in cli_overrides.items():
        if action not in merged:
            merged[action] = {}
        for handler, params in handlers.items():
            if handler not in merged[action]:
                merged[action][handler] = {}
            merged[action][handler].update(params)

    return merged


@click.group()
def cli(): ...


@cli.command()
@click.option("--trace", "trace", is_flag=True, default=False)
@click.option("--debug", "debug", is_flag=True, default=False)
@click.option(
    "--socket", "tcp", default=None, type=int, help="start a TCP server"
)  # is_flag=True,
@click.option("--ws", "ws", is_flag=True, default=False, help="start a WS server")
@click.option(
    "--stdio", "stdio", is_flag=True, default=False, help="Use stdio communication"
)
@click.option("--host", "host", default=None, help="Host for TCP and WS server")
@click.option(
    "--port", "port", default=None, type=int, help="Port for TCP and WS server"
)
def start_lsp(
    trace: bool,
    debug: bool,
    tcp: int | None,
    ws: bool,
    stdio: bool,
    host: str | None,
    port: int | None,
):
    if debug is True:
        import debugpy

        try:
            debugpy.listen(5680)
            debugpy.wait_for_client()
        except Exception as e:
            logger.info(e)

    if tcp is not None:
        comm_type = communication_utils.CommunicationType.TCP
        port = tcp
        host = "127.0.0.1"
    elif ws is True:
        comm_type = communication_utils.CommunicationType.WS
    elif stdio is True:
        comm_type = communication_utils.CommunicationType.STDIO
    else:
        raise ValueError("Specify either --tcp, --ws or --stdio")

    asyncio.run(
        wm_lsp_server.start(comm_type=comm_type, host=host, port=port, trace=trace)
    )


async def show_user_message(message: str, message_type: str) -> None:
    # user messages in CLI are not needed because CLI outputs own messages
    ...


def deserialize_action_payload(raw_payload: dict[str, str]) -> dict[str, typing.Any]:
    deserialized_payload = {}
    for key, value in raw_payload.items():
        if (value.startswith("{") and value.endswith("}")) or (value.startswith('[') and value.endswith(']')):
            try:
                deserialized_value = json.loads(value)
            except json.JSONDecodeError:
                deserialized_value = value
        else:
            deserialized_value = value
        deserialized_payload[key] = deserialized_value
    return deserialized_payload


@cli.command(context_settings=dict(ignore_unknown_options=True, allow_extra_args=True))
@click.pass_context
def run(ctx) -> None:
    args: list[str] = ctx.args
    actions_to_run: list[str] = []
    projects: list[str] | None = None
    workdir_path: pathlib.Path = pathlib.Path(os.getcwd())
    processed_args_count: int = 0
    concurrently: bool = False
    trace: bool = False
    no_env_config: bool = False
    save_results: bool = True
    map_payload_fields: set[str] = set()
    shared_server: bool = False

    # finecode run parameters
    for arg in args:
        if arg.startswith("--workdir"):
            provided_workdir = arg.removeprefix("--workdir=")
            provided_workdir_path = pathlib.Path(provided_workdir).resolve()
            if not provided_workdir_path.exists():
                click.echo(
                    f"Provided workdir '{provided_workdir}' doesn't exist", err=True
                )
                sys.exit(1)
            else:
                workdir_path = provided_workdir_path
        elif arg.startswith("--project"):
            if projects is None:
                projects = []
            project = arg.removeprefix("--project=")
            projects.append(project)
        elif arg == "--concurrently":
            concurrently = True
        elif arg == "--trace":
            trace = True
        elif arg == "--no-env-config":
            no_env_config = True
        elif arg == "--no-save-results":
            save_results = False
        elif arg.startswith("--map-payload-fields"):
            fields = arg.removeprefix("--map-payload-fields=")
            map_payload_fields = {f.replace("-", "_") for f in fields.split(",")}
        elif arg == "--shared-server":
            shared_server = True
        elif not arg.startswith("--"):
            break
        processed_args_count += 1

    logger_utils.init_logger(log_name="cli", trace=trace, stdout=True)

    # Parse handler config from env vars
    handler_config_overrides: dict[str, dict[str, dict[str, str]]] = {}
    if not no_env_config:
        try:
            handler_config_overrides = parse_handler_config_from_env()
        except ConfigurationError as exception:
            click.echo(exception.message, err=True)
            sys.exit(1)

    # actions
    for arg in args[processed_args_count:]:
        if not arg.startswith("--"):
            actions_to_run.append(arg)
        else:
            break
        processed_args_count += 1

    if len(actions_to_run) == 0:
        click.echo("No actions to run", err=True)
        sys.exit(1)

    # action payload and config overrides
    action_payload: dict[str, typing.Any] = {}
    config_args: list[str] = []
    for arg in args[processed_args_count:]:
        if not arg.startswith("--"):
            click.echo(
                f"All action parameters should be named and have form '--<name>=<value>'. Wrong parameter: '{arg}'",
                err=True,
            )
            sys.exit(1)
        else:
            if "=" not in arg:
                click.echo(
                    f"All action parameters should be named and have form '--<name>=<value>'. Wrong parameter: '{arg}'",
                    err=True,
                )
                sys.exit(1)
            elif arg.startswith("--config."):
                config_args.append(arg)
            else:
                arg_name, arg_value = arg[2:].split("=", 1)
                arg_name = arg_name.replace("-", "_")
                action_payload[arg_name] = arg_value.strip('"').strip("'")
        processed_args_count += 1

    # Parse CLI config overrides and merge with env overrides
    if config_args:
        cli_config_overrides = parse_handler_config_from_cli(config_args, actions_to_run)
        if cli_config_overrides:
            logger.trace(f"Handler config overrides from CLI: {cli_config_overrides}")
            handler_config_overrides = merge_config_overrides(
                handler_config_overrides, cli_config_overrides
            )

    user_messages._notification_sender = show_user_message

    deserialized_payload = deserialize_action_payload(action_payload)
    try:
        result = asyncio.run(
            run_cmd.run_actions(
                workdir_path,
                projects,
                actions_to_run,
                deserialized_payload,
                concurrently,
                handler_config_overrides,
                save_results,
                map_payload_fields,
                own_server=not shared_server,
            )
        )
        click.echo(result.output)
        if save_results:
            results_dir = pathlib.Path(sys.executable).parent.parent / "cache" / "finecode" / "results"
            results_dir.mkdir(parents=True, exist_ok=True)
            for project_path, result_by_action in result.result_by_project.items():
                for action_name, action_result in result_by_action.items():
                    output_file = results_dir / f"{action_name}.json"
                    json_result: dict[str, typing.Any] = {}
                    if output_file.exists():
                        json_result = json.loads(output_file.read_text())
                    json_result[str(project_path)] = action_result.json()
                    output_file.write_text(json.dumps(json_result, indent=2))
        sys.exit(result.return_code)
    except run_cmd.RunFailed as exception:
        click.echo(exception.args[0], err=True)
        sys.exit(1)
    except Exception as exception:
        logger.exception(exception)
        click.echo("Unexpected error, see logs in file for more details", err=True)
        sys.exit(2)


@cli.command()
@click.option("--trace", "trace", is_flag=True, default=False)
@click.option("--debug", "debug", is_flag=True, default=False)
@click.option("--recreate", "recreate", is_flag=True, default=False)
@click.option("--shared-server", "shared_server", is_flag=True, default=False)
def prepare_envs(trace: bool, debug: bool, recreate: bool, shared_server: bool) -> None:
    """
    `prepare-envs` should be called from workspace/project root directory.
    """
    # idea: project parameter to allow to run from other directories?
    if debug is True:
        import debugpy

        try:
            debugpy.listen(5680)
            debugpy.wait_for_client()
        except Exception as e:
            logger.info(e)

    logger_utils.init_logger(log_name="cli", trace=trace, stdout=True)
    user_messages._notification_sender = show_user_message

    try:
        asyncio.run(
            prepare_envs_cmd.prepare_envs(
                workdir_path=pathlib.Path(os.getcwd()),
                recreate=recreate,
                own_server=not shared_server,
            )
        )
    except prepare_envs_cmd.PrepareEnvsFailed as exception:
        click.echo(exception.message, err=True)
        sys.exit(1)
    except Exception as exception:
        logger.exception(exception)
        click.echo("Unexpected error, see logs in file for more details", err=True)
        sys.exit(2)


@cli.command()
@click.option("--trace", "trace", is_flag=True, default=False)
@click.option("--debug", "debug", is_flag=True, default=False)
@click.option("--project", "project", type=str)
@click.option("--shared-server", "shared_server", is_flag=True, default=False)
def dump_config(trace: bool, debug: bool, project: str | None, shared_server: bool):
    if debug is True:
        import debugpy

        try:
            debugpy.listen(5680)
            debugpy.wait_for_client()
        except Exception as e:
            logger.info(e)

    if project is None:
        click.echo("--project parameter is required", err=True)
        return

    logger_utils.init_logger(log_name="cli", trace=trace, stdout=True)
    user_messages._notification_sender = show_user_message

    try:
        asyncio.run(
            dump_config_cmd.dump_config(
                workdir_path=pathlib.Path(os.getcwd()),
                project_name=project,
                own_server=not shared_server,
            )
        )
    except dump_config_cmd.DumpFailed as exception:
        click.echo(exception.message, err=True)
        sys.exit(1)


@cli.command()
@click.option("--workdir", "workdir", default=None, type=str, help="Workspace root directory")
@click.option("--trace", "trace", is_flag=True, default=False)
def start_mcp(workdir: str | None, trace: bool):
    """Start the FineCode MCP server (stdio). Connects to a running FineCode API server."""
    from finecode import mcp_server

    logger_utils.init_logger(log_name="mcp_server", trace=trace, stdout=False)
    workdir_path = pathlib.Path(workdir) if workdir else pathlib.Path(os.getcwd())
    mcp_server.start(workdir_path)


@cli.command()
@click.option("--trace", "trace", is_flag=True, default=False)
@click.option(
    "--port-file",
    "port_file",
    default=None,
    type=str,
    help="Write the listening port to this file instead of the shared discovery file. "
         "Used by dedicated instances started without --shared-server.",
)
@click.option(
    "--disconnect-timeout",
    "disconnect_timeout",
    default=30,
    type=int,
    show_default=True,
    help="Seconds to wait after the last client disconnects before shutting down.",
)
def start_api_server(trace: bool, port_file: str | None, disconnect_timeout: int):
    """Start the FineCode API server standalone (TCP JSON-RPC). Auto-stops when all clients disconnect."""
    from finecode.api_server import api_server

    logger_utils.init_logger(log_name="api_server", trace=trace, stdout=False)
    port_file_path = pathlib.Path(port_file) if port_file else None
    asyncio.run(api_server.start_standalone(port_file=port_file_path, disconnect_timeout=disconnect_timeout))


if __name__ == "__main__":
    cli()
