import typing
import tomlkit


def dump_config(config: dict[str, typing.Any]) -> str:
    return tomlkit.dumps(config)
