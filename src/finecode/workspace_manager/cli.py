import asyncio
import os
import pathlib
import sys

import click
from loguru import logger

import finecode.workspace_manager.main as workspace_manager
from finecode import communication_utils
from finecode.workspace_manager import logger_utils, user_messages
from finecode.workspace_manager.cli_app import run as run_cmd


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
def start_api(
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

    # loop = asyncio.get_event_loop()
    # loop.run_until_complete(
    #     workspace_manager.start(
    #         comm_type=comm_type, host=host, port=port, trace=trace
    #     )
    # )
    # loop.run_forever()

    # workspace manager doesn't stop with async start after closing LS client(IDE).
    # Use sync start until this problem is solved
    workspace_manager.start_sync(comm_type=comm_type, host=host, port=port, trace=trace)


async def show_user_message(
    message: str, message_type: str
) -> None:
    logger.log(message_type, message)


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
        elif not arg.startswith("--"):
            break
        processed_args_count += 1

    logger_utils.init_logger(trace=trace, stdout=True)

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

    # action payload
    action_payload: dict[str, str] = {}
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
            else:
                arg_name, arg_value = arg[2:].split("=")
                action_payload[arg_name] = arg_value
        processed_args_count += 1

    user_messages._notification_sender = show_user_message

    try:
        output, return_code = asyncio.run(
            run_cmd.run_actions(
                workdir_path, projects, actions_to_run, action_payload, concurrently
            )
        )
        click.echo(output)
        sys.exit(return_code)
    except run_cmd.RunFailed as exception:
        click.echo(exception.args[0], err=True)
        sys.exit(1)
    except Exception as exception:
        logger.exception(exception)
        click.echo("Unexpected error, see logs in file for more details", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
