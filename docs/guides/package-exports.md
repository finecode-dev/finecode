# Package Exports

This page is the single definition of what a FineCode package makes public.
Other guides apply it to their own steps and link here rather than restating
it.

The [general rules](#general-rules) apply to every package written for
FineCode: FineCode's own packages and extension and preset packages. The
[extension and preset rules](#extension-and-preset-rules) apply to extension
and preset packages only.

## General rules

1. **Module `__all__`.** Explicitly export public module members using
   `__all__`. It may not contain dynamic elements, only literal strings. The
   list names the module's supported interface, including external use.
   Pruning a list entry does not delete the definition. An existing module
   list that becomes empty stays as `__all__ = []`.

2. **Package facade.** A package's `__init__.py` imports only its intended
   public interface and declares a literal `__all__`. This is separate from
   rule 1: rule 1 is the supported interface of one module, this rule is what
   the package re-exports at its top level.

3. **Not grounds for export.** A missing leading underscore, the existence of
   a definition, an existing inventory, or an import-only test does not by
   itself make a name public.

4. **Python imports.** New code and examples import from an available
   supported facade, and otherwise from the established defining or support
   module.

5. **Submodule imports.** Importing `pkg.sub` runs `pkg/__init__.py` first,
   and a submodule is importable without a re-export.

6. **Initializer side effects.** Keep root-owned definitions and any
   initialization a package needs. Do not move or delete them to empty an
   initializer.

7. **Packages without an interface.** A new package may have an empty or
   docstring-only initializer. An existing initializer whose `__all__` empties
   keeps `__all__ = []`.

## Extension and preset rules

8. **Public roles.** Public roles are action classes with their payload and
   result types, handler classes, and service interfaces. Supporting run
   contexts, value types, exceptions, and enums are included where they form
   the consumed contract; not every support type is re-exported at the top
   level. These roles are exported without needing a local caller. A
   configuration-service interface is public by its role whatever its suffix.
   For example, `PyreflyConfig` is public and is named by its established
   path `fine_python_pyrefly.pyrefly_config.PyreflyConfig`.

9. **Not facade exports.** Handler config records and implementation-only
   service config records stay in their defining modules. Framework
   introspection does not need a package re-export.

10. **Service `interface` keys.** Service declarations merge by the literal
    `interface` string. Repeat the binding's established key; a shorter Python
    alias is not an interchangeable key.

11. **Activator entry points.** Declared as `module:attribute` under
    `[project.entry-points."finecode.activator"]`, for example
    `fine_python_ruff.activator:Activator`. They do not go through the facade,
    and the spelling is kept.
