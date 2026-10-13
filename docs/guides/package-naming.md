# Package Naming

FineCode package names identify both what a package targets and what role it plays. Use these conventions for public extension packages, which are the distributable capability units.

## Extensions and presets share capability naming

Concrete integrations use `fine_<domain>_<qualifier>` (e.g. `fine_python_ruff`, `fine_toml_tombi`). Feature/composition packages use `fine_<domain?>_<role>` (e.g. `fine_format`, `fine_python_format`); existing `fine_format` stays valid. Open, non-colliding role words are preserved and bare base-language names stay reserved.

A name alone does not decide the capabilities a package supplies, and neither
directory nor resource presence classifies a package exclusively. The same
package may supply handlers and an explicitly selected preset resource.

## Extensions

Extension package names follow the pattern `fine_<domain>_<qualifier>`.

The **domain** is a language (`python`, `toml`) when the extension is language-scoped. Otherwise it names the preset family the extension supplies handlers for — `fine_docs_mkdocs` serves `fine_docs`, `fine_dep_graph_falkordb` serves `fine_dep_graph`, `fine_agent_pi` serves `fine_agent`. When an extension serves more than one family, name the one it primarily exists for, not the one it happened to serve first. `fine_agent_pi` also contributes a `setup_system` handler that installs the `pi` CLI, but installing is in service of running agent tasks, so `agent` is the domain. Its sibling `fine_agent_claude_code` is named on the same reading: it was `fine_system_claude_code` while installing the CLI was all it did, and became `fine_agent_claude_code` when it gained a `run_agent_task` handler — the domain follows what the package primarily exists for, so it moves when that changes.

The **qualifier** is either:

- the **tool name** for extensions that wrap a specific tool (`fine_python_ruff`, `fine_toml_tombi`, `fine_python_mypy`), or
- a **capability descriptor** for infrastructure extensions that provide shared functionality (`fine_python_package_info`, `fine_toml_lang`).

Prefer the tool name whenever the extension directly wraps an external tool. An extension always carries a qualifier.

Role words are off-limits as an extension **qualifier**. Those words signal preset roles and would mislead readers in that position. They are fine in the domain slot, where they name the family being served — that is what `fine_docs_mkdocs` does.

## Presets

Preset package names follow the pattern `fine_<domain?>_<role>`, where `<role>` is a **role word** and the domain segment is optional:

- **Domain-specific** preset: `fine_<domain>_<role>` (e.g. `fine_python_format`, `fine_python_lint`, `fine_toml_recommended`). Configures a role for one language or domain.
- **Cross-language** preset: `fine_<role>` (e.g. `fine_format`, `fine_lint`, `fine_test`, `fine_recommended`). Holds registrations for inter-language actions whose contract is language-agnostic (e.g. `format_file`, `lint`).

A language-specific preset typically depends on and composes the matching cross-language preset, so `fine_python_format` activates `fine_format` and adds Python-specific handlers on top. Place a registration in the cross-language preset only if the action itself is inter-language; language-specific subactions and handlers belong in the `fine_<lang>_<role>` preset.

The bare language name `fine_<lang>` is **reserved** for a base preset that provides the minimal configuration for a language without committing to a specific toolchain (e.g. `fine_toml` could enable TOML language detection without prescribing a formatter). If no such base preset exists yet, the name stays unoccupied. It must never be used as an extension package name.

## Role words

A role word names the semantic domain a preset covers. It doubles as a suffix for domain-specific presets and as the bare-slot name for cross-language ones, and may be compound (`code_hierarchy`, `symbol_info`, `git_hooks`).

**The vocabulary is open.** Coin a new role word when a new capability needs one — no registry to update, no ADR to amend. Follow the guidance below when choosing.

The `fine_<word>` slot is overloaded: `<word>` reads as a role word when it matches one, and as a language name otherwise. Role words and language names do not collide in practice, and keeping it that way is a constraint on new role words — reject one that collides with a language name.

## Choosing a role word

Name presets after the **semantic domain** they cover, not the query mechanism or access pattern.

- **Describe what information the preset provides**, not how you retrieve it. `fine_symbol_info` is better than `fine_code_lookup` because "lookup" describes the retrieval pattern, not the domain.
- **Avoid terms that overlap with adjacent families.** `fine_code_navigation` was rejected for hover/definition/references because hierarchy navigation (`fine_code_hierarchy`) is also navigation — the boundary disappears. `fine_symbol_info` is unambiguous: it covers information about a specific symbol at cursor, not tree traversal.
- **Be self-explanatory in a flat list.** A developer reading `fine_symbol_info` alongside `fine_format`, `fine_lint`, `fine_code_hierarchy` should immediately understand what each provides without opening its source.
- **Prefer concrete nouns over abstract ones.** `symbol_info` (concrete: symbols, information) is clearer than `code_intelligence` (abstract) or `language_features` (LSP-internal jargon).

Read the existing names in `presets/` before coining one — they are the working vocabulary, and the nearest neighbour usually shows whether a new word is warranted or an existing family should absorb the capability.

## Reading a package name

A bare `fine_<word>` reads as a feature/composition package, and a multi-segment name hints at its primary capability (`fine_docs_mkdocs` serves `fine_docs`; `fine_docs_markdown` supplies `fine_docs` configuration). The name alone never decides the full capability set: the same package may supply handlers and an explicitly selected preset resource.

See proposed [ADR-0115](../../finecode_internal_docs/adr/0115-extension-capabilities-share-terminology-and-repository-directory.md) for the umbrella terminology; [ADR-0081](../adr/0081-open-role-word-vocabulary-and-domain-first-segment-in-package-names.md) remains the historical governing context for role words, and [ADR-0026](../adr/0026-extension-and-preset-package-naming.md) for the superseded original convention.
