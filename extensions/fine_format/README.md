# Cross-language FineCode preset for formatting

`fine_format` owns the inter-language `format`, `format_files`, and `format_file`
action contracts and ships their canonical registration. Language-specific
formatting presets (e.g. `fine_python_format`) compose this preset and add their
own language subactions and handlers on top.

See [ADR-0036](../../docs/adr/0036-feature-presets-own-their-action-contracts.md)
for the rationale.
