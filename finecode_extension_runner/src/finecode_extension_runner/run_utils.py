import importlib

from loguru import logger


def import_module_member_by_source_str(source: str):
    member_name = source.split(".")[-1]
    module_path = ".".join(source.split(".")[:-1])

    # TODO: handle errors
    module = importlib.import_module(module_path)
    try:
        member = module.__dict__[member_name]
        return member
    except KeyError:
        logger.error(f"Member {member_name} not found in module {module_path}")
        raise ModuleNotFoundError()
