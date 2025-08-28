"""
Here we don't validate file, we extract only valid data. Linter is responsible for
validation but we need to be aware of invalid data.
"""

import ast
import enum

from finecode_extension_api import common_types


def find_exported_members_names(module_ast: ast.Module) -> list[str] | None:
    # if returns None, then there are no explicit exports
    exports_found: bool = False
    exported_members_names: list[str] = []

    # '__all__' is usually at the end of file, iterate from the end
    for stmt in module_ast.body[::-1]:
        if isinstance(stmt, ast.Assign):
            assign_target = stmt.targets[0]
            if isinstance(assign_target, ast.Name) and assign_target.id == "__all__":
                exports_found = True
                rvalue = stmt.value
                if isinstance(rvalue, ast.List):
                    exported_members_names = [
                        item.value
                        for item in rvalue.elts
                        if isinstance(item, ast.Constant)
                        and isinstance(item.value, str)
                    ]
                break

    if not exports_found:
        return None
    return exported_members_names


class ModuleMemberAccessType(enum.Enum):
    PUBLIC = enum.auto()
    PRIVATE = enum.auto()
    # some statements like imports have no access type currently
    UNKNOWN = enum.auto()


class PositionRelativeRange(enum.Enum):
    BEFORE = enum.auto()
    INSIDE = enum.auto()
    AFTER = enum.auto()


def get_stmt_position_relative_range(
    stmt: ast.stmt, range_in_doc: common_types.Range
) -> PositionRelativeRange:
    # check start position of statement relative to range. Note that end of statement
    # if it is multiline can be in range even with PositionRelativeRange.BEFORE.
    if stmt.lineno < range_in_doc.start.line:
        return PositionRelativeRange.BEFORE
    elif stmt.lineno > range_in_doc.end.line:
        return PositionRelativeRange.AFTER
    else:
        # if statement is at the first line of at the last line of range, check also
        # column
        if stmt.lineno == range_in_doc.start.line:
            if stmt.col_offset < range_in_doc.start.character:
                return PositionRelativeRange.BEFORE
            else:
                return PositionRelativeRange.INSIDE
        elif stmt.lineno == range_in_doc.end.line:
            if stmt.col_offset > range_in_doc.end.character:
                return PositionRelativeRange.BEFORE
            else:
                return PositionRelativeRange.INSIDE
        else:
            return PositionRelativeRange.INSIDE


def get_module_members_with_access_type(
    module_ast: ast.Module,
    exported_members_names: list[str] | None,
    range_in_doc: common_types.Range | None,
) -> dict[ast.stmt, ModuleMemberAccessType]:
    module_members_with_access_type: dict[ast.stmt, ModuleMemberAccessType] = {}
    default_stmt_access_type: ModuleMemberAccessType = (
        ModuleMemberAccessType.PRIVATE
        if exported_members_names is not None
        else ModuleMemberAccessType.PUBLIC
    )

    for stmt in module_ast.body:
        stmt_name: str = ""

        if range_in_doc is not None:
            relative_position: PositionRelativeRange = get_stmt_position_relative_range(
                stmt, range_in_doc
            )
            if relative_position == PositionRelativeRange.BEFORE:
                continue
            elif relative_position == PositionRelativeRange.AFTER:
                break

        match stmt:
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                stmt_name = stmt.name
            # TODO: assignment
            # TODO: import? <- possible improvement in future
            case _:
                continue

        stmt_access_type: ModuleMemberAccessType = default_stmt_access_type
        if exported_members_names is not None:
            stmt_access_type = (
                ModuleMemberAccessType.PUBLIC
                if stmt_name in exported_members_names
                else ModuleMemberAccessType.PRIVATE
            )
        module_members_with_access_type[stmt] = stmt_access_type

    return module_members_with_access_type
