import click

from finecode.cli_app.cli import (
    bootstrap,
    dump_config,
    prepare_envs,
    reload_action,
    reload_config,
    restart_runner,
    restart_wm,
    run,
    stop_wm,
    version,
)
from finecode.lsp_server.cli import start_lsp
from finecode.mcp_server.cli import start_mcp
from finecode.wm_server.cli import start_wm_server


@click.group()
def cli(): ...


cli.add_command(run)
cli.add_command(prepare_envs)
cli.add_command(bootstrap)
cli.add_command(dump_config)
cli.add_command(start_lsp)
cli.add_command(start_wm_server)
cli.add_command(start_mcp)
cli.add_command(reload_action)
cli.add_command(restart_runner)
cli.add_command(reload_config)
cli.add_command(restart_wm)
cli.add_command(stop_wm)
cli.add_command(version)


if __name__ == "__main__":
    cli()
