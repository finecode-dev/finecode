"""Custom import-linter contract: domain-layer purity.

A domain-layer module (pure data types and exceptions, no infrastructure) may
only import the Python standard library and other modules that are
themselves part of the domain layer. Any other import — a third-party
package, or an internal module outside the domain layer — is a violation.

This is stricter than a ``forbidden`` contract: rather than enumerating the
specific packages a domain layer must not import (which needs updating every
time a new infrastructure dependency is added anywhere in the project), it
enumerates the small, stable set of modules the domain layer is *allowed* to
depend on, and rejects everything else automatically — including packages
nobody has thought to blocklist yet.
"""

from __future__ import annotations

import sys

from grimp import ImportGraph
from importlinter import Contract, ContractCheck, fields, output
from importlinter.application import contract_utils
from importlinter.application.contract_utils import AlertLevel
from importlinter.domain.helpers import module_expressions_to_modules


def _is_stdlib_module(module_name: str) -> bool:
    top_level = module_name.split(".", 1)[0]
    return top_level in sys.stdlib_module_names


class DomainPurityContract(Contract):
    """
    Domain purity contracts check that a set of "domain" modules only import
    each other and the standard library — never a third-party package or an
    internal module outside the domain set.

    Configuration options:
        - domain_modules: The modules that make up the domain layer. Each may
                           import the standard library and any other module
                           in this same list (including descendants of
                           listed packages), and nothing else.
        - ignore_imports:  A set of ImportExpressions for specific, accepted
                           exceptions — e.g. known pre-existing debt tracked
                           separately. (Optional.)
        - unmatched_ignore_imports_alerting: How to report an ignore_imports
                           expression that matches nothing. Default "error".
    """

    type_name = "domain_purity"

    domain_modules = fields.SetField(subfield=fields.ModuleExpressionField())
    ignore_imports = fields.SetField(subfield=fields.ImportExpressionField(), required=False)
    unmatched_ignore_imports_alerting = fields.EnumField(AlertLevel, default=AlertLevel.ERROR)

    def check(self, graph: ImportGraph, verbose: bool) -> ContractCheck:
        warnings = contract_utils.remove_ignored_imports(
            graph=graph,
            ignore_imports=self.ignore_imports,  # type: ignore[arg-type]
            unmatched_alerting=self.unmatched_ignore_imports_alerting,  # type: ignore[arg-type]
        )

        domain_modules = module_expressions_to_modules(
            graph,
            self.domain_modules,  # type: ignore[arg-type]
        )

        allowed_module_names: set[str] = set()
        for module in domain_modules:
            allowed_module_names.add(module.name)
            if not graph.is_module_squashed(module.name):
                allowed_module_names.update(graph.find_descendants(module.name))

        illegal_imports: list[dict] = []

        for module_name in sorted(allowed_module_names):
            for imported in sorted(graph.find_modules_directly_imported_by(module_name)):
                if imported in allowed_module_names or _is_stdlib_module(imported):
                    continue
                import_details = graph.get_import_details(
                    importer=module_name, imported=imported
                )
                illegal_imports.append(
                    {
                        "importer": module_name,
                        "imported": imported,
                        "line_numbers": tuple(
                            detail["line_number"] for detail in import_details
                        ),
                    }
                )

        return ContractCheck(
            kept=not illegal_imports,
            warnings=warnings,
            metadata={"illegal_imports": illegal_imports},
        )

    def render_broken_contract(self, check: ContractCheck) -> None:
        for illegal in check.metadata["illegal_imports"]:
            line_numbers = ", ".join(f"l.{n}" for n in illegal["line_numbers"])
            output.print_error(
                f"{illegal['importer']} is not allowed to import "
                f"{illegal['imported']} ({line_numbers}) — domain modules may"
                " only import the standard library and other domain modules.",
                bold=False,
            )
            output.new_line()
