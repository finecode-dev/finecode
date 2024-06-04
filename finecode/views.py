from __future__ import annotations

import abc
from typing import (TYPE_CHECKING, Generic, Protocol, Type, TypeVar,
                    runtime_checkable)

if TYPE_CHECKING:
    from finecode import workspace_context


class BaseEntity(abc.ABC):
    # label for tree view
    label: str


EntityTypeVar = TypeVar("EntityTypeVar")


class BaseManager(abc.ABC, Generic[EntityTypeVar]):
    def get_list(
        self,
        parent: BaseEntity | None,
        ws_context: workspace_context.WorkspaceContext,
    ) -> list[EntityTypeVar]:
        raise NotImplementedError()


@runtime_checkable
class BaseView(Protocol):
    ROOT_ENTITY: Type[BaseEntity]
    MANAGERS: dict[Type[BaseEntity], Type[BaseManager]]
