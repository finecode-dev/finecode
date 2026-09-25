import collections.abc
import functools
import importlib.metadata
import json
import pathlib
import re
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

import ordered_set
from finecode_extension_api.interfaces import (  # idevenvinfoprovider,
    icache,
    idataclasscodec,
    iextensionrunnerinfoprovider,
    ifileeditor,
    ifilemanager,
    iknowledgestore,
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
    iuser_messenger,
    iuserprompt,
    iworkspaceactionregistry,
    iworkspaceactionrunner,
    iworkspaceinfoprovider,
)
from loguru import logger

from finecode_extension_runner import context, domain, process_slots, service_config
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.di.registry import Registry
from finecode_extension_runner.impls import (  # dev_env_info_provider,
    dataclass_codec,
    extension_runner_info_provider,
    file_editor,
    file_manager,
    inmemory_cache,
    knowledge_store,
    loguru_logger,
    project_action_runner,
    project_info_provider,
    service_registry,
    user_prompt,
    workspace_action_registry,
    workspace_action_runner,
    workspace_info_provider,
)
from finecode_extension_runner.impls import user_messenger as user_messenger_module
from finecode_extension_runner.run_utils import import_module_member_by_source_str

_COMMAND_RUNNER_INTERFACE = (
    "finecode_extension_api.interfaces.icommandrunner.ICommandRunner"
)
_COMMAND_RUNNER_DEFAULT_SOURCE = (
    "finecode_extension_runner.impls.command_runner.CommandRunner"
)

_REPOSITORY_CREDENTIALS_PROVIDER_INTERFACE = (
    "finecode_extension_api.interfaces.irepositorycredentialsprovider"
    ".IRepositoryCredentialsProvider"
)
_REPOSITORY_CREDENTIALS_PROVIDER_DEFAULT_SOURCE = (
    "finecode_extension_runner.impls.repository_credentials_provider"
    ".ConfigRepositoryCredentialsProvider"
)


class StaleEntryPointsError(Exception):
    """Raised when handler packages are installed but missing their activator entry points.

    This happens when a ``finecode.activator`` entry point is added to a package's
    ``pyproject.toml`` after it was already installed as an editable install.
    """

    def __init__(self, packages: set[str]) -> None:
        self.packages = packages
        super().__init__(
            f"Activator entry points missing for installed packages: "
            f"{', '.join(sorted(packages))}. "
            "Reinstall the env to register the entry points."
        )


