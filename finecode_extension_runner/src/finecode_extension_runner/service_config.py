"""Resolution of a service binding's effective config.

A binding's config comes from up to three places, in increasing precedence:

1. ``raw_config`` passed by whoever registered the binding (an activator).
2. ``config`` on a ``[[tool.finecode.service]]`` entry naming the same interface.
3. An environment-variable override addressing the interface's derived alias.

Resolution happens here, in the Extension Runner, because only the ER can see
activator-registered bindings alongside declared ones -- so only the ER can tell
an override that addresses nothing from one that addresses a binding the
Workspace Manager never observed. See ADR-0070.
"""

import typing

from finecode_extension_runner.service_names import derive_service_name

__all__ = ["ServiceConfigResolver", "deep_merge_config"]


def deep_merge_config(
    target: dict[str, typing.Any], override: dict[str, typing.Any]
) -> None:
    """Deep-merge ``override`` into ``target`` in place.

    A shallow ``{**target, **override}`` would drop sibling keys under a nested
    table (other repositories alongside the one an override names), which is the
    entire reason the override format nests instead of flattening.
    """
    for key, value in override.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            deep_merge_config(target[key], value)
        else:
            target[key] = value


class ServiceConfigResolver:
    """Computes the effective config for each service binding as it registers.

    Interfaces are keyed by **type object**, not by the dotted path a
    declaration was written with: the same interface can be re-exported under
    several aliases, and importing them all yields one class.

    Override aliases are derived from the interface's class name, which is the
    part of the path the derivation uses anyway -- so an activator-registered
    binding, for which no declared path exists, resolves the same alias as a
    declared one.
    """

    def __init__(
        self,
        declared_config_by_interface: dict[type, dict[str, typing.Any]],
        overrides_by_name: dict[str, dict[str, typing.Any]],
    ) -> None:
        self._declared = declared_config_by_interface
        self._overrides = overrides_by_name
        self._interfaces_by_name: dict[str, list[type]] = {}
        self._matched_names: set[str] = set()

    def resolve(
        self, interface: type, raw_config: dict[str, typing.Any] | None
    ) -> dict[str, typing.Any] | None:
        name = derive_service_name(interface.__name__)
        seen = self._interfaces_by_name.setdefault(name, [])
        if interface not in seen:
            seen.append(interface)

        declared = self._declared.get(interface)
        override = self._overrides.get(name)
        if override is not None:
            self._matched_names.add(name)

        if declared is None and override is None:
            return raw_config

        effective: dict[str, typing.Any] = {}
        for layer in (raw_config, declared, override):
            if layer:
                deep_merge_config(effective, layer)
        return effective

    def unmatched_names(self) -> list[str]:
        """Override names that have not addressed any binding registered so far."""
        return sorted(set(self._overrides) - self._matched_names)

    def ambiguous_names(self) -> dict[str, list[type]]:
        """Override names claimed by more than one interface.

        Only names an override actually addresses are reported: two interfaces
        sharing a class name are harmless until something tries to configure one
        of them (ADR-0070).
        """
        return {
            name: interfaces
            for name, interfaces in self._interfaces_by_name.items()
            if len(interfaces) > 1 and name in self._overrides
        }
