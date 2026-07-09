# Why FineCode's Action Model Works

## The problems with ad-hoc tool integration

Developer tooling has a recurring structural problem. Every time a team adopts a new tool — a linter, a formatter, a dependency manager — someone has to write glue code. CI scripts, IDE plugins, configuration files, output parsers. And every time a team *switches* a tool, much of that glue is thrown away and rewritten.

Three symptoms keep showing up:

**Tool lock-in.** Switching from pylint to ruff should be a one-line config change. In practice, it often means updating CI pipelines, IDE settings, pre-commit hooks, and custom scripts — because each integration was written against a specific tool rather than against the *operation* of linting.

**Language silos.** A team adds Kotlin to a Java project. The Java side has a mature toolchain: linting, formatting, dependency locking, all wired up. The Kotlin side needs its own, separate wiring — even though the operations are structurally identical. "Lint these files and report diagnostics" is the same operation in any language. The tools differ; the shape of the work does not.

**All-or-nothing results.** A CI job lints 500 files and reports results only after the last file finishes. An IDE, meanwhile, wants diagnostics file by file as they become available. Supporting both modes typically means maintaining two separate integrations — or accepting that one of them will be suboptimal.

These are symptoms of a missing abstraction. FineCode's Action Model is that abstraction.

## Actions are typed contracts

The central idea is simple: separate *what* an operation does from *how* it does it.

An **Action** is a typed declaration — a contract — defined by three types:

- **Payload** — the input parameters (e.g., which files to lint)
- **Context** — mutable state for the duration of one execution
- **Result** — the output (e.g., a list of diagnostics)

In Python, this is just a class with type parameters:

```python
class LintFilesAction(Action[LintFilesRunPayload, LintFilesRunContext, LintFilesRunResult]):
    PAYLOAD_TYPE = LintFilesRunPayload
    RUN_CONTEXT_TYPE = LintFilesRunContext
    RESULT_TYPE = LintFilesRunResult
```

An action contains no execution logic. It says: "linting files takes a list of file paths and produces a dictionary of diagnostics, keyed by file." That's it — the contract.

A **Handler** is an implementation of that contract. Multiple handlers can be registered for the same action. A ruff handler and a mypy handler both implement the lint action — they accept the same payload type and return the same result type. The caller doesn't know or care which handlers run. It invokes "lint" and gets diagnostics back.

This separation is what makes the rest possible. When the contract is stable, everything built on top of it — dispatch, composition, streaming — works regardless of which tools are plugged in underneath.

## How results compose

When multiple handlers implement the same action, their results need to be combined. FineCode makes each action define its own **merge strategy** — there is no implicit default.

Three strategies cover the common cases:

**Accumulation.** Two linters produce diagnostics for the same files. The results are merged by union — diagnostics from both linters appear in the final output. Neither overwrites the other.

```
ruff diagnostics:  {main.py: [W291, E501]}
mypy diagnostics:  {main.py: [error: incompatible type]}
merged:            {main.py: [W291, E501, error: incompatible type]}
```

**Replacement.** A build action produces a single output artifact. If a second handler runs (perhaps a post-processor), its result replaces the previous one. Only the final output matters.

**Pipeline.** A formatter modifies source files, then a subsequent handler writes the changes to disk. Order matters — writing must follow formatting. Each handler can read the accumulated result so far and build on it.

The important property: merge strategies are **associative**. Merging result A with result B, then with C, gives the same answer as merging A with the result of merging B and C. This means the execution engine can safely group and reorder intermediate merges without affecting the outcome.

In practical terms: when an action's merge is associative and handlers don't depend on each other's output (like independent linters), FineCode can run them **in parallel or sequentially — same result either way**. The framework makes this decision transparently. Handlers don't need to know.

This safety does not come from analysing handler code. A result type defines its own merge by implementing `update()`, and the framework cannot verify that an arbitrary `update()` is associative — so it does not try. Instead, these three strategies are the **recommended, well-understood patterns** an `update()` should follow: accumulation and replacement are associative, so the engine is free to reorder handlers and schedule them concurrently; pipeline is order-dependent and runs sequentially. Choosing an associative merge is the action designer's responsibility, and the payoff is direct — it is exactly what lets the engine treat the handlers as parallelizable.

## Three levels of specificity: why swapping tools is painless

This is the design decision that has the most direct impact on day-to-day configuration.

FineCode separates parameters into three levels:

| Level | What it carries | Who changes it |
|---|---|---|
| **Generic action** | Cross-language concepts | Almost never changes |
| **Language-specific subaction** | Ecosystem parameters | Changes when your ecosystem needs change |
| **Handler configuration** | Tool-specific parameters | Changes when you swap tools |

Consider dependency locking. The generic action carries parameters meaningful in any ecosystem: which artifact to lock, where to write the lock file. The Python-specific subaction adds parameters meaningful across *all Python locking tools*: target Python version and target platform. The handler configuration carries parameters specific to *one tool*: pip-compile's `generate_hashes` flag, or uv's `resolution` strategy.

```
lock_dependencies                    ← generic: src_artifact_def_path, output_dir
  └─ lock_python_dependencies        ← ecosystem: + target_python_version, target_platform
       ├─ pip-compile handler config  ← tool: generate_hashes, allow_unsafe
       └─ uv handler config          ← tool: resolution strategy
```

**Switching from pip-compile to uv** means changing the handler and its config. The ecosystem parameters (`target_python_version`, `target_platform`) stay exactly where they are — they are properties of the *target environment*, not the *tool*. One line in `pyproject.toml` changes:

```toml
# Before
[[tool.finecode.action.lock_python_dependencies.handlers]]
name = "pip_compile"
source = "fine_python_pip.PipCompileLockHandler"
env = "dev_workspace"
config.generate_hashes = true

# After
[[tool.finecode.action.lock_python_dependencies.handlers]]
name = "uv"
source = "fine_python_uv.UvLockHandler"
env = "dev_workspace"
config.resolution = "highest"
```

The `target_python_version` and `target_platform` in the payload? Untouched. The dispatch handler that routes `lock_dependencies` to the right language? Untouched. Everything above the handler level is stable.

### Adding a new language

Supporting a new language follows the same principle. Say your workspace adds a Node.js project. You register a subaction:

```python
class LockNodeDependenciesAction(Action[...]):
    LANGUAGE = "node"
    PARENT_ACTION = LockDependenciesAction
```

The dispatch handler discovers it automatically — it looks up subactions by metadata (`LANGUAGE` + `PARENT_ACTION`), not by hardcoded names or string patterns. No existing code changes. No dispatch tables to update.

```
                    lock_dependencies
                    ┌───────┴───────┐
          detect language         detect language
                │                       │
    lock_python_dependencies    lock_node_dependencies
         │                              │
    pylock.toml                  package-lock.json
```

This **open extension** property — new languages without modifying existing dispatch logic — is what makes FineCode scale to mixed-language workspaces without growing configuration complexity.

## Incremental results

When a payload is decomposable into independent items — like a list of files to lint — FineCode's execution engine can process items individually and deliver results as they complete.

The lint handler doesn't need to implement streaming. It receives a list of file URIs, and the runner handles decomposition:

1. The runner breaks the file list into individual items
2. Each file is linted independently (potentially in parallel)
3. Per-file results are sent to the consumer as they finish — via LSP `$/progress` notifications to the IDE, for instance
4. The full merged result is still accumulated for the final response

The same handler code works whether results are streamed to an IDE in real time or collected into a single CI report. The handler doesn't know and doesn't care. Streaming is a property of the *execution environment*, not the handler.

This is only possible because the merge strategy is defined on the result type, not in the handler. The runner knows how to combine partial results because the action's contract specifies it.

## Caching is the handler's responsibility

Incremental results (above) are about *delivering* work as it completes. **Caching** is about *skipping* work that does not need to be redone — relinting a file only when its contents changed, for example.

Typed build systems make incrementality a framework-level service: every value flows through one uniform model, so the engine can content-address inputs and outputs and decide centrally what to rebuild. FineCode deliberately does **not** do this. An action's `Result` is arbitrarily typed and semantically general — a list of diagnostics, a formatted file, a lock file, a dependency graph — so there is no universal content-addressing the framework could apply across all of them.

Instead, FineCode makes incremental execution the responsibility of individual handlers. Each handler decides whether a cached result lets it skip recomputation. This is a deliberate trade-off: FineCode gives up framework-level global incrementality in exchange for the semantic generality that makes multi-handler actions and declared merges possible in the first place.

To support this, the framework provides a caching *service* (`ICache`) rather than a caching *mechanism*. `ICache` is a convenience, not the only path — because caching is just a handler concern served through dependency injection, a handler may bind a different `ICache` implementation (for example a persistent, on-disk cache that survives restarts — a planned FineCode direction), introduce an entirely different caching service of its own design, or lean on the underlying tool's native cache. Nothing in the framework privileges one approach.

The built-in `ICache` is file-versioned: a cached value is stored against the file version it was computed from and is silently treated as a miss once the file changes. A handler caches its own results under its own key and reads them back on the next run:

```python
class Flake8LintFilesHandler(...):
    CACHE_KEY = "flake8"

    async def run_on_single_file(self, file_uri):
        file_path = resource_uri_to_path(file_uri)
        try:
            # Cache hit — skip linting this file entirely.
            cached = await self.cache.get_file_cache(file_path, self.CACHE_KEY)
            return DiagnosticFilesRunResult(messages={file_uri: cached})
        except icache.CacheMissException:
            pass

        # Cache miss — do the real work, then cache it against the file version.
        messages = await self._lint(file_path)
        await self.cache.save_file_cache(
            file_path, file_version, self.CACHE_KEY, messages
        )
        return DiagnosticFilesRunResult(messages={file_uri: messages})
```

Because the cache key and the decision to consult it live in the handler, two handlers on the same action cache independently, a handler with no meaningful notion of reuse simply does not call the cache, and a handler is free to use a smarter or coarser invalidation rule than file version when its work warrants it. See [Caching with `ICache`](../reference/services.md#caching-with-icache) for the practical pattern.

## What this means in practice

The Action Model is not theoretical overhead — it's the reason FineCode can make these concrete guarantees:

- **Swap a tool without touching project configuration.** Handler config is the only thing that changes. Ecosystem parameters and dispatch logic are unaffected.
- **Add a language without modifying dispatch logic.** Register a subaction with the right metadata. The dispatch handler discovers it automatically.
- **Run handlers in parallel when safe, sequentially when not.** The framework decides based on the action's merge properties and handler configuration. Handlers don't coordinate with each other.
- **Stream partial results to IDEs automatically.** Actions with decomposable payloads get incremental delivery for free. Orchestrator actions can also stream by delegating to a subaction and yielding mapped results — the framework handles delivery in both cases.
- **Skip redundant work per handler, not per framework.** Caching is a handler-level decision served by `ICache`, not a global rebuilder. Handlers that benefit from reuse opt in; the framework imposes no universal content-addressing.
- **No implicit merge behavior.** Every action that supports multiple handlers must define how results combine. Silent data loss from undefined merges is impossible.

For practical guidance on designing new actions, see [Designing Actions](../guides/designing-actions.md).