def bootstrap(
    registry: Registry,
    runner_context: context.RunnerContext,
    project_def_path_getter: Callable[[], pathlib.Path],
    project_raw_config_getter: Callable[
        [str], collections.abc.Awaitable[dict[str, Any]]
    ],
    current_project_raw_config_version_getter: Callable[[], int],
    cache_dir_path_getter: Callable[[], pathlib.Path],
    actions_getter: Callable[[], dict[str, domain.ActionDeclaration]],
    current_env_name_getter: Callable[[], str],
    handler_packages: set[str],
    service_declarations: list,
    service_config_overrides: dict[str, dict[str, Any]] | None = None,
    workspace_packages_getter: Callable[
        [], collections.abc.Awaitable[dict[str, iprojectinfoprovider.WorkspacePackage]]
    ]
    | None = None,
    workspace_extra_selection_getter: Callable[
        [], collections.abc.Awaitable[dict[str, list[str]]]
    ]
    | None = None,
    send_request_to_wm: Callable[[str, dict], collections.abc.Awaitable[Any]]
    | None = None,
    send_user_message_notification: Callable[[str, str], None] | None = None,
):
    # logger_instance = loguru_logger.LoguruLogger()
    logger_instance = loguru_logger.get_logger()

    # dev_env_info_provider_instance = dev_env_info_provider.DevEnvInfoProvider(logger=logger_instance)
    file_manager_instance = file_manager.FileManager(
        logger=logger_instance,
    )
    file_editor_instance = file_editor.FileEditor(
        logger=logger_instance, file_manager=file_manager_instance
    )
    cache_instance = inmemory_cache.InMemoryCache(
        file_editor=file_editor_instance, logger=logger_instance
    )
    registry.register_instance(ilogger.ILogger, logger_instance)
    # One ER-lifetime gate shared by CommandRunner and ProcessExecutor
    # (ADR-0090). It must outlive every RunnerContext rebuild, so it is the
    # process-wide singleton, registered fresh into each new registry.
    registry.register_instance(
        process_slots.ProcessSlots, process_slots.get_process_slots()
    )
    _send_user_message = send_user_message_notification or (lambda msg, level: None)
    registry.register_instance(
        iuser_messenger.IUserMessenger,
        user_messenger_module.UserMessenger(send_notification=_send_user_message),
    )
    # Telling and asking are separate services (ADR-0082): the messenger above
    # broadcasts a string to every connected client and cannot fail, this one
    # addresses the run's originating client and returns what it said.
    registry.register_instance(
        iuserprompt.IUserPrompt,
        user_prompt.UserPrompt(send_request_to_wm),
    )
    # Stateless and config-free, so an instance is correct rather than a factory
    # (S-303 does not apply) and there is nothing to dispose.
    registry.register_instance(
        idataclasscodec.IDataclassCodec,
        dataclass_codec.DataclassCodec(),
    )
    registry.register_instance(ifilemanager.IFileManager, file_manager_instance)
    registry.register_instance(ifileeditor.IFileEditor, file_editor_instance)
    registry.register_instance(icache.ICache, cache_instance)
    registry.register_instance(
        iprojectactionrunner.IProjectActionRunner,
        project_action_runner.ProjectActionRunnerImpl(
            send_request_to_wm,
            run_action_func=functools.partial(
                run_action_service.run_action, runner_context=runner_context
            ),
            actions_getter=actions_getter,
            current_env_name_getter=current_env_name_getter,
        ),
    )
    registry.register_instance(
        iworkspaceactionrunner.IWorkspaceActionRunner,
        workspace_action_runner.WorkspaceActionRunnerImpl(send_request_to_wm),
    )
    registry.register_instance(
        iworkspaceactionregistry.IWorkspaceActionRegistry,
        workspace_action_registry.WorkspaceActionRegistryImpl(send_request_to_wm),
    )
    registry.register_instance(
        iworkspaceinfoprovider.IWorkspaceInfoProvider,
        workspace_info_provider.WorkspaceInfoProviderImpl(send_request_to_wm),
    )
    registry.register_instance(
        iknowledgestore.IKnowledgeStore,
        knowledge_store.KnowledgeStoreImpl(send_request_to_wm),
    )
    # registry.register_instance(idevenvinfoprovider.IDevEnvInfoProvider, dev_env_info_provider_instance)

    registry.register_factory(
        iprojectinfoprovider.IProjectInfoProvider,
        functools.partial(
            project_info_provider_factory,
            project_def_path_getter=project_def_path_getter,
            project_raw_config_getter=project_raw_config_getter,
            workspace_packages_getter=workspace_packages_getter,
            workspace_extra_selection_getter=workspace_extra_selection_getter,
            current_project_raw_config_version_getter=current_project_raw_config_version_getter,
        ),
    )
    registry.register_factory(
        iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
        functools.partial(
            extension_runner_info_provider_factory,
            cache_dir_path_getter=cache_dir_path_getter,
            current_env_name_getter=current_env_name_getter,
        ),
    )

    config_resolver = _build_service_config_resolver(
        service_declarations, service_config_overrides or {}
    )
    svc_registry = service_registry.ServiceRegistry(
        di_registry=registry, config_resolver=config_resolver
    )
    _register_command_runner_service(service_declarations, svc_registry)
    _register_repository_credentials_provider_service(
        service_declarations, svc_registry
    )
    all_eps, activated = _activate_extensions(handler_packages, svc_registry)
    _apply_user_service_config(
        [
            svc
            for svc in service_declarations
            if svc.interface
            not in (
                _COMMAND_RUNNER_INTERFACE,
                _REPOSITORY_CREDENTIALS_PROVIDER_INTERFACE,
            )
        ],
        svc_registry,
    )
    _report_service_config_override_problems(config_resolver)

    remaining = sorted(set(all_eps.keys()) - set(activated))
    if remaining:
        deferred = [
            _make_deferred_activator(pkg, all_eps[pkg], svc_registry)
            for pkg in remaining
        ]
        registry.set_deferred_activators(deferred)


