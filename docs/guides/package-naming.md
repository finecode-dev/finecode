# Package Naming

FineCode package names identify both what a package targets and what role it plays. Use these conventions for public extension and preset packages.

## Extensions

Extension package names follow the pattern `fine_<lang>_<qualifier>`, where `<qualifier>` is either:

- the **tool name** for extensions that wrap a specific tool (`fine_python_ruff`, `fine_toml_tombi`), or
- a **capability descriptor** for infrastructure extensions that provide shared functionality (`fine_python_package_info`, `fine_toml_lang`).

The role-word vocabulary (`recommended`, `format`, `lint`, `test`, ...) is off-limits as an extension qualifier. Those words signal preset roles and would mislead readers if attached to an extension. Use a tool name or capability descriptor instead.

## Presets

Preset package names follow the pattern `fine_<lang?>_<role>`, where `<role>` is a **role word** and the language segment is optional:

- **Language-specific** preset: `fine_<lang>_<role>` (e.g. `fine_python_format`, `fine_python_lint`, `fine_toml_recommended`). Configures a role for one language.
- **Cross-language** preset: `fine_<role>` (e.g. `fine_format`, `fine_lint`, `fine_test`, `fine_recommended`). Holds registrations for inter-language actions whose contract is language-agnostic (e.g. `format_file`, `lint`).

A language-specific preset typically depends on and composes the matching cross-language preset, so `fine_python_format` activates `fine_format` and adds Python-specific handlers on top. Place a registration in the cross-language preset only if the action itself is inter-language; language-specific subactions and handlers belong in the `fine_<lang>_<role>` preset.

The bare language name `fine_<lang>` is **reserved** for a base preset that provides the minimal configuration for a language without committing to a specific toolchain (e.g. `fine_toml` could enable TOML language detection without prescribing a formatter). If no such base preset exists yet, the name stays unoccupied. It must never be used as an extension package name.

## Role Words

Role words double as suffixes for language-specific presets and as bare-slot names for cross-language presets. Role words may be compound (`code_hierarchy`, `symbol_info`).

The following table lists known role words for reference:

| Role word | Language-specific | Cross-language | Meaning |
| --- | --- | --- | --- |
| `recommended` | `fine_python_recommended` | `fine_recommended` | Opinionated default toolset |
| `format` | `fine_python_format` | `fine_format` | Formatting-only preset |
| `lint` | `fine_python_lint` | `fine_lint` | Linting-only preset |
| `test` | `fine_python_test` | `fine_test` | Test runner preset |
| `code_hierarchy` | `fine_python_code_hierarchy` | `fine_code_hierarchy` | Call hierarchy and type hierarchy navigation |
| `symbol_info` | `fine_python_symbol_info` | `fine_symbol_info` | Point queries about a symbol at cursor: hover, definition, references |

The `fine_<word>` slot is overloaded: `<word>` is read as a role word when it matches the table above, and as a language name otherwise. Role words and language names do not collide in practice, so the meaning is unambiguous from the name alone.

## Choosing a role word

Name presets after the **semantic domain** they cover, not the query mechanism or access pattern.

- **Describe what information the preset provides**, not how you retrieve it. `fine_symbol_info` is better than `fine_code_lookup` because "lookup" describes the retrieval pattern, not the domain.
- **Avoid terms that overlap with adjacent families.** `fine_code_navigation` was rejected for hover/definition/references because hierarchy navigation (`fine_code_hierarchy`) is also navigation — the boundary disappears. `fine_symbol_info` is unambiguous: it covers information about a specific symbol at cursor, not tree traversal.
- **Be self-explanatory in a flat list.** A developer reading `fine_symbol_info` alongside `fine_format`, `fine_lint`, `fine_code_hierarchy` should immediately understand what each provides without opening its source.
- **Prefer concrete nouns over abstract ones.** `symbol_info` (concrete: symbols, information) is clearer than `code_intelligence` (abstract) or `language_features` (LSP-internal jargon).

This rule ensures that the package name alone unambiguously identifies whether a package is an extension or a preset, regardless of which directory it lives in. See [ADR-0026](../adr/0026-extension-and-preset-package-naming.md) for the rationale.
