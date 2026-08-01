# Designing Services Rules

This page is the normative layer for **service** design — the long-lived,
DI-injected dependencies handlers request (see the [services reference](../reference/services.md)
for the ones that ship today). It is the services counterpart to
[Designing Actions Rules](designing-actions-rules.md): each rule is short,
numbered, and phrased so it can be referenced in reviews and gradually turned
into validation checks.

Use `MUST` for hard constraints and `SHOULD` for strong defaults.

Rule IDs use reserved numeric ranges by group so we can extend one area later
without renumbering the others:

- `S-100` to `S-199`: Service contract (the interface)
- `S-200` to `S-299`: Provisioning and configuration
- `S-300` to `S-399`: Registration and lifecycle

Services are a smaller surface than actions, and this rule set is deliberately
compact. It is the distilled form of [ADR-0038](../../../finecode_internal_docs/adr/0038-init-action-for-shared-handler-infrastructure-config.md),
[ADR-0056](../../../finecode_internal_docs/adr/0056-service-declarations-support-constructor-config-injection.md),
[ADR-0068](../../../finecode_internal_docs/adr/0068-service-provisioning-belongs-to-implementation-not-interface.md),
and [ADR-0070](../../../finecode_internal_docs/adr/0070-one-binding-per-service-interface-addressed-by-derived-name.md);
consult those for the full rationale.

## Service Contract

## S-100: One binding per interface — model plurality as types or as a keyed collection

A service interface MUST resolve to exactly one implementation instance per
Extension Runner. Two concurrent instances of one interface are not supported.
When several like-shaped things are needed, which mechanism applies depends on
where the *set* of them is known:

- **Known in code** — several implementations of one shape, each bound to a
  consumer that always wants that specific one (the per-tool LSP services). Give
  each its **own interface type**. The variant's identity (binary, arguments,
  protocol quirks) is code regardless, so a type carries it honestly and the
  consumer gets a statically checked injection.
- **Known only from config** — many like-shaped resources whose set comes from
  user configuration (package registries, endpoints, connections). Use **one
  service holding a name-keyed collection** (S-205), with consumers selecting by
  key at call time.

The second case is the one that looks like it needs multiple instances and does
not: the set is data, so no injection site can name a resource the user adds
later. See ADR-0070.

## S-101: A service interface declares only its consumer contract

A service interface MUST expose only the operations its consumers (handlers and
other services) call. Methods that exist solely to *populate* or *mutate* one
implementation's internal state — seed data, inject credentials, set a client —
MUST NOT appear on the interface.

Provisioning is what distinguishes one implementation from another (an in-memory
store is push-seeded; a Vault/keyring/env-var store is pull-based and has nothing
to set). Putting a write/seed method on the universal interface forces every
implementation to fake a capability that is not part of the abstraction. Keep
such methods on the concrete class and reach them by injecting the concrete type
(S-204). See ADR-0068.

## S-102: A service interface lives with its consumers until a second, unrelated consumer appears

A service interface SHOULD live in the extension package that defines the handler
group consuming it. Promote it to `finecode_extension_api/interfaces/` only when
multiple *unrelated* extensions need to consume it. Premature promotion turns a
local contract into a framework-wide commitment. See ADR-0038.

## S-103: A service exposes ready-to-use resources, not raw clients or config

A service SHOULD return the resource a handler actually needs (`get_graph() -> Graph`),
not a raw client the handler must then re-configure or re-select against. The
service owns connection/selection concerns so no consumer repeats them. See
ADR-0038 Implementation Notes.

## Provisioning And Configuration

## S-201: Static provisioning is service config, not per-handler config