def _activate_extensions(
    handler_packages: set[str],
    svc_registry: service_registry.ServiceRegistry,
) -> tuple[dict[str, importlib.metadata.EntryPoint], ordered_set.OrderedSet[str]]:
    all_eps = {
        ep.name: ep
        for ep in importlib.metadata.entry_points(group="finecode.activator")
    }
    logger.debug(f"Found activator entry points: {list(all_eps.keys())}")

    stale = _find_installed_packages_with_missing_eps(handler_packages, all_eps)
    if stale:
        raise StaleEntryPointsError(stale)

    packages_to_activate = _collect_activatable_packages(handler_packages, all_eps)
    logger.debug(
        f"Handler packages: {handler_packages}; packages to activate: {list(packages_to_activate)}"
    )

    for pkg_name in packages_to_activate:
        try:
            activator_cls = all_eps[pkg_name].load()
            activator_cls(registry=svc_registry).activate()
            logger.debug(f"Activated extension '{pkg_name}'")
        # Entry-point loading imports a third-party extension package and
        # activate() is its code; the reachable exception set is open.
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to activate extension '{pkg_name}': {e}")

    return all_eps, packages_to_activate


def _make_deferred_activator(
    pkg_name: str,
    ep: importlib.metadata.EntryPoint,
    svc_registry: service_registry.ServiceRegistry,
) -> Callable[[], None]:
    def activate() -> None:
        try:
            activator_cls = ep.load()
            activator_cls(registry=svc_registry).activate()
            logger.debug(f"On-demand activated extension '{pkg_name}'")
        # Entry-point loading imports a third-party extension package and
        # activate() is its code; the reachable exception set is open.
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to on-demand activate extension '{pkg_name}': {e}")

    return activate


def _find_installed_packages_with_missing_eps(
    handler_packages: set[str],
    all_eps: dict[str, importlib.metadata.EntryPoint],
) -> set[str]:
    """Return handler packages that are installed but have a stale activator entry point.

    For editable installs the pyproject.toml in the source directory is the source of
    truth: if it declares a ``finecode.activator`` entry point but the installed metadata
    doesn't expose it yet, the install is stale and the env needs to be reinstalled.

    For non-editable installs the installed metadata is authoritative — entry points are
    always registered at install time, so a missing entry point simply means the package
    doesn't define one.
    """
    missing = set()
    for pkg in handler_packages:
        if _normalize_pkg_name(pkg) in all_eps:
            continue
        try:
            dist = importlib.metadata.distribution(pkg)
        except importlib.metadata.PackageNotFoundError:
            continue  # not installed at all — different problem

        source_path = _get_editable_source_path(dist)
        if source_path is not None and _pyproject_has_activator_ep(source_path):
            missing.add(pkg)
        # Non-editable: installed metadata is authoritative; absence is intentional.
    return missing


