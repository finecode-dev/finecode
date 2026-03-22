import collections.abc
import typing
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iactionrunner

from finecode_extension_runner import domain, run_utils


class ActionRunner(iactionrunner.IActionRunner):
    def __init__(self, run_action_func: typing.Callable[[domain.ActionDeclaration, code_action.RunActionPayload, code_action.RunActionMeta], collections.abc.Coroutine[None, None, code_action.RunActionResult]],
                 actions_getter: typing.Callable[[], dict[str, domain.ActionDeclaration]]):
        self._run_action_func = run_action_func
        self._actions_getter = actions_getter
        self._source_cls_cache: dict[str, type] = {}

    def _get_cls(self, source: str) -> type:
        # TODO: reset cache on ER config update?
        cls = self._source_cls_cache.get(source)
        if cls is None:
            cls = run_utils.import_module_member_by_source_str(source)
            self._source_cls_cache[source] = cls
        return cls

    @typing.override
    async def run_action(
        self, action: iactionrunner.ActionDeclaration[iactionrunner.ActionT], payload: code_action.RunActionPayload, meta: code_action.RunActionMeta
    ) -> code_action.RunActionResult:
        try:
            return await self._run_action_func(action, payload, meta)
        except Exception as exception:
            raise iactionrunner.ActionRunFailed(str(exception)) from exception

    @typing.override
    def get_actions_names(self) -> list[str]:
        return list(self._actions_getter().keys())

    @typing.override
    def get_action_by_source(self, action_type: type[iactionrunner.ActionT]) -> iactionrunner.ActionDeclaration[iactionrunner.ActionT]:
        for action in self._actions_getter().values():
            try:
                cls = self._get_cls(action.source)
            except Exception:
                continue
            if cls is action_type:
                return action
        raise iactionrunner.ActionNotFound(f"Action '{action_type.__name__}' not found")

    @typing.override
    def get_action_by_name(self, name: str, action_type: type[iactionrunner.ActionT]) -> iactionrunner.ActionDeclaration[iactionrunner.ActionT]:
        actions = self._actions_getter()
        if name not in actions:
            raise iactionrunner.ActionNotFound(f"Action '{name}' not found")
        return actions[name]

    @typing.override
    def get_actions_for_parent(
        self, parent_action_type: type[iactionrunner.ActionT]
    ) -> dict[str, iactionrunner.ActionDeclaration[iactionrunner.ActionT]]:
        result: dict[str, iactionrunner.ActionDeclaration[iactionrunner.ActionT]] = {}
        for action in self._actions_getter().values():
            try:
                cls = self._get_cls(action.source)
            except Exception:
                continue
            if getattr(cls, "PARENT_ACTION", None) is parent_action_type:
                lang = getattr(cls, "LANGUAGE", None)
                if lang is not None:
                    result[lang] = action
        return result