Configuration shared across a handler group (connection parameters, registry
lists, seed data) MUST be supplied once as `[[tool.finecode.service]]` constructor
config (ADR-0056), not duplicated into each handler's config. The implementation
takes a typed `config` constructor parameter, structured from the declaration the
same way handler config is. See the [services reference](../reference/services.md#how-services-are-registered-and-resolved).

## S-202: Secrets resolve from a non-VCS config source; they are never committed

Secret service config (credentials, tokens) MUST resolve through the standard
[config-source chain](../configuration.md#where-configuration-lives) from a
non-VCS source — environment variables or a personal `finecode-user.toml`
(ADR-0040) — and MUST NOT be committed. Non-secret configuration (hosts, resource
definitions) MAY live in committed `pyproject.toml`/preset config. Split a
service's config along this line so the non-secret half stays discoverable and
the secret half stays out of version control.

## S-203: Init action vs. service config — real work vs. config carrier

How a service is initialized decides the mechanism:

- If initialization does **real work** — open a connection, probe reachability,
  fail fast with an actionable error — it MUST be an **init action** (ADR-0038).
  The action earns its keep as a health check.
- If initialization is a **pure config carrier** — copy static values into a
  store, no I/O, nothing to validate — it MUST be **service config** (ADR-0068).
  An init action that only carries config MUST NOT exist; it adds a required
  ordering step and a runtime round trip for no behavioral benefit.

## S-204: Dynamic runtime seeding stays an optional action bound to the concrete impl

When values genuinely must be supplied at run time (tokens fetched or rotated
mid-session — something static config cannot express), model it as an *optional*
action whose handler injects the **concrete** implementation and calls its
concrete write methods (S-101). The action's payload types stay universal; only
the handler is implementation-specific, so an implementation that cannot be
seeded this way simply registers no seeding handler. This is the same
action-universal / handler-specific split used for language handlers. See
ADR-0068.

## S-205: Service config that is overridden per entry MUST be keyed by name, not a list

Service config that individual entries need to override (per-repository
credentials, per-endpoint settings, ...) MUST be a table keyed by the entry's
name, not a list. A list forces overrides to address elements by index, which is
unreadable in an environment variable, breaks when the list is reordered, and
cannot be expressed in the flat env-var format at all. Use a table keyed by the
entry's name and let the implementation carry the key into the domain object —
`ConfigRepositoryCredentialsProvider`'s `config.credentials_by_repository` is the
model to follow (see [service config environment variables](../configuration.md#service-config-environment-variables)).

## S-206: A declaration is identified by `interface`; overrides address it by a derived alias

The `interface` path is a service declaration's identity — the merge key across
config layers, the DI binding target, and the term to use in diagnostics.

Config overrides address a declaration by a short **alias** instead, because an
interface path cannot be written readably in an environment variable. The alias
is always **derived** from the interface's final segment (a leading `I` before an
uppercase letter is stripped, the rest is snake-cased); it is never declared. Two
interfaces deriving the same alias is an error only when an override actually
addresses that alias.

Designers get one consequence from this: **the interface's class name is
user-facing**, since it is what appears in a CI environment variable. Name it for
the role it fills, and expect a rename to break env vars set elsewhere. See
ADR-0070 and [service names](../configuration.md#service-names).

## S-207: Config addresses a binding, not a declaration

Every service binding is configurable, whether it was bound by a
`[[tool.finecode.service]]` declaration or by an implementation package's
activator (S-301). Nothing has to be declared in order to be configured, and a
declaration MUST NOT be written solely to carry config for a binding an activator
already owns — restating `interface`/`source`/`env` pins a `source` that goes
stale when the implementation package changes its default, and is the same
pure-config-carrier pattern S-203 rejects for init actions.

Two consequences for service authors:

- TOML config may omit the binding. `source` and `env` are optional, so an entry
  carrying only `interface` and `config` layers onto whatever binding exists.
- Overrides are applied by the **Extension Runner**, not the WM, because only the
  ER can see activator-registered bindings. So an unknown or ambiguous override
  name is reported when a runner starts rather than at config collection.

See ADR-0070.

## Registration And Lifecycle

## S-301: Ship the default binding in the implementation's activator; reserve `[[tool.finecode.service]]` for overrides

A reusable service whose implementation is a separate, replaceable package SHOULD
ship its **default binding** in that package's own `finecode.activator`, picked up
via deferred activation on first request — no dependency coupling between
consumers and the implementation. `[[tool.finecode.service]]` declarations are for
**overrides**, where being explicit is the point. Configuring the service is not
such a case: per S-207 config attaches to the binding without restating it. See
the [services reference](../reference/services.md#where-to-register-a-reusable-service).

## S-302: State shared across handlers MUST be a singleton

If a writer and readers, or several handlers, must observe the *same* service
state, the service MUST be registered as a singleton. Otherwise the DI container
constructs a fresh instance per injection and writes are invisible to readers.
This is the load-bearing constraint whenever a concrete-injected writer (S-204)
must be seen through the interface by readers. See ADR-0038 and ADR-0068.

## S-303: A service that needs config MUST go through `register_impl`, not `register_instance`

Core services registered as ready instances at bootstrap take priority over
factories and therefore **cannot** be reconfigured by activators or config. A
service that needs to accept `[[tool.finecode.service]]` config MUST be registered
through the `register_impl` factory path (constructed lazily on first injection),
as `ICommandRunner` and `IRepositoryCredentialsProvider` are. See ADR-0056 and the
[services reference](../reference/services.md#precedence--what-overrides-what).

## S-304: A concrete-injected service MUST resolve to the same instance through both types

When a handler injects the **concrete** implementation (S-204) while others
inject the interface, the binding MUST make both resolve to the same instance —
otherwise the concrete-injected writer mutates a second object the readers never
see, which is S-302's failure in a form the type checker cannot catch.

The runner guarantees this: `register_impl` always alias-binds the concrete type
to the interface's instance, so nothing needs requesting. (`singleton` is still
accepted but decides nothing — it never controlled lifetime, since every resolved
service is cached for the runner's life regardless. It was opt-in until a
`[[tool.finecode.service]]` entry, which had no way to pass it, was found to
silently hand concrete-injecting handlers a second instance.)

The rule remains a design constraint even so: an implementation whose concrete
type is injected MUST be reachable as one instance, so do not work around it by
constructing the implementation yourself.

## Review checklist

- Does the interface contain any method that only one implementation could
  meaningfully support? (S-101)
- Do multiple like-shaped things come from code (→ types) or from config
  (→ keyed collection)? (S-100)
- Is initialization doing real work, or just carrying config? (S-203)
- Are secrets kept out of committed config? (S-202)
- Is per-entry config keyed by name rather than a list? (S-205)
- Is any declaration written only to carry config for an activator's binding? (S-207)
- If handlers share state, is the service a singleton? (S-302)
- Does a configurable service go through `register_impl`? (S-303)
- If a concrete type is injected anywhere, is it alias-bound to the interface's
  instance? (S-304)