def _get_editable_source_path(
    dist: importlib.metadata.Distribution,
) -> pathlib.Path | None:
    """Return the source directory for an editable install, or None if not editable."""
    direct_url_text = dist.read_text("direct_url.json")
    if not direct_url_text:
        return None
    try:
        data = json.loads(direct_url_text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not data.get("dir_info", {}).get("editable", False):
        return None
    url = data.get("url", "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "file":
        return None
    return pathlib.Path(urllib.request.url2pathname(parsed.path))


def _pyproject_has_activator_ep(source_path: pathlib.Path) -> bool:
    """Return True if pyproject.toml in source_path declares a finecode.activator entry point."""
    pyproject_path = source_path / "pyproject.toml"
    if not pyproject_path.exists():
        return False
    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return "finecode.activator" in data.get("project", {}).get("entry-points", {})


def _build_service_config_resolver(
    service_declarations: list[object],
    service_config_overrides: dict[str, dict[str, Any]],
) -> service_config.ServiceConfigResolver:
    """Index declared config by interface *type* so any binding can pick it up.

    Config-only entries (no ``source``) are indexed here and nowhere else: they
    carry configuration for a binding an activator owns, and must not register a
    binding of their own (ADR-0070).
    """
    declared_by_interface: dict[type, dict[str, Any]] = {}
    for svc in service_declarations:
        if svc.config is None:
            continue
        try:
            interface = import_module_member_by_source_str(svc.interface)
        # The import executes the interface module's top-level code, so the
        # reachable exception set is open and not enumerable here.
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to resolve service interface '{svc.interface}': {e}")
            continue
        existing = declared_by_interface.setdefault(interface, {})
        service_config.deep_merge_config(existing, svc.config)

    return service_config.ServiceConfigResolver(
        declared_config_by_interface=declared_by_interface,
        overrides_by_name=service_config_overrides,
    )


def _report_service_config_override_problems(
    config_resolver: service_config.ServiceConfigResolver,
) -> None:
    for name, interfaces in config_resolver.ambiguous_names().items():
        names = ", ".join(
            sorted(f"{i.__module__}.{i.__qualname__}" for i in interfaces)
        )
        logger.error(
            f"Service config override '{name}' is ambiguous: it matches {names}. "
            f"Rename one of the interfaces so the override addresses exactly one."
        )

    unmatched = config_resolver.unmatched_names()
    if unmatched:
        # Deferred activators register on first request, so a name may still be
        # claimed later; the override is applied if that happens. This is
        # reported because an unclaimed name is far more often a typo.
        logger.warning(
            f"Service config override(s) {', '.join(unmatched)} did not match any "
            f"service registered at startup. Check the name, or ignore this if the "
            f"service is provided by an activator that has not run yet."
        )


def _apply_user_service_config(
    service_declarations: list[object],
    svc_registry: service_registry.ServiceRegistry,
) -> None:
    for svc in service_declarations:
        if svc.source is None:
            # Config-only entry: its config reaches the binding through the
            # config resolver, and it must not bind anything itself.
            continue
        try:
            interface = import_module_member_by_source_str(svc.interface)
            impl_cls = import_module_member_by_source_str(svc.source)
            svc_registry.register_impl(interface, impl_cls)
            logger.trace(f"Configured service '{svc.source}' for '{svc.interface}'")
        # The imports execute user-configured modules' top-level code, so the
        # reachable exception set is open and not enumerable here.
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to configure service '{svc.source}': {e}")


def _register_command_runner_service(
    service_declarations: list[object],
    svc_registry: service_registry.ServiceRegistry,
) -> None:
    """Register the default binding for ``ICommandRunner``, merged with any
    project/user-declared override for the same interface.

    ``ICommandRunner`` used to be wired as a hardcoded ``registry.register_instance``
    call, bypassing the ``register_impl``/config-injection path entirely — see
    ADR-0056. Routing it through ``register_impl`` instead lets a project or
    personal ``finecode-user.toml`` declaration configure it (e.g.
    ``config.max_concurrent_processes``, used to bound ``prepare-envs``
    subprocess fan-out — ADR-0055) the same way any other service is rebound
    by declaring the same ``interface``.
    """
    override = next(
        (
            svc
            for svc in service_declarations
            if svc.interface == _COMMAND_RUNNER_INTERFACE
        ),
        None,
    )
    source = (
        override.source if override is not None else None
    ) or _COMMAND_RUNNER_DEFAULT_SOURCE

    try:
        interface = import_module_member_by_source_str(_COMMAND_RUNNER_INTERFACE)
        impl_cls = import_module_member_by_source_str(source)
        svc_registry.register_impl(interface, impl_cls)
        logger.trace(f"Configured service '{source}' for '{_COMMAND_RUNNER_INTERFACE}'")
    # `source` may be a user-declared override; the import executes its
    # top-level code, so the reachable exception set is open.
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to configure service '{source}': {e}")


def _register_repository_credentials_provider_service(
    service_declarations: list[object],
    svc_registry: service_registry.ServiceRegistry,
) -> None:
    """Register the default binding for ``IRepositoryCredentialsProvider``, merged
    with any project/user-declared override for the same interface.

    Mirrors ``_register_command_runner_service`` (ADR-0056). Provisioning data
    (registry definitions, credentials) is the concrete implementation's own
    config concern, not part of the universal interface — see ADR-0068. Routed
    through ``register_impl`` so the concrete impl class
    resolves to the same instance whether it is requested through the interface
    (the read-only consumers) or through the concrete type (the optional
    dynamic-runtime-seeding action's handler, see ``init_repository_provider``).
    """
    override = next(
        (
            svc
            for svc in service_declarations
            if svc.interface == _REPOSITORY_CREDENTIALS_PROVIDER_INTERFACE
        ),
        None,
    )
    source = (
        override.source if override is not None else None
    ) or _REPOSITORY_CREDENTIALS_PROVIDER_DEFAULT_SOURCE

    try:
        interface = import_module_member_by_source_str(
            _REPOSITORY_CREDENTIALS_PROVIDER_INTERFACE
        )
        impl_cls = import_module_member_by_source_str(source)
        # Config comes from the resolver (declaration config + env overrides).
        svc_registry.register_impl(interface, impl_cls)
        logger.trace(
            f"Configured service '{source}' for '{_REPOSITORY_CREDENTIALS_PROVIDER_INTERFACE}'"
        )
    # `source` may be a user-declared override; the import executes its
    # top-level code, so the reachable exception set is open.
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to configure service '{source}': {e}")


def _collect_activatable_packages(
    seed_packages: set[str],
    all_eps: dict[str, importlib.metadata.EntryPoint],
) -> ordered_set.OrderedSet[str]:
    """Expand seed_packages to include transitive deps that have activators."""
    result: ordered_set.OrderedSet[str] = ordered_set.OrderedSet([])
    visited: set[str] = set()
    queue = list(seed_packages)

    while queue:
        pkg = queue.pop()
        normalized = _normalize_pkg_name(pkg)
        if normalized in visited:
            continue
        visited.add(normalized)

        if normalized in all_eps:
            result.add(normalized)

        try:
            requires = importlib.metadata.requires(pkg) or []
        except importlib.metadata.PackageNotFoundError:
            continue

        for req_str in requires:
            dep_name = _parse_dep_name(req_str)
            dep_normalized = _normalize_pkg_name(dep_name)
            if dep_normalized not in visited and dep_normalized in all_eps:
                queue.append(dep_name)

    return result


def _normalize_pkg_name(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _parse_dep_name(req_str: str) -> str:
    # PEP 508: package name precedes any version specifier, extra marker, or whitespace
    return re.split(r"[\s>=<!~\(;]", req_str)[0]


def project_info_provider_factory(
    _,
    project_def_path_getter: Callable[[], pathlib.Path],
    project_raw_config_getter: Callable[
        [str], collections.abc.Awaitable[dict[str, Any]]
    ],
    current_project_raw_config_version_getter: Callable[[], int],
    workspace_packages_getter: Callable[
        [], collections.abc.Awaitable[dict[str, iprojectinfoprovider.WorkspacePackage]]
    ]
    | None = None,
    workspace_extra_selection_getter: Callable[
        [], collections.abc.Awaitable[dict[str, list[str]]]
    ]
    | None = None,
):
    return project_info_provider.ProjectInfoProvider(
        project_def_path_getter=project_def_path_getter,
        project_raw_config_getter=project_raw_config_getter,
        workspace_packages_getter=workspace_packages_getter,
        workspace_extra_selection_getter=workspace_extra_selection_getter,
        current_project_raw_config_version_getter=current_project_raw_config_version_getter,
    )


async def extension_runner_info_provider_factory(
    registry,
    cache_dir_path_getter: Callable[[], pathlib.Path],
    current_env_name_getter: Callable[[], str],
):
    logger = await registry.get_instance(ilogger.ILogger)
    return extension_runner_info_provider.ExtensionRunnerInfoProvider(
        cache_dir_path_getter=cache_dir_path_getter,
        logger=logger,
        current_env_name_getter=current_env_name_getter,
    )
