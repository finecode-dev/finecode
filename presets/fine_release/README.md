# fine_release

Workspace-wide action that releases every publishable package whose declared
version is absent from its registry, in dependency order, with a per-package
git tag (ADR-0060, ADR-0062). See PRD-0006.
